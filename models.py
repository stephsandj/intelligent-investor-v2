"""
models.py - PostgreSQL database layer for the Stock Screening SaaS application.
Connection string from env var DATABASE_URL.
"""

import subprocess
import sys

# Auto-install psycopg2-binary if not present
try:
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"])
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _reset_pool() -> None:
    """Close all pool connections and force recreation on next access."""
    global _pool
    with _pool_lock:
        try:
            if _pool:
                _pool.closeall()
        except Exception:
            pass
        _pool = None
        logger.info("Connection pool reset — will reconnect on next request")


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=50,
            dsn=database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info("Database connection pool created")
    return _pool


def get_db():
    """Borrow a connection from the pool with automatic stale-connection recovery."""
    for attempt in range(2):
        try:
            conn = _get_pool().getconn()
            # Quick health check — detects broken connections after DB restart
            conn.cursor().execute("SELECT 1")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as _e:
            if attempt == 0:
                logger.warning("Stale DB connection detected (DB restarted?), resetting pool: %s", _e)
                try:
                    _pool.putconn(conn, close=True)
                except Exception:
                    pass
                _reset_pool()
                continue
            raise
    raise RuntimeError("Cannot connect to database after pool reset")


def release_db(conn, close: bool = False):
    """Return a connection to the pool (or discard it on error)."""
    try:
        _get_pool().putconn(conn, close=close)
    except Exception:
        pass


@contextmanager
def db_cursor(commit: bool = True):
    """Context manager that provides a cursor and handles commit/rollback/release."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db(conn)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    email_verify_token VARCHAR(128),
    reset_token VARCHAR(128),
    reset_token_expires TIMESTAMPTZ,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    stripe_customer_id VARCHAR(128) UNIQUE,
    last_login_at TIMESTAMPTZ,
    last_login_ip INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    description TEXT,
    runs_per_day INTEGER,
    max_ai_picks INTEGER,
    max_pdf_history INTEGER,
    trial_days INTEGER NOT NULL DEFAULT 0,
    features JSONB NOT NULL DEFAULT '{}',
    price_monthly NUMERIC(10,2),
    price_yearly NUMERIC(10,2),
    stripe_price_monthly VARCHAR(128),
    stripe_price_yearly VARCHAR(128),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    status VARCHAR(32) NOT NULL DEFAULT 'trial',
    billing_cycle VARCHAR(16),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    trial_ends_at TIMESTAMPTZ,
    payment_method VARCHAR(50),
    stripe_customer_id VARCHAR(255),
    stripe_sub_id VARCHAR(255),
    stripe_price_id VARCHAR(128),
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    canceled_at TIMESTAMPTZ,
    activated_by UUID REFERENCES users(id),
    activated_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT subscriptions_user_unique UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS user_configs (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    loser_period VARCHAR(20) NOT NULL DEFAULT 'daily100',
    markets TEXT[] NOT NULL DEFAULT ARRAY['NYSE','NASDAQ'],
    stock_geography VARCHAR(20) NOT NULL DEFAULT 'usa',
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    pdf_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    email_address VARCHAR(255),
    schedule_hour INTEGER NOT NULL DEFAULT 18,
    schedule_minute INTEGER NOT NULL DEFAULT 0,
    schedule_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
    schedule_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS screening_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_date DATE NOT NULL DEFAULT CURRENT_DATE,
    loser_period VARCHAR(20),
    markets TEXT[],
    symbols TEXT[],
    criteria JSONB,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    picks_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    results JSONB,
    summary TEXT,
    pdf_path VARCHAR(512),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    ip_address INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_run_counts (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_date DATE NOT NULL DEFAULT CURRENT_DATE,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, run_date)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    target_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(128) NOT NULL,
    details JSONB,
    notes TEXT,
    ip_address INET,
    user_agent VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_users (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL DEFAULT 'admin'
);

CREATE TABLE IF NOT EXISTS admin_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(20) NOT NULL DEFAULT 'superadmin',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    last_login_ip INET
);

CREATE TABLE IF NOT EXISTS password_history (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    email VARCHAR(255),
    ip_address INET,
    success BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_email_created
    ON login_attempts(email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_password_history_user_created
    ON password_history(user_id, created_at DESC);
"""

