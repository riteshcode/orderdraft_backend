"""
Customer portal - shop (customer ka staff products dekhe, cart, order, apne orders, profile).

Access (Settings -> Customer portal):
  - portal band               -> 403 "Online ordering isn't open yet"
  - private                   -> sab kuch login ke baad
  - public                    -> products bina login dikhte hain (price: "show prices to guests" setting),
                                 order ke liye login zaroori
Price: login hai to customer ka pantry special price (valid ho to), warna product ka normal price.

  GET  /portal/{slug}/catalog/home              banners, categories, (login) aapke usual products
  GET  /portal/{slug}/catalog/categories
  GET  /portal/{slug}/catalog/products?q=&category_id=&page=
  GET  /portal/{slug}/catalog/products/{id}
  POST /portal/{slug}/cart/quote                {items: [{product_id, quantity}]} -> current prices + totals
  GET  /portal/{slug}/delivery-dates            (login) checkout ki dates (delivery days / cut-off) - delivery.py
  POST /portal/{slug}/orders                    (login) {items, delivery_date, po_number, notes, payment_method}
  GET  /portal/{slug}/orders                    (login) is customer ke orders (har channel ke)
  GET  /portal/{slug}/orders/{id}
  GET  /portal/{slug}/profile   PUT /portal/{slug}/profile   (login) naam, phone, delivery address
"""
import html
from dataclasses import dataclass
from datetime import date, datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.helpers import get_tenant_settings, log_activity, money, to_float
from app.core.mailer import MailError, send_email, sender_for, simple_html
from app.core.request_log import log_event
from app.modules.portal.delivery import check_delivery_date, delivery_out
from app.core.security import read_token
from app.models import (Category, Customer, CustomerLogin, CustomerProductStat, CustomerPrice, DraftOrder,
                        DraftOrderLine, PortalBanner, Product, Tenant, TenantSettings)
from app.modules.catalog.categories import category_path, descendant_ids
from app.modules.catalog.images import image_out, media_url, sorted_images
from app.modules.catalog.pricing import price_map
from app.modules.portal.auth import _tenant

router = APIRouter(prefix="/portal", tags=["portal-shop"])
bearer = HTTPBearer(auto_error=False)

MAX_LINES = 200
MAX_QTY = 100000


@dataclass
class Ctx:
    tenant: Tenant
    cfg: TenantSettings
    login: CustomerLogin | None
    impersonator_id: int | None = None      # staff "login as" (support) - customer ka password nahi chahiye

    @property
    def customer_id(self) -> int | None:
        return self.login.customer_id if self.login else None

    @property
    def show_prices(self) -> bool:
        return bool(self.login) or self.cfg.portal_show_prices_public


def _login_from_token(db: Session, tenant: Tenant, creds: HTTPAuthorizationCredentials | None) -> CustomerLogin | None:
    if not creds:
        return None
    try:
        payload = read_token(creds.credentials, "portal")
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session has expired - please log in again")
    if int(payload.get("tid", 0)) != tenant.id:
        raise HTTPException(401, "Please log in again")
    db.info["portal_impersonator"] = payload.get("imp")
    login = db.get(CustomerLogin, int(payload["sub"]))
    if not login or not login.is_active or login.customer.deleted_at or not login.customer.is_active:
        raise HTTPException(401, "This login is disabled - please contact your supplier")
    return login


def portal_ctx(slug: str, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
               db: Session = Depends(get_db)) -> Ctx:
    """Har shop endpoint: client dhundo, login (optional), portal on / private check."""
    tenant = _tenant(db, slug)
    login = _login_from_token(db, tenant, creds)
    cfg = get_tenant_settings(db, tenant.id)
    if not cfg.portal_enabled:
        raise HTTPException(403, "Online ordering isn't open yet - please contact us to order")
    if cfg.portal_access != "public" and not login:
        raise HTTPException(401, "Please log in to see products and prices")
    return Ctx(tenant, cfg, login, db.info.get("portal_impersonator") if login else None)


def login_ctx(ctx: Ctx = Depends(portal_ctx)) -> Ctx:
    if not ctx.login:
        raise HTTPException(401, "Please log in to place an order")
    if ctx.login.must_change_password and not ctx.impersonator_id:
        raise HTTPException(403, "Please set your own password first")
    return ctx


