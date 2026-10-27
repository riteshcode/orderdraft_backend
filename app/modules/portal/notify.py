"""
Customer notifications - admin apne customers ko message bheje (naya stock, delivery change, holiday hours...).
Portal mein bell icon, phone/desktop par push (agar customer ne on kiya), aur chaho to email bhi.

Staff (Settings -> Customer portal -> Notify customers):
  GET    /portal-notifications
  POST   /portal-notifications        {title, body, link_url, audience: all|customers, customer_ids, send_push, send_email}
  DELETE /portal-notifications/{id}

Customer portal (login):
  GET  /portal/{slug}/notifications             list + unread count
  POST /portal/{slug}/notifications/read        {ids: [..]} ya {} = sab padh liye
  POST /portal/{slug}/push/subscribe            {endpoint, keys: {p256dh, auth}}
  POST /portal/{slug}/push/unsubscribe          {endpoint}
"""
import html

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, utcnow
from app.core.deps import current_user, require_role
from app.core.helpers import get_tenant_settings, log_activity
from app.core.mailer import MailError, send_email, sender_for, simple_html
from app.core.request_log import log_event
from app.core.webpush import push_enabled, send_push
from app.models import (Customer, CustomerLogin, PortalNotification, PortalNotificationRead, PortalPushSubscription,
                        User)
from app.modules.portal.shop import Ctx, login_ctx

router = APIRouter(tags=["portal-notifications"])
admin_only = require_role("owner", "admin")


class NotificationIn(BaseModel):
    title: str
    body: str | None = None
    link_url: str | None = None
    audience: str = "all"
    customer_ids: list[int] = []
    send_push: bool = True
    send_email: bool = False


def notification_out(n: PortalNotification, read: bool | None = None) -> dict:
    d = {"id": n.id, "title": n.title, "body": n.body, "link_url": n.link_url, "created_at": n.created_at}
    if read is not None:
        d["read"] = read
    return d


def _recipients(db: Session, tenant_id: int, n: PortalNotification) -> list[CustomerLogin]:
    stmt = (select(CustomerLogin).join(Customer, Customer.id == CustomerLogin.customer_id)
            .where(CustomerLogin.tenant_id == tenant_id, CustomerLogin.is_active.is_(True),
                   Customer.deleted_at.is_(None), Customer.is_active.is_(True)))
    if n.audience == "customers":
        stmt = stmt.where(CustomerLogin.customer_id.in_(n.customer_ids or []))
    return list(db.scalars(stmt))


def _visible(n: PortalNotification, login: CustomerLogin) -> bool:
    return n.audience == "all" or login.customer_id in (n.customer_ids or [])


