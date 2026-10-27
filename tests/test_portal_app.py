"""Portal v2: Web Push (encryption), notifications, PWA manifest/icons, branding, staff login-as, set password."""
import io
import json
import re

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from app.core import mailer, webpush
from app.core.config import settings
from tests.test_portal_shop import _login, _setup


# ---------------- Web Push crypto ----------------
def _browser_keys():
    """Browser ki taraf: private key + subscription keys (jaise pushManager.subscribe deta hai)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    return priv, webpush.b64url(webpush._public_bytes(priv.public_key())), webpush.b64url(auth), auth


def _decrypt(body: bytes, priv, auth: bytes) -> bytes:
    salt, idlen = body[:16], body[20]
    as_public = body[21:21 + idlen]
    ua_public = webpush._public_bytes(priv.public_key())
    shared = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = webpush._hkdf(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_webpush_encrypt_roundtrip_and_vapid(monkeypatch):
    priv, p256dh, auth_b64, auth = _browser_keys()
    msg = json.dumps({"title": "Hi Café ☕", "body": "x" * 500}).encode()
    body = webpush.encrypt(msg, p256dh, auth_b64)
    assert int.from_bytes(body[16:20], "big") == 4096 and body[20] == 65
    assert _decrypt(body, priv, auth) == msg

    private, public = webpush.generate_vapid_keys()
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", private)
    assert webpush.vapid_public_key() == public and webpush.push_enabled()
    header = webpush._vapid_header("https://fcm.googleapis.com/fcm/send/abc")
    token = re.search(r"t=([^,]+)", header).group(1)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64url_decode(public))
    claims = jwt.decode(token, pub, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"].startswith("mailto:") and f"k={public}" in header


def test_send_push_statuses(monkeypatch):
    private, _ = webpush.generate_vapid_keys()
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", private)
    _, p256dh, auth_b64, _ = _browser_keys()
    sub = {"endpoint": "https://push.example.com/x", "p256dh": p256dh, "auth": auth_b64}

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, ""
    for code, expected in ((201, "ok"), (410, "gone"), (404, "gone"), (500, "error: 500 ")):
        monkeypatch.setattr(webpush.httpx, "post", lambda *a, code=code, **k: R(code))
        assert webpush.send_push(sub, {"title": "t"}) == expected


# ---------------- notifications ----------------
def test_customer_notifications_push_email_and_bell(client, auth, monkeypatch):
    private, _ = webpush.generate_vapid_keys()
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", private)
    sent = []
    import app.modules.portal.notify as notify
    monkeypatch.setattr(notify, "send_push", lambda sub, msg: sent.append((sub["endpoint"], msg)) or
                        ("gone" if "dead" in sub["endpoint"] else "ok"))
    _setup(client, auth)
    c1, h1 = _login(client, auth, "Cafe A", "a@a.com")
    c2, h2 = _login(client, auth, "Cafe B", "b@b.com")
    info = client.get("/portal/freshfarms/info").json()
    assert info["push_public_key"] and info["branding"]["font"] == "modern"
    _, p256dh, auth_b64, _ = _browser_keys()
    for ep in ("https://push.example.com/a1", "https://push.example.com/dead"):
        r = client.post("/portal/freshfarms/push/subscribe", headers=h1,
                        json={"endpoint": ep, "keys": {"p256dh": p256dh, "auth": auth_b64}})
        assert r.status_code == 200, r.text
    assert client.post("/portal/freshfarms/push/subscribe", headers=h1,
                       json={"endpoint": "http://x", "keys": {}}).status_code == 400

    mailer.SENT.clear()
    r = client.post("/portal-notifications", headers=auth, json={
        "title": "Christmas hours", "body": "Closed 25-26 Dec", "send_email": True}).json()
    assert r["recipients"] == 2 and r["push_sent"] == 1 and r["email_sent"] == 2
    assert sent[0][1]["title"] == "Christmas hours" and sent[0][1]["url"].startswith("/portal/freshfarms")
    assert {m["to"] for m in mailer.SENT} == {"a@a.com", "b@b.com"}
    # "gone" subscription hat gayi
    logins = {l["email"]: l for l in client.get("/portal-logins", headers=auth).json()}
    assert logins["a@a.com"]["push_devices"] == 1 and logins["a@a.com"]["customer_name"] == "Cafe A"

    only_b = client.post("/portal-notifications", headers=auth, json={
        "title": "Your order is ready", "audience": "customers", "customer_ids": [c2["id"]], "send_push": False}).json()
    assert only_b["recipients"] == 1
    a = client.get("/portal/freshfarms/notifications", headers=h1).json()
    b = client.get("/portal/freshfarms/notifications", headers=h2).json()
    assert [x["title"] for x in a["items"]] == ["Christmas hours"] and a["unread"] == 1
    assert b["unread"] == 2
    client.post("/portal/freshfarms/notifications/read", headers=h2, json={})
    assert client.get("/portal/freshfarms/notifications", headers=h2).json()["unread"] == 0
    hist = client.get("/portal-notifications", headers=auth).json()
    assert hist[0]["customers"] == ["Cafe B"] and hist[0]["read_by"] == 1
    assert client.post("/portal-notifications", headers=auth, json={
        "title": "x", "audience": "customers", "customer_ids": [999999]}).status_code == 400


# ---------------- PWA ----------------
def test_manifest_and_icons(client, auth):
    _setup(client, auth)
    m = client.get("/portal/freshfarms/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["start_url"].endswith("/portal/freshfarms") and data["display"] == "standalone"
    assert data["icons"][0]["src"].endswith("/portal/freshfarms/icon-192.png")
    png = client.get("/portal/freshfarms/icon-512.png")
    im = Image.open(io.BytesIO(png.content))
    assert im.size == (512, 512) and im.getpixel((5, 5)) == (37, 99, 235)       # initials on default blue
    assert client.get("/portal/freshfarms/icon-64.png").status_code == 404
    # brand colour + uploaded icon
    s = client.get("/settings/portal", headers=auth).json()
    client.put("/settings/portal", headers=auth, json={**s, "brand_color": "#16A34A", "font": "classic",
                                                       "header_style": "dark", "announcement": "Free delivery over $200"})
    m = client.get("/portal/freshfarms/manifest.webmanifest").json()
    assert m["theme_color"] == m["background_color"] == "#0f172a"                 # status bar = dark header
    assert [x["name"] for x in m["shortcuts"]] == ["Shop", "Cart", "My orders"]
    client.put("/settings/portal", headers=auth, json={"header_style": "brand"})
    assert client.get("/portal/freshfarms/manifest.webmanifest").json()["theme_color"] == "#16a34a"
    client.put("/settings/portal", headers=auth, json={"header_style": "dark"})
    assert Image.open(io.BytesIO(client.get("/portal/freshfarms/icon-192.png").content)).getpixel((3, 3)) == (22, 163, 74)
    img = io.BytesIO()
    Image.new("RGB", (300, 300), "red").save(img, "PNG")
    r = client.post("/settings/portal/icon", headers=auth, files={"file": ("i.png", img.getvalue(), "image/png")}).json()
    assert r["icon_url"]
    icon = Image.open(io.BytesIO(client.get("/portal/freshfarms/icon-192.png").content)).convert("RGB")
    assert icon.getpixel((96, 96)) == (255, 0, 0) and icon.getpixel((1, 1)) == (255, 255, 255)
    info = client.get("/portal/freshfarms/info").json()["branding"]
    assert info["announcement"] == "Free delivery over $200" and info["header_style"] == "dark"
    bad = client.put("/settings/portal", headers=auth, json={**s, "brand_color": "green"})
    assert bad.status_code == 400
    client.delete("/settings/portal/icon", headers=auth)
    assert client.get("/settings/portal", headers=auth).json()["icon_url"] is None


# ---------------- staff: login as + set password ----------------
def test_staff_login_as_and_set_password(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    c, h = _login(client, auth)
    lid = client.get(f"/customers/{c['id']}/portal-logins", headers=auth).json()["items"][0]["id"]

    r = client.post(f"/customers/{c['id']}/portal-logins/{lid}/login-as", headers=auth).json()
    assert r["url"].startswith("/portal/freshfarms/impersonate#token=")
    imp = {"Authorization": f"Bearer {r['token']}"}
    me = client.get("/portal/me", headers=imp).json()
    assert me["impersonated_by"] == "Ritesh" and me["must_change_password"] is False
    oid = client.post("/portal/freshfarms/orders", headers=imp, json={
        "items": [{"product_id": lemon["id"], "quantity": 2}], "payment_method": "cod"}).json()["id"]
    hist = client.get(f"/activity/entity/order/{oid}", headers=auth).json()["items"]
    assert hist[0]["user_name"] == "Ritesh" and hist[0]["detail"]["placed_by_staff"]
    assert client.post("/portal/change-password", headers=imp,
                       json={"current_password": "x", "new_password": "whatever123"}).status_code == 403

    # staff sets a password, no email, must change
    mailer.SENT.clear()
    r = client.post(f"/customers/{c['id']}/portal-logins/{lid}/password", headers=auth,
                    json={"password": "Temp-2026!", "must_change": True})
    assert r.status_code == 200 and r.json()["must_change_password"] and mailer.SENT == []
    assert client.post("/portal/freshfarms/login", json={"email": "chef@joes.com", "password": "chefpass1"}).status_code == 401
    login = client.post("/portal/freshfarms/login", json={"email": "chef@joes.com", "password": "Temp-2026!"}).json()
    assert login["login"]["must_change_password"]
    # with email, no forced change
    r = client.post(f"/customers/{c['id']}/portal-logins/{lid}/password", headers=auth,
                    json={"password": "Final-pass-1", "must_change": False, "send_email": True}).json()
    assert r["must_change_password"] is False and "Final-pass-1" in mailer.SENT[-1]["text"]
    assert client.post(f"/customers/{c['id']}/portal-logins/{lid}/password", headers=auth,
                       json={"password": "short"}).status_code == 400
    acts = [a["action"] for a in client.get(f"/activity/entity/customer/{c['id']}", headers=auth).json()["items"]]
    assert "customer.portal_login_as" in acts and "customer.portal_password_reset" in acts
