"""MYOB Business / AccountRight connector: connect + company file, sync, invoice export, status."""
import base64
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.models import AccountCode, Connection, Customer, Invoice, Product
from app.modules.connectors.base import TokenSet
from app.modules.connectors.myob import MyobProvider

CF = "https://arl2.api.myob.com/accountright/cf-1"
FILES_ROOT = "/"      # https://api.myob.com/accountright/ (pending connection pe api_base = root)


def approve(client, h, order):
    return client.post(f"/orders/{order['id']}/approve", headers=h).json()


def myob(fake_accounting):
    return fake_accounting("myob", org_id="cf-1", org_name="Fresh MYOB", extra={"cf_uri": CF})


def add_destination(client, h, **config):
    r = client.post("/destinations", headers=h, json={"name": "MYOB", "type": "accounting", "config": config})
    assert r.status_code == 200, r.text


def chart_routes(fake):
    fake.routes[("GET", "/GeneralLedger/Account")] = {"Items": [
        {"UID": "acc-hdr", "DisplayID": "4-0000", "Name": "Income", "IsHeader": True, "Classification": "Income"},
        {"UID": "acc-sales", "DisplayID": "4-1100", "Name": "Sales", "Classification": "Income", "IsActive": True},
        {"UID": "acc-cogs", "DisplayID": "5-1000", "Name": "Purchases", "Classification": "CostOfSales"},
    ], "NextPageLink": None}
    fake.routes[("GET", "/GeneralLedger/TaxCode")] = {"Items": [
        {"UID": "tax-gst", "Code": "GST", "Description": "Goods & Services Tax", "Rate": 10},
        {"UID": "tax-fre", "Code": "FRE", "Description": "GST Free", "Rate": 0},
    ]}


# ---------------- Connect + company file ----------------
def _connect(client, h, monkeypatch, fake_http, files):
    monkeypatch.setattr(MyobProvider, "exchange_code", lambda self, code, params: TokenSet("at", "rt", 1200))
    fake_http.routes[("GET", FILES_ROOT)] = [{"Id": f[0], "Name": f[1], "Uri": f"https://arl2.api.myob.com/accountright/{f[0]}"}
                                             for f in files]
    url = client.get("/integrations/myob/connect-url", headers=h).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    assert "secure.myob.com/oauth2/account/authorize" in url
    return client.get(f"/integrations/myob/callback?code=abc&state={state}", follow_redirects=False)


def test_connect_choose_company_file_with_login(client, auth, monkeypatch, fake_http, db):
    r = _connect(client, auth, monkeypatch, fake_http, [("cf-1", "Fresh Farms"), ("cf-2", "Fresh Farms Old")])
    assert "choose_org=myob" in r.headers["location"]
    orgs = client.get("/integrations/orgs", headers=auth).json()
    assert orgs["login_fields"] == ["username", "password"] and len(orgs["orgs"]) == 2

    fake_http.routes[("GET", "https://arl2.api.myob.com/accountright/cf-2/Company")] = {"CompanyName": "Fresh"}
    conn = client.post("/integrations/org", headers=auth,
                       json={"org_id": "cf-2", "username": "Administrator", "password": "pw"}).json()
    assert conn["status"] == "connected" and conn["org_name"] == "Fresh Farms Old"
    check = [c for c in fake_http.calls if c["url"].endswith("cf-2/Company")][0]
    assert check["headers"]["x-myobapi-cftoken"] == base64.b64encode(b"Administrator:pw").decode()
    assert check["headers"]["x-myobapi-key"] == "myob-key" and check["headers"]["x-myobapi-version"] == "v2"

    stored = db.scalars(select(Connection)).one()
    assert stored.extra["cf_uri"].endswith("/cf-2") and "orgs" not in stored.extra
    # Baad ki calls bhi cftoken ke saath (encrypted credentials connection mein)
    client.post("/integrations/sync", headers=auth)
    later = [c for c in fake_http.calls if c["path"] == "/GeneralLedger/Account"][-1]
    assert later["headers"]["x-myobapi-cftoken"] == base64.b64encode(b"Administrator:pw").decode()


