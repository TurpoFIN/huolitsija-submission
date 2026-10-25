"""Finnish VAT return (kausiveroilmoitus) and EU sales list (yhteenvetoilmoitus) from invoices and carrier invoices.

Official sources (copies and URLs in backend/data/vero/README.md):
- Record VSRALVKV "Arvonlisäveron kausiveroilmoitus", Verohallinto tietuekuvaus v1.10 (27.2.2026, structure valid since
  29.8.2024): https://www.vero.fi/contentassets/ef5905e0f5b74bcba89aa9ba9c34015d/verohallinto_tietuekuvaus_vsralvkv_290824.pdf
- Record VSRALVYV "Yhteenvetoilmoitus", tietuekuvaus v1.4 (27.2.2026):
  https://www.vero.fi/contentassets/ef5905e0f5b74bcba89aa9ba9c34015d/verohallinto_tietuekuvaus_vsralvyv.pdf
- File format: "Sähköinen ilmoittaminen – Yleiskuvaus" v6.12 (13.2.2026): `NNN:value` one per line, ISO-8859-1,
  money G13,2 with a decimal comma, record ends with `999:<n>`.
- Row labels and what goes where: vero.fi "Arvonlisäveron ilmoitus- ja maksuohjeet" (täyttöohje, updated 1.1.2026),
  https://www.vero.fi/yritykset-ja-yhteisot/verot-ja-maksut/arvonlisaverotus/ilmoitus-ja-maksuohjeet/ and the detailed
  guide "Palvelujen ulkomaankaupan arvonlisäverotus" (48679), https://www.vero.fi/syventavat-vero-ohjeet/ohje-hakusivu/48679/
- Vero API (SelfAssessedTaxes, FileVATReturn/v2 + FileECSalesList/v1), https://www.vero.fi/tietoa-verohallinnosta/kehittaja/vero-api/

Mapping (sales by tax point = supply_date, else issue_date; credit notes negative; EUR from the invoice's `eur` breakdown):
    FI_25_5            -> 301 tax (24 % and 25.5 % both go to 301 / HighVATRate)
    FI_13_5 / FI_14    -> 302 tax          FI_10 -> 303 tax       (the return carries only the tax, not the base)
    ZERO_INTL_TRANSPORT, ZERO_EXPORT -> 309 "0-verokannan alainen liikevaihto" (täyttöohje 309: VAT-free sales with
                          the right to deduct input VAT; Arvonlisäverovelvollisen opas 8.5.1 lists AVL 71 § sales)
    EU_ICS_RC          -> 312 "Palvelujen myynnit muihin EU-maihin" + EU sales list field 211 per buyer VAT id
    OUT_OF_SCOPE_FOREIGN -> 309 (täyttöohje 309: "palvelujen yleissäännön mukainen myynti EU:n ulkopuolelle";
                          guide 48679 ch. 9: "Kohdassa 0-verokannan alainen liikevaihto ilmoitetaan palvelumyynnit EU:n
                          ulkopuolelle")
    OUT_OF_SCOPE_DISBURSEMENT -> not reported (AVL 79 §: outside the taxable amount); listed for reconciliation
Purchases (carrier_invoices approved|scheduled|paid, by invoice_date):
    domestic           -> 307 deductible VAT (eur.vat_cents)
    eu_reverse_charge  -> 314 base, 306 tax at the Finnish rate, same tax in 307 (guide 48679 ch. 9). A service whose
                          supply is exempt (AVL 71 § international transport that is itself a direct transport across
                          the EU border) is NOT reported at all: täyttöohje 314 "Tässä ei ilmoiteta … yleissäännöksen
                          mukaisia palveluja, joiden myynti on veroton". Subcontracted legs that are not themselves a
                          direct border-crossing transport are taxable (guide 48679 §7.4.1, CJEU C-288/16). Every such
                          exemption decision carries a `verify` flag.
    non_eu_reverse_charge -> tax in 301 (by rate) and 307; no base anywhere (täyttöohje: "EU:n ulkopuolelle
                          sijoittautuneelta elinkeinonharjoittajalta ostetuista palveluista ei ilmoiteta veron perustetta
                          … Ilmoita näiden palveluostojen vero kuitenkin kohdassa Vero kotimaan myynnistä verokannoittain")
    zero_rated         -> nothing to report (a domestic supplier's VAT-free invoice)
308 = 301 + 302 + 303 + 304 + 305 + 306 + 318 − 307 (VSRALVKV check #1924, tax years 2025-).

Filing: VAT_FILING_MODE=file (default) produces the Ilmoitin.fi file (upload at ilmoitin.fi). VAT_FILING_MODE=api builds
the Vero API JSON (FileVATReturn/v2, FileECSalesList/v1) and posts it with the client certificate VERO_API_CERT /
VERO_API_KEY and headers Vero-SoftwareKey (VERO_API_SOFTWARE_KEY) and, when filing for a client, Vero-AuthorizationToken.
The API path is NOT proven: it needs Vero's API agreement, a certificate from Vero's certificate service, software
registration and (for a client company) a Suomi.fi authorisation. Going live = set those env values + VAT_FILING_MODE=api.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core import config
from core.db import db, register_index
from core.settings import DEFAULTS, get_settings
from core.tenant_visibility import exclude_test, finance_scope, is_test_record, test_company_ids, test_shipment_ids
from core.util import NO_ID, new_id, now_iso
from services.finance_core import HELSINKI, finance_job, helsinki_now

log = logging.getLogger("finance.vat")

register_index("vat_reports", "period", unique=True)
DEFAULTS.setdefault("vat_period_length", "month")  # month | quarter | year (Verohallinto decides the filer's period)

SRC_VSRALVKV = "https://www.vero.fi/contentassets/ef5905e0f5b74bcba89aa9ba9c34015d/verohallinto_tietuekuvaus_vsralvkv_290824.pdf"
SRC_VSRALVYV = "https://www.vero.fi/contentassets/ef5905e0f5b74bcba89aa9ba9c34015d/verohallinto_tietuekuvaus_vsralvyv.pdf"
SRC_TAYTTOOHJE = "https://www.vero.fi/yritykset-ja-yhteisot/verot-ja-maksut/arvonlisaverotus/ilmoitus-ja-maksuohjeet/"
SRC_GUIDE_48679 = "https://www.vero.fi/syventavat-vero-ohjeet/ohje-hakusivu/48679/"
SRC_VERO_API = "https://www.vero.fi/tietoa-verohallinnosta/kehittaja/vero-api/"

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "EL", "HU", "IE", "IT", "LV", "LT", "LU",
      "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "XI"}

# VSRALVKV amount fields in form order: code -> (label_fi, label_en, Vero API v2 path). Labels from the täyttöohje /
# the English VSRALVKV description; API names from FileVATReturnv2Request (same meaning, no official cross table).
FIELDS: dict[str, tuple[str, str, str]] = {
    "301": ("Vero kotimaan myynnistä verokannoittain: 25,5 %", "Tax on domestic sales by tax rate: 25.5 %",
            "VATDetails.VATOnDomesticSalesByTaxRate.HighVATRate"),
    "302": ("Vero kotimaan myynnistä verokannoittain: 13,5 %", "Tax on domestic sales by tax rate: 13.5 %",
            "VATDetails.VATOnDomesticSalesByTaxRate.MediumVATRate"),
    "303": ("Vero kotimaan myynnistä verokannoittain: 10 %", "Tax on domestic sales by tax rate: 10 %",
            "VATDetails.VATOnDomesticSalesByTaxRate.LowVATRate"),
    "305": ("Vero tavaraostoista muista EU-maista", "Tax on goods purchased from other EU Member States",
            "VATDetails.VATOnPurchasesAndImports.GoodsFromEUMemberStates"),
    "306": ("Vero palveluostoista muista EU-maista", "Tax on services purchased from other EU Member States",
            "VATDetails.VATOnPurchasesAndImports.ServicesFromEUMemberStates"),
    "304": ("Vero tavaroiden maahantuonneista EU:n ulkopuolelta", "Tax on imports of goods from outside the EU",
            "VATDetails.VATOnPurchasesAndImports.ImportOfGoodsOutsideEU"),
    "318": ("Vero rakentamispalvelun ja metalliromun ostosta", "Tax on purchases of construction services and scrap metal",
            "VATDetails.VATOnPurchasesAndImports.ConstructionServicesAndScrapMetal"),
    "307": ("Verokauden vähennettävä vero", "Deductible tax for the tax period", "VATDetails.DeductibleVAT"),
    "308": ("Maksettava vero / Palautukseen oikeuttava vero (-)", "Tax payable / Tax qualifying for refund (-)", ""),
    "309": ("0-verokannan alainen liikevaihto", "Turnover subject to 0 % VAT",
            "VATDetails.SalesPurchasesImports.ZeroVATRateTurnover"),
    "311": ("Tavaroiden myynnit muihin EU-maihin", "Sales of goods to other EU Member States",
            "VATDetails.SalesPurchasesImports.SalesOfGoodsToEUMemberStates"),
    "312": ("Palvelujen myynnit muihin EU-maihin", "Sales of services to other EU Member States",
            "VATDetails.SalesPurchasesImports.SalesOfServicesToEUMemberStates"),
    "313": ("Tavaraostot muista EU-maista", "Purchases of goods from other EU Member States",
            "VATDetails.SalesPurchasesImports.PurchasesOfGoodsFromEUMemberStates"),
    "314": ("Palveluostot muista EU-maista", "Purchases of services from other EU Member States",
            "VATDetails.SalesPurchasesImports.PurchasesOfServicesFromEUMemberStates"),
    "310": ("Tavaroiden maahantuonnit EU:n ulkopuolelta", "Imports of goods from outside the EU",
            "VATDetails.SalesPurchasesImports.ImportsOfGoodsFromOutsideEU"),
    "319": ("Rakentamispalvelun ja metalliromun myynnit", "Sales of construction services and scrap metal",
            "VATDetails.SalesPurchasesImports.SalesOfConstructionServicesAndScrapMetal"),
    "320": ("Rakentamispalvelun ja metalliromun ostot", "Purchases of construction services and scrap metal",
            "VATDetails.SalesPurchasesImports.PurchasesOfConstructionServicesAndScrapMetal"),
}
TAX_FIELDS = ("301", "302", "303", "304", "305", "306", "318")
SALES_FIELD_BY_KIND = {"zero": "309", "reverse_charge": "312", "out_of_scope": "309"}
APPROVED_CI = ("approved", "scheduled", "paid")


class VatPeriodError(ValueError):
    pass


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int) -> str:
    """Vero G13,2: decimal comma, minus allowed, no thousands separator."""
    neg = cents < 0
    c = abs(int(cents))
    return f"{'-' if neg else ''}{c // 100},{c % 100:02d}"


def money_float(cents: int) -> float:
    return float(Decimal(int(cents)) / 100)


# ------------------------------------------------------------------------------------------- periods
def parse_period(period: str) -> dict:
    p = (period or "").strip().upper()
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise VatPeriodError("Month must be 01–12")
        start = date(y, mo, 1)
        end = date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)
        return {"period": f"{y}-{mo:02d}", "kind": "month", "code": "K", "number": mo, "year": y, "start": start, "end": end,
                "months": [mo]}
    m = re.fullmatch(r"(\d{4})-Q([1-4])", p)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        start = date(y, 3 * q - 2, 1)
        end = date(y + (q == 4), (3 * q) % 12 + 1, 1) - timedelta(days=1)
        return {"period": f"{y}-Q{q}", "kind": "quarter", "code": "Q", "number": q, "year": y, "start": start, "end": end,
                "months": [3 * q - 2, 3 * q - 1, 3 * q]}
    m = re.fullmatch(r"(\d{4})", p)
    if m:
        y = int(m.group(1))
        return {"period": str(y), "kind": "year", "code": "V", "number": None, "year": y, "start": date(y, 1, 1),
                "end": date(y, 12, 31), "months": list(range(1, 13))}
    raise VatPeriodError("Period must be YYYY-MM, YYYY-Qn or YYYY")


def due_date(p: dict) -> date:
    """Kausiveroilmoitus due date: the 12th of the second month after the period (monthly and quarterly filers);
    annual filers by the end of February (vero.fi 'Ilmoitus- ja maksuohjeet')."""
    end = p["end"]
    if p["kind"] == "year":
        return date(end.year + 1, 3, 1) - timedelta(days=1)
    y, m = end.year, end.month + 2
    if m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 12)


async def current_period(today: date | None = None) -> str:
    today = today or helsinki_now().date()
    length = (await get_settings()).get("vat_period_length") or "month"
    if length == "quarter":
        return f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    if length == "year":
        return str(today.year)
    return f"{today.year}-{today.month:02d}"


async def previous_period(today: date | None = None) -> str:
    today = today or helsinki_now().date()
    length = (await get_settings()).get("vat_period_length") or "month"
    if length == "quarter":
        q = (today.month - 1) // 3 + 1
        return f"{today.year - 1}-Q4" if q == 1 else f"{today.year}-Q{q - 1}"
    if length == "year":
        return str(today.year - 1)
    first = today.replace(day=1) - timedelta(days=1)
    return f"{first.year}-{first.month:02d}"


# ------------------------------------------------------------------------------------------- sales side
def tax_point(inv: dict) -> str | None:
    """Suoriteperuste (Arvonlisäverovelvollisen opas 11.1): the supply date; the issue date when none is set.
    Credit notes count in the period they are issued (flagged `verify` when that differs from the original supply
    period: not confirmed from a vero.fi sentence)."""
    if inv.get("kind") == "credit_note":
        return (inv.get("issue_date") or "")[:10] or None
    return (inv.get("supply_date") or inv.get("issue_date") or "")[:10] or None


def invoice_vat_groups(inv: dict) -> list[dict]:
    """[{vat_code, rate_pct, kind, base_cents, vat_cents}] in EUR, signed (credit notes negative)."""
    from services.billing import VAT_CODES
    cur = (inv.get("currency") or "EUR").upper()
    src = inv.get("vat_breakdown") or []
    if cur != "EUR" and (inv.get("eur") or {}).get("vat_breakdown"):
        src = inv["eur"]["vat_breakdown"]
        conv = None
    else:
        conv = (inv.get("fx") or (inv.get("eur") or {}).get("fx") or {}).get("rate") if cur != "EUR" else None
    if cur != "EUR" and not (inv.get("eur") or {}).get("vat_breakdown") and not conv:
        raise VatPeriodError(f"Laskun {inv.get('number')} EUR-muunnos puuttuu ({cur}). Korjaa kurssi ennen ALV-raporttia.")
    if not src:
        agg: dict[tuple, dict] = {}
        for ln in inv.get("lines") or []:
            code = ln.get("vat_code") or "FI_25_5"
            g = agg.setdefault((code, ln.get("vat_rate_pct")), {"vat_code": code, "rate_pct": ln.get("vat_rate_pct"),
                                                                "base_cents": 0, "vat_cents": 0})
            g["base_cents"] += _i(ln.get("amount_cents"))
            g["vat_cents"] += _i(ln.get("vat_cents"))
        src = list(agg.values())
    out = []
    sign = -1 if inv.get("kind") == "credit_note" else 1
    for b in src:
        base, vat = _i(b.get("base_cents")), _i(b.get("vat_cents"))
        if conv:
            from services.fx import convert
            base, vat = convert(base, conv), convert(vat, conv)
        if sign < 0:
            base, vat = -abs(base), -abs(vat)
        code = b.get("vat_code") or "FI_25_5"
        spec = VAT_CODES.get(code) or {}
        out.append({"vat_code": code, "rate_pct": float(b["rate_pct"] if b.get("rate_pct") is not None else spec.get("rate_pct", 0)), "kind": b.get("kind") or spec.get("kind"),
                    "base_cents": base, "vat_cents": vat})
    return out


def tax_field_for_rate(rate: float) -> str:
    if rate >= 20:
        return "301"
    if rate >= 12:
        return "302"
    return "303"


def eu_country_of(vat_id: str | None, fallback: str | None) -> str:
    v = re.sub(r"[^A-Za-z0-9]", "", vat_id or "").upper()
    cc = v[:2] if len(v) > 2 and v[:2].isalpha() else (fallback or "").upper()
    return "EL" if cc == "GR" else cc


def vat_number_without_prefix(vat_id: str | None) -> str:
    v = re.sub(r"[^A-Za-z0-9]", "", vat_id or "").upper()
    return v[2:] if len(v) > 2 and v[:2].isalpha() else v


async def _sales(p: dict, company_id: str | None, test_ids: list[str] | None = None) -> list[dict]:
    lo, hi = p["start"].isoformat(), p["end"].isoformat()
    rng = {"$gte": lo, "$lte": hi + "T99"}
    q = {"number": {"$nin": [None, ""]}, "status": {"$nin": ["draft", "void"]},
         "$or": [{"kind": {"$ne": "credit_note"}, "supply_date": rng},
                 {"kind": {"$ne": "credit_note"}, "supply_date": {"$in": [None, ""]}, "issue_date": rng},
                 {"kind": "credit_note", "issue_date": rng}]}
    # every customer except the test tenants, whatever the ops toggle says; an explicit company_id (even a test
    # tenant) shows that company's own sales
    q.update(await finance_scope(company_id, ids=test_ids, database=db))
    coll = db.invoices
    raw = getattr(coll, "raw", coll)
    return await raw.find(q, NO_ID).to_list(None)


# ------------------------------------------------------------------------------------------- purchase side
def _crosses_eu_border(a: str | None, b: str | None) -> bool:
    a, b = (a or "").upper(), (b or "").upper()
    return bool(a and b) and ((a in EU) != (b in EU))


async def exemption_for(ci: dict) -> dict:
    """Is the purchased service an exempt international transport (AVL 71 §) that is itself a direct transport across
    the EU border? Explicit `vat_exempt_transport` on the invoice wins; otherwise derived from the matched shipments'
    legs. Always flagged `verify` when it decides 'exempt' from data (guide 48679 §7.4.1, CJEU C-288/16)."""
    if ci.get("vat_exempt_transport") is not None:
        return {"exempt": bool(ci["vat_exempt_transport"]), "basis": "set on the invoice", "verify": False}
    if (ci.get("vat_treatment") or "") not in ("eu_reverse_charge", "non_eu_reverse_charge"):
        return {"exempt": False, "basis": "not reverse charge", "verify": False}
    matches = [m for m in ci.get("matches") or [] if m.get("shipment_id")]
    if not matches:
        return {"exempt": False, "basis": "no matched shipment: general rule, taxable", "verify": False}
    sids = list({m["shipment_id"] for m in matches})
    acc_ids = [m.get("accrual_id") for m in matches if m.get("accrual_id")]
    shipments = {s["id"]: s for s in await getattr(db.shipments, "raw", db.shipments).find({"id": {"$in": sids}}, NO_ID).to_list(None)}
    accruals = {a["id"]: a for a in await db.cost_accruals.find({"id": {"$in": acc_ids}}, NO_ID).to_list(None)} if acc_ids else {}
    crossing_all = True
    for m in matches:
        s = shipments.get(m["shipment_id"]) or {}
        acc = accruals.get(m.get("accrual_id")) or {}
        legs = {lg.get("seq"): lg for lg in s.get("legs") or []}
        seqs = {ln.get("leg_seq") for ln in acc.get("lines") or [] if ln.get("leg_seq") is not None}
        stages = {ln.get("stage") for ln in acc.get("lines") or [] if ln.get("stage")}
        if seqs and all(q in legs for q in seqs):
            ok = all(_crosses_eu_border((legs[q].get("from") or {}).get("country"), (legs[q].get("to") or {}).get("country"))
                     for q in seqs)
        else:
            ok = _crosses_eu_border((s.get("origin") or {}).get("country"), (s.get("destination") or {}).get("country")) \
                and (not stages or stages <= {"main"})
        crossing_all = crossing_all and ok
    if crossing_all:
        return {"exempt": True, "verify": True,
                "basis": "AVL 71 §: the purchased leg is itself a transport across the EU border (import/export); "
                         "not reported (täyttöohje 314). Confirm it is a direct transport (guide 48679 §7.4.1, C-288/16)."}
    return {"exempt": False, "verify": False,
            "basis": "subcontracted leg that is not itself a border-crossing transport: taxable (guide 48679 §7.4.1)"}


def _treatment(ci: dict) -> str:
    t = ci.get("vat_treatment")
    if t in ("domestic", "eu_reverse_charge", "non_eu_reverse_charge", "zero_rated"):
        return t
    c = ((ci.get("supplier") or {}).get("country") or "FI").upper()
    if c == "FI":
        return "domestic"
    return "eu_reverse_charge" if c in EU else "non_eu_reverse_charge"


def _ci_eur(ci: dict) -> dict:
    cur = (ci.get("currency") or "EUR").upper()
    eur = ci.get("eur") or {}
    if cur == "EUR" and not eur:
        eur = {"net_cents": ci.get("net_cents"), "vat_cents": ci.get("vat_cents"), "total_cents": ci.get("total_cents")}
    net, vat = _i(eur.get("net_cents")), _i(eur.get("vat_cents"))
    total = _i(eur.get("total_cents")) or net + vat
    return {"net": net, "vat": vat, "total": total, "missing": cur != "EUR" and not ci.get("eur")}


def purchase_vat(ci: dict, *, exemption: dict | None = None, rate_pct: float = 25.5) -> dict:
    """VAT treatment of one approved carrier invoice in EUR (used by the VAT return and the ledger)."""
    t = _treatment(ci)
    e = _ci_eur(ci)
    ex = exemption or {"exempt": False, "verify": False, "basis": ""}
    out = {"treatment": t, "base_eur_cents": e["net"], "deductible_vat_cents": 0, "rc_tax_cents": 0, "rc_rate_pct": 0.0,
           "total_payable_eur_cents": e["total"], "fields": {}, "exempt": False, "verify": [], "vat_code": None}
    if e["missing"]:
        out["verify"].append(f"Carrier invoice {ci.get('number')} is in {ci.get('currency')} without an EUR breakdown")
    if t == "domestic":
        out.update(deductible_vat_cents=e["vat"], vat_code="KOOS")
        out["fields"] = {"307": e["vat"]}
    elif t in ("eu_reverse_charge", "non_eu_reverse_charge"):
        out["vat_code"] = "EUOS" if t == "eu_reverse_charge" else "MAAL"
        out["total_payable_eur_cents"] = e["total"] or e["net"]
        if ex["exempt"]:
            out["exempt"] = True
            if ex.get("verify"):
                out["verify"].append(f"{ci.get('number')}: {ex['basis']}")
        else:
            tax = _round(Decimal(e["net"]) * Decimal(str(rate_pct)) / 100)
            out.update(rc_tax_cents=tax, deductible_vat_cents=tax, rc_rate_pct=rate_pct)
            if t == "eu_reverse_charge":
                out["fields"] = {"314": e["net"], "306": tax, "307": tax}
            else:
                out["fields"] = {tax_field_for_rate(rate_pct): tax, "307": tax}
    out["exemption"] = ex
    return out


async def standard_rate(on: str | None) -> float:
    from services.billing import vat_rate
    d = date.fromisoformat((on or helsinki_now().date().isoformat())[:10])
    return vat_rate("FI_25_5", d, await get_settings())


async def purchase_vat_for(ci: dict) -> dict:
    t = _treatment(ci)
    ex = await exemption_for(ci) if t in ("eu_reverse_charge", "non_eu_reverse_charge") else None
    return purchase_vat(ci, exemption=ex, rate_pct=await standard_rate(ci.get("invoice_date")))


async def _purchases(p: dict, company_id: str | None = None, test_ids: list[str] | None = None) -> list[dict]:
    """Approved carrier invoices of the period. company_id set: that company's purchases only (its own test records
    included, whatever the ops toggle). Without company_id: every company except test-tenant purchases (own test
    flag, test company, or matched only to test-tenant shipments)."""
    q = {"status": {"$in": list(APPROVED_CI)},
         "invoice_date": {"$gte": p["start"].isoformat(), "$lte": p["end"].isoformat() + "T99"}}
    cinv = getattr(db.carrier_invoices, "raw", db.carrier_invoices)
    if not company_id:
        ids = await test_company_ids(db) if test_ids is None else test_ids
        sids = await test_shipment_ids(ids, db)
        return [ci for ci in await cinv.find(q, NO_ID).to_list(None) if not is_test_record(ci, ids, sids)]
    # AP records are owned through matched shipments, not the supplier or payer.
    shipments = await getattr(db.shipments, "raw", db.shipments).find({"company_id": company_id}, {"_id": 0, "id": 1}).to_list(None)
    shipment_ids = {s["id"] for s in shipments}
    q["$or"] = [{"company_id": company_id}, {"matches.shipment_id": {"$in": list(shipment_ids)}}]
    purchases = await cinv.find(q, NO_ID).to_list(None)
    for ci in purchases:
        matches = ci.get("matches") or []
        # Invoice-level VAT cannot be attributed to one customer by selecting a
        # single match. Fail visibly instead of leaking or inventing a split.
        if ((ci.get("company_id") and ci["company_id"] != company_id)
                or any(m.get("shipment_id") not in shipment_ids for m in matches)):
            raise VatPeriodError("Ostolaskun ALV-kohdistus asiakkaalle on epäselvä; käytä koko yrityksen raporttia.")
    return purchases


async def import_tilitys(period: str, company_id: str | None = None, test_ids: list[str] | None = None) -> list[dict]:
    """Actual decisions per importer. Declaration estimates are deliberately excluded. company_id set: that company's
    declarations (test tenant included); without it every company except test tenants."""
    p = parse_period(period)
    settings = await get_settings()
    own = (settings.get("seller_business_id") or "").replace("-", "")
    out = []
    query = {"vat_route": "tilitys", "customs_decision.decision_date": {
        "$gte": p["start"].isoformat(), "$lte": p["end"].isoformat()}}
    if company_id:
        query["company_id"] = company_id
    else:
        query = exclude_test(query, ids=await test_company_ids(db) if test_ids is None else test_ids)
    async for decl in getattr(db.customs_declarations, "raw", db.customs_declarations).find(query, NO_ID):
        dec = decl["customs_decision"]
        # Ownership and amounts come only from the recorded decision/assessment.
        # A shipment customer or payer is not necessarily the importer of record.
        bid = dec.get("importer_business_id") if dec.get("document_id") else None
        bid = bid.strip() if isinstance(bid, str) and bid.strip() else None
        missing = []
        if not bid:
            missing.append("importer_business_id")
        if not own:
            missing.append("seller_business_id")
        def amount(field, source_field=None):
            value = dec.get(field)
            source = (dec.get(source_field) or {}) if source_field else None
            valid_source = (not source_field or (isinstance(source, dict) and all(
                source.get(k) for k in ("document_id", "basis", "recorded_by", "recorded_at"))))
            valid = type(value) is int and value >= 0
            if source_field:
                valid = valid and valid_source and dec.get("currency") == "EUR" and bool(bid)
            if not valid:
                missing.append(field)
                return None
            return value
        base = amount("vat_base_cents", "vat_base_source")
        deduction = amount("deductible_vat_cents", "deductible_vat_source")
        tax = amount("vat_cents")
        if deduction is not None and (tax is None or deduction > tax):
            deduction = None
            missing.append("deductible_vat_cents")
        out.append({"declaration_id": decl["id"], "mrn": dec.get("mrn") or decl.get("mrn"),
                    "importer": bid, "business_id": bid,
                    "decision_date": dec["decision_date"], "vat_base_cents": base,
                    "vat_cents": tax, "deductible_vat_cents": deduction, "missing_fields": missing,
                    "own_return": (bid.replace("-", "") == own) if own and bid else None,
                    "vat_base_source": dec.get("vat_base_source"),
                    "deductible_vat_source": dec.get("deductible_vat_source"),
                    "source": dec.get("source"), "document_id": dec.get("document_id")})
    return out


# ------------------------------------------------------------------------------------------- the report
async def vat_report(period: str, *, company_id: str | None = None) -> dict:
    """Period figures, optionally limited to one customer's sales, purchases and declarations.
    Scoped views are working papers; import ownership still comes from the decision.
    """
    p = parse_period(period)
    amounts: dict[str, int] = defaultdict(int)
    sources: dict[str, list[dict]] = defaultdict(list)
    verify: list[dict] = []
    excluded: list[dict] = []
    info_bases: dict[str, int] = defaultdict(int)
    ec_lines: dict[tuple, dict] = {}
    test_ids = await test_company_ids(db)  # one lookup per report
    for inv in await _sales(p, company_id, test_ids):
        buyer = inv.get("buyer") or {}
        if inv.get("kind") == "credit_note" and (inv.get("supply_date") or "")[:7] != (inv.get("issue_date") or "")[:7]:
            verify.append({"code": "credit_note_timing",
                           "message_fi": f"Hyvityslasku {inv.get('number')} ilmoitetaan antokaudella {tax_point(inv)[:7]} "
                                         f"(alkuperäinen suoritus {(inv.get('supply_date') or '')[:7]}); tarkista",
                           "message_en": f"Credit note {inv.get('number')} is reported in its issue period "
                                         f"{tax_point(inv)[:7]} (original supply {(inv.get('supply_date') or '')[:7]}); verify"})
        for g in invoice_vat_groups(inv):
            ref = {"type": "invoice", "id": inv.get("id"), "number": inv.get("number"), "kind": inv.get("kind"),
                   "tax_point": tax_point(inv), "company": inv.get("company_name"), "vat_code": g["vat_code"],
                   "base_cents": g["base_cents"], "vat_cents": g["vat_cents"], "path": f"/ops/billing/{inv.get('id')}"}
            kind = g["kind"]
            if kind in ("standard", "reduced"):
                f = tax_field_for_rate(g["rate_pct"])
                amounts[f] += g["vat_cents"]
                info_bases[f] += g["base_cents"]
                sources[f].append({**ref, "amount_cents": g["vat_cents"]})
            elif kind == "disbursement":
                excluded.append({**ref, "reason_fi": "Läpikulkuerä (AVL 79 §), ei ilmoiteta",
                                 "reason_en": "Disbursement (VAT Act 79 §), not reported"})
            elif kind in SALES_FIELD_BY_KIND:
                f = SALES_FIELD_BY_KIND[kind]
                amounts[f] += g["base_cents"]
                sources[f].append({**ref, "amount_cents": g["base_cents"]})
                if g["vat_cents"]:
                    verify.append({"code": f, "message_fi": f"Laskulla {inv.get('number')} on veroa verottomalla koodilla",
                                   "message_en": f"Invoice {inv.get('number')} has VAT on a zero-rated code"})
                if kind == "reverse_charge":
                    vat_id = buyer.get("vat_id")
                    cc = eu_country_of(vat_id, buyer.get("country"))
                    if not vat_id or cc not in EU or cc == "FI":
                        verify.append({"code": "312", "message_fi": f"Laskun {inv.get('number')} ostajalta puuttuu EU-ALV-tunniste",
                                       "message_en": f"Invoice {inv.get('number')}: buyer has no EU VAT id"})
                    key = (cc, vat_number_without_prefix(vat_id))
                    line = ec_lines.setdefault(key, {"country_code": cc, "vat_number": key[1], "vat_id": vat_id,
                                                     "buyer": buyer.get("name") or inv.get("company_name"),
                                                     "goods_cents": 0, "services_cents": 0, "triangulation_cents": 0,
                                                     "months": defaultdict(int), "sources": []})
                    line["services_cents"] += g["base_cents"]
                    line["months"][int((tax_point(inv) or "0000-00")[5:7])] += g["base_cents"]
                    line["sources"].append(ref)
            else:
                verify.append({"code": "?", "message_fi": f"Tuntematon ALV-koodi {g['vat_code']} laskulla {inv.get('number')}",
                               "message_en": f"Unknown VAT code {g['vat_code']} on invoice {inv.get('number')}"})
    purchases_zero = []
    for ci in await _purchases(p, company_id, test_ids):
        if _ci_eur(ci)["missing"]:
            raise VatPeriodError(f"Ostolaskun {ci.get('number')} EUR-muunnos puuttuu. Korjaa kurssi ennen ALV-raporttia.")
        pv = await purchase_vat_for(ci)
        ref = {"type": "carrier_invoice", "id": ci.get("id"), "number": ci.get("number"),
               "supplier": (ci.get("supplier") or {}).get("name"), "invoice_date": ci.get("invoice_date"),
               "treatment": pv["treatment"], "base_cents": pv["base_eur_cents"],
               "path": f"/ops/finance/carrier-invoices/{ci.get('id')}"}
        for f, v in pv["fields"].items():
            amounts[f] += v
            sources[f].append({**ref, "amount_cents": v})
        for msg in pv["verify"]:
            verify.append({"code": "314" if pv["treatment"] == "eu_reverse_charge" else "301", "message_fi": msg,
                           "message_en": msg})
        if not pv["fields"]:
            purchases_zero.append({**ref, "exempt": pv["exempt"], "basis": (pv.get("exemption") or {}).get("basis")})
    tilitys = await import_tilitys(period, company_id, test_ids)
    for imp in tilitys:
        if imp["own_return"] is False:
            continue
        if imp["own_return"] is None or imp["missing_fields"]:
            verify.append({"code": "import_missing", "message_fi": "Tuonnin päätöksen maahantuoja, veropohja tai vähennysoikeus puuttuu: " + str(imp["mrn"]),
                           "message_en": "Import decision owner, base or deductible VAT missing: " + str(imp["mrn"])})
            continue
        for field, value in (("310", imp["vat_base_cents"]), ("304", imp["vat_cents"]), ("307", imp["deductible_vat_cents"])):
            amounts[field] += value
            sources[field].append({"type": "customs_decision", "id": imp["declaration_id"], "amount_cents": value})
    payable = sum(amounts[f] for f in TAX_FIELDS) - amounts["307"]
    amounts["308"] = payable
    rows = []
    for code, (fi, en, api) in FIELDS.items():
        rows.append({"code": code, "label_fi": fi, "label_en": en, "amount_cents": amounts.get(code, 0),
                     "source_count": len(sources.get(code, [])), "api_field": api or None,
                     **({"base_info_cents": info_bases[code]} if code in info_bases else {})})
    ec = sorted(({k: v for k, v in ln.items() if k != "months"} | {"months": dict(ln["months"])} for ln in ec_lines.values()),
                key=lambda x: (x["country_code"], x["vat_number"]))
    no_activity = not any(amounts.get(c) for c in FIELDS)
    settings = await get_settings()
    return {
        "period": p["period"], "period_kind": p["kind"], "start": p["start"].isoformat(), "end": p["end"].isoformat(),
        "due_date": due_date(p).isoformat(), "company_id": company_id,
        "business_id": settings.get("seller_business_id") or None,
        "rows": rows, "amounts": {c: amounts.get(c, 0) for c in FIELDS},
        "tax_payable_cents": payable, "payable": payable >= 0, "no_activity": no_activity,
        "reconciliation": {code: srcs for code, srcs in sources.items()},
        "excluded": excluded, "purchases_not_reported": purchases_zero, "ec_sales": ec, "import_tilitys": tilitys,
        "verify": verify, "filing_mode": filing_mode(),
        "sources": {"record": SRC_VSRALVKV, "instructions": SRC_TAYTTOOHJE, "guide": SRC_GUIDE_48679},
        "generated_at": now_iso(),
    }


async def ec_sales_list(period: str, *, company_id: str | None = None) -> dict:
    """Yhteenvetoilmoitus lines per buyer VAT id: services (field 211) from EU_ICS_RC sales; goods 210 and triangulation
    212 are 0 (the forwarder sells services only). The list is monthly (VSRALVYV 052 = month)."""
    rep = await vat_report(period, company_id=company_id)
    lines = rep["ec_sales"]
    return {"period": rep["period"], "items": lines, "total": len(lines),
            "services_total_cents": sum(x["services_cents"] for x in lines),
            "verify": [v for v in rep["verify"] if v["code"] == "312"], "source": SRC_VSRALVYV}


# ------------------------------------------------------------------------------------------- exports
def filing_mode() -> str:
    v = (config.env("VAT_FILING_MODE", "file") or "file").lower()
    return v if v in ("file", "api") else "file"


def _software_fields(settings: dict) -> tuple[str, str]:
    """014 = software producer's Y-tunnus + '_' + 2 characters; 048 = software name (AN35)."""
    sw_id = config.env("VERO_SOFTWARE_ID", "") or f"{settings.get('seller_business_id') or ''}_HU"
    return sw_id, (config.env("VERO_SOFTWARE_NAME", "Huolitsija") or "Huolitsija")[:35]


