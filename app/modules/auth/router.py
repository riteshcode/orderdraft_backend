"""
Auth: register (naya tenant + 14 din trial), login, me, forgot/reset password, invite accept.
"""
import logging
import re
import secrets
from datetime import datetime

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import bind_tenant, get_db, provision_tenant, tenant_db_name, tenant_session, utcnow
from app.core.deps import current_user
from app.core.helpers import get_tenant_settings, log_activity
from app.core.mailer import send_email, simple_html
from app.core.request_log import log_event
from app.core.security import access_token, hash_password, read_token, reset_token, verify_password
from app.models import Connection, Invite, Tenant, User
from app.modules.billing.limits import get_plan, trial_end_date, usage_summary

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("auth")


class RegisterIn(BaseModel):
    business_name: str
    name: str
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: str


class AcceptInviteIn(BaseModel):
    token: str
    name: str
    password: str


def check_password(password: str):
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")


def make_slug(db: Session, business_name: str) -> str:
    """'Fresh Farms Pty Ltd' -> 'freshfarms' (unique). Yahi inbound email address banta hai."""
    cleaned = re.sub(r"\b(pty|ltd|limited|co)\b", "", business_name.lower())
    base = re.sub(r"[^a-z0-9]", "", cleaned)[:30] or "client"
    slug = base
    while db.scalars(select(Tenant).where(Tenant.slug == slug)).first():
        slug = f"{base}{secrets.randbelow(9000) + 1000}"
    return slug


def login_response(db: Session, user: User, impersonator_id: int | None = None) -> dict:
    user.last_login_at = utcnow()
    db.commit()
    bind_tenant(db, user.tenant_id)   # me_payload client DB se connection info padhta hai
    return {"token": access_token(user.id, impersonator_id), "user": me_payload(db, user, impersonator_id)}


def me_payload(db: Session, user: User, impersonator_id: int | None = None) -> dict:
    """Frontend header / permissions ke liye sab kuch ek call mein."""
    tenant = user.tenant
    conn = db.scalars(select(Connection).where(Connection.tenant_id == tenant.id)
                      .order_by(Connection.id.desc())).first()   # ek hi accounting software
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_superadmin": user.is_superadmin,
        "impersonating": bool(impersonator_id),
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "slug": tenant.slug,
            "status": tenant.status,
            "inbound_email": f"{tenant.slug}@{settings.INBOUND_DOMAIN}",
        },
        "usage": usage_summary(db, tenant),
        "accounting": {
            "provider": conn.provider if conn else None,
            "status": conn.status if conn else None,
            "connected": bool(conn and conn.refresh_token and conn.status == "connected"),
            "org_name": conn.external_name if conn else None,
            "last_sync_at": conn.last_sync_at if conn else None,
        },
    }


