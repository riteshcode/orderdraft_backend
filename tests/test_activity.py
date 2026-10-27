"""Audit trail + activity log + request log: kisne kya badla, support khud dekh sake."""
import glob
import json
import os

from app.core.config import settings
from tests.conftest import register
from tests.test_imports import upload


def search(client, h, **params):
    r = client.get("/activity/search", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_product_create_update_delete_diff(client, auth):
    p = client.post("/products", headers=auth, json={"sku": "R1", "name": "Roma 10kg", "price": 32.5}).json()
    client.put(f"/products/{p['id']}", headers=auth, json={"sku": "R1", "name": "Roma 10kg", "price": 34, "unit": "box"})
    rows = search(client, auth, entity="product", entity_id=p["id"])["items"]
    actions = [r["action"] for r in rows]
    assert actions[:2] == ["product.updated", "product.created"]
    upd = rows[0]
    assert upd["user_name"] == "Ritesh"
    assert upd["detail"]["changes"]["price"] == [32.5, 34.0]
    assert "updated_at" not in upd["detail"]["changes"]                 # noise columns skip


def test_no_change_no_log_and_filters(client, auth):
    p = client.post("/products", headers=auth, json={"sku": "R2", "name": "Lettuce", "price": 3}).json()
    client.put(f"/products/{p['id']}", headers=auth, json={"sku": "R2", "name": "Lettuce", "price": 3})
    assert search(client, auth, entity="product", entity_id=p["id"])["total"] == 1   # sirf created
    assert search(client, auth, q="product.created")["total"] == 1
    assert search(client, auth, q="nothing.like.this")["total"] == 0
    assert search(client, auth, date_from="2000-01-01", date_to="2000-01-02")["total"] == 0


def test_import_is_one_summary_not_per_row(client, auth):
    csv = "Code,Name\nA1,Apple\nA2,Banana\nA3,Cherry\n".encode()
    body = upload(client, auth, "products", csv)
    client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]})
    assert search(client, auth, entity="product")["total"] == 0          # no_audit
    assert search(client, auth, q="import.products")["total"] == 1


def test_child_rows_show_in_parent_history(client, auth):
    p = client.post("/products", headers=auth, json={"sku": "P1", "name": "Tomato", "price": 5}).json()
    c = client.post("/customers", headers=auth, json={"name": "Joe's Cafe"}).json()
    r = client.post(f"/customers/{c['id']}/pantry", headers=auth, json={"product_id": p["id"], "price": 4.5})
    assert r.status_code in (200, 201), r.text
    s = client.post("/suppliers", headers=auth, json={"name": "Farm Co"}).json()
    r = client.post(f"/suppliers/{s['id']}/products", headers=auth, json={"product_id": p["id"], "unit_cost": 3, "min_order_qty": 10})
    assert r.status_code in (200, 201), r.text

    hist = client.get(f"/activity/entity/customer/{c['id']}", headers=auth).json()["items"]
    assert "customer.price_added" in [h["action"] for h in hist]
    assert next(h for h in hist if h["action"] == "customer.price_added")["detail"]["product"] == "Tomato"
    hist = client.get(f"/activity/entity/supplier/{s['id']}", headers=auth).json()["items"]
    assert {"supplier.created", "supplier.product_linked"} <= {h["action"] for h in hist}


def test_login_and_failed_login_logged(client, auth):
    assert client.post("/auth/login", json={"email": "ritesh@freshfarms.com.au", "password": "wrong-pass"}).status_code == 401
    assert client.post("/auth/login", json={"email": "ritesh@freshfarms.com.au", "password": "password123"}).status_code == 200
    actions = [r["action"] for r in search(client, auth, entity="user")["items"]]
    assert "auth.login" in actions and "auth.login_failed" in actions


def test_tenant_isolation_and_superadmin_view(client, auth):
    client.post("/products", headers=auth, json={"sku": "X1", "name": "Mine"})
    other = register(client, "a@ocean.com", "Ocean Seafood Co")
    assert search(client, other, entity="product")["total"] == 0          # dusre client ka log nahi dikhta
    admin = register(client, "boss@platform.com", "Platform HQ")
    tid = client.get("/auth/me", headers=auth).json()["tenant"]["id"]
    r = client.get(f"/admin/tenants/{tid}/activity", headers=admin, params={"entity": "product"}).json()
    assert r["total"] == 1 and r["items"][0]["detail"]["name"] == "Mine"
    assert client.get(f"/admin/tenants/{tid}/activity", headers=auth).status_code == 403


def test_request_log_file_and_error_reference(client, auth):
    r = client.get("/products", headers=auth)
    rid = r.headers["X-Request-ID"]
    assert len(rid) == 12
    lines = []
    for f in glob.glob(os.path.join(settings.LOG_DIR, "app.log*")):
        lines += [json.loads(line) for line in open(f, encoding="utf-8") if line.strip()]
    entry = next(e for e in lines if e.get("request_id") == rid)
    assert entry["path"] == "/products" and entry["status"] == 200 and entry["user_id"] and entry["tenant_id"]

    from app.main import app

    @app.get("/__boom")
    def boom():
        raise RuntimeError("kaboom")
    r = client.get("/__boom", headers={"X-Request-ID": "support-ref-1"})
    assert r.status_code == 500 and r.json()["error_id"] == "support-ref-1"
    text = "".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(settings.LOG_DIR, "app.log*")))
    assert "kaboom" in text and "support-ref-1" in text