def _require_business_id(settings: dict) -> str:
    from core.refs import ytunnus_valid
    bid = (settings.get("seller_business_id") or "").strip()
    if not ytunnus_valid(bid):
        raise VatPeriodError("Set the company's Y-tunnus (settings seller_business_id / COMPANY_BUSINESS_ID) before "
                             "exporting a Vero file")
    return bid


def _stamp() -> str:
    return datetime.now(HELSINKI).strftime("%d%m%Y%H%M%S")  # PPKKVVVVHHMMSS


def vsralvkv_lines(rep: dict, p: dict, *, business_id: str, software_id: str, software_name: str,
                   phone: str | None = None, correction_reason: str | None = None) -> list[str]:
    lines = ["000:VSRALVKV", f"198:{_stamp()}", f"048:{software_name}", f"014:{software_id}", f"010:{business_id}",
             f"050:{p['code']}"]
    if p["code"] in ("K", "Q"):
        lines.append(f"052:{p['number']}")
    lines.append(f"053:{p['year']}")
    if rep["no_activity"]:
        lines.append("056:1")
    else:
        for code in ("301", "302", "303", "304", "305", "306", "307", "308", "309", "310", "311", "312", "313", "314",
                     "318", "319", "320"):
            lines.append(f"{code}:{money(rep['amounts'].get(code, 0))}")
    if correction_reason in ("332", "333", "334", "335"):
        lines.append(f"{correction_reason}:1")
    if phone:
        lines.append(f"042:{phone[:35]}")
    lines.append("999:1")
    return lines


