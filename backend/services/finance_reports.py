"""Management P&L from actual cost (s13): per shipment, per lane, margin reports and the finance landing page.

Revenue = issued customer invoices for the shipment (status not draft/void), own services only (`services_net_cents`,
i.e. net of VAT and of pass-through disbursements such as duty and import VAT paid on the customer's behalf). Credit
notes subtract. Foreign-currency invoices use their `eur` breakdown (written by billing at issue, ECB rate in `fx`).

Cost = one figure per `cost_accruals` row (one row per shipment + supplier):
    matched            -> actual_eur_cents                       (the carrier invoice)
    partially_matched  -> actual_eur_cents + the part of the expected cost no invoice has covered yet
    open | disputed    -> expected_eur_cents                     (accrued, not invoiced yet)
    released | written_off -> 0
Pass-through accrual lines (customs duty, import VAT paid for the customer) are not our cost and are left out, in the
same way they are left out of revenue.

FX: realised FX on supplier payments (`payables.realised_fx_cents`) and on customer receipts (`invoice.fx_realised_cents`)
is shown below gross profit (positive = gain), so gross profit is the operational margin and net result includes FX.

basis: `actual` = invoiced and every cost row settled by a carrier invoice; `partly_actual` = invoiced but some cost is
still accrued (or comes from the quote because no accrual exists yet); `estimated` = not invoiced yet (revenue and,
where nothing is accrued, cost come from the accepted quote option).

Rows written by other s13 parts (accruals, carrier invoices, payables) are read defensively: a missing field counts as
an explicitly incomplete state; incomplete cost must never appear as a profitable zero-cost shipment.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from core.db import db
from core.tenant_visibility import exclude_test, is_test_record, test_company_ids, test_shipment_ids
from core.util import NO_ID, now_iso
from services.finance_core import autopilot_on, halted, helsinki_now, lane_of

log = logging.getLogger("finance.reports")

COUNTED_INVOICE_EXCLUDED = ("draft", "void")
SETTLED_ACCRUAL = ("matched", "released", "written_off")
CANCELLED_SHIPMENT = ("cancelled",)


# ------------------------------------------------------------------------------------------- small helpers
def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def pct(part: int, whole: int) -> float | None:
    if part is None or not whole:
        return None
    return float((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_day(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _raw(coll):
    """Unfiltered collection: these figures apply their own test-tenant rule (core.tenant_visibility.finance_scope), not
    the request's ops toggle."""
    return getattr(coll, "raw", coll)


def invoice_eur(inv: dict) -> dict:
    """EUR amounts of one issued invoice, signed (credit notes negative). Keys: services_net, pass_through, vat, total,
    fx_realised, converted (bool), warning."""
    sign = -1 if inv.get("kind") == "credit_note" else 1
    cur = (inv.get("currency") or "EUR").upper()
    src = inv
    warning = None
    converted = False
    if cur != "EUR":
        if isinstance(inv.get("eur"), dict):
            src = inv["eur"]
            converted = True
        else:
            rate = (inv.get("fx") or {}).get("rate")
            if rate:
                from services.fx import convert
                src = {k: convert(_i(inv.get(k)), rate) for k in ("services_net_cents", "pass_through_cents", "vat_cents",
                                                                  "total_cents")}
                converted = True
            else:
                warning = f"Invoice {inv.get('number')} is in {cur} without an EUR breakdown: revenue unavailable"

    def amt(k):
        v = _i(src.get(k))
        return -abs(v) if sign < 0 else v

    services = amt("services_net_cents")
    if "services_net_cents" not in src and "subtotal_cents" in src:
        services = amt("subtotal_cents") - amt("pass_through_cents")
    return {"services_net": services, "pass_through": amt("pass_through_cents"), "vat": amt("vat_cents"),
            "total": amt("total_cents"), "fx_realised": _i(inv.get("fx_realised_cents")), "converted": converted,
            "currency": cur, "warning": warning, "complete": warning is None}


