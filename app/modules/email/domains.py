"""
Client ka apna sender domain (Postmark Account API).

Flow:
  1. Client "orders@freshfarms.com.au" daalta hai -> Postmark mein domain banate hain
  2. Client apne DNS mein 2 record daalta hai:
       TXT   <selector>._domainkey.freshfarms.com.au   (DKIM - email sign hota hai)
       CNAME pm-bounces.freshfarms.com.au -> pm.mtasv.net  (Return-Path - bounce + SPF alignment)
  3. "Verify" -> Postmark DNS check karta hai -> dono OK = verified -> emails client ke naam se

SPF alag se add nahi karna padta: Return-Path CNAME Postmark ke SPF pe point karta hai (DMARC align hota hai).
Verify hone tak emails "Fresh Farms via OrderDraft" <platform address> se jaate hain (Reply-To = client).

Ek domain sirf ek client ka (main DB integration_links, provider="email_domain") -
warna client B, client A ke verified domain se email bhej sakta tha.

POSTMARK_ACCOUNT_TOKEN khali = dev mode (fake records, verify kabhi pass nahi hota).
"""
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import utcnow
from app.models import IntegrationLink, TenantSettings

log = logging.getLogger("email.domains")
API = "https://api.postmarkapp.com"
LINK_PROVIDER = "email_domain"
RETURN_PATH_PREFIX = "pm-bounces"
RETURN_PATH_TARGET = "pm.mtasv.net"

FREE_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "live.com.au", "msn.com",
    "yahoo.com", "yahoo.com.au", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "zoho.com", "bigpond.com", "bigpond.net.au", "optusnet.com.au",
    "tpg.com.au", "iinet.net.au", "rediffmail.com", "mail.com", "yandex.com",
}


class DomainError(Exception):
    pass


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


