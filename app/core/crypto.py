"""
Secrets (Xero tokens, client API keys) DB mein encrypted rakhte hain.
Fernet = AES + HMAC, cryptography library se.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

PREFIX = "enc:"
MASK = "••••••••"   # UI ko secret ki jagah yahi dikhate hain


def _fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key:
        # SECRET_KEY se 32-byte key bana lo
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return None