def _accrual_own_share(acc: dict) -> Decimal:
    """Share of the accrual that is our own cost (pass-through lines excluded). 1 when lines are absent."""
    lines = acc.get("lines") or []
    if not lines:
        kind = ((acc.get("supplier") or {}).get("kind") or "")
        return Decimal(0) if acc.get("is_pass_through") else Decimal(1) if kind != "pass_through" else Decimal(0)
    total = sum(abs(_i(ln.get("amount_cents"))) for ln in lines)
    if not total:
        return Decimal(0) if all(ln.get("is_pass_through") for ln in lines) else Decimal(1)
    own = sum(abs(_i(ln.get("amount_cents"))) for ln in lines if not ln.get("is_pass_through"))
    return Decimal(own) / Decimal(total)


def accrual_cost(acc: dict, matched_expected: int | None = None) -> dict:
    """Cost of one accrual row in EUR: {actual, accrued, total, counted_status}. matched_expected = expected EUR
    already covered by carrier invoices (sum of matches[].accrued_eur_cents) for partially matched rows."""
    status = acc.get("status") or "open"
    expected = _i(acc.get("expected_eur_cents"))
    actual = _i(acc.get("actual_eur_cents"))
    share = _accrual_own_share(acc)
    if status == "matched":
        a, r = actual, 0
    elif status == "partially_matched":
        if acc.get("remaining_eur_cents") is not None:
            remaining = _i(acc.get("remaining_eur_cents"))
        elif matched_expected is not None:
            remaining = max(0, expected - matched_expected)
        else:
            remaining = max(0, expected - actual)
        a, r = actual, remaining
    elif status in ("released", "written_off"):
        a, r = 0, 0
    else:  # open | disputed | anything unknown counts as accrued
        a, r = 0, expected
    if share != 1:
        a, r = _round(Decimal(a) * share), _round(Decimal(r) * share)
    return {"actual": a, "accrued": r, "total": a + r, "own_share": float(share)}


def quoted_figures(quote: dict | None, option_id: str | None) -> dict | None:
    if not quote:
        return None
    opt_id = option_id or quote.get("accepted_option_id")
    opt = next((o for o in quote.get("options") or [] if o.get("id") == opt_id), None)
    if not opt:
        return None
    lines = opt.get("lines") or []
    if lines:
        own = [ln for ln in lines if not ln.get("is_pass_through") and
               (ln.get("payer") or "").strip().lower() not in ("third_party", "third party")]
        revenue = sum(_i(ln.get("amount_cents")) for ln in own)
        cost = 0
        for ln in own:
            if ln.get("cost_cents") is not None:
                cost += _i(ln.get("cost_cents"))
            else:
                qty = Decimal(str(ln["qty"] if ln.get("qty") is not None else 1))
                cost += _round(Decimal(_i(ln.get("unit_cost_cents"))) * qty)
    else:
        revenue = _i(opt.get("total_cents")) - _i(opt.get("pass_through_cents"))
        cost = _i(opt.get("cost_cents")) - _i(opt.get("pass_through_cents"))
    return {"quote_id": quote.get("id"), "quote_number": quote.get("number"), "option_id": opt.get("id"),
            "carrier_name": opt.get("carrier_name"), "revenue_cents": revenue, "cost_cents": cost,
            "margin_cents": revenue - cost, "margin_pct": pct(revenue - cost, revenue)}


def main_carrier(shipment: dict, quoted: dict | None) -> str:
    for leg in shipment.get("legs") or []:
        if leg.get("role") == "main" and leg.get("carrier"):
            return leg["carrier"]
    return shipment.get("carrier") or (quoted or {}).get("carrier_name") or "—"


def shipment_period_day(s: dict) -> date | None:
    return to_day(s.get("delivered_at")) or to_day(s.get("created_at"))


