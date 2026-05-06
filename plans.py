"""
plans.py - Plan feature gating module for the Stock Screening SaaS application.
Reads plan limits from the database, cached in memory for 5 minutes.

6-Tier Plan Structure:
  Trial     : Free,  1 run/day,   5 picks, 5 PDF,  no email, no ETF/Bond, no ticker,  no agent_logs
  Starter   : $29,   3 runs/day,  5 picks, 5 PDF,  email ✓,  no ETF/Bond, ticker→daily limit, no agent_logs
  Pro       : $79,   5 runs/day,  5 picks, 5 PDF,  email ✓,  ETF only,   ticker→daily limit, no agent_logs
  Advanced  : $149,  8 runs/day,  5 picks, 5 PDF,  email ✓,  ETF+Bond,   ticker→daily limit, no agent_logs
  Analyst   : $199,  unlimited,   5 picks, 5 PDF,  email ✓,  ETF+Bond,   ticker→daily limit, no agent_logs
  Enterprise: $499+, unlimited,   5 picks, unlimited PDF, all, ticker→daily limit, agent_logs ✓ Full
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import models

_TZ_EST = ZoneInfo("America/New_York")


def _next_midnight_est_label() -> str:
    """Return a human-readable label for when today's screen count resets (midnight EST/EDT)."""
    now_est = datetime.now(_TZ_EST)
    midnight = (now_est + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tz_label = "EDT" if midnight.dst() else "EST"
    return midnight.strftime(f"%-I:%M %p {tz_label}")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory plan cache (TTL = 5 minutes)
# ---------------------------------------------------------------------------

_plan_cache: Dict[int, Dict[str, Any]] = {}
_plan_cache_ts: float = 0.0
_CACHE_TTL = 300  # seconds


def _load_plans() -> Dict[int, Dict[str, Any]]:
    """Load all plans from the DB and cache them."""
    global _plan_cache, _plan_cache_ts
    with models.db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM plans WHERE is_active = TRUE")
        rows = cur.fetchall()
    _plan_cache = {row["id"]: dict(row) for row in rows}
    _plan_cache_ts = time.time()
    return _plan_cache


def _get_plans() -> Dict[int, Dict[str, Any]]:
    """Return the cached plan map, refreshing if stale."""
    if not _plan_cache or (time.time() - _plan_cache_ts) > _CACHE_TTL:
        _load_plans()
    return _plan_cache


def _get_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    return _get_plans().get(plan_id)


# ---------------------------------------------------------------------------
# Plan tier helpers
# ---------------------------------------------------------------------------

# Plans at each tier (for feature access checks by name)
_TRIAL_ONLY = {"trial"}
_STARTER_PLUS = {"starter", "pro", "advanced", "analyst", "enterprise"}
_PRO_PLUS_PLAN_NAMES = {"pro", "advanced", "analyst", "enterprise"}
_ADVANCED_PLUS_PLAN_NAMES = {"advanced", "analyst", "enterprise"}
_ANALYST_PLUS_PLAN_NAMES = {"analyst", "enterprise"}

# Allowed periods per tier
_TRIAL_STARTER_PERIODS = ["daily100"]
_PRO_PLUS_PERIODS = [
    "daily100", "daily500", "dailyall",
    "weekly100", "weekly500", "weeklyall",
    "yearly100", "yearly500", "yearlyall",
    "value100", "value500",
]

_TRIAL_STARTER_MARKETS = ["NYSE", "NASDAQ"]
_PRO_PLUS_MARKETS = ["NYSE", "NASDAQ", "AMEX"]


def _plan_name_lower(plan_name: Optional[str]) -> str:
    return (plan_name or "").lower().strip()


def _is_starter_plus(plan_name: Optional[str]) -> bool:
    return _plan_name_lower(plan_name) in _STARTER_PLUS


def _is_pro_plus(plan_name: Optional[str]) -> bool:
    return _plan_name_lower(plan_name) in _PRO_PLUS_PLAN_NAMES


def _is_advanced_plus(plan_name: Optional[str]) -> bool:
    return _plan_name_lower(plan_name) in _ADVANCED_PLUS_PLAN_NAMES


def _is_analyst_plus(plan_name: Optional[str]) -> bool:
    return _plan_name_lower(plan_name) in _ANALYST_PLUS_PLAN_NAMES


# ---------------------------------------------------------------------------
# Core access check
# ---------------------------------------------------------------------------

def check_plan_access(user_id: str, skip_run_limit: bool = False) -> Dict[str, Any]:
    """
    Return a dict describing whether the user may run the screener right now.

    Args:
        skip_run_limit: When True, skip the daily run count check. Use this
                        for feature-flag lookups (email, ETF, etc.) where the
                        run limit should NOT block access to settings.

    Keys:
        allowed      bool
        reason       str   (empty string when allowed=True)
        plan         dict  (plan row or {})
        subscription dict  (subscription row or {})
        runs_today   int
        runs_limit   int | None
        features     dict
    """
    subscription = models.get_user_subscription(user_id)
    if not subscription:
        return {
            "allowed": False,
            "reason": "No active subscription found. Please sign up for a plan.",
            "plan": {},
            "subscription": {},
            "runs_today": 0,
            "runs_limit": 0,
            "features": {},
        }

    plan_id = subscription.get("plan_id")
    plan = _get_plan(plan_id) if plan_id else None
    if not plan:
        return {
            "allowed": False,
            "reason": "Plan configuration not found. Please contact support.",
            "plan": {},
            "subscription": dict(subscription),
            "runs_today": 0,
            "runs_limit": None,
            "features": {},
        }

    status = subscription.get("status", "")
    runs_today = models.get_daily_run_count(user_id)
    runs_limit = plan.get("runs_per_day")  # None = unlimited

    raw_features = subscription.get("features") or plan.get("features") or {}
    if isinstance(raw_features, str):
        import json
        raw_features = json.loads(raw_features)

    base = {
        "plan": dict(plan),
        "subscription": dict(subscription),
        "runs_today": runs_today,
        "runs_limit": runs_limit,
        "features": raw_features,
        "resets_at": _next_midnight_est_label(),  # e.g. "12:00 AM EDT"
    }

    # Status checks
    if status not in ("trial", "active"):
        reason_map = {
            "inactive": "Your subscription is inactive. Please renew to continue.",
            "cancelled": "Your subscription has been cancelled.",
            "pending_payment": "Your payment is pending. Access will resume once confirmed.",
            "expired": "Your subscription has expired. Please renew.",
        }
        reason = reason_map.get(status, f"Subscription status '{status}' does not allow access.")
        return dict(base, allowed=False, reason=reason)

    # Trial expiry (trial users have status="active" with trial_ends_at set)
    trial_ends_at = subscription.get("trial_ends_at")
    if trial_ends_at:
        now = datetime.now(tz=timezone.utc)
        if hasattr(trial_ends_at, "tzinfo") and trial_ends_at.tzinfo is None:
            trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)
        if now > trial_ends_at:
            return dict(
                base,
                allowed=False,
                reason="Your free trial has expired. Upgrade to continue using the screener.",
            )

    # Active subscription expiry (paid plans)
    if status == "active":
        expires_at = subscription.get("expires_at")
        if expires_at:
            now = datetime.now(tz=timezone.utc)
            if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at:
                return dict(
                    base,
                    allowed=False,
                    reason="Your subscription has expired. Please renew to continue.",
                )

    # Daily run limit (only checked when evaluating screener access, not feature flags)
    if not skip_run_limit and runs_limit is not None and runs_today >= runs_limit:
        return dict(
            base,
            allowed=False,
            reason=(
                f"You have reached your daily limit of {runs_limit} "
                f"screen{'s' if runs_limit != 1 else ''}. "
                "Upgrade your plan or try again tomorrow."
            ),
        )

    return dict(base, allowed=True, reason="")


