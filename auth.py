"""
auth.py - JWT authentication module for the Stock Screening SaaS application.
Secrets from env vars: JWT_SECRET (required), JWT_ALGORITHM (default HS256).
Email delivery via: GMAIL_FROM, GMAIL_APP_PASSWORD, APP_BASE_URL.
"""

import subprocess
import sys

# Auto-install dependencies
for _pkg, _import in [("PyJWT", "jwt"), ("bcrypt", "bcrypt")]:
    try:
        __import__(_import)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg])

import jwt
import bcrypt

import logging
import os
import re
import secrets
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from typing import Dict, Optional, Tuple

import time as _time

from flask import Blueprint, g, jsonify, redirect, request

import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return secret


JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 30

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def generate_tokens(user_id: str) -> Dict[str, str]:
    now = datetime.now(tz=timezone.utc)
    access_payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    refresh_payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    }
    secret = _jwt_secret()
    return {
        "access_token": jwt.encode(access_payload, secret, algorithm=JWT_ALGORITHM),
        "refresh_token": jwt.encode(refresh_payload, secret, algorithm=JWT_ALGORITHM),
    }


def generate_admin_token(admin_id: str) -> str:
    """Generate a long-lived admin access token stored in admin_access cookie."""
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(admin_id),
        "type": "admin_access",
        "iat": now,
        "exp": now + timedelta(hours=8),  # 8-hour admin sessions
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Dict:
    return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")


_SMTP_PLACEHOLDERS = {"", "REPLACE_ME", "your-email@gmail.com", "replace_me"}


