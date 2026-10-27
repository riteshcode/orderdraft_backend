"""Category import (CSV / Excel) - Parent column ya "Main > Sub", update, errors."""
from tests.test_imports import upload


def run(client, auth, csv: bytes, mapping=None):
    body = upload(client, auth, "categories", csv)
    r = client.post("/imports/categories/run", headers=auth,
                    json={"upload_id": body["upload_id"], "mapping": mapping or body["mapping"]})
    assert r.status_code == 200, r.text
    return body, r.json()


def tree(client, auth):
    return {c["path"]: c for c in client.get("/categories", headers=auth).json()}


def test_import_sample_file(client, auth):
    with open("sample_data/categories.csv", "rb") as f:
        body, r = run(client, auth, f.read())
    assert body["mapping"] == {"Category": "name", "Parent": "parent", "Description": "description",
                               "Sort order": "sort_order", "Active": "is_active"}
    assert r["created"] == 8 and r["skipped"] == 0          # 7 rows + "Dairy" (Cheese ka parent) apne aap
    t = tree(client, auth)
    assert set(t) == {"Fruit", "Fruit › Citrus", "Fruit › Berries", "Vegetables", "Vegetables › Leafy greens",
                      "Herbs/Spices", "Dairy", "Dairy › Cheese"}
    assert t["Fruit › Citrus"]["description"] == "Lemons, limes, oranges" and t["Fruit › Berries"]["sort_order"] == 2
    assert t["Herbs/Spices"]["parent_id"] is None            # slash = naam ka hissa


def test_reimport_updates_not_duplicates(client, auth):
    run(client, auth, b"Name,Parent\nFruit,\nCitrus,Fruit\n")
    _, r = run(client, auth, b"Name,Parent,Description,Active\nfruit,,All fruit,yes\nCITRUS,Fruit,Sour stuff,no\nApples,Fruit,,\n")
    assert r["created"] == 1 and r["updated"] == 2
    t = tree(client, auth)
    assert len(t) == 3 and t["Fruit"]["description"] == "All fruit"
    assert t["Fruit › Citrus"]["is_active"] is False and t["Fruit › Citrus"]["description"] == "Sour stuff"


def test_parent_created_when_missing_and_errors(client, auth):
    csv = ("Name,Parent,Sort\n"
           "Cheese,Dairy,abc\n"                 # Dairy file mein nahi -> ban jaata hai; sort galat -> note
           ",Dairy,\n"                          # naam khali
           "A > B > C,,\n"                      # 3 level
           "Milk,Dairy > Fresh,\n"              # parent mein 2 level
           "Yoghurt > Greek,Dairy,\n").encode()  # dono tarike ek saath
    _, r = run(client, auth, csv)
    assert r["created"] == 2 and r["skipped"] == 4
    assert any("sort order" in e for e in r["errors"])
    assert set(tree(client, auth)) == {"Dairy", "Dairy › Cheese"}
    # sub-category ke neeche sub nahi
    _, r = run(client, auth, b"Name,Parent\nFeta,Cheese\n")
    assert set(tree(client, auth)) == {"Dairy", "Dairy › Cheese", "Cheese", "Cheese › Feta"}   # naya main "Cheese"


def test_dry_run_saves_nothing_and_job_logged(client, auth):
    body = upload(client, auth, "categories", b"Name\nFrozen\n")
    r = client.post("/imports/categories/run", headers=auth,
                    json={"upload_id": body["upload_id"], "mapping": body["mapping"], "dry_run": True}).json()
    assert r["dry_run"] and r["created"] == 1 and tree(client, auth) == {}
    run(client, auth, b"Name\nFrozen\n")
    jobs = client.get("/imports/jobs", headers=auth).json()
    assert jobs[0]["kind"] == "categories" and jobs[0]["created"] == 1
    assert client.get("/activity/search?q=import.categories", headers=auth).json()["total"] == 1


def test_export_categories_roundtrip(client, auth):
    import csv as _csv
    import io as _io
    fruit = client.post("/categories", headers=auth, json={"name": "Fruit", "sort_order": 2}).json()
    client.post("/categories", headers=auth, json={"name": "Citrus, Lemons", "parent_id": fruit["id"],
                                                   "description": "Café grade", "is_active": False})
    r = client.get("/categories/export.csv", headers=auth)
    assert r.status_code == 200 and "categories.csv" in r.headers["content-disposition"]
    rows = list(_csv.DictReader(_io.StringIO(r.text.lstrip("﻿"))))
    assert [(x["name"], x["parent"], x["is_active"]) for x in rows] == [("Fruit", "", "yes"), ("Citrus, Lemons", "Fruit", "no")]
    assert rows[1]["description"] == "Café grade" and rows[0]["sort_order"] == "2"
