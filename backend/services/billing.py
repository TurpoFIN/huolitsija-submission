"""Invoicing (V4): invoices from delivered shipments, VAT per line, Finnish references, credit notes, overdue sweep.

Lifecycle (domain-spec 13.2: DELIVERED -> INVOICED -> PAID -> CLOSED):
    draft --issue--> open --due date passes--> overdue
    open|overdue|processing --payment recorded (Stripe or bank transfer)--> paid
    draft|open|overdue --void--> void           (issued invoices keep their number; drafts have none)
    open|overdue|paid --credit note--> credited (full credit) / amount due reduced (partial)

Numbers and payment references are assigned when an invoice is issued, so the number series has no gaps
from discarded drafts. VAT codes follow RULE V-1; the rate table below is data with valid_from dates.
Zero-rated and out-of-scope lines cite a legal basis text from the environment (RULE V-4); until the tax
adviser has approved the wording the built-in default text is used and the line is marked
`legal_basis_status = "pending_tax_adviser_review"`.
"""
from __future__ import annotations

import contextvars

import math

import logging
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from core import config
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import emit, on
from core.lifecycle import background_loop
from core.refs import fi_reference, next_ref, rf_reference
from core.sandbox import is_test_company
from core.settings import get_settings
from core.util import NO_ID, new_id, now_iso, parse_iso

log = logging.getLogger("billing")
# Set by the demo seed: invoices are created through the real path but no e-mail leaves for fictional customers.
QUIET_EMAIL: ContextVar[bool] = ContextVar("billing_quiet_email", default=False)
HELSINKI = ZoneInfo("Europe/Helsinki")

register_index("invoices", "id", unique=True)
register_index("invoices", [("company_id", 1), ("status", 1), ("due_date", 1)])
register_index("invoices", "shipment_id")
register_index("invoices", "number")
register_index("invoices", "reference")
register_index("payment_transactions", "session_id", unique=True, sparse=True)
register_index("payment_transactions", "invoice_id")

EU_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
                "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
OPEN_STATES = ("open", "overdue", "processing")
STATUSES = ("draft", "open", "processing", "paid", "overdue", "void", "credited")


