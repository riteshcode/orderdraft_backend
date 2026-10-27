"""
OrderDraft API - entry point.
Run:   uvicorn app.main:app --reload
Docs:  http://localhost:8000/docs
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.core.audit  # noqa: F401  (automatic audit trail listeners)

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.request_log import install as install_request_log
from app.modules.accounting.router import router as accounting_router
from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.billing.router import router as billing_router
from app.modules.catalog.router import router as catalog_router
from app.modules.catalog.images import router as images_router
from app.modules.catalog.lifecycle import router as lifecycle_router
from app.modules.catalog.barcodes import router as barcodes_router
from app.modules.catalog.stock import router as stock_router
from app.modules.catalog.categories import router as categories_router
from app.modules.catalog.product_detail import router as product_detail_router
from app.modules.portal.accounts import router as portal_accounts_router
from app.modules.portal.auth import router as portal_auth_router
from app.modules.portal.admin import router as portal_admin_router
from app.modules.portal.shop import router as portal_shop_router
from app.modules.portal.notify import router as portal_notify_router
from app.modules.portal.pwa import router as portal_pwa_router
from app.modules.notifications.staff import router as staff_notify_router
from app.modules.standing.router import router as standing_router
from app.modules.announcements.router import router as announcements_router
from app.modules.reports.sales import router as sales_reports_router
from app.modules.purchasing.suppliers import router as suppliers_router
from app.modules.purchasing.purchase_orders import router as purchase_orders_router
from app.modules.catalog.customer_detail import router as customer_detail_router
from app.modules.email.router import router as email_router
from app.modules.destinations.router import router as destinations_router
from app.modules.imports.router import router as imports_router
from app.modules.intake.router import router as intake_router
from app.modules.invoices.router import router as invoices_router
from app.modules.orders.router import router as orders_router
from app.modules.reports.router import router as reports_router
from app.modules.reports.activity import router as activity_router
from app.modules.settings.router import router as settings_router
from app.modules.team.router import router as team_router
from app.modules.connectors.router import router as connectors_router
from app.seed import seed_plans

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run_migrations():
    """Main DB + har client ka DB latest schema pe (Windows/local pe alag command yaad nahi rakhni)."""
    from app.migrations import upgrade_all_tenants, upgrade_main

    upgrade_main()
    result = upgrade_all_tenants()
    if result["failed"]:
        logging.getLogger("migrations").error("Tenant migrations failed: %s", result["failed"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.STORAGE_DIR, exist_ok=True)
    if settings.AUTO_MIGRATE:
        run_migrations()
    db = SessionLocal()
    try:
        seed_plans(db)
    finally:
        db.close()
    if settings.RUN_SCHEDULER_IN_APP:
        from app.scheduler import start_in_background
        start_in_background()
    yield


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)

# Har request ki line logs/app.log mein + error pe reference id (core/request_log.py).
# CORS iske baad add hota hai = bahar wala layer, taaki 500 response pe bhi CORS headers lagein.
install_request_log(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

for r in (auth_router, barcodes_router, categories_router, catalog_router, product_detail_router, images_router, lifecycle_router, stock_router, suppliers_router, purchase_orders_router, customer_detail_router, email_router, imports_router, orders_router, intake_router, destinations_router,
          invoices_router, accounting_router, connectors_router, reports_router, activity_router, sales_reports_router, portal_accounts_router, portal_auth_router, portal_admin_router,
          portal_shop_router, portal_notify_router, portal_pwa_router, staff_notify_router, standing_router,
          announcements_router, settings_router, team_router, billing_router, admin_router):
    app.include_router(r)


@app.get("/health")
def health():
    return {"ok": True}
