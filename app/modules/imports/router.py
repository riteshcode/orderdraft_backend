"""
CSV/XLSX import wizard - 3 steps:
  1. POST /imports/{kind}/upload        -> headers + preview + suggested mapping
  2. POST /imports/{kind}/run dry_run=true  -> "142 create, 38 update, 3 errors" (kuch save nahi)
  3. POST /imports/{kind}/run dry_run=false -> asli import (+ mapping template save)
kind = products | customers | suppliers | categories | history
"""
import os
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.audit import no_audit
from app.core.database import get_db
from app.core.deps import require_role
from app.core.helpers import log_activity
from app.models import ImportJob, MappingTemplate, User
from app.modules.imports import service
from app.modules.imports.fields import FIELDS, field_keys, required_keys
from app.modules.imports.reader import ReadError, headers_signature, read_table, suggest_mapping

router = APIRouter(prefix="/imports", tags=["imports"])
admin_only = require_role("owner", "admin")

MAX_BYTES = 20 * 1024 * 1024
KINDS = ("products", "customers", "suppliers", "categories", "history")


class RunIn(BaseModel):
    upload_id: str
    mapping: dict[str, str | None]
    dry_run: bool = False
    save_template: bool = True
    template_name: str | None = None


def _check_kind(kind: str):
    if kind not in KINDS:
        raise HTTPException(404, "Unknown import type")


def _upload_dir(tenant_id: int) -> str:
    path = os.path.join(settings.STORAGE_DIR, str(tenant_id), "imports")
    os.makedirs(path, exist_ok=True)
    return path


def _upload_path(tenant_id: int, upload_id: str) -> str:
    # upload_id sirf hamara banaya naam ho sakta hai (path traversal se bachao)
    if not re.fullmatch(r"[a-f0-9]{32}__[\w\-. ]{1,150}", upload_id):
        raise HTTPException(400, "Invalid upload id")
    path = os.path.join(_upload_dir(tenant_id), upload_id)
    if not os.path.exists(path):
        raise HTTPException(400, "The upload has expired - please upload the file again")
    return path


@router.get("/fields/{kind}")
def get_fields(kind: str, user: User = Depends(admin_only)):
    _check_kind(kind)
    return FIELDS[kind]


@router.post("/{kind}/upload")
async def upload(kind: str, file: UploadFile = File(...), user: User = Depends(admin_only), db: Session = Depends(get_db)):
    _check_kind(kind)
    content = await file.read()
    if len(content) > MAX_BYTES:
        raise HTTPException(400, "File is larger than 20MB")
    filename = re.sub(r"[^\w\-. ]", "_", file.filename or "upload.csv")[-150:]
    try:
        headers, rows = read_table(content, filename)
    except ReadError as e:
        raise HTTPException(400, str(e))

    upload_id = f"{uuid.uuid4().hex}__{filename}"
    with open(os.path.join(_upload_dir(user.tenant_id), upload_id), "wb") as f:
        f.write(content)

    # Pehle aisi file aayi thi? Template ki mapping use karo
    sig = headers_signature(headers)
    template = db.scalars(select(MappingTemplate).where(
        MappingTemplate.tenant_id == user.tenant_id, MappingTemplate.kind == kind,
        MappingTemplate.headers_signature == sig)).first()
    if template:
        mapping = {h: template.mapping.get(h) for h in headers}
    else:
        mapping = suggest_mapping(headers, kind)

    return {
        "upload_id": upload_id,
        "filename": filename,
        "headers": headers,
        "preview": rows[:5],
        "total_rows": len(rows),
        "mapping": mapping,
        "template_name": template.name if template else None,
        "fields": FIELDS[kind],
    }


@router.post("/{kind}/run")
def run_import(kind: str, data: RunIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    _check_kind(kind)
    path = _upload_path(user.tenant_id, data.upload_id)

    allowed = set(field_keys(kind))
    mapped = [v for v in data.mapping.values() if v]
    bad = [v for v in mapped if v not in allowed]
    if bad:
        raise HTTPException(400, f"Unknown fields: {', '.join(bad)}")
    if len(mapped) != len(set(mapped)):
        raise HTTPException(400, "A field can't be mapped to more than one column")
    missing = [k for k in required_keys(kind) if k not in mapped]
    if missing:
        labels = [f["label"] for f in FIELDS[kind] if f["key"] in missing]
        raise HTTPException(400, f"Map these required fields: {', '.join(labels)}")

    with open(path, "rb") as f:
        headers, raw_rows = read_table(f.read(), data.upload_id.split("__", 1)[1])
    rows = service.apply_mapping(raw_rows, data.mapping)

    with no_audit(db):          # har row ka audit nahi - neeche ek "import.<kind>" summary log
        if kind == "products":
            result = service.import_products(db, user.tenant_id, rows)
        elif kind == "customers":
            result = service.import_customers(db, user.tenant_id, rows)
        elif kind == "suppliers":
            result = service.import_suppliers(db, user.tenant_id, rows)
        elif kind == "categories":
            result = service.import_categories(db, user.tenant_id, rows)
        else:
            result = service.import_history(db, user.tenant_id, rows)
        db.flush()

    if data.dry_run:
        db.rollback()   # sirf preview - kuch save nahi
        return {**result.as_dict(), "dry_run": True}

    job = ImportJob(tenant_id=user.tenant_id, kind=kind, source="csv", filename=data.upload_id.split("__", 1)[1],
                    created=result.created, updated=result.updated, skipped=result.skipped,
                    errors=result.errors, user_id=user.id)
    db.add(job)

    if data.save_template:
        sig = headers_signature(headers)
        template = db.scalars(select(MappingTemplate).where(
            MappingTemplate.tenant_id == user.tenant_id, MappingTemplate.kind == kind,
            MappingTemplate.headers_signature == sig)).first()
        if not template:
            template = MappingTemplate(tenant_id=user.tenant_id, kind=kind, headers_signature=sig,
                                       name=data.template_name or f"{kind} - {job.filename}"[:100], mapping={})
            db.add(template)
        template.mapping = data.mapping
        if data.template_name:
            template.name = data.template_name[:100]

    log_activity(db, user.tenant_id, user.id, f"import.{kind}", detail=result.as_dict() | {"errors": len(result.errors)})
    db.commit()
    try:
        os.remove(path)
    except OSError:
        pass
    return {**result.as_dict(), "dry_run": False, "job_id": job.id}


@router.get("/jobs")
def list_jobs(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.tenant_id == user.tenant_id)
                      .order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(100))
    return [
        {"id": j.id, "kind": j.kind, "source": j.source, "filename": j.filename, "status": j.status,
         "created": j.created, "updated": j.updated, "skipped": j.skipped, "errors": j.errors or [],
         "created_at": j.created_at}
        for j in jobs
    ]


@router.get("/templates")
def list_templates(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(MappingTemplate).where(MappingTemplate.tenant_id == user.tenant_id)
                      .order_by(MappingTemplate.created_at.desc(), MappingTemplate.id.desc()))
    return [{"id": t.id, "kind": t.kind, "name": t.name, "mapping": t.mapping, "created_at": t.created_at} for t in rows]


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    t = db.get(MappingTemplate, template_id)
    if not t or t.tenant_id != user.tenant_id:
        raise HTTPException(404, "Template not found")
    db.delete(t)
    db.commit()
    return {"ok": True}
