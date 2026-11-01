# GPU image for the drama ASR + translation service.
# cuDNN is required by CTranslate2 / faster-whisper.
FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY drama_asr/requirements-server.txt /app/requirements-server.txt
RUN pip3 install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu121 \
    && pip3 install --no-cache-dir -r /app/requirements-server.txt

COPY drama_asr /app/drama_asr

EXPOSE 8000
CMD ["uvicorn", "drama_asr.server.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
