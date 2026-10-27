"""
Admin side: customer ko portal access dena (Customer page -> Portal access).

  GET    /customers/{id}/portal-logins
  POST   /customers/{id}/portal-logins               {email, name?, send_welcome: true}  -> temp password + welcome email
  POST   /customers/{id}/portal-logins/{lid}/resend  naya temp password + welcome email dobara
  PUT    /customers/{id}/portal-logins/{lid}         {name?, is_active}
  DELETE /customers/{id}/portal-logins/{lid}
  POST   /customers/{id}/portal-logins/{lid}/login-as  staff bina password customer ki tarah portal kholein (2 ghante)
  POST   /customers/{id}/portal-logins/{lid}/password  {password, must_change: true, send_email: false}
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity
from app.core.mailer import MailError
from app.models import Customer, CustomerLogin, User
from app.modules.portal.service import portal_url, send_welcome, set_temp_password

router = APIRouter(tags=["portal-admin"])
admin_only = require_role("owner", "admin")


class LoginIn(BaseModel):
    email: EmailStr
    name: str | None = None
    send_welcome: bool = True


class LoginUpdate(BaseModel):
    name: str | None = None
    is_active: bool = True


def _customer(db: Session, user: User, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if not c or c.tenant_id != user.tenant_id:
        raise HTTPException(404, "Customer not found")
    return c


def _login(db: Session, c: Customer, login_id: int) -> CustomerLogin:
    login = db.get(CustomerLogin, login_id)
    if not login or login.customer_id != c.id:
        raise HTTPException(404, "Login not found")
    return login


def login_out(login: CustomerLogin) -> dict:
    return {"id": login.id, "customer_id": login.customer_id, "email": login.email, "name": login.name,
            "is_active": login.is_active, "must_change_password": login.must_change_password,
            "welcome_sent_at": login.welcome_sent_at, "last_login_at": login.last_login_at,
            "created_at": login.created_at}


@router.get("/customers/{customer_id}/portal-logins")
def list_logins(customer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    rows = db.scalars(select(CustomerLogin).where(CustomerLogin.customer_id == c.id).order_by(CustomerLogin.id))
    return {"portal_url": portal_url(user.tenant), "items": [login_out(r) for r in rows]}


def _send(db: Session, user: User, login: CustomerLogin, password: str) -> str | None:
    try:
        send_welcome(db, user.tenant, login, password)
        return None
    except MailError as e:
        return str(e)


@router.post("/customers/{customer_id}/portal-logins")
def add_login(customer_id: int, data: LoginIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    if c.deleted_at:
        raise HTTPException(400, "This customer is in Trash - restore it first")
    email = data.email.lower().strip()
    if db.scalars(select(CustomerLogin).where(CustomerLogin.tenant_id == user.tenant_id,
                                              func.lower(CustomerLogin.email) == email)).first():
        raise HTTPException(400, "This email already has a portal login (maybe for another customer)")
    login = CustomerLogin(tenant_id=user.tenant_id, customer_id=c.id, email=email,
                          name=(data.name or "").strip()[:200] or None, password_hash="")
    password = set_temp_password(login)
    login.customer = c
    db.add(login)
    db.flush()
    error = _send(db, user, login, password) if data.send_welcome else None
    if data.send_welcome and not error:
        log_activity(db, user.tenant_id, user.id, "customer.portal_welcome_sent", "customer", c.id, {"email": email})
    db.commit()
    return {**login_out(login), "email_error": error,
            # Email na gaya ho (ya band rakha) to admin khud bata sake
            "temp_password": password if (error or not data.send_welcome) else None}


@router.post("/customers/{customer_id}/portal-logins/{login_id}/resend")
def resend_welcome(customer_id: int, login_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = _customer(db, user, customer_id)
    login = _login(db, c, login_id)
    if not login.is_active:
        raise HTTPException(400, "This login is disabled - enable it first")
    password = set_temp_password(login)
    error = _send(db, user, login, password)
    if not error:
        log_activity(db, user.tenant_id, user.id, "customer.portal_welcome_sent", "customer", c.id,
                     {"email": login.email, "resend": True})
    db.commit()
    return {**login_out(login), "email_error": error, "temp_password": password if error else None}


@router.put("/customers/{customer_id}/portal-logins/{login_id}")
def update_login(customer_id: int, login_id: int, data: LoginUpdate, user: User = Depends(admin_only),
                 db: Session = Depends(get_db)):
    login = _login(db, _customer(db, user, customer_id), login_id)
    login.name = (data.name or "").strip()[:200] or None
    login.is_active = data.is_active
    db.commit()
    return login_out(login)


@router.delete("/customers/{customer_id}/portal-logins/{login_id}")
def delete_login(customer_id: int, login_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    login = _login(db, _customer(db, user, customer_id), login_id)
    db.delete(login)
    db.commit()
    return {"ok": True}


class PasswordIn(BaseModel):
    password: str
    must_change: bool = True      # True = customer pehli login pe apna password banaye
    send_email: bool = False      # True = welcome email (naye password ke saath)


@router.post("/customers/{customer_id}/portal-logins/{login_id}/password")
def set_password(customer_id: int, login_id: int, data: PasswordIn, user: User = Depends(admin_only),
                 db: Session = Depends(get_db)):
    """Staff khud password set kare (phone pe customer ko bata de) - email bhejna optional."""
    from app.core.security import hash_password
    login = _login(db, _customer(db, user, customer_id), login_id)
    if len(data.password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    login.password_hash = hash_password(data.password)
    login.must_change_password = data.must_change
    error = _send(db, user, login, data.password) if data.send_email else None
    log_activity(db, user.tenant_id, user.id, "customer.portal_password_reset", "customer", login.customer_id,
                 {"email": login.email, "emailed": bool(data.send_email and not error)})
    db.commit()
    return {**login_out(login), "email_error": error}


@router.post("/customers/{customer_id}/portal-logins/{login_id}/login-as")
def login_as(customer_id: int, login_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """
    Support: customer ki jagah portal dekho / order karo - unka password nahi chahiye.
    Token 2 ghante ka, "imp" = staff user. Portal mein "Viewing as ... (staff)" patti dikhti hai,
    aur jo order lagega uspe staff ka naam (Activity log) aata hai.
    """
    from datetime import timedelta
    from app.core.security import make_token
    login = _login(db, _customer(db, user, customer_id), login_id)
    if not login.is_active:
        raise HTTPException(400, "This login is disabled - enable it first")
    token = make_token({"sub": str(login.id), "tid": user.tenant_id, "imp": user.id}, "portal", timedelta(hours=2))
    log_activity(db, user.tenant_id, user.id, "customer.portal_login_as", "customer", login.customer_id,
                 {"email": login.email})
    db.commit()
    return {"token": token, "slug": user.tenant.slug,
            "url": f"/portal/{user.tenant.slug}/impersonate#token={token}"}
