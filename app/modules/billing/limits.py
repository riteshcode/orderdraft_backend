"""
Plan limits aur usage.

Rule:
  - Trial ya active subscription = orders process honge
  - Monthly limit cross = warning, par HARD_LIMIT_PERCENT (default 120%) tak chalne do
    (client ke customer ka order kabhi achanak "gayab" na ho)
  - Uske baad / trial khatam / canceled = message save hota hai, status "blocked".
    Upgrade ke baad "Retry" se process ho jata hai.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import utcnow
from app.models import DraftOrder, Plan, Tenant, UsageEvent, User


def period_start(tenant: Tenant) -> datetime:
    """Billing period kab shuru hua. Stripe period na ho to calendar month."""
    now = utcnow()
    if tenant.current_period_start and tenant.current_period_start <= now:
        return tenant.current_period_start
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def orders_this_period(db: Session, tenant: Tenant) -> int:
    return db.scalar(
        select(func.count()).select_from(DraftOrder)
        .where(DraftOrder.tenant_id == tenant.id, DraftOrder.created_at >= period_start(tenant),
               DraftOrder.source != "manual")   # manual entry mein AI nahi lagta - plan limit mein nahi gina
    ) or 0


def get_plan(db: Session, tenant: Tenant) -> Plan | None:
    if tenant.plan_id:
        return db.get(Plan, tenant.plan_id)
    return db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)).first()


def subscription_ok(tenant: Tenant) -> tuple[bool, str]:
    if tenant.status == "suspended":
        return False, "Account suspended"
    status = tenant.subscription_status
    if status == "trialing":
        if tenant.trial_ends_at and tenant.trial_ends_at < utcnow():
            return False, "Trial ended - please choose a plan"
        return True, ""
    if status in ("active", "past_due"):   # past_due: Stripe retry kar raha hai, service mat roko
        return True, ""
    return False, "No active subscription - please choose a plan"


def usage_summary(db: Session, tenant: Tenant) -> dict:
    plan = get_plan(db, tenant)
    used = orders_this_period(db, tenant)
    limit = plan.order_limit if plan else 0
    users = db.scalar(select(func.count()).select_from(User).where(User.tenant_id == tenant.id, User.is_active.is_(True)))
    ok, reason = subscription_ok(tenant)
    return {
        "plan_code": plan.code if plan else None,
        "plan_name": plan.name if plan else None,
        "orders_used": used,
        "orders_limit": limit,
        "percent": round(used * 100 / limit, 1) if limit else 0,
        "users_used": users,
        "users_limit": plan.user_limit if plan else 1,
        "subscription_status": tenant.subscription_status,
        "trial_ends_at": tenant.trial_ends_at,
        "current_period_end": tenant.current_period_end,
        "can_process": ok and (not limit or used < limit * settings.HARD_LIMIT_PERCENT / 100),
        "blocked_reason": reason or (
            "" if not limit or used < limit * settings.HARD_LIMIT_PERCENT / 100 else "Monthly order limit reached - please upgrade"
        ),
    }


def can_process(db: Session, tenant: Tenant) -> tuple[bool, str]:
    s = usage_summary(db, tenant)
    return s["can_process"], s["blocked_reason"]


def record_ai_usage(db: Session, tenant_id: int, kind: str, input_tokens: int = 0,
                    output_tokens: int = 0, audio_seconds: float = 0):
    """AI ka kharcha estimate karke save karo (super admin dashboard)."""
    cost = (
        input_tokens * settings.CLAUDE_INPUT_COST_PER_M / 1_000_000
        + output_tokens * settings.CLAUDE_OUTPUT_COST_PER_M / 1_000_000
        + audio_seconds / 60 * settings.DEEPGRAM_COST_PER_MIN
    )
    db.add(UsageEvent(tenant_id=tenant_id, kind=kind, input_tokens=input_tokens,
                      output_tokens=output_tokens, audio_seconds=audio_seconds, cost_usd=round(cost, 6)))


def trial_end_date() -> datetime:
    return utcnow() + timedelta(days=settings.TRIAL_DAYS)
