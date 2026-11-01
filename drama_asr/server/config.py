"""Runtime configuration for the drama ASR service.

All settings are read from environment variables so the same image can be
deployed to different cloud hosts without code changes. See ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- ASR (speech recognition) ---
    # A faster-whisper / CTranslate2 model directory, or a Hugging Face model id
    # that has already been converted to the CTranslate2 format. Use
    # ``scripts/prepare_model.py`` to convert ``litagin/anime-whisper``.
    asr_model: str = os.environ.get("ASR_MODEL", "models/anime-whisper-ct2")
    device: str = os.environ.get("DEVICE", "cuda")
    # float16 is a good default on a 16 GB GPU; use int8_float16 to save memory.
    compute_type: str = os.environ.get("COMPUTE_TYPE", "float16")
    asr_language: str = os.environ.get("ASR_LANGUAGE", "ja")
    beam_size: int = int(os.environ.get("BEAM_SIZE", "5"))
    # Use faster-whisper's built-in Silero VAD to split long audio sensibly.
    vad_filter: bool = _as_bool(os.environ.get("VAD_FILTER"), True)

    # --- Translation ---
    enable_translation: bool = _as_bool(os.environ.get("ENABLE_TRANSLATION"), True)
    nllb_model: str = os.environ.get("NLLB_MODEL", "facebook/nllb-200-distilled-1.3B")
    source_lang: str = os.environ.get("SOURCE_LANG", "jpn_Jpan")
    target_lang: str = os.environ.get("TARGET_LANG", "zho_Hans")
    translate_batch_size: int = int(os.environ.get("TRANSLATE_BATCH_SIZE", "16"))

    # --- Service ---
    # Optional bearer token. When set, clients must send
    # ``Authorization: Bearer <token>``. Strongly recommended for a public host.
    api_key: str | None = os.environ.get("API_KEY") or None
    work_dir: str = os.environ.get("WORK_DIR", "/tmp/drama_asr_jobs")
    # Delete the uploaded audio from the server as soon as a job finishes so the
    # cloud host does not retain user media.
    delete_audio_after: bool = _as_bool(os.environ.get("DELETE_AUDIO_AFTER"), True)
    # How long (seconds) to keep a finished job's result in memory before it is
    # garbage-collected. Clients should download results promptly.
    job_ttl_seconds: int = int(os.environ.get("JOB_TTL_SECONDS", "3600"))

    # --- Testing ---
    # When enabled, ASR and translation are replaced by deterministic stubs so
    # the full HTTP pipeline can run without a GPU or any downloaded model.
    mock: bool = _as_bool(os.environ.get("MOCK"), False)

    @property
    def max_upload_bytes(self) -> int:
        # Default 2 GiB; enough for multi-hour lossless drama CDs.
        return int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024 ** 3)))


def get_settings() -> Settings:
    return Settings()
