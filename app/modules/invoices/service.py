"""
`tenant` parameter = client ki settings (TenantSettings: gst_rate, invoice_prefix, business_name ...)

Invoice logic: GST calculation, numbering, order -> invoice, email, payments, void.

GST (Australia, default 10%):
  prices_include_gst = False:  subtotal = sum(qty x price), gst = 10% of taxable lines, total = subtotal + gst
  prices_include_gst = True :  total = sum(qty x price), gst = taxable x 10/110, subtotal = total - gst
"""
import html
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.helpers import get_tenant_settings, log_activity, money
from app.core.mailer import send_email, sender_for, simple_html
from app.models import DraftOrder, Invoice, InvoiceLine, Payment, Product, TenantSettings
from app.modules.accounting.service import resolve_tax, tax_rate_map


def calculate_lines(tenant: TenantSettings, items: list[dict]) -> tuple[list[dict], Decimal, Decimal, Decimal]:
    """
    items = [{"product_id", "sku", "description", "quantity", "unit_price",
              "taxable", "tax_rate" (optional %), "tax_code", "account_code"}]
    tax_rate diya hai to wahi lagta hai, warna taxable ? tenant.gst_rate : 0
    Returns (lines, subtotal, gst, total)
    """
    default_rate = Decimal(str(tenant.gst_rate or 0))
    lines, gross_total, gst_total = [], Decimal("0"), Decimal("0")

    for item in items:
        qty = Decimal(str(item["quantity"] or 0))
        price = money(item.get("unit_price") or 0)
        line_total = money(qty * price)
        if item.get("tax_rate") is not None:
            rate = Decimal(str(item["tax_rate"]))
        else:
            rate = default_rate if item.get("taxable", True) else Decimal("0")
        if rate:
            if tenant.prices_include_gst:
                gst = money(line_total * rate / (100 + rate))
            else:
                gst = money(line_total * rate / 100)
        else:
            gst = Decimal("0.00")
        lines.append({**item, "unit_price": price, "line_total": line_total, "gst_amount": gst, "tax_rate": float(rate)})
        gross_total += line_total
        gst_total += gst

    if tenant.prices_include_gst:
        total = gross_total
        subtotal = total - gst_total
    else:
        subtotal = gross_total
        total = subtotal + gst_total
    return lines, money(subtotal), money(gst_total), money(total)


def order_items(order: DraftOrder, tenant: TenantSettings, rates: dict[str, float]) -> list[dict]:
    """Order lines -> invoice items. Product ka tax_type TaxRate table mein ho to uska rate."""
    items = []
    for l in order.lines:
        if not l.product:
            continue
        price = l.unit_price if l.unit_price is not None else l.product.price
        tax_code, rate = resolve_tax(tenant, rates, l.product.tax_type, l.product.is_taxable)
        items.append({
            "product_id": l.product_id,
            "sku": l.product.sku,
            "description": l.product.name,
            "quantity": l.quantity,
            "unit_price": price or 0,
            "taxable": l.product.is_taxable,
            "tax_code": tax_code,
            "tax_rate": rate,
            "account_code": l.product.account_code,
        })
    return items


def next_invoice_number(db: Session, tenant_id: int) -> str:
    """Settings row lock karke number lo - do invoices ko same number na mile (MySQL)."""
    tenant = get_tenant_settings(db, tenant_id, for_update=True)
    number = f"{tenant.invoice_prefix}{tenant.invoice_next_number}"
    tenant.invoice_next_number += 1
    return number


def create_invoice_from_order(
    db: Session, order: DraftOrder, tenant: TenantSettings, source: str = "internal",
    external_id: str | None = None, number: str | None = None, status: str = "draft",
) -> Invoice:
    lines, subtotal, gst, total = calculate_lines(tenant, order_items(order, tenant, tax_rate_map(db, tenant.tenant_id)))
    if not lines:
        raise HTTPException(400, "This order has no lines that can be invoiced")

    today = date.today()
    invoice = Invoice(
        tenant_id=tenant.tenant_id, order_id=order.id, customer_id=order.customer_id,
        source=source, external_id=external_id,
        number=number or (next_invoice_number(db, tenant.tenant_id) if source == "internal" else None),
        reference=order.po_number, status=status,
        issue_date=today, due_date=today + timedelta(days=tenant.payment_terms_days or 0),
        subtotal=subtotal, gst=gst, total=total, amount_paid=0,
    )
    invoice.lines = [
        InvoiceLine(product_id=l["product_id"], sku=l["sku"], description=l["description"][:500],
                    quantity=l["quantity"], unit_price=l["unit_price"], account_code=l.get("account_code"),
                    tax_code=l.get("tax_code"), tax_rate=l["tax_rate"],
                    gst_amount=l["gst_amount"], line_total=l["line_total"])
        for l in lines
    ]
    db.add(invoice)
    db.flush()
    return invoice


