# File: gunicorn.conf.py
# Why: small buffers & TCP tuning help live streaming under proxies.
# Docs: https://docs.gunicorn.org/en/stable/settings.html
accesslog = "-"
errorlog = "-"
loglevel = "info"
forwarded_allow_ips = "*"
proxy_protocol = False
keepalive = 2
# gthread worker already chosen in entrypoint
# worker_tmp_dir not needed; streaming to stdout