class BillingError(Exception):
    """A business rule stopped the operation. The message is a plain sentence for the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------------------------- dates

# A tenant-scoped test run (services.test_tenant_finance) may set a later date for its own request only.
today_override: contextvars.ContextVar = contextvars.ContextVar("billing_today_override", default=None)


def real_today_fi() -> date:
    return datetime.now(HELSINKI).date()


def today_fi() -> date:
    from core.util import now
    return today_override.get() or now().astimezone(HELSINKI).date()


def to_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        dt = parse_iso(value) if not isinstance(value, datetime) else value
    except (TypeError, ValueError):
        return None  # not an ISO date; callers turn None into a plain 400
    if dt is None:
        return None
    return dt.astimezone(HELSINKI).date() if dt.tzinfo else dt.date()


# --------------------------------------------------------------------------------------------- VAT (RULE V-1)

_PENDING = "pending_tax_adviser_review"

# Rate table is data. `rates` = [(valid_from ISO date, rate %)]. FI_25_5 takes its current rate from settings
# (vat_rate_standard_pct) so a future change is a settings edit plus a new row here for the history.
VAT_CODES: dict[str, dict] = {
    "FI_25_5": {"rates": [("2013-01-01", 24.0), ("2024-09-01", None)], "kind": "standard",
                "label": {"fi": "ALV {rate} %", "en": "VAT {rate} %"}},
    # Reduced rate for foodstuffs, feed, pharmaceuticals, books: 13.5 % (Tulli VAT rates page, domain-spec 4.2).
    # The code keeps its RULE V-1 name; FI_13_5 is an alias with the same table.
    "FI_14": {"rates": [("2013-01-01", 14.0), ("2026-01-01", 13.5)], "kind": "reduced", "alias_of": "FI_13_5",
              "label": {"fi": "ALV {rate} %", "en": "VAT {rate} %"}},
    "FI_13_5": {"rates": [("2013-01-01", 14.0), ("2026-01-01", 13.5)], "kind": "reduced",
                "label": {"fi": "ALV {rate} %", "en": "VAT {rate} %"}},
    "FI_10": {"rates": [("2013-01-01", 10.0)], "kind": "reduced",
              "label": {"fi": "ALV {rate} %", "en": "VAT {rate} %"}},
    "ZERO_INTL_TRANSPORT": {"rates": [("1995-01-01", 0.0)], "kind": "zero", "env": "VAT_TEXT_ZERO_INTL_TRANSPORT",
                            "env_aliases": ["VAT_TEXT_ZERO_INTL"],  # the name the quote PDF used before 2026-10
                            "label": {"fi": "ALV 0 %, kansainvälinen kuljetus", "en": "VAT 0 %, international transport"},
                            "default_text": {
                                "fi": "Veroton kansainvälinen kuljetuspalvelu, joka liittyy välittömästi tavaroiden "
                                      "vientiin tai maahantuontiin (AVL 71 §; ALV-direktiivi 2006/112/EY 144 art. ja "
                                      "146 art. 1 kohdan e alakohta).",
                                "en": "VAT 0 %: international transport directly connected with the export or import "
                                      "of goods (Finnish VAT Act 71 §; Directive 2006/112/EC art. 144 and "
                                      "art. 146(1)(e))."}},
    "ZERO_EXPORT": {"rates": [("1995-01-01", 0.0)], "kind": "zero", "env": "VAT_TEXT_ZERO_EXPORT",
                    "label": {"fi": "ALV 0 %, vienti", "en": "VAT 0 %, export"},
                    "default_text": {
                        "fi": "Veroton vientiin liittyvä palvelu (AVL 70–71 §; ALV-direktiivi 2006/112/EY 146 art.).",
                        "en": "VAT 0 %: service connected with the export of goods (Finnish VAT Act 70–71 §; "
                              "Directive 2006/112/EC art. 146)."}},
    "EU_ICS_RC": {"rates": [("1995-01-01", 0.0)], "kind": "reverse_charge", "env": "VAT_TEXT_REVERSE_CHARGE",
                  "label": {"fi": "Käännetty verovelvollisuus", "en": "Reverse charge"},
                  "default_text": {
                      "fi": "Käännetty verovelvollisuus. Ostaja on verovelvollinen (ALV-direktiivi 2006/112/EY "
                            "44 ja 196 art.).",
                      "en": "Reverse charge: VAT to be accounted for by the recipient (Directive 2006/112/EC "
                            "art. 44 and 196)."}},
    "OUT_OF_SCOPE_DISBURSEMENT": {"rates": [("1995-01-01", 0.0)], "kind": "disbursement", "env": "VAT_TEXT_DISBURSEMENT",
                                  "label": {"fi": "Läpikulkuerä", "en": "Disbursement"},
                                  "default_text": {
                                      "fi": "Läpikulkuerä: asiakkaan nimissä ja lukuun maksettu, ei kuulu "
                                            "arvonlisäveron perusteeseen (AVL 79 §; ALV-direktiivi 79 art. c).",
                                      "en": "Disbursement paid in the name and on behalf of the customer, outside "
                                            "the taxable amount (Finnish VAT Act 79 §; Directive 2006/112/EC "
                                            "art. 79(c))."}},
    "OUT_OF_SCOPE_FOREIGN": {"rates": [("1995-01-01", 0.0)], "kind": "out_of_scope", "env": "VAT_TEXT_OUT_OF_SCOPE_FOREIGN",
                             "label": {"fi": "Ei Suomen ALV:n piirissä", "en": "Outside the scope of Finnish VAT"},
                             "default_text": {
                                 "fi": "Myyntimaa ei ole Suomi; palvelun ostaja on EU:n ulkopuolinen elinkeinonharjoittaja "
                                       "(ALV-direktiivi 2006/112/EY 44 art.).",
                                 "en": "Place of supply outside Finland: business customer established outside the EU "
                                       "(Directive 2006/112/EC art. 44)."}},
}

PASS_THROUGH_CUSTOMS_CODES = ("DUTY", "IMPORT_VAT", "EXCISE", "CUSTOMS_VAT", "TULLI")


def vat_rate(code: str, on: date, settings: dict) -> float:
    spec = VAT_CODES.get(code)
    if not spec:
        raise BillingError(f"Unknown VAT code {code}")
    rate = 0.0
    for valid_from, r in spec["rates"]:
        if on >= date.fromisoformat(valid_from):
            rate = float(settings.get("vat_rate_standard_pct", 25.5)) if r is None else float(r)
    return rate


def legal_basis(code: str) -> dict | None:
    """Legal basis text for zero-rated / out-of-scope codes (RULE V-4). From env when approved, else default."""
    spec = VAT_CODES.get(code) or {}
    if "default_text" not in spec:
        return None
    return configured_vat_text(code) or {**spec["default_text"], "status": _PENDING, "source": "default"}


def configured_vat_text(code: str) -> dict | None:
    """The approved legal-basis wording from env ({env}_FI / {env}_EN, then each alias), shared by invoices and quote
    PDFs so one setting changes both. None when nothing is configured."""
    spec = VAT_CODES.get(code) or {}
    for name in [spec["env"], *spec.get("env_aliases", [])] if spec.get("env") else []:
        env_fi, env_en = config.env(name + "_FI"), config.env(name + "_EN")
        if env_fi or env_en:
            return {"fi": env_fi or env_en, "en": env_en or env_fi, "status": "approved", "source": "config", "env": name}
    return None


def vat_label(code: str, rate: float, locale: str = "fi") -> str:
    spec = VAT_CODES.get(code) or {}
    tmpl = (spec.get("label") or {}).get(locale) or code
    rate_txt = (f"{rate:.1f}".rstrip("0").rstrip(".")) if rate else "0"
    if locale == "fi":
        rate_txt = rate_txt.replace(".", ",")
    return tmpl.replace("{rate}", rate_txt)


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def suggest_vat_code(line: dict, shipment: dict | None, company: dict | None) -> tuple[str, str]:
    """VAT code for a line that arrives without one, with the reason. Pass-through -> disbursement.

    Customer abroad: EU business with VAT id -> reverse charge (B2B general rule); outside EU -> out of scope.
    Finnish customer: domestic haulage 25.5 %; transport connected with import/export across the EU border 0 %;
    intra-EU transport for a Finnish business 25.5 % (place of supply Finland, no export/import exemption)."""
    if line.get("is_pass_through"):
        return "OUT_OF_SCOPE_DISBURSEMENT", "pass-through disbursement"
    country = ((company or {}).get("country") or (company or {}).get("address", {}).get("country") or "FI").upper()
    if country != "FI":
        if country in EU_COUNTRIES and (company or {}).get("vat_id"):
            return "EU_ICS_RC", f"business customer in {country} (B2B, place of supply {country})"
        if country not in EU_COUNTRIES:
            return "OUT_OF_SCOPE_FOREIGN", f"business customer outside the EU ({country})"
    s = shipment or {}
    oc = ((s.get("origin") or {}).get("country") or "").upper()
    dc = ((s.get("destination") or {}).get("country") or "").upper()
    code = (line.get("code") or "").upper()
    domestic_leg = any(k in code for k in ("DELIVERY", "PICKUP", "PICK_UP", "HAULAGE", "DOMESTIC", "DISTRIBUTION"))
    if oc == "FI" and dc == "FI":
        return "FI_25_5", "domestic transport in Finland"
    crosses_eu_border = (oc and oc not in EU_COUNTRIES) or (dc and dc not in EU_COUNTRIES)
    if crosses_eu_border:
        if domestic_leg and s.get("direction") == "domestic":
            return "FI_25_5", "domestic leg"
        return "ZERO_INTL_TRANSPORT", "transport connected with import/export across the EU border"
    return "FI_25_5", "intra-EU transport for a Finnish business customer (verify with tax adviser)"


def compute_totals(lines: list[dict], *, on: date, settings: dict) -> dict:
    """Fills vat_rate_pct / vat_cents on each line and returns totals + per-rate breakdown.

    VAT is computed per (code, rate) group on the summed base and allocated back to lines with the
    largest-remainder method, so line VATs add up to the breakdown exactly."""
    groups: dict[tuple[str, float], list[int]] = {}
    for i, ln in enumerate(lines):
        code = ln.get("vat_code") or "FI_25_5"
        if code not in VAT_CODES:
            raise BillingError(f"Line {i + 1}: unknown VAT code {code}")
        rate = vat_rate(code, on, settings)
        ln["vat_code"], ln["vat_rate_pct"] = code, rate
        qty = Decimal(str(ln.get("qty") if ln.get("qty") not in (None, "") else 1))
        if ln.get("unit_price_cents") is None and ln.get("amount_cents") is not None:
            ln["unit_price_cents"] = int(ln["amount_cents"]) if qty == 1 else _round(Decimal(int(ln["amount_cents"])) / qty)
        unit = Decimal(int(ln.get("unit_price_cents") or 0))
        ln["qty"] = float(qty) if qty != qty.to_integral_value() else int(qty)
        ln["amount_cents"] = _round(qty * unit) if ln.get("amount_cents") is None or ln.get("_recalc", True) else int(ln["amount_cents"])
        ln.pop("_recalc", None)
        lb = legal_basis(code)
        ln["legal_basis"] = lb
        groups.setdefault((code, rate), []).append(i)

    breakdown = []
    for (code, rate), idxs in groups.items():
        base = sum(lines[i]["amount_cents"] for i in idxs)
        vat_total = _round(Decimal(base) * Decimal(str(rate)) / Decimal(100))
        exact = [Decimal(lines[i]["amount_cents"]) * Decimal(str(rate)) / Decimal(100) for i in idxs]
        floors = [int(e.to_integral_value(rounding="ROUND_FLOOR")) for e in exact]
        remainder = vat_total - sum(floors)
        order = sorted(range(len(idxs)), key=lambda k: exact[k] - floors[k], reverse=remainder >= 0)
        alloc = list(floors)
        step = 1 if remainder >= 0 else -1
        for k in order[:abs(remainder)]:
            alloc[k] += step
        for k, i in enumerate(idxs):
            lines[i]["vat_cents"] = alloc[k]
        spec = VAT_CODES[code]
        breakdown.append({"vat_code": code, "rate_pct": rate, "base_cents": base, "vat_cents": vat_total,
                          "kind": spec["kind"], "label_fi": vat_label(code, rate, "fi"), "label_en": vat_label(code, rate, "en"),
                          "legal_basis": legal_basis(code)})
    kind_order = {"standard": 0, "reduced": 1, "zero": 2, "reverse_charge": 3, "out_of_scope": 4, "disbursement": 5}
    breakdown.sort(key=lambda b: (kind_order.get(b["kind"], 9), -b["rate_pct"], b["vat_code"]))
    subtotal = sum(ln["amount_cents"] for ln in lines)
    pass_through = sum(ln["amount_cents"] for ln in lines if ln.get("is_pass_through"))
    vat = sum(b["vat_cents"] for b in breakdown)
    return {"subtotal_cents": subtotal, "services_net_cents": subtotal - pass_through, "pass_through_cents": pass_through,
            "vat_cents": vat, "total_cents": subtotal + vat, "vat_breakdown": breakdown,
            "legal_basis_pending": any((b.get("legal_basis") or {}).get("status") == _PENDING for b in breakdown)}


# --------------------------------------------------------------------------------------------- helpers

def _amount_due(inv: dict) -> int:
    return max(0, int(inv.get("total_cents") or 0) - int(inv.get("paid_cents") or 0) - int(inv.get("credited_cents") or 0))


# Finnish for the ops-only explanations the billing engine writes (warnings on held drafts, why a VAT code was chosen).
# Stored text stays English (audit trail); the API adds {en, fi} pairs so the Finnish ops UI can show Finnish.
_FI_TEXT = [
    (r"No accepted quote is linked to this shipment; add the charges by hand\.",
     "Lähetykseen ei ole liitetty hyväksyttyä tarjousta; lisää veloitukset käsin."),
    (r"Quote (.+) has no accepted option; add the charges by hand\.",
     r"Tarjouksessa \1 ei ole hyväksyttyä vaihtoehtoa; lisää veloitukset käsin."),
    (r"(\d+) quote line\(s\) are billed to a third party and were not invoiced\.",
     r"\1 tarjousriviä laskutetaan kolmannelta osapuolelta, eikä niitä ole tällä laskulla."),
    (r"The import declaration is not released yet; duty and import VAT are not on this invoice\.",
     "Tuonti-ilmoitusta ei ole vielä vapautettu; tulli ja tuonnin ALV eivät ole tällä laskulla."),
    (r"Record Tulli's decided duty and import VAT before customs charges can be invoiced\.",
     "Kirjaa Tullin päätöksen mukaiset tullit ja tuonnin ALV ennen tullierien laskutusta."),
    (r"No billable lines were found\.", "Laskutettavia rivejä ei löytynyt."),
    (r"Add at least one line before issuing", "Lisää vähintään yksi rivi ennen lähettämistä"),
    (r"The invoice total must be above zero", "Laskun summan on oltava yli nolla"),
    (r"This customs amount is payable by the importer and cannot be invoiced as a disbursement",
     "Tämän tullimäärän maksaa maahantuoja, eikä sitä voi laskuttaa läpikulkueränä"),
    (r"Customs invoice amounts must match the recorded Tulli decision exactly",
     "Tullierien laskun summien on vastattava kirjattua Tullin päätöstä täsmälleen"),
    (r"The customs decision has already been invoiced", "Tullipäätös on jo laskutettu"),
    (r"Invoice was issued by someone else just now", "Joku toinen lähetti laskun juuri"),
    (r"from quote", "tarjouksen mukaan"), (r"set by ops", "asetettu käsin"), (r"recorded on shipment", "kirjattu lähetykselle"),
    (r"pass-through disbursement", "läpikulkuerä (asiakkaan puolesta maksettu)"),
    (r"credit of original line", "alkuperäisen rivin hyvitys"),
    (r"business customer in (\w+) \(B2B, place of supply \w+\)", r"yritysasiakas maassa \1 (B2B, myyntimaa \1)"),
    (r"business customer outside the EU \((\w+)\)", r"yritysasiakas EU:n ulkopuolella (\1)"),
    (r"domestic transport in Finland", "kotimaan kuljetus Suomessa"), (r"domestic leg", "kotimaan osuus"),
    (r"transport connected with import/export across the EU border", "EU:n rajan ylittävään tuontiin tai vientiin liittyvä kuljetus"),
    (r"intra-EU transport for a Finnish business customer \(verify with tax adviser\)",
     "EU:n sisäinen kuljetus suomalaiselle yritysasiakkaalle (tarkista veroneuvojalta)"),
]


def text_i18n(text: str | None) -> dict | None:
    import re

    if not text:
        return None
    for pat, fi in _FI_TEXT:
        if re.fullmatch(pat, text):
            return {"en": text, "fi": re.sub(pat, fi, text)}
    return {"en": text, "fi": text}


def public_invoice(inv: dict, *, internal: bool) -> dict:
    """API shape. Customers do not see internal notes, warnings or cost data."""
    out = {k: v for k, v in inv.items() if k != "_id"}
    out["amount_due_cents"] = _amount_due(inv) if inv.get("status") not in ("void", "draft") else (
        int(inv.get("total_cents") or 0) if inv.get("status") == "draft" else 0)
    due = to_date(inv.get("due_date"))
    out["days_overdue"] = (today_fi() - due).days if due and inv.get("status") in OPEN_STATES and today_fi() > due else 0
    out["days_until_due"] = (due - today_fi()).days if due and inv.get("status") in OPEN_STATES else None
    if not internal:
        for k in ("internal_note", "warnings", "created_by", "events", "source_detail", "emails", "issued_by",
                  "status_after_issue"):
            out.pop(k, None)
        out["lines"] = [{k: v for k, v in ln.items() if k not in ("vat_reason", "source_ref")} for ln in inv.get("lines", [])]
    else:
        out["warnings_i18n"] = [text_i18n(w) for w in inv.get("warnings") or []]
        if "lines" in inv:
            out["lines"] = [{**ln, "vat_reason_i18n": text_i18n(ln.get("vat_reason"))} for ln in inv.get("lines") or []]
    return out


async def _company(company_id: str | None) -> dict:
    if not company_id:
        return {}
    return (await db.companies.find_one({"id": company_id}, NO_ID)) or {}


def _buyer(company: dict) -> dict:
    addr = company.get("address") or {}
    return {"name": company.get("name"), "business_id": company.get("business_id"), "vat_id": company.get("vat_id"),
            "address": addr, "country": company.get("country") or addr.get("country") or "FI",
            "billing_email": company.get("billing_email")}


def _seller(settings: dict | None = None, company: dict | None = None) -> dict:
    settings = settings or {}
    if company is not None and is_test_company(company):  # TEST invoices print a marked TEST account, never a payable one
        return {**_seller(settings), "iban": None, "bic": None, "test_bank": True}
    return {"name": config.COMPANY_NAME, "business_id": settings.get("seller_business_id") or None,
            "vat_id": settings.get("seller_vat_id") or None, "address": config.COMPANY_ADDRESS, "email": config.COMPANY_EMAIL,
            "phone": config.COMPANY_PHONE or None, "iban": settings.get("seller_iban") or None, "bic": settings.get("seller_bic") or None}


async def billing_locale(company_id: str | None) -> str:
    """Language for invoices/e-mails: the company's admins' locale, Finnish by default."""
    if not company_id:
        return "fi"
    u = await db.users.find_one({"company_id": company_id, "role": "customer_admin", "disabled": {"$ne": True}},
                                {"locale": 1, "_id": 0})
    return (u or {}).get("locale") or "fi"


async def public_base_url() -> str:
    if config.APP_BASE_URL:
        return config.APP_BASE_URL
    doc = await db.system.find_one({"key": "v4:public_base_url"}, NO_ID)
    return (doc or {}).get("value", "")


async def remember_base_url(url: str):
    if url and not config.APP_BASE_URL:
        await db.system.update_one({"key": "v4:public_base_url"}, {"$set": {"value": url}}, upsert=True)


def _line(description: str, *, qty=1, unit_price_cents: int | None = None, amount_cents: int | None = None,
          vat_code: str | None = None, is_pass_through=False, code: str | None = None, unit: str | None = None,
          source: str = "manual", source_ref: str | None = None, vat_reason: str | None = None, recalc=True) -> dict:
    ln = {"id": new_id(), "code": code, "description": description, "qty": qty, "unit": unit,
          "unit_price_cents": unit_price_cents, "amount_cents": amount_cents, "vat_code": vat_code,
          "is_pass_through": bool(is_pass_through), "source": source, "source_ref": source_ref, "vat_reason": vat_reason,
          "_recalc": recalc}
    return ln


def _is_customs_pass_through(line: dict) -> bool:
    code = (line.get("code") or "").upper()
    return bool(line.get("is_pass_through")) and any(k in code for k in PASS_THROUGH_CUSTOMS_CODES)


def _payer_ok(line: dict, shipment: dict) -> bool:
    """V2 puts only the customer's charged lines on an option (the other trade party's share goes to
    not_included), so `payer` there just names the customer's role. Only an explicit third party is skipped."""
    return (line.get("payer") or "").strip().lower() not in ("third_party", "third party")


