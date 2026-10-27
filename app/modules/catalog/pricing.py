"""
Customer-wise price (pantry).

Product ka normal price 10 hai, par is customer ko 9.50 mein dena hai (hamesha ya kisi date tak):
    customer_prices row: price=9.50, starts_on=None, expires_on=2026-12-31

    price_for(db, customer_id, product)   -> 9.50 (aaj valid hai to) warna product.price
    price_map(db, customer_id)           -> {product_id: 9.50, ...}  (sirf abhi valid special prices)

Order approve, manual order, order review aur manual invoice - sab yahi use karte hain.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CustomerPrice, Product


def is_valid(row: CustomerPrice, today: date | None = None) -> bool:
    """Special price aaj lagega?"""
    today = today or date.today()
    if row.price is None:
        return False
    if row.starts_on and row.starts_on > today:
        return False
    if row.expires_on and row.expires_on < today:
        return False
    return True


def price_status(row: CustomerPrice, today: date | None = None) -> str:
    """UI badge: none (sirf pantry) | active | scheduled | expired"""
    today = today or date.today()
    if row.price is None:
        return "none"
    if row.starts_on and row.starts_on > today:
        return "scheduled"
    if row.expires_on and row.expires_on < today:
        return "expired"
    return "active"


def price_map(db: Session, customer_id: int | None, today: date | None = None) -> dict[int, object]:
    if not customer_id:
        return {}
    rows = db.scalars(select(CustomerPrice).where(CustomerPrice.customer_id == customer_id))
    return {r.product_id: r.price for r in rows if is_valid(r, today)}


def price_for(db: Session, customer_id: int | None, product: Product | None, today: date | None = None):
    if not product:
        return None
    if customer_id:
        row = db.scalars(select(CustomerPrice).where(CustomerPrice.customer_id == customer_id,
                                                     CustomerPrice.product_id == product.id)).first()
        if row and is_valid(row, today):
            return row.price
    return product.price
