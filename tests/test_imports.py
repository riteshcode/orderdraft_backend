"""CSV/XLSX import wizard + mapping templates."""
import io

from tests.conftest import upload_and_run

WEIRD_CSV = b"Item Code,Product Name,Sell Price,UOM,GST,Other\nA1,Apple Pink Lady,1.50,each,GST Free,x\nA2,Banana,2,kg,yes,y\n"


def upload(client, headers, kind, content, name="items.csv"):
    r = client.post(f"/imports/{kind}/upload", headers=headers, files={"file": (name, io.BytesIO(content), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_sample_imports(client, seeded):
    products = client.get("/products", headers=seeded).json()
    assert products["total"] == 16
    roma = next(p for p in products["items"] if p["sku"] == "TOM-ROMA-10KG")
    assert roma["price"] == 32.5 and "roma" in roma["aliases"]
    assert client.get("/customers", headers=seeded).json()["total"] == 5
    jobs = client.get("/imports/jobs", headers=seeded).json()
    assert [j["kind"] for j in jobs] == ["history", "customers", "products"]


def test_mapping_suggest_dry_run_and_template(client, auth):
    body = upload(client, auth, "products", WEIRD_CSV)
    assert body["mapping"] == {"Item Code": "sku", "Product Name": "name", "Sell Price": "price",
                               "UOM": "unit", "GST": "is_taxable", "Other": None}
    assert body["total_rows"] == 2 and body["template_name"] is None

    # Dry run - kuch save nahi
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"],
                                                                "mapping": body["mapping"], "dry_run": True}).json()
    assert r["created"] == 2 and r["dry_run"]
    assert client.get("/products", headers=auth).json()["total"] == 0

    # User ne "Other" ko aliases pe map kiya + template naam
    mapping = {**body["mapping"], "Other": "aliases"}
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"], "mapping": mapping,
                                                                "template_name": "Supplier sheet"}).json()
    assert r["created"] == 2
    items = {p["sku"]: p for p in client.get("/products", headers=auth).json()["items"]}
    assert items["A1"]["is_taxable"] is False and items["A2"]["is_taxable"] is True
    assert items["A1"]["aliases"] == ["x"]

    # Same headers wali file dobara - template ki mapping aani chahiye
    again = upload(client, auth, "products", WEIRD_CSV)
    assert again["template_name"] == "Supplier sheet" and again["mapping"]["Other"] == "aliases"


def test_required_fields_and_duplicates(client, auth):
    body = upload(client, auth, "products", WEIRD_CSV)
    r = client.post("/imports/products/run", headers=auth,
                    json={"upload_id": body["upload_id"], "mapping": {"Item Code": "sku"}})
    assert r.status_code == 400 and "Product name" in r.json()["detail"]
    r = client.post("/imports/products/run", headers=auth,
                    json={"upload_id": body["upload_id"], "mapping": {"Item Code": "sku", "Product Name": "sku"}})
    assert r.status_code == 400


def test_bad_rows_reported(client, auth):
    body = upload(client, auth, "products", b"sku,name,price\nA1,Apple,abc\n,NoSku,1\nA3,Ok,2\n")
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"],
                                                                "mapping": body["mapping"]}).json()
    assert r["created"] == 1 and r["skipped"] == 2 and len(r["errors"]) == 2


def test_upload_id_cannot_traverse(client, auth):
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": "../../etc/passwd", "mapping": {}})
    assert r.status_code == 400


def test_xlsx_customers(client, auth):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["Customer Name", "Email Address", "Mobile"])
    wb.active.append(["Cafe One", "one@cafe.com", "0400 111 222"])
    buf = io.BytesIO()
    wb.save(buf)
    body = upload(client, auth, "customers", buf.getvalue(), name="customers.xlsx")
    r = client.post("/imports/customers/run", headers=auth, json={"upload_id": body["upload_id"],
                                                                 "mapping": body["mapping"]}).json()
    assert r["created"] == 1
    c = client.get("/customers", headers=auth).json()["items"][0]
    assert c["email"] == "one@cafe.com" and c["phone"] == "0400 111 222"


def test_reimport_updates_not_duplicates(client, auth):
    upload_and_run(client, auth, "customers", "customers.csv")
    r = upload_and_run(client, auth, "customers", "customers.csv")
    assert r["created"] == 0 and r["updated"] == 5
