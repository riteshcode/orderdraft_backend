"""
Email bhejna (Postmark outbound API).
POSTMARK_SERVER_TOKEN khali ho to email sirf log mein print hota hai - local dev ke liye.
SENT list mein last emails rehti hain (tests / debugging).
"""
import base64
import logging
import re
from email.utils import parseaddr

import httpx

from app.core.config import settings

log = logging.getLogger("mailer")
SENT: list[dict] = []   # dev/test: bheje gaye emails


class MailError(Exception):
    pass


def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,  # (filename, content, mime)
    reply_to: str | None = None,
    from_email: str | None = None,       # poora From header bhi chalega: '"Fresh Farms" <orders@ff.com>'
    from_name: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
):
    sender = settings.MAIL_FROM
    if from_email:
        sender = format_from(from_name, from_email) if from_name else from_email
    cc = [c for c in (cc or []) if c and c.lower() != to.lower()]
    bcc = [b for b in (bcc or []) if b]
    message = {
        "From": sender,
        "To": to,
        "Subject": subject,
        "HtmlBody": html,
        "TextBody": text or "",
        "MessageStream": "outbound",
    }
    if reply_to:
        message["ReplyTo"] = reply_to
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if attachments:
        message["Attachments"] = [
            {"Name": name, "Content": base64.b64encode(content).decode(), "ContentType": mime}
            for name, content, mime in attachments
        ]

    SENT.append({"to": to, "subject": subject, "html": html, "text": text, "from": sender, "reply_to": reply_to,
                 "cc": cc, "bcc": bcc, "attachments": [a[0] for a in attachments or []]})
    del SENT[:-50]

    if not settings.POSTMARK_SERVER_TOKEN:
        log.info("EMAIL (dev, not sent) to=%s subject=%s\n%s", to, subject, text or html)
        return

    try:
        resp = httpx.post(
            "https://api.postmarkapp.com/email",
            headers={"X-Postmark-Server-Token": settings.POSTMARK_SERVER_TOKEN, "Accept": "application/json"},
            json=message,
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise MailError(f"Email send failed: {e}")
    if resp.status_code != 200:
        raise MailError(f"Postmark error {resp.status_code}: {resp.text[:300]}")


def clean_name(name: str | None) -> str:
    """From header mein quotes / newlines / <> nahi chahiye (header injection se bachao)."""
    return re.sub(r'[\r\n"<>\\]', "", name or "").strip()[:80]


def format_from(name: str | None, email: str) -> str:
    name = clean_name(name)
    return f'"{name}" <{email}>' if name else email


def platform_address() -> str:
    """MAIL_FROM se sirf email address (naam ke bina)."""
    return parseaddr(settings.MAIL_FROM)[1] or "no-reply@example.com"


def sender_for(cfg) -> tuple[str, str | None]:
    """
    Client ke naam se email (From, Reply-To):
      - client ka domain verified (DKIM + Return-Path)  -> "Fresh Farms" <orders@freshfarms.com.au>
      - warna platform ka address                     -> "Fresh Farms via OrderDraft" <no-reply@platform>,
                                                         Reply-To = client ka email (customer reply client ko jaaye)
    """
    business = clean_name(getattr(cfg, "business_name", "")) or settings.MAIL_PLATFORM_NAME
    if cfg is not None and cfg.sender_status == "verified" and cfg.sender_email:
        name = clean_name(cfg.sender_name) or business
        reply = cfg.email if cfg.email and cfg.email.lower() != cfg.sender_email.lower() else None
        return format_from(name, cfg.sender_email), reply
    reply = (cfg.sender_email or cfg.email) if cfg is not None else None
    return format_from(f"{business} via {settings.MAIL_PLATFORM_NAME}", platform_address()), reply or None


def simple_html(title: str, body_html: str, button_text: str | None = None, button_url: str | None = None) -> str:
    """Saare system emails ka ek simple template."""
    button = ""
    if button_text and button_url:
        button = (
            f'<p style="margin:24px 0"><a href="{button_url}" style="background:#2563eb;color:#fff;'
            f'padding:10px 18px;border-radius:6px;text-decoration:none">{button_text}</a></p>'
        )
    return (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#111">'
        f'<h2 style="margin-bottom:8px">{title}</h2>{body_html}{button}'
        f'<p style="color:#888;font-size:12px;margin-top:32px">{settings.APP_NAME}</p></div>'
    )
