"""Accounts & Tax (CRUD + Xero import), manual orders, manual invoices."""
from datetime import date, timedelta

from app.models import Product


def products_by_sku(client, h):
    return {p["sku"]: p for p in client.get("/products", headers=h, params={"page_size": 200}).json()["items"]}


def customer_id(client, h, name="Joe"):
    return [c for c in client.get("/customers", headers=h).json()["items"] if name in c["name"]][0]["id"]


# ---------------- Accounting ----------------
def test_accounting_defaults_and_crud(client, auth):
    accs = client.get("/accounting/accounts", headers=auth).json()
    assert [a["code"] for a in accs] == ["200", "260"]
    taxes = {t["code"]: t["rate"] for t in client.get("/accounting/tax-rates", headers=auth).json()}
    assert taxes == {"BASEXCLUDED": 0, "EXEMPTOUTPUT": 0, "OUTPUT": 10}

    a = client.post("/accounting/accounts", headers=auth, json={"code": "210", "name": "Wholesale Sales"}).json()
    assert a["source"] == "manual"
    assert client.post("/accounting/accounts", headers=auth, json={"code": "210", "name": "dup"}).status_code == 400
    assert client.post("/accounting/accounts", headers=auth,
                       json={"code": "211", "name": "x", "account_class": "BOGUS"}).status_code == 400
    r = client.put(f"/accounting/accounts/{a['id']}", headers=auth, json={"code": "215", "name": "Wholesale"}).json()
    assert r["code"] == "215"
    client.delete(f"/accounting/accounts/{a['id']}", headers=auth)
    assert "215" not in [x["code"] for x in client.get("/accounting/accounts", headers=auth).json()]
    assert "215" in [x["code"] for x in client.get("/accounting/accounts?include_inactive=true", headers=auth).json()]

    t = client.post("/accounting/tax-rates", headers=auth, json={"code": "lux", "name": "Luxury", "rate": 33}).json()
    assert t["code"] == "LUX" and t["rate"] == 33
    assert client.post("/accounting/tax-rates", headers=auth, json={"code": "X", "name": "x", "rate": 150}).status_code == 422


def test_accounting_xero_import(client, auth, fake_xero):
    fake_xero.routes[("GET", "/Accounts")] = {"Accounts": [
        {"AccountID": "a1", "Code": "200", "Name": "Sales - Xero", "Class": "REVENUE", "Status": "ACTIVE"},
        {"AccountID": "a2", "Code": "400", "Name": "Advertising", "Class": "EXPENSE", "Status": "ACTIVE"},
        {"AccountID": "a3", "Name": "Business Bank", "Class": "ASSET"},   # no code - skip
    ]}
    fake_xero.routes[("GET", "/TaxRates")] = {"TaxRates": [
        {"TaxType": "OUTPUT", "Name": "GST on Income", "EffectiveRate": 10, "CanApplyToRevenue": True},
        {"TaxType": "INPUT", "Name": "GST on Expenses", "EffectiveRate": 10, "CanApplyToRevenue": False,
         "CanApplyToExpenses": True},
    ]}
    r = client.post("/accounting/import", headers=auth).json()
    assert r["accounts"] == {"created": 2, "updated": 0}       # defaults abhi bane nahi the
    accs = {a["code"]: a for a in client.get("/accounting/accounts", headers=auth).json()}
    assert accs["200"]["name"] == "Sales - Xero" and accs["200"]["source"] == "xero"
    assert accs["400"]["account_class"] == "EXPENSE"
    taxes = {t["code"]: t for t in client.get("/accounting/tax-rates", headers=auth).json()}
    assert taxes["INPUT"]["applies_to"] == "purchases"
    # Xero wale ka code nahi badal sakte
    assert client.put(f"/accounting/accounts/{accs['200']['id']}", headers=auth,
                      json={"code": "201", "name": "x"}).status_code == 400
    # dobara import = update
    r = client.post("/accounting/import", headers=auth).json()
    assert r["accounts"] == {"created": 0, "updated": 2}


def test_accounting_xero_not_connected(client, auth):
    assert client.post("/accounting/import", headers=auth).status_code == 400


# ---------------- Manual orders ----------------
def test_manual_order_draft_and_approve(client, seeded):
    p = products_by_sku(client, seeded)
    cid = customer_id(client, seeded)
    body = {"customer_id": cid, "po_number": "PHONE-1", "delivery_date": (date.today() + timedelta(days=1)).isoformat(),
            "notes": "Phone order", "lines": [
                {"product_id": p["TOM-ROMA-10KG"]["id"], "quantity": 2, "unit": "box"},
                {"product_id": p["LET-COS-EA"]["id"], "quantity": 6, "unit_price": 1.5},
            ]}
    o = client.post("/orders/manual", headers=seeded, json=body).json()
    assert o["source"] == "manual" and o["status"] not in ("approved", "exported")
    assert o["message"] is None and o["customer_confidence"] == 100
    prices = [l["unit_price"] for l in o["lines"]]
    assert prices == [32.5, 1.5]
    assert all(l["match_method"] == "manual" for l in o["lines"])

    listed = client.get("/orders", headers=seeded).json()["items"]
    assert listed[0]["source"] == "manual"

    approved = client.post(f"/orders/{o['id']}/approve", headers=seeded).json()
    assert approved["status"] == "approved" and approved["total_amount"] == 74.0     # 65 + 9 (override)

    # create + approve ek saath
    body["approve"] = True
    o2 = client.post("/orders/manual", headers=seeded, json=body).json()
    assert o2["status"] == "approved"
    # manual orders AI use nahi karte - plan limit mein count nahi
    assert client.get("/auth/me", headers=seeded).json()["usage"]["orders_used"] == 0


