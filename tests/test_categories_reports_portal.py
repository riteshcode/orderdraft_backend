"""Categories, product orders, portal logins (welcome email), announcements, sales reports."""
import re

from app.core import mailer
from tests.conftest import register
from tests.test_destinations import approve
from tests.test_imports import upload


# ---------------- Categories ----------------
def test_category_tree_rules(client, auth):
    fruit = client.post("/categories", headers=auth, json={"name": "Fruit"}).json()
    citrus = client.post("/categories", headers=auth, json={"name": "Citrus", "parent_id": fruit["id"]}).json()
    assert citrus["path"] == "Fruit › Citrus"
    # 3rd level mana, duplicate mana, khud ka parent mana
    assert client.post("/categories", headers=auth, json={"name": "Lemons", "parent_id": citrus["id"]}).status_code == 400
    assert client.post("/categories", headers=auth, json={"name": "fruit"}).status_code == 400
    assert client.put(f"/categories/{fruit['id']}", headers=auth,
                      json={"name": "Fruit", "parent_id": fruit["id"]}).status_code == 400
    veg = client.post("/categories", headers=auth, json={"name": "Veg"}).json()
    assert client.put(f"/categories/{fruit['id']}", headers=auth,     # has subs -> can't move under Veg
                      json={"name": "Fruit", "parent_id": veg["id"]}).status_code == 400
    assert [c["path"] for c in client.get("/categories", headers=auth).json()] == ["Fruit", "Fruit › Citrus", "Veg"]
    assert client.delete(f"/categories/{fruit['id']}", headers=auth).status_code == 400   # subs first


def test_product_category_filter_bulk_and_delete(client, auth):
    fruit = client.post("/categories", headers=auth, json={"name": "Fruit"}).json()
    citrus = client.post("/categories", headers=auth, json={"name": "Citrus", "parent_id": fruit["id"]}).json()
    a = client.post("/products", headers=auth, json={"sku": "L1", "name": "Lemons", "category_id": citrus["id"]}).json()
    b = client.post("/products", headers=auth, json={"sku": "A1", "name": "Apples"}).json()
    assert a["category_name"] == "Fruit › Citrus" and b["category_id"] is None
    assert client.get(f"/products?category_id={fruit['id']}", headers=auth).json()["total"] == 1   # parent includes subs
    assert client.get("/products?category_id=0", headers=auth).json()["total"] == 1                # uncategorised
    r = client.post("/products/bulk-category", headers=auth, json={"product_ids": [b["id"]], "category_id": fruit["id"]})
    assert r.json()["updated"] == 1
    assert {c["path"]: c["product_count"] for c in client.get("/categories", headers=auth).json()} == {
        "Fruit": 1, "Fruit › Citrus": 1}
    assert client.delete(f"/categories/{citrus['id']}", headers=auth).json()["products_uncategorised"] == 1
    assert client.get(f"/products/{a['id']}", headers=auth).json()["category_id"] is None


def test_import_creates_category_path(client, auth):
    csv = "SKU,Name,Category\nC1,Oranges,Fruit > Citrus\nC2,Limes,Fruit / Citrus\nC3,Kale,Veg\n".encode()
    body = upload(client, auth, "products", csv)
    assert body["mapping"]["Category"] == "category"
    client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]})
    cats = client.get("/categories", headers=auth).json()
    assert [(c["path"], c["product_count"]) for c in cats] == [("Fruit", 0), ("Fruit › Citrus", 2), ("Veg", 1)]


# ---------------- Product -> orders ----------------
def test_product_orders(client, seeded, draft_order):
    line = draft_order["lines"][0]
    pid = line["product_id"]
    r = client.get(f"/products/{pid}/orders", headers=seeded).json()
    assert r["total"] == 1 and r["items"][0]["status"] == "needs_review"
    assert r["summary"]["pending_quantity"] == line["quantity"] and r["summary"]["sold_quantity"] == 0
    approve(client, seeded, draft_order)
    r = client.get(f"/products/{pid}/orders", headers=seeded).json()
    item = r["items"][0]
    assert item["customer_name"] and item["quantity"] == line["quantity"] and item["line_total"] is not None
    assert r["summary"]["sold_quantity"] == line["quantity"] and r["summary"]["customers"] == 1
    assert client.get(f"/products/{pid}/orders?status=rejected", headers=seeded).json()["total"] == 0


# ---------------- Portal logins ----------------
def _token_from(html: str) -> str:
    return re.search(r"reset-password\?token=([\w\-\.]+)", html).group(1)