def send_invoice(db: Session, invoice: Invoice, tenant: TenantSettings, to: str | None = None,
                 message: str | None = None, user_id: int | None = None) -> Invoice:
    from app.modules.invoices.pdf import invoice_pdf   # yahan import (circular se bachne ke liye)

    if invoice.source != "internal":
        raise HTTPException(400, "This invoice is in your accounting software - please send it from there")
    if invoice.status == "void":
        raise HTTPException(400, "A void invoice can't be sent")
    to = to or (invoice.customer.email if invoice.customer else None)
    if not to:
        raise HTTPException(400, "This customer has no email address - please enter one")

    pdf = invoice_pdf(invoice, tenant)
    esc = html.escape          # customer / business ke naam mein & < > ho to email HTML na toote
    body = message or (
        f"<p>Hi {esc(invoice.customer.name) if invoice.customer else ''},</p>"
        f"<p>Please find attached tax invoice <b>{esc(invoice.number or '')}</b> for <b>${invoice.total:,.2f}</b>"
        f"{', due ' + invoice.due_date.strftime('%d/%m/%Y') if invoice.due_date else ''}.</p>"
        f"<p>Thank you for your business.</p><p>{esc(tenant.name)}</p>"
    )
    sender, reply_to = sender_for(tenant)    # client ka verified domain, warna "X via OrderDraft"
    send_email(
        to, f"Tax invoice {invoice.number} from {tenant.name}",
        simple_html(f"Invoice {invoice.number}", body),
        attachments=[(f"{invoice.number}.pdf", pdf, "application/pdf")],
        reply_to=reply_to, from_email=sender,
    )
    if invoice.status in ("draft", "authorised"):
        invoice.status = "sent"
    invoice.sent_at = utcnow()
    log_activity(db, tenant.tenant_id, user_id, "invoice.sent", "invoice", invoice.id, {"to": to})
    db.commit()
    return invoice


def record_payment(db: Session, invoice: Invoice, amount: float, paid_on: date,
                   method: str | None = None, reference: str | None = None, user_id: int | None = None) -> Invoice:
    if invoice.source != "internal":
        raise HTTPException(400, "Record payments for Xero invoices in Xero")
    if invoice.status == "void":
        raise HTTPException(400, "Void invoice")
    amount = money(amount)
    if amount <= 0:
        raise HTTPException(400, "Amount must be greater than 0")
    balance = money(invoice.total) - money(invoice.amount_paid)
    if amount > balance:
        raise HTTPException(400, f"Amount is more than the balance (${balance})")

    invoice.payments.append(Payment(amount=amount, paid_on=paid_on, method=method, reference=reference))
    invoice.amount_paid = money(invoice.amount_paid) + amount
    if money(invoice.amount_paid) >= money(invoice.total):
        invoice.status = "paid"
        invoice.paid_at = utcnow()
    log_activity(db, invoice.tenant_id, user_id, "invoice.payment", "invoice", invoice.id, {"amount": float(amount)})
    db.commit()
    return invoice


def void_invoice(db: Session, invoice: Invoice, user_id: int | None = None) -> Invoice:
    if invoice.source != "internal":
        raise HTTPException(400, "Void Xero invoices in Xero")
    if money(invoice.amount_paid) > 0:
        raise HTTPException(400, "A payment has already been recorded, so this invoice can't be voided")
    invoice.status = "void"
    log_activity(db, invoice.tenant_id, user_id, "invoice.voided", "invoice", invoice.id)
    db.commit()
    return invoice


def is_overdue(invoice: Invoice) -> bool:
    return bool(invoice.due_date and invoice.due_date < date.today()
                and invoice.status in ("authorised", "sent"))