def test_manual_order_validation(client, seeded):
    cid = customer_id(client, seeded)
    assert client.post("/orders/manual", headers=seeded, json={"customer_id": 99999, "lines": [
        {"product_id": 1, "quantity": 1}]}).status_code in (400, 404)
    assert client.post("/orders/manual", headers=seeded, json={"customer_id": cid, "lines": []}).status_code == 400
    assert client.post("/orders/manual", headers=seeded, json={"customer_id": cid, "lines": [
        {"product_id": 99999, "quantity": 1}]}).status_code == 400


def test_price_override_on_review(client, seeded, draft_order):
    lines = [{"id": l["id"], "quantity": l["quantity"], "unit": l["unit"], "product_id": l["product_id"],
              "unit_price": 30 if i == 0 else None} for i, l in enumerate(draft_order["lines"])]
    o = client.put(f"/orders/{draft_order['id']}", headers=seeded, json={
        "customer_id": draft_order["customer_id"], "po_number": draft_order["po_number"],
        "delivery_date": draft_order["delivery_date"], "notes": None, "lines": lines}).json()
    assert o["lines"][0]["unit_price"] == 30
    o = client.post(f"/orders/{draft_order['id']}/approve", headers=seeded).json()
    assert o["total_amount"] == 60 + 6 * draft_order["lines"][1]["unit_price"]


# ---------------- Manual invoices ----------------
def test_manual_invoice_tax_codes(client, seeded, db):
    p = products_by_sku(client, seeded)
    cid = customer_id(client, seeded)
    # product ko GST-free bana do (tax_type se)
    prod = db.get(Product, p["LET-COS-EA"]["id"])
    prod.tax_type = "EXEMPTOUTPUT"
    db.commit()

    body = {"customer_id": cid, "reference": "PHONE-7", "lines": [
        {"product_id": p["TOM-ROMA-10KG"]["id"], "quantity": 2},                      # default -> 10%
        {"product_id": p["LET-COS-EA"]["id"], "quantity": 10, "unit_price": 1},       # EXEMPTOUTPUT -> 0
        {"description": "Delivery fee", "quantity": 1, "unit_price": 15, "tax_code": "OUTPUT", "account_code": "260"},
        {"description": "Crate deposit", "quantity": 1, "unit_price": 5, "tax_code": ""},   # no tax
    ]}
    inv = client.post("/invoices", headers=seeded, json=body).json()
    assert inv["source"] == "internal" and inv["status"] == "draft" and inv["number"].startswith("INV-")
    lines = inv["lines"]
    assert [l["tax_rate"] for l in lines] == [10, 0, 10, 0]
    assert lines[1]["tax_code"] == "EXEMPTOUTPUT" and lines[3]["tax_code"] is None
    assert lines[2]["account_code"] == "260"
    assert inv["due_date"] == (date.today() + timedelta(days=14)).isoformat()
    # prices GST exclusive (default): GST sirf tax wali lines (65 + 15) pe
    assert inv["subtotal"] == 95 and inv["gst"] == 8.0 and inv["total"] == 103.0


def test_manual_invoice_edit_and_validation(client, seeded):
    cid = customer_id(client, seeded)
    bad = client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": [
        {"description": "", "quantity": 1, "unit_price": 5}]})
    assert bad.status_code == 400
    assert client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": [
        {"description": "x", "quantity": 0, "unit_price": 5}]}).status_code == 400
    assert client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": []}).status_code == 400
    assert client.post("/invoices", headers=seeded, json={
        "customer_id": cid, "issue_date": "2026-10-20", "due_date": "2026-10-01",
        "lines": [{"description": "x", "quantity": 1, "unit_price": 5}]}).status_code == 400

    inv = client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": [
        {"description": "Consulting", "quantity": 1, "unit_price": 100}]}).json()
    edited = client.put(f"/invoices/{inv['id']}/draft", headers=seeded, json={
        "customer_id": cid, "notes": "updated", "lines": [
            {"description": "Consulting", "quantity": 2, "unit_price": 100},
            {"description": "Travel", "quantity": 1, "unit_price": 50, "tax_code": ""}]}).json()
    assert edited["number"] == inv["number"] and len(edited["lines"]) == 2 and edited["notes"] == "updated"
    assert edited["subtotal"] == 250 and edited["gst"] == 20 and edited["total"] == 270

    # sent invoice edit nahi hota
    client.post(f"/invoices/{inv['id']}/send", headers=seeded, json={"to": "a@b.com"})
    assert client.put(f"/invoices/{inv['id']}/draft", headers=seeded, json={
        "customer_id": cid, "lines": [{"description": "x", "quantity": 1, "unit_price": 1}]}).status_code == 400


def test_manual_invoice_send_email(client, seeded):
    from app.core import mailer
    cid = customer_id(client, seeded)
    inv = client.post("/invoices", headers=seeded, json={"customer_id": cid, "send_email": True, "lines": [
        {"description": "Delivery", "quantity": 1, "unit_price": 20}]}).json()
    assert inv["status"] == "sent" and len(mailer.SENT) == 1


def test_manual_create_blocked_when_subscription_ends(client, seeded, db):
    from datetime import timedelta as td
    from app.core.database import utcnow
    from app.models import Tenant
    p = products_by_sku(client, seeded)
    cid = customer_id(client, seeded)
    t = db.query(Tenant).first()
    t.trial_ends_at = utcnow() - td(hours=1)
    db.commit()
    r = client.post("/orders/manual", headers=seeded, json={"customer_id": cid, "lines": [
        {"product_id": p["TOM-ROMA-10KG"]["id"], "quantity": 1}]})
    assert r.status_code == 402 and "Trial" in r.json()["detail"]
    r = client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": [
        {"description": "x", "quantity": 1, "unit_price": 1}]})
    assert r.status_code == 402
