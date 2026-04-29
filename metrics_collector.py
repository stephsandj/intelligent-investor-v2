"""
metrics_collector.py — Lightweight server-resource sampler for the admin
monitoring dashboard.

Called once per minute by the APScheduler leader worker in dashboard_v2.py.
Impact: one psutil call (≈1 s CPU sample) + one DB INSERT per minute.
No per-request overhead — safe to run on a low-resource VPS.
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

# Gunicorn app port — used to count active TCP connections as a proxy for
# concurrent users.  Can be overridden by APP_PORT env var.
_APP_PORT = int(os.environ.get("APP_PORT", "5050"))


# ─────────────────────────────────────────────────────────────────────────────
# psutil — auto-install on first use
# ─────────────────────────────────────────────────────────────────────────────

def _get_psutil():
    """Return the psutil module, installing it if it's not present."""
    try:
        import psutil
        return psutil
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "psutil>=5.9.0", "-q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import psutil  # noqa: F811
            return psutil
        except Exception as exc:
            logger.warning("metrics_collector: cannot import psutil (%s) — metrics skipped", exc)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Core sample function
# ─────────────────────────────────────────────────────────────────────────────

def collect_and_store(agent_dir: str) -> None:
    """
    Sample current system metrics and store one row in system_metrics.

    Args:
        agent_dir: path to AGENT_DIR — used to detect active screener runs
                   via the agent_running.json PID file.

    Swallows all exceptions so a metrics failure never interrupts the scheduler.
    """
    try:
        import models
        psutil = _get_psutil()
        if psutil is None:
            return  # psutil unavailable — silently skip

        # ── CPU  ─────────────────────────────────────────────────────────────
        # interval=1 takes a 1-second blocking sample for accuracy.
        # This is acceptable because this function runs once per minute
        # in a background APScheduler thread, not in the request path.
        cpu_pct = psutil.cpu_percent(interval=1)

        # ── RAM ──────────────────────────────────────────────────────────────
        vm           = psutil.virtual_memory()
        ram_pct      = vm.percent
        ram_used_mb  = int(vm.used  / 1024 / 1024)
        ram_total_mb = int(vm.total / 1024 / 1024)

        # ── Active screener runs ──────────────────────────────────────────────
        # Detect whether the screener subprocess is currently running by checking
        # the agent_running.json PID file written by dashboard_v2.py.
        screener_runs_active = 0
        try:
            pid_file = os.path.join(agent_dir, "agent_running.json")
            if os.path.exists(pid_file):
                with open(pid_file, "r") as _f:
                    pid_data = json.load(_f)
                pid = pid_data.get("pid")
                if pid:
                    try:
                        proc = psutil.Process(int(pid))
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            screener_runs_active = 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                        pass
        except Exception:
            pass  # PID file missing or malformed — treat as no active run

        # ── HTTP connections (concurrent active users proxy) ──────────────────
        # Count ESTABLISHED TCP connections where the local port == app port.
        # This approximates how many clients have open connections to Gunicorn.
        # Requires net_connections() — may need CAP_NET_ADMIN on some Linux hosts.
        http_connections = 0
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if (
                    conn.status == psutil.CONN_ESTABLISHED
                    and conn.laddr
                    and conn.laddr.port == _APP_PORT
                ):
                    http_connections += 1
        except (psutil.AccessDenied, AttributeError):
            # Fall back: count all ESTABLISHED connections if port filtering fails
            try:
                http_connections = sum(
                    1 for c in psutil.net_connections(kind="tcp")
                    if getattr(c, "status", "") == "ESTABLISHED"
                    and getattr(c.laddr, "port", 0) == _APP_PORT
                )
            except Exception:
                http_connections = 0
        except Exception:
            http_connections = 0

        # ── Active users today ────────────────────────────────────────────────
        # Count distinct users who have used at least one screener run today.
        active_users_today = 0
        try:
            active_users_today = models.count_active_users_today()
        except Exception:
            pass

        # ── Persist ───────────────────────────────────────────────────────────
        models.insert_metric_sample(
            cpu_pct=cpu_pct,
            ram_pct=ram_pct,
            ram_used_mb=ram_used_mb,
            ram_total_mb=ram_total_mb,
            screener_runs_active=screener_runs_active,
            http_connections=http_connections,
            active_users_today=active_users_today,
        )

        # ── Housekeeping: purge rows > 30 days old ────────────────────────────
        # Run every call — the DELETE is a no-op when there's nothing old to remove.
        models.purge_old_metrics(days=30)

    except Exception as exc:
        logger.warning("metrics_collector: sample failed: %s", exc)
