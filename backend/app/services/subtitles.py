from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..settings import settings
from .video import probe_video, run_command


log = logging.getLogger(__name__)
_model = None
_model_lock = threading.Lock()


def _whisper_base_cached() -> bool:
    cache_root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return (Path(cache_root) / "whisper" / "base.pt").is_file()


def _load_whisper():
    global _model
    with _model_lock:
        if _model is None:
            import whisper

            _model = whisper.load_model("base")
    return _model


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    )
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap_caption(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for character in re.sub(r"\s+", " ", text.strip()):
        candidate = f"{line}{character}"
        if not line or draw.textlength(candidate, font=font) <= max_width:
            line = candidate
            continue
        lines.append(line.strip())
        line = character.lstrip()
    if line.strip():
        lines.append(line.strip())
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = f"{lines[-1].rstrip('…')}…"
    return lines


def create_caption_images(
    segments: list[dict], work_dir: Path, style: str, width: int, height: int
) -> list[tuple[Path, float, float]]:
    style_settings = {
        "gaming": {"fill": "white", "stroke": "black", "size": 0.054, "stroke_width": 0.004, "background": None},
        "minimal": {"fill": "white", "stroke": "black", "size": 0.045, "stroke_width": 0.002, "background": (0, 0, 0, 145)},
        "neon": {"fill": (224, 255, 0, 255), "stroke": (32, 16, 56, 255), "size": 0.052, "stroke_width": 0.004, "background": (20, 10, 34, 150)},
    }
    selected = style_settings.get(style, style_settings["gaming"])
    font = _load_font(max(24, round(height * selected["size"])))
    stroke_width = max(1, round(height * selected["stroke_width"]))
    max_text_width = round(width * 0.82)
    margin_bottom = round(height * 0.07)
    line_gap = max(6, round(height * 0.008))
    padding_x = max(18, round(width * 0.016))
    padding_y = max(10, round(height * 0.010))
    output: list[tuple[Path, float, float]] = []

    for index, segment in enumerate(segments):
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        lines = _wrap_caption(draw, str(segment.get("text", "")), font, max_text_width)
        if not lines:
            continue
        bboxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width) for line in lines]
        line_heights = [box[3] - box[1] for box in bboxes]
        block_height = sum(line_heights) + line_gap * (len(lines) - 1)
        y = height - margin_bottom - block_height
        background = selected["background"]
        if background:
            widest = max(box[2] - box[0] for box in bboxes)
            left = (width - widest) // 2 - padding_x
            top = y - padding_y
            right = (width + widest) // 2 + padding_x
            bottom = y + block_height + padding_y
            draw.rounded_rectangle((left, top, right, bottom), radius=padding_y, fill=background)
        for line, box, line_height in zip(lines, bboxes, line_heights, strict=True):
            line_width = box[2] - box[0]
            x = (width - line_width) / 2
            draw.text(
                (x, y),
                line,
                font=font,
                fill=selected["fill"],
                stroke_width=stroke_width,
                stroke_fill=selected["stroke"],
            )
            y += line_height + line_gap
        path = work_dir / f"caption-{index:03d}.png"
        canvas.save(path, optimize=True)
        output.append((path, float(segment["start"]), float(segment["end"])))
    return output


def transcribe_and_burn(
    input_clip: Path,
    output_clip: Path,
    work_dir: Path,
    style: str,
    message_cb=None,
    token=None,
) -> dict:
    wav_path = work_dir / "speech.wav"
    run_command(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(input_clip),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture=False,
        token=token,
    )
    if token is not None:
        token.raise_if_cancelled()
    if message_cb:
        if _whisper_base_cached():
            message_cb("正在加载语音识别模型")
        else:
            message_cb("首次使用需下载语音识别模型，可能需要几分钟，请耐心等待")
    model = _load_whisper()
    if message_cb:
        message_cb("语音模型已就绪，正在识别字幕")
    result = model.transcribe(str(wav_path), fp16=False, verbose=False)
    segments = [
        {"start": float(item["start"]), "end": float(item["end"]), "text": str(item["text"])}
        for item in result.get("segments", [])
        if str(item.get("text", "")).strip()
    ]
    if not segments:
        input_clip.replace(output_clip)
        return {"language": result.get("language"), "segment_count": 0, "burned": False}

    metadata = probe_video(input_clip)
    caption_images = create_caption_images(
        segments, work_dir, style, int(metadata["width"]), int(metadata["height"])
    )
    command: list[str] = [
        settings.ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(input_clip),
    ]
    for image_path, _, _ in caption_images:
        command.extend(["-loop", "1", "-framerate", "1", "-i", str(image_path)])

    filter_parts: list[str] = []
    current = "[0:v]"
    for index, (_, start, end) in enumerate(caption_images, start=1):
        output_label = f"[captioned{index}]"
        filter_parts.append(
            f"{current}[{index}:v]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'{output_label}"
        )
        current = output_label
    command.extend(
        [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        current,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-t",
        str(metadata["duration"]),
        "-movflags",
        "+faststart",
        str(output_clip),
        ]
    )
    if token is not None:
        token.raise_if_cancelled()
    try:
        run_command(command, capture=False, token=token)
    except subprocess.CalledProcessError:
        log.exception("字幕烧录失败，保留无字幕成片")
        input_clip.replace(output_clip)
        return {"language": result.get("language"), "segment_count": len(segments), "burned": False}
    input_clip.unlink(missing_ok=True)
    return {"language": result.get("language"), "segment_count": len(segments), "burned": True}
