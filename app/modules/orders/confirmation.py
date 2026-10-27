"""
Order confirmation email - order approve hote hi customer ko.

From:
  client ka domain verified (DKIM + Return-Path)  -> "Fresh Farms" <orders@freshfarms.com.au>
  warna                                          -> "Fresh Farms via OrderDraft" <platform address>,
                                                    Reply-To = client ka email
To: customer.email   CC: customer.other_emails (setting on ho to)   BCC: client ki copy (setting)

Settings -> Email mein on/off, subject, message. Subject/message mein ye likh sakte ho:
  {order_number} {customer_name} {business_name} {po_number} {delivery_date}
"""
import html
import logging
import re

from sqlalchemy.orm import Session

from app.core.database import tenant_session, utcnow
from app.core.helpers import get_tenant_settings, log_activity, money
from app.core.mailer import MailError, send_email, sender_for
from app.models import DraftOrder, TenantSettings

log = logging.getLogger("orders.confirmation")
DEFAULT_SUBJECT = "Order confirmation #{order_number} - {business_name}"
DEFAULT_MESSAGE = "Thanks for your order. We've received it and it's being prepared."
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def split_emails(value: str | None) -> list[str]:
    return [e.strip().lower() for e in re.split(r"[,;\s]+", value or "") if EMAIL_RE.match(e.strip())]


def order_number(order: DraftOrder) -> str:
    return order.po_number or str(order.id)


def _vars(order: DraftOrder, cfg: TenantSettings) -> dict:
    return {
        "order_number": order_number(order), "customer_name": order.customer.name if order.customer else "",
        "business_name": cfg.business_name or "", "po_number": order.po_number or "",
        "delivery_date": order.delivery_date.strftime("%d/%m/%Y") if order.delivery_date else "",
    }


def fill(template: str, values: dict) -> str:
    """{order_number} jaise placeholders - galat placeholder ho to bhi crash nahi."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def _money(value) -> str:
    return "" if value is None else f"${money(value):,.2f}"


def _price(line):
    if line.unit_price is not None:
        return line.unit_price
    return line.product.price if line.product else None


def build_email(order: DraftOrder, cfg: TenantSettings) -> tuple[str, str, str]:
    """(subject, html, text)"""
    values = _vars(order, cfg)
    subject = fill(cfg.order_email_subject or DEFAULT_SUBJECT, values)
    message = fill(cfg.order_email_message or DEFAULT_MESSAGE, values)
    esc = html.escape

    rows, text_rows = [], []
    for line in order.lines:
        name = line.product.name if line.product else line.raw_text
        price = _price(line)
        amount = money(price) * money(line.quantity) if price is not None else None
        qty = f"{line.quantity:g}"
        rows.append(
            f"<tr><td style='padding:6px;border-bottom:1px solid #eee'>{esc(name)}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;text-align:right'>{qty} {esc(line.unit or '')}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;text-align:right'>{_money(price)}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;text-align:right'>{_money(amount)}</td></tr>")
        text_rows.append(f"- {name}: {qty} {line.unit or ''} {_money(amount)}".rstrip())

    details = [("Order", f"#{values['order_number']}")]
    if order.po_number:
        details.append(("PO number", order.po_number))
    if order.delivery_date:
        details.append(("Delivery date", values["delivery_date"]))
    if order.customer and (order.customer.address or order.customer.delivery_instructions):
        details.append(("Deliver to", order.customer.address or ""))
        if order.customer.delivery_instructions:
            details.append(("Instructions", order.customer.delivery_instructions))
    detail_html = "".join(f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{esc(k)}</td><td>{esc(v)}</td></tr>"
                          for k, v in details)
    total = _money(order.total_amount)
    gst_note = "incl. GST" if cfg.prices_include_gst else "excl. GST"
    notes = f"<p><b>Notes:</b> {esc(order.notes)}</p>" if order.notes else ""
    contact = esc(cfg.email or cfg.sender_email or "")

    body = f"""<div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#111">
<h2 style="margin-bottom:4px">{esc(cfg.business_name or '')}</h2>
<p>Hi {esc(values['customer_name'])},</p>
<p>{esc(message).replace(chr(10), '<br>')}</p>
<table style="margin:12px 0">{detail_html}</table>
<table style="width:100%;border-collapse:collapse;font-size:14px">
<thead><tr style="background:#f5f5f5"><th style="padding:6px;text-align:left">Item</th>
<th style="padding:6px;text-align:right">Qty</th><th style="padding:6px;text-align:right">Price</th>
<th style="padding:6px;text-align:right">Amount</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
<tfoot><tr><td colspan="3" style="padding:8px 6px;text-align:right"><b>Total</b> <span style="color:#666">({gst_note})</span></td>
<td style="padding:8px 6px;text-align:right"><b>{total}</b></td></tr></tfoot></table>
{notes}
<p style="color:#666;font-size:13px;margin-top:24px">Questions about this order? Just reply to this email{(' or contact ' + contact) if contact else ''}.</p>
</div>"""
    text = "\n".join([f"Hi {values['customer_name']},", "", message, "", f"Order #{values['order_number']}",
                      *(f"{k}: {v}" for k, v in details[1:]), "", *text_rows, "", f"Total ({gst_note}): {total}",
                      "", cfg.business_name or ""])
    return subject, body, text


def send_order_confirmation(db: Session, cfg: TenantSettings, order: DraftOrder, force: bool = False,
                            to: str | None = None, user_id: int | None = None) -> bool:
    """True = email gaya. Customer ka email na ho to chupchap skip (activity log mein note)."""
    if order.confirmation_sent_at and not force:
        return False
    recipient = (to or (order.customer.email if order.customer else None) or "").strip().lower()
    if not EMAIL_RE.match(recipient):
        log_activity(db, order.tenant_id, user_id, "order.confirmation_skipped", "order", order.id,
                     {"reason": "Customer has no email address"})
        db.commit()
        return False

    cc = split_emails(order.customer.other_emails) if (cfg.order_email_cc_other and order.customer and not to) else []
    bcc = split_emails(cfg.order_email_bcc)
    subject, body, text = build_email(order, cfg)
    sender, reply_to = sender_for(cfg)
    try:
        send_email(recipient, subject, body, text, reply_to=reply_to, from_email=sender, cc=cc, bcc=bcc)
    except MailError as e:
        log.warning("Order %s confirmation failed: %s", order.id, e)
        log_activity(db, order.tenant_id, user_id, "order.confirmation_failed", "order", order.id, {"error": str(e)[:300]})
        db.commit()
        return False

    order.confirmation_sent_at = utcnow()
    order.confirmation_to = ", ".join([recipient, *cc])[:300]
    log_activity(db, order.tenant_id, user_id, "order.confirmation_sent", "order", order.id,
                 {"to": recipient, "cc": cc, "from": sender})
    db.commit()
    return True


def send_order_confirmation_job(tenant_id: int, order_id: int):
    """Approve ke baad background mein (client ka apna DB)."""
    with tenant_session(tenant_id) as db:
        order = db.get(DraftOrder, order_id)
        cfg = get_tenant_settings(db, tenant_id)
        if order and cfg.order_email_enabled and order.status in ("approved", "exported"):
            send_order_confirmation(db, cfg, order)
