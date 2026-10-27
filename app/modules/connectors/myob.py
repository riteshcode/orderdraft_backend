"""
MYOB Business / AccountRight connector (API v2).
Docs: https://developer.myob.com/api/myob-business-api/api-overview/
      https://developer.myob.com/api/myob-business-api/v2/

Gotchas (dhyan rakho):
- Login (OAuth) ke baad client ki COMPANY FILE chunni padti hai. Har company file ka apna
  URI hota hai (https://arl2.api.myob.com/accountright/<id>) - saari API calls usi URI pe.
  Ye Connection.extra["cf_uri"] mein save hota hai.
- Kuch company files ka apna username/password hota hai -> header "x-myobapi-cftoken"
  = base64("user:pass"). Optional hai (online files mein aksar zaroorat nahi).
- Access token sirf ~20 minute chalta hai (refresh api.py apne aap karta hai).
- Har call pe x-myobapi-key (client id) + x-myobapi-version: v2 headers chahiye.
- Invoice "Number" max 13 characters.
- Rate limit ~8 requests/second per API key - bulk sync mein dhyan.
- Invoice do layouts mein: Item (MYOB inventory items ke saath) aur Service (sirf
  description + account). Saari lines MYOB items hon to Item layout, warna Service.
- Account ka hamara code = DisplayID ("4-1100"), tax code = Code ("GST"/"FRE");
  API ko UID chahiye - wo AccountCode/TaxRate.external_id mein sync hota hai.
"""
import base64
from datetime import date, datetime, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.modules.connectors.base import (
    AccountingProvider, ConnectorError, InvoiceStatus, Org, PushResult, TokenSet, line_price, own_external_id,
)

AUTH_URL = "https://secure.myob.com/oauth2/account/authorize"
TOKEN_URL = "https://secure.myob.com/oauth2/v1/authorize"
API_ROOT = "https://api.myob.com/accountright/"

CLASS_MAP = {"Income": "REVENUE", "OtherIncome": "REVENUE", "Expense": "EXPENSE", "CostOfSales": "EXPENSE",
             "OtherExpense": "EXPENSE", "Asset": "ASSET", "Liability": "LIABILITY", "Equity": "EQUITY"}
NON_TAX_CODES = {"FRE", "N-T", "EXP"}
# document_type -> MYOB endpoint ka naam + hamara "kind"
DOC_ENDPOINTS = {"invoice_authorised": ("Invoice", "invoice"), "sales_order": ("Order", "order"),
                 "quote": ("Quote", "quote")}