# --------------------------------------------------------------------------------------------- build from shipment

async def _quote_lines(shipment: dict, company: dict, warnings: list[str], locale: str = "fi") -> list[dict]:
    quote = None
    if shipment.get("quote_id"):
        quote = await db.quotes.find_one({"id": shipment["quote_id"]}, NO_ID)
    if not quote:
        warnings.append("No accepted quote is linked to this shipment; add the charges by hand.")
        return []
    option_id = shipment.get("option_id") or quote.get("accepted_option_id")
    option = next((o for o in quote.get("options", []) if o.get("id") == option_id), None)
    if option is None and len(quote.get("options", [])) == 1:
        option = quote["options"][0]
    if not option:
        warnings.append(f"Quote {quote.get('number')} has no accepted option; add the charges by hand.")
        return []
    out, skipped_payer, estimated_customs = [], 0, 0
    for ql in option.get("lines", []):
        if not _payer_ok(ql, shipment):
            skipped_payer += 1
            continue
        if _is_customs_pass_through(ql):
            estimated_customs += 1  # replaced by the actual amounts from the released declaration
            continue
        code = ql.get("vat_code")
        reason = "from quote"
        if code not in VAT_CODES:
            code, reason = suggest_vat_code(ql, shipment, company)
        elif not ql.get("is_pass_through"):
            # Customer established abroad: the B2B place-of-supply rule overrides the domestic code.
            sug, why = suggest_vat_code(ql, shipment, company)
            if sug in ("EU_ICS_RC", "OUT_OF_SCOPE_FOREIGN"):
                code, reason = sug, why
        qty = ql.get("qty") if ql.get("qty") not in (None, "", 0) else 1
        unit_price = ql.get("unit_price_cents")
        amount = ql.get("amount_cents")
        recalc = unit_price is not None
        if unit_price is None:
            unit_price, qty = amount, 1
        desc = (ql.get("description_fi") if locale == "fi" else None) or ql.get("description") or ql.get("code") or "Charge"
        out.append(_line(desc, qty=qty, unit_price_cents=unit_price,
                         amount_cents=amount, vat_code=code, is_pass_through=ql.get("is_pass_through", False),
                         code=ql.get("code"), unit=ql.get("basis"), source="quote",
                         source_ref=f"{quote.get('number') or quote['id']}/{option.get('id')}", vat_reason=reason,
                         recalc=recalc and amount is None))
        # amount from the quote is authoritative (it already includes any rounding the quote showed)
        if amount is not None:
            out[-1]["amount_cents"] = int(amount)
            out[-1]["_recalc"] = False
    if skipped_payer:
        warnings.append(f"{skipped_payer} quote line(s) are billed to a third party and were not invoiced.")
    if estimated_customs:
        out_info = "estimated duty/VAT lines from the quote were replaced by the customs declaration"
        log.info("shipment %s: %s", shipment.get("id"), out_info)
    return out


async def _extra_lines(shipment: dict, company: dict) -> list[dict]:
    out = []
    for ch in shipment.get("extra_charges") or shipment.get("charges") or []:
        if ch.get("invoice_id") or ch.get("invoiced"):
            continue
        amount = ch.get("amount_cents")
        unit = ch.get("unit_price_cents", amount)
        if unit is None:
            continue
        code = ch.get("vat_code")
        reason = "recorded on shipment"
        if code not in VAT_CODES:
            code, reason = suggest_vat_code(ch, shipment, company)
        out.append(_line(ch.get("description") or ch.get("code") or "Additional charge", qty=ch.get("qty") or 1,
                         unit_price_cents=unit, amount_cents=amount, vat_code=code,
                         is_pass_through=ch.get("is_pass_through", False), code=ch.get("code"), source="extra",
                         source_ref=ch.get("id"), vat_reason=reason, recalc=amount is None))
        if amount is not None:
            out[-1]["_recalc"] = False
    return out


