"""
Portal login (public) - customer ka staff apne supplier ke portal pe.

  GET  /portal/{slug}/info                 business ka naam (login page ke liye)
  POST /portal/{slug}/login                {email, password} -> {token, must_change_password, ...}
  POST /portal/{slug}/forgot-password      {email}   (same response chahe email ho ya na ho)
  POST /portal/{slug}/reset-password       {token, password}  (welcome email ka "Set your password" bhi yahi)
  GET  /portal/me                          Authorization: Bearer <portal token>
  POST /portal/change-password             {current_password, new_password}

Portal token alag type ("portal") - OrderDraft staff ke API pe kaam nahi karta, aur ulta bhi nahi.
"""
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import bind_tenant, get_db, utcnow
from app.core.helpers import get_tenant_settings, log_activity
from app.core.mailer import MailError
from app.core.request_log import log_event
from app.core.security import hash_password, make_token, read_token, verify_password
from app.models import CustomerLogin, Tenant, User
from app.modules.portal.service import send_forgot

router = APIRouter(prefix="/portal", tags=["portal"])
bearer = HTTPBearer(auto_error=False)
PORTAL_TOKEN_HOURS = 24 * 7
MIN_PASSWORD = 8


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: str


class ChangeIn(BaseModel):
    current_password: str
    new_password: str


def _tenant(db: Session, slug: str) -> Tenant:
    t = db.scalars(select(Tenant).where(Tenant.slug == slug.lower())).first()
    if not t or not t.db_ready or t.status == "suspended":
        raise HTTPException(404, "This ordering portal doesn't exist")
    bind_tenant(db, t.id)
    return t


def _check_password(pw: str):
    if len(pw or "") < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters")


def me_out(db: Session, t: Tenant, login: CustomerLogin) -> dict:
    cfg = get_tenant_settings(db, t.id)
    staff_id = db.info.get("portal_impersonator")
    staff = db.get(User, staff_id) if staff_id else None
    return {"id": login.id, "email": login.email, "name": login.name or login.customer.name,
            # Staff "login as" kar raha hai to password badalne ko mat bolo
            "must_change_password": login.must_change_password and not staff_id,
            "impersonated_by": staff.name if staff else None,
            "customer": {"id": login.customer.id, "name": login.customer.name},
            "business": {"name": cfg.business_name or t.name, "slug": t.slug}}


def current_portal_login(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                         db: Session = Depends(get_db)) -> tuple[Tenant, CustomerLogin]:
    if not creds:
        raise HTTPException(401, "Please log in")
    try:
        payload = read_token(creds.credentials, "portal")
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session has expired - please log in again")
    t = db.get(Tenant, int(payload["tid"]))
    if not t or not t.db_ready or t.status == "suspended":
        raise HTTPException(401, "Portal not available")
    bind_tenant(db, t.id)
    login = db.get(CustomerLogin, int(payload["sub"]))
    if not login or not login.is_active or login.customer.deleted_at or not login.customer.is_active:
        raise HTTPException(401, "This login is disabled - please contact your supplier")
    db.info["portal_impersonator"] = payload.get("imp")     # staff "login as" (Customer -> Portal access)
    return t, login


@router.get("/{slug}/info")
def portal_info(slug: str, db: Session = Depends(get_db)):
    """Portal ka public info (bina login): naam, contact, on/off, public/private, checkout settings."""
    from app.core.webpush import vapid_public_key
    from app.modules.portal.admin import branding_out
    from app.modules.portal.shop import payment_options
    t = _tenant(db, slug)
    cfg = get_tenant_settings(db, t.id)
    return {"name": cfg.business_name or t.name, "slug": t.slug, "email": cfg.email, "phone": cfg.phone,
            "address": cfg.address, "enabled": cfg.portal_enabled, "access": cfg.portal_access,
            "show_prices_public": cfg.portal_show_prices_public, "welcome_title": cfg.portal_welcome_title,
            "welcome_text": cfg.portal_welcome_text, "checkout_note": cfg.portal_checkout_note,
            "min_order": cfg.portal_min_order, "payment_options": payment_options(cfg),
            "prices_include_gst": cfg.prices_include_gst, "gst_rate": cfg.gst_rate,
            "branding": branding_out(cfg), "push_public_key": vapid_public_key(),
            "allow_standing": cfg.portal_allow_standing}


@router.post("/{slug}/login")
def portal_login(slug: str, data: LoginIn, request: Request, db: Session = Depends(get_db)):
    t = _tenant(db, slug)
    login = db.scalars(select(CustomerLogin).where(CustomerLogin.tenant_id == t.id,
                                                   func.lower(CustomerLogin.email) == data.email.lower())).first()
    if not login or not verify_password(data.password, login.password_hash):
        log_event("portal_login_failed", tenant_id=t.id, email=data.email.lower()[:120],
                  ip=request.client.host if request.client else None)
        raise HTTPException(401, "Email or password is incorrect")
    if not login.is_active or login.customer.deleted_at or not login.customer.is_active:
        raise HTTPException(403, "This login is disabled - please contact your supplier")
    login.last_login_at = utcnow()
    db.commit()
    token = make_token({"sub": str(login.id), "tid": t.id}, "portal", timedelta(hours=PORTAL_TOKEN_HOURS))
    return {"token": token, "login": me_out(db, t, login)}


@router.post("/{slug}/forgot-password")
def portal_forgot(slug: str, data: ForgotIn, db: Session = Depends(get_db)):
    t = _tenant(db, slug)
    login = db.scalars(select(CustomerLogin).where(CustomerLogin.tenant_id == t.id,
                                                   func.lower(CustomerLogin.email) == data.email.lower())).first()
    if login and login.is_active:
        try:
            send_forgot(db, t, login)
        except MailError:
            log_event("portal_forgot_email_failed", tenant_id=t.id, login_id=login.id)
    return {"ok": True, "message": "If that email has an account, we've sent a reset link."}


@router.post("/{slug}/reset-password")
def portal_reset(slug: str, data: ResetIn, db: Session = Depends(get_db)):
    t = _tenant(db, slug)
    try:
        payload = read_token(data.token, "portal_reset")
    except jwt.PyJWTError:
        raise HTTPException(400, "This link has expired or isn't valid - ask for a new one")
    login = db.get(CustomerLogin, int(payload["sub"]))
    if (not login or payload.get("tid") != t.id or login.tenant_id != t.id
            or login.password_hash[-12:] != payload.get("pwh")):
        raise HTTPException(400, "This link has already been used or isn't valid - ask for a new one")
    _check_password(data.password)
    login.password_hash = hash_password(data.password)
    login.must_change_password = False
    log_activity(db, t.id, None, "customer.portal_password_set", "customer", login.customer_id, {"email": login.email})
    db.commit()
    return {"ok": True, "email": login.email}


@router.get("/me")
def portal_me(ctx=Depends(current_portal_login), db: Session = Depends(get_db)):
    t, login = ctx
    return me_out(db, t, login)


@router.post("/change-password")
def portal_change_password(data: ChangeIn, ctx=Depends(current_portal_login), db: Session = Depends(get_db)):
    t, login = ctx
    if db.info.get("portal_impersonator"):
        raise HTTPException(403, "You're logged in as this customer - use Set password on their Portal access tab instead")
    if not verify_password(data.current_password, login.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    _check_password(data.new_password)
    if data.new_password == data.current_password:
        raise HTTPException(400, "Choose a different password from the temporary one")
    login.password_hash = hash_password(data.new_password)
    login.must_change_password = False
    db.commit()
    return me_out(db, t, login)
