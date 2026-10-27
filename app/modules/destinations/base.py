"""
Destination ka common contract. Har destination file mein ye cheezein hoti hain:

    TYPE          = "webhook"
    LABEL         = "Webhook"
    SECRET_FIELDS = ["secret"]                 # ye config values encrypted rehti hain
    def validate_config(config: dict) -> dict  # galat ho to ValueError
    def send(db, order, config, tenant) -> ExportResult

Naya destination (MYOB, Unleashed...) = nayi file + registry.py mein ek line.
"""
from dataclasses import dataclass

from app.core.helpers import money, to_float
from app.models import DraftOrder


@dataclass
class ExportResult:
    ok: bool
    message: str
    external_id: str | None = None
    external_url: str | None = None
    retryable: bool = False   # True = network/5xx - baad mein dobara try karo


def order_payload(order: DraftOrder) -> dict:
    """Order ka standard JSON - webhook, custom API, CSV sab yahi use karte hain."""
    c = order.customer
    lines = []
    for l in order.lines:
        price = l.unit_price if l.unit_price is not None else (l.product.price if l.product else None)
        lines.append({
            "sku": l.product.sku if l.product else None,
            "product_name": l.product.name if l.product else None,
            "quantity": l.quantity,
            "unit": l.unit or (l.product.unit if l.product else None),
            "unit_price": to_float(price),
            "line_total": to_float(money(price) * money(l.quantity)) if price is not None else None,
            "taxable": l.product.is_taxable if l.product else True,
            "original_text": l.raw_text,
        })
    return {
        "order_id": order.id,
        "status": order.status,
        "customer": {
            "id": c.id if c else None,
            "code": c.code if c else None,
            "name": c.name if c else order.customer_raw,
            "email": c.email if c else None,
            "external_id": c.external_id if c else None,
        },
        "po_number": order.po_number,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "notes": order.notes,
        "total": to_float(order.total_amount),
        "lines": lines,
        "source_channel": order.message.channel if order.message else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
    }


def get_path(data, path: str):
    """get_path({"a": {"b": 1}}, "a.b") -> 1"""
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def set_path(data: dict, path: str, value):
    """set_path({}, "order.ref", 5) -> {"order": {"ref": 5}}"""
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value
