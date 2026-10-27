"""
Default plans - pehli baar app start hone par ban jate hain (DB khali ho to).
Prices/limits Super Admin -> Plans se badlo, aur wahan Stripe price id daalo.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Plan

DEFAULT_PLANS = [
    {"code": "starter", "name": "Starter", "price_aud_month": 149, "order_limit": 300, "user_limit": 2, "sort_order": 1,
     "features": ["300 orders / month", "Email, voice & PDF orders", "Xero, QuickBooks, MYOB or CSV export", "2 users"]},
    {"code": "growth", "name": "Growth", "price_aud_month": 349, "order_limit": 1000, "user_limit": 5, "sort_order": 2,
     "features": ["1,000 orders / month", "Everything in Starter", "Custom API & webhooks", "OrderDraft invoices", "5 users"]},
    {"code": "pro", "name": "Pro", "price_aud_month": 799, "order_limit": 3000, "user_limit": 15, "sort_order": 3,
     "features": ["3,000 orders / month", "Everything in Growth", "Priority support", "15 users"]},
]


def seed_plans(db: Session):
    if db.scalar(select(func.count()).select_from(Plan)):
        return
    for p in DEFAULT_PLANS:
        db.add(Plan(**p))
    db.commit()
