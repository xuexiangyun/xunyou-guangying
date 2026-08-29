from __future__ import annotations

import shutil
from pathlib import Path

from ..database import Database
from ..settings import OUTPUT_DIR, TEMP_DIR, UPLOAD_DIR


def repair_stored_paths(database: Database) -> int:
    """Fix absolute paths that became stale after the project directory moved.

    Uploads are keyed by their file name (``<upload_id>.<ext>``) and outputs live
    under ``OUTPUT_DIR/<job_id>/``, so both can be re-derived without trusting the
    absolute path previously stored in the database.
    """
    repaired = 0

    for upload in database.list_uploads():
        stored = Path(upload["stored_path"])
        if stored.exists():
            continue
        candidate = UPLOAD_DIR / stored.name
        if candidate.exists():
            database.update_upload(upload["id"], stored_path=str(candidate))
            repaired += 1

    for job in database.list_jobs(100000):
        result = job.get("result") or {}
        output_path = result.get("output_path")
        if not output_path:
            continue
        current = Path(output_path)
        if current.exists():
            continue
        filename = str(result.get("filename") or current.name)
        candidate = OUTPUT_DIR / job["id"] / filename
        if candidate.exists():
            result = {**result, "output_path": str(candidate)}
            database.update_job(job["id"], result_json=result)
            repaired += 1

    return repaired


def cleanup_storage(database: Database) -> dict[str, int]:
    """Remove files and directories that are no longer referenced by the database.

    Matching is done by file/directory *name* rather than absolute path so that a
    moved project directory never causes valid data to be deleted.
    """
    uploads = {record["id"]: record for record in database.list_uploads()}
    known_names = {Path(record["stored_path"]).name for record in uploads.values()}

    removed_upload_files = 0
    for path in UPLOAD_DIR.iterdir():
        if not path.is_file() or path.name == ".gitkeep":
            continue
        if path.name not in known_names:
            path.unlink(missing_ok=True)
            removed_upload_files += 1

    removed_invalid_uploads = 0
    for upload_id, record in uploads.items():
        if record.get("state") == "invalid":
            (UPLOAD_DIR / Path(record["stored_path"]).name).unlink(missing_ok=True)
            database.delete_upload(upload_id)
            removed_invalid_uploads += 1

    known_jobs = {record["id"] for record in database.list_jobs(100000)}
    removed_directories = 0
    for base in (OUTPUT_DIR, TEMP_DIR):
        for child in base.iterdir():
            if child.is_dir() and child.name not in known_jobs:
                shutil.rmtree(child, ignore_errors=True)
                removed_directories += 1

    return {
        "removed_upload_files": removed_upload_files,
        "removed_invalid_uploads": removed_invalid_uploads,
        "removed_directories": removed_directories,
    }


__all__ = ["cleanup_storage", "repair_stored_paths"]
