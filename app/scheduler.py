"""
Scheduler - periodic kaam:
  har 1 min   : failed exports retry (jinka time aa gaya)
  har 15 min  : standing orders - jinka order-time aa gaya unka order (Needs review)
  har 10 min  : 15 min se atke "processing" messages -> failed (retry ho sake)
  har 30 min  : accounting software (Xero/QuickBooks/MYOB) invoices ka status refresh
  har 1 ghanta: nightly catalog sync (24 ghante purana ho to), trial expiry

Har client ka DB alag hai - tenant-level tasks har ready tenant ke liye alag session mein chalte hain.
Ek client ka error baaki clients ko nahi rokta.

Local: app ke andar thread mein chalta hai (RUN_SCHEDULER_IN_APP=true)
Production: alag process ->  python -m app.scheduler
"""
import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from app.core.database import SessionLocal, tenant_session, utcnow
from app.core.jobs import enqueue
from app.models import Connection, InboundMessage, Invoice, Tenant
from app.modules.destinations.service import retry_due_jobs

log = logging.getLogger("scheduler")

TASK_INTERVALS = {          # task name -> seconds
    "retry_exports": 60,
    "standing_orders": 900,
    "stuck_messages": 600,
    "accounting_invoices": 1800,
    "accounting_sync": 3600,
    "email_domains": 3 * 3600,
    "trial_expiry": 3600,
}
_last_run: dict[str, float] = {}


# ---------------- Tenant level (client ka DB) ----------------
def retry_exports(db, tenant_id: int):
    count = retry_due_jobs(db)
    if count:
        log.info("Tenant %s: retrying %s export jobs", tenant_id, count)


def stuck_messages(db, tenant_id: int):
    cutoff = utcnow() - timedelta(minutes=15)
    for msg in db.scalars(select(InboundMessage).where(InboundMessage.status == "processing",
                                                       InboundMessage.created_at < cutoff)):
        msg.status = "failed"
        msg.error = "Processing timed out - please retry"
    db.commit()


def accounting_invoices(db, tenant_id: int):
    from app.modules.connectors.sync import refresh_invoice_job
    cutoff = utcnow() - timedelta(hours=6)
    ids = db.scalars(select(Invoice.id).where(
        Invoice.source != "internal", Invoice.external_id.is_not(None),
        Invoice.status.in_(["draft", "authorised", "sent"]),
        (Invoice.last_synced_at.is_(None)) | (Invoice.last_synced_at < cutoff),
    ).limit(50))
    for invoice_id in ids:
        enqueue(refresh_invoice_job, tenant_id, invoice_id)


def accounting_sync(db, tenant_id: int):
    from app.modules.connectors.sync import sync_job
    cutoff = utcnow() - timedelta(hours=24)
    for conn in db.scalars(select(Connection).where(Connection.status == "connected", Connection.auto_sync.is_(True))):
        if not conn.last_sync_at or conn.last_sync_at < cutoff:
            enqueue(sync_job, tenant_id, False, None)   # incremental


def email_domains(db, tenant_id: int):
    """Client ne DNS records daal diye? Pending sender domain apne aap verify."""
    from app.core.helpers import get_tenant_settings
    from app.modules.email.domains import recheck_pending
    cfg = get_tenant_settings(db, tenant_id)
    if recheck_pending(db, cfg):
        log.info("Tenant %s sender domain %s verified", tenant_id, cfg.sender_domain)
    db.commit()


def standing_orders(db, tenant_id: int):
    from app.modules.standing.service import run_due
    made = run_due(db, tenant_id)
    if made:
        log.info("Tenant %s: %s standing order(s) created", tenant_id, made)


# ---------------- Platform level (main DB) ----------------
def trial_expiry(db):
    for t in db.scalars(select(Tenant).where(Tenant.subscription_status == "trialing",
                                             Tenant.trial_ends_at < utcnow(),
                                             Tenant.stripe_subscription_id.is_(None))):
        t.subscription_status = "expired"
    db.commit()


TENANT_TASKS = {
    "retry_exports": retry_exports,
    "standing_orders": standing_orders,
    "stuck_messages": stuck_messages,
    "accounting_invoices": accounting_invoices,
    "accounting_sync": accounting_sync,
    "email_domains": email_domains,
}
PLATFORM_TASKS = {"trial_expiry": trial_expiry}


def _ready_tenant_ids() -> list[int]:
    with SessionLocal() as db:
        return list(db.scalars(select(Tenant.id).where(Tenant.db_ready.is_(True), Tenant.status == "active")))


def run_due_tasks(force: bool = False):
    now = time.time()
    due = [n for n in TASK_INTERVALS if force or now - _last_run.get(n, 0) >= TASK_INTERVALS[n]]
    for name in due:
        _last_run[name] = now

    for name in due:
        if name in PLATFORM_TASKS:
            db = SessionLocal()
            try:
                PLATFORM_TASKS[name](db)
            except Exception:
                log.exception("Scheduled task %s failed", name)
                db.rollback()
            finally:
                db.close()

    tenant_tasks = [n for n in due if n in TENANT_TASKS]
    if not tenant_tasks:
        return
    for tenant_id in _ready_tenant_ids():
        for name in tenant_tasks:
            db = tenant_session(tenant_id)
            try:
                TENANT_TASKS[name](db, tenant_id)
            except Exception:
                log.exception("Scheduled task %s failed for tenant %s", name, tenant_id)
                db.rollback()
            finally:
                db.close()


def run_forever():
    log.info("Scheduler started")
    while True:
        run_due_tasks()
        time.sleep(20)


def start_in_background():
    threading.Thread(target=run_forever, daemon=True, name="scheduler").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