def _cftoken(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def _q(value: str) -> str:
    """OData filter mein single quote escape: O'Brien -> O''Brien"""
    return (value or "").replace("'", "''")


class MyobProvider(AccountingProvider):
    key = "myob"
    label = "MYOB Business"
    doc_types = {"invoice_authorised": "Invoice", "sales_order": "Sales order", "quote": "Quote"}
    org_login_fields = ["username", "password"]
    supports_webhooks = False   # MYOB webhooks nahi bhejta - status polling (scheduler)

    def configured(self) -> bool:
        return bool(settings.MYOB_CLIENT_ID and settings.MYOB_CLIENT_SECRET)

    # ---------------- OAuth ----------------
    def authorize_url(self, state: str) -> str:
        if not settings.MYOB_CLIENT_ID:
            raise ConnectorError("MYOB isn't set up on this platform yet (MYOB_CLIENT_ID)")
        return AUTH_URL + "?" + urlencode({
            "client_id": settings.MYOB_CLIENT_ID, "redirect_uri": settings.MYOB_REDIRECT_URI,
            "response_type": "code", "scope": settings.MYOB_SCOPES, "state": state,
        })

    def _token_call(self, data: dict) -> TokenSet:
        data = {"client_id": settings.MYOB_CLIENT_ID, "client_secret": settings.MYOB_CLIENT_SECRET, **data}
        try:
            resp = httpx.post(TOKEN_URL, data=data, timeout=30)
        except httpx.HTTPError as e:
            raise ConnectorError(f"MYOB login failed: {e}", retryable=True)
        if resp.status_code != 200:
            raise ConnectorError("MYOB connection expired - please reconnect in Settings → Integrations", reconnect=True)
        d = resp.json()
        return TokenSet(d["access_token"], d.get("refresh_token"), int(d.get("expires_in", 1200)), raw=d)

    def exchange_code(self, code: str, params: dict) -> TokenSet:
        return self._token_call({"code": code, "redirect_uri": settings.MYOB_REDIRECT_URI,
                                 "grant_type": "authorization_code", "scope": settings.MYOB_SCOPES})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, refresh_token: str) -> None:
        return None   # MYOB ka revoke endpoint nahi - token khud expire hota hai

    def list_orgs(self, api, params: dict) -> list[Org]:
        files = api.get(API_ROOT, absolute=True) or []
        if isinstance(files, dict):           # kabhi {"Items": [...]} shape
            files = files.get("Items", [])
        return [Org(id=f["Id"], name=f.get("Name") or f["Id"],
                    extra={"uri": f.get("Uri"), "requires_login": False}) for f in files if f.get("Id")]

    def activate_org(self, api, org: Org, credentials: dict | None) -> dict:
        cf_uri = (org.extra.get("uri") or "").rstrip("/")
        if not cf_uri:
            raise ConnectorError("Couldn't find the company file address (URI)")
        # Credentials abhi save nahi hue (service baad mein karta hai) - check ke liye header khud banao
        headers = {}
        if credentials and credentials.get("username"):
            headers["x-myobapi-cftoken"] = _cftoken(credentials["username"], credentials.get("password", ""))
        # Seedha send (api.request 401 pe token refresh karta - yahan 401 ka matlab galat login hai)
        try:
            resp = api.send("GET", f"{cf_uri}/Company", {**self.auth_headers(api.conn, api.token()), **headers})
        except httpx.HTTPError as e:
            raise ConnectorError(f"MYOB request failed: {e}", retryable=True)
        if resp.status_code in (401, 403):
            raise ConnectorError("Company file login failed - check the username and password")
        if resp.status_code >= 400:
            raise ConnectorError(self.parse_error(resp))
        return {"cf_uri": cf_uri}

    # ---------------- API ----------------
    def api_base(self, conn) -> str:
        # Company file chunne se pehle sirf absolute calls hoti hain - tab root URL
        return ((conn.extra or {}).get("cf_uri") or API_ROOT).rstrip("/")

    def _require_cf(self, api):
        if not (api.conn.extra or {}).get("cf_uri"):
            raise ConnectorError("No company file selected - choose one in Settings → Integrations")

    def auth_headers(self, conn, token: str) -> dict:
        headers = {"Authorization": f"Bearer {token}", "x-myobapi-key": settings.MYOB_CLIENT_ID,
                   "x-myobapi-version": "v2", "Accept": "application/json"}
        from app.modules.connectors.service import org_credentials   # circular import se bachne ke liye
        creds = org_credentials(conn)
        if creds and creds[0]:
            headers["x-myobapi-cftoken"] = _cftoken(*creds)
        return headers

    def parse_error(self, resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"MYOB error {resp.status_code}: {resp.text[:300]}"
        errors = data.get("Errors") if isinstance(data, dict) else None
        if errors:
            parts = []
            for e in errors:
                msg = e.get("Message") or e.get("Name") or "error"
                if e.get("AdditionalDetails"):
                    msg += f" ({e['AdditionalDetails']})"
                parts.append(msg)
            return "MYOB: " + "; ".join(parts)
        return f"MYOB error {resp.status_code}: {str(data)[:300]}"

    def _list(self, api, path: str, filters: list[str] | None = None) -> list[dict]:
        """Saare pages (NextPageLink follow karke)."""
        self._require_cf(api)
        params = {"$top": 1000}
        if filters:
            params["$filter"] = " and ".join(filters)
        data = api.get(path, params=params)
        items = list(data.get("Items", []))
        pages = 0
        while data.get("NextPageLink") and pages < 200:
            data = api.get(data["NextPageLink"], absolute=True)
            items.extend(data.get("Items", []))
            pages += 1
        return items

    def _since(self, since: datetime | None) -> list[str]:
        return [f"LastModified gt datetime'{since.strftime('%Y-%m-%dT%H:%M:%S')}'"] if since else []

    # ---------------- Data ----------------
    def fetch_accounts(self, api) -> list[dict]:
        rows = []
        for a in self._list(api, "/GeneralLedger/Account"):
            if a.get("IsHeader") or not a.get("DisplayID"):
                continue
            rows.append({"code": a["DisplayID"], "name": a.get("Name") or a["DisplayID"],
                         "account_class": CLASS_MAP.get(a.get("Classification"), "REVENUE"),
                         "external_id": a.get("UID"), "is_active": a.get("IsActive", True)})
        return rows

    def fetch_tax_rates(self, api) -> list[dict]:
        return [{"code": t["Code"], "name": t.get("Description") or t["Code"], "rate": float(t.get("Rate") or 0),
                 "applies_to": "both", "external_id": t.get("UID"), "is_active": True}
                for t in self._list(api, "/GeneralLedger/TaxCode") if t.get("Code")]

    def fetch_products(self, api, since: datetime | None) -> list[dict]:
        filters = self._since(since) or ["IsActive eq true"]
        rows = []
        for item in self._list(api, "/Inventory/Item", filters):
            if not item.get("IsSold", True) or not item.get("Number"):
                continue
            selling = item.get("SellingDetails") or {}
            buying = item.get("BuyingDetails") or {}
            tax_code = (selling.get("TaxCode") or {}).get("Code") or ""
            rows.append({
                "sku": item["Number"], "name": item.get("Name") or item["Number"],
                "description": item.get("Description") or "", "price": selling.get("BaseSellingPrice"),
                "unit": selling.get("SellingUnitOfMeasure") or "",
                "account_code": (item.get("IncomeAccount") or {}).get("DisplayID") or "",
                "tax_type": tax_code,
                "is_taxable": ("no" if tax_code in NON_TAX_CODES else "yes") if tax_code else "",
                "purchase_price": buying.get("LastPurchasePrice") or buying.get("StandardCost"),
                "purchase_account_code": (item.get("ExpenseAccount") or item.get("CostOfSalesAccount") or {}).get("DisplayID") or "",
                "purchase_tax_type": (buying.get("TaxCode") or {}).get("Code") or "",
                "is_tracked_inventory": "yes" if item.get("IsInventoried") else "no",
                "quantity_on_hand": item.get("QuantityOnHand") if item.get("IsInventoried") else None,
                "is_sold": "yes" if item.get("IsSold", True) else "no",
                "is_purchased": "yes" if item.get("IsBought") else "no",
                "external_updated_at": _myob_time(item.get("LastModified")),
                "external_id": item.get("UID"),
            })
        return rows

    def fetch_customers(self, api, since: datetime | None, include_all: bool) -> list[dict]:
        return [_customer_row(c) for c in self._list(api, "/Contact/Customer", self._since(since))]

    def fetch_suppliers(self, api, since: datetime | None) -> list[dict]:
        rows = []
        for c in self._list(api, "/Contact/Supplier", self._since(since)):
            r = _customer_row(c)          # same contact shape (name, addresses, phones)
            buying = c.get("BuyingDetails") or {}
            address = ", ".join(x for x in (r.get("postal_line1"), r.get("postal_line2"), " ".join(
                y for y in (r.get("postal_city"), r.get("postal_region"), r.get("postal_postcode")) if y)) if x)
            rows.append({"name": r["name"], "code": r["code"], "email": r["email"], "cc_emails": "",
                         "contact_name": (r.get("postal_attention") or ""), "phone": r["phone"],
                         "address": address, "abn": buying.get("ABN") or "", "currency": r["default_currency"],
                         "account_number": "", "payment_terms_days": None,
                         "external_id": r["external_id"], "is_active": r["is_active"]})
        return rows

    # ---------------- Export ----------------
    def _default_tax_uid(self, api, dest_config: dict) -> str | None:
        return api.tax_ref(dest_config.get("default_tax_type")) or api.tax_ref("GST") or api.tax_ref("FRE")

    def _customer_uid(self, api, order, dest_config: dict) -> tuple[str, bool]:
        """(UID, naya bana/mila?) - pehle apna id, phir DisplayID / naam se dhundho, warna banao."""
        customer = order.customer
        own = own_external_id(customer, self.key)
        if own:
            return own, False
        name = customer.name if customer else (order.customer_raw or "Unknown customer")
        filters = []
        if customer and customer.code:
            filters.append(f"DisplayID eq '{_q(customer.code)}'")
        filters.append(f"CompanyName eq '{_q(name)}'")
        for f in filters:
            found = api.get("/Contact/Customer", params={"$filter": f, "$top": 1}).get("Items", [])
            if found:
                return found[0]["UID"], True

        tax_uid = self._default_tax_uid(api, dest_config)
        body = {"CompanyName": name[:50], "IsIndividual": False, "IsActive": True,
                "SellingDetails": {"TaxCode": {"UID": tax_uid}, "FreightTaxCode": {"UID": tax_uid}}}
        if customer and customer.code and len(customer.code) <= 15:
            body["DisplayID"] = customer.code
        if customer and customer.email:
            body["Addresses"] = [{"Location": 1, "Email": customer.email}]
        created = self._post(api, "/Contact/Customer?returnBody=true", body)
        return created["UID"], True

    def _find_by_number(self, api, endpoint: str, number: str) -> dict | None:
        """Sale document (kisi bhi layout ka) number se - retry pe duplicate check."""
        data = api.get(f"/Sale/{endpoint}", params={"$filter": f"Number eq '{number}'", "$top": 1})
        items = (data or {}).get("Items") or []
        return items[0] if items else None

    def _post(self, api, path: str, body: dict) -> dict:
        """POST + response body ya Location header se UID."""
        resp = api.request("POST", path, json=body, raw=True)
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ConnectorError(f"MYOB error {resp.status_code}", retryable=True)
        if resp.status_code == 401:
            raise ConnectorError("MYOB connection expired - please reconnect in Settings → Integrations", reconnect=True)
        if resp.status_code >= 400:
            raise ConnectorError(self.parse_error(resp))
        data = {}
        if resp.content:
            try:
                data = resp.json() or {}
            except ValueError:
                data = {}
        if not data.get("UID"):
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            if location:
                data["UID"] = location.rstrip("/").split("/")[-1]
        if not data.get("UID"):
            raise ConnectorError("MYOB didn't return a record ID")
        return data

    def _tax_uid(self, api, product, dest_config: dict) -> str | None:
        return (api.tax_ref(product.tax_type) or api.tax_ref(dest_config.get("default_tax_type"))
                or api.tax_ref("GST" if product.is_taxable else "FRE"))

    def push_order(self, api, order, cfg, dest_config: dict) -> PushResult:
        self._require_cf(api)
        lines = [l for l in order.lines if l.product]
        if not lines:
            raise ConnectorError("None of the order lines are matched to a product")
        endpoint, kind = DOC_ENDPOINTS.get(dest_config.get("document_type"), DOC_ENDPOINTS["invoice_authorised"])
        label = {"invoice": "Invoice", "order": "Sales order", "quote": "Quote"}[kind]
        number = f"OD{order.id}"[:13]

        # MYOB mein idempotency key nahi hoti - retry pe pehle dekho: pichli koshish mein ban to nahi gaya tha?
        if dest_config.get("_retry"):
            found = self._find_by_number(api, endpoint, number)
            if found:
                st = _status(found) if found.get("Status") else InvoiceStatus(status="authorised", number=number)
                return PushResult(
                    external_id=found["UID"], number=found.get("Number") or number, status=st.status, kind=kind,
                    message=f"{label} {found.get('Number') or number} already in MYOB (created on an earlier attempt)",
                    raw=found, invoice_status=st if kind == "invoice" else None,
                )

        customer_uid, new_customer = self._customer_uid(api, order, dest_config)

        # Saari lines MYOB items hain? -> Item layout, warna Service layout
        item_layout = all(own_external_id(l.product, self.key) for l in lines)
        out_lines = []
        for l in lines:
            p, qty, price = l.product, float(l.quantity or 0), line_price(l)
            total = round(qty * price, 2)
            tax = {"UID": self._tax_uid(api, p, dest_config)}
            if item_layout:
                out_lines.append({"Type": "Transaction", "Description": p.name, "ShipQuantity": qty,
                                  "UnitPrice": price, "Total": total, "Item": {"UID": p.external_id}, "TaxCode": tax})
            else:
                account_uid = api.account_ref(p.account_code) or api.account_ref(dest_config.get("default_account_code"))
                if not account_uid:
                    raise ConnectorError("MYOB: sales account not found - choose a default sales account in the destination settings "
                                         "(sync accounts first from Settings > Accounts & Tax)")
                out_lines.append({"Type": "Transaction", "Description": f"{p.name} ({qty:g} x {price:.2f})",
                                  "Total": total, "Account": {"UID": account_uid}, "TaxCode": tax})

        body = {
            "Number": number,
            "Date": date.today().isoformat(),
            "Customer": {"UID": customer_uid},
            "IsTaxInclusive": bool(cfg.prices_include_gst),
            "JournalMemo": f"OrderDraft #{order.id}",
            "Lines": out_lines,
        }
        if order.po_number:
            body["CustomerPurchaseOrderNumber"] = order.po_number[:20]
        if order.notes:
            body["Comment"] = order.notes[:255]
        if kind == "invoice" and dest_config.get("send_email"):
            body["InvoiceDeliveryStatus"] = "ToBeEmailed"

        layout = "Item" if item_layout else "Service"
        created = self._post(api, f"/Sale/{endpoint}/{layout}?returnBody=true", body)
        uid = created["UID"]
        st = _status(created) if created.get("Status") else InvoiceStatus(status="authorised", number=body["Number"])
        return PushResult(
            external_id=uid, number=created.get("Number") or body["Number"], status=st.status, kind=kind,
            message=f"{label} {created.get('Number') or body['Number']} created in MYOB ({layout} layout)",
            raw=created, invoice_status=st if kind == "invoice" else None,
            customer_external_id=customer_uid if new_customer else None,
        )

    def fetch_invoice(self, api, external_id: str) -> InvoiceStatus | None:
        self._require_cf(api)
        found = api.get(f"/Sale/Invoice/{external_id}")
        return _status(found) if found else None

    def invoice_url(self, conn, external_id: str) -> str | None:
        return None   # MYOB ka stable web link nahi hota


def _status(x: dict) -> InvoiceStatus:
    status = "paid" if x.get("Status") == "Closed" else "authorised"
    total = x.get("TotalAmount")
    balance = x.get("BalanceDueAmount")
    paid = round(float(total) - float(balance), 2) if total is not None and balance is not None else None
    return InvoiceStatus(status=status, number=x.get("Number"), subtotal=x.get("Subtotal"),
                         gst=x.get("TotalTax"), total=total, amount_paid=paid)


def _myob_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _myob_address(a: dict, prefix: str) -> dict:
    lines = [x.strip() for x in (a.get("Street") or "").replace("\r", "").split("\n") if x.strip()]
    return {f"{prefix}_line1": lines[0] if lines else "", f"{prefix}_line2": ", ".join(lines[1:]),
            f"{prefix}_city": a.get("City") or "", f"{prefix}_region": a.get("State") or "",
            f"{prefix}_postcode": a.get("PostCode") or "", f"{prefix}_country": a.get("Country") or "",
            f"{prefix}_attention": a.get("ContactName") or ""}


def _customer_row(c: dict) -> dict:
    """MYOB customer. Address Location 1 = postal/billing, Location 2 = delivery."""
    if c.get("IsIndividual"):
        name = " ".join(p for p in (c.get("FirstName"), c.get("LastName")) if p)
    else:
        name = c.get("CompanyName") or ""
    code = c.get("DisplayID") or ""
    if code.upper() == "*NONE":
        code = ""
    by_loc = {a.get("Location"): a for a in c.get("Addresses") or [] if a}
    main = by_loc.get(1) or next(iter(by_loc.values()), {})
    selling = c.get("SellingDetails") or {}
    row = {"name": name, "code": code, "email": main.get("Email") or "", "other_emails": "",
           "first_name": c.get("FirstName") or "", "last_name": c.get("LastName") or "",
           "phone": main.get("Phone1") or "", "mobile": main.get("Phone2") or "", "ddi": main.get("Phone3") or "",
           "fax": main.get("Fax") or "", "address": "",
           "abn": selling.get("ABN") or "", "sales_tax_type": (selling.get("TaxCode") or {}).get("Code") or "",
           "default_currency": (c.get("ForeignCurrency") or {}).get("Code") or "",
           "external_updated_at": _myob_time(c.get("LastModified")),
           "external_id": c.get("UID"), "is_active": c.get("IsActive", True)}
    if main:
        row.update(_myob_address(main, "postal"))
    row.update(_myob_address(by_loc.get(2) or main or {}, "delivery"))
    return row
