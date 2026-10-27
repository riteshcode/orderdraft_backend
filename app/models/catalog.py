"""
Products, aliases, customers, customer history.
source + external_id = record kahan se aaya (csv / xero) - sync isi se match karta hai.
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TenantBase, utcnow


class Category(TenantBase):
    """
    Product category - 2 level: parent (Fruit) -> sub (Citrus). Product ki ek category.
    Reports (sales by category) aur aage customer portal ka menu isi se.
    """
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(150))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    parent: Mapped["Category | None"] = relationship(remote_side="Category.id")


class Product(TenantBase):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku", name="uq_product_sku"),
                      UniqueConstraint("tenant_id", "barcode", name="uq_product_barcode"))

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    sku: Mapped[str] = mapped_column(String(100))
    # Scanner ke liye: manufacturer ka EAN/UPC, ya khali ho to hum internal EAN-13 bana dete hain (20 prefix)
    barcode: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    is_taxable: Mapped[bool] = mapped_column(Boolean, default=True)    # GST lagta hai?
    account_code: Mapped[str | None] = mapped_column(String(50), nullable=True)  # sales account
    tax_type: Mapped[str | None] = mapped_column(String(50), nullable=True)      # sales tax type/code

    # Purchase side (Xero PurchaseDetails / QBO PurchaseCost / MYOB BuyingDetails)
    purchase_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    purchase_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    purchase_account_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    purchase_tax_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Stock flags (Xero IsTrackedAsInventory / IsSold / IsPurchased)
    is_tracked_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity_on_hand: Mapped[float | None] = mapped_column(Float, nullable=True)
    reorder_level: Mapped[float | None] = mapped_column(Float, nullable=True)   # isse kam = "Low stock"
    # Weighted average cost (ex GST) - har stock receipt pe update. Stock value + margin isi se.
    avg_cost: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=True)
    is_purchased: Mapped[bool] = mapped_column(Boolean, default=False)

    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft delete: Trash mein jaata hai (is_active bhi False), Restore ho sakta hai. Permanent delete alag.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | csv | xero | quickbooks | myob
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # software ka UpdatedDateUTC
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    category: Mapped["Category | None"] = relationship()
    aliases: Mapped[list["ProductAlias"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product", cascade="all, delete-orphan",
        order_by="(ProductImage.is_primary.desc(), ProductImage.sort_order, ProductImage.id)")


class ProductImage(TenantBase):
    """
    Product photos (customer portal / catalog ke liye). Files: STORAGE_DIR/t<tenant>/products/
    Accounting software images nahi deta - ye OrderDraft mein upload hote hain.
    """
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(100))          # random (uuid) - URL guess nahi ho sakta
    thumb_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(50), default="image/jpeg")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    alt_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product] = relationship(back_populates="images")


class ProductAlias(TenantBase):
    """customer_id null = sabke liye; warna sirf us customer ke liye (learned)."""
    __tablename__ = "product_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    alias: Mapped[str] = mapped_column(String(300), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | csv | learned
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product] = relationship(back_populates="aliases")


class Customer(TenantBase):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    other_emails: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Contact person
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Phones (formatted "+64 04 1111111")
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)      # DEFAULT
    mobile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fax: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ddi: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # address = display ke liye ek line (delivery, warna postal). Structured niche.
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    postal_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    postal_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    postal_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_postcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    postal_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_attention: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)     # Xero STREET
    delivery_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_postcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivery_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_attention: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Business / tax
    abn: Mapped[str | None] = mapped_column(String(30), nullable=True)                 # Xero TaxNumber
    company_number: Mapped[str | None] = mapped_column(String(50), nullable=True)      # ACN / CompanyNumber
    bank_account_details: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sales_tax_type: Mapped[str | None] = mapped_column(String(50), nullable=True)      # AccountsReceivableTaxType
    purchase_tax_type: Mapped[str | None] = mapped_column(String(50), nullable=True)   # AccountsPayableTaxType
    default_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    is_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft delete: Trash mein jaata hai (is_active bhi False), Restore ho sakta hai. Permanent delete alag.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # ContactID / Id / UID
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CustomerLogin(TenantBase):
    """
    Customer portal login (business ka customer - Joe's Cafe ka staff). Ek customer ke kai logins ho sakte hain.
    Banate hi welcome email: temp password + "set your password" link. Pehli login pe password badalna zaroori.
    Email per client unique (portal URL mein client ka slug hota hai: /portal/<slug>).
    """
    __tablename__ = "customer_logins"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_customer_login_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    customer: Mapped["Customer"] = relationship()


class PortalBanner(TenantBase):
    """
    Customer portal home page ka banner (slider). Click -> product / category / koi bhi link.
      link_type: none | product | category | url
    """
    __tablename__ = "portal_banners"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    subtitle: Mapped[str | None] = mapped_column(String(300), nullable=True)
    button_text: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_name: Mapped[str | None] = mapped_column(String(100), nullable=True)   # STORAGE_DIR/t<id>/banners/<uuid>.jpg
    link_type: Mapped[str] = mapped_column(String(20), default="none")
    link_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    link_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CustomerProductStat(TenantBase):
    __tablename__ = "customer_product_stats"
    __table_args__ = (UniqueConstraint("customer_id", "product_id", name="uq_cust_prod"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    times_ordered: Mapped[int] = mapped_column(Integer, default=0)
    last_ordered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CustomerPrice(TenantBase):
    """
    Customer ki pantry: is customer ke liye chune hue products + special sell price.
      price khali     = sirf pantry item (normal price lagega)
      expires_on khali = hamesha;  date di = us din tak (uske baad normal price)
    """
    __tablename__ = "customer_prices"
    __table_args__ = (UniqueConstraint("customer_id", "product_id", name="uq_customer_price"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    product: Mapped[Product] = relationship()


class StockMovement(TenantBase):
    """
    Stock ka har badlav (audit log). quantity_on_hand = last movement ka qty_after.
      reason: adjust | receive | count | damaged | return | sale | opening
    """
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    change: Mapped[float] = mapped_column(Float)            # +10 aaya, -2 gaya
    qty_before: Mapped[float] = mapped_column(Float, default=0)
    qty_after: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(20), default="adjust")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # main DB user (cross-DB FK nahi)
    # Kis cheez se hua: order | receipt | stocktake ... (aage PO / GRN bhi yahi use karenge)
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    product: Mapped[Product] = relationship()
