"""
Portal logins: temp password, welcome email, reset link.

Welcome email mein:
  - portal ka link (FRONTEND_URL/portal/<slug>)
  - email + temporary password (pehli login pe badalna zaroori)
  - "Set your password" button = reset link (7 din valid) - temp password type karne ki zaroorat hi nahi
Email client ke naam se jaata hai (sender_for) - customer ko OrderDraft nahi, apna supplier dikhe.
"""
import html
import secrets
import string
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import utcnow
from app.core.helpers import get_tenant_settings
from app.core.mailer import send_email, sender_for, simple_html
from app.core.security import hash_password, make_token
from app.models import CustomerLogin, Tenant

WELCOME_LINK_DAYS = 7
FORGOT_LINK_HOURS = 1


def temp_password(length: int = 10) -> str:
    """Padhne mein aasaan (0/O, 1/l nahi) - customer phone se bhi type kar sake."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def portal_url(tenant: Tenant) -> str:
    return f"{settings.FRONTEND_URL}/portal/{tenant.slug}"


def portal_reset_token(login: CustomerLogin, hours: int) -> str:
    # Password badalte hi purana link bekaar (pwh match nahi karega)
    return make_token({"sub": str(login.id), "tid": login.tenant_id, "pwh": login.password_hash[-12:]},
                      "portal_reset", timedelta(hours=hours))


def reset_link(tenant: Tenant, login: CustomerLogin, hours: int) -> str:
    return f"{portal_url(tenant)}/reset-password?token={portal_reset_token(login, hours)}"


def set_temp_password(login: CustomerLogin) -> str:
    pw = temp_password()
    login.password_hash = hash_password(pw)
    login.must_change_password = True
    return pw


def send_welcome(db: Session, tenant: Tenant, login: CustomerLogin, password: str):
    cfg = get_tenant_settings(db, tenant.id)
    business = cfg.business_name or tenant.name
    e = html.escape
    name = login.name or login.customer.name
    body = (
        f"<p>Hi {e(name)},</p>"
        f"<p><b>{e(business)}</b> has set up an online ordering account for <b>{e(login.customer.name)}</b>. "
        "You can see your prices, place orders and check past orders any time.</p>"
        '<table style="margin:16px 0;border-collapse:collapse">'
        f'<tr><td style="padding:4px 12px 4px 0;color:#666">Website</td><td><a href="{portal_url(tenant)}">{e(portal_url(tenant))}</a></td></tr>'
        f'<tr><td style="padding:4px 12px 4px 0;color:#666">Email</td><td>{e(login.email)}</td></tr>'
        f'<tr><td style="padding:4px 12px 4px 0;color:#666">Temporary password</td>'
        f'<td style="font-family:monospace;font-size:15px"><b>{e(password)}</b></td></tr></table>'
        "<p>For your security, please set your own password using the button below "
        f"(the link works for {WELCOME_LINK_DAYS} days). If you log in with the temporary password instead, "
        "you'll be asked to change it.</p>"
    )
    text = (f"Hi {name},\n\n{business} has set up an online ordering account for {login.customer.name}.\n\n"
            f"Website: {portal_url(tenant)}\nEmail: {login.email}\nTemporary password: {password}\n\n"
            f"Set your own password: {reset_link(tenant, login, WELCOME_LINK_DAYS * 24)}\n")
    sender, reply_to = sender_for(cfg)
    send_email(login.email, f"Your {business} ordering account",
               simple_html(f"Welcome to {business}", body, "Set your password",
                           reset_link(tenant, login, WELCOME_LINK_DAYS * 24)),
               text, from_email=sender, reply_to=reply_to)
    login.welcome_sent_at = utcnow()


def send_forgot(db: Session, tenant: Tenant, login: CustomerLogin):
    cfg = get_tenant_settings(db, tenant.id)
    business = cfg.business_name or tenant.name
    link = reset_link(tenant, login, FORGOT_LINK_HOURS)
    sender, reply_to = sender_for(cfg)
    send_email(login.email, f"Reset your {business} password",
               simple_html("Reset your password", "<p>Click below to choose a new password. "
                           "The link works for 1 hour. If you didn't ask for this, ignore this email.</p>",
                           "Choose a new password", link),
               f"Choose a new password: {link}", from_email=sender, reply_to=reply_to)
