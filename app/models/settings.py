"""
Client ki apni configuration (client ke DB mein): business details + invoice settings.
Har tenant DB mein bas ek row.  Use:  cfg = get_tenant_settings(db, tenant_id)
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase, utcnow


class TenantSettings(TenantBase):
    __tablename__ = "tenant_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)

    # Business details (invoice par print hote hain)
    business_name: Mapped[str] = mapped_column(String(200), default="")
    abn: Mapped[str | None] = mapped_column(String(30), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Australia/Sydney")
    currency: Mapped[str] = mapped_column(String(3), default="AUD")

    # Invoice settings
    invoice_prefix: Mapped[str] = mapped_column(String(20), default="INV-")
    invoice_next_number: Mapped[int] = mapped_column(Integer, default=1001)
    gst_rate: Mapped[float] = mapped_column(Float, default=10.0)
    prices_include_gst: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)
    bank_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_footer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Email sender (client ka apna domain verify ho to uske naam se, warna platform ke) ----
    sender_name: Mapped[str | None] = mapped_column(String(100), nullable=True)      # "Fresh Farms Orders"
    sender_email: Mapped[str | None] = mapped_column(String(200), nullable=True)     # orders@freshfarms.com.au
    sender_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # none | pending | verified | failed
    sender_status: Mapped[str] = mapped_column(String(20), default="none")
    sender_provider_id: Mapped[str | None] = mapped_column(String(50), nullable=True)   # Postmark domain ID
    sender_dns: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # DNS records jo client ko add karne hain
    sender_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- Order confirmation email (approve hone par customer ko) ----
    order_email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    order_email_subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    order_email_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_email_cc_other: Mapped[bool] = mapped_column(Boolean, default=False)   # customer ke other_emails ko CC
    order_email_bcc: Mapped[str | None] = mapped_column(String(200), nullable=True)  # client ki copy

    # ---- Stock ----
    # Order approve hone par tracked products ka on hand kam karo (Settings -> Stock)
    stock_deduct_on_approve: Mapped[bool] = mapped_column(Boolean, default=True)
    # Purchase order / goods received numbering
    po_prefix: Mapped[str] = mapped_column(String(20), default="PO-")
    po_next_number: Mapped[int] = mapped_column(Integer, default=1001)
    receipt_next_number: Mapped[int] = mapped_column(Integer, default=1001)   # GRN-1001

    # ---- Customer portal (Settings -> Customer portal) ----
    portal_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    portal_access: Mapped[str] = mapped_column(String(10), default="private")      # private (login zaroori) | public
    portal_show_prices_public: Mapped[bool] = mapped_column(Boolean, default=True)  # public mein bina login price dikhe?
    portal_allow_cod: Mapped[bool] = mapped_column(Boolean, default=True)           # Cash on delivery
    portal_allow_account: Mapped[bool] = mapped_column(Boolean, default=False)      # On account (invoice terms)
    portal_min_order: Mapped[float | None] = mapped_column(Float, nullable=True)     # minimum order value
    portal_hide_out_of_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    portal_welcome_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    portal_welcome_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    portal_checkout_note: Mapped[str | None] = mapped_column(Text, nullable=True)    # "Order by 4pm for next-day delivery"
    portal_notify_email: Mapped[str | None] = mapped_column(String(200), nullable=True)  # naya order aaya - staff ko
    # Branding (portal ka look - admin khud badle)
    portal_logo: Mapped[str | None] = mapped_column(String(100), nullable=True)       # file name (media)
    portal_icon: Mapped[str | None] = mapped_column(String(100), nullable=True)       # app icon (square) - PWA
    portal_brand_color: Mapped[str | None] = mapped_column(String(7), nullable=True)  # "#16a34a"
    portal_header_style: Mapped[str] = mapped_column(String(10), default="light")    # light | brand | dark
    portal_font: Mapped[str] = mapped_column(String(10), default="modern")           # modern | classic | rounded
    portal_corners: Mapped[str] = mapped_column(String(10), default="rounded")       # rounded | soft | sharp
    portal_tagline: Mapped[str | None] = mapped_column(String(200), nullable=True)
    portal_announcement: Mapped[str | None] = mapped_column(String(200), nullable=True)  # upar patti "Free delivery over $200"
    portal_footer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Delivery days / cut-off (portal checkout pe delivery date inhi se)
    portal_delivery_days: Mapped[str | None] = mapped_column(String(20), nullable=True)   # "0,1,2,3,4" (0=Mon) - khali = koi bhi din
    portal_cutoff_time: Mapped[str | None] = mapped_column(String(5), nullable=True)      # "16:00" - iske baad order = ek din aur
    portal_lead_days: Mapped[int] = mapped_column(Integer, default=1)                      # order se kam se kam kitne din baad delivery
    portal_closed_dates: Mapped[list | None] = mapped_column(JSON, nullable=True)         # ["2026-12-25", ...] chhutti
    portal_allow_standing: Mapped[bool] = mapped_column(Boolean, default=True)           # customer khud standing order bana sake

    onboarding_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Purane code / templates mein `tenant.name` likha hai
    @property
    def name(self) -> str:
        return self.business_name
