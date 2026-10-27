"""Destinations: webhook, custom API, internal invoice, accounting software (Xero) + retries."""
import json

import httpx

from app.core import mailer
from app.core.database import utcnow
from app.models import Destination, ExportJob, Invoice
from app.modules.destinations import custom_api, webhook


class Resp:
    def __init__(self, status=200, body=None, text="ok"):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def approve(client, h, order):
    r = client.post(f"/orders/{order['id']}/approve", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_crud_masks_secrets(client, auth, db):
    r = client.post("/destinations", headers=auth, json={"name": "ERP", "type": "custom_api", "config": {
        "url": "https://erp.example.com/orders", "auth_type": "bearer", "auth_value": "super-secret"}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["config"]["auth_value"] == "••••••••"
    raw = db.get(Destination, d["id"]).config
    assert raw["auth_value"].startswith("enc:")

    # Update bina secret badle (MASK wapas bheja) - purana secret rehna chahiye
    r = client.put(f"/destinations/{d['id']}", headers=auth, json={"name": "ERP 2", "type": "custom_api",
                                                                   "config": {**d["config"], "method": "PUT"}})
    assert r.status_code == 200, r.text
    db.rollback()
    db.expire_all()
    from app.modules.destinations.service import decrypt_config
    assert decrypt_config("custom_api", db.get(Destination, d["id"]).config)["auth_value"] == "super-secret"

    bad = client.post("/destinations", headers=auth, json={"name": "x", "type": "webhook", "config": {"url": "ftp://x"}})
    assert bad.status_code == 400


def test_webhook_on_approve(client, seeded, draft_order, monkeypatch):
    sent = {}
    monkeypatch.setattr(webhook.httpx, "post", lambda url, content=None, headers=None, timeout=None:
                        sent.update(url=url, body=json.loads(content), headers=headers) or Resp())
    client.post("/destinations", headers=seeded, json={"name": "Zapier", "type": "webhook",
                                                       "config": {"url": "https://hooks.example.com/x"}})
    o = approve(client, seeded, draft_order)
    assert o["status"] == "exported" and o["exports"][0]["status"] == "success"
    assert sent["body"]["data"]["lines"][0]["sku"] == "TOM-ROMA-10KG"
    assert sent["body"]["data"]["lines"][0]["unit_price"] == 32.5
    assert "X-OrderDraft-Signature" in sent["headers"]


def test_custom_api_mapping_and_response_id(client, seeded, draft_order, monkeypatch):
    captured = {}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        captured.update(method=method, body=json, headers=headers)
        return Resp(201, {"data": {"order_number": "SO-991"}})

    monkeypatch.setattr(custom_api.httpx, "request", fake_request)
    client.post("/destinations", headers=seeded, json={"name": "ERP", "type": "custom_api", "config": {
        "url": "https://erp.example.com/orders", "auth_type": "api_key", "auth_value": "k1",
        "body_mode": "mapped", "field_map": {"customer_code": "customer.code", "ref": "po_number"},
        "lines_key": "items", "line_field_map": {"code": "sku", "qty": "quantity"},
        "response_id_path": "data.order_number"}})
    o = approve(client, seeded, draft_order)
    assert captured["body"] == {"customer_code": "JOES01", "ref": "J-55",
                                "items": [{"code": "TOM-ROMA-10KG", "qty": 2.0}, {"code": "LET-COS-EA", "qty": 6.0}]}
    assert captured["headers"]["X-API-Key"] == "k1"
    assert o["exports"][0]["external_id"] == "SO-991"


def test_retry_backoff_and_manual_retry(client, seeded, draft_order, monkeypatch, db):
    monkeypatch.setattr(webhook.httpx, "post", lambda *a, **k: Resp(503, text="down"))
    client.post("/destinations", headers=seeded, json={"name": "Hook", "type": "webhook",
                                                       "config": {"url": "https://hooks.example.com/x"}})
    o = approve(client, seeded, draft_order)
    job = o["exports"][0]
    assert job["status"] == "failed" and job["attempts"] == 1 and job["next_retry_at"]
    assert o["status"] == "approved"

    # Scheduler: retry time aa gaya
    j = db.get(ExportJob, job["id"])
    j.next_retry_at = utcnow()
    db.commit()
    monkeypatch.setattr(webhook.httpx, "post", lambda *a, **k: Resp(200))
    from app.scheduler import run_due_tasks
    run_due_tasks(force=True)
    db.rollback()
    db.expire_all()
    assert db.get(ExportJob, job["id"]).status == "success"

    # 4xx = retry nahi
    monkeypatch.setattr(webhook.httpx, "post", lambda *a, **k: Resp(400, text="bad"))
    r = client.post(f"/orders/{o['id']}/send", headers=seeded, json={"destination_ids": [job["destination_id"]]}).json()
    failed = [e for e in r["exports"] if e["status"] == "failed"][0]
    assert failed["next_retry_at"] is None

    r = client.post(f"/exports/{failed['id']}/retry", headers=seeded)
    assert r.status_code == 200 and r.json()["attempts"] == 2


def test_network_error_retryable(client, seeded, draft_order, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(webhook.httpx, "post", boom)
    client.post("/destinations", headers=seeded, json={"name": "Hook", "type": "webhook",
                                                       "config": {"url": "https://hooks.example.com/x"}})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and job["next_retry_at"]


def test_internal_invoice_destination(client, seeded, draft_order, db):
    client.post("/destinations", headers=seeded, json={"name": "Invoices", "type": "internal_invoice",
                                                       "config": {"auto_email": True}})
    o = approve(client, seeded, draft_order)
    assert o["exports"][0]["status"] == "success" and "emailed" in o["exports"][0]["response_summary"]
    inv = o["invoices"][0]
    assert inv["number"] == "INV-1001" and inv["status"] == "sent" and inv["source"] == "internal"
    # 2 x 32.50 + 6 x 2.60 = 80.60 ; GST 10% = 8.06
    assert inv["total"] == 88.66
    inv_mail = next(m for m in mailer.SENT if m["attachments"])
    assert inv_mail["attachments"] == ["INV-1001.pdf"] and inv_mail["to"] == "orders@joescafe.com.au"
    assert "via OrderDraft" in inv_mail["from"]                       # domain verified nahi -> platform sender
    assert mailer.SENT[-1]["subject"].startswith("Order confirmation")  # approve par confirmation bhi gaya


def test_xero_invoice_destination(client, seeded, draft_order, fake_xero, db):
    def create_invoice(body, params):
        doc = body["Invoices"][0]
        return {"Invoices": [{"InvoiceID": "xi-1", "InvoiceNumber": "INV-0042", "Status": doc["Status"],
                              "SubTotal": 80.6, "TotalTax": 8.06, "Total": 88.66, "AmountPaid": 0,
                              "Contact": {"ContactID": "xc-joe"}}]}
    fake_xero.routes[("POST", "/Invoices")] = create_invoice
    client.post("/destinations", headers=seeded, json={"name": "Xero", "type": "accounting",
                                                       "config": {"document_type": "invoice_draft", "default_account_code": "200"}})
    o = approve(client, seeded, draft_order)
    assert o["exports"][0]["status"] == "success", o["exports"][0]
    doc = fake_xero.calls[-1]["json"]["Invoices"][0]
    assert doc["Type"] == "ACCREC" and doc["Status"] == "DRAFT" and doc["Reference"] == "J-55"
    assert "CurrencyCode" not in doc        # org ki base currency (non-AUD org pe AUD bhejna error deta tha)
    assert doc["Contact"]["Name"] == "Joe's Cafe Bondi"
    assert doc["LineItems"][0] == {"Description": "Roma Tomatoes 10kg", "Quantity": 2.0, "AccountCode": "200",
                                   "UnitAmount": 32.5}   # CSV product - ItemCode nahi bheja
    inv = db.query(Invoice).filter_by(source="xero").one()
    assert inv.external_id == "xi-1" and inv.number == "INV-0042" and inv.status == "draft"
    assert db.query(Invoice).filter_by(source="xero").one().customer.external_id == "xc-joe"


def test_xero_error_not_retryable(client, seeded, draft_order, fake_xero):
    fake_xero.routes[("POST", "/Invoices")] = (400, {"Elements": [{"ValidationErrors": [
        {"Message": "Account code '999' is not a valid code"}]}]})
    client.post("/destinations", headers=seeded, json={"name": "Xero", "type": "accounting", "config": {}})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and "999" in job["last_error"] and job["next_retry_at"] is None


def test_xero_quote(client, seeded, draft_order, fake_xero):
    fake_xero.routes[("POST", "/Quotes")] = {"Quotes": [{"QuoteID": "q-1", "QuoteNumber": "QU-7"}]}
    client.post("/destinations", headers=seeded, json={"name": "Xero quotes", "type": "accounting",
                                                       "config": {"document_type": "quote"}})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success" and job["external_id"] == "q-1"


def test_manual_only_destination(client, seeded, draft_order, monkeypatch):
    monkeypatch.setattr(webhook.httpx, "post", lambda *a, **k: Resp())
    d = client.post("/destinations", headers=seeded, json={"name": "Manual", "type": "webhook", "auto_send": False,
                                                           "config": {"url": "https://x.example.com"}}).json()
    o = approve(client, seeded, draft_order)
    assert o["exports"] == []
    o = client.post(f"/orders/{o['id']}/send", headers=seeded, json={"destination_ids": [d["id"]]}).json()
    assert o["exports"][0]["status"] == "success"
