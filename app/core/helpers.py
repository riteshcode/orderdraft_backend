"""
Chhote common helpers.
"""
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.models import ActivityLog


def money(value) -> Decimal:
    """Kisi bhi number ko 2 decimal Decimal mein: money(10.005) -> Decimal('10.01')"""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_float(value) -> float | None:
    """Decimal -> float (JSON / API ke liye)."""
    return None if value is None else float(value)


def log_activity(db: Session, tenant_id: int, user_id: int | None, action: str,
                 entity: str | None = None, entity_id: int | None = None, detail: dict | None = None):
    """Activity log mein entry. Commit caller karega."""
    db.add(ActivityLog(tenant_id=tenant_id, user_id=user_id, action=action,
                       entity=entity, entity_id=entity_id, detail=detail))


def get_tenant_settings(db: Session, tenant_id: int, for_update: bool = False):
    """
    Client ki configuration (client ke DB mein ek row). Na ho to bana do.
    for_update=True -> row lock (invoice number lene ke liye, MySQL).
    """
    from sqlalchemy import select

    from app.models import Tenant, TenantSettings

    stmt = select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    cfg = db.scalars(stmt).first()
    if cfg is None:
        tenant = db.get(Tenant, tenant_id)
        cfg = TenantSettings(tenant_id=tenant_id, business_name=tenant.name if tenant else "",
                             email=tenant.email if tenant else None)
        db.add(cfg)
        db.flush()
    return cfg
