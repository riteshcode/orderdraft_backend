"""
Settings -> Email

  GET    /settings/email
  PUT    /settings/email/sender          {name, email}      domain add (DNS records milte hain)
  POST   /settings/email/sender/verify                      DNS check
  DELETE /settings/email/sender                             platform sender pe wapas
  PUT    /settings/email/order           {enabled, subject, message, cc_other, bcc}
  POST   /settings/email/test            {to?}              test email (jo From abhi lagega usi se)
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_role
from app.core.helpers import get_tenant_settings, log_activity
from app.core.mailer import MailError, platform_address, send_email, sender_for, simple_html
from app.models import TenantSettings, User
from app.modules.email import domains
from app.modules.orders.confirmation import DEFAULT_MESSAGE, DEFAULT_SUBJECT, split_emails

router = APIRouter(prefix="/settings/email", tags=["settings"])
admin_only = require_role("owner", "admin")


class SenderIn(BaseModel):
    name: str | None = None
    email: str


class OrderEmailIn(BaseModel):
    enabled: bool = True
    subject: str | None = None
    message: str | None = None
    cc_other: bool = False
    bcc: str | None = None


class TestIn(BaseModel):
    to: str | None = None


def email_out(cfg: TenantSettings) -> dict:
    sender, reply_to = sender_for(cfg)
    return {
        "sender": {"name": cfg.sender_name, "email": cfg.sender_email, "domain": cfg.sender_domain,
                   "status": cfg.sender_status or "none", "dns": cfg.sender_dns or [],
                   "checked_at": cfg.sender_checked_at},
        "effective_from": sender, "reply_to": reply_to,
        "order_email": {"enabled": cfg.order_email_enabled, "subject": cfg.order_email_subject,
                        "message": cfg.order_email_message, "cc_other": cfg.order_email_cc_other,
                        "bcc": cfg.order_email_bcc,
                        "default_subject": DEFAULT_SUBJECT, "default_message": DEFAULT_MESSAGE},
        "domain_api_configured": bool(settings.POSTMARK_ACCOUNT_TOKEN),
        "platform_from": platform_address(),
    }


def _cfg(db: Session, user: User) -> TenantSettings:
    return get_tenant_settings(db, user.tenant_id, for_update=True)


@router.get("")
def get_email(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = get_tenant_settings(db, user.tenant_id)
    db.commit()
    return email_out(cfg)


@router.put("/sender")
def save_sender(data: SenderIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = _cfg(db, user)
    try:
        domains.setup_sender(db, cfg, data.name, data.email)
    except domains.DomainError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    log_activity(db, user.tenant_id, user.id, "email.sender_saved", detail={"email": cfg.sender_email})
    db.commit()
    return email_out(cfg)


@router.post("/sender/verify")
def verify_sender(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = _cfg(db, user)
    try:
        missing = domains.verify_sender(cfg)
    except domains.DomainError as e:
        db.commit()
        raise HTTPException(400, str(e))
    if cfg.sender_status == "verified":
        log_activity(db, user.tenant_id, user.id, "email.sender_verified", detail={"domain": cfg.sender_domain})
    db.commit()
    return {**email_out(cfg), "missing": missing}


@router.delete("/sender")
def remove_sender(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = _cfg(db, user)
    domains.remove_sender(db, cfg)
    db.commit()
    return email_out(cfg)


@router.put("/order")
def save_order_email(data: OrderEmailIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    bcc = split_emails(data.bcc)
    if data.bcc and data.bcc.strip() and not bcc:
        raise HTTPException(400, "Enter a valid BCC email address")
    cfg = _cfg(db, user)
    cfg.order_email_enabled = data.enabled
    cfg.order_email_subject = (data.subject or "").strip()[:200] or None
    cfg.order_email_message = (data.message or "").strip() or None
    cfg.order_email_cc_other = data.cc_other
    cfg.order_email_bcc = ", ".join(bcc)[:200] or None
    db.commit()
    return email_out(cfg)


@router.post("/test")
def test_email(data: TestIn | None = None, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = get_tenant_settings(db, user.tenant_id)
    to = ((data.to if data else None) or user.email or "").strip()
    if not split_emails(to):
        raise HTTPException(400, "Enter a valid email address")
    sender, reply_to = sender_for(cfg)
    try:
        send_email(to, f"Test email from {cfg.business_name or settings.APP_NAME}",
                   simple_html("It works!", f"<p>This is how your customers will see emails from you.</p>"
                                            f"<p><b>From:</b> {sender.replace('<', '&lt;')}<br>"
                                            f"<b>Reply-To:</b> {reply_to or '-'}</p>"),
                   reply_to=reply_to, from_email=sender)
    except MailError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"ok": True, "to": to, "from": sender}
