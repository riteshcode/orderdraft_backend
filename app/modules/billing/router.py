"""
Billing page: plans, usage, Stripe checkout / portal, Stripe webhook.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import to_float
from app.models import Plan, Tenant, User
from app.modules.billing import stripe_service
from app.modules.billing.limits import usage_summary

router = APIRouter(tags=["billing"])
owner_only = require_role("owner")
log = logging.getLogger("billing")


class CheckoutIn(BaseModel):
    plan_code: str


def plan_out(p: Plan) -> dict:
    return {"id": p.id, "code": p.code, "name": p.name, "price_aud_month": to_float(p.price_aud_month),
            "order_limit": p.order_limit, "user_limit": p.user_limit, "features": p.features or [],
            "has_stripe_price": bool(p.stripe_price_id), "stripe_price_id": p.stripe_price_id,
            "is_active": p.is_active, "sort_order": p.sort_order}


@router.get("/public/plans")
def public_plans(db: Session = Depends(get_db)):
    """Homepage pricing section ke liye - login nahi chahiye. Stripe ids nahi bhejte."""
    plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id))
    return {
        "trial_days": settings.TRIAL_DAYS,
        "plans": [{"code": p.code, "name": p.name, "price_aud_month": to_float(p.price_aud_month),
                   "order_limit": p.order_limit, "user_limit": p.user_limit, "features": p.features or []}
                  for p in plans],
    }


@router.get("/billing")
def billing(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id))
    return {
        "plans": [plan_out(p) for p in plans],
        "usage": usage_summary(db, tenant),
        "has_subscription": bool(tenant.stripe_subscription_id),
        "stripe_enabled": bool(settings.STRIPE_SECRET_KEY),
    }


@router.post("/billing/checkout")
def checkout(data: CheckoutIn, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    plan = db.scalars(select(Plan).where(Plan.code == data.plan_code, Plan.is_active.is_(True))).first()
    if not plan:
        raise HTTPException(404, "Plan not found")
    try:
        return {"url": stripe_service.checkout_url(db, db.get(Tenant, user.tenant_id), user, plan)}
    except stripe_service.BillingError as e:
        raise HTTPException(400, str(e))


@router.post("/billing/portal")
def portal(user: User = Depends(owner_only), db: Session = Depends(get_db)):
    try:
        return {"url": stripe_service.portal_url(db, db.get(Tenant, user.tenant_id), user)}
    except stripe_service.BillingError as e:
        raise HTTPException(400, str(e))


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    try:
        event = stripe_service.verify_event(payload, request.headers.get("stripe-signature", ""))
    except Exception as e:
        log.warning("Stripe webhook rejected: %s", e)
        return Response(status_code=400)
    stripe_service.handle_event(db, event)
    return {"ok": True}