async def _customs_lines(shipment: dict, locale: str, warnings: list[str], info: list[dict]) -> list[dict]:
    """Pass-through duty and import VAT from the released import declaration (V3) when the forwarder paid them."""
    out = []
    decls = await db.customs_declarations.find(
        {"shipment_id": shipment["id"], "type": {"$in": ["import", None]}}, NO_ID).to_list(20)
    released = [d for d in decls if d.get("status") in ("released", "closed")]
    pending = [d for d in decls if d.get("status") not in ("released", "closed", "invalidated", "rejected", "not_released")]
    if pending and not released:
        warnings.append("The import declaration is not released yet; duty and import VAT are not on this invoice.")
    for d in released:
        decision = d.get("customs_decision")
        if not decision or decision.get("source") != "tulli_decision":
            warnings.append("Record Tulli's decided duty and import VAT before customs charges can be invoiced.")
            info.append({"code": "CUSTOMS_DECISION_PENDING", "mrn": d.get("mrn")})
            continue
        totals = decision
        payer = decision["paid_by"]
        if payer in ("customer", "importer", "consignee"):
            info.append({"code": "CUSTOMS_PAID_BY_CUSTOMER", "mrn": d.get("mrn")})
            continue
        mrn = d.get("mrn") or (d.get("submission") or {}).get("mrn") or d.get("lrn") or ""
        duty = int(totals.get("duty_cents") or 0)
        if duty > 0:
            out.append(_line((f"Tulli: tullimaksu, MRN {mrn}" if locale == "fi" else f"Customs duty, MRN {mrn}").strip(", "),
                             qty=1, unit_price_cents=duty, amount_cents=duty, vat_code="OUT_OF_SCOPE_DISBURSEMENT",
                             is_pass_through=True, code="DUTY", source="customs", source_ref=d.get("id"),
                             vat_reason="pass-through disbursement", recalc=False))
        vat = int(totals.get("vat_cents") or 0)
        route = (d.get("vat_route") or "").lower()
        if route == "paid_to_customs" and vat > 0:
            out.append(_line((f"Tulli: maahantuonnin arvonlisävero, MRN {mrn}" if locale == "fi"
                              else f"Import VAT paid to Finnish Customs, MRN {mrn}"),
                             qty=1, unit_price_cents=vat, amount_cents=vat, vat_code="OUT_OF_SCOPE_DISBURSEMENT",
                             is_pass_through=True, code="IMPORT_VAT", source="customs", source_ref=d.get("id"),
                             vat_reason="pass-through disbursement", recalc=False))
        elif route == "tilitys":
            # RULE V-3: the importer reports import VAT in its own VAT return, nothing to pass through.
            info.append({"code": "IMPORT_VAT_POSTPONED", "mrn": mrn, "vat_cents": vat})
    return out


async def create_invoice_for_shipment(shipment_id: str, actor: dict | None) -> dict:
    """Draft (and, when clean, issue) the invoice for a delivered shipment. Idempotent per shipment."""
    shipment = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not shipment:
        raise BillingError("Shipment not found", 404)
    existing = await db.invoices.find_one({"shipment_id": shipment_id, "kind": "invoice",
                                           "origin": {"$in": ["shipment", "prepayment"]}, "status": {"$ne": "void"}}, NO_ID)
    if existing:
        return existing
    company = await _company(shipment.get("company_id")) or {}
    if not company:
        raise BillingError("The shipment's customer company was not found", 409)
    settings = await get_settings()
    locale = await billing_locale(company.get("id"))
    warnings: list[str] = []
    info: list[dict] = []
    lines = await _quote_lines(shipment, company, warnings, locale)
    lines += await _extra_lines(shipment, company)
    lines += await _customs_lines(shipment, locale, warnings, info)
    if not lines:
        warnings.append("No billable lines were found.")
    delivered = to_date(shipment.get("delivered_at") or _milestone_at(shipment, "DELIVERED")) or today_fi()
    inv = await _new_invoice(company, lines, settings=settings, actor=actor, shipment=shipment, origin="shipment",
                             supply_date=delivered, warnings=warnings, info=info)
    await audit(actor, "invoice_drafted", "invoice", inv["id"], {"shipment_id": shipment_id, "warnings": warnings},
                company_id=company["id"])
    # A person creating the invoice issues it at once; on delivery the invoice agent (services.agents.invoice_agent)
    # decides, inside Autopilot's guardrails, and an agent never issues through this shortcut.
    auto_issue = config.env_bool("INVOICE_AUTO_ISSUE", True) and (actor or {}).get("org_type") == "internal" \
        and (actor or {}).get("type") != "agent"
    if auto_issue and not warnings and inv["total_cents"] > 0:
        try:
            inv = await issue_invoice(inv["id"], actor or {"id": "autopilot", "name": "Autopilot"}, send=True)
        except BillingError as exc:
            await db.invoices.update_one({"id": inv["id"]}, {"$push": {"warnings": str(exc)}})
            warnings.append(str(exc))
    if inv["status"] == "draft":
        from core.notify import bi, notify_role

        texts = [text_i18n(w) for w in warnings]
        await notify_role("finance", bi(f"Tarkistettava laskuluonnos: {shipment.get('reference')}",
                                        f"Invoice draft to review: {shipment.get('reference')}"),
                          bi("; ".join(t["fi"] for t in texts) or "Tarkista ja lähetä lasku.",
                             "; ".join(warnings) or "Review and issue the invoice."), link=f"/ops/billing/{inv['id']}")
    if inv["status"] != "draft":  # a draft gets its INVOICED milestone when it is issued (issue_invoice)
        await _shipment_milestone(shipment_id, "INVOICED", "Invoiced" if locale == "en" else "Laskutettu", actor,
                                  note=inv.get("number"))
    return await db.invoices.find_one({"id": inv["id"]}, NO_ID)


async def create_prepayment_invoice(q: dict, opt: dict, company: dict, *, actor: dict, send_also_to: str | None = None) -> dict:
    """s2 (services.prepayment): the invoice for one quote option of a prepayment-only customer, issued at once and
    due on issue. Lines and VAT come from the quote option exactly as the shipment invoice would take them."""
    settings = await get_settings()
    locale = await billing_locale(company.get("id"))
    req = q.get("request") or {}
    basis = {"quote_id": q["id"], "option_id": opt["id"], "company_id": company["id"], "origin": req.get("origin"),
             "destination": req.get("destination"), "direction": req.get("direction"), "incoterm": req.get("incoterm")}
    warnings: list[str] = []
    lines = await _quote_lines(basis, company, warnings, locale)
    if not lines:
        raise BillingError("This quote option has no billable lines", 409)
    inv = await _new_invoice(company, lines, settings=settings, actor=actor, shipment=None, origin="prepayment",
                             warnings=warnings, customer_reference=q.get("number"))
    await db.invoices.update_one({"id": inv["id"]}, {"$set": {"payment_terms_days": 0, "quote_id": q["id"],
                                                             "quote_number": q.get("number"), "option_id": opt["id"],
                                                             "send_also_to": send_also_to}})
    await audit(actor, "invoice_drafted", "invoice", inv["id"], {"quote_id": q["id"], "prepayment": True,
                                                                 "warnings": warnings}, company_id=company["id"])
    if not warnings and inv["total_cents"] > 0:
        return await issue_invoice(inv["id"], actor, send=True)
    from core.notify import bi, notify_role
    await notify_role("finance", bi(f"Tarkistettava ennakkolasku: {q.get('number')}", f"Prepayment invoice to review: {q.get('number')}"),
                      bi("Tarkista ja lähetä ennakkolasku.", "; ".join(warnings) or "Review and issue the prepayment invoice."),
                      link=f"/ops/billing/{inv['id']}")
    return await db.invoices.find_one({"id": inv["id"]}, NO_ID)


def _milestone_at(shipment: dict, code: str) -> str | None:
    for m in reversed(shipment.get("milestones") or []):
        if (m.get("code") or "").upper() == code and m.get("at") and not m.get("planned"):
            return m["at"]
    return None


