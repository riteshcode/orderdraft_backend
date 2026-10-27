"""
Draft orders: list (filters + pagination), stats, detail, edit, approve, reject, send, reprocess, CSV.
"""
import csv
import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.core.database import get_db
from app.core.text import content_disposition
from app.core.deps import current_user, require_role
from app.core.helpers import to_float
from app.core.jobs import enqueue
from app.core.sorting import apply_sort
from app.models import (
    Customer, Destination, DraftOrder, DraftOrderLine, ExportJob, InboundMessage, Invoice, User,
)
from app.modules.catalog.pricing import price_map
from app.modules.destinations.base import order_payload
from app.modules.destinations.service import job_out, queue_exports
from app.modules.orders import service
from app.modules.orders.service import ManualOrderIn, OrderUpdate
from app.modules.parsing.pipeline import process_message_job
from app.modules.portal.notify import notify_customer_order, notify_order_status

router = APIRouter(prefix="/orders", tags=["orders"])
admin_only = require_role("owner", "admin")


class SendIn(BaseModel):
    destination_ids: list[int]


LOAD_ALL = (selectinload(DraftOrder.lines).selectinload(DraftOrderLine.product),
            selectinload(DraftOrder.customer), selectinload(DraftOrder.message))


def _query(user: User):
    return select(DraftOrder).options(*LOAD_ALL).where(DraftOrder.tenant_id == user.tenant_id)


def get_order(db: Session, user: User, order_id: int) -> DraftOrder:
    order = db.scalars(_query(user).where(DraftOrder.id == order_id)).first()
    if not order:
        raise HTTPException(404, "Order not found")
    return order


def summary_out(o: DraftOrder) -> dict:
    return {
        "id": o.id, "status": o.status, "customer_id": o.customer_id,
        "customer_name": o.customer.name if o.customer else None, "customer_raw": o.customer_raw,
        "channel": o.message.channel if o.message else None,
        "source": o.source, "payment_method": o.payment_method, "standing_order_id": o.standing_order_id,
        "line_count": len(o.lines), "overall_confidence": o.overall_confidence,
        "po_number": o.po_number, "delivery_date": o.delivery_date,
        "total_amount": to_float(o.total_amount), "created_at": o.created_at, "approved_at": o.approved_at,
        "customer_email": o.customer.email if o.customer else None,
        "confirmation_sent_at": o.confirmation_sent_at, "confirmation_to": o.confirmation_to,
    }


def detail_out(db: Session, o: DraftOrder) -> dict:
    m = o.message
    jobs = db.scalars(select(ExportJob).where(ExportJob.order_id == o.id).order_by(ExportJob.id))
    invoices = db.scalars(select(Invoice).where(Invoice.order_id == o.id).order_by(Invoice.id))
    special = price_map(db, o.customer_id) if o.status == "needs_review" else {}   # approve ke baad price fix ho chuka
    return {
        **summary_out(o),
        "customer_confidence": o.customer_confidence, "notes": o.notes,
        "lines": [
            {"id": l.id, "raw_text": l.raw_text, "product_hint": l.product_hint, "quantity": l.quantity,
             "unit": l.unit, "product_id": l.product_id,
             "product_sku": l.product.sku if l.product else None,
             "product_name": l.product.name if l.product else None,
             "unit_price": to_float(l.unit_price if l.unit_price is not None
                                    else (special.get(l.product_id, l.product.price) if l.product else None)),
             "price_source": ("portal" if o.source == "portal" else "override")
             if l.unit_price is not None and o.status == "needs_review"
             else ("customer" if l.unit_price is None and l.product_id in special else "product"),
             "match_confidence": l.match_confidence, "match_method": l.match_method,
             "candidates": l.candidates or []}
            for l in o.lines
        ],
        "message": {
            "id": m.id, "channel": m.channel, "sender_email": m.sender_email, "sender_name": m.sender_name,
            "subject": m.subject, "body_text": m.body_text, "combined_text": m.combined_text,
            "attachments": [{k: v for k, v in a.items() if k != "path"} for a in (m.attachments or [])],
            "created_at": m.created_at, "processing_ms": m.processing_ms,
        } if m else None,
        "exports": [job_out(j) for j in jobs],
        "invoices": [{"id": i.id, "number": i.number, "source": i.source, "status": i.status,
                      "total": to_float(i.total)} for i in invoices],
    }


