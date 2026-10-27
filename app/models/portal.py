"""
Customer portal: app notifications (admin -> customers) aur Web Push subscriptions.
Banners: catalog.PortalBanner.  Logins: catalog.CustomerLogin.  Settings/branding: TenantSettings.portal_*
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase, utcnow


class PortalPushSubscription(TenantBase):
    """Ek browser / phone jahan customer ne "Turn on notifications" kiya."""
    __tablename__ = "portal_push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    login_id: Mapped[int] = mapped_column(ForeignKey("customer_logins.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(700))
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PortalNotification(TenantBase):
    """
    Admin ka message customers ko (Settings -> Customer portal -> Notify customers).
      audience: all | customers (customer_ids)
    Portal mein bell icon + (agar on kiya) phone push + (optional) email.
    """
    __tablename__ = "portal_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audience: Mapped[str] = mapped_column(String(20), default="all")
    kind: Mapped[str] = mapped_column(String(20), default="manual")    # manual (admin ne bheja) | order (order updates)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)     # kind=order: kis order ka
    event: Mapped[str | None] = mapped_column(String(20), nullable=True)   # confirmed|rejected|updated|cancelled|invoiced|placed
    customer_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    send_push: Mapped[bool] = mapped_column(Boolean, default=True)
    send_email: Mapped[bool] = mapped_column(Boolean, default=False)
    push_sent: Mapped[int] = mapped_column(Integer, default=0)
    push_failed: Mapped[int] = mapped_column(Integer, default=0)
    email_sent: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PortalNotificationRead(TenantBase):
    __tablename__ = "portal_notification_reads"
    __table_args__ = (UniqueConstraint("notification_id", "login_id", name="uq_portal_notification_read"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("portal_notifications.id", ondelete="CASCADE"), index=True)
    login_id: Mapped[int] = mapped_column(ForeignKey("customer_logins.id", ondelete="CASCADE"), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
