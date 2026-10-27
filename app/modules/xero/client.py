"""
Xero API client - OAuth2 + authenticated requests.

Flow:
  1. authorize_url()   -> user Xero login pe jaata hai
  2. handle_callback() -> code se tokens, /connections se org id
  3. request()         -> har API call (token apne aap refresh, 429 pe wait)

Access token ~30 min chalta hai, refresh_token se naya milta hai (refresh token bhi rotate hota hai).
Docs: https://developer.xero.com/documentation/guides/oauth2/auth-flow
"""
import base64
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.core.database import utcnow
from app.models import Connection

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"


class XeroError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def authorize_url(state: str) -> str:
    if not settings.XERO_CLIENT_ID:
        raise XeroError("XERO_CLIENT_ID set nahi hai (.env)")
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": settings.XERO_CLIENT_ID,
        "redirect_uri": settings.XERO_REDIRECT_URI,
        "scope": settings.XERO_SCOPES,
        "state": state,
    })


def _basic_auth() -> str:
    raw = f"{settings.XERO_CLIENT_ID}:{settings.XERO_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _save_tokens(conn: Connection, data: dict):
    conn.access_token = encrypt(data["access_token"])
    if data.get("refresh_token"):
        conn.refresh_token = encrypt(data["refresh_token"])
    conn.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 1800)) - 60)


def get_connection(db: Session, tenant_id: int) -> Connection | None:
    return db.scalars(select(Connection).where(Connection.tenant_id == tenant_id, Connection.provider == "xero")).first()


def handle_callback(db: Session, tenant_id: int, code: str) -> Connection:
    resp = httpx.post(
        TOKEN_URL, headers={"Authorization": _basic_auth()},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.XERO_REDIRECT_URI},
        timeout=30,
    )
    if resp.status_code != 200:
        raise XeroError(f"Token exchange failed: {resp.text[:300]}")

    conn = get_connection(db, tenant_id)
    if not conn:
        conn = Connection(tenant_id=tenant_id, provider="xero")
        db.add(conn)
    _save_tokens(conn, resp.json())

    orgs = httpx.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {decrypt(conn.access_token)}"}, timeout=30)
    if orgs.status_code == 200 and orgs.json():
        # Note: user ne ek se zyada org select kiye to pehla le rahe hain
        first = orgs.json()[0]
        conn.external_tenant_id = first.get("tenantId")
        conn.external_name = first.get("tenantName")
    conn.connected_at = utcnow()
    db.commit()
    return conn


def access_token(db: Session, conn: Connection) -> str:
    if conn.expires_at and conn.expires_at > utcnow() and conn.access_token:
        return decrypt(conn.access_token)

    resp = httpx.post(
        TOKEN_URL, headers={"Authorization": _basic_auth()},
        data={"grant_type": "refresh_token", "refresh_token": decrypt(conn.refresh_token)},
        timeout=30,
    )
    if resp.status_code != 200:
        raise XeroError("Xero connection expire ho gaya - Integrations page se dobara connect karo")
    _save_tokens(conn, resp.json())
    db.commit()
    return decrypt(conn.access_token)


def request(db: Session, conn: Connection, method: str, path: str, params=None, json=None,
            headers: dict | None = None) -> dict:
    """
    Xero API call. path = "/Invoices" (API_BASE ke baad).
    429 (rate limit) pe Retry-After tak ruk ke ek baar dobara try.
    """
    if not conn or not conn.refresh_token:
        raise XeroError("Xero connected nahi hai")

    for attempt in range(2):
        all_headers = {
            "Authorization": f"Bearer {access_token(db, conn)}",
            "xero-tenant-id": conn.external_tenant_id or "",
            "Accept": "application/json",
            **(headers or {}),
        }
        try:
            resp = httpx.request(method, API_BASE + path, params=params, json=json, headers=all_headers, timeout=60)
        except httpx.HTTPError as e:
            raise XeroError(f"Xero request failed: {e}", retryable=True)

        if resp.status_code == 429 and attempt == 0:
            wait = min(int(resp.headers.get("Retry-After", "5")), 60)
            time.sleep(wait)
            continue
        if resp.status_code == 304:      # If-Modified-Since: kuch nahi badla
            return {}
        if resp.status_code >= 500 or resp.status_code == 429:
            raise XeroError(f"Xero error {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise XeroError(_error_message(resp))
        return resp.json()
    raise XeroError("Xero rate limit", retryable=True)


def _error_message(resp: httpx.Response) -> str:
    """Xero validation errors ko readable banao."""
    try:
        data = resp.json()
    except ValueError:
        return f"Xero error {resp.status_code}: {resp.text[:300]}"
    messages = []
    for el in data.get("Elements", []) or []:
        for err in el.get("ValidationErrors", []) or []:
            messages.append(err.get("Message"))
    if messages:
        return "Xero: " + "; ".join(m for m in messages if m)
    return f"Xero error {resp.status_code}: {data.get('Message') or data.get('Detail') or str(data)[:300]}"
