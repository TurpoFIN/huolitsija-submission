"""s14 management insight: KPIs and forecast against the real local API and MongoDB.

A fixture company (test=True) gets a hand-made month of shipments, quotes and invoices in March 2025. Expected values are
computed in this file from the raw fixture documents (and cross-checked against literal numbers worked out by hand), then
compared with GET /api/insights/kpis?company_id=... . The forecast is checked on a synthetic seasonal series with a known
answer and through the API on inserted weekly history.

Env: S14_API (default http://localhost:8033/api), S14_DB (huo_s14_ins), MONGO_URL, DEMO_OPS_PASSWORD, DEMO_CUSTOMER_PASSWORD.
"""
from __future__ import annotations

import math
import os
import random
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx
import pymongo
import pytest

API = os.environ.get("S14_API", "http://localhost:8033/api")
DB_NAME = os.environ.get("S14_DB", "huo_s14_ins")
os.environ.setdefault("DB_NAME", DB_NAME)
db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from services.forecast import forecast_series  # noqa: E402

HEL = ZoneInfo("Europe/Helsinki")
PW = os.environ.get("DEMO_OPS_PASSWORD", "ci-pass-123456")


def login(email: str, password: str, include_test: bool = True) -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    if include_test:
        headers["X-Include-Test-Tenants"] = "true"
    return httpx.Client(base_url=API, headers=headers, timeout=60)


@lru_cache
def ops() -> httpx.Client:
    return login("ops.demo@huolitsija.fi", PW)


def internal_user(role: str) -> httpx.Client:
    from core.security import hash_password

    pw = "S14-" + uuid.uuid4().hex[:12] + "x9"
    u = {"id": str(uuid.uuid4()), "email": f"s14-{role}-{uuid.uuid4().hex[:6]}@example.fi", "name": f"S14 {role}",
         "password_hash": hash_password(pw), "role": role, "org_type": "internal", "company_id": None, "locale": "fi",
         "email_verified": True, "session_version": 0, "notify_email": False, "test": True,
         "created_at": datetime.now(timezone.utc).isoformat()}
    db.users.insert_one(dict(u))
    return login(u["email"], pw)


def company(name: str) -> dict:
    c = {"id": str(uuid.uuid4()), "name": f"{name} {uuid.uuid4().hex[:5]}", "business_id": "3990014-1", "country": "FI",
         "kyc_status": "approved", "sanctions_status": "clear", "status": "active", "test": True,
         "created_at": datetime.now(timezone.utc).isoformat()}
    db.companies.insert_one(dict(c))
    return c


def hel_day(iso_s: str) -> date:
    return datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone(HEL).date()


# ------------------------------------------------------------------------------------------- fixture month

START, END = date(2025, 3, 3), date(2025, 3, 30)  # Monday .. Sunday, four ISO weeks


@pytest.fixture(scope="module")
def month():
    c = company("S14 KPI Oy")
    cid = c["id"]

    def quote(status, created, cost=None, sell=None):
        q = {"id": str(uuid.uuid4()), "company_id": cid, "status": status, "created_at": created, "test": True,
             "options": [{"id": str(uuid.uuid4()), "total_cents": sell, "cost_cents": cost}]}
        db.quotes.insert_one(dict(q))
        return q

    q1 = quote("booked", "2025-03-04T08:00:00Z", cost=240000, sell=300000)
    q2 = quote("booked", "2025-03-10T08:00:00Z", cost=70000, sell=100000)
    others = [quote("rejected", "2025-03-12T08:00:00Z"), quote("expired", "2025-03-14T08:00:00Z"),
              quote("sent", "2025-03-26T08:00:00Z"), quote("withdrawn", "2025-03-27T08:00:00Z"),
              quote("rejected", "2025-02-27T08:00:00Z")]  # previous period

    def ship(mode, created, price, status="in_transit", quote_doc=None, actual=None, delivered=None, eta_planned=None):
        s = {"id": str(uuid.uuid4()), "reference": f"HUO-S14-{uuid.uuid4().hex[:6]}", "company_id": cid, "mode": mode,
             "status": status, "created_at": created, "price": {"total_cents": price, "currency": "EUR"},
             "quote_id": (quote_doc or {}).get("id"), "option_id": (quote_doc or {}).get("options", [{}])[0].get("id"),
             "delivered_at": delivered, "eta_planned": eta_planned, "eta": eta_planned, "test": True}
        if actual is not None:
            s["actual_cost_cents"] = actual
        db.shipments.insert_one(dict(s))
        return s

    ships = [
        # on time: delivered the promised day
        ship("sea", "2025-03-04T10:00:00Z", 300000, "delivered", q1, delivered="2025-03-20T09:00:00Z", eta_planned="2025-03-20T15:00:00Z"),
        # late only in Helsinki time: 22:30 UTC on the 25th is 00:30 on the 26th in Helsinki (UTC+2 before 30 March)
        ship("road", "2025-03-11T10:00:00Z", 100000, "delivered", q2, actual=90000, delivered="2025-03-25T22:30:00Z",
             eta_planned="2025-03-25T12:00:00Z"),
        ship("air", "2025-03-18T10:00:00Z", 50000),  # no cost known
        ship("road", "2025-03-25T10:00:00Z", 70000, "cancelled"),  # excluded
        ship("road", "2025-02-20T10:00:00Z", 80000),  # previous period
    ]

    def inv(kind, status, issue, subtotal, total, services_net=None, due=None, paid_at=None, paid=0):
        i = {"id": str(uuid.uuid4()), "company_id": cid, "kind": kind, "status": status, "issue_date": issue, "due_date": due,
             "subtotal_cents": subtotal, "services_net_cents": services_net if services_net is not None else subtotal,
             "total_cents": total, "paid_cents": paid, "credited_cents": 0, "paid_at": paid_at, "test": True}
        db.invoices.insert_one(dict(i))
        return i

    invoices = [
        inv("invoice", "paid", "2025-03-21", 300000, 300000, due="2025-04-04", paid_at="2025-03-28T10:00:00Z", paid=300000),
        inv("invoice", "open", "2025-03-27", 100000, 125500, services_net=100000, due="2025-04-10"),
        inv("credit_note", "open", "2025-03-28", -20000, -20000),
        inv("invoice", "draft", "2025-03-29", 999999, 999999),  # drafts never count
    ]
    yield {"company": c, "quotes": [q1, q2, *others], "shipments": ships, "invoices": invoices}
    for coll in ("quotes", "shipments", "invoices"):
        db[coll].delete_many({"company_id": cid})
    db.companies.delete_one({"id": cid})


