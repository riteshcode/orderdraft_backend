"""
QuickBooks Online connector.
Docs: https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0
      https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/invoice

Dhyan rakhne wali baatein (gotchas):
- realmId = client ki QuickBooks company id. Callback ke query params mein aata hai,
  har API URL mein lagta hai: /v3/company/{realmId}/...
- Access token 1 ghanta; refresh token har refresh pe ROTATE hota hai (naya save karo)
  aur ~100 din mein expire - tab client ko dobara connect karna padta hai
- Har call mein ?minorversion=XX bhejte hain (naye fields ke liye)
- Account / tax / item sab Id se refer hote hain - isliye hamara "code" = QBO Id
- DocNumber max 21 characters; line Amount = Qty x UnitPrice (2 decimal) exact hona chahiye
- QBO mein draft invoice nahi hota - invoice banta hai to seedha "open" (authorised)
"""
import base64
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.modules.connectors.base import (
    AccountingProvider, ConnectorError, InvoiceStatus, Org, PushResult, TokenSet, line_price, own_external_id,
)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
PAGE_SIZE = 1000

CLASS_MAP = {"Revenue": "REVENUE", "Expense": "EXPENSE", "Asset": "ASSET", "Liability": "LIABILITY",
             "Equity": "EQUITY"}


def _q(value: str) -> str:
    """QBO query mein single quote escape: O'Brien -> O\\'Brien"""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _production() -> bool:
    return settings.QUICKBOOKS_ENVIRONMENT == "production"