def vsralvyv_records(ec_lines: list[dict], p: dict, *, business_id: str, software_id: str, software_name: str) -> list[list[str]]:
    """One VSRALVYV record per calendar month that has EU sales (the list is monthly)."""
    records = []
    for month in p["months"]:
        buyers = [(ln, ln["months"].get(month, 0) if isinstance(ln.get("months"), dict) else ln["services_cents"])
                  for ln in ec_lines]
        buyers = [(ln, v) for ln, v in buyers if v]
        if not buyers:
            continue
        rec = ["000:VSRALVYV", f"198:{_stamp()}", f"048:{software_name}", f"014:{software_id}", f"010:{business_id}",
               f"052:{month}", f"053:{p['year']}", f"001:{len(buyers)}"]
        for n, (ln, v) in enumerate(buyers, start=1):
            rec += [f"102:{ln['country_code']}", f"103:{ln['vat_number']}", f"210:{money(0)}", f"211:{money(v)}",
                    f"212:{money(0)}", f"009:{n}"]
        records.append(rec)
    return records


def _encode(records: list[list[str]]) -> bytes:
    out = []
    for i, rec in enumerate(records, start=1):
        rec = rec[:-1] + [f"999:{i}"] if rec[-1].startswith("999:") else rec + [f"999:{i}"]
        out.extend(rec)
    return ("\r\n".join(out) + "\r\n").encode("iso-8859-1", errors="replace")


