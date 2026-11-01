#!/usr/bin/env bash
# Start the ASR service. Env vars (see .env.example) control the models.
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# One worker only: the GPU is shared by a single internal job queue, so
# multiple uvicorn workers would duplicate the models and oversubscribe VRAM.
exec uvicorn drama_asr.server.app:app --host "$HOST" --port "$PORT" --workers 1
