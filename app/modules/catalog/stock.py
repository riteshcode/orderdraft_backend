"""
Stock page - quantity on hand manage karna.

  GET  /stock?status=all|low|out|ok&q=&sort=        tracked products + summary
  POST /stock/adjust        {product_id, mode: add|remove|set, quantity, reason, note}
  POST /stock/track         {product_id, track: bool, reorder_level?}   tracking on/off
  PUT  /stock/{product_id}/reorder-level  {reorder_level}
  GET  /stock/movements?product_id=&page=           history (kisne, kab, kitna, kyun)

Rules:
  - Har badlav ek StockMovement row (audit). quantity_on_hand = naya qty.
  - Stock ka master OrderDraft hai - Xero / QuickBooks / MYOB wale products ka bhi. Sync sirf naye
    product pe opening qty laata hai, uske baad on hand kabhi overwrite nahi karta.
  - Order approve = tracked products ka on hand kam (Settings -> Stock: stock_deduct_on_approve).
  - Sirf "Track inventory" on wale products count hote hain.
  - Low stock = qty <= reorder_level.  Out of stock = qty <= 0.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity, to_float
from app.core.sorting import apply_sort
from app.models import Product, StockMovement, User

router = APIRouter(prefix="/stock", tags=["stock"])
admin_only = require_role("owner", "admin")

REASONS = {"adjust": "Adjustment", "receive": "Stock received", "count": "Stock count", "damaged": "Damaged / waste",
           "return": "Customer return", "sale": "Sold", "opening": "Opening stock",
           "order_cancelled": "Order cancelled"}
MANUAL_REASONS = {k: v for k, v in REASONS.items() if k not in ("sale", "opening", "order_cancelled")}   # Adjust dialog mein
SORTS = {"name": Product.name, "sku": Product.sku, "quantity_on_hand": Product.quantity_on_hand, "avg_cost": Product.avg_cost,
         "reorder_level": Product.reorder_level, "price": Product.price, "updated_at": Product.updated_at}


class AdjustIn(BaseModel):
    product_id: int
    mode: str = Field("add", pattern="^(add|remove|set)$")
    quantity: float = Field(..., ge=0)
    reason: str = "adjust"
    note: str | None = None


class TrackIn(BaseModel):
    product_id: int
    track: bool
    reorder_level: float | None = Field(None, ge=0)


class ReorderIn(BaseModel):
    reorder_level: float | None = Field(None, ge=0)


# ---------------- helpers ----------------
def stock_status(p: Product) -> str:
    qty = p.quantity_on_hand or 0
    if qty <= 0:
        return "out"
    if p.reorder_level is not None and qty <= p.reorder_level:
        return "low"
    return "ok"


def record_movement(db: Session, p: Product, new_qty: float, reason: str, note: str | None = None,
                    user_id: int | None = None, ref_type: str | None = None, ref_id: int | None = None) -> StockMovement:
    """Qty badlo + log. Commit caller karega."""
    before = p.quantity_on_hand or 0
    m = StockMovement(tenant_id=p.tenant_id, product_id=p.id, change=round(new_qty - before, 4), qty_before=before,
                      qty_after=new_qty, reason=reason, note=(note or "").strip()[:255] or None, user_id=user_id,
                      ref_type=ref_type, ref_id=ref_id)
    db.add(m)
    p.quantity_on_hand = new_qty
    return m


def deduct_for_order(db: Session, order, user_id: int | None = None) -> int:
    """
    Order approve -> har tracked product ka on hand kam (ek product ki kai lines = ek movement).
    Stock kam pad jaaye to bhi approve hota hai (negative on hand dikhega). Commit caller karega.
    Returns: kitne products ka stock badla.
    """
    qty_by_product: dict[int, float] = {}
    for line in order.lines:
        if line.product_id and line.quantity:
            qty_by_product[line.product_id] = qty_by_product.get(line.product_id, 0) + line.quantity
    changed = 0
    for product_id, qty in qty_by_product.items():
        p = db.get(Product, product_id)
        if not p or not p.is_tracked_inventory:
            continue
        label = f"Order #{order.id}" + (f" (PO {order.po_number})" if order.po_number else "")
        record_movement(db, p, round((p.quantity_on_hand or 0) - qty, 4), "sale", label, user_id, "order", order.id)
        changed += 1
    return changed


def unit_value(p: Product) -> float:
    """Stock value ke liye cost: average cost -> purchase price -> sell price."""
    return float(p.avg_cost if p.avg_cost is not None else (p.purchase_price or p.price or 0))


def stock_out(p: Product, on_order: float = 0) -> dict:
    from app.modules.catalog.images import primary_thumb
    return {"id": p.id, "sku": p.sku, "barcode": p.barcode, "name": p.name, "unit": p.unit, "image_url": primary_thumb(p),
            "quantity_on_hand": p.quantity_on_hand or 0, "reorder_level": p.reorder_level, "status": stock_status(p),
            "price": to_float(p.price), "purchase_price": to_float(p.purchase_price), "avg_cost": to_float(p.avg_cost),
            "on_order": on_order, "stock_value": round(max(p.quantity_on_hand or 0, 0) * unit_value(p), 2),
            "is_active": p.is_active, "source": p.source, "updated_at": p.updated_at}


def _product(db: Session, user: User, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id or p.deleted_at:
        raise HTTPException(404, "Product not found")
    return p


# ---------------- endpoints ----------------
@router.get("")
def list_stock(
    q: str = "",
    status: str = Query("all", pattern="^(all|low|out|ok)$"),
    sort: str = "name",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    base = select(Product).where(Product.tenant_id == user.tenant_id, Product.deleted_at.is_(None),
                                 Product.is_tracked_inventory.is_(True))
    qty = func.coalesce(Product.quantity_on_hand, 0)
    low = and_(Product.reorder_level.is_not(None), qty <= Product.reorder_level, qty > 0)

    # summary (filter se pehle)
    tracked = db.scalars(base).all()
    summary = {"tracked": len(tracked), "out": sum(stock_status(p) == "out" for p in tracked),
               "low": sum(stock_status(p) == "low" for p in tracked),
               "stock_value": round(sum((p.quantity_on_hand or 0) * unit_value(p)
                                        for p in tracked if (p.quantity_on_hand or 0) > 0), 2),
               "untracked": db.scalar(select(func.count()).select_from(Product).where(
                   Product.tenant_id == user.tenant_id, Product.deleted_at.is_(None),
                   Product.is_tracked_inventory.is_(False))) or 0}

    stmt = base
    if q:
        stmt = stmt.where(or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%"), Product.barcode == q.strip()))
    if status == "out":
        stmt = stmt.where(qty <= 0)
    elif status == "low":
        stmt = stmt.where(low)
    elif status == "ok":
        stmt = stmt.where(qty > 0, or_(Product.reorder_level.is_(None), qty > Product.reorder_level))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    stmt = apply_sort(stmt, sort, SORTS, default=Product.name, tiebreak=Product.id)
    items = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    from app.modules.purchasing.service import on_order_map
    on_order = on_order_map(db, user.tenant_id)
    return {"items": [stock_out(p, on_order.get(p.id, 0)) for p in items], "total": total, "page": page, "page_size": page_size,
            "summary": summary, "reasons": MANUAL_REASONS}


@router.post("/adjust")
def adjust(data: AdjustIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, data.product_id)
    if not p.is_tracked_inventory:
        raise HTTPException(400, "Stock isn't tracked for this product - turn on stock tracking first")
    if data.reason not in MANUAL_REASONS:
        raise HTTPException(400, "Choose a valid reason")
    current = p.quantity_on_hand or 0
    new_qty = {"add": current + data.quantity, "remove": current - data.quantity, "set": data.quantity}[data.mode]
    if data.mode != "set" and data.quantity == 0:
        raise HTTPException(400, "Enter a quantity greater than 0")
    record_movement(db, p, round(new_qty, 4), data.reason, data.note, user.id)
    log_activity(db, user.tenant_id, user.id, "stock.adjusted", "product", p.id,
                 {"sku": p.sku, "mode": data.mode, "quantity": data.quantity, "after": new_qty, "reason": data.reason})
    db.commit()
    return stock_out(p)


@router.post("/track")
def track(data: TrackIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, data.product_id)
    p.is_tracked_inventory = data.track
    if data.track:
        if p.quantity_on_hand is None:
            p.quantity_on_hand = 0
        if "reorder_level" in data.model_fields_set:
            p.reorder_level = data.reorder_level
    db.commit()
    return stock_out(p)


@router.put("/{product_id}/reorder-level")
def set_reorder(product_id: int, data: ReorderIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    p.reorder_level = data.reorder_level
    db.commit()
    return stock_out(p)


@router.get("/movements")
def movements(product_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.models import User as UserModel

    stmt = select(StockMovement).where(StockMovement.tenant_id == user.tenant_id)
    if product_id:
        stmt = stmt.where(StockMovement.product_id == product_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(db.scalars(stmt.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)))
    user_ids = {r.user_id for r in rows if r.user_id}
    names = dict(db.execute(select(UserModel.id, UserModel.name).where(UserModel.id.in_(user_ids))).all()) if user_ids else {}
    return {"items": [{"id": r.id, "product_id": r.product_id, "sku": r.product.sku, "name": r.product.name,
                       "change": r.change, "qty_before": r.qty_before, "qty_after": r.qty_after, "reason": r.reason,
                       "reason_label": REASONS.get(r.reason, r.reason), "note": r.note,
                       "ref_type": r.ref_type, "ref_id": r.ref_id,
                       "user_name": names.get(r.user_id), "created_at": r.created_at} for r in rows],
            "total": total, "page": page, "page_size": page_size}
