"""
Passwords (bcrypt) aur tokens (JWT).

Token types (claim "typ"):
  access  - login token, har API call mein:  Authorization: Bearer <token>
  reset   - password reset link (1 ghanta)
  state   - Xero OAuth state (15 min)
"""
from datetime import timedelta

import bcrypt
import jwt

from app.core.config import settings
from app.core.database import utcnow


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def make_token(data: dict, typ: str, expires: timedelta) -> str:
    payload = {**data, "typ": typ, "exp": utcnow() + expires}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def read_token(token: str, typ: str) -> dict:
    """Invalid / expired / galat type = jwt.PyJWTError raise."""
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    if payload.get("typ") != typ:
        raise jwt.InvalidTokenError("Wrong token type")
    return payload


def access_token(user_id: int, impersonator_id: int | None = None) -> str:
    data = {"sub": str(user_id)}
    if impersonator_id:
        data["imp"] = impersonator_id
    hours = 2 if impersonator_id else settings.TOKEN_EXPIRE_HOURS
    return make_token(data, "access", timedelta(hours=hours))


def reset_token(user_id: int, password_hash: str) -> str:
    # Password badalte hi purana link bekaar (pwh match nahi karega)
    return make_token({"sub": str(user_id), "pwh": password_hash[-12:]}, "reset", timedelta(hours=1))
