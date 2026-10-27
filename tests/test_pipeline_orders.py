"""Intake -> draft order -> review -> approve -> learn."""
import base64

from app.models import Tenant, UsageEvent
from app.core.database import utcnow
from datetime import timedelta
from tests.conftest import JOE_ORDER, PM_AUTH, postmark_payload
from tests.test_units import make_pdf


def latest(client, h):
    items = client.get("/orders", headers=h).json()["items"]
    return client.get(f"/orders/{items[0]['id']}", headers=h).json() if items else None


def test_email_to_draft(client, draft_order):
    o = draft_order
    assert o["status"] == "needs_review" and o["customer_name"] == "Joe's Cafe Bondi"
    assert [l["product_sku"] for l in o["lines"]] == ["TOM-ROMA-10KG", "LET-COS-EA"]
    assert o["lines"][0]["unit_price"] == 32.5
    assert o["message"]["channel"] == "email" and "path" not in str(o["message"]["attachments"])


def test_usage_recorded(client, draft_order, db):
    events = db.query(UsageEvent).all()
    assert events and events[0].input_tokens == 1000 and events[0].cost_usd > 0


def test_voice_and_pdf(client, seeded, mock_ai):
    mock_ai.transcript = "Marco from Bella Italia, three boxes of roma"
    mock_ai.order = {"is_order": True, "customer_name": "Bella Italia",
                     "items": [{"raw_text": "three boxes of roma", "product_hint": "roma", "quantity": 3, "unit": "box"}]}
    client.post("/inbound/upload", headers=seeded, files={"file": ("v.m4a", b"audio", "audio/mp4")})
    o = latest(client, seeded)
    assert o["message"]["channel"] == "voice" and o["message"]["attachments"][0]["transcript"].startswith("Marco")
    assert "Marco" in mock_ai.calls[1]["text"]

    pdf = make_pdf(["PURCHASE ORDER PO-7781", "10 x Washed Potatoes 20kg"])
    mock_ai.order = {"is_order": True, "customer_name": "Harbour View Hotel", "po_number": "PO-7781",
                     "items": [{"raw_text": "10 x Washed Potatoes 20kg", "product_hint": "washed potatoes 20kg", "quantity": 10}]}
    att = [{"Name": "PO.pdf", "ContentType": "application/pdf", "Content": base64.b64encode(pdf).decode()}]
    client.post("/inbound/postmark", auth=PM_AUTH,
                json=postmark_payload("See PO", sender="purchasing@harbourviewhotel.com.au", attachments=att))
    assert "PO-7781" in mock_ai.calls[-1]["text"]
    assert latest(client, seeded)["lines"][0]["product_sku"] == "POT-WASH-20KG"


def test_not_order_failure_and_retry(client, seeded, mock_ai, monkeypatch):
    mock_ai.order = {"is_order": False, "items": []}
    client.post("/inbound/text", headers=seeded, json={"text": "Can you resend invoice?"})
    msg = client.get("/inbound/messages", headers=seeded).json()["items"][0]
    assert msg["status"] == "not_order"

    from app.modules.parsing import extractor
    monkeypatch.setattr(extractor, "extract_order", lambda *a, **k: (_ for _ in ()).throw(extractor.ExtractionError("Claude down")))
    client.post("/inbound/text", headers=seeded, json={"text": "2 romas"})
    msg = client.get("/inbound/messages?status=failed", headers=seeded).json()["items"][0]
    assert "Claude down" in msg["error"]


def test_blocked_when_trial_expired(client, seeded, mock_ai, db):
    t = db.query(Tenant).first()
    t.trial_ends_at = utcnow() - timedelta(days=1)
    db.commit()
    mock_ai.order = JOE_ORDER
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 romas"))
    msg = client.get("/inbound/messages", headers=seeded).json()["items"][0]
    assert msg["status"] == "blocked" and "Trial" in msg["error"]
    assert client.get("/orders", headers=seeded).json()["total"] == 0

    # Upgrade (yahan manually active) ke baad retry
    t.subscription_status = "active"
    db.commit()
    client.post(f"/inbound/messages/{msg['id']}/retry", headers=seeded)
    assert client.get("/orders", headers=seeded).json()["total"] == 1


