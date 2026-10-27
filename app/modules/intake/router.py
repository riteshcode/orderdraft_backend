"""
Orders system mein yahan se aate hain:
  POST /inbound/postmark   Postmark inbound webhook (forwarded order emails)
  POST /inbound/upload     voice note / PDF / photo upload
  POST /inbound/text       chat / SMS paste
  GET  /inbound/messages   inbox log (failed / not_order / blocked dekhne ke liye)
"""
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import bind_tenant, get_db
from app.core.deps import current_user
from app.core.jobs import enqueue
from app.models import InboundMessage, Tenant, User
from app.modules.intake.email_parser import parse_postmark
from app.modules.parsing import pipeline

router = APIRouter(prefix="/inbound", tags=["inbound"])
basic = HTTPBasic()
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class TextIn(BaseModel):
    text: str
    sender_email: str | None = None
    sender_name: str | None = None


def check_webhook_auth(creds: HTTPBasicCredentials = Depends(basic)):
    ok_user = secrets.compare_digest(creds.username, settings.INBOUND_WEBHOOK_USER)
    ok_pass = secrets.compare_digest(creds.password, settings.INBOUND_WEBHOOK_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Bad webhook credentials")


def message_out(m: InboundMessage) -> dict:
    return {
        "id": m.id, "channel": m.channel, "sender_email": m.sender_email, "sender_name": m.sender_name,
        "subject": m.subject, "body_text": m.body_text, "combined_text": m.combined_text,
        "attachments": [{k: v for k, v in a.items() if k != "path"} for a in (m.attachments or [])],
        "status": m.status, "error": m.error, "processing_ms": m.processing_ms, "created_at": m.created_at,
    }


@router.post("/postmark", dependencies=[Depends(check_webhook_auth)])
def postmark_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Postmark -> Inbound -> Webhook URL:  https://<user>:<pass>@api.yourapp.com/inbound/postmark
    Hamesha jaldi 200 do (non-200 par Postmark retry karta hai). Processing background mein.
    """
    email = parse_postmark(payload)
    tenant = None
    if email.tenant_slug:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == email.tenant_slug)).first()
    if not tenant or not tenant.db_ready:
        return {"ok": False, "reason": "unknown tenant"}

    bind_tenant(db, tenant.id)   # message isi client ke DB mein jaayega
    msg = pipeline.save_email_message(db, tenant.id, email)
    enqueue(pipeline.process_message_job, msg.tenant_id, msg.id)
    return {"ok": True, "message_id": msg.id}


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    sender_email: str | None = Form(None),
    sender_name: str | None = Form(None),
    note: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "The file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File is larger than 25MB")
    msg = pipeline.save_upload_message(db, user.tenant_id, file.filename or "upload", file.content_type or "",
                                       content, sender_email=sender_email, sender_name=sender_name, note=note)
    enqueue(pipeline.process_message_job, msg.tenant_id, msg.id)
    return {"ok": True, "message_id": msg.id}


@router.post("/text")
def manual_text(data: TextIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not data.text.strip():
        raise HTTPException(400, "Enter some text")
    msg = pipeline.save_text_message(db, user.tenant_id, data.text, data.sender_email, data.sender_name)
    enqueue(pipeline.process_message_job, msg.tenant_id, msg.id)
    return {"ok": True, "message_id": msg.id}


@router.get("/messages")
def list_messages(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(InboundMessage).where(InboundMessage.tenant_id == user.tenant_id)
    if status:
        stmt = stmt.where(InboundMessage.status.in_(status.split(",")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.scalars(stmt.order_by(InboundMessage.created_at.desc(), InboundMessage.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"items": [message_out(m) for m in items], "total": total, "page": page, "page_size": page_size}


@router.get("/messages/{message_id}")
def get_message(message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    msg = db.get(InboundMessage, message_id)
    if not msg or msg.tenant_id != user.tenant_id:
        raise HTTPException(404, "Message not found")
    return message_out(msg)


@router.post("/messages/{message_id}/retry")
def retry(message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    msg = db.get(InboundMessage, message_id)
    if not msg or msg.tenant_id != user.tenant_id:
        raise HTTPException(404, "Message not found")
    if msg.status not in ("failed", "not_order", "blocked"):
        raise HTTPException(400, "Only failed, not-an-order or blocked messages can be retried")
    msg.status = "received"
    db.commit()
    enqueue(pipeline.process_message_job, msg.tenant_id, msg.id)
    return {"ok": True}
