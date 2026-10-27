"""
Saare accounting software ek jagah. Naya software = provider file + yahan ek line.
"""
from app.modules.connectors.base import AccountingProvider, ConnectorError
from app.modules.connectors.myob import MyobProvider
from app.modules.connectors.quickbooks import QuickBooksProvider
from app.modules.connectors.xero import XeroProvider

PROVIDERS: dict[str, AccountingProvider] = {
    p.key: p for p in (XeroProvider(), QuickBooksProvider(), MyobProvider())
}


def get_provider(key: str) -> AccountingProvider:
    provider = PROVIDERS.get(key)
    if not provider:
        raise ConnectorError(f"Unknown accounting software: {key}")
    return provider
