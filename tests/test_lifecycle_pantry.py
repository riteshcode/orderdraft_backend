"""Active/inactive, Trash (soft delete) + restore + permanent delete, customer finance, pantry prices."""
from datetime import date, timedelta

from tests.conftest import register
from tests.test_destinations import approve


def products(client, h, status="active"):
    return client.get(f"/products?status={status}&page_size=500", headers=h).json()["items"]


def customers(client, h, status="active"):
    return client.get(f"/customers?status={status}&page_size=500", headers=h).json()["items"]


def by_sku(client, h, sku):
    return next(p for p in products(client, h, "all") if p["sku"] == sku)


def joe(client, h):
    return next(c for c in customers(client, h, "all") if c["name"].startswith("Joe"))


# ---------------- active / inactive ----------------
def test_active_inactive_toggle(client, seeded):
    p = products(client, seeded)[0]
    r = client.post(f"/products/{p['id']}/status", headers=seeded, json={"is_active": False}).json()
    assert r["is_active"] is False
    assert p["id"] not in [x["id"] for x in products(client, seeded)]
    assert p["id"] in [x["id"] for x in products(client, seeded, "inactive")]
    assert p["id"] in [x["id"] for x in products(client, seeded, "all")]
    client.post(f"/products/{p['id']}/status", headers=seeded, json={"is_active": True})
    assert p["id"] in [x["id"] for x in products(client, seeded)]

    c = customers(client, seeded)[0]
    client.post(f"/customers/{c['id']}/status", headers=seeded, json={"is_active": False})
    assert c["id"] in [x["id"] for x in customers(client, seeded, "inactive")]


def test_inactive_customer_survives_resync(client, seeded):
    """CSV dobara import hone par user ka 'inactive' wapas active nahi hona chahiye."""
    from tests.conftest import upload_and_run
    p = products(client, seeded)[0]
    client.post(f"/products/{p['id']}/status", headers=seeded, json={"is_active": False})
    upload_and_run(client, seeded, "products", "products.csv")
    assert by_sku(client, seeded, p["sku"])["is_active"] is False


# ---------------- trash ----------------
def test_soft_delete_restore_and_permanent(client, seeded):
    p = products(client, seeded)[0]
    assert client.delete(f"/products/{p['id']}", headers=seeded).json()["ok"]
    assert p["id"] not in [x["id"] for x in products(client, seeded, "all")]
    trash = products(client, seeded, "deleted")
    assert [x["id"] for x in trash] == [p["id"]] and trash[0]["deleted_at"] and trash[0]["is_active"] is False
    # Trash wala edit / status / naya same SKU nahi
    assert client.put(f"/products/{p['id']}", headers=seeded, json={"sku": p["sku"], "name": "x"}).status_code == 400
    assert client.post(f"/products/{p['id']}/status", headers=seeded, json={"is_active": True}).status_code == 400
    r = client.post("/products", headers=seeded, json={"sku": p["sku"], "name": "Again"})
    assert r.status_code == 400 and "Trash" in r.json()["detail"]

    assert client.post(f"/products/{p['id']}/restore", headers=seeded).json()["ok"]
    assert by_sku(client, seeded, p["sku"])["is_active"] and not products(client, seeded, "deleted")
    assert client.post(f"/products/{p['id']}/restore", headers=seeded).status_code == 400

    # permanent sirf Trash se
    assert client.delete(f"/products/{p['id']}/permanent", headers=seeded).status_code == 400
    client.delete(f"/products/{p['id']}", headers=seeded)
    assert client.delete(f"/products/{p['id']}/permanent", headers=seeded).json()["ok"]
    assert not any(x["sku"] == p["sku"] for x in products(client, seeded, "deleted") + products(client, seeded, "all"))
    assert client.post("/products", headers=seeded, json={"sku": p["sku"], "name": "New one"}).status_code == 200


def test_permanent_delete_blocked_when_used(client, seeded, draft_order):
    approve(client, seeded, draft_order)
    pid = draft_order["lines"][0]["product_id"]
    client.delete(f"/products/{pid}", headers=seeded)
    r = client.delete(f"/products/{pid}/permanent", headers=seeded)
    assert r.status_code == 400 and "order/invoice" in r.json()["detail"]

    cid = draft_order["customer_id"]
    client.delete(f"/customers/{cid}", headers=seeded)
    assert cid in [c["id"] for c in customers(client, seeded, "deleted")]
    assert client.delete(f"/customers/{cid}/permanent", headers=seeded).status_code == 400
    assert client.post(f"/customers/{cid}/restore", headers=seeded).json()["ok"]


def test_deleted_items_not_matched(client, seeded, mock_ai):
    """Trash wala product AI matching mein nahi aata."""
    from tests.conftest import JOE_ORDER, PM_AUTH, postmark_payload
    romas = next(p for p in products(client, seeded) if "roma" in p["name"].lower())
    client.delete(f"/products/{romas['id']}", headers=seeded)
    mock_ai.order = JOE_ORDER
    client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 box romas\n6 cos"))
    order = client.get("/orders", headers=seeded).json()["items"][0]
    detail = client.get(f"/orders/{order['id']}", headers=seeded).json()
    assert romas["id"] not in [l["product_id"] for l in detail["lines"]]


def test_customer_permanent_delete_cleans_up(client, seeded):
    c = client.post("/customers", headers=seeded, json={"name": "Temp Cafe"}).json()
    p = products(client, seeded)[0]
    client.post(f"/customers/{c['id']}/pantry", headers=seeded, json={"product_id": p["id"], "price": 1})
    client.delete(f"/customers/{c['id']}", headers=seeded)
    assert client.delete(f"/customers/{c['id']}/permanent", headers=seeded).json()["ok"]
    assert client.get(f"/customers/{c['id']}", headers=seeded).status_code == 404