def expected_from_raw(fx: dict, start: date, end: date) -> dict:
    """Independent computation of the KPI definitions straight from the fixture documents."""
    inside = lambda d: start <= d <= end  # noqa: E731
    ships = [s for s in fx["shipments"] if s["status"] != "cancelled" and inside(hel_day(s["created_at"]))]
    quotes_by_id = {q["id"]: q for q in fx["quotes"]}
    sell = sum(s["price"]["total_cents"] for s in ships)
    known = []
    for s in ships:
        if "actual_cost_cents" in s:
            known.append((s["price"]["total_cents"], s["actual_cost_cents"]))
        elif s["quote_id"]:
            known.append((s["price"]["total_cents"], quotes_by_id[s["quote_id"]]["options"][0]["cost_cents"]))
    m_sell, m_cost = sum(k[0] for k in known), sum(k[1] for k in known)
    delivered = [s for s in fx["shipments"] if s["status"] == "delivered" and inside(hel_day(s["delivered_at"]))]
    on_time = sum(1 for s in delivered if hel_day(s["delivered_at"]) <= hel_day(s["eta_planned"]))
    qs = [q for q in fx["quotes"] if inside(hel_day(q["created_at"]))]
    won = sum(q["status"] == "booked" for q in qs)
    lost = sum(q["status"] in ("rejected", "expired") for q in qs)
    issued = [i for i in fx["invoices"] if i["status"] != "draft" and inside(date.fromisoformat(i["issue_date"]))]
    open_inv = [i for i in fx["invoices"] if i["kind"] == "invoice" and i["status"] in ("open", "overdue", "processing")]
    receivable = sum(i["total_cents"] - i["paid_cents"] for i in open_inv)
    window = [i for i in fx["invoices"] if i["status"] != "draft"
              and end - timedelta(days=89) <= date.fromisoformat(i["issue_date"]) <= end]
    gross = sum(i["total_cents"] for i in window)
    paid = [i for i in fx["invoices"] if i.get("paid_at") and inside(hel_day(i["paid_at"]))]
    return {
        "volume": len(ships), "booked_sell_cents": sell, "margin_cents": m_sell - m_cost,
        "margin_pct": round(100 * (m_sell - m_cost) / m_sell, 1), "known": len(known),
        "on_time_pct": round(100 * on_time / len(delivered), 1), "win_rate": round(100 * won / (won + lost), 1),
        "revenue_cents": sum(i["subtotal_cents"] for i in issued), "receivables_cents": receivable,
        "ar_days": round(receivable / gross * 90, 1),
        "days_to_pay": sum((hel_day(i["paid_at"]) - date.fromisoformat(i["issue_date"])).days for i in paid) / len(paid),
    }