def _months_ec(rep: dict) -> list[dict]:
    lines = []
    for ln in rep["ec_sales"]:
        months = {int(k): v for k, v in (ln.get("months") or {}).items()}
        lines.append({**ln, "months": months})
    return lines


async def export_vat(period: str, fmt: str, *, company_id: str | None = None) -> tuple[bytes, str, str]:
    rep = await vat_report(period, company_id=company_id)
    p = parse_period(period)
    if fmt == "vero" and any(v["code"] == "import_missing" for v in rep["verify"]):
        raise VatPeriodError("Import decision owner, VAT base and deduction must be recorded before filing")
    if fmt == "json":
        import json
        return json.dumps(rep, ensure_ascii=False, indent=1).encode("utf-8"), f"alv_{rep['period']}.json", "application/json"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["kenttä/code", "nimike", "label", "euroa/EUR", "lähteitä/sources"])
        for r in rep["rows"]:
            w.writerow([r["code"], r["label_fi"], r["label_en"], money(r["amount_cents"]), r["source_count"]])
        w.writerow([])
        w.writerow(["kenttä/code", "tyyppi/type", "numero/number", "päivä/date", "osapuoli/party", "ALV-koodi", "euroa/EUR"])
        for code, srcs in sorted(rep["reconciliation"].items()):
            for s in srcs:
                w.writerow([code, s["type"], s.get("number") or "", s.get("tax_point") or s.get("invoice_date") or "",
                            s.get("company") or s.get("supplier") or "", s.get("vat_code") or s.get("treatment") or "",
                            money(s["amount_cents"])])
        for s in rep["excluded"]:
            w.writerow(["-", "excluded", s.get("number") or "", s.get("tax_point") or "", s.get("company") or "",
                        s.get("vat_code"), money(s["base_cents"])])
        return ("﻿" + buf.getvalue()).encode("utf-8"), f"alv_{rep['period']}.csv", "text/csv; charset=utf-8"
    settings = await get_settings()
    bid = _require_business_id(settings)
    sw_id, sw_name = _software_fields(settings)
    lines = vsralvkv_lines(rep, p, business_id=bid, software_id=sw_id, software_name=sw_name,
                           phone=config.env("VAT_CONTACT_PHONE", "") or None)
    data = _encode([lines])
    stamp = datetime.now(HELSINKI).strftime("%Y%m%d_%H%M%S")
    return data, f"V_{stamp}_VSRALVKV_{rep['period'].replace('-', '')}.TXT", "text/plain; charset=iso-8859-1"


