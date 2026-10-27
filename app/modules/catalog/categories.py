"""
Product categories - 2 level: parent (Fruit) -> sub-category (Citrus). Product ki ek category.

  GET    /categories                       saari (tree order), product count ke saath
  GET    /categories/export.csv            CSV download - wahi columns jo import leta hai (wapas import ho sake)
  POST   /categories                       {name, parent_id?, description?, sort_order?, is_active?}
  PUT    /categories/{id}
  DELETE /categories/{id}                  sub-categories ho to mana; products "Uncategorised" ho jaate hain
  POST   /products/bulk-category           {product_ids: [..], category_id: 5 | null}

Import / CSV: "Fruit > Citrus" likho - category (aur sub) apne aap ban jaati hai (find_or_create_path).
"""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity
from app.core.text import content_disposition
from app.models import Category, Product, User

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")

SEP = " › "


class CategoryIn(BaseModel):
    name: str
    parent_id: int | None = None
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True


class BulkCategoryIn(BaseModel):
    product_ids: list[int]
    category_id: int | None = None


def category_path(c: Category | None) -> str | None:
    """'Fruit › Citrus' (sub) ya 'Fruit' (parent)."""
    if c is None:
        return None
    return f"{c.parent.name}{SEP}{c.name}" if c.parent else c.name


def descendant_ids(db: Session, tenant_id: int, category_id: int) -> list[int]:
    """Parent chuna to uski sub-categories bhi (filter / report ke liye)."""
    subs = db.scalars(select(Category.id).where(Category.tenant_id == tenant_id, Category.parent_id == category_id))
    return [category_id, *subs]


def _all(db: Session, tenant_id: int) -> list[Category]:
    return list(db.scalars(select(Category).where(Category.tenant_id == tenant_id)
                           .order_by(Category.sort_order, Category.name)))


def tree_order(cats: list[Category]) -> list[Category]:
    """Parent, uske neeche uski subs, phir agla parent."""
    parents = [c for c in cats if c.parent_id is None]
    out = []
    for p in parents:
        out.append(p)
        out += [c for c in cats if c.parent_id == p.id]
    return out


def category_out(c: Category, counts: dict[int, int]) -> dict:
    return {"id": c.id, "name": c.name, "parent_id": c.parent_id, "path": category_path(c),
            "description": c.description, "sort_order": c.sort_order, "is_active": c.is_active,
            "product_count": counts.get(c.id, 0)}


def _counts(db: Session, tenant_id: int) -> dict[int, int]:
    rows = db.execute(select(Product.category_id, func.count()).where(
        Product.tenant_id == tenant_id, Product.deleted_at.is_(None), Product.category_id.is_not(None))
        .group_by(Product.category_id))
    return dict(rows.all())


def find_or_create_path(db: Session, tenant_id: int, path: str | None) -> int | None:
    """'Fruit > Citrus' / 'Fruit / Citrus' / 'Fruit › Citrus' / 'Fruit' -> category id (nahi hai to banao)."""
    if not path or not str(path).strip():
        return None
    import re
    parts = [p.strip()[:150] for p in re.split(r"\s*(?:>|›|/|\\)\s*", str(path)) if p.strip()][:2]
    if not parts:
        return None
    parent_id = None
    for name in parts:
        same_parent = Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
        c = db.scalars(select(Category).where(Category.tenant_id == tenant_id, same_parent,
                                              func.lower(Category.name) == name.lower())).first()
        if not c:
            c = Category(tenant_id=tenant_id, name=name, parent_id=parent_id)
            db.add(c)
            db.flush()
        parent_id = c.id
    return parent_id


def get_category(db: Session, tenant_id: int, category_id: int | None) -> Category | None:
    if category_id is None:
        return None
    c = db.get(Category, category_id)
    if not c or c.tenant_id != tenant_id:
        raise HTTPException(400, "Category not found")
    return c


