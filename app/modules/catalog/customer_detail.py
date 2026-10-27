"""
Customer detail page ke APIs.

Finance:
  GET    /customers/{id}/finance         orders, sales, invoices, outstanding, overdue (aging), monthly sales, top products

Pantry (customer-wise products + special price):
  GET    /customers/{id}/pantry
  POST   /customers/{id}/pantry          {product_id, price?, starts_on?, expires_on?, note?}   (add ya update)
  POST   /customers/{id}/pantry/bulk     {product_ids: [...]}   ya  {from_history: true}
  PUT    /customers/{id}/pantry/{item_id}
  DELETE /customers/{id}/pantry/{item_id}
"""
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity, money, to_float
from app.models import Customer, CustomerPrice, CustomerProductStat, DraftOrder, Invoice, Product, User
from app.modules.catalog.images import primary_thumb
from app.modules.catalog.pricing import price_status

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")

SOLD = ("approved", "exported")
OPEN_INVOICE = ("authorised", "sent")       # draft = abhi customer ko gaya nahi; paid / void = band


class PantryIn(BaseModel):
    product_id: int | None = None
    price: float | None = Field(None, ge=0)
    starts_on: date | None = None
    expires_on: date | None = None
    note: str | None = None


class BulkIn(BaseModel):
    product_ids: list[int] = []
    from_history: bool = False


def _customer(db: Session, user: User, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if not c or c.tenant_id != user.tenant_id:
        raise HTTPException(404, "Customer not found")
    return c


# ---------------- Finance ----------------
@router.get("/customers/{customer_id}/finance")
def finance(customer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    today = date.today()

    orders = list(db.scalars(select(DraftOrder).options(selectinload(DraftOrder.lines))
                             .where(DraftOrder.tenant_id == user.tenant_id, DraftOrder.customer_id == c.id)
                             .order_by(DraftOrder.created_at.desc(), DraftOrder.id.desc())))
    sold = [o for o in orders if o.status in SOLD]
    sales_total = sum((money(o.total_amount) for o in sold), money(0))
    by_status: dict[str, int] = defaultdict(int)
    for o in orders:
        by_status[o.status] += 1

    # last 6 months sales (order value, ex GST)
    months = []
    first = today.replace(day=1)
    for i in range(5, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        months.append((y, m))
    monthly = {k: money(0) for k in months}
    for o in sold:
        d = o.approved_at or o.created_at
        if (d.year, d.month) in monthly:
            monthly[(d.year, d.month)] += money(o.total_amount)

    # top products (approved orders)
    top: dict[int, dict] = {}
    for o in sold:
        for line in o.lines:
            if not line.product_id:
                continue
            t = top.setdefault(line.product_id, {"product_id": line.product_id, "quantity": 0.0, "value": money(0), "orders": 0})
            t["quantity"] += line.quantity or 0
            t["orders"] += 1
            if line.unit_price is not None:
                t["value"] += money(line.unit_price) * money(line.quantity)
    top_rows = sorted(top.values(), key=lambda t: t["value"], reverse=True)[:8]
    names = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_([t["product_id"] for t in top_rows])))} if top_rows else {}

    # invoices + aging
    invoices = list(db.scalars(select(Invoice).where(Invoice.tenant_id == user.tenant_id, Invoice.customer_id == c.id)
                               .order_by(Invoice.issue_date.desc(), Invoice.id.desc())))
    aging = {"current": money(0), "1_30": money(0), "31_60": money(0), "61_90": money(0), "90_plus": money(0)}
    outstanding = overdue = money(0)
    overdue_count = 0
    open_rows = []
    invoiced = paid = money(0)
    for inv in invoices:
        if inv.status == "void":
            continue
        if inv.status != "draft":
            invoiced += money(inv.total)
            paid += money(inv.amount_paid)
        if inv.status not in OPEN_INVOICE:
            continue
        due = money(inv.total) - money(inv.amount_paid)
        if due <= 0:
            continue
        outstanding += due
        days = (today - inv.due_date).days if inv.due_date else 0
        if days > 0:
            overdue += due
            overdue_count += 1
        bucket = "current" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "90_plus"
        aging[bucket] += due
        open_rows.append({"id": inv.id, "number": inv.number, "source": inv.source, "status": inv.status,
                          "issue_date": inv.issue_date, "due_date": inv.due_date, "total": to_float(inv.total),
                          "balance": to_float(due), "days_overdue": max(days, 0)})

    return {
        "customer": {"id": c.id, "name": c.name, "code": c.code, "email": c.email, "is_active": c.is_active,
                     "deleted_at": c.deleted_at},
        "orders": {
            "count": len(orders), "approved": len(sold), "needs_review": by_status.get("needs_review", 0),
            "rejected": by_status.get("rejected", 0),
            "sales_total": to_float(sales_total),
            "average_order": to_float(money(sales_total / len(sold))) if sold else 0.0,
            "first_order_at": orders[-1].created_at if orders else None,
            "last_order_at": orders[0].created_at if orders else None,
            "recent": [{"id": o.id, "status": o.status, "po_number": o.po_number, "created_at": o.created_at,
                        "total_amount": to_float(o.total_amount), "line_count": len(o.lines)} for o in orders[:10]],
        },
        "monthly_sales": [{"month": f"{y}-{m:02d}", "value": to_float(v)} for (y, m), v in monthly.items()],
        "top_products": [{**t, "value": to_float(t["value"]), "sku": names[t["product_id"]].sku if t["product_id"] in names else None,
                          "name": names[t["product_id"]].name if t["product_id"] in names else "(deleted)"} for t in top_rows],
        "invoices": {
            "count": len([i for i in invoices if i.status != "void"]),
            "invoiced_total": to_float(invoiced), "paid_total": to_float(paid),
            "outstanding": to_float(outstanding), "overdue": to_float(overdue), "overdue_count": overdue_count,
            "aging": {k: to_float(v) for k, v in aging.items()},
            "open": open_rows,
        },
    }


