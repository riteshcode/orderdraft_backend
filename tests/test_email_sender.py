"""Client ka sender domain (Postmark) + order confirmation email on approve."""
import pytest
from sqlalchemy import select

from app.core import mailer
from app.core.config import settings
from app.models import IntegrationLink
from app.modules.email import domains
from tests.conftest import register
from tests.test_destinations import approve

DOMAIN = {"ID": 777, "Name": "freshfarms.com.au", "DKIMPendingHost": "2026pm._domainkey.freshfarms.com.au",
          "DKIMPendingTextValue": "k=rsa;p=ABC", "ReturnPathDomain": "pm-bounces.freshfarms.com.au",
          "ReturnPathDomainCNAMEValue": "pm.mtasv.net", "DKIMVerified": False, "ReturnPathDomainVerified": False}


@pytest.fixture
def postmark(monkeypatch):
    """Fake Postmark Account API (no network)."""
    state = {"domain": dict(DOMAIN), "calls": []}
    monkeypatch.setattr(settings, "POSTMARK_ACCOUNT_TOKEN", "acct-token")

    def call(method, path, json=None):
        state["calls"].append((method, path, json))
        if method == "POST" and path == "/domains":
            if state.get("exists"):
                raise domains.DomainError("Postmark: This domain already exists.")
            return dict(state["domain"])
        if path == "/domains?count=500&offset=0":
            return {"Domains": [{"ID": 777, "Name": "freshfarms.com.au"}]}
        if path.startswith("/domains/777/verify"):
            return {}
        if path == "/domains/777":
            return dict(state["domain"])
        raise AssertionError(path)

    monkeypatch.setattr(domains, "_call", call)
    return state


def save_sender(client, h, email="orders@freshfarms.com.au", name="Fresh Farms Orders"):
    return client.put("/settings/email/sender", headers=h, json={"name": name, "email": email})


# ---------------- sender domain ----------------
def test_default_is_platform_sender_with_reply_to(client, auth):
    client.put("/settings/business", headers=auth, json={"name": "Fresh Farms Pty Ltd", "email": "hello@freshfarms.com.au"})
    e = client.get("/settings/email", headers=auth).json()
    assert e["sender"]["status"] == "none"
    assert e["effective_from"] == '"Fresh Farms Pty Ltd via OrderDraft" <no-reply@example.com>'
    assert e["reply_to"] == "hello@freshfarms.com.au"
    assert e["order_email"]["enabled"] is True


def test_free_mail_rejected(client, auth):
    r = save_sender(client, auth, "freshfarms@gmail.com")
    assert r.status_code == 400 and "business domain" in r.json()["detail"]
    assert save_sender(client, auth, "not-an-email").status_code == 400


def test_dev_mode_shows_records_but_cannot_verify(client, auth):
    e = save_sender(client, auth).json()
    assert e["sender"]["status"] == "pending" and e["domain_api_configured"] is False
    assert [r["type"] for r in e["sender"]["dns"]] == ["TXT", "CNAME"]
    assert "via OrderDraft" in e["effective_from"]                   # pending -> abhi bhi platform se
    assert client.post("/settings/email/sender/verify", headers=auth).status_code == 400


def test_postmark_domain_verify_flow(client, auth, postmark):
    e = save_sender(client, auth).json()
    assert postmark["calls"][0] == ("POST", "/domains", {"Name": "freshfarms.com.au",
                                                          "ReturnPathDomain": "pm-bounces.freshfarms.com.au"})
    dkim, rp = e["sender"]["dns"]
    assert dkim == {"type": "TXT", "purpose": "DKIM", "host": "2026pm._domainkey.freshfarms.com.au",
                    "value": "k=rsa;p=ABC", "verified": False}
    assert rp["host"] == "pm-bounces.freshfarms.com.au" and rp["value"] == "pm.mtasv.net"

    # DNS abhi nahi dala
    e = client.post("/settings/email/sender/verify", headers=auth).json()
    assert e["sender"]["status"] == "pending" and e["missing"] == ["DKIM", "Return-Path"]

    # client ne DNS daal diya
    postmark["domain"].update(DKIMVerified=True, ReturnPathDomainVerified=True, DKIMHost=DOMAIN["DKIMPendingHost"],
                              DKIMTextValue="k=rsa;p=ABC")
    e = client.post("/settings/email/sender/verify", headers=auth).json()
    assert e["sender"]["status"] == "verified" and e["missing"] == []
    assert e["effective_from"] == '"Fresh Farms Orders" <orders@freshfarms.com.au>'

    # Sirf naam badla -> verified rehta hai, Postmark call dobara nahi
    n = len(postmark["calls"])
    e = save_sender(client, auth, name="Fresh Farms").json()
    assert e["sender"]["status"] == "verified" and len(postmark["calls"]) == n

    # Remove -> platform sender
    e = client.delete("/settings/email/sender", headers=auth).json()
    assert e["sender"]["status"] == "none" and "via OrderDraft" in e["effective_from"]


