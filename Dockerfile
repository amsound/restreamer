# File: Dockerfile  (Pi-friendly, generic)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    STATIONS_FILE=/data/stations.yaml \
    UA="VLC/3.0" \
    DEFAULT_FMT=adts

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
COPY app.py gunicorn.conf.py hls_best_audio.sh ./
RUN chmod +x hls_best_audio.sh \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

EXPOSE 8000
USER appuser

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

# All server settings live in gunicorn.conf.py.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
