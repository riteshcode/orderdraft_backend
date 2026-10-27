"""
Automatic audit trail - kisne, kab, kya badla (purana -> naya value).

Har save pe (SQLAlchemy flush) in tables ka create / update / delete ActivityLog mein apne aap likha jaata hai:
  products, customers, suppliers, supplier products, customer prices (pantry), settings,
  destinations, account codes, tax rates

    product.updated   detail = {"name": "Roma 10kg", "changes": {"price": [32.5, 34.0], "unit": ["box", "tray"]}}

Kaun: session.info["actor_id"] (current_user set karta hai; background job = None = "System").
Bulk (CSV import / accounting sync) mein har row log nahi hoti - wahan ek summary log hai:
    with no_audit(db): import_products(...)

Super admin / support isi log se dekh sakta hai ki kuch "ulta seedha" kab aur kisne kiya.
"""
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

# Ye columns log nahi hote (har baar badalte hain / stock movements alag log hote hain / secret)
IGNORE = {"updated_at", "created_at", "external_updated_at", "quantity_on_hand", "avg_cost", "last_cost",
          "last_received_at", "access_token", "refresh_token", "password_hash", "secret", "api_key",
          "sender_checked_at", "sender_dns", "sender_provider_id", "invoice_next_number", "po_next_number",
          "receipt_next_number", "deleted_at", "last_login_at", "welcome_sent_at"}


def _audited() -> dict:
    from app.models import (AccountCode, Category, Customer, CustomerLogin, CustomerPrice, Destination, Product,
                            Supplier, SupplierProduct, TaxRate, TenantSettings)
    return {Category: "category", CustomerLogin: "portal_login", Product: "product", Customer: "customer", Supplier: "supplier", SupplierProduct: "supplier_product",
            CustomerPrice: "customer_price", TenantSettings: "settings", Destination: "destination",
            AccountCode: "account_code", TaxRate: "tax_rate"}


_MAP: dict | None = None


def entity_of(obj) -> str | None:
    global _MAP
    if _MAP is None:
        _MAP = _audited()
    return _MAP.get(type(obj))


def _json(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    if isinstance(v, (dict, list)):
        return "(changed)"
    return v


def _label(obj, session: Session | None = None) -> str | None:
    for attr in ("name", "business_name", "code", "sku", "email"):
        v = getattr(obj, attr, None)
        if v:
            return str(v)[:120]
    # Pantry price / supplier product: product ka naam (naye row pe relationship abhi load nahi hota)
    product_id = getattr(obj, "product_id", None)
    if product_id and session is not None:
        from app.models import Product
        with session.no_autoflush:
            product = session.get(Product, product_id)
        return product.name if product else None
    return None


def _changes(obj) -> dict:
    out = {}
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in IGNORE:
            continue
        hist = state.attrs[key].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else None
        if _json(old) == _json(new):
            continue
        out[key] = [_json(old), _json(new)]
    return out


@contextmanager
def no_audit(db: Session):
    """Bulk kaam (import / sync) - per-row audit band, summary log caller likhta hai."""
    db.info["audit_skip"] = db.info.get("audit_skip", 0) + 1
    try:
        yield
    finally:
        db.info["audit_skip"] -= 1


@event.listens_for(Session, "before_flush")
def _collect(session: Session, flush_context, instances):
    if session.info.get("audit_skip"):
        return
    pending = session.info.setdefault("audit_pending", [])
    # Isi transaction mein bana record (create ke baad barcode set hona etc.) -> alag "updated" nahi, "created" kaafi
    created_now = session.info.setdefault("audit_created", set())
    for obj in session.new:
        if (entity := entity_of(obj)) and getattr(obj, "tenant_id", None):
            pending.append((obj, entity, "created", {"name": _label(obj, session)}))
            created_now.add(id(obj))
    for obj in session.dirty:
        if id(obj) in created_now:
            continue
        if (entity := entity_of(obj)) and getattr(obj, "tenant_id", None) and session.is_modified(obj):
            changes = _changes(obj)
            if changes:
                pending.append((obj, entity, "updated", {"name": _label(obj, session), "changes": changes}))
    for obj in session.deleted:
        if (entity := entity_of(obj)) and getattr(obj, "tenant_id", None):
            pending.append((obj, entity, "deleted", {"name": _label(obj, session), "id": obj.id}))


# Child rows parent ki history mein dikhte hain: pantry price -> customer, supplier product -> supplier
PARENT = {"customer_price": ("customer", "customer_id", {"created": "price_added", "updated": "price_changed",
                                                         "deleted": "price_removed"}),
          "supplier_product": ("supplier", "supplier_id", {"created": "product_linked", "updated": "product_updated",
                                                           "deleted": "product_unlinked"}),
          "portal_login": ("customer", "customer_id", {"created": "portal_access_added", "updated": "portal_access_changed",
                                                       "deleted": "portal_access_removed"})}


@event.listens_for(Session, "after_flush_postexec")
def _write(session: Session, flush_context):
    pending = session.info.pop("audit_pending", None)
    if not pending:
        return
    from app.models import ActivityLog
    actor = session.info.get("actor_id")
    for obj, entity, verb, detail in pending:
        if verb == "deleted":
            entity_id = detail.get("id")
        else:
            entity_id = inspect(obj).identity[0] if inspect(obj).identity else getattr(obj, "id", None)
        if session.info.get("impersonator_id"):
            detail["impersonated_by"] = session.info["impersonator_id"]
        if session.info.get("portal_actor"):     # customer ne portal se badla (profile / address)
            detail["portal_user"] = session.info["portal_actor"]
        action = f"{entity}.{verb}"
        if entity in PARENT:
            parent, fk, verbs = PARENT[entity]
            detail["product"] = detail.pop("name", None)
            entity, entity_id, action = parent, getattr(obj, fk, None), f"{parent}.{verbs[verb]}"
        session.add(ActivityLog(tenant_id=obj.tenant_id, user_id=actor, action=action,
                                entity=entity, entity_id=entity_id, detail=detail))


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _reset(session: Session):
    session.info.pop("audit_created", None)
    session.info.pop("audit_pending", None)
