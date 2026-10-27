"""
Suppliers API.

  GET    /suppliers?q=&status=active|inactive|deleted|all&sort=     (deleted = Trash; all = Trash ke bina)
  POST   /suppliers
  GET    /suppliers/{id}                 detail + products + POs ka summary
  PUT    /suppliers/{id}
  POST   /suppliers/{id}/status          {is_active}
  DELETE /suppliers/{id}                 Trash mein (soft delete) - wapas aa sakta hai
  POST   /suppliers/{id}/restore         Trash se wapas (Active)
  DELETE /suppliers/{id}/permanent       hamesha ke liye - sirf Trash se, aur sirf jab koi PO / receipt nahi

  GET    /suppliers/{id}/products        ye supplier kya deta hai (SKU, cost, lead time, preferred)
  POST   /suppliers/{id}/products        {product_id, supplier_sku?, unit_cost?, min_order_qty?, lead_time_days?, is_preferred?}
  PUT    /suppliers/{id}/products/{link_id}
  DELETE /suppliers/{id}/products/{link_id}
  GET    /products/{id}/suppliers        product kin suppliers se aata hai
  POST   /suppliers/import-from-accounting   connected Xero / QuickBooks / MYOB se suppliers
  (CSV import: Import data -> Suppliers)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity, to_float
from app.core.sorting import apply_sort
from app.models import Product, PurchaseOrder, StockReceipt, Supplier, SupplierProduct, User
from app.modules.catalog.lifecycle import restore, soft_delete
from app.modules.purchasing.service import OPEN_STATUSES, money_4

router = APIRouter(tags=["purchasing"])
admin_only = require_role("owner", "admin")


class SupplierIn(BaseModel):
    name: str
    code: str | None = None
    email: str | None = None
    cc_emails: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    address: str | None = None
    abn: str | None = None
    payment_terms_days: int | None = Field(None, ge=0, le=365)
    currency: str | None = None
    account_number: str | None = None
    notes: str | None = None
    is_active: bool = True


class LinkIn(BaseModel):
    product_id: int | None = None
    supplier_sku: str | None = None
    unit_cost: float | None = Field(None, ge=0)
    min_order_qty: float | None = Field(None, ge=0)
    lead_time_days: int | None = Field(None, ge=0, le=365)
    is_preferred: bool = False


class StatusIn(BaseModel):
    is_active: bool


TEXT_FIELDS = ("code", "email", "cc_emails", "contact_name", "phone", "address", "abn", "currency", "account_number", "notes")


def supplier_out(s: Supplier, stats: dict | None = None) -> dict:
    return {"id": s.id, "name": s.name, **{k: getattr(s, k) for k in TEXT_FIELDS},
            "payment_terms_days": s.payment_terms_days, "is_active": s.is_active, "source": s.source,
            "external_id": s.external_id, "deleted_at": s.deleted_at, "updated_at": s.updated_at, **(stats or {})}


def link_out(l: SupplierProduct) -> dict:
    p = l.product
    return {"id": l.id, "supplier_id": l.supplier_id, "supplier_name": l.supplier.name, "product_id": l.product_id,
            "sku": p.sku, "name": p.name, "unit": p.unit, "product_active": p.is_active and not p.deleted_at,
            "supplier_sku": l.supplier_sku, "unit_cost": to_float(l.unit_cost), "min_order_qty": l.min_order_qty,
            "lead_time_days": l.lead_time_days, "is_preferred": l.is_preferred, "last_cost": to_float(l.last_cost),
            "last_received_at": l.last_received_at, "avg_cost": to_float(p.avg_cost),
            "quantity_on_hand": p.quantity_on_hand if p.is_tracked_inventory else None}


def _supplier(db: Session, user: User, supplier_id: int) -> Supplier:
    s = db.get(Supplier, supplier_id)
    if not s or s.tenant_id != user.tenant_id:
        raise HTTPException(404, "Supplier not found")
    return s


def _apply(s: Supplier, data: SupplierIn):
    s.name = data.name.strip()[:300]
    for k in TEXT_FIELDS:
        v = getattr(data, k)
        setattr(s, k, v.strip() if isinstance(v, str) and v.strip() else None)
    s.email = s.email.lower() if s.email else None
    s.currency = s.currency.upper()[:3] if s.currency else None
    s.payment_terms_days, s.is_active = data.payment_terms_days, data.is_active


# ---------------- suppliers ----------------
@router.get("/suppliers")
def list_suppliers(q: str = "", status: str = Query("active", pattern="^(active|inactive|deleted|all)$"), sort: str = "",
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=5000),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Supplier).where(Supplier.tenant_id == user.tenant_id)
    if status == "deleted":
        stmt = stmt.where(Supplier.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Supplier.deleted_at.is_(None))
        if status != "all":
            stmt = stmt.where(Supplier.is_active.is_(status == "active"))
    if q:
        stmt = stmt.where(or_(Supplier.name.ilike(f"%{q}%"), Supplier.code.ilike(f"%{q}%"), Supplier.email.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sorts = {"name": Supplier.name, "code": Supplier.code, "email": Supplier.email, "phone": Supplier.phone,
             "payment_terms_days": Supplier.payment_terms_days, "updated_at": Supplier.updated_at}
    rows = list(db.scalars(apply_sort(stmt, sort, sorts, default=Supplier.name, tiebreak=Supplier.id)
                           .offset((page - 1) * page_size).limit(page_size)))
    ids = [s.id for s in rows]
    open_po = dict(db.execute(select(PurchaseOrder.supplier_id, func.count()).where(
        PurchaseOrder.supplier_id.in_(ids or [0]), PurchaseOrder.status.in_(OPEN_STATUSES))
        .group_by(PurchaseOrder.supplier_id)).all())
    products = dict(db.execute(select(SupplierProduct.supplier_id, func.count()).where(
        SupplierProduct.supplier_id.in_(ids or [0])).group_by(SupplierProduct.supplier_id)).all())
    return {"items": [supplier_out(s, {"open_pos": open_po.get(s.id, 0), "product_count": products.get(s.id, 0)})
                      for s in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/suppliers")
def create_supplier(data: SupplierIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(400, "Supplier name is required")
    s = Supplier(tenant_id=user.tenant_id, source="manual")
    _apply(s, data)
    db.add(s)
    db.commit()
    return supplier_out(s)


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    pos = list(db.scalars(select(PurchaseOrder).where(PurchaseOrder.supplier_id == s.id)
                          .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())))
    open_value = sum(float(p.total or 0) for p in pos if p.status in OPEN_STATUSES)
    spend = db.scalar(select(func.coalesce(func.sum(StockReceipt.total), 0)).where(StockReceipt.supplier_id == s.id)) or 0
    return supplier_out(s, {
        "open_pos": sum(p.status in OPEN_STATUSES for p in pos), "open_po_value": round(open_value, 2),
        "received_total": float(spend), "po_count": len(pos),
        "recent_pos": [{"id": p.id, "number": p.number, "status": p.status, "order_date": p.order_date,
                        "expected_date": p.expected_date, "total": to_float(p.total)} for p in pos[:10]],
    })


@router.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, data: SupplierIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    _not_in_trash(s)
    if not data.name.strip():
        raise HTTPException(400, "Supplier name is required")
    _apply(s, data)
    db.commit()
    return supplier_out(s)


@router.post("/suppliers/{supplier_id}/status")
def supplier_status(supplier_id: int, data: StatusIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    _not_in_trash(s)
    s.is_active = data.is_active
    db.commit()
    return supplier_out(s)


def _not_in_trash(s: Supplier):
    if s.deleted_at:
        raise HTTPException(400, "This supplier is in the Trash - restore it first")


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Trash mein (POs / receipts wale supplier bhi - record bana rehta hai)."""
    soft_delete(db, user, _supplier(db, user, supplier_id), "supplier")
    return {"ok": True}


