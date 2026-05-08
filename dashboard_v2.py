#!/usr/bin/env python3
"""
Intelligent Investor Agent — V2 SaaS Dashboard
Multi-user, subscription-gated, JWT-authenticated version.

Run (dev):   python3 dashboard_v2.py
Run (prod):  gunicorn -c gunicorn.conf.py dashboard_v2:app
Access:      http://localhost:5050  (or your domain via Nginx)
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Load .env FIRST — before any other imports use env vars
# ─────────────────────────────────────────────────────────────────────────────
import os, sys

def _load_dotenv(path: str):
    """Zero-dependency .env loader. Also fills in any empty existing env vars so shells with
    blank ANTHROPIC_API_KEY don't shadow the real key stored in .env."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            if key not in os.environ or not os.environ.get(key, "").strip():
                os.environ[key] = val

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ─────────────────────────────────────────────────────────────────────────────
# 2. Standard library & auto-installs
# ─────────────────────────────────────────────────────────────────────────────
import glob, re, json, signal, subprocess, threading, logging, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
_TZ_EST = ZoneInfo("America/New_York")  # All user-facing times in Eastern
from functools import wraps

import logging.handlers

# ── Log rotation — 10 MB per file, keep 7 rotated files ──────────────────────
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "dashboard_v2.log")

_rotating_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
)
_rotating_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(), _rotating_handler],
)
logger = logging.getLogger("dashboard_v2")

# ── Suppress urllib3 LibreSSL warning on macOS ──────────────────────────────────
import warnings
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

# ── Raise file-descriptor limit to avoid "Too many open files" from yfinance ────
# macOS launchctl default soft limit is 256; urllib3 connection pools exhaust it
# after a few dozen yfinance calls. Set to 65536 unconditionally.
import resource as _resource
try:
    _resource.setrlimit(_resource.RLIMIT_NOFILE, (65536, _resource.RLIM_INFINITY))
    logger.info("File-descriptor limit raised to 65536")
except ValueError:
    try:
        # Some macOS setups cap the hard limit; try a lower value
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (10240, 10240))
        logger.info("File-descriptor limit raised to 10240")
    except Exception as _e2:
        logger.warning("Could not raise fd limit: %s", _e2)
except Exception as _e:
    logger.warning("Could not raise fd limit: %s", _e)

from flask import (Flask, jsonify, render_template_string, send_file,
                   request, redirect, url_for, g, make_response)
from apscheduler.schedulers.background import BackgroundScheduler

# Metrics collector — lightweight, imported lazily so a missing psutil doesn't
# break startup.  collect_and_store() is wired into the scheduler below.
try:
    import metrics_collector as _metrics_collector
except Exception:
    _metrics_collector = None  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────────────────────
# 3. Paths & constants
# ─────────────────────────────────────────────────────────────────────────────
AGENT_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(AGENT_DIR, "logs")
PYTHON_BIN = sys.executable
PORT       = int(os.environ.get("PORT", 5050))

# ── Per-user data directory helpers ──────────────────────────────────────────
# Every user gets their own isolated subdirectory under AGENT_DIR/users/{user_id}/
# so that one user's screen runs, picks, PDFs, and logs never touch another's.

def _validate_user_id(user_id: str) -> None:
    """Validate user_id is a valid UUID to prevent path traversal attacks."""
    try:
        uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Invalid user_id format: expected UUID, got {repr(user_id)}")

def _user_data_dir(user_id: str) -> str:
    _validate_user_id(user_id)  # Prevent path traversal
    path = os.path.join(AGENT_DIR, "users", str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def _user_reports_dir(user_id: str) -> str:
    path = os.path.join(_user_data_dir(user_id), "reports")
    os.makedirs(path, exist_ok=True)
    return path

def _user_logs_dir(user_id: str) -> str:
    path = os.path.join(_user_data_dir(user_id), "logs")
    os.makedirs(path, exist_ok=True)
    return path

def _picks_detail_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "picks_detail.json")

def _etf_results_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "etf_results.json")

def _bond_results_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "bond_results.json")

def _ticker_results_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "ticker_result.json")

def _user_state_file(user_id: str) -> str:
    return os.path.join(_user_data_dir(user_id), "last_run_state.json")


def _cleanup_stale_running_files():
    """Delete all *_running.json flag files at startup.

    These files are created when a screen run starts and deleted when it ends.
    If the server crashes or is restarted while a run is in progress the file
    remains on disk and causes the status endpoints to falsely report
    running=True for up to 30 minutes — making the frontend spin forever
    after a browser refresh. Purging at startup guarantees a clean slate.
    """
    users_dir = os.path.join(AGENT_DIR, "users")
    if not os.path.isdir(users_dir):
        return
    _stale_names = ("etf_running.json", "bond_running.json",
                    "ticker_running.json", "agent_running.json")
    for entry in os.scandir(users_dir):
        if not entry.is_dir():
            continue
        for fname in _stale_names:
            fpath = os.path.join(entry.path, fname)
            try:
                os.remove(fpath)
            except FileNotFoundError:
                pass  # already gone — normal case
            except Exception as _e:
                pass  # non-fatal; status endpoint will fall back to in-memory state


# Run at import time so both `python3 dashboard_v2.py` and Gunicorn workers
# start with a clean slate — no leftover running-flag files from prior crashes.
_cleanup_stale_running_files()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Flask app
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB max body

# Initialize Sentry for error tracking (if DSN is configured)
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.1,  # 10% of transactions
            environment=os.environ.get("ENVIRONMENT", "production"),
        )
        logger.info("Sentry error tracking initialized")
    except ImportError:
        logger.warning("Sentry SDK not installed; error tracking disabled")
    except Exception as e:
        logger.error("Failed to initialize Sentry: %s", e)

