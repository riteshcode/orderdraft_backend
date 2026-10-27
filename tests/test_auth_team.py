"""Auth, roles, team invites, password reset, suspended tenant."""
import re

from app.core import mailer
from app.models import Tenant
from tests.conftest import register


def test_register_starts_trial(client, auth):
    me = client.get("/auth/me", headers=auth).json()
    assert me["role"] == "owner" and me["tenant"]["slug"] == "freshfarms"
    assert me["tenant"]["inbound_email"] == "freshfarms@orders.test.com"
    assert me["usage"]["subscription_status"] == "trialing" and me["usage"]["can_process"]
    assert me["usage"]["plan_code"] == "starter"


def test_duplicate_email_and_bad_login(client, auth):
    r = client.post("/auth/register", json={"business_name": "X", "name": "Y",
                                            "email": "ritesh@freshfarms.com.au", "password": "password123"})
    assert r.status_code == 400
    r = client.post("/auth/login", json={"email": "ritesh@freshfarms.com.au", "password": "wrong-pass"})
    assert r.status_code == 401


def test_password_reset_flow(client, auth):
    client.post("/auth/forgot-password", json={"email": "ritesh@freshfarms.com.au"})
    token = re.search(r"token=([\w\-\.]+)", mailer.SENT[-1]["html"]).group(1)
    r = client.post("/auth/reset-password", json={"token": token, "password": "newpassword1"})
    assert r.status_code == 200
    assert client.post("/auth/login", json={"email": "ritesh@freshfarms.com.au", "password": "newpassword1"}).status_code == 200
    # Same link dobara nahi chalega
    assert client.post("/auth/reset-password", json={"token": token, "password": "another123"}).status_code == 400


def test_invite_accept_and_reviewer_permissions(client, auth):
    r = client.post("/team/invite", headers=auth, json={"email": "rev@freshfarms.com.au", "role": "reviewer"})
    assert r.status_code == 200, r.text
    token = r.json()["invite_link"].split("token=")[1]
    assert client.get(f"/auth/invite/{token}").json()["tenant_name"] == "Fresh Farms Pty Ltd"

    r = client.post("/auth/accept-invite", json={"token": token, "name": "Rev", "password": "password123"})
    rev = {"Authorization": f"Bearer {r.json()['token']}"}
    assert r.json()["user"]["role"] == "reviewer"

    assert client.get("/orders", headers=rev).status_code == 200           # orders dekh sakta hai
    assert client.post("/destinations", headers=rev, json={"name": "x", "type": "webhook",
                                                           "config": {"url": "https://x.com"}}).status_code == 403
    assert client.get("/team", headers=rev).status_code == 403
    assert client.post("/billing/checkout", headers=rev, json={"plan_code": "growth"}).status_code == 403

    team = client.get("/team", headers=auth).json()
    assert len(team["members"]) == 2 and team["invites"] == []


def test_user_limit_enforced(client, auth):
    # Starter plan = 2 users
    assert client.post("/team/invite", headers=auth, json={"email": "a@x.com"}).status_code == 200
    r = client.post("/team/invite", headers=auth, json={"email": "b@x.com"})
    assert r.status_code == 400 and "upgrade" in r.json()["detail"]


def test_last_owner_protected(client, auth):
    me = client.get("/auth/me", headers=auth).json()
    r = client.put(f"/team/members/{me['id']}", headers=auth, json={"role": "admin", "is_active": True})
    assert r.status_code == 400


def test_suspended_tenant_blocked(client, auth, db):
    t = db.query(Tenant).first()
    t.status = "suspended"
    db.commit()
    assert client.get("/orders", headers=auth).status_code == 403


def test_superadmin_from_env(client):
    h = register(client, email="boss@platform.com", business="Platform")
    assert client.get("/auth/me", headers=h).json()["is_superadmin"]
    assert client.get("/admin/stats", headers=h).status_code == 200


def test_requires_login(client):
    assert client.get("/orders").status_code == 401