_SEED_PLANS = """
INSERT INTO plans (id, name, display_name, price_monthly, price_yearly,
                   runs_per_day, max_ai_picks, max_pdf_history, trial_days, features, sort_order)
VALUES
  (1, 'trial', 'Free Trial', NULL, NULL, 1, 5, 5, 7,
   '{"etf":false,"bond":false,"value":false,"amex":false,"all_modes":false,"email":false,"export":false,"api":false,"single_ticker":false,"agent_logs":false,"markets":["NYSE","NASDAQ"],"geography":["usa"]}',
   1),
  (2, 'starter', 'Starter', 29.00, 249.00, 3, 5, 5, 0,
   '{"etf":false,"bond":false,"value":false,"amex":false,"all_modes":false,"email":true,"export":false,"api":false,"single_ticker":true,"agent_logs":false,"markets":["NYSE","NASDAQ"],"geography":["usa"]}',
   2),
  (3, 'pro', 'Pro', 79.00, 699.00, 5, 5, 5, 0,
   '{"etf":true,"bond":false,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":false,"single_ticker":true,"agent_logs":false,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all"]}',
   3),
  (6, 'advanced', 'Advanced', 149.00, 1299.00, 8, 5, 5, 0,
   '{"etf":true,"bond":true,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":false,"single_ticker":true,"agent_logs":false,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all"]}',
   4),
  (4, 'analyst', 'Analyst', 199.00, 1799.00, NULL, 5, 5, 0,
   '{"etf":true,"bond":true,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":true,"single_ticker":true,"agent_logs":false,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all","international"]}',
   5),
  (5, 'enterprise', 'Enterprise', 499.00, 4499.00, NULL, 5, NULL, 0,
   '{"etf":true,"bond":true,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":true,"single_ticker":true,"agent_logs":true,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all","international"]}',
   6)
ON CONFLICT (id) DO UPDATE SET
  name              = EXCLUDED.name,
  display_name      = EXCLUDED.display_name,
  price_monthly     = EXCLUDED.price_monthly,
  price_yearly      = EXCLUDED.price_yearly,
  runs_per_day      = EXCLUDED.runs_per_day,
  max_ai_picks      = EXCLUDED.max_ai_picks,
  max_pdf_history   = EXCLUDED.max_pdf_history,
  trial_days        = EXCLUDED.trial_days,
  features          = EXCLUDED.features,
  sort_order        = EXCLUDED.sort_order,
  updated_at        = NOW();
"""


_MIGRATIONS = """
ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS schedule_hour INTEGER NOT NULL DEFAULT 18;
ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS schedule_minute INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS schedule_days TEXT NOT NULL DEFAULT '0,1,2,3,4';
ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS schedule_enabled BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS monthly_ticker_counts (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year_month VARCHAR(7) NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, year_month)
);

ALTER TABLE plans ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;

-- Drop FK constraint on audit_log.actor_id so admin_account IDs can be stored
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_id_fkey;

-- Ensure admin_accounts table exists (also in DDL but safe here too)
CREATE TABLE IF NOT EXISTS admin_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(20) NOT NULL DEFAULT 'superadmin',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    last_login_ip INET
);

-- Refresh-token rotation: store a SHA-256 hash of the most recently issued
-- refresh token for each user. On /auth/refresh the incoming token is hashed
-- and compared; a mismatch means the token was already rotated (possible theft).
ALTER TABLE users ADD COLUMN IF NOT EXISTS refresh_token_hash  VARCHAR(64);
ALTER TABLE users ADD COLUMN IF NOT EXISTS refresh_token_issued TIMESTAMPTZ;
"""


def init_db():
    """Create all tables, run migrations, and seed plan data. Safe to call on every startup."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            cur.execute(_MIGRATIONS)
            cur.execute(_SEED_PLANS)
        conn.commit()
        logger.info("Database schema initialised successfully")
    except Exception as exc:
        conn.rollback()
        logger.error("init_db failed: %s", exc)
        raise
    finally:
        release_db(conn)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# User operations
# ---------------------------------------------------------------------------

def create_user(email: str, password_hash: str, full_name: Optional[str] = None) -> Dict[str, Any]:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, password_hash, full_name)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (email.lower().strip(), password_hash, full_name),
        )
        return _row_to_dict(cur.fetchone())


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),))
        return _row_to_dict(cur.fetchone())


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return _row_to_dict(cur.fetchone())


def update_user_last_login(user_id: str, ip: Optional[str] = None):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users SET last_login_at = NOW(), last_login_ip = %s
            WHERE id = %s
            """,
            (ip, user_id),
        )


def verify_user_email(user_id: str):
    """Mark email as verified and clear the one-time verify token."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET email_verified = TRUE, email_verify_token = NULL WHERE id = %s",
            (user_id,),
        )


def set_email_verify_token(user_id: str, token: str) -> None:
    """Store a one-time email verification token for the user."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET email_verify_token = %s WHERE id = %s",
            (token, user_id),
        )