def test_wrong_company_file_login(client, auth, monkeypatch, fake_http):
    _connect(client, auth, monkeypatch, fake_http, [("cf-1", "A"), ("cf-2", "B")])
    fake_http.routes[("GET", "https://arl2.api.myob.com/accountright/cf-1/Company")] = (401, {})
    r = client.post("/integrations/org", headers=auth, json={"org_id": "cf-1", "username": "x", "password": "bad"})
    assert r.status_code == 400 and "login failed" in r.json()["detail"]


def test_single_company_file_auto_activates(client, auth, monkeypatch, fake_http):
    fake_http.routes[("GET", "https://arl2.api.myob.com/accountright/cf-1/Company")] = {}
    r = _connect(client, auth, monkeypatch, fake_http, [("cf-1", "Fresh Farms")])
    assert "connected=myob" in r.headers["location"]
    st = client.get("/integrations", headers=auth).json()["connection"]
    assert st["provider"] == "myob" and st["status"] == "connected"


# ---------------- Sync ----------------
def test_sync_with_pagination(client, auth, fake_accounting, db):
    fake = myob(fake_accounting)
    chart_routes(fake)

    def items(body, params):
        if params:   # pehla page
            assert params["$filter"] == "IsActive eq true"
            return {"Items": [
                {"UID": "it-1", "Number": "TOM-1", "Name": "Tomatoes", "IsSold": True,
                 "SellingDetails": {"BaseSellingPrice": 30, "SellingUnitOfMeasure": "box", "TaxCode": {"Code": "GST"}},
                 "IncomeAccount": {"DisplayID": "4-1100"}},
                {"UID": "it-x", "Number": "BUY-1", "Name": "Packaging", "IsSold": False},
            ], "NextPageLink": f"{CF}/Inventory/Item?$top=1000&$skip=1000"}
        return {"Items": [{"UID": "it-2", "Number": "BRD-1", "Name": "Bread", "IsSold": True,
                           "SellingDetails": {"BaseSellingPrice": 5, "TaxCode": {"Code": "FRE"}}}], "NextPageLink": None}

    fake.routes[("GET", "/Inventory/Item")] = items
    fake.routes[("GET", "/Contact/Customer")] = {"Items": [
        {"UID": "cu-1", "DisplayID": "JOES01", "CompanyName": "Joe's Cafe", "IsActive": True,
         "Addresses": [{"Email": "joe@cafe.com", "Phone1": "0400", "Street": "1 Beach Rd", "City": "Bondi"}],
         "SellingDetails": {"ABN": "12 345"}},
        {"UID": "cu-2", "DisplayID": "*None", "IsIndividual": True, "FirstName": "Sam", "LastName": "Lee"},
    ]}
    assert client.post("/integrations/sync", headers=auth).status_code == 200

    products = {p["sku"]: p for p in client.get("/products", headers=auth).json()["items"]}
    assert set(products) == {"TOM-1", "BRD-1"}
    assert products["TOM-1"]["source"] == "myob" and products["TOM-1"]["external_id"] == "it-1"
    assert products["TOM-1"]["account_code"] == "4-1100" and products["BRD-1"]["is_taxable"] is False
    customers = {c["name"]: c for c in client.get("/customers", headers=auth).json()["items"]}
    assert customers["Joe's Cafe"]["code"] == "JOES01" and customers["Joe's Cafe"]["abn"] == "12 345"
    assert customers["Sam Lee"]["code"] in (None, "")
    accounts = {a["code"]: a for a in client.get("/accounting/accounts", headers=auth).json()}
    assert "4-0000" not in accounts and accounts["5-1000"]["account_class"] == "EXPENSE"
    sales = db.scalars(select(AccountCode).where(AccountCode.code == "4-1100")).one()
    assert sales.external_id == "acc-sales" and sales.source == "myob"


# ---------------- Export ----------------
def _created(body, params):
    return {"UID": "inv-uid-1", "Number": body["Number"], "Status": "Open", "TotalAmount": 88.66,
            "TotalTax": 8.06, "Subtotal": 80.6, "BalanceDueAmount": 88.66}


def _prepare(client, seeded, fake):
    chart_routes(fake)
    client.post("/accounting/import", headers=seeded)       # account/tax UIDs (mapping)
    fake.routes[("GET", "/Contact/Customer")] = lambda body, params: {
        "Items": [{"UID": "cu-joe"}] if "DisplayID eq 'JOES01'" in params["$filter"] else []}


