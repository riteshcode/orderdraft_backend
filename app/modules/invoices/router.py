"""
Invoices: list (filters), detail, PDF, send, payment, void, refresh (Xero), summary.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.core.database import get_db
from app.core.text import content_disposition
from app.core.deps import current_user, require_role
from app.core.helpers import get_tenant_settings, to_float
from app.core.mailer import MailError
from app.core.sorting import apply_sort
from app.models import Customer, Invoice, User
from app.modules.invoices import service
from app.modules.invoices.pdf import invoice_pdf
from app.modules.connectors.base import ConnectorError
from app.modules.connectors.registry import PROVIDERS
from app.modules.connectors.sync import refresh_invoice

router = APIRouter(prefix="/invoices", tags=["invoices"])
admin_only = require_role("owner", "admin")


class SendIn(BaseModel):
    to: str | None = None
    message: str | None = None


class PaymentIn(BaseModel):
    amount: float
    paid_on: date
    method: str | None = None
    reference: str | None = None


class ManualLineIn(BaseModel):
    product_id: int | None = None
    description: str | None = None
    quantity: float
    unit_price: float | None = None
    tax_code: str | None = None       # None = default, "" = no tax
    account_code: str | None = None


class ManualInvoiceIn(BaseModel):
    customer_id: int
    issue_date: date | None = None
    due_date: date | None = None
    reference: str | None = None
    notes: str | None = None
    lines: list[ManualLineIn]
    send_email: bool = False          # save hote hi customer ko email


class InvoiceUpdate(BaseModel):
    due_date: date | None = None
    notes: str | None = None


def invoice_out(inv: Invoice, full: bool = False) -> dict:
    data = {
        "id": inv.id, "number": inv.number, "source": inv.source, "status": inv.status,
        "overdue": service.is_overdue(inv), "order_id": inv.order_id,
        "customer_id": inv.customer_id, "customer_name": inv.customer.name if inv.customer else None,
        "customer_email": inv.customer.email if inv.customer else None,
        "reference": inv.reference, "issue_date": inv.issue_date, "due_date": inv.due_date,
        "subtotal": to_float(inv.subtotal), "gst": to_float(inv.gst), "total": to_float(inv.total),
        "amount_paid": to_float(inv.amount_paid),
        "balance": round(float(inv.total or 0) - float(inv.amount_paid or 0), 2),
        "sent_at": inv.sent_at, "paid_at": inv.paid_at, "external_id": inv.external_id,
        "external_url": (PROVIDERS[inv.source].invoice_url(None, inv.external_id)
                         if inv.source in PROVIDERS and inv.external_id else None),
        "created_at": inv.created_at,
    }
    if full:
        data["notes"] = inv.notes
        data["last_synced_at"] = inv.last_synced_at
        data["lines"] = [
            {"id": l.id, "product_id": l.product_id, "sku": l.sku, "description": l.description,
             "quantity": l.quantity, "unit_price": to_float(l.unit_price), "account_code": l.account_code,
             "tax_code": l.tax_code, "tax_rate": l.tax_rate, "gst_amount": to_float(l.gst_amount),
             "line_total": to_float(l.line_total)}
            for l in inv.lines
        ]
        data["payments"] = [
            {"id": p.id, "amount": to_float(p.amount), "paid_on": p.paid_on, "method": p.method, "reference": p.reference}
            for p in inv.payments
        ]
    return data


def _get(db: Session, user: User, invoice_id: int) -> Invoice:
    inv = db.scalars(
        select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.customer))
        .where(Invoice.id == invoice_id, Invoice.tenant_id == user.tenant_id)
    ).first()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@router.get("")
def list_invoices(
    status: str | None = None,       # draft|authorised|sent|paid|void|overdue|unpaid
    source: str | None = None,       # internal|xero
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = "",                  # number | issue_date | due_date | customer | total | balance | status (-x = desc)
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Invoice).where(Invoice.tenant_id == user.tenant_id)
    if status == "overdue":
        stmt = stmt.where(Invoice.status.in_(["authorised", "sent"]), Invoice.due_date < date.today())
    elif status == "unpaid":
        stmt = stmt.where(Invoice.status.in_(["draft", "authorised", "sent"]))
    elif status:
        stmt = stmt.where(Invoice.status == status)
    if source:
        stmt = stmt.where(Invoice.source == source)
    if q:
        stmt = stmt.outerjoin(Customer, Customer.id == Invoice.customer_id).where(
            or_(Invoice.number.ilike(f"%{q}%"), Invoice.reference.ilike(f"%{q}%"), Customer.name.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sort_customer = aliased(Customer)
    if sort.lstrip("-") == "customer":
        stmt = stmt.outerjoin(sort_customer, sort_customer.id == Invoice.customer_id)
    sorts = {"number": Invoice.number, "issue_date": Invoice.issue_date, "due_date": Invoice.due_date,
             "customer": sort_customer.name, "total": Invoice.total, "balance": Invoice.total - Invoice.amount_paid,
             "status": Invoice.status, "source": Invoice.source, "created_at": Invoice.created_at}
    stmt = apply_sort(stmt, sort, sorts, default=Invoice.created_at, tiebreak=Invoice.id, default_desc=True)
    items = db.scalars(stmt.options(selectinload(Invoice.customer)).offset((page - 1) * page_size).limit(page_size))
    return {"items": [invoice_out(i) for i in items], "total": total, "page": page, "page_size": page_size}


@router.post("")
def create_manual(data: ManualInvoiceIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Bina order ke invoice banana (form se)."""
    tenant = get_tenant_settings(db, user.tenant_id)   # client settings
    payload = data.model_dump()
    payload["lines"] = [l.model_dump() for l in data.lines]
    inv = service.save_manual_invoice(db, tenant, payload, user_id=user.id)
    if data.send_email:
        try:
            service.send_invoice(db, _get(db, user, inv.id), tenant, user_id=user.id)
        except MailError as e:
            raise HTTPException(400, f"Invoice {inv.number} was saved, but the email couldn't be sent: {e}")
    return invoice_out(_get(db, user, inv.id), full=True)