async def _new_invoice(company: dict, lines: list[dict], *, settings: dict, actor: dict | None, shipment: dict | None,
                       origin: str, supply_date: date | None = None, warnings: list[str] | None = None,
                       info: list[dict] | None = None, notes: str | None = None, customer_reference: str | None = None,
                       kind: str = "invoice", credit_note_for: str | None = None, demo: bool = False) -> dict:
    supply = supply_date or today_fi()
    totals = compute_totals(lines, on=supply, settings=settings)
    fx_fields = {}  # s13 multi-currency: non-EUR customers (and their credit notes) only; EUR invoices unchanged
    if (company.get("invoice_currency") or "EUR") != "EUR" or credit_note_for:
        from services.fx_revaluation import localise_draft
        fx_fields = await localise_draft(company, lines, settings=settings, supply=supply, origin=origin,
                                         credit_note_for=credit_note_for)
    refs = (shipment or {}).get("references") or {}
    inv = {
        "id": new_id(), "kind": kind, "origin": origin, "number": None, "status": "draft",
        "company_id": company["id"], "company_name": company.get("name"), "buyer": _buyer(company), "seller": _seller(settings, company),
        "shipment_id": (shipment or {}).get("id"), "shipment_reference": (shipment or {}).get("reference"),
        "customer_reference": customer_reference or refs.get("customer_ref") or (", ".join(refs.get("po_numbers") or []) or None),
        "issue_date": None, "due_date": None, "supply_date": supply.isoformat(),
        "payment_terms_days": int(company.get("payment_terms_days") or settings.get("payment_terms_days") or 14),
        "lines": lines, **totals, "currency": "EUR", "paid_cents": 0, "credited_cents": 0,
        "reference": None, "rf_reference": None, "payment": None, "payments": [], "pdf_file_id": None,
        "credit_note_for": credit_note_for, "credit_note_ids": [], "notes": notes, "info": info or [],
        "warnings": warnings or [], "terms_version": settings.get("terms_version", "NSAB 2015 (PSYM 2015)"),
        "late_interest_pct": float(settings.get("late_interest_pct", 11.5)), "locale": await billing_locale(company["id"]),
        "created_at": now_iso(), "updated_at": now_iso(), "created_by": actor_ref(actor),
        "demo": bool(demo or company.get("demo") and (shipment or {}).get("demo")),
        **fx_fields,  # s13 multi-currency
    }
    await db.invoices.insert_one(dict(inv))
    if shipment:
        await db.shipments.update_one({"id": shipment["id"]}, {"$addToSet": {"invoice_ids": inv["id"]}})
    await emit("invoice.created", {"invoice": inv})
    return inv


# --------------------------------------------------------------------------------------------- manual invoices

def _clean_manual_lines(raw: list[dict], shipment: dict | None, company: dict) -> list[dict]:
    lines = []
    for i, r in enumerate(raw or []):
        desc = (r.get("description") or "").strip()
        if not desc:
            raise BillingError(f"Line {i + 1}: description is required")
        if r.get("unit_price_cents") is None:
            raise BillingError(f"Line {i + 1}: unit price is required")
        qty = r.get("qty") if r.get("qty") not in (None, "") else 1
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            raise BillingError(f"Line {i + 1}: quantity must be a number")
        if not math.isfinite(qty) or qty > 1_000_000:
            raise BillingError(f"Line {i + 1}: quantity must be a number up to 1 000 000")
        if qty <= 0:
            raise BillingError(f"Line {i + 1}: quantity must be above zero")
        try:
            price = float(r["unit_price_cents"])
        except (TypeError, ValueError):
            raise BillingError(f"Line {i + 1}: unit price must be a number")
        if not math.isfinite(price) or abs(price) > 10_000_000_000 or price != int(price):
            raise BillingError(f"Line {i + 1}: unit price must be whole cents up to 100 000 000 €")
        code = r.get("vat_code")
        reason = "set by ops"
        if not code:
            code, reason = suggest_vat_code(r, shipment, company)
        if code not in VAT_CODES:
            raise BillingError(f"Line {i + 1}: unknown VAT code {code}")
        if r.get("is_pass_through") and code != "OUT_OF_SCOPE_DISBURSEMENT":
            raise BillingError(f"Line {i + 1}: pass-through lines use VAT code OUT_OF_SCOPE_DISBURSEMENT")
        lines.append(_line(desc[:300], qty=qty, unit_price_cents=int(price), vat_code=code,
                           is_pass_through=bool(r.get("is_pass_through")), code=r.get("code"), unit=r.get("unit"),
                           source=r.get("source") or "manual", source_ref=r.get("source_ref"), vat_reason=reason))
    return lines


async def create_manual_invoice(body: dict, actor: dict) -> dict:
    company = await _company(body.get("company_id"))
    if not company:
        raise BillingError("Choose the customer company", 400)
    shipment = None
    if body.get("shipment_id"):
        shipment = await db.shipments.find_one({"id": body["shipment_id"]}, NO_ID)
        if not shipment or shipment.get("company_id") != company["id"]:
            raise BillingError("The shipment does not belong to this customer", 400)
    lines = _clean_manual_lines(body.get("lines") or [], shipment, company)
    settings = await get_settings()
    supply = to_date(body.get("supply_date")) or today_fi()
    inv = await _new_invoice(company, lines, settings=settings, actor=actor, shipment=shipment, origin="manual",
                             supply_date=supply, notes=(body.get("notes") or None), customer_reference=body.get("customer_reference"))
    if body.get("payment_terms_days"):
        await db.invoices.update_one({"id": inv["id"]}, {"$set": {"payment_terms_days": int(body["payment_terms_days"])}})
    await audit(actor, "invoice_drafted", "invoice", inv["id"], {"manual": True}, company_id=company["id"])
    if body.get("issue"):
        return await issue_invoice(inv["id"], actor, send=body.get("send", True))
    return await db.invoices.find_one({"id": inv["id"]}, NO_ID)


async def update_draft(invoice_id: str, body: dict, actor: dict) -> dict:
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise BillingError("Invoice not found", 404)
    if inv["status"] != "draft":
        raise BillingError("Only draft invoices can be edited. Issue a credit note to correct an issued invoice.", 409)
    company = await _company(inv["company_id"]) or {}
    shipment = await db.shipments.find_one({"id": inv["shipment_id"]}, NO_ID) if inv.get("shipment_id") else None
    changes: dict = {}
    if "lines" in body and body["lines"] is not None:
        changes["lines"] = _clean_manual_lines(body["lines"], shipment, company)
    for k in ("notes", "customer_reference", "internal_note"):
        if k in body:
            changes[k] = body[k] or None
    if body.get("payment_terms_days") is not None:
        d = int(body["payment_terms_days"])
        if d < 0 or d > 120:
            raise BillingError("Payment terms must be 0–120 days")
        changes["payment_terms_days"] = d
    if body.get("supply_date"):
        changes["supply_date"] = to_date(body["supply_date"]).isoformat()
    lines = changes.get("lines", inv["lines"])
    if "lines" not in changes:
        for ln in lines:
            ln["_recalc"] = False  # untouched lines keep their stored amounts
    settings = await get_settings()
    supply = to_date(changes.get("supply_date") or inv.get("supply_date")) or today_fi()
    totals = compute_totals(lines, on=supply, settings=settings)
    if (inv.get("currency") or "EUR") != "EUR":  # s13 multi-currency: edited lines are in the invoice currency
        from services.fx_revaluation import localise_draft
        totals = await localise_draft(company, lines, settings=settings, supply=supply, origin="edit",
                                      currency=inv["currency"], fx_doc=inv.get("fx")) or totals
    changes.update(lines=lines, **totals, updated_at=now_iso())
    if "lines" in body:
        changes["warnings"] = [w for w in inv.get("warnings", []) if "billable lines" not in w and "by hand" not in w]
    await db.invoices.update_one({"id": invoice_id}, {"$set": changes})
    await audit(actor, "invoice_updated", "invoice", invoice_id, {"fields": sorted(body.keys())}, company_id=inv["company_id"])
    return await db.invoices.find_one({"id": invoice_id}, NO_ID)


# --------------------------------------------------------------------------------------------- issue / send

async def issue_invoice(invoice_id: str, actor: dict | None, *, send: bool = True, issue_date: date | None = None) -> dict:
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise BillingError("Invoice not found", 404)
    if inv["status"] != "draft":
        raise BillingError(f"Invoice is already {inv['status']}", 409)
    if not inv.get("lines"):
        raise BillingError("Add at least one line before issuing")
    if inv["kind"] == "invoice" and inv["total_cents"] <= 0:
        raise BillingError("The invoice total must be above zero")
    await validate_customs_invoice(inv)
    settings = await get_settings()
    company = await _company(inv["company_id"]) or {}
    issued = issue_date or today_fi()
    terms = inv.get("payment_terms_days")  # 0 = due on issue (prepayment, or set on the draft); missing = 14
    due = issued + timedelta(days=int(14 if terms in (None, "") else terms))
    number = await next_ref("HUO-INV")
    reference = fi_reference(number.split("-", 2)[-1])  # digits of 2026-000123 -> 2026000123 + check digit
    changes = {
        "number": number, "reference": reference, "rf_reference": rf_reference(reference),
        "issue_date": issued.isoformat(), "due_date": due.isoformat() if inv["kind"] == "invoice" else issued.isoformat(),
        "status": "open" if inv["kind"] == "invoice" else inv.get("status_after_issue", "open"),
        "issued_at": now_iso(), "issued_by": actor_ref(actor), "buyer": _buyer(company), "seller": _seller(settings, company),
        "updated_at": now_iso(),
    }
    if (inv.get("currency") or "EUR") != "EUR":  # s13 multi-currency: re-convert at the issue-date ECB rate, then fixed
        from services.fx_revaluation import reconvert_at_issue
        changes.update(await reconvert_at_issue(inv, issued, settings))
    if inv["kind"] == "invoice" and due < today_fi():
        changes["status"] = "overdue"
        changes["overdue_since"] = now_iso()
    res = await db.invoices.update_one({"id": invoice_id, "status": "draft"}, {"$set": changes})
    if res.modified_count == 0:
        raise BillingError("Invoice was issued by someone else just now", 409)
    inv.update(changes)
    inv = await _store_pdf(inv)
    await audit(actor, "invoice_issued", "invoice", invoice_id, {"number": number, "total_cents": inv["total_cents"]},
                company_id=inv["company_id"])
    if (actor or {}).get("id") == "autopilot":
        from core.autonomy import action
        await action("invoice_issued", "invoice", invoice_id, company_id=inv["company_id"],
                     data={"number": number, "total_cents": inv["total_cents"]}, reversible=True)
    if inv.get("shipment_id") and inv.get("kind", "invoice") == "invoice":
        await _shipment_milestone(inv["shipment_id"], "INVOICED", "Invoiced" if inv.get("locale") == "en" else "Laskutettu",
                                  actor, note=number)
    await emit("invoice.issued", {"invoice": inv, "actor": actor})
    try:  # s7: credit hold when over limit / overdue (never blocks invoicing)
        from services import credit
        await credit.after_invoice_issued(inv)
    except Exception:  # noqa: BLE001
        log.exception("credit re-check after issuing %s failed", number)
    if send:
        await send_invoice_email(inv)
    return await db.invoices.find_one({"id": invoice_id}, NO_ID)