def get_user_by_verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Look up a user by their one-time email verification token."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM users WHERE email_verify_token = %s",
            (token,),
        )
        return _row_to_dict(cur.fetchone())


def set_password_reset_token(user_id: str, token: str, expires_at) -> None:
    """Store a one-time password reset token for the user."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET reset_token = %s, reset_token_expires = %s WHERE id = %s",
            (token, expires_at, user_id),
        )


def get_user_by_reset_token(token: str) -> Optional[Dict[str, Any]]:
    """Look up a user by their one-time password reset token."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM users WHERE reset_token = %s AND reset_token_expires > NOW()",
            (token,),
        )
        return _row_to_dict(cur.fetchone())


def clear_password_reset_token(user_id: str) -> None:
    """Clear the password reset token after successful reset."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET reset_token = NULL, reset_token_expires = NULL WHERE id = %s",
            (user_id,),
        )


def store_refresh_token_hash(user_id: str, token_hash: str) -> None:
    """Persist the SHA-256 hash of the current refresh token for rotation checks."""
    with db_cursor() as cur:
        cur.execute(
            """UPDATE users
               SET refresh_token_hash = %s, refresh_token_issued = NOW()
               WHERE id = %s""",
            (token_hash, user_id),
        )


def verify_and_rotate_refresh_token(user_id: str, incoming_hash: str) -> bool:
    """
    Return True if incoming_hash matches the stored hash, then clear it so the
    same token can never be reused (rotation).  Returns False on mismatch —
    which may indicate token theft; callers should invalidate the session.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT refresh_token_hash FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row["refresh_token_hash"]:
            # No hash stored yet (legacy session pre-rotation) — accept once
            # and let the caller store a new hash going forward.
            return True
        if row["refresh_token_hash"] != incoming_hash:
            return False
        # Clear immediately so the same token cannot be reused
        cur.execute(
            "UPDATE users SET refresh_token_hash = NULL WHERE id = %s",
            (user_id,),
        )
        return True


def update_user_profile(user_id: str, full_name: Optional[str] = None, email: Optional[str] = None) -> None:
    """Update mutable profile fields (full_name, email)."""
    if full_name is None and email is None:
        return

    # Explicit SQL templates per field combination — avoids dynamic f-string
    # SQL construction flagged by static analysis (Bandit B608).
    # All column names are hardcoded; only VALUES go through %s params.
    if full_name is not None and email is not None:
        sql    = "UPDATE users SET full_name = %s, email = %s, updated_at = NOW() WHERE id = %s"
        params = [full_name, email, user_id]
    elif full_name is not None:
        sql    = "UPDATE users SET full_name = %s, updated_at = NOW() WHERE id = %s"
        params = [full_name, user_id]
    else:
        sql    = "UPDATE users SET email = %s, updated_at = NOW() WHERE id = %s"
        params = [email, user_id]

    with db_cursor() as cur:
        cur.execute(sql, params)


def set_user_password(user_id: str, password_hash: str):
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (password_hash, user_id),
        )


# ---------------------------------------------------------------------------
# Subscription operations
# ---------------------------------------------------------------------------

def create_subscription(
    user_id: str,
    plan_id: int,
    status: str,
    billing_cycle: Optional[str] = None,
    trial_ends_at=None,
) -> Dict[str, Any]:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, plan_id, status, billing_cycle, trial_ends_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, plan_id, status, billing_cycle, trial_ends_at),
        )
        return _row_to_dict(cur.fetchone())


