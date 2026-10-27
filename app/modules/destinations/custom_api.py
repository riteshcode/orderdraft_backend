"""
Custom API destination - client ke apne system ka REST API.

config example:
{
  "url": "https://erp.client.com/api/orders",
  "method": "POST",
  "auth_type": "bearer",          # none | bearer | api_key | basic
  "auth_value": "token123",       # basic ke liye "user:pass"  (encrypted)
  "api_key_header": "X-API-Key",  # auth_type=api_key ho tab
  "headers": {"X-Source": "orderdraft"},
  "body_mode": "mapped",          # standard = hamara JSON as-is | mapped = field_map se
  "field_map": {                  # unka field  <-  hamara field (dot path)
    "customer_code": "customer.code",
    "reference": "po_number",
    "deliver_on": "delivery_date"
  },
  "lines_key": "items",
  "line_field_map": {"item_code": "sku", "qty": "quantity", "price": "unit_price"},
  "response_id_path": "data.order_number"   # unke response mein order id kahan hai
}
"""
import base64

import httpx

from app.modules.destinations.base import ExportResult, get_path, order_payload, set_path

TYPE = "custom_api"
LABEL = "Custom API"
SECRET_FIELDS = ["auth_value"]


def validate_config(config: dict) -> dict:
    url = (config.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("API URL must start with http:// or https://")
    method = (config.get("method") or "POST").upper()
    if method not in ("POST", "PUT", "PATCH"):
        raise ValueError("Method must be POST, PUT or PATCH")
    auth_type = config.get("auth_type") or "none"
    if auth_type not in ("none", "bearer", "api_key", "basic"):
        raise ValueError("Choose a valid authentication type")
    if auth_type != "none" and not config.get("auth_value"):
        raise ValueError("Enter an auth value (token / key / user:pass)")
    body_mode = config.get("body_mode") or "standard"
    if body_mode not in ("standard", "mapped"):
        raise ValueError("Body mode must be standard or mapped")
    headers = config.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("Headers must be a JSON object")
    return {
        "url": url, "method": method, "auth_type": auth_type,
        "auth_value": config.get("auth_value") or "",
        "api_key_header": config.get("api_key_header") or "X-API-Key",
        "headers": {str(k): str(v) for k, v in headers.items()},
        "body_mode": body_mode,
        "field_map": config.get("field_map") or {},
        "lines_key": config.get("lines_key") or "lines",
        "line_field_map": config.get("line_field_map") or {},
        "response_id_path": config.get("response_id_path") or "",
    }


def build_body(payload: dict, config: dict) -> dict:
    if config.get("body_mode") != "mapped":
        return payload
    body: dict = {}
    for their_field, our_path in (config.get("field_map") or {}).items():
        set_path(body, their_field, get_path(payload, our_path))
    line_map = config.get("line_field_map") or {}
    if line_map:
        lines = []
        for line in payload["lines"]:
            item: dict = {}
            for their_field, our_path in line_map.items():
                set_path(item, their_field, get_path(line, our_path))
            lines.append(item)
        set_path(body, config.get("lines_key") or "lines", lines)
    return body


def build_headers(config: dict) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json", **(config.get("headers") or {})}
    if config.get("_idempotency_key"):      # retry pe same - receiver duplicate order skip kar sakta hai
        headers["Idempotency-Key"] = config["_idempotency_key"]
    auth_type, value = config.get("auth_type"), config.get("auth_value") or ""
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    elif auth_type == "api_key":
        headers[config.get("api_key_header") or "X-API-Key"] = value
    elif auth_type == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(value.encode()).decode()
    return headers


def send(db, order, config: dict, tenant) -> ExportResult:
    body = build_body(order_payload(order), config)
    try:
        resp = httpx.request(config["method"], config["url"], json=body, headers=build_headers(config), timeout=20)
    except httpx.HTTPError as e:
        return ExportResult(False, f"Request failed: {e}", retryable=True)

    if not (200 <= resp.status_code < 300):
        return ExportResult(False, f"HTTP {resp.status_code}: {resp.text[:300]}", retryable=resp.status_code >= 500)

    external_id = None
    if config.get("response_id_path"):
        try:
            value = get_path(resp.json(), config["response_id_path"])
            external_id = str(value) if value is not None else None
        except ValueError:
            pass
    return ExportResult(True, f"HTTP {resp.status_code}" + (f" - id {external_id}" if external_id else ""),
                        external_id=external_id)
