"""
Dashboard numbers + onboarding checklist + activity log.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, utcnow
from app.core.deps import current_user
from app.models import (
    ActivityLog, Connection, Customer, Destination, DraftOrder, DraftOrderLine, InboundMessage, Invoice, Product, User,
)
from app.core.helpers import get_tenant_settings

router = APIRouter(tags=["reports"])


def _tenant_tz(tenant):
    try:
        return ZoneInfo(tenant.timezone or "Australia/Sydney")
    except Exception:   # Windows pe "tzdata" package na ho to
        return timezone.utc


@router.get("/reports/dashboard")
def dashboard(days: int = Query(30, ge=1, le=365), user: User = Depends(current_user), db: Session = Depends(get_db)):
    tid = user.tenant_id
    since = utcnow() - timedelta(days=days)

    # ---- Orders ----
    by_status = dict(db.execute(
        select(DraftOrder.status, func.count()).where(DraftOrder.tenant_id == tid, DraftOrder.created_at >= since)
        .group_by(DraftOrder.status)).all())
    approved_value = db.scalar(select(func.coalesce(func.sum(DraftOrder.total_amount), 0)).where(
        DraftOrder.tenant_id == tid, DraftOrder.created_at >= since,
        DraftOrder.status.in_(["approved", "exported"]))) or 0

    # Day-wise series - tenant ke timezone mein (Sydney ki subah 9 baje = UTC mein pichla din)
    # Python mein group karte hain taaki SQLite/MySQL dono pe same chale
    tz = _tenant_tz(get_tenant_settings(db, tid))
    today = datetime.now(tz).date()
    series = {(today - timedelta(days=i)).isoformat(): {"orders": 0, "value": 0.0} for i in range(days - 1, -1, -1)}
    for created, status, amount in db.execute(
            select(DraftOrder.created_at, DraftOrder.status, DraftOrder.total_amount)
            .where(DraftOrder.tenant_id == tid, DraftOrder.created_at >= since)):
        key = created.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
        if key in series:
            series[key]["orders"] += 1
            if status in ("approved", "exported") and amount:
                series[key]["value"] += float(amount)

    by_channel = dict(db.execute(
        select(InboundMessage.channel, func.count()).join(DraftOrder, DraftOrder.message_id == InboundMessage.id)
        .where(DraftOrder.tenant_id == tid, DraftOrder.created_at >= since).group_by(InboundMessage.channel)).all())

    top_customers = [
        {"customer_id": cid, "name": name, "orders": n, "value": float(v or 0)}
        for cid, name, n, v in db.execute(
            select(Customer.id, Customer.name, func.count(DraftOrder.id), func.sum(DraftOrder.total_amount))
            .join(DraftOrder, DraftOrder.customer_id == Customer.id)
            .where(DraftOrder.tenant_id == tid, DraftOrder.created_at >= since,
                   DraftOrder.status.in_(["approved", "exported"]))
            .group_by(Customer.id, Customer.name)
            .order_by(func.sum(DraftOrder.total_amount).desc()).limit(8))
    ]

    # ---- AI accuracy: approved lines jinka product system ne sahi suggest kiya ----
    line_rows = db.execute(
        select(DraftOrderLine.product_id, DraftOrderLine.suggested_product_id, DraftOrderLine.match_method)
        .join(DraftOrder, DraftOrder.id == DraftOrderLine.order_id)
        .where(DraftOrder.tenant_id == tid, DraftOrder.approved_at >= since,
               DraftOrder.status.in_(["approved", "exported"]))).all()
    total_lines = len(line_rows)
    correct = sum(1 for p, s, m in line_rows if p and p == s)
    accuracy = round(correct * 100 / total_lines, 1) if total_lines else None

    approved_orders = by_status.get("approved", 0) + by_status.get("exported", 0)
    saved_seconds = approved_orders * settings.MANUAL_SECONDS_PER_ORDER + total_lines * settings.MANUAL_SECONDS_PER_LINE

    avg_ms = db.scalar(select(func.avg(InboundMessage.processing_ms)).where(
        InboundMessage.tenant_id == tid, InboundMessage.created_at >= since, InboundMessage.processing_ms.is_not(None)))

    # ---- Invoices ----
    inv = db.execute(
        select(func.count(), func.coalesce(func.sum(Invoice.total), 0), func.coalesce(func.sum(Invoice.amount_paid), 0))
        .where(Invoice.tenant_id == tid, Invoice.created_at >= since, Invoice.status != "void")).one()
    sent = db.scalar(select(func.count()).select_from(Invoice).where(
        Invoice.tenant_id == tid, Invoice.created_at >= since, Invoice.status.in_(["sent", "paid"])))
    overdue = db.scalar(select(func.count()).select_from(Invoice).where(
        Invoice.tenant_id == tid, Invoice.status.in_(["authorised", "sent"]), Invoice.due_date < date.today()))

    return {
        "days": days,
        "orders": {
            "received": sum(by_status.values()),
            "needs_review": by_status.get("needs_review", 0),
            "approved": approved_orders,
            "exported": by_status.get("exported", 0),
            "rejected": by_status.get("rejected", 0),
            "approved_value": round(float(approved_value), 2),
        },
        "invoices": {
            "count": inv[0], "total": round(float(inv[1]), 2), "paid": round(float(inv[2]), 2),
            "outstanding": round(float(inv[1]) - float(inv[2]), 2), "sent": sent, "overdue": overdue,
        },
        "accuracy_percent": accuracy,
        "lines_processed": total_lines,
        "time_saved_hours": round(saved_seconds / 3600, 1),
        "avg_processing_seconds": round(avg_ms / 1000, 1) if avg_ms else None,
        "series": [{"date": d, **v} for d, v in series.items()],
        "by_channel": by_channel,
        "top_customers": top_customers,
    }


@router.get("/onboarding")
def onboarding(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tid = user.tenant_id
    tenant = user.tenant
    cfg = get_tenant_settings(db, tid)

    def exists(model, *conds):
        return db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tid, *conds)) > 0

    steps = [
        {"key": "catalog", "title": "Add your products",
         "description": "Connect Xero, QuickBooks or MYOB - or upload a CSV of your products.", "done": exists(Product), "link": "/imports"},
        {"key": "customers", "title": "Add your customers",
         "description": "So we can recognise who is ordering.", "done": exists(Customer), "link": "/imports"},
        {"key": "destination", "title": "Choose where approved orders go",
         "description": "Your accounting software, OrderDraft invoices, your API or a webhook.", "done": exists(Destination), "link": "/destinations"},
        {"key": "first_order", "title": "Send your first order",
         "description": f"Forward an order email to {tenant.slug}@{settings.INBOUND_DOMAIN} or paste one.",
         "done": exists(InboundMessage), "link": "/orders/new"},
        {"key": "first_approval", "title": "Approve a draft order",
         "description": "Review the AI draft and approve it.",
         "done": exists(DraftOrder, DraftOrder.status.in_(["approved", "exported"])), "link": "/orders"},
    ]
    return {
        "steps": steps,
        "completed": sum(s["done"] for s in steps),
        "total": len(steps),
        "dismissed": cfg.onboarding_dismissed,
        "accounting_connected": db.scalar(select(func.count()).select_from(Connection).where(
            Connection.tenant_id == tid, Connection.status == "connected")) > 0,
    }


@router.post("/onboarding/dismiss")
def dismiss_onboarding(user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_tenant_settings(db, user.tenant_id).onboarding_dismissed = True
    db.commit()
    return {"ok": True}


@router.get("/activity")
def activity(limit: int = Query(50, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)):
    users = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == user.tenant_id))}
    rows = db.scalars(select(ActivityLog).where(ActivityLog.tenant_id == user.tenant_id)
                      .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(limit))
    return [{"id": r.id, "action": r.action, "entity": r.entity, "entity_id": r.entity_id, "detail": r.detail,
             "user_name": users.get(r.user_id) if r.user_id else "System", "created_at": r.created_at} for r in rows]
