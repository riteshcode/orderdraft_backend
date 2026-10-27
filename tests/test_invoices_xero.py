"""Own invoices (PDF, payments, void, overdue) + Xero sync + Xero webhook."""
import base64
import hashlib
import hmac
import json
from datetime import date, timedelta

from app.models import Invoice, Product


def make_invoice(client, h, order):
    client.post("/destinations", headers=h, json={"name": "Invoices", "type": "internal_invoice",
                                                  "config": {"auto_email": False}})
    o = client.post(f"/orders/{order['id']}/approve", headers=h).json()
    return client.get(f"/invoices/{o['invoices'][0]['id']}", headers=h).json()


def test_invoice_lifecycle(client, seeded, draft_order):
    inv = make_invoice(client, seeded, draft_order)
    assert inv["status"] == "draft" and inv["number"] == "INV-1001"
    assert [l["sku"] for l in inv["lines"]] == ["TOM-ROMA-10KG", "LET-COS-EA"]
    assert inv["due_date"] == (date.today() + timedelta(days=14)).isoformat()

    pdf = client.get(f"/invoices/{inv['id']}/pdf", headers=seeded)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    r = client.post(f"/invoices/{inv['id']}/send", headers=seeded, json={"to": "accounts@joescafe.com.au"}).json()
    assert r["status"] == "sent" and r["sent_at"]

    r = client.post(f"/invoices/{inv['id']}/payments", headers=seeded,
                    json={"amount": 50, "paid_on": date.today().isoformat(), "method": "Bank transfer"}).json()
    assert r["status"] == "sent" and r["balance"] == 38.66
    over = client.post(f"/invoices/{inv['id']}/payments", headers=seeded,
                       json={"amount": 100, "paid_on": date.today().isoformat()})
    assert over.status_code == 400
    r = client.post(f"/invoices/{inv['id']}/payments", headers=seeded,
                    json={"amount": 38.66, "paid_on": date.today().isoformat()}).json()
    assert r["status"] == "paid" and r["balance"] == 0 and len(r["payments"]) == 2

    assert client.post(f"/invoices/{inv['id']}/void", headers=seeded).status_code == 400   # paid hai
    summary = client.get("/invoices/summary", headers=seeded).json()
    assert summary["paid_total"] == 88.66


def test_overdue_and_void(client, seeded, draft_order, db):
    inv = make_invoice(client, seeded, draft_order)
    client.post(f"/invoices/{inv['id']}/send", headers=seeded, json={})
    row = db.get(Invoice, inv["id"])
    row.due_date = date.today() - timedelta(days=3)
    db.commit()
    listed = client.get("/invoices?status=overdue", headers=seeded).json()
    assert listed["total"] == 1 and listed["items"][0]["overdue"]
    assert client.get("/invoices/summary", headers=seeded).json()["overdue_count"] == 1

    r = client.post(f"/invoices/{inv['id']}/void", headers=seeded).json()
    assert r["status"] == "void"
    assert client.get("/invoices?status=overdue", headers=seeded).json()["total"] == 0


def test_invoice_numbers_increment_and_settings(client, seeded, draft_order, mock_ai):
    client.put("/settings/invoice", headers=seeded, json={"invoice_prefix": "FF-", "invoice_next_number": 500,
                                                          "gst_rate": 10, "prices_include_gst": True,
                                                          "payment_terms_days": 7, "bank_details": "BSB 062-000"})
    inv = make_invoice(client, seeded, draft_order)
    assert inv["number"] == "FF-500"
    assert inv["total"] == 80.6 and inv["gst"] == 7.33      # GST inclusive: 80.60 / 11
    assert client.get("/settings", headers=seeded).json()["invoice"]["invoice_next_number"] == 501


