"""Management KPIs and forecasts computed from the operational collections (shipments, quotes, invoices).

Definitions (shown in the UI next to each number; dates are Europe/Helsinki calendar days):
  volume            shipments booked in the period (created_at), cancelled excluded; split by mode.
  booked sell       sum of the shipment price (price.total_cents, the accepted option's net total) for those shipments.
  gross margin      booked sell - cost, for shipments whose cost is known: actual_cost_cents on the shipment when recorded,
                    else the accepted quote option's cost_cents. `margin_coverage` says for how many shipments the cost was known.
  invoiced revenue  net (VAT excluded) subtotal of issued invoices minus credit notes, by issue_date; drafts and void excluded.
                    `services_net_cents` excludes pass-through disbursements (duty, import VAT paid on the customer's behalf).
  on-time           delivered shipments (delivered_at in the period) delivered on or before the promised ETA day
                    (eta_planned, the ETA given at booking; falls back to eta). Shipments without a promised ETA are not counted.
  quote win rate    quotes created in the period: won = booked; lost = rejected or expired; win rate = won / (won + lost).
                    Open quotes (sent, draft, pending approval, accepted, booking) and withdrawn/cancelled ones are not decided.
  AR days (DSO)     open receivables now / invoiced gross (VAT included) in the trailing 90 days x 90.
  days to pay       average days from issue_date to paid_at for invoices paid in the period.
Company-wide figures never include test tenants, whatever the ops toggle (core.tenant_visibility.finance_scope);
`company_id` narrows to one customer (its own view works even for a test tenant).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.db import db as _db
from core.tenant_visibility import finance_scope
from core.util import now_iso, parse_iso
from services.forecast import forecast_series

# The test-tenant rule here is finance_scope (company-wide never includes test tenants), not the request's ops toggle.
# Demo seed rows stay in: history() reports them as a data-quality signal.
db = getattr(_db, "raw", _db)
TENANT_FLAGS = ("test", "test_tenant")

HELSINKI = ZoneInfo("Europe/Helsinki")
OPEN_AR = ("open", "overdue", "processing")
ISSUED = ("open", "overdue", "processing", "paid", "credited", "partially_paid")
WON = ("booked",)
LOST = ("rejected", "expired")
NOT_DECIDED_EXCLUDED = ("withdrawn", "cancelled")
DSO_WINDOW_DAYS = 90


def local_day(value) -> date | None:
    """Helsinki calendar day of an ISO timestamp, or a plain YYYY-MM-DD date."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value)
    if len(s) == 10:
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    dt = parse_iso(s)
    if not dt:
        return None
    return dt.astimezone(HELSINKI).date() if dt.tzinfo else dt.date()


def today() -> date:
    return datetime.now(HELSINKI).date()


def bucket_start(d: date, interval: str) -> date:
    if interval == "month":
        return d.replace(day=1)
    return d - timedelta(days=d.weekday())  # ISO week, Monday


def next_bucket(d: date, interval: str) -> date:
    if interval == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=7)


def buckets(start: date, end: date, interval: str) -> list[date]:
    out, b = [], bucket_start(start, interval)
    while b <= end:
        out.append(b)
        b = next_bucket(b, interval)
    return out


def _utc_bounds(start: date, end: date) -> tuple[str, str]:
    """ISO range covering Helsinki days start..end with a one-day margin (exact filtering is done per day in Python)."""
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S")
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S")
    return lo, hi


def _pct(num: float, den: float) -> float | None:
    return round(100.0 * num / den, 1) if den else None


async def _option_costs(quote_ids: list[str]) -> dict[tuple[str, str], int | None]:
    """(quote_id, option_id) -> cost_cents of that option."""
    out: dict[tuple[str, str], int | None] = {}
    if not quote_ids:
        return out
    async for q in db.quotes.find({"id": {"$in": quote_ids}}, {"_id": 0, "id": 1, "options.id": 1, "options.cost_cents": 1}):
        for o in q.get("options") or []:
            out[(q["id"], o.get("id"))] = o.get("cost_cents")
    return out