class QuickBooksProvider(AccountingProvider):
    key = "quickbooks"
    label = "QuickBooks Online"
    doc_types = {"invoice_authorised": "Invoice", "quote": "Estimate"}
    supports_webhooks = True

    def configured(self) -> bool:
        return bool(settings.QUICKBOOKS_CLIENT_ID and settings.QUICKBOOKS_CLIENT_SECRET)

    # ---------------- OAuth ----------------
    def authorize_url(self, state: str) -> str:
        if not settings.QUICKBOOKS_CLIENT_ID:
            raise ConnectorError("QuickBooks isn't set up on this platform yet (QUICKBOOKS_CLIENT_ID)")
        return AUTH_URL + "?" + urlencode({
            "client_id": settings.QUICKBOOKS_CLIENT_ID, "response_type": "code",
            "scope": settings.QUICKBOOKS_SCOPES, "redirect_uri": settings.QUICKBOOKS_REDIRECT_URI, "state": state,
        })

    def _basic(self) -> str:
        raw = f"{settings.QUICKBOOKS_CLIENT_ID}:{settings.QUICKBOOKS_CLIENT_SECRET}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_call(self, data: dict) -> TokenSet:
        try:
            resp = httpx.post(TOKEN_URL, headers={"Authorization": self._basic(), "Accept": "application/json"},
                              data=data, timeout=30)
        except httpx.HTTPError as e:
            raise ConnectorError(f"QuickBooks login failed: {e}", retryable=True)
        if resp.status_code != 200:
            raise ConnectorError("QuickBooks connection expired - please reconnect in Settings → Integrations", reconnect=True)
        d = resp.json()
        return TokenSet(d["access_token"], d.get("refresh_token"), int(d.get("expires_in", 3600)),
                        refresh_expires_in=d.get("x_refresh_token_expires_in"), raw=d)

    def exchange_code(self, code: str, params: dict) -> TokenSet:
        return self._token_call({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": settings.QUICKBOOKS_REDIRECT_URI})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, refresh_token: str) -> None:
        try:
            httpx.post(REVOKE_URL, headers={"Authorization": self._basic(), "Accept": "application/json"},
                       json={"token": refresh_token}, timeout=15)
        except httpx.HTTPError:
            pass

    def list_orgs(self, api, params: dict) -> list[Org]:
        realm = params.get("realmId")
        if not realm:
            raise ConnectorError("QuickBooks didn't send a company ID (realmId)")
        api.conn.external_tenant_id = str(realm)          # api_base ko chahiye
        info = self._get(api, f"/companyinfo/{realm}").get("CompanyInfo") or {}
        return [Org(id=str(realm), name=info.get("CompanyName") or f"QuickBooks company {realm}")]

    # ---------------- API ----------------
    def api_base(self, conn) -> str:
        host = "quickbooks.api.intuit.com" if _production() else "sandbox-quickbooks.api.intuit.com"
        return f"https://{host}/v3/company/{conn.external_tenant_id}"

    def parse_error(self, resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"QuickBooks error {resp.status_code}: {resp.text[:300]}"
        errors = ((data.get("Fault") or data.get("fault") or {}).get("Error")
                  or (data.get("Fault") or data.get("fault") or {}).get("error") or [])
        messages = []
        for e in errors:
            msg = e.get("Message") or e.get("message") or ""
            detail = e.get("Detail") or e.get("detail") or ""
            messages.append(f"{msg}: {detail}" if detail and detail != msg else msg)
        if messages:
            return "QuickBooks: " + "; ".join(m for m in messages if m)
        return f"QuickBooks error {resp.status_code}: {str(data)[:300]}"

    # minorversion har call mein
    def _params(self, params: dict | None = None) -> dict:
        return {**(params or {}), "minorversion": settings.QUICKBOOKS_MINOR_VERSION}

    def _get(self, api, path: str, params: dict | None = None) -> dict:
        return api.get(path, params=self._params(params))

    def _post(self, api, path: str, body: dict | None, params: dict | None = None) -> dict:
        return api.post(path, json=body, params=self._params(params))

    def query(self, api, entity: str, where: str = "") -> list[dict]:
        """SELECT * FROM {entity} {where} - 1000-1000 karke saare pages."""
        rows, start = [], 1
        while True:
            sql = f"SELECT * FROM {entity} {where} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}".replace("  ", " ")
            batch = (self._get(api, "/query", {"query": sql}).get("QueryResponse") or {}).get(entity) or []
            rows.extend(batch)
            if len(batch) < PAGE_SIZE or start > 200 * PAGE_SIZE:
                break
            start += PAGE_SIZE
        return rows

    def _where(self, conds: list[str], since: datetime | None) -> str:
        if since:
            conds = conds + [f"MetaData.LastUpdatedTime > '{since.strftime('%Y-%m-%dT%H:%M:%S')}'"]
        return ("WHERE " + " AND ".join(conds)) if conds else ""

    # ---------------- Data ----------------
    def fetch_accounts(self, api) -> list[dict]:
        rows = []
        for a in self.query(api, "Account"):
            name = a.get("Name") or a["Id"]
            rows.append({"code": str(a["Id"]), "name": f"{a['AcctNum']} {name}" if a.get("AcctNum") else name,
                         "account_class": CLASS_MAP.get(a.get("Classification"), "REVENUE"),
                         "external_id": str(a["Id"]), "is_active": a.get("Active", True) is not False})
        return rows

    def fetch_tax_rates(self, api) -> list[dict]:
        rate_values = {str(r["Id"]): float(r.get("RateValue") or 0) for r in self.query(api, "TaxRate")}
        rows = []
        for t in self.query(api, "TaxCode"):
            sales = ((t.get("SalesTaxRateList") or {}).get("TaxRateDetail")) or []
            purchases = ((t.get("PurchaseTaxRateList") or {}).get("TaxRateDetail")) or []
            if not sales and not purchases:
                continue
            rate = sum(rate_values.get(str((d.get("TaxRateRef") or {}).get("value")), 0) for d in sales)
            applies = "both" if sales and purchases else ("sales" if sales else "purchases")
            rows.append({"code": str(t["Id"]), "name": t.get("Name") or str(t["Id"]), "rate": round(rate, 4),
                         "applies_to": applies, "external_id": str(t["Id"]),
                         "is_active": t.get("Active", True) is not False})
        return rows

    def fetch_products(self, api, since: datetime | None) -> list[dict]:
        where = self._where(["Type IN ('Inventory', 'NonInventory', 'Service')"], since)
        rows = []
        for item in self.query(api, "Item", where):
            if item.get("Active") is False:       # import inactive products support nahi karta
                continue
            taxable = item.get("Taxable")
            rows.append({
                "sku": (item.get("Sku") or item.get("Name") or str(item["Id"]))[:100],
                "name": item.get("Name") or str(item["Id"]), "description": item.get("Description") or "",
                "price": item.get("UnitPrice"),
                "account_code": (item.get("IncomeAccountRef") or {}).get("value") or "",
                "tax_type": (item.get("SalesTaxCodeRef") or {}).get("value") or "",
                "is_taxable": ("yes" if taxable else "no") if taxable is not None else "",
                "purchase_description": item.get("PurchaseDesc") or "",
                "purchase_price": item.get("PurchaseCost"),
                "purchase_account_code": (item.get("ExpenseAccountRef") or {}).get("value") or "",
                "purchase_tax_type": (item.get("PurchaseTaxCodeRef") or {}).get("value") or "",
                "is_tracked_inventory": "yes" if item.get("TrackQtyOnHand") else "no",
                "quantity_on_hand": item.get("QtyOnHand") if item.get("TrackQtyOnHand") else None,
                "is_purchased": "yes" if item.get("ExpenseAccountRef") or item.get("PurchaseCost") else "no",
                "external_updated_at": _qb_time((item.get("MetaData") or {}).get("LastUpdatedTime")),
                "external_id": str(item["Id"]),
            })
        return rows

    def fetch_customers(self, api, since: datetime | None, include_all: bool) -> list[dict]:
        rows = []
        for c in self.query(api, "Customer", self._where([], since)):
            emails = [e.strip() for e in ((c.get("PrimaryEmailAddr") or {}).get("Address") or "").split(",") if e.strip()]
            phone = lambda k: (c.get(k) or {}).get("FreeFormNumber") or ""
            row = {
                "name": c.get("CompanyName") or c.get("DisplayName") or "", "code": "",
                "email": emails[0] if emails else "", "other_emails": ", ".join(emails[1:]),
                "first_name": c.get("GivenName") or "", "last_name": c.get("FamilyName") or "",
                "phone": phone("PrimaryPhone") or phone("Mobile"), "mobile": phone("Mobile"), "fax": phone("Fax"),
                "address": "", "abn": c.get("ResaleNum") or "",
                "default_currency": (c.get("CurrencyRef") or {}).get("value") or "",
                "external_updated_at": _qb_time((c.get("MetaData") or {}).get("LastUpdatedTime")),
                "external_id": str(c["Id"]), "is_active": c.get("Active", True) is not False,
            }
            # BillAddr = postal, ShipAddr = delivery
            for key, prefix in (("BillAddr", "postal"), ("ShipAddr", "delivery")):
                a = c.get(key) or {}
                row.update({f"{prefix}_line1": a.get("Line1") or "",
                            f"{prefix}_line2": ", ".join(x for x in (a.get("Line2"), a.get("Line3")) if x),
                            f"{prefix}_city": a.get("City") or "", f"{prefix}_region": a.get("CountrySubDivisionCode") or "",
                            f"{prefix}_postcode": a.get("PostalCode") or "", f"{prefix}_country": a.get("Country") or ""})
            rows.append(row)
        return rows

    def fetch_suppliers(self, api, since: datetime | None) -> list[dict]:
        rows = []
        for v in self.query(api, "Vendor", self._where([], since)):
            bill = v.get("BillAddr") or {}
            address = ", ".join(x for x in (bill.get("Line1"), bill.get("Line2"), " ".join(
                y for y in (bill.get("City"), bill.get("CountrySubDivisionCode"), bill.get("PostalCode")) if y),
                bill.get("Country")) if x)
            emails = [e.strip() for e in ((v.get("PrimaryEmailAddr") or {}).get("Address") or "").split(",") if e.strip()]
            rows.append({
                "name": v.get("CompanyName") or v.get("DisplayName") or "", "code": "",
                "email": emails[0] if emails else "", "cc_emails": ", ".join(emails[1:]),
                "contact_name": " ".join(x for x in (v.get("GivenName"), v.get("FamilyName")) if x),
                "phone": (v.get("PrimaryPhone") or {}).get("FreeFormNumber") or (v.get("Mobile") or {}).get("FreeFormNumber") or "",
                "address": address, "abn": "", "currency": (v.get("CurrencyRef") or {}).get("value") or "",
                "account_number": v.get("AcctNum") or "", "payment_terms_days": None,
                "external_id": str(v["Id"]), "is_active": v.get("Active", True) is not False,
            })
        return rows

    # ---------------- Export ----------------
    def _customer_ref(self, api, order) -> str:
        customer = order.customer
        own = own_external_id(customer, self.key)
        if own:
            return own
        name = (customer.name if customer else (order.customer_raw or "Unknown customer"))[:500]
        found = self.query(api, "Customer", f"WHERE DisplayName = '{_q(name)}'")
        if found:
            return str(found[0]["Id"])
        body = {"DisplayName": name, "CompanyName": name[:100]}
        if customer and customer.email:
            body["PrimaryEmailAddr"] = {"Address": customer.email}
        return str(self._post(api, "/customer", body)["Customer"]["Id"])

    def _item_ref(self, api, product, dest_config: dict) -> str:
        own = own_external_id(product, self.key)
        if own:
            return own
        name = product.name[:100]
        found = self.query(api, "Item", f"WHERE Name = '{_q(name)}'")
        if found:
            item_id = str(found[0]["Id"])
        else:
            default_code = dest_config.get("default_account_code") or ""
            income = (api.account_ref(product.account_code) or api.account_ref(default_code)
                      or (default_code if default_code.isdigit() else None))
            if not income:
                raise ConnectorError("A default sales account is needed to create new items in QuickBooks - "
                                     "choose a 'Default sales account' in the destination settings")
            body = {"Name": name, "Type": "NonInventory", "IncomeAccountRef": {"value": income}}
            if product.price is not None:
                body["UnitPrice"] = float(product.price)
            item_id = str(self._post(api, "/item", body)["Item"]["Id"])
        # Agli baar dhundhna na pade (sirf tab jab product kisi aur software se linked na ho)
        if not product.external_id:
            product.external_id = item_id
            product.source = self.key
        return item_id

    def build_document(self, api, order, cfg, dest_config: dict) -> dict:
        lines = []
        for line in order.lines:
            p = line.product
            if not p:
                continue
            qty = float(line.quantity or 0)
            price = round(line_price(line), 2)
            detail = {"ItemRef": {"value": self._item_ref(api, p, dest_config)}, "Qty": qty, "UnitPrice": price}
            tax = api.tax_ref(p.tax_type) or api.tax_ref(dest_config.get("default_tax_type"))
            if tax:
                detail["TaxCodeRef"] = {"value": tax}
            lines.append({"DetailType": "SalesItemLineDetail", "Amount": round(qty * price, 2),
                          "Description": p.name, "SalesItemLineDetail": detail})
        if not lines:
            raise ConnectorError("None of the order lines are matched to a product")

        memo = f"OrderDraft #{order.id}" + (f", PO {order.po_number}" if order.po_number else "")
        today = date.today()
        doc = {
            "CustomerRef": {"value": self._customer_ref(api, order)},
            "Line": lines, "DocNumber": f"OD{order.id}"[:21], "TxnDate": today.isoformat(),
            "GlobalTaxCalculation": "TaxInclusive" if cfg.prices_include_gst else "TaxExcluded",
            # CurrencyRef nahi bhejte - company ki home currency / customer ki currency apne aap
            "CustomerMemo": {"value": memo}, "PrivateNote": memo,
        }
        if order.customer and order.customer.email:
            doc["BillEmail"] = {"Address": order.customer.email}
        if dest_config["document_type"] != "quote":
            doc["DueDate"] = (today + timedelta(days=cfg.payment_terms_days or 0)).isoformat()
        return doc

    def push_order(self, api, order, cfg, dest_config: dict) -> PushResult:
        doc = self.build_document(api, order, cfg, dest_config)
        customer_id = doc["CustomerRef"]["value"]

        if dest_config["document_type"] == "quote":
            est = self._post(api, "/estimate", doc, _idem(dest_config))["Estimate"]
            return PushResult(external_id=str(est["Id"]), number=est.get("DocNumber"), kind="quote",
                              message=f"Estimate {est.get('DocNumber', '')} created in QuickBooks", raw=est,
                              customer_external_id=customer_id)

        inv = self._post(api, "/invoice", doc, _idem(dest_config))["Invoice"]
        invoice_id = str(inv["Id"])
        status = "authorised"
        if dest_config.get("send_email"):
            params = {"sendTo": order.customer.email} if order.customer and order.customer.email else None
            sent = self._post(api, f"/invoice/{invoice_id}/send", None, params).get("Invoice")
            inv = sent or inv
            status = "sent"
        st = _status(inv)
        if status == "sent" and st.status == "authorised":
            st.status = "sent"
        return PushResult(
            external_id=invoice_id, number=inv.get("DocNumber"), status=st.status, raw=inv, invoice_status=st,
            message=f"Invoice {inv.get('DocNumber') or ''} created in QuickBooks", url=self.invoice_url(api.conn, invoice_id),
            customer_external_id=customer_id,
        )

    def fetch_invoice(self, api, external_id: str) -> InvoiceStatus | None:
        inv = self._get(api, f"/invoice/{external_id}").get("Invoice")
        return _status(inv) if inv else None

    def invoice_url(self, conn, external_id: str) -> str | None:
        host = "app.qbo.intuit.com" if _production() else "app.sandbox.qbo.intuit.com"
        return f"https://{host}/app/invoice?txnId={external_id}"


def _idem(dest_config: dict) -> dict | None:
    """QuickBooks requestid (max 50) - retry pe same id = QuickBooks duplicate nahi banata, pehla wala lauta deta hai."""
    key = dest_config.get("_idempotency_key")
    return {"requestid": key[:50]} if key else None


def _status(inv: dict) -> InvoiceStatus:
    total = float(inv.get("TotalAmt") or 0)
    balance = float(inv.get("Balance") if inv.get("Balance") is not None else total)
    tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax") or 0)
    if total == 0 and "void" in (inv.get("PrivateNote") or "").lower():
        status = "void"
    elif total > 0 and balance == 0:
        status = "paid"
    elif inv.get("EmailStatus") == "EmailSent":
        status = "sent"
    else:
        status = "authorised"
    return InvoiceStatus(status=status, number=inv.get("DocNumber"), subtotal=round(total - tax, 2), gst=tax,
                         total=total, amount_paid=round(total - balance, 2))


def _qb_time(value):
    """QuickBooks "2026-04-18T10:20:30-07:00" -> naive UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
