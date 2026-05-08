# ─────────────────────────────────────────────────────────────────
# Gunicorn configuration for Intelligent Investor V2
# Local:  gunicorn -c gunicorn.conf.py dashboard_v2:app
# Docker: GUNICORN_BIND=0.0.0.0:5050 gunicorn -c gunicorn.conf.py dashboard_v2:app
# ─────────────────────────────────────────────────────────────────
import os
import multiprocessing

# ── Binding ───────────────────────────────────────────────────────
# Local: bind to loopback (Nginx on same host proxies to this)
# Docker: 0.0.0.0:5050 so the Nginx container can reach the app container
bind            = os.environ.get("GUNICORN_BIND", "127.0.0.1:5050")
backlog         = 2048

# ── Workers ───────────────────────────────────────────────────────
# (2 × CPU cores) + 1 is the standard Gunicorn recommendation
workers         = (multiprocessing.cpu_count() * 2) + 1
worker_class    = "sync"
threads         = 4          # threads per worker for I/O-bound work
worker_connections = 1000
timeout         = 120        # screening runs can take a while
keepalive       = 5

# ── Logging ───────────────────────────────────────────────────────
accesslog       = "-"        # stdout (captured by systemd journal)
errorlog        = "-"        # stderr
loglevel        = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Security ──────────────────────────────────────────────────────
limit_request_line   = 4096
limit_request_fields = 100
# Trust X-Forwarded-For only from the Nginx container on the Docker bridge network.
# Override with GUNICORN_FORWARDED_IPS env var if your network CIDR is different.
forwarded_allow_ips  = os.environ.get("GUNICORN_FORWARDED_IPS", "172.18.0.0/16")
secure_scheme_headers = {"X-Forwarded-Proto": "https"}

# ── Process name ─────────────────────────────────────────────────
proc_name = "intelligentinvestor_v2"

# ── Reload on code change (disable in production) ─────────────────
reload = os.environ.get("GUNICORN_RELOAD", "false").lower() == "true"

# ── Pre-fork hook ─────────────────────────────────────────────────
def on_starting(server):
    server.log.info("Intelligent Investor V2 starting up…")

def worker_exit(server, worker):
    server.log.info("Worker %s exited", worker.pid)