def _shipment_cost(s: dict, costs: dict) -> tuple[int | None, str | None]:
    if s.get("actual_cost_cents") is not None:
        return int(s["actual_cost_cents"]), "actual"
    c = costs.get((s.get("quote_id"), s.get("option_id")))
    if c is not None:
        return int(c), "quoted"
    return None, None


async def load(start: date, end: date, company_id: str | None = None) -> dict:
    """Raw rows the KPIs need, already narrowed to the period (plus AR, which is a snapshot)."""
    base = await finance_scope(company_id, flags=TENANT_FLAGS)
    lo, hi = _utc_bounds(start, end)
    ships = await db.shipments.find({**base, "created_at": {"$gte": lo, "$lt": hi}, "status": {"$ne": "cancelled"}},
                                    {"_id": 0, "id": 1, "mode": 1, "created_at": 1, "price": 1, "quote_id": 1, "option_id": 1,
                                     "actual_cost_cents": 1, "status": 1, "company_id": 1}).to_list(None)
    ships = [s for s in ships if start <= (local_day(s.get("created_at")) or date.min) <= end]
    delivered = await db.shipments.find({**base, "status": "delivered", "delivered_at": {"$gte": lo, "$lt": hi}},
                                        {"_id": 0, "id": 1, "delivered_at": 1, "eta_planned": 1, "eta": 1, "mode": 1}).to_list(None)
    delivered = [s for s in delivered if start <= (local_day(s.get("delivered_at")) or date.min) <= end]
    quotes = await db.quotes.find({**base, "created_at": {"$gte": lo, "$lt": hi}},
                                  {"_id": 0, "id": 1, "status": 1, "created_at": 1, "shipment_id": 1}).to_list(None)
    quotes = [q for q in quotes if start <= (local_day(q.get("created_at")) or date.min) <= end]
    invoices = await db.invoices.find({**base, "status": {"$in": list(ISSUED)}, "issue_date": {"$gte": (start - timedelta(days=DSO_WINDOW_DAYS)).isoformat(),
                                                                                             "$lte": end.isoformat()}},
                                      {"_id": 0, "id": 1, "kind": 1, "status": 1, "issue_date": 1, "subtotal_cents": 1,
                                       "services_net_cents": 1, "total_cents": 1}).to_list(None)
    paid = await db.invoices.find({**base, "kind": "invoice", "paid_at": {"$gte": lo, "$lt": hi}},
                                  {"_id": 0, "id": 1, "issue_date": 1, "paid_at": 1}).to_list(None)
    paid = [i for i in paid if start <= (local_day(i.get("paid_at")) or date.min) <= end]
    open_ar = await db.invoices.find({**base, "kind": "invoice", "status": {"$in": list(OPEN_AR)}},
                                     {"_id": 0, "id": 1, "status": 1, "due_date": 1, "total_cents": 1, "paid_cents": 1,
                                      "credited_cents": 1}).to_list(None)
    costs = await _option_costs(sorted({s["quote_id"] for s in ships if s.get("quote_id")}))
    return {"shipments": ships, "delivered": delivered, "quotes": quotes, "invoices": invoices, "paid": paid,
            "open_ar": open_ar, "costs": costs}


def _amount_due(inv: dict) -> int:
    return max(0, int(inv.get("total_cents") or 0) - int(inv.get("paid_cents") or 0) - int(inv.get("credited_cents") or 0))


