"""
Team: users list, invite (email link), role change, disable, cancel invite.

Roles:
  owner    - sab kuch + billing + team
  admin    - sab kuch except billing; team mein owners ko nahi chhed sakta
  reviewer - orders review/approve, inbox, catalog dekhna
"""
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, utcnow
from app.core.deps import ROLES, require_role
from app.core.helpers import log_activity
from app.core.mailer import send_email, simple_html
from app.models import Invite, Tenant, User
from app.modules.billing.limits import get_plan

router = APIRouter(prefix="/team", tags=["team"])
manager = require_role("owner", "admin")


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "reviewer"


class MemberUpdate(BaseModel):
    role: str
    is_active: bool = True


@router.get("")
def team(user: User = Depends(manager), db: Session = Depends(get_db)):
    members = db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.created_at))
    invites = db.scalars(select(Invite).where(Invite.tenant_id == user.tenant_id, Invite.accepted_at.is_(None),
                                              Invite.expires_at > utcnow()))
    plan = get_plan(db, db.get(Tenant, user.tenant_id))
    return {
        "members": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "is_active": u.is_active,
                     "last_login_at": u.last_login_at, "is_me": u.id == user.id} for u in members],
        "invites": [{"id": i.id, "email": i.email, "role": i.role, "expires_at": i.expires_at} for i in invites],
        "user_limit": plan.user_limit if plan else 1,
    }


@router.post("/invite")
def invite(data: InviteIn, user: User = Depends(manager), db: Session = Depends(get_db)):
    email = data.email.lower()
    if data.role not in ROLES:
        raise HTTPException(400, "Invalid role")
    if data.role == "owner" and user.role != "owner":
        raise HTTPException(403, "Only an owner can invite a new owner")
    if db.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(400, "An account with this email already exists")

    tenant = db.get(Tenant, user.tenant_id)
    plan = get_plan(db, tenant)
    active = db.scalar(select(func.count()).select_from(User).where(User.tenant_id == tenant.id, User.is_active.is_(True)))
    pending = db.scalar(select(func.count()).select_from(Invite).where(
        Invite.tenant_id == tenant.id, Invite.accepted_at.is_(None), Invite.expires_at > utcnow()))
    if plan and active + pending >= plan.user_limit:
        raise HTTPException(400, f"Your plan allows {plan.user_limit} users - upgrade to add more")

    inv = Invite(tenant_id=tenant.id, email=email, role=data.role, token=secrets.token_urlsafe(32),
                 invited_by_id=user.id, expires_at=utcnow() + timedelta(days=7))
    db.add(inv)
    log_activity(db, tenant.id, user.id, "team.invited", detail={"email": email, "role": data.role})
    db.commit()

    link = f"{settings.FRONTEND_URL}/accept-invite?token={inv.token}"
    send_email(email, f"{user.name} invited you to {tenant.name} on {settings.APP_NAME}",
               simple_html(f"Join {tenant.name}", f"<p>{user.name} has invited you to review orders on "
                           f"{settings.APP_NAME} as <b>{data.role}</b>.</p>", "Accept invite", link),
               text=f"Accept invite: {link}")
    return {"ok": True, "invite_link": link}


@router.delete("/invites/{invite_id}")
def cancel_invite(invite_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    inv = db.get(Invite, invite_id)
    if not inv or inv.tenant_id != user.tenant_id:
        raise HTTPException(404, "Invite not found")
    db.delete(inv)
    db.commit()
    return {"ok": True}


@router.put("/members/{member_id}")
def update_member(member_id: int, data: MemberUpdate, user: User = Depends(manager), db: Session = Depends(get_db)):
    member = db.get(User, member_id)
    if not member or member.tenant_id != user.tenant_id:
        raise HTTPException(404, "User not found")
    if data.role not in ROLES:
        raise HTTPException(400, "Invalid role")
    if user.role != "owner" and (member.role == "owner" or data.role == "owner"):
        raise HTTPException(403, "Only an owner can change another owner")
    if member.id == user.id and (data.role != user.role or not data.is_active):
        raise HTTPException(400, "You can't change your own role or status")

    # Kam se kam ek active owner hamesha rahe
    if member.role == "owner" and (data.role != "owner" or not data.is_active):
        owners = db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == user.tenant_id, User.role == "owner", User.is_active.is_(True)))
        if owners <= 1:
            raise HTTPException(400, "There must be at least one owner")

    member.role, member.is_active = data.role, data.is_active
    log_activity(db, user.tenant_id, user.id, "team.updated", detail={"email": member.email, "role": data.role,
                                                                      "active": data.is_active})
    db.commit()
    return {"ok": True}
