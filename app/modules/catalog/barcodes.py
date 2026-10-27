"""
Product barcode + QR code (aage scanner / POS ke liye base).

Har product ka ek `barcode` hota hai:
  - manufacturer ka EAN-13 / UPC-A / EAN-8 (box pe chhapa hua) - product form mein daalo
  - khali ho to hum internal EAN-13 bana dete hain:  "20" + product id (10 digit) + check digit
    (GS1 mein 20-29 prefix "in-store use" ke liye hai - kisi asli product se clash nahi karta)
  - koi bhi aur code (letters wala) = Code128

QR code mein wahi barcode value hoti hai - scanner kuch bhi padhe, lookup same.

  GET  /products/lookup?code=...            scan -> product (barcode, phir SKU)
  GET  /products/{id}/codes                 {barcode, symbology, barcode_svg, qr_svg}
  POST /products/labels        {ids: [...]} label print ke liye sab ek saath
  POST /products/{id}/barcode/regenerate    internal barcode wapas
  POST /products/barcodes/generate-missing  jinke paas barcode nahi unko
"""
import re
from io import BytesIO

import barcode as pybarcode
import qrcode
import qrcode.image.svg
from barcode.writer import SVGWriter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import to_float
from app.models import Product, User

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")

CODE128_RE = re.compile(r"^[\x20-\x7e]{1,48}$")   # printable ASCII


class LabelsIn(BaseModel):
    ids: list[int]


# ---------------- helpers ----------------
def check_digit(body: str) -> str:
    """EAN/UPC check digit (body = 7, 11 ya 12 digits)."""
    # right se chalo: pehla digit x3, doosra x1, teesra x3 ...
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def internal_ean13(product_id: int) -> str:
    body = "20" + f"{product_id:010d}"
    return body + check_digit(body)


def symbology(code: str) -> str:
    """Kaunsa barcode type: ean13 | upca | ean8 | code128"""
    if code.isdigit():
        if len(code) == 13 and check_digit(code[:12]) == code[12]:
            return "ean13"
        if len(code) == 12 and check_digit(code[:11]) == code[11]:
            return "upca"
        if len(code) == 8 and check_digit(code[:7]) == code[7]:
            return "ean8"
    return "code128"


def clean_barcode(value: str | None) -> str | None:
    """Form / import se aaya barcode. Galat ho to ValueError."""
    code = (value or "").strip()
    if not code:
        return None
    if code.isdigit() and len(code) in (8, 12, 13) and symbology(code) == "code128":
        raise ValueError(f"Barcode {code} has an invalid check digit (EAN/UPC)")
    if not CODE128_RE.match(code):
        raise ValueError("Barcodes can only contain letters, numbers and simple symbols (max 48 characters)")
    return code


def barcode_taken(db: Session, tenant_id: int, code: str, exclude_id: int | None = None) -> bool:
    stmt = select(Product.id).where(Product.tenant_id == tenant_id, Product.barcode == code)
    if exclude_id:
        stmt = stmt.where(Product.id != exclude_id)
    return db.scalars(stmt).first() is not None


def assign_missing_barcodes(db: Session, tenant_id: int) -> int:
    """Naye products (create / CSV import / sync) ko internal barcode. Flush ke baad call karo (id chahiye)."""
    count = 0
    for p in db.scalars(select(Product).where(Product.tenant_id == tenant_id, Product.barcode.is_(None))):
        code = internal_ean13(p.id)
        if not barcode_taken(db, tenant_id, code, p.id):
            p.barcode = code
            count += 1
    db.flush()
    return count


def _strip_xml(svg: bytes | str) -> str:
    text = svg.decode() if isinstance(svg, bytes) else svg
    return text[text.index("<svg"):]


def barcode_svg(code: str) -> str:
    kind = symbology(code)
    value = code[:-1] if kind in ("ean13", "upca", "ean8") else code   # library check digit khud lagati hai
    obj = pybarcode.get(kind, value, writer=SVGWriter())
    return _strip_xml(obj.render(writer_options={"module_height": 14, "font_size": 8, "text_distance": 4, "quiet_zone": 3}))


def qr_svg(code: str) -> str:
    img = qrcode.make(code, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = BytesIO()
    img.save(buf)
    return _strip_xml(buf.getvalue())


def codes_out(p: Product) -> dict:
    code = p.barcode or internal_ean13(p.id)
    return {"id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "price": to_float(p.price), "barcode": code,
            "symbology": symbology(code), "barcode_svg": barcode_svg(code), "qr_svg": qr_svg(code)}


def _product(db: Session, user: User, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    return p


# ---------------- endpoints ----------------
@router.get("/products/lookup")
def lookup(code: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Scanner ne jo padha (barcode ya QR) -> product. Pehle barcode, phir SKU (case ignore)."""
    from app.modules.catalog.router import product_out

    code = code.strip()
    if not code:
        raise HTTPException(400, "Enter a code")
    base = select(Product).where(Product.tenant_id == user.tenant_id, Product.deleted_at.is_(None))
    p = db.scalars(base.where(Product.barcode == code)).first() or \
        db.scalars(base.where(func.lower(Product.sku) == code.lower())).first()
    if not p:
        raise HTTPException(404, f"No product found for this code: {code}")
    return product_out(p)


@router.get("/products/{product_id}/codes")
def product_codes(product_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return codes_out(_product(db, user, product_id))


@router.post("/products/labels")
def labels(data: LabelsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not data.ids or len(data.ids) > 500:
        raise HTTPException(400, "Choose between 1 and 500 products")
    rows = db.scalars(select(Product).where(Product.tenant_id == user.tenant_id, Product.id.in_(data.ids))).all()
    order = {pid: n for n, pid in enumerate(data.ids)}
    return [codes_out(p) for p in sorted(rows, key=lambda p: order[p.id])]


@router.post("/products/{product_id}/barcode/regenerate")
def regenerate(product_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    p = _product(db, user, product_id)
    code = internal_ean13(p.id)
    if barcode_taken(db, user.tenant_id, code, p.id):
        raise HTTPException(400, "This internal barcode is already used by another product")
    p.barcode = code
    db.commit()
    return codes_out(p)


@router.post("/products/barcodes/generate-missing")
def generate_missing(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    count = assign_missing_barcodes(db, user.tenant_id)
    db.commit()
    return {"generated": count}


def search_filter(q: str):
    """Product search mein barcode bhi (scanner se search box mein scan)."""
    return or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%"), Product.barcode == q.strip())
