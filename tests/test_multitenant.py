"""
Har client ka alag database + accounting connection rules (data conflict nahi hona chahiye).
"""
import os
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.core.database import TenantMismatch, tenant_session, url_for_tenant
from app.models import Connection, IntegrationLink, Product, Tenant
from app.modules.connectors.base import TokenSet
from app.modules.connectors.xero import CONNECTIONS_URL, XeroProvider
from tests.conftest import JOE_ORDER, PM_AUTH, postmark_payload, register, tenant_db, upload_and_run


def two_tenants(client):
    a = register(client, "a@fresh.com.au", "Fresh Farms Pty Ltd")
    b = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    return a, b


# ---------------- Database isolation ----------------
def test_each_client_gets_own_database(client, db):
    two_tenants(client)
    tenants = db.scalars(select(Tenant).order_by(Tenant.id)).all()
    assert [t.db_ready for t in tenants] == [True, True]
    urls = {url_for_tenant(t) for t in tenants}
    assert len(urls) == 2                                  # alag alag DB
    if all(u.startswith("sqlite") for u in urls):
        assert all(os.path.exists(u.replace("sqlite:///", "")) for u in urls)


def test_data_never_leaks_between_clients(client, mock_ai):
    a, b = two_tenants(client)
    upload_and_run(client, a, "products", "products.csv")
    upload_and_run(client, a, "customers", "customers.csv")
    assert client.get("/products", headers=a).json()["total"] > 0
    assert client.get("/products", headers=b).json()["total"] == 0

    mock_ai.order = JOE_ORDER
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 box romas", to="freshfarms@orders.test.com"))
    a_orders = client.get("/orders", headers=a).json()["items"]
    assert len(a_orders) == 1
    # Dono DB mein id 1 ho sakta hai - B ko kabhi A ka order nahi dikhna chahiye
    assert client.get("/orders", headers=b).json()["items"] == []
    assert client.get(f"/orders/{a_orders[0]['id']}", headers=b).status_code == 404

    # Settings bhi alag
    client.put("/settings/invoice", headers=a, json={"invoice_prefix": "FF-", "invoice_next_number": 10,
                                                      "gst_rate": 10, "payment_terms_days": 7})
    assert client.get("/settings", headers=b).json()["invoice"]["invoice_prefix"] == "INV-"


def test_inbound_email_goes_to_right_client_db(client, mock_ai, db):
    a, b = two_tenants(client)
    mock_ai.order = {"is_order": False, "items": []}
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("hi", to="oceanseafood@orders.test.com"))
    assert client.get("/inbound/messages", headers=b).json()["total"] == 1
    assert client.get("/inbound/messages", headers=a).json()["total"] == 0


def test_guard_blocks_cross_tenant_writes(client):
    two_tenants(client)
    s = tenant_session(1)
    s.add(Product(tenant_id=2, sku="X", name="Wrong client"))
    with pytest.raises(TenantMismatch):
        s.commit()
    s.rollback()
    with pytest.raises(TenantMismatch):
        from app.core.database import bind_tenant
        bind_tenant(s, 2)                                   # ek session = ek client
    s.close()


def test_admin_sees_all_clients_without_mixing(client, db):
    two_tenants(client)
    admin = register(client, "boss@platform.com", "Platform HQ")
    tenants = client.get("/admin/tenants", headers=admin).json()["items"]
    assert {t["name"] for t in tenants} >= {"Fresh Farms Pty Ltd", "Ocean Seafood Co"}
    detail = client.get(f"/admin/tenants/{tenants[-1]['id']}", headers=admin).json()
    assert detail["db_ready"] and detail["usage"]["orders_used"] == 0
    r = client.post(f"/admin/tenants/{tenants[-1]['id']}/impersonate", headers=admin)
    assert r.status_code == 200 and r.json()["user"]["impersonating"]


