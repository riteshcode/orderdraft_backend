"""
Products aur customers - list (search + pagination), create, edit, deactivate, learned aliases.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import money, to_float
from app.core.sorting import apply_sort
from app.models import Category, Customer, CustomerProductStat, Product, ProductAlias, User
from app.modules.catalog.categories import category_path, descendant_ids, get_category
from app.modules.catalog.barcodes import barcode_taken, clean_barcode, internal_ean13, search_filter
from app.modules.catalog.images import image_out, sorted_images
from app.modules.catalog.stock import record_movement
from app.modules.imports.service import add_alias, display_address

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")


class ProductIn(BaseModel):
    sku: str
    name: str
    barcode: str | None = None          # khali = internal EAN-13 apne aap
    reorder_level: float | None = None
    description: str | None = None
    unit: str | None = None
    price: float | None = None
    is_taxable: bool = True
    account_code: str | None = None
    tax_type: str | None = None
    # Purchase side (Xero PurchaseDetails jaisa)
    purchase_description: str | None = None
    purchase_price: float | None = None
    purchase_account_code: str | None = None
    purchase_tax_type: str | None = None
    is_tracked_inventory: bool = False
    quantity_on_hand: float | None = None
    is_sold: bool = True
    is_purchased: bool = False
    category_id: int | None = None
    aliases: list[str] = []


class CustomerIn(BaseModel):
    name: str
    code: str | None = None
    email: str | None = None
    other_emails: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    mobile: str | None = None
    fax: str | None = None
    ddi: str | None = None
    address: str | None = None          # khali = delivery/postal parts se ban jaata hai
    postal_line1: str | None = None
    postal_line2: str | None = None
    postal_city: str | None = None
    postal_region: str | None = None
    postal_postcode: str | None = None
    postal_country: str | None = None
    postal_attention: str | None = None
    delivery_line1: str | None = None
    delivery_line2: str | None = None
    delivery_city: str | None = None
    delivery_region: str | None = None
    delivery_postcode: str | None = None
    delivery_country: str | None = None
    delivery_attention: str | None = None
    delivery_instructions: str | None = None
    abn: str | None = None
    company_number: str | None = None
    bank_account_details: str | None = None
    sales_tax_type: str | None = None
    purchase_tax_type: str | None = None
    default_currency: str | None = None
    is_customer: bool = True
    is_supplier: bool = False
    is_active: bool = True


CUSTOMER_FIELDS = [f for f in CustomerIn.model_fields]
PRODUCT_EXTRA = ("purchase_description", "purchase_account_code", "purchase_tax_type", "is_tracked_inventory",
                 "is_sold", "is_purchased")


def product_out(p: Product) -> dict:
    images = [image_out(i) for i in sorted_images(p)]
    return {
        "id": p.id, "sku": p.sku, "barcode": p.barcode, "name": p.name, "description": p.description, "unit": p.unit,
        "price": to_float(p.price), "is_taxable": p.is_taxable, "account_code": p.account_code,
        "tax_type": p.tax_type, "is_active": p.is_active, "source": p.source, "external_id": p.external_id,
        **{k: getattr(p, k) for k in PRODUCT_EXTRA},
        "purchase_price": to_float(p.purchase_price), "quantity_on_hand": p.quantity_on_hand,
        "reorder_level": p.reorder_level, "avg_cost": to_float(p.avg_cost),
        "category_id": p.category_id, "category_name": category_path(p.category),
        "external_updated_at": p.external_updated_at,
        "image_url": images[0]["thumb_url"] if images else None, "images": images,
        "aliases": sorted({a.alias for a in p.aliases if a.customer_id is None}),
        "deleted_at": p.deleted_at, "updated_at": p.updated_at,
    }


def customer_out(c: Customer) -> dict:
    return {
        "id": c.id, **{k: getattr(c, k) for k in CUSTOMER_FIELDS},
        "source": c.source, "external_id": c.external_id, "external_updated_at": c.external_updated_at,
        "deleted_at": c.deleted_at, "updated_at": c.updated_at,
    }


def _clean_customer(c: Customer):
    c.email = c.email.lower().strip() if c.email else None
    c.default_currency = c.default_currency.upper().strip()[:3] if c.default_currency else None
    for k in CUSTOMER_FIELDS:   # "" -> None
        if isinstance(getattr(c, k), str) and not getattr(c, k).strip():
            setattr(c, k, None)
    if not c.address:
        c.address = display_address(c)
    if not c.phone:
        c.phone = c.mobile or c.ddi


def apply_status(stmt, model, status: str, include_inactive: bool):
    """status: active (default) | inactive | deleted (Trash) | all (deleted ke alawa sab)."""
    if include_inactive and status == "active":
        status = "all"
    if status == "deleted":
        return stmt.where(model.deleted_at.is_not(None))
    stmt = stmt.where(model.deleted_at.is_(None))
    if status == "active":
        return stmt.where(model.is_active.is_(True))
    if status == "inactive":
        return stmt.where(model.is_active.is_(False))
    return stmt


STATUS_RE = "^(active|inactive|deleted|all)$"


# ---------------- Products ----------------
@router.get("/products")
def list_products(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=5000),
    include_inactive: bool = False,
    status: str = Query("active", pattern=STATUS_RE),
    sort: str = "",                      # field ya -field (desc)
    category_id: int | None = None,      # parent chuno to uski sub-categories bhi; 0 = Uncategorised
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = apply_status(select(Product).where(Product.tenant_id == user.tenant_id), Product, status, include_inactive)
    if q:
        stmt = stmt.where(search_filter(q))      # name / SKU / barcode (scan karke search)
    if category_id == 0:
        stmt = stmt.where(Product.category_id.is_(None))
    elif category_id:
        stmt = stmt.where(Product.category_id.in_(descendant_ids(db, user.tenant_id, category_id)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sorts = {"sku": Product.sku, "name": Product.name, "unit": Product.unit, "price": Product.price,
             "is_taxable": Product.is_taxable, "source": Product.source, "quantity_on_hand": Product.quantity_on_hand,
             "barcode": Product.barcode, "is_active": Product.is_active, "updated_at": Product.updated_at,
             "deleted_at": Product.deleted_at}
    stmt = apply_sort(stmt, sort, sorts, default=Product.name, tiebreak=Product.id)
    items = db.scalars(stmt.options(selectinload(Product.aliases), selectinload(Product.images),
                                    selectinload(Product.category).selectinload(Category.parent))
                       .offset((page - 1) * page_size).limit(page_size))
    return {"items": [product_out(p) for p in items], "total": total, "page": page, "page_size": page_size}


def _apply_product(db: Session, p: Product, data: ProductIn):
    p.sku, p.name = data.sku.strip(), data.name.strip()
    p.description, p.unit = data.description, data.unit
    p.price = money(data.price) if data.price is not None else None
    p.is_taxable, p.account_code, p.tax_type = data.is_taxable, data.account_code, data.tax_type
    # Naye fields sirf tab badlo jab bheje gaye (purana form bheje to synced data mit na jaaye)
    sent = data.model_fields_set
    for k in PRODUCT_EXTRA:
        if k in sent or p.id is None:
            setattr(p, k, getattr(data, k))
    if "purchase_price" in sent:
        p.purchase_price = money(data.purchase_price) if data.purchase_price is not None else None
    if "quantity_on_hand" in sent and p.quantity_on_hand != data.quantity_on_hand:
        if p.id is not None:     # stock log mein bhi (Stock page pe history dikhe)
            record_movement(db, p, data.quantity_on_hand, "adjust", "Edited on product form")
        p.quantity_on_hand = data.quantity_on_hand
    if "category_id" in sent or p.id is None:
        p.category_id = get_category(db, p.tenant_id, data.category_id).id if data.category_id else None
    if "reorder_level" in sent or p.id is None:
        p.reorder_level = data.reorder_level
    if "barcode" in sent:
        try:
            code = clean_barcode(data.barcode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if code and barcode_taken(db, p.tenant_id, code, p.id):
            raise HTTPException(400, f"Barcode {code} is already used by another product")
        p.barcode = code or (internal_ean13(p.id) if p.id else None)


@router.post("/products")
def create_product(data: ProductIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.sku.strip() or not data.name.strip():
        raise HTTPException(400, "Enter a SKU and name")
    existing = db.scalars(select(Product).where(Product.tenant_id == user.tenant_id, Product.sku == data.sku.strip())).first()
    if existing:
        raise HTTPException(400, "This SKU is in Trash - restore it from there" if existing.deleted_at else "This SKU already exists")
    p = Product(tenant_id=user.tenant_id, source="manual")
    _apply_product(db, p, data)
    db.add(p)
    db.flush()
    if not p.barcode:
        p.barcode = internal_ean13(p.id)
    for a in data.aliases:
        add_alias(db, user.tenant_id, p.id, a)
    db.commit()
    db.refresh(p)
    return product_out(p)


@router.put("/products/{product_id}")
def update_product(product_id: int, data: ProductIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    if p.deleted_at:
        raise HTTPException(400, "This product is in Trash - restore it first")
    clash = db.scalars(select(Product).where(Product.tenant_id == user.tenant_id, Product.sku == data.sku.strip(),
                                             Product.id != p.id)).first()
    if clash:
        raise HTTPException(400, "This SKU belongs to another product")
    _apply_product(db, p, data)
    # Global aliases replace (customer-specific learned aliases ko mat chhedo)
    for a in list(p.aliases):
        if a.customer_id is None:
            db.delete(a)
    db.flush()
    for a in data.aliases:
        add_alias(db, user.tenant_id, p.id, a)
    db.commit()
    db.refresh(p)
    return product_out(p)


# ---------------- Customers ----------------
@router.get("/customers")
def list_customers(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=5000),
    include_inactive: bool = False,
    status: str = Query("active", pattern=STATUS_RE),
    sort: str = "",                      # field ya -field (desc)
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = apply_status(select(Customer).where(Customer.tenant_id == user.tenant_id), Customer, status, include_inactive)
    if q:
        stmt = stmt.where(or_(Customer.name.ilike(f"%{q}%"), Customer.code.ilike(f"%{q}%"), Customer.email.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sorts = {"code": Customer.code, "name": Customer.name, "email": Customer.email, "phone": Customer.phone,
             "source": Customer.source, "is_active": Customer.is_active, "city": Customer.delivery_city,
             "updated_at": Customer.updated_at, "deleted_at": Customer.deleted_at}
    stmt = apply_sort(stmt, sort, sorts, default=Customer.name, tiebreak=Customer.id)
    items = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    return {"items": [customer_out(c) for c in items], "total": total, "page": page, "page_size": page_size}


@router.post("/customers")
def create_customer(data: CustomerIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(400, "Enter a name")
    c = Customer(tenant_id=user.tenant_id, source="manual", **data.model_dump())
    _clean_customer(c)
    db.add(c)
    db.commit()
    return customer_out(c)


@router.put("/customers/{customer_id}")
def update_customer(customer_id: int, data: CustomerIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _get_customer(db, user, customer_id)
    if c.deleted_at:
        raise HTTPException(400, "This customer is in Trash - restore it first")
    sent = data.model_dump(exclude_unset=True)
    for k, v in sent.items():
        setattr(c, k, v)
    if "address" not in sent and any(k.startswith(("delivery_line", "delivery_city", "postal_line", "postal_city"))
                                     or k.endswith(("_region", "_postcode", "_country")) for k in sent):
        c.address = None     # parts badle -> display address dobara banao
    _clean_customer(c)
    db.commit()
    return customer_out(c)


@router.get("/customers/{customer_id}")
def get_customer(customer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return customer_out(_get_customer(db, user, customer_id))


@router.get("/customers/{customer_id}/insights")
def customer_insights(customer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Customer kya mangata hai + system ne kya seekha."""
    _get_customer(db, user, customer_id)
    aliases = db.execute(
        select(ProductAlias.id, ProductAlias.alias, ProductAlias.source, Product.sku, Product.name)
        .join(Product, Product.id == ProductAlias.product_id)
        .where(ProductAlias.tenant_id == user.tenant_id, ProductAlias.customer_id == customer_id)
        .order_by(ProductAlias.alias)
    ).all()
    usual = db.execute(
        select(Product.sku, Product.name, CustomerProductStat.times_ordered, CustomerProductStat.last_ordered_at)
        .join(Product, Product.id == CustomerProductStat.product_id)
        .where(CustomerProductStat.customer_id == customer_id)
        .order_by(CustomerProductStat.times_ordered.desc()).limit(30)
    ).all()
    return {
        "aliases": [{"id": r.id, "alias": r.alias, "source": r.source, "sku": r.sku, "product_name": r.name} for r in aliases],
        "usual_products": [{"sku": r.sku, "name": r.name, "times_ordered": r.times_ordered,
                            "last_ordered_at": r.last_ordered_at} for r in usual],
    }


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Galat seekha alias hatao."""
    a = db.get(ProductAlias, alias_id)
    if not a or a.tenant_id != user.tenant_id:
        raise HTTPException(404, "Alias not found")
    db.delete(a)
    db.commit()
    return {"ok": True}


def _get_customer(db: Session, user: User, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if not c or c.tenant_id != user.tenant_id:
        raise HTTPException(404, "Customer not found")
    return c
