from __future__ import annotations

import json
import math
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from ..settings import settings
from .cancellation import CancellationError, CancellationToken


ProgressCallback = Callable[[float, str], None]


class VideoValidationError(ValueError):
    pass


def run_command(
    command: list[str],
    *,
    capture: bool = True,
    token: CancellationToken | None = None,
) -> subprocess.CompletedProcess:
    if token is not None and token.is_cancelled:
        raise CancellationError("任务已取消")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if token is not None:
        token.register(process)
    try:
        if token is None:
            stdout, stderr = process.communicate()
        else:
            while process.poll() is None:
                if token.is_cancelled:
                    token.terminate_and_wait(process)
                    raise CancellationError("任务已取消")
                time.sleep(0.1)
            stdout, stderr = process.communicate()
            if token.is_cancelled:
                raise CancellationError("任务已取消")
        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode, command, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if token is not None:
            token.unregister(process)


def probe_video(path: Path) -> dict[str, Any]:
    command = [
        settings.ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = run_command(command)
        payload = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as exc:
        raise VideoValidationError("无法读取视频，请确认文件没有损坏，并已安装 FFmpeg。") from exc

    video_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if not video_stream:
        raise VideoValidationError("文件中没有可识别的视频画面。")

    try:
        duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration"))
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (TypeError, ValueError, KeyError) as exc:
        raise VideoValidationError("无法读取视频时长或分辨率。") from exc

    frame_rate = video_stream.get("avg_frame_rate", "0/1")
    try:
        numerator, denominator = frame_rate.split("/")
        fps = float(numerator) / max(float(denominator), 1.0)
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    metadata = {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "video_codec": video_stream.get("codec_name", "unknown"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "has_audio": bool(audio_stream),
        "size": path.stat().st_size,
    }
    validate_video_metadata(path, metadata)
    return metadata


def validate_video_metadata(path: Path, metadata: dict[str, Any]) -> None:
    if path.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
        raise VideoValidationError("仅支持 MP4、MOV 和 MKV 文件。")
    if metadata["duration"] > settings.max_duration_seconds:
        raise VideoValidationError("视频超过2小时，暂不支持。")
    if metadata["width"] > settings.max_width or metadata["height"] > settings.max_height:
        raise VideoValidationError("视频超过1080P，请先降低到1920×1080或更低分辨率。")
    if metadata["width"] <= metadata["height"]:
        raise VideoValidationError("第一版仅支持横屏录像。")


def candidate_count_for_duration(duration: float, sensitivity: str = "balanced") -> int:
    # Search broadly first; sensitivity controls admission later in the pipeline.
    # Reducing recall in strict mode made it impossible for Qwen to recover missed highlights.
    base = 10 + int(math.ceil(duration / 300))
    multiplier = {"strict": 1.0, "balanced": 1.0, "broad": 1.15}.get(sensitivity, 1.0)
    return max(6, min(24, int(round(base * multiplier))))


def estimate_cost(duration: float, sensitivity: str = "balanced") -> dict[str, Any]:
    candidates = candidate_count_for_duration(duration, sensitivity)
    images = candidates * int(settings.qwen_candidate_seconds * 2)
    audio_tokens = candidates * settings.qwen_candidate_seconds * 12.5
    low_video_tokens = candidates * 8_000
    high_video_tokens = candidates * 16_384
    low_output_tokens = candidates * 180
    high_output_tokens = candidates * 600
    audio_cost = audio_tokens / 1_000_000 * settings.qwen_audio_input_cny_per_million
    low = (
        low_video_tokens / 1_000_000 * settings.qwen_video_input_cny_per_million
        + audio_cost
        + low_output_tokens / 1_000_000 * settings.qwen_text_output_cny_per_million
    )
    high = (
        high_video_tokens / 1_000_000 * settings.qwen_video_input_cny_per_million
        + audio_cost
        + high_output_tokens / 1_000_000 * settings.qwen_text_output_cny_per_million
    )
    return {
        "candidate_count": candidates,
        "image_count": images,
        "estimated_usd_low": round(low / settings.usd_to_cny, 4),
        "estimated_usd_high": round(high / settings.usd_to_cny, 4),
        "estimated_cny_low": round(low, 2),
        "estimated_cny_high": round(high, 2),
        "free_tier_may_be_zero": True,
        "model": settings.qwen_model,
    }


def _robust_normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    median = float(np.median(values))
    p90 = float(np.percentile(values, 90))
    scale = max(p90 - median, float(np.std(values)) * 0.5, 1e-6)
    return np.clip((values - median) / scale, -1.0, 4.0).astype(np.float32)


def extract_visual_signals(
    path: Path,
    duration: float,
    callback: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> dict[str, np.ndarray]:
    width, height = 384, 216
    frame_bytes = width * height
    command = [
        settings.ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        f"fps=1,scale={width}:{height}:flags=fast_bilinear,format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if token is not None:
        token.register(process)
    motion: list[float] = []
    luma: list[float] = []
    edge: list[float] = []
    previous: np.ndarray | None = None
    expected = max(1, int(math.ceil(duration)))

    try:
        assert process.stdout is not None
        while True:
            if token is not None and token.is_cancelled:
                token.terminate_and_wait(process)
                raise CancellationError("任务已取消")
            raw = process.stdout.read(frame_bytes)
            if len(raw) != frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)
            luma.append(float(frame.mean()))
            if previous is None:
                motion.append(0.0)
            else:
                motion.append(float(np.mean(np.abs(frame - previous))))
            horizontal = np.mean(np.abs(frame[:, 1:] - frame[:, :-1]))
            vertical = np.mean(np.abs(frame[1:, :] - frame[:-1, :]))
            edge.append(float((horizontal + vertical) / 2))
            previous = frame
            if callback and len(motion) % 30 == 0:
                callback(min(1.0, len(motion) / expected), f"正在扫描画面：{len(motion)} 秒")

        return_code = process.wait()
        if token is not None and token.is_cancelled:
            raise CancellationError("任务已取消")
        if return_code != 0 and not motion:
            stderr = process.stderr.read().decode("utf-8", errors="ignore") if process.stderr else ""
            raise RuntimeError(f"画面扫描失败：{stderr[-300:]}")

        return {
            "motion": np.asarray(motion, dtype=np.float32),
            "luma": np.asarray(luma, dtype=np.float32),
            "edge": np.asarray(edge, dtype=np.float32),
        }
    finally:
        if token is not None:
            token.unregister(process)


def extract_audio_signals(
    path: Path,
    duration: float,
    callback: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> np.ndarray:
    sample_rate = 8000
    bytes_per_second = sample_rate * 2
    command = [
        settings.ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if token is not None:
        token.register(process)
    rms_values: list[float] = []
    expected = max(1, int(math.ceil(duration)))
    try:
        assert process.stdout is not None
        while True:
            if token is not None and token.is_cancelled:
                token.terminate_and_wait(process)
                raise CancellationError("任务已取消")
            raw = process.stdout.read(bytes_per_second)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size:
                rms_values.append(float(np.sqrt(np.mean(samples * samples) + 1e-12)))
            if callback and len(rms_values) % 60 == 0:
                callback(min(1.0, len(rms_values) / expected), f"正在分析声音：{len(rms_values)} 秒")
        process.wait()
        if token is not None and token.is_cancelled:
            raise CancellationError("任务已取消")
        return np.asarray(rms_values, dtype=np.float32)
    finally:
        if token is not None:
            token.unregister(process)


def rank_local_candidates(
    path: Path,
    duration: float,
    sensitivity: str,
    callback: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> list[dict[str, float]]:
    # Video and audio are independent lightweight scans. Running them together
    # avoids making long recordings wait for two full sequential passes.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="local-scan") as executor:
        visual_future = executor.submit(extract_visual_signals, path, duration, callback, token)
        audio_future = executor.submit(extract_audio_signals, path, duration, None, token)
        visual = visual_future.result()
        audio = audio_future.result()
    count = min(len(visual["motion"]), len(audio) if len(audio) else len(visual["motion"]))
    if count == 0:
        return [{"center": duration / 2, "local_score": 0.5}]

    motion = visual["motion"][:count]
    luma = visual["luma"][:count]
    edge = visual["edge"][:count]
    audio_values = audio[:count] if len(audio) else np.zeros(count, dtype=np.float32)
    flash = np.abs(np.diff(luma, prepend=luma[:1]))

    score = (
        0.38 * _robust_normalize(motion)
        + 0.34 * _robust_normalize(audio_values)
        + 0.16 * _robust_normalize(flash)
        + 0.12 * _robust_normalize(edge)
    )
    kernel = np.ones(7, dtype=np.float32) / 7.0
    smoothed = np.convolve(score, kernel, mode="same")

    desired = candidate_count_for_duration(duration, sensitivity)
    minimum_gap = {"strict": 22, "balanced": 20, "broad": 16}.get(sensitivity, 20)
    order = np.argsort(smoothed)[::-1]
    selected: list[int] = []
    for index in order:
        second = int(index)
        if second < 8 or second > duration - 8:
            continue
        if all(abs(second - existing) >= minimum_gap for existing in selected):
            selected.append(second)
        if len(selected) >= desired:
            break

    if not selected:
        selected = [int(max(1, duration / 2))]
    selected_values = [float(smoothed[index]) for index in selected]
    minimum = min(selected_values)
    maximum = max(selected_values)
    spread = max(maximum - minimum, 1e-6)
    return [
        {
            "center": float(index),
            "local_score": round(0.35 + 0.65 * ((float(smoothed[index]) - minimum) / spread), 4),
        }
        for index in selected
    ]


def extract_candidate_frames(
    source: Path,
    candidate_dir: Path,
    center: float,
    source_duration: float,
    context_duration: float = 45.0,
    frame_count: int = 8,
) -> tuple[list[Path], float, float]:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    start = max(0.0, min(center - context_duration * 0.45, max(0.0, source_duration - context_duration)))
    actual_duration = min(context_duration, source_duration - start)
    pattern = candidate_dir / "frame_%02d.jpg"
    command = [
        settings.ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{actual_duration:.3f}",
        "-i",
        str(source),
        "-vf",
        f"fps={frame_count}/{max(actual_duration, 1):.3f},scale=384:216:flags=lanczos",
        "-frames:v",
        str(frame_count),
        "-q:v",
        "3",
        str(pattern),
    ]
    run_command(command, capture=False)
    return sorted(candidate_dir.glob("frame_*.jpg")), start, actual_duration


def candidate_window(
    center: float,
    source_duration: float,
    context_duration: float | None = None,
) -> tuple[float, float]:
    requested = context_duration or settings.qwen_candidate_seconds
    start = max(0.0, min(center - requested * 0.45, max(0.0, source_duration - requested)))
    return start, min(requested, source_duration - start)


def render_analysis_proxy(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    token: CancellationToken | None = None,
) -> Path:
    """Create a small MP4 that stays below Qwen's Base64 request limit."""
    output.parent.mkdir(parents=True, exist_ok=True)
    profiles = [
        ("scale=1280:-2:force_original_aspect_ratio=decrease,fps=15", "1250k", "1400k"),
        ("scale=960:-2:force_original_aspect_ratio=decrease,fps=12", "900k", "950k"),
        ("scale=768:-2:force_original_aspect_ratio=decrease,fps=10", "520k", "580k"),
    ]
    for video_filter, bitrate, maxrate in profiles:
        command = [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            bitrate,
            "-maxrate",
            maxrate,
            "-bufsize",
            "1900k",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            str(output),
        ]
        run_command(command, capture=False, token=token)
        if output.stat().st_size <= settings.qwen_proxy_max_bytes:
            return output
    raise RuntimeError("候选视频压缩后仍超过7MB，请降低输入视频码率后重试。")


def render_clip(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    token: CancellationToken | None = None,
    *,
    inject_silence: bool = False,
    normalize: tuple[int, int, float] | None = None,
) -> None:
    """Cut one clip from ``source``.

    ``inject_silence`` adds a silent stereo track for sources without audio so
    the clip stays concat-compatible with clips that do have audio. ``normalize``
    (target_width, target_height, target_fps) re-encodes the clip to a uniform
    format when multiple recordings feed the same montage.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        settings.ffmpeg,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
    ]
    if inject_silence:
        command.extend(["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"])
    command.extend(["-map", "0:v:0", "-map", "1:a" if inject_silence else "0:a?"])
    if normalize:
        target_width, target_height, target_fps = normalize
        command.extend(
            [
                "-vf",
                ",".join(
                    [
                        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease",
                        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black",
                        "setsar=1",
                        f"fps={target_fps:.3f}",
                    ]
                ),
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    )
    if normalize or inject_silence:
        command.extend(["-ar", "48000", "-ac", "2"])
    command.extend(["-movflags", "+faststart", str(output)])
    run_command(command, capture=False, token=token)


def _diagonal_stripe_expression(direction: str) -> str:
    """Build a soft-edged diagonal stripe wipe for FFmpeg's custom xfade."""
    coordinate = "(W-X)+0.35*Y" if direction == "right" else "X+0.35*Y"
    stripe_coordinate = "(W-X)+0.65*Y" if direction == "right" else "X+0.65*Y"
    band = "0.09*W"
    sweep = f"P*(W+0.35*H+2*({band}))-({band})"
    delta = f"({sweep})-({coordinate})"
    return (
        f"if(lte({delta},-({band})),A,"
        f"if(gte({delta},{band}),B,"
        f"if(lt(mod({stripe_coordinate},0.05*W),0.029*W),B,A)))"
    )


def render_montage(
    output: Path,
    segments: list[dict],
    transition_duration: float = 0.0,
    token: CancellationToken | None = None,
    sources_info: dict[str, dict] | None = None,
) -> dict[str, float | int | str]:
    """Render one highlight reel from per-segment sources.

    Each segment must carry its own ``source`` path (segments may come from
    different recordings). ``sources_info`` maps each source path to its probe
    metadata (width/height/fps/has_audio); when segments span multiple sources
    every input is normalized to the smallest shared resolution and frame rate
    so xfade/acrossfade keep working.
    """
    if not segments:
        raise ValueError("至少需要一个高光片段。")
    output.parent.mkdir(parents=True, exist_ok=True)

    def info_for(source: str) -> dict:
        return (sources_info or {}).get(source) or {
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "has_audio": True,
        }

    segment_durations = [float(segment["duration"]) for segment in segments]
    if len(segments) == 1:
        segment = segments[0]
        render_clip(
            Path(segment["source"]),
            output,
            float(segment["start"]),
            float(segment["duration"]),
            token=token,
        )
        duration = float(probe_video(output)["duration"])
        return {
            "duration": duration,
            "transition_count": 0,
            "transition_duration": 0.0,
            "transition_style": "none",
        }

    used_sources = {str(segment["source"]) for segment in segments}
    infos = {source: info_for(source) for source in used_sources}
    multi_source = len(used_sources) > 1
    has_audio_any = any(bool(info["has_audio"]) for info in infos.values())
    normalize: tuple[int, int, float] | None = None
    if multi_source:
        target_width = min(int(info["width"]) for info in infos.values())
        target_height = min(int(info["height"]) for info in infos.values())
        fps_values = [float(info.get("fps") or 0) for info in infos.values()]
        target_fps = min(value for value in fps_values if value > 0) if any(value > 0 for value in fps_values) else 30.0
        normalize = (target_width, target_height, target_fps)

    requested_transition = max(0.0, float(transition_duration))
    effective_transition = min(requested_transition, min(segment_durations) * 0.45)

    if effective_transition >= 0.1:
        # Seek each source input directly and encode the finished timeline once.
        # The previous implementation encoded every part and then encoded the
        # montage again, which doubled the slowest part of the render stage.
        command = [settings.ffmpeg, "-y", "-v", "error"]
        segment_video_inputs: list[int] = []
        next_input = 0
        for segment in segments:
            command.extend(
                [
                    "-ss",
                    f"{float(segment['start']):.3f}",
                    "-t",
                    f"{float(segment['duration']):.3f}",
                    "-i",
                    str(segment["source"]),
                ]
            )
            segment_video_inputs.append(next_input)
            next_input += 1

        # Stand-in silent inputs keep the acrossfade chain valid when some
        # recordings have an audio track and others do not.
        segment_audio_inputs: list[int | None] = []
        if has_audio_any:
            for segment in segments:
                if bool(infos[str(segment["source"])]["has_audio"]):
                    segment_audio_inputs.append(None)
                else:
                    command.extend(
                        [
                            "-f",
                            "lavfi",
                            "-t",
                            f"{float(segment['duration']):.3f}",
                            "-i",
                            "anullsrc=r=48000:cl=stereo",
                        ]
                    )
                    segment_audio_inputs.append(next_input)
                    next_input += 1

        filters: list[str] = []
        for index in range(len(segments)):
            video_input = segment_video_inputs[index]
            if multi_source:
                filters.append(
                    f"[{video_input}:v]settb=AVTB,setpts=PTS-STARTPTS,"
                    f"scale={normalize[0]}:{normalize[1]}:force_original_aspect_ratio=decrease,"
                    f"pad={normalize[0]}:{normalize[1]}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"setsar=1,fps={normalize[2]:.3f},format=yuv420p[v{index}]"
                )
            else:
                filters.append(f"[{video_input}:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{index}]")

        video_label = "v0"
        timeline_duration = segment_durations[0]
        for index in range(1, len(segments)):
            output_label = f"vx{index}"
            offset = max(0.0, timeline_duration - effective_transition)
            direction = "left" if index % 2 else "right"
            expression = _diagonal_stripe_expression(direction)
            filters.append(
                f"[{video_label}][v{index}]xfade=transition=custom:"
                f"duration={effective_transition:.3f}:offset={offset:.3f}:"
                f"expr='{expression}'[{output_label}]"
            )
            video_label = output_label
            timeline_duration += segment_durations[index] - effective_transition

        audio_label = ""
        if has_audio_any:
            for index in range(len(segments)):
                silent_input = segment_audio_inputs[index]
                if silent_input is None:
                    audio_input = segment_video_inputs[index]
                    if multi_source:
                        filters.append(
                            f"[{audio_input}:a]aresample=48000,"
                            f"aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]"
                        )
                    else:
                        filters.append(f"[{audio_input}:a]asetpts=PTS-STARTPTS[a{index}]")
                else:
                    filters.append(f"[{silent_input}:a]asetpts=PTS-STARTPTS[a{index}]")
            audio_label = "a0"
            for index in range(1, len(segments)):
                output_label = f"ax{index}"
                filters.append(
                    f"[{audio_label}][a{index}]acrossfade=d={effective_transition:.3f}:"
                    f"c1=qsin:c2=qsin[{output_label}]"
                )
                audio_label = output_label

        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{video_label}]"])
        if has_audio_any:
            command.extend(["-map", f"[{audio_label}]"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if has_audio_any:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(["-shortest", "-movflags", "+faststart", str(output)])
        run_command(command, capture=False, token=token)
        return {
            "duration": float(probe_video(output)["duration"]),
            "transition_count": len(segments) - 1,
            "transition_duration": round(effective_transition, 3),
            "transition_style": "diagonal_stripes",
        }

    parts_dir = output.parent / "montage_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_paths: list[Path] = []
    for index, segment in enumerate(segments):
        part_path = parts_dir / f"part_{index:03d}.mp4"
        render_clip(
            Path(segment["source"]),
            part_path,
            float(segment["start"]),
            float(segment["duration"]),
            token=token,
            inject_silence=has_audio_any and not bool(infos[str(segment["source"])]["has_audio"]),
            normalize=normalize,
        )
        part_paths.append(part_path)

    concat_file = parts_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in part_paths),
        encoding="utf-8",
    )
    run_command(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture=False,
        token=token,
    )
    return {
        "duration": float(probe_video(output)["duration"]),
        "transition_count": 0,
        "transition_duration": 0.0,
        "transition_style": "none",
    }
