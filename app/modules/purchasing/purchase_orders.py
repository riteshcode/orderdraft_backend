"""
Purchase orders + stock received (GRN) API.

  GET    /purchase-orders?status=&supplier_id=&q=&sort=
  GET    /purchase-orders/suggestions            reorder level se kam wale products
  POST   /purchase-orders/from-suggestions       {items:[{product_id, supplier_id, quantity}]} -> har supplier ka draft PO
  POST   /purchase-orders                        draft banao
  GET    /purchase-orders/{id}
  PUT    /purchase-orders/{id}                   draft edit
  DELETE /purchase-orders/{id}                   sirf draft
  POST   /purchase-orders/{id}/send              {to?, message?} PDF email supplier ko -> sent
  POST   /purchase-orders/{id}/status            {action: mark_sent | cancel | close | reopen}
  GET    /purchase-orders/{id}/pdf
  POST   /purchase-orders/{id}/receive           {lines:[{po_line_id, quantity, unit_cost?}], reference?, received_on?, notes?}

  GET    /stock-receipts?supplier_id=&po_id=
  GET    /stock-receipts/{id}
  POST   /stock-receipts                         bina PO maal aaya {supplier_id?, lines:[{product_id, quantity, unit_cost?}]}
"""
import html
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db, utcnow
from app.core.text import content_disposition
from app.core.deps import current_user, require_role
from app.core.helpers import get_tenant_settings, log_activity, to_float
from app.core.mailer import MailError, send_email, sender_for, simple_html
from app.core.sorting import apply_sort
from app.models import Product, PurchaseOrder, PurchaseOrderLine, StockReceipt, Supplier, SupplierProduct, User
from app.modules.orders.confirmation import EMAIL_RE, split_emails
from app.modules.purchasing import service
from app.modules.purchasing.pdf import po_pdf

router = APIRouter(tags=["purchasing"])
admin_only = require_role("owner", "admin")


class POLineIn(BaseModel):
    product_id: int | None = None
    description: str | None = None
    quantity: float
    unit_cost: float | None = None
    tax_rate: float | None = Field(None, ge=0, le=100)


class POIn(BaseModel):
    supplier_id: int
    order_date: date | None = None
    expected_date: date | None = None
    reference: str | None = None
    deliver_to: str | None = None
    notes: str | None = None
    lines: list[POLineIn]


class SendIn(BaseModel):
    to: str | None = None
    message: str | None = None


class StatusIn(BaseModel):
    action: str = Field(pattern="^(mark_sent|cancel|close|reopen)$")


class ReceiveLineIn(BaseModel):
    po_line_id: int | None = None
    product_id: int | None = None
    quantity: float
    unit_cost: float | None = Field(None, ge=0)


class ReceiveIn(BaseModel):
    lines: list[ReceiveLineIn]
    supplier_id: int | None = None
    reference: str | None = None
    received_on: date | None = None
    notes: str | None = None


class SuggestionItem(BaseModel):
    product_id: int
    supplier_id: int
    quantity: float = Field(gt=0)
    unit_cost: float | None = Field(None, ge=0)


class FromSuggestionsIn(BaseModel):
    items: list[SuggestionItem]


# ---------------- output ----------------
def po_summary(po: PurchaseOrder) -> dict:
    ordered = sum(l.quantity for l in po.lines)
    received = sum(min(l.quantity_received, l.quantity) for l in po.lines)
    return {"id": po.id, "number": po.number, "status": po.status, "supplier_id": po.supplier_id,
            "supplier_name": po.supplier.name, "order_date": po.order_date, "expected_date": po.expected_date,
            "reference": po.reference, "subtotal": to_float(po.subtotal), "gst": to_float(po.gst), "total": to_float(po.total),
            "line_count": len(po.lines), "received_pct": round(received / ordered * 100) if ordered else 0,
            "overdue": bool(po.expected_date and po.expected_date < date.today() and po.status in ("sent", "partial")),
            "sent_at": po.sent_at, "sent_to": po.sent_to, "received_at": po.received_at, "created_at": po.created_at}


def receipt_out(r: StockReceipt, full: bool = False) -> dict:
    out = {"id": r.id, "number": r.number, "supplier_id": r.supplier_id, "supplier_name": r.supplier.name if r.supplier else None,
           "po_id": r.po_id, "po_number": r.po.number if r.po else None, "received_on": r.received_on,
           "reference": r.reference, "notes": r.notes, "total": to_float(r.total), "line_count": len(r.lines),
           "created_at": r.created_at}
    if full:
        out["lines"] = [{"id": l.id, "po_line_id": l.po_line_id, "product_id": l.product_id, "sku": l.product.sku,
                         "name": l.product.name, "quantity": l.quantity, "unit_cost": to_float(l.unit_cost)} for l in r.lines]
    return out


