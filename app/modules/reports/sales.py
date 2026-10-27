"""
Sales reports - approved (aur exported) orders = sale. Value ex GST (approve ke time ka price).
Date = approve date, client ke timezone mein.

  GET /reports/sales/summary?date_from=&date_to=
  GET /reports/sales/by-category?date_from=&date_to=&level=parent|sub     (parent = sub-categories parent mein jud jaati hain)
  GET /reports/sales/by-customer?date_from=&date_to=
  GET /reports/sales/by-order?date_from=&date_to=&customer_id=&page=
  Har report: &format=csv  -> Excel ke liye CSV download
"""
import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.core.deps import current_user
from app.core.text import content_disposition
from app.models import Category, Customer, DraftOrder, DraftOrderLine, Product, User

router = APIRouter(prefix="/reports/sales", tags=["reports"])
SOLD = ("approved", "exported")


def _tz(user: User):
    from app.modules.reports.router import _tenant_tz
    return _tenant_tz(user.tenant)


def _range(user: User, date_from: date | None, date_to: date | None):
    """Client ke din (Sydney) -> UTC datetimes. Default: last 30 days."""
    tz = _tz(user)
    today = datetime.now(tz).date()
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=29))
    start = datetime.combine(date_from, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    return date_from, date_to, start, end


def _sold(user: User, start, end):
    return (DraftOrder.tenant_id == user.tenant_id, DraftOrder.status.in_(SOLD),
            DraftOrder.approved_at >= start, DraftOrder.approved_at < end)


LINE_VALUE = func.coalesce(DraftOrderLine.quantity * DraftOrderLine.unit_price, 0)


def _csv(filename: str, header: list[str], rows: list[list]) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": content_disposition(filename)})


def _r(v) -> float:
    return round(float(v or 0), 2)


def _local(dt: datetime | None, tz):
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).isoformat() if dt else None


@router.get("/summary")
def summary(date_from: date | None = None, date_to: date | None = None, user: User = Depends(current_user),
            db: Session = Depends(get_db)):
    d1, d2, start, end = _range(user, date_from, date_to)
    orders, value, customers = db.execute(
        select(func.count(DraftOrder.id), func.sum(DraftOrder.total_amount),
               func.count(func.distinct(DraftOrder.customer_id))).where(*_sold(user, start, end))).one()
    qty = db.scalar(select(func.sum(DraftOrderLine.quantity)).join(DraftOrder, DraftOrder.id == DraftOrderLine.order_id)
                    .where(*_sold(user, start, end))) or 0
    return {"date_from": d1, "date_to": d2, "orders": orders or 0, "value": _r(value), "quantity": float(qty),
            "customers": customers or 0, "average_order": _r((value or 0) / orders) if orders else 0}