def _smtp_configured() -> bool:
    """Return True only when real (non-placeholder) SMTP credentials are present."""
    gmail_from     = os.environ.get("GMAIL_FROM", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    return (
        bool(gmail_from)
        and bool(gmail_password)
        and gmail_from.lower()     not in _SMTP_PLACEHOLDERS
        and gmail_password.lower() not in _SMTP_PLACEHOLDERS
        and "@" in gmail_from
    )


def _send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via Gmail SMTP. Returns True on success, False on failure."""
    gmail_from     = os.environ.get("GMAIL_FROM", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not _smtp_configured():
        logger.warning(
            "SMTP not configured (GMAIL_FROM / GMAIL_APP_PASSWORD are placeholders) "
            "— skipping email to %s. Set real credentials in .env to enable delivery.",
            to_email,
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Intelligent Investor <{gmail_from}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(gmail_from, gmail_password)
            server.sendmail(gmail_from, to_email, msg.as_string())
        logger.info("✉️  Email sent to %s: %s", to_email, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP auth failed for %s — check GMAIL_FROM and GMAIL_APP_PASSWORD in .env. "
            "Gmail requires an App Password (not your account password): "
            "https://myaccount.google.com/apppasswords",
            gmail_from,
        )
        return False
    except Exception as exc:
        logger.error("Email send failed to %s: %s", to_email, exc)
        return False


def _send_verification_email(to_email: str, verify_token: str) -> str:
    """Build and send the account verification email. Returns the verify URL."""
    verify_url = f"{_app_base_url()}/auth/verify-email?token={verify_token}"

    # Always log the URL so it's accessible even when SMTP is not configured
    logger.info(
        "🔗 VERIFY URL for %s → %s",
        to_email, verify_url,
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden">
      <div style="background:#f0a500;padding:24px 32px">
        <h1 style="margin:0;font-size:1.4rem;color:#0d1117">💼 Intelligent Investor</h1>
        <p style="margin:4px 0 0;font-size:.85rem;color:#0d1117;opacity:.8">Graham–Buffett Value Screener</p>
      </div>
      <div style="padding:32px">
        <h2 style="margin-top:0;font-size:1.1rem">Verify your email address</h2>
        <p style="color:#8b949e;line-height:1.6">
          Thanks for signing up! Click the button below to verify your email address
          and activate your 7-day free trial.
        </p>
        <div style="text-align:center;margin:32px 0">
          <a href="{verify_url}"
             style="background:#f0a500;color:#0d1117;padding:14px 32px;border-radius:8px;
                    font-weight:700;font-size:1rem;text-decoration:none;display:inline-block">
            ✅ Verify My Email
          </a>
        </div>
        <p style="color:#8b949e;font-size:.8rem;line-height:1.6">
          Or copy and paste this link into your browser:<br>
          <a href="{verify_url}" style="color:#f0a500;word-break:break-all">{verify_url}</a>
        </p>
        <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
        <p style="color:#6e7681;font-size:.75rem">
          If you didn't create an account, you can ignore this email safely.
        </p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(to_email, "Verify your Intelligent Investor account", html),
        daemon=True,
    ).start()

    return verify_url


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _extract_token_from_request() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return request.cookies.get("access_token")


def _get_client_ip() -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token_from_request()
        if not token:
            return jsonify({"error": "Authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token", "code": "invalid_token"}), 401

        if payload.get("type") != "access":
            return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

        user_id = payload.get("sub")
        user = models.get_user_by_id(user_id)
        if not user:
            return jsonify({"error": "User not found", "code": "user_not_found"}), 401

        g.user_id = user_id
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """Legacy decorator: checks app users table + admin_users FK table."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token_from_request()
        if not token:
            return jsonify({"error": "Authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token", "code": "invalid_token"}), 401

        if payload.get("type") != "access":
            return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

        user_id = payload.get("sub")
        user = models.get_user_by_id(user_id)
        if not user:
            return jsonify({"error": "User not found", "code": "user_not_found"}), 401

        g.user_id = user_id
        g.user = user

        if not models.is_admin(g.user_id):
            return jsonify({"error": "Admin access required", "code": "forbidden"}), 403

        return fn(*args, **kwargs)
    return wrapper


def admin_portal_required(fn):
    """
    Decorator for admin portal API routes.
    Checks for admin_access token (from admin_access cookie or Authorization header).
    Admin accounts are stored in admin_accounts table — completely separate from app users.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Try admin_access cookie first, then Authorization header
        token = request.cookies.get("admin_access") or ""
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
        if not token:
            return jsonify({"error": "Admin authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Admin session expired — please log in again", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid admin token", "code": "invalid_token"}), 401

        if payload.get("type") != "admin_access":
            return jsonify({"error": "Invalid token type for admin portal", "code": "invalid_token"}), 401

        admin_id = payload.get("sub")
        admin = models.get_admin_account_by_id(admin_id)
        if not admin:
            return jsonify({"error": "Admin account not found or inactive", "code": "admin_not_found"}), 401

        g.admin_id = admin_id
        g.admin = admin
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Blueprint routes
# ---------------------------------------------------------------------------

_login_attempts: dict = {}  # ip -> [timestamp, ...]
_RATE_LIMIT_WINDOW = 900   # 15 minutes
_RATE_LIMIT_MAX    = 10    # max failed attempts per window

def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is NOT rate-limited. Cleans up old entries."""
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    # Remove attempts outside the window
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _RATE_LIMIT_MAX

def _record_failed_login(ip: str) -> None:
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    attempts.append(now)
    _login_attempts[ip] = attempts

auth_bp = Blueprint("auth_bp", __name__, url_prefix="/auth")


@auth_bp.route("/register", methods=["POST"])
def register():
    """POST /auth/register — Create account + trial subscription + send verification email."""
    data = request.get_json(silent=True) or {}
    email     = (data.get("email")     or "").strip().lower()
    password  =  data.get("password",  "")
    full_name = (data.get("full_name") or "").strip() or None

    errors = {}
    if not email or not _EMAIL_RE.match(email):
        errors["email"] = "A valid email address is required"
    if not password or len(password) < 12:
        errors["password"] = "Password must be at least 12 characters"
    if errors:
        return jsonify({"error": "Validation failed", "fields": errors}), 422

    if models.get_user_by_email(email):
        return jsonify({"error": "An account with that email already exists"}), 409

    try:
        password_hash = hash_password(password)
        user = models.create_user(email, password_hash, full_name)
    except Exception as exc:
        logger.error("register: create_user failed: %s", exc)
        return jsonify({"error": "Failed to create account"}), 500

    user_id = str(user["id"])

    # Store initial password in history
    try:
        models.store_password_in_history(user_id, password_hash)
    except Exception as exc:
        logger.error("register: store password history failed: %s", exc)

    # Create active subscription (no trial period)
    try:
        models.create_subscription(
            user_id=user_id,
            plan_id=1,
            status="active",
        )
    except Exception as exc:
        logger.error("register: create_subscription failed: %s", exc)

    # Generate + store verification token, send email asynchronously
    verify_token = secrets.token_urlsafe(32)
    verify_url   = None
    try:
        models.set_email_verify_token(user_id, verify_token)
        verify_url = _send_verification_email(email, verify_token)
        logger.info("Verification email dispatched for user %s", user_id)
    except Exception as exc:
        logger.error("register: send verification email failed: %s", exc)

    tokens = generate_tokens(user_id)

    models.log_audit(
        user_id=user_id,
        action="user_registered",
        details_dict={"email": email},
        ip_address=_get_client_ip(),
    )

    resp_body: dict = {
        "user_id": user_id,
        "email":   user["email"],
        "tokens":  tokens,
        "email_verification_sent": True,
    }
    # When SMTP is not configured, surface the verify URL so the client can
    # present it directly (useful during local development / testing).
    if not _smtp_configured() and verify_url:
        resp_body["verify_url"] = verify_url

    resp = jsonify(resp_body)
    # Set HttpOnly cookies so the user is immediately logged in after registration
    resp.set_cookie("access_token",  tokens["access_token"],
                    httponly=True, samesite="Lax", secure=False,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    resp.set_cookie("refresh_token", tokens["refresh_token"],
                    httponly=True, samesite="Lax", secure=False,
                    max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600)
    return resp, 201


@auth_bp.route("/login", methods=["POST"])
def login():
    """POST /auth/login — Authenticate and return tokens with account lockout protection."""
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    password =  data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 422

    ip = _get_client_ip()

    # Check if IP is rate limited (disabled - using database-based account lockout instead)
    # if not _check_rate_limit(ip or "unknown"):
    #     return jsonify({"error": "Too many login attempts. Please wait 15 minutes."}), 429

    # Check if account is locked due to failed attempts
    if models.is_account_locked(email, max_attempts=5, lockout_minutes=5):
        failed_count = models.get_failed_login_attempts(email, 5)
        return jsonify({
            "error": "Account temporarily locked due to too many failed login attempts. Please try again in 5 minutes.",
            "code": "account_locked",
            "failed_attempts": failed_count
        }), 429

    # Verify credentials
    user = models.get_user_by_email(email)
    if not user or not check_password(password, user["password_hash"]):
        # Record failed attempt
        models.record_login_attempt(email, ip or "unknown", success=False, user_id=str(user["id"]) if user else None)
        return jsonify({"error": "Invalid email or password"}), 401

    # Login successful - clear failed attempts
    models.clear_failed_login_attempts(email)
    models.record_login_attempt(email, ip or "unknown", success=True, user_id=str(user["id"]))
    models.update_user_last_login(str(user["id"]), ip)

    subscription = models.get_user_subscription(str(user["id"]))
    sub_info = None
    if subscription:
        sub_info = {
            "status":        subscription.get("status"),
            "plan_name":     subscription.get("plan_name"),
            "trial_ends_at": _serialize_dt(subscription.get("trial_ends_at")),
            "expires_at":    _serialize_dt(subscription.get("expires_at")),
        }

    # Check subscription status — block access for inactive/cancelled/pending accounts
    if subscription:
        sub_status = subscription.get("status", "").lower()
        if sub_status == "inactive":
            return jsonify({
                "error": "Your account is inactive. Please contact the helpdesk to reactivate it.",
                "code": "account_inactive"
            }), 403
        elif sub_status == "cancelled":
            return jsonify({
                "error": "Your subscription is cancelled. Please contact the helpdesk to renew it.",
                "code": "subscription_cancelled"
            }), 403
        elif sub_status == "pending_payment":
            return jsonify({
                "error": "Your account is pending payment. Please contact the helpdesk to complete your payment.",
                "code": "pending_payment"
            }), 403

    tokens = generate_tokens(str(user["id"]))

    models.log_audit(
        user_id=str(user["id"]),
        action="user_login",
        ip_address=ip,
    )

    resp = jsonify({
        "user_id":      str(user["id"]),
        "email":        user["email"],
        "subscription": sub_info,
        "tokens":       tokens,  # Keep in body for admin panel backward-compat
    })
    # Set HttpOnly cookies — more secure than localStorage
    resp.set_cookie("access_token",  tokens["access_token"],
                    httponly=True, samesite="Lax", secure=False,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    resp.set_cookie("refresh_token", tokens["refresh_token"],
                    httponly=True, samesite="Lax", secure=False,
                    max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600)
    return resp


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """POST /auth/logout — Clear cookies and confirm logout."""
    response = jsonify({"ok": True})
    response.delete_cookie("access_token",  path="/", samesite="Lax")
    response.delete_cookie("refresh_token", path="/", samesite="Lax")
    return response


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    """POST /auth/forgot-password — Send password reset link via email."""
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"error": "Email address is required"}), 422

    user = models.get_user_by_email(email)
    if not user:
        # For security, don't reveal whether email exists
        return jsonify({"message": "If an account exists, a reset link has been sent"}), 200

    # Generate a secure token valid for 24 hours
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=24)

    models.set_password_reset_token(str(user["id"]), reset_token, expires_at)

    # Build reset link
    reset_link = f"{_app_base_url()}/?reset_token={reset_token}&email={email}"

    # Send email in background
    def send_reset_email():
        try:
            _send_email(
                email,
                "Reset Your Password",
                f"""
                <h2>Password Reset Request</h2>
                <p>Click the link below to reset your password:</p>
                <p><a href="{reset_link}">Reset Password</a></p>
                <p>This link will expire in 24 hours.</p>
                <p>If you didn't request this, you can safely ignore this email.</p>
                """
            )
        except Exception as e:
            logger.error(f"Failed to send password reset email to {email}: {e}")

    threading.Thread(target=send_reset_email, daemon=True).start()

    # Always return success to prevent email enumeration attacks
    return jsonify({"message": "If an account exists, a reset link has been sent"}), 200


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    """POST /auth/reset-password — Reset password using reset token with policy enforcement."""
    data = request.get_json(silent=True) or {}
    reset_token = (data.get("reset_token") or "").strip()
    password = data.get("password", "")

    if not reset_token or not password:
        return jsonify({"error": "Reset token and password are required"}), 422

    # Validate password strength (minimum 12 characters)
    if len(password) < 12:
        return jsonify({"error": "Password must be at least 12 characters"}), 422

    # Validate token
    user = models.get_user_by_reset_token(reset_token)
    if not user:
        return jsonify({"error": "Invalid or expired reset link"}), 401

    # Check password history - user cannot reuse last 3 passwords
    old_password_hashes = models.get_password_history(str(user["id"]), limit=3)
    for old_hash in old_password_hashes:
        if check_password(password, old_hash):
            return jsonify({
                "error": "You cannot reuse one of your last 3 passwords. Please choose a different password."
            }), 422

    # Hash and update password
    password_hash = hash_password(password)

    # Store old password in history before updating
    models.store_password_in_history(str(user["id"]), user["password_hash"])

    with models.db_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (password_hash, str(user["id"])),
        )

    # Clear the reset token
    models.clear_password_reset_token(str(user["id"]))

    # Log the password change
    models.log_audit(
        user_id=str(user["id"]),
        action="password_reset",
    )

    return jsonify({"message": "Password updated successfully"}), 200


