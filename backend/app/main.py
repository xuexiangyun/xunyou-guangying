from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import db
from .jobs import JobManager
from .schemas import (
    CostEstimate,
    JobCreateRequest,
    JobStatus,
    QwenCredentialRequest,
    UploadInitRequest,
    UploadInitResponse,
    UploadStatus,
)
from .services.maintenance import cleanup_storage, repair_stored_paths
from .services.qwen import load_credentials, qwen_error_message, save_credentials, validate_api_key
from .services.video import VideoValidationError, estimate_cost, probe_video
from .settings import FRONTEND_DIST, OUTPUT_DIR, TEMP_DIR, UPLOAD_DIR, ensure_directories, settings


ensure_directories()
app = FastAPI(title=settings.app_name, version="0.1.0")
jobs = JobManager(db)
repair_stored_paths(db)
cleanup_storage(db)


def _require_qwen_credentials() -> tuple[str, str]:
    try:
        credentials = load_credentials()
    except Exception as exc:
        raise HTTPException(400, qwen_error_message(exc)) from exc
    if not credentials:
        raise HTTPException(400, "千问尚未配置，请先完成一次Qwen3-Omni-Flash设置。")
    return credentials


def _upload_status(record: dict) -> UploadStatus:
    return UploadStatus(
        upload_id=record["id"],
        filename=record["filename"],
        size=record["size"],
        received_bytes=record["received_bytes"],
        state=record["state"],
        metadata=record.get("metadata"),
    )