# ---------------- helpers ----------------
def _visible_products(ctx: Ctx):
    stmt = select(Product).where(Product.tenant_id == ctx.tenant.id, Product.deleted_at.is_(None),
                                 Product.is_active.is_(True), Product.is_sold.is_(True))
    if ctx.cfg.portal_hide_out_of_stock:
        stmt = stmt.where(or_(Product.is_tracked_inventory.is_(False), Product.quantity_on_hand > 0))
    return stmt


def _load(stmt):
    return stmt.options(selectinload(Product.images), selectinload(Product.category).selectinload(Category.parent))


def product_card(p: Product, ctx: Ctx, specials: dict, full: bool = False) -> dict:
    special = specials.get(p.id)
    price = special if special is not None else p.price
    imgs = sorted_images(p)
    out = {
        "id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit,
        "category_id": p.category_id, "category": category_path(p.category),
        "image_url": image_out(imgs[0])["thumb_url"] if imgs else None,
        "price": to_float(price) if ctx.show_prices else None,
        "regular_price": to_float(p.price) if ctx.show_prices and special is not None else None,
        "special": special is not None,
        "taxable": p.is_taxable,
        # None = stock track nahi hota; False = khatam (order phir bhi ho sakta hai)
        "in_stock": (p.quantity_on_hand or 0) > 0 if p.is_tracked_inventory else None,
    }
    if full:
        out["description"] = p.description
        out["images"] = [image_out(i) for i in imgs]
    return out


def _specials(db: Session, ctx: Ctx) -> dict:
    return price_map(db, ctx.customer_id) if ctx.customer_id else {}


def _today(ctx: Ctx) -> date:
    from app.modules.reports.router import _tenant_tz
    return datetime.now(timezone.utc).astimezone(_tenant_tz(ctx.tenant)).date()


def _banners(db: Session, ctx: Ctx) -> list[dict]:
    today = _today(ctx)
    rows = db.scalars(select(PortalBanner).where(
        PortalBanner.tenant_id == ctx.tenant.id, PortalBanner.is_active.is_(True),
        or_(PortalBanner.starts_on.is_(None), PortalBanner.starts_on <= today),
        or_(PortalBanner.ends_on.is_(None), PortalBanner.ends_on >= today))
        .order_by(PortalBanner.sort_order, PortalBanner.id))
    return [{"id": b.id, "title": b.title, "subtitle": b.subtitle, "button_text": b.button_text,
             "image_url": media_url(b.tenant_id, b.image_name), "link_type": b.link_type,
             "product_id": b.link_product_id, "category_id": b.link_category_id, "url": b.link_url}
            for b in rows if b.image_name or b.title]


def _categories(db: Session, ctx: Ctx) -> list[dict]:
    """Sirf active categories jinme kam se kam ek dikhne wala product hai (parent ke count mein subs bhi)."""
    visible_ids = _visible_products(ctx).with_only_columns(Product.id)
    counts = dict(db.execute(select(Product.category_id, func.count()).where(Product.id.in_(visible_ids))
                             .group_by(Product.category_id)).all())
    cats = list(db.scalars(select(Category).where(Category.tenant_id == ctx.tenant.id, Category.is_active.is_(True))
                           .order_by(Category.sort_order, Category.name)))
    # Har category ki ek photo (pehle product ki) - category tiles ke liye
    images: dict[int, str] = {}
    for p in db.scalars(_load(_visible_products(ctx).where(Product.category_id.is_not(None)).order_by(Product.name))):
        imgs = sorted_images(p)
        if imgs:
            top = p.category.parent_id or p.category_id if p.category else None
            for key in (p.category_id, top):
                if key and key not in images:
                    images[key] = image_out(imgs[0])["thumb_url"]
    out = []
    for c in [c for c in cats if c.parent_id is None]:
        subs = [s for s in cats if s.parent_id == c.id]
        sub_out = [{"id": s.id, "name": s.name, "product_count": counts.get(s.id, 0)} for s in subs if counts.get(s.id)]
        total = counts.get(c.id, 0) + sum(s["product_count"] for s in sub_out)
        if total:
            out.append({"id": c.id, "name": c.name, "description": c.description, "product_count": total,
                        "image_url": images.get(c.id), "children": sub_out})
    return out


