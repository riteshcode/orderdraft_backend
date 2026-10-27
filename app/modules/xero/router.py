"""
Xero: connect / callback / disconnect / sync / settings / accounts + webhook (invoice status).
"""
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_role
from app.core.helpers import log_activity
from app.core.jobs import enqueue
from app.core.security import make_token, read_token
from app.models import Connection, Invoice, User
from app.modules.xero import client, sync

router = APIRouter(tags=["xero"])
admin_only = require_role("owner", "admin")


class XeroSettingsIn(BaseModel):
    auto_sync: bool = True
    include_all_contacts: bool = False


def status_out(conn: Connection | None) -> dict:
    return {
        "connected": bool(conn and conn.refresh_token),
        "org_name": conn.external_name if conn else None,
        "connected_at": conn.connected_at if conn else None,
        "last_sync_at": conn.last_sync_at if conn else None,
        "last_sync_status": conn.last_sync_status if conn else None,
        "auto_sync": conn.auto_sync if conn else True,
        "include_all_contacts": conn.include_all_contacts if conn else False,
        "configured": bool(settings.XERO_CLIENT_ID),
    }


@router.get("/xero/status")
def xero_status(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return status_out(client.get_connection(db, user.tenant_id))


@router.get("/xero/connect-url")
def connect_url(user: User = Depends(admin_only)):
    state = make_token({"tenant_id": user.tenant_id, "user_id": user.id}, "state", timedelta(minutes=15))
    try:
        return {"url": client.authorize_url(state)}
    except client.XeroError as e:
        raise HTTPException(400, str(e))


@router.get("/xero/callback")
def callback(code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    target = f"{settings.FRONTEND_URL}/integrations"
    if error or not code:
        return RedirectResponse(f"{target}?xero=error")
    try:
        payload = read_token(state, "state")
        client.handle_callback(db, payload["tenant_id"], code)
        log_activity(db, payload["tenant_id"], payload.get("user_id"), "xero.connected")
        db.commit()
    except (jwt.PyJWTError, client.XeroError):
        return RedirectResponse(f"{target}?xero=error")
    # Connect hote hi pehla full sync
    enqueue(sync.sync_job, payload["tenant_id"], True, payload.get("user_id"))
    return RedirectResponse(f"{target}?xero=connected")


@router.post("/xero/disconnect")
def disconnect(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    conn = client.get_connection(db, user.tenant_id)
    if conn:
        db.delete(conn)
        log_activity(db, user.tenant_id, user.id, "xero.disconnected")
        db.commit()
    return {"ok": True}


@router.post("/xero/sync")
def start_sync(full: bool = True, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not client.get_connection(db, user.tenant_id):
        raise HTTPException(400, "Pehle Xero connect karo")
    enqueue(sync.sync_job, user.tenant_id, full, user.id)
    return {"ok": True, "message": "Sync started"}


@router.put("/xero/settings")
def save_settings(data: XeroSettingsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    conn = client.get_connection(db, user.tenant_id)
    if not conn:
        raise HTTPException(400, "Xero connected nahi hai")
    conn.auto_sync = data.auto_sync
    conn.include_all_contacts = data.include_all_contacts
    db.commit()
    return status_out(conn)


@router.get("/xero/accounts")
def accounts(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Destination setup dropdown: revenue accounts + tax rates."""
    conn = client.get_connection(db, user.tenant_id)
    try:
        acc = client.request(db, conn, "GET", "/Accounts", params={"where": 'Class=="REVENUE"'})
        tax = client.request(db, conn, "GET", "/TaxRates")
    except client.XeroError as e:
        raise HTTPException(400, str(e))
    return {
        "accounts": [{"code": a.get("Code"), "name": a.get("Name")} for a in acc.get("Accounts", []) if a.get("Code")],
        "tax_rates": [{"type": t.get("TaxType"), "name": t.get("Name")} for t in tax.get("TaxRates", [])
                      if t.get("Status") == "ACTIVE"],
    }


# --------------------------------------------------------------------------
# Xero webhook - Invoice create/update events
# Xero developer portal -> Webhooks -> URL: https://api.yourapp.com/webhooks/xero
# Pehle "Intent to receive" check aata hai: sahi signature = 200, galat = 401
# --------------------------------------------------------------------------
@router.post("/webhooks/xero")
async def xero_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("x-xero-signature", "")
    expected = base64.b64encode(
        hmac.new(settings.XERO_WEBHOOK_KEY.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not settings.XERO_WEBHOOK_KEY or not hmac.compare_digest(signature, expected):
        return Response(status_code=401)

    try:
        events = json.loads(body or b"{}").get("events", [])
    except ValueError:
        events = []

    for ev in events:
        if ev.get("eventCategory") != "INVOICE":
            continue
        invoice = db.scalars(select(Invoice).where(Invoice.source == "xero",
                                                   Invoice.external_id == ev.get("resourceId"))).first()
        if invoice:
            enqueue(sync.refresh_invoice_job, invoice.id)
    return Response(status_code=200)
