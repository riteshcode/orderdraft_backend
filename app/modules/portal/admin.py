"""
Staff side: Settings -> Customer portal.

  GET  /settings/portal                  saari portal settings + portal URL
  PUT  /settings/portal                  {enabled, access, show_prices_public, allow_cod, allow_account, min_order,
                                          hide_out_of_stock, welcome_title, welcome_text, checkout_note, notify_email}
  GET    /portal-banners
  POST   /portal-banners                 {title, subtitle, button_text, link_type, link_product_id, link_category_id,
                                          link_url, is_active, starts_on, ends_on}
  PUT    /portal-banners/{id}
  POST   /portal-banners/{id}/image      multipart: file (JPG / PNG / WEBP, wide 1600x500 best)
  PUT    /portal-banners/order           {ids: [3, 1, 2]}
  DELETE /portal-banners/{id}

Banner images products wale folder mein hi (same /media URL, same resize).
"""
import re
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import get_tenant_settings
from app.models import Category, PortalBanner, Product, ProductImage, User
from app.modules.catalog.categories import category_path
from app.modules.catalog.images import MAX_BYTES, delete_files, media_url, process_upload
from app.core.webpush import push_enabled
from app.modules.portal.delivery import days_to_str, parse_days, parse_time
from app.modules.portal.service import portal_url

router = APIRouter(tags=["portal-admin"])
admin_only = require_role("owner", "admin")

LINK_TYPES = ("none", "product", "category", "url")


FONTS = ("modern", "classic", "rounded")
HEADER_STYLES = ("light", "brand", "dark")
CORNERS = ("rounded", "soft", "sharp")


class PortalSettingsIn(BaseModel):
    enabled: bool = False
    access: str = "private"
    show_prices_public: bool = True
    allow_cod: bool = True
    allow_account: bool = False
    min_order: float | None = None
    hide_out_of_stock: bool = False
    welcome_title: str | None = None
    welcome_text: str | None = None
    checkout_note: str | None = None
    notify_email: EmailStr | None = None
    # Delivery days / cut-off (delivery.py)
    delivery_days: list[int] | None = None       # 0 = Mon ... 6 = Sun
    cutoff_time: str | None = None               # "16:00"
    lead_days: int = 1
    closed_dates: list[date] | None = None
    allow_standing: bool = True                  # customer khud standing order bana sake
    # Branding
    brand_color: str | None = None
    header_style: str = "light"
    font: str = "modern"
    corners: str = "rounded"
    tagline: str | None = None
    announcement: str | None = None
    footer_text: str | None = None