async def _store_pdf(inv: dict) -> dict:
    from core.storage import put_file
    from services.invoice_pdf import render_invoice_pdf

    try:
        pdf = render_invoice_pdf(inv)
    except Exception:  # noqa: BLE001 - a PDF failure must not lose the issued invoice
        log.exception("invoice pdf failed for %s", inv.get("id"))
        return inv
    file_id = await put_file(pdf, f"{inv['number']}.pdf", "application/pdf",
                             {"invoice_id": inv["id"], "company_id": inv["company_id"]})
    await db.invoices.update_one({"id": inv["id"]}, {"$set": {"pdf_file_id": file_id}})
    inv["pdf_file_id"] = file_id
    return inv


def _fmt_eur(cents: int, locale: str, currency: str = "EUR") -> str:
    v = Decimal(cents) / 100
    s = f"{v:,.2f}"
    if locale == "fi":
        s = s.replace(",", " ").replace(".", ",")
    if (currency or "EUR") != "EUR":  # s13 multi-currency: amounts of a foreign-currency invoice carry its code
        return f"{s} {currency}"
    return f"{s} €" if locale == "fi" else f"€{s}"


def _fmt_date(d: str | None, locale: str) -> str:
    if not d:
        return "—"
    dd = to_date(d)
    return f"{dd.day}.{dd.month}.{dd.year}" if locale == "fi" else dd.strftime("%d %b %Y")


async def _company_recipients(company_id: str, billing_email: str | None) -> list[str]:
    if billing_email:
        return [billing_email]
    out = []
    async for u in db.users.find({"company_id": company_id, "role": "customer_admin", "disabled": {"$ne": True}},
                                 {"email": 1, "_id": 0}):
        out.append(u["email"])
    return out


async def send_invoice_email(inv: dict, *, kind: str = "issued") -> list[dict]:
    """E-mail templates: issued | reminder | paid | credit_note | void. Link goes to the portal invoice page."""
    from core.email import render, send_email
    from core.notify import bi, notify_company

    if kind == "reminder":
        from services.invoice_disputes import is_open
        latest = await db.invoices.find_one({"id": inv["id"]}, {"dispute": 1})
        if is_open(latest or inv):
            return []
    if QUIET_EMAIL.get():
        return []
    loc = inv.get("locale") or "fi"
    base = await public_base_url()
    url = f"{base}/portal/invoices/{inv['id']}" if base else None
    if kind == "issued" and inv["kind"] == "credit_note":
        kind = "credit_note"
    orig = (await db.invoices.find_one({"id": inv.get("credit_note_for")}, {"number": 1, "_id": 0}) or {}) if kind == "credit_note" else {}

    def texts(loc: str) -> tuple[str, list[str], str]:
        fi = loc == "fi"
        amount = _fmt_eur(_amount_due(inv) if kind != "paid" else inv["total_cents"], loc, inv.get("currency"))  # s13 multi-currency
        num = inv.get("number") or ""
        bank = []
        seller = inv.get("seller") or {}
        if seller.get("iban"):
            bank = [f"IBAN {seller['iban']}" + (f" · BIC {seller['bic']}" if seller.get("bic") else ""),
                    (f"Viitenumero {inv.get('reference')}" if fi else f"Reference {inv.get('reference')} (RF {inv.get('rf_reference')})")]
        if kind == "issued":
            subject = f"Lasku {num} – {config.COMPANY_NAME}" if fi else f"Invoice {num} from {config.COMPANY_NAME}"
            lines = ([f"Lasku {num}" + (f" lähetykselle {inv['shipment_reference']}" if inv.get("shipment_reference") else "") + ".",
                      f"Summa {amount}, eräpäivä {_fmt_date(inv['due_date'], loc)}."] if fi else
                     [f"Invoice {num}" + (f" for shipment {inv['shipment_reference']}" if inv.get("shipment_reference") else "") + ".",
                      f"Amount {amount}, due {_fmt_date(inv['due_date'], loc)}."]) + bank
            label = "Näytä ja maksa" if fi else "View and pay"
        elif kind == "reminder":
            subject = f"Maksumuistutus: lasku {num}" if fi else f"Payment reminder: invoice {num}"
            lines = ([f"Laskun {num} eräpäivä {_fmt_date(inv['due_date'], loc)} on ohitettu.",
                      f"Avoinna {amount}. Jos lasku on jo maksettu, tämä viesti on aiheeton.",
                      f"Viivästyskorko {str(inv.get('late_interest_pct', 11.5)).replace('.', ',')} % korkolain mukaan."] if fi else
                     [f"Invoice {num} was due on {_fmt_date(inv['due_date'], loc)}.",
                      f"Outstanding {amount}. If you have already paid, please ignore this message.",
                      f"Late payment interest {inv.get('late_interest_pct', 11.5)} % per the Finnish Interest Act."]) + bank
            label = "Näytä lasku" if fi else "View invoice"
        elif kind == "paid":
            subject = f"Maksu vastaanotettu: lasku {num}" if fi else f"Payment received: invoice {num}"
            lines = [f"Kiitos, vastaanotimme maksun {amount} laskulle {num}." if fi else
                     f"Thank you, we received {amount} for invoice {num}."]
            label = "Näytä kuitti" if fi else "View receipt"
        elif kind == "credit_note":
            subject = f"Hyvityslasku {num}" if fi else f"Credit note {num}"
            lines = [f"Hyvityslasku {num} laskulle {orig.get('number', '')}, summa {_fmt_eur(inv['total_cents'], loc, inv.get('currency'))}." if fi else
                     f"Credit note {num} for invoice {orig.get('number', '')}, amount {_fmt_eur(inv['total_cents'], loc, inv.get('currency'))}."]
            label = "Näytä hyvityslasku" if fi else "View credit note"
        else:  # void
            subject = f"Lasku {num} on mitätöity" if fi else f"Invoice {num} has been voided"
            lines = [f"Lasku {num} on mitätöity, eikä sitä tarvitse maksaa." if fi else
                     f"Invoice {num} has been voided; no payment is due."] + ([inv["void_reason"]] if inv.get("void_reason") else [])
            label = "Näytä lasku" if fi else "View invoice"
        return subject, lines, label

    subject, lines, label = texts(loc)
    fi = loc == "fi"
    html, text = render(subject, lines, url, label if url else None,
                        footer=f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS} · "
                               + ("Palvelut PSYM 2015 -ehdoin" if fi else "Services under NSAB 2015 (PSYM 2015)"))
    sent = []
    recipients = await _company_recipients(inv["company_id"], (inv.get("buyer") or {}).get("billing_email"))
    for to in dict.fromkeys(recipients + ([inv["send_also_to"]] if inv.get("send_also_to") else [])):
        sent.append(await send_email(to, subject, html, text, tag=f"invoice_{kind}", company_id=inv["company_id"],
                                     ref={"type": "invoice", "id": inv["id"]}))
    (subject_fi, lines_fi, _), (subject_en, lines_en, _) = texts("fi"), texts("en")
    await notify_company(inv["company_id"], bi(subject_fi, subject_en), bi(lines_fi[0] if lines_fi else "", lines_en[0] if lines_en else ""),
                         link=f"/portal/invoices/{inv['id']}", kind="billing")
    await db.invoices.update_one({"id": inv["id"]}, {"$push": {"emails": {
        "kind": kind, "at": now_iso(), "to": [s["to"] for s in sent], "status": [s["status"] for s in sent]}}})
    return sent


# --------------------------------------------------------------------------------------------- void / credit

