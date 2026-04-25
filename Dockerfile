# ─────────────────────────────────────────────────────────────────
# Intelligent Investor V2 — Production Dockerfile
# Build:  docker build -t ii-v2 .
# Run:    docker compose up -d   (use docker-compose.yml)
# ─────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# ── System dependencies ───────────────────────────────────────────
# curl: used by HEALTHCHECK and by the agent's HTTP calls
# gcc + libpq-dev: needed only if you switch from psycopg2-binary → psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────
RUN useradd -m -u 1001 -s /bin/sh appuser

# ── Working directory ─────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (separate layer for cache efficiency) ─────
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────
COPY --chown=appuser:appuser . .

# ── Runtime directories (logs, PDFs, picks JSON) ──────────────────
RUN mkdir -p logs data \
 && chown -R appuser:appuser /app

# ── Drop to non-root ─────────────────────────────────────────────
USER appuser

# ── Port (Flask/Gunicorn listens on this inside the container) ────
EXPOSE 5050

# ── Health check — Docker monitors this every 30s ─────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -sf http://localhost:5050/health | python3 -c \
        "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('status')=='ok' else 1)" \
     || exit 1

# ── Entrypoint ────────────────────────────────────────────────────
CMD ["gunicorn", "-c", "gunicorn.conf.py", "dashboard_v2:app"]