@router.post("/suppliers/{supplier_id}/restore")
def restore_supplier(supplier_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    restore(db, user, s, "supplier")
    return supplier_out(s)


@router.delete("/suppliers/{supplier_id}/permanent")
def purge_supplier(supplier_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    if not s.deleted_at:
        raise HTTPException(400, "Move the supplier to the Trash first, then delete it permanently")
    used = db.scalar(select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.supplier_id == s.id)) or 0
    used += db.scalar(select(func.count()).select_from(StockReceipt).where(StockReceipt.supplier_id == s.id)) or 0
    if used:
        raise HTTPException(400, f"This supplier has {used} purchase order(s) / receipt(s), so it can't be deleted "
                                 "permanently (they're kept for your records). It can stay in the Trash.")
    log_activity(db, user.tenant_id, user.id, "supplier.purged", "supplier", s.id, {"name": s.name})
    db.delete(s)
    db.commit()
    return {"ok": True}


# ---------------- supplier products ----------------
def _links(db: Session, stmt) -> list[dict]:
    rows = db.scalars(stmt.options(selectinload(SupplierProduct.product), selectinload(SupplierProduct.supplier)))
    return [link_out(l) for l in rows]


@router.get("/suppliers/{supplier_id}/products")
def supplier_products(supplier_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    rows = _links(db, select(SupplierProduct).where(SupplierProduct.supplier_id == s.id))
    return sorted(rows, key=lambda r: r["name"].lower())


def _set_link(db: Session, l: SupplierProduct, data: LinkIn):
    l.supplier_sku = (data.supplier_sku or "").strip()[:100] or None
    l.unit_cost = money_4(data.unit_cost) if data.unit_cost is not None else None
    l.min_order_qty, l.lead_time_days = data.min_order_qty, data.lead_time_days
    if data.is_preferred:   # ek product ka ek hi preferred supplier
        for other in db.scalars(select(SupplierProduct).where(SupplierProduct.product_id == l.product_id,
                                                              SupplierProduct.id != (l.id or 0))):
            other.is_preferred = False
    l.is_preferred = data.is_preferred


@router.post("/suppliers/{supplier_id}/products")
def add_link(supplier_id: int, data: LinkIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = _supplier(db, user, supplier_id)
    p = db.get(Product, data.product_id) if data.product_id else None
    if not p or p.tenant_id != user.tenant_id or p.deleted_at:
        raise HTTPException(400, "Choose a valid product")
    l = db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == s.id,
                                                 SupplierProduct.product_id == p.id)).first()
    if not l:
        l = SupplierProduct(tenant_id=user.tenant_id, supplier_id=s.id, product_id=p.id)
        db.add(l)
        db.flush()
    _set_link(db, l, data)
    db.commit()
    db.refresh(l)
    return link_out(l)


def _link(db: Session, s: Supplier, link_id: int) -> SupplierProduct:
    l = db.get(SupplierProduct, link_id)
    if not l or l.supplier_id != s.id:
        raise HTTPException(404, "Not found")
    return l


@router.put("/suppliers/{supplier_id}/products/{link_id}")
def update_link(supplier_id: int, link_id: int, data: LinkIn, user: User = Depends(admin_only),
                db: Session = Depends(get_db)):
    l = _link(db, _supplier(db, user, supplier_id), link_id)
    _set_link(db, l, data)
    db.commit()
    db.refresh(l)
    return link_out(l)


@router.delete("/suppliers/{supplier_id}/products/{link_id}")
def delete_link(supplier_id: int, link_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    db.delete(_link(db, _supplier(db, user, supplier_id), link_id))
    db.commit()
    return {"ok": True}


@router.get("/products/{product_id}/suppliers")
def product_suppliers(product_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    return _links(db, select(SupplierProduct).where(SupplierProduct.product_id == p.id))


@router.post("/suppliers/import-from-accounting")
def import_from_accounting(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Connected software ke saare suppliers (Xero: IsSupplier contacts, QuickBooks: Vendors, MYOB: Suppliers)."""
    from app.modules.connectors.api import get_connection, open_api
    from app.modules.connectors.base import ConnectorError
    from app.modules.imports.service import import_suppliers

    if not get_connection(db, user.tenant_id):
        raise HTTPException(400, "Connect your accounting software first (Settings → Integrations)")
    try:
        api = open_api(db, user.tenant_id)
        rows = api.provider.fetch_suppliers(api, None)
    except ConnectorError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    from app.core.audit import no_audit
    with no_audit(db):
        result = import_suppliers(db, user.tenant_id, rows, source=api.provider.key)
        db.flush()
    log_activity(db, user.tenant_id, user.id, "import.suppliers", detail={"source": api.provider.key, **result.as_dict()})
    db.commit()
    return {"provider": api.provider.key, "label": api.provider.label, **result.as_dict()}
