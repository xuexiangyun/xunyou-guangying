from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..settings import TEMP_DIR, settings
from .pipeline import (
    SourceVideo,
    _apply_analysis_budget,
    _target_duration,
    analysis_budget_for,
)
from .video import candidate_window, rank_local_candidates, render_analysis_proxy


log = logging.getLogger(__name__)
SLICE_DIR_NAME = "local_slices"


class LocalSliceManager:
    """Renders the locally ranked candidate windows for manual inspection.

    This is a throwaway preview tool: it never calls Qwen and never writes a
    job record, so it stays out of the job history. Output lives under
    ``data/temp/local_slices/<task_id>/`` and disappears with the rest of temp.
    """

    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xunyou-slices")
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._task_uploads: dict[str, list[str]] = {}

    def submit(
        self,
        sources: list[SourceVideo],
        *,
        upload_ids: list[str] | None = None,
        sensitivity: str = "balanced",
        duration_mode: str = "seconds_60",
        cleanup: Callable[[], None] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex
        directory = TEMP_DIR / SLICE_DIR_NAME / task_id
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "state": "queued",
                "progress": 0.0,
                "message": "等待开始",
                "directory": str(directory),
                "slices": [],
                "error": None,
            }
            self._task_uploads[task_id] = list(upload_ids or [])
        self.executor.submit(
            self._run, task_id, sources, directory, sensitivity, duration_mode, cleanup
        )
        return task_id

    def active_upload_ids(self) -> set[str]:
        """Uploads held by running slice tasks, so cleanup does not delete them."""
        with self._lock:
            return {
                upload_id
                for task_id, task in self._tasks.items()
                if task["state"] in {"queued", "running"}
                for upload_id in self._task_uploads.get(task_id, [])
            }

    def status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def _update(self, task_id: str, **changes: object) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.update(changes)

    def _run(
        self,
        task_id: str,
        sources: list[SourceVideo],
        directory: Path,
        sensitivity: str,
        duration_mode: str,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._update(task_id, state="running", message="正在扫描画面与声音")
            slices: list[dict] = []
            file_count = max(1, len(sources))
            file_durations = [float(source.metadata["duration"]) for source in sources]
            total_duration = sum(file_durations)
            scan_share = 0.6

            candidates: list[dict] = []
            for file_index, source in enumerate(sources):
                file_base = file_index / file_count * scan_share
                span = scan_share / file_count

                def scan_progress(
                    value: float,
                    message: str,
                    *,
                    base: float = file_base,
                    width: float = span,
                    filename: str = source.filename,
                ) -> None:
                    self._update(
                        task_id,
                        progress=round(base + value * width, 4),
                        message=f"{filename}：{message}",
                    )

                file_candidates = rank_local_candidates(
                    source.path,
                    float(source.metadata["duration"]),
                    sensitivity,
                    scan_progress,
                )
                candidates.extend(
                    {
                        **candidate,
                        "file_index": file_index,
                        "source": str(source.path),
                        "filename": source.filename,
                    }
                    for candidate in file_candidates
                )

            # Apply the exact same budget a real job uses before calling Qwen.
            target_duration = _target_duration(duration_mode, total_duration)
            budget = analysis_budget_for(target_duration, sensitivity, file_count)
            candidates = _apply_analysis_budget(candidates, budget)
            total = max(1, len(candidates))
            self._update(
                task_id,
                message=f"本地筛选出 {len(candidates)} 个候选，正在生成切片",
            )

            for index, candidate in enumerate(candidates):
                file_index = int(candidate.get("file_index", 0))
                start, slice_duration = candidate_window(
                    float(candidate["center"]),
                    file_durations[file_index],
                    settings.qwen_candidate_seconds,
                )
                filename = f"candidate_{index:02d}_f{file_index + 1}_{int(start):04d}s.mp4"
                output = directory / filename
                render_analysis_proxy(
                    Path(candidate["source"]), output, start, slice_duration
                )
                slices.append(
                    {
                        "filename": filename,
                        "source": str(candidate.get("filename", "")),
                        "file_index": file_index,
                        "start": round(start, 3),
                        "duration": round(slice_duration, 3),
                        "local_score": round(float(candidate["local_score"]), 4),
                        "path": str(output),
                    }
                )
                self._update(
                    task_id,
                    progress=round(scan_share + (1 - scan_share) * (index + 1) / total, 4),
                    message=f"已生成 {index + 1}/{total} 个切片",
                )

            (directory / "slices.json").write_text(
                json.dumps({"task_id": task_id, "slices": slices}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._update(
                task_id,
                state="completed",
                progress=1.0,
                message=f"已生成 {len(slices)} 个本地切片",
                slices=slices,
            )
        except Exception as exc:
            self._update(
                task_id,
                state="failed",
                progress=1.0,
                message="生成本地切片失败",
                error=str(exc),
            )
        finally:
            with self._lock:
                self._task_uploads.pop(task_id, None)
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    log.exception("Local slice cleanup failed")


__all__ = ["LocalSliceManager"]