@router.put("/{invoice_id}/draft")
def edit_draft(invoice_id: int, data: ManualInvoiceIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Draft invoice ki lines / customer / dates badlo."""
    inv = _get(db, user, invoice_id)
    payload = data.model_dump()
    payload["lines"] = [l.model_dump() for l in data.lines]
    service.save_manual_invoice(db, get_tenant_settings(db, user.tenant_id), payload, invoice=inv, user_id=user.id)
    return invoice_out(_get(db, user, invoice_id), full=True)


@router.get("/summary")
def summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Invoice.status, func.count(), func.coalesce(func.sum(Invoice.total), 0),
               func.coalesce(func.sum(Invoice.amount_paid), 0))
        .where(Invoice.tenant_id == user.tenant_id).group_by(Invoice.status)
    ).all()
    by_status = {s: {"count": c, "total": float(t), "paid": float(p)} for s, c, t, p in rows}
    overdue = db.execute(
        select(func.count(), func.coalesce(func.sum(Invoice.total - Invoice.amount_paid), 0))
        .where(Invoice.tenant_id == user.tenant_id, Invoice.status.in_(["authorised", "sent"]),
               Invoice.due_date < date.today())
    ).one()
    outstanding = sum(v["total"] - v["paid"] for s, v in by_status.items() if s in ("authorised", "sent"))
    return {
        "by_status": by_status,
        "outstanding": round(outstanding, 2),
        "overdue_count": overdue[0],
        "overdue_amount": round(float(overdue[1]), 2),
        "paid_total": round(by_status.get("paid", {}).get("total", 0), 2),
    }


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return invoice_out(_get(db, user, invoice_id), full=True)


@router.put("/{invoice_id}")
def update_invoice(invoice_id: int, data: InvoiceUpdate, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    inv = _get(db, user, invoice_id)
    if inv.source != "internal" or inv.status in ("paid", "void"):
        raise HTTPException(400, "This invoice can't be edited")
    inv.due_date, inv.notes = data.due_date, data.notes
    db.commit()
    return invoice_out(inv, full=True)


@router.get("/{invoice_id}/pdf")
def download_pdf(invoice_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = _get(db, user, invoice_id)
    if inv.source != "internal":
        raise HTTPException(400, "Download the PDF for this Xero invoice from Xero")
    pdf = invoice_pdf(inv, get_tenant_settings(db, user.tenant_id))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition(f"{inv.number}.pdf", inline=True)})


@router.post("/{invoice_id}/send")
def send(invoice_id: int, data: SendIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    inv = _get(db, user, invoice_id)
    try:
        service.send_invoice(db, inv, get_tenant_settings(db, user.tenant_id), to=data.to, user_id=user.id)
    except MailError as e:
        raise HTTPException(400, str(e))
    return invoice_out(_get(db, user, invoice_id), full=True)


@router.post("/{invoice_id}/payments")
def add_payment(invoice_id: int, data: PaymentIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    inv = _get(db, user, invoice_id)
    service.record_payment(db, inv, data.amount, data.paid_on, data.method, data.reference, user.id)
    return invoice_out(_get(db, user, invoice_id), full=True)


@router.post("/{invoice_id}/void")
def void(invoice_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    inv = _get(db, user, invoice_id)
    service.void_invoice(db, inv, user.id)
    return invoice_out(inv, full=True)


@router.post("/{invoice_id}/refresh")
def refresh(invoice_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Accounting software (Xero / QuickBooks / MYOB) se latest status lao."""
    inv = _get(db, user, invoice_id)
    if inv.source == "internal" or not inv.external_id:
        raise HTTPException(400, "Only invoices from your accounting software can be refreshed")
    try:
        refresh_invoice(db, inv)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    return invoice_out(_get(db, user, invoice_id), full=True)
