"""
Review screen actions: edit, approve, reject.

Approve par:
  1. PRICE   - har line ka unit_price snapshot + order total (baad mein price badle to order na badle)
  2. LEARN   - user ne product badla / system unsure tha -> customer-specific alias
  3. HISTORY - customer_product_stats update
  4. EXPORT  - auto_send destinations ke export jobs
"""
from fastapi import HTTPException
from pydantic import BaseModel
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.helpers import get_tenant_settings, log_activity, money
from app.core.jobs import enqueue
from app.models import CustomerProductStat, DraftOrder, DraftOrderLine, Product
from app.modules.destinations.service import queue_exports
from app.modules.catalog.pricing import price_map
from app.modules.imports.service import add_alias
from app.modules.parsing.pipeline import overall_confidence

TRUSTED_METHODS = {"alias", "sku", "exact", "repeat"}


class LineUpdate(BaseModel):
    id: int | None = None          # None = nayi line
    raw_text: str | None = None
    quantity: float
    unit: str | None = None
    product_id: int | None = None
    unit_price: float | None = None   # None = product ka price, value = override


class ManualOrderIn(BaseModel):
    customer_id: int
    po_number: str | None = None
    delivery_date: date | None = None
    notes: str | None = None
    lines: list[LineUpdate]
    approve: bool = False          # True = turant approve + destinations pe bhejo


class OrderUpdate(BaseModel):
    customer_id: int | None = None
    po_number: str | None = None
    delivery_date: date | None = None
    notes: str | None = None
    lines: list[LineUpdate]


def update_order(db: Session, order: DraftOrder, data: OrderUpdate) -> DraftOrder:
    if order.status != "needs_review":
        raise HTTPException(400, "Only orders that need review can be edited")

    valid_products = set(db.scalars(select(Product.id).where(Product.tenant_id == order.tenant_id)))
    if data.customer_id:
        from app.models import Customer
        c = db.get(Customer, data.customer_id)
        if not c or c.tenant_id != order.tenant_id:
            raise HTTPException(400, "Invalid customer")
        if data.customer_id != order.customer_id:
            order.customer_confidence = 100

    order.customer_id = data.customer_id
    order.po_number, order.delivery_date, order.notes = data.po_number, data.delivery_date, data.notes

    existing = {l.id: l for l in order.lines}
    keep_ids, new_lines = set(), []
    for item in data.lines:
        if item.quantity < 0:
            raise HTTPException(400, "Quantity can't be negative")
        if item.product_id and item.product_id not in valid_products:
            raise HTTPException(400, f"Product {item.product_id} invalid")

        if item.id and item.id in existing:
            line = existing[item.id]
            keep_ids.add(line.id)
            if line.product_id != item.product_id:
                line.match_method = "manual"
                line.match_confidence = 100 if item.product_id else 0
            line.product_id, line.quantity, line.unit = item.product_id, item.quantity, item.unit
            line.unit_price = money(item.unit_price) if item.unit_price is not None else None
            if item.raw_text is not None:
                line.raw_text = item.raw_text[:500]
        else:
            new_lines.append(DraftOrderLine(
                raw_text=(item.raw_text or "(added manually)")[:500], quantity=item.quantity, unit=item.unit,
                product_id=item.product_id, match_confidence=100 if item.product_id else 0,
                match_method="manual", candidates=[],
                unit_price=money(item.unit_price) if item.unit_price is not None else None,
            ))

    order.lines = [l for l in order.lines if l.id in keep_ids] + new_lines
    db.flush()
    order.overall_confidence = overall_confidence(order)
    db.commit()
    return order


