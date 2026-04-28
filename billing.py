"""
billing.py - Stripe integration module for the Stock Screening SaaS application.
Secrets from env vars: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
                       STRIPE_PUBLISHABLE_KEY, APP_BASE_URL.
Price ID env vars: STRIPE_PRICE_<PLAN>_<CYCLE>  e.g. STRIPE_PRICE_STARTER_MONTHLY
"""

import subprocess
import sys

# Auto-install stripe if not present
try:
    import stripe
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "stripe"])
    import stripe

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from flask import Blueprint, g, jsonify, request, redirect

from auth import auth_required
import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stripe configuration
# ---------------------------------------------------------------------------

def _stripe_secret() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY environment variable is not set")
    return key


def _webhook_secret() -> str:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET environment variable is not set")
    return secret


def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")


# ---------------------------------------------------------------------------
# Price ID lookup
# ---------------------------------------------------------------------------

# Environment variable naming convention:
#   STRIPE_PRICE_STARTER_MONTHLY, STRIPE_PRICE_STARTER_YEARLY
#   STRIPE_PRICE_PRO_MONTHLY,     STRIPE_PRICE_PRO_YEARLY
#   STRIPE_PRICE_ANALYST_MONTHLY, STRIPE_PRICE_ANALYST_YEARLY
#   STRIPE_PRICE_ENTERPRISE_MONTHLY, STRIPE_PRICE_ENTERPRISE_YEARLY

def _get_price_id(plan_name: str, billing_cycle: str) -> Optional[str]:
    """
    Look up the Stripe Price ID from environment variables.
    Returns None if the env var is not set.
    """
    env_key = f"STRIPE_PRICE_{plan_name.upper()}_{billing_cycle.upper()}"
    return os.environ.get(env_key)


# Plan name → DB plan_id mapping (matches seeded plans)
_PLAN_NAME_TO_ID = {
    "starter": 2,
    "pro": 3,
    "advanced": 6,
    "analyst": 4,
    "enterprise": 5,
}


# ---------------------------------------------------------------------------
# Stripe helpers
# ---------------------------------------------------------------------------

def create_checkout_session(user_id: str, plan_name: str, billing_cycle: str) -> str:
    """
    Create a Stripe Checkout Session for the given plan and billing cycle.
    Returns the session URL.
    Raises ValueError for invalid plan/cycle, RuntimeError for Stripe errors.
    """
    plan_name = plan_name.lower()
    billing_cycle = billing_cycle.lower()

    if plan_name not in _PLAN_NAME_TO_ID:
        raise ValueError(f"Unknown plan: '{plan_name}'")
    if billing_cycle not in ("monthly", "yearly"):
        raise ValueError(f"billing_cycle must be 'monthly' or 'yearly', got '{billing_cycle}'")

    price_id = _get_price_id(plan_name, billing_cycle)
    if not price_id:
        raise RuntimeError(
            f"No Stripe price ID configured for {plan_name}/{billing_cycle}. "
            f"Set STRIPE_PRICE_{plan_name.upper()}_{billing_cycle.upper()} env var."
        )

    stripe.api_key = _stripe_secret()
    base_url = _app_base_url()

    # Retrieve or create Stripe customer ID from subscription record
    subscription = models.get_user_subscription(user_id)
    customer_id = subscription.get("stripe_customer_id") if subscription else None

    session_kwargs = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/billing/cancel",
        "client_reference_id": user_id,
        "metadata": {
            "user_id": user_id,
            "plan_name": plan_name,
            "billing_cycle": billing_cycle,
        },
        "subscription_data": {
            "metadata": {
                "user_id": user_id,
                "plan_name": plan_name,
            }
        },
        "allow_promotion_codes": True,
    }
    if customer_id:
        session_kwargs["customer"] = customer_id
    else:
        user = models.get_user_by_id(user_id)
        if user:
            session_kwargs["customer_email"] = user["email"]

    try:
        session = stripe.checkout.Session.create(**session_kwargs)
        return session.url
    except stripe.error.StripeError as exc:
        logger.error("create_checkout_session failed: %s", exc)
        raise RuntimeError(f"Stripe error: {exc.user_message or str(exc)}")


