# File: Dockerfile
# syntax=docker/dockerfile:1.7   # enables faster pip cache mounts with BuildKit

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    STATIONS_FILE=/data/stations.yaml \
    UA="VLC/3.0"

# FFmpeg only; keep image small
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man

# Non-root user
RUN useradd -m -u 10001 -s /usr/sbin/nologin appuser

WORKDIR /app

# Layer cache for deps
COPY --chown=appuser:appuser requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# App files only
COPY --chown=appuser:appuser app.py hls_best_audio.sh ./
RUN chmod +x hls_best_audio.sh \
 && mkdir -p /data \
 && chown -R appuser:appuser /data

EXPOSE 8000
USER appuser

# Gunicorn with streaming-friendly settings (matches your systemd)
CMD ["gunicorn",
     "-w","1","--threads","4","--worker-class","gthread",
     "--timeout","0","--graceful-timeout","10",
     "--max-requests","1000","--max-requests-jitter","100",
     "--bind","0.0.0.0:8000",
     "app:app"]
