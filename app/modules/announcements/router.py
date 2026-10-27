"""
Announcements - super admin sab clients ko notice de (maintenance, naya feature, koi issue).

Super admin (Admin -> Notifications):
  GET    /admin/announcements
  POST   /admin/announcements          {title, body, level, display, audience, tenant_ids, plan_ids, roles,
                                        show_in_app, show_on_login, dismissible, link_url, link_text,
                                        starts_at, ends_at, is_active}
  PUT    /admin/announcements/{id}
  DELETE /admin/announcements/{id}

App (logged-in user):
  GET    /announcements                jo is user ko dikhne chahiye (client / plan / role / time window match,
                                        aur user ne band nahi kiya)
  POST   /announcements/{id}/dismiss   "X" - sirf dismissible wale
Login page (bina login):
  GET    /public/announcements         show_on_login = true (sabko dikhta hai)
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db, utcnow
from app.core.deps import current_user, require_superadmin
from app.models import Announcement, AnnouncementDismissal, User

router = APIRouter(tags=["announcements"])

LEVELS = ("info", "success", "warning", "critical")
DISPLAYS = ("banner", "popup")
AUDIENCES = ("all", "tenants", "plans")


class AnnouncementIn(BaseModel):
    title: str
    body: str | None = None
    level: str = "info"
    display: str = "banner"
    audience: str = "all"
    tenant_ids: list[int] = []
    plan_ids: list[int] = []
    roles: list[str] = []            # khali = sab roles
    show_in_app: bool = True
    show_on_login: bool = False
    dismissible: bool = True
    link_url: str | None = None
    link_text: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True


def out(a: Announcement, dismissed: int | None = None) -> dict:
    d = {"id": a.id, "title": a.title, "body": a.body, "level": a.level, "display": a.display,
         "audience": a.audience, "tenant_ids": a.tenant_ids or [], "plan_ids": a.plan_ids or [],
         "roles": a.roles or [], "show_in_app": a.show_in_app, "show_on_login": a.show_on_login,
         "dismissible": a.dismissible, "link_url": a.link_url, "link_text": a.link_text,
         "starts_at": a.starts_at, "ends_at": a.ends_at, "is_active": a.is_active,
         "created_at": a.created_at, "updated_at": a.updated_at}
    if dismissed is not None:
        d["dismissed_count"] = dismissed
    return d


def _apply(a: Announcement, data: AnnouncementIn):
    title = (data.title or "").strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if data.level not in LEVELS or data.display not in DISPLAYS or data.audience not in AUDIENCES:
        raise HTTPException(400, "Invalid level / display / audience")
    if data.audience == "tenants" and not data.tenant_ids:
        raise HTTPException(400, "Choose at least one client")
    if data.audience == "plans" and not data.plan_ids:
        raise HTTPException(400, "Choose at least one plan")
    if not data.show_in_app and not data.show_on_login:
        raise HTTPException(400, "Show it in the app, on the login page, or both")
    if data.starts_at and data.ends_at and data.ends_at <= data.starts_at:
        raise HTTPException(400, "End time must be after the start time")
    link = (data.link_url or "").strip()
    if link and not (link.startswith("https://") or link.startswith("http://") or link.startswith("/")):
        raise HTTPException(400, "Link must start with https:// or /")
    a.title, a.body = title[:200], (data.body or "").strip() or None
    a.level, a.display, a.audience = data.level, data.display, data.audience
    a.tenant_ids = data.tenant_ids if data.audience == "tenants" else None
    a.plan_ids = data.plan_ids if data.audience == "plans" else None
    a.roles = [r for r in data.roles if r in ("owner", "admin", "reviewer")] or None
    a.show_in_app, a.show_on_login, a.dismissible = data.show_in_app, data.show_on_login, data.dismissible
    a.link_url, a.link_text = link[:500] or None, (data.link_text or "").strip()[:100] or None
    a.starts_at, a.ends_at, a.is_active = data.starts_at, data.ends_at, data.is_active


def _live(now):
    """Abhi chalu: active + start ho chuka + khatam nahi hua."""
    return (Announcement.is_active.is_(True),
            or_(Announcement.starts_at.is_(None), Announcement.starts_at <= now),
            or_(Announcement.ends_at.is_(None), Announcement.ends_at > now))


def visible_to(a: Announcement, user: User) -> bool:
    if a.audience == "tenants" and user.tenant_id not in (a.tenant_ids or []):
        return False
    if a.audience == "plans" and user.tenant.plan_id not in (a.plan_ids or []):
        return False
    if a.roles and user.role not in a.roles:
        return False
    return True


# ---------------- super admin ----------------
@router.get("/admin/announcements")
def admin_list(admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(AnnouncementDismissal.announcement_id, func.count())
                             .group_by(AnnouncementDismissal.announcement_id)).all())
    rows = db.scalars(select(Announcement).order_by(Announcement.created_at.desc(), Announcement.id.desc()))
    return [out(a, counts.get(a.id, 0)) for a in rows]


@router.get("/admin/announcements/targets")
def admin_targets(admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    """Form ke liye: saare clients aur plans (naam ke saath)."""
    from app.models import Plan, Tenant
    tenants = db.scalars(select(Tenant).order_by(Tenant.name))
    plans = db.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))
    return {"tenants": [{"id": t.id, "name": t.name, "slug": t.slug, "plan_id": t.plan_id} for t in tenants],
            "plans": [{"id": p.id, "name": p.name} for p in plans]}


@router.post("/admin/announcements")
def admin_create(data: AnnouncementIn, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    a = Announcement(created_by_id=admin.id)
    _apply(a, data)
    db.add(a)
    db.commit()
    db.refresh(a)
    return out(a, 0)


def _get(db: Session, ann_id: int) -> Announcement:
    a = db.get(Announcement, ann_id)
    if not a:
        raise HTTPException(404, "Notification not found")
    return a


@router.put("/admin/announcements/{ann_id}")
def admin_update(ann_id: int, data: AnnouncementIn, admin: User = Depends(require_superadmin),
                 db: Session = Depends(get_db)):
    a = _get(db, ann_id)
    _apply(a, data)
    db.commit()
    db.refresh(a)
    return out(a)


@router.post("/admin/announcements/{ann_id}/reset-dismissals")
def admin_reset(ann_id: int, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    """Zaroori update - jinhone band kiya tha unhe bhi dobara dikhao."""
    _get(db, ann_id)
    n = db.query(AnnouncementDismissal).filter(AnnouncementDismissal.announcement_id == ann_id).delete()
    db.commit()
    return {"reset": n}


@router.delete("/admin/announcements/{ann_id}")
def admin_delete(ann_id: int, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    a = _get(db, ann_id)
    db.query(AnnouncementDismissal).filter(AnnouncementDismissal.announcement_id == a.id).delete()
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------------- app + login page ----------------
@router.get("/announcements")
def my_announcements(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = utcnow()
    dismissed = set(db.scalars(select(AnnouncementDismissal.announcement_id)
                               .where(AnnouncementDismissal.user_id == user.id)))
    rows = db.scalars(select(Announcement).where(Announcement.show_in_app.is_(True), *_live(now))
                      .order_by(Announcement.created_at.desc()))
    return [out(a) for a in rows if visible_to(a, user) and not (a.dismissible and a.id in dismissed)]


@router.post("/announcements/{ann_id}/dismiss")
def dismiss(ann_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _get(db, ann_id)
    if not a.dismissible:
        raise HTTPException(400, "This notice can't be closed")
    exists = db.scalars(select(AnnouncementDismissal).where(AnnouncementDismissal.announcement_id == a.id,
                                                            AnnouncementDismissal.user_id == user.id)).first()
    if not exists:
        db.add(AnnouncementDismissal(announcement_id=a.id, user_id=user.id))
        db.commit()
    return {"ok": True}


@router.get("/public/announcements")
def public_announcements(db: Session = Depends(get_db)):
    rows = db.scalars(select(Announcement).where(Announcement.show_on_login.is_(True), *_live(utcnow()))
                      .order_by(Announcement.created_at.desc()))
    # Login page pe client / role pata nahi - isliye sirf public fields
    return [{k: v for k, v in out(a).items() if k in ("id", "title", "body", "level", "display", "dismissible",
                                                     "link_url", "link_text")} for a in rows]