def approve_order(db: Session, order: DraftOrder, user_id: int | None = None) -> DraftOrder:
    if order.status != "needs_review":
        raise HTTPException(400, "Order already processed")
    if not order.lines:
        raise HTTPException(400, "This order has no lines")
    unmatched = [l.raw_text for l in order.lines if not l.product_id]
    if unmatched:
        raise HTTPException(400, f"Choose a product for these lines: {', '.join(unmatched[:5])}")
    if not order.customer_id:
        raise HTTPException(400, "Choose a customer")

    now = utcnow()
    total = money(0)
    special = price_map(db, order.customer_id)   # customer ki pantry price (valid ho to)
    for line in order.lines:
        product = db.get(Product, line.product_id)
        if line.unit_price is None:          # override nahi hai -> customer price, warna product ka current price
            line.unit_price = special.get(product.id, product.price)
        if line.unit_price is not None:
            total += money(line.unit_price) * money(line.quantity)
    order.total_amount = money(total)
    order.status = "approved"
    order.approved_at = now
    order.approved_by_id = user_id

    # LEARN (manual / portal orders mein customer ne kuch "likha" nahi - seekhne ko kuch nahi)
    for line in (order.lines if order.source not in ("manual", "portal", "standing") else []):
        changed = line.suggested_product_id != line.product_id
        if changed or line.match_method not in TRUSTED_METHODS:
            hint = line.product_hint or line.raw_text
            if hint and not hint.startswith("(added") and not hint.startswith("(repeat"):
                add_alias(db, order.tenant_id, line.product_id, hint, customer_id=order.customer_id, source="learned")

    # HISTORY (ek product do lines mein ho sakta hai - isliye har product sirf ek baar count)
    for product_id in {line.product_id for line in order.lines}:
        stat = db.scalars(select(CustomerProductStat).where(
            CustomerProductStat.customer_id == order.customer_id,
            CustomerProductStat.product_id == product_id)).first()
        if not stat:
            stat = CustomerProductStat(tenant_id=order.tenant_id, customer_id=order.customer_id,
                                       product_id=product_id, times_ordered=0)
            db.add(stat)
        stat.times_ordered += 1
        stat.last_ordered_at = now

    # STOCK (Settings -> Stock: approve par on hand kam karo - sirf "Track inventory" wale products)
    if get_tenant_settings(db, order.tenant_id).stock_deduct_on_approve:
        from app.modules.catalog.stock import deduct_for_order
        deduct_for_order(db, order, user_id)

    log_activity(db, order.tenant_id, user_id, "order.approved", "order", order.id,
                 {"total": float(order.total_amount), "lines": len(order.lines)})
    db.commit()

    # EXPORT
    queue_exports(db, order)

    # CUSTOMER KO CONFIRMATION EMAIL (Settings -> Email mein on/off)
    if get_tenant_settings(db, order.tenant_id).order_email_enabled:
        from app.modules.orders.confirmation import send_order_confirmation_job
        enqueue(send_order_confirmation_job, order.tenant_id, order.id)
    return order


def reject_order(db: Session, order: DraftOrder, user_id: int | None = None, reason: str | None = None) -> DraftOrder:
    if order.status != "needs_review":
        raise HTTPException(400, "Order already processed")
    order.status = "rejected"
    log_activity(db, order.tenant_id, user_id, "order.rejected", "order", order.id, {"reason": reason} if reason else None)
    db.commit()
    return order


def cancel_order(db: Session, order: DraftOrder, user_id: int | None = None, reason: str | None = None) -> list[str]:
    """
    Approved / exported order cancel (customer ne mana kiya, maal nahi hai ...). Returns: staff ke liye warnings.
      - stock wapas (agar approve pe ghata tha)
      - pending / failed exports band
      - OrderDraft ki apni invoice void (payment na ho to); accounting software ki invoice wahan void karni padegi
    Needs review wale orders ke liye Reject hai.
    """
    from app.models import ExportJob, Invoice, StockMovement
    from app.modules.catalog.stock import record_movement

    if order.status not in ("approved", "exported"):
        raise HTTPException(400, "Only approved orders can be cancelled - use Reject for orders that need review")
    warnings = []
    order.status = "cancelled"

    # Stock wapas - jitna is order ne ghataya tha (ek product = ek movement)
    sold: dict[int, float] = {}
    for m in db.scalars(select(StockMovement).where(StockMovement.ref_type == "order", StockMovement.ref_id == order.id)):
        sold[m.product_id] = sold.get(m.product_id, 0) + (m.change or 0)
    for product_id, change in sold.items():
        p = db.get(Product, product_id)
        if p and change:
            record_movement(db, p, round((p.quantity_on_hand or 0) - change, 4), "order_cancelled",
                            f"Order #{order.id} cancelled", user_id, "order", order.id)

    for job in db.scalars(select(ExportJob).where(ExportJob.order_id == order.id, ExportJob.status.in_(["pending", "failed"]))):
        job.status, job.next_retry_at, job.last_error = "failed", None, "Order cancelled - not sent"

    for inv in db.scalars(select(Invoice).where(Invoice.order_id == order.id, Invoice.status != "void")):
        if inv.source != "internal":
            warnings.append(f"Invoice {inv.number or inv.external_id} is in your accounting software - void it there")
        elif money(inv.amount_paid) > 0:
            warnings.append(f"Invoice {inv.number} has a payment recorded, so it wasn't voided - arrange a refund or credit")
        else:
            inv.status = "void"
            log_activity(db, order.tenant_id, user_id, "invoice.voided", "invoice", inv.id, {"reason": "order cancelled"})
    log_activity(db, order.tenant_id, user_id, "order.cancelled", "order", order.id,
                 {k: v for k, v in {"reason": reason, "stock_returned": len(sold) or None}.items() if v})
    db.commit()
    return warnings