# ------------------------------------------------------------------------------------------- batch loading
async def _context(shipment_ids: list[str], test_ids: list[str] | None = None) -> dict:
    """All rows the P&L needs for these shipments, loaded in a handful of queries. `test_ids` given (company-wide
    figures): test-tenant invoices, accruals, carrier invoices and payables are left out."""
    ids = list(shipment_ids)
    ctx = {"invoices": defaultdict(list), "accruals": defaultdict(list), "cinv": defaultdict(list),
           "payables": {}, "quotes": {}, "matched_expected": defaultdict(int)}
    if not ids:
        return ctx
    def scoped(q: dict) -> dict:
        return q if test_ids is None else exclude_test(q, ids=test_ids)

    async for inv in _raw(db.invoices).find(scoped({"shipment_id": {"$in": ids}}), NO_ID):
        ctx["invoices"][inv["shipment_id"]].append(inv)
    acc_ids = []
    async for acc in _raw(db.cost_accruals).find(scoped({"shipment_id": {"$in": ids}}), NO_ID):
        ctx["accruals"][acc.get("shipment_id")].append(acc)
        acc_ids.append(acc.get("id"))
    ci_ids = []
    q = {"$or": [{"matches.shipment_id": {"$in": ids}}, {"matches.accrual_id": {"$in": acc_ids}}]}
    test_sids = await test_shipment_ids(test_ids) if test_ids is not None else set()
    async for ci in _raw(db.carrier_invoices).find(q, NO_ID):
        if test_ids is not None and is_test_record(ci, test_ids, test_sids):
            continue
        ci_ids.append(ci.get("id"))
        counted = ci.get("status") not in ("rejected", "duplicate")
        seen = set()
        for m in ci.get("matches") or []:
            sid = m.get("shipment_id")
            if counted and m.get("accrual_id"):
                ctx["matched_expected"][m["accrual_id"]] += _i(m.get("accrued_eur_cents"))
            if sid in ids and sid not in seen:
                seen.add(sid)
                ctx["cinv"][sid].append(ci)
    if ci_ids:
        async for p in _raw(db.payables).find(scoped({"carrier_invoice_id": {"$in": ci_ids}}), NO_ID):
            ctx["payables"].setdefault(p.get("carrier_invoice_id"), []).append(p)
    return ctx


async def _quotes_for(shipments: list[dict]) -> dict:
    qids = [s.get("quote_id") for s in shipments if s.get("quote_id")]
    out = {}
    if qids:
        async for q in db.quotes.find({"id": {"$in": qids}}, NO_ID):
            out[q["id"]] = q
    return out


