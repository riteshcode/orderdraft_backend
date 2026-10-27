"""Billing (limits + Stripe webhook), super admin, reports, onboarding, scheduler."""
from datetime import timedelta

from app.core.database import utcnow
from app.models import DraftOrder, Plan, Tenant
from app.modules.billing import stripe_service
from tests.conftest import JOE_ORDER, PM_AUTH, postmark_payload, register


def test_billing_page_and_no_stripe(client, auth):
    b = client.get("/billing", headers=auth).json()
    assert [p["code"] for p in b["plans"]] == ["starter", "growth", "pro"]
    assert b["usage"]["orders_limit"] == 300 and not b["stripe_enabled"]
    r = client.post("/billing/checkout", headers=auth, json={"plan_code": "growth"})
    assert r.status_code == 400


def test_order_limit_blocks_at_hard_limit(client, seeded, mock_ai, db):
    plan = db.query(Plan).filter_by(code="starter").one()
    plan.order_limit = 2      # hard limit 120% = 2.4 -> 3 orders chalenge, 4th blocked
    db.commit()
    mock_ai.order = JOE_ORDER
    for _ in range(4):
        client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 romas"))
    assert db.query(DraftOrder).count() == 3
    statuses = [m["status"] for m in client.get("/inbound/messages", headers=seeded).json()["items"]]
    assert statuses.count("done") == 3 and statuses.count("blocked") == 1
    usage = client.get("/auth/me", headers=seeded).json()["usage"]
    assert usage["orders_used"] == 3 and usage["percent"] == 150.0 and not usage["can_process"]


def test_stripe_webhook_updates_subscription(client, auth, db, monkeypatch):
    plan = db.query(Plan).filter_by(code="growth").one()
    plan.stripe_price_id = "price_growth"
    db.commit()
    tenant = db.query(Tenant).first()
    now = int(utcnow().timestamp())
    event = {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"tenant_id": str(tenant.id)},
        "items": {"data": [{"price": {"id": "price_growth"}, "current_period_start": now,
                            "current_period_end": now + 30 * 86400}]}}}}
    monkeypatch.setattr(stripe_service, "verify_event", lambda payload, sig: event)
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "x"})
    assert r.status_code == 200
    db.rollback()
    db.expire_all()
    t = db.get(Tenant, tenant.id)
    assert t.subscription_status == "active" and t.plan_id == plan.id and t.stripe_customer_id == "cus_1"
    assert t.current_period_end > utcnow() + timedelta(days=29)
    assert client.get("/auth/me", headers=auth).json()["usage"]["plan_code"] == "growth"

    event["data"]["object"]["status"] = "canceled"
    client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "x"})
    db.rollback()
    db.expire_all()
    assert db.get(Tenant, tenant.id).subscription_status == "canceled"


def test_stripe_webhook_bad_signature(client):
    assert client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "bad"}).status_code == 400


def test_superadmin(client, draft_order, seeded, db):
    boss = register(client, email="boss@platform.com", business="Platform HQ")
    assert client.get("/admin/stats", headers=seeded).status_code == 403

    stats = client.get("/admin/stats", headers=boss).json()
    assert stats["tenants_total"] == 2 and stats["orders_30d"] == 1 and stats["ai_cost_usd_30d"] >= 0
    tenants = client.get("/admin/tenants?q=fresh", headers=boss).json()
    ff = tenants["items"][0]
    assert ff["slug"] == "freshfarms" and ff["orders_30d"] == 1

    detail = client.put(f"/admin/tenants/{ff['id']}", headers=boss, json={"subscription_status": "active",
                                                                           "plan_id": 3}).json()
    assert detail["subscription_status"] == "active" and detail["usage"]["plan_code"] == "pro"

    imp = client.post(f"/admin/tenants/{ff['id']}/impersonate", headers=boss).json()
    imp_h = {"Authorization": f"Bearer {imp['token']}"}
    me = client.get("/auth/me", headers=imp_h).json()
    assert me["impersonating"] and me["tenant"]["slug"] == "freshfarms"
    assert client.get("/admin/stats", headers=imp_h).status_code == 403   # impersonate token admin nahi

    client.put(f"/admin/tenants/{ff['id']}", headers=boss, json={"status": "suspended"})
    assert client.get("/orders", headers=seeded).status_code == 403

    p = client.post("/admin/plans", headers=boss, json={"code": "enterprise", "name": "Enterprise",
                                                        "price_aud_month": 1999, "order_limit": 10000}).json()
    assert p["code"] == "enterprise"
    assert len(client.get("/admin/plans", headers=boss).json()) == 4


def test_dashboard_and_onboarding(client, seeded, draft_order):
    ob = client.get("/onboarding", headers=seeded).json()
    done = {s["key"]: s["done"] for s in ob["steps"]}
    assert done == {"catalog": True, "customers": True, "destination": False, "first_order": True,
                    "first_approval": False}

    client.post("/destinations", headers=seeded, json={"name": "Inv", "type": "internal_invoice", "config": {}})
    client.post(f"/orders/{draft_order['id']}/approve", headers=seeded)
    d = client.get("/reports/dashboard?days=7", headers=seeded).json()
    assert d["orders"]["received"] == 1 and d["orders"]["approved"] == 1
    assert d["orders"]["approved_value"] == 80.6
    assert d["accuracy_percent"] == 100.0 and d["time_saved_hours"] > 0
    assert d["invoices"]["count"] == 1 and d["by_channel"] == {"email": 1}
    assert d["top_customers"][0]["name"] == "Joe's Cafe Bondi"
    assert len(d["series"]) == 7 and d["series"][-1]["orders"] == 1

    acts = [a["action"] for a in client.get("/activity", headers=seeded).json()]
    assert "order.approved" in acts and "import.products" in acts


def test_scheduler_trial_expiry_and_stuck(client, auth, db):
    from app.models import InboundMessage
    from app.scheduler import run_due_tasks
    t = db.query(Tenant).first()
    t.trial_ends_at = utcnow() - timedelta(hours=1)
    db.add(InboundMessage(tenant_id=t.id, channel="manual", status="processing",
                          created_at=utcnow() - timedelta(hours=1)))
    db.commit()
    run_due_tasks(force=True)
    db.rollback()
    db.expire_all()
    assert db.get(Tenant, t.id).subscription_status == "expired"
    assert db.query(InboundMessage).one().status == "failed"
    assert not client.get("/auth/me", headers=auth).json()["usage"]["can_process"]


def test_public_plans_no_login(client):
    r = client.get("/public/plans")
    assert r.status_code == 200
    data = r.json()
    assert data["trial_days"] == 14 and [p["code"] for p in data["plans"]] == ["starter", "growth", "pro"]
    assert "stripe_price_id" not in data["plans"][0]
