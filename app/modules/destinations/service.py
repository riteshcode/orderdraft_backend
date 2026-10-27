"""
Export jobs: order -> destination, retry ke saath.

  queue_exports(order)      approve par: har active + auto_send destination ka ek job
  run_export_job(job_id)    background job: bhejo, result save, fail = retry schedule
  retry_due_jobs()          scheduler har minute: jinka next_retry_at aa gaya
"""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.crypto import MASK, decrypt, encrypt
from app.core.database import tenant_session, utcnow
from app.core.helpers import log_activity
from app.core.jobs import enqueue
from app.core.helpers import get_tenant_settings
from app.models import Destination, DraftOrder, DraftOrderLine, ExportJob
from app.modules.destinations.registry import get_module

log = logging.getLogger("exports")

RETRY_MINUTES = [1, 5, 15, 60, 240]   # attempt 1 fail -> 1 min baad, phir 5, 15...


# ---------------- Config secrets ----------------
def encrypt_config(dest_type: str, config: dict, old_config: dict | None = None) -> dict:
    """Secret fields encrypt. UI se MASK wapas aaya = purani value rakho."""
    module = get_module(dest_type)
    out = dict(config)
    for key in module.SECRET_FIELDS:
        value = out.get(key)
        if value == MASK and old_config:
            out[key] = old_config.get(key)
        elif value:
            out[key] = encrypt(value)
    return out


def decrypt_config(dest_type: str, config: dict) -> dict:
    module = get_module(dest_type)
    out = dict(config or {})
    for key in module.SECRET_FIELDS:
        if out.get(key):
            out[key] = decrypt(out[key])
    return out


def masked_config(dest_type: str, config: dict) -> dict:
    module = get_module(dest_type)
    out = dict(config or {})
    for key in module.SECRET_FIELDS:
        if out.get(key):
            out[key] = MASK
    return out


# ---------------- Jobs ----------------
def queue_exports(db: Session, order: DraftOrder, destination_ids: list[int] | None = None) -> list[ExportJob]:
    """destination_ids None = saare auto_send destinations."""
    stmt = select(Destination).where(Destination.tenant_id == order.tenant_id, Destination.is_active.is_(True))
    if destination_ids is None:
        stmt = stmt.where(Destination.auto_send.is_(True))
    else:
        stmt = stmt.where(Destination.id.in_(destination_ids))

    jobs = []
    for dest in db.scalars(stmt):
        job = ExportJob(tenant_id=order.tenant_id, order_id=order.id, destination_id=dest.id, status="pending")
        db.add(job)
        jobs.append(job)
    db.commit()
    for job in jobs:
        enqueue(run_export_job, job.tenant_id, job.id)
    # Job doosre session/thread mein update hota hai - is session ka cache purana na rahe
    db.expire_all()
    return jobs


def idempotency_key(job: ExportJob) -> str:
    """Ek export job = ek key (retry mein wahi). Manual "Send again" = naya job = nayi key (jaan-boojhkar)."""
    return f"orderdraft-{job.tenant_id}-{job.order_id}-{job.id}"


def run_export_job(tenant_id: int, job_id: int):
    """Background job: is client ke DB ka session."""
    db = tenant_session(tenant_id)
    try:
        run_export(db, job_id)
    finally:
        db.close()


def run_export(db: Session, job_id: int) -> ExportJob | None:
    job = db.get(ExportJob, job_id)
    if not job or job.status in ("success", "running"):
        return job

    job.status = "running"
    job.attempts += 1
    db.commit()

    order = db.scalars(
        select(DraftOrder)
        .options(selectinload(DraftOrder.lines).selectinload(DraftOrderLine.product),
                 selectinload(DraftOrder.customer), selectinload(DraftOrder.message))
        .where(DraftOrder.id == job.order_id)
    ).one()
    tenant = get_tenant_settings(db, job.tenant_id)   # client settings (GST, invoice prefix ...)
    dest = job.destination

    try:
        module = get_module(dest.type)
        config = decrypt_config(dest.type, dest.config)
        # Idempotency: is job ki har retry mein SAME key. Agar pehli koshish mein invoice ban gaya tha par
        # response raaste mein kho gaya (network drop / timeout), to software dobara invoice nahi banata.
        config["_idempotency_key"] = idempotency_key(job)
        config["_retry"] = job.attempts > 1
        result = module.send(db, order, config, tenant)
    except Exception as e:   # destination code mein bug - job ko atka mat do
        log.exception("Export job %s crashed", job_id)
        db.rollback()
        job = db.get(ExportJob, job_id)
        from app.modules.destinations.base import ExportResult
        result = ExportResult(False, f"Internal error: {e}", retryable=True)

    job.response_summary = result.message[:2000]
    job.external_id = result.external_id
    job.external_url = result.external_url

    if result.ok:
        job.status = "success"
        job.last_error = None
        job.next_retry_at = None
        job.finished_at = utcnow()
        if order.status == "approved":
            order.status = "exported"
        log_activity(db, job.tenant_id, None, "export.success", "order", order.id,
                     {"destination": dest.name, "message": result.message[:200]})
    else:
        job.status = "failed"
        job.last_error = result.message[:2000]
        if result.retryable and job.attempts < job.max_attempts:
            minutes = RETRY_MINUTES[min(job.attempts - 1, len(RETRY_MINUTES) - 1)]
            job.next_retry_at = utcnow() + timedelta(minutes=minutes)
        else:
            job.next_retry_at = None
            job.finished_at = utcnow()
        log_activity(db, job.tenant_id, None, "export.failed", "order", order.id,
                     {"destination": dest.name, "error": result.message[:200]})
    db.commit()
    if result.ok:
        _notify_invoice(db, order)
    return job


def _notify_invoice(db: Session, order: DraftOrder) -> None:
    """Export se invoice bani (OrderDraft ya Xero/QBO/MYOB) -> portal customer ko "invoice ready" (ek baar)."""
    from app.models import Invoice, Tenant
    from app.modules.portal.notify import notify_customer_order
    if order.source not in ("portal", "standing"):
        return
    inv = db.scalars(select(Invoice).where(Invoice.order_id == order.id, Invoice.status != "void")
                     .order_by(Invoice.id.desc())).first()
    if inv:
        notify_customer_order(db, db.get(Tenant, order.tenant_id), order, "invoiced",
                              invoice=inv.number or "", email=False)


def retry_due_jobs(db: Session) -> int:
    due = list(db.scalars(select(ExportJob.id).where(
        ExportJob.status == "failed", ExportJob.next_retry_at.is_not(None), ExportJob.next_retry_at <= utcnow()
    ).limit(100)))
    for job_id in due:
        job = db.get(ExportJob, job_id)
        job.next_retry_at = None
        db.commit()
        enqueue(run_export_job, job.tenant_id, job_id)
    return len(due)


def job_out(job: ExportJob) -> dict:
    return {
        "id": job.id, "order_id": job.order_id, "destination_id": job.destination_id,
        "destination_name": job.destination.name if job.destination else None,
        "destination_type": job.destination.type if job.destination else None,
        "status": job.status, "attempts": job.attempts, "max_attempts": job.max_attempts,
        "next_retry_at": job.next_retry_at, "last_error": job.last_error,
        "response_summary": job.response_summary, "external_id": job.external_id,
        "external_url": job.external_url, "created_at": job.created_at, "finished_at": job.finished_at,
    }
