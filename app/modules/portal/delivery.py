"""
Delivery days / cut-off time - portal checkout pe customer sirf sahi delivery date chun sake.

Settings -> Customer portal -> Setup -> Delivery:
  delivery_days   kis din delivery hoti hai (Mon..Sun). Khali = koi bhi din
  cutoff_time     "16:00" - iske baad aaya order ek din aur late (business ke timezone mein)
  lead_days       order se kam se kam kitne din baad (1 = next day, 0 = same day)
  closed_dates    chhutti ke din (Christmas, public holidays)

Rules tabhi lagte hain jab delivery days ya cut-off time set ho - tab delivery date zaroori hai.
Warna pehle jaisa: date optional, bas past nahi.

Example: Mon-Fri, cut-off 4pm, lead 1 day.
  Mon 3pm order  -> earliest Tue
  Mon 5pm order  -> earliest Wed   (cut-off nikal gaya)
  Fri 5pm order  -> earliest Mon   (Sat/Sun delivery nahi; Fri+2 = Sun -> agla delivery day Mon)

Customer portal (login):
  GET /portal/{slug}/delivery-dates     {required, earliest, dates: [..30 din..], cutoff_time, days, lead_days}
"""
from datetime import date, datetime, time, timedelta, timezone

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SEARCH_DAYS = 180          # itne din aage tak delivery day dhundho


def parse_days(value: str | None) -> list[int]:
    """"0,1,2" -> [0, 1, 2] (0 = Monday). Galat values chhod do."""
    days = set()
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return sorted(days)


def days_to_str(days: list[int] | None) -> str | None:
    days = sorted({d for d in (days or []) if 0 <= d <= 6})
    return ",".join(str(d) for d in days) if days and len(days) < 7 else None   # saare 7 = koi rule nahi


def parse_time(value: str | None) -> time | None:
    try:
        h, m = (value or "").split(":")
        return time(int(h), int(m))
    except ValueError:
        return None


def rules_active(cfg) -> bool:
    return bool(parse_days(cfg.portal_delivery_days)) or parse_time(cfg.portal_cutoff_time) is not None


def local_now(tenant) -> datetime:
    from app.modules.reports.router import _tenant_tz
    return datetime.now(timezone.utc).astimezone(_tenant_tz(tenant)).replace(tzinfo=None)


def is_delivery_day(cfg, d: date) -> bool:
    days = parse_days(cfg.portal_delivery_days)
    if days and d.weekday() not in days:
        return False
    return d.isoformat() not in (cfg.portal_closed_dates or [])


def earliest_date(cfg, now: datetime) -> date:
    """Sabse pehli date jis din delivery ho sakti hai."""
    lead = max(cfg.portal_lead_days or 0, 0)
    cutoff = parse_time(cfg.portal_cutoff_time)
    if cutoff and now.time() >= cutoff:
        lead += 1
    d = now.date() + timedelta(days=lead)
    for _ in range(SEARCH_DAYS):
        if is_delivery_day(cfg, d):
            return d
        d += timedelta(days=1)
    return d


def available_dates(cfg, now: datetime, count: int = 30) -> list[date]:
    out, d = [], earliest_date(cfg, now)
    for _ in range(SEARCH_DAYS):
        if len(out) >= count:
            break
        if is_delivery_day(cfg, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def check_delivery_date(cfg, tenant, d: date | None) -> str | None:
    """Checkout pe: galat date ho to customer ke liye error message, theek ho to None."""
    now = local_now(tenant)
    if not rules_active(cfg):
        return "Delivery date can't be in the past" if d and d < now.date() else None
    if not d:
        return "Please choose a delivery date"
    if not is_delivery_day(cfg, d):
        return f"We don't deliver on {d:%A %d %b} - please choose another date"
    first = earliest_date(cfg, now)
    if d < first:
        return f"The earliest delivery date is {first:%A %d %b} - please choose that or a later date"
    return None


def delivery_out(cfg, tenant) -> dict:
    now = local_now(tenant)
    active = rules_active(cfg)
    dates = available_dates(cfg, now) if active else []
    return {"required": active, "earliest": dates[0] if dates else None, "dates": dates,
            "days": parse_days(cfg.portal_delivery_days), "cutoff_time": cfg.portal_cutoff_time,
            "lead_days": cfg.portal_lead_days or 0}
