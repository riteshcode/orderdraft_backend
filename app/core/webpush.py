"""
Web Push (phone / desktop notification) - bina extra library ke, sirf `cryptography` + `pyjwt`.

Setup (ek baar):
    python -m app.cli vapid-keys          -> .env mein VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY daalo
    VAPID_SUBJECT=mailto:support@yourdomain.com

Browser (customer portal) subscribe karta hai -> endpoint + keys (p256dh, auth) humare DB mein.
Bhejna:
    send_push(subscription_dict, {"title": "...", "body": "...", "url": "/portal/x/orders/5"})
    -> "ok" | "gone" (subscription khatam - DB se hatao) | "error: ..."

Standards: RFC 8291 (message encryption, aes128gcm) + RFC 8292 (VAPID). Har browser (Chrome, Edge,
Firefox, Safari 16.4+ / iOS home screen app) yahi samajhta hai.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------- VAPID keys ----------------
def generate_vapid_keys() -> tuple[str, str]:
    """(private, public) - dono base64url. Public = browser ka applicationServerKey."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return b64url(private), b64url(_public_bytes(key.public_key()))


def _public_bytes(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _vapid_private_key():
    raw = settings.VAPID_PRIVATE_KEY
    if not raw:
        return None
    return ec.derive_private_key(int.from_bytes(b64url_decode(raw), "big"), ec.SECP256R1())


def vapid_public_key() -> str | None:
    """Browser ko dena hai (portal info). Keys set nahi = push band (in-app notifications phir bhi chalti hain)."""
    key = _vapid_private_key()
    return b64url(_public_bytes(key.public_key())) if key else None


def push_enabled() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY)


def _vapid_header(endpoint: str) -> str:
    key = _vapid_private_key()
    origin = "{0.scheme}://{0.netloc}".format(urlparse(endpoint))
    token = jwt.encode({"aud": origin, "exp": int(time.time()) + 12 * 3600, "sub": settings.VAPID_SUBJECT},
                       key, algorithm="ES256")
    return f"vapid t={token}, k={vapid_public_key()}"


# ---------------- encryption (RFC 8291) ----------------
def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Browser ki public key se message lock karo - sirf wahi browser khol sakta hai."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())     # har message ki nayi key
    as_public = _public_bytes(server_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = server_key.exchange(ec.ECDH(), ua_key)

    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)            # \x02 = last record
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + body


# ---------------- send ----------------
def send_push(sub: dict, message: dict, ttl: int = 24 * 3600) -> str:
    """sub = {"endpoint", "p256dh", "auth"}. Return: ok | gone | error: ..."""
    if not push_enabled():
        return "error: push not configured"
    try:
        data = encrypt(json.dumps(message).encode(), sub["p256dh"], sub["auth"])
        r = httpx.post(sub["endpoint"], content=data, timeout=15, headers={
            "Authorization": _vapid_header(sub["endpoint"]), "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "normal"})
    except Exception as e:   # noqa: BLE001 - network / bad key, ek subscription ki wajah se baaki na rukein
        return f"error: {e}"[:200]
    if r.status_code in (404, 410):
        return "gone"
    if r.status_code >= 400:
        return f"error: {r.status_code} {r.text[:120]}"
    return "ok"
