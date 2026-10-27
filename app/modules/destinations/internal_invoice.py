"""
Internal invoice destination - jin clients ke paas Xero nahi, unke liye hamara system
khud tax invoice banata hai (PDF + email).

config: {"auto_email": true}   # bante hi customer ko email karo
"""
from fastapi import HTTPException
from sqlalchemy import select

from app.core.mailer import MailError
from app.models import DraftOrder, Invoice, TenantSettings
from app.modules.destinations.base import ExportResult
from app.modules.invoices.service import create_invoice_from_order, send_invoice

TYPE = "internal_invoice"
LABEL = "OrderDraft Invoice"
SECRET_FIELDS: list[str] = []


def validate_config(config: dict) -> dict:
    return {"auto_email": bool(config.get("auto_email", True))}


def send(db, order: DraftOrder, config: dict, tenant: TenantSettings) -> ExportResult:
    # Retry par duplicate invoice mat banao
    invoice = db.scalars(select(Invoice).where(Invoice.order_id == order.id, Invoice.source == "internal",
                                               Invoice.status != "void")).first()
    if not invoice:
        try:
            invoice = create_invoice_from_order(db, order, tenant, source="internal")
        except HTTPException as e:
            return ExportResult(False, e.detail)
        db.commit()

    message = f"Invoice {invoice.number} created"
    if config.get("auto_email") and invoice.status == "draft":
        if not (order.customer and order.customer.email):
            message += " (not emailed - customer has no email)"
        else:
            try:
                send_invoice(db, invoice, tenant)
                message += f" and emailed to {order.customer.email}"
            except MailError as e:
                return ExportResult(False, f"{message}, email failed: {e}", external_id=str(invoice.id), retryable=True)
    return ExportResult(True, message, external_id=str(invoice.id))
