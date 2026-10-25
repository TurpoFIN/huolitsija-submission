"""Foreign currency on the receivables and payables side (s13): multi-currency invoicing, realised FX on payment,
month-end revaluation of open foreign-currency items, and the ECB refresh job.

Functional currency is EUR. A customer company can be invoiced in another currency (`companies.invoice_currency`,
ISO 4217, any currency in the latest ECB reference-rate set). For such a customer:

* Draft: every EUR-priced line (quote, extra charges, Tulli duty / import VAT disbursements) is converted with the ECB
  reference rate of the draft date. The EUR originals stay on the line (`eur_unit_price_cents`, `eur_amount_cents`,
  `price_currency: "EUR"`). Lines typed by ops on a foreign-currency draft are taken as invoice-currency amounts
  (`price_currency: <invoice currency>`).
* Issue: EUR-priced lines are converted again with the ECB rate of the issue date, then the amounts are fixed.
* VAT is computed in the invoice currency (billing.compute_totals) and stated in EUR as well (`invoice.eur`), because
  the VAT amount has to be expressed in the national currency at the latest ECB rate (VAT Directive 2006/112/EC
  art. 230 and 91(2); AVL 73 §). The PDF prints the EUR VAT line.
* Payments are recorded in the invoice currency. On `invoice.paid` the settled EUR (the bank's / Stripe's EUR amount
  on the payment row when given, else the ECB rate of the payment date) is compared with the booked EUR total:
  `invoice.fx_realised_cents` (positive = gain).
* Month end (`finance.fx_revalue`): open foreign-currency receivables and payables are revalued at the ECB rate of the
  closing date -> `fx_revaluations` (unrealised; the ledger books them and reverses them on the 1st).

EUR customers never reach any of this: every billing hook is gated on a non-EUR currency.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from core.audit import audit
from core.db import db, register_index
from core.events import on
from core.util import NO_ID, new_id, now_iso
from services import fx
from services.finance_core import AGENT_ACTOR, finance_job, get_settings, helsinki_now

log = logging.getLogger("finance.fx")

register_index("fx_revaluations", "period", unique=True)
register_index("invoices", [("currency", 1), ("status", 1)])

# Currencies shown on the FX screen (latest + 30-day history).
WATCHED = ["USD", "SEK", "NOK", "DKK", "GBP", "CNY", "PLN", "CHF", "JPY"]
AR_OPEN = ("open", "overdue", "processing")
AP_CLOSED = ("paid", "cancelled", "rejected")
# Invoices built from EUR price sources: their lines are converted from EUR. Other origins (manual, dunning fees)
# keep what was entered: dunning compensation is a fixed EUR amount by law (Korkolaki 10 e §).
EUR_PRICED_ORIGINS = ("shipment", "customs_decision")


class FxError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ currencies
async def allowed_currencies() -> list[str]:
    """EUR + every currency in the latest stored ECB publication (fetched once when nothing is stored yet)."""
    doc = await db.fx_rates.find_one({}, NO_ID, sort=[("date", -1)])
    if not doc:
        try:
            await fx.refresh("daily")
        except Exception as exc:  # noqa: BLE001 - surfaced as "no rates" below
            log.warning("ECB refresh failed: %s", exc)
        doc = await db.fx_rates.find_one({}, NO_ID, sort=[("date", -1)])
    return ["EUR"] + sorted((doc or {}).get("rates", {}).keys())


async def set_customer_currency(company_id: str, currency: str, actor: dict) -> dict:
    cur = (currency or "").strip().upper()
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise FxError("Customer company not found", 404)
    allowed = await allowed_currencies()
    if cur not in allowed:
        raise FxError(f"{cur or 'Empty'} is not a currency with an ECB reference rate. Choose one of: "
                      f"{', '.join(allowed)}.", 400)
    before = company.get("invoice_currency") or "EUR"
    await db.companies.update_one({"id": company_id}, {"$set": {"invoice_currency": cur, "updated_at": now_iso()}})
    await audit(actor, "company_invoice_currency_set", "company", company_id, {"before": before, "after": cur},
                company_id=company_id)
    return {"company_id": company_id, "invoice_currency": cur, "before": before}


# ------------------------------------------------------------------------------------------------ invoice helpers
def _fx_doc(r: dict, basis: str) -> dict:
    return {"rate": r["rate"], "rate_date": r["rate_date"], "source": r["source"], "currency": r["currency"],
            "basis": basis, "at": now_iso()}


def eur_block(totals: dict, rate: float) -> dict:
    """Invoice totals expressed in EUR at `rate` (units per EUR). VAT per rate group converted on its own and summed,
    so the EUR VAT equals the EUR VAT breakdown exactly."""
    bd = [{"vat_code": b["vat_code"], "rate_pct": b.get("rate_pct"), "base_cents": fx.convert(b["base_cents"], rate),
           "vat_cents": fx.convert(b["vat_cents"], rate)} for b in totals.get("vat_breakdown") or []]
    subtotal = fx.convert(totals["subtotal_cents"], rate)
    pass_through = fx.convert(totals.get("pass_through_cents") or 0, rate)
    vat = sum(b["vat_cents"] for b in bd)
    return {"subtotal_cents": subtotal, "services_net_cents": subtotal - pass_through,
            "pass_through_cents": pass_through, "vat_cents": vat, "total_cents": subtotal + vat, "vat_breakdown": bd}


def _convert_line(ln: dict, rate: float, currency: str):
    """EUR-priced line -> invoice currency, EUR originals kept. Amount = EUR amount x rate (half-up)."""
    if ln.get("price_currency") != "EUR":
        ln["eur_unit_price_cents"] = int(ln.get("unit_price_cents") or 0)
        ln["eur_amount_cents"] = int(ln.get("amount_cents") or 0)
        ln["price_currency"] = "EUR"
    ln["unit_price_cents"] = fx.from_eur(ln["eur_unit_price_cents"], rate)
    ln["amount_cents"] = fx.from_eur(ln["eur_amount_cents"], rate)
    ln["currency"] = currency
    ln["_recalc"] = False


def _currency_line(ln: dict, rate: float, currency: str):
    """Line entered in the invoice currency: keep the amount, note its EUR value at `rate`."""
    ln["price_currency"] = currency
    ln["currency"] = currency
    ln["eur_amount_cents"] = fx.convert(int(ln.get("amount_cents") or 0), rate)
    ln["_recalc"] = False


async def localise_draft(company: dict, lines: list[dict], *, settings: dict, supply: date, origin: str,
                         credit_note_for: str | None = None, currency: str | None = None,
                         fx_doc: dict | None = None) -> dict:
    """billing._new_invoice / update_draft / customs-decision hook. Returns {} for EUR invoices (nothing changes);
    otherwise converts `lines` in place and returns the fields to store: recomputed totals in the invoice currency,
    currency, fx {rate, rate_date, source}, eur {...}."""
    from services.billing import BillingError, compute_totals

    inherited = None
    if credit_note_for:
        orig = await db.invoices.find_one({"id": credit_note_for}, {"_id": 0, "currency": 1, "fx": 1, "number": 1})
        if not orig or (orig.get("currency") or "EUR") == "EUR" or not orig.get("fx"):
            return {}
        currency, fx_doc, inherited = orig["currency"], orig["fx"], orig.get("number") or credit_note_for
    cur = (currency or company.get("invoice_currency") or "EUR").upper()
    if cur == "EUR" or origin == "dunning":
        return {}
    if fx_doc and fx_doc.get("rate"):
        r = {"rate": fx_doc["rate"], "rate_date": fx_doc["rate_date"], "source": fx_doc.get("source") or fx.SOURCE,
             "currency": cur}
        basis = fx_doc.get("basis") or "draft_date"
    else:
        try:
            r = await fx.eur_rate(cur, helsinki_now().date())
        except fx.FxUnavailable as exc:
            raise BillingError(f"{exc}. The invoice cannot be drafted in {cur} yet.", 409)
        basis = "draft_date"
    for ln in lines:
        if inherited:
            ln["price_currency"], ln["currency"] = cur, cur
            ln["eur_amount_cents"] = fx.convert(int(ln.get("amount_cents") or 0), r["rate"])
            ln["_recalc"] = False
        elif ln.get("price_currency") == "EUR" or (origin in EUR_PRICED_ORIGINS and not ln.get("price_currency")):
            _convert_line(ln, r["rate"], cur)
        else:
            _currency_line(ln, r["rate"], cur)
    totals = compute_totals(lines, on=supply, settings=settings)
    out = {**totals, "currency": cur, "fx": _fx_doc(r, "credit_note_of_original" if inherited else basis),
           "eur": eur_block(totals, r["rate"])}
    if inherited:
        out["fx"]["inherited_from"] = inherited
    return out


async def reconvert_at_issue(inv: dict, issued: date, settings: dict) -> dict:
    """billing.issue_invoice hook for non-EUR invoices: EUR-priced lines are converted again with the ECB rate of the
    issue date, typed lines keep their amounts; totals, VAT and the EUR statement are recomputed and then fixed."""
    from services.billing import BillingError, compute_totals, to_date

    cur = (inv.get("currency") or "EUR").upper()
    if cur == "EUR":
        return {}
    if inv.get("kind") == "credit_note" or (inv.get("fx") or {}).get("inherited_from"):
        return {"fx": {**(inv.get("fx") or {}), "fixed_at": now_iso()}}
    try:
        r = await fx.eur_rate(cur, issued)
    except fx.FxUnavailable as exc:
        raise BillingError(f"{exc}. The invoice cannot be issued in {cur} yet.", 409)
    lines = inv.get("lines") or []
    for ln in lines:
        if ln.get("price_currency") == "EUR":
            _convert_line(ln, r["rate"], cur)
        else:
            _currency_line(ln, r["rate"], cur)
    supply = to_date(inv.get("supply_date")) or issued
    totals = compute_totals(lines, on=supply, settings=settings)
    if inv.get("kind") == "invoice" and totals["total_cents"] <= 0:
        raise BillingError("The invoice total must be above zero")
    fxd = _fx_doc(r, "issue_date")
    fxd["fixed_at"] = now_iso()
    fxd["draft"] = {k: (inv.get("fx") or {}).get(k) for k in ("rate", "rate_date")}
    return {"lines": lines, **totals, "fx": fxd, "eur": eur_block(totals, r["rate"])}


def booked_eur_of(inv: dict, amount_cents: int) -> int:
    """EUR value of `amount_cents` of this invoice at its booked (issue-date) rate."""
    rate = (inv.get("fx") or {}).get("rate")
    if not rate:
        return int(amount_cents)
    total = int(inv.get("total_cents") or 0)
    eur_total = int((inv.get("eur") or {}).get("total_cents") or 0)
    if total and int(amount_cents) == total and eur_total:
        return eur_total
    return fx.convert(int(amount_cents), rate)


# ------------------------------------------------------------------------------------------------ realised FX (AR)
def _pay_date(p: dict) -> str:
    from services.billing import to_date

    d = to_date(p.get("paid_at") or p.get("recorded_at"))
    return (d or helsinki_now().date()).isoformat()


async def realise_invoice(invoice_id: str) -> dict | None:
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv or (inv.get("currency") or "EUR") == "EUR" or inv.get("kind") != "invoice":
        return None
    cur = inv["currency"]
    settled, rows = 0, []
    for p in inv.get("payments") or []:
        if p.get("eur_amount_cents") is not None:
            eur, how = int(p["eur_amount_cents"]), {"source": "bank_or_stripe"}
        else:
            eur, r = await fx.to_eur(int(p["amount_cents"]), cur, _pay_date(p))
            how = {"source": r["source"], "rate": r["rate"], "rate_date": r["rate_date"]}
        settled += eur
        rows.append({"payment_id": p.get("id"), "amount_cents": p["amount_cents"], "settled_eur_cents": eur, **how})
    credited = int(inv.get("credited_cents") or 0)
    booked = int((inv.get("eur") or {}).get("total_cents") or 0) - (booked_eur_of(inv, credited) if credited else 0)
    diff = fx.realised_difference(booked, settled, side="ar")
    detail = {"booked_eur_cents": booked, "settled_eur_cents": settled, "side": "ar", "payments": rows, "at": now_iso()}
    await db.invoices.update_one({"id": invoice_id}, {"$set": {"fx_realised_cents": diff, "fx_realised": detail}})
    for row in rows:
        if row["payment_id"]:
            await db.invoices.update_one({"id": invoice_id, "payments.id": row["payment_id"]},
                                         {"$set": {"payments.$.settled_eur_cents": row["settled_eur_cents"]}})
    await audit(AGENT_ACTOR, "invoice_fx_realised", "invoice", invoice_id,
                {"currency": cur, "fx_realised_cents": diff, **{k: detail[k] for k in ("booked_eur_cents", "settled_eur_cents")}},
                company_id=inv.get("company_id"))
    return {"invoice_id": invoice_id, "fx_realised_cents": diff, **detail}


@on("invoice.paid")
async def _on_invoice_paid(p: dict):
    inv = p.get("invoice") or {}
    if inv.get("id") and (inv.get("currency") or "EUR") != "EUR":
        await realise_invoice(inv["id"])


# ------------------------------------------------------------------------------------------------ revaluation
def _ar_open(inv: dict) -> int:
    return max(0, int(inv.get("total_cents") or 0) - int(inv.get("paid_cents") or 0) - int(inv.get("credited_cents") or 0))


async def revalue(as_of: date, company_id: str | None = None) -> dict:
    """Unrealised FX on open foreign-currency receivables and payables at the ECB rate of `as_of`. One row per period
    (YYYY-MM of as_of); running it again replaces that period's row (same id). With company_id only that company's
    receivables and payables, stored as its own row (scope=company) that never replaces the all-company row."""
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])
    period = as_of.isoformat()[:7]
    rates: dict[str, dict] = {}

    async def rate_of(cur: str) -> dict:
        if cur not in rates:
            rates[cur] = await fx.eur_rate(cur, as_of)
        return rates[cur]

    items, missing = [], []
    only = {"company_id": company_id} if company_id else {}
    async for inv in db.invoices.find({"kind": "invoice", "currency": {"$nin": ["EUR", None]},
                                       "status": {"$in": list(AR_OPEN)}, **only}, NO_ID):
        open_cents = _ar_open(inv)
        if open_cents <= 0:
            continue
        try:
            r = await rate_of(inv["currency"])
        except fx.FxUnavailable:
            missing.append(inv["currency"])
            continue
        booked = booked_eur_of(inv, open_cents)
        reval = fx.convert(open_cents, r["rate"])
        items.append({"type": "ar", "id": inv["id"], "number": inv.get("number"), "company_id": inv.get("company_id"),
                      "currency": inv["currency"], "open_cents": open_cents, "booked_rate": (inv.get("fx") or {}).get("rate"),
                      "rate": r["rate"], "booked_eur_cents": booked, "revalued_eur_cents": reval,
                      "diff_cents": fx.realised_difference(booked, reval, side="ar")})
    async for pay in db.payables.find({"currency": {"$nin": ["EUR", None]}, "status": {"$nin": list(AP_CLOSED)}, **only},
                                      NO_ID):
        open_cents = int(pay.get("amount_cents") or 0)
        if open_cents <= 0:
            continue
        try:
            r = await rate_of(pay["currency"])
        except fx.FxUnavailable:
            missing.append(pay["currency"])
            continue
        booked = int(pay.get("eur_cents") or 0)
        reval = fx.convert(open_cents, r["rate"])
        items.append({"type": "ap", "id": pay["id"], "number": (pay.get("supplier") or {}).get("name"),
                      "company_id": pay.get("company_id"), "invoice_number": pay.get("invoice_number"),
                      "currency": pay["currency"], "open_cents": open_cents, "rate": r["rate"],
                      "booked_eur_cents": booked, "revalued_eur_cents": reval,
                      "diff_cents": fx.realised_difference(booked, reval, side="ap")})
    gain = sum(i["diff_cents"] for i in items if i["diff_cents"] > 0)
    loss = -sum(i["diff_cents"] for i in items if i["diff_cents"] < 0)
    rate_date = max((r["rate_date"] for r in rates.values()), default=None)
    # `period` is unique (index): a company row is keyed "YYYY-MM/<company_id>"; period_month keeps the month.
    row_period = f"{period}/{company_id}" if company_id else period
    key = {"period": row_period}
    existing = await db.fx_revaluations.find_one(key, {"_id": 0, "id": 1, "created_at": 1, "runs": 1})
    doc = {"id": (existing or {}).get("id") or new_id(), "period": row_period, "period_month": period,
           "as_of": as_of.isoformat(),
           "company_id": company_id, "scope": "company" if company_id else "all",
           "rate_date": rate_date, "rates": {c: r["rate"] for c, r in rates.items()}, "items": items,
           "gain_cents": gain, "loss_cents": loss, "net_cents": gain - loss, "missing_rates": sorted(set(missing)),
           "source": fx.SOURCE, "created_at": (existing or {}).get("created_at") or now_iso(), "updated_at": now_iso(),
           "runs": int((existing or {}).get("runs") or 0) + 1}
    await db.fx_revaluations.replace_one(key, dict(doc), upsert=True)
    await audit(AGENT_ACTOR, "fx_revaluation_run", "fx_revaluation", doc["id"],
                {"period": period, "as_of": doc["as_of"], "items": len(items), "gain_cents": gain, "loss_cents": loss})
    return doc


# ------------------------------------------------------------------------------------------------ jobs
# ECB publishes around 16:00 CET (17:00 Helsinki), so the job runs at 17:30 Helsinki. eur_rate() also fetches on
# demand whenever the stored set is stale, so a booking never waits for this job.
@finance_job("finance.fx_refresh", daily_at="17:30", label_fi="EKP:n valuuttakurssit", label_en="ECB exchange rates")
async def fx_refresh_job(j: dict):
    out = {}
    if not await db.fx_rates.find_one({}, {"_id": 1}):
        out["90d"] = await fx.refresh("90d")
    out["daily"] = await fx.refresh("daily")
    return out


@finance_job("finance.fx_revalue", daily_at="23:00", label_fi="Valuuttaerien arvostus kuun lopussa",
             label_en="Month-end FX revaluation")
async def fx_revalue_job(j: dict):
    today = helsinki_now().date()
    forced = (j.get("payload") or {}).get("as_of")
    if forced:
        return await _summary(await revalue(date.fromisoformat(forced[:10])))
    if (today + timedelta(days=1)).day != 1:
        return {"skipped": "not_month_end", "date": today.isoformat()}
    if not (await get_settings()).get("fx_revalue_month_end", True):
        return {"skipped": "disabled"}
    return await _summary(await revalue(today))


async def _summary(doc: dict) -> dict:
    return {k: doc[k] for k in ("id", "period", "as_of", "rate_date", "gain_cents", "loss_cents")} | {"items": len(doc["items"])}


async def rates_view() -> dict:
    """Latest ECB set + 30 days of history for the watched currencies."""
    latest = await fx.latest_rates()
    since = (helsinki_now().date() - timedelta(days=30)).isoformat()
    hist = await db.fx_rates.find({"date": {"$gte": since}}, NO_ID).sort("date", 1).to_list(60)
    history = {c: [{"date": d["date"], "rate": (d.get("rates") or {}).get(c)} for d in hist if c in (d.get("rates") or {})]
               for c in WATCHED}
    meta = await db.system.find_one({"key": "fx_refresh"}, {"_id": 0}) or {}
    return {"date": latest.get("date"), "source": latest.get("source"), "fetched_at": latest.get("fetched_at"),
            "feed": latest.get("feed"), "base": "EUR",
            "latest": {c: latest["rates"].get(c) for c in WATCHED},
            "rates": latest.get("rates") or {}, "history": history, "currencies": WATCHED, "last_refresh": meta}
