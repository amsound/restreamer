# File: entrypoint.sh
#!/usr/bin/env bash
set -Eeuo pipefail
# Why: ensure Gunicorn inherits env and gets clean stop (signals → workers → ffmpeg).
exec "$@" \
  -w 1 --threads 4 --worker-class gthread \
  --timeout 0 --graceful-timeout 10 \
  --max-requests 1000 --max-requests-jitter 100 \
  --bind 0.0.0.0:"${PORT:-8000}" \
  -c /app/gunicorn.conf.py
