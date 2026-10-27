"""
Stripe subscriptions.

Setup (Stripe dashboard):
  1. Har plan ka Product + monthly Price (AUD) banao -> price id (price_...) Super Admin -> Plans mein daalo
  2. Webhook endpoint: https://api.yourapp.com/webhooks/stripe
     Events: checkout.session.completed, customer.subscription.created,
             customer.subscription.updated, customer.subscription.deleted
  3. .env: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
  4. Customer portal settings enable karo (plan change / cancel / card update ke liye)
"""
from datetime import datetime, timedelta, timezone

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import tenant_session, utcnow
from app.core.helpers import log_activity
from app.models import Plan, Tenant, User

STATUS_MAP = {
    "trialing": "trialing", "active": "active", "past_due": "past_due",
    "canceled": "canceled", "unpaid": "canceled", "incomplete_expired": "canceled",
}


class BillingError(Exception):
    pass


def client() -> stripe.StripeClient:
    if not settings.STRIPE_SECRET_KEY:
        raise BillingError("Billing isn't set up yet (STRIPE_SECRET_KEY)")
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY)


def ensure_customer(db: Session, tenant: Tenant, user: User) -> str:
    if tenant.stripe_customer_id:
        return tenant.stripe_customer_id
    customer = client().v1.customers.create(params={
        "name": tenant.name, "email": tenant.email or user.email,
        "metadata": {"tenant_id": str(tenant.id), "slug": tenant.slug},
    })
    tenant.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def checkout_url(db: Session, tenant: Tenant, user: User, plan: Plan) -> str:
    if not plan.stripe_price_id:
        raise BillingError("This plan doesn't have a Stripe price set up yet")
    params = {
        "mode": "subscription",
        "customer": ensure_customer(db, tenant, user),
        "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
        "client_reference_id": str(tenant.id),
        "success_url": f"{settings.FRONTEND_URL}/settings/billing?checkout=success",
        "cancel_url": f"{settings.FRONTEND_URL}/settings/billing?checkout=cancel",
        "subscription_data": {"metadata": {"tenant_id": str(tenant.id), "plan_code": plan.code}},
        "allow_promotion_codes": True,
    }
    # App trial ke bache din Stripe mein bhi (Stripe ko kam se kam 2 din aage chahiye)
    if tenant.subscription_status == "trialing" and tenant.trial_ends_at and tenant.trial_ends_at > utcnow() + timedelta(days=2):
        params["subscription_data"]["trial_end"] = int(tenant.trial_ends_at.replace(tzinfo=timezone.utc).timestamp())
    session = client().v1.checkout.sessions.create(params=params)
    return session.url


def portal_url(db: Session, tenant: Tenant, user: User) -> str:
    session = client().v1.billing_portal.sessions.create(params={
        "customer": ensure_customer(db, tenant, user),
        "return_url": f"{settings.FRONTEND_URL}/settings/billing",
    })
    return session.url


def verify_event(payload: bytes, signature: str) -> dict:
    """Signature check. Galat = ValueError / SignatureVerificationError."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET set nahi hai")
    stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    import json
    return json.loads(payload)


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None) if value else None


def apply_subscription(db: Session, sub: dict) -> Tenant | None:
    """Stripe subscription object -> tenant ka plan / status / period."""
    tenant = None
    tenant_id = (sub.get("metadata") or {}).get("tenant_id")
    if tenant_id:
        tenant = db.get(Tenant, int(tenant_id))
    if not tenant and sub.get("customer"):
        tenant = db.scalars(select(Tenant).where(Tenant.stripe_customer_id == sub["customer"])).first()
    if not tenant:
        return None

    items = (sub.get("items") or {}).get("data") or []
    first_item = items[0] if items else {}
    price_id = (first_item.get("price") or {}).get("id")
    if price_id:
        plan = db.scalars(select(Plan).where(Plan.stripe_price_id == price_id)).first()
        if plan:
            tenant.plan_id = plan.id

    status = STATUS_MAP.get(sub.get("status"))
    if status:
        tenant.subscription_status = status
    tenant.stripe_subscription_id = sub.get("id")
    if sub.get("customer"):
        tenant.stripe_customer_id = sub["customer"]
    # Naye Stripe API versions mein period item level pe hota hai
    tenant.current_period_start = _ts(sub.get("current_period_start") or first_item.get("current_period_start"))
    tenant.current_period_end = _ts(sub.get("current_period_end") or first_item.get("current_period_end"))
    if sub.get("trial_end"):
        tenant.trial_ends_at = _ts(sub["trial_end"])

    db.commit()
    if tenant.db_ready:   # log client ke apne DB mein
        with tenant_session(tenant.id) as tdb:
            log_activity(tdb, tenant.id, None, "billing.subscription", detail={"status": sub.get("status"), "price": price_id})
            tdb.commit()
    return tenant


def handle_event(db: Session, event: dict):
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed" and obj.get("subscription"):
        tenant_id = obj.get("client_reference_id")
        if tenant_id:
            tenant = db.get(Tenant, int(tenant_id))
            if tenant:
                tenant.stripe_customer_id = obj.get("customer") or tenant.stripe_customer_id
                tenant.stripe_subscription_id = obj["subscription"]
                db.commit()
        # Poori subscription details Stripe se lao
        if settings.STRIPE_SECRET_KEY:
            sub = client().v1.subscriptions.retrieve(obj["subscription"])
            apply_subscription(db, sub.to_dict() if hasattr(sub, "to_dict") else dict(sub))

    elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        apply_subscription(db, obj)