async def export_ec_sales(period: str, fmt: str, *, company_id: str | None = None) -> tuple[bytes, str, str]:
    rep = await vat_report(period, company_id=company_id)
    p = parse_period(period)
    lines = _months_ec(rep)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["maatunnus/country", "ALV-tunniste/VAT number", "ostaja/buyer", "tavaramyynnit/goods (210)",
                    "palvelumyynnit/services (211)", "kolmikantakauppa/triangulation (212)"])
        for ln in lines:
            w.writerow([ln["country_code"], ln["vat_number"], ln.get("buyer") or "", money(0), money(ln["services_cents"]),
                        money(0)])
        return ("﻿" + buf.getvalue()).encode("utf-8"), f"yhteenveto_{rep['period']}.csv", "text/csv; charset=utf-8"
    if fmt == "json":
        import json
        return json.dumps(await ec_sales_list(period, company_id=company_id), ensure_ascii=False).encode(), \
            f"yhteenveto_{rep['period']}.json", "application/json"
    settings = await get_settings()
    bid = _require_business_id(settings)
    sw_id, sw_name = _software_fields(settings)
    records = vsralvyv_records(lines, p, business_id=bid, software_id=sw_id, software_name=sw_name)
    if not records:
        raise VatPeriodError("No EU sales in this period: the EU sales list is filed only for months with EU sales")
    stamp = datetime.now(HELSINKI).strftime("%Y%m%d_%H%M%S")
    return _encode(records), f"V_{stamp}_VSRALVYV_{rep['period'].replace('-', '')}.TXT", "text/plain; charset=iso-8859-1"


