"""
InboundMessage (jo aaya) -> DraftOrder -> DraftOrderLine
"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase, utcnow
from app.models.catalog import Customer, Product


class InboundMessage(TenantBase):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(20))  # email | voice | pdf | manual
    sender_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    combined_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # received | processing | done | not_order | failed | blocked (billing)
    status: Mapped[str] = mapped_column(String(20), default="received", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DraftOrder(TenantBase):
    __tablename__ = "draft_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    customer_raw: Mapped[str | None] = mapped_column(String(300), nullable=True)
    customer_confidence: Mapped[float] = mapped_column(Float, default=0)
    po_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # needs_review | approved | exported | rejected | cancelled (approve ke baad cancel)
    status: Mapped[str] = mapped_column(String(20), default="needs_review", index=True)
    overall_confidence: Mapped[float] = mapped_column(Float, default=0)
    total_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)  # ex GST, approve par
    approved_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # main DB users.id (FK nahi - alag DB)
    source: Mapped[str] = mapped_column(String(20), default="ai")   # ai | manual | portal | standing
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)   # portal: cod | account
    portal_login_id: Mapped[int | None] = mapped_column(Integer, nullable=True)     # kis portal login ne order kiya
    standing_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)   # standing order se bana
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # main DB users.id (FK nahi - alag DB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Approve hone par customer ko order confirmation email
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmation_to: Mapped[str | None] = mapped_column(String(300), nullable=True)

    customer: Mapped[Customer | None] = relationship()
    message: Mapped[InboundMessage | None] = relationship()
    lines: Mapped[list["DraftOrderLine"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="DraftOrderLine.id"
    )


class DraftOrderLine(TenantBase):
    __tablename__ = "draft_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("draft_orders.id"), index=True)
    raw_text: Mapped[str] = mapped_column(String(500))
    product_hint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    suggested_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)  # approve par snapshot
    match_confidence: Mapped[float] = mapped_column(Float, default=0)
    match_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)

    order: Mapped[DraftOrder] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()
