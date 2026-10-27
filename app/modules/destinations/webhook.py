"""
Webhook destination - hamara standard JSON POST karta hai.
Header X-OrderDraft-Signature = HMAC-SHA256(body, secret) hex. Client verify kar sakta hai.

config: {"url": "https://...", "secret": "..."}
"""
import hashlib
import hmac
import json
import secrets

import httpx

from app.modules.destinations.base import ExportResult, order_payload

TYPE = "webhook"
LABEL = "Webhook"
SECRET_FIELDS = ["secret"]


def validate_config(config: dict) -> dict:
    url = (config.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    return {"url": url, "secret": config.get("secret") or secrets.token_hex(16)}


def post_signed(url: str, secret: str | None, payload: dict, idempotency_key: str | None = None) -> ExportResult:
    body = json.dumps(payload, default=str)
    headers = {"Content-Type": "application/json", "User-Agent": "OrderDraft-Webhook/1.0"}
    if idempotency_key:      # retry pe same - receiver duplicate order skip kar sakta hai
        headers["Idempotency-Key"] = idempotency_key
    if secret:
        headers["X-OrderDraft-Signature"] = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=20)
    except httpx.HTTPError as e:
        return ExportResult(False, f"Request failed: {e}", retryable=True)
    if 200 <= resp.status_code < 300:
        return ExportResult(True, f"HTTP {resp.status_code}")
    return ExportResult(False, f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=resp.status_code >= 500)


def send(db, order, config: dict, tenant) -> ExportResult:
    return post_signed(config["url"], config.get("secret"), {"event": "order.approved", "data": order_payload(order)},
                       config.get("_idempotency_key"))