async def void_invoice(invoice_id: str, reason: str, actor: dict) -> dict:
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise BillingError("Invoice not found", 404)
    if inv["status"] not in ("draft", "open", "overdue"):
        raise BillingError(f"A {inv['status']} invoice cannot be voided" + (
            "; issue a credit note instead" if inv["status"] in ("paid", "processing") else ""), 409)
    if int(inv.get("paid_cents") or 0) > 0:
        raise BillingError("Payments are recorded on this invoice; issue a credit note instead", 409)
    if not (reason or "").strip():
        raise BillingError("Give a reason for voiding")
    await db.invoices.update_one({"id": invoice_id}, {"$set": {"status": "void", "void_reason": reason.strip(),
                                                               "voided_at": now_iso(), "voided_by": actor_ref(actor),
                                                               "updated_at": now_iso()}})
    await audit(actor, "invoice_voided", "invoice", invoice_id, {"reason": reason}, company_id=inv["company_id"])
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if inv.get("number"):
        await send_invoice_email(inv, kind="void")
    return inv


async def credit_invoice(invoice_id: str, *, reason: str, line_ids: list[str] | None, actor: dict) -> dict:
    """Credit note (negative invoice) linked via credit_note_for. Full credit unless line_ids are given."""
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise BillingError("Invoice not found", 404)
    if inv["kind"] != "invoice" or inv["status"] not in ("open", "overdue", "paid", "processing"):
        raise BillingError(f"A {inv['status']} {inv['kind'].replace('_', ' ')} cannot be credited", 409)
    if not (reason or "").strip():
        raise BillingError("Give a reason for the credit note")
    src = [ln for ln in inv["lines"] if not line_ids or ln["id"] in line_ids]
    if not src:
        raise BillingError("Choose at least one line to credit")
    already = int(inv.get("credited_cents") or 0)
    lines = []
    for ln in src:
        q = ln.get("qty") or 1
        lines.append(_line(ln["description"], qty=-abs(q), unit_price_cents=ln["unit_price_cents"], vat_code=ln["vat_code"],
                           is_pass_through=ln.get("is_pass_through", False), code=ln.get("code"), unit=ln.get("unit"),
                           source="credit", source_ref=ln["id"], vat_reason="credit of original line"))
        if ln.get("amount_cents") is not None and not line_ids:
            lines[-1]["amount_cents"] = -int(ln["amount_cents"])
            lines[-1]["_recalc"] = False
    company = await _company(inv["company_id"]) or {}
    settings = await get_settings()
    shipment = await db.shipments.find_one({"id": inv["shipment_id"]}, NO_ID) if inv.get("shipment_id") else None
    supply = to_date(inv.get("supply_date")) or today_fi()
    import copy

    preview = compute_totals(copy.deepcopy(lines), on=supply, settings=settings)
    if -preview["total_cents"] + already > inv["total_cents"]:
        raise BillingError("The credit would exceed what is left on the invoice", 409)
    cn = await _new_invoice(company, lines, settings=settings, actor=actor, shipment=shipment, origin="credit",
                            supply_date=supply, notes=reason.strip(), kind="credit_note", credit_note_for=inv["id"],
                            customer_reference=inv.get("customer_reference"), demo=inv.get("demo", False))
    credit_abs = -cn["total_cents"]
    paid = int(inv.get("paid_cents") or 0)
    # A credit on a paid invoice is money owed back: the credit note stays open until finance records the refund.
    owed_back = max(0, paid + already + credit_abs - inv["total_cents"])
    await db.invoices.update_one({"id": cn["id"]}, {"$set": {"status_after_issue": "open" if owed_back > 0 else "paid",
                                                             "refund_due_cents": owed_back,
                                                             "credit_note_for_number": inv.get("number")}})
    cn = await issue_invoice(cn["id"], actor, send=True)
    new_credited = already + credit_abs
    changes = {"credited_cents": new_credited, "updated_at": now_iso()}
    if new_credited >= inv["total_cents"]:
        changes["status"] = "credited"
    elif paid + new_credited >= inv["total_cents"]:
        changes["status"] = "paid"
    await db.invoices.update_one({"id": inv["id"]}, {"$set": changes, "$push": {"credit_note_ids": cn["id"]}})
    await audit(actor, "invoice_credited", "invoice", inv["id"], {"credit_note": cn["number"], "amount_cents": credit_abs,
                                                                  "reason": reason}, company_id=inv["company_id"])
    return cn


# --------------------------------------------------------------------------------------------- payments

async def record_payment(invoice_id: str, *, amount_cents: int, method: str, ref: str, paid_at: str | None = None,
                         actor: dict | None = None, details: dict | None = None) -> tuple[dict, bool]:
    """Adds a payment exactly once per (method, ref). Returns (invoice, applied_now)."""
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise BillingError("Invoice not found", 404)
    key = f"{method}:{ref}"
    if any(p.get("key") == key for p in inv.get("payments") or []):
        return inv, False
    if amount_cents <= 0:
        raise BillingError("Payment amount must be above zero")
    if inv["status"] in ("void", "draft"):
        raise BillingError(f"Cannot record a payment on a {inv['status']} invoice", 409)
    entry = {"id": new_id(), "key": key, "method": method, "ref": ref, "amount_cents": int(amount_cents),
             "paid_at": paid_at or now_iso(), "recorded_at": now_iso(), "recorded_by": actor_ref(actor), **(details or {})}
    res = await db.invoices.update_one({"id": invoice_id, "payments.key": {"$ne": key}},
                                       {"$push": {"payments": entry}, "$inc": {"paid_cents": int(amount_cents)},
                                        "$set": {"updated_at": now_iso()}})
    if res.modified_count == 0:
        return await db.invoices.find_one({"id": invoice_id}, NO_ID), False
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    became_paid = False
    if inv["kind"] == "credit_note":
        if int(inv.get("paid_cents") or 0) >= int(inv.get("refund_due_cents") or 0):
            await db.invoices.update_one({"id": invoice_id}, {"$set": {"status": "paid", "paid_at": entry["paid_at"]}})
    elif _amount_due(inv) == 0 and inv["status"] in ("open", "overdue", "processing"):
        r = await db.invoices.update_one({"id": invoice_id, "status": {"$in": list(OPEN_STATES)}},
                                         {"$set": {"status": "paid", "paid_at": entry["paid_at"],
                                                   "payment": {"method": method, "ref": ref, "paid_at": entry["paid_at"],
                                                               **(details or {})}}})
        became_paid = r.modified_count == 1
    await audit(actor, "invoice_payment_recorded", "invoice", invoice_id,
                {"amount_cents": amount_cents, "method": method, "ref": ref}, company_id=inv["company_id"])
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if became_paid:
        await emit("invoice.paid", {"invoice": inv})
        await send_invoice_email(inv, kind="paid")
        if inv.get("shipment_id"):
            await _shipment_milestone(inv["shipment_id"], "PAID",
                                      "Maksettu" if inv.get("locale") == "fi" else "Paid", actor, note=inv.get("number"))
    return inv, True


async def mark_processing(invoice_id: str, note: str):
    await db.invoices.update_one({"id": invoice_id, "status": {"$in": ["open", "overdue"]}},
                                 {"$set": {"status": "processing", "processing_note": note, "updated_at": now_iso()}})


async def reopen_after_failed_payment(invoice_id: str, note: str):
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv or inv["status"] != "processing":
        return
    due = to_date(inv.get("due_date"))
    status = "overdue" if due and due < today_fi() else "open"
    await db.invoices.update_one({"id": invoice_id, "status": "processing"},
                                 {"$set": {"status": status, "processing_note": note, "updated_at": now_iso()}})


async def record_refund(invoice_id: str, *, amount_cents: int, ref: str, actor: dict | None = None,
                        charge_id: str | None = None):
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        return None
    key = f"refund:{ref}"
    res = await db.invoices.update_one({"id": invoice_id, "refunds.key": {"$ne": key}},
                                       {"$push": {"refunds": {"key": key, "amount_cents": amount_cents, "at": now_iso(),
                                                                    "charge_id": charge_id}},
                                        "$inc": {"paid_cents": -int(amount_cents)}})
    if res.modified_count == 0:
        return inv
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if inv["status"] == "paid" and _amount_due(inv) > 0:
        due = to_date(inv.get("due_date"))
        await db.invoices.update_one({"id": invoice_id}, {"$set": {
            "status": "overdue" if due and due < today_fi() else "open", "updated_at": now_iso()}})
    from core.notify import bi, notify_role

    await notify_role("finance", bi(f"Palautus laskulle {inv.get('number')}", f"Refund on {inv.get('number')}"),
                      bi(f"{_fmt_eur(amount_cents, 'fi')} palautettu Stripessä. Tee hyvityslasku, jos veloitus ei ole aiheellinen.",
                         f"{amount_cents / 100:.2f} EUR refunded in Stripe. Issue a credit note if the charge is not owed."),
                      link=f"/ops/billing/{invoice_id}")
    await audit(actor, "invoice_refund_recorded", "invoice", invoice_id, {"amount_cents": amount_cents, "ref": ref},
                company_id=inv["company_id"])
    return await db.invoices.find_one({"id": invoice_id}, NO_ID)


# --------------------------------------------------------------------------------------------- overdue sweep

