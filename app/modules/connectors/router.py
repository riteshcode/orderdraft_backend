"""
Integrations API (Xero / QuickBooks / MYOB - sab ek jaise):

  GET  /integrations                      providers list + is client ki connection
  GET  /integrations/{provider}/connect-url
  GET  /integrations/{provider}/callback  OAuth redirect (login ke baad software yahan bhejta hai)
  GET  /integrations/orgs                 organisation / company file chunne ke liye
  POST /integrations/org                  {org_id, username?, password?}
  POST /integrations/disconnect
  POST /integrations/sync?full=true
  PUT  /integrations/settings             {auto_sync, include_all_contacts}
  GET  /integrations/options              destination form: doc types, accounts, tax rates

Webhooks (software -> hum):  POST /webhooks/xero,  POST /webhooks/quickbooks
"""
import base64
import hashlib
import hmac
import json
import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, tenant_session
from app.core.deps import require_role
from app.core.jobs import enqueue
from app.core.security import read_token
from app.models import AccountCode, Connection, Invoice, TaxRate, User
from app.modules.connectors import service, sync
from app.modules.connectors.api import get_connection
from app.modules.connectors.base import ConnectorError
from app.modules.connectors.registry import PROVIDERS, get_provider

log = logging.getLogger("connectors")
router = APIRouter(tags=["integrations"])
admin_only = require_role("owner", "admin")


class OrgIn(BaseModel):
    org_id: str
    username: str | None = None
    password: str | None = None


class SettingsIn(BaseModel):
    auto_sync: bool = True
    include_all_contacts: bool = False


def connection_out(conn: Connection | None) -> dict | None:
    if not conn:
        return None
    return {
        "provider": conn.provider, "label": get_provider(conn.provider).label, "status": conn.status,
        "org_name": conn.external_name, "connected_at": conn.connected_at, "last_sync_at": conn.last_sync_at,
        "last_sync_status": conn.last_sync_status, "auto_sync": conn.auto_sync,
        "include_all_contacts": conn.include_all_contacts,
        "org_currency": (conn.extra or {}).get("base_currency"),
        "org_country": (conn.extra or {}).get("country"),
    }


def _http_error(e: ConnectorError) -> HTTPException:
    return HTTPException(400, str(e))