def test_xero_sync(client, auth, fake_xero, db):
    fake_xero.routes[("GET", "/Items")] = {"Items": [
        {"ItemID": "i1", "Code": "TOM-1", "Name": "Tomatoes", "SalesDetails": {"UnitPrice": 30, "AccountCode": "200",
                                                                              "TaxType": "OUTPUT"}},
        {"ItemID": "i2", "Code": "FREE-1", "Name": "Bread", "SalesDetails": {"UnitPrice": 5, "TaxType": "EXEMPTOUTPUT"}},
        {"ItemID": "i3", "Code": "BUY-ONLY", "Name": "Packaging", "IsSold": False},
    ]}
    fake_xero.routes[("GET", "/Contacts")] = {"Contacts": [
        {"ContactID": "c1", "Name": "Joe's Cafe", "AccountNumber": "J1", "EmailAddress": "joe@cafe.com",
         "Phones": [{"PhoneType": "MOBILE", "PhoneNumber": "0400111222"}], "ContactStatus": "ACTIVE"},
        {"ContactID": "c2", "Name": "Closed Co", "ContactStatus": "ARCHIVED"},
    ]}
    r = client.post("/integrations/sync", headers=auth)
    assert r.status_code == 200
    products = {p["sku"]: p for p in client.get("/products", headers=auth).json()["items"]}
    assert set(products) == {"TOM-1", "FREE-1"}
    assert products["TOM-1"]["source"] == "xero" and products["TOM-1"]["external_id"] == "i1"
    assert products["FREE-1"]["is_taxable"] is False
    customers = client.get("/customers", headers=auth).json()["items"]
    assert [c["name"] for c in customers] == ["Joe's Cafe"]     # archived = inactive
    contacts_call = [c for c in fake_xero.calls if c["path"] == "/Contacts"][0]
    assert contacts_call["params"]["where"] == "IsCustomer==true"

    # Xero mein naam badla - sync update kare, duplicate nahi
    fake_xero.routes[("GET", "/Items")] = {"Items": [{"ItemID": "i1", "Code": "TOM-1", "Name": "Roma Tomatoes"}]}
    client.post("/integrations/sync", headers=auth)
    assert db.query(Product).filter_by(external_id="i1").one().name == "Roma Tomatoes"
    status = client.get("/integrations", headers=auth).json()["connection"]
    assert status["status"] == "connected" and status["last_sync_status"].startswith("OK")


def test_xero_webhook_signature_and_refresh(client, seeded, draft_order, fake_xero, db):
    fake_xero.routes[("POST", "/Invoices")] = {"Invoices": [{"InvoiceID": "xi-9", "InvoiceNumber": "INV-9",
                                                             "Status": "DRAFT", "Total": 88.66}]}
    client.post("/destinations", headers=seeded, json={"name": "Xero", "type": "accounting", "config": {}})
    client.post(f"/orders/{draft_order['id']}/approve", headers=seeded)

    body = json.dumps({"events": [{"resourceId": "xi-9", "tenantId": "xt-1", "eventCategory": "INVOICE",
                                   "eventType": "UPDATE"}]}).encode()
    bad = client.post("/webhooks/xero", content=body, headers={"x-xero-signature": "nope"})
    assert bad.status_code == 401

    fake_xero.routes[("GET", "/Invoices/xi-9")] = {"Invoices": [{"InvoiceID": "xi-9", "Status": "PAID",
                                                                 "Total": 88.66, "AmountPaid": 88.66}]}
    sig = base64.b64encode(hmac.new(b"xero-webhook-key", body, hashlib.sha256).digest()).decode()
    ok = client.post("/webhooks/xero", content=body, headers={"x-xero-signature": sig})
    assert ok.status_code == 200
    db.rollback()
    db.expire_all()
    inv = db.query(Invoice).filter_by(external_id="xi-9").one()
    assert inv.status == "paid" and float(inv.amount_paid) == 88.66


def test_provider_not_configured(client, auth, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "XERO_CLIENT_ID", "")
    r = client.get("/integrations/xero/connect-url", headers=auth)
    assert r.status_code == 400 and "configured" in r.json()["detail"]
