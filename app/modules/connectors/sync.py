"""
Connected accounting software se data sync - har provider ke liye same code.

- Header ka "Sync" button = full sync
- Nightly scheduler = incremental (last_sync_at ke baad badla hua)
- Products/customers external_id se match hote hain; hamare learned aliases kabhi nahi chhedte
- Accounts + tax rates (product dropdowns, invoice tax) bhi saath mein
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import no_audit
from app.core.database import tenant_session, utcnow
from app.core.helpers import log_activity
from app.models import AccountCode, ImportJob, Invoice, TaxRate
from app.modules.connectors.api import ApiSession, open_api
from app.modules.connectors.base import ConnectorError, InvoiceStatus
from app.modules.imports.service import import_customers, import_products, import_suppliers

log = logging.getLogger("connectors.sync")


def import_chart(db: Session, tenant_id: int, provider_key: str, accounts: list[dict], tax_rates: list[dict]) -> dict:
    """Accounts + tax rates upsert (code se match). Doosre purane software ke rows inactive."""
    def upsert(model, rows: list[dict], fields: tuple[str, ...]) -> dict:
        existing = {r.code: r for r in db.scalars(select(model).where(model.tenant_id == tenant_id))}
        created = updated = 0
        seen = set()
        for x in rows:
            code = (x.get("code") or "").strip()[:50]
            if not code or code in seen:
                continue
            seen.add(code)
            row = existing.get(code)
            if row:
                updated += 1
            else:
                row = model(tenant_id=tenant_id, code=code)
                db.add(row)
                existing[code] = row
                created += 1
            for f in fields:
                if f in x and x[f] is not None:
                    setattr(row, f, x[f])
            row.name = (x.get("name") or code)[:200]
            row.source = provider_key
            row.external_id = x.get("external_id")
            row.is_active = x.get("is_active", True)
        # Pehle kisi aur software se aaye codes ab kaam ke nahi (client ne software badla)
        for row in existing.values():
            if row.source not in ("manual", provider_key):
                row.is_active = False
        return {"created": created, "updated": updated}

    result = {
        "accounts": upsert(AccountCode, accounts, ("account_class",)),
        "tax_rates": upsert(TaxRate, tax_rates, ("rate", "applies_to")),
    }
    db.flush()
    return result


def import_chart_from_connection(db: Session, tenant_id: int) -> dict:
    api = open_api(db, tenant_id)
    with no_audit(db):
        result = import_chart(db, tenant_id, api.provider.key,
                              api.provider.fetch_accounts(api), api.provider.fetch_tax_rates(api))
        db.flush()
    db.commit()
    return result


def sync_catalog(db: Session, tenant_id: int, full: bool = True, user_id: int | None = None) -> dict:
    api = open_api(db, tenant_id)
    provider, conn = api.provider, api.conn
    started = utcnow()
    since = None if full or not conn.last_sync_at else conn.last_sync_at

    try:
        with no_audit(db):   # sync = bulk: har row ka audit nahi, neeche "accounting.synced" summary
            chart = import_chart(db, tenant_id, provider.key, provider.fetch_accounts(api), provider.fetch_tax_rates(api))
            p_result = import_products(db, tenant_id, provider.fetch_products(api, since), source=provider.key)
            c_result = import_customers(db, tenant_id, provider.fetch_customers(api, since, conn.include_all_contacts),
                                        source=provider.key)
            s_result = import_suppliers(db, tenant_id, provider.fetch_suppliers(api, since), source=provider.key)
            db.flush()

        for kind, res in (("products", p_result), ("customers", c_result), ("suppliers", s_result)):
            db.add(ImportJob(tenant_id=tenant_id, kind=kind, source=provider.key, filename=conn.external_name,
                             created=res.created, updated=res.updated, skipped=res.skipped,
                             errors=res.errors, user_id=user_id))
        conn.last_sync_at = started
        conn.last_sync_status = (f"OK - products +{p_result.created}/~{p_result.updated}, "
                                 f"customers +{c_result.created}/~{c_result.updated}, "
                                 f"suppliers +{s_result.created}/~{s_result.updated}")
        log_activity(db, tenant_id, user_id, "accounting.synced",
                     detail={"provider": provider.key, "products": p_result.created, "customers": c_result.created,
                             "suppliers": s_result.created})
        db.commit()
        return {"provider": provider.key, "products": p_result.as_dict(), "customers": c_result.as_dict(),
                "suppliers": s_result.as_dict(), **chart}
    except ConnectorError as e:
        db.rollback()
        conn = api.conn
        conn.last_sync_status = f"Failed: {e}"[:300]
        db.commit()
        raise


def sync_job(tenant_id: int, full: bool = True, user_id: int | None = None):
    """Background job wrapper (client ka apna DB session)."""
    db = tenant_session(tenant_id)
    try:
        sync_catalog(db, tenant_id, full=full, user_id=user_id)
    except ConnectorError as e:
        log.warning("Accounting sync failed for tenant %s: %s", tenant_id, e)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Invoice status (software mein invoice approve / email / paid hua?)
# --------------------------------------------------------------------------
def apply_status(invoice: Invoice, st: InvoiceStatus):
    if st.status == "sent" and not invoice.sent_at:
        invoice.sent_at = utcnow()
    invoice.status = st.status
    invoice.number = st.number or invoice.number
    for ours, theirs in (("subtotal", st.subtotal), ("gst", st.gst), ("total", st.total), ("amount_paid", st.amount_paid)):
        if theirs is not None:
            setattr(invoice, ours, theirs)
    if st.status == "paid" and not invoice.paid_at:
        invoice.paid_at = utcnow()
    invoice.last_synced_at = utcnow()


def refresh_invoice(db: Session, invoice: Invoice, api: ApiSession | None = None) -> Invoice:
    api = api or open_api(db, invoice.tenant_id)
    if invoice.source != api.provider.key:
        raise ConnectorError("This invoice isn't from your currently connected accounting software")
    st = api.provider.fetch_invoice(api, invoice.external_id)
    if st:
        apply_status(invoice, st)
        db.commit()
    return invoice


def refresh_invoice_job(tenant_id: int, invoice_id: int):
    db = tenant_session(tenant_id)
    try:
        invoice = db.get(Invoice, invoice_id)
        if invoice and invoice.source != "internal" and invoice.external_id:
            refresh_invoice(db, invoice)
    except ConnectorError as e:
        log.warning("Invoice %s refresh failed: %s", invoice_id, e)
    finally:
        db.close()