# ---------------------------------------------------------------------------
# Feature gating
# ---------------------------------------------------------------------------

_FEATURE_UPGRADE_HINTS = {
    "etf":          "ETF screening requires the Pro plan or above. Upgrade to unlock ETF screens.",
    "bond":         "Bond screening requires the Advanced plan or above. Upgrade to unlock Bond screens.",
    "value":        "Value screening requires the Pro plan or above.",
    "amex":         "AMEX market access requires the Pro plan or above.",
    "all_modes":    "All screening modes require the Pro plan or above.",
    "email":        "Email delivery requires the Starter plan or above. Upgrade to unlock email reports.",
    "export":       "Export functionality requires the Pro plan or above.",
    "api":          "API access requires the Analyst plan or above.",
    "single_ticker":"Single Ticker Research requires the Starter plan or above. Upgrade to research specific stocks.",
    "agent_logs":   "Agent Logs require the Analyst plan or above. Upgrade to view detailed run logs.",
}


def can_use_feature(user_id: str, feature_name: str) -> Tuple[bool, str]:
    """
    Return (True, "") if the user's plan includes feature_name,
    otherwise (False, human-readable reason).

    Daily run limits are intentionally skipped: hitting a daily cap should
    not lock out settings/config features (email toggle, PDF toggle, etc.).
    """
    access = check_plan_access(user_id, skip_run_limit=True)
    if not access["allowed"]:
        return False, access["reason"]

    features = access.get("features") or {}
    if features.get(feature_name):
        return True, ""

    hint = _FEATURE_UPGRADE_HINTS.get(
        feature_name,
        f"The '{feature_name}' feature is not included in your current plan. Please upgrade.",
    )
    return False, hint


