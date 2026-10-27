"""QuickBooks Online connector: connect, sync, invoice/estimate export, status refresh, webhook."""
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.models import AccountCode, IntegrationLink, Invoice, Product, TaxRate
from app.modules.connectors.base import TokenSet
from app.modules.connectors.quickbooks import QuickBooksProvider
from tests.conftest import register


def query_router(tables: dict):
    """/query route: SQL ke entity ke hisaab se rows (STARTPOSITION se paging)."""
    def handler(body, params):
        sql = params["query"]
        assert params["minorversion"]
        entity = sql.split("FROM ")[1].split()[0]
        rows = tables.get(entity, [])
        if callable(rows):
            rows = rows(sql)
        start = int(sql.split("STARTPOSITION ")[1].split()[0])
        return {"QueryResponse": {entity: rows[start - 1:start - 1 + 1000]}}
    return handler


def approve(client, h, order):
    return client.post(f"/orders/{order['id']}/approve", headers=h).json()


# ---------------- Connect ----------------
def test_connect_with_realm_id(client, auth, monkeypatch, fake_http, db):
    monkeypatch.setattr(QuickBooksProvider, "exchange_code", lambda self, code, params: TokenSet("at", "rt", 3600))
    fake_http.routes[("GET", "/companyinfo/9130")] = {"CompanyInfo": {"CompanyName": "Fresh QBO"}}
    fake_http.routes[("GET", "/query")] = query_router({})
    url = client.get("/integrations/quickbooks/connect-url", headers=auth).json()["url"]
    assert "appcenter.intuit.com" in url
    state = parse_qs(urlparse(url).query)["state"][0]
    r = client.get(f"/integrations/quickbooks/callback?code=c&state={state}&realmId=9130", follow_redirects=False)
    assert "connected=quickbooks" in r.headers["location"]
    conn = client.get("/integrations", headers=auth).json()["connection"]
    assert conn["provider"] == "quickbooks" and conn["status"] == "connected" and conn["org_name"] == "Fresh QBO"
    link = db.scalars(select(IntegrationLink)).one()
    assert (link.provider, link.external_id) == ("quickbooks", "9130")
    assert "sandbox-quickbooks.api.intuit.com/v3/company/9130/companyinfo/9130" in fake_http.calls[0]["url"]


def test_connect_without_realm_fails(client, auth, monkeypatch, fake_http):
    monkeypatch.setattr(QuickBooksProvider, "exchange_code", lambda self, code, params: TokenSet("at", "rt", 3600))
    url = client.get("/integrations/quickbooks/connect-url", headers=auth).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    r = client.get(f"/integrations/quickbooks/callback?code=c&state={state}", follow_redirects=False)
    assert "error=quickbooks" in r.headers["location"]


