"""
Standing orders - logic (dates, validation, order banana).

Dates kaise nikalte hain (example: delivery Monday, order Saturday 06:00, har 2 hafte, start 3 Nov):
  pehli delivery   = start date ke baad ka pehla Monday            -> Mon 3 Nov
  deliveries       = har 2 hafte                                   -> 3 Nov, 17 Nov, 1 Dec ...
  order kab bane   = delivery se (Mon - Sat) % 7 = 2 din pehle      -> Sat 1 Nov 06:00, Sat 15 Nov 06:00 ...
  skip_dates mein delivery date ho to us hafte order nahi banta.
  Chhutti (Settings -> Customer portal -> Closed dates) wali delivery bhi skip (staff ko bell mein bataya jaata hai).

Scheduler har 15 min run_due() chalata hai. Order ka time nikal gaya (server band tha) par delivery abhi
aage hai to bhi order ban jaata hai. Ek delivery date ka order sirf ek baar (last_generated_for + DB check).
Order hamesha "Needs review" mein banta hai - staff approve karta hai.
"""
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.helpers import get_tenant_settings, log_activity, money, to_float
from app.core.request_log import log_event
from app.models import Customer, DraftOrder, DraftOrderLine, Product, StandingOrder, StandingOrderLine, Tenant

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
EVERY_WEEKS = (1, 2, 3, 4)


class LineIn(BaseModel):
    product_id: int
    quantity: float


class StandingIn(BaseModel):
    name: str | None = None
    is_active: bool = True
    delivery_weekday: int
    generate_weekday: int
    generate_time: str = "06:00"
    every_weeks: int = 1
    start_date: date | None = None
    end_date: date | None = None
    po_number: str | None = None
    notes: str | None = None
    payment_method: str | None = None
    lines: list[LineIn] = []


# ---------------- dates ----------------
def local_now(tenant) -> datetime:
    from app.modules.reports.router import _tenant_tz
    return datetime.now(timezone.utc).astimezone(_tenant_tz(tenant)).replace(tzinfo=None)


def parse_time(value: str | None) -> time:
    try:
        h, m = (value or "").split(":")
        return time(int(h), int(m))
    except ValueError:
        return time(6, 0)


def lead_days(so: StandingOrder) -> int:
    """Order delivery se kitne din pehle banta hai (0 = usi din)."""
    return (so.delivery_weekday - so.generate_weekday) % 7


def first_delivery(so: StandingOrder) -> date:
    d = so.start_date
    while d.weekday() != so.delivery_weekday:
        d += timedelta(days=1)
    return d


