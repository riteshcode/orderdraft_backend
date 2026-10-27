"""
Xero se products (Items) aur customers (Contacts) sync.

- Header ka "Sync" button = full sync
- Nightly scheduler = incremental (If-Modified-Since: last_sync_at)
- Match external_id (ItemID / ContactID) se hota hai; hamare learned aliases kabhi nahi chhedte
"""
import logging

from sqlalchemy.orm import Session

from app.core.database import SessionLocal, utcnow
from app.core.helpers import log_activity
from app.models import ImportJob, Invoice
from app.modules.imports.service import import_customers, import_products
from app.modules.xero import client

log = logging.getLogger("xero.sync")

NON_TAX_TYPES = {"EXEMPTOUTPUT", "BASEXCLUDED", "EXEMPTEXPORT", "NONE"}


def item_to_row(item: dict) -> dict | None:
    if item.get("IsSold") is False or not item.get("Code"):
        return None
    sales = item.get("SalesDetails") or {}
    tax_type = sales.get("TaxType")
    return {
        "sku": item["Code"],
        "name": item.get("Name") or item["Code"],
        "description": item.get("Description") or "",
        "price": sales.get("UnitPrice"),
        "account_code": sales.get("AccountCode") or "",
        "tax_type": tax_type or "",
        "is_taxable": ("no" if tax_type in NON_TAX_TYPES else "yes") if tax_type else "",
        "external_id": item["ItemID"],
    }


def contact_to_row(c: dict) -> dict:
    phone = ""
    for p in c.get("Phones") or []:
        if p.get("PhoneNumber") and p.get("PhoneType") in ("DEFAULT", "MOBILE"):
            phone = f"{p.get('PhoneAreaCode') or ''}{p['PhoneNumber']}".strip()
            break
    address = ""
    for a in c.get("Addresses") or []:
        parts = [a.get(k) for k in ("AddressLine1", "AddressLine2", "City", "Region", "PostalCode")]
        if any(parts):
            address = ", ".join(p for p in parts if p)
            break
    other = [p.get("EmailAddress") for p in c.get("ContactPersons") or [] if p.get("EmailAddress")]
    return {
        "name": c.get("Name") or "",
        "code": c.get("AccountNumber") or "",
        "email": c.get("EmailAddress") or "",
        "other_emails": ", ".join(other),
        "phone": phone,
        "address": address,
        "abn": c.get("TaxNumber") or "",
        "external_id": c["ContactID"],
        "is_active": c.get("ContactStatus", "ACTIVE") == "ACTIVE",
    }


def sync_catalog(db: Session, tenant_id: int, full: bool = True, user_id: int | None = None) -> dict:
    conn = client.get_connection(db, tenant_id)
    if not conn:
        raise client.XeroError("Xero connected nahi hai")

    started = utcnow()
    headers = {}
    if not full and conn.last_sync_at:
        headers["If-Modified-Since"] = conn.last_sync_at.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        # ---- Accounts + tax rates (product dropdowns ke liye) ----
        from app.modules.accounting.service import import_from_xero
        import_from_xero(db, tenant_id)

        # ---- Items ----
        items = client.request(db, conn, "GET", "/Items", headers=headers).get("Items", [])
        product_rows = [r for r in (item_to_row(i) for i in items) if r]
        p_result = import_products(db, tenant_id, product_rows, source="xero")

        # ---- Contacts (paged, 100 per page) ----
        contacts = []
        page = 1
        params = {"includeArchived": "true"}
        if not conn.include_all_contacts:
            params["where"] = "IsCustomer==true"
        while True:
            data = client.request(db, conn, "GET", "/Contacts", params={**params, "page": page}, headers=headers)
            batch = data.get("Contacts", [])
            contacts.extend(batch)
            if len(batch) < 100 or page >= 200:
                break
            page += 1
        c_result = import_customers(db, tenant_id, [contact_to_row(c) for c in contacts], source="xero")

        for kind, res in (("products", p_result), ("customers", c_result)):
            db.add(ImportJob(tenant_id=tenant_id, kind=kind, source="xero", filename=conn.external_name,
                             created=res.created, updated=res.updated, skipped=res.skipped,
                             errors=res.errors, user_id=user_id))
        conn.last_sync_at = started
        conn.last_sync_status = (f"OK - products +{p_result.created}/~{p_result.updated}, "
                                 f"customers +{c_result.created}/~{c_result.updated}")
        log_activity(db, tenant_id, user_id, "xero.synced", detail={"products": p_result.as_dict()["created"],
                                                                     "customers": c_result.as_dict()["created"]})
        db.commit()
        return {"products": p_result.as_dict(), "customers": c_result.as_dict()}
    except client.XeroError as e:
        db.rollback()
        conn = client.get_connection(db, tenant_id)
        conn.last_sync_status = f"Failed: {e}"[:300]
        db.commit()
        raise


def sync_job(tenant_id: int, full: bool = True, user_id: int | None = None):
    """Background job wrapper."""
    db = SessionLocal()
    try:
        sync_catalog(db, tenant_id, full=full, user_id=user_id)
    except client.XeroError as e:
        log.warning("Xero sync failed for tenant %s: %s", tenant_id, e)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Invoice status (Xero mein invoice approve / email / paid hua?)
# --------------------------------------------------------------------------
XERO_STATUS_MAP = {"DRAFT": "draft", "SUBMITTED": "draft", "AUTHORISED": "authorised",
                   "PAID": "paid", "VOIDED": "void", "DELETED": "void"}


def refresh_invoice(db: Session, invoice: Invoice) -> Invoice:
    conn = client.get_connection(db, invoice.tenant_id)
    data = client.request(db, conn, "GET", f"/Invoices/{invoice.external_id}")
    found = (data.get("Invoices") or [None])[0]
    if not found:
        return invoice
    apply_xero_invoice(invoice, found)
    db.commit()
    return invoice


def apply_xero_invoice(invoice: Invoice, x: dict):
    status = XERO_STATUS_MAP.get(x.get("Status"), invoice.status)
    if status == "authorised" and x.get("SentToContact"):
        status = "sent"
        invoice.sent_at = invoice.sent_at or utcnow()
    invoice.status = status
    invoice.number = x.get("InvoiceNumber") or invoice.number
    for ours, theirs in (("subtotal", "SubTotal"), ("gst", "TotalTax"), ("total", "Total"), ("amount_paid", "AmountPaid")):
        if x.get(theirs) is not None:
            setattr(invoice, ours, x[theirs])
    if status == "paid" and not invoice.paid_at:
        invoice.paid_at = utcnow()
    invoice.last_synced_at = utcnow()


def refresh_invoice_job(invoice_id: int):
    db = SessionLocal()
    try:
        invoice = db.get(Invoice, invoice_id)
        if invoice and invoice.source == "xero" and invoice.external_id:
            refresh_invoice(db, invoice)
    except client.XeroError as e:
        log.warning("Invoice %s refresh failed: %s", invoice_id, e)
    finally:
        db.close()