@auth_bp.route("/verify-reset-token", methods=["GET"])
def verify_reset_token():
    """GET /auth/verify-reset-token?token=... — Verify if a reset token is valid."""
    token = request.args.get("token", "").strip()

    if not token:
        return jsonify({"valid": False}), 200

    user = models.get_user_by_reset_token(token)
    return jsonify({"valid": user is not None}), 200


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    """POST /auth/refresh — Exchange a refresh token for a new access token."""
    data          = request.get_json(silent=True) or {}
    # Accept refresh token from either body or HttpOnly cookie
    refresh_token = data.get("refresh_token", "").strip() or request.cookies.get("refresh_token", "")

    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 422

    try:
        payload = decode_token(refresh_token)
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token has expired", "code": "token_expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid refresh token", "code": "invalid_token"}), 401

    if payload.get("type") != "refresh":
        return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

    user_id = payload.get("sub")
    if not models.get_user_by_id(user_id):
        return jsonify({"error": "User not found"}), 401

    now = datetime.now(tz=timezone.utc)
    new_access_token = jwt.encode(
        {"sub": user_id, "type": "access", "iat": now,
         "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        _jwt_secret(), algorithm=JWT_ALGORITHM,
    )
    resp = jsonify({"access_token": new_access_token})
    resp.set_cookie("access_token", new_access_token,
                    httponly=True, samesite="Lax", secure=False,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return resp


@auth_bp.route("/me", methods=["GET"])
@auth_required
def me():
    """GET /auth/me — Return current user profile, subscription, plan features, and admin flag."""
    user         = g.user
    subscription = models.get_user_subscription(g.user_id)
    is_admin     = models.is_admin(g.user_id)

    features = None
    sub_info = None
    if subscription:
        raw_features = subscription.get("features")
        if isinstance(raw_features, str):
            import json as _json
            raw_features = _json.loads(raw_features)
        features = raw_features
        sub_info = {
            "status":          subscription.get("status"),
            "plan_id":         subscription.get("plan_id"),
            "plan_name":       subscription.get("plan_name"),
            "display_name":    subscription.get("display_name"),
            "billing_cycle":   subscription.get("billing_cycle"),
            "started_at":      _serialize_dt(subscription.get("started_at")),
            "expires_at":      _serialize_dt(subscription.get("expires_at")),
            "trial_ends_at":   _serialize_dt(subscription.get("trial_ends_at")),
            "runs_per_day":    subscription.get("runs_per_day"),
            "max_ai_picks":    subscription.get("max_ai_picks"),
            "max_pdf_history": subscription.get("max_pdf_history"),
        }

    return jsonify({
        "user_id":        str(user["id"]),
        "email":          user["email"],
        "full_name":      user.get("full_name"),
        "email_verified": user.get("email_verified"),
        "is_admin":       is_admin,
        "created_at":     _serialize_dt(user.get("created_at")),
        "last_login_at":  _serialize_dt(user.get("last_login_at")),
        "subscription":   sub_info,
        "features":       features,
    })


@auth_bp.route("/verify-email", methods=["GET"])
def verify_email():
    """
    GET /auth/verify-email?token=<token>
    Called when user clicks the link in the verification email.
    Marks email as verified and redirects to the app.
    """
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"error": "Verification token is required"}), 422

    user = models.get_user_by_verify_token(token)
    if not user:
        # Token expired or already used — redirect with error param
        return redirect(f"{_app_base_url()}/?verify_error=1")

    if user.get("email_verified"):
        # Already verified — redirect with info
        return redirect(f"{_app_base_url()}/?already_verified=1")

    models.verify_user_email(str(user["id"]))
    models.log_audit(
        user_id=str(user["id"]),
        action="email_verified",
        ip_address=_get_client_ip(),
    )
    logger.info("Email verified for user %s", user["id"])

    # Redirect to app — frontend detects ?verified=1 and shows success toast
    return redirect(f"{_app_base_url()}/?verified=1")


