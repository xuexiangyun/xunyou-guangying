from __future__ import annotations

import logging
import math
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from ..database import Database
from ..settings import OUTPUT_DIR, TEMP_DIR, settings
from .cancellation import CancellationError, CancellationToken
from .qwen import analyze_candidate, is_authentication_error, qwen_error_message, verify_candidate
from .subtitles import transcribe_and_burn
from .video import candidate_window, rank_local_candidates, render_analysis_proxy, render_montage


log = logging.getLogger(__name__)
MIN_QWEN_HIGHLIGHT_SCORE = 50.0
SENSITIVITY_SCORE_THRESHOLDS = {"strict": 70.0, "balanced": 60.0, "broad": 50.0}
SENSITIVITY_CONFIDENCE_THRESHOLDS = {"strict": 0.72, "balanced": 0.58, "broad": 0.42}
SMART_SUBTITLE_CATEGORIES = {
    "reaction",
    "funny",
    "teamfight",
    "unexpected",
    "clutch",
    "escape",
    "victory",
    "utility",
    "rescue",
    "boss",
}


class PipelineError(RuntimeError):
    pass


def _target_duration(mode: str, source_duration: float) -> float:
    targets = {
        "seconds_30": 30.0,
        "seconds_60": 60.0,
        "seconds_90": 90.0,
        "seconds_120": 120.0,
        # Legacy mappings for tasks saved by the previous version.
        "short": 30.0,
        "auto": 60.0,
        "long": 60.0,
    }
    return max(1.0, min(targets.get(mode, 60.0), source_duration))


def _subtitle_mode(options: dict) -> str:
    mode = str(options.get("subtitle_mode") or "").strip()
    if mode in {"smart", "off", "always"}:
        return mode
    if "subtitles" in options:
        return "always" if bool(options.get("subtitles")) else "off"
    return "smart"


def _should_generate_subtitles(options: dict, montage_plan: list[dict], has_audio: bool) -> tuple[bool, str]:
    mode = _subtitle_mode(options)
    if not has_audio:
        return False, "原视频没有音轨"
    if mode == "off":
        return False, "已关闭字幕"
    if mode == "always":
        return True, "已选择始终开启字幕"

    for segment in montage_plan:
        speech_value = float(segment.get("speech_value", 0))
        categories = {str(segment.get("category", "")), *map(str, segment.get("secondary_categories", []))}
        if speech_value >= 60:
            return True, str(segment.get("speech_reason") or "检测到对高光有价值的语音")
        if speech_value >= 35 and categories & SMART_SUBTITLE_CATEGORIES:
            return True, str(segment.get("speech_reason") or "语音有助于理解这段高光")
    return False, "以枪法和操作画面为主，没有检测到值得烧录的语音"


def _safe_number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _looks_like_rainbow_six(value: object) -> bool:
    text = str(value or "").lower()
    return any(alias in text for alias in ("彩虹六号", "彩六", "rainbow six", "rainbow 6", "r6 siege", "r6s"))


def _qualified_candidates(
    analyzed: list[dict],
    sensitivity: str,
    *,
    qwen_enabled: bool,
) -> list[dict]:
    if not qwen_enabled:
        return [item for item in analyzed if item.get("category") != "none"]

    score_threshold = SENSITIVITY_SCORE_THRESHOLDS.get(sensitivity, 60.0)
    confidence_threshold = SENSITIVITY_CONFIDENCE_THRESHOLDS.get(sensitivity, 0.58)
    return [
        item
        for item in analyzed
        if item.get("provider") == "qwen"
        and item.get("category") != "none"
        and _safe_number(item.get("score"), 0.0) >= score_threshold
        and _safe_number(item.get("confidence"), 0.0) >= confidence_threshold
        and (
            not isinstance(item.get("rainbow_six"), dict)
            or bool((item.get("verification") or {}).get("reviewed"))
        )
    ]


