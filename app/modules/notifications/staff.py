"""
Staff notifications - client ke staff ko app ke andar bell + browser/desktop push.

Kab aati hai (notify_staff ko yahan se call):
  - customer ne portal se order kiya            (portal/shop.py place_order)
  - standing order se order apne aap bana       (standing/service.py)
  - standing order ka order nahi ban paya       (sab products inactive etc)
  - email / voice / chat se naya AI order aaya  (parsing/pipeline.py)
Email pehle jaisa alag jaata hai (Settings -> Customer portal -> "Email new orders to").

API (staff login):
  GET  /staff-notifications                    {items: [..50], unread, push_public_key, push_subscribed}
  POST /staff-notifications/read               {ids: [..]} ya {} = sab padh liye
  POST /staff-push/subscribe                   {endpoint, keys: {p256dh, auth}}
  POST /staff-push/unsubscribe                 {endpoint}
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db, utcnow
from app.core.deps import current_user
from app.core.request_log import log_event
from app.core.webpush import push_enabled, send_push, vapid_public_key
from app.models import StaffNotification, StaffNotificationRead, StaffPushSubscription, User

router = APIRouter(tags=["staff-notifications"])


def notify_staff(db: Session, tenant_id: int, kind: str, title: str, body: str | None = None,
                 link_url: str | None = None, order_id: int | None = None) -> StaffNotification | None:
    """
    Bell mein entry + saare staff browsers pe push. Commit caller karega.
    Kuch bhi fail ho to asli kaam (order) nahi rukna chahiye - isliye yahan error sirf log.
    """
    try:
        n = StaffNotification(tenant_id=tenant_id, kind=kind, title=title[:150], body=body, link_url=link_url,
                              order_id=order_id)
        db.add(n)
        db.flush()
        if push_enabled():
            for sub in list(db.scalars(select(StaffPushSubscription).where(StaffPushSubscription.tenant_id == tenant_id))):
                result = send_push({"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth},
                                   {"title": n.title, "body": body or "", "url": link_url or "/orders", "tag": f"s{n.id}"})
                if result == "ok":
                    n.push_sent += 1
                    sub.last_sent_at = utcnow()
                elif result == "gone":          # browser ne notifications band kar di
                    db.delete(sub)
        return n
    except Exception as err:
        log_event("staff_notify_failed", tenant_id=tenant_id, kind=kind, error=str(err)[:200])
        return None


def notification_out(n: StaffNotification, read: bool) -> dict:
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link_url": n.link_url,
            "order_id": n.order_id, "created_at": n.created_at, "read": read}


def _mine(db: Session, user: User) -> list[StaffNotification]:
    # Naye staff ko purani notifications nahi (unread count 200 na dikhe)
    return list(db.scalars(select(StaffNotification).where(
        StaffNotification.tenant_id == user.tenant_id, StaffNotification.created_at >= user.created_at)
        .order_by(StaffNotification.created_at.desc(), StaffNotification.id.desc()).limit(50)))


@router.get("/staff-notifications")
def my_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = _mine(db, user)
    read = set(db.scalars(select(StaffNotificationRead.notification_id).where(
        StaffNotificationRead.user_id == user.id, StaffNotificationRead.notification_id.in_([n.id for n in rows] or [0]))))
    items = [notification_out(n, n.id in read) for n in rows]
    subscribed = db.scalar(select(StaffPushSubscription.id).where(StaffPushSubscription.user_id == user.id).limit(1))
    return {"items": items, "unread": sum(1 for i in items if not i["read"]),
            "push_public_key": vapid_public_key(), "push_subscribed": bool(subscribed)}


class ReadIn(BaseModel):
    ids: list[int] | None = None


@router.post("/staff-notifications/read")
def mark_read(data: ReadIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if getattr(request.state, "impersonator_id", None):   # super admin "login as" - client ka unread na badle
        return {"ok": True}
    rows = _mine(db, user)
    done = set(db.scalars(select(StaffNotificationRead.notification_id).where(StaffNotificationRead.user_id == user.id)))
    ids = {n.id for n in rows} - done
    if data.ids is not None:
        ids &= set(data.ids)
    for nid in ids:
        db.add(StaffNotificationRead(notification_id=nid, user_id=user.id))
    db.commit()
    return {"ok": True, "marked": len(ids)}


class SubscribeIn(BaseModel):
    endpoint: str
    keys: dict


class UnsubscribeIn(BaseModel):
    endpoint: str


@router.post("/staff-push/subscribe")
def push_subscribe(data: SubscribeIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from fastapi import HTTPException
    p256dh, auth = (data.keys or {}).get("p256dh"), (data.keys or {}).get("auth")
    if not data.endpoint.startswith("https://") or not p256dh or not auth:
        raise HTTPException(400, "Invalid push subscription")
    sub = db.scalars(select(StaffPushSubscription).where(StaffPushSubscription.endpoint == data.endpoint)).first()
    if not sub:
        sub = StaffPushSubscription(tenant_id=user.tenant_id, user_id=user.id, endpoint=data.endpoint[:700])
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user.id, p256dh[:200], auth[:100]
    sub.user_agent = (request.headers.get("user-agent") or "")[:200] or None
    db.commit()
    return {"ok": True}


@router.post("/staff-push/unsubscribe")
def push_unsubscribe(data: UnsubscribeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(StaffPushSubscription).filter(StaffPushSubscription.endpoint == data.endpoint,
                                           StaffPushSubscription.user_id == user.id).delete()
    db.commit()
    return {"ok": True}