def test_trash_is_per_client(client, seeded):
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    p = products(client, seeded)[0]
    assert client.delete(f"/products/{p['id']}", headers=other).status_code == 404
    client.delete(f"/products/{p['id']}", headers=seeded)
    assert client.post(f"/products/{p['id']}/restore", headers=other).status_code == 404
    assert products(client, other, "deleted") == []


# ---------------- pantry ----------------
def test_pantry_price_used_on_approve(client, seeded, draft_order):
    cid = draft_order["customer_id"]
    line = draft_order["lines"][0]
    base = line["unit_price"]
    item = client.post(f"/customers/{cid}/pantry", headers=seeded,
                       json={"product_id": line["product_id"], "price": 20.0, "note": "Loyal customer"}).json()
    assert item["status"] == "active" and item["effective_price"] == 20.0 and item["base_price"] == base

    o = client.get(f"/orders/{draft_order['id']}", headers=seeded).json()
    assert o["lines"][0]["unit_price"] == 20.0 and o["lines"][0]["price_source"] == "customer"
    o = approve(client, seeded, draft_order)
    assert o["lines"][0]["unit_price"] == 20.0

    # approve ke baad pantry price badle to order na badle (snapshot)
    client.put(f"/customers/{cid}/pantry/{item['id']}", headers=seeded, json={"price": 5})
    assert client.get(f"/orders/{o['id']}", headers=seeded).json()["lines"][0]["unit_price"] == 20.0


def test_pantry_expiry_and_schedule(client, seeded, draft_order):
    cid = draft_order["customer_id"]
    line = draft_order["lines"][0]
    yesterday, tomorrow = date.today() - timedelta(days=1), date.today() + timedelta(days=1)
    item = client.post(f"/customers/{cid}/pantry", headers=seeded, json={
        "product_id": line["product_id"], "price": 1.0, "expires_on": str(yesterday)}).json()
    assert item["status"] == "expired" and item["effective_price"] == line["unit_price"]
    item = client.put(f"/customers/{cid}/pantry/{item['id']}", headers=seeded,
                      json={"price": 1.0, "starts_on": str(tomorrow)}).json()
    assert item["status"] == "scheduled"
    assert client.put(f"/customers/{cid}/pantry/{item['id']}", headers=seeded, json={
        "price": 1, "starts_on": str(tomorrow), "expires_on": str(yesterday)}).status_code == 400
    o = approve(client, seeded, draft_order)
    assert o["lines"][0]["unit_price"] == line["unit_price"]        # special price abhi valid nahi


def test_pantry_bulk_from_history_and_manual_invoice(client, seeded):
    c = joe(client, seeded)
    r = client.post(f"/customers/{c['id']}/pantry/bulk", headers=seeded, json={"from_history": True}).json()
    assert r["added"] > 0 and all(i["status"] == "none" for i in r["items"])
    again = client.post(f"/customers/{c['id']}/pantry/bulk", headers=seeded, json={"from_history": True}).json()
    assert again["added"] == 0

    item = r["items"][0]
    client.put(f"/customers/{c['id']}/pantry/{item['id']}", headers=seeded, json={"price": 7.77})
    inv = client.post("/invoices", headers=seeded, json={
        "customer_id": c["id"], "lines": [{"product_id": item["product_id"], "quantity": 1}]}).json()
    assert inv["lines"][0]["unit_price"] == 7.77

    assert client.delete(f"/customers/{c['id']}/pantry/{item['id']}", headers=seeded).json()["ok"]
    assert len(client.get(f"/customers/{c['id']}/pantry", headers=seeded).json()) == len(r["items"]) - 1


def test_pantry_rejects_invalid_products_and_isolated(client, seeded):
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    c = joe(client, seeded)
    # har client ka apna DB hai - doosre client ka product id yahan exist hi nahi karta
    assert client.post(f"/customers/{c['id']}/pantry", headers=seeded, json={"product_id": 99999}).status_code == 400
    trashed = products(client, seeded)[0]
    client.delete(f"/products/{trashed['id']}", headers=seeded)
    assert client.post(f"/customers/{c['id']}/pantry", headers=seeded, json={"product_id": trashed["id"]}).status_code == 400
    assert client.get(f"/customers/{c['id']}/pantry", headers=other).status_code == 404


# ---------------- finance ----------------
def test_customer_finance(client, seeded, draft_order):
    cid = draft_order["customer_id"]
    o = approve(client, seeded, draft_order)
    pid = draft_order["lines"][0]["product_id"]
    old = date.today() - timedelta(days=45)
    client.post("/invoices", headers=seeded, json={
        "customer_id": cid, "issue_date": str(old), "due_date": str(old + timedelta(days=7)), "send_email": True,
        "lines": [{"product_id": pid, "quantity": 1, "unit_price": 100, "tax_code": ""}]})
    f = client.get(f"/customers/{cid}/finance", headers=seeded).json()
    assert f["orders"]["count"] == 1 and f["orders"]["approved"] == 1
    assert f["orders"]["sales_total"] == o["total_amount"] and f["orders"]["average_order"] == o["total_amount"]
    assert f["orders"]["recent"][0]["id"] == o["id"]
    assert len(f["monthly_sales"]) == 6 and f["monthly_sales"][-1]["value"] == o["total_amount"]
    assert f["top_products"] and f["top_products"][0]["name"]
    inv = f["invoices"]
    assert inv["overdue_count"] == 1 and inv["overdue"] == 100.0 and inv["aging"]["31_60"] == 100.0
    assert inv["outstanding"] >= 100.0 and inv["open"][0]["days_overdue"] == 38
    assert client.get(f"/customers/{cid}", headers=seeded).json()["id"] == cid
