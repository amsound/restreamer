# File: gunicorn.conf.py
# Docs: https://docs.gunicorn.org/en/stable/settings.html
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# One process; each listener holds one thread for as long as it listens, so
# `threads` is the maximum number of simultaneous streams.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("THREADS", "8"))

# Streams never finish, so never time them out.
timeout = 0
graceful_timeout = 10
keepalive = 2

# Deliberately no max_requests: recycling the worker would cut every live stream.

accesslog = "-"
errorlog = "-"
loglevel = "info"
forwarded_allow_ips = "*"
proxy_protocol = False