def po_detail(db: Session, po: PurchaseOrder) -> dict:
    receipts = db.scalars(select(StockReceipt).options(selectinload(StockReceipt.lines))
                          .where(StockReceipt.po_id == po.id).order_by(StockReceipt.id))
    links = {l.product_id: l for l in db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == po.supplier_id))}
    return {**po_summary(po), "deliver_to": po.deliver_to, "notes": po.notes,
            "supplier": {"id": po.supplier.id, "name": po.supplier.name, "email": po.supplier.email,
                         "cc_emails": po.supplier.cc_emails, "phone": po.supplier.phone},
            "lines": [{"id": l.id, "product_id": l.product_id, "sku": l.product.sku if l.product else None,
                       "barcode": l.product.barcode if l.product else None, "unit": l.product.unit if l.product else None,
                       "tracked": bool(l.product and l.product.is_tracked_inventory),
                       "description": l.description, "supplier_sku": l.supplier_sku, "quantity": l.quantity,
                       "quantity_received": l.quantity_received, "outstanding": max(l.quantity - l.quantity_received, 0),
                       "unit_cost": to_float(l.unit_cost), "tax_rate": l.tax_rate, "line_total": to_float(l.line_total),
                       "min_order_qty": links[l.product_id].min_order_qty if l.product_id in links else None,
                       "below_min": bool(l.product_id in links and links[l.product_id].min_order_qty
                                         and l.quantity < links[l.product_id].min_order_qty)}
                      for l in po.lines],
            "receipts": [receipt_out(r) for r in receipts]}


LOAD = (selectinload(PurchaseOrder.lines).selectinload(PurchaseOrderLine.product), selectinload(PurchaseOrder.supplier))


def _po(db: Session, user: User, po_id: int) -> PurchaseOrder:
    po = db.scalars(select(PurchaseOrder).options(*LOAD).where(PurchaseOrder.id == po_id,
                                                               PurchaseOrder.tenant_id == user.tenant_id)).first()
    if not po:
        raise HTTPException(404, "Purchase order not found")
    return po


def _payload(data: POIn) -> dict:
    d = data.model_dump()
    d["lines"] = [l.model_dump() for l in data.lines]
    return d


