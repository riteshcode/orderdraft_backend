"""
Staff (client ke apne log - owner/admin/reviewer) ki notifications:
  naya portal order, standing order se order bana, naya email/voice order ...
App ke top bar mein bell + (agar on kiya) browser/desktop push. Email pehle jaisa ("Email new orders to").
Users main DB mein hain - isliye user_id pe FK nahi.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase, utcnow


class StaffNotification(TenantBase):
    __tablename__ = "staff_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(30))                  # portal_order | standing_order | new_order | standing_skipped
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)   # "/orders/12"
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    push_sent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StaffNotificationRead(TenantBase):
    __tablename__ = "staff_notification_reads"
    __table_args__ = (UniqueConstraint("notification_id", "user_id", name="uq_staff_notification_read"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("staff_notifications.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StaffPushSubscription(TenantBase):
    """Staff ka browser jahan "Desktop notifications" on kiya."""
    __tablename__ = "staff_push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    endpoint: Mapped[str] = mapped_column(String(700))
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
