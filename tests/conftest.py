"""
Test setup. Claude, Deepgram, accounting software, Stripe sab MOCK - tests bina API keys ke chalte hain.
    pytest -v
MySQL pe chalana ho (main + har client ka alag DB banega):
    TEST_DATABASE_URL=mysql+pymysql://user:pass@localhost/od_test \
    TEST_TENANT_DATABASE_URL="mysql+pymysql://user:pass@localhost/{db_name}" pytest
"""
import glob
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_tmp}/main.db")
os.environ["TENANT_DATABASE_URL"] = os.getenv("TEST_TENANT_DATABASE_URL", f"sqlite:///{_tmp}/{{db_name}}.db")
os.environ["TENANT_DB_PREFIX"] = "odtest_t_"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["LOG_DIR"] = f"{_tmp}/logs"
os.environ["JOBS_MODE"] = "sync"
os.environ["AUTO_MIGRATE"] = "false"
os.environ["RUN_SCHEDULER_IN_APP"] = "false"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["DEEPGRAM_API_KEY"] = "test-key"
os.environ["INBOUND_DOMAIN"] = "orders.test.com"
os.environ["INBOUND_WEBHOOK_USER"] = "pm"
os.environ["INBOUND_WEBHOOK_PASS"] = "secret"
os.environ["XERO_WEBHOOK_KEY"] = "xero-webhook-key"
os.environ["XERO_CLIENT_ID"] = "xero-id"
os.environ["XERO_CLIENT_SECRET"] = "xero-secret"
os.environ["QUICKBOOKS_CLIENT_ID"] = "qbo-id"
os.environ["QUICKBOOKS_CLIENT_SECRET"] = "qbo-secret"
os.environ["QUICKBOOKS_WEBHOOK_VERIFIER"] = "qbo-verifier"
os.environ["MYOB_CLIENT_ID"] = "myob-key"
os.environ["MYOB_CLIENT_SECRET"] = "myob-secret"
os.environ["SUPERADMIN_EMAILS"] = "boss@platform.com"
os.environ["POSTMARK_SERVER_TOKEN"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import mailer
from app.core.database import (
    MainBase, SessionLocal, TenantBase, bind_tenant, dispose_tenant_engines, main_engine, tenant_db_url,
)
from app.main import app
from app.seed import seed_plans

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
PM_AUTH = ("pm", "secret")


def _drop_tenant_dbs():
    dispose_tenant_engines()
    url = os.environ["TENANT_DATABASE_URL"]
    if url.startswith("sqlite"):
        for f in glob.glob(f"{_tmp}/odtest_t_*.db"):
            os.remove(f)
        return
    with main_engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SHOW DATABASES LIKE 'odtest\\_t\\_%'"))]
        for name in names:
            conn.execute(text(f"DROP DATABASE `{name}`"))


@pytest.fixture(autouse=True)
def fresh_db():
    _drop_tenant_dbs()
    MainBase.metadata.drop_all(bind=main_engine)
    MainBase.metadata.create_all(bind=main_engine)
    db = SessionLocal()
    seed_plans(db)
    db.close()
    mailer.SENT.clear()
    yield


class TestSession(Session):
    """
    Tests ke liye: pehli baar client ka data chhua to (ya naya row add kiya to)
    us tenant ke DB se apne aap bind. App code mein aisa nahi hota - wahan current_user bind karta hai.
    """

    def _auto_bind(self, tenant_id=None):
        if "tenant_id" in self.info:
            return
        if tenant_id is None:
            from app.models import Tenant
            first = self.query(Tenant).order_by(Tenant.id).first()
            if not first:
                return
            tenant_id = first.id
        bind_tenant(self, tenant_id)

    def add(self, instance, _warn=True):
        if isinstance(instance, TenantBase):
            self._auto_bind(getattr(instance, "tenant_id", None))
        return super().add(instance, _warn=_warn)

    def get_bind(self, mapper=None, clause=None, **kw):
        if mapper is not None and "tenant_id" not in self.info:
            cls = getattr(mapper, "class_", mapper)
            if isinstance(cls, type) and issubclass(cls, TenantBase):
                self._auto_bind()
        return super().get_bind(mapper=mapper, clause=clause, **kw)


@pytest.fixture
def db():
    session = TestSession(binds={MainBase: main_engine}, autoflush=False, expire_on_commit=False)
    yield session
    session.close()


def tenant_db(tenant_id: int):
    """Kisi specific client ke DB ka session (isolation tests)."""
    s = TestSession(binds={MainBase: main_engine}, autoflush=False, expire_on_commit=False)
    bind_tenant(s, tenant_id)
    return s


@pytest.fixture
def client():
    return TestClient(app)


def register(client, email="ritesh@freshfarms.com.au", business="Fresh Farms Pty Ltd"):
    r = client.post("/auth/register", json={"business_name": business, "name": "Ritesh",
                                            "email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def auth(client):
    return register(client)


def upload_and_run(client, headers, kind, filename, mapping=None):
    """Import wizard ke dono steps ek saath (tests ke liye)."""
    with open(os.path.join(SAMPLE_DIR, filename), "rb") as f:
        up = client.post(f"/imports/{kind}/upload", headers=headers, files={"file": (filename, f, "text/csv")})
    assert up.status_code == 200, up.text
    body = up.json()
    r = client.post(f"/imports/{kind}/run", headers=headers,
                    json={"upload_id": body["upload_id"], "mapping": mapping or body["mapping"]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def seeded(client, auth):
    upload_and_run(client, auth, "products", "products.csv")
    upload_and_run(client, auth, "customers", "customers.csv")
    upload_and_run(client, auth, "history", "history.csv")
    return auth


@pytest.fixture
def mock_ai(monkeypatch):
    """mock_ai.order = {...} set karo - extract_order wahi return karega."""
    from app.modules.intake import voice_parser
    from app.modules.parsing import extractor

    class Mock:
        order = {"is_order": False, "items": []}
        picks = {}
        transcript = ""
        calls = []

    m = Mock()

    def fake_extract(text, pdf_files=None, image_files=None, known_customers=None, today=None, on_usage=None):
        m.calls.append({"text": text, "pdfs": len(pdf_files or []), "images": len(image_files or [])})
        if on_usage:
            on_usage("claude_extract", 1000, 200)
        return m.order

    def fake_pick(lines, on_usage=None):
        m.calls.append({"pick": lines})
        return m.picks

    def fake_transcribe(content, content_type="audio/mpeg", keyterms=None, on_usage=None):
        m.calls.append({"transcribe": len(content), "keyterms": keyterms})
        if on_usage:
            on_usage(30.0)
        return m.transcript

    monkeypatch.setattr(extractor, "extract_order", fake_extract)
    monkeypatch.setattr(extractor, "pick_products", fake_pick)
    monkeypatch.setattr(voice_parser, "transcribe_audio", fake_transcribe)
    return m


def postmark_payload(body: str, subject="Order", sender="orders@joescafe.com.au", attachments=None,
                     to="freshfarms@orders.test.com"):
    return {
        "FromFull": {"Email": sender, "Name": "Joe"},
        "ToFull": [{"Email": to, "MailboxHash": ""}],
        "Subject": subject, "TextBody": body, "Attachments": attachments or [],
    }


JOE_ORDER = {
    "is_order": True, "customer_name": "Joe's Cafe", "customer_email": "orders@joescafe.com.au",
    "po_number": "J-55", "delivery_date": "2026-10-20", "notes": "Back door",
    "items": [
        {"raw_text": "2 box romas", "product_hint": "romas", "quantity": 2, "unit": "box"},
        {"raw_text": "6 cos", "product_hint": "cos lettuce", "quantity": 6, "unit": "each"},
    ],
}


@pytest.fixture
def draft_order(client, seeded, mock_ai):
    """Joe ka ek draft order (needs_review). Returns order detail."""
    mock_ai.order = JOE_ORDER
    r = client.post("/inbound/postmark", auth=PM_AUTH, json=postmark_payload("2 box romas\n6 cos"))
    assert r.json()["ok"], r.text
    orders = client.get("/orders", headers=seeded).json()["items"]
    return client.get(f"/orders/{orders[0]['id']}", headers=seeded).json()


class FakeApi:
    """
    Accounting software ka fake HTTP. routes[(METHOD, path)] = response
      response: dict/list (200 JSON) | (status, dict) | callable(json, params) | Exception
    path = provider api_base ke baad ka hissa ("/Invoices"), ya poora URL (absolute calls).
    """

    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []


@pytest.fixture
def fake_http(monkeypatch):
    """Sirf fake HTTP (connection khud banao) - connect flow tests ke liye."""
    import httpx

    from app.modules.connectors.api import ApiSession

    fake = FakeApi()

    def fake_send(self, method, url, headers, params=None, json=None, content=None):
        base = self.provider.api_base(self.conn)
        path = url[len(base):] if url.startswith(base) else url
        fake.calls.append({"method": method, "path": path, "url": url, "params": params, "json": json,
                           "headers": headers})
        result = fake.routes.get((method, path.split("?")[0]), {})
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(json, params)
        if isinstance(result, httpx.Response):
            return result
        status = 200
        if isinstance(result, tuple):
            status, result = result
        return httpx.Response(status, json=result, request=httpx.Request(method, url))

    monkeypatch.setattr(ApiSession, "send", fake_send)
    return fake


@pytest.fixture
def fake_accounting(fake_http, db, auth):
    """fake = fake_accounting("quickbooks", org_id="123", extra={...}) - connection + fake HTTP."""
    from datetime import timedelta

    from app.core.crypto import encrypt
    from app.core.database import utcnow
    from app.models import Connection, IntegrationLink, Tenant

    fake = fake_http

    def make(provider="xero", org_id="xt-1", org_name="Fresh Farms Demo", extra=None):
        tenant = db.query(Tenant).first()
        db.add(Connection(tenant_id=tenant.id, provider=provider, status="connected", access_token=encrypt("at"),
                          refresh_token=encrypt("rt"), expires_at=utcnow() + timedelta(hours=1),
                          external_tenant_id=org_id, external_name=org_name, extra=extra or {}))
        db.add(IntegrationLink(provider=provider, external_id=org_id, tenant_id=tenant.id))
        tenant.accounting_provider = provider
        db.commit()
        return fake

    return make


@pytest.fixture
def fake_xero(fake_accounting):
    return fake_accounting("xero")