def summarize(rows: dict, start: date, end: date, *, as_of: date | None = None) -> dict:
    """Pure function: KPI summary for rows already narrowed to [start, end]."""
    as_of = as_of or today()
    ships = rows["shipments"]
    by_mode: dict[str, int] = {}
    sell = margin_sell = cost = 0
    covered = actual = 0
    for s in ships:
        by_mode[s.get("mode") or "other"] = by_mode.get(s.get("mode") or "other", 0) + 1
        price = int((s.get("price") or {}).get("total_cents") or 0)
        sell += price
        c, src = _shipment_cost(s, rows["costs"])
        if c is not None and price:
            covered += 1
            actual += src == "actual"
            margin_sell += price
            cost += c
    margin = margin_sell - cost if covered else None

    in_period = [i for i in rows["invoices"] if start <= (local_day(i.get("issue_date")) or date.min) <= end]
    revenue = sum(int(i.get("subtotal_cents") or 0) for i in in_period)
    services_net = sum(int(i.get("services_net_cents") if i.get("services_net_cents") is not None else i.get("subtotal_cents") or 0)
                       for i in in_period)

    on_time = late = 0
    for s in rows["delivered"]:
        promised = local_day(s.get("eta_planned") or s.get("eta"))
        got = local_day(s.get("delivered_at"))
        if not promised or not got:
            continue
        if got <= promised:
            on_time += 1
        else:
            late += 1

    won = sum(1 for q in rows["quotes"] if q.get("status") in WON)
    lost = sum(1 for q in rows["quotes"] if q.get("status") in LOST)
    excluded = sum(1 for q in rows["quotes"] if q.get("status") in NOT_DECIDED_EXCLUDED)
    open_q = len(rows["quotes"]) - won - lost - excluded

    receivables = sum(_amount_due(i) for i in rows["open_ar"])
    overdue = [i for i in rows["open_ar"] if (local_day(i.get("due_date")) or date.max) < as_of]
    overdue_cents = sum(_amount_due(i) for i in overdue)
    window_start = end - timedelta(days=DSO_WINDOW_DAYS - 1)
    gross_window = sum(int(i.get("total_cents") or 0) for i in rows["invoices"]
                       if window_start <= (local_day(i.get("issue_date")) or date.min) <= end)
    dso = round(receivables / gross_window * DSO_WINDOW_DAYS, 1) if gross_window > 0 else None
    pay_days = [(local_day(i["paid_at"]) - local_day(i["issue_date"])).days for i in rows["paid"]
                if local_day(i.get("issue_date")) and local_day(i.get("paid_at"))]

    return {
        "volume": len(ships), "volume_by_mode": dict(sorted(by_mode.items())),
        "booked_sell_cents": sell,
        "margin_cents": margin, "margin_pct": _pct(margin, margin_sell) if margin is not None else None,
        "margin_coverage": {"known": covered, "of": len(ships), "actual": actual, "quoted": covered - actual,
                            "sell_cents": margin_sell, "cost_cents": cost},
        "revenue_cents": revenue, "services_net_cents": services_net, "invoices_issued": len(in_period),
        "on_time_pct": _pct(on_time, on_time + late), "on_time": on_time, "late": late, "delivered": len(rows["delivered"]),
        "quote_win_rate_pct": _pct(won, won + lost), "quotes": {"created": len(rows["quotes"]), "won": won, "lost": lost,
                                                                 "open": open_q, "withdrawn": excluded},
        "receivables_cents": receivables, "overdue_cents": overdue_cents, "overdue_count": len(overdue),
        "open_invoices": len(rows["open_ar"]), "ar_days": dso, "dso_window_days": DSO_WINDOW_DAYS, "dso_gross_cents": gross_window,
        "avg_days_to_pay": round(sum(pay_days) / len(pay_days), 1) if pay_days else None, "paid_invoices": len(pay_days),
    }