def test_edit_approve_learns(client, seeded, mock_ai):
    mock_ai.order = {"is_order": True, "customer_email": "orders@joescafe.com.au",
                     "items": [{"raw_text": "2 x the usual reds", "product_hint": "the usual reds", "quantity": 2}]}
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 x the usual reds"))
    o = latest(client, seeded)
    products = {p["sku"]: p["id"] for p in client.get("/products?page_size=100", headers=seeded).json()["items"]}

    r = client.put(f"/orders/{o['id']}", headers=seeded, json={
        "customer_id": o["customer_id"], "po_number": "J-9", "delivery_date": "2026-10-21", "notes": None,
        "lines": [{"id": o["lines"][0]["id"], "quantity": 2, "unit": "box", "product_id": products["CAP-RED-5KG"]},
                  {"quantity": 1, "unit": "bunch", "product_id": products["HERB-CORI-BCH"], "raw_text": "coriander"}]})
    assert r.status_code == 200 and len(r.json()["lines"]) == 2

    r = client.post(f"/orders/{o['id']}/approve", headers=seeded)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"           # koi destination nahi = approved hi rahega
    assert approved["total_amount"] == 2 * 27.0 + 2.10

    insights = client.get(f"/customers/{o['customer_id']}/insights", headers=seeded).json()
    assert any(a["alias"] == "the usual reds" for a in insights["aliases"])
    assert any(u["sku"] == "CAP-RED-5KG" for u in insights["usual_products"])

    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 x the usual reds"))
    again = latest(client, seeded)
    assert again["lines"][0]["product_sku"] == "CAP-RED-5KG" and again["lines"][0]["match_method"] == "alias"


def test_approve_requires_customer_and_products(client, seeded, mock_ai):
    mock_ai.order = {"is_order": True, "customer_name": "Totally Unknown",
                     "items": [{"raw_text": "1 romas", "product_hint": "romas", "quantity": 1}]}
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("x", sender="nobody@gmail.com"))
    o = latest(client, seeded)
    assert o["customer_id"] is None
    r = client.post(f"/orders/{o['id']}/approve", headers=seeded)
    assert r.status_code == 400 and "customer" in r.json()["detail"]


def test_reject_and_filters_and_csv(client, draft_order, seeded):
    client.post(f"/orders/{draft_order['id']}/reject", headers=seeded)
    assert client.get("/orders?status=rejected", headers=seeded).json()["total"] == 1
    assert client.get("/orders?q=Joe", headers=seeded).json()["total"] == 1
    assert client.get("/orders?channel=voice", headers=seeded).json()["total"] == 0
    stats = client.get("/orders/stats", headers=seeded).json()
    assert stats["by_status"] == {"rejected": 1}
    csv_text = client.get("/orders/export.csv?status=rejected", headers=seeded).text
    assert "TOM-ROMA-10KG" in csv_text and "32.5" in csv_text


def test_tenant_isolation(client, draft_order):
    from tests.conftest import register
    other = register(client, email="x@other.com", business="Other Co")
    assert client.get(f"/orders/{draft_order['id']}", headers=other).status_code == 404
    assert client.get("/products", headers=other).json()["total"] == 0


def test_same_product_on_two_lines(client, seeded, mock_ai):
    """Ek order mein same product do baar - approve crash nahi hona chahiye."""
    mock_ai.order = {"is_order": True, "customer_email": "orders@joescafe.com.au",
                     "items": [{"raw_text": "2 romas", "product_hint": "romas", "quantity": 2},
                               {"raw_text": "1 more romas", "product_hint": "romas", "quantity": 1}]}
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("x"))
    o = latest(client, seeded)
    r = client.post(f"/orders/{o['id']}/approve", headers=seeded)
    assert r.status_code == 200, r.text