def _validate(db: Session, user: User, data: CategoryIn, current: Category | None):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "Category name is required")
    parent = get_category(db, user.tenant_id, data.parent_id)
    if parent:
        if parent.parent_id is not None:
            raise HTTPException(400, "Sub-categories can't have their own sub-categories (2 levels only)")
        if current and parent.id == current.id:
            raise HTTPException(400, "A category can't be its own parent")
        if current and db.scalar(select(func.count()).select_from(Category).where(Category.parent_id == current.id)):
            raise HTTPException(400, "This category has sub-categories, so it can't be moved under another category")
    same = select(Category).where(Category.tenant_id == user.tenant_id, func.lower(Category.name) == name.lower(),
                                  Category.parent_id.is_(None) if parent is None else Category.parent_id == parent.id)
    if current:
        same = same.where(Category.id != current.id)
    if db.scalars(same).first():
        raise HTTPException(400, f'"{name}" already exists here')
    return name[:150]


@router.get("/categories")
def list_categories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    counts = _counts(db, user.tenant_id)
    return [category_out(c, counts) for c in tree_order(_all(db, user.tenant_id))]


@router.get("/categories/export.csv")
def export_categories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Columns import jaise hi (name, parent, description, sort_order, is_active) - Excel mein edit karke wapas import."""
    counts = _counts(db, user.tenant_id)
    rows = tree_order(_all(db, user.tenant_id))
    names = {c.id: c.name for c in rows}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "parent", "description", "sort_order", "is_active", "products"])
    for c in rows:
        w.writerow([c.name, names.get(c.parent_id, "") if c.parent_id else "", c.description or "", c.sort_order or 0,
                    "yes" if c.is_active else "no", category_out(c, counts)["product_count"]])
    # BOM = Excel UTF-8 sahi padhta hai (é, 中文)
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": content_disposition("categories.csv")})


@router.post("/categories")
def create_category(data: CategoryIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    name = _validate(db, user, data, None)
    c = Category(tenant_id=user.tenant_id, name=name, parent_id=data.parent_id, description=data.description,
                 sort_order=data.sort_order, is_active=data.is_active)
    db.add(c)
    db.commit()
    db.refresh(c)
    return category_out(c, {})


@router.put("/categories/{category_id}")
def update_category(category_id: int, data: CategoryIn, user: User = Depends(admin_only),
                    db: Session = Depends(get_db)):
    c = get_category(db, user.tenant_id, category_id)
    c.name = _validate(db, user, data, c)
    c.parent_id, c.description = data.parent_id, data.description
    c.sort_order, c.is_active = data.sort_order, data.is_active
    db.commit()
    db.refresh(c)
    return category_out(c, _counts(db, user.tenant_id))


@router.delete("/categories/{category_id}")
def delete_category(category_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = get_category(db, user.tenant_id, category_id)
    if db.scalar(select(func.count()).select_from(Category).where(Category.parent_id == c.id)):
        raise HTTPException(400, "Delete or move its sub-categories first")
    moved = db.execute(update(Product).where(Product.tenant_id == user.tenant_id, Product.category_id == c.id)
                       .values(category_id=None)).rowcount
    db.delete(c)
    db.commit()
    return {"ok": True, "products_uncategorised": moved}


@router.post("/products/bulk-category")
def bulk_category(data: BulkCategoryIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = get_category(db, user.tenant_id, data.category_id)
    if not data.product_ids:
        raise HTTPException(400, "Select at least one product")
    from app.core.audit import no_audit
    with no_audit(db):   # ek summary log, har product ka alag nahi
        n = db.execute(update(Product).where(Product.tenant_id == user.tenant_id, Product.id.in_(data.product_ids))
                       .values(category_id=c.id if c else None)).rowcount
        db.flush()
    log_activity(db, user.tenant_id, user.id, "product.bulk_category", "category", c.id if c else None,
                 {"name": category_path(c) or "Uncategorised", "products": n})
    db.commit()
    return {"updated": n, "category": category_path(c)}