# --------------------------------------------------------------------------
# Manual invoice (bina order ke) - form se banaya hua
# --------------------------------------------------------------------------
def manual_items(db: Session, tenant: TenantSettings, lines_in: list[dict], customer_id: int | None = None) -> list[dict]:
    """
    lines_in = [{"product_id"?, "description", "quantity", "unit_price", "tax_code"?, "account_code"?}]
    tax_code:  None = product / tenant default,  "" = no tax,  "OUTPUT" = us code ka rate
    """
    from app.modules.catalog.pricing import price_map
    rates = tax_rate_map(db, tenant.tenant_id)
    special = price_map(db, customer_id)          # customer ki pantry price (price khali chhoda ho to)
    items = []
    for n, l in enumerate(lines_in, start=1):
        product = None
        if l.get("product_id"):
            product = db.get(Product, l["product_id"])
            if not product or product.tenant_id != tenant.tenant_id:
                raise HTTPException(400, f"Line {n}: product invalid")
        description = (l.get("description") or (product.name if product else "")).strip()
        if not description:
            raise HTTPException(400, f"Line {n}: enter a description")
        if float(l.get("quantity") or 0) <= 0:
            raise HTTPException(400, f"Line {n}: quantity must be greater than 0")
        if float(l.get("unit_price") or 0) < 0:
            raise HTTPException(400, f"Line {n}: price can't be negative")

        tax_code = l.get("tax_code")
        if tax_code == "":
            tax_code, rate = None, 0.0
        else:
            default_code = tax_code if tax_code is not None else (product.tax_type if product else None)
            taxable = product.is_taxable if product else True
            tax_code, rate = resolve_tax(tenant, rates, default_code, taxable)

        items.append({
            "product_id": product.id if product else None,
            "sku": product.sku if product else None,
            "description": description[:500],
            "quantity": float(l["quantity"]),
            "unit_price": l.get("unit_price") if l.get("unit_price") is not None
            else (special.get(product.id, product.price) if product else 0),
            "tax_code": tax_code,
            "tax_rate": rate,
            "account_code": l.get("account_code") or (product.account_code if product else None),
        })
    if not items:
        raise HTTPException(400, "Add at least one line")
    return items


def _set_lines(invoice: Invoice, tenant: TenantSettings, items: list[dict]):
    lines, subtotal, gst, total = calculate_lines(tenant, items)
    invoice.lines = [
        InvoiceLine(product_id=l["product_id"], sku=l["sku"], description=l["description"],
                    quantity=l["quantity"], unit_price=l["unit_price"], account_code=l.get("account_code"),
                    tax_code=l.get("tax_code"), tax_rate=l["tax_rate"],
                    gst_amount=l["gst_amount"], line_total=l["line_total"])
        for l in lines
    ]
    invoice.subtotal, invoice.gst, invoice.total = subtotal, gst, total


def save_manual_invoice(db: Session, tenant: TenantSettings, data: dict, invoice: Invoice | None = None,
                        user_id: int | None = None) -> Invoice:
    """Naya manual invoice banao, ya draft invoice edit karo (invoice diya ho to)."""
    from app.models import Customer

    customer = db.get(Customer, data["customer_id"]) if data.get("customer_id") else None
    if not customer or customer.tenant_id != tenant.tenant_id:
        raise HTTPException(400, "Choose a customer")
    items = manual_items(db, tenant, data.get("lines") or [], customer.id)

    issue_date = data.get("issue_date") or date.today()
    due_date = data.get("due_date") or issue_date + timedelta(days=tenant.payment_terms_days or 0)
    if due_date < issue_date:
        raise HTTPException(400, "Due date can't be before the issue date")

    if invoice is None:
        from app.modules.billing.limits import subscription_ok
        from app.models import Tenant
        ok, reason = subscription_ok(db.get(Tenant, tenant.tenant_id))
        if not ok:
            raise HTTPException(402, reason)
        invoice = Invoice(tenant_id=tenant.tenant_id, source="internal", status="draft",
                          number=next_invoice_number(db, tenant.tenant_id), amount_paid=0)
        db.add(invoice)
        action = "invoice.created_manual"
    else:
        if invoice.source != "internal" or invoice.status != "draft" or money(invoice.amount_paid) > 0:
            raise HTTPException(400, "Only draft invoices with no payments can be edited")
        action = "invoice.edited"

    invoice.customer_id = customer.id
    invoice.issue_date, invoice.due_date = issue_date, due_date
    invoice.reference = (data.get("reference") or "").strip()[:100] or None
    invoice.notes = data.get("notes")
    invoice.customer = customer
    _set_lines(invoice, tenant, items)
    db.flush()
    log_activity(db, tenant.tenant_id, user_id, action, "invoice", invoice.id, {"total": float(invoice.total)})
    db.commit()
    return invoice
