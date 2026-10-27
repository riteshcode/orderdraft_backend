"""
Main pipeline: InboundMessage -> DraftOrder

  1. Message save (email / upload / text) -> InboundMessage      [save_* functions]
  2. process_message_job(message_id) background mein:
       billing check -> attachments padho (PDF, voice, image)
       -> Claude extraction -> customer match -> line matching
       -> DraftOrder (needs_review)
"""
import logging
import time
import traceback
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.database import tenant_session
from app.models import Customer, DraftOrder, DraftOrderLine, InboundMessage, Product, Tenant
from app.modules.billing.limits import can_process, record_ai_usage
from app.modules.intake import pdf_parser, voice_parser
from app.modules.intake.email_parser import ParsedEmail
from app.modules.intake.storage import detect_kind, read_file, save_file
from app.modules.parsing import extractor
from app.modules.parsing.matcher import Catalog, match_customer

log = logging.getLogger("pipeline")


# --------------------------------------------------------------------------
# Step 1: message save karna
# --------------------------------------------------------------------------
def save_email_message(db: Session, tenant_id: int, email: ParsedEmail) -> InboundMessage:
    attachments = []
    for att in email.attachments:
        kind = detect_kind(att.name, att.content_type)
        if kind == "other":
            continue  # .ics, signature images waghera
        path = save_file(tenant_id, att.name, att.content)
        attachments.append({"name": att.name, "path": path, "type": att.content_type, "kind": kind})

    msg = InboundMessage(
        tenant_id=tenant_id, channel="email",
        sender_email=email.sender_email, sender_name=email.sender_name,
        subject=email.subject[:500] if email.subject else None,
        body_text=email.body, attachments=attachments,
    )
    db.add(msg)
    db.commit()
    return msg


def save_upload_message(db: Session, tenant_id: int, filename: str, content_type: str, content: bytes,
                        sender_email: str | None = None, sender_name: str | None = None,
                        note: str | None = None) -> InboundMessage:
    kind = detect_kind(filename, content_type)
    path = save_file(tenant_id, filename, content)
    channel = {"audio": "voice", "pdf": "pdf"}.get(kind, "manual")
    msg = InboundMessage(
        tenant_id=tenant_id, channel=channel, sender_email=sender_email, sender_name=sender_name,
        subject=filename, body_text=note or "",
        attachments=[{"name": filename, "path": path, "type": content_type, "kind": kind}],
    )
    db.add(msg)
    db.commit()
    return msg


def save_text_message(db: Session, tenant_id: int, text: str, sender_email=None, sender_name=None) -> InboundMessage:
    msg = InboundMessage(tenant_id=tenant_id, channel="manual", sender_email=sender_email,
                         sender_name=sender_name, body_text=text, attachments=[])
    db.add(msg)
    db.commit()
    return msg


# --------------------------------------------------------------------------
# Step 2: process
# --------------------------------------------------------------------------
def process_message_job(tenant_id: int, message_id: int):
    """Background job entry point (core.jobs.enqueue) - is client ke DB ka session."""
    db = tenant_session(tenant_id)
    try:
        process_message(db, message_id)
    finally:
        db.close()


def process_message(db: Session, message_id: int) -> DraftOrder | None:
    msg = db.get(InboundMessage, message_id)
    if not msg:
        return None

    tenant = db.get(Tenant, msg.tenant_id)
    ok, reason = can_process(db, tenant)
    if not ok:
        msg.status = "blocked"
        msg.error = reason
        db.commit()
        return None

    msg.status = "processing"
    msg.error = None
    db.commit()
    started = time.time()

    try:
        order = _run(db, msg)
        msg.status = "done" if order else "not_order"
        msg.processing_ms = int((time.time() - started) * 1000)
        _meter(db, msg.tenant_id, msg.status, order)
        db.commit()
        return order
    except Exception as e:
        db.rollback()
        log.error("Message %s failed: %s", message_id, traceback.format_exc())
        msg = db.get(InboundMessage, message_id)
        msg.status = "failed"
        msg.error = str(e)[:2000]
        _meter(db, msg.tenant_id, "failed", None)
        db.commit()
        return None