def deliveries(so: StandingOrder, from_date: date, count: int) -> list[date]:
    """from_date ya uske baad ki delivery dates (every_weeks + end_date ke hisaab se). Skip wali bhi shamil."""
    first = first_delivery(so)
    d = max(first, from_date)
    while d.weekday() != so.delivery_weekday:
        d += timedelta(days=1)
    weeks = max(so.every_weeks or 1, 1)
    while ((d - first).days // 7) % weeks:
        d += timedelta(days=7)
    out = []
    while len(out) < count and (not so.end_date or d <= so.end_date):
        out.append(d)
        d += timedelta(days=7 * weeks)
    return out


def is_skipped(so: StandingOrder, d: date) -> bool:
    return d.isoformat() in (so.skip_dates or [])


def due_deliveries(so: StandingOrder, now: datetime) -> list[date]:
    """Abhi kin delivery dates ka order banna chahiye (order time aa gaya, delivery abhi aage, bana nahi)."""
    today, k = now.date(), lead_days(so)
    created = so.created_at.date() if so.created_at else so.start_date
    out = []
    for d in deliveries(so, today, 2):
        gen = d - timedelta(days=k)
        if gen > today or (gen == today and now.time() < parse_time(so.generate_time)):
            break
        if gen < created:      # standing order bana usse pehle ka order-day - purana order mat banao
            continue
        if is_skipped(so, d) or (so.last_generated_for and d <= so.last_generated_for):
            continue
        out.append(d)
    return out


def upcoming(so: StandingOrder, now: datetime, count: int = 4) -> list[dict]:
    """UI ke liye agli deliveries: kab order banega, skip hai ya nahi, order ban gaya to uska id."""
    out = []
    for d in deliveries(so, now.date(), count):
        gen = datetime.combine(d - timedelta(days=lead_days(so)), parse_time(so.generate_time))
        # skip wali date "bani" nahi maani jaati (create-now ne aage wali bana di ho tab bhi)
        done = bool(so.last_generated_for and (d == so.last_generated_for or
                                               (d < so.last_generated_for and not is_skipped(so, d))))
        out.append({"delivery_date": d, "generate_at": gen, "skipped": is_skipped(so, d),
                    "order_id": so.last_order_id if done and d == so.last_generated_for else None, "generated": done})
    return out


def schedule_text(so: StandingOrder) -> str:
    every = {1: "Every", 2: "Every 2nd", 3: "Every 3rd", 4: "Every 4th"}.get(so.every_weeks or 1, "Every")
    t = parse_time(so.generate_time)
    clock = f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"
    when = "the same day" if lead_days(so) == 0 else WEEKDAYS[so.generate_weekday]
    return f"{every} {WEEKDAYS[so.delivery_weekday]} · order created {when} at {clock}"


# ---------------- validation ----------------
def apply_input(db: Session, so: StandingOrder, data: StandingIn, tenant, portal: bool = False) -> None:
    """Form se aaya data check karke standing order pe lagao. Galat ho to 400 (English message)."""
    if not (0 <= data.delivery_weekday <= 6 and 0 <= data.generate_weekday <= 6):
        raise HTTPException(400, "Choose a delivery day and an order day")
    if data.every_weeks not in EVERY_WEEKS:
        raise HTTPException(400, "Repeat must be every 1, 2, 3 or 4 weeks")
    t = (data.generate_time or "06:00").strip()
    try:
        h, m = t.split(":")
        time(int(h), int(m))
    except ValueError:
        raise HTTPException(400, "Order time must look like 06:00")
    start = data.start_date or local_now(tenant).date()
    if data.end_date and data.end_date < start:
        raise HTTPException(400, "The end date can't be before the start date")
    if not data.lines:
        raise HTTPException(400, "Add at least one product")

    lines, seen = [], set()
    for n, l in enumerate(data.lines, start=1):
        p = db.get(Product, l.product_id)
        if not p or p.tenant_id != so.tenant_id or p.deleted_at or not p.is_active:
            raise HTTPException(400, f"Line {n}: choose an active product")
        if not l.quantity or l.quantity <= 0:
            raise HTTPException(400, f"Line {n}: quantity must be more than 0")
        if p.id in seen:
            raise HTTPException(400, f"{p.name} is on the list twice - change the quantity instead")
        seen.add(p.id)
        lines.append((p.id, float(l.quantity)))

    so.name = (data.name or "").strip()[:150] or f"{WEEKDAYS[data.delivery_weekday]} order"
    so.is_active = data.is_active
    schedule_changed = (so.delivery_weekday, so.generate_weekday, so.every_weeks, so.start_date) != \
        (data.delivery_weekday, data.generate_weekday, data.every_weeks, start)
    so.delivery_weekday, so.generate_weekday = data.delivery_weekday, data.generate_weekday
    so.generate_time, so.every_weeks = f"{int(h):02d}:{int(m):02d}", data.every_weeks
    so.start_date, so.end_date = start, data.end_date
    so.po_number = (data.po_number or "").strip()[:100] or None
    so.notes = (data.notes or "").strip()[:2000] or None
    if portal or data.payment_method is not None:
        so.payment_method = data.payment_method
    if schedule_changed and so.id:
        # din badla - purani skip dates ab match nahi karti
        so.skip_dates = [d for d in (so.skip_dates or []) if d in {x.isoformat() for x in deliveries(so, start, 26)}] or None

    existing = {l.product_id: l for l in so.lines}
    so.lines = [existing.get(pid) or StandingOrderLine(product_id=pid) for pid, _ in lines]
    for line, (_, qty) in zip(so.lines, lines):
        line.quantity = qty


def set_skip(so: StandingOrder, d: date, skip: bool) -> None:
    if d not in deliveries(so, d, 1):
        raise HTTPException(400, f"{d:%a %d %b} isn't a delivery date for this standing order")
    dates = set(so.skip_dates or [])
    dates.add(d.isoformat()) if skip else dates.discard(d.isoformat())
    so.skip_dates = sorted(dates) or None


def standing_out(db: Session, so: StandingOrder, now: datetime, with_prices: bool = False) -> dict:
    prices = {}
    if with_prices:
        from app.modules.catalog.pricing import price_map
        special = price_map(db, so.customer_id)
        prices = {l.product_id: special.get(l.product_id, l.product.price) for l in so.lines}
    lines = [{"id": l.id, "product_id": l.product_id, "name": l.product.name, "sku": l.product.sku,
              "unit": l.product.unit, "quantity": l.quantity,
              "available": bool(l.product.is_active and not l.product.deleted_at),
              **({"price": to_float(prices.get(l.product_id))} if with_prices else {})} for l in so.lines]
    out = {"id": so.id, "customer_id": so.customer_id, "customer_name": so.customer.name if so.customer else None,
           "name": so.name, "is_active": so.is_active, "delivery_weekday": so.delivery_weekday,
           "generate_weekday": so.generate_weekday, "generate_time": so.generate_time, "every_weeks": so.every_weeks,
           "start_date": so.start_date, "end_date": so.end_date, "skip_dates": sorted(so.skip_dates or []),
           "po_number": so.po_number, "notes": so.notes, "payment_method": so.payment_method,
           "created_by": so.created_by, "last_generated_for": so.last_generated_for, "last_order_id": so.last_order_id,
           "schedule": schedule_text(so), "lines": lines,
           "upcoming": upcoming(so, now) if so.is_active else [], "created_at": so.created_at}
    if with_prices:
        out["estimate"] = round(sum((l["price"] or 0) * l["quantity"] for l in lines), 2)
    return out


# ---------------- order banana ----------------
def create_order(db: Session, tenant, so: StandingOrder, delivery: date, user_id: int | None = None) -> DraftOrder | None:
    """
    Ek delivery date ka order (Needs review). Commit karta hai.
    None = nahi bana (customer band, sab products band, chhutti, subscription) - staff ko bell mein reason.
    """
    from app.modules.billing.limits import subscription_ok
    from app.modules.notifications.staff import notify_staff
    from app.modules.portal.notify import notify_customer_order

    cfg = get_tenant_settings(db, so.tenant_id)
    customer: Customer = so.customer

    def skip(reason: str):
        so.last_generated_for = delivery          # is date ke liye dobara try mat karo
        log_activity(db, so.tenant_id, user_id, "standing.skipped", "standing_order", so.id,
                     {"delivery_date": delivery.isoformat(), "reason": reason})
        notify_staff(db, so.tenant_id, "standing_skipped", f"Standing order not created: {customer.name}",
                     f"{so.name} for {delivery:%a %d %b} - {reason}", f"/customers/{customer.id}?tab=standing")
        db.commit()
        return None

    if db.scalars(select(DraftOrder.id).where(DraftOrder.standing_order_id == so.id,
                                              DraftOrder.delivery_date == delivery)).first():
        so.last_generated_for = max(so.last_generated_for or delivery, delivery)
        db.commit()
        return None
    if not customer or customer.deleted_at or not customer.is_active:
        return skip("the customer is inactive")
    if delivery.isoformat() in (cfg.portal_closed_dates or []):
        return skip("that day is a closed date (Settings → Customer portal)")
    ok, reason = subscription_ok(db.get(Tenant, so.tenant_id))
    if not ok:
        return skip(f"your OrderDraft subscription isn't active ({reason})")
    lines = [l for l in so.lines if l.product and l.product.is_active and not l.product.deleted_at]
    if not lines:
        return skip("none of its products are active any more")

    from app.modules.catalog.pricing import price_map
    special = price_map(db, customer.id)
    estimate = money(0)
    for l in lines:
        price = special.get(l.product_id, l.product.price)
        if price is not None:
            estimate += money(price) * money(l.quantity)
    note = f"Standing order: {so.name}" + (f"\n{so.notes}" if so.notes else "")
    order = DraftOrder(
        tenant_id=so.tenant_id, customer_id=customer.id, customer_raw=customer.name, customer_confidence=100,
        po_number=so.po_number, delivery_date=delivery, notes=note, status="needs_review", overall_confidence=100,
        source="standing", payment_method=so.payment_method, portal_login_id=so.portal_login_id,
        standing_order_id=so.id, created_by_id=user_id, total_amount=money(estimate),
    )
    # Price lock nahi - approve pe customer ki us din ki price lagti hai
    order.lines = [DraftOrderLine(
        raw_text=f"{l.quantity:g} x {l.product.name}"[:500], quantity=l.quantity, unit=l.product.unit,
        product_id=l.product_id, suggested_product_id=l.product_id, match_confidence=100, match_method="standing",
        candidates=[]) for l in lines]
    db.add(order)
    db.flush()
    so.last_generated_for, so.last_order_id = delivery, order.id
    missing = len(so.lines) - len(lines)
    log_activity(db, so.tenant_id, user_id, "order.created_standing", "order", order.id,
                 {"standing_order": so.name, "delivery_date": delivery.isoformat(), "lines": len(lines),
                  **({"skipped_inactive_products": missing} if missing else {})})
    db.commit()

    body = f"{so.name} · {len(lines)} item{'s' if len(lines) != 1 else ''} · delivery {delivery:%a %d %b}"
    if missing:
        body += f" · {missing} inactive product{'s' if missing != 1 else ''} left out"
    notify_staff(db, so.tenant_id, "standing_order", f"Standing order #{order.id} for {customer.name}", body,
                 f"/orders/{order.id}", order.id)
    db.commit()
    _email_staff(db, cfg, tenant, order, customer, body)
    notify_customer_order(db, tenant, order, "placed")
    return order


def _email_staff(db: Session, cfg, tenant, order: DraftOrder, customer: Customer, body: str) -> None:
    """Portal order jaisa - "Email new orders to" (Settings -> Customer portal) ya business email."""
    import html
    from app.core.config import settings
    from app.core.mailer import MailError, send_email, simple_html
    staff = cfg.portal_notify_email or cfg.email
    if not staff:
        return
    try:
        send_email(staff, f"Standing order #{order.id} for {customer.name}",
                   simple_html("New standing order", f"<p>{html.escape(body)}</p><p>It's waiting for review.</p>",
                               "Review order", f"{settings.FRONTEND_URL}/orders/{order.id}"))
    except MailError as err:
        log_event("standing_staff_email_failed", tenant_id=order.tenant_id, order_id=order.id, error=str(err)[:200])


def run_due(db: Session, tenant_id: int) -> int:
    """Scheduler (har 15 min): is client ke saare active standing orders - jinka time aa gaya unka order."""
    tenant = db.get(Tenant, tenant_id)
    now = local_now(tenant)
    made = 0
    for so in list(db.scalars(select(StandingOrder).where(StandingOrder.tenant_id == tenant_id,
                                                          StandingOrder.is_active.is_(True)))):
        for d in due_deliveries(so, now):
            try:
                if create_order(db, tenant, so, d):
                    made += 1
            except Exception as err:
                db.rollback()
                log_event("standing_order_failed", tenant_id=tenant_id, standing_order_id=so.id, error=str(err)[:200])
    return made


def next_delivery_to_create(so: StandingOrder, now: datetime) -> date | None:
    """"Create next order now" button: agli delivery jiska order abhi nahi bana aur skip nahi."""
    for d in deliveries(so, now.date(), 12):
        if not is_skipped(so, d) and not (so.last_generated_for and d <= so.last_generated_for):
            return d
    return None