# ---------------- catalog ----------------
@router.get("/{slug}/catalog/home")
def home(ctx: Ctx = Depends(portal_ctx), db: Session = Depends(get_db)):
    specials = _specials(db, ctx)
    usual = []
    if ctx.customer_id:
        # Aapke usual: pantry + jo sabse zyada order kiye (approved orders ki history)
        pantry_ids = list(db.scalars(select(CustomerPrice.product_id).where(CustomerPrice.customer_id == ctx.customer_id)))
        top_ids = list(db.scalars(select(CustomerProductStat.product_id)
                                  .where(CustomerProductStat.customer_id == ctx.customer_id)
                                  .order_by(CustomerProductStat.times_ordered.desc()).limit(24)))
        ids = list(dict.fromkeys(top_ids + pantry_ids))[:24]
        if ids:
            rows = {p.id: p for p in db.scalars(_load(_visible_products(ctx).where(Product.id.in_(ids))))}
            usual = [product_card(rows[i], ctx, specials) for i in ids if i in rows]
    return {"banners": _banners(db, ctx), "categories": _categories(db, ctx), "usual": usual}


@router.get("/{slug}/catalog/categories")
def categories(ctx: Ctx = Depends(portal_ctx), db: Session = Depends(get_db)):
    return _categories(db, ctx)


