"""
Saare models yahan import - taaki metadata aur Alembic ko sab tables dikhein.
Kahin bhi:  from app.models import Product, DraftOrder

MAIN DB   (MainBase):    Plan, Tenant, User, Invite, UsageEvent, IntegrationLink, Announcement(+Dismissal)
TENANT DB (TenantBase):  baaki sab (har client ka alag database)
"""
from app.models.platform import (Announcement, AnnouncementDismissal, IntegrationLink, Invite, Plan, Tenant,
                                 UsageEvent, User)
from app.models.settings import TenantSettings
from app.models.catalog import (Category, Customer, CustomerLogin, CustomerProductStat, PortalBanner, Product, ProductAlias,
                                ProductImage, CustomerPrice, StockMovement)
from app.models.orders import DraftOrder, DraftOrderLine, InboundMessage
from app.models.integrations import Connection, Destination, ExportJob, ImportJob, MappingTemplate
from app.models.invoices import Invoice, InvoiceLine, Payment
from app.models.logs import ActivityLog
from app.models.portal import PortalNotification, PortalNotificationRead, PortalPushSubscription
from app.models.accounting import AccountCode, TaxRate
from app.models.staff import StaffNotification, StaffNotificationRead, StaffPushSubscription
from app.models.standing import StandingOrder, StandingOrderLine
from app.models.purchasing import (PurchaseOrder, PurchaseOrderLine, StockReceipt, StockReceiptLine, Supplier,
                                   SupplierProduct)

__all__ = [
    "Plan", "Tenant", "User", "Invite", "UsageEvent", "IntegrationLink", "Announcement", "AnnouncementDismissal",
    "TenantSettings",
    "Category", "CustomerLogin", "PortalBanner", "Product", "ProductAlias", "ProductImage", "CustomerPrice", "StockMovement", "Customer", "CustomerProductStat",
    "InboundMessage", "DraftOrder", "DraftOrderLine",
    "Connection", "Destination", "ExportJob", "ImportJob", "MappingTemplate",
    "Invoice", "InvoiceLine", "Payment",
    "ActivityLog", "PortalNotification", "PortalNotificationRead", "PortalPushSubscription",
    "AccountCode", "TaxRate",
    "StaffNotification", "StaffNotificationRead", "StaffPushSubscription", "StandingOrder", "StandingOrderLine",
    "Supplier", "SupplierProduct", "PurchaseOrder", "PurchaseOrderLine", "StockReceipt", "StockReceiptLine",
]