@router.get("/by-category")
def by_category(date_from: date | None = None, date_to: date | None = None,
                level: str = Query("parent", pattern="^(parent|sub)$"), format: str = "json",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    d1, d2, start, end = _range(user, date_from, date_to)
    parent = aliased(Category)
    if level == "parent":   # sub-category ki sale uske parent mein
        key_id = func.coalesce(Category.parent_id, Category.id)
        key_name = func.coalesce(parent.name, Category.name)
    else:
        key_id = Category.id
        key_name = case((parent.name.is_not(None), parent.name + " › " + Category.name), else_=Category.name)
    rows = db.execute(
        select(key_id, key_name, func.sum(DraftOrderLine.quantity), func.sum(LINE_VALUE),
               func.count(func.distinct(DraftOrder.id)), func.count(func.distinct(DraftOrderLine.product_id)))
        .join(DraftOrder, DraftOrder.id == DraftOrderLine.order_id)
        .outerjoin(Product, Product.id == DraftOrderLine.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
        .outerjoin(parent, parent.id == Category.parent_id)
        .where(*_sold(user, start, end))
        .group_by(key_id, key_name)).all()
    total = sum(float(r[3] or 0) for r in rows) or 0
    items = sorted([{"category_id": r[0], "category": r[1] or "Uncategorised", "quantity": float(r[2] or 0),
                     "value": _r(r[3]), "orders": r[4], "products": r[5],
                     "share": round(float(r[3] or 0) * 100 / total, 1) if total else 0} for r in rows],
                   key=lambda x: -x["value"])
    if format == "csv":
        return _csv(f"sales-by-category-{d1}-{d2}.csv", ["Category", "Quantity", "Sales (ex GST)", "Orders",
                                                          "Products", "Share %"],
                    [[i["category"], i["quantity"], i["value"], i["orders"], i["products"], i["share"]] for i in items])
    return {"date_from": d1, "date_to": d2, "level": level, "total": _r(total), "items": items}


@router.get("/by-customer")
def by_customer(date_from: date | None = None, date_to: date | None = None, format: str = "json",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    d1, d2, start, end = _range(user, date_from, date_to)
    rows = db.execute(
        select(DraftOrder.customer_id, Customer.name, Customer.code, func.count(DraftOrder.id),
               func.sum(DraftOrder.total_amount), func.max(DraftOrder.approved_at))
        .outerjoin(Customer, Customer.id == DraftOrder.customer_id)
        .where(*_sold(user, start, end))
        .group_by(DraftOrder.customer_id, Customer.name, Customer.code)).all()
    qty = dict(db.execute(select(DraftOrder.customer_id, func.sum(DraftOrderLine.quantity))
                          .join(DraftOrderLine, DraftOrderLine.order_id == DraftOrder.id)
                          .where(*_sold(user, start, end)).group_by(DraftOrder.customer_id)).all())
    total = sum(float(r[4] or 0) for r in rows) or 0
    tz = _tz(user)
    items = sorted([{"customer_id": r[0], "customer": r[1] or "(No customer)", "code": r[2], "orders": r[3],
                     "quantity": float(qty.get(r[0]) or 0), "value": _r(r[4]),
                     "average_order": _r((r[4] or 0) / r[3]) if r[3] else 0,
                     "last_order": r[5], "last_order_local": _local(r[5], tz),
                     "share": round(float(r[4] or 0) * 100 / total, 1) if total else 0} for r in rows],
                   key=lambda x: -x["value"])
    if format == "csv":
        return _csv(f"sales-by-customer-{d1}-{d2}.csv",
                    ["Customer", "Code", "Orders", "Quantity", "Sales (ex GST)", "Average order", "Last order", "Share %"],
                    [[i["customer"], i["code"] or "", i["orders"], i["quantity"], i["value"], i["average_order"],
                      (i["last_order_local"] or "")[:10], i["share"]] for i in items])
    return {"date_from": d1, "date_to": d2, "total": _r(total), "items": items}


@router.get("/by-order")
def by_order(date_from: date | None = None, date_to: date | None = None, customer_id: int | None = None,
             page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500), format: str = "json",
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    d1, d2, start, end = _range(user, date_from, date_to)
    conds = list(_sold(user, start, end))
    if customer_id:
        conds.append(DraftOrder.customer_id == customer_id)
    lines = (select(DraftOrderLine.order_id, func.count(DraftOrderLine.id).label("n"),
                    func.sum(DraftOrderLine.quantity).label("qty"))
             .group_by(DraftOrderLine.order_id).subquery())
    stmt = (select(DraftOrder, Customer.name, lines.c.n, lines.c.qty)
            .outerjoin(Customer, Customer.id == DraftOrder.customer_id)
            .outerjoin(lines, lines.c.order_id == DraftOrder.id)
            .where(*conds).order_by(DraftOrder.approved_at.desc(), DraftOrder.id.desc()))
    total_count, total_value = db.execute(select(func.count(DraftOrder.id), func.sum(DraftOrder.total_amount))
                                          .where(*conds)).one()
    if format != "csv":
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    tz = _tz(user)
    items = [{"order_id": o.id, "approved_at": o.approved_at, "approved_local": _local(o.approved_at, tz), "delivery_date": o.delivery_date,
              "customer_id": o.customer_id, "customer": name or o.customer_raw or "(No customer)",
              "po_number": o.po_number, "status": o.status, "source": o.source, "lines": n or 0,
              "quantity": float(qty or 0), "value": _r(o.total_amount)} for o, name, n, qty in db.execute(stmt).all()]
    if format == "csv":
        return _csv(f"sales-by-order-{d1}-{d2}.csv",
                    ["Order #", "Approved", "Customer", "PO number", "Delivery date", "Lines", "Quantity",
                     "Sales (ex GST)", "Status"],
                    [[i["order_id"], (i["approved_local"] or "")[:16].replace("T", " "), i["customer"], i["po_number"] or "",
                      i["delivery_date"] or "", i["lines"], i["quantity"], i["value"], i["status"]] for i in items])
    return {"date_from": d1, "date_to": d2, "total_value": _r(total_value), "items": items, "total": total_count,
            "page": page, "page_size": page_size}
