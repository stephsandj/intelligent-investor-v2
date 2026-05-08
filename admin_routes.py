"""
admin_routes.py — Flask Blueprint for admin portal API routes.

All data API routes (/admin/api/*) require @admin_portal_required (admin_accounts table).
Admin accounts are completely separate from app users.
g.admin_id / g.admin is set by admin_portal_required before each handler runs.
"""

import secrets
import logging
from datetime import datetime, timezone, timedelta

from flask import Blueprint, g, jsonify, request, make_response

logger = logging.getLogger("admin_routes")

from auth import admin_portal_required, hash_password, check_password, generate_admin_token, decode_token, clear_admin_session
from models import (
    get_all_users_admin,
    get_user_by_id,
    get_user_by_email,
    get_user_subscription,
    update_subscription_status,
    update_subscription_plan,
    reset_daily_run_count,
    set_daily_run_count,
    get_user_runs_admin,
    get_all_subscriptions_admin,
    get_audit_log_for_user,
    extend_trial_ends_at,
    count_total_users,
    get_active_subscriptions_by_plan,
    get_runs_today_all_users,
    log_audit,
    set_admin,
    is_admin,
    db_cursor,
    create_user,
    create_subscription,
    verify_user_email,
    set_email_verify_token,
    update_user_profile,
    update_user_email,
    delete_user,
    get_signups_by_day,
    get_all_subscriptions_with_user,
    get_trial_subscriptions_count,
    get_admin_account_by_email,
    get_admin_account_by_id,
    get_all_admin_accounts,
    create_admin_account,
    update_admin_last_login,
    get_daily_run_count,
    get_metrics_series,
)

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/admin")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_body(*required_fields):
    """Parse JSON body and validate required fields.

    Returns (data_dict, None) on success or (None, error_response) on failure.
    """
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    for field in required_fields:
        if field not in data:
            return None, (jsonify({"error": f"Missing required field: {field}"}), 400)
    return data, None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _handle_exception(exc: Exception, action: str, logger_obj) -> tuple:
    """Log exception details server-side, return generic error to client."""
    logger_obj.error(f"{action} failed: {type(exc).__name__}: {str(exc)}", exc_info=True)
    return jsonify({"error": "An error occurred. Please try again."}), 500


def _require_superadmin():
    """Return a 403 response tuple if the current admin is not a superadmin, else None.

    Usage inside a view:
        err = _require_superadmin()
        if err: return err
    """
    role = (g.admin or {}).get("role", "")
    if role != "superadmin":
        logger.warning(
            "RBAC: admin %s (role=%s) attempted a superadmin-only action on %s",
            g.admin_id, role, request.path,
        )
        return jsonify({"error": "Superadmin role required for this action", "code": "forbidden"}), 403
    return None


# ---------------------------------------------------------------------------
# POST /admin/api/login  — Admin portal login (admin_accounts table)
# ---------------------------------------------------------------------------

@admin_bp.route("/api/login", methods=["POST"])
def admin_login():
    """
    POST /admin/api/login
    Body: { "email": "...", "password": "..." }
    Authenticates against the admin_accounts table (completely separate from users).
    Sets an HttpOnly 'admin_access' cookie valid for 8 hours.
    """
    data = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    password =  data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 422

    admin = get_admin_account_by_email(email)
    if not admin or not check_password(password, admin["password_hash"]):
        return jsonify({"error": "Invalid admin credentials"}), 401

    admin_id = str(admin["id"])
    token = generate_admin_token(admin_id)
    update_admin_last_login(admin_id, request.remote_addr)

    log_audit(
        actor_id=admin_id,
        action="admin_login",
        notes=f"Admin login: {email}",
        ip_address=request.remote_addr,
    )

    import secrets as _secrets
    admin_csrf = _secrets.token_hex(32)

    resp = make_response(jsonify({
        "ok": True,
        "admin_id": admin_id,
        "email": admin["email"],
        "full_name": admin.get("full_name"),
        "role": admin.get("role"),
    }))
    resp.set_cookie(
        "admin_access", token,
        httponly=True, samesite="Lax", secure=True,   # must be True in prod — HTTPS only
        max_age=8 * 3600,
        path="/admin",
    )
    # JS-readable CSRF token for admin portal double-submit pattern
    resp.set_cookie(
        "admin_csrf_token", admin_csrf,
        httponly=False, samesite="Lax", secure=True,
        max_age=8 * 3600,
        path="/admin",
    )
    return resp, 200


# ---------------------------------------------------------------------------
# POST /admin/api/logout
# ---------------------------------------------------------------------------

@admin_bp.route("/api/logout", methods=["POST"])
def admin_logout():
    """Clear the admin_access cookie and server-side idle tracking."""
    token = request.cookies.get("admin_access", "")
    if token:
        try:
            payload = decode_token(token)
            admin_id = payload.get("sub")
            if admin_id:
                clear_admin_session(admin_id)
        except Exception:
            pass
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("admin_access",       path="/admin", samesite="Lax", secure=True)
    resp.delete_cookie("admin_csrf_token",   path="/admin", samesite="Lax", secure=True)
    return resp, 200


# ---------------------------------------------------------------------------
# GET /admin/api/me  — Current admin info
# ---------------------------------------------------------------------------