def get_user_subscription(user_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT s.*, p.name AS plan_name, p.display_name, p.runs_per_day,
                   p.max_ai_picks, p.max_pdf_history, p.features
            FROM subscriptions s
            JOIN plans p ON s.plan_id = p.id
            WHERE s.user_id = %s
            ORDER BY s.started_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        return _row_to_dict(cur.fetchone())


def update_subscription_status(
    user_id: str,
    status: str,
    activated_by: Optional[str] = None,
):
    with db_cursor() as cur:
        if activated_by:
            cur.execute(
                """
                UPDATE subscriptions
                SET status = %s, activated_by = %s, activated_at = NOW()
                WHERE user_id = %s
                """,
                (status, activated_by, user_id),
            )
        else:
            cur.execute(
                "UPDATE subscriptions SET status = %s WHERE user_id = %s",
                (status, user_id),
            )


def update_subscription_plan(
    user_id: str,
    plan_id: int,
    billing_cycle: str,
    expires_at,
):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE subscriptions
            SET plan_id = %s, billing_cycle = %s, expires_at = %s, activated_at = NOW()
            WHERE user_id = %s
            """,
            (plan_id, billing_cycle, expires_at, user_id),
        )


def get_all_subscriptions_admin(status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        if status_filter:
            cur.execute(
                """
                SELECT u.id AS user_id, u.email, u.full_name, p.name AS plan_name,
                       s.status, s.billing_cycle, s.started_at, s.expires_at, s.trial_ends_at
                FROM subscriptions s
                JOIN users u ON s.user_id = u.id
                JOIN plans p ON s.plan_id = p.id
                WHERE s.status = %s
                ORDER BY s.started_at DESC
                """,
                (status_filter,),
            )
        else:
            cur.execute(
                """
                SELECT u.id AS user_id, u.email, u.full_name, p.name AS plan_name,
                       s.status, s.billing_cycle, s.started_at, s.expires_at, s.trial_ends_at
                FROM subscriptions s
                JOIN users u ON s.user_id = u.id
                JOIN plans p ON s.plan_id = p.id
                ORDER BY s.started_at DESC
                """
            )
        return [_row_to_dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Config operations
# ---------------------------------------------------------------------------

_CONFIG_DEFAULTS = {
    "loser_period": "daily100",
    "markets": ["NYSE", "NASDAQ"],
    "stock_geography": "usa",
    "email_enabled": False,
    "pdf_enabled": True,
    "email_address": None,
    "schedule_hour": 18,
    "schedule_minute": 0,
    "schedule_days": "0,1,2,3,4",
    "schedule_enabled": True,
}


def get_user_config(user_id: str) -> Dict[str, Any]:
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM user_configs WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            return _row_to_dict(row)
        return dict(_CONFIG_DEFAULTS, user_id=user_id)


def upsert_user_config(
    user_id: str,
    loser_period: str,
    markets: List[str],
    stock_geography: str,
    email_enabled: bool,
    pdf_enabled: bool,
    email_address: Optional[str],
    schedule_hour: int = 18,
    schedule_minute: int = 0,
    schedule_days: str = "0,1,2,3,4",
    schedule_enabled: bool = True,
):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_configs
                (user_id, loser_period, markets, stock_geography, email_enabled,
                 pdf_enabled, email_address,
                 schedule_hour, schedule_minute, schedule_days, schedule_enabled,
                 updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                loser_period     = EXCLUDED.loser_period,
                markets          = EXCLUDED.markets,
                stock_geography  = EXCLUDED.stock_geography,
                email_enabled    = EXCLUDED.email_enabled,
                pdf_enabled      = EXCLUDED.pdf_enabled,
                email_address    = EXCLUDED.email_address,
                schedule_hour    = EXCLUDED.schedule_hour,
                schedule_minute  = EXCLUDED.schedule_minute,
                schedule_days    = EXCLUDED.schedule_days,
                schedule_enabled = EXCLUDED.schedule_enabled,
                updated_at       = NOW()
            """,
            (
                user_id,
                loser_period,
                markets,
                stock_geography,
                email_enabled,
                pdf_enabled,
                email_address,
                schedule_hour,
                schedule_minute,
                schedule_days,
                schedule_enabled,
            ),
        )


def get_all_active_user_schedules() -> List[Dict[str, Any]]:
    """Return schedule config for all users with active/trial subscriptions."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id AS user_id,
                   COALESCE(uc.schedule_hour,    18)          AS schedule_hour,
                   COALESCE(uc.schedule_minute,  0)           AS schedule_minute,
                   COALESCE(uc.schedule_days,    '0,1,2,3,4') AS schedule_days,
                   COALESCE(uc.schedule_enabled, TRUE)        AS schedule_enabled
            FROM users u
            JOIN subscriptions s ON s.user_id = u.id
            LEFT JOIN user_configs uc ON uc.user_id = u.id
            WHERE s.status IN ('active', 'trial')
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

def create_run(
    user_id: str,
    loser_period: str,
    markets: List[str],
    ip_address: Optional[str] = None,
) -> str:
    with db_cursor() as cur:
        run_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO screening_runs (id, user_id, loser_period, markets, ip_address)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, user_id, loser_period, markets, ip_address),
        )
        return run_id


def finish_run(run_id: str, picks_count: int, completed: bool = True):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE screening_runs
            SET stopped_at = NOW(), completed = %s, picks_count = %s
            WHERE id = %s
            """,
            (completed, picks_count, run_id),
        )