def order_changes(before: dict, order: DraftOrder) -> list[str]:
    """Customer ko batane layak badlav (products / qty / delivery date). before = snapshot(order) edit se pehle."""
    after = snapshot(order)
    out = []
    names = {**before["names"], **after["names"]}
    for pid, qty in after["qty"].items():
        old = before["qty"].get(pid)
        if old is None:
            out.append(f"Added {names[pid]} ({qty:g})")
        elif old != qty:
            out.append(f"{names[pid]}: {old:g} → {qty:g}")
    for pid in before["qty"]:
        if pid not in after["qty"]:
            out.append(f"Removed {names[pid]}")
    if before["delivery_date"] != after["delivery_date"]:
        d = after["delivery_date"]
        out.append(f"Delivery date: {d:%a %d %b}" if d else "Delivery date removed")
    return out


def snapshot(order: DraftOrder) -> dict:
    qty: dict[int, float] = {}
    names: dict[int, str] = {}
    for l in order.lines:
        if l.product_id:
            qty[l.product_id] = qty.get(l.product_id, 0) + (l.quantity or 0)
            p = l.product if l.product and l.product.id == l.product_id else Session.object_session(order).get(Product, l.product_id)
            names[l.product_id] = p.name if p else l.raw_text
    return {"qty": qty, "names": names, "delivery_date": order.delivery_date}


def create_manual_order(db: Session, user, data: ManualOrderIn) -> DraftOrder:
    """Phone pe order mila / customer counter pe - staff khud order banata hai."""
    from app.models import Customer

    from app.models import Tenant
    from app.modules.billing.limits import subscription_ok

    ok, reason = subscription_ok(db.get(Tenant, user.tenant_id))   # AI quota nahi, sirf active subscription
    if not ok:
        raise HTTPException(402, reason)
    customer = db.get(Customer, data.customer_id)
    if not customer or customer.tenant_id != user.tenant_id:
        raise HTTPException(400, "Choose a customer")
    if not data.lines:
        raise HTTPException(400, "Add at least one line")

    lines = []
    for n, item in enumerate(data.lines, start=1):
        product = db.get(Product, item.product_id) if item.product_id else None
        if not product or product.tenant_id != user.tenant_id:
            raise HTTPException(400, f"Line {n}: choose a product")
        if item.quantity <= 0:
            raise HTTPException(400, f"Line {n}: quantity must be greater than 0")
        if item.unit_price is not None and item.unit_price < 0:
            raise HTTPException(400, f"Line {n}: price can't be negative")
        lines.append(DraftOrderLine(
            raw_text=(item.raw_text or f"{item.quantity:g} x {product.name}")[:500],
            quantity=item.quantity, unit=item.unit or product.unit, product_id=product.id,
            suggested_product_id=product.id, match_confidence=100, match_method="manual", candidates=[],
            unit_price=money(item.unit_price) if item.unit_price is not None else None,
        ))

    order = DraftOrder(
        tenant_id=user.tenant_id, customer_id=customer.id, customer_raw=customer.name,
        customer_confidence=100, po_number=(data.po_number or "").strip() or None,
        delivery_date=data.delivery_date, notes=data.notes, status="needs_review",
        overall_confidence=100, source="manual", created_by_id=user.id,
    )
    order.lines = lines
    db.add(order)
    db.flush()
    log_activity(db, user.tenant_id, user.id, "order.created_manual", "order", order.id, {"lines": len(lines)})
    db.commit()

    if data.approve:
        approve_order(db, order, user.id)
    return order