def series(rows: dict, start: date, end: date, interval: str) -> list[dict]:
    out = []
    for b in buckets(start, end, interval):
        b_end = min(end, next_bucket(b, interval) - timedelta(days=1))
        b_start = max(start, b)

        def within(v) -> bool:
            d = local_day(v)
            return bool(d) and b_start <= d <= b_end

        sub = {
            "shipments": [s for s in rows["shipments"] if within(s.get("created_at"))],
            "delivered": [s for s in rows["delivered"] if within(s.get("delivered_at"))],
            "quotes": [q for q in rows["quotes"] if within(q.get("created_at"))],
            "invoices": [i for i in rows["invoices"] if within(i.get("issue_date"))],
            "paid": [], "open_ar": [], "costs": rows["costs"],
        }
        k = summarize(sub, b_start, b_end)
        out.append({"start": b.isoformat(), "end": (next_bucket(b, interval) - timedelta(days=1)).isoformat(),
                    "partial": b < start or next_bucket(b, interval) - timedelta(days=1) > end,
                    "volume": k["volume"], "volume_by_mode": k["volume_by_mode"], "booked_sell_cents": k["booked_sell_cents"],
                    "margin_cents": k["margin_cents"], "margin_pct": k["margin_pct"], "revenue_cents": k["revenue_cents"],
                    "on_time_pct": k["on_time_pct"], "on_time": k["on_time"], "late": k["late"],
                    "quotes_won": k["quotes"]["won"], "quotes_lost": k["quotes"]["lost"], "quote_win_rate_pct": k["quote_win_rate_pct"]})
    return out


FINANCIAL_KEYS = ("booked_sell_cents", "margin_cents", "margin_pct", "margin_coverage", "revenue_cents", "services_net_cents",
                  "receivables_cents", "overdue_cents", "ar_days", "dso_gross_cents", "avg_days_to_pay")


def _hide_financials(d: dict):
    for k in FINANCIAL_KEYS:
        if k in d:
            d[k] = None


async def kpis(start: date, end: date, interval: str = "week", *, company_id: str | None = None,
               financials: bool = True) -> dict:
    if end < start:
        start, end = end, start
    rows = await load(start, end, company_id)
    current = summarize(rows, start, end)
    days = (end - start).days + 1
    p_end = start - timedelta(days=1)
    p_start = p_end - timedelta(days=days - 1)
    prev = summarize(await load(p_start, p_end, company_id), p_start, p_end)
    ser = series(rows, start, end, interval)
    if not financials:
        _hide_financials(current)
        _hide_financials(prev)
        for row in ser:
            _hide_financials(row)
    return {"from": start.isoformat(), "to": end.isoformat(), "interval": interval, "days": days, "company_id": company_id,
            "kpis": current, "previous": {"from": p_start.isoformat(), "to": p_end.isoformat(), "kpis": prev},
            "series": ser, "financials_visible": financials, "generated_at": now_iso(), "timezone": "Europe/Helsinki",
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "volume": {"fi": "Kaudella varatut lähetykset (peruutetut pois).", "en": "Shipments booked in the period (cancelled excluded)."},
    "revenue": {"fi": "Lähetettyjen laskujen veroton summa hyvitykset vähennettyinä, laskun päivän mukaan.",
                "en": "Net (VAT excluded) total of issued invoices minus credit notes, by issue date."},
    "margin": {"fi": "Myyntihinta − kustannus lähetyksille, joiden kustannus tiedetään (toteutunut tai hyväksytyn tarjouksen).",
               "en": "Sell − cost for shipments with a known cost (actual, else the accepted quote option's)."},
    "on_time": {"fi": "Toimitetut lähetykset, jotka toimitettiin luvattuna ETA-päivänä tai aiemmin.",
                "en": "Delivered shipments delivered on or before the promised ETA day."},
    "win_rate": {"fi": "Kauden tarjouksista varatut / (varatut + hylätyt + vanhentuneet).",
                 "en": "Quotes created in the period: booked / (booked + rejected + expired)."},
    "ar_days": {"fi": "Avoimet saatavat ÷ viimeisen 90 päivän laskutus (sis. ALV) × 90.",
                "en": "Open receivables ÷ last 90 days' invoicing (incl. VAT) × 90."},
}


# ------------------------------------------------------------------------------------------------ forecast