@router.get("/integrations")
def integrations(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return {
        "providers": [{"key": p.key, "label": p.label, "configured": p.configured(), "doc_types": p.doc_types,
                       "org_login_fields": p.org_login_fields} for p in PROVIDERS.values()],
        "connection": connection_out(get_connection(db, user.tenant_id, include_pending=True)),
    }


@router.get("/integrations/options")
def options(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Destination form ke dropdowns - client ke synced accounts / tax rates se (live call nahi)."""
    conn = get_connection(db, user.tenant_id)
    accounts = db.scalars(select(AccountCode).where(AccountCode.tenant_id == user.tenant_id,
                                                    AccountCode.is_active.is_(True)).order_by(AccountCode.code))
    taxes = db.scalars(select(TaxRate).where(TaxRate.tenant_id == user.tenant_id, TaxRate.is_active.is_(True),
                                             TaxRate.applies_to != "purchases").order_by(TaxRate.code))
    return {
        "provider": conn.provider if conn else None,
        "label": get_provider(conn.provider).label if conn else None,
        "doc_types": get_provider(conn.provider).doc_types if conn else {},
        "accounts": [{"code": a.code, "name": a.name, "account_class": a.account_class} for a in accounts],
        "tax_rates": [{"code": t.code, "name": t.name, "rate": t.rate} for t in taxes],
    }


@router.get("/integrations/{provider}/connect-url")
def connect_url(provider: str, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    try:
        return {"url": service.connect_url(db, user.tenant_id, user.id, provider)}
    except ConnectorError as e:
        raise _http_error(e)


@router.get("/integrations/{provider}/callback")
def callback(provider: str, request: Request, code: str = "", state: str = "", error: str = ""):
    """Software login ke baad yahan aata hai (no login header - state token se client pata chalta hai)."""
    target = f"{settings.FRONTEND_URL}/integrations"
    if error or not code:
        return RedirectResponse(f"{target}?error={provider}")
    try:
        payload = read_token(state, "state")
    except jwt.PyJWTError:
        return RedirectResponse(f"{target}?error=state")
    if payload.get("provider") != provider:
        return RedirectResponse(f"{target}?error=state")

    tenant_id, user_id = payload["tenant_id"], payload.get("user_id")
    db = tenant_session(tenant_id)
    try:
        conn = service.handle_callback(db, tenant_id, user_id, provider, code, dict(request.query_params))
        if conn.status == "pending_org":
            return RedirectResponse(f"{target}?choose_org={provider}")
    except ConnectorError as e:
        log.warning("Connect %s failed for tenant %s: %s", provider, tenant_id, e)
        db.rollback()
        return RedirectResponse(f"{target}?error={provider}&message={str(e)[:150]}")
    finally:
        db.close()
    enqueue(sync.sync_job, tenant_id, True, user_id)   # connect hote hi pehla full sync
    return RedirectResponse(f"{target}?connected={provider}")


@router.get("/integrations/orgs")
def orgs(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    try:
        return service.pending_orgs(db, user.tenant_id)
    except ConnectorError as e:
        raise _http_error(e)


@router.post("/integrations/org")
def choose_org(data: OrgIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    creds = {"username": data.username or "", "password": data.password or ""} if data.username else None
    try:
        conn = service.activate_org(db, user.tenant_id, user.id, data.org_id, creds)
    except ConnectorError as e:
        db.rollback()
        raise _http_error(e)
    enqueue(sync.sync_job, user.tenant_id, True, user.id)
    return connection_out(conn)


@router.post("/integrations/disconnect")
def disconnect(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    service.disconnect(db, user.tenant_id, user.id)
    return {"ok": True}


@router.post("/integrations/sync")
def start_sync(full: bool = True, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not get_connection(db, user.tenant_id):
        raise HTTPException(400, "Connect your accounting software first")
    enqueue(sync.sync_job, user.tenant_id, full, user.id)
    return {"ok": True, "message": "Sync started"}


@router.put("/integrations/settings")
def save_settings(data: SettingsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    conn = get_connection(db, user.tenant_id)
    if not conn:
        raise HTTPException(400, "No accounting software is connected")
    conn.auto_sync, conn.include_all_contacts = data.auto_sync, data.include_all_contacts
    db.commit()
    return connection_out(conn)


# --------------------------------------------------------------------------
# Webhooks - software ka org id -> main DB integration_links -> client ka DB
# --------------------------------------------------------------------------
def _queue_invoice_refresh(provider_key: str, org_id: str, external_ids: list[str], db: Session):
    tenant_id = service.tenant_for_org(db, provider_key, org_id)
    if not tenant_id or not external_ids:
        return
    with tenant_session(tenant_id) as tdb:
        ids = list(tdb.scalars(select(Invoice.id).where(Invoice.source == provider_key,
                                                        Invoice.external_id.in_(external_ids))))
    for invoice_id in ids:
        enqueue(sync.refresh_invoice_job, tenant_id, invoice_id)


@router.post("/webhooks/xero")
async def xero_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Xero developer portal -> Webhooks -> https://api.yourapp.com/webhooks/xero (INVOICE events)
    "Intent to receive": sahi signature = 200, galat = 401.
    """
    body = await request.body()
    expected = base64.b64encode(hmac.new(settings.XERO_WEBHOOK_KEY.encode(), body, hashlib.sha256).digest()).decode()
    if not settings.XERO_WEBHOOK_KEY or not hmac.compare_digest(request.headers.get("x-xero-signature", ""), expected):
        return Response(status_code=401)
    try:
        events = json.loads(body or b"{}").get("events", [])
    except ValueError:
        events = []
    by_org: dict[str, list[str]] = {}
    for ev in events:
        if ev.get("eventCategory") == "INVOICE" and ev.get("tenantId"):
            by_org.setdefault(ev["tenantId"], []).append(ev.get("resourceId"))
    for org_id, ids in by_org.items():
        _queue_invoice_refresh("xero", org_id, ids, db)
    return Response(status_code=200)


@router.post("/webhooks/quickbooks")
async def quickbooks_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Intuit developer portal -> Webhooks -> https://api.yourapp.com/webhooks/quickbooks (Invoice entity)
    Header intuit-signature = base64(HMAC-SHA256(body, verifier token)).
    """
    body = await request.body()
    verifier = settings.QUICKBOOKS_WEBHOOK_VERIFIER
    expected = base64.b64encode(hmac.new(verifier.encode(), body, hashlib.sha256).digest()).decode()
    if not verifier or not hmac.compare_digest(request.headers.get("intuit-signature", ""), expected):
        return Response(status_code=401)
    try:
        notes = json.loads(body or b"{}").get("eventNotifications", [])
    except ValueError:
        notes = []
    for note in notes:
        ids = [e.get("id") for e in (note.get("dataChangeEvent") or {}).get("entities", [])
               if e.get("name") == "Invoice" and e.get("id")]
        if note.get("realmId"):
            _queue_invoice_refresh("quickbooks", str(note["realmId"]), ids, db)
    return Response(status_code=200)
