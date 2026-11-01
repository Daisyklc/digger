"""FastAPI application exposing the async transcription/translation service.

Deploy this on the cloud GPU host. Clients keep their audio and output files
locally: they upload audio, poll for progress, then download the result, after
which the server deletes its temporary copy of the audio.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .jobs import JobManager

settings: Settings = get_settings()
os.makedirs(settings.work_dir, exist_ok=True)

app = FastAPI(title="Drama ASR + Translation", version="1.0")
job_manager = JobManager(settings)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "mock": settings.mock,
        "translation": settings.enable_translation,
        "asr_model": settings.asr_model,
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
) -> JSONResponse:
    job_id_dir = settings.work_dir
    safe_name = os.path.basename(file.filename or "audio")
    tmp_path = os.path.join(job_id_dir, f"{os.urandom(8).hex()}_{safe_name}")

    size = 0
    with open(tmp_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.max_upload_bytes:
                out.close()
                os.remove(tmp_path)
                raise HTTPException(status_code=413, detail="upload too large")
            out.write(chunk)

    if size == 0:
        os.remove(tmp_path)
        raise HTTPException(status_code=400, detail="empty upload")

    job = job_manager.submit(tmp_path, safe_name)
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, _: None = Depends(require_auth)) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job.id,
        "filename": job.filename,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "error": job.error,
    }


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str, _: None = Depends(require_auth)) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.error or "job failed")
    if job.status != "done" or job.result is None:
        raise HTTPException(status_code=409, detail=f"job not finished (status={job.status})")
    return {"job_id": job.id, "filename": job.filename, **job.result}