# ------------------------------------------------------------------------------------------- Vero API (VAT_FILING_MODE=api)
def api_vat_payload(rep: dict, *, business_id: str, contact_name: str, contact_phone: str,
                    replacement_reason: str | None = None) -> dict:
    """FileVATReturn/v2 request body (schema FileVATReturnv2Request in data/vero/vero_api_SAT_schemas.json).
    FilingPeriod: 'yyyy-MM-dd' as returned by GetVATPeriods — we send the period's last day; verify against
    GetVATPeriods before going live."""
    a = rep["amounts"]
    body: dict = {"BusinessId": business_id, "FilingPeriod": rep["end"],
                  "ContactDetails": {"FullName": contact_name[:35], "PhoneNumber": contact_phone[:35]}}
    if replacement_reason:
        body.update(ReplacementReturn=True, ReplacementReason=replacement_reason)
    if rep["no_activity"]:
        body["NoActivity"] = True
        return body
    body["VATDetails"] = {
        "VATOnDomesticSalesByTaxRate": {"HighVATRate": money_float(a["301"]), "MediumVATRate": money_float(a["302"]),
                                        "LowVATRate": money_float(a["303"])},
        "VATOnPurchasesAndImports": {"GoodsFromEUMemberStates": money_float(a["305"]),
                                     "ServicesFromEUMemberStates": money_float(a["306"]),
                                     "ImportOfGoodsOutsideEU": money_float(a["304"]),
                                     "ConstructionServicesAndScrapMetal": money_float(a["318"])},
        "DeductibleVAT": money_float(a["307"]),
        "SalesPurchasesImports": {"ZeroVATRateTurnover": money_float(a["309"]),
                                  "SalesOfGoodsToEUMemberStates": money_float(a["311"]),
                                  "SalesOfServicesToEUMemberStates": money_float(a["312"]),
                                  "PurchasesOfGoodsFromEUMemberStates": money_float(a["313"]),
                                  "PurchasesOfServicesFromEUMemberStates": money_float(a["314"]),
                                  "ImportsOfGoodsFromOutsideEU": money_float(a["310"]),
                                  "SalesOfConstructionServicesAndScrapMetal": money_float(a["319"]),
                                  "PurchasesOfConstructionServicesAndScrapMetal": money_float(a["320"])}}
    return body


