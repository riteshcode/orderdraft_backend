"""Pure functions (DB/API nahi)."""
import base64
import io
from types import SimpleNamespace

from reportlab.pdfgen import canvas

from app.core.crypto import MASK, decrypt, encrypt
from app.modules.destinations.base import get_path, set_path
from app.modules.destinations.custom_api import build_body, build_headers
from app.modules.imports.reader import read_table, suggest_mapping
from app.modules.intake.email_parser import html_to_text, parse_postmark
from app.modules.intake.pdf_parser import extract_pdf_text
from app.modules.intake.storage import detect_kind
from app.modules.invoices.service import calculate_lines
from app.modules.parsing.text_utils import clean_email, clean_phone, normalize


def make_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def test_normalize_and_cleaners():
    assert normalize("  2x Roma TOMS (10kg)!! ") == "roma tomatoes 10kg"
    assert clean_email("Joe <Joe@Cafe.com.au>") == "joe@cafe.com.au"
    assert clean_phone("+61 412 345 678") == "0412345678"


def test_detect_kind():
    assert detect_kind("order.pdf", None) == "pdf"
    assert detect_kind("voice.m4a", "") == "audio"
    assert detect_kind("photo.JPG", None) == "image"
    assert detect_kind("invite.ics", "text/calendar") == "other"


def test_email_parsing():
    assert "2 | Roma" in html_to_text("<table><tr><td>2</td><td>Roma</td></tr></table>")
    payload = {"FromFull": {"Email": "A@B.com"}, "ToFull": [{"Email": "x+freshfarms@inbound.postmarkapp.com",
                                                             "MailboxHash": "freshfarms"}],
               "TextBody": "hi", "Attachments": [{"Name": "a.pdf", "ContentType": "application/pdf",
                                                   "Content": base64.b64encode(b"PDF").decode()}]}
    e = parse_postmark(payload)
    assert e.tenant_slug == "freshfarms" and e.attachments[0].content == b"PDF"


def test_pdf_extraction():
    assert "PO-7781" in extract_pdf_text(make_pdf(["PURCHASE ORDER PO-7781", "10 x Potatoes"])).text
    assert extract_pdf_text(make_pdf([])).needs_vision


def test_read_csv_semicolon_and_suggest_mapping():
    csv_bytes = "Item Code;Product Name;Sell Price;UOM;Junk\nA1;Apple;1.50;each;x\n".encode()
    headers, rows = read_table(csv_bytes, "x.csv")
    assert headers == ["Item Code", "Product Name", "Sell Price", "UOM", "Junk"]
    assert rows[0]["Sell Price"] == "1.50"
    m = suggest_mapping(headers, "products")
    assert m == {"Item Code": "sku", "Product Name": "name", "Sell Price": "price", "UOM": "unit", "Junk": None}


def test_read_xlsx():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Customer Name", "Email Address", "Account Number"])
    ws.append(["Joe's Cafe", "joe@cafe.com", "J1"])
    buf = io.BytesIO()
    wb.save(buf)
    headers, rows = read_table(buf.getvalue(), "c.xlsx")
    assert rows[0]["Customer Name"] == "Joe's Cafe"
    assert suggest_mapping(headers, "customers") == {"Customer Name": "name", "Email Address": "email",
                                                     "Account Number": "code"}


def test_crypto_roundtrip():
    enc = encrypt("secret-token")
    assert enc.startswith("enc:") and decrypt(enc) == "secret-token"
    assert encrypt(enc) == enc   # double encrypt nahi
    assert MASK not in enc


def test_paths():
    data = {}
    set_path(data, "order.ref", 5)
    assert data == {"order": {"ref": 5}} and get_path(data, "order.ref") == 5
    assert get_path({"a": [{"b": 1}]}, "a.0.b") == 1


def test_custom_api_mapping():
    payload = {"po_number": "PO1", "customer": {"code": "C1"},
               "lines": [{"sku": "S1", "quantity": 2, "unit_price": 3.5}]}
    config = {"body_mode": "mapped", "field_map": {"ref": "po_number", "account.code": "customer.code"},
              "lines_key": "items", "line_field_map": {"item": "sku", "qty": "quantity"}}
    assert build_body(payload, config) == {"ref": "PO1", "account": {"code": "C1"}, "items": [{"item": "S1", "qty": 2}]}
    h = build_headers({"auth_type": "api_key", "auth_value": "k", "api_key_header": "X-Key", "headers": {"A": "B"}})
    assert h["X-Key"] == "k" and h["A"] == "B"


def test_gst_exclusive_and_inclusive():
    items = [{"quantity": 2, "unit_price": 10, "taxable": True}, {"quantity": 1, "unit_price": 5, "taxable": False}]
    ex = SimpleNamespace(gst_rate=10, prices_include_gst=False)
    _, sub, gst, total = calculate_lines(ex, items)
    assert (float(sub), float(gst), float(total)) == (25.0, 2.0, 27.0)

    inc = SimpleNamespace(gst_rate=10, prices_include_gst=True)
    _, sub, gst, total = calculate_lines(inc, [{"quantity": 1, "unit_price": 110, "taxable": True}])
    assert (float(sub), float(gst), float(total)) == (100.0, 10.0, 110.0)