def test_existing_postmark_domain_reused(client, auth, postmark):
    postmark["exists"] = True
    e = save_sender(client, auth).json()
    assert e["sender"]["status"] == "pending" and e["sender"]["dns"][0]["value"] == "k=rsa;p=ABC"


def test_domain_belongs_to_one_client_only(client, auth, postmark, db):
    save_sender(client, auth)
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    r = save_sender(client, other, "sales@freshfarms.com.au")
    assert r.status_code == 400 and "another account" in r.json()["detail"]
    assert client.get("/settings/email", headers=other).json()["sender"]["status"] == "none"
    links = db.scalars(select(IntegrationLink).where(IntegrationLink.provider == "email_domain")).all()
    assert [(l.external_id, l.tenant_id) for l in links] == [("freshfarms.com.au", 1)]
    # A ne domain hataya -> ab B le sakta hai
    client.delete("/settings/email/sender", headers=auth)
    assert save_sender(client, other, "sales@freshfarms.com.au").status_code == 200


def test_test_email_uses_effective_sender(client, auth):
    r = client.post("/settings/email/test", headers=auth, json={"to": "me@freshfarms.com.au"}).json()
    assert r["ok"] and mailer.SENT[-1]["to"] == "me@freshfarms.com.au" and "via OrderDraft" in mailer.SENT[-1]["from"]


# ---------------- order confirmation ----------------
def test_approve_sends_confirmation(client, seeded, draft_order, db):
    client.put("/settings/email/order", headers=seeded, json={
        "enabled": True, "subject": "Order {order_number} confirmed - {business_name}",
        "message": "Thanks {customer_name}!", "cc_other": True, "bcc": "copy@freshfarms.com.au"})
    o = approve(client, seeded, draft_order)
    mail = mailer.SENT[-1]
    assert mail["to"] == "orders@joescafe.com.au"
    assert mail["subject"] == f"Order {o['po_number'] or o['id']} confirmed - Fresh Farms Pty Ltd"
    assert mail["bcc"] == ["copy@freshfarms.com.au"]
    assert "Roma" in mail["html"] and "$80.60" in mail["html"] and "Thanks Joe" in mail["html"]
    assert "via OrderDraft" in mail["from"]
    o = client.get(f"/orders/{o['id']}", headers=seeded).json()
    assert o["confirmation_sent_at"] and o["confirmation_to"].startswith("orders@joescafe.com.au")


def test_verified_domain_used_for_confirmation(client, seeded, draft_order, postmark):
    save_sender(client, seeded)
    postmark["domain"].update(DKIMVerified=True, ReturnPathDomainVerified=True)
    client.post("/settings/email/sender/verify", headers=seeded)
    approve(client, seeded, draft_order)
    assert mailer.SENT[-1]["from"] == '"Fresh Farms Orders" <orders@freshfarms.com.au>'


def test_no_confirmation_when_disabled(client, seeded, draft_order):
    client.put("/settings/email/order", headers=seeded, json={"enabled": False})
    approve(client, seeded, draft_order)
    assert not any(m["subject"].startswith("Order confirmation") for m in mailer.SENT)


def test_resend_and_no_double_send(client, seeded, draft_order):
    o = approve(client, seeded, draft_order)
    assert sum(m["subject"].startswith("Order confirmation") for m in mailer.SENT) == 1
    assert client.post(f"/orders/{o['id']}/approve", headers=seeded).status_code == 400   # dobara approve nahi
    r = client.post(f"/orders/{o['id']}/send-confirmation", headers=seeded, json={"to": "boss@joescafe.com.au"})
    assert r.status_code == 200 and r.json()["confirmation_to"] == "boss@joescafe.com.au"
    assert mailer.SENT[-1]["to"] == "boss@joescafe.com.au"
    assert client.post(f"/orders/{o['id']}/send-confirmation", headers=seeded, json={"to": "bad"}).status_code == 400
    assert client.post(f"/orders/{draft_order['id'] + 999}/send-confirmation", headers=seeded).status_code == 404


def test_customer_without_email_is_skipped(client, seeded, draft_order):
    cid = draft_order["customer_id"]
    client.put(f"/customers/{cid}", headers=seeded, json={"name": "Joe's Cafe", "email": None})
    o = approve(client, seeded, draft_order)                  # approve fail nahi hota
    assert o["status"] in ("approved", "exported")
    assert not any(m["subject"].startswith("Order confirmation") for m in mailer.SENT)
    r = client.post(f"/orders/{o['id']}/send-confirmation", headers=seeded)
    assert r.status_code == 400 and "email" in r.json()["detail"]


def test_bcc_validation(client, auth):
    assert client.put("/settings/email/order", headers=auth, json={"bcc": "nope"}).status_code == 400
