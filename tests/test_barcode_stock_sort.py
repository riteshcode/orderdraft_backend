"""Barcode + QR, stock page (adjust / log / low stock), column sorting."""
from app.modules.catalog.barcodes import check_digit, internal_ean13, symbology
from tests.conftest import register
from tests.test_imports import upload


def items(client, h, path):
    return client.get(path, headers=h).json()["items"]


# ---------------- barcode ----------------
def test_check_digit_and_symbology():
    assert check_digit("400638133393") == "1"                    # real EAN-13 4006381333931
    assert symbology("4006381333931") == "ean13" and symbology("036000291452") == "upca"
    assert symbology("96385074") == "ean8" and symbology("4006381333932") == "code128"
    assert internal_ean13(1) == "2000000000015" and symbology(internal_ean13(987654)) == "ean13"


def test_every_product_gets_barcode(client, seeded):
    prods = items(client, seeded, "/products?page_size=500")
    assert prods and all(p["barcode"] and symbology(p["barcode"]) == "ean13" for p in prods)
    assert all(p["barcode"] == internal_ean13(p["id"]) for p in prods)
    new = client.post("/products", headers=seeded, json={"sku": "NEW-1", "name": "New"}).json()
    assert new["barcode"] == internal_ean13(new["id"])


def test_own_barcode_validation_and_unique(client, seeded):
    p, q = items(client, seeded, "/products?page_size=2")
    body = {"sku": p["sku"], "name": p["name"]}
    r = client.put(f"/products/{p['id']}", headers=seeded, json={**body, "barcode": "4006381333931"}).json()
    assert r["barcode"] == "4006381333931"
    bad = client.put(f"/products/{p['id']}", headers=seeded, json={**body, "barcode": "4006381333932"})
    assert bad.status_code == 400 and "check digit" in bad.json()["detail"]
    dup = client.put(f"/products/{q['id']}", headers=seeded, json={"sku": q["sku"], "name": q["name"], "barcode": "4006381333931"})
    assert dup.status_code == 400
    # khali = internal wapas
    r = client.put(f"/products/{p['id']}", headers=seeded, json={**body, "barcode": ""}).json()
    assert r["barcode"] == internal_ean13(p["id"])
    # purana form (barcode bheja hi nahi) = barcode wahi rahe
    client.put(f"/products/{p['id']}", headers=seeded, json={**body, "barcode": "ABC-123"})
    assert client.put(f"/products/{p['id']}", headers=seeded, json=body).json()["barcode"] == "ABC-123"


def test_codes_svg_lookup_labels(client, seeded):
    p = items(client, seeded, "/products?page_size=1")[0]
    c = client.get(f"/products/{p['id']}/codes", headers=seeded).json()
    assert c["symbology"] == "ean13" and c["barcode_svg"].startswith("<svg") and c["qr_svg"].startswith("<svg")
    assert client.get(f"/products/lookup?code={p['barcode']}", headers=seeded).json()["id"] == p["id"]
    assert client.get(f"/products/lookup?code={p['sku'].lower()}", headers=seeded).json()["id"] == p["id"]
    assert client.get("/products/lookup?code=nope", headers=seeded).status_code == 404
    assert client.get(f"/products?q={p['barcode']}", headers=seeded).json()["items"][0]["id"] == p["id"]
    labels = client.post("/products/labels", headers=seeded, json={"ids": [p["id"]]}).json()
    assert len(labels) == 1 and labels[0]["barcode"] == p["barcode"]
    # code128 bhi render hota hai
    client.put(f"/products/{p['id']}", headers=seeded, json={"sku": p["sku"], "name": p["name"], "barcode": "SKU<&>1"})
    c = client.get(f"/products/{p['id']}/codes", headers=seeded).json()
    assert c["symbology"] == "code128" and "&lt;" in c["barcode_svg"]


def test_barcode_lookup_isolated(client, seeded):
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    p = items(client, seeded, "/products?page_size=1")[0]
    client.put(f"/products/{p['id']}", headers=seeded, json={"sku": p["sku"], "name": p["name"], "barcode": "4006381333931"})
    assert client.get("/products/lookup?code=4006381333931", headers=other).status_code == 404
    # doosra client bhi same manufacturer barcode use kar sakta hai (apne DB mein)
    fish = client.post("/products", headers=other, json={"sku": "F1", "name": "Fish", "barcode": "4006381333931"})
    assert fish.status_code == 200


def test_csv_import_barcode(client, auth):
    csv = b"Code,Name,EAN\nA1,Apples,4006381333931\nB1,Bananas,123\nC1,Cherries,4006381333932\n"
    body = upload(client, auth, "products", csv)
    assert body["mapping"]["EAN"] == "barcode"
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]}).json()
    assert r["created"] == 3 and any("check digit" in e for e in r["errors"])
    by = {p["sku"]: p for p in items(client, auth, "/products?page_size=10")}
    assert by["A1"]["barcode"] == "4006381333931" and by["B1"]["barcode"] == "123"
    assert by["C1"]["barcode"] == internal_ean13(by["C1"]["id"])            # galat -> internal


