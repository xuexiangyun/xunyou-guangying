from __future__ import annotations

import threading
import time
from typing import Any


class CancellationError(RuntimeError):
    """Raised inside the pipeline when a job has been cancelled."""


class CancellationToken:
    """Cooperative cancellation with best-effort termination of child processes."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: list[Any] = []

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            for process in list(self._processes):
                try:
                    process.terminate()
                except Exception:
                    pass

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancellationError("任务已取消")

    def register(self, process: Any) -> None:
        with self._lock:
            if self._event.is_set():
                try:
                    process.terminate()
                except Exception:
                    pass
            else:
                self._processes.append(process)

    def unregister(self, process: Any) -> None:
        with self._lock:
            try:
                self._processes.remove(process)
            except ValueError:
                pass

    def terminate_and_wait(self, process: Any) -> None:
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass


__all__ = ["CancellationError", "CancellationToken"]