def deliver(db: Session, tenant, n: PortalNotification) -> None:
    """Push + email bhejo (bell mein to apne aap dikhta hai)."""
    logins = _recipients(db, tenant.id, n)
    slug_url = f"{settings.FRONTEND_URL}/portal/{tenant.slug}"
    link = n.link_url or f"/portal/{tenant.slug}/notifications"
    if n.send_push and push_enabled():
        subs = db.scalars(select(PortalPushSubscription).where(
            PortalPushSubscription.login_id.in_([l.id for l in logins]))) if logins else []
        for sub in subs:
            result = send_push({"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth},
                               {"title": n.title, "body": n.body or "", "url": link, "tag": f"n{n.id}"})
            if result == "ok":
                n.push_sent += 1
                sub.last_sent_at = utcnow()
            elif result == "gone":           # customer ne notifications band kar di / app hata diya
                db.delete(sub)
            else:
                n.push_failed += 1
    if n.send_email and logins:
        cfg = get_tenant_settings(db, tenant.id)
        sender, reply_to = sender_for(cfg)
        e = html.escape
        url = link if link.startswith("http") else f"{settings.FRONTEND_URL}{link}" if link.startswith("/") else slug_url
        for l in logins:
            try:
                send_email(l.email, n.title, simple_html(n.title, f"<p>{e(n.body or '')}</p>".replace("\n", "<br/>"),
                                                         "Open", url),
                           n.body or "", from_email=sender, reply_to=reply_to)
                n.email_sent += 1
            except MailError:
                pass


CUSTOMER_ORDER_SOURCES = ("portal", "standing")     # in orders ka customer portal pe hai - unhe updates jaate hain
UPDATE_WINDOW_MINUTES = 15                          # itni der mein dobara edit = wahi message update (spam nahi)


def _order_message(order, event: str, reason: str | None, changes: list[str] | None,
                   invoice: str | None) -> tuple[str, str]:
    """(title, body) - customer ki bhasha mein, English."""
    n = order.id
    when = f" for delivery on {order.delivery_date:%a %d %b}" if order.delivery_date else ""
    if event == "confirmed":
        return f"Order #{n} confirmed", f"Your order #{n} has been confirmed{when}."
    if event == "rejected":
        body = f"Sorry, we couldn't accept your order #{n}."
        if reason:
            body += f"\nReason: {reason}"
        return f"Order #{n} was not accepted", body + "\nPlease contact us if you have any questions."
    if event == "cancelled":
        body = f"Your order #{n} has been cancelled."
        if reason:
            body += f"\nReason: {reason}"
        return f"Order #{n} cancelled", body + "\nPlease contact us if you have any questions."
    if event == "updated":
        return f"Order #{n} updated", "We've made changes to your order:\n" + "\n".join(f"- {c}" for c in (changes or []))
    if event == "invoiced":
        return f"Invoice for order #{n}", f"Your invoice {invoice or ''} for order #{n} is ready.".replace("  ", " ")
    if event == "placed":
        return (f"Your usual order #{n} has been placed",
                f"We've placed your standing order #{n}{when}. We'll confirm it shortly - you can change it by contacting us.")
    return f"Order #{n}", ""


def notify_customer_order(db: Session, tenant, order, event: str, reason: str | None = None,
                          changes: list[str] | None = None, invoice: str | None = None,
                          email: bool = True) -> PortalNotification | None:
    """
    Order ka update customer ko: bell + push (customer ke saare logins), aur email us login ko jisne order kiya.
      event: confirmed | rejected | cancelled | updated | invoiced | placed
    Sirf portal / standing orders. Kuch bhi fail ho to order ka kaam nahi rukta (sirf log).
    updated: 15 min ke andar dobara edit -> wahi message update hota hai (dobara push/email nahi).
    invoiced: ek order pe ek hi baar.
    """
    from datetime import timedelta
    if order.source not in CUSTOMER_ORDER_SOURCES or not order.customer_id:
        return None
    try:
        title, body = _order_message(order, event, reason, changes, invoice)
        link = f"/portal/{tenant.slug}/orders/{order.id}"
        previous = db.scalars(select(PortalNotification).where(
            PortalNotification.order_id == order.id, PortalNotification.event == event)
            .order_by(PortalNotification.id.desc())).first()
        if event == "invoiced" and previous:
            return None
        if event == "updated" and previous and previous.created_at > utcnow() - timedelta(minutes=UPDATE_WINDOW_MINUTES):
            previous.body, previous.created_at = body, utcnow()
            db.query(PortalNotificationRead).filter(PortalNotificationRead.notification_id == previous.id).delete()
            db.commit()                       # dobara unread - par push/email nahi
            return previous

        n = PortalNotification(tenant_id=tenant.id, kind="order", order_id=order.id, event=event, title=title[:150],
                               body=body, link_url=link, audience="customers", customer_ids=[order.customer_id],
                               send_push=True, send_email=False)
        db.add(n)
        db.flush()
        deliver(db, tenant, n)        # bell + push

        login = db.get(CustomerLogin, order.portal_login_id) if order.portal_login_id else None
        if email and login and login.is_active:
            cfg = get_tenant_settings(db, tenant.id)
            customer_email = (order.customer.email or "").strip().lower() if order.customer else ""
            # Approve pe "Order confirmation email" (Settings -> Email) usi address pe jaata hai to doosra nahi
            already_emailed = event == "confirmed" and cfg.order_email_enabled and customer_email == login.email.lower()
            if not already_emailed:
                e = html.escape
                sender, reply_to = sender_for(cfg)
                business = cfg.business_name or tenant.name
                text = f"<p>Hi {e(login.name or order.customer.name)},</p><p>{e(body)}</p>".replace("\n", "<br/>")
                if order.po_number:
                    text += f"<p>Your PO: {e(order.po_number)}</p>"
                try:
                    send_email(login.email, f"{title} - {business}",
                               simple_html(title, text, "View order", f"{settings.FRONTEND_URL}{link}"),
                               body, from_email=sender, reply_to=reply_to)
                    n.email_sent = 1
                except MailError as err:
                    log_event("portal_order_email_failed", tenant_id=tenant.id, order_id=order.id, event=event,
                              error=str(err)[:200])
        db.commit()
        return n
    except Exception as err:   # notification ki wajah se asli kaam fail nahi hona chahiye
        db.rollback()
        log_event("portal_order_notify_failed", tenant_id=tenant.id, order_id=order.id, event=event, error=str(err)[:200])
        return None


def notify_order_status(db: Session, tenant, order, reason: str | None = None) -> PortalNotification | None:
    """Approve / reject ke baad (orders router). Status se event."""
    if order.status in ("approved", "exported"):
        return notify_customer_order(db, tenant, order, "confirmed")
    if order.status == "rejected":
        return notify_customer_order(db, tenant, order, "rejected", reason=reason)
    return None


# ---------------- staff ----------------
@router.get("/portal-notifications")
def list_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(PortalNotification).where(PortalNotification.tenant_id == user.tenant_id,
                                                       PortalNotification.kind == "manual")    # order wale yahan nahi
                      .order_by(PortalNotification.created_at.desc(), PortalNotification.id.desc()).limit(100))
    names = {}
    out = []
    for n in rows:
        ids = n.customer_ids or []
        missing = [i for i in ids if i not in names]
        if missing:
            names.update(dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(missing))).all()))
        reads = db.query(PortalNotificationRead).filter(PortalNotificationRead.notification_id == n.id).count()
        out.append({**notification_out(n), "audience": n.audience,
                    "customers": [names.get(i, f"#{i}") for i in ids], "send_push": n.send_push,
                    "send_email": n.send_email, "push_sent": n.push_sent, "push_failed": n.push_failed,
                    "email_sent": n.email_sent, "recipients": len(_recipients(db, user.tenant_id, n)), "read_by": reads})
    return out