@admin_bp.route("/api/me", methods=["GET"])
@admin_portal_required
def admin_me():
    """Return current admin account info."""
    return jsonify({
        "admin_id": g.admin_id,
        "email": g.admin["email"],
        "full_name": g.admin.get("full_name"),
        "role": g.admin.get("role"),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/api/users
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users", methods=["GET"])
@admin_portal_required
def api_list_users():
    """Return all app users with their plan, status, and daily run count."""
    users = get_all_users_admin()
    # Serialize datetimes
    serialized = []
    for u in users:
        row = {}
        for k, v in u.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            else:
                row[k] = v
        serialized.append(row)
    return jsonify({"users": serialized}), 200


# ---------------------------------------------------------------------------
# GET /admin/api/stats
# ---------------------------------------------------------------------------

@admin_bp.route("/api/stats", methods=["GET"])
@admin_portal_required
def api_stats():
    """Return aggregate platform statistics."""
    total_users = count_total_users()
    active_subs_by_plan = get_active_subscriptions_by_plan()
    runs_today = get_runs_today_all_users()

    mrr = 0.0
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT s.billing_cycle, p.price_monthly, p.price_yearly, COUNT(*) AS cnt
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.status = 'active'
                  AND (s.expires_at IS NULL OR s.expires_at > NOW())
                GROUP BY s.billing_cycle, p.price_monthly, p.price_yearly
                """
            )
            for row in cur.fetchall():
                bc  = row.get("billing_cycle") or ""
                pm  = float(row.get("price_monthly") or 0)
                py_ = float(row.get("price_yearly")  or 0)
                cnt = int(row.get("cnt") or 0)
                if bc == "monthly":
                    mrr += pm * cnt
                elif bc == "yearly":
                    mrr += (py_ / 12.0) * cnt
    except Exception:
        pass

    try:
        active_trials_count = get_trial_subscriptions_count()
    except Exception:
        active_trials_count = 0

    return jsonify({
        "total_users": total_users,
        "active_subscriptions": sum(active_subs_by_plan.values()),
        "active_trials": active_trials_count,
        "active_subscriptions_by_plan": active_subs_by_plan,
        "mrr_usd": round(mrr, 2),
        "runs_today": runs_today,
        "as_of": _utcnow().isoformat(),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/api/users/<user_id>
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>", methods=["GET"])
@admin_portal_required
def api_user_detail(user_id: str):
    """Return full profile, subscription, recent runs, and audit log for a user."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    subscription = get_user_subscription(user_id)
    runs = get_user_runs_admin(user_id, limit=20)
    try:
        audit_entries = get_audit_log_for_user(user_id, limit=50)
    except Exception:
        audit_entries = []

    def _ser(obj):
        if isinstance(obj, dict):
            return {k: _ser(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_ser(i) for i in obj]
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return obj

    return jsonify({
        "user":        _ser(dict(user)),
        "subscription": _ser(dict(subscription)) if subscription else None,
        "recent_runs":  _ser(runs),
        "audit_log":    _ser(audit_entries),
        "daily_runs_today": get_daily_run_count(user_id),
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/<user_id>/set-runs  — manually set daily run count
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>/set-runs", methods=["POST"])
@admin_portal_required
def api_set_user_runs(user_id: str):
    """
    Manually set the daily run count for a user to any value (0 or more).
    Body: { "count": <int> }
    """
    data, err = _parse_json_body("count")
    if err:
        return err

    try:
        count = int(data["count"])
    except (TypeError, ValueError):
        return jsonify({"error": "count must be a non-negative integer"}), 400

    if count < 0:
        return jsonify({"error": "count must be >= 0"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    new_count = set_daily_run_count(user_id, count)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_set_daily_runs",
        notes=f"Daily run count manually set to {count} by admin {g.admin.get('email')}.",
    )
    return jsonify({"message": f"Daily run count set to {new_count} for user {user_id}.", "count": new_count}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/<user_id>/reset-runs
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>/reset-runs", methods=["POST"])
@admin_portal_required
def api_reset_user_runs(user_id: str):
    """Reset the daily run count for a user to 0."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    reset_daily_run_count(user_id)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="reset_daily_runs",
        notes=f"Daily run count reset to 0 by admin {g.admin.get('email')}.",
    )
    return jsonify({"message": f"Daily run count reset to 0 for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/<user_id>/plan
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>/plan", methods=["POST"])
@admin_portal_required
def api_update_user_plan(user_id: str):
    """Update the user's subscription plan."""
    data, err = _parse_json_body("plan_id")
    if err:
        return err

    plan_id: int = data["plan_id"]
    billing_cycle: str = data.get("billing_cycle", "monthly")
    expires_at_raw: str = data.get("expires_at")

    if billing_cycle not in ("monthly", "yearly"):
        billing_cycle = "monthly"

    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return jsonify({"error": "expires_at must be a valid ISO 8601 date string"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_plan(user_id=user_id, plan_id=plan_id, billing_cycle=billing_cycle, expires_at=expires_at)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="update_plan",
        notes=f"plan_id={plan_id}, billing_cycle={billing_cycle}, expires_at={expires_at_raw}. By admin {g.admin.get('email')}.",
    )
    return jsonify({"message": f"Plan updated for user {user_id}.", "plan_id": plan_id}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/<user_id>/status
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>/status", methods=["POST"])
@admin_portal_required
def api_update_user_status(user_id: str):
    """Update a user's subscription status."""
    data, err = _parse_json_body("status")
    if err:
        return err

    status = data["status"]
    allowed_statuses = ("active", "inactive", "cancelled", "pending_payment", "trial", "expired")
    if status not in allowed_statuses:
        return jsonify({"error": f"status must be one of {allowed_statuses}"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_status(user_id, status)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="update_status",
        notes=f"Status updated to '{status}' by admin {g.admin.get('email')}.",
    )
    return jsonify({"message": f"Status updated to '{status}' for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/<user_id>/extend-trial
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>/extend-trial", methods=["POST"])
@admin_portal_required
def api_extend_trial(user_id: str):
    """Extend trial_ends_at by N days. Body: {"days": <int>}"""
    data, err = _parse_json_body("days")
    if err:
        return err

    try:
        days = int(data["days"])
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400

    if days <= 0:
        return jsonify({"error": "days must be a positive integer"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    subscription = get_user_subscription(user_id)
    if not subscription:
        return jsonify({"error": "No subscription found for this user"}), 404

    current_trial_ends = subscription.get("trial_ends_at")
    if current_trial_ends:
        base = current_trial_ends if hasattr(current_trial_ends, "tzinfo") else datetime.fromisoformat(str(current_trial_ends))
        if base < _utcnow():
            base = _utcnow()
    else:
        base = _utcnow()

    new_trial_ends = base + timedelta(days=days)
    extend_trial_ends_at(user_id, new_trial_ends)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="extend_trial",
        notes=f"Trial extended by {days} day(s). New trial_ends_at: {new_trial_ends.isoformat()}",
    )
    return jsonify({"message": f"Trial extended by {days} day(s).", "new_trial_ends_at": new_trial_ends.isoformat()}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users  — create a new app user
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users", methods=["POST"])
@admin_portal_required
def api_create_user():
    """Create a new app user with optional plan."""
    data, err = _parse_json_body("email")
    if err:
        return err

    email     = (data.get("email") or "").strip().lower()
    password  = data.get("password", "") or secrets.token_urlsafe(16)
    full_name = (data.get("full_name") or "").strip() or None
    plan_id   = int(data.get("plan_id", 1))
    status    = data.get("status", "active")
    should_verify = bool(data.get("verify_email", False))

    if not email or "@" not in email:
        return jsonify({"error": "Invalid input"}), 422
    if len(password) < 12:
        password = secrets.token_urlsafe(16)

    if get_user_by_email(email):
        # Don't leak that email exists (user enumeration protection)
        logger.warning(f"Admin {g.admin_id} attempted to create duplicate user: {email}")
        return jsonify({"error": "Invalid input"}), 422

    try:
        pw_hash = hash_password(password)
        user = create_user(email, pw_hash, full_name)
    except Exception as exc:
        return _handle_exception(exc, "create_user", logger)

    user_id = str(user["id"])
    try:
        trial_ends_at = _utcnow() + timedelta(days=7) if plan_id == 1 else None
        create_subscription(user_id=user_id, plan_id=plan_id, status=status, trial_ends_at=trial_ends_at)
    except Exception as exc:
        logger.error(f"Subscription creation failed for user {user_id}: {type(exc).__name__}: {str(exc)}", exc_info=True)
        return jsonify({"error": "User created but subscription setup failed"}), 207

    if should_verify:
        try:
            verify_user_email(user_id)
        except Exception:
            pass

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_create_user",
        notes=f"Created by admin {g.admin.get('email')}. email={email}, plan_id={plan_id}, status={status}",
    )
    return jsonify({"message": "User created successfully.", "user_id": user_id, "email": email}), 201


# ---------------------------------------------------------------------------
# DELETE /admin/api/users/<user_id>
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>", methods=["DELETE"])
@admin_portal_required
def api_delete_user(user_id: str):
    """Hard-delete a user and all related data."""
    err = _require_superadmin()
    if err:
        return err
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_delete_user",
        notes=f"Deleted user: {user.get('email')} by admin {g.admin.get('email')}",
    )
    try:
        delete_user(user_id)
    except Exception as exc:
        logger.error("delete_user failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500
    return jsonify({"message": f"User {user_id} deleted."}), 200


# ---------------------------------------------------------------------------
# POST /admin/api/users/bulk-delete  — delete multiple users at once
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/bulk-delete", methods=["POST"])
@admin_portal_required
def api_bulk_delete_users():
    """Bulk hard-delete a list of users. Body: { user_ids: [uuid, ...] }"""
    err = _require_superadmin()
    if err:
        return err
    data     = request.get_json(silent=True) or {}
    user_ids = data.get("user_ids", [])

    if not user_ids or not isinstance(user_ids, list):
        return jsonify({"error": "user_ids must be a non-empty list"}), 422
    if len(user_ids) > 200:
        return jsonify({"error": "Cannot delete more than 200 users at once"}), 422

    deleted = 0
    errors  = []

    for uid in user_ids:
        try:
            user = get_user_by_id(uid)
            if not user:
                errors.append(f"{uid}: not found")
                continue
            log_audit(
                actor_id=g.admin_id,
                target_user_id=uid,
                action="admin_bulk_delete_user",
                notes=f"Bulk deleted: {user.get('email')} by {g.admin.get('email')}",
            )
            delete_user(uid)
            deleted += 1
        except Exception as exc:
            logger.error("bulk_delete uid=%s failed: %s", uid, exc, exc_info=True)
            errors.append(f"{uid}: internal error")

    return jsonify({"deleted": deleted, "errors": errors}), 200


# ---------------------------------------------------------------------------
# PUT /admin/api/users/<user_id>  — update name / email / password
# ---------------------------------------------------------------------------

@admin_bp.route("/api/users/<string:user_id>", methods=["PUT"])
@admin_portal_required
def api_update_user(user_id: str):
    """Update a user's full_name, email, or password."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    changes = []
    if "full_name" in data:
        try:
            update_user_profile(user_id, full_name=(data["full_name"] or "").strip() or None)
            changes.append("full_name")
        except Exception as exc:
            logger.error("update_full_name failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if "email" in data:
        new_email = (data["email"] or "").strip().lower()
        if not new_email or "@" not in new_email:
            return jsonify({"error": "Invalid email address"}), 422
        existing = get_user_by_email(new_email)
        if existing and str(existing["id"]) != user_id:
            return jsonify({"error": "That email is already in use"}), 409
        try:
            update_user_email(user_id, new_email)
            changes.append("email")
        except Exception as exc:
            logger.error("update_email failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if "password" in data:
        new_pw = data["password"]
        if not new_pw or len(new_pw) < 12:
            return jsonify({"error": "Password must be at least 12 characters"}), 422
        try:
            with db_cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(new_pw), user_id))
            changes.append("password")
        except Exception as exc:
            logger.error("update_password failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if changes:
        log_audit(
            actor_id=g.admin_id,
            target_user_id=user_id,
            action="admin_update_user",
            notes=f"Updated fields: {', '.join(changes)} by admin {g.admin.get('email')}",
        )
    return jsonify({"message": f"User {user_id} updated.", "changed_fields": changes}), 200


# ---------------------------------------------------------------------------
# GET /admin/api/plans
# ---------------------------------------------------------------------------

@admin_bp.route("/api/plans", methods=["GET"])
@admin_portal_required
def api_list_plans():
    """Return all subscription plans."""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT id, name, display_name, price_monthly, price_yearly,
                          runs_per_day, max_ai_picks, max_pdf_history, features, sort_order
                   FROM plans WHERE is_active = TRUE ORDER BY sort_order ASC"""
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    plans = []
    for row in rows:
        p = dict(row)
        if p.get("features") and isinstance(p["features"], str):
            import json as _json
            try:
                p["features"] = _json.loads(p["features"])
            except Exception:
                pass
        plans.append(p)
    return jsonify({"plans": plans}), 200


# ---------------------------------------------------------------------------
# GET /admin/api/audit-log
# ---------------------------------------------------------------------------

@admin_bp.route("/api/audit-log", methods=["GET"])
@admin_portal_required
def api_audit_log():
    """Return the last 100 audit log entries."""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT al.id, al.actor_id, al.target_user_id, al.action, al.notes, al.created_at,
                       target_u.email AS target_email
                FROM audit_log al
                LEFT JOIN users target_u ON target_u.id = al.target_user_id
                ORDER BY al.created_at DESC LIMIT 100
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    entries = []
    for row in rows:
        entry = dict(row)
        if "created_at" in entry and hasattr(entry["created_at"], "isoformat"):
            entry["created_at"] = entry["created_at"].isoformat()
        entries.append(entry)
    return jsonify({"audit_log": entries, "count": len(entries)}), 200


# ---------------------------------------------------------------------------
# GET /admin/api/analytics
# ---------------------------------------------------------------------------

@admin_bp.route("/api/analytics", methods=["GET"])
@admin_portal_required
def api_analytics():
    """Return analytics: daily signups + plan distribution + trial count."""
    days = min(int(request.args.get("days", 30)), 365)
    try:
        signups_by_day = get_signups_by_day(days=days)
    except Exception:
        signups_by_day = []
    try:
        plan_distribution = get_active_subscriptions_by_plan()
    except Exception:
        plan_distribution = {}
    try:
        trial_count = get_trial_subscriptions_count()
    except Exception:
        trial_count = 0
    try:
        total_users = count_total_users()
    except Exception:
        total_users = 0

    return jsonify({
        "signups_by_day": signups_by_day,
        "plan_distribution": plan_distribution,
        "trial_count": trial_count,
        "total_users": total_users,
        "days": days,
        "as_of": _utcnow().isoformat(),
    }), 200


# ---------------------------------------------------------------------------
# Legacy routes (kept for backward compat, use admin_portal_required)
# ---------------------------------------------------------------------------

# GET /admin/users
# ---------------------------------------------------------------------------

@admin_bp.route("/users", methods=["GET"])
@admin_portal_required
def list_users():
    """Return all users with their plan, status, and daily run count."""
    users = get_all_users_admin()
    return jsonify({"users": users}), 200


# ---------------------------------------------------------------------------
# GET /admin/users/<user_id>
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>", methods=["GET"])
@admin_portal_required
def user_detail(user_id: str):
    """Return full profile, subscription, last 20 runs, and audit log for a user."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    subscription = get_user_subscription(user_id)
    runs = get_user_runs_admin(user_id, limit=20)

    try:
        audit_entries = get_audit_log_for_user(user_id, limit=50)
    except Exception:
        audit_entries = []

    return jsonify({
        "user": user,
        "subscription": subscription,
        "recent_runs": runs,
        "audit_log": audit_entries,
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/activate
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/activate", methods=["POST"])
@admin_portal_required
def activate_user(user_id: str):
    """Set subscription status to 'active' and log an audit entry."""
    data, err = _parse_json_body()
    if err:
        return err

    notes = data.get("notes", "")

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_status(user_id, "active", activated_by=None)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="activate_user",
        notes=notes,
    )

    return jsonify({"message": f"User {user_id} activated.", "status": "active"}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/deactivate
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/deactivate", methods=["POST"])
@admin_portal_required
def deactivate_user(user_id: str):
    """Set subscription status to 'inactive' and log an audit entry."""
    data, err = _parse_json_body()
    if err:
        return err

    notes = data.get("notes", "")

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_status(user_id, "inactive")
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="deactivate_user",
        notes=notes,
    )

    return jsonify({"message": f"User {user_id} deactivated.", "status": "inactive"}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/plan
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/plan", methods=["POST"])
@admin_portal_required
def update_user_plan(user_id: str):
    """Update the user's subscription plan.

    Expected JSON body:
        {
            "plan_id": <int>,
            "billing_cycle": "monthly" | "yearly",
            "expires_at": "<ISO 8601 date string>",   // e.g. "2025-12-31" (for paid plans)
            "trial_ends_at": "<ISO 8601 date string>" // e.g. "2025-01-15" (for trials)
        }
    """
    data, err = _parse_json_body("plan_id")
    if err:
        return err

    plan_id: int = data["plan_id"]
    billing_cycle: str = data.get("billing_cycle", "monthly")
    expires_at_raw: str = data.get("expires_at")
    trial_ends_raw: str = data.get("trial_ends_at")

    if billing_cycle not in ("monthly", "yearly"):
        billing_cycle = "monthly"

    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return jsonify({"error": "expires_at must be a valid ISO 8601 date/datetime string"}), 400

    trial_ends_at = None
    if trial_ends_raw:
        try:
            trial_ends_at = datetime.fromisoformat(trial_ends_raw)
        except ValueError:
            return jsonify({"error": "trial_ends_at must be a valid ISO 8601 date/datetime string"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_plan(
        user_id=user_id,
        plan_id=plan_id,
        billing_cycle=billing_cycle,
        expires_at=expires_at,
    )

    # Update trial_ends_at if provided
    if trial_ends_at:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE subscriptions SET trial_ends_at = %s WHERE user_id = %s",
                (trial_ends_at, user_id),
            )

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="update_plan",
        notes=f"plan_id={plan_id}, billing_cycle={billing_cycle}, expires_at={expires_at_raw}, trial_ends_at={trial_ends_raw}",
    )

    return jsonify({
        "message": f"Plan updated for user {user_id}.",
        "plan_id": plan_id,
        "billing_cycle": billing_cycle,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "trial_ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/reset-runs
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/reset-runs", methods=["POST"])
@admin_portal_required
def reset_user_runs(user_id: str):
    """Reset the daily run count for a user to 0."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    reset_daily_run_count(user_id)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="reset_daily_runs",
        notes="Daily run count reset to 0 by admin.",
    )

    return jsonify({"message": f"Daily run count reset for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/extend-trial
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/extend-trial", methods=["POST"])
@admin_portal_required
def extend_trial(user_id: str):
    """Extend trial_ends_at by N days.

    Expected JSON body: {"days": <int>}
    """
    data, err = _parse_json_body("days")
    if err:
        return err

    try:
        days = int(data["days"])
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400

    if days <= 0:
        return jsonify({"error": "days must be a positive integer"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    subscription = get_user_subscription(user_id)
    if not subscription:
        return jsonify({"error": "No subscription found for this user"}), 404

    # Determine base date: existing trial_ends_at (if future) or now
    current_trial_ends = subscription.get("trial_ends_at")
    if current_trial_ends:
        if isinstance(current_trial_ends, str):
            base = datetime.fromisoformat(current_trial_ends)
        else:
            base = current_trial_ends
        if base < _utcnow():
            base = _utcnow()
    else:
        base = _utcnow()

    new_trial_ends = base + timedelta(days=days)
    extend_trial_ends_at(user_id, new_trial_ends)

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="extend_trial",
        notes=f"Trial extended by {days} day(s). New trial_ends_at: {new_trial_ends.isoformat()}",
    )

    return jsonify({
        "message": f"Trial extended by {days} day(s) for user {user_id}.",
        "new_trial_ends_at": new_trial_ends.isoformat(),
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/make-admin
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/make-admin", methods=["POST"])
@admin_portal_required
def make_admin(user_id: str):
    """Grant admin role to a user."""
    err = _require_superadmin()
    if err:
        return err
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    if is_admin(user_id):
        return jsonify({"message": f"User {user_id} is already an admin."}), 200

    set_admin(user_id)   # set_admin only takes user_id (inserts into admin_users)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="grant_admin",
        notes=f"Admin role granted by user {g.admin_id}.",
    )

    return jsonify({"message": f"User {user_id} has been granted admin privileges."}), 200


# ---------------------------------------------------------------------------
# GET /admin/stats
# ---------------------------------------------------------------------------

@admin_bp.route("/stats", methods=["GET"])
@admin_portal_required
def stats():
    """Return aggregate platform statistics.

    Calculated metrics:
        - total_users: count of all registered users
        - active_subscriptions_by_plan: dict of plan_name -> count of active subs
        - mrr_usd: Monthly Recurring Revenue (USD)
        - runs_today: total screening runs across all users today
    """
    total_users = count_total_users()
    active_subs_by_plan = get_active_subscriptions_by_plan()
    runs_today = get_runs_today_all_users()

    # MRR calculation using db_cursor (no external 'db' module needed)
    mrr = 0.0
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT
                    s.billing_cycle,
                    p.price_monthly,
                    p.price_yearly,
                    COUNT(*) AS cnt
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.status = 'active'
                  AND (s.expires_at IS NULL OR s.expires_at > NOW())
                GROUP BY s.billing_cycle, p.price_monthly, p.price_yearly
                """
            )
            rows = cur.fetchall()
        for row in rows:
            billing_cycle = row.get("billing_cycle") or ""
            price_monthly = float(row.get("price_monthly") or 0)
            price_yearly = float(row.get("price_yearly") or 0)
            cnt = int(row.get("cnt") or 0)
            if billing_cycle == "monthly":
                mrr += price_monthly * cnt
            elif billing_cycle == "yearly":
                mrr += (price_yearly / 12.0) * cnt
    except Exception as exc:
        pass  # MRR is non-critical; return 0 on DB error

    # Active trials and total active subs
    try:
        active_trials_count = get_trial_subscriptions_count()
    except Exception:
        active_trials_count = 0

    total_active = sum(active_subs_by_plan.values())

    return jsonify({
        "total_users": total_users,
        "active_subscriptions": total_active,
        "active_trials": active_trials_count,
        "active_subscriptions_by_plan": active_subs_by_plan,
        "plan_breakdown": active_subs_by_plan,  # alias for frontend
        "mrr_usd": round(mrr, 2),
        "runs_today": runs_today,
        "as_of": _utcnow().isoformat(),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/pending-payment
# ---------------------------------------------------------------------------

@admin_bp.route("/pending-payment", methods=["GET"])
@admin_portal_required
def pending_payment():
    """Return subscriptions with status='pending_payment'."""
    subscriptions = get_all_subscriptions_admin(status_filter="pending_payment")
    return jsonify({
        "pending_payment": subscriptions,
        "count": len(subscriptions),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/audit-log
# ---------------------------------------------------------------------------

@admin_bp.route("/audit-log", methods=["GET"])
@admin_portal_required
def audit_log():
    """Return the last 100 audit log entries, joined with users for email."""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT
                    al.id,
                    al.actor_id,
                    actor_u.email           AS actor_email,
                    al.target_user_id,
                    target_u.email          AS target_email,
                    al.action,
                    al.notes,
                    al.created_at
                FROM audit_log al
                LEFT JOIN users actor_u  ON actor_u.id  = al.actor_id
                LEFT JOIN users target_u ON target_u.id = al.target_user_id
                ORDER BY al.created_at DESC
                LIMIT 100
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    entries = []
    for row in rows:
        entry = dict(row)
        if "created_at" in entry and hasattr(entry["created_at"], "isoformat"):
            entry["created_at"] = entry["created_at"].isoformat()
        entries.append(entry)

    return jsonify({"audit_log": entries, "count": len(entries)}), 200


# ---------------------------------------------------------------------------
# GET /admin/plans
# ---------------------------------------------------------------------------

@admin_bp.route("/plans", methods=["GET"])
@admin_portal_required
def list_plans():
    """Return all subscription plans."""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT id, name, display_name, price_monthly, price_yearly,
                       runs_per_day, max_ai_picks, max_pdf_history, features
                FROM plans ORDER BY price_monthly ASC
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    plans = []
    for row in rows:
        p = dict(row)
        if p.get("features") and isinstance(p["features"], str):
            import json as _json
            try:
                p["features"] = _json.loads(p["features"])
            except Exception:
                pass
        plans.append(p)

    return jsonify({"plans": plans}), 200


# PUT /admin/plans/<plan_id>
# ---------------------------------------------------------------------------

@admin_bp.route("/plans/<int:plan_id>", methods=["PUT"])
@admin_portal_required
def update_plan(plan_id):
    """Update plan configuration (display name, pricing, limits)."""
    data = request.get_json() or {}

    display_name = data.get("display_name", "").strip()
    price_monthly = data.get("price_monthly")
    price_yearly = data.get("price_yearly")
    runs_per_day = data.get("runs_per_day")
    max_ai_picks = data.get("max_ai_picks")
    max_pdf_history = data.get("max_pdf_history")

    # Validate
    if not display_name:
        return jsonify({"error": "display_name is required"}), 400

    # Convert to appropriate types
    try:
        if price_monthly is not None:
            price_monthly = float(price_monthly)
        if price_yearly is not None:
            price_yearly = float(price_yearly)
        if runs_per_day is not None:
            runs_per_day = int(runs_per_day) if runs_per_day != "" else None
        if max_ai_picks is not None:
            max_ai_picks = int(max_ai_picks) if max_ai_picks != "" else None
        if max_pdf_history is not None:
            max_pdf_history = int(max_pdf_history) if max_pdf_history != "" else None
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid numeric values"}), 400

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE plans
                SET display_name = %s,
                    price_monthly = %s,
                    price_yearly = %s,
                    runs_per_day = %s,
                    max_ai_picks = %s,
                    max_pdf_history = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, display_name, price_monthly, price_yearly,
                          runs_per_day, max_ai_picks, max_pdf_history, features
                """,
                (display_name, price_monthly, price_yearly, runs_per_day,
                 max_ai_picks, max_pdf_history, plan_id)
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    if not row:
        return jsonify({"error": "Plan not found"}), 404

    plan = dict(row)
    if plan.get("features") and isinstance(plan["features"], str):
        import json as _json
        try:
            plan["features"] = _json.loads(plan["features"])
        except Exception:
            pass

    # Log audit entry
    log_audit(
        actor_id=g.admin_id,
        action="update_plan",
        notes=f"Updated plan '{plan.get('name')}' (id={plan_id}): display_name={display_name}",
    )

    return jsonify({"plan": plan, "message": "Plan updated successfully"}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/status — update subscription status
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/status", methods=["POST"])
@admin_portal_required
def update_user_status(user_id: str):
    """Update a user's subscription status."""
    data, err = _parse_json_body("status")
    if err:
        return err

    status = data["status"]
    allowed_statuses = ("active", "inactive", "cancelled", "pending_payment", "trial", "expired")
    if status not in allowed_statuses:
        return jsonify({"error": f"status must be one of {allowed_statuses}"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    update_subscription_status(user_id, status, activated_by=None)
    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="update_status",
        notes=f"Status updated to '{status}' by admin.",
    )

    return jsonify({"message": f"Status updated to '{status}' for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/users  — create a new user with optional plan
# ---------------------------------------------------------------------------

@admin_bp.route("/users", methods=["POST"])
@admin_portal_required
def create_admin_user():
    """Create a new user and optionally assign a plan.

    Expected JSON body:
        {
            "email": "user@example.com",
            "password": "secret123",           // optional — auto-generated if omitted
            "full_name": "Jane Doe",           // optional
            "plan_id": 1,                      // optional (defaults to 1 = trial)
            "status": "active",                // optional (defaults to "active")
            "verify_email": true               // optional (defaults to false)
        }
    """
    data, err = _parse_json_body("email")
    if err:
        return err

    email     = (data.get("email") or "").strip().lower()
    # Auto-generate a random temp password if none provided
    password  = data.get("password", "") or secrets.token_urlsafe(16)
    full_name = (data.get("full_name") or "").strip() or None
    plan_id   = int(data.get("plan_id", 1))
    status    = data.get("status", "active")  # Default to "active" - trial is marked via trial_ends_at
    should_verify = bool(data.get("verify_email", False))

    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required"}), 422
    if not password or len(password) < 12:
        password = secrets.token_urlsafe(16)  # fallback

    if get_user_by_email(email):
        return jsonify({"error": "An account with that email already exists"}), 409

    try:
        password_hash = hash_password(password)
        user = create_user(email, password_hash, full_name)
    except Exception as exc:
        logger.error("create_user failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    user_id = str(user["id"])

    # Create subscription
    try:
        # Trial users get 7-day trial_ends_at; status is always "active"
        trial_ends_at = _utcnow() + timedelta(days=7) if plan_id == 1 else None
        create_subscription(
            user_id=user_id,
            plan_id=plan_id,
            status=status,
            trial_ends_at=trial_ends_at,
        )
    except Exception as exc:
        logger.error("subscription_create failed: %s", exc, exc_info=True)
        return jsonify({"error": "User created but subscription setup encountered an error"}), 207

    # Optionally mark email as verified
    if should_verify:
        try:
            verify_user_email(user_id)
        except Exception:
            pass
    else:
        # Generate verification token
        try:
            token = secrets.token_urlsafe(32)
            set_email_verify_token(user_id, token)
        except Exception:
            pass

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_create_user",
        notes=f"Created by admin. email={email}, plan_id={plan_id}, status={status}",
    )

    return jsonify({
        "message": "User created successfully.",
        "user_id": user_id,
        "email":   email,
    }), 201


# ---------------------------------------------------------------------------
# DELETE /admin/users/<user_id>
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>", methods=["DELETE"])
@admin_portal_required
def delete_admin_user(user_id: str):
    """Hard-delete a user and all related data."""
    err = _require_superadmin()
    if err:
        return err
    if user_id == g.admin_id:
        return jsonify({"error": "You cannot delete your own account"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_delete_user",
        notes=f"Deleted user: {user.get('email')}",
    )
    try:
        delete_user(user_id)
    except Exception as exc:
        logger.error("delete_user failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    return jsonify({"message": f"User {user_id} deleted."}), 200


# ---------------------------------------------------------------------------
# PUT /admin/users/<user_id>  — update name / email / password
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>", methods=["PUT"])
@admin_portal_required
def update_admin_user(user_id: str):
    """Update a user's full_name, email, or password.

    Expected JSON body (all fields optional):
        {
            "full_name": "New Name",
            "email": "new@example.com",
            "password": "<new_password>"
        }
    """
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    changes = []

    if "full_name" in data:
        full_name = (data["full_name"] or "").strip() or None
        try:
            update_user_profile(user_id, full_name=full_name)
            changes.append("full_name")
        except Exception as exc:
            logger.error("update_full_name failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if "email" in data:
        new_email = (data["email"] or "").strip().lower()
        if not new_email or "@" not in new_email:
            return jsonify({"error": "Invalid email address"}), 422
        existing = get_user_by_email(new_email)
        if existing and str(existing["id"]) != user_id:
            return jsonify({"error": "That email is already in use"}), 409
        try:
            update_user_email(user_id, new_email)
            changes.append("email")
        except Exception as exc:
            logger.error("update_email failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if "password" in data:
        new_pw = data["password"]
        if not new_pw or len(new_pw) < 12:
            return jsonify({"error": "Password must be at least 12 characters"}), 422
        try:
            pw_hash = hash_password(new_pw)
            with db_cursor() as cur:
                cur.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (pw_hash, user_id),
                )
            changes.append("password")
        except Exception as exc:
            logger.error("update_password failed: %s", exc, exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    if changes:
        log_audit(
            actor_id=g.admin_id,
            target_user_id=user_id,
            action="admin_update_user",
            notes=f"Updated fields: {', '.join(changes)}",
        )

    return jsonify({
        "message": f"User {user_id} updated.",
        "changed_fields": changes,
    }), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/verify-email
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/verify-email", methods=["POST"])
@admin_portal_required
def admin_verify_email(user_id: str):
    """Mark a user's email as verified without requiring email link."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    if user.get("email_verified"):
        return jsonify({"message": "Email is already verified.", "already_verified": True}), 200

    try:
        verify_user_email(user_id)
    except Exception as exc:
        logger.error("verify_email failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_verify_email",
        notes="Email manually verified by admin.",
    )

    return jsonify({"message": f"Email verified for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/unverify-email
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/unverify-email", methods=["POST"])
@admin_portal_required
def admin_unverify_email(user_id: str):
    """Mark a user's email as unverified."""
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    if not user.get("email_verified"):
        return jsonify({"message": "Email is already unverified.", "already_unverified": True}), 200

    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE users SET email_verified = FALSE WHERE id = %s",
                (user_id,),
            )
    except Exception as exc:
        logger.error("unverify_email failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="admin_unverify_email",
        notes="Email manually unverified by admin.",
    )

    return jsonify({"message": f"Email unverified for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# POST /admin/users/<user_id>/revoke-admin
# ---------------------------------------------------------------------------

@admin_bp.route("/users/<string:user_id>/revoke-admin", methods=["POST"])
@admin_portal_required
def revoke_admin(user_id: str):
    """Revoke admin role from a user."""
    err = _require_superadmin()
    if err:
        return err
    if user_id == g.admin_id:
        return jsonify({"error": "You cannot revoke your own admin role"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    if not is_admin(user_id):
        return jsonify({"message": "User is not an admin.", "was_admin": False}), 200

    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM admin_users WHERE user_id = %s", (user_id,))
    except Exception as exc:
        logger.error("revoke_admin failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    log_audit(
        actor_id=g.admin_id,
        target_user_id=user_id,
        action="revoke_admin",
        notes=f"Admin role revoked by {g.admin_id}.",
    )

    return jsonify({"message": f"Admin role revoked for user {user_id}."}), 200


# ---------------------------------------------------------------------------
# GET /admin/subscriptions
# ---------------------------------------------------------------------------

@admin_bp.route("/subscriptions", methods=["GET"])
@admin_portal_required
def list_subscriptions():
    """Return all subscriptions with user and plan info."""
    limit = min(int(request.args.get("limit", 500)), 2000)
    try:
        subs = get_all_subscriptions_with_user(limit=limit)
    except Exception as exc:
        logger.error("database error: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    return jsonify({
        "subscriptions": subs,
        "count": len(subs),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/analytics
# ---------------------------------------------------------------------------

@admin_bp.route("/analytics", methods=["GET"])
@admin_portal_required
def analytics():
    """Return analytics data: daily signups + plan distribution + trial count."""
    days = min(int(request.args.get("days", 30)), 365)

    try:
        signups_by_day       = get_signups_by_day(days=days)
    except Exception as exc:
        signups_by_day = []

    try:
        plan_distribution    = get_active_subscriptions_by_plan()
    except Exception as exc:
        plan_distribution = {}

    try:
        trial_count          = get_trial_subscriptions_count()
    except Exception as exc:
        trial_count = 0

    try:
        total_users          = count_total_users()
    except Exception:
        total_users = 0

    # Revenue by plan (MRR breakdown)
    mrr_by_plan = {}
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT p.name AS plan_name, p.display_name,
                       s.billing_cycle,
                       p.price_monthly, p.price_yearly,
                       COUNT(*) AS cnt
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.status = 'active'
                  AND (s.expires_at IS NULL OR s.expires_at > NOW())
                GROUP BY p.name, p.display_name, s.billing_cycle,
                         p.price_monthly, p.price_yearly
                """
            )
            for row in cur.fetchall():
                name     = row["plan_name"]
                bc       = row["billing_cycle"] or "monthly"
                pm       = float(row["price_monthly"] or 0)
                py_      = float(row["price_yearly"]  or 0)
                cnt      = int(row["cnt"] or 0)
                contrib  = pm * cnt if bc == "monthly" else (py_ / 12.0) * cnt
                mrr_by_plan[name] = mrr_by_plan.get(name, 0) + round(contrib, 2)
    except Exception:
        pass

    return jsonify({
        "signups_by_day":    signups_by_day,
        "plan_distribution": plan_distribution,
        "mrr_by_plan":       mrr_by_plan,
        "trial_count":       trial_count,
        "total_users":       total_users,
        "days":              days,
        "as_of":             _utcnow().isoformat(),
    }), 200


# ---------------------------------------------------------------------------
# GET /admin/api/metrics  — system resource monitoring time-series
# ---------------------------------------------------------------------------

@admin_bp.route("/api/metrics", methods=["GET"])
@admin_portal_required
def api_metrics():
    """
    Return time-series system metrics for the admin monitoring dashboard.

    Query params:
      range  24h | 7d | 30d  (default: 24h)

    Response:
      series  list of {ts, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
                        screener_runs_active, http_connections, active_users_today}
      latest  most recent data point (or empty dict if no data yet)
      range   echoed back
      count   number of data points
      as_of   server timestamp
    """
    range_label = request.args.get("range", "24h")
    if range_label not in ("24h", "7d", "30d"):
        range_label = "24h"

    try:
        series = get_metrics_series(range_label)
    except Exception as exc:
        logger.error("load_metrics failed: %s", exc, exc_info=True)
        return jsonify({"error": "An internal error occurred"}), 500

    latest = series[-1] if series else {}

    return jsonify({
        "series": series,
        "latest": latest,
        "range":  range_label,
        "count":  len(series),
        "as_of":  _utcnow().isoformat(),
    }), 200