def create_customer_portal_session(stripe_customer_id: str) -> str:
    """
    Create a Stripe Customer Portal session for the given customer.
    Returns the portal URL.
    """
    stripe.api_key = _stripe_secret()
    base_url = _app_base_url()

    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=f"{base_url}/?portal_return=1",
        )
        return session.url
    except stripe.error.StripeError as exc:
        logger.error("create_customer_portal_session failed: %s", exc)
        raise RuntimeError(f"Stripe error: {exc.user_message or str(exc)}")


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

def handle_webhook(payload_bytes: bytes, sig_header: str) -> Tuple[bool, str]:
    """
    Verify Stripe webhook signature and process the event.
    Returns (True, "ok") on success, (False, error_message) on failure.
    """
    stripe.api_key = _stripe_secret()
    try:
        event = stripe.Webhook.construct_event(
            payload_bytes, sig_header, _webhook_secret()
        )
    except ValueError:
        return False, "Invalid payload"
    except stripe.error.SignatureVerificationError:
        return False, "Invalid signature"

    event_type = event["type"]
    data_object = event["data"]["object"]
    logger.info("Stripe webhook received: %s", event_type)

    try:
        if event_type == "checkout.session.completed":
            _handle_checkout_completed(data_object)

        elif event_type == "invoice.paid":
            _handle_invoice_paid(data_object)

        elif event_type == "invoice.payment_failed":
            _handle_invoice_payment_failed(data_object)

        elif event_type == "customer.subscription.deleted":
            _handle_subscription_deleted(data_object)

        else:
            logger.debug("Unhandled Stripe event type: %s", event_type)

    except Exception as exc:
        logger.error("Webhook handler error for %s: %s", event_type, exc, exc_info=True)
        return False, str(exc)

    return True, "ok"


def _handle_checkout_completed(session):
    """checkout.session.completed → store stripe_customer_id, set status=pending_payment."""
    metadata_raw = getattr(session, "metadata", None)
    metadata = metadata_raw.to_dict() if (metadata_raw and hasattr(metadata_raw, "to_dict")) else (metadata_raw or {})
    user_id = getattr(session, "client_reference_id", None) or metadata.get("user_id")
    if not user_id:
        logger.warning("checkout.session.completed: no user_id in session metadata")
        return

    # Fix #7: validate user actually exists before running UPDATE
    user = models.get_user_by_id(user_id)
    if not user:
        logger.error(
            "checkout.session.completed: user_id=%s not found in DB — skipping update",
            user_id,
        )
        return

    customer_raw = getattr(session, "customer", None)
    customer_id = getattr(customer_raw, "id", None) or (
        customer_raw if isinstance(customer_raw, str) else None
    )
    sub_raw = getattr(session, "subscription", None)
    stripe_sub_id = getattr(sub_raw, "id", None) or (
        sub_raw if isinstance(sub_raw, str) else None
    )

    with models.db_cursor() as cur:
        cur.execute(
            """
            UPDATE subscriptions
            SET stripe_customer_id = %s,
                stripe_sub_id = %s,
                status = 'pending_payment'
            WHERE user_id = %s
            """,
            (customer_id, stripe_sub_id, user_id),
        )

    models.log_audit(
        user_id=user_id,
        action="checkout_completed",
        details_dict={
            "stripe_customer_id": customer_id,
            "stripe_sub_id": stripe_sub_id,
        },
    )
    logger.info("checkout.session.completed processed for user_id=%s", user_id)