@router.get("/{slug}/catalog/products")
def products(q: str = "", category_id: int | None = None, page: int = Query(1, ge=1),
             page_size: int = Query(24, ge=1, le=100), ctx: Ctx = Depends(portal_ctx), db: Session = Depends(get_db)):
    stmt = _visible_products(ctx)
    if q.strip():
        from app.modules.catalog.barcodes import search_filter
        stmt = stmt.where(search_filter(q.strip()))
    if category_id:
        stmt = stmt.where(Product.category_id.in_(descendant_ids(db, ctx.tenant.id, category_id)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(_load(stmt).order_by(Product.name, Product.id).offset((page - 1) * page_size).limit(page_size))
    specials = _specials(db, ctx)
    cat = db.get(Category, category_id) if category_id else None
    return {"items": [product_card(p, ctx, specials) for p in rows], "total": total, "page": page,
            "page_size": page_size,
            "category": {"id": cat.id, "name": cat.name, "path": category_path(cat), "description": cat.description,
                         "parent_id": cat.parent_id} if cat and cat.tenant_id == ctx.tenant.id else None}


@router.get("/{slug}/catalog/products/{product_id}")
def product(product_id: int, ctx: Ctx = Depends(portal_ctx), db: Session = Depends(get_db)):
    p = db.scalars(_load(_visible_products(ctx).where(Product.id == product_id))).first()
    if not p:
        raise HTTPException(404, "This product isn't available")
    return product_card(p, ctx, _specials(db, ctx), full=True)


# ---------------- cart / order ----------------
class CartItem(BaseModel):
    product_id: int
    quantity: float


class QuoteIn(BaseModel):
    items: list[CartItem]


class OrderIn(QuoteIn):
    delivery_date: date | None = None
    po_number: str | None = None
    notes: str | None = None
    payment_method: str


def quote(db: Session, ctx: Ctx, items: list[CartItem], strict: bool) -> dict:
    """
    Cart ki current prices + totals (server pe - browser ke price pe bharosa nahi).
    strict=True (order place): koi bhi galat line = error. False (cart dekhna): galat line hata ke batao.
    """
    if len(items) > MAX_LINES:
        raise HTTPException(400, f"Too many lines (max {MAX_LINES})")
    merged: dict[int, float] = {}
    for it in items:                      # same product do baar = ek line
        merged[it.product_id] = merged.get(it.product_id, 0) + it.quantity
    products = {p.id: p for p in db.scalars(_load(_visible_products(ctx).where(Product.id.in_(list(merged)))))}
    specials = _specials(db, ctx)
    gst_rate, inclusive = ctx.cfg.gst_rate or 0, ctx.cfg.prices_include_gst
    lines, removed = [], []
    subtotal, gst = money(0), money(0)
    for pid, qty in merged.items():
        p = products.get(pid)
        if not p or qty <= 0 or qty > MAX_QTY:
            if strict:
                raise HTTPException(400, "Some items in your cart are no longer available - please check your cart")
            removed.append(pid)
            continue
        price = specials.get(p.id, p.price)
        amount = money(price) * money(qty) if price is not None else money(0)
        line_gst = money(0)
        if p.is_taxable and price is not None:
            line_gst = money(amount * money(gst_rate) / (money(100) + money(gst_rate))) if inclusive \
                else money(amount * money(gst_rate) / money(100))
        subtotal += amount
        gst += line_gst
        lines.append({**product_card(p, ctx, specials), "quantity": qty,
                      "line_total": to_float(amount) if ctx.show_prices else None, "_price": price})
    total = subtotal if inclusive else subtotal + gst
    min_order = ctx.cfg.portal_min_order
    return {"lines": lines, "removed": removed,
            "subtotal": to_float(subtotal) if ctx.show_prices else None,
            "gst": to_float(gst) if ctx.show_prices else None,
            "total": to_float(total) if ctx.show_prices else None,
            "prices_include_gst": inclusive, "gst_rate": gst_rate,
            "min_order": min_order, "below_min": bool(min_order and float(subtotal) < min_order)}


def _public_quote(q: dict) -> dict:
    return {**q, "lines": [{k: v for k, v in l.items() if k != "_price"} for l in q["lines"]]}


@router.post("/{slug}/cart/quote")
def cart_quote(data: QuoteIn, ctx: Ctx = Depends(portal_ctx), db: Session = Depends(get_db)):
    return _public_quote(quote(db, ctx, data.items, strict=False))


def payment_options(cfg: TenantSettings) -> list[dict]:
    out = []
    if cfg.portal_allow_cod:
        out.append({"key": "cod", "label": "Cash on delivery", "hint": "Pay when your order is delivered"})
    if cfg.portal_allow_account:
        out.append({"key": "account", "label": "On account",
                    "hint": f"We'll invoice you - payment due in {cfg.payment_terms_days} days"})
    return out


PAYMENT_LABEL = {"cod": "Cash on delivery", "account": "On account"}


@router.post("/{slug}/orders")
def place_order(data: OrderIn, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    from app.modules.billing.limits import subscription_ok
    ok, _ = subscription_ok(ctx.tenant)
    if not ok:
        raise HTTPException(503, "Online ordering is temporarily unavailable - please contact us to order")
    if data.payment_method not in [p["key"] for p in payment_options(ctx.cfg)]:
        raise HTTPException(400, "Choose a payment option")
    if not data.items:
        raise HTTPException(400, "Your cart is empty")
    error = check_delivery_date(ctx.cfg, ctx.tenant, data.delivery_date)    # delivery days / cut-off
    if error:
        raise HTTPException(400, error)
    q = quote(db, ctx, data.items, strict=True)
    if q["below_min"]:
        raise HTTPException(400, f"The minimum order is ${ctx.cfg.portal_min_order:,.2f} - please add a few more items")

    customer = ctx.login.customer
    order = DraftOrder(
        tenant_id=ctx.tenant.id, customer_id=customer.id, customer_raw=customer.name, customer_confidence=100,
        po_number=(data.po_number or "").strip()[:100] or None, delivery_date=data.delivery_date,
        notes=(data.notes or "").strip()[:2000] or None, status="needs_review", overall_confidence=100,
        source="portal", payment_method=data.payment_method, portal_login_id=ctx.login.id,
        created_by_id=ctx.impersonator_id,      # staff ne customer ki taraf se order kiya (login as)
        total_amount=money(q["subtotal"]) if q["subtotal"] is not None else None,   # list mein value dikhe (approve pe dobara)
    )
    # Price lock: customer ne jo price dekha wahi order pe (approve pe badlega nahi)
    order.lines = [DraftOrderLine(
        raw_text=f"{l['quantity']:g} x {l['name']}"[:500], quantity=l["quantity"], unit=l["unit"],
        product_id=l["id"], suggested_product_id=l["id"], match_confidence=100, match_method="portal",
        candidates=[], unit_price=money(l["_price"]) if l["_price"] is not None else None,
    ) for l in q["lines"]]
    db.add(order)
    db.flush()
    log_activity(db, ctx.tenant.id, ctx.impersonator_id, "order.created_portal", "order", order.id,
                 {"lines": len(order.lines), "by": ctx.login.email, "payment": data.payment_method,
                  "total": q["total"], **({"placed_by_staff": ctx.impersonator_id} if ctx.impersonator_id else {})})
    db.commit()
    _send_order_emails(db, ctx, order, q)
    # Staff ko bell + browser push
    from app.modules.notifications.staff import notify_staff
    total = f" · ${q['total']:,.2f}" if q["total"] is not None else ""
    notify_staff(db, ctx.tenant.id, "portal_order", f"New online order #{order.id} from {customer.name}",
                 f"{len(order.lines)} item{'s' if len(order.lines) != 1 else ''}{total}"
                 + (f" · delivery {order.delivery_date:%a %d %b}" if order.delivery_date else ""),
                 f"/orders/{order.id}", order.id)
    db.commit()
    return {"id": order.id, "status": order.status, "total": q["total"]}


def _lines_html(q: dict) -> str:
    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:4px 8px 4px 0">{e(l["name"])}</td>'
        f'<td style="padding:4px 8px;text-align:right">{l["quantity"]:g} {e(l["unit"] or "")}</td>'
        f'<td style="padding:4px 0 4px 8px;text-align:right">'
        f'{"$%.2f" % l["line_total"] if l["line_total"] is not None else ""}</td></tr>' for l in q["lines"])
    total = f'<tr><td colspan="2" style="padding-top:8px"><b>Total</b></td><td style="text-align:right;padding-top:8px">' \
            f'<b>${q["total"]:,.2f}</b></td></tr>' if q["total"] is not None else ""
    return f'<table style="border-collapse:collapse;width:100%;font-size:14px">{rows}{total}</table>'


def _send_order_emails(db: Session, ctx: Ctx, order: DraftOrder, q: dict):
    """Customer ko "order mila" + staff ko "naya portal order" (fail ho to order phir bhi save)."""
    from app.core.config import settings
    e = html.escape
    cfg, login = ctx.cfg, ctx.login
    business = cfg.business_name or ctx.tenant.name
    sender, reply_to = sender_for(cfg)
    details = []
    if order.delivery_date:
        details.append(f"Delivery date: {order.delivery_date:%d/%m/%Y}")
    if order.po_number:
        details.append(f"Your PO: {e(order.po_number)}")
    details.append(f"Payment: {PAYMENT_LABEL.get(order.payment_method, order.payment_method)}")
    body = (f"<p>Hi {e(login.name or login.customer.name)},</p>"
            f"<p>Thanks - we've received your order <b>#{order.id}</b>. We'll confirm it shortly.</p>"
            f"<p>{'<br/>'.join(details)}</p>{_lines_html(q)}")
    try:
        send_email(login.email, f"Order #{order.id} received - {business}",
                   simple_html("Order received", body, "View your orders",
                               f"{settings.FRONTEND_URL}/portal/{ctx.tenant.slug}/orders"),
                   from_email=sender, reply_to=reply_to)
    except MailError as err:
        log_event("portal_order_email_failed", tenant_id=ctx.tenant.id, order_id=order.id, error=str(err)[:200])
    staff = cfg.portal_notify_email or cfg.email
    if staff:
        try:
            send_email(staff, f"New online order #{order.id} from {login.customer.name}",
                       simple_html("New online order",
                                   f"<p><b>{e(login.customer.name)}</b> ({e(login.email)}) placed order "
                                   f"<b>#{order.id}</b> on your ordering portal.</p><p>{'<br/>'.join(details)}</p>"
                                   f"{_lines_html(q)}" + (f"<p>Notes: {e(order.notes)}</p>" if order.notes else ""),
                                   "Review order", f"{settings.FRONTEND_URL}/orders/{order.id}"))
        except MailError as err:
            log_event("portal_staff_email_failed", tenant_id=ctx.tenant.id, order_id=order.id, error=str(err)[:200])


@router.get("/{slug}/delivery-dates")
def delivery_dates(ctx: Ctx = Depends(login_ctx)):
    """Checkout ke liye: kaunsi dates chun sakte ho (delivery days, cut-off, chhutti ke hisaab se)."""
    return delivery_out(ctx.cfg, ctx.tenant)


STATUS_LABEL = {"needs_review": "Received", "approved": "Confirmed", "exported": "Confirmed", "rejected": "Not accepted",
                "cancelled": "Cancelled"}


def order_out(o: DraftOrder, ctx: Ctx, full: bool = False) -> dict:
    total = o.total_amount
    if total is None:   # abhi approve nahi hua - line prices se
        total = sum((money(l.unit_price) * money(l.quantity) for l in o.lines if l.unit_price is not None), money(0))
    out = {"id": o.id, "status": o.status, "status_label": STATUS_LABEL.get(o.status, o.status),
           "created_at": o.created_at, "delivery_date": o.delivery_date, "po_number": o.po_number,
           "payment_method": o.payment_method, "source": o.source, "line_count": len(o.lines),
           "total": to_float(total)}
    if full:
        out["notes"] = o.notes
        out["lines"] = [{"product_id": l.product_id, "name": l.product.name if l.product else l.raw_text,
                         "sku": l.product.sku if l.product else None, "quantity": l.quantity,
                         "unit": l.unit, "unit_price": to_float(l.unit_price),
                         "line_total": to_float(money(l.unit_price) * money(l.quantity)) if l.unit_price is not None
                         else None} for l in o.lines]
    return out


@router.get("/{slug}/orders")
def my_orders(page: int = Query(1, ge=1), ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    stmt = select(DraftOrder).where(DraftOrder.tenant_id == ctx.tenant.id, DraftOrder.customer_id == ctx.customer_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.options(selectinload(DraftOrder.lines)).order_by(DraftOrder.created_at.desc(),
                                                                            DraftOrder.id.desc())
                      .offset((page - 1) * 20).limit(20))
    return {"items": [order_out(o, ctx) for o in rows], "total": total, "page": page, "page_size": 20}


@router.get("/{slug}/orders/{order_id}")
def my_order(order_id: int, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    o = db.scalars(select(DraftOrder).where(DraftOrder.id == order_id, DraftOrder.tenant_id == ctx.tenant.id,
                                            DraftOrder.customer_id == ctx.customer_id)
                   .options(selectinload(DraftOrder.lines).selectinload(DraftOrderLine.product))).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return order_out(o, ctx, full=True)


# ---------------- profile ----------------
class ProfileIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    delivery_line1: str | None = None
    delivery_line2: str | None = None
    delivery_city: str | None = None
    delivery_region: str | None = None
    delivery_postcode: str | None = None
    delivery_instructions: str | None = None


ADDRESS_FIELDS = ("phone", "delivery_line1", "delivery_line2", "delivery_city", "delivery_region",
                  "delivery_postcode", "delivery_instructions")


def profile_out(ctx: Ctx) -> dict:
    c = ctx.login.customer
    return {"name": ctx.login.name, "email": ctx.login.email, "customer": c.name,
            **{k: getattr(c, k) for k in ADDRESS_FIELDS},
            "payment_options": payment_options(ctx.cfg)}


@router.get("/{slug}/profile")
def get_profile(ctx: Ctx = Depends(login_ctx)):
    return profile_out(ctx)


@router.put("/{slug}/profile")
def save_profile(data: ProfileIn, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    from app.modules.imports.service import display_address
    db.info["portal_actor"] = ctx.login.email     # audit: "changed by portal user ..."
    ctx.login.name = (data.name or "").strip()[:200] or None
    c: Customer = ctx.login.customer
    for k in ADDRESS_FIELDS:
        v = (getattr(data, k) or "").strip()
        setattr(c, k, (v[:2000] if k == "delivery_instructions" else v[:255]) or None)
    c.address = display_address(c) or c.address
    db.commit()
    return profile_out(ctx)