def test_portal_welcome_email_and_login(client, auth):
    c = client.post("/customers", headers=auth, json={"name": "Joe's Café"}).json()
    mailer.SENT.clear()
    r = client.post(f"/customers/{c['id']}/portal-logins", headers=auth, json={"email": "Joe@Cafe.com", "name": "Joe"})
    assert r.status_code == 200, r.text
    login = r.json()
    assert login["email"] == "joe@cafe.com" and login["must_change_password"] and login["temp_password"] is None
    mail = mailer.SENT[-1]
    assert mail["to"] == "joe@cafe.com" and "ordering account" in mail["subject"]
    temp = re.search(r"Temporary password: (\S+)", mail["text"]).group(1)
    assert "/portal/freshfarms" in mail["html"] and "reset-password?token=" in mail["html"]
    # duplicate email
    assert client.post(f"/customers/{c['id']}/portal-logins", headers=auth, json={"email": "joe@cafe.com"}).status_code == 400

    # temp password se login -> must change
    r = client.post("/portal/freshfarms/login", json={"email": "joe@cafe.com", "password": temp})
    assert r.status_code == 200 and r.json()["login"]["must_change_password"]
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/portal/me", headers=h).json()["customer"]["name"] == "Joe's Café"
    assert client.get("/products", headers=h).status_code == 401          # portal token != staff token
    assert client.post("/portal/change-password", headers=h,
                       json={"current_password": temp, "new_password": "short"}).status_code == 400
    me = client.post("/portal/change-password", headers=h, json={"current_password": temp, "new_password": "newpass123"})
    assert me.json()["must_change_password"] is False
    # welcome link ab bekaar (password badal gaya)
    r = client.post("/portal/freshfarms/reset-password", json={"token": _token_from(mail["html"]), "password": "another123"})
    assert r.status_code == 400
    assert client.post("/portal/freshfarms/login", json={"email": "joe@cafe.com", "password": "wrong"}).status_code == 401


def test_portal_reset_link_resend_disable_and_tenant_isolation(client, auth):
    c = client.post("/customers", headers=auth, json={"name": "Bella"}).json()
    lid = client.post(f"/customers/{c['id']}/portal-logins", headers=auth, json={"email": "b@b.com"}).json()["id"]
    token = _token_from(mailer.SENT[-1]["html"])
    assert client.post("/portal/freshfarms/reset-password", json={"token": token, "password": "mypass123"}).status_code == 200
    assert client.post("/portal/freshfarms/login", json={"email": "b@b.com", "password": "mypass123"}).status_code == 200
    # dusre client ke portal pe ye login nahi chalega
    register(client, "o@ocean.com", "Ocean Seafood Co")
    assert client.post("/portal/oceanseafood/login", json={"email": "b@b.com", "password": "mypass123"}).status_code == 401
    # forgot password: email jaata hai, unknown email pe bhi same response
    mailer.SENT.clear()
    assert client.post("/portal/freshfarms/forgot-password", json={"email": "b@b.com"}).json()["ok"]
    assert client.post("/portal/freshfarms/forgot-password", json={"email": "nobody@x.com"}).json()["ok"]
    assert len(mailer.SENT) == 1 and "Reset your" in mailer.SENT[0]["subject"]
    # resend = naya temp password, purana password bekaar
    r = client.post(f"/customers/{c['id']}/portal-logins/{lid}/resend", headers=auth).json()
    assert r["must_change_password"]
    assert client.post("/portal/freshfarms/login", json={"email": "b@b.com", "password": "mypass123"}).status_code == 401
    # disable
    client.put(f"/customers/{c['id']}/portal-logins/{lid}", headers=auth, json={"is_active": False})
    temp = re.search(r"Temporary password: (\S+)", mailer.SENT[-1]["text"]).group(1)
    assert client.post("/portal/freshfarms/login", json={"email": "b@b.com", "password": temp}).status_code == 403
    hist = client.get(f"/activity/entity/customer/{c['id']}", headers=auth).json()["items"]
    assert "customer.portal_welcome_sent" in [h["action"] for h in hist]


def test_portal_login_without_email(client, auth):
    c = client.post("/customers", headers=auth, json={"name": "Mama's"}).json()
    r = client.post(f"/customers/{c['id']}/portal-logins", headers=auth,
                    json={"email": "m@m.com", "send_welcome": False}).json()
    assert r["temp_password"] and r["welcome_sent_at"] is None