def test_service_layout_when_products_not_in_myob(client, seeded, draft_order, fake_accounting, db):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    fake.routes[("POST", "/Sale/Invoice/Service")] = _created
    add_destination(client, seeded, document_type="invoice_authorised", default_account_code="4-1100",
                    send_email=True)
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success", job
    call = [c for c in fake.calls if c["path"].startswith("/Sale/Invoice/Service")][0]
    body = call["json"]
    assert call["path"] == "/Sale/Invoice/Service?returnBody=true"
    assert body["Number"] == f"OD{draft_order['id']}" and body["Customer"] == {"UID": "cu-joe"}
    assert body["CustomerPurchaseOrderNumber"] == "J-55" and body["InvoiceDeliveryStatus"] == "ToBeEmailed"
    line = body["Lines"][0]
    assert line["Account"] == {"UID": "acc-sales"} and line["TaxCode"] == {"UID": "tax-gst"} and line["Total"] == 65.0
    inv = db.query(Invoice).filter_by(source="myob").one()
    assert inv.external_id == "inv-uid-1" and inv.status == "authorised" and float(inv.total) == 88.66
    assert db.query(Customer).filter_by(code="JOES01").one().external_id == "cu-joe"


def test_item_layout_when_all_products_are_myob_items(client, seeded, draft_order, fake_accounting, db):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    for p in db.scalars(select(Product)):
        p.source, p.external_id = "myob", f"item-{p.sku}"
    db.commit()
    fake.routes[("POST", "/Sale/Invoice/Item")] = _created
    add_destination(client, seeded, document_type="invoice_authorised")
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success", job
    body = [c for c in fake.calls if c["path"].startswith("/Sale/Invoice/Item")][0]["json"]
    assert body["Lines"][0]["Item"] == {"UID": "item-TOM-ROMA-10KG"} and body["Lines"][0]["ShipQuantity"] == 2.0
    assert "Account" not in body["Lines"][0]


def test_missing_sales_account(client, seeded, draft_order, fake_accounting):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    add_destination(client, seeded, document_type="invoice_authorised")
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and "sales account" in job["last_error"]


def test_sales_order_and_new_customer(client, seeded, draft_order, fake_accounting, db):
    fake = myob(fake_accounting)
    chart_routes(fake)
    client.post("/accounting/import", headers=seeded)
    fake.routes[("GET", "/Contact/Customer")] = {"Items": []}
    fake.routes[("POST", "/Contact/Customer")] = {"UID": "cu-new"}
    fake.routes[("POST", "/Sale/Order/Service")] = {"UID": "ord-1", "Number": "OD1"}
    add_destination(client, seeded, document_type="sales_order", default_account_code="4-1100")
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success" and job["external_id"] == "ord-1"
    new_cust = [c for c in fake.calls if c["path"].startswith("/Contact/Customer") and c["method"] == "POST"][0]["json"]
    assert new_cust["DisplayID"] == "JOES01" and new_cust["SellingDetails"]["TaxCode"] == {"UID": "tax-gst"}
    assert db.query(Invoice).filter_by(source="myob").count() == 0   # sales order = invoice row nahi


def test_myob_validation_error(client, seeded, draft_order, fake_accounting):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    fake.routes[("POST", "/Sale/Invoice/Service")] = (400, {"Errors": [
        {"Name": "ValidationError", "Message": "Number already exists", "AdditionalDetails": "Number"}]})
    add_destination(client, seeded, document_type="invoice_authorised", default_account_code="4-1100")
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and job["last_error"] == "MYOB: Number already exists (Number)"
    assert job["next_retry_at"] is None


def test_refresh_closed_invoice_is_paid(client, seeded, draft_order, fake_accounting):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    fake.routes[("POST", "/Sale/Invoice/Service")] = _created
    add_destination(client, seeded, document_type="invoice_draft", default_account_code="4-1100")  # -> Invoice
    approve(client, seeded, draft_order)
    inv = client.get("/invoices", headers=seeded).json()["items"][0]
    assert inv["source"] == "myob" and inv["external_url"] is None
    fake.routes[("GET", "/Sale/Invoice/inv-uid-1")] = {"UID": "inv-uid-1", "Number": "OD1", "Status": "Closed",
                                                      "TotalAmount": 88.66, "BalanceDueAmount": 0}
    r = client.post(f"/invoices/{inv['id']}/refresh", headers=seeded).json()
    assert r["status"] == "paid" and r["amount_paid"] == 88.66