# ---------------- Postmark HTTP ----------------
def _call(method: str, path: str, json: dict | None = None) -> dict:
    try:
        resp = httpx.request(method, API + path, json=json, timeout=30, headers={
            "X-Postmark-Account-Token": settings.POSTMARK_ACCOUNT_TOKEN, "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise DomainError(f"Couldn't connect to Postmark: {e}")
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("Message") or resp.text
        except ValueError:
            msg = resp.text
        raise DomainError(f"Postmark: {msg[:200]}")
    return resp.json() if resp.content else {}


def _find_existing(name: str) -> dict | None:
    data = _call("GET", "/domains?count=500&offset=0")
    for d in data.get("Domains") or []:
        if (d.get("Name") or "").lower() == name:
            return _call("GET", f"/domains/{d['ID']}")
    return None


def _records(d: dict) -> list[dict]:
    """Postmark domain response -> DNS records table (client ko dikhane ke liye)."""
    dkim_host = d.get("DKIMHost") or d.get("DKIMPendingHost") or ""
    dkim_value = d.get("DKIMTextValue") or d.get("DKIMPendingTextValue") or ""
    return [
        {"type": "TXT", "purpose": "DKIM", "host": dkim_host, "value": dkim_value, "verified": bool(d.get("DKIMVerified"))},
        {"type": "CNAME", "purpose": "Return-Path", "host": d.get("ReturnPathDomain") or "",
         "value": d.get("ReturnPathDomainCNAMEValue") or RETURN_PATH_TARGET,
         "verified": bool(d.get("ReturnPathDomainVerified"))},
    ]


def _apply(cfg: TenantSettings, d: dict):
    cfg.sender_provider_id = str(d.get("ID") or cfg.sender_provider_id or "")
    cfg.sender_dns = _records(d)
    ok = bool(d.get("DKIMVerified")) and bool(d.get("ReturnPathDomainVerified"))
    cfg.sender_status = "verified" if ok else "pending"
    cfg.sender_checked_at = utcnow()


def _dev_domain(domain: str) -> dict:
    """POSTMARK_ACCOUNT_TOKEN nahi hai - records ka shape dikhane ke liye placeholder."""
    return {"ID": "dev", "Name": domain, "DKIMPendingHost": f"20260101000000pm._domainkey.{domain}",
            "DKIMPendingTextValue": "k=rsa;p=MIGf... (example - set POSTMARK_ACCOUNT_TOKEN to get the real value)",
            "ReturnPathDomain": f"{RETURN_PATH_PREFIX}.{domain}", "ReturnPathDomainCNAMEValue": RETURN_PATH_TARGET,
            "DKIMVerified": False, "ReturnPathDomainVerified": False}


# ---------------- domain ownership (main DB) ----------------
def _claim(main_db: Session, tenant_id: int, domain: str):
    link = main_db.scalars(select(IntegrationLink).where(IntegrationLink.provider == LINK_PROVIDER,
                                                         IntegrationLink.external_id == domain)).first()
    if link and link.tenant_id != tenant_id:
        raise DomainError("This domain is already linked to another account. Please contact support.")
    if not link:
        main_db.add(IntegrationLink(provider=LINK_PROVIDER, external_id=domain, tenant_id=tenant_id))
    main_db.flush()


def _release(main_db: Session, tenant_id: int, domain: str | None):
    if not domain:
        return
    for link in main_db.scalars(select(IntegrationLink).where(IntegrationLink.provider == LINK_PROVIDER,
                                                              IntegrationLink.tenant_id == tenant_id,
                                                              IntegrationLink.external_id == domain)):
        main_db.delete(link)


# ---------------- public functions ----------------
def setup_sender(db: Session, cfg: TenantSettings, name: str | None, email: str):
    """Sender save + Postmark domain create/reuse. db = tenant session (main DB bhi isi session se)."""
    email = email.strip().lower()
    if "@" not in email or "." not in domain_of(email) or " " in email:
        raise DomainError("Enter a valid email address")
    domain = domain_of(email)
    if domain in FREE_MAIL:
        raise DomainError(f"You can't send from a free email address like {domain} - use an email on your business domain "
                          "(e.g. orders@yourbusiness.com.au)")

    _claim(db, cfg.tenant_id, domain)
    cfg.sender_name = (name or "").strip()[:100] or None
    if cfg.sender_domain == domain and cfg.sender_provider_id:   # sirf naam / address badla
        cfg.sender_email = email
        return

    _release(db, cfg.tenant_id, cfg.sender_domain)
    cfg.sender_email, cfg.sender_domain = email, domain
    cfg.sender_status, cfg.sender_dns, cfg.sender_provider_id = "pending", None, None

    if not settings.POSTMARK_ACCOUNT_TOKEN:
        _apply(cfg, _dev_domain(domain))
        return
    try:
        d = _call("POST", "/domains", {"Name": domain, "ReturnPathDomain": f"{RETURN_PATH_PREFIX}.{domain}"})
    except DomainError as e:
        if "already" not in str(e).lower():
            raise
        d = _find_existing(domain)     # hamare Postmark account mein pehle se (isi client ne pehle add kiya tha)
        if not d:
            raise
    _apply(cfg, d)


def verify_sender(cfg: TenantSettings):
    if not cfg.sender_domain:
        raise DomainError("Save the sender email first")
    if not settings.POSTMARK_ACCOUNT_TOKEN or cfg.sender_provider_id in (None, "", "dev"):
        cfg.sender_checked_at = utcnow()
        raise DomainError("Email domain verification isn't set up yet (POSTMARK_ACCOUNT_TOKEN)")
    pid = cfg.sender_provider_id
    try:
        _call("PUT", f"/domains/{pid}/verifyDkim")
        _call("PUT", f"/domains/{pid}/verifyReturnPath")
    except DomainError as e:
        log.info("Verify call for %s: %s", cfg.sender_domain, e)   # DNS abhi nahi mila - status neeche se
    _apply(cfg, _call("GET", f"/domains/{pid}"))
    return [r["purpose"] for r in cfg.sender_dns or [] if not r["verified"]]   # abhi tak kya missing hai


def remove_sender(db: Session, cfg: TenantSettings):
    """Platform sender pe wapas. Postmark domain delete nahi karte (dobara add karne pe DKIM badal jaata)."""
    _release(db, cfg.tenant_id, cfg.sender_domain)
    cfg.sender_name = cfg.sender_email = cfg.sender_domain = None
    cfg.sender_provider_id, cfg.sender_dns, cfg.sender_checked_at = None, None, None
    cfg.sender_status = "none"


def recheck_pending(db: Session, cfg: TenantSettings) -> bool:
    """Scheduler: pending domain apne aap verify ho jaaye jab client DNS daal de."""
    if cfg.sender_status != "pending" or not settings.POSTMARK_ACCOUNT_TOKEN or cfg.sender_provider_id in (None, "dev"):
        return False
    try:
        verify_sender(cfg)
    except DomainError:
        return False
    return cfg.sender_status == "verified"
