"""
Purchasing: suppliers, supplier ke products (cost / SKU), purchase orders, stock received (GRN).

Flow:  Supplier -> Purchase order (draft -> sent) -> Stock received (ek ya kai baar) -> received / closed
Stock received = tracked products ka on hand badhta hai + product ka average cost update hota hai.
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase, utcnow
from app.models.catalog import Product


class Supplier(TenantBase):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)       # PO yahan jaata hai
    cc_emails: Mapped[str | None] = mapped_column(Text, nullable=True)           # comma separated
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    abn: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    account_number: Mapped[str | None] = mapped_column(String(100), nullable=True)   # supplier ke yahan hamara account no
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)   # Trash (soft delete)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    products: Mapped[list["SupplierProduct"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class SupplierProduct(TenantBase):
    """Ye supplier ye product kis code / cost pe deta hai."""
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "product_id", name="uq_supplier_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    supplier_sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)   # agreed price (ex GST)
    min_order_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)              # reorder isi se
    last_cost: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)   # last receipt ka cost
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    supplier: Mapped[Supplier] = relationship(back_populates="products")
    product: Mapped[Product] = relationship()


class PurchaseOrder(TenantBase):
    """
    status: draft | sent | partial | received | closed | cancelled
      partial  = kuch maal aaya, baaki aana hai
      received = sab aa gaya
      closed   = baaki ab nahi aayega (manually band kiya)
    """
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_po_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    number: Mapped[str] = mapped_column(String(50))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    order_date: Mapped[date] = mapped_column(Date)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deliver_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)                 # supplier ko PO pe dikhta hai
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    gst: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_to: Mapped[str | None] = mapped_column(String(300), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # poora aaya / close hua
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="PurchaseOrderLine.id")


class PurchaseOrderLine(TenantBase):
    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(500))
    supplier_sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    quantity_received: Mapped[float] = mapped_column(Float, default=0)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 4), default=0)          # ex GST
    tax_rate: Mapped[float] = mapped_column(Float, default=0)                     # % (10 = GST)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)          # ex GST

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()


class StockReceipt(TenantBase):
    """Goods received note (GRN) - maal aaya. PO ke against ya bina PO."""
    __tablename__ = "stock_receipts"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_receipt_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    number: Mapped[str] = mapped_column(String(50))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True, index=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True, index=True)
    received_on: Mapped[date] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)     # supplier docket / invoice no
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)               # ex GST
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    supplier: Mapped[Supplier | None] = relationship()
    po: Mapped[PurchaseOrder | None] = relationship()
    lines: Mapped[list["StockReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="StockReceiptLine.id")


class StockReceiptLine(TenantBase):
    __tablename__ = "stock_receipt_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("stock_receipts.id"), index=True)
    po_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_lines.id"), nullable=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 4), default=0)

    receipt: Mapped[StockReceipt] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()
