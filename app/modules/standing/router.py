"""
Standing orders API (recurring / schedule orders). Logic: standing/service.py

Staff (Customer page -> Standing orders):
  GET    /standing-orders?customer_id=          list (agli deliveries ke saath)
  POST   /standing-orders                       {customer_id, name?, delivery_weekday, generate_weekday, generate_time,
                                                 every_weeks, start_date?, end_date?, po_number?, notes?, is_active, lines}
  PUT    /standing-orders/{id}
  DELETE /standing-orders/{id}                  (bane hue orders rehte hain)
  POST   /standing-orders/{id}/skip             {date, skip: true|false}
  POST   /standing-orders/{id}/create-now       agli delivery ka order abhi bana do (Needs review)

Customer portal (login; Settings -> Customer portal -> "Customers can set up standing orders"):
  GET/POST /portal/{slug}/standing-orders    PUT/DELETE /portal/{slug}/standing-orders/{id}
  POST     /portal/{slug}/standing-orders/{id}/skip
  GET      /portal/{slug}/standing-orders/products   picker ke liye saare products (customer price ke saath)
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity
from app.models import Customer, StandingOrder, StandingOrderLine, User
from app.modules.portal.shop import Ctx, login_ctx, payment_options
from app.modules.standing import service
from app.modules.standing.service import StandingIn

router = APIRouter(tags=["standing-orders"])
staff_only = require_role("owner", "admin", "reviewer")


class StaffStandingIn(StandingIn):
    customer_id: int


class SkipIn(BaseModel):
    date: date
    skip: bool = True


def _load(db: Session):
    return select(StandingOrder).options(selectinload(StandingOrder.lines).selectinload(StandingOrderLine.product),
                                         selectinload(StandingOrder.customer))


def _get(db: Session, tenant_id: int, so_id: int, customer_id: int | None = None) -> StandingOrder:
    so = db.scalars(_load(db).where(StandingOrder.id == so_id)).first()
    if not so or so.tenant_id != tenant_id or (customer_id is not None and so.customer_id != customer_id):
        raise HTTPException(404, "Standing order not found")
    return so


# ---------------- staff ----------------
@router.get("/standing-orders")
def list_standing(customer_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = _load(db).where(StandingOrder.tenant_id == user.tenant_id)
    if customer_id:
        stmt = stmt.where(StandingOrder.customer_id == customer_id)
    now = service.local_now(user.tenant)
    rows = db.scalars(stmt.order_by(StandingOrder.is_active.desc(), StandingOrder.delivery_weekday, StandingOrder.id))
    return [service.standing_out(db, so, now) for so in rows]


@router.post("/standing-orders")
def create_standing(data: StaffStandingIn, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    customer = db.get(Customer, data.customer_id)
    if not customer or customer.tenant_id != user.tenant_id or customer.deleted_at:
        raise HTTPException(400, "Choose a customer")
    so = StandingOrder(tenant_id=user.tenant_id, customer_id=customer.id, created_by="staff", created_by_id=user.id)
    so.customer = customer
    service.apply_input(db, so, data, user.tenant)
    db.add(so)
    db.flush()
    log_activity(db, user.tenant_id, user.id, "standing.created", "standing_order", so.id,
                 {"name": so.name, "customer": customer.name, "schedule": service.schedule_text(so)})
    db.commit()
    return service.standing_out(db, _get(db, user.tenant_id, so.id), service.local_now(user.tenant))


@router.put("/standing-orders/{so_id}")
def update_standing(so_id: int, data: StandingIn, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    so = _get(db, user.tenant_id, so_id)
    service.apply_input(db, so, data, user.tenant)
    log_activity(db, user.tenant_id, user.id, "standing.updated", "standing_order", so.id,
                 {"name": so.name, "schedule": service.schedule_text(so), "active": so.is_active})
    db.commit()
    return service.standing_out(db, _get(db, user.tenant_id, so.id), service.local_now(user.tenant))


@router.delete("/standing-orders/{so_id}")
def delete_standing(so_id: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    so = _get(db, user.tenant_id, so_id)
    log_activity(db, user.tenant_id, user.id, "standing.deleted", "standing_order", so.id, {"name": so.name})
    db.delete(so)
    db.commit()
    return {"ok": True}


@router.post("/standing-orders/{so_id}/skip")
def skip_standing(so_id: int, data: SkipIn, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    so = _get(db, user.tenant_id, so_id)
    service.set_skip(so, data.date, data.skip)
    log_activity(db, user.tenant_id, user.id, "standing.skipped_date" if data.skip else "standing.unskipped_date",
                 "standing_order", so.id, {"name": so.name, "delivery_date": data.date.isoformat()})
    db.commit()
    return service.standing_out(db, so, service.local_now(user.tenant))


@router.post("/standing-orders/{so_id}/create-now")
def create_now(so_id: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    so = _get(db, user.tenant_id, so_id)
    d = service.next_delivery_to_create(so, service.local_now(user.tenant))
    if not d:
        raise HTTPException(400, "There's no upcoming delivery left to create")
    order = service.create_order(db, user.tenant, so, d, user.id)
    if not order:
        raise HTTPException(400, "The order couldn't be created - see the notification bell for the reason")
    return {"order_id": order.id, "delivery_date": d}


# ---------------- customer portal ----------------
def _portal_ctx(ctx: Ctx = Depends(login_ctx)) -> Ctx:
    if not ctx.cfg.portal_allow_standing:
        raise HTTPException(403, "Standing orders aren't available - please contact us")
    return ctx


def _check_payment(ctx: Ctx, data: StandingIn):
    keys = [p["key"] for p in payment_options(ctx.cfg)]
    if data.payment_method not in keys:
        data.payment_method = keys[0] if keys else None


@router.get("/portal/{slug}/standing-orders/products")
def portal_products(ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    from app.core.helpers import to_float
    from app.models import Product
    from app.modules.catalog.pricing import price_map
    from app.modules.portal.shop import _visible_products
    special = price_map(db, ctx.customer_id)
    rows = db.scalars(_visible_products(ctx).order_by(Product.name).limit(5000))
    return [{"id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit,
             "price": to_float(special.get(p.id, p.price))} for p in rows]


@router.get("/portal/{slug}/standing-orders")
def portal_list(ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    now = service.local_now(ctx.tenant)
    rows = db.scalars(_load(db).where(StandingOrder.customer_id == ctx.customer_id)
                      .order_by(StandingOrder.is_active.desc(), StandingOrder.delivery_weekday, StandingOrder.id))
    return [service.standing_out(db, so, now, with_prices=True) for so in rows]


@router.post("/portal/{slug}/standing-orders")
def portal_create(data: StandingIn, ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    _check_payment(ctx, data)
    so = StandingOrder(tenant_id=ctx.tenant.id, customer_id=ctx.customer_id, created_by="customer",
                       portal_login_id=ctx.login.id, created_by_id=ctx.impersonator_id)
    so.customer = ctx.login.customer
    service.apply_input(db, so, data, ctx.tenant, portal=True)
    db.add(so)
    db.flush()
    log_activity(db, ctx.tenant.id, ctx.impersonator_id, "standing.created", "standing_order", so.id,
                 {"name": so.name, "by": ctx.login.email, "schedule": service.schedule_text(so)})
    db.commit()
    return service.standing_out(db, _get(db, ctx.tenant.id, so.id), service.local_now(ctx.tenant), with_prices=True)


@router.put("/portal/{slug}/standing-orders/{so_id}")
def portal_update(so_id: int, data: StandingIn, ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    so = _get(db, ctx.tenant.id, so_id, ctx.customer_id)
    _check_payment(ctx, data)
    service.apply_input(db, so, data, ctx.tenant, portal=True)
    log_activity(db, ctx.tenant.id, ctx.impersonator_id, "standing.updated", "standing_order", so.id,
                 {"name": so.name, "by": ctx.login.email, "schedule": service.schedule_text(so), "active": so.is_active})
    db.commit()
    return service.standing_out(db, _get(db, ctx.tenant.id, so.id), service.local_now(ctx.tenant), with_prices=True)


@router.delete("/portal/{slug}/standing-orders/{so_id}")
def portal_delete(so_id: int, ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    so = _get(db, ctx.tenant.id, so_id, ctx.customer_id)
    log_activity(db, ctx.tenant.id, ctx.impersonator_id, "standing.deleted", "standing_order", so.id,
                 {"name": so.name, "by": ctx.login.email})
    db.delete(so)
    db.commit()
    return {"ok": True}


@router.post("/portal/{slug}/standing-orders/{so_id}/skip")
def portal_skip(so_id: int, data: SkipIn, ctx: Ctx = Depends(_portal_ctx), db: Session = Depends(get_db)):
    so = _get(db, ctx.tenant.id, so_id, ctx.customer_id)
    service.set_skip(so, data.date, data.skip)
    log_activity(db, ctx.tenant.id, ctx.impersonator_id, "standing.skipped_date" if data.skip else "standing.unskipped_date",
                 "standing_order", so.id, {"name": so.name, "delivery_date": data.date.isoformat(), "by": ctx.login.email})
    db.commit()
    return service.standing_out(db, so, service.local_now(ctx.tenant), with_prices=True)
