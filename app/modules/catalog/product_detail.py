"""
Product page - product ke naam pe click: kis kis order mein, kitni qty, kis customer ne.

  GET /products/{id}                     product (product_out)
  GET /products/{id}/orders?status=&page=  order lines + summary (qty / value / orders per status)
Stock movements: GET /stock/movements?product_id=   ·   Change history: GET /activity/entity/product/{id}
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import current_user
from app.core.helpers import to_float
from app.models import Category, Customer, DraftOrder, DraftOrderLine, Product, User
from app.modules.catalog.router import product_out

router = APIRouter(tags=["catalog"])

SOLD = ("approved", "exported")   # yahi "sale" maana jaata hai (reports bhi)


def _product(db: Session, user: User, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    return p


@router.get("/products/{product_id}")
def get_product(product_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.scalars(select(Product).where(Product.id == product_id, Product.tenant_id == user.tenant_id).options(
        selectinload(Product.aliases), selectinload(Product.images),
        selectinload(Product.category).selectinload(Category.parent))).first()
    if not p:
        raise HTTPException(404, "Product not found")
    return product_out(p)


@router.get("/products/{product_id}/orders")
def product_orders(product_id: int, status: str | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(25, ge=1, le=200), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    base = (select(DraftOrderLine, DraftOrder)
            .join(DraftOrder, DraftOrder.id == DraftOrderLine.order_id)
            .where(DraftOrder.tenant_id == user.tenant_id, DraftOrderLine.product_id == p.id))

    # Summary: har status ka orders / qty / value (ek query)
    summary_rows = db.execute(
        select(DraftOrder.status, func.count(func.distinct(DraftOrder.id)), func.sum(DraftOrderLine.quantity),
               func.sum(DraftOrderLine.quantity * DraftOrderLine.unit_price))
        .join(DraftOrder, DraftOrder.id == DraftOrderLine.order_id)
        .where(DraftOrder.tenant_id == user.tenant_id, DraftOrderLine.product_id == p.id)
        .group_by(DraftOrder.status)).all()
    by_status = {s: {"orders": n, "quantity": float(q or 0), "value": round(float(v or 0), 2)}
                 for s, n, q, v in summary_rows}
    sold = [by_status[s] for s in SOLD if s in by_status]
    summary = {
        "by_status": by_status,
        "sold_quantity": sum(x["quantity"] for x in sold),
        "sold_value": round(sum(x["value"] for x in sold), 2),
        "sold_orders": sum(x["orders"] for x in sold),
        "pending_quantity": by_status.get("needs_review", {}).get("quantity", 0),   # review mein (abhi approve nahi)
        "customers": db.scalar(select(func.count(func.distinct(DraftOrder.customer_id)))
                               .join(DraftOrderLine, DraftOrderLine.order_id == DraftOrder.id)
                               .where(DraftOrder.tenant_id == user.tenant_id, DraftOrderLine.product_id == p.id,
                                      DraftOrder.status.in_(SOLD))) or 0,
    }

    stmt = base.where(DraftOrder.status == status) if status else base
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(DraftOrder.created_at.desc(), DraftOrder.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    cust_ids = {o.customer_id for _, o in rows if o.customer_id}
    names = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))).all()) if cust_ids else {}
    items = [{
        "order_id": o.id, "status": o.status, "source": o.source, "created_at": o.created_at,
        "approved_at": o.approved_at, "delivery_date": o.delivery_date, "po_number": o.po_number,
        "customer_id": o.customer_id, "customer_name": names.get(o.customer_id) or o.customer_raw,
        "quantity": l.quantity, "unit": l.unit or p.unit, "unit_price": to_float(l.unit_price),
        "line_total": round(float(l.unit_price) * l.quantity, 2) if l.unit_price is not None else None,
    } for l, o in rows]
    return {"summary": summary, "items": items, "total": total, "page": page, "page_size": page_size}