# ---------------- stock ----------------
def test_stock_adjust_log_and_filters(client, seeded):
    p = items(client, seeded, "/products?page_size=1")[0]
    assert client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "quantity": 5}).status_code == 400
    s = client.post("/stock/track", headers=seeded, json={"product_id": p["id"], "track": True, "reorder_level": 10}).json()
    assert s["quantity_on_hand"] == 0 and s["status"] == "out"

    s = client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "mode": "add", "quantity": 24,
                                                            "reason": "receive", "note": "PO 55"}).json()
    assert s["quantity_on_hand"] == 24 and s["status"] == "ok"
    s = client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "mode": "remove", "quantity": 16,
                                                            "reason": "damaged"}).json()
    assert s["quantity_on_hand"] == 8 and s["status"] == "low"
    s = client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "mode": "set", "quantity": 30,
                                                            "reason": "count"}).json()
    assert s["quantity_on_hand"] == 30
    assert client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "quantity": 1, "reason": "x"}).status_code == 400

    log = client.get(f"/stock/movements?product_id={p['id']}", headers=seeded).json()["items"]
    assert [(m["change"], m["qty_after"], m["reason"]) for m in log] == [(22, 30, "count"), (-16, 8, "damaged"), (24, 24, "receive")]
    assert log[-1]["note"] == "PO 55" and log[0]["user_name"]

    stock = client.get("/stock", headers=seeded).json()
    assert stock["summary"]["tracked"] == 1 and stock["items"][0]["id"] == p["id"]
    client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "mode": "set", "quantity": 3})
    assert [x["id"] for x in client.get("/stock?status=low", headers=seeded).json()["items"]] == [p["id"]]
    assert client.get("/stock?status=out", headers=seeded).json()["items"] == []
    client.put(f"/stock/{p['id']}/reorder-level", headers=seeded, json={"reorder_level": None})
    assert client.get("/stock?status=ok", headers=seeded).json()["items"][0]["id"] == p["id"]


def test_product_form_qty_change_is_logged(client, seeded):
    p = items(client, seeded, "/products?page_size=1")[0]
    client.post("/stock/track", headers=seeded, json={"product_id": p["id"], "track": True})
    client.put(f"/products/{p['id']}", headers=seeded, json={"sku": p["sku"], "name": p["name"], "quantity_on_hand": 12})
    log = client.get(f"/stock/movements?product_id={p['id']}", headers=seeded).json()["items"]
    assert log[0]["qty_after"] == 12 and "product form" in log[0]["note"]


def test_synced_items_stock_managed_in_orderdraft(client, auth, fake_xero):
    """Xero item: pehli sync pe opening qty, uske baad sync on hand / tracking nahi badalta."""
    item = {"ItemID": "i1", "Code": "X1", "Name": "Xero item", "IsSold": True, "IsTrackedAsInventory": True,
            "QuantityOnHand": 7, "SalesDetails": {"UnitPrice": 5}}
    fake_xero.routes[("GET", "/Items")] = {"Items": [item]}
    fake_xero.routes[("GET", "/Contacts")] = {"Contacts": []}
    client.post("/integrations/sync", headers=auth)
    s = client.get("/stock", headers=auth).json()["items"][0]
    assert s["quantity_on_hand"] == 7 and "managed_by" not in s
    log = client.get(f"/stock/movements?product_id={s['id']}", headers=auth).json()["items"]
    assert [(m["reason"], m["qty_after"]) for m in log] == [("opening", 7)]

    # ab OrderDraft mein adjust ho sakta hai
    r = client.post("/stock/adjust", headers=auth, json={"product_id": s["id"], "quantity": 3, "reason": "receive"})
    assert r.status_code == 200 and r.json()["quantity_on_hand"] == 10
    # next sync Xero ka 7 wapas nahi laata, aur tracking bhi user ki rehti hai
    fake_xero.routes[("GET", "/Items")] = {"Items": [{**item, "QuantityOnHand": 2, "IsTrackedAsInventory": False}]}
    client.post("/integrations/sync", headers=auth)
    s = client.get("/stock", headers=auth).json()["items"][0]
    assert s["quantity_on_hand"] == 10
    assert client.post("/stock/track", headers=auth, json={"product_id": s["id"], "track": False}).status_code == 200
    assert client.get("/stock", headers=auth).json()["items"] == []


def _track(client, h, pid, qty):
    client.post("/stock/track", headers=h, json={"product_id": pid, "track": True})
    client.post("/stock/adjust", headers=h, json={"product_id": pid, "mode": "set", "quantity": qty, "reason": "count"})


