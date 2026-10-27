"""
Destinations CRUD + test + export jobs list / retry.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db, utcnow
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity
from app.core.jobs import enqueue
from app.models import Destination, ExportJob, User
from app.modules.destinations import custom_api, webhook
from app.modules.destinations.registry import DESTINATIONS, get_module
from app.modules.destinations.service import (
    decrypt_config, encrypt_config, job_out, masked_config, run_export_job,
)

router = APIRouter(tags=["destinations"])
admin_only = require_role("owner", "admin")

SAMPLE_PAYLOAD = {
    "order_id": 0, "status": "approved",
    "customer": {"id": 1, "code": "TEST01", "name": "Test Customer", "email": "test@example.com", "external_id": None},
    "po_number": "TEST-PO", "delivery_date": None, "notes": "This is a test from OrderDraft", "total": 65.0,
    "lines": [{"sku": "TEST-SKU", "product_name": "Test product", "quantity": 2, "unit": "box",
               "unit_price": 32.5, "line_total": 65.0, "taxable": True, "original_text": "2 box test"}],
    "source_channel": "email", "created_at": None, "approved_at": None,
}


class DestinationIn(BaseModel):
    name: str
    type: str
    config: dict = {}
    auto_send: bool = True
    is_active: bool = True


def dest_out(d: Destination) -> dict:
    return {"id": d.id, "name": d.name, "type": d.type, "type_label": get_module(d.type).LABEL,
            "config": masked_config(d.type, d.config), "auto_send": d.auto_send,
            "is_active": d.is_active, "created_at": d.created_at}


def _get(db: Session, user: User, dest_id: int) -> Destination:
    d = db.get(Destination, dest_id)
    if not d or d.tenant_id != user.tenant_id:
        raise HTTPException(404, "Destination not found")
    return d


def _validated(data: DestinationIn, old: Destination | None = None) -> dict:
    if data.type not in DESTINATIONS:
        raise HTTPException(400, "Unknown destination type")
    module = get_module(data.type)
    # Secret MASK ke saath aaya = purani (decrypted) value se validate karo
    config = dict(data.config)
    if old:
        old_plain = decrypt_config(old.type, old.config)
        for key in module.SECRET_FIELDS:
            if config.get(key) == "••••••••":
                config[key] = old_plain.get(key)
    try:
        clean = module.validate_config(config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return encrypt_config(data.type, clean)


@router.get("/destinations/types")
def destination_types(user: User = Depends(current_user)):
    return [{"type": t, "label": m.LABEL} for t, m in DESTINATIONS.items()]


@router.get("/destinations")
def list_destinations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Destination).where(Destination.tenant_id == user.tenant_id).order_by(Destination.id))
    return [dest_out(d) for d in rows]


@router.post("/destinations")
def create_destination(data: DestinationIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(400, "Enter a name")
    d = Destination(tenant_id=user.tenant_id, name=data.name.strip()[:100], type=data.type,
                    config=_validated(data), auto_send=data.auto_send, is_active=data.is_active)
    db.add(d)
    db.commit()
    return dest_out(d)


@router.put("/destinations/{dest_id}")
def update_destination(dest_id: int, data: DestinationIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    d = _get(db, user, dest_id)
    if data.type != d.type:
        raise HTTPException(400, "The type can't be changed - create a new destination instead")
    d.config = _validated(data, d)
    d.name, d.auto_send, d.is_active = data.name.strip()[:100], data.auto_send, data.is_active
    db.commit()
    return dest_out(d)


@router.delete("/destinations/{dest_id}")
def delete_destination(dest_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    d = _get(db, user, dest_id)
    has_jobs = db.scalars(select(ExportJob.id).where(ExportJob.destination_id == d.id).limit(1)).first()
    if has_jobs:
        d.is_active = False      # history ke liye rakho, bas band karo
    else:
        db.delete(d)
    db.commit()
    return {"ok": True}


@router.post("/destinations/{dest_id}/test")
def test_destination(dest_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Sample order bhejke check karo ki URL/auth sahi hai (sirf webhook / custom API)."""
    d = _get(db, user, dest_id)
    config = decrypt_config(d.type, d.config)
    if d.type == "webhook":
        result = webhook.post_signed(config["url"], config.get("secret"), {"event": "test", "data": SAMPLE_PAYLOAD})
    elif d.type == "custom_api":
        import httpx
        try:
            resp = httpx.request(config["method"], config["url"], json=custom_api.build_body(SAMPLE_PAYLOAD, config),
                                 headers=custom_api.build_headers(config), timeout=20)
            ok = 200 <= resp.status_code < 300
            return {"ok": ok, "message": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": str(e)}
    elif d.type == "accounting":
        from app.modules.connectors.api import get_connection
        from app.modules.connectors.registry import get_provider
        conn = get_connection(db, user.tenant_id)
        return {"ok": bool(conn), "message": (f"Connected to {get_provider(conn.provider).label}: {conn.external_name}"
                                              if conn else "No accounting software connected")}
    else:
        return {"ok": True, "message": "Internal invoices are created inside OrderDraft"}
    return {"ok": result.ok, "message": result.message}


@router.get("/destinations/{dest_id}/preview")
def preview_body(dest_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Custom API: mapping ke baad body kaisi dikhegi."""
    d = _get(db, user, dest_id)
    if d.type != "custom_api":
        return SAMPLE_PAYLOAD
    return custom_api.build_body(SAMPLE_PAYLOAD, decrypt_config(d.type, d.config))


# ---------------- Export jobs ----------------
@router.get("/exports")
def list_exports(
    status: str | None = None,
    order_id: int | None = None,
    limit: int = Query(100, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(ExportJob).where(ExportJob.tenant_id == user.tenant_id).order_by(ExportJob.created_at.desc(), ExportJob.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(ExportJob.status == status)
    if order_id:
        stmt = stmt.where(ExportJob.order_id == order_id)
    return [job_out(j) for j in db.scalars(stmt)]


@router.post("/exports/{job_id}/retry")
def retry_export(job_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    job = db.get(ExportJob, job_id)
    if not job or job.tenant_id != user.tenant_id:
        raise HTTPException(404, "Job not found")
    if job.status == "success":
        raise HTTPException(400, "Already successful")
    job.status = "pending"
    job.next_retry_at = None
    job.max_attempts = max(job.max_attempts, job.attempts + 1)
    db.commit()
    enqueue(run_export_job, job.tenant_id, job.id)
    db.expire_all()
    return job_out(db.get(ExportJob, job_id))
