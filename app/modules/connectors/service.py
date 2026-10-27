"""
Connect / choose organisation / disconnect - har accounting software ke liye same flow.

  1. connect_url()   -> client apne software mein login karta hai (OAuth)
  2. handle_callback -> tokens client ke DB mein, organisations list
  3. activate_org()  -> ek org/company file chuna (1 hi ho to apne aap)
  4. disconnect()

Data conflict se bachav:
  - Ek client = ek accounting software (doosra connect karne se pehle pehla disconnect)
  - Ek Xero org / QuickBooks company / MYOB file sirf EK client se link ho sakti hai
    (main DB: integration_links) - do clients ka data kabhi ek books mein mix na ho
"""
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt
from app.core.database import utcnow
from app.core.helpers import log_activity
from app.core.security import make_token
from app.models import Connection, IntegrationLink, Tenant
from app.modules.connectors.api import ApiSession, get_connection, save_tokens
from app.modules.connectors.base import ConnectorError, Org
from app.modules.connectors.registry import get_provider


def active_other(db: Session, tenant_id: int, provider_key: str) -> Connection | None:
    return db.scalars(select(Connection).where(Connection.tenant_id == tenant_id, Connection.status == "connected",
                                               Connection.provider != provider_key)).first()


def connect_url(db: Session, tenant_id: int, user_id: int, provider_key: str) -> str:
    provider = get_provider(provider_key)
    if not provider.configured():
        raise ConnectorError(f"{provider.label} isn't available on the platform yet (client ID/secret not configured)")
    other = active_other(db, tenant_id, provider_key)
    if other:
        raise ConnectorError(f"Your {get_provider(other.provider).label} is already connected. "
                             "Only one accounting software can be connected per account - please disconnect it first.")
    state = make_token({"tenant_id": tenant_id, "user_id": user_id, "provider": provider_key}, "state",
                       timedelta(minutes=15))
    return provider.authorize_url(state)


def handle_callback(db: Session, tenant_id: int, user_id: int | None, provider_key: str,
                    code: str, params: dict) -> Connection:
    """OAuth code -> tokens. Return connection (status connected ya pending_org)."""
    provider = get_provider(provider_key)
    if active_other(db, tenant_id, provider_key):
        raise ConnectorError("Another accounting software is already connected")

    tokens = provider.exchange_code(code, params)

    # Is client ki purani (pending / isi software ki) connections hata do - ek hi rakhenge
    db.execute(delete(Connection).where(Connection.tenant_id == tenant_id))
    conn = Connection(tenant_id=tenant_id, provider=provider_key, status="pending_org", extra={})
    db.add(conn)
    save_tokens(conn, tokens)
    db.flush()

    api = ApiSession(db, conn, provider)
    orgs = provider.list_orgs(api, {**params, "_tokens": tokens.raw})
    if not orgs:
        raise ConnectorError(f"No organisations found in your {provider.label} account")
    conn.extra = {"orgs": [{"id": o.id, "name": o.name, "extra": o.extra} for o in orgs]}
    db.commit()

    # Ek hi org aur login ki zaroorat nahi -> seedha activate
    if len(orgs) == 1 and not (provider.org_login_fields and orgs[0].extra.get("requires_login")):
        activate_org(db, tenant_id, user_id, orgs[0].id, None)
    return conn


def pending_orgs(db: Session, tenant_id: int) -> dict:
    conn = get_connection(db, tenant_id, include_pending=True)
    if not conn or conn.status != "pending_org":
        raise ConnectorError("There's no pending connection")
    provider = get_provider(conn.provider)
    return {"provider": conn.provider, "label": provider.label, "login_fields": provider.org_login_fields,
            "orgs": [{"id": o["id"], "name": o["name"], "requires_login": bool(o.get("extra", {}).get("requires_login"))}
                     for o in (conn.extra or {}).get("orgs", [])]}


def activate_org(db: Session, tenant_id: int, user_id: int | None, org_id: str, credentials: dict | None) -> Connection:
    conn = get_connection(db, tenant_id, include_pending=True)
    if not conn or not conn.refresh_token:
        raise ConnectorError("Connect your accounting software first")
    provider = get_provider(conn.provider)
    org_data = next((o for o in (conn.extra or {}).get("orgs", []) if o["id"] == org_id), None)
    if not org_data:
        raise ConnectorError("This organisation isn't in the list")

    # Ye books kisi aur OrderDraft client se to connected nahi?
    link = db.scalars(select(IntegrationLink).where(IntegrationLink.provider == provider.key,
                                                    IntegrationLink.external_id == org_id)).first()
    if link and link.tenant_id != tenant_id:
        raise ConnectorError(f"'{org_data['name']}' is already connected to another OrderDraft account. "
                             "An organisation can only be linked to one account.")

    org = Org(id=org_data["id"], name=org_data["name"], extra=org_data.get("extra") or {})
    conn.external_tenant_id = org.id
    conn.external_name = org.name
    api = ApiSession(db, conn, provider)
    extra_updates = provider.activate_org(api, org, credentials) or {}
    if credentials:
        extra_updates["credentials"] = encrypt(f"{credentials.get('username', '')}\n{credentials.get('password', '')}")

    conn.extra = {**{k: v for k, v in (conn.extra or {}).items() if k != "orgs"}, **extra_updates}
    conn.status = "connected"
    conn.connected_at = utcnow()

    # Main DB: webhook routing + "ye org kiska hai"
    db.execute(delete(IntegrationLink).where(IntegrationLink.tenant_id == tenant_id))
    db.add(IntegrationLink(provider=provider.key, external_id=org.id, tenant_id=tenant_id))
    tenant = db.get(Tenant, tenant_id)
    tenant.accounting_provider = provider.key
    log_activity(db, tenant_id, user_id, "accounting.connected", detail={"provider": provider.key, "org": org.name})
    db.commit()
    return conn


def org_credentials(conn: Connection) -> tuple[str, str] | None:
    """MYOB company file login (encrypted in connection.extra)."""
    raw = (conn.extra or {}).get("credentials")
    if not raw:
        return None
    user, _, password = decrypt(raw).partition("\n")
    return user, password


def disconnect(db: Session, tenant_id: int, user_id: int | None) -> None:
    conn = get_connection(db, tenant_id, include_pending=True)
    if conn:
        provider = get_provider(conn.provider)
        if conn.refresh_token:
            try:
                provider.revoke(decrypt(conn.refresh_token))
            except Exception:   # noqa: BLE001 - disconnect hamesha ho jaana chahiye
                pass
        db.execute(delete(Connection).where(Connection.tenant_id == tenant_id))
        log_activity(db, tenant_id, user_id, "accounting.disconnected", detail={"provider": conn.provider})
    db.execute(delete(IntegrationLink).where(IntegrationLink.tenant_id == tenant_id))
    tenant = db.get(Tenant, tenant_id)
    tenant.accounting_provider = None
    db.commit()


def tenant_for_org(db: Session, provider_key: str, org_id: str) -> int | None:
    """Webhook: software ka org id -> hamara client."""
    link = db.scalars(select(IntegrationLink).where(IntegrationLink.provider == provider_key,
                                                    IntegrationLink.external_id == org_id)).first()
    return link.tenant_id if link else None