def _handle_invoice_paid(invoice):
    """invoice.paid → set subscription active, update expires_at."""
    customer_id = invoice.get("customer")
    stripe_sub_id = invoice.get("subscription")

    if not customer_id:
        logger.warning("invoice.paid: missing customer ID")
        return

    # Determine billing period end from the invoice
    period_end = None
    lines = invoice.get("lines", {}).get("data", [])
    if lines:
        period_end_ts = lines[0].get("period", {}).get("end")
        if period_end_ts:
            period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)

    with models.db_cursor() as cur:
        # Look up user by stripe_customer_id
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_customer_id = %s",
            (customer_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("invoice.paid: no subscription for customer_id=%s", customer_id)
            return

        user_id = str(row["user_id"])

        cur.execute(
            """
            UPDATE subscriptions
            SET status = 'active',
                expires_at = %s,
                stripe_sub_id = COALESCE(%s, stripe_sub_id),
                activated_at = NOW()
            WHERE stripe_customer_id = %s
            """,
            (period_end, stripe_sub_id, customer_id),
        )

    models.log_audit(
        user_id=user_id,
        action="invoice_paid",
        details_dict={"stripe_customer_id": customer_id, "period_end": str(period_end)},
    )
    logger.info("invoice.paid: subscription activated for customer_id=%s", customer_id)


def _handle_invoice_payment_failed(invoice):
    """invoice.payment_failed → set subscription inactive."""
    customer_id = invoice.get("customer")
    if not customer_id:
        return

    with models.db_cursor() as cur:
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_customer_id = %s",
            (customer_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("invoice.payment_failed: no subscription for customer_id=%s", customer_id)
            return
        user_id = str(row["user_id"])
        cur.execute(
            "UPDATE subscriptions SET status = 'inactive' WHERE stripe_customer_id = %s",
            (customer_id,),
        )

    models.log_audit(
        user_id=user_id,
        action="invoice_payment_failed",
        details_dict={"stripe_customer_id": customer_id},
    )
    logger.info("invoice.payment_failed: subscription deactivated for customer_id=%s", customer_id)


def _handle_subscription_deleted(subscription_obj):
    """customer.subscription.deleted → set status=cancelled."""
    customer_id = subscription_obj.get("customer")
    stripe_sub_id = subscription_obj.get("id")

    if not customer_id:
        return

    with models.db_cursor() as cur:
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_customer_id = %s",
            (customer_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("subscription.deleted: no subscription for customer_id=%s", customer_id)
            return
        user_id = str(row["user_id"])
        cur.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE stripe_customer_id = %s",
            (customer_id,),
        )

    models.log_audit(
        user_id=user_id,
        action="subscription_cancelled",
        details_dict={"stripe_customer_id": customer_id, "stripe_sub_id": stripe_sub_id},
    )
    logger.info("subscription.deleted: subscription cancelled for customer_id=%s", customer_id)


# ---------------------------------------------------------------------------
# Blueprint routes
# ---------------------------------------------------------------------------

billing_bp = Blueprint("billing_bp", __name__, url_prefix="/billing")


@billing_bp.route("/checkout", methods=["GET"])
@auth_required
def checkout():
    """
    GET /billing/checkout?plan=starter&cycle=monthly
    Returns {"checkout_url": "https://checkout.stripe.com/..."}
    """
    plan_name = request.args.get("plan", "").strip().lower()
    billing_cycle = request.args.get("cycle", "").strip().lower()

    if not plan_name:
        return jsonify({"error": "Query parameter 'plan' is required"}), 422
    if billing_cycle not in ("monthly", "yearly"):
        return jsonify({"error": "Query parameter 'cycle' must be 'monthly' or 'yearly'"}), 422

    try:
        checkout_url = create_checkout_session(g.user_id, plan_name, billing_cycle)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        logger.error("checkout route error: %s", exc)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"checkout_url": checkout_url})