def _candidate_bounds(item: dict, source_duration: float) -> tuple[float, float, float]:
    context_start = _safe_number(item.get("context_start"), 0.0)
    context_duration = max(0.0, _safe_number(item.get("context_duration"), 30.0))
    event_time = max(0.0, min(context_duration, _safe_number(item.get("event_time"), context_duration / 2)))
    recommended = max(6.0, min(20.0, _safe_number(item.get("recommended_duration"), 12.0)))

    clip_start = _safe_number(item.get("clip_start"), event_time - recommended * 0.55)
    clip_end = _safe_number(item.get("clip_end"), event_time + recommended * 0.45)
    clip_start = max(0.0, min(context_duration, clip_start))
    clip_end = max(0.0, min(context_duration, clip_end))
    death_safe_trimmed = bool(item.get("death_safe_trimmed"))
    if not death_safe_trimmed and (
        clip_end <= clip_start or event_time < clip_start or event_time > clip_end
    ):
        clip_start = event_time - recommended * 0.55
        clip_end = event_time + recommended * 0.45

    duration = max(0.0, min(20.0, clip_end - clip_start))
    if not death_safe_trimmed and duration < min(6.0, source_duration):
        duration = min(6.0, source_duration)
        clip_start = event_time - duration * 0.55
        clip_start = max(0.0, min(clip_start, max(0.0, context_duration - duration)))
    start = context_start + clip_start
    start = max(0.0, min(start, max(0.0, source_duration - duration)))
    duration = max(0.0, min(duration, source_duration - start))
    event_absolute = max(0.0, min(source_duration, context_start + event_time))
    return round(start, 3), round(duration, 3), round(event_absolute, 3)


def _build_montage_plan(
    analyzed: list[dict],
    target_duration: float,
    source_duration: float,
    transition_duration: float = 0.0,
) -> list[dict]:
    if not analyzed:
        return []
    ranked = sorted(analyzed, key=lambda item: _safe_number(item.get("score"), 0.0), reverse=True)
    selected: list[tuple[dict, float, float, float]] = []
    timeline_duration = 0.0
    transition = max(0.0, float(transition_duration))

    for item in ranked:
        start, duration, event_absolute = _candidate_bounds(item, source_duration)
        if duration <= 0:
            continue
        if any(
            abs(event_absolute - existing_event) < max(10.0, min(20.0, (duration + existing_duration) * 0.55))
            for _, _, existing_duration, existing_event in selected
        ):
            continue
        projected = duration if not selected else timeline_duration + duration - min(transition, duration * 0.45)
        if selected and projected > target_duration + 0.25:
            continue
        selected.append((item, start, duration, event_absolute))
        timeline_duration = projected

    if not selected:
        item = ranked[0]
        start, duration, event_absolute = _candidate_bounds(item, source_duration)
        selected = [(item, start, min(duration, target_duration), event_absolute)]

    plan: list[dict] = []
    for item, start, duration, event_absolute in selected:
        segment = {
            "start": round(start, 3),
            "duration": round(duration, 3),
            "score": float(item["score"]),
            "title": str(item["title"]),
            "category": str(item["category"]),
            "category_label": str(item.get("category_label", item["category"])),
            "secondary_categories": list(item.get("secondary_categories", [])),
            "confidence": float(item.get("confidence", 0.0)),
            "content_type": str(item.get("content_type", "unknown")),
            "evidence": list(item.get("evidence", [])),
            "evidence_timestamps": list(item.get("evidence_timestamps", [])),
            "speech_value": float(item.get("speech_value", 0)),
            "speech_reason": str(item.get("speech_reason", "")),
            "reason": str(item["reason"]),
            "provider": str(item["provider"]),
            "event_absolute": event_absolute,
            "recommended_duration": _safe_number(item.get("recommended_duration"), duration),
        }
        if isinstance(item.get("verification"), dict):
            segment["verification"] = dict(item["verification"])
        if isinstance(item.get("rainbow_six"), dict):
            segment["rainbow_six"] = dict(item["rainbow_six"])
        plan.append(segment)
    return sorted(plan, key=lambda item: float(item["start"]))


