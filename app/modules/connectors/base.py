"""
Accounting connector contract - Xero, QuickBooks Online, MYOB sab isi shape mein.

Har client (tenant) APNA account connect karta hai. Platform ka ek OAuth app per software
(.env mein client id/secret), tokens client ke apne DB (connections table) mein encrypted.
Ek client ek hi accounting software use karta hai (connect karte waqt check hota hai).

Naya software (Reckon, Sage...) = nayi file jo AccountingProvider implement kare + registry.py mein ek line.

Provider ko kya implement karna hai:

  OAuth
    authorize_url(state)                -> login URL
    exchange_code(code, params)         -> TokenSet   (params = callback ke saare query params, e.g. realmId)
    refresh(refresh_token)              -> TokenSet
    revoke(refresh_token)               -> best effort
    list_orgs(api, params)              -> [Org]  (Xero orgs / QBO realm / MYOB company files)
    activate_org(api, org, credentials) -> Connection.extra mein jo chahiye (MYOB: company file URI + login)

  API
    api_base(conn), auth_headers(conn, token), parse_error(resp)

  Data (normalized dicts - neeche NORMALIZED SHAPES dekho)
    fetch_accounts(api) / fetch_tax_rates(api) / fetch_products(api, since) / fetch_customers(api, since, include_all)
    fetch_suppliers(api, since)

  Export
    push_order(api, order, cfg, dest_config) -> PushResult
    fetch_invoice(api, external_id)          -> InvoiceStatus | None
    invoice_url(conn, external_id)           -> str | None

NORMALIZED SHAPES
  account : {"code", "name", "account_class" (REVENUE|EXPENSE|ASSET|LIABILITY|EQUITY), "external_id", "is_active"}
  tax rate: {"code", "name", "rate" (percent), "applies_to" (sales|purchases|both), "external_id", "is_active"}
  product : import_products() row - {"sku", "name", "description", "price", "unit", "account_code",
            "tax_type", "is_taxable" ("yes"/"no"/""), "external_id"}
  customer: import_customers() row - {"name", "code", "email", "other_emails", "phone", "address", "abn",
            "external_id", "is_active"}
"""
from dataclasses import dataclass, field
from datetime import datetime


class ConnectorError(Exception):
    """
    retryable = network / 5xx / rate limit -> export job baad mein dobara
    reconnect = token revoke / expire -> user ko dobara connect karna padega
    """

    def __init__(self, message: str, retryable: bool = False, reconnect: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.reconnect = reconnect


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int = 1800                     # seconds
    refresh_expires_in: int | None = None      # QBO: ~100 din
    raw: dict = field(default_factory=dict)


@dataclass
class Org:
    id: str
    name: str
    extra: dict = field(default_factory=dict)   # MYOB: {"uri": ..., "requires_login": bool}


@dataclass
class InvoiceStatus:
    status: str                      # draft | authorised | sent | paid | void
    number: str | None = None
    subtotal: float | None = None
    gst: float | None = None
    total: float | None = None
    amount_paid: float | None = None


@dataclass
class PushResult:
    external_id: str
    number: str | None = None
    status: str = "draft"            # hamara invoice status: draft | authorised | sent | paid
    message: str = ""
    url: str | None = None
    kind: str = "invoice"            # invoice | quote | order (quote/order ka invoice row nahi banta)
    raw: dict = field(default_factory=dict)
    customer_external_id: str | None = None   # naya contact bana ho to yaad rakhne ke liye
    invoice_status: InvoiceStatus | None = None  # software ke totals / status (tracking row ke liye)


class AccountingProvider:
    key: str = ""
    label: str = ""
    # document_type -> label (destination form dropdown)
    doc_types: dict[str, str] = {}
    # MYOB company file ke liye username/password lag sakta hai
    org_login_fields: list[str] = []
    # Xero / QBO webhook bhejte hain; MYOB nahi (polling hi)
    supports_webhooks: bool = False

    # ---- config ----
    def configured(self) -> bool:
        raise NotImplementedError

    # ---- OAuth ----
    def authorize_url(self, state: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str, params: dict) -> TokenSet:
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> TokenSet:
        raise NotImplementedError

    def revoke(self, refresh_token: str) -> None:
        return None

    def list_orgs(self, api, params: dict) -> list[Org]:
        raise NotImplementedError

    def activate_org(self, api, org: Org, credentials: dict | None) -> dict:
        """Org chuna gaya. Return: Connection.extra mein merge karne wali values."""
        return {}

    # ---- low level API ----
    def api_base(self, conn) -> str:
        raise NotImplementedError

    def auth_headers(self, conn, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def parse_error(self, resp) -> str:
        return f"{self.label} error {resp.status_code}: {resp.text[:300]}"

    # ---- data ----
    def fetch_accounts(self, api) -> list[dict]:
        raise NotImplementedError

    def fetch_tax_rates(self, api) -> list[dict]:
        raise NotImplementedError

    def fetch_products(self, api, since: datetime | None) -> list[dict]:
        raise NotImplementedError

    def fetch_customers(self, api, since: datetime | None, include_all: bool) -> list[dict]:
        raise NotImplementedError

    def fetch_suppliers(self, api, since: datetime | None) -> list[dict]:
        """Supplier rows: {name, code, email, contact_name, phone, address, abn, currency,
        payment_terms_days, external_id, is_active}. Software jo nahi deta wo field khali."""
        return []

    # ---- export ----
    def push_order(self, api, order, cfg, dest_config: dict) -> PushResult:
        raise NotImplementedError

    def fetch_invoice(self, api, external_id: str) -> InvoiceStatus | None:
        raise NotImplementedError

    def invoice_url(self, conn, external_id: str) -> str | None:
        return None


# ---- chhote helpers jo sab providers use karte hain ----
def own_external_id(obj, provider_key: str) -> str | None:
    """
    Product/Customer ka external_id sirf tab bharosa karo jab wo isi software se aaya ho.
    (Client ne pehle Xero use kiya, ab QuickBooks - purane Xero ids QuickBooks mein bekaar hain.)
    """
    if obj is not None and obj.external_id and obj.source == provider_key:
        return obj.external_id
    return None


def line_price(line) -> float:
    price = line.unit_price if line.unit_price is not None else (line.product.price if line.product else None)
    return float(price or 0)