def _meter(db: Session, tenant_id: int, status: str, order):
    """Main DB mein counter (super admin stats - har client ka DB kholna na pade)."""
    record_ai_usage(db, tenant_id, f"message_{status}")
    if order:
        record_ai_usage(db, tenant_id, "order")


def _run(db: Session, msg: InboundMessage) -> DraftOrder | None:
    tenant_id = msg.tenant_id

    def on_claude_usage(kind, input_tokens, output_tokens):
        record_ai_usage(db, tenant_id, kind, input_tokens=input_tokens, output_tokens=output_tokens)

    def on_audio_usage(seconds):
        record_ai_usage(db, tenant_id, "deepgram", audio_seconds=seconds)

    # ---- 2a. Saare inputs ko text mein badlo ----
    text_parts = []
    if msg.subject:
        text_parts.append(f"Subject: {msg.subject}")
    if msg.sender_name or msg.sender_email:
        text_parts.append(f"From: {msg.sender_name or ''} <{msg.sender_email or ''}>")
    if msg.body_text:
        text_parts.append(msg.body_text)

    vision_pdfs: list[bytes] = []
    images: list[tuple[bytes, str]] = []

    for att in msg.attachments or []:
        content = read_file(att["path"])
        kind = att.get("kind")

        if kind == "pdf":
            result = pdf_parser.extract_pdf_text(content)
            if result.needs_vision:
                vision_pdfs.append(content)
                text_parts.append(f"[Attached PDF '{att['name']}' - see document above]")
            else:
                text_parts.append(f"[Attached PDF '{att['name']}']\n{result.text}")

        elif kind == "audio":
            transcript = att.get("transcript")
            if not transcript:   # retry par dobara paisa mat kharch karo
                transcript = voice_parser.transcribe_audio(
                    content, att.get("type") or "audio/mpeg", _product_keyterms(db, tenant_id), on_usage=on_audio_usage
                )
                att["transcript"] = transcript
            text_parts.append(f"[Voice note transcript '{att['name']}']\n{transcript}")

        elif kind == "image":
            media_type = att.get("type") or "image/jpeg"
            if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                media_type = "image/jpeg"
            images.append((content, media_type))
            text_parts.append(f"[Attached image '{att['name']}' - see image above]")

    flag_modified(msg, "attachments")   # JSON ke andar change kiya (transcript)
    msg.combined_text = "\n\n".join(text_parts)
    db.commit()

    # ---- 2b. Claude extraction ----
    customer_names = list(db.scalars(
        select(Customer.name).where(Customer.tenant_id == tenant_id, Customer.is_active.is_(True)).limit(100)
    ))
    data = extractor.extract_order(
        text=msg.combined_text, pdf_files=vision_pdfs, image_files=images,
        known_customers=customer_names, on_usage=on_claude_usage,
    )
    db.commit()   # usage events save

    if not data.get("is_order"):
        return None

    # ---- 2c. Customer match ----
    customer, cust_conf = match_customer(
        db, tenant_id, name=data.get("customer_name"),
        email=data.get("customer_email"), phone=data.get("customer_phone"),
    )
    if not customer and msg.sender_email:
        customer, cust_conf = match_customer(db, tenant_id, email=msg.sender_email, name=msg.sender_name)

    order = DraftOrder(
        tenant_id=tenant_id, message_id=msg.id,
        customer_id=customer.id if customer else None,
        customer_raw=data.get("customer_name") or data.get("customer_email") or msg.sender_name,
        customer_confidence=cust_conf,
        po_number=data.get("po_number"),
        delivery_date=_parse_date(data.get("delivery_date")),
        notes=data.get("notes"),
        status="needs_review",
    )
    db.add(order)

    # ---- 2d. Lines ----
    items = data.get("items") or []
    if not items and customer:
        order.lines = _repeat_last_order(db, customer.id)
    else:
        order.lines = _match_lines(db, tenant_id, customer.id if customer else None, items, on_claude_usage)

    order.overall_confidence = overall_confidence(order)
    db.commit()

    # Staff ko bell + browser push: naya order review ke liye
    from app.modules.notifications.staff import notify_staff
    channel = {"email": "email", "voice": "voice note", "pdf": "PDF / photo", "manual": "pasted message"}.get(msg.channel, msg.channel or "message")
    who = customer.name if customer else (order.customer_raw or "unknown customer")
    notify_staff(db, tenant_id, "new_order", f"New order #{order.id} from {who}",
                 f"{len(order.lines)} line{'s' if len(order.lines) != 1 else ''} · via {channel} · needs review",
                 f"/orders/{order.id}", order.id)
    db.commit()
    return order


