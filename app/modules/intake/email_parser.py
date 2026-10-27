"""
Postmark inbound webhook ka JSON -> hamara simple format.

Postmark docs: https://postmarkapp.com/developer/webhooks/inbound-webhook
Important fields: FromFull, ToFull (MailboxHash), Subject, TextBody, HtmlBody, Attachments
"""
import base64
import html
import re
from dataclasses import dataclass, field

from app.core.config import settings


@dataclass
class ParsedAttachment:
    name: str
    content_type: str
    content: bytes


@dataclass
class ParsedEmail:
    tenant_slug: str | None
    sender_email: str | None
    sender_name: str | None
    subject: str
    body: str
    attachments: list[ParsedAttachment] = field(default_factory=list)


def html_to_text(raw_html: str) -> str:
    """Bahut simple HTML -> text. TextBody na ho tab use hota hai."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"(?i)</td>|</th>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def find_tenant_slug(payload: dict) -> str | None:
    """
    Tenant pehchaano. Do tareeke support hain:
      1. Postmark MailboxHash:  abc123+freshfarms@inbound.postmarkapp.com -> "freshfarms"
      2. Custom domain:          freshfarms@orders.yourapp.com           -> "freshfarms"
    """
    recipients = payload.get("ToFull") or []
    # Forward karne par kabhi kabhi hamara address Cc/Bcc mein hota hai
    recipients += payload.get("CcFull") or []
    recipients += payload.get("BccFull") or []

    for r in recipients:
        if r.get("MailboxHash"):
            return r["MailboxHash"].lower()

    for r in recipients:
        email = (r.get("Email") or "").lower()
        if email.endswith("@" + settings.INBOUND_DOMAIN.lower()):
            return email.split("@")[0]

    original = (payload.get("OriginalRecipient") or "").lower()
    if "+" in original:
        return original.split("+", 1)[1].split("@")[0]
    if original.endswith("@" + settings.INBOUND_DOMAIN.lower()):
        return original.split("@")[0]
    return None


def parse_postmark(payload: dict) -> ParsedEmail:
    from_full = payload.get("FromFull") or {}

    body = payload.get("TextBody") or ""
    if not body.strip() and payload.get("HtmlBody"):
        body = html_to_text(payload["HtmlBody"])

    attachments = []
    for att in payload.get("Attachments") or []:
        try:
            content = base64.b64decode(att.get("Content") or "")
        except Exception:
            continue
        attachments.append(
            ParsedAttachment(
                name=att.get("Name") or "attachment",
                content_type=att.get("ContentType") or "application/octet-stream",
                content=content,
            )
        )

    return ParsedEmail(
        tenant_slug=find_tenant_slug(payload),
        sender_email=(from_full.get("Email") or payload.get("From") or "").lower() or None,
        sender_name=from_full.get("Name") or None,
        subject=payload.get("Subject") or "",
        body=body.strip(),
        attachments=attachments,
    )
