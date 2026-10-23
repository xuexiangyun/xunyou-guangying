from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .database import Database
from .services.cancellation import CancellationToken
from .services.pipeline import SourceVideo, run_pipeline


class JobManager:
    """Runs one heavy video job at a time so a laptop remains responsive."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xunyou-job")
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}
        for record in self.database.list_jobs(100):
            if record["state"] in {"queued", "running"}:
                self.database.update_job(
                    record["id"],
                    state="failed",
                    stage="failed",
                    progress=1.0,
                    message="任务因服务重启而中断",
                    error="服务重启后任务已停止，请重新上传并开始剪辑。",
                )

    def submit(
        self,
        *,
        job_id: str,
        sources: list[SourceVideo],
        options: dict,
        qwen_api_key: str,
        qwen_api_host: str,
    ) -> None:
        token = CancellationToken()
        with self._lock:
            self._tokens[job_id] = token
        future = self.executor.submit(
            run_pipeline,
            database=self.database,
            job_id=job_id,
            sources=sources,
            options=options,
            qwen_api_key=qwen_api_key,
            qwen_api_host=qwen_api_host,
            cancel_token=token,
        )
        future.add_done_callback(lambda _: self._discard_token(job_id))

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(job_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _discard_token(self, job_id: str) -> None:
        with self._lock:
            self._tokens.pop(job_id, None)


__all__ = ["JobManager"]