def api_ec_payload(lines: list[dict], *, month: int, year: int, business_id: str, contact_name: str,
                   contact_phone: str) -> dict:
    """FileECSalesList/v1 request body (schema FileECSalesListRequest)."""
    buyers = []
    for ln in lines:
        v = ln["months"].get(month, 0) if isinstance(ln.get("months"), dict) else ln["services_cents"]
        if v:
            buyers.append({"CountryCode": ln["country_code"], "VATIdentifier": ln["vat_number"], "SalesOfGoods": 0.0,
                           "SalesOfServices": money_float(v), "TriangulationSales": 0.0})
    return {"BusinessId": business_id, "FilingPeriod": {"Month": month, "Year": year}, "Buyers": buyers,
            "ContactDetails": {"FullName": contact_name[:35], "PhoneNumber": contact_phone[:35]}}


async def submit_via_api(endpoint: str, payload: dict) -> dict:
    """POST to the Vero API with the client certificate. NOT PROVEN: needs VERO_API_BASE (e.g. the test host
    https://apitest.vero.fi/... path from the API portal), VERO_API_CERT + VERO_API_KEY (certificate from Vero's
    certificate service), VERO_API_SOFTWARE_KEY (Vero-SoftwareKey) and, when filing for a client company,
    VERO_API_AUTH_TOKEN (Suomi.fi authorisation, Vero-AuthorizationToken). Sandbox: VERO_API_BASE=
    https://api-sandbox.vero.fi/Return/SAT with VERO_API_SUBSCRIPTION_KEY (Ocp-Apim-Subscription-Key)."""
    import httpx
    base = config.env("VERO_API_BASE", "")
    if not base:
        return {"ok": False, "status": None, "error": "VERO_API_BASE is not set; the return was not sent"}
    headers = {"Content-Type": "application/json"}
    for env_name, header in (("VERO_API_SOFTWARE_KEY", "Vero-SoftwareKey"), ("VERO_API_SOFTWARE_ID", "Vero-SoftwareId"),
                             ("VERO_API_AUTH_TOKEN", "Vero-AuthorizationToken"),
                             ("VERO_API_SUBSCRIPTION_KEY", "Ocp-Apim-Subscription-Key")):
        if config.env(env_name, ""):
            headers[header] = config.env(env_name)
    cert = (config.env("VERO_API_CERT"), config.env("VERO_API_KEY")) if config.env("VERO_API_CERT", "") else None
    try:
        async with httpx.AsyncClient(timeout=60, cert=cert) as client:
            r = await client.post(base.rstrip("/") + endpoint, json=payload, headers=headers)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text[:2000]}
        return {"ok": r.status_code == 200, "status": r.status_code, "response": body}
    except Exception as exc:  # noqa: BLE001 - reported back to the person filing
        return {"ok": False, "status": None, "error": str(exc)[:500]}


