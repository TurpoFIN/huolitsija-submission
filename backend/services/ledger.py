"""Double-entry journal (s13) and export to the accounting system.

`build_entries(date_from, date_to)` turns business events into balanced journal entries in `ledger_entries`, idempotent
per (source, source_id, kind) — running it again never books twice. Accounts come from finance_core.ledger_accounts()
(Finnish chart, overridable with LEDGER_ACCOUNTS_JSON). Every entry must balance (sum debit == sum credit); an entry
that does not is refused, logged and reported back, never stored.

Sources and entries (EUR cents, foreign amounts through the EUR breakdown at the ECB rate):
- invoice (issued customer invoice, date = issue date): Dr receivables total; Cr sales account per VAT code net;
  Cr vat_payable VAT; pass-through disbursement lines Cr disbursements_receivable. Credit notes are the mirror image.
  An issued invoice that is later voided gets a reversing entry on the void date.
- payment (customer payment on an invoice, date = paid_at): Dr bank, Cr receivables; foreign currency: the receivable is
  cleared at the booked EUR amount and the realised difference goes to fx_gain / fx_loss. Refunds the other way.
- carrier_invoice (status approved|scheduled|paid, date = invoice date): Dr freight_costs | freight_costs_eu_rc |
  freight_costs_non_eu net EUR (split per matched shipment for the dimensions); Dr vat_receivable deductible VAT;
  reverse charge with tax: Dr vat_receivable + Cr vat_rc_payable (tax from services.vat_report.purchase_vat, the same
  function the VAT return uses); Cr payables total EUR.
- supplier_payment (payable paid): Dr payables (booked EUR), Cr bank (EUR paid), realised FX to fx_gain / fx_loss.
- accrual / accrual_reversal: month end, every cost accrual still open: Dr freight_costs, Cr accrued_expenses on the
  last day of the month; the reversal on the 1st of the next month (standard reversing-entry method, so the carrier
  invoice booked later is not counted twice).
- fx_revaluation: unrealised FX on open foreign receivables / payables at the period end, reversed on the 1st of the
  next period.
- collections: fees and interest from collections_cases once charged (not when billed through a separate invoice,
  which is booked as an invoice).

Exports: see `export()` — formats `procountor`, `netvisor`, `csv` (LEDGER_EXPORT_FORMAT); LEDGER_API pushes to the
vendor API (off by default; see services.ledger_formats docstrings in this module for the specs and what is proven).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.db import db, register_index
from core.tenant_visibility import TEST_FLAGS, exclude_test, is_test_record, test_company_ids, test_shipment_ids
from core.util import NO_ID, new_id, now_iso
from services.finance_core import lane_of, ledger_accounts

log = logging.getLogger("finance.ledger")

register_index("ledger_entries", [("source", 1), ("source_id", 1), ("kind", 1)], unique=True)
register_index("ledger_entries", "date")
register_index("ledger_entries", "id", unique=True)
register_index("ledger_exports", "id", unique=True)

ACCOUNT_NAMES = {
    "receivables": ("Myyntisaamiset", "Trade receivables"),
    "accrued_income": ("Siirtosaamiset", "Accrued income"),
    "vat_receivable": ("Arvonlisäverosaamiset", "VAT receivable"),
    "disbursements_receivable": ("Läpikulkuerät", "Disbursements receivable"),
    "bank": ("Pankkitili", "Bank account"),
    "payables": ("Ostovelat", "Trade payables"),
    "accrued_expenses": ("Siirtovelat (kuluvaraukset)", "Accrued expenses"),
    "vat_payable": ("Arvonlisäverovelka", "VAT payable"),
    "vat_rc_payable": ("ALV-velka, käännetty verovelvollisuus", "VAT payable, reverse charge"),
    "sales_fi": ("Myynti 25,5 %", "Sales 25.5 %"),
    "sales_zero_intl": ("Myynti 0 %, kansainvälinen kuljetus", "Sales 0 %, international transport"),
    "sales_zero_export": ("Myynti 0 %, vienti", "Sales 0 %, export"),
    "sales_eu_rc": ("Palvelumyynti EU, käännetty verovelvollisuus", "Service sales EU, reverse charge"),
    "sales_non_eu": ("Palvelumyynti EU:n ulkopuolelle", "Service sales outside the EU"),
    "freight_costs": ("Rahdit ja kuljetusostot", "Freight and transport costs"),
    "freight_costs_eu_rc": ("Kuljetusostot EU, käännetty verovelvollisuus", "Transport purchases EU, reverse charge"),
    "freight_costs_non_eu": ("Kuljetusostot EU:n ulkopuolelta", "Transport purchases outside the EU"),
    "collection_fees_income": ("Perintä- ja muistutusmaksut", "Collection and reminder fees"),
    "credit_losses": ("Luottotappiot", "Credit losses"),
    "fx_gain": ("Kurssivoitot", "Exchange rate gains"),
    "fx_loss": ("Kurssitappiot", "Exchange rate losses"),
}

SALES_ACCOUNT_BY_VAT = {
    "FI_25_5": "sales_fi", "FI_14": "sales_fi", "FI_13_5": "sales_fi", "FI_10": "sales_fi",
    "ZERO_INTL_TRANSPORT": "sales_zero_intl", "ZERO_EXPORT": "sales_zero_export", "EU_ICS_RC": "sales_eu_rc",
    "OUT_OF_SCOPE_FOREIGN": "sales_non_eu", "OUT_OF_SCOPE_DISBURSEMENT": "disbursements_receivable",
}
COST_ACCOUNT_BY_TREATMENT = {"domestic": "freight_costs", "zero_rated": "freight_costs",
                             "eu_reverse_charge": "freight_costs_eu_rc", "non_eu_reverse_charge": "freight_costs_non_eu"}
APPROVED_CI = ("approved", "scheduled", "paid")


class Unbalanced(Exception):
    pass


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _day(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def account_name(number: str, locale: str = "fi") -> str:
    acc = ledger_accounts()
    for key, num in acc.items():
        if num == number:
            names = ACCOUNT_NAMES.get(key)
            return (names[0] if locale == "fi" else names[1]) if names else key
    return number


# ------------------------------------------------------------------------------------------- entry building
class Entry:
    """Collects signed postings: +amount on the normal side given, negative amounts flip to the other side."""

    def __init__(self, *, source: str, source_id: str, kind: str, day: date, description: str,
                 dimension: dict | None = None, company_id: str | None = None, currency_info: dict | None = None):
        self.source, self.source_id, self.kind, self.day = source, source_id, kind, day
        self.description = description[:240]
        self.dimension = dimension or {}
        self.company_id = company_id
        self.meta = currency_info or {}
        self.lines: list[dict] = []
        self.acc = ledger_accounts()

    def _add(self, key: str, debit: int, credit: int, vat_code: str | None, dimension: dict | None, rate=None):
        if not debit and not credit:
            return
        ln = {"account": self.acc.get(key, key), "account_key": key, "debit_cents": int(debit),
              "credit_cents": int(credit), "vat_code": vat_code, "dimension": {**self.dimension, **(dimension or {})}}
        if rate is not None:
            ln["vat_rate_pct"] = float(rate)
        self.lines.append(ln)

    def dr(self, key: str, cents: int, vat_code: str | None = None, dimension: dict | None = None, rate=None):
        cents = int(cents)
        self._add(key, cents if cents > 0 else 0, -cents if cents < 0 else 0, vat_code, dimension, rate)

    def cr(self, key: str, cents: int, vat_code: str | None = None, dimension: dict | None = None, rate=None):
        cents = int(cents)
        self._add(key, -cents if cents < 0 else 0, cents if cents > 0 else 0, vat_code, dimension, rate)

    def doc(self) -> dict:
        d = sum(ln["debit_cents"] for ln in self.lines)
        c = sum(ln["credit_cents"] for ln in self.lines)
        if d != c:
            raise Unbalanced(f"{self.source}/{self.source_id}/{self.kind}: debit {d} != credit {c}")
        return {"id": new_id(), "date": self.day.isoformat(), "period": self.day.isoformat()[:7], "source": self.source,
                "source_id": self.source_id, "kind": self.kind, "lines": self.lines, "description": self.description,
                "total_cents": d, "company_id": self.company_id, "dimension": self.dimension, **self.meta,
                "export_batch_id": None, "exported_at": None, "created_at": now_iso()}


async def _store(entry: Entry, result: dict):
    try:
        doc = entry.doc()
    except Unbalanced as exc:
        log.error("ledger entry refused: %s lines=%s", exc, entry.lines)
        result["rejected"].append({"source": entry.source, "source_id": entry.source_id, "kind": entry.kind,
                                   "error": str(exc)})
        return
    if not doc["lines"]:
        result["skipped"] += 1
        return
    res = await db.ledger_entries.update_one({"source": doc["source"], "source_id": doc["source_id"], "kind": doc["kind"]},
                                             {"$setOnInsert": doc}, upsert=True)
    if res.upserted_id is not None:
        result["created"] += 1
        result["by_source"][doc["source"]] += 1
    else:
        result["existing"] += 1


async def _shipment_dims(shipment_ids: set[str]) -> dict:
    out = {}
    ids = [x for x in shipment_ids if x]
    if ids:
        async for s in _raw("shipments").find({"id": {"$in": ids}}, NO_ID):
            out[s["id"]] = {"shipment_ref": s.get("reference"), "lane": lane_of(s)["key"]}
    return out


def _raw(name: str):
    """Unfiltered collection (the journal covers every company except test tenants; the ops toggle is a UI filter)."""
    c = db[name]
    return getattr(c, "raw", c)


async def _test_scope() -> dict:
    """Test-tenant data the company journal and every export leave out, whatever the ops toggle and with or without a
    request: test companies, their shipments, carrier invoices that are test (own flag, test company, or matched only to
    test-tenant shipments) and the payables of those."""
    ids = await test_company_ids()
    sids = await test_shipment_ids(ids)
    flags = [{f: True} for f in TEST_FLAGS]
    ci_ids = {ci["id"] async for ci in _raw("carrier_invoices").find(
        {"$or": [*flags, {"company_id": {"$in": ids}}, {"matches.shipment_id": {"$in": list(sids)}}]},
        {"_id": 0, "id": 1, "company_id": 1, "matches": 1, **{f: 1 for f in TEST_FLAGS}}) if is_test_record(ci, ids, sids)}
    pay_ids = {p["id"] async for p in _raw("payables").find(
        {"$or": [*flags, {"company_id": {"$in": ids}}, {"carrier_invoice_id": {"$in": list(ci_ids)}}]}, {"_id": 0, "id": 1})}
    return {"ids": ids, "sids": sids, "ci_ids": ci_ids, "pay_ids": pay_ids}


def _without_test_entries(q: dict, ts: dict) -> dict:
    """Journal entries that are not test-tenant entries (also ones booked before test tenants were left out)."""
    return {"$and": [q, {"company_id": {"$nin": ts["ids"]}},
                     {"source_id": {"$nin": sorted(ts["ci_ids"] | ts["pay_ids"])}}]}


def _invoice_groups_eur(inv: dict) -> tuple[list[dict], int]:
    """[{vat_code, rate, base, vat}] in EUR, signed as booked (credit notes negative), and the EUR total. Same groups
    as the VAT return (services.vat_report.invoice_vat_groups)."""
    from services.vat_report import invoice_vat_groups
    groups = [{"vat_code": g["vat_code"], "rate": g["rate_pct"], "base": g["base_cents"], "vat": g["vat_cents"]}
              for g in invoice_vat_groups(inv)]
    return groups, sum(g["base"] + g["vat"] for g in groups)


async def _book_invoices(df: date, dt: date, result: dict, ts: dict | None = None):
    ts = ts or await _test_scope()
    lo, hi = df.isoformat(), dt.isoformat()
    q = {"issue_date": {"$gte": lo, "$lte": hi}, "number": {"$nin": [None, ""]}}
    invs = await _raw("invoices").find(exclude_test(q, ids=ts["ids"]), NO_ID).to_list(None)
    voided = await _raw("invoices").find(exclude_test({"status": "void", "number": {"$nin": [None, ""]},
                                                       "voided_at": {"$gte": lo, "$lt": (dt + timedelta(days=1)).isoformat()}},
                                                      ids=ts["ids"]), NO_ID).to_list(None)
    dims = await _shipment_dims({i.get("shipment_id") for i in invs + voided})
    for inv in invs:
        groups, total = _invoice_groups_eur(inv)
        dim = dims.get(inv.get("shipment_id"), {})
        cn = inv.get("kind") == "credit_note"
        e = Entry(source="invoice", source_id=inv["id"], kind="credit_note" if cn else "issue", day=_day(inv["issue_date"]),
                  description=f"{'Hyvityslasku' if cn else 'Myyntilasku'} {inv.get('number')} {inv.get('company_name') or ''}",
                  dimension=dim, company_id=inv.get("company_id"),
                  currency_info={"document_number": inv.get("number"), "currency": inv.get("currency") or "EUR"})
        _post_invoice(e, groups, total)
        await _store(e, result)
    for inv in voided:
        groups, total = _invoice_groups_eur(inv)
        e = Entry(source="invoice", source_id=inv["id"], kind="void", day=_day(inv["voided_at"]),
                  description=f"Mitätöity lasku {inv.get('number')}", dimension=dims.get(inv.get("shipment_id"), {}),
                  company_id=inv.get("company_id"), currency_info={"document_number": inv.get("number")})
        _post_invoice(e, [{**g, "base": -g["base"], "vat": -g["vat"]} for g in groups], -total)
        await _store(e, result)


def _post_invoice(e: Entry, groups: list[dict], total: int):
    e.dr("receivables", total)
    for g in groups:
        e.cr(SALES_ACCOUNT_BY_VAT.get(g["vat_code"], "sales_fi"), g["base"], vat_code=g["vat_code"], rate=g.get("rate"))
        if g["vat"]:
            e.cr("vat_payable", g["vat"], vat_code=g["vat_code"], rate=g.get("rate"))


async def _book_payments(df: date, dt: date, result: dict, ts: dict | None = None):
    ts = ts or await _test_scope()
    lo, hi = df.isoformat(), (dt + timedelta(days=1)).isoformat()
    q = {"$or": [{"payments.paid_at": {"$gte": lo, "$lt": hi}}, {"refunds.at": {"$gte": lo, "$lt": hi}},
                 {"fx_realised_at": {"$gte": lo, "$lt": hi}}]}
    invs = await _raw("invoices").find(exclude_test(q, ids=ts["ids"]), NO_ID).to_list(None)
    dims = await _shipment_dims({i.get("shipment_id") for i in invs})
    for inv in invs:
        dim = dims.get(inv.get("shipment_id"), {})
        cur = (inv.get("currency") or "EUR").upper()
        total = _i(inv.get("total_cents"))
        eur_total = abs(_i((inv.get("eur") or {}).get("total_cents"))) if cur != "EUR" else abs(total)
        cn = inv.get("kind") == "credit_note"
        for p in inv.get("payments") or []:
            day = _day(p.get("paid_at"))
            if not day or not (df <= day <= dt):
                continue
            amt = _i(p.get("amount_cents"))
            booked = amt if cur == "EUR" or not total else _round(Decimal(amt) * Decimal(eur_total) / Decimal(abs(total)))
            bank = _i(p.get("eur_cents")) if p.get("eur_cents") is not None else booked
            fx = bank - booked if p.get("eur_cents") is not None else 0
            e = Entry(source="payment", source_id=inv["id"], kind=f"payment:{p.get('key') or p.get('id')}", day=day,
                      description=f"{'Hyvityksen palautus' if cn else 'Asiakasmaksu'} {inv.get('number')} {p.get('ref') or ''}",
                      dimension=dim, company_id=inv.get("company_id"),
                      currency_info={"document_number": inv.get("number"), "currency": cur})
            if cn:  # paying a credit back to the customer
                e.dr("receivables", booked)
                e.cr("bank", bank)
                e.dr("fx_loss", fx) if fx > 0 else e.cr("fx_gain", -fx)
            else:
                e.dr("bank", bank)
                e.cr("receivables", booked)
                if fx > 0:
                    e.cr("fx_gain", fx)
                elif fx < 0:
                    e.dr("fx_loss", -fx)
            await _store(e, result)
        for r in inv.get("refunds") or []:
            day = _day(r.get("at"))
            if not day or not (df <= day <= dt):
                continue
            e = Entry(source="payment", source_id=inv["id"], kind=f"refund:{r.get('key')}", day=day,
                      description=f"Palautus {inv.get('number')}", dimension=dim, company_id=inv.get("company_id"),
                      currency_info={"document_number": inv.get("number")})
            e.dr("receivables", _i(r.get("amount_cents")))
            e.cr("bank", _i(r.get("amount_cents")))
            await _store(e, result)
        # realised FX recorded on the invoice without a per-payment EUR amount (the payment was booked at the
        # invoice's EUR rate above, so the bank difference is booked here)
        fxr = _i(inv.get("fx_realised_cents"))
        if fxr and not any(p.get("eur_cents") is not None for p in inv.get("payments") or []):
            day = _day(inv.get("fx_realised_at") or inv.get("paid_at"))
            if day and df <= day <= dt:
                e = Entry(source="fx_realised", source_id=inv["id"], kind="ar", day=day,
                          description=f"Toteutunut kurssiero {inv.get('number')}", dimension=dim,
                          company_id=inv.get("company_id"))
                if fxr > 0:
                    e.dr("bank", fxr)
                    e.cr("fx_gain", fxr)
                else:
                    e.dr("fx_loss", -fxr)
                    e.cr("bank", -fxr)
                await _store(e, result)


async def _book_carrier_invoices(df: date, dt: date, result: dict, ts: dict | None = None):
    from services.vat_report import purchase_vat_for
    ts = ts or await _test_scope()
    q = {"status": {"$in": list(APPROVED_CI)}, "invoice_date": {"$gte": df.isoformat(), "$lte": dt.isoformat()},
         "id": {"$nin": sorted(ts["ci_ids"])}}
    cis = await _raw("carrier_invoices").find(q, NO_ID).to_list(None)
    for ci in cis:
        pv = await purchase_vat_for(ci)
        if pv["treatment"] == "domestic":
            ev = (ci.get("eur") or {}) or {"net_cents": ci.get("net_cents"), "vat_cents": ci.get("vat_cents")}
            rate = _snap_rate(_i(ev.get("vat_cents")), _i(ev.get("net_cents")))
        else:
            rate = pv["rc_rate_pct"]
        sup = ci.get("supplier") or {}
        cost_key = COST_ACCOUNT_BY_TREATMENT.get(pv["treatment"], "freight_costs")
        e = Entry(source="carrier_invoice", source_id=ci["id"], kind="approved", day=_day(ci.get("invoice_date")),
                  description=f"Ostolasku {ci.get('number') or ''} {sup.get('name') or ''}",
                  currency_info={"document_number": ci.get("number"), "currency": ci.get("currency") or "EUR",
                                 "supplier": sup.get("name")})
        # payables = what we owe in EUR; the cost is that minus the deductible VAT on the invoice (domestic), so the
        # entry balances even when the supplier's EUR total and net + VAT differ by a rounding cent
        net = pv["total_payable_eur_cents"] - (pv["deductible_vat_cents"] if pv["treatment"] == "domestic" else 0)
        # split the net cost over the matched shipments (dimensions), remainder undimensioned
        matches = [m for m in ci.get("matches") or [] if _i(m.get("invoiced_eur_cents"))]
        msum = sum(_i(m.get("invoiced_eur_cents")) for m in matches)
        allocated = 0
        if matches and msum:
            dims = await _shipment_dims({m.get("shipment_id") for m in matches})
            for k, m in enumerate(matches):
                part = _round(Decimal(net) * Decimal(_i(m.get("invoiced_eur_cents"))) / Decimal(msum)) \
                    if k < len(matches) - 1 else net - allocated
                allocated += part
                d = dims.get(m.get("shipment_id")) or {"shipment_ref": m.get("shipment_ref"), "lane": None}
                e.dr(cost_key, part, vat_code=pv["vat_code"], dimension=d, rate=rate)
        else:
            e.dr(cost_key, net, vat_code=pv["vat_code"], rate=rate)
        e.dr("vat_receivable", pv["deductible_vat_cents"], vat_code=pv["vat_code"])
        if pv["rc_tax_cents"]:
            e.cr("vat_rc_payable", pv["rc_tax_cents"], vat_code=pv["vat_code"])
        e.cr("payables", pv["total_payable_eur_cents"])
        await _store(e, result)


def _snap_rate(vat: int, net: int) -> float:
    if not net or not vat:
        return 0.0
    r = float(Decimal(vat) * 100 / Decimal(net))
    return min((25.5, 24.0, 14.0, 13.5, 10.0), key=lambda k: abs(k - r))


async def _book_supplier_payments(df: date, dt: date, result: dict, ts: dict | None = None):
    ts = ts or await _test_scope()
    lo, hi = df.isoformat(), (dt + timedelta(days=1)).isoformat()
    async for p in _raw("payables").find({"status": "paid", "paid_at": {"$gte": lo, "$lt": hi},
                                          "id": {"$nin": sorted(ts["pay_ids"])}}, NO_ID):
        booked = _i(p.get("eur_cents") if p.get("eur_cents") is not None else p.get("amount_cents"))
        fx = _i(p.get("realised_fx_cents"))  # positive = gain (we paid fewer EUR than booked)
        bank = booked - fx
        sup = p.get("supplier") or {}
        e = Entry(source="supplier_payment", source_id=p["id"], kind="paid", day=_day(p["paid_at"]),
                  description=f"Toimittajamaksu {sup.get('name') or ''} {p.get('end_to_end_id') or ''}",
                  currency_info={"currency": p.get("currency") or "EUR", "supplier": sup.get("name")})
        e.dr("payables", booked)
        e.cr("bank", bank)
        if fx > 0:
            e.cr("fx_gain", fx)
        elif fx < 0:
            e.dr("fx_loss", -fx)
        await _store(e, result)


def _month_ends(df: date, dt: date, today: date) -> list[date]:
    out = []
    y, m = df.year, df.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        end = nxt - timedelta(days=1)
        if end > dt:
            break
        if end >= df and end < today:
            out.append(end)
        y, m = nxt.year, nxt.month
    return out


def _settled_after(acc: dict, end: date) -> bool:
    for k in ("matched_at", "released_at", "written_off_at", "settled_at", "updated_at"):
        d = _day(acc.get(k))
        if d:
            return d > end
    return False


async def _book_accruals(df: date, dt: date, result: dict, today: date, ts: dict | None = None):
    from services.finance_reports import accrual_cost
    ts = ts or await _test_scope()
    for end in _month_ends(df, dt, today):
        first_next = end + timedelta(days=1)
        hi = first_next.isoformat()
        q = exclude_test({"created_at": {"$lt": hi}, "shipment_id": {"$nin": sorted(ts["sids"])}}, ids=ts["ids"])
        async for acc in _raw("cost_accruals").find(q, NO_ID):
            status = acc.get("status") or "open"
            if status in ("open", "disputed", "partially_matched"):
                amount = accrual_cost(acc)["accrued"] if status == "partially_matched" else _i(acc.get("expected_eur_cents"))
            elif status in ("matched", "released", "written_off") and _settled_after(acc, end):
                amount = _i(acc.get("expected_eur_cents"))
            else:
                continue
            if not amount:
                continue
            lane = (acc.get("lane") or {}).get("key")
            dim = {"shipment_ref": acc.get("shipment_ref"), "lane": lane}
            sup = (acc.get("supplier") or {}).get("name") or ""
            share = _own_share(acc)
            own = _round(Decimal(amount) * share)
            pt = amount - own
            period = end.isoformat()[:7]
            e = Entry(source="accrual", source_id=acc["id"], kind=f"accrual:{period}", day=end,
                      description=f"Kuluvaraus {period} {acc.get('shipment_ref') or ''} {sup}", dimension=dim,
                      company_id=acc.get("company_id"))
            e.dr("freight_costs", own)
            e.dr("disbursements_receivable", pt)
            e.cr("accrued_expenses", amount)
            await _store(e, result)
            r = Entry(source="accrual_reversal", source_id=acc["id"], kind=f"reversal:{period}", day=first_next,
                      description=f"Kuluvarauksen peruutus {period} {acc.get('shipment_ref') or ''} {sup}", dimension=dim,
                      company_id=acc.get("company_id"))
            r.dr("accrued_expenses", amount)
            r.cr("freight_costs", own)
            r.cr("disbursements_receivable", pt)
            await _store(r, result)


def _own_share(acc: dict) -> Decimal:
    from services.finance_reports import _accrual_own_share
    return _accrual_own_share(acc)


async def _book_revaluations(df: date, dt: date, result: dict, ts: dict | None = None):
    ts = ts or await _test_scope()
    async for rv in db.fx_revaluations.find({}, NO_ID):
        as_of = _day(rv.get("as_of")) or _day(rv.get("rate_date"))
        if not as_of or not (df <= as_of <= dt):
            continue
        e = Entry(source="fx_revaluation", source_id=rv["id"], kind="revaluation", day=as_of,
                  description=f"Valuuttaerien arvostus {rv.get('period') or as_of.isoformat()[:7]} (EKP {rv.get('rate_date') or ''})")
        r = Entry(source="fx_revaluation", source_id=rv["id"], kind="reversal", day=as_of + timedelta(days=1),
                  description=f"Valuuttaerien arvostuksen peruutus {rv.get('period') or ''}")
        for it in rv.get("items") or []:
            if it.get("company_id") in ts["ids"] or ((it.get("type") or "ar") == "ap" and it.get("id") in ts["pay_ids"]):
                continue  # test-tenant receivable / payable
            if it.get("revalued_eur_cents") is not None and it.get("booked_eur_cents") is not None:
                diff = _i(it["revalued_eur_cents"]) - _i(it["booked_eur_cents"])
            else:
                diff = _i(it.get("diff_cents"))
            if not diff:
                continue
            if (it.get("type") or "ar") == "ar":  # receivable worth more EUR = gain
                e.dr("receivables", diff)
                (e.cr("fx_gain", diff) if diff > 0 else e.dr("fx_loss", -diff))
                r.cr("receivables", diff)
                (r.dr("fx_gain", diff) if diff > 0 else r.cr("fx_loss", -diff))
            else:  # payable worth more EUR = loss
                e.cr("payables", diff)
                (e.dr("fx_loss", diff) if diff > 0 else e.cr("fx_gain", -diff))
                r.dr("payables", diff)
                (r.cr("fx_loss", diff) if diff > 0 else r.dr("fx_gain", -diff))
        await _store(e, result)
        if e.lines:
            await _store(r, result)


async def _book_collections(df: date, dt: date, result: dict, ts: dict | None = None):
    acc = ledger_accounts()
    ts = ts or await _test_scope()
    async for c in _raw("collections_cases").find(exclude_test({}, ids=ts["ids"]), NO_ID):
        if c.get("fee_invoice_id"):  # charged through a billing invoice: booked as that invoice
            continue
        charged = c.get("fees_charged_at") or c.get("fee_charged_at") or next(
            (a.get("at") for a in c.get("actions") or [] if a.get("action") in ("fee_charged", "fees_charged",
                                                                                "final_demand", "final_demand_sent")), None)
        day = _day(charged)
        if not day or not (df <= day <= dt):
            continue
        fee, interest = _i(c.get("fee_cents")), _i(c.get("interest_cents"))
        booked = await db.ledger_entries.find({"source": "collections", "source_id": c["id"]}, NO_ID).to_list(None)
        done_fee = sum(ln["credit_cents"] - ln["debit_cents"] for b in booked for ln in b["lines"] if ln.get("account_key") == "collection_fees_income")
        done_int = sum(ln["credit_cents"] - ln["debit_cents"] for b in booked for ln in b["lines"] if ln.get("account_key") == "interest_income")
        dfee, dint = fee - done_fee, interest - done_int
        if not dfee and not dint:
            continue
        when = day if not booked else min(max(_day(now_iso()), df), dt)
        e = Entry(source="collections", source_id=c["id"], kind=f"fees:{fee}:{interest}", day=when, description=f"Perintäkulut ja viivästyskorko {c.get('invoice_number') or ''}",
                  company_id=c.get("company_id"))
        e.dr("receivables", dfee + dint)
        e.cr("collection_fees_income", dfee)
        if "interest_income" in acc:
            e.cr("interest_income", dint)
        else:
            e.cr("collection_fees_income", dint)
        await _store(e, result)


async def build_entries(date_from, date_to) -> dict:
    """Book every source event dated in [date_from, date_to] that is not booked yet. Idempotent. Test tenants are never
    booked (core.tenant_visibility), whatever the ops toggle and with or without a request."""
    df, dt = _day(date_from), _day(date_to)
    if not df or not dt or df > dt:
        raise ValueError("Give a valid date range (from <= to)")
    from services.finance_core import helsinki_now
    today = helsinki_now().date()
    result = {"from": df.isoformat(), "to": dt.isoformat(), "created": 0, "existing": 0, "skipped": 0,
              "rejected": [], "by_source": defaultdict(int)}
    ts = await _test_scope()
    await _book_invoices(df, dt, result, ts)
    await _book_payments(df, dt, result, ts)
    await _book_carrier_invoices(df, dt, result, ts)
    await _book_supplier_payments(df, dt, result, ts)
    await _book_accruals(df, dt, result, today, ts)
    await _book_revaluations(df, dt, result, ts)
    await _book_collections(df, dt, result, ts)
    result["by_source"] = dict(result["by_source"])
    return result


# ------------------------------------------------------------------------------------------- queries
def entry_query(date_from=None, date_to=None, *, source=None, account=None, shipment_ref=None, exported=None,
                company_id=None) -> dict:
    q: dict = {}
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = str(date_from)[:10]
        if date_to:
            q["date"]["$lte"] = str(date_to)[:10]
    if source:
        q["source"] = source
    if account:
        q["lines.account"] = account
    if shipment_ref:
        q["lines.dimension.shipment_ref"] = shipment_ref
    if company_id:
        q["company_id"] = company_id
    if exported is True:
        q["exported_at"] = {"$nin": [None, ""]}
    elif exported is False:
        q["exported_at"] = {"$in": [None, ""]}
    return q


def trial_balance(entries: list[dict]) -> dict:
    acc: dict[str, dict] = {}
    for e in entries:
        for ln in e.get("lines") or []:
            a = acc.setdefault(ln["account"], {"account": ln["account"], "name_fi": account_name(ln["account"], "fi"),
                                               "name_en": account_name(ln["account"], "en"), "debit_cents": 0,
                                               "credit_cents": 0})
            a["debit_cents"] += _i(ln.get("debit_cents"))
            a["credit_cents"] += _i(ln.get("credit_cents"))
    rows = sorted(acc.values(), key=lambda r: r["account"])
    for r in rows:
        r["balance_cents"] = r["debit_cents"] - r["credit_cents"]
    return {"accounts": rows, "debit_cents": sum(r["debit_cents"] for r in rows),
            "credit_cents": sum(r["credit_cents"] for r in rows),
            "balance_cents": sum(r["balance_cents"] for r in rows)}


async def list_entries(date_from=None, date_to=None, *, source=None, account=None, shipment_ref=None, exported=None,
                       company_id=None, limit: int = 200, skip: int = 0) -> dict:
    q = entry_query(date_from, date_to, source=source, account=account, shipment_ref=shipment_ref, exported=exported,
                    company_id=company_id)
    if not company_id:  # the company-wide journal and trial balance never show test tenants; one company shows its own
        q = _without_test_entries(q, await _test_scope())
    entries = await db.ledger_entries.find(q, NO_ID).sort([("date", 1), ("created_at", 1)]).to_list(None)
    counts: dict[str, int] = defaultdict(int)
    for e in entries:
        counts[e["source"]] += 1
    unbalanced = [e["id"] for e in entries
                  if sum(_i(ln["debit_cents"]) for ln in e["lines"]) != sum(_i(ln["credit_cents"]) for ln in e["lines"])]
    return {"items": entries[skip:skip + limit], "total": len(entries), "by_source": dict(counts),
            "trial_balance": trial_balance(entries), "unbalanced": unbalanced}


# ------------------------------------------------------------------------------------------- exports
FORMATS = ("procountor", "netvisor", "csv")


def default_format() -> str:
    from core import config
    fmt = (config.env("LEDGER_EXPORT_FORMAT", "procountor") or "procountor").lower()
    return fmt if fmt in FORMATS else "procountor"


def ledger_api() -> str:
    from core import config
    v = (config.env("LEDGER_API", "off") or "off").lower()
    return v if v in ("off", "netvisor", "procountor") else "off"


def _eur(cents: int, sep: str = ",") -> str:
    neg = cents < 0
    cents = abs(int(cents))
    s = f"{cents // 100}{sep}{cents % 100:02d}"
    return "-" + s if neg else s


def render_csv(entries: list[dict]) -> bytes:
    """Generic journal CSV (UTF-8 with BOM, semicolon, decimal comma — opens directly in Finnish Excel)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["date", "entry_id", "document_number", "source", "source_id", "kind", "account", "account_name",
                "debit", "credit", "vat_code", "shipment_ref", "lane", "description"])
    for e in entries:
        for ln in e["lines"]:
            dim = ln.get("dimension") or {}
            w.writerow([e["date"], e["id"], e.get("document_number") or "", e["source"], e["source_id"], e.get("kind") or "",
                        ln["account"], account_name(ln["account"]), _eur(ln["debit_cents"]) if ln["debit_cents"] else "",
                        _eur(ln["credit_cents"]) if ln["credit_cents"] else "", ln.get("vat_code") or "",
                        dim.get("shipment_ref") or "", dim.get("lane") or "", e.get("description") or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def _entries_for_export(df: date, dt: date, only_new: bool) -> list[dict]:
    """Entries sent to the accounting system: never test-tenant ones (also not ones booked before they were left out)."""
    q = _without_test_entries(entry_query(df, dt, exported=False if only_new else None), await _test_scope())
    return await db.ledger_entries.find(q, NO_ID).sort([("date", 1), ("created_at", 1)]).to_list(None)


async def export(fmt: str, df: date, dt: date, *, actor: dict | None = None, only_new: bool = False,
                 target: str = "file") -> dict:
    """Build entries for the range, render the file in `fmt`, store it and record a `ledger_exports` row.
    target "file" = download only; "api" = also pushed to LEDGER_API (see push_to_api)."""
    from core.audit import actor_ref, audit
    from core.storage import put_file
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    built = await build_entries(df, dt)
    entries = await _entries_for_export(df, dt, only_new)
    validation: dict = {}
    if fmt == "csv":
        data, ext, ctype = render_csv(entries), "csv", "text/csv; charset=utf-8"
    elif fmt == "procountor":
        data = render_procountor(entries)
        ext, ctype = ("zip", "application/zip") if data[:2] == b"PK" else ("csv", "text/csv; charset=windows-1252")
        validation = validate_procountor(data, entries)
    else:
        data, ext, ctype = render_netvisor(entries), "zip", "application/zip"
        validation = validate_netvisor(data)
    export_id = new_id()
    filename = f"kirjanpito_{fmt}_{df.isoformat()}_{dt.isoformat()}.{ext}"
    file_id = await put_file(data, filename, ctype.split(";")[0], {"kind": "ledger_export", "export_id": export_id})
    tb = trial_balance(entries)
    status = "ready" if validation.get("valid", True) else "invalid"
    row = {"id": export_id, "format": fmt, "from": df.isoformat(), "to": dt.isoformat(), "count": len(entries),
           "file_id": file_id, "filename": filename, "content_type": ctype, "target": target, "status": status,
           "validation": validation, "debit_cents": tb["debit_cents"], "credit_cents": tb["credit_cents"],
           "built": {k: built[k] for k in ("created", "existing", "rejected")}, "only_new": only_new,
           "created_at": now_iso(), "created_by": actor_ref(actor) if actor else {"id": "finance", "name": "Finance agent"}}
    await db.ledger_exports.insert_one(dict(row))
    if status == "ready" and entries:
        await db.ledger_entries.update_many({"id": {"$in": [e["id"] for e in entries]}, "exported_at": {"$in": [None, ""]}},
                                            {"$set": {"export_batch_id": export_id, "exported_at": now_iso()}})
    await audit(actor, "ledger_exported", "ledger_export", export_id, {"format": fmt, "from": row["from"], "to": row["to"],
                                                                       "count": len(entries), "status": status})
    return row


async def list_exports(limit: int = 50) -> dict:
    items = await db.ledger_exports.find({}, NO_ID).sort("created_at", -1).to_list(limit)
    return {"items": items, "total": await db.ledger_exports.count_documents({}), "default_format": default_format(),
            "api": ledger_api()}


async def export_file(export_id: str):
    from core.storage import get_file
    row = await db.ledger_exports.find_one({"id": export_id}, NO_ID)
    if not row or not row.get("file_id"):
        return None
    data, filename, _ = await get_file(row["file_id"])
    return data, row.get("filename") or filename, row.get("content_type") or "application/octet-stream"


# ------------------------------------------------------------------------------------------- Procountor
# Spec: Procountor "Kirjanpitoaineiston siirtotiedosto" / "Accounting data import file" (help article 531736, updated
# 2026-10-02; copies in data/ledger/procountor_accounting_information_import_file_{fi,en}.html). One CSV file = one
# journal receipt (muistiotosite); no header row; semicolon-separated, no quoting; one row per entry:
#   1 Account (4 digits, must exist) ; 2 Transaction name (max 80) ; 3 Sum (debit +, credit -, comma or point) ;
#   4 Dimension (must exist) ; 5 Item (must exist) ; 6 VAT % ("VAT is added to the sum": left empty, our VAT is
#   posted on the VAT accounts) ; 7 VAT type (P/S, empty = P)
# Encoding: not stated; the official template is Windows-1252, so files are written in cp1252.
# Several receipts (one per date) are delivered as a ZIP of such files.
PROCOUNTOR_SPEC = "https://help.procountor.com/fi/articles/531736 (Kirjanpitoaineiston siirtotiedosto)"


def _clean_text(s: str, n: int) -> str:
    return (s or "").replace(";", ",").replace("\r", " ").replace("\n", " ").strip()[:n]


def _procountor_rows(entries: list[dict]) -> list[str]:
    from core import config
    dim_name = config.env("PROCOUNTOR_DIMENSION", "")  # e.g. "Lähetys" (items = shipment references) when set up
    rows = []
    for e in entries:
        for ln in e["lines"]:
            amount = _i(ln["debit_cents"]) - _i(ln["credit_cents"])
            if not amount:
                continue
            item = ((ln.get("dimension") or {}).get("shipment_ref") or "") if dim_name else ""
            name = _clean_text(f"{e.get('document_number') or ''} {e.get('description') or ''}".strip() or e["source"], 80)
            rows.append(";".join([str(ln["account"]), name, _eur(amount), dim_name if item else "",
                                  _clean_text(item, 255), "", ""]))
    return rows


def render_procountor(entries: list[dict]) -> bytes:
    """One cp1252 CSV per entry date (= one journal receipt per day); a single date is returned as the CSV itself,
    several dates as a ZIP named procountor_<date>.csv inside."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_day[e["date"]].append(e)
    files = {f"procountor_{d}.csv": ("\r\n".join(_procountor_rows(es)) + "\r\n").encode("cp1252", errors="replace")
             for d, es in sorted(by_day.items())}
    if len(files) == 1:
        return next(iter(files.values()))
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def read_procountor(data: bytes) -> dict[str, list[list[str]]]:
    """Re-read a Procountor export (CSV or ZIP of CSVs) -> {filename: [fields]}."""
    import io
    import zipfile
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return {n: [r.split(";") for r in zf.read(n).decode("cp1252").splitlines() if r] for n in zf.namelist()}
    return {"procountor.csv": [r.split(";") for r in data.decode("cp1252").splitlines() if r]}


def validate_procountor(data: bytes, entries: list[dict]) -> dict:
    import re
    errors = []
    files = read_procountor(data)
    total_rows = 0
    for name, rows in files.items():
        s = Decimal(0)
        for k, f in enumerate(rows, start=1):
            total_rows += 1
            if len(f) != 7:
                errors.append(f"{name}:{k}: {len(f)} fields, expected 7")
                continue
            if not re.fullmatch(r"\d{4}", f[0]):
                errors.append(f"{name}:{k}: account {f[0]!r} is not 4 digits")
            if not f[1] or len(f[1]) > 80:
                errors.append(f"{name}:{k}: transaction name missing or over 80 characters")
            try:
                s += Decimal(f[2].replace(",", "."))
            except Exception:  # noqa: BLE001
                errors.append(f"{name}:{k}: sum {f[2]!r} is not a decimal")
        if s != 0:
            errors.append(f"{name}: debit and credit differ by {s} (Procountor would post it to 8450)")
    return {"valid": not errors, "errors": errors[:50], "files": len(files), "rows": total_rows, "spec": PROCOUNTOR_SPEC}


# ------------------------------------------------------------------------------------------- Netvisor
# Spec: Netvisor API accounting.nv "Kirjanpitoaineiston tuonti / Import accounting data" (support.netvisor.fi article
# 766888, updated 2026-08-10) and its schema accounting.dtd (http://isvapi.netvisor.fi/netvisor/accounting.dtd, copy in
# data/ledger/netvisor_accounting.dtd; Netvisor publishes a DTD, no XSD, for this resource). One request = one voucher,
# so the export is a ZIP with one XML per entry (each validated against the DTD); no XML declaration, UTF-8.
# calculationmode net: Netvisor adds no VAT or rounding lines; our VAT lines are on the VAT accounts. linesum uses a
# decimal comma (the doc's example "-100,00"); vatpercent likewise ("25,5"; unconfirmed for decimals).
NETVISOR_SPEC = "https://support.netvisor.fi/en/articles/766888-import-accounting-data-accounting-nv"
NETVISOR_VOUCHER_CLASS = {
    "fi": {"invoice": "Myyntilasku", "payment": "Myyntisuoritus", "carrier_invoice": "Ostolasku",
           "supplier_payment": "Ostosuoritus", "accrual": "Jaksotukset", "accrual_reversal": "Jaksotukset",
           "fx_revaluation": "Muut", "fx_realised": "Muut", "collections": "Muut"},
    "en": {"invoice": "Sales invoice", "payment": "Sales transaction", "carrier_invoice": "Purchase invoice",
           "supplier_payment": "Purchase transaction", "accrual": "Accruals", "accrual_reversal": "Accruals",
           "fx_revaluation": "Others", "fx_realised": "Others", "collections": "Others"},
}
# Netvisor VAT codes (Alv-koodit ja -tunnisteet, 10.8.2026) for the net lines; VAT account lines carry NONE.
NETVISOR_VATCODE = {"FI_25_5": "KOMY", "FI_14": "KOMY", "FI_13_5": "KOMY", "FI_10": "KOMY", "ZERO_INTL_TRANSPORT": "KOMY0",
                    "ZERO_EXPORT": "EUUM", "EU_ICS_RC": "EUPM312", "OUT_OF_SCOPE_FOREIGN": "EUUM",
                    "OUT_OF_SCOPE_DISBURSEMENT": "NONE", "KOOS": "KOOS", "EUOS": "EUPO", "MAAL": "KAVE"}


def _pct_text(rate) -> str:
    r = Decimal(str(rate or 0)).normalize()
    return format(r, "f").replace(".", ",")


def netvisor_voucher_xml(e: dict) -> bytes:
    from xml.sax.saxutils import escape
    from core import config
    lang = (config.env("NETVISOR_LANGUAGE", "FI") or "FI").lower()
    classes = NETVISOR_VOUCHER_CLASS.get(lang, NETVISOR_VOUCHER_CLASS["fi"])
    dims_on = config.env("NETVISOR_DIMENSIONS", "on").lower() != "off"
    rates = {"FI_25_5": None, "FI_13_5": 13.5, "FI_14": 13.5, "FI_10": 10}
    out = ["<root><voucher>", "<calculationmode>net</calculationmode>",
           f'<voucherdate format="ansi">{e["date"]}</voucherdate>',
           f"<description>{escape(_clean_text(e.get('description') or '', 255))}</description>",
           f"<voucherclass>{escape(classes.get(e['source'], classes['fx_revaluation']))}</voucherclass>"]
    vat_accounts = {ledger_accounts().get(k) for k in ("vat_payable", "vat_receivable", "vat_rc_payable")}
    for ln in e["lines"]:
        amount = _i(ln["debit_cents"]) - _i(ln["credit_cents"])
        code = "NONE"
        pct = Decimal(0)
        vc = ln.get("vat_code")
        if vc and ln["account"] not in vat_accounts:
            code = NETVISOR_VATCODE.get(vc, "NONE")
            if code in ("KOMY", "KOOS", "EUPO", "KAVE"):
                r = ln["vat_rate_pct"] if ln.get("vat_rate_pct") is not None else (rates.get(vc) or 25.5)
                pct = Decimal(str(r))
        dim = ln.get("dimension") or {}
        out.append("<voucherline>")
        out.append(f'<linesum type="net">{_eur(amount)}</linesum>')
        out.append(f"<description>{escape(_clean_text(account_name(ln['account']), 255))}</description>")
        out.append(f"<accountnumber>{escape(str(ln['account']))}</accountnumber>")
        out.append(f'<vatpercent vatcode="{code}">{_pct_text(pct)}</vatpercent>')
        if dims_on:
            for name, key in (("Lähetys", "shipment_ref"), ("Reitti", "lane")):
                if dim.get(key):
                    out.append(f"<dimension><dimensionname>{name}</dimensionname>"
                               f"<dimensionitem>{escape(str(dim[key])[:200])}</dimensionitem></dimension>")
        out.append("</voucherline>")
    out.append("</voucher></root>")
    return "".join(out).encode("utf-8")


def render_netvisor(entries: list[dict]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, e in enumerate(entries, start=1):
            zf.writestr(f"voucher_{e['date']}_{n:05d}_{e['source']}.xml", netvisor_voucher_xml(e))
    return buf.getvalue()


def netvisor_dtd():
    import os
    from lxml import etree
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ledger",
                        "netvisor_accounting.dtd")
    with open(path, "rb") as fh:
        return etree.DTD(fh)


def validate_netvisor(data: bytes) -> dict:
    import io
    import zipfile
    from lxml import etree
    dtd = netvisor_dtd()
    errors, n = [], 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            n += 1
            doc = etree.fromstring(zf.read(name))
            if not dtd.validate(doc):
                errors.append(f"{name}: {dtd.error_log.filter_from_errors()[:3]}")
    return {"valid": not errors, "errors": [str(e) for e in errors[:50]], "vouchers": n, "schema": "accounting.dtd",
            "spec": NETVISOR_SPEC}


# ------------------------------------------------------------------------------------------- vendor APIs (LEDGER_API)
def netvisor_headers(url: str, *, sender: str, customer_id: str, partner_id: str, customer_key: str, partner_key: str,
                     organisation_id: str, language: str = "FI", transaction_id: str | None = None,
                     now=None) -> dict:
    """Netvisor API authentication headers (support.netvisor.fi 'API authentication'): HMAC-SHA256 over
    URL&Sender&CustomerId&Timestamp&Language&OrganisationId&TransactionId&TimestampUnix&CustomerKey&PartnerKey with key
    CustomerKey&PartnerKey, both ISO-8859-15, hex digest."""
    import hashlib
    import hmac
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    ts_unix = str(int(now.timestamp()))
    tx = transaction_id or new_id().replace("-", "")[:30]
    msg = "&".join([url, sender, customer_id, ts, language, organisation_id, tx, ts_unix, customer_key, partner_key])
    mac = hmac.new(f"{customer_key}&{partner_key}".encode("iso-8859-15"), msg.encode("iso-8859-15"),
                   hashlib.sha256).hexdigest()
    return {"X-Netvisor-Authentication-Sender": sender, "X-Netvisor-Authentication-CustomerId": customer_id,
            "X-Netvisor-Authentication-PartnerId": partner_id, "X-Netvisor-Authentication-Timestamp": ts,
            "X-Netvisor-Authentication-TimestampUnix": ts_unix, "X-Netvisor-Authentication-TransactionId": tx,
            "X-Netvisor-Interface-Language": language, "X-Netvisor-Organisation-ID": organisation_id,
            "X-Netvisor-Authentication-UseHTTPResponseStatusCodes": "1", "X-Netvisor-Authentication-MAC": mac,
            "X-Netvisor-Authentication-MACHashCalculationAlgorithm": "HMACSHA256",
            "Content-Type": "text/plain; charset=utf-8"}


def procountor_receipt(e: dict) -> dict:
    """POST /ledgerreceipts body (OpenAPI LedgerReceipt, dev.procountor.com swagger v26.10): JOURNAL, debit positive /
    credit negative, VAT already on its own accounts -> vatStatus 12 (no VAT handling), vatPercent 0."""
    vat_type = "SALES" if e["source"] in ("invoice", "payment", "collections") else "PURCHASE"
    return {"type": "JOURNAL", "name": _clean_text(f"{e.get('document_number') or ''} {e.get('description') or ''}".strip(), 80)
            or e["source"], "receiptDate": e["date"], "vatType": vat_type, "vatStatus": 12, "vatProcessing": "FINLAND",
            "transactionDescription": _clean_text(e.get("description") or "", 255),
            "accountantsNotes": f"Huolitsija {e['source']} {e['source_id']} {e.get('kind') or ''}"[:255],
            "transactions": [{"transactionType": "ENTRY", "account": str(ln["account"]),
                              "accountingValue": float(Decimal(_i(ln["debit_cents"]) - _i(ln["credit_cents"])) / 100),
                              "vatPercent": 0.0, "vatStatus": 12, "description": _clean_text(account_name(ln["account"]), 255)}
                             for ln in e["lines"] if _i(ln["debit_cents"]) - _i(ln["credit_cents"])]}


async def push_to_api(entries: list[dict]) -> dict:
    """Send entries to the vendor API named by LEDGER_API. Built to the published specs; NOT PROVEN without vendor
    test credentials. Netvisor: NETVISOR_BASE (default https://isvapi.netvisor.fi), NETVISOR_SENDER,
    NETVISOR_CUSTOMER_ID, NETVISOR_PARTNER_ID, NETVISOR_CUSTOMER_KEY, NETVISOR_PARTNER_KEY, NETVISOR_ORGANISATION_ID.
    Procountor: PROCOUNTOR_BASE (default test https://pts-api.procountor.com/api), PROCOUNTOR_CLIENT_ID,
    PROCOUNTOR_CLIENT_SECRET, PROCOUNTOR_REDIRECT_URI, PROCOUNTOR_API_KEY (M2M client-credentials token)."""
    import httpx
    from core import config
    api = ledger_api()
    out = {"api": api, "sent": 0, "failed": [], "responses": []}
    if api == "off" or not entries:
        return out
    if api == "netvisor":
        need = ["NETVISOR_SENDER", "NETVISOR_CUSTOMER_ID", "NETVISOR_PARTNER_ID", "NETVISOR_CUSTOMER_KEY",
                "NETVISOR_PARTNER_KEY", "NETVISOR_ORGANISATION_ID"]
        missing = [k for k in need if not config.env(k, "")]
        if missing:
            return {**out, "error": f"Missing {', '.join(missing)}"}
        url = (config.env("NETVISOR_BASE", "https://isvapi.netvisor.fi") or "").rstrip("/") + "/accounting.nv"
        async with httpx.AsyncClient(timeout=60) as client:
            for e in entries:
                h = netvisor_headers(url, sender=config.env("NETVISOR_SENDER"), customer_id=config.env("NETVISOR_CUSTOMER_ID"),
                                     partner_id=config.env("NETVISOR_PARTNER_ID"),
                                     customer_key=config.env("NETVISOR_CUSTOMER_KEY"),
                                     partner_key=config.env("NETVISOR_PARTNER_KEY"),
                                     organisation_id=config.env("NETVISOR_ORGANISATION_ID"),
                                     language=config.env("NETVISOR_LANGUAGE", "FI") or "FI")
                r = await client.post(url, content=netvisor_voucher_xml(e), headers=h)
                # UseHTTPResponseStatusCodes=1: 200 = accepted, 4xx/5xx = error with a ResponseStatus body
                if r.status_code == 200:
                    out["sent"] += 1
                    out["responses"].append({"id": e["id"], "body": r.text[:300]})
                else:
                    out["failed"].append({"id": e["id"], "status": r.status_code, "body": r.text[:500]})
        return out
    need = ["PROCOUNTOR_CLIENT_ID", "PROCOUNTOR_CLIENT_SECRET", "PROCOUNTOR_REDIRECT_URI", "PROCOUNTOR_API_KEY"]
    missing = [k for k in need if not config.env(k, "")]
    if missing:
        return {**out, "error": f"Missing {', '.join(missing)}"}
    base = (config.env("PROCOUNTOR_BASE", "https://pts-api.procountor.com/api") or "").rstrip("/")
    async with httpx.AsyncClient(timeout=60) as client:
        tok = await client.post(f"{base}/oauth/token", data={
            "grant_type": "client_credentials", "client_id": config.env("PROCOUNTOR_CLIENT_ID"),
            "client_secret": config.env("PROCOUNTOR_CLIENT_SECRET"), "redirect_uri": config.env("PROCOUNTOR_REDIRECT_URI"),
            "api_key": config.env("PROCOUNTOR_API_KEY")})
        if tok.status_code != 200:
            return {**out, "error": f"Procountor token: HTTP {tok.status_code} {tok.text[:300]}"}
        headers = {"Authorization": f"Bearer {tok.json().get('access_token')}"}
        for e in entries:
            r = await client.post(f"{base}/ledgerreceipts", json=procountor_receipt(e), headers=headers)
            if r.status_code == 200:
                out["sent"] += 1
                out["responses"].append({"id": e["id"], "receipt_id": (r.json() or {}).get("id")})
            else:
                out["failed"].append({"id": e["id"], "status": r.status_code, "body": r.text[:500]})
    return out


from services.finance_core import finance_job  # noqa: E402


@finance_job("finance.ledger_export", daily_at="02:00", label_fi="Kirjanpitosiirto", label_en="Ledger export")
async def ledger_export_job(j: dict):
    """Daily 02:00: book yesterday's events and push the not-yet-exported entries to LEDGER_API (only when it is on)."""
    from services.finance_core import helsinki_now
    if ledger_api() == "off":
        return {"skipped": "LEDGER_API is off"}
    today = helsinki_now().date()
    df = today - timedelta(days=45)  # late-approved documents dated earlier are still picked up
    dt = today - timedelta(days=1)
    row = await export(ledger_api(), df, dt, only_new=True, target="api")
    entries = await db.ledger_entries.find({"export_batch_id": row["id"]}, NO_ID).to_list(None)
    res = await push_to_api(entries)
    status = "sent" if not res.get("error") and not res["failed"] else "failed"
    if status == "failed":  # leave failed ones unexported so the next run retries them
        failed_ids = [f["id"] for f in res["failed"]] or [e["id"] for e in entries]
        await db.ledger_entries.update_many({"id": {"$in": failed_ids}}, {"$set": {"export_batch_id": None, "exported_at": None}})
    await db.ledger_exports.update_one({"id": row["id"]}, {"$set": {"status": status, "api_result": res}})
    return {"export_id": row["id"], "count": row["count"], **{k: res.get(k) for k in ("sent", "error")}}