def _pnl(s: dict, ctx: dict, quote: dict | None) -> dict:
    sid = s["id"]
    lane = lane_of(s)
    warnings: list[str] = []
    # revenue -------------------------------------------------------------------------------------------
    inv_lines, revenue, fx_ar, issued = [], 0, 0, 0
    revenue_complete = True
    for inv in sorted(ctx["invoices"].get(sid, []), key=lambda x: x.get("issue_date") or x.get("created_at") or ""):
        counted = inv.get("status") not in COUNTED_INVOICE_EXCLUDED and bool(inv.get("number") or inv.get("issue_date"))
        e = invoice_eur(inv)
        if e["warning"]:
            warnings.append(e["warning"])
        if counted:
            revenue_complete = revenue_complete and e["complete"]
            revenue += e["services_net"]
            fx_ar += e["fx_realised"]
            if inv.get("kind") != "credit_note":
                issued += 1
        inv_lines.append({"id": inv.get("id"), "number": inv.get("number"), "kind": inv.get("kind"),
                          "status": inv.get("status"), "issue_date": inv.get("issue_date"),
                          "supply_date": inv.get("supply_date"), "currency": e["currency"],
                          "services_net_cents": _i(inv.get("services_net_cents")),
                          "services_net_eur_cents": e["services_net"] if e["complete"] else None, "pass_through_eur_cents": e["pass_through"],
                          "fx_realised_cents": e["fx_realised"], "counted": counted,
                          "path": f"/ops/billing/{inv.get('id')}"})
    quoted = quoted_figures(quote, s.get("option_id"))
    # cost ----------------------------------------------------------------------------------------------
    acc_lines, cost_actual, cost_accrued = [], 0, 0
    accruals = ctx["accruals"].get(sid, [])
    for acc in accruals:
        c = accrual_cost(acc, ctx["matched_expected"].get(acc.get("id")) if acc.get("status") == "partially_matched" else None)
        cost_actual += c["actual"]
        cost_accrued += c["accrued"]
        sup = acc.get("supplier") or {}
        acc_lines.append({"id": acc.get("id"), "supplier": sup.get("name"), "supplier_kind": sup.get("kind"),
                          "status": acc.get("status"), "currency": acc.get("currency"),
                          "expected_cents": acc.get("expected_cents"),
                          "expected_eur_cents": acc.get("expected_eur_cents"),
                          "actual_eur_cents": acc.get("actual_eur_cents"),
                          "variance_eur_cents": acc.get("variance_eur_cents"),
                          "cost_actual_cents": c["actual"], "cost_accrued_cents": c["accrued"],
                          "own_share": c["own_share"], "path": f"/ops/finance/accruals?shipment_id={sid}"})
    cost_source = "accruals"
    if not accruals and quoted and s.get("status") not in CANCELLED_SHIPMENT:
        cost_accrued = quoted["cost_cents"]
        cost_source = "quote"
    # carrier invoices + realised FX on their payables ---------------------------------------------------
    ci_lines, fx_ap = [], 0
    for ci in ctx["cinv"].get(sid, []):
        ms = [m for m in ci.get("matches") or [] if m.get("shipment_id") == sid]
        mine = sum(_i(m.get("invoiced_eur_cents")) for m in ms)
        all_matched = sum(_i(m.get("invoiced_eur_cents")) for m in ci.get("matches") or [])
        realised = sum(_i(p.get("realised_fx_cents")) for p in ctx["payables"].get(ci.get("id"), []))
        share = _round(Decimal(realised) * Decimal(mine) / Decimal(all_matched)) if all_matched else \
            (realised if len(ci.get("matches") or []) <= 1 else 0)
        fx_ap += share
        sup = ci.get("supplier") or {}
        ci_lines.append({"id": ci.get("id"), "number": ci.get("number"), "supplier": sup.get("name"),
                         "status": ci.get("status"), "invoice_date": ci.get("invoice_date"), "currency": ci.get("currency"),
                         "invoiced_eur_cents": mine, "accrued_eur_cents": sum(_i(m.get("accrued_eur_cents")) for m in ms),
                         "realised_fx_cents": share, "path": f"/ops/finance/carrier-invoices/{ci.get('id')}"})
    # basis ---------------------------------------------------------------------------------------------
    if issued == 0:
        basis = "estimated"
        if quoted:
            revenue = quoted["revenue_cents"]
        else:
            revenue_complete = False
            warnings.append("No invoice and no accepted quote option: revenue unknown")
    elif accruals and all(a.get("status") in SETTLED_ACCRUAL for a in accruals):
        basis = "actual"
    else:
        basis = "partly_actual"
    own_lines = [x for x in ((quote or {}).get("options") or []) if x.get("id") == s.get("option_id")]
    missing_quote_cost = any(ln.get("cost_cents") is None and ln.get("unit_cost_cents") is None
                             for o in own_lines for ln in o.get("lines", [])
                             if not ln.get("is_pass_through") and ln.get("payer") != "third_party")
    incomplete_cost = bool(s.get("finance_cost_incomplete") or missing_quote_cost or
                           any(a.get("status") == "no_booked_rate" or
                               (a.get("status") in ("matched", "partially_matched") and a.get("actual_eur_cents") is None) or
                               (a.get("status") not in SETTLED_ACCRUAL and a.get("expected_eur_cents") is None)
                               for a in accruals) or
                           (not accruals and not quoted))
    if incomplete_cost:
        warnings.append("Purchase cost missing: margin unavailable")
    cost = None if incomplete_cost else cost_actual + cost_accrued
    revenue = revenue if revenue_complete else None
    gp = None if cost is None or revenue is None else revenue - cost
    fx = fx_ar + fx_ap
    variance = None
    if quoted and gp is not None:
        variance = {"revenue_cents": revenue - quoted["revenue_cents"], "cost_cents": cost - quoted["cost_cents"],
                    "margin_cents": gp - quoted["margin_cents"],
                    "margin_pct_points": (None if pct(gp, revenue) is None or quoted["margin_pct"] is None
                                          else round(pct(gp, revenue) - quoted["margin_pct"], 1))}
    return {
        "shipment": {"id": sid, "reference": s.get("reference"), "customer": s.get("customer_name"),
                     "company_id": s.get("company_id"), "lane": lane, "mode": s.get("mode"), "status": s.get("status"),
                     "delivered_at": s.get("delivered_at"), "created_at": s.get("created_at"),
                     "carrier": main_carrier(s, quoted), "path": f"/ops/shipments/{sid}"},
        "period": (shipment_period_day(s) or date.today()).isoformat()[:7],
        "revenue_cents": revenue, "revenue_complete": revenue_complete, "cost_cents": cost, "cost_actual_cents": cost_actual, "cost_accrued_cents": cost_accrued,
        "cost_source": cost_source, "cost_complete": not incomplete_cost, "gross_profit_cents": gp, "margin_pct": pct(gp, revenue),
        "fx_result_cents": fx, "fx_ar_cents": fx_ar, "fx_ap_cents": fx_ap, "net_result_cents": None if gp is None else gp + fx,
        "quoted": ({k: quoted[k] for k in ("revenue_cents", "cost_cents", "margin_cents", "margin_pct", "quote_id",
                                           "quote_number", "option_id")} if quoted else None),
        "variance": variance, "basis": basis, "warnings": warnings,
        "lines": {"invoices": inv_lines, "accruals": acc_lines, "carrier_invoices": ci_lines},
    }