@billing_bp.route("/webhook", methods=["POST"])
def webhook():
    """
    POST /billing/webhook
    Stripe webhook endpoint — no authentication required.
    Stripe signs the request; we verify the signature inside handle_webhook().
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not sig_header:
        return jsonify({"error": "Missing Stripe-Signature header"}), 400

    ok, message = handle_webhook(payload, sig_header)
    if not ok:
        logger.warning("Webhook rejected: %s", message)
        return jsonify({"error": message}), 400

    return jsonify({"ok": True})


@billing_bp.route("/portal", methods=["GET"])
@auth_required
def portal():
    """
    GET /billing/portal
    Returns {"portal_url": "https://billing.stripe.com/..."}
    """
    subscription = models.get_user_subscription(g.user_id)
    if not subscription:
        return jsonify({"error": "No subscription found"}), 404

    customer_id = subscription.get("stripe_customer_id")
    if not customer_id:
        return jsonify({"error": "No Stripe customer on file. Please contact support."}), 404

    try:
        portal_url = create_customer_portal_session(customer_id)
    except RuntimeError as exc:
        logger.error("portal route error: %s", exc)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"portal_url": portal_url})


@billing_bp.route("/success", methods=["GET"])
def success():
    """
    GET /billing/success?session_id=<session_id>
    Stripe checkout success redirect. Retrieves session details and updates subscription if needed.
    """
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        logger.warning("success: missing session_id parameter")
        return redirect("/")

    try:
        stripe.api_key = _stripe_secret()

        # Retrieve the checkout session (this should work for completed sessions)
        try:
            session = stripe.checkout.Session.retrieve(
                session_id,
                expand=['customer', 'subscription']
            )
        except Exception as retrieve_err:
            logger.warning(
                "success: Could not retrieve session %s (may not be available yet): %s. "
                "Relying on webhook to process payment.",
                session_id, retrieve_err
            )
            # Even if we can't retrieve it now, redirect. Webhook will process it.
            return redirect("/")

        # Stripe SDK v5+ uses attribute access, not .get()
        # Use getattr with fallback to safely read fields
        user_id = (
            getattr(session, "client_reference_id", None)
            or (getattr(session, "metadata", None) or {}).get("user_id")
        )
        if not user_id:
            logger.warning("success: session %s missing user_id", session_id)
            return redirect("/")

        # customer/subscription may be expanded objects; extract the ID string
        customer_raw = getattr(session, "customer", None)
        customer_id = getattr(customer_raw, "id", None) or (
            customer_raw if isinstance(customer_raw, str) else None
        )

        sub_raw = getattr(session, "subscription", None)
        stripe_sub_id = getattr(sub_raw, "id", None) or (
            sub_raw if isinstance(sub_raw, str) else None
        )

        # metadata is a StripeObject in SDK v15 — convert to plain dict
        metadata_raw = getattr(session, "metadata", None)
        if metadata_raw is not None and hasattr(metadata_raw, "to_dict"):
            metadata = metadata_raw.to_dict()
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            metadata = {}
        plan_name = metadata.get("plan_name", "").lower()

        # Look up plan_id from plan_name
        plan_id = _PLAN_NAME_TO_ID.get(plan_name)

        logger.info(
            "success: session=%s user=%s plan=%s (id=%s) customer=%s sub=%s",
            session_id, user_id, plan_name, plan_id, customer_id, stripe_sub_id
        )

        # Build UPDATE statement
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        update_fields = [
            "stripe_customer_id = COALESCE(%s, stripe_customer_id)",
            "stripe_sub_id = COALESCE(%s, stripe_sub_id)",
            "status = 'active'",
            "activated_at = %s",
        ]
        update_values = [customer_id, stripe_sub_id, now_utc]

        if plan_id:
            update_fields.append("plan_id = %s")
            update_values.append(plan_id)

        update_values.append(user_id)

        with models.db_cursor() as cur:
            cur.execute(
                f"UPDATE subscriptions SET {', '.join(update_fields)} WHERE user_id = %s",
                update_values,
            )

        logger.info(
            "success: subscription updated for user %s — plan: %s, status: active",
            user_id, plan_name,
        )

    except Exception as exc:
        logger.error("success: Error processing session %s: %s", session_id, exc, exc_info=True)

    # Redirect back to app — the ?payment_success=1 param triggers a toast and
    # forces the account/billing page to display so the user sees their new plan.
    return redirect("/?payment_success=1")


@billing_bp.route("/cancel", methods=["GET"])
def cancel():
    """
    GET /billing/cancel
    Stripe checkout cancel redirect. Redirects user back to the dashboard.
    """
    return redirect("/?payment_cancelled=1")


# Destination for all enterprise sales inquiries
_ENTERPRISE_INQUIRY_TO = "enroll@terminalelearn.com"


@billing_bp.route("/enterprise-inquiry", methods=["POST"])
def enterprise_inquiry():
    """
    POST /billing/enterprise-inquiry
    Submits an Enterprise Sales inquiry form and emails it to the sales address.
    Public endpoint — accessible by both authenticated and unauthenticated users.
    Body (JSON): { name, company, email, message }
    """
    from auth import _send_email, _smtp_configured

    body    = request.get_json(silent=True) or {}
    name    = str(body.get("name",    "")).strip()
    company = str(body.get("company", "")).strip()
    message = str(body.get("message", "")).strip()

    if not message:
        return jsonify({"error": "Please enter a message before sending."}), 400

    # Try to get email: first from form body, then from auth context if logged in
    form_email = str(body.get("email", "")).strip()
    auth_user  = getattr(g, "user", None) or {}
    user_email = form_email or auth_user.get("email", "") or ""
    user_id    = getattr(g, "user_id", "anonymous")

    subject = "Enterprise Sales Inquiry"
    if name or user_email:
        label = name or user_email
        subject = f"Enterprise Sales Inquiry — {label}"
        if company:
            subject = f"Enterprise Sales Inquiry — {label} ({company})"

    html_body = f"""
