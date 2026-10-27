"""
Xero destination - approved order ko Xero mein Invoice ya Quote banana.

config:
{
  "document_type": "invoice_draft",   # invoice_draft | invoice_authorised | quote
  "default_account_code": "200",      # product ka account_code na ho tab
  "default_tax_type": "",             # khali = Xero account ka default
  "send_email": false                 # authorised invoice Xero se customer ko email karo
}

Item codes: sirf Xero se sync hue products ka ItemCode bhejte hain (CSV wale products
Xero mein exist na karein to Xero error deta hai) - baaki ke liye description + account code.
"""
from datetime import date, timedelta

from app.core.helpers import to_float
from app.models import DraftOrder, Tenant
from app.modules.destinations.base import ExportResult
from app.modules.invoices.service import create_invoice_from_order
from app.modules.xero import client
from app.modules.xero.sync import apply_xero_invoice

TYPE = "xero"
LABEL = "Xero"
SECRET_FIELDS: list[str] = []
DOC_TYPES = ("invoice_draft", "invoice_authorised", "quote")


def validate_config(config: dict) -> dict:
    doc_type = config.get("document_type") or "invoice_draft"
    if doc_type not in DOC_TYPES:
        raise ValueError("document_type galat hai")
    return {
        "document_type": doc_type,
        "default_account_code": (config.get("default_account_code") or "200").strip(),
        "default_tax_type": (config.get("default_tax_type") or "").strip(),
        "send_email": bool(config.get("send_email")) and doc_type == "invoice_authorised",
    }


def build_document(order: DraftOrder, tenant: Tenant, config: dict) -> dict:
    customer = order.customer
    if customer and customer.external_id and customer.source == "xero":
        contact = {"ContactID": customer.external_id}
    else:
        # Xero is naam ka contact dhundhta hai, na mile to naya bana deta hai
        contact = {"Name": customer.name if customer else (order.customer_raw or "Unknown customer")}
        if customer and customer.email:
            contact["EmailAddress"] = customer.email

    line_items = []
    for line in order.lines:
        p = line.product
        if not p:
            continue
        item = {
            "Description": p.name,
            "Quantity": line.quantity,
            "AccountCode": p.account_code or config.get("default_account_code") or "200",
        }
        if p.source == "xero":
            item["ItemCode"] = p.sku
        price = line.unit_price if line.unit_price is not None else p.price
        if price is not None:
            item["UnitAmount"] = to_float(price)
        tax_type = p.tax_type or config.get("default_tax_type")
        if tax_type:
            item["TaxType"] = tax_type
        line_items.append(item)

    today = date.today()
    doc = {
        "Contact": contact,
        "Date": today.isoformat(),
        "LineItems": line_items,
        "Reference": order.po_number or f"OD-{order.id}",
        "LineAmountTypes": "Inclusive" if tenant.prices_include_gst else "Exclusive",
    }
    if config["document_type"] == "quote":
        doc["Status"] = "DRAFT"
        if order.delivery_date:
            doc["ExpiryDate"] = order.delivery_date.isoformat()
    else:
        doc["Type"] = "ACCREC"
        doc["Status"] = "AUTHORISED" if config["document_type"] == "invoice_authorised" else "DRAFT"
        doc["DueDate"] = (today + timedelta(days=tenant.payment_terms_days or 0)).isoformat()
    return doc


def send(db, order: DraftOrder, config: dict, tenant: Tenant) -> ExportResult:
    conn = client.get_connection(db, tenant.id)
    if not conn:
        return ExportResult(False, "Xero connected nahi hai - Integrations page se connect karo")

    doc = build_document(order, tenant, config)
    if not doc["LineItems"]:
        return ExportResult(False, "Koi matched line nahi hai")

    is_quote = config["document_type"] == "quote"
    try:
        if is_quote:
            data = client.request(db, conn, "POST", "/Quotes", json={"Quotes": [doc]})
            created = data["Quotes"][0]
            return ExportResult(True, f"Quote {created.get('QuoteNumber', '')} created in Xero",
                                external_id=created.get("QuoteID"))

        data = client.request(db, conn, "POST", "/Invoices", json={"Invoices": [doc]})
        created = data["Invoices"][0]
        invoice_id = created["InvoiceID"]

        if config.get("send_email"):
            # 204 No Content aata hai - request() {} return karega
            client.request(db, conn, "POST", f"/Invoices/{invoice_id}/Email", json={})
            created["SentToContact"] = True
    except client.XeroError as e:
        return ExportResult(False, str(e), retryable=e.retryable)

    # Contact id yaad rakho (agli baar naam se dhundhna nahi padega)
    contact_id = (created.get("Contact") or {}).get("ContactID")
    if order.customer and contact_id and not order.customer.external_id:
        order.customer.external_id = contact_id

    # Invoice tracking row (invoice module + dashboard)
    invoice = create_invoice_from_order(db, order, tenant, source="xero", external_id=invoice_id,
                                        number=created.get("InvoiceNumber"))
    apply_xero_invoice(invoice, created)

    return ExportResult(
        True, f"Invoice {created.get('InvoiceNumber') or ''} created in Xero ({created.get('Status')})",
        external_id=invoice_id,
        external_url=f"https://go.xero.com/AccountsReceivable/View.aspx?InvoiceID={invoice_id}",
    )