# ------------------------------------------------------------------------------------------- public API
async def shipment_pnl(shipment_id: str) -> dict | None:
    """P&L of one shipment (see module docstring). None when the shipment does not exist."""
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        return None
    ctx = await _context([shipment_id])
    quotes = await _quotes_for([s])
    return _pnl(s, ctx, quotes.get(s.get("quote_id")))


def _period_query(date_from: date, date_to: date, *, lane: str | None = None, company_id: str | None = None) -> dict:
    lo, hi = date_from.isoformat(), (date_to + timedelta(days=1)).isoformat()
    q: dict = {"status": {"$nin": list(CANCELLED_SHIPMENT)},
               "$or": [{"delivered_at": {"$gte": lo, "$lt": hi}},
                       {"delivered_at": {"$in": [None, ""]}, "created_at": {"$gte": lo, "$lt": hi}}]}
    if company_id:
        q["company_id"] = company_id
    if lane:
        try:
            mode, rest = lane.split(":", 1)
            oc, dc = rest.split(">", 1)
            q.update({"origin.country": oc, "destination.country": dc})
            if mode != "multimodal":
                q["mode"] = mode
        except ValueError:
            pass
    return q


async def shipments_pnl(date_from, date_to, *, lane: str | None = None, company_id: str | None = None,
                        basis: str | None = None, limit: int | None = None) -> dict:
    """P&L for every shipment in the period (delivered_at, else created_at), newest first, with totals. Company-wide
    (no company_id) never includes test tenants, whatever the ops toggle; an explicit company_id shows that company."""
    df, dt = to_day(date_from), to_day(date_to)
    q = _period_query(df, dt, lane=lane, company_id=company_id)
    test_ids = None if company_id else await test_company_ids()
    if test_ids is not None:
        q = exclude_test(q, ids=test_ids)
    shipments = await _raw(db.shipments).find(q, NO_ID).to_list(None)
    ctx = await _context([s["id"] for s in shipments], test_ids)
    quotes = await _quotes_for(shipments)
    rows = [_pnl(s, ctx, quotes.get(s.get("quote_id"))) for s in shipments]
    if lane:
        rows = [r for r in rows if r["shipment"]["lane"]["key"] == lane]
    counts = {"actual": 0, "partly_actual": 0, "estimated": 0}
    for r in rows:
        counts[r["basis"]] = counts.get(r["basis"], 0) + 1
    if basis:
        rows = [r for r in rows if r["basis"] == basis]
    rows.sort(key=lambda r: (r["shipment"].get("delivered_at") or r["shipment"].get("created_at") or ""), reverse=True)
    totals = _sum_rows(rows)
    items = rows[:limit] if limit else rows
    return {"items": items, "total": len(rows), "from": df.isoformat(), "to": dt.isoformat(), "totals": totals,
            "basis_counts": counts}


