"""In-memory async job queue.

A single background worker processes jobs sequentially, which keeps GPU memory
usage predictable (one transcription at a time). Jobs carry progress so clients
can poll while a long drama is processed. Models are loaded lazily on the first
job so the server can boot instantly and fail loudly only when work arrives.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Queue

from .asr import build_asr
from .pipeline import transcribe_and_translate
from .translate import build_translator


@dataclass
class Job:
    id: str
    audio_path: str
    filename: str
    status: str = "queued"  # queued | running | done | error
    stage: str = "queued"
    progress: float = 0.0
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class JobManager:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._queue: "Queue[str]" = Queue()
        self._lock = threading.Lock()
        self._asr = None
        self._translator = None
        self._models_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # --- public API -----------------------------------------------------
    def submit(self, audio_path: str, filename: str) -> Job:
        job = Job(id=uuid.uuid4().hex, audio_path=audio_path, filename=filename)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job.id)
        self._gc()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    # --- worker ---------------------------------------------------------
    def _ensure_models(self) -> None:
        with self._models_lock:
            if self._asr is None:
                self._asr = build_asr(self.settings)
            if self._translator is None:
                self._translator = build_translator(self.settings)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._ensure_models()
                job.status = "running"
                job.stage = "asr"

                def on_progress(stage: str, frac: float, _job=job) -> None:
                    _job.stage = stage
                    _job.progress = round(frac, 4)

                result = transcribe_and_translate(
                    job.audio_path,
                    self._asr,
                    self._translator,
                    progress=on_progress,
                )
                job.result = result
                job.status = "done"
                job.stage = "done"
                job.progress = 1.0
            except Exception as exc:  # surface failure to the client
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = time.time()
                if self.settings.delete_audio_after:
                    try:
                        os.remove(job.audio_path)
                    except OSError:
                        pass
                self._queue.task_done()

    def _gc(self) -> None:
        """Drop finished jobs past their TTL to bound memory."""
        ttl = self.settings.job_ttl_seconds
        now = time.time()
        with self._lock:
            stale = [
                jid
                for jid, j in self._jobs.items()
                if j.finished_at is not None and (now - j.finished_at) > ttl
            ]
            for jid in stale:
                del self._jobs[jid]