async def history(interval: str, *, company_id: str | None = None, until: date | None = None, max_buckets: int = 156) -> dict:
    """Complete buckets of shipment count and booked sell, oldest first, up to the last complete bucket before `until`."""
    until = until or today()
    last_complete_end = bucket_start(until, interval) - timedelta(days=1)
    base = await finance_scope(company_id, flags=TENANT_FLAGS)
    rows = await db.shipments.find({**base, "status": {"$ne": "cancelled"}},
                                   {"_id": 0, "created_at": 1, "price.total_cents": 1,
                                    "demo": 1, "test": 1, "test_tenant": 1, "source": 1}).to_list(None)
    days = [(local_day(r.get("created_at")), (r.get("price") or {}).get("total_cents"),
             bool(r.get("demo") or r.get("test") or r.get("test_tenant") or r.get("source") == "seed")) for r in rows]
    days = [(d, v, demo) for d, v, demo in days if d and d <= last_complete_end]
    quality = {"records": 0, "demo_test_records": 0, "missing_price_records": 0,
               "operating_history_validated": False}
    if not days:
        return {"buckets": [], "volume": [], "sell_cents": [], "data_quality": quality}
    first = max(min(d for d, _, _ in days), last_complete_end - timedelta(days=(31 if interval == "month" else 7) * max_buckets))
    bs = buckets(first, last_complete_end, interval)
    idx = {b: i for i, b in enumerate(bs)}
    vol, sell = [0] * len(bs), [0] * len(bs)
    for d, v, demo in days:
        i = idx.get(bucket_start(d, interval))
        if i is not None:
            vol[i] += 1
            quality["records"] += 1
            quality["demo_test_records"] += int(demo)
            if not isinstance(v, int) or isinstance(v, bool):
                sell[i] = None
                quality["missing_price_records"] += 1
            elif sell[i] is not None:
                sell[i] += v
    return {"buckets": [b.isoformat() for b in bs], "volume": vol, "sell_cents": sell, "data_quality": quality}


async def forecast(horizon: int = 12, interval: str = "week", *, company_id: str | None = None, financials: bool = True) -> dict:
    h = await history(interval, company_id=company_id)
    season = 52 if interval == "week" else 12
    requested = horizon
    # never forecast further ahead than half the history we have (minimum 4 periods): short histories give short forecasts
    horizon = max(1, min(horizon, max(4, len(h["buckets"]) // 2)))
    future = []
    if h["buckets"]:
        b = date.fromisoformat(h["buckets"][-1])
        for _ in range(horizon):
            b = next_bucket(b, interval)
            future.append(b.isoformat())
    vol = forecast_series(h["volume"], horizon=horizon, season=season)
    rev = forecast_series(h["sell_cents"], horizon=horizon, season=season) if financials else None
    for fc in (vol, rev):
        if fc and fc.get("forecast"):
            for p, d in zip(fc["forecast"], future):
                p["start"] = d
    return {
        "interval": interval, "horizon": horizon, "horizon_requested": requested, "company_id": company_id, "season_length": season,
        "history": {"buckets": h["buckets"], "volume": h["volume"], "sell_cents": h["sell_cents"] if financials else None},
        "data_quality": {k: v for k, v in h["data_quality"].items() if financials or k != "missing_price_records"},
        "volume": vol, "revenue": rev, "revenue_basis": "booked_sell",
        "financials_visible": financials, "generated_at": now_iso(),
        "method_note": {"fi": "Liukuvan takatestin pienempi keskivirhe ratkaisee: tasoitettu malli tai naiivi vertailuennuste. "
                              "Takatesti on myös menetelmän valinta-aineisto, ei riippumaton tulevan tarkkuuden todiste. "
                              "80 % väli on laskennallinen; toteutunutta kattavuutta ei ole varmistettu.",
                        "en": "Select smoothing or the naive baseline by rolling-origin mean absolute error. "
                              "This backtest also selects the method; it is not independent evidence of future accuracy. "
                              "The nominal 80% band has not been calibrated on future outcomes."},
    }