# ---------------- Pantry ----------------
def pantry_out(row: CustomerPrice) -> dict:
    p = row.product
    return {
        "id": row.id, "product_id": row.product_id, "sku": p.sku, "name": p.name, "unit": p.unit,
        "image_url": primary_thumb(p), "product_active": p.is_active and not p.deleted_at,
        "base_price": to_float(p.price), "price": to_float(row.price),
        "starts_on": row.starts_on, "expires_on": row.expires_on, "note": row.note,
        "status": price_status(row),
        "effective_price": to_float(row.price if price_status(row) == "active" else p.price),
        "updated_at": row.updated_at,
    }


def _validate(data: PantryIn):
    if data.starts_on and data.expires_on and data.expires_on < data.starts_on:
        raise HTTPException(400, "Expiry date can't be before the start date")


def _product(db: Session, user: User, product_id: int | None) -> Product:
    p = db.get(Product, product_id) if product_id else None
    if not p or p.tenant_id != user.tenant_id or p.deleted_at:
        raise HTTPException(400, "Product invalid")
    return p


def _rows(db: Session, customer_id: int) -> list[CustomerPrice]:
    rows = db.scalars(select(CustomerPrice).options(selectinload(CustomerPrice.product).selectinload(Product.images))
                      .where(CustomerPrice.customer_id == customer_id))
    return sorted(rows, key=lambda r: r.product.name.lower())


@router.get("/customers/{customer_id}/pantry")
def get_pantry(customer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    return [pantry_out(r) for r in _rows(db, c.id)]


@router.post("/customers/{customer_id}/pantry")
def add_pantry(customer_id: int, data: PantryIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    _validate(data)
    p = _product(db, user, data.product_id)
    row = db.scalars(select(CustomerPrice).where(CustomerPrice.customer_id == c.id, CustomerPrice.product_id == p.id)).first()
    if not row:
        row = CustomerPrice(tenant_id=user.tenant_id, customer_id=c.id, product_id=p.id)
        db.add(row)
    _apply(row, data)
    db.commit()
    db.refresh(row)
    return pantry_out(row)


def _apply(row: CustomerPrice, data: PantryIn):
    row.price = money(data.price) if data.price is not None else None
    row.starts_on, row.expires_on = data.starts_on, data.expires_on
    row.note = (data.note or "").strip()[:255] or None


@router.post("/customers/{customer_id}/pantry/bulk")
def bulk_add(customer_id: int, data: BulkIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Kai products ek saath pantry mein (price baad mein set karo). from_history = jo customer usually mangata hai."""
    c = _customer(db, user, customer_id)
    ids = set(data.product_ids)
    if data.from_history:
        ids |= set(db.scalars(select(CustomerProductStat.product_id).where(CustomerProductStat.customer_id == c.id)))
    existing = set(db.scalars(select(CustomerPrice.product_id).where(CustomerPrice.customer_id == c.id)))
    valid = set(db.scalars(select(Product.id).where(Product.tenant_id == user.tenant_id, Product.id.in_(ids or [0]),
                                                    Product.deleted_at.is_(None))))
    added = 0
    for pid in sorted(valid - existing):
        db.add(CustomerPrice(tenant_id=user.tenant_id, customer_id=c.id, product_id=pid))
        added += 1
    db.commit()
    return {"added": added, "items": [pantry_out(r) for r in _rows(db, c.id)]}


def _row(db: Session, c: Customer, item_id: int) -> CustomerPrice:
    row = db.get(CustomerPrice, item_id)
    if not row or row.customer_id != c.id:
        raise HTTPException(404, "Pantry item not found")
    return row


@router.put("/customers/{customer_id}/pantry/{item_id}")
def update_pantry(customer_id: int, item_id: int, data: PantryIn, user: User = Depends(admin_only),
                  db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    _validate(data)
    row = _row(db, c, item_id)
    _apply(row, data)
    db.commit()
    db.refresh(row)
    return pantry_out(row)


@router.delete("/customers/{customer_id}/pantry/{item_id}")
def remove_pantry(customer_id: int, item_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    db.delete(_row(db, c, item_id))
    db.commit()
    return {"ok": True}