# ---------------------------------------------------------------------------
# Screener run eligibility
# ---------------------------------------------------------------------------

def can_run_screener(user_id: str) -> Tuple[bool, str]:
    """
    Return (True, "") if the user may start a new screening run right now.
    Checks: subscription status, trial/expiry, daily run limit.
    """
    access = check_plan_access(user_id)
    if not access["allowed"]:
        return False, access["reason"]
    return True, ""


# ---------------------------------------------------------------------------
# Single Ticker Research monthly limit check
# ---------------------------------------------------------------------------

def check_ticker_access(user_id: str) -> Tuple[bool, str]:
    """
    Check whether the user can run a Single Ticker Research.

    Single Ticker Research counts toward the user's daily run limit — there is no
    separate monthly ticker limit.  Only checks:
      1. Plan has single_ticker feature enabled (Starter plan and above).
      2. The user has not exhausted their daily run limit.

    Returns (True, "") if allowed, (False, reason) if blocked.
    """
    # Use normal plan access check (includes daily run limit)
    access = check_plan_access(user_id)
    if not access["allowed"]:
        return False, access["reason"]

    features = access.get("features") or {}

    # Check single_ticker feature flag — Trial plan does NOT have access
    if not features.get("single_ticker"):
        return False, _FEATURE_UPGRADE_HINTS["single_ticker"]

    return True, ""


def increment_and_check_ticker_count(user_id: str) -> Tuple[bool, str]:
    """
    Check the user's daily run limit FIRST, then increment only if within limits.
    Single Ticker Research now uses the same daily run quota as screener runs —
    there is no separate monthly ticker limit.

    Returns (True, "") if allowed, (False, reason) if at or over limit.
    """
    # Check single_ticker feature flag first (skip run limit here; it's checked below)
    access = check_plan_access(user_id, skip_run_limit=True)
    if not access["allowed"]:
        return False, access["reason"]

    features = access.get("features") or {}
    if not features.get("single_ticker"):
        return False, _FEATURE_UPGRADE_HINTS["single_ticker"]

    # Now use the standard daily run check + increment
    return increment_and_check_run_count(user_id)


# ---------------------------------------------------------------------------
# Allowed options per plan tier
# ---------------------------------------------------------------------------

def get_allowed_periods(user_id: str) -> List[str]:
    """
    Return the list of loser_period strings available to this user.
    Trial/Starter → ["daily100"]
    Pro+ → full list
    """
    subscription = models.get_user_subscription(user_id)
    if not subscription:
        return _TRIAL_STARTER_PERIODS

    plan_name = subscription.get("plan_name", "")
    if _is_pro_plus(plan_name):
        return list(_PRO_PLUS_PERIODS)
    return list(_TRIAL_STARTER_PERIODS)