def _match_lines(db: Session, tenant_id: int, customer_id: int | None, items: list[dict], on_usage) -> list[DraftOrderLine]:
    catalog = Catalog(db, tenant_id, customer_id)
    lines: list[DraftOrderLine] = []
    unsure = []

    for i, item in enumerate(items):
        result = catalog.match(item.get("product_hint", ""), item.get("raw_text", ""), item.get("unit"))
        line = DraftOrderLine(
            raw_text=(item.get("raw_text") or item.get("product_hint") or "")[:500],
            product_hint=(item.get("product_hint") or "")[:300],
            quantity=float(item.get("quantity") or 1),
            unit=item.get("unit"),
            product_id=result.product_id,
            suggested_product_id=result.product_id,
            match_confidence=round(result.confidence, 1),
            match_method=result.method,
            candidates=result.candidates,
        )
        lines.append(line)
        if result.confidence < settings.MATCH_AUTO_THRESHOLD and result.candidates:
            unsure.append({
                "line_index": i, "text": line.raw_text, "product_hint": line.product_hint, "unit": line.unit,
                "candidates": [{k: c[k] for k in ("product_id", "sku", "name", "unit", "usual")} for c in result.candidates],
            })

    if unsure:
        try:
            picks = extractor.pick_products(unsure, on_usage=on_usage)
        except extractor.ExtractionError as e:
            log.warning("pick_products failed: %s", e)
            picks = {}
        for idx, (pid, conf) in picks.items():
            if idx >= len(lines):
                continue
            valid_ids = {c["product_id"] for c in lines[idx].candidates or []}
            if pid in valid_ids:
                lines[idx].product_id = pid
                lines[idx].suggested_product_id = pid
                lines[idx].match_confidence = round(min(conf, 90), 1)
                lines[idx].match_method = "llm"
    return lines


def _repeat_last_order(db: Session, customer_id: int) -> list[DraftOrderLine]:
    """'Same as last week' - customer ka last approved order copy."""
    last = db.scalars(
        select(DraftOrder)
        .where(DraftOrder.customer_id == customer_id, DraftOrder.status.in_(["approved", "exported"]))
        .order_by(DraftOrder.approved_at.desc()).limit(1)
    ).first()
    if not last:
        return []
    return [
        DraftOrderLine(raw_text=f"(repeat) {l.raw_text}"[:500], product_hint=l.product_hint, quantity=l.quantity,
                       unit=l.unit, product_id=l.product_id, suggested_product_id=l.product_id,
                       match_confidence=80, match_method="repeat", candidates=[])
        for l in last.lines
    ]


def overall_confidence(order: DraftOrder) -> float:
    """Order utna hi pakka jitni uski sabse kamzor line (ya customer)."""
    scores = [l.match_confidence if l.product_id else 0 for l in order.lines]
    scores.append(order.customer_confidence if order.customer_id else 0)
    if not order.lines:
        scores.append(0)
    return round(min(scores), 1)


def _product_keyterms(db: Session, tenant_id: int) -> list[str]:
    names = db.scalars(select(Product.name).where(Product.tenant_id == tenant_id, Product.is_active.is_(True)).limit(200))
    terms = []
    for n in names:
        short = " ".join(n.split()[:3])
        if short not in terms:
            terms.append(short)
    return terms[:50]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
