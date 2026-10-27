"""
Standing order (recurring / schedule order) - customer har hafte same cheezein mangata hai.
Example: har Monday delivery - 10 kg lemons, 5 L milk. Order Saturday 6am apne aap ban jaata hai (Needs review).

  delivery_weekday   kis din delivery (0 = Mon ... 6 = Sun)
  generate_weekday   kis din order banana hai (delivery se pehle ya usi din)
  generate_time      us din kitne baje ("06:00", business ke time zone mein)
  every_weeks        1 = har hafte, 2 = har doosre hafte, 4 = har chauthe hafte
  start_date / end_date   kab se kab tak (end khali = hamesha)
  skip_dates         delivery dates jo skip karni hain (chhutti, "is hafte nahi chahiye")

Staff (Customer page -> Standing orders) aur customer khud (portal -> Standing orders) dono bana sakte hain.
Scheduler (app/scheduler.py -> standing_orders) har 15 min check karta hai - logic: modules/standing/service.py
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase, utcnow
from app.models.catalog import Customer, Product


class StandingOrder(TenantBase):
    __tablename__ = "standing_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_weekday: Mapped[int] = mapped_column(Integer)
    generate_weekday: Mapped[int] = mapped_column(Integer)
    generate_time: Mapped[str] = mapped_column(String(5), default="06:00")
    every_weeks: Mapped[int] = mapped_column(Integer, default=1)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    skip_dates: Mapped[list | None] = mapped_column(JSON, nullable=True)        # ["2026-12-28", ...]
    po_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)   # portal: cod | account
    created_by: Mapped[str] = mapped_column(String(10), default="staff")        # staff | customer
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # staff user id
    portal_login_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # customer login (portal se bana)
    last_generated_for: Mapped[date | None] = mapped_column(Date, nullable=True)   # aakhri delivery date jiska order bana
    last_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list["StandingOrderLine"]] = relationship(
        back_populates="standing_order", cascade="all, delete-orphan", order_by="StandingOrderLine.id")


class StandingOrderLine(TenantBase):
    __tablename__ = "standing_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    standing_order_id: Mapped[int] = mapped_column(ForeignKey("standing_orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[float] = mapped_column(Float, default=1)

    standing_order: Mapped[StandingOrder] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()