# ---------------- purchase orders ----------------
@router.get("/purchase-orders")
def list_pos(status: str | None = None, supplier_id: int | None = None, q: str = "", sort: str = "",
             page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(PurchaseOrder).where(PurchaseOrder.tenant_id == user.tenant_id)
    if status == "open":
        stmt = stmt.where(PurchaseOrder.status.in_(service.OPEN_STATUSES))
    elif status:
        stmt = stmt.where(PurchaseOrder.status.in_(status.split(",")))
    if supplier_id:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    sup = None
    if q or sort.lstrip("-") == "supplier":
        sup = Supplier
        stmt = stmt.join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
    if q:
        stmt = stmt.where(or_(PurchaseOrder.number.ilike(f"%{q}%"), PurchaseOrder.reference.ilike(f"%{q}%"),
                              Supplier.name.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    sorts = {"number": PurchaseOrder.id, "order_date": PurchaseOrder.order_date, "expected_date": PurchaseOrder.expected_date,
             "status": PurchaseOrder.status, "total": PurchaseOrder.total, "created_at": PurchaseOrder.created_at}
    if sup is not None:
        sorts["supplier"] = Supplier.name
    stmt = apply_sort(stmt, sort, sorts, default=PurchaseOrder.created_at, tiebreak=PurchaseOrder.id, default_desc=True)
    rows = db.scalars(stmt.options(*LOAD).offset((page - 1) * page_size).limit(page_size))
    counts = dict(db.execute(select(PurchaseOrder.status, func.count()).where(PurchaseOrder.tenant_id == user.tenant_id)
                             .group_by(PurchaseOrder.status)).all())
    return {"items": [po_summary(p) for p in rows], "total": total, "page": page, "page_size": page_size,
            "counts": counts}


@router.get("/purchase-orders/suggestions")
def reorder_suggestions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return service.suggestions(db, user.tenant_id)


@router.post("/purchase-orders/from-suggestions")
def create_from_suggestions(data: FromSuggestionsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(400, "Select at least one product")
    cfg = get_tenant_settings(db, user.tenant_id, for_update=True)
    by_supplier: dict[int, list[dict]] = {}
    for i in data.items:
        by_supplier.setdefault(i.supplier_id, []).append(
            {"product_id": i.product_id, "quantity": i.quantity, "unit_cost": i.unit_cost})
    created = [service.save_po(db, cfg, {"supplier_id": sid, "lines": lines}, user.id) for sid, lines in by_supplier.items()]
    db.commit()
    return [{"id": p.id, "number": p.number, "supplier_id": p.supplier_id} for p in created]


@router.post("/purchase-orders")
def create_po(data: POIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = get_tenant_settings(db, user.tenant_id, for_update=True)
    po = service.save_po(db, cfg, _payload(data), user.id)
    db.commit()
    return po_detail(db, _po(db, user, po.id))


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return po_detail(db, _po(db, user, po_id))


@router.put("/purchase-orders/{po_id}")
def update_po(po_id: int, data: POIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    po = _po(db, user, po_id)
    service.save_po(db, get_tenant_settings(db, user.tenant_id), _payload(data), user.id, po=po)
    db.commit()
    return po_detail(db, _po(db, user, po_id))


@router.delete("/purchase-orders/{po_id}")
def delete_po(po_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    po = _po(db, user, po_id)
    if po.status != "draft":
        raise HTTPException(400, "Only draft POs can be deleted - cancel other POs instead")
    log_activity(db, user.tenant_id, user.id, "po.deleted", "purchase_order", po.id, {"number": po.number})
    db.delete(po)
    db.commit()
    return {"ok": True}


@router.post("/purchase-orders/{po_id}/status")
def po_status(po_id: int, data: StatusIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    po = _po(db, user, po_id)
    service.set_status(db, po, data.action, user.id)
    db.commit()
    return po_detail(db, po)


@router.get("/purchase-orders/{po_id}/pdf")
def download_pdf(po_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    po = _po(db, user, po_id)
    pdf = po_pdf(po, get_tenant_settings(db, user.tenant_id))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": content_disposition(f"{po.number}.pdf", inline=True)})


@router.post("/purchase-orders/{po_id}/send")
def send_po(po_id: int, data: SendIn | None = None, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """PDF supplier ko email. Client ka verified domain ho to usi se, warna "Client via OrderDraft"."""
    po = _po(db, user, po_id)
    if po.status not in ("draft", "sent", "partial"):
        raise HTTPException(400, "This PO can't be sent")
    cfg = get_tenant_settings(db, user.tenant_id)
    to = ((data.to if data else None) or po.supplier.email or "").strip().lower()
    if not EMAIL_RE.match(to):
        raise HTTPException(400, "This supplier has no email address - please enter one")
    cc = split_emails(po.supplier.cc_emails) if not (data and data.to) else []
    message = (data.message if data else None) or (
        f"Hi {po.supplier.contact_name or po.supplier.name},\n\nPlease find attached purchase order {po.number}"
        + (f" for delivery by {po.expected_date.strftime('%d/%m/%Y')}" if po.expected_date else "")
        + f".\n\nPlease confirm and quote {po.number} on your delivery docket and invoice.\n\nThanks,\n{cfg.business_name}")
    html_body = "".join(f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>" for part in message.split("\n\n"))
    sender, reply_to = sender_for(cfg)
    try:
        send_email(to, f"Purchase order {po.number} from {cfg.business_name}", simple_html(f"Purchase order {po.number}", html_body),
                   message, attachments=[(f"{po.number}.pdf", po_pdf(po, cfg), "application/pdf")],
                   reply_to=reply_to, from_email=sender, cc=cc)
    except MailError as e:
        raise HTTPException(400, f"The email couldn't be sent: {e}")
    if po.status == "draft":
        po.status = "sent"
    po.sent_at, po.sent_to = utcnow(), ", ".join([to, *cc])[:300]
    log_activity(db, user.tenant_id, user.id, "po.sent", "purchase_order", po.id, {"number": po.number, "to": to})
    db.commit()
    return po_detail(db, po)


@router.post("/purchase-orders/{po_id}/receive")
def receive_po(po_id: int, data: ReceiveIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    po = _po(db, user, po_id)
    cfg = get_tenant_settings(db, user.tenant_id, for_update=True)
    service.receive_stock(db, cfg, user.id, supplier_id=None, po=po, lines_in=[l.model_dump() for l in data.lines],
                          received_on=data.received_on, reference=data.reference, notes=data.notes)
    db.commit()
    return po_detail(db, _po(db, user, po_id))


# ---------------- stock receipts ----------------
@router.get("/stock-receipts")
def list_receipts(supplier_id: int | None = None, po_id: int | None = None, page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(StockReceipt).where(StockReceipt.tenant_id == user.tenant_id)
    if supplier_id:
        stmt = stmt.where(StockReceipt.supplier_id == supplier_id)
    if po_id:
        stmt = stmt.where(StockReceipt.po_id == po_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.options(selectinload(StockReceipt.lines), selectinload(StockReceipt.supplier),
                                   selectinload(StockReceipt.po))
                      .order_by(StockReceipt.created_at.desc(), StockReceipt.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return {"items": [receipt_out(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def _receipt(db: Session, user: User, receipt_id: int) -> StockReceipt:
    r = db.get(StockReceipt, receipt_id)
    if not r or r.tenant_id != user.tenant_id:
        raise HTTPException(404, "Receipt not found")
    return r


@router.get("/stock-receipts/{receipt_id}")
def get_receipt(receipt_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return receipt_out(_receipt(db, user, receipt_id), full=True)


@router.post("/stock-receipts")
def receive_without_po(data: ReceiveIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = get_tenant_settings(db, user.tenant_id, for_update=True)
    r = service.receive_stock(db, cfg, user.id, supplier_id=data.supplier_id, po=None,
                              lines_in=[l.model_dump() for l in data.lines], received_on=data.received_on,
                              reference=data.reference, notes=data.notes)
    db.commit()
    return receipt_out(_receipt(db, user, r.id), full=True)