def _sum_rows(rows: list[dict]) -> dict:
    revenue_complete = all(r.get("revenue_cents") is not None for r in rows)
    rev = sum(r["revenue_cents"] for r in rows) if revenue_complete else None
    complete = all(r.get("cost_complete", True) and r.get("cost_cents") is not None for r in rows)
    cost = sum(r["cost_cents"] for r in rows) if complete else None
    act = sum(r["cost_actual_cents"] for r in rows)
    gp = rev - cost if complete and revenue_complete else None
    fx = sum(r["fx_result_cents"] for r in rows)
    quoted_rows = [r for r in rows if r.get("quoted") and r.get("gross_profit_cents") is not None]
    q_margin = sum(r["quoted"]["margin_cents"] for r in quoted_rows)
    q_rev = sum(r["quoted"]["revenue_cents"] for r in quoted_rows)
    a_margin_q = sum(r["gross_profit_cents"] for r in quoted_rows)
    a_rev_q = sum(r["revenue_cents"] for r in quoted_rows)
    n = len(rows)
    return {"shipments": n, "revenue_cents": rev, "revenue_complete": revenue_complete, "cost_cents": cost, "cost_actual_cents": act,
            "cost_complete": complete, "incomplete_shipments": sum(not r.get("cost_complete", True) for r in rows),
            "cost_accrued_cents": None if cost is None else cost - act, "gross_profit_cents": gp, "margin_pct": pct(gp, rev),
            "gp_per_shipment_cents": _round(Decimal(gp) / n) if n and gp is not None else None, "fx_result_cents": fx,
            "net_result_cents": None if gp is None else gp + fx, "quoted_margin_cents": q_margin, "quoted_margin_pct": pct(q_margin, q_rev),
            "margin_variance_cents": a_margin_q - q_margin,
            "margin_variance_pct_points": (None if pct(a_margin_q, a_rev_q) is None or pct(q_margin, q_rev) is None
                                           else round(pct(a_margin_q, a_rev_q) - pct(q_margin, q_rev), 1)),
            "cost_actual_share_pct": pct(act, cost)}


async def lane_pnl(date_from, date_to, *, company_id: str | None = None) -> dict:
    """P&L grouped by lane key (finance_core.lane_of): shipments, revenue, cost, GP, margin %, GP per shipment,
    quoted vs actual margin variance and the share of cost that is actual (invoiced) rather than accrued."""
    base = await shipments_pnl(date_from, date_to, company_id=company_id)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in base["items"]:
        groups[r["shipment"]["lane"]["key"]].append(r)
    items = []
    for key, rows in groups.items():
        lane = rows[0]["shipment"]["lane"]
        basis_counts = {basis: sum(r["basis"] == basis for r in rows)
                        for basis in ("actual", "partly_actual", "estimated")}
        params = {"lane": key, "from": base["from"], "to": base["to"]}
        if company_id:
            params["company_id"] = company_id
        items.append({"basis_counts": basis_counts, "lane": key, "mode": lane["mode"], "origin_country": lane["origin_country"],
                      "destination_country": lane["destination_country"], **_sum_rows(rows),
                      "path": "/ops/finance/pnl?" + urlencode(params)})
    items.sort(key=lambda x: (x["gross_profit_cents"] is not None, x["gross_profit_cents"] or 0), reverse=True)
    return {"items": items, "total": len(items), "from": base["from"], "to": base["to"], "totals": base["totals"]}


MARGIN_GROUPS = ("customer", "carrier", "month", "mode", "lane")


async def margin_report(date_from, date_to, group: str = "customer", *, company_id: str | None = None) -> dict:
    """Margin grouped by customer | carrier (main carriage) | month (YYYY-MM of delivery) | mode | lane."""
    if group not in MARGIN_GROUPS:
        raise ValueError(f"group must be one of {', '.join(MARGIN_GROUPS)}")
    base = await shipments_pnl(date_from, date_to, company_id=company_id)
    groups: dict[str, list[dict]] = defaultdict(list)
    labels: dict[str, str] = {}
    for r in base["items"]:
        s = r["shipment"]
        if group == "customer":
            k, label = s.get("company_id") or "—", s.get("customer") or "—"
        elif group == "carrier":
            k = label = s.get("carrier") or "—"
        elif group == "month":
            k = label = r["period"]
        elif group == "mode":
            k = label = s.get("mode") or "—"
        else:
            k = label = s["lane"]["key"]
        groups[k].append(r)
        labels[k] = label
    items = [{"key": k, "label": labels[k], **_sum_rows(rows)} for k, rows in groups.items()]
    items.sort(key=(lambda x: x["key"]) if group == "month" else (lambda x: -(x["gross_profit_cents"] or 0)))
    return {"items": items, "total": len(items), "group": group, "from": base["from"], "to": base["to"],
            "totals": base["totals"]}