async def mark_overdue() -> int:
    """open invoices whose due date (Helsinki) has passed -> overdue, with one reminder e-mail each."""
    today = today_fi().isoformat()
    n = 0
    async for inv in db.invoices.find({"status": "open", "kind": "invoice", "due_date": {"$lt": today}}, NO_ID):
        r = await db.invoices.update_one({"id": inv["id"], "status": "open"},
                                         {"$set": {"status": "overdue", "overdue_since": now_iso(), "updated_at": now_iso()}})
        if r.modified_count:
            n += 1
            inv["status"] = "overdue"
            await audit(None, "invoice_overdue", "invoice", inv["id"], {"due_date": inv["due_date"]}, company_id=inv["company_id"])
            if not inv.get("demo"):
                await send_invoice_email(inv, kind="reminder")
            from core.notify import bi, notify_role

            await notify_role("finance", bi(f"Erääntynyt: {inv.get('number')}", f"Overdue: {inv.get('number')}"),
                              bi(f"{inv.get('company_name')} · {_fmt_eur(_amount_due(inv), 'fi')}, eräpäivä {_fmt_date(inv['due_date'], 'fi')}",
                                 f"{inv.get('company_name')} · {_fmt_eur(_amount_due(inv), 'en')} was due {inv['due_date']}"),
                              link=f"/ops/billing/{inv['id']}")
    return n


@background_loop(seconds=600)
async def overdue_loop():
    await mark_overdue()


# --------------------------------------------------------------------------------------------- lists

async def list_counts(base_q: dict) -> dict:
    counts = {s: 0 for s in STATUSES}
    async for row in db.invoices.aggregate([{"$match": base_q}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    totals = {"open_cents": 0, "overdue_cents": 0, "draft_cents": 0, "open_count": 0, "overdue_count": 0}
    aging = {"current": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "d90_plus": 0}
    aging_n = {k: 0 for k in aging}
    today = today_fi()
    async for inv in db.invoices.find({**base_q, "kind": "invoice", "status": {"$in": ["open", "overdue", "processing", "draft"]}},
                                      {"_id": 0, "status": 1, "total_cents": 1, "paid_cents": 1, "credited_cents": 1, "due_date": 1}):
        due_amt = _amount_due(inv)
        if inv["status"] == "draft":
            totals["draft_cents"] += inv.get("total_cents") or 0
            continue
        key = "overdue" if inv["status"] == "overdue" else "open"
        totals[f"{key}_cents"] += due_amt
        totals[f"{key}_count"] += 1
        d = to_date(inv.get("due_date"))
        late = (today - d).days if d else 0
        b = "current" if late <= 0 else "d1_30" if late <= 30 else "d31_60" if late <= 60 else "d61_90" if late <= 90 else "d90_plus"
        aging[b] += due_amt
        aging_n[b] += 1
    totals["outstanding_cents"] = totals["open_cents"] + totals["overdue_cents"]
    return {"counts": counts, "totals": totals,
            "aging": [{"bucket": k, "amount_cents": aging[k], "count": aging_n[k]} for k in aging]}


# --------------------------------------------------------------------------------------------- shipment milestones

async def _shipment_milestone(shipment_id: str, code: str, label: str, actor: dict | None, note: str | None = None):
    """Post-delivery milestones (INVOICED, PAID) through V1's services.shipments when available."""
    s = await db.shipments.find_one({"id": shipment_id}, {"_id": 0, "status": 1, "milestones": 1})
    if not s or s.get("status") != "delivered":
        return  # INVOICED / PAID follow DELIVERED in the shipment state machine (domain-spec 13.2)
    if any((m.get("code") or "").upper() == code for m in s.get("milestones") or []):
        return
    try:
        from services import shipments as v1  # noqa: WPS433 - lazy: V1 module may be mid-edit

        await v1.set_status(shipment_id, s.get("status") or "delivered", actor=actor, note=note, milestone=code.lower(),
                            source="billing")
        return
    except Exception as exc:  # noqa: BLE001 - fall back to a direct milestone so billing never blocks on V1
        log.info("V1 set_status unavailable for milestone %s (%s); writing milestone directly", code, exc)
    await db.shipments.update_one({"id": shipment_id}, {"$set": {"milestone": code.lower()}, "$push": {"milestones": {
        "code": code.lower(), "label": label, "at": now_iso(), "source": "billing", "location": None,
        "by": actor_ref(actor).get("name"), "planned": False, "note": note}}})


# --------------------------------------------------------------------------------------------- events

# shipment.delivered: the invoice agent (services.agents.invoice_agent) drafts and issues the invoice as a durable job.


async def validate_customs_invoice(inv: dict):
    """Last write boundary: even edited/manual drafts cannot bill an estimate or duplicate a disbursement."""
    customs = [ln for ln in inv.get("lines") or [] if (ln.get("code") or "").upper() in
               {"DUTY", "CUSTOMS_DUTY", "IMPORT_VAT", "CUSTOMS_VAT"} or ln.get("source") == "customs"]
    seen = set()
    for ln in customs:
        did = ln.get("source_ref")
        d = await db.customs_declarations.find_one({"id": did, "company_id": inv["company_id"],
                                                   "shipment_id": inv.get("shipment_id")}, NO_ID) if did else None
        decision = (d or {}).get("customs_decision") or {}
        code = (ln.get("code") or "").upper()
        key = "vat_cents" if "VAT" in code else "duty_cents"
        if not d or d.get("status") not in ("released", "closed") or decision.get("source") != "tulli_decision":
            raise BillingError("Record Tulli's decided duty and import VAT before customs charges can be invoiced.", 409)
        if decision.get("paid_by") != "forwarder" or key == "vat_cents" and d.get("vat_route") != "paid_to_customs":
            raise BillingError("This customs amount is payable by the importer and cannot be invoiced as a disbursement", 409)
        amount = ln.get("eur_amount_cents", ln.get("amount_cents"))  # s13 multi-currency: Tulli's EUR original
        if (did, key) in seen or ln.get("qty") != 1 or amount != decision.get(key):
            raise BillingError("Customs invoice amounts must match the recorded Tulli decision exactly", 409)
        seen.add((did, key))
        other = await db.invoices.find_one({"id": {"$ne": inv["id"]}, "kind": "invoice",
            "status": {"$nin": ["draft", "void", "credited"]},
            "lines": {"$elemMatch": {"source_ref": did, "code": ln.get("code")}}})
        if other:
            raise BillingError("The customs decision has already been invoiced", 409)


@on("customs.released")
@on("customs.decision_recorded")
async def _on_customs_decision(p: dict):
    d = p["declaration"]
    if not d.get("customs_decision"):
        return
    s = await db.shipments.find_one({"id": d.get("shipment_id")}, NO_ID)
    if not s or s.get("status") != "delivered" or d.get("status") not in ("released", "closed"):
        return
    inv = await db.invoices.find_one({"shipment_id": s["id"], "origin": "shipment", "status": {"$ne": "void"}}, NO_ID)
    if inv and inv["status"] == "draft":
        company = await _company(s["company_id"])
        warnings, info = [], []
        lines = await _quote_lines(s, company, warnings, inv["locale"])
        lines += await _extra_lines(s, company)
        lines += await _customs_lines(s, inv["locale"], warnings, info)
        totals = compute_totals(lines, on=to_date(inv.get("supply_date")) or today_fi(), settings=await get_settings())
        if (inv.get("currency") or "EUR") != "EUR":  # s13 multi-currency: rebuilt EUR lines -> invoice currency
            from services.fx_revaluation import localise_draft
            totals = await localise_draft(company, lines, settings=await get_settings(), origin="shipment",
                                          supply=to_date(inv.get("supply_date")) or today_fi(),
                                          currency=inv["currency"], fx_doc=inv.get("fx")) or totals
        await db.invoices.update_one({"id": inv["id"], "status": "draft"}, {"$set": {
            "lines": lines, "warnings": warnings, "info": info, **totals, "updated_at": now_iso()}})
        if not warnings:
            await _agent_issue(inv["id"])
    elif not inv:
        await create_invoice_for_shipment(s["id"], p.get("actor"))
    else:
        # Freight was invoiced separately. Customs gets its own draft and number, never changes an issued invoice.
        if await db.invoices.find_one({"shipment_id": s["id"], "origin": "customs_decision", "decision_id": d["id"],
                                       "status": {"$ne": "void"}}):
            return
        warnings, info = [], []
        lines = await _customs_lines(s, inv["locale"], warnings, info)
        lines = [ln for ln in lines if ln.get("source_ref") == d["id"]]
        if lines:
            settings = await get_settings()
            new = await _new_invoice(await _company(s["company_id"]), lines, settings=settings, actor=p.get("actor"),
                shipment=s, origin="customs_decision", warnings=warnings, info=info)
            await db.invoices.update_one({"id": new["id"]}, {"$set": {"decision_id": d["id"]}})
            if not warnings:
                await _agent_issue(new["id"])


async def _agent_issue(invoice_id: str):
    """The invoice agent decides (Autopilot on, kill switch off, guardrails) and issues; otherwise the owner queue."""
    from core.jobs import enqueue
    inv = await db.invoices.find_one({"id": invoice_id}, {"_id": 0, "company_id": 1})
    await enqueue("invoice.issue", {"invoice_id": invoice_id}, company_id=(inv or {}).get("company_id"),
                  subject=("invoice", invoice_id), key=f"invoice-issue:{invoice_id}")