def get_daily_run_count(user_id: str) -> int:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT count FROM daily_run_counts
            WHERE user_id = %s AND run_date = (NOW() AT TIME ZONE 'America/New_York')::date
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def increment_daily_run_count(user_id: str) -> int:
    """Upsert today's run count and return the new count."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_run_counts (user_id, run_date, count)
            VALUES (%s, (NOW() AT TIME ZONE 'America/New_York')::date, 1)
            ON CONFLICT (user_id, run_date) DO UPDATE
                SET count = daily_run_counts.count + 1
            RETURNING count
            """,
            (user_id,),
        )
        return cur.fetchone()["count"]


def decrement_daily_run_count(user_id: str) -> int:
    """Decrement today's run count by 1 (floor 0). Used when a run is stopped before completion."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE daily_run_counts
            SET count = GREATEST(0, count - 1)
            WHERE user_id = %s AND run_date = (NOW() AT TIME ZONE 'America/New_York')::date
            RETURNING count
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def reset_daily_run_count(user_id: str):
    """Admin: zero out today's run count for a user."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE daily_run_counts SET count = 0
            WHERE user_id = %s AND run_date = (NOW() AT TIME ZONE 'America/New_York')::date
            """,
            (user_id,),
        )


def set_daily_run_count(user_id: str, count: int) -> int:
    """Admin: set today's run count for a user to a specific value (creates row if missing)."""
    count = max(0, int(count))
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_run_counts (user_id, run_date, count)
            VALUES (%s, (NOW() AT TIME ZONE 'America/New_York')::date, %s)
            ON CONFLICT (user_id, run_date) DO UPDATE
                SET count = EXCLUDED.count
            RETURNING count
            """,
            (user_id, count),
        )
        row = cur.fetchone()
        return row["count"] if row else count


def get_user_runs_admin(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT * FROM screening_runs
            WHERE user_id = %s
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------

def get_all_users_admin() -> List[Dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id AS user_id, u.email, u.full_name, u.created_at,
                   u.last_login_at, u.email_verified,
                   p.id AS plan_id, p.name AS plan_name,
                   s.status,
                   COALESCE(drc.count, 0) AS daily_runs_today,
                   CASE WHEN au.user_id IS NOT NULL THEN TRUE ELSE FALSE END AS is_admin
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id
            LEFT JOIN plans p ON s.plan_id = p.id
            LEFT JOIN daily_run_counts drc
                   ON drc.user_id = u.id AND drc.run_date = (NOW() AT TIME ZONE 'America/New_York')::date
            LEFT JOIN admin_users au ON u.id = au.user_id
            ORDER BY u.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def set_admin(user_id: str):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_users (user_id) VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )


def is_admin(user_id: str) -> bool:
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT 1 FROM admin_users WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Admin accounts (separate table — completely independent from app users)
# ---------------------------------------------------------------------------

def get_admin_account_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Look up an admin_accounts row by email (case-insensitive)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM admin_accounts WHERE LOWER(email) = LOWER(%s) AND is_active = TRUE",
            (email.strip(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_admin_account_by_id(admin_id: str) -> Optional[Dict[str, Any]]:
    """Look up an admin_accounts row by UUID."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM admin_accounts WHERE id = %s AND is_active = TRUE",
            (admin_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def create_admin_account(email: str, password_hash: str, full_name: Optional[str] = None, role: str = "superadmin") -> Dict[str, Any]:
    """Create a new admin account. Returns the created row."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_accounts (email, password_hash, full_name, role)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (email.lower().strip(), password_hash, full_name, role),
        )
        return dict(cur.fetchone())


def update_admin_last_login(admin_id: str, ip: Optional[str] = None) -> None:
    """Update last_login_at and last_login_ip for an admin account."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE admin_accounts SET last_login_at = NOW(), last_login_ip = %s WHERE id = %s",
            (ip, admin_id),
        )


