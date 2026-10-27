"""Customer portal: settings, private/public access, customer prices, cart, order, my orders, profile, banners."""
import io
import re

from PIL import Image

from app.core import mailer


def _login(client, auth, customer_name="Joe's Cafe", email="chef@joes.com"):
    """Customer + portal login + apna password -> portal headers."""
    c = client.post("/customers", headers=auth, json={"name": customer_name}).json()
    client.post(f"/customers/{c['id']}/portal-logins", headers=auth, json={"email": email})
    token = re.search(r"reset-password\?token=([\w\-\.]+)", mailer.SENT[-1]["html"]).group(1)
    assert client.post("/portal/freshfarms/reset-password", json={"token": token, "password": "chefpass1"}).status_code == 200
    r = client.post("/portal/freshfarms/login", json={"email": email, "password": "chefpass1"}).json()
    return c, {"Authorization": f"Bearer {r['token']}"}


def _setup(client, auth, **settings):
    fruit = client.post("/categories", headers=auth, json={"name": "Fruit"}).json()
    citrus = client.post("/categories", headers=auth, json={"name": "Citrus", "parent_id": fruit["id"]}).json()
    lemon = client.post("/products", headers=auth, json={"sku": "LEM", "name": "Lemons", "price": 10, "unit": "kg",
                                                         "category_id": citrus["id"]}).json()
    milk = client.post("/products", headers=auth, json={"sku": "MILK", "name": "Milk", "price": 4,
                                                        "is_taxable": False}).json()
    client.post("/products", headers=auth, json={"sku": "OLD", "name": "Old stock"})      # no category
    body = {"enabled": True, "access": "private", "allow_cod": True, **settings}
    r = client.put("/settings/portal", headers=auth, json=body)
    assert r.status_code == 200, r.text
    return fruit, citrus, lemon, milk


def test_portal_off_private_public(client, auth):
    assert client.get("/portal/freshfarms/info").json()["enabled"] is False
    assert client.get("/portal/freshfarms/catalog/products").status_code == 403           # portal band
    _setup(client, auth)
    assert client.get("/portal/freshfarms/catalog/products").status_code == 401           # private = login
    client.put("/settings/portal", headers=auth, json={"enabled": True, "access": "public", "show_prices_public": False})
    r = client.get("/portal/freshfarms/catalog/products").json()
    assert r["total"] == 3 and all(p["price"] is None for p in r["items"])                # guest, prices hidden
    client.put("/settings/portal", headers=auth, json={"enabled": True, "access": "public", "show_prices_public": True})
    assert {p["price"] for p in client.get("/portal/freshfarms/catalog/products").json()["items"]} >= {10.0, 4.0}
    # guest order nahi kar sakta
    assert client.post("/portal/freshfarms/orders", json={"items": [], "payment_method": "cod"}).status_code == 401
    assert client.put("/settings/portal", headers=auth,
                      json={"enabled": True, "allow_cod": False, "allow_account": False}).status_code == 400


def test_customer_price_cart_and_order(client, auth):
    fruit, citrus, lemon, milk = _setup(client, auth, min_order=20, notify_email="staff@ff.com",
                                        checkout_note="Order by 4pm")
    c, h = _login(client, auth)
    client.post(f"/customers/{c['id']}/pantry", headers=auth, json={"product_id": lemon["id"], "price": 8.5})

    # catalog: special price + category filter (parent includes sub)
    items = client.get(f"/portal/freshfarms/catalog/products?category_id={fruit['id']}", headers=h).json()["items"]
    assert [(i["name"], i["price"], i["regular_price"], i["special"]) for i in items] == [("Lemons", 8.5, 10.0, True)]
    home = client.get("/portal/freshfarms/catalog/home", headers=h).json()
    assert home["categories"][0]["name"] == "Fruit" and home["categories"][0]["children"][0]["name"] == "Citrus"
    assert [u["name"] for u in home["usual"]] == ["Lemons"]                                # pantry = usual

    # quote: GST sirf taxable pe, min order
    q = client.post("/portal/freshfarms/cart/quote", headers=h,
                    json={"items": [{"product_id": lemon["id"], "quantity": 1}, {"product_id": 99999, "quantity": 1}]}).json()
    assert q["removed"] == [99999] and q["below_min"] and q["subtotal"] == 8.5
    items = [{"product_id": lemon["id"], "quantity": 2}, {"product_id": milk["id"], "quantity": 1},
             {"product_id": lemon["id"], "quantity": 1}]
    q = client.post("/portal/freshfarms/cart/quote", headers=h, json={"items": items}).json()
    assert q["subtotal"] == 29.5 and q["gst"] == 2.55 and q["total"] == 32.05 and not q["below_min"]

    bad = client.post("/portal/freshfarms/orders", headers=h, json={"items": items, "payment_method": "account"})
    assert bad.status_code == 400                                                          # account band hai
    mailer.SENT.clear()
    r = client.post("/portal/freshfarms/orders", headers=h, json={
        "items": items, "payment_method": "cod", "po_number": "PO-77", "notes": "Back door", "delivery_date": "2030-01-05"})
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    subjects = {m["to"]: m["subject"] for m in mailer.SENT}
    assert "received" in subjects["chef@joes.com"] and "New online order" in subjects["staff@ff.com"]

    # staff side: needs review, source portal, price lock
    o = client.get(f"/orders/{oid}", headers=auth).json()
    assert o["source"] == "portal" and o["status"] == "needs_review" and o["payment_method"] == "cod"
    lem = next(l for l in o["lines"] if l["product_id"] == lemon["id"])
    assert lem["quantity"] == 3 and lem["unit_price"] == 8.5 and lem["price_source"] == "portal"
    assert client.get("/orders?channel=portal", headers=auth).json()["total"] == 1
    client.put(f"/products/{lemon['id']}", headers=auth, json={"sku": "LEM", "name": "Lemons", "price": 50})
    client.delete(f"/customers/{c['id']}/pantry/{lemon['id']}", headers=auth)
    approved = client.post(f"/orders/{oid}/approve", headers=auth).json()
    assert approved["total_amount"] == 29.5                                                # locked price used

    mine = client.get("/portal/freshfarms/orders", headers=h).json()
    assert mine["total"] == 1 and mine["items"][0]["status_label"] == "Confirmed"
    detail = client.get(f"/portal/freshfarms/orders/{oid}", headers=h).json()
    assert len(detail["lines"]) == 2 and detail["po_number"] == "PO-77"
    # past delivery date mana
    assert client.post("/portal/freshfarms/orders", headers=h, json={
        "items": items, "payment_method": "cod", "delivery_date": "2020-01-01"}).status_code == 400


