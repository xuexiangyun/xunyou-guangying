from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .settings import DB_DIR, ensure_directories


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | None = None) -> None:
        ensure_directories()
        self.path = path or DB_DIR / "xunyou.sqlite3"
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    content_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    metadata_json TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version < 1:
                connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ("metadata_json", "options_json", "result_json"):
            if key in data:
                value = data.pop(key)
                data[key.removesuffix("_json")] = json.loads(value) if value else None
        return data

    def create_upload(self, upload_id: str, filename: str, path: Path, size: int, content_type: str) -> dict:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO uploads VALUES (?, ?, ?, ?, 0, ?, 'uploading', NULL, ?, ?)",
                (upload_id, filename, str(path), size, content_type, now, now),
            )
        return self.get_upload(upload_id) or {}

    def get_upload(self, upload_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return self._decode(row)

    def list_uploads(self) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM uploads ORDER BY created_at DESC").fetchall()
        return [self._decode(row) or {} for row in rows]

    def update_upload(self, upload_id: str, **fields: Any) -> dict | None:
        allowed = {"received_bytes", "state", "metadata_json", "stored_path"}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(upload_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE uploads SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get_upload(upload_id)

    def delete_upload(self, upload_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))

    def create_job(self, job_id: str, upload_id: str, options: dict) -> dict:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO jobs
                (id, upload_id, state, stage, progress, message, options_json, created_at, updated_at)
                VALUES (?, ?, 'queued', 'queued', 0, '等待开始', ?, ?, ?)""",
                (job_id, upload_id, json.dumps(options, ensure_ascii=False), now, now),
            )
        return self.get_job(job_id) or {}

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row)

    def list_jobs(self, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> dict | None:
        allowed = {
            "state",
            "stage",
            "progress",
            "message",
            "metadata_json",
            "result_json",
            "error",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


db = Database()