# ---------------- Sync ----------------
def test_sync_accounts_tax_items_customers(client, auth, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130", org_name="Fresh QBO")
    items = [{"Id": str(i), "Name": f"Item {i}", "Sku": f"SKU-{i}", "UnitPrice": 2, "Type": "NonInventory",
              "Active": True, "IncomeAccountRef": {"value": "79"}, "SalesTaxCodeRef": {"value": "5"}, "Taxable": True}
             for i in range(1, 1003)]                       # 1002 = do pages
    items.append({"Id": "9999", "Name": "Old", "Active": False, "Type": "Service"})
    fake.routes[("GET", "/query")] = query_router({
        "Account": [{"Id": "79", "Name": "Sales", "AcctNum": "4000", "Classification": "Revenue", "Active": True},
                    {"Id": "80", "Name": "COGS", "Classification": "Expense", "Active": True}],
        "TaxRate": [{"Id": "20", "RateValue": 10}, {"Id": "21", "RateValue": 0}],
        "TaxCode": [{"Id": "5", "Name": "GST", "Active": True,
                     "SalesTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "20"}}]},
                     "PurchaseTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "20"}}]}},
                    {"Id": "6", "Name": "FRE", "Active": True,
                     "SalesTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "21"}}]}},
                    {"Id": "7", "Name": "Empty", "Active": True}],
        "Item": items,
        "Customer": [{"Id": "58", "DisplayName": "Joe", "CompanyName": "Joe's Cafe Bondi", "Active": True,
                      "PrimaryEmailAddr": {"Address": "orders@joescafe.com.au, joe@joescafe.com.au"},
                      "PrimaryPhone": {"FreeFormNumber": "0412 345 678"},
                      "BillAddr": {"Line1": "1 Beach Rd", "City": "Bondi", "CountrySubDivisionCode": "NSW",
                                   "PostalCode": "2026"}}],
    })
    r = client.post("/integrations/sync", headers=auth)
    assert r.status_code == 200
    db.expire_all()
    accs = {a.code: a for a in db.scalars(select(AccountCode))}
    assert accs["79"].name == "4000 Sales" and accs["79"].source == "quickbooks" and accs["80"].account_class == "EXPENSE"
    taxes = {t.code: t for t in db.scalars(select(TaxRate))}
    assert taxes["5"].rate == 10 and taxes["5"].applies_to == "both" and taxes["6"].applies_to == "sales"
    assert "7" not in taxes
    products = db.scalars(select(Product)).all()
    assert len(products) == 1002 and all(p.source == "quickbooks" for p in products)
    p1 = db.scalars(select(Product).where(Product.external_id == "1")).one()
    assert (p1.sku, p1.account_code, p1.tax_type, p1.is_taxable) == ("SKU-1", "79", "5", True)
    customers = client.get("/customers", headers=auth).json()["items"]
    assert customers[0]["name"] == "Joe's Cafe Bondi" and customers[0]["email"] == "orders@joescafe.com.au"
    assert customers[0]["external_id"] == "58" and "Bondi" in customers[0]["address"]
    item_queries = [c for c in fake.calls if "FROM Item" in (c["params"] or {}).get("query", "")]
    assert len(item_queries) == 2 and "STARTPOSITION 1001" in item_queries[1]["params"]["query"]


# ---------------- Export ----------------
def setup_export(client, seeded, fake, db, config=None):
    db.add(AccountCode(tenant_id=1, code="79", name="Sales", source="quickbooks", external_id="79"))
    db.add(TaxRate(tenant_id=1, code="5", name="GST", rate=10, source="quickbooks", external_id="5"))
    db.commit()
    client.post("/destinations", headers=seeded, json={"name": "QuickBooks", "type": "accounting", "config": config or {
        "document_type": "invoice_draft", "default_account_code": "79", "default_tax_type": "5"}})


def test_push_invoice_creates_customer_and_items(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [], "Item": []})
    created_items = []

    def create_item(body, params):
        created_items.append(body)
        return {"Item": {"Id": str(100 + len(created_items)), "Name": body["Name"]}}

    fake.routes[("POST", "/customer")] = lambda body, params: {"Customer": {"Id": "77", "DisplayName": body["DisplayName"]}}
    fake.routes[("POST", "/item")] = create_item
    fake.routes[("POST", "/invoice")] = lambda body, params: {"Invoice": {
        "Id": "145", "DocNumber": body["DocNumber"], "TotalAmt": 88.66, "Balance": 88.66,
        "TxnTaxDetail": {"TotalTax": 8.06}}}
    setup_export(client, seeded, fake, db)

    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success", job
    assert job["external_url"] == "https://app.sandbox.qbo.intuit.com/app/invoice?txnId=145"
    doc = [c for c in fake.calls if c["path"] == "/invoice"][0]["json"]
    assert doc["CustomerRef"] == {"value": "77"} and doc["GlobalTaxCalculation"] == "TaxExcluded"
    assert doc["DocNumber"] == f"OD{draft_order['id']}" and "PO J-55" in doc["PrivateNote"]
    first = doc["Line"][0]
    assert first["Amount"] == 65.0 and first["SalesItemLineDetail"]["Qty"] == 2.0
    assert first["SalesItemLineDetail"]["UnitPrice"] == 32.5
    assert first["SalesItemLineDetail"]["ItemRef"] == {"value": "101"}
    assert first["SalesItemLineDetail"]["TaxCodeRef"] == {"value": "5"}
    assert all(l["Amount"] == round(l["SalesItemLineDetail"]["Qty"] * l["SalesItemLineDetail"]["UnitPrice"], 2)
               for l in doc["Line"])
    assert created_items[0]["IncomeAccountRef"] == {"value": "79"} and created_items[0]["Type"] == "NonInventory"

    db.expire_all()
    inv = db.scalars(select(Invoice).where(Invoice.source == "quickbooks")).one()
    assert inv.external_id == "145" and inv.status == "authorised" and float(inv.total) == 88.66
    assert inv.customer.external_id == "77" and inv.customer.source == "quickbooks"
    # item id product pe yaad
    assert db.scalars(select(Product).where(Product.sku == "TOM-ROMA-10KG")).one().external_id == "101"


def test_push_uses_existing_and_inclusive_gst(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    client.put("/settings/invoice", headers=seeded, json={"invoice_prefix": "INV-", "invoice_next_number": 1001,
                                                          "gst_rate": 10, "prices_include_gst": True,
                                                          "payment_terms_days": 7})
    fake.routes[("GET", "/query")] = query_router({
        "Customer": lambda sql: [{"Id": "58"}] if "Joe\\'s Cafe Bondi" in sql else [],
        "Item": [{"Id": "33", "Name": "x"}]})
    fake.routes[("POST", "/invoice")] = {"Invoice": {"Id": "9", "DocNumber": "OD1", "TotalAmt": 80.6, "Balance": 80.6}}
    setup_export(client, seeded, fake, db)
    assert approve(client, seeded, draft_order)["exports"][0]["status"] == "success"
    doc = [c for c in fake.calls if c["path"] == "/invoice"][0]["json"]
    assert doc["GlobalTaxCalculation"] == "TaxInclusive" and doc["CustomerRef"] == {"value": "58"}
    assert {l["SalesItemLineDetail"]["ItemRef"]["value"] for l in doc["Line"]} == {"33"}
    assert not [c for c in fake.calls if c["path"] in ("/customer", "/item")]


def test_no_income_account_gives_clear_error(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": []})
    client.post("/destinations", headers=seeded, json={"name": "QB", "type": "accounting", "config": {}})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and "default sales account" in job["last_error"]


def test_fault_not_retryable_500_retryable(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": [{"Id": "33"}]})
    fake.routes[("POST", "/invoice")] = (400, {"Fault": {"Error": [
        {"Message": "Invalid Reference Id", "Detail": "Invalid Reference Id : Something's wrong"}], "type": "ValidationFault"}})
    setup_export(client, seeded, fake, db)
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and "Invalid Reference Id" in job["last_error"] and job["next_retry_at"] is None

    fake.routes[("POST", "/invoice")] = (500, {})
    assert client.post(f"/exports/{job['id']}/retry", headers=seeded).status_code == 200
    jobs = client.get(f"/orders/{draft_order['id']}", headers=seeded).json()["exports"]
    assert jobs[0]["status"] == "failed" and jobs[0]["next_retry_at"] is not None


def test_estimate(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": [{"Id": "33"}]})
    fake.routes[("POST", "/estimate")] = {"Estimate": {"Id": "e1", "DocNumber": "OD1"}}
    setup_export(client, seeded, fake, db, {"document_type": "quote"})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success" and job["external_id"] == "e1"
    doc = [c for c in fake.calls if c["path"] == "/estimate"][0]["json"]
    assert "DueDate" not in doc
    db.expire_all()
    assert db.scalars(select(Invoice).where(Invoice.source == "quickbooks")).all() == []


def test_send_email_and_refresh_to_paid(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": [{"Id": "33"}]})
    fake.routes[("POST", "/invoice")] = {"Invoice": {"Id": "145", "DocNumber": "OD1", "TotalAmt": 88.66, "Balance": 88.66}}
    fake.routes[("POST", "/invoice/145/send")] = {"Invoice": {"Id": "145", "DocNumber": "OD1", "TotalAmt": 88.66,
                                                              "Balance": 88.66, "EmailStatus": "EmailSent"}}
    setup_export(client, seeded, fake, db, {"document_type": "invoice_authorised", "default_account_code": "79",
                                            "send_email": True})
    assert approve(client, seeded, draft_order)["exports"][0]["status"] == "success"
    send_call = [c for c in fake.calls if c["path"] == "/invoice/145/send"][0]
    assert send_call["params"]["sendTo"] == "orders@joescafe.com.au"
    db.expire_all()
    inv = db.scalars(select(Invoice).where(Invoice.source == "quickbooks")).one()
    assert inv.status == "sent"

    fake.routes[("GET", "/invoice/145")] = {"Invoice": {"Id": "145", "DocNumber": "OD1", "TotalAmt": 88.66,
                                                        "Balance": 0, "TxnTaxDetail": {"TotalTax": 8.06}}}
    r = client.post(f"/invoices/{inv.id}/refresh", headers=seeded).json()
    assert r["status"] == "paid" and r["amount_paid"] == 88.66 and r["gst"] == 8.06
    assert r["external_url"].endswith("txnId=145")


# ---------------- Webhook ----------------
def test_webhook_signature_and_routing(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": [{"Id": "33"}]})
    fake.routes[("POST", "/invoice")] = {"Invoice": {"Id": "145", "DocNumber": "OD1", "TotalAmt": 88.66, "Balance": 88.66}}
    setup_export(client, seeded, fake, db)
    approve(client, seeded, draft_order)
    register(client, "other@ocean.com.au", "Ocean Seafood Co")       # doosra client - chhua nahi jaana chahiye

    body = json.dumps({"eventNotifications": [{"realmId": "9130", "dataChangeEvent": {"entities": [
        {"name": "Invoice", "id": "145", "operation": "Update"}]}}]}).encode()
    assert client.post("/webhooks/quickbooks", content=body, headers={"intuit-signature": "bad"}).status_code == 401

    fake.routes[("GET", "/invoice/145")] = {"Invoice": {"Id": "145", "TotalAmt": 88.66, "Balance": 0}}
    sig = base64.b64encode(hmac.new(b"qbo-verifier", body, hashlib.sha256).digest()).decode()
    assert client.post("/webhooks/quickbooks", content=body, headers={"intuit-signature": sig}).status_code == 200
    db.rollback()
    db.expire_all()
    assert db.scalars(select(Invoice).where(Invoice.external_id == "145")).one().status == "paid"
    assert [c for c in fake.calls if c["path"] == "/invoice/145"]
