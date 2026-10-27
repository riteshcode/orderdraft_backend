"""
FastAPI dependencies - login user, role check, super admin.

Use:
    user: User = Depends(current_user)                  # koi bhi logged-in
                                                        # (+ db session is user ke client DB se bind)
    user: User = Depends(require_role("owner", "admin")) # sirf ye roles
    admin: User = Depends(require_superadmin)
"""
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import bind_tenant, get_db
from app.core.security import read_token
from app.models import User

bearer = HTTPBearer(auto_error=False)

ROLES = ("owner", "admin", "reviewer")


def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(401, "Login required")
    try:
        payload = read_token(creds.credentials, "access")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")

    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or disabled")

    # Super admin "login as" kar raha hai to usse request mein yaad rakho
    request.state.impersonator_id = payload.get("imp")

    if user.tenant.status == "suspended" and not payload.get("imp") and not user.is_superadmin:
        raise HTTPException(403, "Account suspended. Please contact support.")

    # Is request ka session ab sirf isi client ke DB ko dekhega (dusre client ka data possible hi nahi)
    if not user.tenant.db_ready:
        raise HTTPException(503, "Your account is still being set up - please try again in a few minutes")
    bind_tenant(db, user.tenant_id)
    # Audit trail: is request mein jo bhi badlega, uska "kisne" yahi user (core/audit.py)
    db.info["actor_id"] = user.id
    db.info["impersonator_id"] = payload.get("imp")
    request.state.user_id, request.state.tenant_id = user.id, user.tenant_id
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Your role doesn't have permission to do this")
        return user
    return checker


def require_superadmin(request: Request, user: User = Depends(current_user)) -> User:
    if not user.is_superadmin or getattr(request.state, "impersonator_id", None):
        raise HTTPException(403, "Super admin only")
    return user