def get_all_admin_accounts() -> List[Dict[str, Any]]:
    """Return all admin accounts (for admin management)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, email, full_name, role, is_active, created_at, last_login_at FROM admin_accounts ORDER BY created_at ASC"
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def seed_default_admin(password_hash: str) -> bool:
    """Insert the default admin@admin.local account if it doesn't exist yet. Returns True if created."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_accounts (email, password_hash, full_name, role)
            VALUES ('admin@admin.local', %s, 'System Admin', 'superadmin')
            ON CONFLICT (email) DO NOTHING
            RETURNING id
            """,
            (password_hash,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(
    user_id: Optional[str] = None,
    action: str = "",
    details_dict: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    actor_id: Optional[str] = None,
    target_user_id: Optional[str] = None,
    notes: Optional[str] = None,
):
    """Fire-and-forget audit log entry. Silently swallowed on failure."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (user_id, actor_id, target_user_id, action, details, notes, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    actor_id,
                    target_user_id,
                    action,
                    json.dumps(details_dict) if details_dict else None,
                    notes,
                    ip_address,
                ),
            )
    except Exception as exc:
        logger.error("audit log failed for action=%s: %s", action, exc)


# ---------------------------------------------------------------------------
# Missing admin functions (Fix #2)
# ---------------------------------------------------------------------------

def get_audit_log_for_user(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return the last N audit log entries touching this user (as actor or target)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT al.id, al.actor_id, al.target_user_id, al.action,
                   al.details, al.notes, al.ip_address, al.created_at,
                   actor_u.email  AS actor_email,
                   target_u.email AS target_email
            FROM audit_log al
            LEFT JOIN users actor_u  ON actor_u.id  = al.actor_id
            LEFT JOIN users target_u ON target_u.id = al.target_user_id
            WHERE al.user_id = %s
               OR al.actor_id = %s
               OR al.target_user_id = %s
            ORDER BY al.created_at DESC
            LIMIT %s
            """,
            (user_id, user_id, user_id, limit),
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = _row_to_dict(r)
            if d and "created_at" in d and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            result.append(d)
        return result


def extend_trial_ends_at(user_id: str, new_trial_ends_at) -> None:
    """Update trial_ends_at for a user's subscription (admin use)."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE subscriptions
            SET trial_ends_at = %s, status = 'active'
            WHERE user_id = %s
            """,
            (new_trial_ends_at, user_id),
        )


def count_total_users() -> int:
    """Return the total number of registered users."""
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0


def get_active_subscriptions_by_plan() -> Dict[str, int]:
    """Return a dict of plan_name -> count of active subscriptions."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT p.name AS plan_name, COUNT(*) AS cnt
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.status = 'active'
              AND (s.expires_at IS NULL OR s.expires_at > NOW())
            GROUP BY p.name
            """
        )
        return {row["plan_name"]: int(row["cnt"]) for row in cur.fetchall()}


def get_runs_today_all_users() -> int:
    """Return total screening runs across all users for today."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(count), 0) AS total
            FROM daily_run_counts
            WHERE run_date = (NOW() AT TIME ZONE 'America/New_York')::date
            """
        )
        row = cur.fetchone()
        return int(row["total"]) if row else 0


# ---------------------------------------------------------------------------
# Admin-only additions
# ---------------------------------------------------------------------------

def delete_user(user_id: str) -> None:
    """Hard-delete a user and all related data (CASCADE handles child rows)."""
    with db_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


def update_user_email(user_id: str, email: str) -> None:
    """Update a user's email address."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET email = %s WHERE id = %s",
            (email, user_id),
        )


def get_signups_by_day(days: int = 30) -> List[Dict[str, Any]]:
    """Return daily signup counts for the last N days (including days with 0)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                gs.day::date AS day,
                COALESCE(COUNT(u.id), 0) AS count
            FROM generate_series(
                    (CURRENT_DATE - (%s - 1) * INTERVAL '1 day'),
                    CURRENT_DATE,
                    INTERVAL '1 day'
                 ) AS gs(day)
            LEFT JOIN users u
                   ON DATE(u.created_at AT TIME ZONE 'UTC') = gs.day::date
            GROUP BY gs.day
            ORDER BY gs.day
            """,
            (days,),
        )
        result = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("day") and hasattr(d["day"], "isoformat"):
                d["day"] = d["day"].isoformat()
            d["count"] = int(d.get("count", 0))
            result.append(d)
        return result


