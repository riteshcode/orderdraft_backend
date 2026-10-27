"""
Account codes + tax rates: defaults aur "is line pe kitna tax lagega".
Software se import: app/modules/connectors/sync.py (import_chart).

Tax kaise decide hota hai (invoice line):
  1. Line / product ka tax_code hai aur wo TaxRate table mein hai -> uska rate
  2. Warna product.is_taxable ? tenant.gst_rate : 0
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountCode, TaxRate, TenantSettings

# Naye tenant ke liye Australian defaults (Xero ke standard codes jaise)
DEFAULT_ACCOUNTS = [
    {"code": "200", "name": "Sales", "account_class": "REVENUE"},
    {"code": "260", "name": "Other Revenue", "account_class": "REVENUE"},
]
DEFAULT_TAX_RATES = [
    {"code": "OUTPUT", "name": "GST on Income", "rate": 10.0, "applies_to": "sales"},
    {"code": "EXEMPTOUTPUT", "name": "GST Free Income", "rate": 0.0, "applies_to": "sales"},
    {"code": "BASEXCLUDED", "name": "BAS Excluded", "rate": 0.0, "applies_to": "both"},
]

XERO_CLASSES = {"REVENUE", "EXPENSE", "ASSET", "LIABILITY", "EQUITY"}


def ensure_defaults(db: Session, tenant_id: int, commit: bool = True):
    """Tenant ke paas ek bhi account / tax rate nahi hai to defaults bana do."""
    has_acc = db.scalar(select(func.count()).select_from(AccountCode).where(AccountCode.tenant_id == tenant_id))
    has_tax = db.scalar(select(func.count()).select_from(TaxRate).where(TaxRate.tenant_id == tenant_id))
    if not has_acc:
        for a in DEFAULT_ACCOUNTS:
            db.add(AccountCode(tenant_id=tenant_id, **a))
    if not has_tax:
        for t in DEFAULT_TAX_RATES:
            db.add(TaxRate(tenant_id=tenant_id, **t))
    if not has_acc or not has_tax:
        db.commit() if commit else db.flush()


def tax_rate_map(db: Session, tenant_id: int) -> dict[str, float]:
    """{"OUTPUT": 10.0, "EXEMPTOUTPUT": 0.0}  (defaults pehle ensure - naye tenant ke liye bhi sahi rate)"""
    ensure_defaults(db, tenant_id, commit=False)   # caller ka transaction - beech mein commit nahi
    return {t.code: t.rate for t in db.scalars(select(TaxRate).where(TaxRate.tenant_id == tenant_id))}


def resolve_tax(tenant: TenantSettings, rates: dict[str, float], tax_code: str | None, taxable: bool = True) -> tuple[str | None, float]:
    """Returns (tax_code, rate_percent)."""
    if tax_code and tax_code in rates:
        return tax_code, float(rates[tax_code])
    if not taxable:
        return tax_code, 0.0
    return tax_code, float(tenant.gst_rate or 0)