def _job_status(record: dict) -> JobStatus:
    upload = db.get_upload(record["upload_id"])
    return JobStatus(
        job_id=record["id"],
        upload_id=record["upload_id"],
        state=record["state"],
        stage=record["stage"],
        progress=record["progress"],
        message=record["message"],
        options=record["options"],
        filename=upload["filename"] if upload else None,
        source_size=upload["size"] if upload else None,
        metadata=record.get("metadata"),
        result=record.get("result"),
        error=record.get("error"),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


@app.get("/api/health")
def health() -> dict:
    try:
        qwen_configured = load_credentials() is not None
    except Exception:
        qwen_configured = False
    return {
        "ok": True,
        "name": settings.app_name,
        "qwen_model": settings.qwen_model,
        "qwen_configured": qwen_configured,
        "ffmpeg": settings.ffmpeg,
        "max_upload_gb": settings.max_upload_bytes // (1024**3),
    }


@app.post("/api/uploads", response_model=UploadInitResponse, status_code=status.HTTP_201_CREATED)
def initialize_upload(payload: UploadInitRequest) -> UploadInitResponse:
    suffix = Path(payload.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv"}:
        raise HTTPException(400, "仅支持 MP4、MOV 和 MKV 文件。")
    if payload.size > settings.max_upload_bytes:
        raise HTTPException(413, "单个文件不能超过20GB。")
    upload_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{upload_id}{suffix}.part"
    stored_path.touch(exist_ok=False)
    db.create_upload(upload_id, payload.filename, stored_path, payload.size, payload.content_type)
    return UploadInitResponse(upload_id=upload_id, chunk_size=settings.chunk_size)


@app.put("/api/uploads/{upload_id}/chunk", response_model=UploadStatus)
async def append_upload_chunk(
    upload_id: str,
    request: Request,
    upload_offset: int = Header(alias="Upload-Offset"),
) -> UploadStatus:
    record = db.get_upload(upload_id)
    if not record:
        raise HTTPException(404, "上传任务不存在。")
    if record["state"] != "uploading":
        raise HTTPException(409, "这个文件已经结束上传。")
    if upload_offset != record["received_bytes"]:
        raise HTTPException(409, f"上传位置不一致，应从 {record['received_bytes']} 字节继续。")

    path = Path(record["stored_path"])
    written = 0
    with path.open("ab") as handle:
        async for chunk in request.stream():
            written += len(chunk)
            if written > settings.chunk_size:
                raise HTTPException(413, "单个上传分块过大。")
            if record["received_bytes"] + written > record["size"]:
                raise HTTPException(400, "上传内容超过声明的文件大小。")
            handle.write(chunk)
    updated = db.update_upload(upload_id, received_bytes=record["received_bytes"] + written)
    return _upload_status(updated or record)


@app.post("/api/uploads/{upload_id}/complete", response_model=UploadStatus)
def complete_upload(upload_id: str) -> UploadStatus:
    record = db.get_upload(upload_id)
    if not record:
        raise HTTPException(404, "上传任务不存在。")
    if record["received_bytes"] != record["size"]:
        raise HTTPException(409, "文件还没有上传完整。")

    part_path = Path(record["stored_path"])
    final_path = part_path.with_suffix("")
    if part_path != final_path:
        part_path.replace(final_path)
    try:
        metadata = probe_video(final_path)
    except VideoValidationError as exc:
        db.update_upload(upload_id, state="invalid", stored_path=str(final_path))
        raise HTTPException(400, str(exc)) from exc
    updated = db.update_upload(
        upload_id,
        state="ready",
        stored_path=str(final_path),
        metadata_json=metadata,
    )
    return _upload_status(updated or record)


@app.get("/api/uploads/{upload_id}", response_model=UploadStatus)
def get_upload(upload_id: str) -> UploadStatus:
    record = db.get_upload(upload_id)
    if not record:
        raise HTTPException(404, "上传任务不存在。")
    return _upload_status(record)


@app.delete("/api/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_upload(upload_id: str) -> None:
    record = db.get_upload(upload_id)
    if not record:
        return
    Path(record["stored_path"]).unlink(missing_ok=True)
    db.delete_upload(upload_id)


@app.get("/api/estimate", response_model=CostEstimate)
def get_estimate(
    duration: float = Query(gt=0, le=settings.max_duration_seconds),
    sensitivity: str = Query(default="balanced", pattern="^(strict|balanced|broad)$"),
) -> CostEstimate:
    return CostEstimate(**estimate_cost(duration, sensitivity))


@app.post("/api/qwen/validate")
def validate_qwen_key(payload: QwenCredentialRequest) -> dict:
    try:
        model = validate_api_key(payload.qwen_api_key, payload.qwen_api_host)
    except Exception as exc:
        raise HTTPException(400, qwen_error_message(exc)) from exc
    return {"valid": True, **model}


@app.get("/api/qwen/status")
def qwen_status() -> dict:
    try:
        configured = load_credentials() is not None
        return {"configured": configured, "model": settings.qwen_model}
    except Exception as exc:
        return {"configured": False, "model": settings.qwen_model, "error": qwen_error_message(exc)}


@app.post("/api/qwen/configure")
def configure_qwen(payload: QwenCredentialRequest) -> dict:
    try:
        model = validate_api_key(payload.qwen_api_key, payload.qwen_api_host)
        save_credentials(payload.qwen_api_key, payload.qwen_api_host)
    except Exception as exc:
        raise HTTPException(400, qwen_error_message(exc)) from exc
    return {"configured": True, **model}


@app.post("/api/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
def create_job(payload: JobCreateRequest) -> JobStatus:
    upload = db.get_upload(payload.upload_id)
    if not upload:
        raise HTTPException(404, "上传文件不存在。")
    if upload["state"] != "ready" or not upload.get("metadata"):
        raise HTTPException(409, "请先完成视频上传和校验。")
    active = [job for job in db.list_jobs(20) if job["state"] in {"queued", "running"}]
    if active:
        raise HTTPException(409, "当前已有剪辑任务正在运行，请等待它完成。")

    qwen_api_key, qwen_api_host = _require_qwen_credentials()

    job_id = uuid.uuid4().hex
    options = payload.options.model_dump(mode="json")
    record = db.create_job(job_id, payload.upload_id, options)
    db.update_job(job_id, metadata_json=upload["metadata"])
    jobs.submit(
        job_id=job_id,
        source=Path(upload["stored_path"]),
        metadata=upload["metadata"],
        options=options,
        qwen_api_key=qwen_api_key,
        qwen_api_host=qwen_api_host,
    )
    return _job_status(db.get_job(job_id) or record)


@app.post("/api/jobs/{job_id}/retry", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
def retry_job(job_id: str) -> JobStatus:
    record = db.get_job(job_id)
    if not record:
        raise HTTPException(404, "剪辑任务不存在。")
    if record["state"] != "failed":
        raise HTTPException(409, "只有失败的任务可以重试。")
    upload = db.get_upload(record["upload_id"])
    if not upload or upload["state"] != "ready" or not upload.get("metadata"):
        raise HTTPException(409, "原始上传文件已经不存在，请重新选择视频。")
    active = [job for job in db.list_jobs(20) if job["state"] in {"queued", "running"}]
    if active:
        raise HTTPException(409, "当前已有剪辑任务正在运行，请等待它完成。")

    qwen_api_key, qwen_api_host = _require_qwen_credentials()

    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    shutil.rmtree(TEMP_DIR / job_id, ignore_errors=True)
    updated = db.update_job(
        job_id,
        state="queued",
        stage="queued",
        progress=0.0,
        message="等待重新开始",
        result_json=None,
        error=None,
    )
    jobs.submit(
        job_id=job_id,
        source=Path(upload["stored_path"]),
        metadata=upload["metadata"],
        options=record["options"],
        qwen_api_key=qwen_api_key,
        qwen_api_host=qwen_api_host,
    )
    return _job_status(updated or record)


@app.get("/api/jobs", response_model=list[JobStatus])
def list_jobs() -> list[JobStatus]:
    return [_job_status(record) for record in db.list_jobs()]


@app.post("/api/jobs/{job_id}/cancel", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(job_id: str) -> JobStatus:
    record = db.get_job(job_id)
    if not record:
        raise HTTPException(404, "剪辑任务不存在。")
    if record["state"] not in {"queued", "running"}:
        raise HTTPException(409, "只有排队中或运行中的任务可以取消。")
    if not jobs.cancel(job_id):
        raise HTTPException(409, "任务已经结束，无法取消。")
    updated = db.update_job(
        job_id,
        state="cancelled",
        stage="cancelled",
        progress=1.0,
        message="任务已取消",
        error="任务已取消",
    )
    return _job_status(updated or record)


@app.post("/api/maintenance/cleanup")
def maintenance_cleanup() -> dict:
    return cleanup_storage(db)


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    record = db.get_job(job_id)
    if not record:
        raise HTTPException(404, "剪辑任务不存在。")
    return _job_status(record)


def _result_path(job_id: str) -> tuple[dict, Path]:
    record = db.get_job(job_id)
    if not record or record["state"] != "completed" or not record.get("result"):
        raise HTTPException(404, "成片尚未生成。")
    path = Path(record["result"]["output_path"])
    if not path.exists():
        raise HTTPException(404, "成片文件不存在。")
    return record, path


@app.get("/api/jobs/{job_id}/video")
def stream_result(job_id: str) -> FileResponse:
    _, path = _result_path(job_id)
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str) -> FileResponse:
    _, path = _result_path(job_id)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, delete_source: bool = True) -> None:
    record = db.get_job(job_id)
    if not record:
        return
    if record["state"] in {"queued", "running"}:
        raise HTTPException(409, "任务运行中，暂时不能删除。")
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    shutil.rmtree(TEMP_DIR / job_id, ignore_errors=True)
    upload_id = record["upload_id"]
    db.delete_job(job_id)
    if delete_source:
        upload = db.get_upload(upload_id)
        if upload:
            Path(upload["stored_path"]).unlink(missing_ok=True)
            db.delete_upload(upload_id)


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        candidate = FRONTEND_DIST / path
        if path and candidate.is_file() and FRONTEND_DIST in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
