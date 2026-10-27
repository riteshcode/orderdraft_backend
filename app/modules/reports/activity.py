"""
Activity log - "kisne, kab, kya kiya / kya badla". Customer khud dekhe (Activity log page),
super admin / support kisi bhi client ka dekhe (Admin -> Tenant -> Activity).

  GET /activity/search?entity=product&entity_id=12&user_id=3&q=price&date_from=2026-10-01&date_to=2026-10-24&page=1
  GET /activity?limit=12                       (dashboard "Recent activity" - purana, waisa hi)
  GET /admin/tenants/{id}/activity?...          (super admin, same filters)

Row:  {"action": "product.updated", "entity": "product", "entity_id": 12, "user_name": "Ritesh",
       "detail": {"name": "Roma 10kg", "changes": {"price": [32.5, 34.0]}, "impersonated_by": 1?}, "created_at": ...}
"""
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db, tenant_session
from app.core.deps import current_user, require_role, require_superadmin
from app.models import ActivityLog, Tenant, User

router = APIRouter(tags=["activity"])


def _user_names(tenant_id: int) -> dict[int, str]:
    """Users main DB mein hain (tenant DB alag) - isliye apna session. Super admin ka naam bhi (impersonation)."""
    with SessionLocal() as s:
        rows = s.scalars(select(User).where(or_(User.tenant_id == tenant_id, User.is_superadmin.is_(True))))
        return {u.id: u.name for u in rows}


def search_activity(db: Session, tenant_id: int, *, entity: str | None = None, entity_id: int | None = None,
                    user_id: int | None = None, q: str = "", date_from: date | None = None,
                    date_to: date | None = None, page: int = 1, page_size: int = 50) -> dict:
    conds = [ActivityLog.tenant_id == tenant_id]
    if entity:
        conds.append(ActivityLog.entity == entity)
    if entity_id is not None:
        conds.append(ActivityLog.entity_id == entity_id)
    if user_id is not None:
        conds.append(ActivityLog.user_id == (None if user_id == 0 else user_id))   # 0 = System
    if q.strip():
        conds.append(ActivityLog.action.like(f"%{q.strip().lower()}%"))
    if date_from:
        conds.append(ActivityLog.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(ActivityLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min))

    total = db.scalar(select(func.count()).select_from(ActivityLog).where(*conds))
    rows = db.scalars(select(ActivityLog).where(*conds).order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    names = _user_names(tenant_id)
    items = []
    for r in rows:
        detail = r.detail or {}
        imp = detail.get("impersonated_by") if isinstance(detail, dict) else None
        items.append({
            "id": r.id, "action": r.action, "entity": r.entity, "entity_id": r.entity_id, "detail": r.detail,
            "user_id": r.user_id, "user_name": names.get(r.user_id, "Deleted user") if r.user_id else "System",
            "impersonated_by_name": names.get(imp) if imp else None, "created_at": r.created_at,
        })
    users = [{"id": uid, "name": n} for uid, n in sorted(names.items(), key=lambda x: x[1].lower())]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "users": users}


@router.get("/activity/search")
def activity_search(entity: str | None = None, entity_id: int | None = None, user_id: int | None = None, q: str = "",
                    date_from: date | None = None, date_to: date | None = None, page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=200),
                    user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    return search_activity(db, user.tenant_id, entity=entity, entity_id=entity_id, user_id=user_id, q=q,
                           date_from=date_from, date_to=date_to, page=page, page_size=page_size)


@router.get("/activity/entity/{entity}/{entity_id}")
def entity_history(entity: str, entity_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Ek record ki history (customer / supplier / product / PO page pe "History" panel). Sab roles dekh sakte hain."""
    return search_activity(db, user.tenant_id, entity=entity, entity_id=entity_id, page_size=100)


@router.get("/admin/tenants/{tenant_id}/activity")
def admin_tenant_activity(tenant_id: int, entity: str | None = None, entity_id: int | None = None,
                          user_id: int | None = None, q: str = "", date_from: date | None = None,
                          date_to: date | None = None, page: int = Query(1, ge=1),
                          page_size: int = Query(50, ge=1, le=200),
                          admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    if not t.db_ready:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "users": []}
    with tenant_session(t.id) as tdb:
        result = search_activity(tdb, t.id, entity=entity, entity_id=entity_id, user_id=user_id, q=q,
                                 date_from=date_from, date_to=date_to, page=page, page_size=page_size)
        tdb.commit()
    return result
