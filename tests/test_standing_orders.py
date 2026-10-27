"""Standing (recurring) orders + staff notifications + customer order messages (cancel / edit / invoice / placed)."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.core import mailer
from app.modules.standing import service
from tests.test_portal_shop import _login, _setup


def so(**kw):
    base = dict(delivery_weekday=0, generate_weekday=5, generate_time="06:00", every_weeks=1,
                start_date=date(2030, 1, 1), end_date=None, skip_dates=None, last_generated_for=None,
                created_at=datetime(2029, 12, 1))
    return SimpleNamespace(**{**base, **kw})


# ---------------- dates ----------------
def test_deliveries_and_due_dates():
    s = so()                                                    # Mon delivery, Sat 06:00 order
    assert service.lead_days(s) == 2
    assert service.deliveries(s, date(2030, 1, 1), 3) == [date(2030, 1, 7), date(2030, 1, 14), date(2030, 1, 21)]
    sat = datetime(2030, 1, 5, 5, 59)
    assert service.due_deliveries(s, sat) == []                 # 6 baje se pehle
    assert service.due_deliveries(s, sat.replace(hour=6)) == [date(2030, 1, 7)]
    assert service.due_deliveries(s, datetime(2030, 1, 6, 12)) == [date(2030, 1, 7)]   # Sunday - catch up
    assert service.due_deliveries(so(last_generated_for=date(2030, 1, 7)), datetime(2030, 1, 6, 12)) == []
    assert service.due_deliveries(so(skip_dates=["2030-01-07"]), sat.replace(hour=7)) == []
    # har 2 hafte: 7 Jan, 21 Jan
    s2 = so(every_weeks=2)
    assert service.deliveries(s2, date(2030, 1, 8), 2) == [date(2030, 1, 21), date(2030, 2, 4)]
    assert service.due_deliveries(s2, datetime(2030, 1, 12, 8)) == []           # 14 Jan off-week
    # same day order (generate = delivery)
    s3 = so(generate_weekday=0, generate_time="05:00")
    assert service.due_deliveries(s3, datetime(2030, 1, 7, 5, 30)) == [date(2030, 1, 7)]
    # bana usse pehle ka order-day -> purana order nahi
    assert service.due_deliveries(so(created_at=datetime(2030, 1, 6, 9)), datetime(2030, 1, 6, 10)) == []
    # end date
    assert service.deliveries(so(end_date=date(2030, 1, 10)), date(2030, 1, 1), 5) == [date(2030, 1, 7)]


def _payload(lemon, milk, **kw):
    return {"name": "Monday usual", "delivery_weekday": 0, "generate_weekday": 5, "generate_time": "06:00",
            "every_weeks": 1, "lines": [{"product_id": lemon["id"], "quantity": 5}, {"product_id": milk["id"], "quantity": 2}],
            **kw}


def test_staff_standing_order_crud_create_now_and_notifications(client, auth, monkeypatch):
    _, _, lemon, milk = _setup(client, auth, notify_email="staff@ff.com")
    c, h = _login(client, auth)
    bad = client.post("/standing-orders", headers=auth, json={**_payload(lemon, milk), "customer_id": c["id"], "lines": []})
    assert bad.status_code == 400 and "at least one product" in bad.json()["detail"]
    r = client.post("/standing-orders", headers=auth, json={**_payload(lemon, milk), "customer_id": c["id"]})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["schedule"] == "Every Monday · order created Saturday at 6:00 am" and len(s["upcoming"]) == 4
    assert all(date.fromisoformat(u["delivery_date"]).weekday() == 0 for u in s["upcoming"])

    # skip pehli delivery
    first = s["upcoming"][0]["delivery_date"]
    s = client.post(f"/standing-orders/{s['id']}/skip", headers=auth, json={"date": first}).json()
    assert s["skip_dates"] == [first] and s["upcoming"][0]["skipped"]
    assert client.post(f"/standing-orders/{s['id']}/skip", headers=auth,
                       json={"date": (date.fromisoformat(first) + timedelta(days=1)).isoformat()}).status_code == 400

    mailer.SENT.clear()
    r = client.post(f"/standing-orders/{s['id']}/create-now", headers=auth).json()
    assert r["delivery_date"] == s["upcoming"][1]["delivery_date"]        # skip wali chhod ke
    o = client.get(f"/orders/{r['order_id']}", headers=auth).json()
    assert o["source"] == "standing" and o["status"] == "needs_review" and len(o["lines"]) == 2
    assert "Standing order: Monday usual" in o["notes"]
    # staff: bell + email ; customer: bell "usual order placed"
    bell = client.get("/staff-notifications", headers=auth).json()
    assert bell["unread"] >= 1 and bell["items"][0]["title"].startswith(f"Standing order #{o['id']}")
    assert any(m["to"] == "staff@ff.com" and "Standing order" in m["subject"] for m in mailer.SENT)
    n = client.get("/portal/freshfarms/notifications", headers=h).json()["items"][0]
    assert "usual order" in n["title"]
    # dobara same date nahi
    r2 = client.post(f"/standing-orders/{s['id']}/create-now", headers=auth).json()
    assert r2["delivery_date"] != r["delivery_date"]

    # mark read
    client.post("/staff-notifications/read", headers=auth, json={})
    assert client.get("/staff-notifications", headers=auth).json()["unread"] == 0

    lst = client.get(f"/standing-orders?customer_id={c['id']}", headers=auth).json()
    assert len(lst) == 1 and lst[0]["last_order_id"] == r2["order_id"]
    assert client.delete(f"/standing-orders/{s['id']}", headers=auth).json()["ok"]
    assert client.get(f"/orders/{r['order_id']}", headers=auth).status_code == 200     # orders bache


def test_scheduler_run_due_and_inactive_products(client, auth, db, monkeypatch):
    _, _, lemon, milk = _setup(client, auth)
    c, _ = _login(client, auth)
    s = client.post("/standing-orders", headers=auth, json={**_payload(lemon, milk), "customer_id": c["id"]}).json()
    from app.models import StandingOrder
    row = db.get(StandingOrder, s["id"])
    row.created_at = datetime(2020, 1, 1)
    db.commit()
    sat = datetime(2030, 1, 5, 6, 30)
    row.start_date = date(2030, 1, 1)
    db.commit()
    monkeypatch.setattr(service, "local_now", lambda tenant: sat)
    assert service.run_due(db, row.tenant_id) == 1
    assert service.run_due(db, row.tenant_id) == 0                         # dobara nahi
    db.refresh(row)
    assert row.last_generated_for == date(2030, 1, 7)

    # sab products band -> order nahi, staff ko reason
    for p in (lemon, milk):
        client.post(f"/products/{p['id']}/status", headers=auth, json={"is_active": False})
    monkeypatch.setattr(service, "local_now", lambda tenant: sat + timedelta(days=7))
    db.commit()
    db.expire_all()            # MySQL repeatable read: API ke badlav dekhne ke liye naya snapshot
    assert service.run_due(db, row.tenant_id) == 0
    items = client.get("/staff-notifications", headers=auth).json()["items"]
    assert items[0]["kind"] == "standing_skipped" and "none of its products" in items[0]["body"]


def test_portal_customer_standing_orders(client, auth):
    _, _, lemon, milk = _setup(client, auth)
    c, h = _login(client, auth)
    r = client.post("/portal/freshfarms/standing-orders", headers=h, json=_payload(lemon, milk, generate_weekday=6))
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["created_by"] == "customer" and s["payment_method"] == "cod" and s["estimate"] > 0
    assert s["lines"][0]["price"] == 10.0
    s = client.put(f"/portal/freshfarms/standing-orders/{s['id']}", headers=h,
                   json=_payload(lemon, milk, every_weeks=2, is_active=False)).json()
    assert s["every_weeks"] == 2 and s["is_active"] is False and s["upcoming"] == []
    # staff ko dikhta hai
    assert client.get(f"/standing-orders?customer_id={c['id']}", headers=auth).json()[0]["created_by"] == "customer"
    # doosra customer nahi dekh sakta
    _, h2 = _login(client, auth, "Cafe B", "b@b.com")
    assert client.put(f"/portal/freshfarms/standing-orders/{s['id']}", headers=h2,
                      json=_payload(lemon, milk)).status_code == 404
    # setting band
    client.put("/settings/portal", headers=auth, json={"allow_standing": False})
    assert client.get("/portal/freshfarms/standing-orders", headers=h).status_code == 403
    assert client.get("/portal/freshfarms/info").json()["allow_standing"] is False


def test_new_portal_order_notifies_staff(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    _, h = _login(client, auth)
    oid = client.post("/portal/freshfarms/orders", headers=h, json={
        "items": [{"product_id": lemon["id"], "quantity": 2}], "payment_method": "cod"}).json()["id"]
    n = client.get("/staff-notifications", headers=auth).json()["items"][0]
    assert n["kind"] == "portal_order" and n["link_url"] == f"/orders/{oid}" and "Joe's Cafe" in n["title"]


def test_customer_messages_edit_cancel_invoice(client, auth, db):
    _, _, lemon, milk = _setup(client, auth)
    _, h = _login(client, auth)
    oid = client.post("/portal/freshfarms/orders", headers=h, json={
        "items": [{"product_id": lemon["id"], "quantity": 2}, {"product_id": milk["id"], "quantity": 1}],
        "payment_method": "cod"}).json()["id"]
    o = client.get(f"/orders/{oid}", headers=auth).json()
    lines = [{"id": l["id"], "product_id": l["product_id"], "quantity": 3 if l["product_id"] == lemon["id"] else l["quantity"],
              "unit": l["unit"], "unit_price": l["unit_price"]} for l in o["lines"] if l["product_id"] == lemon["id"]]
    body = {"customer_id": o["customer_id"], "po_number": None, "delivery_date": "2030-02-04", "notes": None, "lines": lines}
    mailer.SENT.clear()
    assert client.put(f"/orders/{oid}", headers=auth, json=body).status_code == 200
    n = client.get("/portal/freshfarms/notifications", headers=h).json()["items"][0]
    assert n["title"] == f"Order #{oid} updated"
    assert "Lemons: 2 → 3" in n["body"] and "Removed Milk" in n["body"] and "Mon 04 Feb" in n["body"]
    assert len([m for m in mailer.SENT if m["to"] == "chef@joes.com"]) == 1
    # 15 min ke andar dobara edit -> wahi message (koi naya email nahi)
    mailer.SENT.clear()
    client.put(f"/orders/{oid}", headers=auth, json={**body, "delivery_date": "2030-02-05"})
    items = client.get("/portal/freshfarms/notifications", headers=h).json()["items"]
    assert len([i for i in items if "updated" in i["title"]]) == 1 and "Tue 05 Feb" in items[0]["body"]
    assert mailer.SENT == []
    # edit bina badlav -> koi message nahi
    before = len(items)
    client.put(f"/orders/{oid}", headers=auth, json={**body, "delivery_date": "2030-02-05"})
    assert len(client.get("/portal/freshfarms/notifications", headers=h).json()["items"]) == before

    # needs review cancel nahi (reject hai)
    assert client.post(f"/orders/{oid}/cancel", headers=auth, json={}).status_code == 400
    # internal invoice destination -> approve -> invoice message
    client.post("/destinations", headers=auth, json={"name": "Invoices", "type": "internal_invoice",
                                                     "config": {"auto_email": False}})
    client.post(f"/orders/{oid}/approve", headers=auth)
    titles = [i["title"] for i in client.get("/portal/freshfarms/notifications", headers=h).json()["items"]]
    assert f"Invoice for order #{oid}" in titles and f"Order #{oid} confirmed" in titles

    r = client.post(f"/orders/{oid}/cancel", headers=auth, json={"reason": "Truck broke down"}).json()
    assert r["status"] == "cancelled" and r["warnings"] == []
    from app.models import Invoice
    db.commit()
    db.expire_all()
    assert db.query(Invoice).filter_by(order_id=oid).one().status == "void"
    n = client.get("/portal/freshfarms/notifications", headers=h).json()["items"][0]
    assert n["title"] == f"Order #{oid} cancelled" and "Truck broke down" in n["body"]
    assert client.get(f"/portal/freshfarms/orders/{oid}", headers=h).json()["status_label"] == "Cancelled"


def test_cancel_returns_stock(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    client.put(f"/products/{lemon['id']}", headers=auth, json={"sku": "LEM", "name": "Lemons", "price": 10,
                                                               "is_tracked_inventory": True, "quantity_on_hand": 50})
    _, h = _login(client, auth)
    oid = client.post("/portal/freshfarms/orders", headers=h, json={
        "items": [{"product_id": lemon["id"], "quantity": 4}], "payment_method": "cod"}).json()["id"]
    client.post(f"/orders/{oid}/approve", headers=auth)
    assert client.get(f"/products/{lemon['id']}", headers=auth).json()["quantity_on_hand"] == 46
    client.post(f"/orders/{oid}/cancel", headers=auth)
    assert client.get(f"/products/{lemon['id']}", headers=auth).json()["quantity_on_hand"] == 50


def test_portal_product_picker(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    c, h = _login(client, auth)
    client.post(f"/customers/{c['id']}/pantry", headers=auth, json={"product_id": lemon["id"], "price": 8.5})
    rows = client.get("/portal/freshfarms/standing-orders/products", headers=h).json()
    assert {r["name"]: r["price"] for r in rows}["Lemons"] == 8.5
