"""Suppliers, purchase orders, receiving (GRN), weighted average cost, reorder suggestions."""
from datetime import date, timedelta

from app.core import mailer
from tests.conftest import register


def products(client, h, n=3):
    return client.get(f"/products?page_size={n}&sort=sku", headers=h).json()["items"]


def supplier(client, h, **kw):
    body = {"name": "Sydney Produce Markets", "email": "orders@spm.com.au", "contact_name": "Tony",
            "payment_terms_days": 14, **kw}
    r = client.post("/suppliers", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def track(client, h, pid, qty=None, reorder=None):
    client.post("/stock/track", headers=h, json={"product_id": pid, "track": True, "reorder_level": reorder})
    if qty is not None:
        client.post("/stock/adjust", headers=h, json={"product_id": pid, "mode": "set", "quantity": qty, "reason": "count"})


def stock_of(client, h, pid):
    return next(i for i in client.get("/stock?page_size=500", headers=h).json()["items"] if i["id"] == pid)


def make_po(client, h, sid, lines, **kw):
    r = client.post("/purchase-orders", headers=h, json={"supplier_id": sid, "lines": lines, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- suppliers ----------------
def test_supplier_crud_and_products(client, seeded):
    s = supplier(client, seeded, email="Orders@SPM.com.au", currency="aud")
    assert s["email"] == "orders@spm.com.au" and s["currency"] == "AUD"
    p1, p2 = products(client, seeded, 2)
    link = client.post(f"/suppliers/{s['id']}/products", headers=seeded, json={
        "product_id": p1["id"], "supplier_sku": "SPM-1", "unit_cost": 18.5, "lead_time_days": 2, "is_preferred": True}).json()
    assert link["unit_cost"] == 18.5 and link["is_preferred"]
    # doosra supplier preferred bana to pehle ka hat jaata hai
    s2 = supplier(client, seeded, name="Fresh Direct")
    client.post(f"/suppliers/{s2['id']}/products", headers=seeded, json={"product_id": p1["id"], "is_preferred": True})
    links = client.get(f"/products/{p1['id']}/suppliers", headers=seeded).json()
    assert {l["supplier_name"]: l["is_preferred"] for l in links} == {"Sydney Produce Markets": False, "Fresh Direct": True}

    lst = client.get("/suppliers?sort=name", headers=seeded).json()["items"]
    assert [x["name"] for x in lst] == ["Fresh Direct", "Sydney Produce Markets"] and lst[1]["product_count"] == 1
    client.put(f"/suppliers/{s['id']}", headers=seeded, json={"name": "SPM", "email": "a@spm.com.au"})
    assert client.get(f"/suppliers/{s['id']}", headers=seeded).json()["name"] == "SPM"
    client.post(f"/suppliers/{s['id']}/status", headers=seeded, json={"is_active": False})
    assert [x["name"] for x in client.get("/suppliers", headers=seeded).json()["items"]] == ["Fresh Direct"]
    assert client.delete(f"/suppliers/{s2['id']}", headers=seeded).json()["ok"]
    assert client.delete(f"/suppliers/{s2['id']}/permanent", headers=seeded).json()["ok"]   # koi PO nahi -> hamesha ke liye
    assert client.get("/suppliers?status=deleted", headers=seeded).json()["items"] == []


def test_suppliers_isolated(client, seeded):
    s = supplier(client, seeded)
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    assert client.get("/suppliers", headers=other).json()["items"] == []
    assert client.get(f"/suppliers/{s['id']}", headers=other).status_code == 404


# ---------------- purchase orders ----------------
def test_po_create_defaults_and_totals(client, seeded):
    s = supplier(client, seeded)
    p1, p2 = products(client, seeded, 2)
    client.post(f"/suppliers/{s['id']}/products", headers=seeded, json={"product_id": p1["id"], "supplier_sku": "SPM-1", "unit_cost": 10})
    po = make_po(client, seeded, s["id"], [
        {"product_id": p1["id"], "quantity": 5},                                   # supplier cost 10
        {"product_id": p2["id"], "quantity": 2, "unit_cost": 7.5, "tax_rate": 0},
        {"description": "Freight", "quantity": 1, "unit_cost": 20}],
        expected_date=str(date.today() + timedelta(days=3)), notes="Back door")
    assert po["number"] == "PO-1001" and po["status"] == "draft"
    l1, l2, l3 = po["lines"]
    assert l1["unit_cost"] == 10 and l1["supplier_sku"] == "SPM-1" and l2["tax_rate"] == 0
    gst1 = 0 if not p1["is_taxable"] else 5.0
    assert po["subtotal"] == 85 and po["gst"] == gst1 + 2.0 and po["total"] == 85 + gst1 + 2.0
    # draft edit
    po = client.put(f"/purchase-orders/{po['id']}", headers=seeded, json={"supplier_id": s["id"], "lines": [
        {"product_id": p1["id"], "quantity": 6}]}).json()
    assert len(po["lines"]) == 1 and po["subtotal"] == 60
    assert make_po(client, seeded, s["id"], [{"product_id": p1["id"], "quantity": 1}])["number"] == "PO-1002"
    assert client.post("/purchase-orders", headers=seeded, json={"supplier_id": s["id"], "lines": []}).status_code == 400
    assert client.post("/purchase-orders", headers=seeded, json={"supplier_id": s["id"], "lines": [{"quantity": 1}]}).status_code == 400


def test_send_po_email_pdf(client, seeded):
    s = supplier(client, seeded, cc_emails="accounts@spm.com.au")
    p = products(client, seeded, 1)[0]
    po = make_po(client, seeded, s["id"], [{"product_id": p["id"], "quantity": 2, "unit_cost": 5}])
    pdf = client.get(f"/purchase-orders/{po['id']}/pdf", headers=seeded)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    po = client.post(f"/purchase-orders/{po['id']}/send", headers=seeded, json={}).json()
    assert po["status"] == "sent" and po["sent_to"] == "orders@spm.com.au, accounts@spm.com.au"
    mail = mailer.SENT[-1]
    assert mail["to"] == "orders@spm.com.au" and mail["attachments"] == ["PO-1001.pdf"] and "via OrderDraft" in mail["from"]
    assert client.put(f"/purchase-orders/{po['id']}", headers=seeded, json={"supplier_id": s["id"], "lines": [
        {"product_id": p["id"], "quantity": 1}]}).status_code == 400                     # sent = edit nahi
    assert client.delete(f"/purchase-orders/{po['id']}", headers=seeded).status_code == 400
    nomail = supplier(client, seeded, name="No Email", email=None)
    po2 = make_po(client, seeded, nomail["id"], [{"product_id": p["id"], "quantity": 1}])
    assert client.post(f"/purchase-orders/{po2['id']}/send", headers=seeded, json={}).status_code == 400


def test_receive_partial_then_full_updates_stock_and_avg_cost(client, seeded):
    s = supplier(client, seeded)
    p1, p2 = products(client, seeded, 2)
    track(client, seeded, p1["id"], qty=10)
    # p1 ka pehla avg cost: bina PO receive (10 qty @ 4 -> on hand 10 pehle se, avg pehli baar = 4)
    client.post("/stock-receipts", headers=seeded, json={"supplier_id": s["id"], "lines": [
        {"product_id": p1["id"], "quantity": 10, "unit_cost": 4}]})
    st = stock_of(client, seeded, p1["id"])
    assert st["quantity_on_hand"] == 20 and st["avg_cost"] == 4

    po = make_po(client, seeded, s["id"], [{"product_id": p1["id"], "quantity": 20, "unit_cost": 7},
                                           {"product_id": p2["id"], "quantity": 4, "unit_cost": 3}])
    client.post(f"/purchase-orders/{po['id']}/send", headers=seeded, json={})
    assert stock_of(client, seeded, p1["id"])["on_order"] == 20
    l1, l2 = po["lines"]
    po = client.post(f"/purchase-orders/{po['id']}/receive", headers=seeded, json={
        "reference": "DOCKET 55", "lines": [{"po_line_id": l1["id"], "quantity": 10}, {"po_line_id": l2["id"], "quantity": 0}]}).json()
    assert po["status"] == "partial" and po["lines"][0]["quantity_received"] == 10 and po["received_pct"] == 42
    st = stock_of(client, seeded, p1["id"])
    # (20 x 4 + 10 x 7) / 30 = 5.0
    assert st["quantity_on_hand"] == 30 and st["avg_cost"] == 5 and st["on_order"] == 10
    log = client.get(f"/stock/movements?product_id={p1['id']}", headers=seeded).json()["items"][0]
    assert log["reason"] == "receive" and log["ref_type"] == "receipt" and "PO-1001" in log["note"]

    po = client.post(f"/purchase-orders/{po['id']}/receive", headers=seeded, json={"lines": [
        {"po_line_id": l1["id"], "quantity": 10, "unit_cost": 8}, {"po_line_id": l2["id"], "quantity": 4}]}).json()
    assert po["status"] == "received" and len(po["receipts"]) == 2 and po["received_at"]
    st = stock_of(client, seeded, p1["id"])
    # (30 x 5 + 10 x 8) / 40 = 5.75
    assert st["quantity_on_hand"] == 40 and st["avg_cost"] == 5.75 and st["on_order"] == 0
    # p2 tracked nahi: stock movement nahi, par avg cost (= receipt cost) set
    assert client.get(f"/stock/movements?product_id={p2['id']}", headers=seeded).json()["items"] == []
    assert next(p for p in products(client, seeded, 2) if p["id"] == p2["id"])["avg_cost"] == 3
    # receive ke baad supplier link pe last cost
    link = next(l for l in client.get(f"/products/{p1['id']}/suppliers", headers=seeded).json())
    assert link["last_cost"] == 8
    assert client.post(f"/purchase-orders/{po['id']}/receive", headers=seeded, json={"lines": [
        {"po_line_id": l1["id"], "quantity": 1}]}).status_code == 400                    # received PO pe aur nahi
    rec = client.get(f"/stock-receipts?po_id={po['id']}", headers=seeded).json()["items"]
    assert [r["number"] for r in rec] == ["GRN-1003", "GRN-1002"] and rec[1]["reference"] == "DOCKET 55"


def test_negative_stock_avg_cost_resets(client, seeded):
    s = supplier(client, seeded)
    p = products(client, seeded, 1)[0]
    track(client, seeded, p["id"], qty=0)
    c = client.get("/customers?page_size=1", headers=seeded).json()["items"][0]
    client.post("/orders/manual", headers=seeded, json={"customer_id": c["id"], "approve": True,
                                                       "lines": [{"product_id": p["id"], "quantity": 5}]})
    assert stock_of(client, seeded, p["id"])["quantity_on_hand"] == -5                     # negative allowed
    client.post("/stock-receipts", headers=seeded, json={"supplier_id": s["id"], "lines": [
        {"product_id": p["id"], "quantity": 10, "unit_cost": 6}]})
    st = stock_of(client, seeded, p["id"])
    assert st["quantity_on_hand"] == 5 and st["avg_cost"] == 6


def test_close_cancel_reopen(client, seeded):
    s = supplier(client, seeded)
    p = products(client, seeded, 1)[0]
    po = make_po(client, seeded, s["id"], [{"product_id": p["id"], "quantity": 10, "unit_cost": 2}])
    assert client.post(f"/purchase-orders/{po['id']}/status", headers=seeded, json={"action": "close"}).status_code == 400
    po = client.post(f"/purchase-orders/{po['id']}/status", headers=seeded, json={"action": "mark_sent"}).json()
    assert po["status"] == "sent"
    client.post(f"/purchase-orders/{po['id']}/receive", headers=seeded, json={"lines": [{"po_line_id": po["lines"][0]["id"], "quantity": 4}]})
    assert client.post(f"/purchase-orders/{po['id']}/status", headers=seeded, json={"action": "cancel"}).status_code == 400
    po = client.post(f"/purchase-orders/{po['id']}/status", headers=seeded, json={"action": "close"}).json()
    assert po["status"] == "closed"
    po = client.post(f"/purchase-orders/{po['id']}/status", headers=seeded, json={"action": "reopen"}).json()
    assert po["status"] == "partial"
    po2 = make_po(client, seeded, s["id"], [{"product_id": p["id"], "quantity": 1}])
    assert client.post(f"/purchase-orders/{po2['id']}/status", headers=seeded, json={"action": "cancel"}).json()["status"] == "cancelled"
    # Trash: POs wala supplier bhi Trash mein ja sakta hai, par permanently delete nahi
    assert client.delete(f"/suppliers/{s['id']}/permanent", headers=seeded).status_code == 400   # pehle Trash
    assert client.delete(f"/suppliers/{s['id']}", headers=seeded).json()["ok"]
    trash = client.get("/suppliers?status=deleted", headers=seeded).json()["items"]
    assert [x["id"] for x in trash] == [s["id"]] and trash[0]["deleted_at"]
    assert s["id"] not in [x["id"] for x in client.get("/suppliers?status=all", headers=seeded).json()["items"]]
    assert client.put(f"/suppliers/{s['id']}", headers=seeded, json={"name": "X"}).status_code == 400
    r = client.delete(f"/suppliers/{s['id']}/permanent", headers=seeded)
    assert r.status_code == 400 and "purchase order" in r.json()["detail"]                  # POs hain
    assert client.post(f"/suppliers/{s['id']}/restore", headers=seeded).json()["is_active"] is True
    assert client.get("/suppliers?status=deleted", headers=seeded).json()["items"] == []


def test_reorder_suggestions_to_draft_pos(client, seeded):
    a = supplier(client, seeded, name="A Supplies")
    b = supplier(client, seeded, name="B Supplies")
    p1, p2, p3 = products(client, seeded, 3)
    track(client, seeded, p1["id"], qty=2, reorder=10)       # low
    track(client, seeded, p2["id"], qty=50, reorder=10)      # ok
    track(client, seeded, p3["id"], qty=0, reorder=4)        # out
    client.post(f"/suppliers/{a['id']}/products", headers=seeded, json={"product_id": p1["id"], "unit_cost": 3, "is_preferred": True,
                                                                         "min_order_qty": 24})
    client.post(f"/suppliers/{b['id']}/products", headers=seeded, json={"product_id": p3["id"], "unit_cost": 9})
    sug = {x["product_id"]: x for x in client.get("/purchase-orders/suggestions", headers=seeded).json()}
    assert set(sug) == {p1["id"], p3["id"]}
    assert sug[p1["id"]]["supplier_name"] == "A Supplies" and sug[p1["id"]]["suggested_qty"] == 24   # min order qty
    assert sug[p3["id"]]["suggested_qty"] == 8 and sug[p3["id"]]["supplier_id"] == b["id"]
    created = client.post("/purchase-orders/from-suggestions", headers=seeded, json={"items": [
        {"product_id": p1["id"], "supplier_id": a["id"], "quantity": 24},
        {"product_id": p3["id"], "supplier_id": b["id"], "quantity": 8}]}).json()
    assert len(created) == 2
    # ab on order hai -> suggestion se hat gaya
    assert client.get("/purchase-orders/suggestions", headers=seeded).json() == []
    lst = client.get("/purchase-orders?status=open&sort=supplier", headers=seeded).json()
    assert [x["supplier_name"] for x in lst["items"]] == ["A Supplies", "B Supplies"] and lst["counts"]["draft"] == 2


def test_po_numbering_settings(client, seeded):
    client.put("/settings/stock", headers=seeded, json={"po_prefix": "FF-PO-", "po_next_number": 7})
    s = supplier(client, seeded)
    p = products(client, seeded, 1)[0]
    assert make_po(client, seeded, s["id"], [{"product_id": p["id"], "quantity": 1}])["number"] == "FF-PO-7"
    client.put("/settings/stock", headers=seeded, json={"po_next_number": 7})                     # peeche kiya
    assert make_po(client, seeded, s["id"], [{"product_id": p["id"], "quantity": 1}])["number"] == "FF-PO-8"
    assert client.get("/settings", headers=seeded).json()["stock"]["deduct_on_approve"] is True   # baaki setting same


def test_supplier_terms_drive_po(client, seeded):
    """Supplier product ka cost, supplier code, min order aur lead time PO mein kaam aate hain."""
    s = supplier(client, seeded)
    p1, p2 = products(client, seeded, 2)
    client.post(f"/suppliers/{s['id']}/products", headers=seeded, json={
        "product_id": p1["id"], "supplier_sku": "SPM-1", "unit_cost": 9.25, "min_order_qty": 24, "lead_time_days": 3})
    client.post(f"/suppliers/{s['id']}/products", headers=seeded, json={"product_id": p2["id"], "lead_time_days": 5})
    po = make_po(client, seeded, s["id"], [{"product_id": p1["id"], "quantity": 10}, {"product_id": p2["id"], "quantity": 2}])
    l1 = po["lines"][0]
    assert l1["unit_cost"] == 9.25 and l1["supplier_sku"] == "SPM-1"
    assert l1["min_order_qty"] == 24 and l1["below_min"] is True and po["lines"][1]["below_min"] is False
    assert po["expected_date"] == str(date.today() + timedelta(days=5))       # sabse lamba lead time
    po2 = make_po(client, seeded, s["id"], [{"product_id": p1["id"], "quantity": 24}], expected_date=str(date.today()))
    assert po2["lines"][0]["below_min"] is False and po2["expected_date"] == str(date.today())   # diya hua date rehta hai


def test_import_suppliers_csv(client, auth):
    from tests.test_imports import upload
    csv = ("ContactName,AccountNumber,EmailAddress,FirstName,PhoneNumber,POAddressLine1,POCity,POPostalCode,TaxNumber,Terms\n"
           "Sydney Produce,SPM,Orders@SPM.com.au,Tony,02 9999 1111,1 Market St,Flemington,2129,111 222,14\n"
           "Fresh Direct,,fd@fresh.com.au,,,,,,,abc\n,NONAME,x@x.com,,,,,,,\n").encode()
    body = upload(client, auth, "suppliers", csv, name="suppliers.csv")
    assert body["mapping"]["ContactName"] == "name" and body["mapping"]["POCity"] == "city"
    r = client.post("/imports/suppliers/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]}).json()
    assert r["created"] == 2 and r["skipped"] == 1 and any("payment terms" in e for e in r["errors"])
    sups = {x["name"]: x for x in client.get("/suppliers", headers=auth).json()["items"]}
    spm = client.get(f"/suppliers/{sups['Sydney Produce']['id']}", headers=auth).json()
    assert spm["email"] == "orders@spm.com.au" and spm["code"] == "SPM" and spm["contact_name"] == "Tony"
    assert spm["address"] == "1 Market St, Flemington 2129" and spm["payment_terms_days"] == 14 and spm["abn"] == "111 222"
    # dobara import = update, duplicate nahi
    body = upload(client, auth, "suppliers", csv, name="suppliers.csv")
    r = client.post("/imports/suppliers/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]}).json()
    assert r["created"] == 0 and r["updated"] == 2


def test_import_suppliers_from_xero(client, auth, fake_xero):
    def contacts(body, params):
        assert params.get("where") == "IsSupplier==true"
        return {"Contacts": [{"ContactID": "s1", "Name": "Market Co", "EmailAddress": "m@market.com", "AccountNumber": "MKT",
                              "FirstName": "Ann", "LastName": "Lee", "TaxNumber": "999",
                              "PaymentTerms": {"Bills": {"Day": 30, "Type": "DAYSAFTERBILLDATE"}},
                              "Addresses": [{"AddressType": "POBOX", "AddressLine1": "PO Box 9", "City": "Sydney", "PostalCode": "2000"}],
                              "Phones": [{"PhoneType": "DEFAULT", "PhoneNumber": "1234"}], "ContactStatus": "ACTIVE"}]}
    fake_xero.routes[("GET", "/Contacts")] = contacts
    r = client.post("/suppliers/import-from-accounting", headers=auth).json()
    assert r["created"] == 1 and r["label"] == "Xero"
    s = client.get("/suppliers", headers=auth).json()["items"][0]
    d = client.get(f"/suppliers/{s['id']}", headers=auth).json()
    assert d["name"] == "Market Co" and d["contact_name"] == "Ann Lee" and d["payment_terms_days"] == 30
    assert d["address"] == "PO Box 9, Sydney 2000" and d["source"] == "xero" and d["external_id"] == "s1"
    assert client.post("/suppliers/import-from-accounting", headers=auth).json()["updated"] == 1


def test_import_suppliers_quickbooks_and_myob_mapping():
    from app.modules.connectors.myob import MyobProvider
    from app.modules.connectors.quickbooks import QuickBooksProvider

    qbo = QuickBooksProvider()
    qbo.query = lambda api, entity, where="": [{"Id": 5, "DisplayName": "Veg Co", "PrimaryEmailAddr": {"Address": "a@veg.com, b@veg.com"},
                                                "PrimaryPhone": {"FreeFormNumber": "555"}, "AcctNum": "C-77",
                                                "BillAddr": {"Line1": "2 Farm Rd", "City": "Dubbo", "PostalCode": "2830"}}] \
        if entity == "Vendor" else []
    r = qbo.fetch_suppliers(None, None)[0]
    assert r["name"] == "Veg Co" and r["email"] == "a@veg.com" and r["cc_emails"] == "b@veg.com"
    assert r["account_number"] == "C-77" and r["address"] == "2 Farm Rd, Dubbo 2830" and r["external_id"] == "5"

    myob = MyobProvider()
    myob._list = lambda api, path, filters=None: [{"UID": "u1", "CompanyName": "Fruit Pty", "DisplayID": "FRU", "IsActive": True,
                                                   "BuyingDetails": {"ABN": "12 345"},
                                                   "Addresses": [{"Location": 1, "Email": "f@fruit.com", "Phone1": "777",
                                                                  "Street": "3 Orchard Ln", "City": "Orange", "PostCode": "2800"}]}] \
        if path == "/Contact/Supplier" else []
    r = myob.fetch_suppliers(None, None)[0]
    assert r["name"] == "Fruit Pty" and r["code"] == "FRU" and r["abn"] == "12 345" and r["email"] == "f@fruit.com"
    assert r["address"] == "3 Orchard Ln, Orange 2800"


def test_receive_same_product_on_two_lines_and_new_supplier_link(client, seeded):
    """Bug: ek product PO ki do lines mein + supplier link pehle se nahi -> link do baar insert -> 500."""
    s = supplier(client, seeded)
    p1, _ = products(client, seeded, 2)
    po = make_po(client, seeded, s["id"], [{"product_id": p1["id"], "quantity": 5, "unit_cost": 2},
                                           {"product_id": p1["id"], "quantity": 3, "unit_cost": 2.5}])
    client.post(f"/purchase-orders/{po['id']}/send", headers=seeded, json={})
    l1, l2 = po["lines"]
    r = client.post(f"/purchase-orders/{po['id']}/receive", headers=seeded, json={
        "lines": [{"po_line_id": l1["id"], "quantity": 5}, {"po_line_id": l2["id"], "quantity": 3}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "received"
    links = client.get(f"/suppliers/{s['id']}/products", headers=seeded).json()
    assert len(links) == 1 and links[0]["last_cost"] == 2.5