@app.after_request
def _security_headers(response):
    """Add security headers to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

    # HSTS — instruct browsers to always use HTTPS for this domain (1 year)
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )

    # Admin panel has no inline scripts/styles — apply strict CSP with no unsafe-inline.
    # Main SPA still uses unsafe-inline (index.html has 135 inline handlers; tracked as TODO).
    if request.path.startswith("/admin"):
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "script-src 'self' https://js.stripe.com; "
                "style-src 'self'; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https://api.stripe.com; "
                "frame-src https://js.stripe.com; "
                "font-src 'self' data:; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self';"
            ),
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https://api.stripe.com; "
                "frame-src https://js.stripe.com; "
                "font-src 'self' data:; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self';"
            ),
        )
    return response


# CSRF defence — validate Origin header on all mutating requests.
# Requests without an Origin header are permitted (server-to-server / old browsers);
# SameSite=Lax on cookies already covers most of those cases.
_CSRF_ALLOWED_ORIGIN = os.environ.get("APP_BASE_URL", "").rstrip("/")

@app.before_request
def _csrf_origin_check():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed = _CSRF_ALLOWED_ORIGIN or ""

    # Fail-closed: require valid origin or deny
    if not origin and not allowed:
        logger.warning("CSRF check: Origin header missing AND APP_BASE_URL unset (fail-closed)")
        return jsonify({"error": "Forbidden", "code": "invalid_origin"}), 403

    if origin and allowed and origin != allowed:
        logger.warning("CSRF origin mismatch: expected %s got %s path=%s",
                       allowed, origin, request.path)
        return jsonify({"error": "Forbidden", "code": "invalid_origin"}), 403


# Fix #4: JWT_SECRET must be set explicitly — no random fallback that would
# invalidate all sessions on every restart and hide misconfiguration.
_jwt_secret_val = os.environ.get("JWT_SECRET")
if not _jwt_secret_val:
    # In local-only mode (no .env) warn loudly but don't crash Flask startup.
    # Auth routes will still raise RuntimeError when they try to sign tokens.
    logger.warning(
        "JWT_SECRET is not set. Authentication endpoints will not work. "
        "Copy .env.example to .env and set a strong JWT_SECRET."
    )
    _jwt_secret_val = "INSECURE_PLACEHOLDER_SET_JWT_SECRET_IN_ENV"
app.config["SECRET_KEY"] = _jwt_secret_val

# ─────────────────────────────────────────────────────────────────────────────
# 5. Database & blueprints — imported after env is loaded
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, AGENT_DIR)

_DB_AVAILABLE = False
try:
    import models
    import plans as plan_gate
    from auth import auth_bp, auth_required, admin_required, csrf_required, _get_client_ip
    from admin_routes import admin_bp
    from billing import billing_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(billing_bp)

    # Initialise schema on startup
    try:
        models.init_db()
        _DB_AVAILABLE = True
        logger.info("Database initialised successfully.")
        # Create system_metrics table for the monitoring dashboard
        try:
            models.init_metrics_table()
            logger.info("System metrics table ready.")
        except Exception as _metrics_table_err:
            logger.warning("system_metrics table init skipped: %s", _metrics_table_err)
        # Migrate any remaining 'trial' status values to 'active'
        try:
            migrated_count = models.migrate_trial_status_to_active()
            if migrated_count > 0:
                logger.info(f"Migration: {migrated_count} subscriptions updated from status='trial' to status='active'")
        except Exception as _migration_err:
            logger.warning(f"Trial status migration skipped: {_migration_err}")
    except Exception as _e:
        logger.warning("DB init failed (%s). Running in local-only mode.", _e)

except Exception as _import_err:
    logger.critical(
        "FATAL: Auth/DB modules failed to load: %s. "
        "Fix the dependency and restart the container.",
        _import_err,
        exc_info=True,
    )
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 6. Global JSON error handlers — always return JSON, never HTML
# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(RuntimeError)
def _handle_runtime_error(exc):
    """Convert RuntimeError (e.g. DATABASE_URL not set) to a JSON 503 response."""
    msg = str(exc)
    logger.error("RuntimeError in request: %s", msg)
    # DB-not-configured errors are a service-unavailable condition
    if "DATABASE_URL" in msg or "not set" in msg.lower():
        return jsonify({"error": "Service temporarily unavailable — database not configured."}), 503
    return jsonify({"error": msg}), 500


@app.errorhandler(500)
def _handle_500(exc):
    """Catch-all 500 handler — return JSON instead of Flask's default HTML page."""
    logger.error("500 error: %s", exc, exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(404)
def _handle_404(exc):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def _handle_405(exc):
    return jsonify({"error": "Method not allowed"}), 405


# ─────────────────────────────────────────────────────────────────────────────
# 7. Run-state management
#    The standalone agent (value_investor_agent.py) has been removed.
#    Run state is kept as a lightweight in-memory dict so that /api/status
#    and the UI continue to work without the subprocess machinery.
# ─────────────────────────────────────────────────────────────────────────────
_run_lock         = threading.Lock()           # protects _user_run_states dict
_user_run_states: dict = {}                    # user_id -> {running, started_at, finished_at, exit_code}
_user_processes:  dict = {}                    # user_id -> subprocess.Popen handle
_user_log_files:  dict = {}                    # user_id -> path of the current agent_run_*.log file

def _default_run_state() -> dict:
    return {"running": False, "started_at": None, "finished_at": None, "exit_code": None,
            "last_completed_at": None, "run_summary": {}}

def _persist_run_state(user_id: str) -> None:
    """Write a user's finished-run state (including run summary stats) to disk for restart persistence."""
    try:
        state = _user_run_states.get(user_id, _default_run_state())
        _atomic_write_json(_user_state_file(user_id), {
            "started_at":        state.get("started_at"),
            "finished_at":       state.get("finished_at"),
            "exit_code":         state.get("exit_code"),
            # Only the last *successful* completion time — shown in LAST RUN KPI card
            "last_completed_at": state.get("last_completed_at"),
            # Persist run summary so stats bar survives any restart
            "run_summary":       state.get("run_summary", {}),
            # Persist completed-run duration so stats bar time field survives restarts
            "last_completed_duration_secs": state.get("last_completed_duration_secs"),
        })
    except Exception as _e:
        logger.warning("Could not persist run state for %s: %s", user_id, _e)

def _load_persisted_run_states() -> None:
    """On startup, restore the last finished run state (including run summary) for each user."""
    users_dir = os.path.join(AGENT_DIR, "users")
    if not os.path.isdir(users_dir):
        return
    for uid in os.listdir(users_dir):
        sf = os.path.join(users_dir, uid, "last_run_state.json")
        if os.path.exists(sf):
            try:
                with open(sf) as f:
                    saved = json.load(f)
                # One-time migration: if last_completed_at is absent (older state file)
                # but finished_at is from a successful run (exit_code == 0), backfill it.
                _last_completed = saved.get("last_completed_at")
                if not _last_completed and saved.get("exit_code") == 0:
                    _last_completed = saved.get("finished_at")
                _user_run_states[uid] = {
                    "running":           False,
                    "started_at":        saved.get("started_at"),
                    "finished_at":       saved.get("finished_at"),
                    "exit_code":         saved.get("exit_code"),
                    "last_completed_at": _last_completed,
                    # Restore persisted run summary so stats bar works immediately after restart
                    "run_summary":       saved.get("run_summary", {}),
                    # Restore last completed run duration so stats bar time field works after restart
                    "last_completed_duration_secs": saved.get("last_completed_duration_secs"),
                }
            except Exception as _e:
                logger.warning("Could not load persisted run state for %s: %s", uid, _e)

_load_persisted_run_states()

# Stock screener agent — lives in V2's own directory (fully standalone)
_AGENT_PY = os.path.join(AGENT_DIR, "value_investor_agent.py")


def _get_run_state(user_id: "str | None" = None) -> dict:
    if not user_id:
        return _default_run_state()
    state = dict(_user_run_states.get(user_id, _default_run_state()))

    # Cross-worker persistence: a different Gunicorn worker may have finished
    # the run and written last_completed_at / run_summary to disk while this
    # worker's in-memory dict still has the stale pre-run snapshot.
    # Always sync from disk when the fields are absent so every worker shows
    # the correct "Last Run" KPI card values.
    if not state.get("last_completed_at") or not state.get("run_summary") or state.get("last_completed_duration_secs") is None:
        try:
            sf = _user_state_file(user_id)
            if os.path.exists(sf):
                with open(sf) as _dsf:
                    saved = json.load(_dsf)
                if not state.get("last_completed_at") and saved.get("last_completed_at"):
                    state["last_completed_at"] = saved["last_completed_at"]
                if not state.get("run_summary") and saved.get("run_summary"):
                    state["run_summary"] = saved["run_summary"]
                if state.get("last_completed_duration_secs") is None and saved.get("last_completed_duration_secs") is not None:
                    state["last_completed_duration_secs"] = saved["last_completed_duration_secs"]
                # Back-fill this worker's in-memory dict so subsequent requests are
                # served from memory without a disk read.
                mem = _user_run_states.setdefault(user_id, _default_run_state())
                if not mem.get("last_completed_at") and saved.get("last_completed_at"):
                    mem["last_completed_at"] = saved["last_completed_at"]
                if not mem.get("run_summary") and saved.get("run_summary"):
                    mem["run_summary"] = saved["run_summary"]
                if mem.get("last_completed_duration_secs") is None and saved.get("last_completed_duration_secs") is not None:
                    mem["last_completed_duration_secs"] = saved["last_completed_duration_secs"]
        except Exception as _dse:
            logger.debug("Could not sync run state from disk for %s: %s", user_id, _dse)

    # Cross-worker: if this Gunicorn worker has no memory of the run, check the
    # PID file written by whichever worker actually launched the subprocess.
    if not state.get("running"):
        pid_file = os.path.join(_user_data_dir(user_id), "agent_running.json")
        try:
            if os.path.exists(pid_file):
                with open(pid_file) as _pf:
                    pid_data = json.load(_pf)
                pid = pid_data.get("pid")
                if pid:
                    try:
                        os.kill(pid, 0)  # signal 0: check existence only
                        state = {
                            "running":           True,
                            "started_at":        pid_data.get("started_at"),
                            "finished_at":       None,
                            "exit_code":         None,
                            "source":            pid_data.get("source", "manual"),
                            "run_summary":       state.get("run_summary", {}),
                            "last_completed_at": state.get("last_completed_at"),
                        }
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            os.remove(pid_file)
                        except Exception:
                            pass
        except Exception:
            pass
    return state


def _get_daily_log_path():
    """Return the path to today's agent daily log file."""
    import re as _re
    daily_logs = sorted(
        [f for f in glob.glob(os.path.join(LOG_DIR, "agent_*.log"))
         if _re.match(r"agent_\d{8}\.log$", os.path.basename(f))],
        reverse=True,
    )
    return daily_logs[0] if daily_logs else None


def _start_agent(user_id: "str | None" = None, source: str = "manual"):
    """Launch value_investor_agent.py in a subprocess for a specific user.
    Each user gets their own process, state entry, and output directory.
    source: "manual" (user-triggered via UI) or "scheduled" (background cron job).
    Scheduled runs are completely invisible to the frontend — they must not affect
    _isRunning state on the client or block the user from logging out."""
    if not user_id:
        return False, "user_id required for multi-user mode"
    with _run_lock:
        current = _user_run_states.get(user_id, _default_run_state())
        if current.get("running"):
            return False, "Agent is already running"
        if not os.path.exists(_AGENT_PY):
            return False, f"Agent script not found at {_AGENT_PY}"

        # Ensure per-user output directories exist
        out_dir     = _user_data_dir(user_id)
        reports_dir = _user_reports_dir(user_id)
        logs_dir    = _user_logs_dir(user_id)

        # Sync this user's DB config to disk (backup) and retrieve it for env-var injection.
        # The returned dict is then passed directly to the subprocess via env vars, which
        # eliminates the race condition where concurrent users overwrite each other's
        # shared config.json before their agent subprocess reads it.
        try:
            cfg_env = _save_config({}, user_id)
        except Exception as _sync_err:
            logger.warning("Config sync before agent start failed: %s", _sync_err)
            try:
                cfg_env = _load_config(user_id)
            except Exception:
                cfg_env = {}

        # Capture subprocess stdout to a per-user timestamped log file
        ts           = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file_path = os.path.join(logs_dir, f"agent_run_{ts}.log")
        log_file_obj  = open(log_file_path, 'w')

        # Build env — pass full per-user config as env vars so each user's agent
        # reads their own settings regardless of what's in the shared config.json.
        env = os.environ.copy()
        env["AGENT_OUTPUT_DIR"]      = out_dir        # picks_detail.json, last_run_state.json
        env["AGENT_REPORTS_DIR"]     = reports_dir    # PDF files → users/{id}/reports/
        env["AGENT_EMAIL_ENABLED"]   = "1" if cfg_env.get("email_enabled") else "0"
        env["AGENT_PDF_ENABLED"]     = "1" if cfg_env.get("pdf_enabled") else "0"
        env["AGENT_EMAIL_ADDRESS"]   = str(cfg_env.get("email_address") or "")
        env["AGENT_MARKETS"]         = ",".join(cfg_env.get("markets") or ["NYSE", "NASDAQ"])
        env["AGENT_LOSER_PERIOD"]    = str(cfg_env.get("loser_period") or "daily100")
        env["AGENT_STOCK_GEOGRAPHY"] = str(cfg_env.get("stock_geography") or "usa")
        env["AGENT_CLAUDE_KEY"]      = str(cfg_env.get("claude_api_key") or "")

        proc = subprocess.Popen(
            [PYTHON_BIN, "-u", _AGENT_PY],   # -u = unbuffered stdout so logs appear immediately
            cwd=AGENT_DIR,
            stdout=log_file_obj,
            stderr=subprocess.STDOUT,
            env=env,
        )
        _user_processes[user_id]  = proc
        _user_log_files[user_id]  = log_file_path
        _prev_state = _user_run_states.get(user_id, _default_run_state())
        _user_run_states[user_id] = {
            "running":           True,
            "started_at":        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "finished_at":       None,
            "exit_code":         None,
            "source":            source,   # "manual" | "scheduled"
            # Carry forward last successful completion time so LAST RUN KPI card stays
            # correct while the new run is in progress.
            "last_completed_at": _prev_state.get("last_completed_at"),
        }
        # Write PID file so ALL Gunicorn workers can detect this run is active
        try:
            with open(os.path.join(out_dir, "agent_running.json"), "w") as _pf:
                json.dump({"pid": proc.pid, "started_at": _user_run_states[user_id]["started_at"], "source": source}, _pf)
        except Exception as _pf_err:
            logger.warning("Could not write agent PID file: %s", _pf_err)
        logger.info("Stock screen started for user %s (pid=%d, source=%s)",
                    user_id[:8], proc.pid, source)

    def _monitor(uid, p):
        p.wait()
        finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with _run_lock:
            prev = _user_run_states.get(uid, {})
            # For non-zero exits (stop/crash), carry forward the last successful
            # run_summary and last_completed_duration_secs so _monitor() never
            # overwrites the preserved stats that _stop_agent() already restored.
            _carry_summary  = prev.get("run_summary") or {}
            _carry_duration = prev.get("last_completed_duration_secs")
            _user_run_states[uid] = {
                "running":           False,
                "started_at":        prev.get("started_at"),
                "finished_at":       finished_at,
                "exit_code":         p.returncode,
                # Successful run: start with empty dict, filled in below via _parse_run_summary.
                # Stopped/failed run: carry forward the last completed run's summary so the
                # stats bar keeps showing real data instead of reverting to "—".
                "run_summary":       {} if p.returncode == 0 else _carry_summary,
                "source":            prev.get("source", "manual"),  # preserve source after run ends
                # Only update last_completed_at on a clean exit — stops/crashes keep the
                # previous successful timestamp so LAST RUN always reflects a completed run.
                "last_completed_at": finished_at if p.returncode == 0 else prev.get("last_completed_at"),
                # Preserve duration from last completed run for stopped/failed exits.
                "last_completed_duration_secs": None if p.returncode == 0 else _carry_duration,
            }
            _user_processes.pop(uid, None)
        # After a successful run, parse the completion stats from the agent log
        # and persist them in last_run_state.json so the stats bar survives restarts.
        if p.returncode == 0:
            try:
                summary = _parse_run_summary(uid)
                if summary:
                    with _run_lock:
                        _user_run_states[uid]["run_summary"] = summary
                    logger.debug("Persisting run_summary for user %s: %s", uid[:8], summary)
            except Exception as _sum_err:
                logger.warning("Could not parse run_summary for user %s: %s", uid[:8], _sum_err)
            # Store this run's duration so the stats bar can show it even after
            # a subsequent run is stopped (which would otherwise clobber start/end times).
            try:
                with _run_lock:
                    _sa = _user_run_states[uid].get("started_at", "")
                    if _sa:
                        _s = datetime.fromisoformat(_sa.replace("Z", "+00:00"))
                        _f = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                        _user_run_states[uid]["last_completed_duration_secs"] = max(0, int((_f - _s).total_seconds()))
            except Exception:
                pass
        # Remove PID file — signals all workers this run is done
        try:
            os.remove(os.path.join(_user_data_dir(uid), "agent_running.json"))
        except FileNotFoundError:
            pass
        except Exception as _pf_err:
            logger.warning("Could not remove agent PID file: %s", _pf_err)
        _persist_run_state(uid)
        logger.info("Stock screen finished for user %s (exit=%d)", uid[:8], p.returncode)

    threading.Thread(target=_monitor, args=(user_id, proc), daemon=True).start()
    return True, "Agent started"


def _stop_agent(user_id: "str | None" = None):
    """Terminate the running subprocess for a specific user.

    Cross-worker safe: if this Gunicorn worker did not launch the subprocess
    (so _user_processes[user_id] is None), fall back to the agent_running.json
    PID file and kill the process directly via os.kill().  This ensures that a
    stop request routed to a different worker than the one that started the run
    always terminates the process instead of silently returning "No agent running".
    """
    if not user_id:
        return False, "user_id required"
    pid_file = os.path.join(_user_data_dir(user_id), "agent_running.json")
    with _run_lock:
        state = _user_run_states.get(user_id, _default_run_state())
        proc  = _user_processes.get(user_id)

        # Determine whether a run is actually active on ANY worker:
        # 1. This worker owns the proc object, OR
        # 2. The PID file exists (another worker launched it and it's still alive)
        pid_from_file = None
        if proc is None and os.path.exists(pid_file):
            try:
                with open(pid_file) as _pf:
                    _pd = json.load(_pf)
                _pid = _pd.get("pid")
                if _pid:
                    try:
                        os.kill(_pid, 0)   # signal 0: check existence only
                        pid_from_file = _pid
                    except (ProcessLookupError, PermissionError):
                        pass   # process already dead — still clean up below
            except Exception:
                pass

        if proc is None and pid_from_file is None and not state.get("running"):
            return False, "No agent running"

        # Terminate: prefer the proc object; fall back to PID from file
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        elif pid_from_file is not None:
            try:
                os.kill(pid_from_file, signal.SIGTERM)
            except Exception:
                pass

        # Recover run_summary and last_completed_duration_secs from previous completed
        # run so the stats bar keeps showing real data after a stop.
        # If this worker has no in-memory state (cross-worker stop), fall back to disk.
        _prev_summary  = state.get("run_summary") or {}
        _prev_duration = state.get("last_completed_duration_secs")
        if not _prev_summary or _prev_duration is None:
            try:
                sf = _user_state_file(user_id)
                if os.path.exists(sf):
                    with open(sf) as _sf:
                        _saved = json.load(_sf)
                    if not _prev_summary:
                        _prev_summary  = _saved.get("run_summary") or {}
                    if _prev_duration is None:
                        _prev_duration = _saved.get("last_completed_duration_secs")
            except Exception:
                pass

        _user_run_states[user_id] = {
            "running":                      False,
            "started_at":                   state.get("started_at"),
            "finished_at":                  datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "exit_code":                    -1,
            # Preserve last successful completion time — stop events must not overwrite it
            "last_completed_at":            state.get("last_completed_at"),
            # Preserve stats from the last completed run so the stats bar stays populated
            "run_summary":                  _prev_summary,
            "last_completed_duration_secs": _prev_duration,
        }
        _user_processes.pop(user_id, None)
    # Remove PID file on manual stop
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    _persist_run_state(user_id)
    logger.info("Stock screen stopped by user %s", user_id[:8])
    return True, "Agent stopped"

# ─────────────────────────────────────────────────────────────────────────────
# 7. Helper utilities (identical to V1 — preserved exactly)
# ─────────────────────────────────────────────────────────────────────────────
def _next_run_time(user_id=None):
    """Return next scheduled run. Schedule uses properly merged config."""
    cfg = _load_config(user_id)
    try:
        hour   = int(cfg.get("schedule_hour", 18))
        minute = int(cfg.get("schedule_minute", 0))
        days   = [int(d) for d in cfg.get("schedule_days", [0, 1, 2, 3, 4])]
        if not days:
            days = [0, 1, 2, 3, 4]
    except Exception:
        hour, minute, days = 18, 0, [0, 1, 2, 3, 4]
    now = datetime.now(_TZ_EST)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() not in days:
        candidate += timedelta(days=1)
    return candidate


def _latest_log_lines(n: int = 300, from_bytes: int = 0):
    """Read the last N lines from the global dashboard rotating log (used by health/admin)."""
    try:
        with open(_log_file, "rb") as f:
            if from_bytes > 0:
                f.seek(from_bytes)
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def _latest_user_log_lines(user_id: str, n: int = 300) -> list:
    """Read the last N lines from the most recent agent_run_*.log for a specific user."""
    logs_dir = _user_logs_dir(user_id)
    run_logs = sorted(glob.glob(os.path.join(logs_dir, "agent_run_*.log")), reverse=True)
    if not run_logs:
        return []
    try:
        with open(run_logs[0], "rb") as f:
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def _list_reports(user_id: str):
    # Scan the user's own reports directory — fully isolated from other users
    rdir = _user_reports_dir(user_id)
    all_pdfs = glob.glob(os.path.join(rdir, "intelligent_investor_*.pdf"))
    pdfs_sorted = sorted(all_pdfs, key=lambda p: os.path.basename(p), reverse=True)
    # Enforce per-user PDF history limit from their plan
    max_hist = 5
    if _DB_AVAILABLE:
        try:
            sub = models.get_user_subscription(user_id)
            if sub and sub.get("max_pdf_history") is not None:
                max_hist = sub["max_pdf_history"]
        except Exception:
            pass
    to_keep = pdfs_sorted[:max_hist]
    for old_pdf in pdfs_sorted[max_hist:]:
        try:
            os.remove(old_pdf)
            logger.info("Deleted old PDF report for user %s: %s", user_id[:8], os.path.basename(old_pdf))
        except Exception as _e:
            logger.warning("Could not delete old PDF %s: %s", old_pdf, _e)
    out = []
    for p in to_keep:
        fname = os.path.basename(p)
        dp = fname.replace("intelligent_investor_", "").replace(".pdf", "")
        # Strip optional duplicate-collision suffix (e.g. "_1", "_2") so the
        # date parser only sees the timestamp portion.
        core = dp
        # If filename has more than 2 underscore-segments, the trailing one
        # is the collision suffix — drop it.
        parts = core.split("_")
        if len(parts) >= 3 and parts[-1].isdigit() and len(parts[-1]) <= 3 and parts[-1] != parts[2]:
            # Heuristic: 4th segment present and short → collision suffix
            if len(parts) == 4:
                core = "_".join(parts[:3])
        try:
            if len(core) == 19 and core[15] == "_":
                # YYYYMMDD_HHMMSS_mmm — seconds + milliseconds (Eastern time)
                base, ms = core[:15], core[16:]
                dt_est = datetime.strptime(base, "%Y%m%d_%H%M%S")
                d = dt_est.strftime("%b %d, %Y  %H:%M:%S") + f".{ms} EST"
            elif len(core) == 15:
                # YYYYMMDD_HHMMSS — legacy seconds-only format (Eastern time)
                dt_est = datetime.strptime(core, "%Y%m%d_%H%M%S")
                d = dt_est.strftime("%b %d, %Y  %H:%M:%S") + " EST"
            elif len(core) == 8:  # YYYYMMDD — old format, use file mtime for time
                dt_date = datetime.strptime(core, "%Y%m%d")
                mtime = os.path.getmtime(p)
                dt_mtime = datetime.fromtimestamp(mtime, tz=_TZ_EST)
                # Use mtime for time if it falls on the same date
                if dt_mtime.date() == dt_date.date():
                    d = dt_mtime.strftime("%b %d, %Y  %H:%M:%S") + " EST"
                else:
                    d = dt_date.strftime("%b %d, %Y")
            else:
                d = dp
        except Exception:
            d = dp
        out.append({"filename": fname, "date": d,
                    "size_kb": round(os.path.getsize(p) / 1024, 1)})
    return out


def _parse_last_picks(user_id: str):
    try:
        path = _picks_detail_file(user_id)
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            picks = []
            for p in d.get("picks", []):
                roe = p.get("roe")
                picks.append({
                    "rank": p["rank"], "symbol": p["symbol"], "name": p["name"],
                    "score": p["score"], "grade": p["grade"],
                    "roe": f"{roe*100:.1f}%" if roe is not None else "N/A",
                })
            return picks
    except Exception:
        pass
    return []


def _parse_snap_picks(user_id: str):
    try:
        path = _picks_detail_file(user_id)
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            picks = []
            for p in d.get("picks", []):
                div = p.get("dividend_yield")
                roe = p.get("roe")
                nm  = p.get("net_margin")
                picks.append({
                    "rank": p["rank"], "symbol": p["symbol"], "name": p["name"],
                    "change": p.get("price_change_pct"),
                    "fwd_pe": p.get("forward_pe"), "pb": p.get("pb_ratio"),
                    "cr": p.get("current_ratio"), "de": p.get("debt_to_equity"),
                    "roe": roe * 100 if roe is not None else None,
                    "nm":  nm  * 100 if nm  is not None else None,
                    "div": div, "grade": p["grade"], "score": p["score"],
                })
            return picks
    except Exception:
        pass
    return []


def _parse_run_summary(user_id: "str | None" = None):
    lines = _latest_user_log_lines(user_id, 500) if user_id else _latest_log_lines(500)
    trigger_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "COMPLETE":
            trigger_idx = i
    if trigger_idx is None:
        return {}
    summary = {}
    for line in lines[trigger_idx + 1:]:
        m = re.match(r"\s+Universe screened\s*:\s*([\d,]+)", line)
        if m:
            summary["universe"] = m.group(1)
        m = re.match(r"\s+(?:Down today|Down 5-day|Down 52-wk|Eligible|Candidates)\s*:\s*([\d,]+)", line)
        if m:
            summary["down_today"] = m.group(1)
        m = re.match(r"\s+Stocks analyzed\s*:\s*(\d+)", line)
        if m:
            summary["analyzed"] = m.group(1)
        m = re.match(r"\s+Top picks\s*:\s*(\d+)", line)
        if m:
            summary["top_picks"] = m.group(1)
        if re.search(r"={10}", line) and summary:
            break
    return summary


def _load_screener_results(path: str):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


# PDF pruning is now per-user and happens when _list_reports(user_id) is called.


# ─────────────────────────────────────────────────────────────────────────────
# 8. User config — reads from DB when available, falls back to config.json
# ─────────────────────────────────────────────────────────────────────────────
_CONFIG_FILE  = os.path.join(AGENT_DIR, "config.json")
_DEFAULT_CFG  = {
    "email_enabled": False, "pdf_enabled": True,
    "markets": ["NYSE", "NASDAQ"], "loser_period": "daily100",
    "stock_geography": "usa",
    "schedule_hour": 18, "schedule_minute": 0,
    "schedule_days": [0, 1, 2, 3, 4],  # Mon–Fri
    "enabled": True,  # Auto-run scheduler enabled/disabled
}

def _atomic_write_json(path: str, data: dict) -> bool:
    """Write JSON atomically — write to .tmp, then rename. Prevents corrupted/truncated files."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as _f:
            json.dump(data, _f, indent=2, default=str)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as _e:
        logger.error(f"Atomic write failed for {path}: {_e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _sanitize_config_for_disk(cfg: dict) -> dict:
    """Strip non-JSON-serializable fields (UUID, datetime) before writing to disk.
    user_id/created_at/updated_at come from the DB row and would corrupt the JSON file."""
    clean = {}
    _allowed_keys = {
        "email_enabled", "pdf_enabled", "markets", "loser_period",
        "stock_geography", "schedule_hour", "schedule_minute", "schedule_days",
        "email_address", "claude_api_key", "enabled",
    }
    for k, v in cfg.items():
        if k in _allowed_keys:
            clean[k] = v
    return clean


def _load_config(user_id: "str | None" = None) -> dict:
    cfg = dict(_DEFAULT_CFG)
    # Always load disk config first (has schedule fields)
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE) as f:
                raw = f.read()
            if raw.strip():
                disk_cfg = json.loads(raw)
                cfg.update(disk_cfg)
                logger.debug(f"Loaded disk config: schedule_hour={cfg.get('schedule_hour')}")
        except Exception as e:
            logger.error(f"Error loading disk config (will use defaults + repair): {e}")
            # Auto-repair: rewrite file with defaults so future loads succeed
            try:
                _atomic_write_json(_CONFIG_FILE, _sanitize_config_for_disk(cfg))
            except Exception:
                pass
    # Layer in DB config if available (overrides disk for user-specific fields)
    if _DB_AVAILABLE and user_id:
        try:
            db_cfg = models.get_user_config(user_id)
            if db_cfg:
                db_cfg.setdefault("email_enabled", False)
                db_cfg.setdefault("pdf_enabled", True)
                cfg.update(db_cfg)
                logger.debug(f"Updated with DB config: schedule_hour={cfg.get('schedule_hour')}")
        except Exception as e:
            logger.error(f"Error loading DB config: {e}")
            pass
    # Normalize: DB stores as 'schedule_enabled', code uses 'enabled'
    if "schedule_enabled" in cfg:
        cfg["enabled"] = bool(cfg["schedule_enabled"])
    # Normalize schedule_days: DB stores as comma string, frontend expects list
    raw_days = cfg.get("schedule_days", [0, 1, 2, 3, 4])
    if isinstance(raw_days, str):
        try:
            cfg["schedule_days"] = [int(d.strip()) for d in raw_days.split(",") if d.strip().isdigit()]
        except Exception:
            cfg["schedule_days"] = [0, 1, 2, 3, 4]
    logger.debug(f"Final config: schedule_hour={cfg.get('schedule_hour')}, user_id={user_id}")
    return cfg


def _save_config(updates: dict, user_id: "str | None" = None) -> dict:
    if _DB_AVAILABLE and user_id:
        try:
            existing = _load_config(user_id)
            existing.update(updates)
            # Normalise schedule_days to a comma string for DB storage
            raw_days = existing.get("schedule_days", [0, 1, 2, 3, 4])
            if isinstance(raw_days, list):
                raw_days = ",".join(str(d) for d in raw_days)
            models.upsert_user_config(
                user_id=user_id,
                loser_period=existing.get("loser_period", "daily100"),
                markets=existing.get("markets", ["NYSE", "NASDAQ"]),
                stock_geography=existing.get("stock_geography", "usa"),
                email_enabled=bool(existing.get("email_enabled", False)),
                pdf_enabled=bool(existing.get("pdf_enabled", True)),
                email_address=existing.get("email_address"),
                schedule_hour=int(existing.get("schedule_hour", 18)),
                schedule_minute=int(existing.get("schedule_minute", 0)),
                schedule_days=raw_days,
                schedule_enabled=bool(existing.get("enabled", True)),
            )
            # Persist full config to disk so schedule fields survive restarts
            _atomic_write_json(_CONFIG_FILE, _sanitize_config_for_disk(existing))
            return existing
        except Exception as _e:
            logger.error(f"DB path for _save_config failed: {_e}")
    # Fallback: disk
    cfg = _load_config()
    cfg.update(updates)
    _atomic_write_json(_CONFIG_FILE, _sanitize_config_for_disk(cfg))
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 9. Token helper — extracts user_id from cookie/header without requiring auth
# ─────────────────────────────────────────────────────────────────────────────
def _current_user_id() -> "str | None":
    """Return the authenticated user_id or None (never raises)."""
    if not _DB_AVAILABLE:
        return None
    try:
        import jwt as _jwt
        from auth import decode_token, _extract_token_from_request
        token = _extract_token_from_request()
        if not token:
            return None
        payload = decode_token(token)
        if payload.get("type") == "access":
            return payload.get("sub")
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 10. Plan info helper — returns subscription summary for dashboard header
# ─────────────────────────────────────────────────────────────────────────────
def _get_plan_info(user_id: "str | None") -> dict:
    if not _DB_AVAILABLE or not user_id:
        return {"plan": "Local", "status": "active", "runs_today": 0,
                "runs_limit": None, "features": {}}
    try:
        access = plan_gate.check_plan_access(user_id)
        sub = access.get("subscription", {})
        features = access.get("features", {})
        # Parse features if still a string
        if isinstance(features, str):
            try: features = json.loads(features)
            except Exception: features = {}

        # Monthly ticker info
        ticker_limit = plan_gate.get_ticker_monthly_limit(user_id)
        ticker_used  = models.get_monthly_ticker_count(user_id) if _DB_AVAILABLE else 0

        return {
            "plan": sub.get("display_name", "Unknown"),
            "plan_name": sub.get("plan_name", ""),
            "status": sub.get("status", ""),
            "runs_today": access["runs_today"],
            "runs_limit": access["runs_limit"],
            "allowed": access["allowed"],
            "reason": access.get("reason", ""),
            "features": features,
            "ticker_runs_this_month": ticker_used,
            "ticker_runs_limit": ticker_limit,  # None = unlimited, 0 = not allowed
            "trial_ends_at": str(sub.get("trial_ends_at", "")) if sub.get("trial_ends_at") else None,
            "expires_at": str(sub.get("expires_at", "")) if sub.get("expires_at") else None,
            "resets_at": access.get("resets_at"),  # e.g. "12:00 AM EDT"
        }
    except Exception:
        return {"plan": "Unknown", "status": "active", "runs_today": 0,
                "runs_limit": None, "features": {}}


# ─────────────────────────────────────────────────────────────────────────────
# 11. ETF / Bond screener state
# ─────────────────────────────────────────────────────────────────────────────
_user_etf_states:  dict = {}  # user_id -> {running, error, started_at}
_user_bond_states: dict = {}  # user_id -> {running, error, started_at}
_user_etf_cancel:  dict = {}  # user_id -> threading.Event
_user_bond_cancel: dict = {}  # user_id -> threading.Event

# Per-user state locks — protect the _user_etf_states / _user_bond_states dicts.
# No global execution lock needed: run_screen() in both screener modules is fully
# stateless (all variables are local), so concurrent runs across users are safe.
_etf_state_lock  = threading.Lock()  # protects _user_etf_states dict
_bond_state_lock = threading.Lock()  # protects _user_bond_states dict

_user_ticker_states: dict = {}  # user_id -> {running, error, started_at, symbol}
_user_ticker_cancel: dict = {}  # user_id -> threading.Event
_ticker_state_lock   = threading.Lock()

def _default_etf_state() -> dict:
    return {"running": False, "error": None, "started_at": None}

def _default_bond_state() -> dict:
    return {"running": False, "error": None, "started_at": None}

def _default_ticker_state() -> dict:
    return {"running": False, "error": None, "started_at": None, "symbol": None}


def _run_etf_screen(user_id: str):
    """Run the ETF screen for a specific user. Writes to their private results file."""
    cancel_ev = _user_etf_cancel.setdefault(user_id, threading.Event())
    cancel_ev.clear()
    try:
        with open(os.path.join(_user_data_dir(user_id), "etf_running.json"), "w") as _rf:
            json.dump({"started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, _rf)
    except Exception:
        pass
    with _etf_state_lock:
        _user_etf_states[user_id]["started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sys.path.insert(0, AGENT_DIR)

    def _progress(msg: str):
        logger.info("[etf-screen uid=%s] %s", user_id[:8], msg)

    try:
        # No global lock needed — growth_etf_screener.run_screen() is fully stateless
        # (all variables are local to the function). Concurrent runs across users are safe.
        import growth_etf_screener as m
        try:
            data = m.run_screen(on_progress=_progress)
            if cancel_ev.is_set():
                pass  # finally block below still runs — cleans up file + state
            elif data.get("results") and len(data["results"]) > 0:
                with open(_etf_results_file(user_id), "w") as f:
                    json.dump(data, f, indent=2)
            else:
                with _etf_state_lock:
                    _user_etf_states[user_id]["error"] = "Run returned 0 results — possible network issue."
        except Exception as e:
            if not cancel_ev.is_set():
                with _etf_state_lock:
                    _user_etf_states[user_id]["error"] = str(e)
    finally:
        # Always clean up — whether run completed, was cancelled, or raised an exception.
        # Without this, etf_running.json stays on disk and the stale-check in
        # api_etf_status() would wrongly return running=True for up to 30 minutes.
        with _etf_state_lock:
            _user_etf_states[user_id]["running"] = False
        try:
            os.remove(os.path.join(_user_data_dir(user_id), "etf_running.json"))
        except Exception:
            pass


def _run_bond_screen(user_id: str):
    """Run the Bond screen for a specific user. Writes to their private results file."""
    cancel_ev = _user_bond_cancel.setdefault(user_id, threading.Event())
    cancel_ev.clear()
    try:
        with open(os.path.join(_user_data_dir(user_id), "bond_running.json"), "w") as _rf:
            json.dump({"started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, _rf)
    except Exception:
        pass
    with _bond_state_lock:
        _user_bond_states[user_id]["started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sys.path.insert(0, AGENT_DIR)

    def _progress(msg: str):
        logger.info("[bond-screen uid=%s] %s", user_id[:8], msg)

    try:
        # No global lock needed — bond_etf_screener.run_screen() is fully stateless
        # (all variables are local to the function). Concurrent runs across users are safe.
        import bond_etf_screener as m
        try:
            data = m.run_screen(on_progress=_progress)
            if cancel_ev.is_set():
                pass  # finally block below still runs — cleans up file + state
            elif data.get("results") and len(data["results"]) > 0:
                with open(_bond_results_file(user_id), "w") as f:
                    json.dump(data, f, indent=2)
            else:
                with _bond_state_lock:
                    _user_bond_states[user_id]["error"] = "Run returned 0 results — possible network issue."
        except Exception as e:
            if not cancel_ev.is_set():
                with _bond_state_lock:
                    _user_bond_states[user_id]["error"] = str(e)
    finally:
        # Always clean up — whether run completed, was cancelled, or raised an exception.
        # Without this, bond_running.json stays on disk and the stale-check in
        # api_bond_status() would wrongly return running=True for up to 30 minutes.
        with _bond_state_lock:
            _user_bond_states[user_id]["running"] = False
        try:
            os.remove(os.path.join(_user_data_dir(user_id), "bond_running.json"))
        except Exception:
            pass


def _run_ticker_research(user_id: str, symbol: str):
    """Fetch, score, and AI-analyse a single ticker for a specific user."""
    cancel_ev = _user_ticker_cancel.setdefault(user_id, threading.Event())
    cancel_ev.clear()
    try:
        with open(os.path.join(_user_data_dir(user_id), "ticker_running.json"), "w") as _rf:
            json.dump({"started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, _rf)
    except Exception:
        pass
    with _ticker_state_lock:
        _user_ticker_states[user_id]["started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _user_ticker_states[user_id]["symbol"] = symbol

    sys.path.insert(0, AGENT_DIR)
    try:
        import value_investor_agent as via

        if cancel_ev.is_set():
            return

        fmp_key = os.environ.get("FMP_API_KEY")
        if not fmp_key:
            raise ValueError("FMP_API_KEY environment variable is required")
        fmp_client = via.FMPClient(fmp_key)
        analyzer   = via.ValueInvestingAnalyzer(fmp_client)

        m = analyzer.fetch_stock_metrics(symbol.upper())

        if cancel_ev.is_set():
            return

        # Treat missing or empty profile as "not found"
        if m is None or not (m.company_name or "").strip():
            with _ticker_state_lock:
                _user_ticker_states[user_id]["error"] = (
                    f"Ticker '{symbol.upper()}' not found or has no market data. "
                    "Please verify the symbol is correct (e.g. AAPL, MSFT, NVDA)."
                )
            # Ticker not found — refund the run quota so the user is not charged
            if _DB_AVAILABLE:
                try:
                    models.decrement_daily_run_count(user_id)
                    models.decrement_monthly_ticker_count(user_id)
                except Exception as _e:
                    logger.warning("Could not refund run counts on ticker not found: %s", _e)
            return

        # Treat a zero price as "no market data" (delisted / invalid)
        if not m.price:
            with _ticker_state_lock:
                _user_ticker_states[user_id]["error"] = (
                    f"No current market data for '{symbol.upper()}'. "
                    "The stock may be delisted or the symbol may be incorrect."
                )
            # No market data — refund the run quota so the user is not charged
            if _DB_AVAILABLE:
                try:
                    models.decrement_daily_run_count(user_id)
                    models.decrement_monthly_ticker_count(user_id)
                except Exception as _e:
                    logger.warning("Could not refund run counts on ticker no market data: %s", _e)
            return

        scored = analyzer.score_stock(m)

        if cancel_ev.is_set():
            return

        pick = {
            "rank": 1,
            "symbol": m.symbol,
            "name": m.company_name or symbol.upper(),
            "sector": m.sector or "",
            "industry": m.industry or "",
            "country": m.country or "",
            "price_change_pct": m.price_change_percent,
            "pe_ratio": m.pe_ratio,
            "forward_pe": m.forward_pe,
            "pb_ratio": m.pb_ratio,
            "debt_to_equity": m.debt_to_equity,
            "current_ratio": m.current_ratio,
            "roe": m.roe,
            "net_margin": m.net_margin,
            "dividend_yield": m.dividend_yield,
            "market_cap": m.market_cap,
            "revenue": m.revenue,
            "net_income": m.net_income,
            "free_cash_flow": m.free_cash_flow,
            "eps": m.eps,
            "book_value_per_share": m.book_value_per_share,
            "beta": m.beta,
            "hist_profitable_years": m.hist_profitable_years,
            "hist_total_years": m.hist_total_years,
            "hist_earnings_source": m.hist_earnings_source,
            "hist_div_years": m.hist_div_years,
            "hist_eps_growth_pct": m.hist_eps_growth_pct,
            "score": scored["score"],
            "graham_score": scored["graham_score"],
            "buffett_score": scored["buffett_score"],
            "max_score": scored["max_score"],
            "grade": scored["grade"],
            "checklist": scored["checklist"],
            "buffett_checklist": scored["buffett_checklist"],
        }
        pick["ai_analysis"] = via._generate_enhanced_fallback_analysis(pick)

        if cancel_ev.is_set():
            return

        with open(_ticker_results_file(user_id), "w") as f:
            json.dump({"symbol": symbol.upper(), "result": pick,
                       "run_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, f, indent=2)
        logger.info("Ticker research done for user %s: %s score=%.1f",
                    user_id[:8], symbol.upper(), scored["score"])

    except Exception as e:
        if not cancel_ev.is_set():
            sym     = symbol.upper()
            raw     = str(e)
            if "404" in raw or "not found" in raw.lower():
                err_msg = f"Ticker '{sym}' not found. Please verify the symbol."
            elif "429" in raw or "rate limit" in raw.lower():
                err_msg = "API rate limit reached. Please try again in a moment."
            elif isinstance(e, TypeError):
                err_msg = (f"Ticker '{sym}' has insufficient or malformed data "
                           "for analysis. Try a different symbol.")
            elif isinstance(e, (KeyError, AttributeError)):
                err_msg = (f"Unexpected data format for '{sym}'. "
                           "The symbol may be an ETF, fund, or index — not a stock.")
            else:
                err_msg = f"Analysis failed for '{sym}': {raw[:120]}"
            with _ticker_state_lock:
                _user_ticker_states[user_id]["error"] = err_msg
        logger.warning("Ticker research error for user %s symbol=%s: %s",
                       user_id[:8], symbol, e)
    finally:
        with _ticker_state_lock:
            _user_ticker_states[user_id]["running"] = False
        try:
            os.remove(os.path.join(_user_data_dir(user_id), "ticker_running.json"))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 12. Login / Landing page (served when not authenticated)
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terminal Investor — Sign In</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .logo{font-size:2rem;font-weight:800;color:#f0a500;margin-bottom:8px;letter-spacing:-1px}
  .tagline{color:#8b949e;font-size:.9rem;margin-bottom:40px;text-align:center}
  .card{background:#161b22;border:1px solid #30363d;border-radius:16px;padding:36px 40px;width:100%;max-width:420px}
  .tabs{display:flex;gap:0;margin-bottom:28px;border-bottom:1px solid #30363d}
  .tab{flex:1;padding:10px 0;text-align:center;cursor:pointer;font-size:.9rem;color:#8b949e;border-bottom:2px solid transparent;transition:all .2s}
  .tab.active{color:#f0a500;border-bottom-color:#f0a500}
  label{display:block;font-size:.82rem;color:#8b949e;margin-bottom:6px;margin-top:16px}
  input{width:100%;padding:10px 14px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:.9rem;outline:none;transition:border-color .2s}
  input:focus{border-color:#f0a500}
  .btn{width:100%;padding:12px;margin-top:24px;background:#f0a500;color:#0d1117;border:none;border-radius:8px;font-weight:700;font-size:.95rem;cursor:pointer;transition:opacity .2s}
  .btn:hover{opacity:.9}
  .err{color:#f85149;font-size:.82rem;margin-top:10px;display:none}
  .trial-badge{background:#1c2d1e;border:1px solid #2ea043;border-radius:8px;padding:12px 16px;margin-bottom:24px;font-size:.82rem;color:#3fb950;line-height:1.5}
  .footer-link{margin-top:20px;text-align:center;font-size:.8rem;color:#8b949e}
  .footer-link a{color:#f0a500;text-decoration:none}
</style>
</head>
<body>
<div class="logo"><img src="/logo.png" alt="Terminal Investor" style="max-height:56px;max-width:260px;width:auto;"></div>
<div class="tagline">Graham–Buffett Value Screener · AI-Powered Analysis</div>
<div class="card">
  <div class="trial-badge">🎁 <strong>7-day free trial</strong> — no credit card required. Start screening today.</div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('login')">Sign In</div>
    <div class="tab" onclick="showTab('register')">Create Account</div>
  </div>

  <!-- LOGIN -->
  <div id="pane-login">
    <label>Email address</label>
    <input type="email" id="l-email" placeholder="you@example.com" autocomplete="email">
    <label>Password</label>
    <input type="password" id="l-pwd" placeholder="••••••••" autocomplete="current-password">
    <div class="err" id="l-err"></div>
    <button class="btn" onclick="doLogin()">Sign In</button>
  </div>

  <!-- REGISTER -->
  <div id="pane-register" style="display:none">
    <label>Full name</label>
    <input type="text" id="r-name" placeholder="Jane Doe">
    <label>Email address</label>
    <input type="email" id="r-email" placeholder="you@example.com" autocomplete="email">
    <label>Password <span style="color:#8b949e;font-size:.75rem">(min 8 chars)</span></label>
    <input type="password" id="r-pwd" placeholder="••••••••" autocomplete="new-password">
    <div class="err" id="r-err"></div>
    <button class="btn" id="r-btn" onclick="doRegister()">Start Free Trial</button>
  </div>
  <div class="footer-link">Need help? <a href="mailto:support@intelligentinvestor.io">Contact support</a></div>
</div>

<script>
function showTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',i===(t==='login'?0:1)));
  document.getElementById('pane-login').style.display=t==='login'?'':'none';
  document.getElementById('pane-register').style.display=t==='register'?'':'none';
}

async function doLogin(){
  const e=document.getElementById('l-email').value;
  const p=document.getElementById('l-pwd').value;
  const errEl=document.getElementById('l-err');
  errEl.style.display='none';
  try{
    const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e,password:p})});
    const d=await r.json();
    if(!r.ok){errEl.textContent=d.error||'Login failed';errEl.style.display='block';return;}
    // Tokens are set as HttpOnly cookies by the server — just redirect
    window.location.href='/';
  }catch(ex){errEl.textContent='Network error. Try again.';errEl.style.display='block';}
}

let _isRegistering = false;
async function doRegister(){
  if(_isRegistering) return;          // block duplicate clicks / held Enter
  _isRegistering = true;
  const btn=document.getElementById('r-btn');
  if(btn){ btn.disabled=true; btn.textContent='Creating account…'; }
  const name=document.getElementById('r-name').value;
  const e=document.getElementById('r-email').value;
  const p=document.getElementById('r-pwd').value;
  const errEl=document.getElementById('r-err');
  errEl.style.display='none';
  try{
    const r=await fetch('/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e,password:p,full_name:name})});
    const d=await r.json();
    if(!r.ok){
      const msg=d.fields?Object.values(d.fields).join(' · '):d.error||'Registration failed';
      errEl.textContent=msg;errEl.style.display='block';
      // Re-enable button so user can correct and retry
      if(btn){ btn.disabled=false; btn.textContent='Start Free Trial'; }
      _isRegistering=false;
      return;
    }
    // Tokens are set as HttpOnly cookies by the server — just redirect
    window.location.href='/';
  }catch(ex){
    errEl.textContent='Network error. Try again.';errEl.style.display='block';
    if(btn){ btn.disabled=false; btn.textContent='Start Free Trial'; }
    _isRegistering=false;
  }
}

// Auto-submit on Enter
['l-pwd','r-pwd'].forEach(id=>{
  const el=document.getElementById(id);
  if(el) el.addEventListener('keydown',ev=>{if(ev.key==='Enter'){id==='l-pwd'?doLogin():doRegister();}});
});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 13. Subscription status banner injected into main dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────
def _subscription_banner_js(plan_info: dict) -> str:
    """Returns a JS snippet that renders the subscription banner."""
    import json as _json
    return f"window._PLAN_INFO = {_json.dumps(plan_info)};"


# ─────────────────────────────────────────────────────────────────────────────
# 14. Routes — protected API layer
# ─────────────────────────────────────────────────────────────────────────────

_PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")

@app.route("/favicon.png")
@app.route("/favicon.ico")
def favicon():
    """Serve the app favicon from preview/favicon.png (drop your file there to update it)."""
    png = os.path.join(_PREVIEW_DIR, "favicon.png")
    if os.path.exists(png):
        return send_file(png, mimetype="image/png")
    return "", 204

@app.route("/favicon.svg")
def favicon_svg():
    """Serve the SVG favicon from preview/favicon.svg."""
    svg = os.path.join(_PREVIEW_DIR, "favicon.svg")
    if os.path.exists(svg):
        return send_file(svg, mimetype="image/svg+xml")
    return "", 204

@app.route("/logo.png")
def logo_png():
    """Serve the app logo from preview/logo.png."""
    png = os.path.join(_PREVIEW_DIR, "logo.png")
    if os.path.exists(png):
        return send_file(png, mimetype="image/png")
    return "", 204


@app.route("/js/<path:filename>")
def serve_js(filename):
    """Serve bundled JS libraries from preview/js/ — avoids CDN dependencies."""
    safe = os.path.realpath(os.path.join(_PREVIEW_DIR, "js", filename))
    allowed = os.path.realpath(os.path.join(_PREVIEW_DIR, "js"))
    if not safe.startswith(allowed + os.sep):
        return "Not found", 404
    if not os.path.exists(safe):
        return "Not found", 404
    return send_file(safe, mimetype="application/javascript")


@app.route("/css/<path:filename>")
def serve_css(filename):
    """Serve CSS files from preview/css/."""
    safe = os.path.realpath(os.path.join(_PREVIEW_DIR, "css", filename))
    allowed = os.path.realpath(os.path.join(_PREVIEW_DIR, "css"))
    if not safe.startswith(allowed + os.sep):
        return "Not found", 404
    if not os.path.exists(safe):
        return "Not found", 404
    return send_file(safe, mimetype="text/css")


@app.route("/")
def index():
    """Serve the main SaaS UI (self-contained preview/index.html)."""
    preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview", "index.html")
    try:
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        return html, 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    except FileNotFoundError:
        return "<h1>UI not found — expected preview/index.html</h1>", 404


@app.route("/login")
def login_page():
    """Serve the login / signup page."""
    if _DB_AVAILABLE and _current_user_id():
        return redirect("/")
    return LOGIN_HTML


@app.route("/admin")
@app.route("/admin/")
def admin_panel():
    """Serve the standalone admin panel (admin_panel.html)."""
    admin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_panel.html")
    try:
        with open(admin_path, "r", encoding="utf-8") as f:
            html = f.read()
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except FileNotFoundError:
        return "<h1>Admin panel not found — expected admin_panel.html</h1>", 404


@app.route("/logout")
def logout_page():
    """Clear auth cookies and redirect to login."""
    resp = redirect("/login")
    resp.delete_cookie("access_token",  path="/", samesite="Lax", secure=True)
    resp.delete_cookie("refresh_token", path="/", samesite="Lax", secure=True)
    return resp


# ── Status API ────────────────────────────────────────────────────────────────
@app.route("/api/status")
@auth_required
def api_status():
    user_id = getattr(g, "user_id", None)
    nxt   = _next_run_time(user_id)
    delta = nxt - datetime.now(_TZ_EST)
    h, r  = divmod(int(delta.total_seconds()), 3600)
    m     = r // 60
    plan_info = _get_plan_info(user_id)
    run_state = _get_run_state(user_id)
    # Calculate last run duration in seconds.
    # For stopped runs (exit_code=-1) use the stored last_completed_duration_secs so
    # the stats bar shows the last *successful* run's time, not the partial stop time.
    duration_secs = None
    if run_state.get("exit_code") == -1 and run_state.get("last_completed_duration_secs") is not None:
        duration_secs = run_state["last_completed_duration_secs"]
    elif run_state.get("finished_at") and run_state.get("started_at"):
        try:
            started  = datetime.fromisoformat(run_state["started_at"].replace("Z", "+00:00"))
            finished = datetime.fromisoformat(run_state["finished_at"].replace("Z", "+00:00"))
            duration_secs = max(0, int((finished - started).total_seconds()))
        except Exception:
            pass
    # Build human-readable schedule string (e.g. "Mon · Tue · Wed · Thu · Fri at 18:00")
    # Load config with proper user context to get their schedule
    _sched_cfg = _load_config(user_id)
    try:
        _sched_hour   = int(_sched_cfg.get("schedule_hour", 18))
        _sched_minute = int(_sched_cfg.get("schedule_minute", 0))
        _sched_days   = [int(d) for d in _sched_cfg.get("schedule_days", [0, 1, 2, 3, 4])]
    except Exception:
        _sched_hour, _sched_minute, _sched_days = 18, 0, [0, 1, 2, 3, 4]
    _day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _day_labels = " · ".join(_day_names[d] for d in sorted(_sched_days) if 0 <= d <= 6)
    _sched_enabled = bool(_sched_cfg.get("enabled", True))
    schedule_fmt = f"{_day_labels} at {_sched_hour:02d}:{_sched_minute:02d} EST" if _sched_enabled else "Scheduler OFF"
    return jsonify({
        "run_state":       run_state,
        "next_run_fmt":    (nxt.strftime("%a %b %-d at %-I:%M %p") + " EST") if _sched_enabled else None,
        "schedule_fmt":    schedule_fmt,
        "schedule_enabled": _sched_enabled,
        "countdown":       f"{h}h {m}m" if _sched_enabled else None,
        "last_picks":    _parse_last_picks(user_id) if user_id else [],
        "snap_picks":    _parse_snap_picks(user_id) if user_id else [],
        # Use live log parsing as primary source; fall back to persisted summary from
        # last_run_state.json so the stats bar shows correct values even when the
        # most recent log file has no COMPLETE marker (stopped/failed run).
        "run_summary":   _parse_run_summary(user_id) or run_state.get("run_summary", {}),
        "report_count":  len(_list_reports(user_id)) if user_id else 0,
        "plan_info":     plan_info,
        "duration_secs": duration_secs,
    })


# ── Run API ───────────────────────────────────────────────────────────────────
@app.route("/api/run", methods=["POST"])
@auth_required
@csrf_required
def api_run():
    user_id = getattr(g, "user_id", None)
    
    # Check subscription status
    if _DB_AVAILABLE and user_id:
        try:
            subscription = models.get_user_subscription(user_id)
            if subscription:
                sub_status = subscription.get("status", "").lower()
                if sub_status == "expired":
                    return jsonify({
                        "ok": False, 
                        "message": "Your subscription has expired. Please contact the helpdesk to renew it."
                    }), 403
                elif sub_status in ("inactive", "cancelled", "pending_payment"):
                    return jsonify({
                        "ok": False,
                        "message": f"Your account is {sub_status}. Please contact the helpdesk."
                    }), 403
        except Exception as _sub_err:
            logger.warning("Subscription check failed: %s", _sub_err)
    
    # Plan gate — check daily run quota then increment
    if _DB_AVAILABLE and user_id:
        try:
            allowed, reason = plan_gate.increment_and_check_run_count(user_id)
            if not allowed:
                return jsonify({"ok": False, "message": reason}), 403
        except Exception as _pg_err:
            logger.warning("Plan gate check failed: %s", _pg_err)
    ok, msg = _start_agent(user_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.route("/api/run", methods=["DELETE"])
@auth_required
@csrf_required
def api_stop():
    user_id = getattr(g, "user_id", None)
    was_running = _get_run_state(user_id).get("running", False)
    ok, msg = _stop_agent(user_id)
    if ok and was_running and _DB_AVAILABLE and user_id:
        try:
            models.decrement_daily_run_count(user_id)
        except Exception as _e:
            logger.warning("Could not decrement run count on stop: %s", _e)
    return jsonify({"ok": ok, "message": msg})


# ── Logs API (Analyst+ plans only) ───────────────────────────────────────────
@app.route("/api/logs")
@auth_required
def api_logs():
    user_id = getattr(g, "user_id", None)
    # NOTE: Do NOT gate this endpoint — all users need logs for progress detection
    # during runs (matching log milestones to advance progress steps).
    # The Agent Logs TAB itself is gated via the frontend features check.
    n = min(int(request.args.get("n", 300)), 2000)
    lines = _latest_user_log_lines(user_id, n) if user_id else []
    return jsonify({"lines": lines})


# ── Reports API ───────────────────────────────────────────────────────────────
@app.route("/api/reports")
@auth_required
def api_reports():
    user_id = getattr(g, "user_id", None)
    reports = _list_reports(user_id)
    return jsonify({"reports": reports})


@app.route("/reports/<path:fname>")
@auth_required
def serve_report(fname):
    """Serve a PDF report — auth required, file must belong to the requesting user."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    basename = os.path.basename(fname)
    if not basename.endswith(".pdf"):
        return "Not found", 404
    # Resolve to an absolute path within the user's own reports directory only.
    # This prevents both path traversal AND cross-user access.
    user_reports = os.path.realpath(_user_reports_dir(user_id))
    safe = os.path.realpath(os.path.join(user_reports, basename))
    if safe.startswith(user_reports) and os.path.exists(safe):
        return send_file(safe, mimetype="application/pdf")
    return "Not found", 404


# ── Config API — per-user from DB ─────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
@auth_required
def api_config_get():
    user_id = getattr(g, "user_id", None)
    cfg = _load_config(user_id)
    # Inject plan-allowed options
    if _DB_AVAILABLE and user_id:
        cfg["allowed_periods"] = plan_gate.get_allowed_periods(user_id)
        cfg["allowed_markets"]  = plan_gate.get_allowed_markets(user_id)
        # Inject feature flags for frontend gating
        email_ok, _ = plan_gate.can_use_feature(user_id, "email")
        cfg["plan_email_allowed"]  = email_ok
        etf_ok,   _ = plan_gate.can_use_feature(user_id, "etf")
        cfg["plan_etf_allowed"]    = etf_ok
        bond_ok,  _ = plan_gate.can_use_feature(user_id, "bond")
        cfg["plan_bond_allowed"]   = bond_ok
        # Force email_enabled to False for plans without email access
        if not email_ok and cfg.get("email_enabled"):
            cfg["email_enabled"] = False
    # Remove internal DB fields before sending to frontend
    for _key in ("user_id", "updated_at", "created_at"):
        cfg.pop(_key, None)
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@auth_required
@csrf_required
def api_config_post():
    user_id = getattr(g, "user_id", None)
    data = request.get_json(force=True, silent=True) or {}

    # Guard: strip disallowed periods/markets/features before saving
    if _DB_AVAILABLE and user_id:
        allowed_periods = plan_gate.get_allowed_periods(user_id)
        allowed_markets = plan_gate.get_allowed_markets(user_id)
        if "loser_period" in data and data["loser_period"] not in allowed_periods:
            return jsonify({"ok": False, "message": "Period not available on your plan."}), 403
        if "markets" in data:
            data["markets"] = [m for m in data["markets"] if m in allowed_markets]
        # Email gate — only Starter+ plans can enable email delivery
        if data.get("email_enabled"):
            email_ok, email_reason = plan_gate.can_use_feature(user_id, "email")
            if not email_ok:
                data["email_enabled"] = False  # silently force off for Trial

    cfg = _save_config(data, user_id)
    if user_id:
        try:
            update_user_schedule_job(user_id)
        except Exception as _e:
            logger.warning("Schedule job update failed after config save: %s", _e)
    return jsonify({"ok": True, "config": cfg})


# ── Picks detail API ──────────────────────────────────────────────────────────
@app.route("/api/picks/detail")
@auth_required
def api_picks_detail():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"picks": []})
    try:
        path = _picks_detail_file(user_id)
        if os.path.exists(path):
            with open(path) as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify({"picks": []})