def get_all_subscriptions_with_user(limit: int = 500) -> List[Dict[str, Any]]:
    """Return all subscriptions joined with user and plan info, newest first."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                s.id,
                s.status,
                s.billing_cycle,
                s.started_at,
                s.expires_at,
                s.trial_ends_at,
                s.stripe_sub_id,
                u.id        AS user_id,
                u.email,
                u.full_name,
                u.email_verified,
                p.id        AS plan_id,
                p.name      AS plan_name,
                p.display_name,
                p.price_monthly
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            JOIN plans p ON s.plan_id = p.id
            ORDER BY s.started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        result = []
        for row in cur.fetchall():
            d = _row_to_dict(row)
            for key in ("started_at", "expires_at", "trial_ends_at"):
                if d.get(key) and hasattr(d[key], "isoformat"):
                    d[key] = d[key].isoformat()
            result.append(d)
        return result


def get_trial_subscriptions_count() -> int:
    """Return count of currently active trial subscriptions."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt FROM subscriptions
            WHERE trial_ends_at IS NOT NULL
              AND trial_ends_at > NOW()
            """
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0


def migrate_trial_status_to_active() -> int:
    """
    Migrate all subscriptions with status='trial' to status='active'.
    This is a data cleanup migration to ensure consistency.
    Trial users are identified by trial_ends_at field, not status value.
    Returns the number of records updated.
    """
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE subscriptions
            SET status = 'active'
            WHERE status = 'trial'
            RETURNING id
            """
        )
        updated_ids = [row["id"] for row in cur.fetchall()]
        if updated_ids:
            logger.info(f"Migrated {len(updated_ids)} subscriptions from status='trial' to status='active'")
        return len(updated_ids)


# ---------------------------------------------------------------------------
# Monthly ticker run tracking
# ---------------------------------------------------------------------------

def _current_year_month() -> str:
    """Return the current year-month string, e.g. '2026-04'."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m")


def get_monthly_ticker_count(user_id: str) -> int:
    """Return how many single-ticker runs this user has done this calendar month."""
    ym = _current_year_month()
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT count FROM monthly_ticker_counts
            WHERE user_id = %s AND year_month = %s
            """,
            (user_id, ym),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def increment_monthly_ticker_count(user_id: str) -> int:
    """Upsert this month's ticker run count and return the new count."""
    ym = _current_year_month()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO monthly_ticker_counts (user_id, year_month, count)
            VALUES (%s, %s, 1)
            ON CONFLICT (user_id, year_month) DO UPDATE
                SET count = monthly_ticker_counts.count + 1
            RETURNING count
            """,
            (user_id, ym),
        )
        return cur.fetchone()["count"]


def decrement_monthly_ticker_count(user_id: str) -> int:
    """Decrement this month's ticker run count (floor 0). Used on user-stop/cancel."""
    ym = _current_year_month()
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE monthly_ticker_counts
            SET count = GREATEST(0, count - 1)
            WHERE user_id = %s AND year_month = %s
            RETURNING count
            """,
            (user_id, ym),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def get_all_plans() -> list:
    """Return all active plans ordered by sort_order."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM plans WHERE is_active = TRUE ORDER BY sort_order, id"
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


# ==================== PASSWORD POLICY FUNCTIONS ====================

def store_password_in_history(user_id: str, password_hash: str) -> None:
    """Store current password hash in password history."""
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO password_history (user_id, password_hash) VALUES (%s, %s)",
            (user_id, password_hash),
        )


def get_password_history(user_id: str, limit: int = 3) -> list:
    """Get the last N password hashes for a user."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT password_hash FROM password_history WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [row["password_hash"] for row in cur.fetchall()]


def clear_password_history(user_id: str) -> None:
    """Clear all password history for a user (optional, if user requests)."""
    with db_cursor() as cur:
        cur.execute("DELETE FROM password_history WHERE user_id = %s", (user_id,))


# ==================== LOGIN ATTEMPT TRACKING ====================

def record_login_attempt(email: str, ip_address: str, success: bool, user_id: Optional[str] = None) -> None:
    """Record a login attempt (success or failure)."""
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO login_attempts (user_id, email, ip_address, success) VALUES (%s, %s, %s, %s)",
            (user_id, email.lower(), ip_address, success),
        )


def get_failed_login_attempts(email: str, minutes: int = 5) -> int:
    """Get count of failed login attempts for an email in the last N minutes."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT COUNT(*) as count FROM login_attempts
            WHERE email = %s AND success = FALSE
            AND created_at > NOW() - INTERVAL '%s minutes'
            """,
            (email.lower(), minutes),
        )
        row = cur.fetchone()
        return row["count"] if row else 0


def get_last_failed_login_time(email: str) -> Optional[datetime]:
    """Get the timestamp of the last failed login attempt."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT created_at FROM login_attempts
            WHERE email = %s AND success = FALSE
            ORDER BY created_at DESC LIMIT 1
            """,
            (email.lower(),),
        )
        row = cur.fetchone()
        return row["created_at"] if row else None


def is_account_locked(email: str, max_attempts: int = 5, lockout_minutes: int = 5) -> bool:
    """Check if account is locked due to too many failed login attempts."""
    failed_attempts = get_failed_login_attempts(email, lockout_minutes)
    return failed_attempts >= max_attempts


def clear_failed_login_attempts(email: str) -> None:
    """Clear failed login attempts after successful login."""
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM login_attempts WHERE email = %s AND success = FALSE",
            (email.lower(),),
        )


# ─────────────────────────────────────────────────────────────────────────────
# System Metrics — monitoring dashboard
# ─────────────────────────────────────────────────────────────────────────────

def init_metrics_table() -> None:
    """Create system_metrics table and its index if they do not already exist."""
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_metrics (
                id                   BIGSERIAL PRIMARY KEY,
                sampled_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cpu_pct              REAL,
                ram_pct              REAL,
                ram_used_mb          INTEGER,
                ram_total_mb         INTEGER,
                screener_runs_active INTEGER DEFAULT 0,
                http_connections     INTEGER DEFAULT 0,
                active_users_today   INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_system_metrics_ts
            ON system_metrics (sampled_at DESC)
            """
        )


