"""
Claude se order extract karna (structured JSON).

Hum "tool use" use karte hain with forced tool_choice - isse Claude hamesha
hamare schema wala JSON hi deta hai, free text nahi.

Do functions:
  extract_order()    -> message text/PDF/image se order details nikalo
  pick_products()    -> jahan fuzzy matching confuse hai, Claude se candidates mein se choose karwao
"""
import base64
import json
from datetime import date

import anthropic

from app.core.config import settings

ORDER_TOOL = {
    "name": "save_order",
    "description": "Save the purchase order extracted from the customer message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_order": {
                "type": "boolean",
                "description": "False if the message is not a product order (e.g. complaint, invoice query, spam, newsletter).",
            },
            "customer_name": {"type": ["string", "null"], "description": "Business/person placing the order (original sender if forwarded)."},
            "customer_email": {"type": ["string", "null"], "description": "Email of the person placing the order (original sender if forwarded)."},
            "customer_phone": {"type": ["string", "null"]},
            "po_number": {"type": ["string", "null"], "description": "Customer PO / reference number if mentioned."},
            "delivery_date": {"type": ["string", "null"], "description": "Requested delivery date as YYYY-MM-DD. Resolve words like 'tomorrow' or 'Friday' using today's date."},
            "notes": {"type": ["string", "null"], "description": "Delivery instructions or special requests, short."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "raw_text": {"type": "string", "description": "The item line exactly as written/spoken."},
                        "product_hint": {"type": "string", "description": "Cleaned product name only, no quantity. e.g. 'roma tomatoes 10kg'."},
                        "quantity": {"type": "number"},
                        "unit": {"type": ["string", "null"], "description": "box, kg, carton, each, bunch, tray, etc."},
                    },
                    "required": ["raw_text", "product_hint", "quantity"],
                },
            },
        },
        "required": ["is_order", "items"],
    },
}

SYSTEM_PROMPT = """You extract wholesale purchase orders for an Australian distributor.
Messages come from email, forwarded email, PDF purchase orders, chat messages, or voice-note transcripts.

Rules:
- If the email was FORWARDED by the distributor's staff, the customer is the ORIGINAL sender in the forwarded part, not the forwarder.
- Dates are Australian format (DD/MM/YYYY). Today's date is given; resolve relative dates ('tomorrow', 'Monday').
- Extract every line item. Do not invent items. Do not merge different items.
- If quantity is missing, use 1. Words like 'a couple' = 2, 'half a dozen' = 6, 'a dozen' = 12.
- Voice transcripts can have mistakes; use common sense for product names (e.g. 'roamer tomatoes' -> 'roma tomatoes').
- Ignore email signatures, disclaimers, and previous quoted replies that are not the current order.
- If the customer says 'same as last week' or 'usual order', set is_order true, items empty, and put that in notes.
- Always call the save_order tool."""


class ExtractionError(Exception):
    pass


def _report_usage(on_usage, kind: str, resp):
    if on_usage and getattr(resp, "usage", None):
        on_usage(kind, resp.usage.input_tokens or 0, resp.usage.output_tokens or 0)


def get_client() -> anthropic.Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise ExtractionError("Order reading isn't set up yet (ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def extract_order(
    text: str,
    pdf_files: list[bytes] | None = None,
    image_files: list[tuple[bytes, str]] | None = None,
    known_customers: list[str] | None = None,
    today: date | None = None,
    on_usage=None,
) -> dict:
    """
    text         : email body + PDF text + voice transcripts (sab mila ke)
    pdf_files    : scanned PDFs jo Claude ko seedha dikhane hain
    image_files  : [(bytes, media_type)] - photo of handwritten order etc.
    on_usage     : on_usage(kind, input_tokens, output_tokens) - cost tracking
    Returns dict jaisa ORDER_TOOL schema mein hai.
    """
    today = today or date.today()
    content = []

    for pdf in pdf_files or []:
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()},
        })
    for img, media_type in image_files or []:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(img).decode()},
        })

    hint = ""
    if known_customers:
        # Thode customer names dene se Claude sahi spelling pakadta hai
        hint = "\nSome known customer names: " + ", ".join(known_customers[:100])

    content.append({
        "type": "text",
        "text": f"Today's date: {today.isoformat()} ({today.strftime('%A')}){hint}\n\n<message>\n{text}\n</message>",
    })

    client = get_client()
    try:
        resp = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=[ORDER_TOOL],
            tool_choice={"type": "tool", "name": "save_order"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        raise ExtractionError(f"Claude API error: {e}")
    _report_usage(on_usage, "claude_extract", resp)

    for block in resp.content:
        if block.type == "tool_use" and block.name == "save_order":
            return block.input
    raise ExtractionError("The AI didn't return any order details - please try again")


# --------------------------------------------------------------------------
# Low-confidence lines ke liye: Claude candidates mein se sahi product chune
# --------------------------------------------------------------------------
PICK_TOOL = {
    "name": "pick_products",
    "description": "For each order line, choose the matching product_id from its candidates, or null if none match.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_index": {"type": "integer"},
                        "product_id": {"type": ["integer", "null"]},
                        "confidence": {"type": "number", "description": "0-100"},
                    },
                    "required": ["line_index", "product_id", "confidence"],
                },
            }
        },
        "required": ["picks"],
    },
}


def pick_products(lines: list[dict], on_usage=None) -> dict[int, tuple[int | None, float]]:
    """
    lines = [{"index": 0, "text": "2 box roma tom", "unit": "box",
              "candidates": [{"product_id": 5, "sku": "...", "name": "...", "unit": "..."}]}]
    Returns {line_index: (product_id, confidence)}
    """
    if not lines:
        return {}

    prompt = (
        "Match each customer order line to the correct product from its candidate list. "
        "Consider product name, size and unit. Candidates marked 'usual' are products this customer "
        "orders often - prefer them when ambiguous. Return null if no candidate fits.\n\n"
        + json.dumps(lines, indent=1)
    )

    client = get_client()
    try:
        resp = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=2000,
            tools=[PICK_TOOL],
            tool_choice={"type": "tool", "name": "pick_products"},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise ExtractionError(f"Claude API error: {e}")
    _report_usage(on_usage, "claude_pick", resp)

    result = {}
    for block in resp.content:
        if block.type == "tool_use":
            for p in block.input.get("picks", []):
                result[int(p["line_index"])] = (p.get("product_id"), float(p.get("confidence", 0)))
    return result
