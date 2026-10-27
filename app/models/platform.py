"""
MAIN DB models - platform level (sab clients ke liye ek hi database).

    Plan             subscription plans
    Tenant           client registry: naam, slug (inbound email), status, billing, uska DB kahan hai
    User             login (email unique poore platform pe) - har user ek tenant ka
    Invite           team invite tokens
    UsageEvent       AI usage / cost (billing + super admin)
    IntegrationLink  Xero org id / QuickBooks realm id -> tenant (webhooks ko sahi client tak pahunchana)

Client ka business data (products, orders, invoices, tokens, settings) yahan NAHI hai -
wo client ke apne DB mein hai (app/models/catalog.py, orders.py, ...).
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import MainBase, utcnow


class Plan(MainBase):
    """Subscription plan. Stripe mein price banao aur uska id yahan daalo."""
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)      # starter | growth | pro
    name: Mapped[str] = mapped_column(String(100))
    price_aud_month: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    stripe_price_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    order_limit: Mapped[int] = mapped_column(Integer, default=200)    # orders / month
    user_limit: Mapped[int] = mapped_column(Integer, default=3)
    features: Mapped[list | None] = mapped_column(JSON, nullable=True)  # marketing bullets
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Tenant(MainBase):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # inbound email
    status: Mapped[str] = mapped_column(String(20), default="active")        # active | suspended
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)    # owner / billing contact

    # Client ka database
    db_name: Mapped[str | None] = mapped_column(String(64), nullable=True)   # od_t_12
    db_url: Mapped[str | None] = mapped_column(Text, nullable=True)          # dedicated server (encrypted), optional
    db_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    # Konsa accounting software connected hai (sirf ek) - header / admin ke liye copy
    accounting_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Billing
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    # trialing | active | past_due | canceled | expired
    subscription_status: Mapped[str] = mapped_column(String(20), default="trialing")
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    plan: Mapped[Plan | None] = relationship()


class User(MainBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner | admin | reviewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tenant: Mapped[Tenant] = relationship()


class Invite(MainBase):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="reviewer")
    token: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    invited_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tenant: Mapped[Tenant] = relationship()


class UsageEvent(MainBase):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))   # claude_extract | claude_pick | deepgram | order
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class IntegrationLink(MainBase):
    """
    External org -> tenant. Webhook (Xero / QuickBooks) mein sirf unka org id aata hai,
    isse pata chalta hai kis client ke DB mein jaana hai.
    """
    __tablename__ = "integration_links"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_integration_link"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))                    # xero | quickbooks | myob
    external_id: Mapped[str] = mapped_column(String(200))               # Xero tenantId / QBO realmId / MYOB cf id
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Announcement(MainBase):
    """
    Super admin ka notice sab clients ko (maintenance, new feature, issue). Main DB mein - sab clients ke liye ek.
      audience: all | tenants (tenant_ids) | plans (plan_ids);  roles: None = sab, ya ["owner","admin"]
      show_in_app = app ke andar banner/popup;  show_on_login = login page pe bhi (bina login)
      display: banner (upar patti) | popup (beech mein dialog - zaroori baat)
    """
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="info")        # info | success | warning | critical
    display: Mapped[str] = mapped_column(String(20), default="banner")    # banner | popup
    audience: Mapped[str] = mapped_column(String(20), default="all")      # all | tenants | plans
    tenant_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    plan_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)
    show_in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_login: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissible: Mapped[bool] = mapped_column(Boolean, default=True)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    link_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AnnouncementDismissal(MainBase):
    """User ne notice band kiya - dobara nahi dikhega (har user ka apna)."""
    __tablename__ = "announcement_dismissals"
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", name="uq_announcement_dismissal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