def insert_metric_sample(
    cpu_pct: float,
    ram_pct: float,
    ram_used_mb: int,
    ram_total_mb: int,
    screener_runs_active: int = 0,
    http_connections: int = 0,
    active_users_today: int = 0,
) -> None:
    """Insert one system metrics sample row. Called every 60 s by the scheduler leader."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_metrics
                (sampled_at, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
                 screener_runs_active, http_connections, active_users_today)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
                screener_runs_active, http_connections, active_users_today,
            ),
        )


def count_active_users_today() -> int:
    """Count distinct users who have used at least one run today (EST/EDT date)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM daily_runs
            WHERE date = (NOW() AT TIME ZONE 'America/New_York')::date
              AND run_count > 0
            """
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0


def purge_old_metrics(days: int = 30) -> None:
    """Delete metric samples older than *days* days. Swallows errors — non-critical."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "DELETE FROM system_metrics WHERE sampled_at < NOW() - (%s || ' days')::interval",
                (str(days),),
            )
    except Exception:
        pass


def get_metrics_series(range_label: str = "24h") -> List[Dict[str, Any]]:
    """
    Return aggregated metric time-series for the admin monitoring dashboard.

    range_label:
      "24h"  → 5-minute bucket averages over the last 24 hours
      "7d"   → 1-hour bucket averages over the last 7 days
      "30d"  → 6-hour bucket averages over the last 30 days

    Returns a list of dicts, each with keys:
      ts, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
      screener_runs_active, http_connections, active_users_today
    """
    if range_label == "7d":
        interval = "7 days"
        bucket   = "1 hour"
    elif range_label == "30d":
        interval = "30 days"
        bucket   = "6 hours"
    else:  # 24h (default)
        interval = "24 hours"
        bucket   = "5 minutes"

    # bucket is from a safe fixed set — not user input — so f-string here is safe
    sql = f"""
        SELECT
            date_trunc('{bucket}', sampled_at)  AS ts,
            AVG(cpu_pct)               AS cpu_pct,
            AVG(ram_pct)               AS ram_pct,
            AVG(ram_used_mb)           AS ram_used_mb,
            AVG(ram_total_mb)          AS ram_total_mb,
            AVG(screener_runs_active)  AS screener_runs_active,
            AVG(http_connections)      AS http_connections,
            AVG(active_users_today)    AS active_users_today
        FROM system_metrics
        WHERE sampled_at >= NOW() - (%s || ' days')::interval
        GROUP BY 1
        ORDER BY 1 ASC
    """
    # Parse interval days from the label for the parameterized query
    interval_days = {"24 hours": "1", "7 days": "7", "30 days": "30"}[interval]

    with db_cursor(commit=False) as cur:
        cur.execute(sql, (interval_days,))
        rows = cur.fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append({
            "ts":                    row["ts"].isoformat() if row["ts"] else None,
            "cpu_pct":               round(float(row["cpu_pct"] or 0), 1),
            "ram_pct":               round(float(row["ram_pct"] or 0), 1),
            "ram_used_mb":           int(row["ram_used_mb"] or 0),
            "ram_total_mb":          int(row["ram_total_mb"] or 0),
            "screener_runs_active":  round(float(row["screener_runs_active"] or 0), 2),
            "http_connections":      round(float(row["http_connections"] or 0), 1),
            "active_users_today":    round(float(row["active_users_today"] or 0), 1),
        })
    return result
