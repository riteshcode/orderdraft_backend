"""
Products / customers ka lifecycle:

  POST   /products/{id}/status      {is_active}   Active <-> Inactive (matching / naye orders mein nahi aata)
  DELETE /products/{id}                           Soft delete -> Trash (wapas aa sakta hai)
  POST   /products/{id}/restore                   Trash se wapas (Active)
  DELETE /products/{id}/permanent                 Hamesha ke liye (sirf Trash se, aur sirf agar
                                                  kisi order / invoice mein use nahi hua)
  (same 4 endpoints /customers/{id}/... ke liye)

Trash list:  GET /products?status=deleted   (status = active | inactive | deleted | all)
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db, utcnow
from app.core.deps import require_role
from app.core.audit import no_audit
from app.core.helpers import log_activity
from app.models import (Customer, CustomerPrice, CustomerProductStat, DraftOrder, DraftOrderLine, Invoice, InvoiceLine,
                        Product, ProductAlias, ProductImage, User)
from app.modules.catalog.images import delete_files

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")


class StatusIn(BaseModel):
    is_active: bool


def _product(db: Session, user: User, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    return p


def _customer(db: Session, user: User, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if not c or c.tenant_id != user.tenant_id:
        raise HTTPException(404, "Customer not found")
    return c


def _count(db: Session, stmt) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


# ---------------- shared logic ----------------
def set_status(db: Session, user: User, obj, kind: str, is_active: bool):
    if obj.deleted_at:
        raise HTTPException(400, f"{kind.title()} is in Trash - restore it first")
    with no_audit(db):   # apna saaf log neeche (activated / deactivated), audit ka "updated" duplicate nahi
        obj.is_active = is_active
        db.flush()
    log_activity(db, user.tenant_id, user.id, f"{kind}.{'activated' if is_active else 'deactivated'}", kind, obj.id)
    db.commit()


def soft_delete(db: Session, user: User, obj, kind: str):
    if not obj.deleted_at:
        with no_audit(db):
            obj.deleted_at = utcnow()
            obj.is_active = False      # saare purane is_active filters (matching, dropdowns) apne aap chhod dete hain
            db.flush()
        log_activity(db, user.tenant_id, user.id, f"{kind}.deleted", kind, obj.id, {"name": obj.name})
        db.commit()


def restore(db: Session, user: User, obj, kind: str):
    if not obj.deleted_at:
        raise HTTPException(400, f"{kind.title()} isn't in Trash")
    with no_audit(db):
        obj.deleted_at = None
        obj.is_active = True
        db.flush()
    log_activity(db, user.tenant_id, user.id, f"{kind}.restored", kind, obj.id, {"name": obj.name})
    db.commit()


# ---------------- products ----------------
@router.post("/products/{product_id}/status")
def product_status(product_id: int, data: StatusIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    set_status(db, user, p, "product", data.is_active)
    return {"ok": True, "id": p.id, "is_active": p.is_active}


@router.delete("/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    soft_delete(db, user, _product(db, user, product_id), "product")
    return {"ok": True}


@router.post("/products/{product_id}/restore")
def restore_product(product_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    restore(db, user, p, "product")
    return {"ok": True, "id": p.id}


@router.delete("/products/{product_id}/permanent")
def purge_product(product_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    if not p.deleted_at:
        raise HTTPException(400, "Move the product to Trash before deleting it permanently")
    used = _count(db, select(DraftOrderLine.id).where(or_(DraftOrderLine.product_id == p.id,
                                                          DraftOrderLine.suggested_product_id == p.id)))
    used += _count(db, select(InvoiceLine.id).where(InvoiceLine.product_id == p.id))
    if used:
        raise HTTPException(400, f"This product is used on {used} order/invoice line(s), so it can't be permanently deleted "
                                 "(it's needed for your records). Please leave it in Trash.")
    for img in db.scalars(select(ProductImage).where(ProductImage.product_id == p.id)):
        delete_files(img)
    for model in (ProductImage, ProductAlias, CustomerProductStat, CustomerPrice):
        db.execute(delete(model).where(model.product_id == p.id))
    log_activity(db, user.tenant_id, user.id, "product.purged", "product", p.id, {"sku": p.sku, "name": p.name})
    db.expunge(p)
    db.execute(delete(Product).where(Product.id == p.id))
    db.commit()
    return {"ok": True}


# ---------------- customers ----------------
@router.post("/customers/{customer_id}/status")
def customer_status(customer_id: int, data: StatusIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    set_status(db, user, c, "customer", data.is_active)
    return {"ok": True, "id": c.id, "is_active": c.is_active}


@router.delete("/customers/{customer_id}")
def delete_customer(customer_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    soft_delete(db, user, _customer(db, user, customer_id), "customer")
    return {"ok": True}


@router.post("/customers/{customer_id}/restore")
def restore_customer(customer_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    restore(db, user, c, "customer")
    return {"ok": True, "id": c.id}


@router.delete("/customers/{customer_id}/permanent")
def purge_customer(customer_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    if not c.deleted_at:
        raise HTTPException(400, "Move the customer to Trash before deleting them permanently")
    used = _count(db, select(DraftOrder.id).where(DraftOrder.customer_id == c.id))
    used += _count(db, select(Invoice.id).where(Invoice.customer_id == c.id))
    if used:
        raise HTTPException(400, f"This customer has {used} order(s)/invoice(s), so they can't be permanently deleted "
                                 "(they're needed for your records). Please leave them in Trash.")
    for model in (CustomerPrice, CustomerProductStat, ProductAlias):
        db.execute(delete(model).where(model.customer_id == c.id))
    log_activity(db, user.tenant_id, user.id, "customer.purged", "customer", c.id, {"name": c.name})
    db.expunge(c)
    db.execute(delete(Customer).where(Customer.id == c.id))
    db.commit()
    return {"ok": True}
