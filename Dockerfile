# File: Dockerfile  (Pi-friendly, generic)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    STATIONS_FILE=/data/stations.yaml \
    UA="VLC/3.0"

# FFmpeg + certs
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates wget curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root
RUN useradd -m -u 10001 -s /usr/sbin/nologin appuser
WORKDIR /app

# Dependencies (no BuildKit cache needed)
COPY requirements.txt .
RUN pip install -r requirements.txt

# App
COPY app.py hls_best_audio.sh ./
RUN chmod +x hls_best_audio.sh \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

EXPOSE 8000
USER appuser

# Portable shell-form CMD (env expansion works; single line avoids parser issues)
CMD gunicorn -w 1 --threads 4 --worker-class gthread \
    --timeout 0 --graceful-timeout 10 \
    --max-requests 1000 --max-requests-jitter 100 \
    --bind 0.0.0.0:${PORT:-8000} app:app
