"""Delivery days / cut-off time / chhutti - portal checkout."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.modules.portal import delivery
from tests.test_portal_shop import _login, _setup


def cfg(days="0,1,2,3,4", cutoff="16:00", lead=1, closed=None):
    return SimpleNamespace(portal_delivery_days=days, portal_cutoff_time=cutoff, portal_lead_days=lead,
                           portal_closed_dates=closed)


def test_earliest_date_rules():
    mon = datetime(2030, 1, 7, 15, 0)                                     # Monday 3pm
    assert delivery.earliest_date(cfg(), mon) == date(2030, 1, 8)          # Tue
    assert delivery.earliest_date(cfg(), mon.replace(hour=17)) == date(2030, 1, 9)    # cut-off nikal gaya -> Wed
    fri_late = datetime(2030, 1, 11, 17, 0)
    assert delivery.earliest_date(cfg(), fri_late) == date(2030, 1, 14)    # Sat/Sun nahi -> Mon
    assert delivery.earliest_date(cfg(closed=["2030-01-14"]), fri_late) == date(2030, 1, 15)   # Mon chhutti
    assert delivery.earliest_date(cfg(days=None, cutoff=None, lead=0), mon) == date(2030, 1, 7)  # same day
    dates = delivery.available_dates(cfg(), mon, count=6)
    assert [d.weekday() for d in dates] == [1, 2, 3, 4, 0, 1]


def test_days_to_str():
    assert delivery.days_to_str([4, 0, 0, 9]) == "0,4"
    assert delivery.days_to_str(list(range(7))) is None and delivery.days_to_str([]) is None


def test_settings_and_checkout(client, auth):
    _, _, lemon, _ = _setup(client, auth)
    _, h = _login(client, auth)
    assert client.get("/portal/freshfarms/delivery-dates", headers=h).json()["required"] is False

    r = client.put("/settings/portal", headers=auth, json={"delivery_days": [0, 2, 4], "cutoff_time": "9:30",
                                                          "lead_days": 1, "closed_dates": ["2030-12-25"]})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["delivery_days"] == [0, 2, 4] and s["cutoff_time"] == "09:30" and s["closed_dates"] == ["2030-12-25"]
    assert client.put("/settings/portal", headers=auth, json={"cutoff_time": "25:99"}).status_code == 400
    assert client.put("/settings/portal", headers=auth, json={"lead_days": 99}).status_code == 400

    d = client.get("/portal/freshfarms/delivery-dates", headers=h).json()
    assert d["required"] is True and len(d["dates"]) == 30
    assert all(date.fromisoformat(x).weekday() in (0, 2, 4) for x in d["dates"])
    earliest = date.fromisoformat(d["earliest"])

    items = [{"product_id": lemon["id"], "quantity": 1}]
    order = lambda dd: client.post("/portal/freshfarms/orders", headers=h,
                                   json={"items": items, "payment_method": "cod", "delivery_date": dd})
    assert "choose a delivery date" in order(None).json()["detail"]
    wrong_day = next(earliest + timedelta(days=i) for i in range(1, 8) if (earliest + timedelta(days=i)).weekday() == 1)
    assert "don't deliver on" in order(wrong_day.isoformat()).json()["detail"]
    too_early = earliest - timedelta(days=7)
    assert order(too_early.isoformat()).status_code == 400
    assert order(earliest.isoformat()).status_code == 200

    # rules band (saare din, koi cut-off nahi) -> date optional
    client.put("/settings/portal", headers=auth, json={"delivery_days": [], "cutoff_time": ""})
    assert order(None).status_code == 200
