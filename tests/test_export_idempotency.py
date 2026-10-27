"""Export idempotency: network drop ke baad retry pe accounting software mein duplicate invoice nahi banna chahiye."""
import httpx

from app.models import ExportJob
from tests.test_myob import _prepare, add_destination, approve, myob
from tests.test_quickbooks import query_router, setup_export


def test_myob_retry_finds_invoice_from_earlier_attempt(client, seeded, draft_order, fake_accounting, db):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    # Pehli koshish: request chali gayi par response nahi aaya (timeout) - MYOB mein invoice ban chuka hai
    fake.routes[("POST", "/Sale/Invoice/Service")] = httpx.ReadTimeout("timed out")
    add_destination(client, seeded, document_type="invoice_authorised", default_account_code="4-1100")
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "failed" and job["next_retry_at"]

    number = f"OD{draft_order['id']}"
    fake.routes[("GET", "/Sale/Invoice")] = lambda body, params: {
        "Items": [{"UID": "inv-early", "Number": number, "Status": "Open", "TotalAmount": 71.5}]
        if params.get("$filter") == f"Number eq '{number}'" else []}
    fake.routes[("POST", "/Sale/Invoice/Service")] = {"UID": "inv-duplicate", "Number": number}
    assert client.post(f"/exports/{job['id']}/retry", headers=seeded).status_code == 200

    job = client.get(f"/orders/{draft_order['id']}", headers=seeded).json()["exports"][0]
    assert job["status"] == "success" and job["external_id"] == "inv-early", job
    assert "earlier attempt" in job["response_summary"]
    posts = [c for c in fake.calls if c["method"] == "POST" and c["path"].startswith("/Sale/Invoice/Service")]
    assert len(posts) == 1                                         # dobara POST nahi hua


def test_myob_first_attempt_does_not_search(client, seeded, draft_order, fake_accounting):
    fake = myob(fake_accounting)
    _prepare(client, seeded, fake)
    fake.routes[("POST", "/Sale/Invoice/Service")] = {"UID": "inv-1", "Number": "OD1"}
    add_destination(client, seeded, document_type="invoice_authorised", default_account_code="4-1100")
    assert approve(client, seeded, draft_order)["exports"][0]["status"] == "success"
    assert not [c for c in fake.calls if c["method"] == "GET" and c["path"] == "/Sale/Invoice"]


def test_quickbooks_requestid_same_on_retry(client, seeded, draft_order, fake_accounting, db):
    fake = fake_accounting("quickbooks", org_id="9130")
    fake.routes[("GET", "/query")] = query_router({"Customer": [{"Id": "58"}], "Item": [{"Id": "33"}]})
    fake.routes[("POST", "/invoice")] = (503, {})
    setup_export(client, seeded, fake, db)
    job = approve(client, seeded, draft_order)["exports"][0]
    client.post(f"/exports/{job['id']}/retry", headers=seeded)
    ids = [c["params"]["requestid"] for c in fake.calls if c["method"] == "POST" and c["path"] == "/invoice"]
    assert len(ids) == 2 and ids[0] == ids[1] == f"orderdraft-{db.get(ExportJob, job['id']).tenant_id}-{draft_order['id']}-{job['id']}"


def test_xero_idempotency_header(client, seeded, draft_order, fake_xero):
    fake_xero.routes[("POST", "/Invoices")] = {"Invoices": [{"InvoiceID": "x-1", "InvoiceNumber": "INV-1",
                                                             "Status": "DRAFT", "Total": 71.5}]}
    client.post("/destinations", headers=seeded, json={"name": "Xero", "type": "accounting",
                                                       "config": {"document_type": "invoice_draft"}})
    job = approve(client, seeded, draft_order)["exports"][0]
    assert job["status"] == "success", job
    call = [c for c in fake_xero.calls if c["path"] == "/Invoices"][0]
    assert call["headers"]["Idempotency-Key"].endswith(f"-{draft_order['id']}-{job['id']}")