# ------------------------------------------------------------------------------------------- landing page
def _nin(q: dict, ids: list[str], field: str = "company_id") -> dict:
    """Company-wide tile: never test tenants (core.tenant_visibility.exclude_test), whatever the ops toggle."""
    return exclude_test(q, field, ids)


async def overview() -> dict:
    """Numbers for the finance landing page. Every tile carries `path` (the UI link target) and fi/en labels."""
    today = helsinki_now().date()
    tiles: dict[str, dict] = {}
    test_ids = await test_company_ids()
    test_sids = await test_shipment_ids(test_ids)

    # open accruals
    n, s = 0, 0
    async for a in _raw(db.cost_accruals).find(_nin({"status": {"$in": ["open", "partially_matched", "disputed"]}}, test_ids), NO_ID):
        c = accrual_cost(a)
        n += 1
        s += c["accrued"]
    tiles["open_accruals"] = {"label_fi": "Avoimet kuluvaraukset", "label_en": "Open cost accruals", "count": n,
                              "eur_cents": s, "path": "/ops/finance/accruals?status=open"}

    # carrier invoices needing attention
    att = {"needs_review": 0, "disputed": 0}
    eur_att = 0
    async for ci in _raw(db.carrier_invoices).find(_nin({"status": {"$in": list(att)}}, test_ids), NO_ID):
        if is_test_record(ci, test_ids, test_sids):
            continue
        att[ci["status"]] += 1
        eur_att += _i((ci.get("eur") or {}).get("total_cents") or (ci.get("total_cents") if (ci.get("currency") or "EUR") == "EUR" else 0))
    tiles["carrier_invoices_attention"] = {"label_fi": "Ostolaskut: vaatii huomiota",
                                           "label_en": "Carrier invoices needing attention", "count": sum(att.values()),
                                           **att, "eur_cents": eur_att,
                                           "path": "/ops/finance/carrier-invoices?status=needs_review,disputed"}

    # approved payables due within 7 days
    limit = (today + timedelta(days=7)).isoformat()
    by_cur: dict[str, int] = defaultdict(int)
    n, eur_sum = 0, 0
    pq = _nin({"status": {"$in": ["approved", "scheduled"]}, "due_date": {"$lte": limit}}, test_ids)
    async for p in _raw(db.payables).find(pq, NO_ID):
        n += 1
        by_cur[(p.get("currency") or "EUR").upper()] += _i(p.get("amount_cents"))
        eur_sum += _i(p.get("eur_cents") if p.get("eur_cents") is not None else p.get("amount_cents"))
    tiles["payables_due_7d"] = {"label_fi": "Maksettavat 7 päivän sisällä", "label_en": "Payables due within 7 days",
                                "count": n, "by_currency": dict(by_cur), "eur_cents": eur_sum, "until": limit,
                                "path": "/ops/finance/payables?status=approved&due_within_days=7"}

    # AR overdue
    n, due_sum = 0, 0
    async for inv in _raw(db.invoices).find(_nin({"status": "overdue", "kind": "invoice"}, test_ids), NO_ID):
        n += 1
        due = max(0, _i(inv.get("total_cents")) - _i(inv.get("paid_cents")) - _i(inv.get("credited_cents")))
        e = invoice_eur(inv)
        if e["converted"] and _i(inv.get("total_cents")):
            due = _round(Decimal(due) * Decimal(e["total"]) / Decimal(_i(inv.get("total_cents"))))
        due_sum += due
    tiles["ar_overdue"] = {"label_fi": "Erääntyneet myyntisaamiset", "label_en": "Overdue receivables", "count": n,
                           "eur_cents": due_sum, "path": "/ops/billing?status=overdue"}

    # open collections cases
    n, amt = 0, 0
    async for c in _raw(db.collections_cases).find(_nin({"stage": {"$nin": ["paid", "written_off"]}}, test_ids), NO_ID):
        n += 1
        amt += _i(c.get("amount_due_cents")) + _i(c.get("interest_cents")) + _i(c.get("fee_cents"))
    tiles["collections_open"] = {"label_fi": "Avoimet perintätapaukset", "label_en": "Open collections cases",
                                 "count": n, "eur_cents": amt, "path": "/ops/finance/collections"}

    # VAT for the current period (estimate so far)
    try:
        from services.vat_report import current_period, vat_report
        period = await current_period(today)
        rep = await vat_report(period)
        tiles["vat_current"] = {"label_fi": f"ALV {period} (arvio)", "label_en": f"VAT {period} (estimate)",
                                "period": period, "eur_cents": rep["tax_payable_cents"],
                                "payable": rep["tax_payable_cents"] >= 0, "verify": bool(rep.get("verify")),
                                "path": f"/ops/finance/vat?period={period}"}
    except Exception as exc:  # noqa: BLE001 - the landing page must load even if one tile cannot
        log.exception("VAT tile failed")
        tiles["vat_current"] = {"label_fi": "ALV", "label_en": "VAT", "error": str(exc)[:200], "path": "/ops/finance/vat"}

    # gross profit month to date
    first = today.replace(day=1)
    mtd = await shipments_pnl(first, today)
    t = mtd["totals"]
    tiles["gross_profit_mtd"] = {"label_fi": "Myyntikate kuukauden alusta", "label_en": "Gross profit month to date",
                                 "from": first.isoformat(), "to": today.isoformat(), "shipments": t["shipments"],
                                 "revenue_cents": t["revenue_cents"], "cost_cents": t["cost_cents"],
                                 "cost_actual_cents": t["cost_actual_cents"], "cost_accrued_cents": t["cost_accrued_cents"],
                                 "eur_cents": t["gross_profit_cents"], "margin_pct": t["margin_pct"],
                                 "basis_counts": mtd["basis_counts"],
                                 "path": f"/ops/finance/reports/pnl?from={first.isoformat()}&to={today.isoformat()}"}

    # FX
    fx_doc = await db.fx_rates.find_one({}, NO_ID, sort=[("date", -1)])
    meta = await db.system.find_one({"key": "fx_refresh"}, {"_id": 0}) or {}
    tiles["fx"] = {"label_fi": "EKP:n viitekurssit", "label_en": "ECB reference rates",
                   "rate_date": (fx_doc or {}).get("date"), "fetched_at": meta.get("at"),
                   "stale": (not fx_doc) or (today - to_day(fx_doc["date"])).days > 4, "path": "/ops/finance/fx"}

    # ledger export
    last = await db.ledger_exports.find_one({}, NO_ID, sort=[("created_at", -1)])
    tiles["ledger_last_export"] = {"label_fi": "Viimeisin kirjanpitosiirto", "label_en": "Last ledger export",
                                   "id": (last or {}).get("id"), "format": (last or {}).get("format"),
                                   "at": (last or {}).get("created_at"), "status": (last or {}).get("status"),
                                   "from": (last or {}).get("from"), "to": (last or {}).get("to"),
                                   "count": (last or {}).get("count"), "path": "/ops/finance/ledger"}

    # Autopilot
    from core.settings import get_settings
    st = await get_settings()
    tiles["autopilot"] = {"label_fi": "Autopilot", "label_en": "Autopilot", "enabled": bool(st.get("autopilot_enabled")),
                          "halted": await halted(), "active": await autopilot_on(), "path": "/ops/settings#autopilot"}
    order = ["gross_profit_mtd", "ar_overdue", "payables_due_7d", "carrier_invoices_attention", "open_accruals",
             "collections_open", "vat_current", "fx", "ledger_last_export", "autopilot"]
    return {"as_of": now_iso(), "date": today.isoformat(), "tiles": tiles, "order": order}