<html><body style="font-family:sans-serif;color:#333;max-width:620px;margin:0 auto">
  <h2 style="color:#b45309">Enterprise Sales Inquiry</h2>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr style="background:#fafafa">
      <td style="padding:10px 14px;font-weight:600;width:160px;border:1px solid #e5e7eb">Name</td>
      <td style="padding:10px 14px;border:1px solid #e5e7eb">{name or "—"}</td>
    </tr>
    <tr>
      <td style="padding:10px 14px;font-weight:600;border:1px solid #e5e7eb">Company / Fund</td>
      <td style="padding:10px 14px;border:1px solid #e5e7eb">{company or "—"}</td>
    </tr>
    <tr style="background:#fafafa">
      <td style="padding:10px 14px;font-weight:600;border:1px solid #e5e7eb">Email</td>
      <td style="padding:10px 14px;border:1px solid #e5e7eb">{user_email or "—"}</td>
    </tr>
    <tr>
      <td style="padding:10px 14px;font-weight:600;vertical-align:top;border:1px solid #e5e7eb">Message</td>
      <td style="padding:10px 14px;white-space:pre-wrap;border:1px solid #e5e7eb">{message}</td>
    </tr>
  </table>
  <p style="margin-top:20px;font-size:12px;color:#999">
    Sent from Intelligent Investor SaaS — Enterprise inquiry form
  </p>
</body></html>
"""

    logger.info(
        "enterprise-inquiry: user=%s name=%s company=%s smtp_configured=%s",
        user_id, name, company, _smtp_configured(),
    )

    sent = _send_email(_ENTERPRISE_INQUIRY_TO, subject, html_body)
    if sent:
        logger.info("enterprise-inquiry: email delivered to %s", _ENTERPRISE_INQUIRY_TO)
        return jsonify({"ok": True})

    # SMTP not configured or send failed — log the full inquiry so it's not lost
    logger.warning(
        "enterprise-inquiry: email NOT sent (SMTP not configured or error). "
        "Inquiry details — user=%s name=%s company=%s message=%r",
        user_id, name, company, message,
    )
    # Still return OK to the user (we've logged it); let them know to follow up
    return jsonify({"ok": True, "fallback": True})


@billing_bp.route("/plans", methods=["GET"])
def list_plans():
    """
    GET /billing/plans
    Public endpoint — returns all active plans with pricing and features.
    """
    try:
        with models.db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT id, name, display_name, price_monthly, price_yearly,
                       runs_per_day, max_ai_picks, max_pdf_history, trial_days, features
                FROM plans
                WHERE is_active = TRUE
                ORDER BY id
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("list_plans: DB error: %s", exc)
        return jsonify({"error": "Failed to retrieve plans"}), 500

    # Enrich with publishable key so the frontend can render Stripe.js
    publishable_key = os.environ.get("STRIPE_PUBLISHABLE_KEY")

    plans_list = []
    for row in rows:
        plan = dict(row)
        # Ensure features is a dict not a string
        features = plan.get("features")
        if isinstance(features, str):
            plan["features"] = json.loads(features)
        # Attach price IDs (only non-None ones, so the frontend knows which are purchasable)
        price_ids = {}
        for cycle in ("monthly", "yearly"):
            pid = _get_price_id(plan["name"], cycle)
            if pid:
                price_ids[cycle] = pid
        plan["stripe_price_ids"] = price_ids
        # Convert Decimal → float for JSON serialisation
        for field in ("price_monthly", "price_yearly"):
            if plan.get(field) is not None:
                plan[field] = float(plan[field])
        plans_list.append(plan)

    return jsonify({
        "plans": plans_list,
        "stripe_publishable_key": publishable_key,
    })
