"""
limiter.py — Shared flask-limiter instance for Intelligent Investor V2.

All rate-limited routes import `limiter` from here and apply
@limiter.limit() decorators.  The limiter is initialised against the
Flask app object in dashboard_v2.py via limiter.init_app(app), which also
registers the JSON 429 error handler.

Storage backend: Redis (REDIS_URL env var, defaults to redis://redis:6379/0).
swallow_errors=True: if Redis is unreachable the limiter allows the request
through rather than blocking all traffic.
"""

import logging
import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger("limiter")


def _rate_limit_key() -> str:
    """
    Use the same trusted-proxy-aware IP extraction as the rest of the app.
    Falls back to request.remote_addr if auth hasn't been imported yet.
    """
    try:
        from auth import _get_client_ip  # local import avoids circular dep at module load
        ip = _get_client_ip()
        if ip:
            return ip
    except Exception:
        pass
    return get_remote_address()


_redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=[],      # no global limit — each route sets its own
    storage_uri=_redis_url,
    swallow_errors=True,    # Redis downtime → allow through rather than block all traffic
)