def test_orders_isolated_between_customers_and_inactive_products(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    _, h1 = _login(client, auth, "Cafe A", "a@a.com")
    _, h2 = _login(client, auth, "Cafe B", "b@b.com")
    oid = client.post("/portal/freshfarms/orders", headers=h1, json={
        "items": [{"product_id": lemon["id"], "quantity": 1}], "payment_method": "cod"}).json()["id"]
    assert client.get(f"/portal/freshfarms/orders/{oid}", headers=h2).status_code == 404
    assert client.get("/portal/freshfarms/orders", headers=h2).json()["total"] == 0
    client.post(f"/products/{lemon['id']}/status", headers=auth, json={"is_active": False})
    assert client.get(f"/portal/freshfarms/catalog/products/{lemon['id']}", headers=h1).status_code == 404
    assert client.post("/portal/freshfarms/orders", headers=h1, json={
        "items": [{"product_id": lemon["id"], "quantity": 1}], "payment_method": "cod"}).status_code == 400
    # staff token se portal nahi, portal token se staff API nahi
    assert client.get("/portal/freshfarms/orders", headers=auth).status_code == 401


def test_profile_updates_customer_with_audit(client, auth):
    _setup(client, auth)
    c, h = _login(client, auth)
    r = client.put("/portal/freshfarms/profile", headers=h, json={
        "name": "Chef Joe", "phone": "0400 000 000", "delivery_line1": "1 Beach Rd", "delivery_city": "Bondi",
        "delivery_postcode": "2026", "delivery_instructions": "Back door"})
    assert r.json()["name"] == "Chef Joe"
    cust = client.get(f"/customers/{c['id']}", headers=auth).json()
    assert cust["delivery_city"] == "Bondi" and "Bondi" in cust["address"]
    hist = client.get(f"/activity/entity/customer/{c['id']}", headers=auth).json()["items"]
    upd = next(x for x in hist if x["action"] == "customer.updated")
    assert upd["detail"]["portal_user"] == "chef@joes.com" and "delivery_city" in upd["detail"]["changes"]


def test_banners(client, auth):
    fruit, citrus, lemon, _ = _setup(client, auth)
    b = client.post("/portal-banners", headers=auth, json={"title": "Citrus season", "link_type": "category",
                                                          "link_category_id": citrus["id"]}).json()
    assert b["link_label"] == "Fruit › Citrus"
    assert client.post("/portal-banners", headers=auth, json={"link_type": "product"}).status_code == 400
    img = io.BytesIO()
    Image.new("RGB", (1600, 500), "orange").save(img, "JPEG")
    r = client.post(f"/portal-banners/{b['id']}/image", headers=auth, files={"file": ("b.jpg", img.getvalue(), "image/jpeg")})
    assert r.json()["image_url"].endswith(".jpg")
    b2 = client.post("/portal-banners", headers=auth, json={"title": "Old", "ends_on": "2020-01-01"}).json()
    b3 = client.post("/portal-banners", headers=auth, json={"title": "Lemons", "link_type": "product",
                                                           "link_product_id": lemon["id"]}).json()
    client.put("/portal-banners/order", headers=auth, json={"ids": [b3["id"], b["id"], b2["id"]]})
    _, h = _login(client, auth)
    home = client.get("/portal/freshfarms/catalog/home", headers=h).json()
    assert [x["title"] for x in home["banners"]] == ["Lemons", "Citrus season"]           # expired hidden, order kept
    assert home["banners"][1]["category_id"] == citrus["id"]
    client.delete(f"/portal-banners/{b['id']}", headers=auth)
    assert len(client.get("/portal-banners", headers=auth).json()) == 2