def get_allowed_markets(user_id: str) -> List[str]:
    """
    Return the list of market strings available to this user.
    Trial/Starter → ["NYSE","NASDAQ"]
    Pro+ → ["NYSE","NASDAQ","AMEX"]
    """
    subscription = models.get_user_subscription(user_id)
    if not subscription:
        return list(_TRIAL_STARTER_MARKETS)

    plan_name = subscription.get("plan_name", "")
    if _is_pro_plus(plan_name):
        return list(_PRO_PLUS_MARKETS)
    return list(_TRIAL_STARTER_MARKETS)


def get_ticker_monthly_limit(user_id: str) -> Optional[int]:
    """
    Single Ticker Research no longer has a separate monthly limit — it counts toward
    the user's daily run quota.  Returns None (unlimited) for plans that have the
    single_ticker feature, or 0 if the plan does not include single_ticker access.
    """
    subscription = models.get_user_subscription(user_id)
    if not subscription:
        return 0

    plan_id = subscription.get("plan_id")
    plan = _get_plan(plan_id) if plan_id else None
    if not plan:
        return 0

    import json
    raw_features = plan.get("features") or {}
    if isinstance(raw_features, str):
        raw_features = json.loads(raw_features)

    if not raw_features.get("single_ticker"):
        return 0  # Plan does not include single ticker access

    return None  # No monthly cap — uses daily run quota


# ---------------------------------------------------------------------------
# Atomic increment + limit check (daily screen runs)
# ---------------------------------------------------------------------------

def increment_and_check_run_count(user_id: str) -> Tuple[bool, str]:
    """
    Atomically check the user's daily run limit and increment if within limits.
    Uses database-level locking to prevent race conditions across multiple workers.
    Returns (True, "") if allowed, (False, reason) if at or over limit.
    """
    subscription = models.get_user_subscription(user_id)
    if not subscription:
        return False, "No active subscription found."

    plan_id = subscription.get("plan_id")
    plan = _get_plan(plan_id) if plan_id else None
    if not plan:
        return False, "Plan configuration not found."

    runs_limit = plan.get("runs_per_day")

    # Atomic check-and-increment using database transaction with advisory lock
    return _atomic_run_count_check(user_id, runs_limit)


def _atomic_run_count_check(user_id: str, runs_limit: Optional[int]) -> Tuple[bool, str]:
    """
    Atomic check-and-increment using PostgreSQL advisory lock to prevent race conditions.
    Multiple workers can run in parallel without exceeding the daily limit.
    """
    import hashlib
    try:
        with models.db_cursor() as cur:
            # Use a stable advisory lock ID based on user_id
            lock_id = int(hashlib.md5(user_id.encode()).hexdigest()[:8], 16) % (2**31)

            # Acquire exclusive lock for this user
            cur.execute("SELECT pg_advisory_lock(%s)", (lock_id,))

            # Check the current count under lock
            current_count = models.get_daily_run_count(user_id)

            if runs_limit is None:
                # Unlimited plan — increment and proceed
                models.increment_daily_run_count(user_id)
                return True, ""

            if current_count >= runs_limit:
                # At or over limit
                return False, (
                    f"You have used {current_count} of {runs_limit} daily screen"
                    f"{'s' if runs_limit != 1 else ''} allowed on your plan. "
                    "Upgrade or try again tomorrow."
                )

            # Within limit — increment atomically
            models.increment_daily_run_count(user_id)
            # Lock is automatically released when transaction commits
            return True, ""
    except Exception as exc:
        logger.error(f"Atomic run count check failed for user {user_id}: {exc}", exc_info=True)
        # Fail open: allow the run if database fails (better UX than blocking)
        # Log this so we can investigate
        models.log_audit(
            user_id=user_id,
            action="run_limit_check_failed",
            details_dict={"error": str(exc)},
        ) if hasattr(models, 'log_audit') else None
        return True, ""  # Allow on DB error to prevent false positives