def run_pipeline(
    *,
    database: Database,
    job_id: str,
    source: Path,
    metadata: dict,
    options: dict,
    qwen_api_key: str,
    qwen_api_host: str,
    cancel_token: CancellationToken | None = None,
) -> None:
    work_dir = TEMP_DIR / job_id
    output_dir = OUTPUT_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    def update(stage: str, progress: float, message: str) -> None:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        database.update_job(
            job_id,
            state="running",
            stage=stage,
            progress=max(0.0, min(1.0, progress)),
            message=message,
        )

    try:
        update("local_analysis", 0.05, "正在本地扫描声音和画面")

        def local_progress(value: float, message: str) -> None:
            update("local_analysis", 0.05 + value * 0.28, message)

        candidates = rank_local_candidates(
            source,
            float(metadata["duration"]),
            str(options["sensitivity"]),
            local_progress,
            cancel_token,
        )
        target_duration = _target_duration(str(options["duration_mode"]), float(metadata["duration"]))
        if qwen_api_key:
            desired_segments = max(1, math.ceil(target_duration / 12.0))
            analysis_budget = max(12, desired_segments * 2)
            if str(options.get("sensitivity")) == "broad":
                analysis_budget += 2
            candidates = candidates[: min(len(candidates), analysis_budget)]
        update("candidate_video", 0.35, f"找到 {len(candidates)} 个候选高光")

        analyzed: list[dict] = []
        qwen_failures: list[str] = []
        prepared: list[tuple[Path, dict]] = []
        for index, candidate in enumerate(candidates):
            candidate_dir = work_dir / f"candidate_{index:02d}"
            context_start, context_duration = candidate_window(
                candidate["center"],
                float(metadata["duration"]),
                settings.qwen_candidate_seconds,
            )
            result = {
                **candidate,
                "context_start": context_start,
                "context_duration": context_duration,
                "score": round(candidate["local_score"] * 60, 2),
                "category": "skill",
                "category_label": "高水平操作",
                "secondary_categories": [],
                "confidence": 0.0,
                "evidence": [],
                "speech_value": 0,
                "speech_reason": "本地筛选无法判断语音价值",
                "detected_game": "FPS游戏",
                "detected_mode": "未知模式",
                "title": "FPS高光时刻",
                "reason": "本地声音与画面强度最高",
                "event_time": context_duration * 0.45,
                "recommended_duration": 12.0,
                "provider": "local",
            }
            prepared.append((candidate_dir, result))

        if qwen_api_key:
            total = len(prepared)
            workers = max(1, min(settings.qwen_parallel_workers, total))
            update("qwen_analysis", 0.38, f"千问正在{workers}路并行理解 {total} 个候选视频")

            def analyze_prepared(index: int) -> dict:
                candidate_dir, result = prepared[index]
                proxy_path = render_analysis_proxy(
                    source,
                    candidate_dir / "qwen_candidate.mp4",
                    float(result["context_start"]),
                    float(result["context_duration"]),
                    cancel_token,
                )
                preliminary = analyze_candidate(
                    qwen_api_key,
                    qwen_api_host,
                    proxy_path,
                    list(options["highlight_types"]),
                    float(result["local_score"]),
                    float(result["context_duration"]),
                    str(options.get("game", "auto")),
                    str(options.get("game_mode", "auto")),
                )
                selected_game = str(options.get("game", "auto"))
                is_rainbow_six = selected_game == "rainbow_six_siege" or (
                    selected_game == "auto" and _looks_like_rainbow_six(preliminary.get("detected_game"))
                )
                if (
                    is_rainbow_six
                    and preliminary.get("category") != "none"
                    and _safe_number(preliminary.get("score"), 0.0) >= MIN_QWEN_HIGHLIGHT_SCORE
                ):
                    return verify_candidate(
                        qwen_api_key,
                        qwen_api_host,
                        proxy_path,
                        preliminary,
                        list(options["highlight_types"]),
                        float(result["context_duration"]),
                        selected_game,
                        str(options.get("game_mode", "auto")),
                    )
                return preliminary

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qwen-candidate") as executor:
                future_indexes = {
                    executor.submit(analyze_prepared, index): index for index in range(total)
                }
                completed = 0
                for future in as_completed(future_indexes):
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    index = future_indexes[future]
                    _, result = prepared[index]
                    try:
                        result.update(future.result())
                        result["provider"] = "qwen"
                    except Exception as exc:
                        if is_authentication_error(exc):
                            for pending in future_indexes:
                                pending.cancel()
                            raise PipelineError(qwen_error_message(exc)) from exc
                        qwen_failures.append(str(exc))
                        log.exception("Qwen candidate analysis failed")
                    completed += 1
                    update(
                        "qwen_analysis",
                        0.38 + (completed / max(total, 1)) * 0.32,
                        f"千问并行理解：已完成 {completed}/{total} 个候选",
                    )
            analyzed = [result for _, result in prepared]
        else:
            for index, (_, result) in enumerate(prepared):
                analyzed.append(result)
                update(
                    "qwen_analysis",
                    0.38 + ((index + 1) / max(len(prepared), 1)) * 0.32,
                    f"正在整理候选片段 {index + 1}/{len(prepared)}",
                )

        if not analyzed:
            raise PipelineError("没有成功生成候选片段，请换一个视频重试。")
        if qwen_api_key and len(qwen_failures) == len(analyzed):
            raise PipelineError(f"千问分析全部失败：{qwen_failures[0][:180]}")

        qualified = _qualified_candidates(
            analyzed,
            str(options.get("sensitivity", "balanced")),
            qwen_enabled=bool(qwen_api_key),
        )
        if not qualified:
            threshold = SENSITIVITY_SCORE_THRESHOLDS.get(str(options.get("sensitivity")), 60.0)
            raise PipelineError(
                f"千问没有找到达到{threshold:.0f}分且证据完整的高光，"
                "可以改用均衡/宽松模式，或选择其他录像。"
            )

        best = max(qualified, key=lambda item: float(item["score"]))
        montage_plan = _build_montage_plan(
            qualified,
            target_duration,
            float(metadata["duration"]),
            settings.transition_seconds,
        )
        if not montage_plan:
            raise PipelineError("没有足够的高光片段可以生成成片。")

        update("render", 0.74, f"正在合成 {len(montage_plan)} 段高光并加入快速转场")
        raw_clip = work_dir / "highlight_raw.mp4"
        final_clip = output_dir / "瞬游光影_最佳高光.mp4"
        render_result = render_montage(
            source,
            raw_clip,
            montage_plan,
            transition_duration=settings.transition_seconds,
            token=cancel_token,
        )

        subtitle_mode = _subtitle_mode(options)
        should_subtitle, subtitle_reason = _should_generate_subtitles(
            options, montage_plan, bool(metadata.get("has_audio"))
        )
        subtitle_result = {
            "mode": subtitle_mode,
            "burned": False,
            "segment_count": 0,
            "language": None,
            "decision": subtitle_reason,
        }
        if should_subtitle:
            update("subtitles", 0.84, "正在准备语音识别模型")
            try:
                generated_subtitles = transcribe_and_burn(
                    raw_clip,
                    final_clip,
                    work_dir,
                    str(options["subtitle_style"]),
                    message_cb=lambda message: update("subtitles", 0.84, message),
                    token=cancel_token,
                )
                subtitle_result.update(generated_subtitles)
            except Exception as exc:
                log.exception("Subtitle generation failed")
                raw_clip.replace(final_clip)
                subtitle_result["warning"] = str(exc)
        else:
            if subtitle_mode == "smart" and bool(metadata.get("has_audio")):
                update("subtitles", 0.90, "智能字幕判断完成：这条成片无需字幕")
            raw_clip.replace(final_clip)

        result = {
            "filename": final_clip.name,
            "output_path": str(final_clip),
            "download_url": f"/api/jobs/{job_id}/download",
            "video_url": f"/api/jobs/{job_id}/video",
            "title": best["title"],
            "category": best["category"],
            "category_label": best.get("category_label", best["category"]),
            "secondary_categories": best.get("secondary_categories", []),
            "confidence": best.get("confidence", 0.0),
            "evidence": best.get("evidence", []),
            "reason": best["reason"],
            "provider": best["provider"],
            "game": str(options.get("game", "auto")),
            "game_mode": str(options.get("game_mode", "auto")),
            "detected_game": best.get("detected_game", "FPS游戏"),
            "detected_mode": best.get("detected_mode", "未知模式"),
            "rainbow_six": best.get("rainbow_six"),
            "start": montage_plan[0]["start"],
            "duration": round(float(render_result["duration"]), 3),
            "segment_count": len(montage_plan),
            "segments": montage_plan,
            "transition_count": int(render_result["transition_count"]),
            "transition_duration": float(render_result["transition_duration"]),
            "transition_style": str(render_result["transition_style"]),
            "score": best["score"],
            "subtitles": subtitle_result,
            "qwen_failures": len(qwen_failures),
        }
        database.update_job(
            job_id,
            state="completed",
            stage="completed",
            progress=1.0,
            message="剪辑完成",
            result_json=result,
        )
    except CancellationError:
        log.info("Pipeline cancelled for job %s", job_id)
        database.update_job(
            job_id,
            state="cancelled",
            stage="cancelled",
            progress=1.0,
            message="任务已取消",
            error="任务已取消",
        )
    except Exception as exc:
        log.exception("Pipeline failed for job %s", job_id)
        database.update_job(
            job_id,
            state="failed",
            stage="failed",
            progress=1.0,
            message="剪辑失败",
            error=str(exc),
        )
    finally:
        for child in work_dir.glob("candidate_*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
