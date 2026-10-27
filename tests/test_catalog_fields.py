"""Xero jaise poore Contact / Item fields: API, CSV import aur Xero sync mapping."""
from datetime import datetime

from app.modules.connectors.xero import _contact_row, _item_row, contact_payload, xero_date
from tests.test_imports import upload

XERO_CONTACT = {
    "ContactID": "bd2270c3-8706-4c11-9cfb-000b551c3f51", "ContactStatus": "ACTIVE", "Name": "ABC Limited",
    "FirstName": "Andrea", "LastName": "Dutchess", "CompanyNumber": "NumberBusiness1234",
    "EmailAddress": "a.dutchess@abclimited.com", "BankAccountDetails": "45465844", "TaxNumber": "415465456454",
    "AccountsReceivableTaxType": "INPUT2", "AccountsPayableTaxType": "OUTPUT2",
    "Addresses": [
        {"AddressType": "POBOX", "AddressLine1": "P O Box 123", "City": "Wellington", "PostalCode": "6011",
         "AttentionTo": "Andrea"},
        {"AddressType": "STREET"},
    ],
    "Phones": [
        {"PhoneType": "DEFAULT", "PhoneNumber": "1111111", "PhoneAreaCode": "04", "PhoneCountryCode": "64"},
        {"PhoneType": "FAX"}, {"PhoneType": "MOBILE"}, {"PhoneType": "DDI"},
    ],
    "UpdatedDateUTC": "/Date(1488391422280+0000)/", "IsSupplier": False, "IsCustomer": True, "DefaultCurrency": "NZD",
}

XERO_ITEM = {
    "ItemID": "19b79d12-0ae1-496e-9649-cbd04b15c7c5", "Code": "312", "Description": "Coffee",
    "PurchaseDescription": "Coffee Beans", "UpdatedDateUTC": "/Date(1488338552390+0000)/",
    "PurchaseDetails": {"UnitPrice": 12.0000, "AccountCode": "300", "TaxType": "INPUT2"},
    "SalesDetails": {"UnitPrice": 22.0000, "AccountCode": "200", "TaxType": "OUTPUT2"},
    "Name": "Coffee", "IsTrackedAsInventory": False, "IsSold": True, "IsPurchased": True,
}


def test_xero_contact_mapping():
    row = _contact_row(XERO_CONTACT)
    assert row["first_name"] == "Andrea" and row["last_name"] == "Dutchess"
    assert row["phone"] == "+64 04 1111111" and row["mobile"] == "" and row["fax"] == ""
    assert row["postal_line1"] == "P O Box 123" and row["postal_city"] == "Wellington"
    assert row["postal_postcode"] == "6011" and row["postal_attention"] == "Andrea"
    assert row["abn"] == "415465456454" and row["company_number"] == "NumberBusiness1234"
    assert row["sales_tax_type"] == "INPUT2" and row["purchase_tax_type"] == "OUTPUT2"
    assert row["default_currency"] == "NZD" and row["is_customer"] == "yes" and row["is_supplier"] == "no"
    assert row["external_updated_at"] == datetime(2017, 3, 1, 18, 3, 42, 280000)


def test_xero_item_mapping():
    row = _item_row(XERO_ITEM)
    assert row["sku"] == "312" and row["price"] == 22 and row["account_code"] == "200"
    assert row["purchase_description"] == "Coffee Beans" and row["purchase_price"] == 12
    assert row["purchase_account_code"] == "300" and row["purchase_tax_type"] == "INPUT2"
    assert row["is_tracked_inventory"] == "no" and row["is_sold"] == "yes" and row["is_purchased"] == "yes"
    assert xero_date("/Date(0+0000)/") == datetime(1970, 1, 1) and xero_date(None) is None


def test_xero_sync_saves_all_fields(client, auth, fake_xero, db):
    fake_xero.routes[("GET", "/Items")] = {"Items": [XERO_ITEM]}
    fake_xero.routes[("GET", "/Contacts")] = {"Contacts": [XERO_CONTACT]}
    assert client.post("/integrations/sync", headers=auth).status_code == 200
    p = client.get("/products", headers=auth).json()["items"][0]
    assert p["purchase_price"] == 12 and p["purchase_description"] == "Coffee Beans" and p["is_purchased"]
    c = client.get("/customers", headers=auth).json()["items"][0]
    assert c["first_name"] == "Andrea" and c["postal_city"] == "Wellington" and c["default_currency"] == "NZD"
    assert c["address"] == "P O Box 123, Wellington 6011"           # display address parts se bana
    assert c["phone"] == "+64 04 1111111" and c["is_customer"] and not c["is_supplier"]