def test_kpis_match_raw_documents(month):
    r = ops().get("/insights/kpis", params={"from": START.isoformat(), "to": END.isoformat(), "interval": "week",
                                             "company_id": month["company"]["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    k = body["kpis"]
    exp = expected_from_raw(month, START, END)
    # hand-checked literals: 3 shipments, sell 4 500 €, margin 700 € on 4 000 € sell (17.5 %), one of two on time,
    # 2 won / 2 lost quotes, revenue 3 000 + 1 000 − 200 €, DSO 1 255 / 4 055 × 90 = 27.9 days, paid in 7 days
    assert exp == {"volume": 3, "booked_sell_cents": 450000, "margin_cents": 70000, "margin_pct": 17.5, "known": 2,
                   "on_time_pct": 50.0, "win_rate": 50.0, "revenue_cents": 380000, "receivables_cents": 125500,
                   "ar_days": 27.9, "days_to_pay": 7.0}
    assert k["volume"] == exp["volume"]
    assert k["volume_by_mode"] == {"air": 1, "road": 1, "sea": 1}
    assert k["booked_sell_cents"] == exp["booked_sell_cents"]
    assert k["margin_cents"] == exp["margin_cents"] and k["margin_pct"] == exp["margin_pct"]
    assert k["margin_coverage"] == {"known": 2, "of": 3, "actual": 1, "quoted": 1, "sell_cents": 400000, "cost_cents": 330000}
    assert k["on_time_pct"] == exp["on_time_pct"] and (k["on_time"], k["late"]) == (1, 1)
    assert k["quote_win_rate_pct"] == exp["win_rate"]
    assert k["quotes"] == {"created": 6, "won": 2, "lost": 2, "open": 1, "withdrawn": 1}
    assert k["revenue_cents"] == exp["revenue_cents"] and k["invoices_issued"] == 3
    assert k["receivables_cents"] == exp["receivables_cents"]
    assert k["overdue_cents"] == 125500 and k["overdue_count"] == 1  # due 2025-04-10, long past
    assert k["ar_days"] == exp["ar_days"]
    assert k["avg_days_to_pay"] == exp["days_to_pay"]
    # previous period of equal length holds the February shipment and the rejected quote
    prev = body["previous"]
    assert (prev["from"], prev["to"]) == ("2025-02-03", "2025-03-02")
    assert prev["kpis"]["volume"] == 1 and prev["kpis"]["quotes"]["lost"] == 1
    # weekly series: one shipment in each of the first three weeks, invoicing in weeks 3 and 4
    ser = body["series"]
    assert [b["start"] for b in ser] == ["2025-03-03", "2025-03-10", "2025-03-17", "2025-03-24"]
    assert [b["volume"] for b in ser] == [1, 1, 1, 0]
    assert [b["revenue_cents"] for b in ser] == [0, 0, 300000, 80000]
    assert sum(b["volume"] for b in ser) == k["volume"]


def test_monthly_interval_and_hidden_test_tenants(month):
    cid = month["company"]["id"]
    r = ops().get("/insights/kpis", params={"from": START.isoformat(), "to": END.isoformat(), "interval": "month", "company_id": cid})
    assert r.status_code == 200
    assert [b["start"] for b in r.json()["series"]] == ["2025-03-01"]
    assert r.json()["series"][0]["partial"] is True  # the period starts on the 3rd
    # without the operator's "include test tenants" choice the fixture company is invisible, like every ops aggregate
    hidden = login("ops.demo@huolitsija.fi", PW, include_test=False)
    r = hidden.get("/insights/kpis", params={"from": START.isoformat(), "to": END.isoformat(), "company_id": cid})
    assert r.status_code == 200 and r.json()["kpis"]["volume"] == 0 and r.json()["kpis"]["revenue_cents"] == 0


def test_permissions(month):
    r = httpx.post(f"{API}/auth/login", json={"email": "customer.demo@huolitsija.fi",
                                               "password": os.environ.get("DEMO_CUSTOMER_PASSWORD", PW)}, timeout=20)
    cust = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {r.json()['token']}"}, timeout=30)
    assert cust.get("/insights/kpis").status_code == 403
    assert cust.get("/insights/forecast").status_code == 403
    assert httpx.get(f"{API}/insights/kpis", timeout=20).status_code == 401
    # operations staff see volume and service level, not money
    agent = internal_user("ops_agent")
    r = agent.get("/insights/kpis", params={"from": START.isoformat(), "to": END.isoformat(), "company_id": month["company"]["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["financials_visible"] is False
    assert body["kpis"]["volume"] == 3 and body["kpis"]["on_time_pct"] == 50.0
    assert body["kpis"]["revenue_cents"] is None and body["kpis"]["margin_cents"] is None and body["kpis"]["ar_days"] is None
    f = agent.get("/insights/forecast", params={"company_id": month["company"]["id"]}).json()
    assert f["revenue"] is None and f["history"]["sell_cents"] is None
    assert ops().get("/insights/kpis", params={"from": "nonsense"}).status_code == 400


# ------------------------------------------------------------------------------------------- forecast

def test_holt_winters_recovers_known_seasonal_series():
    rnd = random.Random(7)
    truth = lambda t: 50 + 0.5 * t + 10 * math.sin(2 * math.pi * t / 12)  # noqa: E731
    y = [truth(t) + rnd.gauss(0, 1) for t in range(60)]
    fc = forecast_series(y, horizon=12, season=12)
    assert fc["status"] == "ok" and fc["method"] == "holt_winters_additive" and fc["points_used"] == 60
    errs = [abs(p["mean"] - truth(60 + p["step"] - 1)) for p in fc["forecast"]]
    assert max(errs) < 3.0, errs  # noise sigma 1, seasonal amplitude 10
    covered = sum(p["lo80"] <= truth(60 + p["step"] - 1) <= p["hi80"] for p in fc["forecast"])
    assert covered >= 7  # an 80 % band should hold most of the 12 true values
    bt = fc["backtest"]
    assert bt["baseline_method"] == "seasonal_naive" and bt["model_beats_baseline"] is True
    assert bt["model"]["mape_pct"] < bt["baseline"]["mape_pct"]


def test_damped_trend_without_two_seasons_and_insufficient_history():
    y = [10 + 2 * t for t in range(20)]
    fc = forecast_series(y, horizon=4, season=52)
    assert fc["method"] == "damped_trend"
    assert abs(fc["forecast"][0]["mean"] - 50) < 1.5  # next value of an exact line is 50
    assert all(p["lo80"] <= p["mean"] <= p["hi80"] for p in fc["forecast"])
    short = forecast_series([3, 4, 5], horizon=4)
    assert short["status"] == "insufficient_history" and short["forecast"] == []
    assert forecast_series([0] * 10, horizon=3)["forecast"][0]["lo80"] >= 0  # counts never go negative


def test_forecast_api_uses_real_weekly_history():
    c = company("S14 Ennuste Oy")
    today = datetime.now(HEL).date()
    last_monday = today - timedelta(days=today.weekday()) - timedelta(days=7)  # start of the last complete week
    weeks = 30
    counts = [4 + (i % 3) for i in range(weeks)]  # 4, 5, 6, 4, 5, 6 ...
    docs = []
    for i, n in enumerate(counts):
        monday = last_monday - timedelta(days=7 * (weeks - 1 - i))
        for j in range(n):
            at = datetime.combine(monday + timedelta(days=j % 5), datetime.min.time()).replace(hour=9, tzinfo=timezone.utc)
            docs.append({"id": str(uuid.uuid4()), "reference": f"HUO-S14F-{uuid.uuid4().hex[:8]}", "company_id": c["id"], "mode": "road", "status": "delivered",
                         "created_at": at.isoformat().replace("+00:00", "Z"), "price": {"total_cents": 100000}, "test": True})
    # this week's (incomplete) shipments must not be in the history
    docs.append({"id": str(uuid.uuid4()), "reference": f"HUO-S14F-{uuid.uuid4().hex[:8]}", "company_id": c["id"], "mode": "road", "status": "booked",
                 "created_at": datetime.now(timezone.utc).isoformat(), "price": {"total_cents": 100000}, "test": True})
    try:
        db.shipments.insert_many(docs)
        r = ops().get("/insights/forecast", params={"horizon": 8, "interval": "week", "company_id": c["id"]})
        assert r.status_code == 200, r.text
        f = r.json()
        assert f["history"]["volume"] == counts
        assert f["history"]["sell_cents"] == [n * 100000 for n in counts]
        assert f["history"]["buckets"][-1] == last_monday.isoformat()
        vol = f["volume"]
        assert vol["status"] == "ok" and vol["points_used"] == weeks and vol["method"] == "damped_trend"
        assert f["horizon"] == 8 and len(vol["forecast"]) == 8 and vol["forecast"][0]["start"] == (last_monday + timedelta(days=7)).isoformat()
        assert all(3.0 <= p["mean"] <= 7.0 and p["lo80"] <= p["mean"] <= p["hi80"] for p in vol["forecast"])
        assert vol["backtest"]["origins"] >= 1 and vol["backtest"]["model"]["mape_pct"] is not None
        assert f["revenue"]["status"] == "ok" and abs(f["revenue"]["forecast"][0]["mean"] - 500000) < 150000
        # a forecast never reaches further than half the history: 30 weeks of history -> at most 15 weeks ahead
        long = ops().get("/insights/forecast", params={"horizon": 52, "company_id": c["id"]}).json()
        assert (long["horizon_requested"], long["horizon"], len(long["volume"]["forecast"])) == (52, 15, 15)
    finally:
        db.shipments.delete_many({"company_id": c["id"]})
        db.companies.delete_one({"id": c["id"]})
