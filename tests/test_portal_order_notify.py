"""Portal order approve / reject -> customer ko bell + email (staff notification list mein nahi aata)."""
from app.core import mailer
from tests.test_portal_shop import _login, _setup


def _order(client, h, product_id):
    r = client.post("/portal/freshfarms/orders", headers=h, json={
        "items": [{"product_id": product_id, "quantity": 2}], "payment_method": "cod", "delivery_date": "2030-01-07"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_confirmed_notification_and_email(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    _, h = _login(client, auth)
    oid = _order(client, h, lemon["id"])
    mailer.SENT.clear()
    assert client.post(f"/orders/{oid}/approve", headers=auth).status_code == 200

    items = client.get("/portal/freshfarms/notifications", headers=h).json()["items"]
    assert items[0]["title"] == f"Order #{oid} confirmed" and "Mon 07 Jan" in items[0]["body"]
    assert items[0]["link_url"] == f"/portal/freshfarms/orders/{oid}" and items[0]["read"] is False
    mail = [m for m in mailer.SENT if m["to"] == "chef@joes.com"]
    assert len(mail) == 1 and "confirmed" in mail[0]["subject"]
    # staff ki "Notify customers" list mein order wale nahi
    assert client.get("/portal-notifications", headers=auth).json() == []


def test_rejected_with_reason(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    _, h = _login(client, auth)
    oid = _order(client, h, lemon["id"])
    mailer.SENT.clear()
    r = client.post(f"/orders/{oid}/reject", headers=auth, json={"reason": "Out of lemons this week"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    n = client.get("/portal/freshfarms/notifications", headers=h).json()["items"][0]
    assert "not accepted" in n["title"] and "Out of lemons this week" in n["body"]
    assert "Out of lemons" in [m for m in mailer.SENT if m["to"] == "chef@joes.com"][0]["html"]
    act = client.get(f"/activity/entity/order/{oid}", headers=auth).json()["items"]
    assert any(a["action"] == "order.rejected" and a["detail"]["reason"] == "Out of lemons this week" for a in act)


def test_reject_without_body_and_non_portal_orders_skip(client, auth, seeded, draft_order):
    # email / AI order: koi portal notification nahi, reject bina body ke bhi chalta hai
    assert client.post(f"/orders/{draft_order['id']}/reject", headers=seeded).status_code == 200


def test_no_duplicate_email_when_confirmation_email_goes_to_same_address(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    c, h = _login(client, auth)
    client.put(f"/customers/{c['id']}", headers=auth, json={"name": c["name"], "email": "chef@joes.com"})
    client.put("/settings", headers=auth, json={"order_email_enabled": True})
    oid = _order(client, h, lemon["id"])
    mailer.SENT.clear()
    client.post(f"/orders/{oid}/approve", headers=auth)
    portal_mails = [m for m in mailer.SENT if m["to"] == "chef@joes.com" and m["subject"].startswith(f"Order #{oid} confirmed")]
    assert portal_mails == []          # order confirmation email hi jayega (wahi address)
    assert client.get("/portal/freshfarms/notifications", headers=h).json()["unread"] == 1
