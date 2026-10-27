"""
Accounting software destination - approved order client ke connected software
(Xero / QuickBooks Online / MYOB) mein invoice / quote / sales order banta hai.

config:
{
  "document_type": "invoice_draft",   # invoice_draft | invoice_authorised | quote | sales_order
                                      # (software jo support na kare -> uska default)
  "default_account_code": "200",      # product ka account_code na ho tab
  "default_tax_type": "",             # khali = software/product ka default
  "send_email": false                 # software se customer ko invoice email
}
Kaunsa software - ye destination mein nahi, client ki connection se aata hai
(ek client = ek software). Software badla to destination wahi rehta hai.
"""
from app.models import DraftOrder, TenantSettings
from app.modules.connectors.api import open_api
from app.modules.connectors.base import ConnectorError
from app.modules.connectors.sync import apply_status
from app.modules.destinations.base import ExportResult
from app.modules.invoices.service import create_invoice_from_order

TYPE = "accounting"
LABEL = "Accounting software"
SECRET_FIELDS: list[str] = []
DOC_TYPES = ("invoice_draft", "invoice_authorised", "quote", "sales_order")


def validate_config(config: dict) -> dict:
    doc_type = config.get("document_type") or "invoice_draft"
    if doc_type not in DOC_TYPES:
        raise ValueError("Choose a valid document type")
    return {
        "document_type": doc_type,
        "default_account_code": (config.get("default_account_code") or "").strip(),
        "default_tax_type": (config.get("default_tax_type") or "").strip(),
        "send_email": bool(config.get("send_email")),
    }


def effective_doc_type(provider, doc_type: str) -> str:
    """Software ye type support na kare to: draft -> approved invoice, warna pehla type."""
    if doc_type in provider.doc_types:
        return doc_type
    if doc_type == "invoice_draft" and "invoice_authorised" in provider.doc_types:
        return "invoice_authorised"
    return next(iter(provider.doc_types))


def send(db, order: DraftOrder, config: dict, tenant: TenantSettings) -> ExportResult:
    try:
        api = open_api(db, tenant.tenant_id)
    except ConnectorError as e:
        return ExportResult(False, str(e))
    provider = api.provider
    cfg = {**config, "document_type": effective_doc_type(provider, config.get("document_type") or "invoice_draft")}

    try:
        result = provider.push_order(api, order, tenant, cfg)
    except ConnectorError as e:
        return ExportResult(False, str(e), retryable=e.retryable)

    # Naya contact bana / mila - id yaad rakho (agli baar naam se dhundhna nahi padega)
    customer = order.customer
    if customer and result.customer_external_id and not (customer.external_id and customer.source == provider.key):
        customer.external_id = result.customer_external_id
        customer.source = provider.key

    if result.kind == "invoice":
        # Invoice tracking row (invoice module + dashboard), status software se
        invoice = create_invoice_from_order(db, order, tenant, source=provider.key, external_id=result.external_id,
                                            number=result.number, status=result.status)
        if result.invoice_status:
            apply_status(invoice, result.invoice_status)

    return ExportResult(True, result.message or f"{result.kind.title()} {result.number or ''} created in {provider.label}",
                        external_id=result.external_id, external_url=result.url)