@router.post("/portal-notifications")
def create_notification(data: NotificationIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    title = (data.title or "").strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if data.audience not in ("all", "customers"):
        raise HTTPException(400, "Invalid audience")
    if data.audience == "customers" and not data.customer_ids:
        raise HTTPException(400, "Choose at least one customer")
    link = (data.link_url or "").strip()
    if link and not (link.startswith("https://") or link.startswith("http://") or link.startswith("/")):
        raise HTTPException(400, "Link must start with https:// or /")
    n = PortalNotification(tenant_id=user.tenant_id, title=title[:150], body=(data.body or "").strip() or None,
                           link_url=link[:500] or None, audience=data.audience,
                           customer_ids=data.customer_ids if data.audience == "customers" else None,
                           send_push=data.send_push, send_email=data.send_email, created_by_id=user.id)
    db.add(n)
    db.flush()
    if not _recipients(db, user.tenant_id, n):
        raise HTTPException(400, "None of these customers has an active portal login yet")
    deliver(db, user.tenant, n)
    log_activity(db, user.tenant_id, user.id, "portal.notification_sent", None, None,
                 {"name": n.title, "push": n.push_sent, "email": n.email_sent})
    db.commit()
    return {**notification_out(n), "push_sent": n.push_sent, "push_failed": n.push_failed, "email_sent": n.email_sent,
            "recipients": len(_recipients(db, user.tenant_id, n))}


@router.delete("/portal-notifications/{notification_id}")
def delete_notification(notification_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    n = db.get(PortalNotification, notification_id)
    if not n or n.tenant_id != user.tenant_id:
        raise HTTPException(404, "Not found")
    db.query(PortalNotificationRead).filter(PortalNotificationRead.notification_id == n.id).delete()
    db.delete(n)
    db.commit()
    return {"ok": True}


# ---------------- customer portal ----------------
@router.get("/portal/{slug}/notifications")
def my_notifications(ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    login = ctx.login
    rows = db.scalars(select(PortalNotification).where(
        PortalNotification.tenant_id == ctx.tenant.id, PortalNotification.created_at >= login.created_at)
        .order_by(PortalNotification.created_at.desc(), PortalNotification.id.desc()).limit(200))
    mine = [n for n in rows if _visible(n, login)][:50]
    read = set(db.scalars(select(PortalNotificationRead.notification_id).where(PortalNotificationRead.login_id == login.id)))
    items = [notification_out(n, n.id in read) for n in mine]
    return {"items": items, "unread": sum(1 for i in items if not i["read"])}


class ReadIn(BaseModel):
    ids: list[int] | None = None


@router.post("/portal/{slug}/notifications/read")
def mark_read(data: ReadIn, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    if ctx.impersonator_id:      # staff "login as" se dekh raha hai - customer ka unread na badle
        return {"ok": True}
    current = my_notifications(ctx, db)["items"]
    ids = {i["id"] for i in current if not i["read"]}
    if data.ids is not None:
        ids &= set(data.ids)
    for nid in ids:
        db.add(PortalNotificationRead(notification_id=nid, login_id=ctx.login.id))
    db.commit()
    return {"ok": True, "marked": len(ids)}


class SubscribeIn(BaseModel):
    endpoint: str
    keys: dict


@router.post("/portal/{slug}/push/subscribe")
def push_subscribe(data: SubscribeIn, request: Request, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    if not push_enabled():
        raise HTTPException(400, "App notifications aren't set up yet")
    if not data.endpoint.startswith("https://") or not data.keys.get("p256dh") or not data.keys.get("auth"):
        raise HTTPException(400, "Invalid subscription")
    sub = db.scalars(select(PortalPushSubscription).where(PortalPushSubscription.tenant_id == ctx.tenant.id,
                                                          PortalPushSubscription.endpoint == data.endpoint)).first()
    if not sub:
        sub = PortalPushSubscription(tenant_id=ctx.tenant.id, endpoint=data.endpoint[:700])
        db.add(sub)
    sub.login_id = ctx.login.id          # ek device = ek login (dusre ne login kiya to usko milega)
    sub.p256dh, sub.auth = data.keys["p256dh"][:200], data.keys["auth"][:100]
    sub.user_agent = (request.headers.get("user-agent") or "")[:200]
    db.commit()
    return {"ok": True}


class UnsubscribeIn(BaseModel):
    endpoint: str


@router.post("/portal/{slug}/push/unsubscribe")
def push_unsubscribe(data: UnsubscribeIn, ctx: Ctx = Depends(login_ctx), db: Session = Depends(get_db)):
    db.query(PortalPushSubscription).filter(PortalPushSubscription.tenant_id == ctx.tenant.id,
                                            PortalPushSubscription.endpoint == data.endpoint).delete()
    db.commit()
    return {"ok": True}