def test_customer_crud_with_new_fields(client, auth):
    body = {"name": "Cafe One", "email": "Orders@Cafe.com", "first_name": "Joe", "mobile": "0400 111 222",
            "delivery_line1": "12 High St", "delivery_city": "Sydney", "delivery_region": "NSW",
            "delivery_postcode": "2000", "delivery_instructions": "Back door", "default_currency": "aud",
            "is_supplier": True}
    c = client.post("/customers", headers=auth, json=body).json()
    assert c["email"] == "orders@cafe.com" and c["phone"] == "0400 111 222" and c["default_currency"] == "AUD"
    assert c["address"] == "12 High St, Sydney NSW 2000" and c["is_supplier"] and c["is_customer"]

    # Purana form sirf kuch fields bhejta hai - baaki mitne nahi chahiye
    c = client.put(f"/customers/{c['id']}", headers=auth, json={"name": "Cafe One Pty"}).json()
    assert c["name"] == "Cafe One Pty" and c["delivery_instructions"] == "Back door" and c["first_name"] == "Joe"
    c = client.put(f"/customers/{c['id']}", headers=auth, json={"name": "Cafe One Pty", "delivery_city": "Parramatta"}).json()
    assert c["address"] == "12 High St, Parramatta NSW 2000"


def test_product_crud_with_purchase_fields(client, auth):
    p = client.post("/products", headers=auth, json={
        "sku": "C-1", "name": "Coffee", "price": 22, "purchase_price": 12.5, "purchase_account_code": "300",
        "is_tracked_inventory": True, "quantity_on_hand": 40, "is_purchased": True}).json()
    assert p["purchase_price"] == 12.5 and p["quantity_on_hand"] == 40 and p["is_tracked_inventory"]
    p = client.put(f"/products/{p['id']}", headers=auth, json={"sku": "C-1", "name": "Coffee beans", "price": 23}).json()
    assert p["purchase_price"] == 12.5 and p["is_purchased"] and p["images"] == [] and p["image_url"] is None


def test_csv_import_new_columns(client, auth):
    csv = ("ContactName,EmailAddress,FirstName,MobileNumber,POAddressLine1,POCity,POPostalCode,"
           "SA_AddressLine1,SA_City,SA_PostalCode,TaxNumber,IsSupplier,DefaultCurrency\n"
           "Cafe Two,two@cafe.com,Ann,0411 000 000,PO Box 5,Sydney,2001,5 Main Rd,Bondi,2026,123,no,AUD\n").encode()
    body = upload(client, auth, "customers", csv, name="contacts.csv")
    assert body["mapping"]["POAddressLine1"] == "postal_line1" and body["mapping"]["SA_City"] == "delivery_city"
    r = client.post("/imports/customers/run", headers=auth, json={"upload_id": body["upload_id"],
                                                                 "mapping": body["mapping"]}).json()
    assert r["created"] == 1, r
    c = client.get("/customers", headers=auth).json()["items"][0]
    assert c["mobile"] == "0411 000 000" and c["phone"] == "0411 000 000" and c["first_name"] == "Ann"
    assert c["postal_line1"] == "PO Box 5" and c["delivery_city"] == "Bondi" and c["is_supplier"] is False
    assert c["address"] == "5 Main Rd, Bondi 2026"                  # delivery pehle

    csv = b"Code,Name,PurchaseUnitPrice,PurchaseAccount,IsTrackedAsInventory,QuantityOnHand\nX1,Beans,9.5,300,yes,12\n"
    body = upload(client, auth, "products", csv, name="items.csv")
    r = client.post("/imports/products/run", headers=auth, json={"upload_id": body["upload_id"],
                                                                "mapping": body["mapping"]}).json()
    assert r["created"] == 1, (r, body["mapping"])
    p = client.get("/products", headers=auth).json()["items"][0]
    assert p["purchase_price"] == 9.5 and p["is_tracked_inventory"] and p["quantity_on_hand"] == 12


def test_contact_payload_sends_details():
    class C:
        name, email, code, first_name, last_name, abn, company_number, default_currency = (
            "Cafe", "a@b.com", None, "Joe", None, "123", None, None)
        phone, mobile, fax, ddi = "02 9999", "0400", None, None
        delivery_line1, delivery_line2, delivery_city, delivery_region = "1 St", None, "Sydney", "NSW"
        delivery_postcode, delivery_country, delivery_attention = "2000", "Australia", None
        postal_line1 = postal_line2 = postal_city = postal_region = postal_postcode = None
        postal_country = postal_attention = None
    payload = contact_payload(C())
    assert payload["EmailAddress"] == "a@b.com" and payload["FirstName"] == "Joe" and payload["TaxNumber"] == "123"
    assert {p["PhoneType"] for p in payload["Phones"]} == {"DEFAULT", "MOBILE"}
    assert payload["Addresses"] == [{"AddressType": "STREET", "AddressLine1": "1 St", "AddressLine2": "", "City": "Sydney",
                                     "Region": "NSW", "PostalCode": "2000", "Country": "Australia"}]