@router.post("/register")
def register(data: RegisterIn, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(400, "Email already registered")
    check_password(data.password)
    if not data.business_name.strip() or not data.name.strip():
        raise HTTPException(400, "Enter both your business name and your name")

    tenant = Tenant(name=data.business_name.strip(), slug=make_slug(db, data.business_name),
                    email=email, subscription_status="trialing", trial_ends_at=trial_end_date())
    db.add(tenant)
    db.flush()
    plan = get_plan(db, tenant)   # default (sabse pehla) plan trial ke liye
    tenant.plan_id = plan.id if plan else None

    tenant.db_name = tenant_db_name(tenant.id)

    user = User(tenant_id=tenant.id, name=data.name.strip(), email=email, role="owner",
                password_hash=hash_password(data.password),
                is_superadmin=email in settings.SUPERADMIN_EMAILS)
    db.add(user)
    db.commit()

    # Client ka apna database banao (tables ke saath)
    try:
        provision_tenant(tenant)
    except Exception:
        log.exception("Tenant %s DB provisioning failed", tenant.id)
        db.delete(user)
        db.delete(tenant)
        db.commit()
        raise HTTPException(500, "Account setup failed - please try again in a few minutes")

    tenant.db_ready = True
    db.commit()
    bind_tenant(db, tenant.id)
    get_tenant_settings(db, tenant.id)          # default settings row
    log_activity(db, tenant.id, user.id, "tenant.registered")
    db.commit()
    return login_response(db, user)


def _log_login(user: User, action: str, request: Request):
    """Login / galat password - us client ke Activity log mein (kabhi login fail nahi karwata)."""
    if not user.tenant or not user.tenant.db_ready:
        return
    try:
        with tenant_session(user.tenant_id) as tdb:
            log_activity(tdb, user.tenant_id, user.id, action, "user", user.id,
                         {"email": user.email, "ip": request.client.host if request.client else None,
                          "agent": (request.headers.get("user-agent") or "")[:120]})
            tdb.commit()
    except Exception:
        logging.getLogger(__name__).exception("login activity log failed")


@router.post("/login")
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == data.email.lower())).first()
    if not user or not verify_password(data.password, user.password_hash):
        log_event("login_failed", email=data.email.lower()[:120], ip=request.client.host if request.client else None,
                  tenant_id=user.tenant_id if user else None)
        if user:
            _log_login(user, "auth.login_failed", request)
        raise HTTPException(401, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(403, "This user has been disabled. Please contact your admin.")
    if user.email in settings.SUPERADMIN_EMAILS and not user.is_superadmin:
        user.is_superadmin = True
    _log_login(user, "auth.login", request)
    return login_response(db, user)


@router.get("/me")
def me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return me_payload(db, user, getattr(request.state, "impersonator_id", None))


@router.post("/forgot-password")
def forgot_password(data: ForgotIn, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == data.email.lower())).first()
    # User ho ya na ho, same response (email enumeration se bachne ke liye)
    if user and user.is_active:
        link = f"{settings.FRONTEND_URL}/reset-password?token={reset_token(user.id, user.password_hash)}"
        send_email(
            user.email, f"Reset your {settings.APP_NAME} password",
            simple_html("Reset your password", "<p>Click below to set a new password. The link is valid for 1 hour.</p>",
                        "Reset password", link),
            text=f"Reset your password: {link}",
        )
    return {"ok": True}


@router.post("/reset-password")
def reset_password(data: ResetIn, db: Session = Depends(get_db)):
    check_password(data.password)
    try:
        payload = read_token(data.token, "reset")
    except jwt.PyJWTError:
        raise HTTPException(400, "This link has expired or is invalid")
    user = db.get(User, int(payload["sub"]))
    if not user or user.password_hash[-12:] != payload.get("pwh"):
        raise HTTPException(400, "This link has already been used")
    user.password_hash = hash_password(data.password)
    db.commit()
    return login_response(db, user)


@router.get("/invite/{token}")
def invite_info(token: str, db: Session = Depends(get_db)):
    invite = _valid_invite(db, token)
    return {"email": invite.email, "tenant_name": invite.tenant.name, "role": invite.role}


@router.post("/accept-invite")
def accept_invite(data: AcceptInviteIn, db: Session = Depends(get_db)):
    check_password(data.password)
    invite = _valid_invite(db, data.token)
    if db.scalars(select(User).where(User.email == invite.email)).first():
        raise HTTPException(400, "An account with this email already exists")
    user = User(tenant_id=invite.tenant_id, name=data.name.strip() or invite.email, email=invite.email,
                role=invite.role, password_hash=hash_password(data.password))
    db.add(user)
    invite.accepted_at = utcnow()
    db.flush()
    bind_tenant(db, invite.tenant_id)
    log_activity(db, invite.tenant_id, user.id, "team.joined", detail={"email": user.email})
    db.commit()
    return login_response(db, user)


def _valid_invite(db: Session, token: str) -> Invite:
    invite = db.scalars(select(Invite).where(Invite.token == token)).first()
    if not invite or invite.accepted_at or invite.expires_at < utcnow():
        raise HTTPException(400, "This invite link has expired or has already been used")
    return invite