# ---------------- Announcements ----------------
def test_announcements_targeting_dismiss_and_login_page(client, auth):
    admin = register(client, "boss@platform.com", "Platform HQ")
    other = register(client, "o@ocean.com", "Ocean Seafood Co")
    ff_id = client.get("/auth/me", headers=auth).json()["tenant"]["id"]
    assert client.post("/admin/announcements", headers=auth, json={"title": "x"}).status_code == 403
    a_all = client.post("/admin/announcements", headers=admin, json={
        "title": "Maintenance Sunday 10pm", "body": "Short downtime", "level": "warning", "show_on_login": True}).json()
    a_ff = client.post("/admin/announcements", headers=admin, json={
        "title": "Your Xero needs reconnecting", "audience": "tenants", "tenant_ids": [ff_id], "display": "popup",
        "dismissible": False, "level": "critical"}).json()
    client.post("/admin/announcements", headers=admin, json={"title": "Owners only", "roles": ["reviewer"]})
    client.post("/admin/announcements", headers=admin, json={"title": "Old", "ends_at": "2020-01-01T00:00:00"})
    assert client.post("/admin/announcements", headers=admin,
                       json={"title": "bad", "audience": "tenants"}).status_code == 400

    mine = {a["title"] for a in client.get("/announcements", headers=auth).json()}
    assert mine == {"Maintenance Sunday 10pm", "Your Xero needs reconnecting"}
    assert {a["title"] for a in client.get("/announcements", headers=other).json()} == {"Maintenance Sunday 10pm"}
    # dismiss
    assert client.post(f"/announcements/{a_ff['id']}/dismiss", headers=auth).status_code == 400   # not dismissible
    client.post(f"/announcements/{a_all['id']}/dismiss", headers=auth)
    assert [a["title"] for a in client.get("/announcements", headers=auth).json()] == ["Your Xero needs reconnecting"]
    listing = client.get("/admin/announcements", headers=admin).json()
    assert next(x for x in listing if x["id"] == a_all["id"])["dismissed_count"] == 1
    client.post(f"/admin/announcements/{a_all['id']}/reset-dismissals", headers=admin)
    assert len(client.get("/announcements", headers=auth).json()) == 2
    # login page (bina login)
    pub = client.get("/public/announcements").json()
    assert [p["title"] for p in pub] == ["Maintenance Sunday 10pm"] and "tenant_ids" not in pub[0]
    client.delete(f"/admin/announcements/{a_all['id']}", headers=admin)
    assert client.get("/public/announcements").json() == []


# ---------------- Sales reports ----------------
def test_sales_reports(client, seeded, draft_order):
    lines = draft_order["lines"]
    fruit = client.post("/categories", headers=seeded, json={"name": "Veg"}).json()
    sub = client.post("/categories", headers=seeded, json={"name": "Tomatoes", "parent_id": fruit["id"]}).json()
    client.post("/products/bulk-category", headers=seeded, json={"product_ids": [lines[0]["product_id"]],
                                                                 "category_id": sub["id"]})
    empty = client.get("/reports/sales/summary", headers=seeded).json()
    assert empty["orders"] == 0                                   # needs_review = sale nahi
    order = approve(client, seeded, draft_order)
    s = client.get("/reports/sales/summary", headers=seeded).json()
    assert s["orders"] == 1 and s["value"] == order["total_amount"] and s["customers"] == 1

    cat = client.get("/reports/sales/by-category", headers=seeded).json()
    names = {i["category"] for i in cat["items"]}
    assert "Veg" in names and abs(sum(i["value"] for i in cat["items"]) - s["value"]) < 0.01
    sub_level = client.get("/reports/sales/by-category?level=sub", headers=seeded).json()
    assert "Veg › Tomatoes" in {i["category"] for i in sub_level["items"]}

    cust = client.get("/reports/sales/by-customer", headers=seeded).json()
    assert cust["items"][0]["orders"] == 1 and cust["items"][0]["share"] == 100.0
    by_order = client.get("/reports/sales/by-order", headers=seeded).json()
    assert by_order["total"] == 1 and by_order["items"][0]["lines"] == len(lines)
    csv = client.get("/reports/sales/by-customer?format=csv", headers=seeded)
    assert csv.headers["content-type"].startswith("text/csv") and "Sales (ex GST)" in csv.content.decode("utf-8-sig")
    old = client.get("/reports/sales/summary?date_from=2020-01-01&date_to=2020-01-31", headers=seeded).json()
    assert old["orders"] == 0