# ── ETF / Bond screener APIs ──────────────────────────────────────────────────
@app.route("/api/etf/run", methods=["POST"])
@auth_required
@csrf_required
def api_etf_run():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"ok": False, "message": "Authentication required"}), 401
    if _DB_AVAILABLE:
        ok, reason = plan_gate.can_use_feature(user_id, "etf")
        if not ok:
            return jsonify({"ok": False, "message": reason}), 403
        try:
            allowed, reason = plan_gate.increment_and_check_run_count(user_id)
            if not allowed:
                return jsonify({"ok": False, "message": reason}), 403
        except Exception as _pg_err:
            logger.warning("Plan gate check failed for ETF run: %s", _pg_err)
    with _etf_state_lock:
        user_etf = _user_etf_states.get(user_id, _default_etf_state())
        if user_etf.get("running"):
            if _DB_AVAILABLE:
                try: models.decrement_daily_run_count(user_id)
                except Exception: pass
            return jsonify({"ok": False, "message": "ETF screen already running for your account"}), 409
        _user_etf_states[user_id] = {"running": True, "error": None, "started_at": None}
    threading.Thread(target=_run_etf_screen, args=(user_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/etf/stop", methods=["DELETE"])
@auth_required
@csrf_required
def api_etf_stop():
    user_id = getattr(g, "user_id", None)
    # Cross-worker safe: also check the flag file so a stop request routed to a
    # different Gunicorn worker (whose _user_etf_states dict is empty) still
    # cleans up correctly instead of returning "No ETF screen running".
    rf = os.path.join(_user_data_dir(user_id), "etf_running.json") if user_id else None
    file_running = False
    if rf:
        try:
            if os.path.exists(rf):
                with open(rf) as _f:
                    _fd = json.load(_f)
                _started = datetime.fromisoformat(
                    _fd.get("started_at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - _started).total_seconds() < 1800:
                    file_running = True
        except Exception:
            pass
    with _etf_state_lock:
        user_etf = _user_etf_states.get(user_id, _default_etf_state())
        was_running = user_etf.get("running", False) or file_running
        if not was_running:
            return jsonify({"ok": False, "message": "No ETF screen running for your account"})
        cancel_ev = _user_etf_cancel.setdefault(user_id, threading.Event())
        cancel_ev.set()
        _user_etf_states[user_id] = {"running": False, "error": None, "started_at": user_etf.get("started_at")}
    if was_running and _DB_AVAILABLE and user_id:
        try:
            models.decrement_daily_run_count(user_id)
        except Exception as _e:
            logger.warning("Could not decrement run count on ETF stop: %s", _e)
    # Delete the running flag file immediately so the stale-check in
    # api_etf_status() does not mistake the stopped run as still running.
    if rf:
        try:
            os.remove(rf)
        except Exception:
            pass
    logger.info("ETF screen stopped by user %s", user_id[:8] if user_id else "unknown")
    return jsonify({"ok": True})


@app.route("/api/bond/stop", methods=["DELETE"])
@auth_required
@csrf_required
def api_bond_stop():
    user_id = getattr(g, "user_id", None)
    # Cross-worker safe: also check the flag file so a stop request routed to a
    # different Gunicorn worker (whose _user_bond_states dict is empty) still
    # cleans up correctly instead of returning "No Bond screen running".
    rf = os.path.join(_user_data_dir(user_id), "bond_running.json") if user_id else None
    file_running = False
    if rf:
        try:
            if os.path.exists(rf):
                with open(rf) as _f:
                    _fd = json.load(_f)
                _started = datetime.fromisoformat(
                    _fd.get("started_at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - _started).total_seconds() < 1800:
                    file_running = True
        except Exception:
            pass
    with _bond_state_lock:
        user_bond = _user_bond_states.get(user_id, _default_bond_state())
        was_running = user_bond.get("running", False) or file_running
        if not was_running:
            return jsonify({"ok": False, "message": "No Bond screen running for your account"})
        cancel_ev = _user_bond_cancel.setdefault(user_id, threading.Event())
        cancel_ev.set()
        _user_bond_states[user_id] = {"running": False, "error": None, "started_at": user_bond.get("started_at")}
    if was_running and _DB_AVAILABLE and user_id:
        try:
            models.decrement_daily_run_count(user_id)
        except Exception as _e:
            logger.warning("Could not decrement run count on Bond stop: %s", _e)
    # Delete the running flag file immediately so the stale-check in
    # api_bond_status() does not mistake the stopped run as still running.
    if rf:
        try:
            os.remove(rf)
        except Exception:
            pass
    logger.info("Bond screen stopped by user %s", user_id[:8] if user_id else "unknown")
    return jsonify({"ok": True})


@app.route("/api/run/all", methods=["DELETE"])
@auth_required
@csrf_required
def api_stop_all():
    """Stop all running screens for the current user — called on logout."""
    user_id = getattr(g, "user_id", None)
    stopped = []

    # Stock screen
    stock_was = _get_run_state(user_id).get("running", False)
    ok, _ = _stop_agent(user_id)
    if ok and stock_was and _DB_AVAILABLE and user_id:
        try: models.decrement_daily_run_count(user_id)
        except Exception: pass
    if ok:
        stopped.append("stock")

    # ETF screen
    with _etf_state_lock:
        etf_was = _user_etf_states.get(user_id, _default_etf_state()).get("running", False)
        if etf_was:
            cancel_ev = _user_etf_cancel.setdefault(user_id, threading.Event())
            cancel_ev.set()
            _user_etf_states[user_id] = {"running": False, "error": None, "started_at": None}
            stopped.append("etf")
    if etf_was and _DB_AVAILABLE and user_id:
        try: models.decrement_daily_run_count(user_id)
        except Exception: pass

    # Bond screen
    with _bond_state_lock:
        bond_was = _user_bond_states.get(user_id, _default_bond_state()).get("running", False)
        if bond_was:
            cancel_ev = _user_bond_cancel.setdefault(user_id, threading.Event())
            cancel_ev.set()
            _user_bond_states[user_id] = {"running": False, "error": None, "started_at": None}
            stopped.append("bond")
    if bond_was and _DB_AVAILABLE and user_id:
        try: models.decrement_daily_run_count(user_id)
        except Exception: pass

    logger.info("All screens stopped for user %s on logout (stopped: %s)",
                user_id[:8] if user_id else "unknown", stopped)
    return jsonify({"ok": True, "stopped": stopped})


@app.route("/api/etf/status")
@auth_required
def api_etf_status():
    user_id = getattr(g, "user_id", None)
    state   = dict(_user_etf_states.get(user_id, _default_etf_state()))
    if user_id and not state.get("running"):
        rf = os.path.join(_user_data_dir(user_id), "etf_running.json")
        try:
            if os.path.exists(rf):
                with open(rf) as _f:
                    fd = json.load(_f)
                started = datetime.fromisoformat(fd.get("started_at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - started).total_seconds() < 1800:
                    state["running"] = True
                    state.setdefault("started_at", fd.get("started_at"))
        except Exception:
            pass
    results = _load_screener_results(_etf_results_file(user_id)) if user_id else None
    return jsonify({**state, "results": results})


@app.route("/api/bond/run", methods=["POST"])
@auth_required
@csrf_required
def api_bond_run():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"ok": False, "message": "Authentication required"}), 401
    if _DB_AVAILABLE:
        ok, reason = plan_gate.can_use_feature(user_id, "bond")
        if not ok:
            return jsonify({"ok": False, "message": reason}), 403
        try:
            allowed, reason = plan_gate.increment_and_check_run_count(user_id)
            if not allowed:
                return jsonify({"ok": False, "message": reason}), 403
        except Exception as _pg_err:
            logger.warning("Plan gate check failed for Bond run: %s", _pg_err)
    with _bond_state_lock:
        user_bond = _user_bond_states.get(user_id, _default_bond_state())
        if user_bond.get("running"):
            if _DB_AVAILABLE:
                try: models.decrement_daily_run_count(user_id)
                except Exception: pass
            return jsonify({"ok": False, "message": "Bond screen already running for your account"}), 409
        _user_bond_states[user_id] = {"running": True, "error": None, "started_at": None}
    threading.Thread(target=_run_bond_screen, args=(user_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/bond/status")
@auth_required
def api_bond_status():
    user_id = getattr(g, "user_id", None)
    state   = dict(_user_bond_states.get(user_id, _default_bond_state()))
    if user_id and not state.get("running"):
        rf = os.path.join(_user_data_dir(user_id), "bond_running.json")
        try:
            if os.path.exists(rf):
                with open(rf) as _f:
                    fd = json.load(_f)
                started = datetime.fromisoformat(fd.get("started_at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - started).total_seconds() < 1800:
                    state["running"] = True
                    state.setdefault("started_at", fd.get("started_at"))
        except Exception:
            pass
    results = _load_screener_results(_bond_results_file(user_id)) if user_id else None
    return jsonify({**state, "results": results})


# ── Single Ticker AI Research APIs ───────────────────────────────────────────
_TICKER_SYMBOL_RE = re.compile(r'^[A-Za-z]{1,10}([.\-][A-Za-z0-9]{1,5})?$')

@app.route("/api/ticker/run", methods=["POST"])
@auth_required
@csrf_required
def api_ticker_run():
    user_id = getattr(g, "user_id", None)
    body    = request.get_json(silent=True) or {}
    symbol  = (body.get("symbol") or "").strip().upper()

    if not symbol or not _TICKER_SYMBOL_RE.match(symbol):
        return jsonify({"ok": False, "message": "Invalid ticker symbol. Use 1–10 letters (e.g. AAPL, MSFT)."}), 400

    # Plan gate — check single_ticker feature + monthly limit, then increment both counters
    if _DB_AVAILABLE:
        try:
            allowed, reason = plan_gate.increment_and_check_ticker_count(user_id)
            if not allowed:
                return jsonify({"ok": False, "message": reason}), 403
        except Exception as _pg_err:
            logger.warning("Plan gate check failed for ticker run: %s", _pg_err)

    with _ticker_state_lock:
        cur = _user_ticker_states.get(user_id, _default_ticker_state())
        if cur.get("running"):
            # Already running — undo the quota increments we just applied
            if _DB_AVAILABLE:
                try:
                    models.decrement_daily_run_count(user_id)
                    models.decrement_monthly_ticker_count(user_id)
                except Exception: pass
            return jsonify({"ok": False, "message": "Research already running — stop it first."}), 409
        _user_ticker_states[user_id] = {"running": True, "error": None,
                                        "started_at": None, "symbol": symbol}

    # Wipe any stale result from the previous run so it can never bleed through
    try:
        rf = _ticker_results_file(user_id)
        if os.path.exists(rf):
            os.remove(rf)
    except Exception:
        pass

    threading.Thread(target=_run_ticker_research, args=(user_id, symbol), daemon=True).start()
    return jsonify({"ok": True, "symbol": symbol})


@app.route("/api/ticker/stop", methods=["DELETE"])
@auth_required
@csrf_required
def api_ticker_stop():
    user_id = getattr(g, "user_id", None)
    with _ticker_state_lock:
        cur = _user_ticker_states.get(user_id, _default_ticker_state())
        was_running = cur.get("running", False)
        if not was_running:
            return jsonify({"ok": False, "message": "No research running."}), 400
        cancel_ev = _user_ticker_cancel.setdefault(user_id, threading.Event())
        cancel_ev.set()
        _user_ticker_states[user_id] = {
            "running": False, "error": None,
            "started_at": cur.get("started_at"), "symbol": cur.get("symbol"),
        }
    # Refund both the daily run quota and monthly ticker quota — run was cancelled
    if was_running and _DB_AVAILABLE and user_id:
        try:
            models.decrement_daily_run_count(user_id)
        except Exception as _e:
            logger.warning("Could not decrement daily run count on ticker stop: %s", _e)
        try:
            models.decrement_monthly_ticker_count(user_id)
        except Exception as _e:
            logger.warning("Could not decrement monthly ticker count on ticker stop: %s", _e)
    return jsonify({"ok": True})


@app.route("/api/ticker/status")
@auth_required
def api_ticker_status():
    user_id = getattr(g, "user_id", None)
    state   = dict(_user_ticker_states.get(user_id, _default_ticker_state()))
    if user_id and not state.get("running"):
        rf = os.path.join(_user_data_dir(user_id), "ticker_running.json")
        try:
            if os.path.exists(rf):
                with open(rf) as _f:
                    fd = json.load(_f)
                started = datetime.fromisoformat(fd.get("started_at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - started).total_seconds() < 900:
                    state["running"] = True
                    state.setdefault("started_at", fd.get("started_at"))
        except Exception:
            pass
    result  = None
    # Only return a result if the last run succeeded (no error in state)
    if user_id and not state.get("error") and not state.get("running"):
        rf = _ticker_results_file(user_id)
        if os.path.exists(rf):
            try:
                with open(rf) as f:
                    result = json.load(f)
            except Exception:
                pass
    return jsonify({**state, "result": result})


# ── Plan info endpoint ────────────────────────────────────────────────────────
@app.route("/api/me/plan")
@auth_required
def api_my_plan():
    user_id = getattr(g, "user_id", None)
    return jsonify(_get_plan_info(user_id))


# ── Health check (no auth — for Nginx / load balancer) ───────────────────────
def _health_caller_trusted() -> bool:
    """Return True if the caller is an authenticated admin or an internal/monitoring IP."""
    from auth import _get_client_ip
    ip = _get_client_ip() or ""
    # Allow Docker internal network + localhost
    trusted_prefixes = ("127.", "::1", "172.16.", "172.17.", "172.18.", "172.19.",
                        "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                        "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                        "172.30.", "172.31.", "10.", "192.168.")
    if any(ip.startswith(p) for p in trusted_prefixes):
        return True
    # Also accept requests bearing a valid admin_access JWT
    token = request.cookies.get("admin_access") or ""
    if token:
        try:
            from auth import decode_token
            payload = decode_token(token)
            if payload.get("type") == "admin_access":
                return True
        except Exception:
            pass
    return False


@app.route("/health")
def health():
    """Enhanced health + monitoring endpoint.

    Returns HTTP 200 when all critical systems are healthy,
    HTTP 503 when any critical check fails.
    Full details are returned to internal/admin callers only;
    public callers receive a minimal status for uptime monitors.
    """
    import shutil
    checks = {}
    overall_ok = True

    # ── Database ping ──────────────────────────────────────────────────────
    if _DB_AVAILABLE:
        try:
            import models as _m
            with _m.db_cursor(commit=False) as _cur:
                _cur.execute("SELECT 1")
            checks["db"] = {"status": "ok"}
        except Exception as _exc:
            checks["db"] = {"status": "error", "detail": str(_exc)}
            overall_ok = False
    else:
        checks["db"] = {"status": "unavailable", "detail": "running in local-only mode"}

    # ── Disk space (warn when < 500 MB free on AGENT_DIR partition) ────────
    try:
        _usage = shutil.disk_usage(AGENT_DIR)
        _free_mb = _usage.free // (1024 * 1024)
        _pct_used = round(100 * _usage.used / _usage.total, 1)
        checks["disk"] = {
            "status": "ok" if _free_mb >= 500 else "warning",
            "free_mb": _free_mb,
            "used_pct": _pct_used,
        }
        if _free_mb < 200:
            overall_ok = False
    except Exception as _exc:
        checks["disk"] = {"status": "error", "detail": str(_exc)}

    # ── Log directory writable ─────────────────────────────────────────────
    try:
        _test_path = os.path.join(LOG_DIR, ".write_test")
        with open(_test_path, "w") as _f:
            _f.write("ok")
        os.remove(_test_path)
        checks["log_dir"] = {"status": "ok", "path": LOG_DIR}
    except Exception as _exc:
        checks["log_dir"] = {"status": "error", "detail": str(_exc)}
        overall_ok = False

    # ── JWT_SECRET configured ──────────────────────────────────────────────
    _jwt_ok = bool(os.environ.get("JWT_SECRET")) and \
              os.environ.get("JWT_SECRET") != "INSECURE_PLACEHOLDER_SET_JWT_SECRET_IN_ENV"
    checks["jwt_secret"] = {"status": "ok" if _jwt_ok else "warning",
                            "configured": _jwt_ok}

    status_code = 200 if overall_ok else 503
    status_str  = "ok" if overall_ok else "degraded"

    # Internal/admin callers: return full diagnostic detail
    if _health_caller_trusted():
        return jsonify({
            "status": status_str,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "version": "v2",
            "checks": checks,
        }), status_code

    # Public callers (uptime monitors, etc.): minimal response only
    return jsonify({"status": status_str}), status_code


# ─────────────────────────────────────────────────────────────────────────────
# 15. Entry point
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Automatic Daily Scheduling — per-user
# ─────────────────────────────────────────────────────────────────────────────
def _scheduled_stock_screen(user_id: str):
    """Run stock screening for a specific user at their scheduled time."""
    try:
        cfg = _load_config(user_id)
        if not cfg.get("enabled", True):
            logger.info("Scheduled screen skipped for user %s — scheduler disabled", user_id)
            return

        logger.info("Scheduled stock screen triggered for user %s at %s",
                    user_id, datetime.now(_TZ_EST).strftime("%H:%M:%S EST"))
        ok, msg = _start_agent(user_id=user_id, source="scheduled")
        if ok:
            logger.info("Scheduled screen started for user %s: %s", user_id, msg)
            # Track the scheduled run for audit purposes.  Scheduled runs are NOT
            # blocked by quota (they are a paid-plan feature), so we only increment
            # for analytics — we never decrement here (nothing was pre-incremented).
            if _DB_AVAILABLE:
                try:
                    models.increment_daily_run_count(user_id)
                except Exception as e:
                    logger.warning("Could not track run count for scheduled run %s: %s", user_id, e)
        else:
            logger.warning("Could not start scheduled screen for user %s: %s", user_id, msg)
    except Exception as e:
        logger.error("Error in scheduled stock screen for user %s: %s", user_id, e)


def _job_id(user_id: str) -> str:
    return f"stock_screen_{user_id}"


def _add_user_schedule_job(scheduler, user_id: str, hour: int, minute: int,
                            days: list, enabled: bool) -> None:
    """Add or replace a cron job for one user. No-op if disabled."""
    job_id = _job_id(user_id)
    # Remove existing job for this user if present
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    if not enabled or not days:
        logger.info("Schedule job removed/skipped for user %s (enabled=%s)", user_id, enabled)
        return

    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    label_days = ','.join(day_names[d] for d in sorted(set(days)) if 0 <= d <= 6)

    scheduler.add_job(
        _scheduled_stock_screen,
        'cron',
        args=[user_id],
        hour=hour,
        minute=minute,
        day_of_week=','.join(str(d) for d in sorted(set(days))),
        id=job_id,
        name=f'User {user_id[:8]}: {label_days} at {hour:02d}:{minute:02d}',
        replace_existing=True,
    )
    logger.info("Schedule job set for user %s at %02d:%02d on %s", user_id, hour, minute, label_days)


_scheduler = None  # set by _init_scheduler() at startup

# Flag file: any worker touches this to tell the leader to reload schedules from DB.
_SCHED_RELOAD_FLAG = os.path.join(AGENT_DIR, ".scheduler_reload")


def _sync_all_schedules_from_db() -> None:
    """
    Re-read every user's schedule from DB and update the in-process APScheduler jobs.
    Runs in the leader worker only (called by the 60-second interval job and on startup).
    Also clears the reload-flag file written by non-leader workers.
    """
    global _scheduler
    if _scheduler is None:
        return
    # Clear the flag regardless of what we find so stale flags don't loop.
    try:
        if os.path.exists(_SCHED_RELOAD_FLAG):
            os.remove(_SCHED_RELOAD_FLAG)
    except Exception:
        pass
    if not _DB_AVAILABLE:
        return
    try:
        rows = models.get_all_active_user_schedules()
        for row in rows:
            uid     = str(row["user_id"])
            hour    = int(row.get("schedule_hour", 18))
            minute  = int(row.get("schedule_minute", 0))
            raw     = row.get("schedule_days", "0,1,2,3,4")
            days    = [int(d) for d in (raw.split(",") if isinstance(raw, str) else raw)]
            enabled = bool(row.get("schedule_enabled", True))
            _add_user_schedule_job(_scheduler, uid, hour, minute, days, enabled)
        logger.debug("Scheduler: synced %d user job(s) from DB", len(rows))
    except Exception as _se:
        logger.error("Scheduler: DB sync failed: %s", _se)


def update_user_schedule_job(user_id: str) -> None:
    """
    Re-read a user's config from DB/disk and update their scheduler job live.

    If this worker IS the scheduler leader → update immediately.
    If this worker is NOT the leader (80% of Gunicorn requests) → touch a flag
    file so the leader's 60-second sync job picks up the change within one minute.
    """
    global _scheduler
    if _scheduler is None:
        # Non-leader worker: signal the leader via flag file.
        try:
            open(_SCHED_RELOAD_FLAG, "w").close()
            logger.debug("Schedule reload flag set by non-leader worker pid=%d", os.getpid())
        except Exception as _fe:
            logger.warning("Could not set schedule reload flag: %s", _fe)
        return
    # Leader worker: update immediately.
    try:
        cfg = _load_config(user_id)
        hour   = int(cfg.get("schedule_hour", 18))
        minute = int(cfg.get("schedule_minute", 0))
        raw    = cfg.get("schedule_days", [0, 1, 2, 3, 4])
        days   = [int(d) for d in (raw.split(",") if isinstance(raw, str) else raw)]
        enabled = bool(cfg.get("enabled", True))
        _add_user_schedule_job(_scheduler, user_id, hour, minute, days, enabled)
    except Exception as e:
        logger.error("Failed to update schedule job for user %s: %s", user_id, e)


def _init_scheduler():
    """Initialize background scheduler with one cron job per active user."""
    try:
        # Explicitly use Eastern Time so that schedule_hour/minute values entered
        # by users in the UI (which are always in EST/EDT) fire at the correct
        # wall-clock time regardless of the server's local timezone (typically UTC).
        scheduler = BackgroundScheduler(timezone=_TZ_EST)
        scheduler.start()
        job_count = 0

        if _DB_AVAILABLE:
            try:
                user_schedules = models.get_all_active_user_schedules()
                for row in user_schedules:
                    uid     = row["user_id"]
                    hour    = int(row.get("schedule_hour", 18))
                    minute  = int(row.get("schedule_minute", 0))
                    raw     = row.get("schedule_days", "0,1,2,3,4")
                    days    = [int(d) for d in (raw.split(",") if isinstance(raw, str) else raw)]
                    enabled = bool(row.get("schedule_enabled", True))
                    _add_user_schedule_job(scheduler, uid, hour, minute, days, enabled)
                    if enabled:
                        job_count += 1
                logger.info("Scheduler initialised: %d active user job(s)", job_count)
            except Exception as e:
                logger.warning("Could not load per-user schedules from DB, falling back to global config: %s", e)
                _init_scheduler_fallback(scheduler)
        else:
            _init_scheduler_fallback(scheduler)

        # ── Periodic DB sync ─────────────────────────────────────────────────
        # Re-reads all user schedules every 60 seconds.  This ensures that when
        # a user saves a new schedule via the UI and the HTTP request hits a
        # non-leader Gunicorn worker (which has _scheduler=None and therefore
        # can't update the leader's APScheduler), the leader picks up the
        # change within at most 60 seconds via this sync job.  It also checks
        # for the _SCHED_RELOAD_FLAG file written by non-leader workers.
        scheduler.add_job(
            _sync_all_schedules_from_db,
            'interval',
            seconds=60,
            id='_schedule_db_sync',
            name='Schedule DB sync (every 60 s)',
            replace_existing=True,
            max_instances=1,
        )
        logger.info("Scheduler: 60-second DB sync job registered")

        # ── System metrics collection (monitoring dashboard) ──────────────
        # Samples CPU, RAM, active runs, and HTTP connections once per minute.
        # Only runs in the scheduler-leader worker — no cross-worker overhead.
        if _metrics_collector is not None:
            def _collect_metrics_job():
                _metrics_collector.collect_and_store(AGENT_DIR)

            scheduler.add_job(
                _collect_metrics_job,
                'interval',
                seconds=60,
                id='_metrics_collect',
                name='System metrics sample (every 60 s)',
                replace_existing=True,
                max_instances=1,
            )
            logger.info("Scheduler: system metrics collection job registered")
        else:
            logger.warning("Scheduler: metrics_collector not available — monitoring charts will have no data")

        return scheduler
    except Exception as e:
        logger.error("Failed to initialize scheduler: %s", e)
        return None


def _init_scheduler_fallback(scheduler) -> None:
    """Single-user fallback when DB is unavailable — uses disk config."""
    try:
        cfg    = _load_config()
        hour   = int(cfg.get("schedule_hour", 18))
        minute = int(cfg.get("schedule_minute", 0))
        raw    = cfg.get("schedule_days", [0, 1, 2, 3, 4])
        days   = [int(d) for d in (raw.split(",") if isinstance(raw, str) else raw)] or [0,1,2,3,4]
        enabled = bool(cfg.get("enabled", True))
        if not enabled:
            logger.info("Scheduler fallback: schedule disabled in config, no job added")
            return
        day_names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
        label = ','.join(day_names[d] for d in sorted(set(days)) if 0 <= d <= 6)
        scheduler.add_job(
            _scheduled_stock_screen,
            'cron',
            args=["local"],
            hour=hour, minute=minute,
            day_of_week=','.join(str(d) for d in sorted(set(days))),
            id='stock_screen_daily',
            name=f'Stock screening (local): {label} at {hour:02d}:{minute:02d}',
            replace_existing=True,
        )
        logger.info("Scheduler fallback: job set at %02d:%02d on %s", hour, minute, label)
    except Exception as e:
        logger.error("Scheduler fallback init failed: %s", e)


# ── Gunicorn-safe scheduler startup ──────────────────────────────────────────
# Under Gunicorn, __name__ is "dashboard_v2" (not "__main__"), so the
# if __name__ == "__main__" block below never runs.  We therefore start the
# scheduler here at module level, but use a non-blocking file lock so that
# exactly ONE Gunicorn worker process becomes the "scheduler worker".  All
# other workers skip silently.  When the winning worker exits (crash/restart),
# the OS releases the lock and the next worker to reload acquires it.
_SCHEDULER_LOCK_FD = None   # module-level ref keeps the lock alive for this process


def _try_start_scheduler_if_leader() -> None:
    """Acquire a cross-process file lock and, if successful, start the scheduler."""
    global _scheduler, _SCHEDULER_LOCK_FD
    if _scheduler is not None:
        return   # already initialised (e.g. by __main__ block)
    lock_path = os.path.join(AGENT_DIR, ".scheduler.lock")
    try:
        import fcntl
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # non-blocking exclusive lock
        _SCHEDULER_LOCK_FD = fd          # keep open — lock released when process dies
        _scheduler = _init_scheduler()
        logger.info("Scheduler started in worker pid=%d", os.getpid())
    except (IOError, OSError):
        logger.debug("Scheduler lock held by another worker (pid=%d) — skipping", os.getpid())
    except Exception as _sl_err:
        logger.warning("Scheduler startup error in pid=%d: %s", os.getpid(), _sl_err)


# Run at import time so Gunicorn workers start the scheduler automatically.
# The lock ensures only ONE worker wins; the rest skip cleanly.
_try_start_scheduler_if_leader()


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    print("=" * 55)
    print("  Intelligent Investor V2 — SaaS Dashboard")
    print(f"  URL : http://localhost:{PORT}")
    print(f"  DB  : {'Connected' if _DB_AVAILABLE else 'Not connected (local mode)'}")
    print(f"  Dir : {AGENT_DIR}")
    print("=" * 55)

    # _try_start_scheduler_if_leader() already ran above — _scheduler is set.
    # Only call _init_scheduler() again if somehow it was not acquired above.
    if _scheduler is None:
        _scheduler = _init_scheduler()

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