# ------------------------------------------------------------------------------------------- period close + job
async def close_period(period: str | None, *, actor: dict | None = None, submit: bool = False) -> dict:
    """Store the period's report in `vat_reports` (one row per period; a filed row is never overwritten).
    submit=True with VAT_FILING_MODE=api also sends it to the Vero API."""
    from core.audit import audit
    period = period or await previous_period()
    rep = await vat_report(period)
    existing = await db.vat_reports.find_one({"period": rep["period"]}, NO_ID)
    if existing and existing.get("status") == "filed":
        return existing
    doc = {"period": rep["period"], "period_kind": rep["period_kind"], "start": rep["start"], "end": rep["end"],
           "due_date": rep["due_date"], "rows": rep["rows"], "amounts": rep["amounts"],
           "tax_payable_cents": rep["tax_payable_cents"], "verify": rep["verify"], "ec_sales": rep["ec_sales"],
           "reconciliation": rep["reconciliation"], "excluded": rep["excluded"], "status": "draft",
           "filing_mode": filing_mode(), "updated_at": now_iso()}
    if submit and filing_mode() == "api":
        if any(v["code"] == "import_missing" for v in rep["verify"]):
            raise VatPeriodError("Import decision owner, VAT base and deduction must be recorded before filing")
        settings = await get_settings()
        bid = _require_business_id(settings)
        payload = api_vat_payload(rep, business_id=bid, contact_name=config.env("VAT_CONTACT_NAME", "") or "Talous",
                                  contact_phone=config.env("VAT_CONTACT_PHONE", "") or "-",
                                  replacement_reason="CLC" if existing and existing.get("filing", {}).get("ok") else None)
        res = await submit_via_api("/FileVATReturn/v2", payload)
        doc["filing"] = {"mode": "api", "at": now_iso(), **res}
        if res.get("ok"):
            doc["status"] = "filed"
    await db.vat_reports.update_one({"period": rep["period"]}, {"$set": doc,
                                                                "$setOnInsert": {"id": new_id(), "created_at": now_iso()}},
                                    upsert=True)
    await audit(actor, "vat_period_closed", "vat_report", rep["period"],
                {"tax_payable_cents": rep["tax_payable_cents"], "status": doc["status"]})
    return await db.vat_reports.find_one({"period": rep["period"]}, NO_ID)


async def stored_reports() -> dict:
    items = await db.vat_reports.find({}, {"_id": 0, "reconciliation": 0}).sort("period", -1).to_list(36)
    return {"items": items, "total": len(items)}


@finance_job("finance.vat_period_close", monthly_day=12, daily_at="06:00", label_fi="ALV-kauden sulkeminen",
             label_en="VAT period close")
async def vat_period_close_job(j: dict):
    """12th of each month 06:00 Helsinki: store the previous period's VAT report and tell finance."""
    payload = (j or {}).get("payload") or {}
    today = helsinki_now().date()
    if today.day != 12 and not payload.get("period") and not payload.get("force"):
        return {"skipped": "not the 12th"}  # s2's runtime schedules daily; this job runs on the 12th
    length = (await get_settings()).get("vat_period_length") or "month"
    if length == "quarter" and today.month not in (2, 5, 8, 11) and not payload.get("period"):
        return {"skipped": "quarterly filer: closes on the 12th of the second month after the quarter"}
    period = payload.get("period") or await previous_period(today)
    doc = await close_period(period)
    from core.notify import bi, notify_role
    eur = money(doc["tax_payable_cents"])
    await notify_role("finance", bi(f"ALV-ilmoitus {period} valmis tarkistettavaksi", f"VAT return {period} ready to review"),
                      bi(f"Maksettava vero {eur} €, eräpäivä {doc['due_date']}"
                         + (f"; tarkistettavaa {len(doc['verify'])}" if doc.get("verify") else ""),
                         f"Tax payable {eur} EUR, due {doc['due_date']}"
                         + (f"; {len(doc['verify'])} item(s) to verify" if doc.get("verify") else "")),
                      link=f"/ops/finance/vat?period={period}")
    return {"period": period, "tax_payable_cents": doc["tax_payable_cents"], "status": doc["status"]}
