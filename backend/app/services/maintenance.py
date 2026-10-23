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


def discard_orphan_uploads(database: Database, keep_upload_ids: set[str] | None = None) -> int:
    """Delete uploads that no job references any more.

    Covers leftovers such as a duplicate upload whose submission was rejected,
    or a second copy of a file the user picked twice. Uploads still being
    received, or held by a running local-slice task, are kept.
    """
    keep = set(keep_upload_ids or ())
    referenced: set[str] = set()
    for job in database.list_jobs(100000):
        upload_ids = list(job.get("upload_ids") or [])
        if not upload_ids and job.get("upload_id"):
            upload_ids = [job["upload_id"]]
        referenced.update(upload_ids)

    removed = 0
    for upload in database.list_uploads():
        if upload["id"] in referenced or upload["id"] in keep:
            continue
        if upload.get("state") == "uploading":
            continue
        Path(upload["stored_path"]).unlink(missing_ok=True)
        database.delete_upload(upload["id"])
        removed += 1
    return removed


def cleanup_storage(database: Database, keep_upload_ids: set[str] | None = None) -> dict[str, int]:
    """Remove files and directories that are no longer referenced by the database.

    Matching is done by file/directory *name* rather than absolute path so that a
    moved project directory never causes valid data to be deleted.
    """
    removed_orphan_uploads = discard_orphan_uploads(database, keep_upload_ids)
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
        "removed_orphan_uploads": removed_orphan_uploads,
        "removed_directories": removed_directories,
    }


def clear_candidate_dirs(base: Path) -> int:
    """Remove cached candidate proxies left under ``base`` by any job.

    Candidates are kept after a job finishes so the local screening result can
    be inspected; the next job or the next process start clears them.
    """
    removed = 0
    if not base.exists():
        return removed
    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue
        for child in job_dir.glob("candidate_*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


__all__ = [
    "clear_candidate_dirs",
    "cleanup_storage",
    "discard_orphan_uploads",
    "repair_stored_paths",
]