def test_approve_deducts_tracked_products_only(client, seeded, draft_order):
    from tests.test_destinations import approve
    lines = draft_order["lines"]
    tracked, untracked = lines[0]["product_id"], lines[1]["product_id"]
    _track(client, seeded, tracked, 20)
    assert client.get("/settings", headers=seeded).json()["stock"]["deduct_on_approve"] is True
    o = approve(client, seeded, draft_order)
    stock = {i["id"]: i for i in client.get("/stock", headers=seeded).json()["items"]}
    assert stock[tracked]["quantity_on_hand"] == 20 - lines[0]["quantity"]
    assert untracked not in stock                          # track inventory off = koi movement nahi
    m = client.get(f"/stock/movements?product_id={tracked}", headers=seeded).json()["items"][0]
    assert m["reason"] == "sale" and m["ref_type"] == "order" and m["ref_id"] == o["id"]
    assert m["change"] == -lines[0]["quantity"] and f"Order #{o['id']}" in m["note"]
    assert client.get(f"/stock/movements?product_id={untracked}", headers=seeded).json()["items"] == []


def test_deduct_setting_off_and_same_product_lines(client, seeded):
    p = items(client, seeded, "/products?page_size=1")[0]
    c = items(client, seeded, "/customers?page_size=1")[0]
    _track(client, seeded, p["id"], 10)
    # ek product do lines mein = ek movement, total qty
    client.post("/orders/manual", headers=seeded, json={"customer_id": c["id"], "approve": True,
                "lines": [{"product_id": p["id"], "quantity": 2}, {"product_id": p["id"], "quantity": 3}]})
    log = client.get(f"/stock/movements?product_id={p['id']}", headers=seeded).json()["items"]
    assert log[0]["change"] == -5 and log[0]["qty_after"] == 5 and len([m for m in log if m["reason"] == "sale"]) == 1
    # stock kam pad jaaye to bhi approve (negative)
    client.post("/orders/manual", headers=seeded, json={"customer_id": c["id"], "approve": True,
                "lines": [{"product_id": p["id"], "quantity": 8}]})
    assert client.get("/stock", headers=seeded).json()["items"][0]["quantity_on_hand"] == -3
    # setting off -> stock same
    r = client.put("/settings/stock", headers=seeded, json={"deduct_on_approve": False}).json()
    assert r["stock"]["deduct_on_approve"] is False
    client.post("/orders/manual", headers=seeded, json={"customer_id": c["id"], "approve": True,
                "lines": [{"product_id": p["id"], "quantity": 1}]})
    assert client.get("/stock", headers=seeded).json()["items"][0]["quantity_on_hand"] == -3
    # draft (approve nahi) = stock same
    client.put("/settings/stock", headers=seeded, json={"deduct_on_approve": True})
    client.post("/orders/manual", headers=seeded, json={"customer_id": c["id"], "lines": [{"product_id": p["id"], "quantity": 1}]})
    assert client.get("/stock", headers=seeded).json()["items"][0]["quantity_on_hand"] == -3


def test_manual_reasons_exclude_system_ones(client, seeded):
    p = items(client, seeded, "/products?page_size=1")[0]
    client.post("/stock/track", headers=seeded, json={"product_id": p["id"], "track": True})
    assert "sale" not in client.get("/stock", headers=seeded).json()["reasons"]
    r = client.post("/stock/adjust", headers=seeded, json={"product_id": p["id"], "quantity": 1, "reason": "sale"})
    assert r.status_code == 400


# ---------------- sorting ----------------
def test_sorting_products_customers(client, seeded):
    names = [p["name"] for p in items(client, seeded, "/products?page_size=500")]
    assert names == sorted(names, key=str.lower) or names == sorted(names)           # default A-Z (pehle jaisa)
    prices = [p["price"] or 0 for p in items(client, seeded, "/products?page_size=500&sort=-price")]
    assert prices == sorted(prices, reverse=True)
    skus = [p["sku"] for p in items(client, seeded, "/products?page_size=500&sort=sku")]
    assert skus == sorted(skus)
    assert items(client, seeded, "/products?page_size=5&sort=;drop table") == items(client, seeded, "/products?page_size=5")
    codes = [c["code"] for c in items(client, seeded, "/customers?page_size=500&sort=-code")]
    assert codes == sorted(codes, reverse=True)


def test_sorting_orders_and_invoices(client, seeded, draft_order):
    from tests.test_destinations import approve
    approve(client, seeded, draft_order)
    c2 = items(client, seeded, "/customers?page_size=500&sort=-name")[0]
    pid = draft_order["lines"][0]["product_id"]
    client.post("/orders/manual", headers=seeded, json={"customer_id": c2["id"], "lines": [{"product_id": pid, "quantity": 50}]})
    by_default = [o["id"] for o in items(client, seeded, "/orders")]
    assert by_default == sorted(by_default, reverse=True)                              # newest first (pehle jaisa)
    totals = [o["total_amount"] or 0 for o in items(client, seeded, "/orders?sort=-total_amount")]
    assert totals == sorted(totals, reverse=True)
    cust = [o["customer_name"] for o in items(client, seeded, "/orders?sort=customer&q=a")]
    assert cust == sorted(cust)
    for s in ("number", "-total", "due_date", "customer", "-balance"):
        assert client.get(f"/invoices?sort={s}", headers=seeded).status_code == 200
    stock_sort = client.get("/stock?sort=-quantity_on_hand", headers=seeded)
    assert stock_sort.status_code == 200
