"""Unicode / UTF-8: kisi bhi input (form, CSV, PDF, email, download) mein special characters sahi chalein."""
from app.core.text import clean_text, content_disposition, decode_bytes
from tests.test_imports import upload


def test_clean_text_rules():
    assert clean_text("Jalapeño") == "Jalapeño"                  # decomposed -> NFC (search / match same)
    assert clean_text("Roma​ Toms﻿") == "Roma Toms"           # zero-width + BOM gone
    assert clean_text("Fresh Farms") == "Fresh Farms"               # non-breaking space -> space
    assert clean_text("bad\x00\x07char\nnew line\ttab") == "badchar\nnew line\ttab"
    assert clean_text("Chilli 🌶️ 辣椒 मिर्च ₹10") == "Chilli 🌶️ 辣椒 मिर्च ₹10"  # real text stays
    assert clean_text(None) is None and clean_text(5) == 5


def test_decode_bytes_fallbacks():
    assert decode_bytes("Café".encode("utf-8-sig")) == "Café"
    assert decode_bytes("Café".encode("cp1252")) == "Café"              # Windows Excel CSV
    assert decode_bytes(bytes([0x81, 0x41])) == "\x81A"                  # never crashes


def test_saved_text_is_cleaned_and_searchable(client, auth):
    p = client.post("/products", headers=auth, json={"sku": "CH​-1", "name": "Jalapeño Chillies 🌶️ 辣椒"}).json()
    assert p["name"] == "Jalapeño Chillies 🌶️ 辣椒" and p["sku"] == "CH-1"
    assert client.get("/products?q=jalapeño", headers=auth).json()["total"] == 1
    assert client.get("/products?q=辣椒", headers=auth).json()["total"] == 1
    c = client.post("/customers", headers=auth, json={"name": "Café Ünïcödé & Co <Sydney>", "email": "x@café.com"}).json()
    assert c["name"] == "Café Ünïcödé & Co <Sydney>"
    s = client.post("/suppliers", headers=auth, json={"name": "Müller Frischwaren"}).json()
    assert s["name"] == "Müller Frischwaren"


def test_csv_import_windows_encoding(client, auth):
    csv = "Code,Name\nC1,Café Crème\nC2,Jalapeño\n".encode("cp1252")
    body = upload(client, auth, "products", csv)
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": body["mapping"]}).json()
    assert r["created"] == 2
    names = sorted(p["name"] for p in client.get("/products", headers=auth).json()["items"])
    assert names == ["Café Crème", "Jalapeño"]
    csv = "﻿Code,Name\nC3,中文 Name\n".encode("utf-8")
    body = upload(client, auth, "products", csv)
    assert body["headers"] == ["Code", "Name"]                            # BOM header mein nahi aaya


def test_pdfs_and_downloads_with_unicode(client, seeded, draft_order):
    from tests.test_destinations import approve
    client.put("/settings/business", headers=seeded, json={"name": "Fruit & Veg <Ünïcödé> ₹", "address": "1 Straße"})
    cid = draft_order["customer_id"]
    client.put(f"/customers/{cid}", headers=seeded, json={"name": "Joe's Café & Bar"})
    pid = draft_order["lines"][0]["product_id"]
    inv = client.post("/invoices", headers=seeded, json={"customer_id": cid, "lines": [
        {"description": "Crème fraîche 500g — 中文", "quantity": 1, "unit_price": 5}]}).json()
    pdf = client.get(f"/invoices/{inv['id']}/pdf", headers=seeded)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert b"+0" in pdf.content                                           # embedded TrueType subset, not Helvetica

    client.put("/settings/stock", headers=seeded, json={"po_prefix": "Bestellung-ü-"})
    sup = client.post("/suppliers", headers=seeded, json={"name": "Müller & Söhne", "email": "m@m.de"}).json()
    po = client.post("/purchase-orders", headers=seeded, json={"supplier_id": sup["id"], "notes": "Bitte <schnell> & früh",
                                                              "lines": [{"product_id": pid, "quantity": 1}]}).json()
    r = client.get(f"/purchase-orders/{po['id']}/pdf", headers=seeded)
    assert r.status_code == 200 and "filename*=UTF-8''Bestellung-%C3%BC-" in r.headers["content-disposition"]
    assert client.post(f"/purchase-orders/{po['id']}/send", headers=seeded, json={}).status_code == 200

    approve(client, seeded, draft_order)
    csv = client.get("/orders/export.csv", headers=seeded)
    assert csv.content.startswith("﻿".encode()) and "Joe's Café & Bar" in csv.content.decode("utf-8-sig")


def test_content_disposition_header():
    h = content_disposition("Rechnung ü 中文.pdf", inline=True)
    assert h.startswith('inline; filename="Rechnung u .pdf"') and "filename*=UTF-8''Rechnung%20%C3%BC%20%E4%B8%AD%E6%96%87.pdf" in h
    h.encode("latin-1")                                                   # HTTP header safe


def test_pdf_font_choice():
    from app.core.pdf_fonts import pdf_fonts
    assert pdf_fonts(["Café ₹ Straße"]) == ("OD-Sans", "OD-Sans-Bold")
    regular, _ = pdf_fonts(["中文 order"])
    assert regular in ("OD-CJK", "OD-Sans")                               # CJK font server pe ho to wo