# ---------------- Accounting connections ----------------
def _state(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def connect_xero(client, h, monkeypatch, fake_http, orgs):
    monkeypatch.setattr(XeroProvider, "exchange_code",
                        lambda self, code, params: TokenSet("at", "rt", 1800))
    fake_http.routes[("GET", CONNECTIONS_URL)] = [
        {"id": f"conn-{o[0]}", "tenantId": o[0], "tenantName": o[1], "tenantType": "ORGANISATION"} for o in orgs]
    url = client.get("/integrations/xero/connect-url", headers=h).json()["url"]
    return client.get(f"/integrations/xero/callback?code=abc&state={_state(url)}", follow_redirects=False)


def test_connect_single_org_auto_activates(client, auth, monkeypatch, fake_http, db):
    fake_http.routes[("GET", "/Accounts")] = {"Accounts": [{"Code": "200", "Name": "Sales", "Class": "REVENUE"}]}
    fake_http.routes[("GET", "/Organisation")] = {"Organisations": [{"BaseCurrency": "USD", "CountryCode": "US"}]}
    r = connect_xero(client, auth, monkeypatch, fake_http, [("org-1", "Fresh Farms Demo")])
    assert r.status_code in (302, 307) and "connected=xero" in r.headers["location"]
    st = client.get("/integrations", headers=auth).json()
    assert st["connection"]["status"] == "connected" and st["connection"]["org_name"] == "Fresh Farms Demo"
    assert st["connection"]["org_currency"] == "USD" and st["connection"]["org_country"] == "US"
    assert db.scalars(select(IntegrationLink)).one().external_id == "org-1"
    assert db.query(Tenant).first().accounting_provider == "xero"
    # connect ke baad pehla sync bhi chala (accounts aa gaye)
    assert any(a["code"] == "200" and a["source"] == "xero"
               for a in client.get("/accounting/accounts", headers=auth).json())
    me = client.get("/auth/me", headers=auth).json()
    assert me["accounting"]["provider"] == "xero" and me["accounting"]["connected"]


def test_choose_org_when_multiple(client, auth, monkeypatch, fake_http):
    r = connect_xero(client, auth, monkeypatch, fake_http, [("org-1", "Fresh Farms"), ("org-2", "Fresh Farms NZ")])
    assert "choose_org=xero" in r.headers["location"]
    orgs = client.get("/integrations/orgs", headers=auth).json()
    assert [o["name"] for o in orgs["orgs"]] == ["Fresh Farms", "Fresh Farms NZ"]
    assert client.get("/integrations", headers=auth).json()["connection"]["status"] == "pending_org"
    assert client.post("/integrations/org", headers=auth, json={"org_id": "nope"}).status_code == 400
    conn = client.post("/integrations/org", headers=auth, json={"org_id": "org-2"}).json()
    assert conn["status"] == "connected" and conn["org_name"] == "Fresh Farms NZ"


def test_only_one_accounting_software_per_client(client, auth, monkeypatch, fake_http):
    connect_xero(client, auth, monkeypatch, fake_http, [("org-1", "Fresh Farms")])
    r = client.get("/integrations/quickbooks/connect-url", headers=auth)
    assert r.status_code == 400 and "Xero" in r.json()["detail"]
    client.post("/integrations/disconnect", headers=auth)
    assert client.get("/integrations/quickbooks/connect-url", headers=auth).status_code == 200


def test_same_books_cannot_link_two_clients(client, monkeypatch, fake_http, db):
    a, b = two_tenants(client)
    connect_xero(client, a, monkeypatch, fake_http, [("org-1", "Shared Books")])
    r = connect_xero(client, b, monkeypatch, fake_http, [("org-1", "Shared Books")])
    assert "error=xero" in r.headers["location"]
    assert client.get("/integrations", headers=b).json()["connection"]["status"] == "pending_org"
    assert db.scalars(select(IntegrationLink)).one().tenant_id == 1


def test_disconnect_clears_links(client, auth, monkeypatch, fake_http, db):
    connect_xero(client, auth, monkeypatch, fake_http, [("org-1", "Fresh Farms")])
    assert client.post("/integrations/disconnect", headers=auth).json()["ok"]
    db.expire_all()
    assert db.scalars(select(IntegrationLink)).all() == []
    assert db.query(Tenant).first().accounting_provider is None
    assert client.get("/integrations", headers=auth).json()["connection"] is None
    with tenant_db(1) as s:
        assert s.scalars(select(Connection)).all() == []


def test_bad_state_rejected(client):
    r = client.get("/integrations/xero/callback?code=abc&state=forged", follow_redirects=False)
    assert "error=state" in r.headers["location"]


def test_webhook_routes_to_right_client(client, monkeypatch, fake_http):
    """Xero org id -> integration link -> sahi client ka DB (doosre ka invoice nahi chhua)."""
    import base64
    import hashlib
    import hmac
    import json

    a, b = two_tenants(client)
    connect_xero(client, b, monkeypatch, fake_http, [("org-b", "Ocean Books")])
    body = json.dumps({"events": [{"resourceId": "inv-x", "tenantId": "org-b", "eventCategory": "INVOICE"}]}).encode()
    sig = base64.b64encode(hmac.new(b"xero-webhook-key", body, hashlib.sha256).digest()).decode()
    assert client.post("/webhooks/xero", content=body, headers={"x-xero-signature": sig}).status_code == 200
    unknown = json.dumps({"events": [{"resourceId": "i", "tenantId": "stranger", "eventCategory": "INVOICE"}]}).encode()
    sig2 = base64.b64encode(hmac.new(b"xero-webhook-key", unknown, hashlib.sha256).digest()).decode()
    assert client.post("/webhooks/xero", content=unknown, headers={"x-xero-signature": sig2}).status_code == 200
