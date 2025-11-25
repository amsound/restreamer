# File: Dockerfile
FROM python:3.12-slim

# Install ffmpeg and runtime essentials
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -u 10001 appuser

WORKDIR /app

# Copy only requirements first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app sources
COPY app.py /app/app.py
COPY hls_best_audio.sh /app/hls_best_audio.sh
COPY gunicorn.conf.py /app/gunicorn.conf.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/hls_best_audio.sh

# Where stations live (mounted at runtime)
RUN mkdir -p /data && chown -R appuser:appuser /app /data

ENV PORT=8000 \
    STATIONS_FILE=/data/stations.yaml \
    PYTHONUNBUFFERED=1 \
    # Same default UA your app uses; override in compose if you want VLC
    UA="VLC/3.0"

EXPOSE 8000

USER appuser

# NOTE: rely on Gunicorn for prod serving (stream-friendly gthread)
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "app:app"]