@router.get("")
def list_orders(
    status: str | None = None,
    q: str = "",
    channel: str | None = None,
    customer_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = "",                   # id | created_at | customer | total_amount | status | delivery_date ... (-x = desc)
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(DraftOrder).where(DraftOrder.tenant_id == user.tenant_id)
    if status:
        stmt = stmt.where(DraftOrder.status.in_(status.split(",")))
    if customer_id:
        stmt = stmt.where(DraftOrder.customer_id == customer_id)
    if channel == "entered":          # form se manually bane orders (koi message nahi)
        stmt = stmt.where(DraftOrder.source == "manual")
    elif channel == "portal":         # customer ne online portal se order kiya
        stmt = stmt.where(DraftOrder.source == "portal")
    elif channel == "standing":       # standing (recurring) order se apne aap bana
        stmt = stmt.where(DraftOrder.source == "standing")
    elif channel:
        stmt = stmt.join(InboundMessage, InboundMessage.id == DraftOrder.message_id).where(InboundMessage.channel == channel)
    if date_from:
        stmt = stmt.where(DraftOrder.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        stmt = stmt.where(DraftOrder.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    if q:
        stmt = stmt.outerjoin(Customer, Customer.id == DraftOrder.customer_id).where(or_(
            Customer.name.ilike(f"%{q}%"), DraftOrder.customer_raw.ilike(f"%{q}%"),
            DraftOrder.po_number.ilike(f"%{q}%"), DraftOrder.id == (int(q) if q.isdigit() else -1)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sort_customer = aliased(Customer)      # q wale join se alag (dono ek saath chal sakein)
    if sort.lstrip("-") == "customer":
        stmt = stmt.outerjoin(sort_customer, sort_customer.id == DraftOrder.customer_id)
    sorts = {"id": DraftOrder.id, "created_at": DraftOrder.created_at, "customer": sort_customer.name,
             "total_amount": DraftOrder.total_amount, "status": DraftOrder.status, "po_number": DraftOrder.po_number,
             "delivery_date": DraftOrder.delivery_date, "overall_confidence": DraftOrder.overall_confidence,
             "approved_at": DraftOrder.approved_at}
    stmt = apply_sort(stmt, sort, sorts, default=DraftOrder.created_at, tiebreak=DraftOrder.id, default_desc=True)
    items = db.scalars(stmt.options(*LOAD_ALL).offset((page - 1) * page_size).limit(page_size))
    return {"items": [summary_out(o) for o in items], "total": total, "page": page, "page_size": page_size}


@router.get("/stats")
def order_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(DraftOrder.status, func.count()).where(DraftOrder.tenant_id == user.tenant_id)
                      .group_by(DraftOrder.status)).all()
    msg_rows = db.execute(select(InboundMessage.status, func.count()).where(InboundMessage.tenant_id == user.tenant_id)
                          .where(InboundMessage.status.in_(["received", "processing", "failed", "blocked"]))
                          .group_by(InboundMessage.status)).all()
    m = {s: c for s, c in msg_rows}
    failed_exports = db.scalar(select(func.count()).select_from(ExportJob).where(
        ExportJob.tenant_id == user.tenant_id, ExportJob.status == "failed"))
    return {
        "by_status": {s: c for s, c in rows},
        "processing": m.get("received", 0) + m.get("processing", 0),
        "failed_messages": m.get("failed", 0),
        "blocked_messages": m.get("blocked", 0),
        "failed_exports": failed_exports,
    }


@router.get("/export.csv")
def export_csv(status: str = "approved,exported", date_from: date | None = None, date_to: date | None = None,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = _query(user).where(DraftOrder.status.in_(status.split(",")))
    if date_from:
        stmt = stmt.where(DraftOrder.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        stmt = stmt.where(DraftOrder.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))

    out = io.StringIO()
    cols = ["order_id", "status", "customer_code", "customer_name", "po_number", "delivery_date",
            "sku", "product_name", "quantity", "unit", "unit_price", "line_total", "original_text", "notes"]
    writer = csv.DictWriter(out, fieldnames=cols)
    writer.writeheader()
    for order in db.scalars(stmt.order_by(DraftOrder.id)):
        p = order_payload(order)
        for line in p["lines"]:
            writer.writerow({
                "order_id": p["order_id"], "status": p["status"], "customer_code": p["customer"]["code"] or "",
                "customer_name": p["customer"]["name"] or "", "po_number": p["po_number"] or "",
                "delivery_date": p["delivery_date"] or "", "sku": line["sku"] or "",
                "product_name": line["product_name"] or "", "quantity": line["quantity"], "unit": line["unit"] or "",
                "unit_price": "" if line["unit_price"] is None else line["unit_price"],
                "line_total": "" if line["line_total"] is None else line["line_total"],
                "original_text": line["original_text"], "notes": p["notes"] or "",
            })
    filename = f"orders_{datetime.now():%Y%m%d_%H%M}.csv"
    # BOM = Excel UTF-8 pehchaan leta hai (é, ₹, 中文 sahi dikhte hain)
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": content_disposition(filename)})


@router.post("/manual")
def create_manual(data: ManualOrderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Staff khud order banaye (phone / counter order). approve=true = turant approve + export."""
    order = service.create_manual_order(db, user, data)
    return detail_out(db, get_order(db, user, order.id))


@router.get("/{order_id}")
def order_detail(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return detail_out(db, get_order(db, user, order_id))


@router.put("/{order_id}")
def update(order_id: int, data: OrderUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    order = get_order(db, user, order_id)
    before = service.snapshot(order)
    service.update_order(db, order, data)
    order = get_order(db, user, order_id)
    changes = service.order_changes(before, order)
    if changes:                       # portal / standing order -> customer ko "order updated"
        notify_customer_order(db, user.tenant, order, "updated", changes=changes)
    return detail_out(db, get_order(db, user, order_id))


@router.post("/{order_id}/approve")
def approve(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    order = service.approve_order(db, get_order(db, user, order_id), user.id)
    notify_order_status(db, user.tenant, order)          # portal order -> customer ko "confirmed"
    return detail_out(db, get_order(db, user, order_id))


class RejectIn(BaseModel):
    reason: str | None = None       # portal customer ko notification mein dikhega


@router.post("/{order_id}/reject")
def reject(order_id: int, data: RejectIn | None = None, user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    reason = ((data.reason if data else None) or "").strip()[:300] or None
    order = service.reject_order(db, get_order(db, user, order_id), user.id, reason)
    notify_order_status(db, user.tenant, order, reason)  # portal order -> customer ko "not accepted"
    return detail_out(db, get_order(db, user, order_id))


class CancelIn(BaseModel):
    reason: str | None = None


@router.post("/{order_id}/cancel")
def cancel(order_id: int, data: CancelIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Approved / exported order cancel - stock wapas, exports band, apni invoice void, customer ko message."""
    reason = ((data.reason if data else None) or "").strip()[:300] or None
    order = get_order(db, user, order_id)
    warnings = service.cancel_order(db, order, user.id, reason)
    notify_customer_order(db, user.tenant, order, "cancelled", reason=reason)
    return {**detail_out(db, get_order(db, user, order_id)), "warnings": warnings}


@router.post("/{order_id}/send")
def send(order_id: int, data: SendIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Approved order ko chune hue destinations pe (manually) bhejo."""
    order = get_order(db, user, order_id)
    if order.status not in ("approved", "exported"):
        raise HTTPException(400, "Approve the order first")
    valid = set(db.scalars(select(Destination.id).where(Destination.tenant_id == user.tenant_id,
                                                         Destination.is_active.is_(True))))
    ids = [i for i in data.destination_ids if i in valid]
    if not ids:
        raise HTTPException(400, "Select at least one active destination")
    queue_exports(db, order, ids)
    return detail_out(db, get_order(db, user, order_id))


class ConfirmIn(BaseModel):
    to: str | None = None


@router.post("/{order_id}/send-confirmation")
def send_confirmation(order_id: int, data: ConfirmIn | None = None, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Customer ko order confirmation dobara bhejo (ya kisi aur email pe)."""
    from app.core.helpers import get_tenant_settings
    from app.modules.orders.confirmation import EMAIL_RE, send_order_confirmation

    order = get_order(db, user, order_id)
    if order.status not in ("approved", "exported"):
        raise HTTPException(400, "Approve the order first")
    to = (data.to or "").strip() if data else ""
    if to and not EMAIL_RE.match(to):
        raise HTTPException(400, "Enter a valid email address")
    if not to and not (order.customer and order.customer.email):
        raise HTTPException(400, "This customer has no email address - please enter one")
    if not send_order_confirmation(db, get_tenant_settings(db, user.tenant_id), order, force=True,
                                   to=to or None, user_id=user.id):
        raise HTTPException(400, "The email couldn't be sent - check the activity log for details")
    return detail_out(db, get_order(db, user, order_id))


@router.get("/{order_id}/payload")
def payload(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return order_payload(get_order(db, user, order_id))


@router.post("/{order_id}/reprocess")
def reprocess(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """AI result galat laga? Message dobara process karo (ye draft delete hota hai)."""
    order = get_order(db, user, order_id)
    if order.status != "needs_review" or not order.message_id:
        raise HTTPException(400, "Only orders that need review can be reprocessed")
    message_id = order.message_id
    db.delete(order)
    db.commit()
    enqueue(process_message_job, user.tenant_id, message_id)
    return {"ok": True, "message_id": message_id}
