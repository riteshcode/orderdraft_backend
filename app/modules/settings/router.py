"""
Client settings: business details + invoice settings - client ke apne DB mein (TenantSettings).
Business name main DB (tenants registry) mein bhi copy hota hai (admin list / emails ke liye).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings as cfg
from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import get_tenant_settings, log_activity
from app.models import Tenant, TenantSettings, User

router = APIRouter(prefix="/settings", tags=["settings"])
admin_only = require_role("owner", "admin")


class BusinessIn(BaseModel):
    name: str
    abn: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    timezone: str = "Australia/Sydney"


class InvoiceSettingsIn(BaseModel):
    invoice_prefix: str = "INV-"
    invoice_next_number: int = Field(1001, ge=1)
    gst_rate: float = Field(10, ge=0, le=100)
    prices_include_gst: bool = False
    payment_terms_days: int = Field(14, ge=0, le=365)
    bank_details: str | None = None
    invoice_footer: str | None = None


class StockSettingsIn(BaseModel):
    deduct_on_approve: bool | None = None       # None = jaisa hai waisa
    po_prefix: str | None = Field(None, max_length=20)
    po_next_number: int | None = Field(None, ge=1)


def settings_out(s: TenantSettings, tenant: Tenant) -> dict:
    return {
        "business": {"name": s.business_name, "abn": s.abn, "address": s.address, "phone": s.phone,
                     "email": s.email, "timezone": s.timezone},
        "invoice": {"invoice_prefix": s.invoice_prefix, "invoice_next_number": s.invoice_next_number,
                    "gst_rate": s.gst_rate, "prices_include_gst": s.prices_include_gst,
                    "payment_terms_days": s.payment_terms_days, "bank_details": s.bank_details,
                    "invoice_footer": s.invoice_footer},
        "stock": {"deduct_on_approve": s.stock_deduct_on_approve, "po_prefix": s.po_prefix,
                  "po_next_number": s.po_next_number},
        "inbound_email": f"{tenant.slug}@{cfg.INBOUND_DOMAIN}",
    }


@router.get("")
def get_settings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_tenant_settings(db, user.tenant_id)
    db.commit()
    return settings_out(s, user.tenant)


@router.put("/business")
def save_business(data: BusinessIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(400, "Enter a business name")
    s = get_tenant_settings(db, user.tenant_id)
    s.business_name = data.name.strip()
    s.abn, s.address, s.phone, s.email, s.timezone = data.abn, data.address, data.phone, data.email, data.timezone
    user.tenant.name = s.business_name        # main registry copy
    db.commit()
    return settings_out(s, user.tenant)


@router.put("/invoice")
def save_invoice_settings(data: InvoiceSettingsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    s = get_tenant_settings(db, user.tenant_id)
    for k, v in data.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return settings_out(s, user.tenant)


@router.put("/stock")
def save_stock(data: StockSettingsIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Order approve hone par tracked products ka stock kam karna hai ya nahi."""
    s = get_tenant_settings(db, user.tenant_id, for_update=True)
    if data.deduct_on_approve is not None:
        s.stock_deduct_on_approve = data.deduct_on_approve
    if data.po_prefix is not None:
        s.po_prefix = data.po_prefix.strip()
    if data.po_next_number is not None:
        s.po_next_number = data.po_next_number
    db.commit()
    return settings_out(s, user.tenant)
