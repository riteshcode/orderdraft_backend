"""
Ek connected accounting software se baat karna - har provider ke liye same:

    api = open_api(db, tenant_id)          # client ki active connection
    data = api.get("/Items")               # token refresh, 401 retry, 429 wait - sab yahin
    api.post("/Invoices", json={...})

Tests mein `ApiSession.send` ko mock karte hain (koi real HTTP nahi).
"""
import time
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt
from app.core.database import utcnow
from app.models import Connection
from app.modules.connectors.base import AccountingProvider, ConnectorError, TokenSet


def get_connection(db: Session, tenant_id: int, include_pending: bool = False) -> Connection | None:
    """Client ki accounting connection (ek hi hoti hai)."""
    stmt = select(Connection).where(Connection.tenant_id == tenant_id)
    if not include_pending:
        stmt = stmt.where(Connection.status == "connected")
    return db.scalars(stmt.order_by(Connection.id.desc())).first()


def save_tokens(conn: Connection, tokens: TokenSet):
    conn.access_token = encrypt(tokens.access_token)
    if tokens.refresh_token:
        conn.refresh_token = encrypt(tokens.refresh_token)
    conn.expires_at = utcnow() + timedelta(seconds=max(int(tokens.expires_in or 1800) - 60, 60))


class ApiSession:
    def __init__(self, db: Session, conn: Connection, provider: AccountingProvider):
        self.db, self.conn, self.provider = db, conn, provider

    # ---- token ----
    def token(self, force_refresh: bool = False) -> str:
        conn = self.conn
        if not force_refresh and conn.access_token and conn.expires_at and conn.expires_at > utcnow():
            return decrypt(conn.access_token)
        if not conn.refresh_token:
            raise ConnectorError(f"{self.provider.label} isn't connected", reconnect=True)
        try:
            tokens = self.provider.refresh(decrypt(conn.refresh_token))
        except ConnectorError:
            conn.last_sync_status = "Reconnect required - authorization expired"
            self.db.commit()
            raise
        save_tokens(conn, tokens)
        self.db.commit()
        return tokens.access_token

    # ---- HTTP ----
    def send(self, method: str, url: str, headers: dict, params=None, json=None, content=None) -> httpx.Response:
        """Asli network call (tests isse mock karte hain)."""
        return httpx.request(method, url, headers=headers, params=params, json=json, content=content, timeout=60)

    def request(self, method: str, path: str, params=None, json=None, headers: dict | None = None,
                raw: bool = False, absolute: bool = False):
        """
        path = provider ke api_base ke baad ka hissa ("/Invoices"), ya absolute=True pe poora URL.
        Return: JSON dict (204 / 304 pe {}), raw=True pe httpx.Response.
        """
        url = path if absolute else self.provider.api_base(self.conn) + path
        refreshed = False
        for attempt in range(3):
            all_headers = {**self.provider.auth_headers(self.conn, self.token()), **(headers or {})}
            try:
                resp = self.send(method, url, all_headers, params=params, json=json)
            except httpx.HTTPError as e:
                raise ConnectorError(f"{self.provider.label} request failed: {e}", retryable=True)

            if resp.status_code == 401 and not refreshed:
                # Token beech mein revoke/expire - ek baar refresh karke dobara
                refreshed = True
                self.conn.expires_at = utcnow() - timedelta(seconds=1)
                continue
            if resp.status_code == 429 and attempt < 2:
                time.sleep(min(int(resp.headers.get("Retry-After", "5") or 5), 30))
                continue
            if raw:
                return resp
            if resp.status_code in (204, 304):
                return {}
            if resp.status_code == 401:
                raise ConnectorError(f"{self.provider.label} connection expired - please reconnect in Settings → Integrations", reconnect=True)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise ConnectorError(f"{self.provider.label} error {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                raise ConnectorError(self.provider.parse_error(resp))
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                raise ConnectorError(f"{self.provider.label}: unexpected response")
        raise ConnectorError(f"{self.provider.label} rate limit", retryable=True)

    # ---- hamare codes -> software ke ids (client ke chart of accounts / tax rates se) ----
    def account_ref(self, code: str | None) -> str | None:
        """Account code ("4-1100" / "200") -> software ka id (MYOB UID / QBO Id)."""
        if not code:
            return None
        from app.models import AccountCode
        row = self.db.scalars(select(AccountCode).where(AccountCode.tenant_id == self.conn.tenant_id,
                                                        AccountCode.code == code)).first()
        return row.external_id if row and row.source == self.provider.key else None

    def tax_ref(self, code: str | None) -> str | None:
        """Tax code ("GST" / "OUTPUT") -> software ka id."""
        if not code:
            return None
        from app.models import TaxRate
        row = self.db.scalars(select(TaxRate).where(TaxRate.tenant_id == self.conn.tenant_id,
                                                    TaxRate.code == code)).first()
        return row.external_id if row and row.source == self.provider.key else None

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw):
        return self.request("PUT", path, **kw)


def open_api(db: Session, tenant_id: int) -> ApiSession:
    from app.modules.connectors.registry import get_provider

    conn = get_connection(db, tenant_id)
    if not conn:
        raise ConnectorError("No accounting software is connected - connect one in Settings → Integrations")
    return ApiSession(db, conn, get_provider(conn.provider))