@auth_bp.route("/resend-verification", methods=["POST"])
@auth_required
def resend_verification():
    """POST /auth/resend-verification — Resend the email verification link."""
    user = g.user

    if user.get("email_verified"):
        return jsonify({"message": "Your email is already verified."}), 200

    verify_token = secrets.token_urlsafe(32)
    models.set_email_verify_token(g.user_id, verify_token)
    verify_url = _send_verification_email(user["email"], verify_token)

    models.log_audit(
        user_id=g.user_id,
        action="verification_email_resent",
        ip_address=_get_client_ip(),
    )

    resp: dict = {"ok": True, "message": "Verification email sent. Please check your inbox."}
    if not _smtp_configured() and verify_url:
        resp["verify_url"] = verify_url

    return jsonify(resp), 200


@auth_bp.route("/profile", methods=["POST"])
@auth_required
def update_profile():
    """
    POST /auth/profile — Update mutable user profile fields.
    Body: { "full_name": "Jane Doe", "email": "new@example.com", "password": "newpass123" }
    """
    data      = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip() or None
    email     = (data.get("email") or "").strip().lower() or None
    new_pw    = data.get("password", "").strip() or None

    updates = {}

    if full_name:
        models.update_user_profile(g.user_id, full_name=full_name)
        updates["full_name"] = full_name

    if email:
        # Validate email format
        if not _EMAIL_RE.match(email):
            return jsonify({"error": "Invalid email format"}), 422
        # Check if email already exists
        existing = models.get_user_by_email(email)
        if existing and str(existing["id"]) != g.user_id:
            return jsonify({"error": "Email already in use"}), 409
        models.update_user_profile(g.user_id, email=email)
        updates["email"] = email

    if new_pw:
        if len(new_pw) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 422
        pw_hash = hash_password(new_pw)
        with models.db_cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (pw_hash, g.user_id),
            )
        updates["password"] = "***"  # Don't send actual password back

    if updates:
        models.log_audit(
            user_id=g.user_id,
            action="profile_updated",
            details_dict=updates,
        )

    # Return updated user object
    user = models.get_user_by_id(g.user_id)
    return jsonify({
        "ok":       True,
        "full_name": user.get("full_name") if user else full_name,
        "email":    user.get("email") if user else email,
    })


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _serialize_dt(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
