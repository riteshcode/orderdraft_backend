"""
Super Admin (platform owner) - saare tenants, plans, usage, AI cost, impersonate.
Super admin banane ke liye: .env SUPERADMIN_EMAILS=you@x.com  ya  python -m app.cli make-superadmin you@x.com
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db, tenant_session, utcnow
from app.core.deps import require_superadmin
from app.core.helpers import get_tenant_settings, log_activity, money
from app.models import Invoice, Plan, Tenant, UsageEvent, User
from app.modules.auth.router import login_response
from app.modules.billing.limits import usage_summary
from app.modules.billing.router import plan_out

router = APIRouter(prefix="/admin", tags=["admin"])


class TenantUpdate(BaseModel):
    status: str | None = None                 # active | suspended
    plan_id: int | None = None
    subscription_status: str | None = None    # manual override (pilot clients)
    trial_ends_at: datetime | None = None


class PlanIn(BaseModel):
    code: str
    name: str
    price_aud_month: float = 0
    stripe_price_id: str | None = None
    order_limit: int = 200
    user_limit: int = 3
    features: list[str] = []
    is_active: bool = True
    sort_order: int = 0


def _count_usage(db: Session, since, *conds) -> int:
    return db.scalar(select(func.count()).select_from(UsageEvent).where(UsageEvent.created_at >= since, *conds)) or 0


@router.get("/stats")
def platform_stats(admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=30)
    by_sub = dict(db.execute(select(Tenant.subscription_status, func.count()).group_by(Tenant.subscription_status)).all())
    mrr = db.scalar(select(func.coalesce(func.sum(Plan.price_aud_month), 0)).select_from(Tenant)
                    .join(Plan, Plan.id == Tenant.plan_id).where(Tenant.subscription_status == "active")) or 0
    ai_cost = db.scalar(select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(UsageEvent.created_at >= since))
    return {
        "tenants_total": db.scalar(select(func.count()).select_from(Tenant)),
        "tenants_by_subscription": by_sub,
        "users_total": db.scalar(select(func.count()).select_from(User)),
        "mrr_aud": round(float(mrr), 2),
        # Har client ka DB alag hai - counts main DB ke usage events se (pipeline record karti hai)
        "orders_30d": _count_usage(db, since, UsageEvent.kind == "order"),
        "messages_30d": _count_usage(db, since, UsageEvent.kind.like("message_%")),
        "failed_messages_30d": _count_usage(db, since, UsageEvent.kind == "message_failed"),
        "ai_cost_usd_30d": round(float(ai_cost or 0), 2),
        "new_tenants_30d": db.scalar(select(func.count()).select_from(Tenant).where(Tenant.created_at >= since)),
    }


@router.get("/tenants")
def list_tenants(q: str = "", status: str | None = None, page: int = Query(1, ge=1),
                 admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=30)
    stmt = select(Tenant)
    if q:
        stmt = stmt.where(or_(Tenant.name.ilike(f"%{q}%"), Tenant.slug.ilike(f"%{q}%"), Tenant.email.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(or_(Tenant.subscription_status == status, Tenant.status == status))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    tenants = list(db.scalars(stmt.order_by(Tenant.created_at.desc(), Tenant.id.desc()).offset((page - 1) * 50).limit(50)))
    ids = [t.id for t in tenants]

    orders = dict(db.execute(select(UsageEvent.tenant_id, func.count()).where(
        UsageEvent.tenant_id.in_(ids), UsageEvent.kind == "order", UsageEvent.created_at >= since)
        .group_by(UsageEvent.tenant_id)).all())
    cost = dict(db.execute(select(UsageEvent.tenant_id, func.sum(UsageEvent.cost_usd)).where(
        UsageEvent.tenant_id.in_(ids), UsageEvent.created_at >= since).group_by(UsageEvent.tenant_id)).all())
    users = dict(db.execute(select(User.tenant_id, func.count()).where(User.tenant_id.in_(ids)).group_by(User.tenant_id)).all())
    last_order = dict(db.execute(select(UsageEvent.tenant_id, func.max(UsageEvent.created_at)).where(
        UsageEvent.tenant_id.in_(ids), UsageEvent.kind == "order").group_by(UsageEvent.tenant_id)).all())
    plans = {p.id: p for p in db.scalars(select(Plan))}

    return {
        "total": total,
        "items": [{
            "id": t.id, "name": t.name, "slug": t.slug, "email": t.email, "status": t.status,
            "subscription_status": t.subscription_status, "trial_ends_at": t.trial_ends_at,
            "plan_name": plans[t.plan_id].name if t.plan_id in plans else None,
            "orders_30d": orders.get(t.id, 0), "ai_cost_usd_30d": round(float(cost.get(t.id) or 0), 3),
            "users": users.get(t.id, 0), "last_order_at": last_order.get(t.id), "created_at": t.created_at,
            "accounting_provider": t.accounting_provider, "db_name": t.db_name,
        } for t in tenants],
    }


@router.get("/tenants/{tenant_id}")
def tenant_detail(tenant_id: int, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    since = utcnow() - timedelta(days=30)
    members = db.scalars(select(User).where(User.tenant_id == t.id))
    cost_by_kind = dict(db.execute(select(UsageEvent.kind, func.sum(UsageEvent.cost_usd)).where(
        UsageEvent.tenant_id == t.id, UsageEvent.created_at >= since).group_by(UsageEvent.kind)).all())
    # Client ke apne DB se (alag session - admin ka session apne tenant se bound hai)
    usage, invoices, abn = None, 0, None
    if t.db_ready:
        with tenant_session(t.id) as tdb:
            usage = usage_summary(tdb, t)
            invoices = tdb.scalar(select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id))
            abn = get_tenant_settings(tdb, t.id).abn
            tdb.commit()
    return {
        "id": t.id, "name": t.name, "slug": t.slug, "email": t.email, "abn": abn, "status": t.status,
        "db_name": t.db_name, "db_ready": t.db_ready, "accounting_provider": t.accounting_provider,
        "plan_id": t.plan_id, "subscription_status": t.subscription_status, "trial_ends_at": t.trial_ends_at,
        "current_period_end": t.current_period_end, "stripe_customer_id": t.stripe_customer_id,
        "stripe_subscription_id": t.stripe_subscription_id, "created_at": t.created_at,
        "usage": usage,
        "ai_cost_by_kind_30d": {k: round(float(v or 0), 4) for k, v in cost_by_kind.items()},
        "invoices_total": invoices,
        "users": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "is_active": u.is_active,
                   "last_login_at": u.last_login_at} for u in members],
    }


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: int, data: TenantUpdate, admin: User = Depends(require_superadmin),
                  db: Session = Depends(get_db)):
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    if data.status and data.status not in ("active", "suspended"):
        raise HTTPException(400, "Invalid status")
    if data.plan_id and not db.get(Plan, data.plan_id):
        raise HTTPException(400, "Invalid plan")
    if data.subscription_status and data.subscription_status not in ("trialing", "active", "past_due", "canceled", "expired"):
        raise HTTPException(400, "Invalid subscription status")
    changes = data.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(t, k, v)
    db.commit()
    _log_in_tenant(t.id, admin.id, "admin.tenant_updated", {k: str(v) for k, v in changes.items()})
    return tenant_detail(tenant_id, admin, db)


@router.post("/tenants/{tenant_id}/impersonate")
def impersonate(tenant_id: int, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    """Support ke liye client ke owner ki tarah login (2 ghante ka token, banner dikhta hai)."""
    owner = db.scalars(select(User).where(User.tenant_id == tenant_id, User.role == "owner",
                                          User.is_active.is_(True)).order_by(User.id)).first()
    if not owner:
        raise HTTPException(404, "This tenant has no active owner")
    _log_in_tenant(tenant_id, admin.id, "admin.impersonated", {"admin": admin.email})
    # Naya session: admin wala session admin ke apne client DB se bound hai
    with SessionLocal() as s:
        return login_response(s, s.get(User, owner.id), impersonator_id=admin.id)


def _log_in_tenant(tenant_id: int, user_id: int, action: str, detail: dict):
    """Admin action us client ke apne DB ke activity log mein."""
    t_ready = True
    with SessionLocal() as s:
        t = s.get(Tenant, tenant_id)
        t_ready = bool(t and t.db_ready)
    if not t_ready:
        return
    with tenant_session(tenant_id) as tdb:
        log_activity(tdb, tenant_id, user_id, action, detail=detail)
        tdb.commit()


# ---------------- Plans ----------------
@router.get("/plans")
def list_plans(admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    return [plan_out(p) for p in db.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))]


def _apply_plan(p: Plan, data: PlanIn):
    p.code, p.name = data.code.strip().lower(), data.name.strip()
    p.price_aud_month = money(data.price_aud_month)
    p.stripe_price_id = (data.stripe_price_id or "").strip() or None
    p.order_limit, p.user_limit = data.order_limit, data.user_limit
    p.features, p.is_active, p.sort_order = data.features, data.is_active, data.sort_order


@router.post("/plans")
def create_plan(data: PlanIn, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    if db.scalars(select(Plan).where(Plan.code == data.code.strip().lower())).first():
        raise HTTPException(400, "Plan code already exists")
    p = Plan()
    _apply_plan(p, data)
    db.add(p)
    db.commit()
    return plan_out(p)


@router.put("/plans/{plan_id}")
def update_plan(plan_id: int, data: PlanIn, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    p = db.get(Plan, plan_id)
    if not p:
        raise HTTPException(404, "Plan not found")
    _apply_plan(p, data)
    db.commit()
    return plan_out(p)
