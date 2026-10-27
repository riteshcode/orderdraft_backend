"""
External connections (Xero), export destinations, export jobs, import jobs, mapping templates.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase, utcnow


class Connection(TenantBase):
    """
    Accounting software connection (Xero / QuickBooks / MYOB) - client ke apne DB mein.
    Tokens encrypted (core/crypto.py). Ek client ka ek hi accounting software active hota hai.
    """
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(30))  # xero | quickbooks | myob
    # pending_org = login ho gaya, organisation / company file chunna baaki | connected
    status: Mapped[str] = mapped_column(String(20), default="connected")
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Xero tenantId | QuickBooks realmId | MYOB company file id
    external_tenant_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    external_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Provider specific: MYOB company file URI, encrypted cf credentials, available orgs list ...
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=True)          # nightly sync
    include_all_contacts: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(300), nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Destination(TenantBase):
    """
    Approved order kahan jaaye.
    type: accounting | custom_api | webhook | internal_invoice  (accounting = connected Xero/QuickBooks/MYOB)
    config: type ke hisaab se JSON (secrets "enc:" se encrypted)
    """
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(30))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=True)   # approve hote hi bhejo
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExportJob(TenantBase):
    """Ek order ko ek destination pe bhejne ki koshish (retry ke saath)."""
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("draft_orders.id"), index=True)
    destination_id: Mapped[int] = mapped_column(ForeignKey("destinations.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|running|success|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    destination: Mapped[Destination] = relationship()


class ImportJob(TenantBase):
    """Har CSV upload / Xero sync ka record."""
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20))       # products | customers | history
    source: Mapped[str] = mapped_column(String(20))     # csv | xero
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="done")  # running | done | failed
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # main DB users.id (FK nahi - alag DB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MappingTemplate(TenantBase):
    """CSV column mapping yaad rakhna - same headers wali file aaye to auto-apply."""
    __tablename__ = "mapping_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    headers_signature: Mapped[str] = mapped_column(String(64), index=True)  # sorted headers ka hash
    mapping: Mapped[dict] = mapped_column(JSON)    # {"CSV Column": "our_field"}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
