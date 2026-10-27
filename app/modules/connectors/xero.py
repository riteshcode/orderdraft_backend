"""
Xero connector.
Docs: https://developer.xero.com/documentation/guides/oauth2/auth-flow
      https://developer.xero.com/documentation/api/accounting/overview

- Access token ~30 min, refresh token rotate hota hai (har refresh pe naya save karte hain)
- Har API call mein header "xero-tenant-id" = client ka Xero organisation id
- Account / tax: Xero ka Code ("200") aur TaxType ("OUTPUT") seedha use hota hai
"""
import base64
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.modules.connectors.base import (
    AccountingProvider, ConnectorError, InvoiceStatus, Org, PushResult, TokenSet, line_price, own_external_id,
)

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REVOKE_URL = "https://identity.xero.com/connect/revocation"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

NON_TAX_TYPES = {"EXEMPTOUTPUT", "BASEXCLUDED", "EXEMPTEXPORT", "NONE"}
CLASSES = {"REVENUE", "EXPENSE", "ASSET", "LIABILITY", "EQUITY"}
STATUS_MAP = {"DRAFT": "draft", "SUBMITTED": "draft", "AUTHORISED": "authorised",
              "PAID": "paid", "VOIDED": "void", "DELETED": "void"}


class XeroProvider(AccountingProvider):
    key = "xero"
    label = "Xero"
    doc_types = {"invoice_draft": "Draft invoice", "invoice_authorised": "Approved invoice", "quote": "Quote"}
    supports_webhooks = True

    def configured(self) -> bool:
        return bool(settings.XERO_CLIENT_ID and settings.XERO_CLIENT_SECRET)

    # ---------------- OAuth ----------------
    def authorize_url(self, state: str) -> str:
        if not settings.XERO_CLIENT_ID:
            raise ConnectorError("Xero isn't set up on this platform yet (XERO_CLIENT_ID)")
        return AUTH_URL + "?" + urlencode({
            "response_type": "code", "client_id": settings.XERO_CLIENT_ID,
            "redirect_uri": settings.XERO_REDIRECT_URI, "scope": settings.XERO_SCOPES, "state": state,
        })

    def _basic(self) -> str:
        raw = f"{settings.XERO_CLIENT_ID}:{settings.XERO_CLIENT_SECRET}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_call(self, data: dict) -> TokenSet:
        try:
            resp = httpx.post(TOKEN_URL, headers={"Authorization": self._basic()}, data=data, timeout=30)
        except httpx.HTTPError as e:
            raise ConnectorError(f"Xero login failed: {e}", retryable=True)
        if resp.status_code != 200:
            raise ConnectorError("Xero connection expired - please reconnect in Settings → Integrations", reconnect=True)
        d = resp.json()
        return TokenSet(d["access_token"], d.get("refresh_token"), int(d.get("expires_in", 1800)), raw=d)

    def exchange_code(self, code: str, params: dict) -> TokenSet:
        return self._token_call({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": settings.XERO_REDIRECT_URI})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, refresh_token: str) -> None:
        try:
            httpx.post(REVOKE_URL, headers={"Authorization": self._basic()}, data={"token": refresh_token}, timeout=15)
        except httpx.HTTPError:
            pass

    def list_orgs(self, api, params: dict) -> list[Org]:
        data = api.get(CONNECTIONS_URL, absolute=True)
        return [Org(id=c["tenantId"], name=c.get("tenantName") or c["tenantId"], extra={"connection_id": c.get("id")})
                for c in (data or []) if c.get("tenantType", "ORGANISATION") == "ORGANISATION"]

    def activate_org(self, api, org: Org, credentials: dict | None) -> dict:
        """Org ki country / base currency yaad rakho (UI warning: AU GST codes sirf AU org mein)."""
        try:
            info = (api.get("/Organisation").get("Organisations") or [{}])[0]
        except ConnectorError:
            return {}
        return {"base_currency": info.get("BaseCurrency"), "country": info.get("CountryCode")}

    # ---------------- API ----------------
    def api_base(self, conn) -> str:
        return API_BASE

    def auth_headers(self, conn, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "xero-tenant-id": conn.external_tenant_id or "",
                "Accept": "application/json"}

    def parse_error(self, resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"Xero error {resp.status_code}: {resp.text[:300]}"
        messages = [err.get("Message") for el in data.get("Elements", []) or []
                    for err in el.get("ValidationErrors", []) or [] if err.get("Message")]
        if messages:
            return "Xero: " + "; ".join(messages)
        return f"Xero error {resp.status_code}: {data.get('Message') or data.get('Detail') or str(data)[:300]}"

    # ---------------- Data ----------------
    def fetch_accounts(self, api) -> list[dict]:
        rows = []
        for x in api.get("/Accounts").get("Accounts", []):
            code = (x.get("Code") or "").strip()
            if not code:                     # bank accounts ka code nahi hota
                continue
            cls = (x.get("Class") or "").upper()
            rows.append({"code": code, "name": x.get("Name") or code,
                         "account_class": cls if cls in CLASSES else "REVENUE",
                         "external_id": x.get("AccountID"), "is_active": x.get("Status", "ACTIVE") == "ACTIVE"})
        return rows

    def fetch_tax_rates(self, api) -> list[dict]:
        rows = []
        for x in api.get("/TaxRates").get("TaxRates", []):
            code = (x.get("TaxType") or "").strip()
            if not code:
                continue
            revenue, expense = x.get("CanApplyToRevenue", True), x.get("CanApplyToExpenses", False)
            rows.append({"code": code, "name": x.get("Name") or code,
                         "rate": float(x.get("EffectiveRate", x.get("DisplayTaxRate", 0)) or 0),
                         "applies_to": "both" if revenue and expense else ("purchases" if expense and not revenue else "sales"),
                         "external_id": code, "is_active": x.get("Status", "ACTIVE") == "ACTIVE"})
        return rows

    def _since_header(self, since: datetime | None) -> dict:
        return {"If-Modified-Since": since.strftime("%Y-%m-%dT%H:%M:%S")} if since else {}

    def fetch_products(self, api, since: datetime | None) -> list[dict]:
        rows = []
        for item in api.get("/Items", headers=self._since_header(since)).get("Items", []):
            # Sirf bechne wale items - purchase-only item ka order nahi aata
            if item.get("IsSold") is False or not item.get("Code"):
                continue
            rows.append(_item_row(item))
        return rows

    def fetch_customers(self, api, since: datetime | None, include_all: bool) -> list[dict]:
        params = {"includeArchived": "true"}
        if not include_all:
            params["where"] = "IsCustomer==true"
        contacts, page = [], 1
        while True:
            batch = api.get("/Contacts", params={**params, "page": page},
                            headers=self._since_header(since)).get("Contacts", [])
            contacts.extend(batch)
            if len(batch) < 100 or page >= 200:
                break
            page += 1
        return [_contact_row(c) for c in contacts]

    def fetch_suppliers(self, api, since: datetime | None) -> list[dict]:
        contacts, page = [], 1
        while True:
            batch = api.get("/Contacts", params={"where": "IsSupplier==true", "includeArchived": "true", "page": page},
                            headers=self._since_header(since)).get("Contacts", [])
            contacts.extend(batch)
            if len(batch) < 100 or page >= 200:
                break
            page += 1
        return [_supplier_row(c) for c in contacts]

    # ---------------- Export ----------------
    def build_document(self, order, cfg, dest_config: dict) -> dict:
        customer = order.customer
        contact_id = own_external_id(customer, self.key)
        if contact_id:
            contact = {"ContactID": contact_id}
        else:
            # Xero is naam ka contact dhundhta hai, na mile to naya bana deta hai (hamari details ke saath)
            contact = contact_payload(customer) if customer else {"Name": order.customer_raw or "Unknown customer"}

        line_items = []
        for line in order.lines:
            p = line.product
            if not p:
                continue
            item = {"Description": p.name, "Quantity": line.quantity, "UnitAmount": line_price(line),
                    "AccountCode": p.account_code or dest_config.get("default_account_code") or "200"}
            if own_external_id(p, self.key):
                item["ItemCode"] = p.sku
            tax_type = p.tax_type or dest_config.get("default_tax_type")
            if tax_type:
                item["TaxType"] = tax_type
            line_items.append(item)

        today = date.today()
        doc = {"Contact": contact, "Date": today.isoformat(), "LineItems": line_items,
               "Reference": order.po_number or f"OD-{order.id}",
               "LineAmountTypes": "Inclusive" if cfg.prices_include_gst else "Exclusive"}
        # CurrencyCode nahi bhejte - Xero org ki base currency apne aap lagti hai.
        # (AUD bhejne pe non-AU org error deta hai: "Organisation is not subscribed to currency AUD")
        if dest_config["document_type"] == "quote":
            doc["Status"] = "DRAFT"
            if order.delivery_date:
                doc["ExpiryDate"] = order.delivery_date.isoformat()
        else:
            doc["Type"] = "ACCREC"
            doc["Status"] = "AUTHORISED" if dest_config["document_type"] == "invoice_authorised" else "DRAFT"
            doc["DueDate"] = (today + timedelta(days=cfg.payment_terms_days or 0)).isoformat()
        return doc

    def push_order(self, api, order, cfg, dest_config: dict) -> PushResult:
        doc = self.build_document(order, cfg, dest_config)
        if not doc["LineItems"]:
            raise ConnectorError("None of the order lines are matched to a product")

        if dest_config["document_type"] == "quote":
            created = api.post("/Quotes", json={"Quotes": [doc]}, headers=_idem(dest_config))["Quotes"][0]
            return PushResult(external_id=created.get("QuoteID"), number=created.get("QuoteNumber"), kind="quote",
                              message=f"Quote {created.get('QuoteNumber', '')} created in Xero", raw=created,
                              customer_external_id=(created.get("Contact") or {}).get("ContactID"))

        created = api.post("/Invoices", json={"Invoices": [doc]}, headers=_idem(dest_config))["Invoices"][0]
        invoice_id = created["InvoiceID"]
        if dest_config.get("send_email"):
            api.post(f"/Invoices/{invoice_id}/Email", json={})     # 204 -> {}
            created["SentToContact"] = True
        st = _status(created)
        return PushResult(
            external_id=invoice_id, number=created.get("InvoiceNumber"), status=st.status, raw=created,
            invoice_status=st,
            message=f"Invoice {created.get('InvoiceNumber') or ''} created in Xero ({created.get('Status')})",
            url=self.invoice_url(api.conn, invoice_id),
            customer_external_id=(created.get("Contact") or {}).get("ContactID"),
        )

    def fetch_invoice(self, api, external_id: str) -> InvoiceStatus | None:
        found = (api.get(f"/Invoices/{external_id}").get("Invoices") or [None])[0]
        return _status(found) if found else None

    def invoice_url(self, conn, external_id: str) -> str | None:
        return f"https://go.xero.com/AccountsReceivable/View.aspx?InvoiceID={external_id}"


def _idem(dest_config: dict) -> dict | None:
    """Xero Idempotency-Key header - retry pe same key = Xero pehle wala invoice lauta deta hai, naya nahi banata."""
    key = dest_config.get("_idempotency_key")
    return {"Idempotency-Key": key[:128]} if key else None


def _status(x: dict) -> InvoiceStatus:
    status = STATUS_MAP.get(x.get("Status"), "draft")
    if status == "authorised" and x.get("SentToContact"):
        status = "sent"
    return InvoiceStatus(status=status, number=x.get("InvoiceNumber"), subtotal=x.get("SubTotal"),
                         gst=x.get("TotalTax"), total=x.get("Total"), amount_paid=x.get("AmountPaid"))


def xero_date(value) -> datetime | None:
    """Xero ka "/Date(1744934400000+0000)/" (ya ISO string) -> datetime (UTC, naive)."""
    if not value:
        return None
    m = re.search(r"/Date\((-?\d+)", str(value))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _yes(v) -> str:
    return "" if v is None else ("yes" if v else "no")


def _item_row(item: dict) -> dict:
    """Xero Item -> hamara product row (import_products isko DB mein daalta hai)."""
    sales = item.get("SalesDetails") or {}
    purchase = item.get("PurchaseDetails") or {}
    tax_type = sales.get("TaxType")
    return {
        "sku": item["Code"], "name": item.get("Name") or item["Code"],
        "description": item.get("Description") or "", "price": sales.get("UnitPrice"),
        "account_code": sales.get("AccountCode") or "", "tax_type": tax_type or "",
        "is_taxable": ("no" if tax_type in NON_TAX_TYPES else "yes") if tax_type else "",
        "purchase_description": item.get("PurchaseDescription") or "",
        "purchase_price": purchase.get("UnitPrice"),
        "purchase_account_code": purchase.get("AccountCode") or purchase.get("COGSAccountCode") or "",
        "purchase_tax_type": purchase.get("TaxType") or "",
        "is_tracked_inventory": _yes(item.get("IsTrackedAsInventory")),
        "quantity_on_hand": item.get("QuantityOnHand") if item.get("IsTrackedAsInventory") else None,
        "is_sold": _yes(item.get("IsSold")), "is_purchased": _yes(item.get("IsPurchased")),
        "external_updated_at": xero_date(item.get("UpdatedDateUTC")),
        "external_id": item["ItemID"],
    }


def _phone(p: dict) -> str:
    """{PhoneCountryCode: 61, PhoneAreaCode: 02, PhoneNumber: 9999} -> "+61 02 9999"."""
    if not p.get("PhoneNumber"):
        return ""
    cc = str(p.get("PhoneCountryCode") or "").strip().lstrip("+")
    parts = [f"+{cc}" if cc else "", str(p.get("PhoneAreaCode") or "").strip(), str(p["PhoneNumber"]).strip()]
    return " ".join(x for x in parts if x)


def _address(a: dict, prefix: str) -> dict:
    return {
        f"{prefix}_line1": a.get("AddressLine1") or "",
        f"{prefix}_line2": ", ".join(x for x in (a.get("AddressLine2"), a.get("AddressLine3"), a.get("AddressLine4")) if x),
        f"{prefix}_city": a.get("City") or "", f"{prefix}_region": a.get("Region") or "",
        f"{prefix}_postcode": a.get("PostalCode") or "", f"{prefix}_country": a.get("Country") or "",
        f"{prefix}_attention": a.get("AttentionTo") or "",
    }


def _contact_row(c: dict) -> dict:
    """Xero Contact -> hamara customer row. POBOX = postal, STREET = delivery address."""
    phones = {p.get("PhoneType"): _phone(p) for p in c.get("Phones") or []}
    row = {
        "name": c.get("Name") or "", "code": c.get("AccountNumber") or "", "email": c.get("EmailAddress") or "",
        "first_name": c.get("FirstName") or "", "last_name": c.get("LastName") or "",
        "phone": phones.get("DEFAULT") or phones.get("MOBILE") or "",
        "mobile": phones.get("MOBILE") or "", "fax": phones.get("FAX") or "", "ddi": phones.get("DDI") or "",
        "abn": c.get("TaxNumber") or "", "company_number": c.get("CompanyNumber") or "",
        "bank_account_details": c.get("BankAccountDetails") or "",
        "sales_tax_type": c.get("AccountsReceivableTaxType") or "",
        "purchase_tax_type": c.get("AccountsPayableTaxType") or "",
        "default_currency": c.get("DefaultCurrency") or "",
        "is_customer": _yes(c.get("IsCustomer")), "is_supplier": _yes(c.get("IsSupplier")),
        "external_updated_at": xero_date(c.get("UpdatedDateUTC")),
        "address": "",   # khali = delivery/postal parts se ban jaata hai
        "other_emails": ", ".join(p.get("EmailAddress") for p in c.get("ContactPersons") or [] if p.get("EmailAddress")),
        "external_id": c["ContactID"], "is_active": c.get("ContactStatus", "ACTIVE") == "ACTIVE",
    }
    for a in c.get("Addresses") or []:
        kind = {"POBOX": "postal", "STREET": "delivery"}.get(a.get("AddressType"))
        if kind:
            row.update(_address(a, kind))
    return row


def _supplier_row(c: dict) -> dict:
    """Xero supplier contact -> hamara supplier row. Bills payment terms (X din baad) bhi."""
    r = _contact_row(c)
    terms = ((c.get("PaymentTerms") or {}).get("Bills") or {})
    days = terms.get("Day") if terms.get("Type") == "DAYSAFTERBILLDATE" else None
    address = ", ".join(x for x in (
        r.get("postal_line1"), r.get("postal_line2"),
        " ".join(y for y in (r.get("postal_city"), r.get("postal_region"), r.get("postal_postcode")) if y),
        r.get("postal_country")) if x) or ", ".join(x for x in (
        r.get("delivery_line1"), r.get("delivery_city"), r.get("delivery_region"), r.get("delivery_postcode")) if x)
    return {"name": r["name"], "code": r["code"], "email": r["email"], "cc_emails": r["other_emails"],
            "contact_name": " ".join(x for x in (r["first_name"], r["last_name"]) if x),
            "phone": r["phone"] or r["mobile"], "address": address, "abn": r["abn"],
            "currency": r["default_currency"], "payment_terms_days": days,
            "account_number": "", "external_id": r["external_id"], "is_active": r["is_active"]}


def contact_payload(customer) -> dict:
    """Naya Xero contact banate waqt hamare paas jo details hain wo bhi bhejo."""
    c = {"Name": customer.name}
    for key, attr in (("EmailAddress", "email"), ("AccountNumber", "code"), ("FirstName", "first_name"),
                      ("LastName", "last_name"), ("TaxNumber", "abn"), ("CompanyNumber", "company_number"),
                      ("DefaultCurrency", "default_currency")):
        if getattr(customer, attr, None):
            c[key] = getattr(customer, attr)
    phones = [{"PhoneType": t, "PhoneNumber": getattr(customer, attr)}
              for t, attr in (("DEFAULT", "phone"), ("MOBILE", "mobile"), ("FAX", "fax"), ("DDI", "ddi"))
              if getattr(customer, attr, None)]
    if phones:
        c["Phones"] = phones
    addresses = []
    for t, prefix in (("POBOX", "postal"), ("STREET", "delivery")):
        g = lambda k: getattr(customer, f"{prefix}_{k}", None)
        if g("line1") or g("city"):
            addr = {"AddressType": t, "AddressLine1": g("line1") or "", "AddressLine2": g("line2") or "",
                    "City": g("city") or "", "Region": g("region") or "", "PostalCode": g("postcode") or "",
                    "Country": g("country") or ""}
            if g("attention"):
                addr["AttentionTo"] = g("attention")
            addresses.append(addr)
    if addresses:
        c["Addresses"] = addresses
    return c
