"""
limiter.py — Shared flask-limiter instance for Intelligent Investor V2.

All rate-limited routes import `limiter` from here and apply
@limiter.limit() decorators.  The limiter is initialised against the
Flask app object in dashboard_v2.py via limiter.init_app(app).

Storage backend: Redis (REDIS_URL env var, defaults to redis://redis:6379/0).
If Redis is unreachable at request time the limiter swallows the error and
lets the request through rather than blocking all traffic.
"""

import logging
import os

from flask import jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger("limiter")


def _rate_limit_key() -> str:
    """
    Use the same trusted-proxy-aware IP extraction as the rest of the app.
    Falls back to request.remote_addr if auth hasn't been imported yet.
    """
    try:
        from auth import _get_client_ip  # local import to avoid circular deps at module load
        ip = _get_client_ip()
        if ip:
            return ip
    except Exception:
        pass
    return get_remote_address()


def _on_rate_limit_exceeded(e):
    """Return JSON 429 instead of the default text/html response."""
    logger.warning("Rate limit exceeded: %s", e.description)
    response = jsonify({"error": "Too many requests. Please try again later.", "code": "rate_limited"})
    response.status_code = 429
    return response


_redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=[],          # no global limit — each route sets its own
    storage_uri=_redis_url,
    storage_options={
        "socket_connect_timeout": 1,  # don't hang on Redis connection issues
        "socket_timeout": 1,
    },
    swallow_errors=True,        # if Redis is unavailable, allow requests through
    on_breach=_on_rate_limit_exceeded,
)
