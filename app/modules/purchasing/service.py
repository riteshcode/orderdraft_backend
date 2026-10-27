"""
Purchasing ka business logic (routers sirf validate + yahan call karte hain).

  save_po()        PO banao / draft edit karo (lines, cost, GST, totals)
  receive_stock()  maal aaya: GRN + stock badhao + average cost + PO status
  suggestions()    reorder level se kam wale products -> kitna mangwana hai

Costing = weighted average cost (Xero aur MYOB inventory bhi yahi use karte hain):
    naya avg = (purana qty x purana avg + aaya qty x aaya cost) / (purana qty + aaya qty)
    on hand 0 ya minus ho to naya avg = aaya cost.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.helpers import log_activity, money
from app.models import (Product, PurchaseOrder, PurchaseOrderLine, StockReceipt, StockReceiptLine, Supplier,
                        SupplierProduct, TenantSettings)
from app.modules.catalog.stock import record_movement

OPEN_STATUSES = ("draft", "sent", "partial")     # maal abhi aana baaki
RECEIVABLE = ("draft", "sent", "partial")


# ---------------- helpers ----------------
def supplier_link(db: Session, supplier_id: int, product_id: int) -> SupplierProduct | None:
    return db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == supplier_id,
                                                    SupplierProduct.product_id == product_id)).first()


def default_cost(db: Session, supplier_id: int, product: Product):
    """Line ka cost khali chhoda: supplier ka agreed cost -> last cost -> avg cost -> product ka purchase price."""
    link = supplier_link(db, supplier_id, product.id)
    for value in (link.unit_cost if link else None, link.last_cost if link else None, product.avg_cost,
                  product.purchase_price):
        if value is not None:
            return value
    return 0


def line_tax_rate(cfg: TenantSettings, product: Product | None) -> float:
    """Purchase pe GST: product GST-free ho to 0, warna client ka GST rate."""
    if product is not None and not product.is_taxable:
        return 0.0
    return float(cfg.gst_rate or 0)


def recalc(po: PurchaseOrder):
    """PO prices hamesha ex GST (AU B2B norm). Total = subtotal + GST."""
    subtotal = gst = money(0)
    for line in po.lines:
        line.line_total = money(money(line.unit_cost) * money(line.quantity))
        subtotal += money(line.line_total)
        gst += money(money(line.line_total) * money(line.tax_rate) / 100)
    po.subtotal, po.gst, po.total = money(subtotal), money(gst), money(subtotal + gst)


def on_order_map(db: Session, tenant_id: int) -> dict[int, float]:
    """{product_id: qty abhi aana baaki} - open POs (draft / sent / partial) se."""
    rows = db.execute(
        select(PurchaseOrderLine.product_id, func.sum(PurchaseOrderLine.quantity - PurchaseOrderLine.quantity_received))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .where(PurchaseOrder.tenant_id == tenant_id, PurchaseOrder.status.in_(OPEN_STATUSES),
               PurchaseOrderLine.product_id.is_not(None))
        .group_by(PurchaseOrderLine.product_id)).all()
    return {pid: max(float(q or 0), 0) for pid, q in rows}


def next_number(db: Session, cfg: TenantSettings, kind: str) -> str:
    """PO-1001 / GRN-1001. Settings mein number peeche kiya ho to bhi duplicate nahi banega."""
    model = PurchaseOrder if kind == "po" else StockReceipt
    while True:
        if kind == "po":
            number = f"{cfg.po_prefix}{cfg.po_next_number}"
            cfg.po_next_number += 1
        else:
            number = f"GRN-{cfg.receipt_next_number}"
            cfg.receipt_next_number += 1
        if not db.scalars(select(model.id).where(model.tenant_id == cfg.tenant_id, model.number == number)).first():
            return number


def _supplier(db: Session, tenant_id: int, supplier_id: int | None) -> Supplier:
    s = db.get(Supplier, supplier_id) if supplier_id else None
    if not s or s.tenant_id != tenant_id:
        raise HTTPException(400, "Choose a supplier")
    return s


def _product(db: Session, tenant_id: int, product_id: int | None, n: int) -> Product | None:
    if not product_id:
        return None
    p = db.get(Product, product_id)
    if not p or p.tenant_id != tenant_id or p.deleted_at:
        raise HTTPException(400, f"Line {n}: product invalid")
    return p


# ---------------- purchase orders ----------------
def save_po(db: Session, cfg: TenantSettings, data: dict, user_id: int | None, po: PurchaseOrder | None = None) -> PurchaseOrder:
    """
    data = {supplier_id, order_date?, expected_date?, reference?, deliver_to?, notes?,
            lines: [{product_id?, description?, quantity, unit_cost?, tax_rate?}]}
    """
    if po is not None and po.status != "draft":
        raise HTTPException(400, "Only draft POs can be edited")
    supplier = _supplier(db, cfg.tenant_id, data.get("supplier_id"))
    if not supplier.is_active and (po is None or po.supplier_id != supplier.id):
        raise HTTPException(400, "This supplier is inactive")
    lines_in = data.get("lines") or []
    if not lines_in:
        raise HTTPException(400, "Add at least one line")

    new_lines = []
    for n, l in enumerate(lines_in, start=1):
        product = _product(db, cfg.tenant_id, l.get("product_id"), n)
        qty = float(l.get("quantity") or 0)
        if qty <= 0:
            raise HTTPException(400, f"Line {n}: quantity must be greater than 0")
        if l.get("unit_cost") is not None and float(l["unit_cost"]) < 0:
            raise HTTPException(400, f"Line {n}: cost can't be negative")
        description = (l.get("description") or (product.name if product else "")).strip()
        if not description:
            raise HTTPException(400, f"Line {n}: choose a product or enter a description")
        link = supplier_link(db, supplier.id, product.id) if product else None
        cost = l["unit_cost"] if l.get("unit_cost") is not None else (default_cost(db, supplier.id, product) if product else 0)
        rate = l["tax_rate"] if l.get("tax_rate") is not None else line_tax_rate(cfg, product)
        new_lines.append(PurchaseOrderLine(
            tenant_id=cfg.tenant_id, product_id=product.id if product else None, description=description[:500],
            supplier_sku=link.supplier_sku if link else None, quantity=qty, unit_cost=money_4(cost), tax_rate=float(rate)))

    if po is None:
        po = PurchaseOrder(tenant_id=cfg.tenant_id, number=next_number(db, cfg, "po"), status="draft",
                           created_by_id=user_id)
        db.add(po)
        action = "po.created"
    else:
        po.lines.clear()
        db.flush()
        action = "po.updated"
    po.supplier_id = supplier.id
    po.order_date = data.get("order_date") or po.order_date or date.today()
    po.expected_date = data.get("expected_date") or expected_from_lead_time(db, supplier.id, new_lines, po.order_date)
    if po.expected_date and po.expected_date < po.order_date:
        raise HTTPException(400, "Expected date can't be before the order date")
    po.reference = (data.get("reference") or "").strip()[:100] or None
    po.deliver_to = (data.get("deliver_to") or "").strip() or None
    po.notes = (data.get("notes") or "").strip() or None
    po.lines.extend(new_lines)
    recalc(po)
    db.flush()
    log_activity(db, cfg.tenant_id, user_id, action, "purchase_order", po.id, {"number": po.number, "total": float(po.total)})
    return po


def expected_from_lead_time(db: Session, supplier_id: int, lines: list, order_date: date) -> date | None:
    """Expected date nahi diya: order date + sabse lamba lead time (supplier products se)."""
    from datetime import timedelta
    days = [link.lead_time_days for l in lines if l.product_id
            for link in [supplier_link(db, supplier_id, l.product_id)] if link and link.lead_time_days]
    return order_date + timedelta(days=max(days)) if days else None


def money_4(value):
    from decimal import ROUND_HALF_UP, Decimal
    return Decimal(str(value or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def set_status(db: Session, po: PurchaseOrder, action: str, user_id: int | None):
    """mark_sent | cancel | close | reopen"""
    received_any = any(l.quantity_received > 0 for l in po.lines)
    if action == "mark_sent":
        if po.status != "draft":
            raise HTTPException(400, "Only draft POs can be marked as sent")
        po.status, po.sent_at = "sent", utcnow()
    elif action == "cancel":
        if po.status not in ("draft", "sent") or received_any:
            raise HTTPException(400, "A PO that has already received stock can't be cancelled - close it instead")
        po.status = "cancelled"
    elif action == "close":
        if po.status != "partial":
            raise HTTPException(400, "Only partly received POs can be closed (the remaining stock won't be received)")
        po.status, po.received_at = "closed", utcnow()
    elif action == "reopen":
        if po.status != "closed":
            raise HTTPException(400, "Only closed POs can be reopened")
        po.status, po.received_at = "partial", None
    else:
        raise HTTPException(400, "Unknown action")
    log_activity(db, po.tenant_id, user_id, f"po.{action}", "purchase_order", po.id, {"number": po.number})


# ---------------- receiving ----------------
def update_avg_cost(product: Product, qty: float, unit_cost) -> None:
    """Weighted average cost. Stock movement se PEHLE call karo (purana on hand chahiye)."""
    cost = float(unit_cost or 0)
    before = max(product.quantity_on_hand or 0, 0) if product.is_tracked_inventory else 0
    if product.avg_cost is None or before <= 0:
        product.avg_cost = money_4(cost)
    else:
        product.avg_cost = money_4((before * float(product.avg_cost) + qty * cost) / (before + qty))


def receive_stock(db: Session, cfg: TenantSettings, user_id: int | None, *, supplier_id: int | None,
                  po: PurchaseOrder | None, lines_in: list[dict], received_on: date | None = None,
                  reference: str | None = None, notes: str | None = None) -> StockReceipt:
    """
    lines_in (PO ke against) = [{po_line_id, quantity, unit_cost?}]
    lines_in (bina PO)       = [{product_id, quantity, unit_cost}]
    Quantity 0 wali lines skip. Zyada maal aaya (over-receive) to bhi chalega - jo aaya wahi stock mein.
    """
    if po is not None:
        if po.status not in RECEIVABLE:
            raise HTTPException(400, f"Stock can't be received on this PO ({po.status})")
        supplier_id = po.supplier_id
    supplier = _supplier(db, cfg.tenant_id, supplier_id) if supplier_id else None
    po_lines = {l.id: l for l in po.lines} if po else {}

    receipt = StockReceipt(tenant_id=cfg.tenant_id, number=next_number(db, cfg, "receipt"),
                           supplier_id=supplier.id if supplier else None, po_id=po.id if po else None,
                           received_on=received_on or date.today(), reference=(reference or "").strip()[:100] or None,
                           notes=(notes or "").strip() or None, user_id=user_id)
    db.add(receipt)
    db.flush()

    total = money(0)
    new_links: dict[int, SupplierProduct] = {}   # is receipt mein bane links (autoflush off - query mein nahi milte)
    for n, l in enumerate(lines_in, start=1):
        qty = float(l.get("quantity") or 0)
        if qty < 0:
            raise HTTPException(400, f"Line {n}: quantity can't be negative")
        if qty == 0:
            continue
        po_line = None
        if po is not None:
            po_line = po_lines.get(l.get("po_line_id"))
            if not po_line:
                raise HTTPException(400, f"Line {n}: this line isn't on this PO")
            if not po_line.product_id:
                po_line.quantity_received += qty      # sirf description wali line (freight etc) - stock nahi
                continue
            product = db.get(Product, po_line.product_id)
            cost = l["unit_cost"] if l.get("unit_cost") is not None else po_line.unit_cost
        else:
            product = _product(db, cfg.tenant_id, l.get("product_id"), n)
            if not product:
                raise HTTPException(400, f"Line {n}: choose a product")
            cost = l.get("unit_cost")
            if cost is None:
                cost = default_cost(db, supplier.id, product) if supplier else (product.avg_cost or product.purchase_price or 0)
        if float(cost or 0) < 0:
            raise HTTPException(400, f"Line {n}: cost can't be negative")

        update_avg_cost(product, qty, cost)
        if product.is_tracked_inventory:
            label = f"{receipt.number}" + (f" · {po.number}" if po else "") + (f" · {supplier.name}" if supplier else "")
            record_movement(db, product, round((product.quantity_on_hand or 0) + qty, 4), "receive", label, user_id,
                            "receipt", receipt.id)
        if po_line is not None:
            po_line.quantity_received += qty
        if supplier:
            link = new_links.get(product.id) or supplier_link(db, supplier.id, product.id)
            if not link:   # pehli baar is supplier se aaya - link bana do
                link = SupplierProduct(tenant_id=cfg.tenant_id, supplier_id=supplier.id, product_id=product.id,
                                       unit_cost=money_4(cost))
                db.add(link)
                new_links[product.id] = link   # same product doosri line mein bhi ho to dobara insert na ho
            link.last_cost, link.last_received_at = money_4(cost), utcnow()
        receipt.lines.append(StockReceiptLine(tenant_id=cfg.tenant_id, po_line_id=po_line.id if po_line else None,
                                              product_id=product.id, quantity=qty, unit_cost=money_4(cost)))
        total += money(money(cost) * money(qty))

    if not receipt.lines and not (po and any(float(l.get("quantity") or 0) > 0 for l in lines_in)):
        raise HTTPException(400, "Enter a quantity for at least one line")
    receipt.total = money(total)

    if po is not None:
        if all(l.quantity_received >= l.quantity for l in po.lines):
            po.status, po.received_at = "received", utcnow()
        else:
            po.status = "partial"
    log_activity(db, cfg.tenant_id, user_id, "stock.received", "stock_receipt", receipt.id,
                 {"number": receipt.number, "po": po.number if po else None, "total": float(receipt.total)})
    db.flush()
    return receipt


# ---------------- reorder suggestions ----------------
def suggestions(db: Session, tenant_id: int) -> list[dict]:
    """
    Tracked product jiska (on hand + on order) <= reorder level.
    Suggested qty = reorder level ka double tak bharo (min order qty se kam nahi). User badal sakta hai.
    """
    on_order = on_order_map(db, tenant_id)
    products = db.scalars(select(Product).where(Product.tenant_id == tenant_id, Product.deleted_at.is_(None),
                                                Product.is_active.is_(True), Product.is_tracked_inventory.is_(True),
                                                Product.reorder_level.is_not(None)))
    out = []
    for p in products:
        qty, coming = p.quantity_on_hand or 0, on_order.get(p.id, 0)
        if qty + coming > p.reorder_level:
            continue
        links = list(db.scalars(select(SupplierProduct).join(Supplier, Supplier.id == SupplierProduct.supplier_id)
                                .where(SupplierProduct.product_id == p.id, Supplier.is_active.is_(True))))
        best = next((l for l in links if l.is_preferred), None) or \
            min(links, key=lambda l: float(l.unit_cost if l.unit_cost is not None else 1e12), default=None)
        suggested = max(p.reorder_level * 2 - qty - coming, (best.min_order_qty if best else 0) or 0, 1)
        out.append({
            "product_id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "quantity_on_hand": qty,
            "on_order": coming, "reorder_level": p.reorder_level, "suggested_qty": round(suggested, 2),
            "supplier_id": best.supplier_id if best else None, "supplier_name": best.supplier.name if best else None,
            "min_order_qty": best.min_order_qty if best else None, "lead_time_days": best.lead_time_days if best else None,
            "unit_cost": float(best.unit_cost) if best and best.unit_cost is not None else
            (float(p.avg_cost or p.purchase_price) if (p.avg_cost or p.purchase_price) is not None else None),
        })
    return sorted(out, key=lambda r: (r["supplier_name"] or "~", r["name"]))