class BannerIn(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    button_text: str | None = None
    link_type: str = "none"
    link_product_id: int | None = None
    link_category_id: int | None = None
    link_url: str | None = None
    is_active: bool = True
    starts_on: date | None = None
    ends_on: date | None = None


class OrderIn(BaseModel):
    ids: list[int]


def portal_settings_out(cfg, tenant) -> dict:
    return {"enabled": cfg.portal_enabled, "access": cfg.portal_access,
            "show_prices_public": cfg.portal_show_prices_public, "allow_cod": cfg.portal_allow_cod,
            "allow_account": cfg.portal_allow_account, "min_order": cfg.portal_min_order,
            "hide_out_of_stock": cfg.portal_hide_out_of_stock, "welcome_title": cfg.portal_welcome_title,
            "welcome_text": cfg.portal_welcome_text, "checkout_note": cfg.portal_checkout_note,
            "notify_email": cfg.portal_notify_email, "portal_url": portal_url(tenant),
            "payment_terms_days": cfg.payment_terms_days, **branding_out(cfg),
            "delivery_days": parse_days(cfg.portal_delivery_days), "cutoff_time": cfg.portal_cutoff_time,
            "lead_days": cfg.portal_lead_days if cfg.portal_lead_days is not None else 1,
            "closed_dates": sorted(cfg.portal_closed_dates or []), "allow_standing": cfg.portal_allow_standing,
            "push_configured": push_enabled()}


def branding_out(cfg) -> dict:
    """Portal ka look - staff settings aur customer portal (info) dono mein."""
    return {"brand_color": cfg.portal_brand_color, "header_style": cfg.portal_header_style or "light",
            "font": cfg.portal_font or "modern", "corners": cfg.portal_corners or "rounded",
            "tagline": cfg.portal_tagline, "announcement": cfg.portal_announcement,
            "footer_text": cfg.portal_footer_text,
            "logo_url": media_url(cfg.tenant_id, cfg.portal_logo), "icon_url": media_url(cfg.tenant_id, cfg.portal_icon)}


@router.get("/settings/portal")
def get_portal_settings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = get_tenant_settings(db, user.tenant_id)
    db.commit()
    return portal_settings_out(cfg, user.tenant)


@router.put("/settings/portal")
def save_portal_settings(data: PortalSettingsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Sirf jo fields bheji gayi wahi badlo (Setup aur Look & feel alag-alag save hote hain)."""
    sent = data.model_fields_set
    cfg = get_tenant_settings(db, user.tenant_id)

    def text(v, limit=None):
        v = (v or "").strip()
        return (v[:limit] if limit else v) or None

    if "access" in sent and data.access not in ("private", "public"):
        raise HTTPException(400, "Access must be private or public")
    if "min_order" in sent and data.min_order is not None and data.min_order < 0:
        raise HTTPException(400, "Minimum order can't be negative")
    color = (data.brand_color or "").strip()
    if "brand_color" in sent and color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(400, "Brand colour must look like #16a34a")
    cutoff = (data.cutoff_time or "").strip()
    if "cutoff_time" in sent and cutoff and not (re.fullmatch(r"\d{1,2}:\d{2}", cutoff) and parse_time(cutoff)):
        raise HTTPException(400, "Cut-off time must look like 16:00")
    if "lead_days" in sent and not 0 <= data.lead_days <= 30:
        raise HTTPException(400, "Days before delivery must be between 0 and 30")
    if (data.font not in FONTS or data.header_style not in HEADER_STYLES or data.corners not in CORNERS):
        raise HTTPException(400, "Invalid style option")

    values = {
        "enabled": ("portal_enabled", data.enabled), "access": ("portal_access", data.access),
        "show_prices_public": ("portal_show_prices_public", data.show_prices_public),
        "allow_cod": ("portal_allow_cod", data.allow_cod), "allow_account": ("portal_allow_account", data.allow_account),
        "min_order": ("portal_min_order", data.min_order or None),
        "hide_out_of_stock": ("portal_hide_out_of_stock", data.hide_out_of_stock),
        "welcome_title": ("portal_welcome_title", text(data.welcome_title, 200)),
        "welcome_text": ("portal_welcome_text", text(data.welcome_text)),
        "checkout_note": ("portal_checkout_note", text(data.checkout_note)),
        "notify_email": ("portal_notify_email", str(data.notify_email).lower() if data.notify_email else None),
        "brand_color": ("portal_brand_color", color.lower() or None),
        "header_style": ("portal_header_style", data.header_style), "font": ("portal_font", data.font),
        "corners": ("portal_corners", data.corners), "tagline": ("portal_tagline", text(data.tagline, 200)),
        "announcement": ("portal_announcement", text(data.announcement, 200)),
        "footer_text": ("portal_footer_text", text(data.footer_text, 2000)),
        "delivery_days": ("portal_delivery_days", days_to_str(data.delivery_days)),
        "cutoff_time": ("portal_cutoff_time", parse_time(cutoff).strftime("%H:%M") if cutoff else None),
        "lead_days": ("portal_lead_days", data.lead_days),
        "allow_standing": ("portal_allow_standing", data.allow_standing),
        "closed_dates": ("portal_closed_dates", sorted({d.isoformat() for d in data.closed_dates or []}) or None),
    }
    for key, (column, value) in values.items():
        if key in sent:
            setattr(cfg, column, value)
    if cfg.portal_enabled and not (cfg.portal_allow_cod or cfg.portal_allow_account):
        raise HTTPException(400, "Turn on at least one payment option (Cash on delivery or On account)")
    db.commit()
    return portal_settings_out(cfg, user.tenant)


# ---------------- logo / app icon ----------------
def _delete_media(tenant_id: int, name: str | None):
    if name:
        delete_files(ProductImage(tenant_id=tenant_id, file_name=name, thumb_name=name.replace(".", "_thumb.", 1)))


@router.post("/settings/portal/{kind}")
async def upload_brand_image(kind: str, file: UploadFile = File(...), user: User = Depends(admin_only),
                             db: Session = Depends(get_db)):
    """kind = logo (header mein) | icon (phone home screen app icon - square, 512x512 best)"""
    if kind not in ("logo", "icon"):
        raise HTTPException(404, "Not found")
    cfg = get_tenant_settings(db, user.tenant_id)
    saved = process_upload(user.tenant_id, await file.read(MAX_BYTES + 1), file.filename)
    field = f"portal_{kind}"
    _delete_media(user.tenant_id, getattr(cfg, field))
    setattr(cfg, field, saved["file_name"])
    db.commit()
    return portal_settings_out(cfg, user.tenant)


@router.delete("/settings/portal/{kind}")
def delete_brand_image(kind: str, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if kind not in ("logo", "icon"):
        raise HTTPException(404, "Not found")
    cfg = get_tenant_settings(db, user.tenant_id)
    _delete_media(user.tenant_id, getattr(cfg, f"portal_{kind}"))
    setattr(cfg, f"portal_{kind}", None)
    db.commit()
    return portal_settings_out(cfg, user.tenant)


# ---------------- saare portal logins (Settings -> Customer portal -> Customer logins) ----------------
@router.get("/portal-logins")
def all_logins(q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.models import Customer, CustomerLogin, PortalPushSubscription
    from app.modules.portal.accounts import login_out
    stmt = (select(CustomerLogin, Customer.name).join(Customer, Customer.id == CustomerLogin.customer_id)
            .where(CustomerLogin.tenant_id == user.tenant_id))
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(CustomerLogin.email).like(like), func.lower(Customer.name).like(like),
                              func.lower(func.coalesce(CustomerLogin.name, "")).like(like)))
    rows = db.execute(stmt.order_by(Customer.name, CustomerLogin.email)).all()
    push = dict(db.execute(select(PortalPushSubscription.login_id, func.count())
                           .where(PortalPushSubscription.tenant_id == user.tenant_id)
                           .group_by(PortalPushSubscription.login_id)).all())
    return [{**login_out(l), "customer_name": name, "push_devices": push.get(l.id, 0)} for l, name in rows]


# ---------------- banners ----------------
def banner_out(b: PortalBanner, db: Session) -> dict:
    link_label = None
    if b.link_type == "product" and b.link_product_id:
        p = db.get(Product, b.link_product_id)
        link_label = p.name if p else None
    elif b.link_type == "category" and b.link_category_id:
        link_label = category_path(db.get(Category, b.link_category_id))
    elif b.link_type == "url":
        link_label = b.link_url
    return {"id": b.id, "title": b.title, "subtitle": b.subtitle, "button_text": b.button_text,
            "image_url": media_url(b.tenant_id, b.image_name), "link_type": b.link_type,
            "link_product_id": b.link_product_id, "link_category_id": b.link_category_id, "link_url": b.link_url,
            "link_label": link_label, "sort_order": b.sort_order, "is_active": b.is_active,
            "starts_on": b.starts_on, "ends_on": b.ends_on}


def _banner(db: Session, user: User, banner_id: int) -> PortalBanner:
    b = db.get(PortalBanner, banner_id)
    if not b or b.tenant_id != user.tenant_id:
        raise HTTPException(404, "Banner not found")
    return b


def _apply(db: Session, user: User, b: PortalBanner, data: BannerIn):
    if data.link_type not in LINK_TYPES:
        raise HTTPException(400, "Invalid link type")
    if data.starts_on and data.ends_on and data.ends_on < data.starts_on:
        raise HTTPException(400, "End date must be after the start date")
    b.title = (data.title or "").strip()[:150] or None
    b.subtitle = (data.subtitle or "").strip()[:300] or None
    b.button_text = (data.button_text or "").strip()[:50] or None
    b.link_type = data.link_type
    b.link_product_id = b.link_category_id = b.link_url = None
    if data.link_type == "product":
        p = db.get(Product, data.link_product_id) if data.link_product_id else None
        if not p or p.tenant_id != user.tenant_id:
            raise HTTPException(400, "Choose the product this banner opens")
        b.link_product_id = p.id
    elif data.link_type == "category":
        c = db.get(Category, data.link_category_id) if data.link_category_id else None
        if not c or c.tenant_id != user.tenant_id:
            raise HTTPException(400, "Choose the category this banner opens")
        b.link_category_id = c.id
    elif data.link_type == "url":
        url = (data.link_url or "").strip()
        if not (url.startswith("https://") or url.startswith("http://") or url.startswith("/")):
            raise HTTPException(400, "Link must start with https://")
        b.link_url = url[:500]
    b.is_active, b.starts_on, b.ends_on = data.is_active, data.starts_on, data.ends_on


@router.get("/portal-banners")
def list_banners(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(PortalBanner).where(PortalBanner.tenant_id == user.tenant_id)
                      .order_by(PortalBanner.sort_order, PortalBanner.id))
    return [banner_out(b, db) for b in rows]


@router.post("/portal-banners")
def create_banner(data: BannerIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    last = db.scalars(select(PortalBanner.sort_order).where(PortalBanner.tenant_id == user.tenant_id)
                      .order_by(PortalBanner.sort_order.desc())).first()
    b = PortalBanner(tenant_id=user.tenant_id, sort_order=(last or 0) + 1)
    _apply(db, user, b, data)
    db.add(b)
    db.commit()
    return banner_out(b, db)


@router.put("/portal-banners/order")
def reorder_banners(data: OrderIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    for n, bid in enumerate(data.ids):
        b = _banner(db, user, bid)
        b.sort_order = n
    db.commit()
    return list_banners(user, db)


@router.put("/portal-banners/{banner_id}")
def update_banner(banner_id: int, data: BannerIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    b = _banner(db, user, banner_id)
    _apply(db, user, b, data)
    db.commit()
    return banner_out(b, db)


def _delete_image(b: PortalBanner):
    if b.image_name:
        thumb = b.image_name.replace(".", "_thumb.", 1)
        delete_files(ProductImage(tenant_id=b.tenant_id, file_name=b.image_name, thumb_name=thumb))


@router.post("/portal-banners/{banner_id}/image")
async def upload_banner_image(banner_id: int, file: UploadFile = File(...), user: User = Depends(admin_only),
                              db: Session = Depends(get_db)):
    b = _banner(db, user, banner_id)
    saved = process_upload(user.tenant_id, await file.read(MAX_BYTES + 1), file.filename)
    _delete_image(b)
    b.image_name = saved["file_name"]
    db.commit()
    return banner_out(b, db)


@router.delete("/portal-banners/{banner_id}")
def delete_banner(banner_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    b = _banner(db, user, banner_id)
    _delete_image(b)
    db.delete(b)
    db.commit()
    return {"ok": True}
