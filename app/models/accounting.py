"""
Chart of accounts aur tax rates - Xero se import ya manually banao.
Products / invoice lines / Xero export inke `code` use karte hain.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase, utcnow


class AccountCode(TenantBase):
    """Example: 200 - Sales (REVENUE)"""
    __tablename__ = "account_codes"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_account_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    # REVENUE | EXPENSE | ASSET | LIABILITY | EQUITY  (Xero ka "Class")
    account_class: Mapped[str] = mapped_column(String(30), default="REVENUE")
    source: Mapped[str] = mapped_column(String(20), default="manual")   # manual | xero | quickbooks | myob
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TaxRate(TenantBase):
    """Example: OUTPUT - GST on Income - 10%,  EXEMPTOUTPUT - GST Free Income - 0%"""
    __tablename__ = "tax_rates"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_tax_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(50))          # Xero TaxType
    name: Mapped[str] = mapped_column(String(200))
    rate: Mapped[float] = mapped_column(Float, default=0)  # percent: 10 = 10%
    applies_to: Mapped[str] = mapped_column(String(20), default="sales")  # sales | purchases | both
    source: Mapped[str] = mapped_column(String(20), default="manual")   # manual | xero | quickbooks | myob
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # QBO TaxCode Id / MYOB UID
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
