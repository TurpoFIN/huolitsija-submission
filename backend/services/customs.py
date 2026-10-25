"""Import customs declarations (H1, procedure 4000): AI-assisted drafting from the shipment and its
documents, valuation, duty / import VAT estimate, validations, state machine and the human-approved filing.

Legal references are to the Union Customs Code Reg (EU) 952/2013 and its acts (DA 2015/2446, IA 2015/2447);
952/2013 is repealed by Reg (EU) 2026/2108 (OJ 19.9.2026), applicable in stages to 2034.

State machine (domain-spec 13.1, enum.customs.*):
    draft -> needs_info (blocking customer data missing) -> draft
    draft -> ready_for_review (customer confirms goods data) -> approved (human approval "customs_filing")
    approved -> submitted (filed: message exchange, or recorded after manual filing) -> accepted (MRN)
    submitted -> rejected -> draft (amend)            accepted -> under_control -> released | not_released
    accepted -> released | not_released | invalidated  released -> closed
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import HTTPException

from core import config
from core.approvals import cancel_approvals, request_approval
from core.audit import actor_ref, agent_actor, audit
from core.db import db, register_index
from core.events import emit, on
from core.notify import bi, notify_company, notify_role, same
from core.refs import next_ref, ytunnus_valid
from core.settings import get_settings
from core.util import NO_ID, flight_label, new_id, now_iso
from services.documents import EU_COUNTRIES, _num, field_value
from services import customs_licences, taric  # s11: live EU TARIC duty + licences

log = logging.getLogger("customs")

STATUSES = ["draft", "needs_info", "ready_for_review", "approved", "submitted", "accepted", "rejected",
            "under_control", "released", "not_released", "invalidated", "closed"]
OPEN_STATUSES = {"draft", "needs_info", "ready_for_review", "approved", "submitted", "accepted", "rejected", "under_control"}
EDITABLE_CUSTOMER = {"draft", "needs_info"}
EDITABLE_BROKER = {"draft", "needs_info", "ready_for_review", "rejected"}

TRANSITIONS = {
    "draft": {"needs_info", "ready_for_review", "invalidated"},
    "needs_info": {"draft", "ready_for_review", "invalidated"},
    "ready_for_review": {"draft", "needs_info", "approved", "invalidated"},
    "approved": {"submitted", "accepted", "rejected", "draft", "released"},
    "submitted": {"accepted", "rejected", "released", "under_control", "not_released"},
    "rejected": {"draft", "needs_info", "submitted", "invalidated"},
    "accepted": {"under_control", "released", "not_released", "invalidated"},
    "under_control": {"released", "not_released", "invalidated"},
    "released": {"closed"},
    "not_released": {"closed"},
    "invalidated": {"closed"},
    "closed": set(),
}

register_index("customs_declarations", "id", unique=True)
register_index("customs_declarations", [("company_id", 1), ("created_at", -1)])
register_index("customs_declarations", [("status", 1), ("updated_at", -1)])
register_index("customs_declarations", "shipment_id")
register_index("fx_rates", "date", unique=True)

INCOTERM_GROUP = {"EXW": "E", "FCA": "F", "FAS": "F", "FOB": "F", "CFR": "C", "CIF": "C", "CPT": "C", "CIP": "C",
                  "DAP": "D", "DPU": "D", "DDP": "D"}
MODE_CODE = {"sea": "1", "rail": "2", "road": "3", "air": "4", "post": "5", "multimodal": "1"}
# The transport contract document a mode always travels on (DE 2/1). Sea is left open on purpose: a bill of lading
# (N705) and a sea waybill (N710) are different documents, so the type comes from the document or the carrier booking.
MODE_TRANSPORT_DOC = {"air": "air_waybill", "road": "cmr", "rail": "cim"}
TRANSPORT_DOC_TYPES = {"bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim"}


def transport_doc_type(mode: str | None, leg: dict | None = None) -> str | None:
    """Type of the main transport document when no uploaded document says it: the carrier booking's own type, else
    the only document the mode uses (air AWB, road CMR, rail CIM). None for sea without a booking type."""
    t = ((leg or {}).get("transport_doc_type") or "").strip().lower()
    t = {"bol": "bill_of_lading", "b/l": "bill_of_lading", "swb": "sea_waybill", "awb": "air_waybill"}.get(t, t)
    return t if t in TRANSPORT_DOC_TYPES else MODE_TRANSPORT_DOC.get((mode or "").lower())
DOC_CODES = {"commercial_invoice": "N380", "packing_list": "N271", "bill_of_lading": "N705", "sea_waybill": "N710",
             "air_waybill": "N740", "cmr": "N730", "cim": "N720", "certificate_of_origin": "N861", "eur1": "N954"}
GENERIC_TERMS = {"general cargo", "spare parts", "consolidated goods", "consolidated", "goods", "parts", "samples",
                 "gifts", "gift", "various", "miscellaneous", "accessories", "equipment", "machinery", "products",
                 "merchandise", "stc", "said to contain", "freight of all kinds", "fak", "personal effects",
                 "household goods", "textiles", "chemicals", "electronics", "food", "clothes", "toys", "tools"}
STOP = {"of", "and", "for", "the", "a", "an", "with", "in", "on", "to", "x", "pcs", "pc", "set", "sets", "new"}

# Finnish VAT rates as data (vero.fi / tulli.fi VAT rates page, checked 2026-10-24). valid_from inclusive.
VAT_TABLE = [
    {"category": "standard", "rate": 24.0, "valid_from": "2013-01-01"},
    {"category": "standard", "rate": 25.5, "valid_from": "2024-09-01"},
    {"category": "food", "rate": 14.0, "valid_from": "2013-01-01"},
    {"category": "food", "rate": 13.5, "valid_from": "2026-01-01"},
    {"category": "medicine_books", "rate": 10.0, "valid_from": "2013-01-01"},
    {"category": "medicine_books", "rate": 14.0, "valid_from": "2025-01-01"},
    {"category": "medicine_books", "rate": 13.5, "valid_from": "2026-01-01"},
    {"category": "newspapers", "rate": 10.0, "valid_from": "2013-01-01"},
]
VAT_CATEGORY_LABEL = {"standard": "General rate", "food": "Reduced rate: foodstuffs and animal feed",
                      "medicine_books": "Reduced rate: pharmaceuticals and books",
                      "newspapers": "Reduced rate: newspapers and periodicals"}


def vat_category(hs: str | None) -> str:
    hs = (hs or "")[:6]
    if not hs:
        return "standard"
    ch, h4 = hs[:2], hs[:4]
    if h4 in ("2203", "2204", "2205", "2206", "2207", "2208") or ch == "24" or ch == "01":
        return "standard"  # alcohol, tobacco and live animals are not foodstuffs
    if ch in ("02", "03", "04", "07", "08", "09", "10", "11", "15", "16", "17", "18", "19", "20", "21") or \
            h4 in ("1201", "1202", "1203", "1204", "1205", "1206", "1207", "1208", "1212", "2201", "2202", "2209") or \
            ch == "23" or h4 in ("0501",):
        return "food"
    if h4 in ("3003", "3004", "4901", "4903"):
        return "medicine_books"
    if h4 == "4902":
        return "newspapers"
    return "standard"


def vat_rate_for(hs: str | None, on: str | None = None) -> tuple[float, str]:
    on = on or now_iso()[:10]
    cat = vat_category(hs)
    rows = [r for r in VAT_TABLE if r["category"] == cat and r["valid_from"] <= on]
    rate = max(rows, key=lambda r: r["valid_from"])["rate"]
    return rate, cat


# ---------------------------------------------------------------------------------------------
# Customs exchange rate (IA 2015/2447 art. 146): ECB rate of the penultimate Wednesday of the previous
# month applies for the whole month. Rates are fetched from the ECB 90-day history and cached in Mongo.
# ---------------------------------------------------------------------------------------------
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"


def penultimate_wednesday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    weds = []
    while d.month == month:
        if d.weekday() == 2:
            weds.append(d)
        d -= timedelta(days=1)
    return weds[1]


async def _ecb_rates() -> dict[str, dict]:
    cached = await db.system.find_one({"key": "ecb_rates_fetched"})
    stale = not cached or cached.get("at", "") < (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H")
    if stale:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(ECB_URL)
                r.raise_for_status()
            root = ET.fromstring(r.content)
            for cube in root.iter():
                if cube.tag.endswith("Cube") and cube.get("time"):
                    rates = {c.get("currency"): float(c.get("rate")) for c in cube if c.get("currency")}
                    await db.fx_rates.update_one({"date": cube.get("time")}, {"$set": {"rates": rates, "source": "ECB"}},
                                                 upsert=True)
            await db.system.update_one({"key": "ecb_rates_fetched"}, {"$set": {"at": now_iso()}}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("ECB rates fetch failed: %s", exc)
    return {}


async def customs_rate(currency: str, on: str | None = None) -> dict | None:
    """Units of `currency` per 1 EUR valid for customs on date `on` (YYYY-MM-DD)."""
    currency = (currency or "EUR").upper()
    if currency == "EUR":
        return {"currency": "EUR", "rate": 1.0, "source": "EUR", "rate_date": None, "applies_month": None}
    on_d = date.fromisoformat((on or now_iso())[:10])
    prev = date(on_d.year - (on_d.month == 1), (on_d.month - 2) % 12 + 1, 1)
    target = penultimate_wednesday(prev.year, prev.month)
    await _ecb_rates()
    doc = await db.fx_rates.find_one({"date": {"$lte": target.isoformat()}}, NO_ID, sort=[("date", -1)])
    if not doc or currency not in (doc.get("rates") or {}):
        return None
    return {"currency": currency, "rate": doc["rates"][currency], "source": "ECB euro reference rate",
            "rate_date": doc["date"], "applies_month": on_d.strftime("%Y-%m"),
            "rule": "IA 2015/2447 art. 146: ECB rate of the penultimate Wednesday of the previous month"}


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def eori_normalise(v: str | None) -> str | None:
    """Finnish EORI as Tulli and the EU EOS registry hold it: FI + Business ID with the hyphen."""
    if not v:
        return None
    s = re.sub(r"\s+", "", str(v)).upper()
    m = re.fullmatch(r"FI(\d{7})-?(\d)", s)
    if m:
        return f"FI{m.group(1)}-{m.group(2)}"
    return s


def eori_shape_ok(v: str | None) -> bool:
    """The form the EU EOS check takes (services.kyc.eori_candidates): Finnish = FI + Business ID, hyphen optional;
    other countries = country code + 1-15 letters or digits. No check-digit test: EOS decides whether it exists."""
    s = eori_normalise(v) or ""
    return bool(re.fullmatch(r"FI\d{7}-\d", s) or (not s.startswith("FI") and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{1,15}", s)))


def eori_format_ok(v: str | None) -> bool:
    s = eori_normalise(v) or ""
    if s.startswith("FI"):
        return bool(re.fullmatch(r"FI\d{7}-\d", s)) and ytunnus_valid(s[2:])
    return bool(re.fullmatch(r"[A-Z]{2}[A-Za-z0-9]{1,15}", s))


def mrn_ok(mrn: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{2}[A-Z]{2}[A-Z0-9]{12}[A-Z][0-9]", (mrn or "").upper()))


def _iso6346_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    v = 10 + ord(ch) - ord("A")
    return v + (v - 1) // 10  # skip 11, 22, 33 (multiples of 11)


def mrn_check_digit(mrn17: str) -> str:
    """UCC MRN check digit: ISO 6346 algorithm over the first 17 characters."""
    return str(sum(_iso6346_value(c) * (2 ** i) for i, c in enumerate(mrn17.upper())) % 11 % 10)


IMPORT_MRN_PROCEDURES = {"R", "S", "W"}  # import only, import + ENS, import with special fiscal territories


def mrn_advisories(mrn: str) -> list[str]:
    """Typo hints for a format-valid MRN. Advisory only: the broker copies the MRN from Tulli's decision."""
    out = []
    m = mrn.upper()
    if mrn_check_digit(m[:17]) != m[17]:
        out.append(f"Check digit {m[17]} does not match ({mrn_check_digit(m[:17])} expected): compare with Tulli's decision")
    if m[16] not in IMPORT_MRN_PROCEDURES:
        out.append(f"Procedure letter {m[16]} is not an import declaration letter (R, S or W)")
    yy = int(now_iso()[2:4])
    if int(m[:2]) not in (yy, yy - 1):
        out.append(f"Year 20{m[:2]} is not this or last year")
    return out


def description_quality(desc: str | None) -> str | None:
    """RULE S-2: at least 3 meaningful words and not a generic term. Returns a problem or None."""
    d = (desc or "").strip().lower()
    if not d:
        return "Goods description is missing"
    if d in GENERIC_TERMS or re.sub(r"[^a-z ]", "", d).strip() in GENERIC_TERMS:
        return f"'{desc}' is a generic description that Customs rejects; describe what the goods are and their material/use"
    words = [w for w in re.findall(r"[a-zA-ZÀ-ɏ]{2,}", d) if w not in STOP]
    if len(words) < 3:
        return f"'{desc}' is too short; use at least three meaningful words (what, material, use)"
    return None


def _cents(v) -> int | None:
    n = _num(v)
    return None if n is None else int(round(n * 100))


def _short(s: str | None, n: int = 512) -> str | None:
    return s[:n] if s else s


def _party_from_company(c: dict) -> dict:
    a = c.get("address") or {}
    return {"name": c.get("name"), "business_id": c.get("business_id"), "eori": eori_normalise(c.get("eori")),
            "vat_id": c.get("vat_id"), "address": {"street": a.get("street"), "postal_code": a.get("postal_code"),
                                                    "city": a.get("city"), "country": a.get("country") or c.get("country") or "FI"}}


def _forwarder_party() -> dict:
    from core import config

    return {"name": config.COMPANY_NAME, "business_id": config.COMPANY_BUSINESS_ID or None,
            "eori": eori_normalise(config.COMPANY_EORI) or None, "vat_id": config.COMPANY_VAT_ID or None,
            "address": {"street": config.COMPANY_ADDRESS, "country": "FI"}}


# ---------------------------------------------------------------------------------------------
# Valuation inputs from the shipment's accepted quote (RULE I-5; Tulli: costs up to the customs office of
# first entry in the EU are in the customs value, costs from there to the destination are in the VAT base)
# ---------------------------------------------------------------------------------------------
POST_BORDER = re.compile(r"dest|dthc|thc[_ -]?d|delivery|on-?carriage|import|customs|clearance|\bdo\b|delivery order|"
                         r"storage|demurrage|detention|isps|ens|unload|devann|handling at|terminal.*(fi|helsinki|kotka)|"
                         r"forwarding|admin|disbursement", re.I)
PRE_BORDER = re.compile(r"pick-?up|origin|othc|thc[_ -]?o|export|ocean|sea ?freight|air ?freight|road ?freight|"
                        r"rail ?freight|main|freight|line ?haul|baf|caf|lss|bunker|ets|fuel|security|screening|vgm|"
                        r"b/?l fee|documentation|awb|carriage", re.I)
PASS_THROUGH = re.compile(r"duty|import vat|alv|tulli|vat\b", re.I)


async def quote_cost_split(shipment: dict) -> dict:
    """Split the accepted quote's lines into pre-border transport, insurance, post-border costs."""
    out = {"pre_border_cents": 0, "insurance_cents": 0, "post_border_cents": 0, "lines": [], "found": False,
           "quote_id": shipment.get("quote_id")}
    if not shipment.get("quote_id"):
        return out
    q = await db.quotes.find_one({"id": shipment["quote_id"]}, NO_ID)
    if not q:
        return out
    opt_id = shipment.get("option_id") or q.get("accepted_option_id")
    opt = next((o for o in q.get("options") or [] if o.get("id") == opt_id), None)
    if not opt:
        return out
    out["found"] = True
    out["quote_number"] = q.get("number")
    main_seq = next((lg.get("seq") for lg in opt.get("legs") or [] if lg.get("kind") == "main"), None)
    for ln in opt.get("lines") or []:
        text = f"{ln.get('code', '')} {ln.get('description', '')}"
        amt = int(ln.get("amount_cents") or 0)
        stage = ln.get("stage")
        if stage:
            # V2 quote lines carry a stage: origin | main | destination | fees | insurance | pass_through.
            # Only the origin and the main (border-crossing) leg are carriage up to the EU border; connection
            # legs after the first EU port, destination charges and forwarding fees go to the VAT base (ALVL 91 §).
            if stage == "pass_through" or ln.get("is_pass_through"):
                bucket = "excluded"
            elif stage == "insurance":
                bucket = "insurance"
            elif stage == "origin" or (stage == "main" and (main_seq is None or ln.get("leg_seq") in (None, main_seq))):
                bucket = "pre_border"
            else:
                bucket = "post_border"
        elif PASS_THROUGH.search(text) and ln.get("is_pass_through"):
            bucket = "excluded"
        elif re.search(r"insur", text, re.I):
            bucket = "insurance"
        elif POST_BORDER.search(text):
            bucket = "post_border"
        elif PRE_BORDER.search(text):
            bucket = "pre_border"
        else:
            bucket = "post_border"
        if bucket != "excluded":
            out[f"{bucket}_cents" if bucket != "insurance" else "insurance_cents"] += amt
        out["lines"].append({"code": ln.get("code"), "description": ln.get("description"), "amount_cents": amt,
                             "bucket": bucket})
    return out


# ---------------------------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------------------------
async def _shipment_docs(shipment_id: str) -> dict:
    docs = await db.documents.find({"shipment_id": shipment_id}, NO_ID).sort("uploaded_at", -1).to_list(200)
    pick = lambda t: next((d for d in docs if d.get("type") in t and (d.get("extraction") or {}).get("status")  # noqa: E731
                           in ("done", "needs_review")), None)
    return {"all": docs, "invoice": pick({"commercial_invoice"}), "packing": pick({"packing_list"}),
            "transport": pick({"bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim"}),
            "origin": pick({"certificate_of_origin", "eur1"})}


def _match_packing(desc: str, plines: list[dict], used: set) -> dict | None:
    from difflib import SequenceMatcher

    best, score = None, 0.0
    for i, p in enumerate(plines):
        if i in used:
            continue
        s = SequenceMatcher(None, (desc or "").lower(), (p.get("description") or "").lower()).ratio()
        if s > score:
            best, score = i, s
    if best is not None and score >= 0.55:
        used.add(best)
        return plines[best]
    return None


async def draft_declaration_for_shipment(shipment_id: str, actor: dict | None, *, classify: bool = True) -> dict:
    """Build (or rebuild, while still a draft) the import declaration for a shipment from its commercial
    invoice, packing list, transport document and accepted quote. HS codes are proposed by the HS agent
    (classify=False uses only the codes printed on the invoice, checked against HS 2022 — no AI call).
    Returns the declaration."""
    from services import hs as hs_service

    shipment = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not shipment:
        raise HTTPException(404, "Shipment not found")
    company = await db.companies.find_one({"id": shipment.get("company_id")}, NO_ID) or {}
    existing = await db.customs_declarations.find_one(
        {"shipment_id": shipment_id, "type": "import", "status": {"$nin": ["invalidated", "not_released"]}}, NO_ID)
    if existing and existing["status"] not in ("draft", "needs_info"):
        return existing
    docs = await _shipment_docs(shipment_id)
    inv, pl, tdoc = docs["invoice"], docs["packing"], docs["transport"]
    settings = await get_settings()
    today = now_iso()[:10]
    drafter = actor or agent_actor("customs_drafter")

    # ---- items from the invoice lines -------------------------------------------------------
    inv_items = list(((inv or {}).get("extraction") or {}).get("line_items") or [])
    plines = list(((pl or {}).get("extraction") or {}).get("line_items") or [])
    inv_origin = field_value(inv, "country_of_origin")
    shipper_country = ((shipment.get("origin") or {}).get("country") or "").upper() or None
    currency = (field_value(inv, "currency") or "").upper() or None
    items: list[dict] = []
    used: set = set()
    if not inv_items:
        cargo = shipment.get("cargo") or {}
        if cargo.get("description"):
            inv_items = [{"description": cargo.get("description"), "quantity": cargo.get("packages"),
                          "amount": (cargo.get("value_cents") or 0) / 100 or None, "hs_code": cargo.get("hs_hint"),
                          "gross_weight_kg": cargo.get("weight_kg"), "_from": "booking"}]
            currency = (cargo.get("currency") or "").upper() or None
    for i, li in enumerate(inv_items):
        pmatch = _match_packing(li.get("description") or "", plines, used) if plines else None
        items.append({
            "seq": i + 1, "description": li.get("description"), "quantity": _num(li.get("quantity")),
            "unit": li.get("unit"), "unit_price": _num(li.get("unit_price")), "invoice_amount": _num(li.get("amount")),
            "currency": currency, "hs_hint": (li.get("hs_code") or None), "hs_code": None, "cn_code": None,
            "taric_code": None, "taric_additional_codes": [], "hs_candidates": [], "cn_confirmed": False,
            "origin_country": (li.get("country_of_origin") or inv_origin or None),
            "origin_source": "invoice" if (li.get("country_of_origin") or inv_origin) else None,
            "marks": (pmatch or {}).get("marks_and_numbers") or field_value(pl, "marks_and_numbers") or field_value(tdoc, "marks_and_numbers"),
            "packages": _num((pmatch or {}).get("packages")), "package_type": (pmatch or {}).get("package_type"),
            "net_kg": _num(li.get("net_weight_kg")) or _num((pmatch or {}).get("net_weight_kg")),
            "gross_kg": _num(li.get("gross_weight_kg")) or _num((pmatch or {}).get("gross_weight_kg")),
            "weight_source": "invoice" if li.get("gross_weight_kg") or li.get("net_weight_kg") else
            ("packing_list" if pmatch else None),
            "preference": "100", "duty_rate_pct": None, "duty_source": None, "flags": [],
            "edited": [], "source_line": i + 1, "source": li.get("_from") or "invoice",
        })
    # Apportion consignment weights / packages when lines do not carry them.
    tot_gross = _num(field_value(pl, "total_gross_weight_kg")) or _num(field_value(inv, "total_gross_weight_kg")) or \
        _num(field_value(tdoc, "total_gross_weight_kg")) or _num((shipment.get("cargo") or {}).get("weight_kg"))
    tot_net = _num(field_value(pl, "total_net_weight_kg")) or _num(field_value(inv, "total_net_weight_kg"))
    tot_pk = _num(field_value(pl, "total_packages")) or _num(field_value(tdoc, "total_packages")) or \
        _num((shipment.get("cargo") or {}).get("packages"))
    values = [it["invoice_amount"] or 0 for it in items]
    for key, total in (("gross_kg", tot_gross), ("net_kg", tot_net)):
        if total and items and any(it[key] is None for it in items):
            known = sum(it[key] or 0 for it in items)
            rest = max(0.0, total - known)
            share_base = sum(values[i] for i, it in enumerate(items) if it[key] is None) or 1
            for i, it in enumerate(items):
                if it[key] is None:
                    it[key] = round(rest * (values[i] / share_base), 2)
                    it["weight_source"] = "apportioned"
    if items and tot_pk and all(it["packages"] is None for it in items):
        items[0]["packages"] = int(tot_pk)
        items[0]["package_type"] = field_value(pl, "package_type") or field_value(tdoc, "package_type") or (shipment.get("cargo") or {}).get("package_type")
        for it in items[1:]:
            it["packages"] = 0
            it["package_type"] = items[0]["package_type"]
    # Origin fallback: shipment origin country is NOT proof of origin; leave missing so a person confirms it.

    # ---- HS classification (one batched AI call) ---------------------------------------------
    # A redraft reuses earlier AI results and every field a person edited, matched by line description, so
    # adding a packing list after the invoice does not re-run the classifier or undo a broker's work.
    prev = {}
    for old in (existing or {}).get("items") or []:
        key = (old.get("description") or "").strip().lower()
        if key:
            prev[key] = old
    keep = ("hs_suggestion", "hs_candidates", "flags", "hs_code", "hs_source", "cn_code", "cn_confirmed", "cn_confirmed_by",
            "cn_confirmed_at", "taric_code", "taric_additional_codes", "duty_rate_pct", "duty_source", "duty_label",
            "preference_note", "taric_url", "preference")
    todo = []
    for it in items:
        old = prev.get((it.get("description") or "").strip().lower())
        old_cn_ok = not (old or {}).get("cn_code") or bool(hs_service.cn_entry(old["cn_code"]))  # earlier results from
        # before the CN 2026 check may carry an invented CN code: classify those again
        if old and old_cn_ok and (old.get("hs_suggestion") or {}).get("source") in ("ai", "invoice") \
                and old.get("hs_hint") == it.get("hs_hint") \
                and (old.get("origin_country") == it.get("origin_country") or "origin_country" in (old.get("edited") or [])):
            for k in keep:
                if k in old:
                    it[k] = old[k]
            for k in old.get("edited") or []:
                it[k] = old.get(k)
            it["edited"] = list(old.get("edited") or [])
        else:
            if old:
                for k in old.get("edited") or []:
                    it[k] = old.get(k)
                it["edited"] = list(old.get("edited") or [])
            todo.append(it)
    hs_results = []
    if todo and not classify:
        hs_results = [printed_code_result(it) for it in todo]
    elif todo:
        try:
            hs_results = await hs_service.classify_items(
                [{"description": it["description"], "origin_country": it["origin_country"], "hs_hint": it["hs_hint"]}
                 for it in todo], ref={"type": "shipment", "id": shipment_id}, company_id=company.get("id"))
        except Exception as exc:  # noqa: BLE001
            log.warning("HS classification failed for %s: %s", shipment_id, exc)
            hs_results = []
    for it, res in zip(todo, hs_results or [None] * len(todo)):
        apply_hs_result(it, res)

    # ---- parties, transport, valuation -------------------------------------------------------
    rep = company.get("representation_default") or "direct"
    importer = _party_from_company(company)
    forwarder = _forwarder_party()
    from services.customs_import_data import seller_address, document_number, source_location
    seller = {"name": field_value(inv, "seller_name") or ((shipment.get("parties") or {}).get("shipper") or {}).get("name"),
              "address": seller_address(inv, shipment), "country": field_value(inv, "seller_country") or shipper_country,
              "eori": eori_normalise(field_value(inv, "seller_eori")), "vat_id": field_value(inv, "seller_vat_id")}
    buyer = {"name": field_value(inv, "buyer_name"), "address": field_value(inv, "buyer_address"),
             "country": field_value(inv, "buyer_country"), "eori": eori_normalise(field_value(inv, "buyer_eori"))}
    legs = shipment.get("legs") or []
    main_leg = next((lg for lg in legs if lg.get("mode") == shipment.get("mode")), legs[0] if legs else {})
    containers = sorted(set(field_value(pl, "container_numbers") or []) | set(field_value(tdoc, "container_numbers") or []))
    transport = {
        "mode": shipment.get("mode"), "mode_at_border": MODE_CODE.get(shipment.get("mode") or "", None),
        "inland_mode": MODE_CODE.get(main_leg.get("mode") or shipment.get("mode")), "containerised": bool(containers or (shipment.get("cargo") or {}).get("containers")),
        "container_numbers": containers,
        "means_on_arrival": ((main_leg.get("vessel") or {}).get("name") or flight_label(main_leg.get("flight")) or
                             main_leg.get("truck_plate") or field_value(tdoc, "vessel_name") or field_value(tdoc, "vehicle_plate") or None),
        "transport_document": {"type": (tdoc or {}).get("type") or transport_doc_type(main_leg.get("mode") or
                                                                                    shipment.get("mode"), main_leg),
                               "number": document_number(tdoc) or main_leg.get("transport_doc_no"),
                               "document_id": (tdoc or {}).get("id")},
        "port_of_arrival": (shipment.get("destination") or {}).get("unlocode"),
        "nationality": main_leg.get("nationality") or (main_leg.get("vessel") or {}).get("flag") or field_value(tdoc, "vessel_flag") or field_value(tdoc, "vehicle_country"),
        "identification_type": main_leg.get("identification_type"),
        "dispatch_country": shipper_country,
        "destination_country": (shipment.get("destination") or {}).get("country"),
    }
    incoterm = (field_value(inv, "incoterm") or shipment.get("incoterm") or "").upper() or None
    incoterm_place = field_value(inv, "incoterm_place") or shipment.get("incoterm_place")
    split = await quote_cost_split(shipment)
    fx = await customs_rate(currency, today) if currency else None
    group = INCOTERM_GROUP.get(incoterm or "", None)
    valuation = {
        "method": "1", "method_label": "Transaction value (UCC art. 70)", "incoterm": incoterm, "incoterm_group": group,
        "invoice_currency": currency,
        "invoice_total": _num(field_value(inv, "total_amount")),
        "invoice_stated_freight": _num(field_value(inv, "freight_amount")),
        "invoice_stated_insurance": _num(field_value(inv, "insurance_amount")),
        "freight_to_border_cents": split["pre_border_cents"] if group in ("E", "F") else 0,
        "insurance_cents": split["insurance_cents"] if group in ("E", "F") or incoterm in ("CFR", "CPT") else 0,
        "post_border_cents": split["post_border_cents"],
        "freight_known": split["found"] or group in ("C", "D"),
        "allocation": "weight" if items and all(it.get("gross_kg") for it in items) else "value",
        "quote": {"id": split.get("quote_id"), "number": split.get("quote_number"), "lines": split["lines"]},
        "exchange_rate": fx, "notes": [],
    }
    if group in ("E", "F"):
        valuation["notes"].append("Freight and insurance up to the EU border are added to the invoice price "
                                  "(IA art. 137 / RULE I-5); " + (f"taken from the accepted quote {split.get('quote_number') or ''} lines."
                                                                  if split["found"] else
                                                                  "no priced quote on this shipment, so the broker enters them."))
    elif group in ("C", "D"):
        valuation["notes"].append(f"{incoterm}: the seller's price already includes carriage to the named place; "
                                  "costs after the EU border may be deducted if shown separately.")
    if shipment.get("mode") == "air":
        valuation["notes"].append("Air freight: only the share set in IA Annex 23-01 for the departure airport "
                                  "belongs in the customs value — check before filing.")

    decl = existing or {
        "id": new_id(), "lrn": await next_ref("LRN"), "mrn": None, "shipment_id": shipment_id,
        "company_id": shipment.get("company_id"), "type": "import", "status": "draft", "created_at": now_iso(),
        "history": [], "approval_id": None, "submission": None, "office": None, "location_of_goods": None,
        "demo": bool(shipment.get("demo")), "test": bool(shipment.get("test")),
    }
    decl.update({
        "submitter": decl.get("submitter") or shipment.get("customs_submitter") or {"name": config.COMPANY_NAME, "email": config.COMPANY_EMAIL},
        "transaction_nature": shipment.get("transaction_nature") or field_value(inv, "transaction_nature"),
        "shipment_ref": shipment.get("reference"), "company_name": company.get("name"),
        "declaration_type": "IM", "additional_declaration_type": "A", "procedure_code": "4000",
        "additional_procedure": "999", "representation_type": decl.get("representation_type") or rep,
        "importer": importer, "seller": seller, "buyer": buyer, "exporter": seller,
        "declarant": importer if (decl.get("representation_type") or rep) == "direct" else forwarder,
        "representative": forwarder,
        "representative_status_code": "2" if (decl.get("representation_type") or rep) == "direct" else "3",
        "delivery_terms": {"incoterm": incoterm, "place": incoterm_place},
        "invoice": {"number": field_value(inv, "invoice_number"), "date": field_value(inv, "invoice_date"),
                    "currency": currency, "total": valuation["invoice_total"], "document_id": (inv or {}).get("id"),
                    "confirmed": bool(((inv or {}).get("extraction") or {}).get("confirmed_at")),
                    "extraction_status": ((inv or {}).get("extraction") or {}).get("status")},
        "transport": transport, "valuation": valuation, "items": items,
        "documents": [{"document_id": d["id"], "type": d["type"], "code": DOC_CODES.get(d["type"]),
                       "number": document_number(d), "filename": d.get("filename")}
                      for d in docs["all"] if d.get("type") in DOC_CODES],
        "ai": {"drafted_by": actor_ref(drafter), "drafted_at": now_iso(),
               "hs_model": next((r.get("model") for r in hs_results or [] if r and r.get("model")),
                                ((existing or {}).get("ai") or {}).get("hs_model")),
               "hs_source": next((r.get("source") for r in hs_results or [] if r),
                                 ((existing or {}).get("ai") or {}).get("hs_source")),
               "sources": [d["id"] for d in (inv, pl, tdoc) if d],
               "label": "AI drafted · broker approves before filing"},
        "updated_at": now_iso(),
    })
    if not decl.get("office"):
        decl["office"] = shipment.get("office_of_presentation")
    if not decl.get("goods_location"):
        decl["goods_location"] = source_location(shipment)
    await recompute(decl, company=company, shipment=shipment, settings=settings)
    if existing:
        await db.customs_declarations.replace_one({"id": decl["id"]}, decl)
        await _log(decl, "redrafted", drafter, "Rebuilt from the latest documents")
    else:
        decl["history"].append({"at": now_iso(), "from": None, "to": decl["status"], "by": actor_ref(drafter),
                                "note": "Drafted from the shipment documents"})
        await db.customs_declarations.insert_one(dict(decl))
        await db.shipments.update_one({"id": shipment_id}, {"$addToSet": {"customs_declaration_ids": decl["id"]}})
        await audit(drafter, "customs_drafted", "customs_declaration", decl["id"],
                    {"lrn": decl["lrn"], "shipment_id": shipment_id, "items": len(items)}, company_id=decl["company_id"])
        n_fi = "1 tavaranimike luonnosteltu" if len(items) == 1 else f"{len(items)} tavaranimikettä luonnosteltu"
        await notify_role("customs_broker", bi(f"Tulli-ilmoitusluonnos {decl['lrn']} ({shipment.get('reference') or ''})",
                                               f"Customs draft {decl['lrn']} ({shipment.get('reference') or ''})"),
                          bi(f"{n_fi}: {company.get('name')}", f"{len(items)} goods items drafted for {company.get('name')}"),
                          link=f"/ops/customs/{decl['id']}")
        if decl["company_id"]:
            ref = shipment.get("reference")
            await notify_company(decl["company_id"], bi("Tulli-ilmoitus luonnosteltu", "Customs declaration drafted"),
                                 bi(f"Tarkista lähetyksen {ref} tavaratiedot" if ref else "Tarkista lähetyksesi tavaratiedot",
                                    f"Check the goods data for {ref or 'your shipment'}"),
                                 link=f"/portal/customs/{decl['id']}")
    for d in (inv, pl, tdoc):
        if d:
            await db.documents.update_one({"id": d["id"]}, {"$pull": {"used_by": {"declaration_id": decl["id"]}}})
            await db.documents.update_one({"id": d["id"]}, {"$push": {"used_by": {"declaration_id": decl["id"],
                                                                                   "status": decl["status"]}}})
    return await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)


def _printed_cn(hint: str, entry: dict | None) -> dict:
    """A CN code printed on an invoice is kept only if it is a real CN 2026 code under its HS6 (suppliers print
    outdated or foreign codes); otherwise the CN subdivisions are offered for the broker to choose."""
    from services import hs as hs_service

    if not entry:
        return {"cn8": None, "cn_options": []}
    r = hs_service.resolve_cn(hint[:6], hint[:8] if len(hint) >= 8 else None)
    return {"cn8": r["cn8"], "cn_options": r["cn_options"], "cn8_note": r.get("note")}


def printed_code_result(it: dict) -> dict:
    """HS result built only from the code printed on the invoice (validated against HS 2022, no AI)."""
    from services import hs as hs_service

    hint = re.sub(r"\D", "", it.get("hs_hint") or "")
    entry = hs_service.hs_entry(hint[:6]) if len(hint) >= 6 else None
    today = now_iso()[:10]
    return {"description": it.get("description"), "origin_country": it.get("origin_country"),
            "status": "needs_review", "reason": "Printed on the invoice; not classified by AI",
            "hs6": hint[:6] if entry else None, "hs6_description": (entry or {}).get("description"),
            **_printed_cn(hint, entry), "confidence": None, "rationale": None,
            "candidates": [], "duty": {}, "flags": hs_service.flags_for(hint[:6] if entry else None, it.get("origin_country"),
                                                                        it.get("description") or ""),
            "taric_url": hs_service.taric_url(hint[:8] or hint[:6], it.get("origin_country"), today) if entry else None,
            "hs_hint_check": {"code": hint, "valid": bool(entry), "agrees": None,
                              "note": "Code printed on the invoice" if entry else "Printed code is not in HS 2022"} if hint else None,
            "model": None, "source": "invoice", "classified_at": now_iso()}


def apply_hs_result(it: dict, res: dict | None):
    """Copy an HS agent result onto a declaration item (only fields a person has not edited)."""
    if not res:
        return
    edited = set(it.get("edited") or [])
    it["hs_suggestion"] = {k: res.get(k) for k in ("hs6", "hs6_description", "cn8", "cn8_note", "cn8_description", "cn_options",
                                                     "cn_resolution", "cn8_proposed", "confidence",
                                                     "rationale", "rationale_i18n", "status", "reason", "reason_i18n", "taric_url", "source", "model",
                                                     "classified_at", "hs_hint_check", "duty")}
    if res.get("ebti") is not None:
        it["hs_suggestion"]["ebti"] = res["ebti"]  # services.ebti: official BTI precedent for the code
    it["hs_candidates"] = res.get("candidates") or []
    it["flags"] = res.get("flags") or []
    if "hs_code" not in edited:
        hint = (it.get("hs_hint") or "")
        hint_ok = bool((res.get("hs_hint_check") or {}).get("valid"))
        it["hs_code"] = res.get("hs6") or (hint[:6] if hint_ok else None)
        it["hs_source"] = "ai" if res.get("hs6") else ("invoice" if hint_ok else None)
    if "cn_code" not in edited:
        from services import hs as hs_service

        cn = res.get("cn8")
        hint = re.sub(r"\D", "", it.get("hs_hint") or "")
        if hint and len(hint) >= 8 and it.get("hs_code") and hint.startswith(it["hs_code"]) and hs_service.cn_entry(hint[:8]):
            cn = hint[:8]  # the supplier's printed code, when it is a real CN 2026 code under the chosen HS6
        it["cn_code"] = cn if cn and it.get("hs_code") and cn.startswith(it["hs_code"]) and hs_service.cn_entry(cn) else None
        it["cn_description"] = (hs_service.cn_entry(it["cn_code"]) or {}).get("description") if it["cn_code"] else None
        it["cn_confirmed"] = False
        if "taric_code" not in edited and len(hint) == 10 and hint[:8] == it.get("cn_code"):
            # Preserve the supplier's full code; TARIC declarability and duty checks still gate it.
            it["taric_code"] = hint
    if "duty_rate_pct" not in edited and (it.get("taric") or {}).get("status") in (None, "unavailable", "needs_code"):
        # s11: the AI rate is only a fallback until the live EU TARIC answers (recompute -> taric.apply_to_item)
        duty = res.get("duty") or {}
        it["duty_rate_pct"] = duty.get("rate_pct")
        it["duty_source"] = "ai_estimate" if duty.get("rate_pct") is not None else None
        it["duty_label"] = duty.get("label")
        it["preference_note"] = duty.get("preference_note")
    it["taric_url"] = res.get("taric_url")


# ---------------------------------------------------------------------------------------------
# Totals and validations
# ---------------------------------------------------------------------------------------------
def _v(rule, ok, message, *, severity="blocking", owner="broker", item=None, field=None):
    return {"rule": rule, "ok": bool(ok), "severity": severity, "owner": owner, "message": message, "item": item,
            "field": field}


def _alloc(total: int, weights: list[float]) -> list[int]:
    """Split integer cents by weights (largest remainder); the parts always sum to total."""
    n = len(weights)
    if not n:
        return []
    w = [max(0.0, float(x or 0)) for x in weights]
    sw = sum(w)
    if sw <= 0:
        w, sw = [1.0] * n, float(n)
    exact = [total * x / sw for x in w]
    base = [int(e // 1) for e in exact]
    rest = total - sum(base)
    order = sorted(range(n), key=lambda i: (exact[i] - base[i]), reverse=True)
    for i in order[:rest]:
        base[i] += 1
    return base


async def recompute(decl: dict, *, company: dict | None = None, shipment: dict | None = None,
                    settings: dict | None = None, on: str | None = None):
    """Recalculate values, duty, VAT, totals and validations, and settle draft vs needs_info."""
    from services import hs as hs_service

    company = company or await db.companies.find_one({"id": decl.get("company_id")}, NO_ID) or {}
    shipment = shipment or await db.shipments.find_one({"id": decl.get("shipment_id")}, NO_ID) or {}
    settings = settings or await get_settings()
    today = on or now_iso()[:10]
    items = decl.get("items") or []
    from services import customs_origin  # s11: DE 4/17 preference + proof (DE 2/3) per item from official data

    val = decl.get("valuation") or {}
    fx = val.get("exchange_rate")
    rate = (fx or {}).get("rate")

    # transaction value per item: invoice line amount + its share of invoice-level charges
    line_sum = sum(it.get("invoice_amount") or 0 for it in items)
    inv_total = val.get("invoice_total")
    extra_inv = (inv_total - line_sum) if (inv_total is not None and line_sum and inv_total > line_sum) else 0
    gross_all = all((it.get("gross_kg") or 0) > 0 for it in items)
    gsum = sum(it.get("gross_kg") or 0 for it in items) or 1
    freight = int(val.get("freight_to_border_cents") or 0)
    insurance = int(val.get("insurance_cents") or 0)
    post = int(val.get("post_border_cents") or 0)
    totals = {"invoice_value_cents": 0, "customs_value_cents": 0, "duty_cents": 0, "vat_base_cents": 0, "vat_cents": 0,
              "gross_kg": 0.0, "net_kg": 0.0, "packages": 0, "duty_complete": True, "value_complete": True}
    vat_registered = bool(company.get("vat_registered"))
    shares_v = [((it.get("invoice_amount") or 0) / line_sum) if line_sum else (1 / len(items)) for it in items]
    shares_w = [((it.get("gross_kg") or 0) / gsum) if gross_all else sv for it, sv in zip(items, shares_v)]
    # Shared costs are split with the largest-remainder method so the item amounts always add up to the
    # declaration-level figure to the cent (independent rounding drifts by a cent or two on multi-item entries).
    alloc_f, alloc_i, alloc_p = _alloc(freight, shares_w), _alloc(insurance, shares_v), _alloc(post, shares_v)
    raw_eur = []
    for it, sv in zip(items, shares_v):
        amount = it.get("invoice_amount")
        raw_eur.append(None if amount is None or not rate else (amount + extra_inv * sv) / rate * 100)
    auto = [i for i, it in enumerate(items) if not ("value_cents" in (it.get("edited") or []) and it.get("value_cents") is not None)
            and raw_eur[i] is not None]
    auto_cents = _alloc(int(round(sum(raw_eur[i] for i in auto))), [raw_eur[i] for i in auto]) if auto else []
    auto_map = dict(zip(auto, auto_cents))
    for idx, it in enumerate(items):
        if "value_cents" in (it.get("edited") or []) and it.get("value_cents") is not None:
            inv_cents = it["value_cents"]
        else:
            inv_cents = auto_map.get(idx)
            it["value_cents"] = inv_cents
        it["freight_cents"] = alloc_f[idx]
        it["insurance_cents"] = alloc_i[idx]
        it["post_border_cents"] = alloc_p[idx]
        if inv_cents is None:
            it["customs_value_cents"] = None
            totals["value_complete"] = False
        else:
            it["customs_value_cents"] = inv_cents + it["freight_cents"] + it["insurance_cents"]
    await customs_origin.apply_to_declaration(decl, on=today)
    for it in items:
        await taric.apply_to_item(it, on=today)  # s11: live EU TARIC duty; AI estimate only when TARIC is down
        rate_pct = it.get("duty_rate_pct")
        rate_ok = (it.get("taric") or {}).get("status") == "unavailable" or (
            (it.get("taric") or {}).get("status") == "ok" and "duty_rate_pct" in (it.get("edited") or []))
        if taric.item_duty_cents(it) is not None and it.get("customs_value_cents") is not None:
            it["duty_cents"] = taric.item_duty_cents(it)
        elif it.get("customs_value_cents") is not None and rate_pct is not None and rate_ok:
            it["duty_cents"] = int(round(it["customs_value_cents"] * float(rate_pct) / 100))
        else:
            it["duty_cents"] = None
            totals["duty_complete"] = False
        if "vat_rate_pct" not in (it.get("edited") or []):
            vr, cat = vat_rate_for(it.get("hs_code"), today)
            it["vat_rate_pct"], it["vat_category"] = vr, cat
            it["vat_rate_label"] = VAT_CATEGORY_LABEL.get(cat)
        if it.get("customs_value_cents") is not None and it.get("duty_cents") is not None:
            it["vat_base_cents"] = it["customs_value_cents"] + it["duty_cents"] + it["post_border_cents"]
            it["vat_cents"] = int(round(it["vat_base_cents"] * float(it["vat_rate_pct"]) / 100))
        else:
            it["vat_base_cents"] = it["vat_cents"] = None
        it["statistical_value_cents"] = it.get("customs_value_cents")
        it["flags"] = it.get("flags") or hs_service.flags_for(it.get("hs_code"), it.get("origin_country"),
                                                             it.get("description") or "")
        if (it.get("taric") or {}).get("status") not in (None, "unavailable", "needs_code"):
            it["flags"] = [f for f in it["flags"] if f.get("kind") != "trade_defence"]
        if it.get("hs_code") and not it.get("taric_url"):
            it["taric_url"] = hs_service.taric_url(it.get("cn_code") or it["hs_code"], it.get("origin_country"), today)
        if it.get("cn_code") and not it.get("cn_description"):
            it["cn_description"] = (hs_service.cn_entry(it["cn_code"]) or {}).get("description")
        for k in ("value_cents", "customs_value_cents", "duty_cents", "vat_base_cents", "vat_cents"):
            tk = {"value_cents": "invoice_value_cents"}.get(k, k)
            totals[tk] += it.get(k) or 0
        totals["gross_kg"] += it.get("gross_kg") or 0
        totals["net_kg"] += it.get("net_kg") or 0
        totals["packages"] += int(it.get("packages") or 0)
    totals["gross_kg"] = round(totals["gross_kg"], 2)
    totals["net_kg"] = round(totals["net_kg"], 2)
    # A sum of the known items is not the declaration's total when an item is
    # missing. Preserve the gap through JSON, review and PDF serialization.
    if not totals["duty_complete"]:
        totals["duty_cents"] = None
    totals["vat_complete"] = all(it.get("vat_cents") is not None for it in items)
    if not totals["vat_complete"]:
        totals["vat_base_cents"] = totals["vat_cents"] = None
    route = "tilitys" if vat_registered and company.get("tilitys_eligible", True) is not False else "paid_to_customs"
    decl["vat_route"] = route
    decl["vat_route_label"] = ("Import VAT: postponed via VAT return (tilitysmenettely)" if route == "tilitys"
                               else "Import VAT: payable to Customs")
    totals["payable_to_customs_cents"] = (totals["duty_cents"] if totals["duty_complete"] else None)
    if totals["payable_to_customs_cents"] is not None and route == "paid_to_customs":
        totals["payable_to_customs_cents"] += totals["vat_cents"]
    totals["estimate"] = True
    totals["estimate_label"] = "Estimate: duty rates are AI estimates until a broker confirms them in TARIC"
    if items and all(it.get("duty_source") == "taric" for it in items):  # s11
        totals["estimate"] = not totals["duty_complete"]
        totals["estimate_label"] = f"Duty from the EU TARIC consultation ({today})"
    try:  # s11: licences / certificates required by TARIC measure conditions -> DE 2/3 documents, rule LIC-1
        await customs_licences.attribute(decl, on=today)
    except Exception:  # noqa: BLE001 - LIC-1 then reports the requirement check as unavailable
        log.exception("licence attribution failed for %s", decl.get("id"))
        for it in items:
            it["licence_check"] = {"status": "unavailable", "at": now_iso()}
    if not val.get("freight_known"):
        totals["freight_pending"] = True
        totals["estimate_label"] += (". Freight and insurance to the EU border are not included yet; the customs value, "
                                     "duty and VAT will rise when the broker adds them")
    td = sorted({it["seq"] for it in items for f in it.get("flags") or [] if f.get("kind") == "trade_defence"})
    if td:
        totals["trade_defence_items"] = td
        totals["estimate_label"] += (f". Anti-dumping or countervailing duty may apply to item(s) {', '.join(map(str, td))} "
                                     "and is not included")
    decl["totals"] = totals
    decl["validations"] = await validate(decl, company, shipment, settings)
    blocking_customer = [v for v in decl["validations"] if not v["ok"] and v["severity"] == "blocking"
                         and v["owner"] == "customer"]
    if decl.get("status") in ("draft", "needs_info"):
        decl["status"] = "needs_info" if blocking_customer else "draft"
    decl["updated_at"] = now_iso()
    return decl


async def validate(decl: dict, company: dict, shipment: dict, settings: dict) -> list[dict]:
    out: list[dict] = []
    items = decl.get("items") or []
    imp = decl.get("importer") or {}
    out.append(_v("E-1", eori_format_ok(imp.get("eori")),
                  "Importer EORI " + (f"{imp.get('eori')} is valid in format" if eori_format_ok(imp.get("eori")) else
                                      "is missing or malformed: add it in Company (apply in Tulli's EORI Number "
                                      "Registration Service if you have none)"), owner="customer", field="importer.eori"))
    from services.kyc import manual_eori_applies

    eori_check = (company.get("kyc") or {}).get("eori") or {}
    if manual_eori_applies(eori_check, imp.get("eori")):
        by = (eori_check.get("by") or {}).get("name") or "a broker"
        out.append(_v("E-1", True, f"EORI verified manually by {by} on {(eori_check.get('at') or '')[:10]}: "
                                   f"{eori_check.get('note')}", severity="advisory", owner="broker", field="importer.eori"))
    elif eori_check.get("status") == "fail":
        out.append(_v("E-1", False, f"The EU EORI registry did not find {imp.get('eori')}; check the number, or a broker "
                                    "can record a documented manual verification", owner="customer", field="importer.eori"))
    poa = company.get("poa") or {}
    rep = decl.get("representation_type") or "direct"
    from services.kyc import poa_valid

    poa_ok = poa_valid(company) and poa.get("type") == rep and "import" in (poa.get("scope") or [])
    out.append(_v("R-2", poa_ok, ("Power of attorney on file: " + f"{poa.get('type')} representation, valid to {poa.get('valid_to')}")
                  if poa_ok else f"A valid power of attorney for {rep} representation covering imports is needed "
                                 f"before filing (Company → Power of attorney)", owner="customer"))
    if rep == "indirect":
        out.append(_v("R-1", True, "Indirect representation: Huolitsija is the declarant and jointly liable for the "
                                   "customs debt (UCC art. 77(3), 84)", severity="advisory"))
    kyc = company.get("kyc_status")
    out.append(_v("KYC", kyc == "approved", "Customer KYC approved" if kyc == "approved" else
                  f"Customer KYC is {kyc or 'not started'}; it must be approved before filing", owner="broker"))
    sanc = company.get("sanctions_status")
    out.append(_v("K-1", sanc in ("clear",), "Customer sanctions screening clear" if sanc == "clear" else
                  f"Customer sanctions status is {sanc or 'not screened'}; compliance must clear it (RULE K-1)"))
    from services.customs_screening import validation_rows
    out.extend(await validation_rows(decl, shipment))
    dt = decl.get("delivery_terms") or {}
    out.append(_v("I-2", bool(dt.get("incoterm") and dt.get("place")),
                  f"Delivery terms {dt.get('incoterm')} {dt.get('place')}" if dt.get("incoterm") and dt.get("place")
                  else "Incoterm and named place are required (DE 4/1)", owner="customer"))
    inv = decl.get("invoice") or {}
    out.append(_v("DOC-2", bool(inv.get("document_id")), "Commercial invoice attached" if inv.get("document_id") else
                  "Upload the commercial invoice", owner="customer"))
    if inv.get("document_id"):
        out.append(_v("DOC-1", inv.get("extraction_status") == "done" or inv.get("confirmed"),
                      "Invoice fields read and checked" if inv.get("extraction_status") == "done" or inv.get("confirmed")
                      else "Confirm the commercial invoice fields (some were unclear)", owner="customer"))
    td = (decl.get("transport") or {}).get("transport_document") or {}
    out.append(_v("DOC-2", bool(td.get("number")), f"Transport document {td.get('number')}" if td.get("number") else
                  "Transport document number (B/L, AWB or CMR) is missing (DE 2/1)", owner="broker"))
    if td.get("number"):  # s11: the official FI415B/FI515A message needs the document type code as well
        out.append(_v("DOC-3", bool(td.get("type") in TRANSPORT_DOC_TYPES or td.get("code")),
                      f"Transport document type {td.get('type') or td.get('code')}" if td.get("type") or td.get("code") else
                      "Transport document type is missing: bill of lading, sea waybill, AWB, CMR or CIM (DE 2/1)",
                      owner="broker", field="transport.transport_document.type"))
    fx = (decl.get("valuation") or {}).get("exchange_rate")
    cur = (decl.get("valuation") or {}).get("invoice_currency")
    out.append(_v("I-5", bool(fx and fx.get("rate")), f"Exchange rate {cur} {fx.get('rate')} per EUR ({fx.get('source')}, "
                  f"{fx.get('rate_date') or 'n/a'})" if fx and fx.get("rate") else
                  f"No customs exchange rate for {cur}; enter Tulli's monthly rate"))
    v = decl.get("valuation") or {}
    out.append(_v("I-5", bool(v.get("freight_known")), "Freight to the EU border included" if v.get("freight_known") else
                  f"{v.get('incoterm') or 'This incoterm'}: freight and insurance to the EU border are not known; "
                  "enter them under valuation"))
    out.append(_v("C-2", bool((decl.get("office") or {}).get("code")),
                  f"Customs office of presentation {(decl.get('office') or {}).get('code')}" if (decl.get("office") or {}).get("code")
                  else "Set the customs office of presentation (DE 5/26)"))
    # Arrival presentation uses ENT; inland release for free circulation uses RFC.
    # Sandbox retains synthetic lifecycle tests and makes no official-office claim.
    from services import customs_offices, tulli
    if (await tulli.gateway_for(decl)).channel != "sandbox":
        code = (decl.get("office") or {}).get("code")
        try:
            row = await customs_offices.verify(code)
            ok = any(customs_offices.eligible(row, role) for role in ("ENT", "RFC"))
            message = "COL-toimipaikan rooli ja voimassaolo / COL office role and validity: " + str(code)
        except ValueError as exc:
            ok, message = False, str(exc)
        out.append(_v("COL-IMPORT", ok, message, field="office"))
    out.append(_v("C-2", bool(decl.get("goods_location")), f"Location of goods {decl.get('location_of_goods')}"
                  if decl.get("location_of_goods") else "Set the location of goods (DE 5/23)"))
    if not items:
        out.append(_v("C-3", False, "No goods items: the invoice has no readable lines", owner="customer"))
    cbam_items = []
    for it in items:
        n = it["seq"]
        problem = description_quality(it.get("description"))
        out.append(_v("S-2", problem is None, problem or "Goods description is specific", owner="customer", item=n,
                      field="description"))
        out.append(_v("H-1", bool(it.get("hs_code")), f"HS {it.get('hs_code')}" if it.get("hs_code") else
                      "Commodity code missing", item=n, field="hs_code"))
        from services import hs as hs_service

        cn_real = bool(it.get("cn_code") and hs_service.cn_entry(it["cn_code"]))
        cn_ok = bool(cn_real and it.get("cn_confirmed"))
        if it.get("cn_code") and not cn_real:
            msg = f"CN {it['cn_code']} is not a {hs_service.CN_EDITION} code: choose the CN subdivision again"
        elif not it.get("cn_code"):
            msg = "Choose the 8-digit CN subdivision and confirm it against TARIC on the declaration date"
        else:
            msg = f"CN {it.get('cn_code')} confirmed in TARIC" if cn_ok else \
                "Confirm the 8-digit CN code against TARIC on the declaration date"
        out.append(_v("H-1", cn_ok, msg, item=n, field="cn_code"))
        out.append(_v("ORIGIN", bool(it.get("origin_country")), f"Origin {it.get('origin_country')}" if it.get("origin_country")
                      else "Country of origin missing (DE 5/15)", owner="customer", item=n, field="origin_country"))
        g, nn = it.get("gross_kg"), it.get("net_kg")
        w_ok = bool(g and g > 0 and (nn is None or nn <= g))
        out.append(_v("WEIGHT", w_ok, "Weights stated" if w_ok else
                      ("Net weight exceeds gross weight" if (g and nn and nn > g) else "Gross weight missing"),
                      owner="customer", item=n, field="gross_kg"))
        if it.get("customs_value_cents") is None:
            out.append(_v("I-5", False, "Item value could not be converted to EUR", item=n))
        if it.get("duty_rate_pct") is None and it.get("duty_cents") is None and it.get("hs_code"):
            out.append(_v("H-2", False, "Duty rate not estimated; look it up in TARIC", severity="advisory", item=n))
        for f in it.get("flags") or []:
            if f.get("kind") == "trade_defence":
                cleared = bool(it.get("taric_additional_codes") or (it.get("trade_defence_clearance_reason")
                              and it.get("trade_defence_cleared_by") and it.get("trade_defence_cleared_at")))
                out.append(_v("H-4", cleared, "TARIC trade-defence assessment recorded" if cleared else
                              "Anti-dumping risk: enter a TARIC additional code or record the evidence clearing the risk",
                              severity="blocking", item=n, field="taric_additional_codes"))
                continue
            sev = "blocking" if f.get("severity") == "blocking" else "advisory"
            if f.get("kind") == "cbam":
                cbam_items.append(n)
                continue
            out.append(_v(f.get("rule") or f.get("kind"), False, f.get("message"), severity=sev, item=n))
    if cbam_items:
        ok = bool(company.get("cbam_declarant_id") or company.get("cbam_under_threshold"))
        out.append(_v("B-1", ok, ("CBAM goods: importer's authorised CBAM declarant " +
                                  (company.get("cbam_declarant_id") or "status: under 50 t / year declared"))
                      if ok else f"CBAM goods on item(s) {', '.join(map(str, cbam_items))}: the importer needs an "
                                 "authorised CBAM declarant ID, or a statement that CBAM imports stay under 50 t a year"))
    origin = ((shipment.get("origin") or {}).get("country") or "").upper()
    if origin in ("RU", "BY"):
        out.append(_v("K-2", False, f"Shipment from {origin}: mandatory compliance review, default deny (RULE K-2)"))
    # weight tie-out vs documents (DOC-3)
    checks = []
    if decl.get("shipment_id"):
        d = await db.documents.find_one({"shipment_id": decl["shipment_id"], "checks.0": {"$exists": True}}, {"_id": 0, "checks": 1})
        checks = (d or {}).get("checks") or []
    for c in checks:
        if c.get("ok") is False:
            v = _v("DOC-3", False, c.get("message"), severity="advisory")
            if c.get("message_fi"):
                v["message_i18n"] = {"en": c.get("message"), "fi": c["message_fi"]}
            out.append(v)
    tot = (decl.get("totals") or {})
    doc_gross = _num(field_value(await db.documents.find_one({"id": (decl.get("transport") or {}).get(
        "transport_document", {}).get("document_id")}, NO_ID) if (decl.get("transport") or {}).get("transport_document", {}).get(
        "document_id") else None, "total_gross_weight_kg"))
    if doc_gross and tot.get("gross_kg"):
        diff = abs(doc_gross - tot["gross_kg"]) / max(doc_gross, tot["gross_kg"]) * 100
        out.append(_v("DOC-3", diff <= 2, f"Declared gross weight {tot['gross_kg']:,.1f} kg ties to the transport document"
                      if diff <= 2 else f"Declared gross weight {tot['gross_kg']:,.1f} kg differs {diff:.1f} % from the "
                                        f"transport document ({doc_gross:,.1f} kg)", severity="advisory"))
    if tot.get("invoice_value_cents") and tot["invoice_value_cents"] <= 15000:
        out.append(_v("L-1", False, "Consignment value is EUR 150 or less: for distance sales a fixed EUR 3 duty per goods "
                                    "item applies from 1.7.2026 (Reg (EU) 2026/382) and an EUR 2 handling fee (Y126) "
                                    "from 1.11.2026; confirm the regime", severity="advisory"))
    out.append(_v("V-3", True, decl.get("vat_route_label") or "", severity="advisory", owner="customer"))
    from services import customs_origin  # s11: ORIGIN-PREF (blocking only for a preference claimed without valid proof)

    out.extend(customs_origin.validations(decl))
    out.extend(taric.rules(decl))  # s11: TARIC-1/TARIC-2/TD-1/QUOTA-1
    out.extend(customs_licences.rules(decl))  # s11: LIC-1
    from services.customs_import_data import validations as import_validations
    out.extend(import_validations(decl))
    return out


def blocking(decl: dict, owner: str | None = None) -> list[dict]:
    return [v for v in decl.get("validations") or [] if not v["ok"] and v["severity"] == "blocking"
            and (owner is None or v["owner"] == owner)]


# ---------------------------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------------------------
async def _log(decl: dict, action: str, actor: dict | None, note: str | None = None, data: dict | None = None):
    await audit(actor, f"customs_{action}", "customs_declaration", decl["id"], {"lrn": decl.get("lrn"), "note": note,
                                                                                 **(data or {})},
                company_id=decl.get("company_id"))


async def transition(decl: dict, to: str, actor: dict | None, note: str | None = None, extra: dict | None = None,
                     force: bool = False) -> dict:
    frm = decl["status"]
    if to not in STATUSES:
        raise HTTPException(400, f"Unknown status {to}")
    if not force and to != frm and to not in TRANSITIONS.get(frm, set()):
        raise HTTPException(409, f"A declaration that is {frm.replace('_', ' ')} cannot become {to.replace('_', ' ')}")
    entry = {"at": now_iso(), "from": frm, "to": to, "by": actor_ref(actor), "note": note}
    sets = {"status": to, "updated_at": now_iso(), **(extra or {})}
    await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": sets, "$push": {"history": entry}})
    await db.documents.update_many({"used_by.declaration_id": decl["id"]},
                                   {"$set": {"used_by.$[u].status": to}},
                                   array_filters=[{"u.declaration_id": decl["id"]}])
    await _log(decl, f"status_{to}", actor, note)
    fresh = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
    return fresh


_FP_ITEM = ("package_type", "invoice_amount", "currency", "procedure_code", "additional_procedure", "marks", "origin_proofs", "bom", "seq", "description", "hs_code", "cn_code", "taric_code", "taric_additional_codes", "trade_defence_clearance_reason", "origin_country", "preference", "net_kg",
            "gross_kg", "packages", "value_cents", "customs_value_cents", "duty_rate_pct", "duty_cents", "vat_rate_pct",
            "vat_cents", "documents")
_FP_DECL = ("goods_location", "invoice", "importer", "declarant", "exporter", "seller", "representative", "documents", "submitter", "declaration_type", "additional_declaration_type", "procedure_code", "additional_procedure", "transaction_nature", "language", "origin_proofs", "parties", "representation_type", "office", "location_of_goods", "transport", "delivery_terms", "vat_route",
            "procedure")


def filing_fingerprint(decl: dict) -> str:
    """Hash of everything that goes into the filed message. A filing approval is bound to this value: if the
    declaration changes after approval was requested, the approval is void (the approver saw other numbers)."""
    val = decl.get("valuation") or {}
    body = {"items": [{k: it.get(k) for k in _FP_ITEM} for it in decl.get("items") or []],
            **{k: decl.get(k) for k in _FP_DECL},
            "valuation": {k: val.get(k) for k in ("freight_to_border_cents", "insurance_cents", "post_border_cents",
                                                   "invoice_total", "invoice_currency", "method")},
            "rate": (val.get("exchange_rate") or {}).get("rate")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


async def save_and_recompute(decl: dict, actor: dict | None, action: str, data: dict | None = None) -> dict:
    await recompute(decl)
    decl.pop("_id", None)
    if decl.get("approval_id") and decl.get("approval_fingerprint") and decl["status"] == "ready_for_review" \
            and (filing_fingerprint(decl) != decl["approval_fingerprint"] or blocking(decl)):
        await cancel_approvals(decl["id"], "customs_filing")
        reason = ("The declaration changed after filing approval was requested; request approval again"
                  if filing_fingerprint(decl) != decl["approval_fingerprint"] else
                  "A blocking check reopened after filing approval was requested; resolve it and request approval again")
        await _log(decl, "approval_voided", actor, reason)
        decl["approval_voided"] = {"at": now_iso(), "reason": reason, "by": actor_ref(actor), "approval_id": decl["approval_id"]}
        decl["approval_id"] = None
        decl["approval_fingerprint"] = None
    await db.customs_declarations.replace_one({"id": decl["id"]}, decl)
    await _log(decl, action, actor, data=data)
    return await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)


async def submit_for_review(decl: dict, actor: dict) -> dict:
    if decl["status"] not in ("draft", "needs_info"):
        raise HTTPException(409, f"This declaration is {decl['status'].replace('_', ' ')}; nothing to confirm")
    decl = await save_and_recompute(decl, actor, "recomputed")
    missing = blocking(decl, "customer")
    if missing:
        raise HTTPException(400, "Complete these first: " + "; ".join(v["message"] for v in missing[:4]))
    decl = await transition(decl, "ready_for_review", actor, "Goods data confirmed",
                            {"confirmation": {"by": actor_ref(actor), "at": now_iso()}})
    await notify_role("customs_broker", bi(f"{decl['lrn']} valmis tarkistettavaksi", f"{decl['lrn']} ready for review"),
                      bi(f"{decl.get('company_name')} vahvisti tavaratiedot", f"{decl.get('company_name')} confirmed the goods data"),
                      link=f"/ops/customs/{decl['id']}")
    return decl


async def request_filing(decl: dict, actor: dict) -> dict:
    """Broker asks for the human approval that gates filing (approval kind customs_filing)."""
    if decl["status"] not in ("ready_for_review", "draft", "needs_info", "rejected"):
        raise HTTPException(409, f"This declaration is {decl['status'].replace('_', ' ')}")
    decl = await save_and_recompute(decl, actor, "recomputed")
    block = blocking(decl)
    if block:
        raise HTTPException(400, "Resolve blocking checks first: " + "; ".join(
            (f"item {v['item']}: " if v.get("item") else "") + v["message"] for v in block[:5]))
    # s11 AEO controls (services/customs_aeo.py): a person who may not file gets 403 customs_not_authorised (409
    # customs_controls_failed for POA / EORI / KYC / sanctions); the four-eyes requirement travels on the card.
    from services import customs_aeo
    aeo = await customs_aeo.filing_controls(decl, actor, kind="import", stage="request")
    customs_aeo.raise_if_refused(aeo)
    if decl["status"] != "ready_for_review":
        decl = await transition(decl, "ready_for_review", actor, "Broker completed the review", force=True)
    t = decl.get("totals") or {}
    fp = filing_fingerprint(decl)
    req = dict(
        summary=f"{decl.get('company_name')}: {len(decl.get('items') or [])} items, customs value "
                f"{t.get('customs_value_cents', 0) / 100:,.2f} EUR, duty {t.get('duty_cents', 0) / 100:,.2f} EUR, "
                f"VAT {t.get('vat_cents', 0) / 100:,.2f} EUR "
                f"({'postponed via VAT return' if decl.get('vat_route') == 'tilitys' else 'payable to Customs'}), "
                f"{decl.get('representation_type')} representation",
        reason="Filing a customs declaration creates a customs debt and liability: a person must approve it",
        company_id=decl.get("company_id"), requested_by=actor, amount_cents=t.get("duty_cents"),
        link=f"/ops/customs/{decl['id']}",
        data={"lrn": decl["lrn"], "representation": decl.get("representation_type"), "fingerprint": fp,
              "aeo": customs_aeo.summary(aeo)})
    title = f"File import declaration {decl['lrn']} ({decl.get('shipment_ref') or ''})"
    # The customs agent's gate: Autopilot on, inside the guardrails and no legal human point (the director's
    # delegation covers this representation type). Otherwise the filing waits for a person in the owner queue.
    from core.agent_runtime import card_fields, link_card
    from services.agents.customs_agent import gate_filing
    gate = await gate_filing(decl, {"title": title, "summary": req["summary"]}, create_approval=False,
                             aeo=aeo if customs_aeo.actor_kind(actor) == "agent" else None)
    req["extra"] = card_fields(gate)
    approval = await request_approval("customs_filing", "customs_declaration", decl["id"], title, **req)
    if (approval.get("data") or {}).get("fingerprint") != fp:  # a pending approval for older content is void
        await cancel_approvals(decl["id"], "customs_filing")
        approval = await request_approval("customs_filing", "customs_declaration", decl["id"], title, **req)
    await link_card(gate, approval)
    await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"approval_id": approval["id"],
                                                                           "approval_fingerprint": fp, "approval_voided": None}})
    await _log(decl, "approval_requested", actor, data={"approval_id": approval["id"]})
    if gate.auto and approval.get("status") == "pending":
        from core.agent_runtime import agent_bot
        bot = agent_bot("customs_agent")
        approval.update(status="approved", decided_at=now_iso(), decided_by={"id": bot["id"], "name": bot["name"]},
                        decision_note="Autopilot: inside the guardrails, delegation recorded")
        await db.approvals.update_one({"id": approval["id"]}, {"$set": {k: approval[k] for k in ("status", "decided_at", "decided_by", "decision_note")}})
        from core.autonomy import action
        await action("customs_approved", "customs_declaration", decl["id"], company_id=decl.get("company_id"),
                     data={"approval_id": approval["id"], "gateway_mode": config.TULLI_MODE, "agent_decision_id": gate.id},
                     reversible=False)
        await emit("approval.decided", {"approval": approval, "actor": bot})
    return await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)


@on("approval.decided")
async def _on_approval(p):
    a, actor = p["approval"], p.get("actor")
    if a.get("kind") != "customs_filing":
        return
    decl = await db.customs_declarations.find_one({"id": a["ref_id"]}, NO_ID)
    if not decl or decl["status"] != "ready_for_review" or decl.get("approval_id") != a["id"]:
        return
    if a["status"] == "approved" and ((a.get("data") or {}).get("fingerprint") != filing_fingerprint(decl) or blocking(decl)):
        await _log(decl, "approval_voided", actor, "Approved content differs from the current declaration or a blocking "
                                                   "check is open; not filed")
        await db.approvals.update_one({"id": a["id"]}, {"$set": {"status": "cancelled", "void_reason":
                                                                 "Declaration changed or a blocking check reopened"}})
        await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {
            "approval_id": None, "approval_fingerprint": None,
            "approval_voided": {"at": now_iso(), "reason": "Approved content differs from the current declaration or a "
                                "blocking check is open; not filed", "by": actor_ref(actor), "approval_id": a["id"]}}})
        return
    if a["status"] == "rejected":
        await transition(decl, "draft", actor, f"Filing approval rejected: {a.get('decision_note') or 'no reason given'}",
                         {"approval_id": None})
        return
    # s11 AEO controls: the approver may file and four eyes hold (not the preparer or a classifier above the
    # thresholds, unless the audited single-user override is on record). Otherwise nothing is filed.
    from services import customs_aeo
    refused = await customs_aeo.approval_refusal(a, decl, actor)
    if refused:
        why = f"{refused.code}: {refused.en}"
        await db.approvals.update_one({"id": a["id"]}, {"$set": {"status": "cancelled", "void_reason": why,
                                                                 "void_reason_fi": refused.fi}})
        await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {
            "approval_id": None, "approval_fingerprint": None,
            "approval_voided": {"at": now_iso(), "reason": why, "reason_fi": refused.fi, "code": refused.code,
                                "by": actor_ref(actor), "approval_id": a["id"]}}})
        await _log(decl, "approval_voided", actor, why)
        return
    decl = await transition(decl, "approved", actor, a.get("decision_note") or "Filing approved",
                            {"approved_by": actor_ref(actor), "approved_at": now_iso()})
    await emit("customs.approved", {"declaration": decl, "actor": actor})
    await file_declaration(decl, actor)


async def file_declaration(decl: dict, actor: dict | None) -> dict:
    from services import customs_aeo
    from services.tulli import gateway_for
    from services.customs_screening import validation_rows

    # Approval can predate a party edit, daily list refresh or rescreening hit.
    # Re-read and enforce controls at the last boundary before any gateway send.
    decl = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID) or decl
    shipment = await db.shipments.find_one({"id": decl.get("shipment_id")}, NO_ID) or {}
    controls = await validation_rows(decl, shipment)
    held = [v for v in controls if not v["ok"]]
    if held:
        raise HTTPException(409, {"fi": "Tullauksen valvontatarkistukset estävät lähetyksen.",
                                  "en": "Customs control checks block filing.", "checks": held})
    filing_date = now_iso()[:10]
    approved_fp = filing_fingerprint(decl)
    await recompute(decl, on=filing_date)
    if blocking(decl) or filing_fingerprint(decl) != approved_fp:
        raise HTTPException(409, "Declaration changed or blocking checks reopened; review before filing")
    # All filing controls must pass before any licence balance is consumed.
    await customs_aeo.enforce_filing(decl, actor, kind="import")
    await customs_licences.attribute(decl, consume=True, on=filing_date)
    await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"items": decl["items"]}})


    gw = await gateway_for(decl)
    try:
        result = await gw.submit(decl)
    except Exception as exc:  # noqa: BLE001
        log.exception("filing %s failed", decl["id"])
        result = {"channel": gw.channel, "state": "error", "error": str(exc)[:500], "at": now_iso()}
    sub = {**(decl.get("submission") or {}), **result, "messages": (decl.get("submission") or {}).get("messages", [])
           + result.pop("messages", [])}
    await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"submission": sub}})
    decl = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
    if result.get("state") == "sent":
        decl = await transition(decl, "submitted", actor, f"Sent to Tulli ({gw.channel})",
                                {"submission.submitted_at": now_iso()})
        if result.get("mrn"):
            decl = await transition(decl, "accepted", actor, f"Accepted, MRN {result['mrn']}", {"mrn": result["mrn"]})
    elif result.get("state") == "rejected":
        decl = await transition(decl, "submitted", actor, "Sent to Tulli")
        decl = await transition(decl, "rejected", actor, result.get("error") or "Rejected by Tulli")
    else:
        await _log(decl, "filing_prepared", actor, result.get("note") or result.get("error"))
    return decl


RECORDABLE = {"submitted", "accepted", "rejected", "under_control", "released", "not_released", "invalidated", "closed"}
RESPONDABLE = {"approved", "submitted", "accepted", "under_control", "rejected", "released", "not_released", "invalidated"}


async def record_response(decl: dict, event: str, actor: dict, *, mrn: str | None = None, note: str | None = None,
                          document_id: str | None = None, at: str | None = None) -> dict:
    """Broker records Tulli's answer (manual channel, or a correction to the message channel)."""
    if event not in RECORDABLE:
        raise HTTPException(400, f"Unknown response '{event}'")
    mrn = re.sub(r"\s", "", mrn or "").upper() or None
    if mrn and not mrn_ok(mrn):
        raise HTTPException(400, "MRN must be 18 characters: 2-digit year, country code FI, 12 characters, procedure "
                                 "letter and check digit (e.g. 26FI000000123456R7)")
    if mrn and mrn[2:4] != "FI":
        raise HTTPException(400, f"This MRN was issued by {mrn[2:4]}, not by Finnish Customs (FI)")
    hints = mrn_advisories(mrn) if mrn else []
    if hints:
        note = ((note or "") + ("\n" if note else "") + "MRN check: " + "; ".join(hints)).strip()
    if event in ("accepted", "released") and not (mrn or decl.get("mrn")):
        raise HTTPException(400, "Enter the MRN Tulli gave for this declaration")
    if event in ("rejected", "not_released", "under_control") and not note:
        raise HTTPException(400, "Add Tulli's reason or request as a note")
    st = decl["status"]
    if st not in RESPONDABLE:
        raise HTTPException(409, f"This declaration is {st.replace('_', ' ')}: Tulli's answer can be recorded only after "
                                 f"the filing is approved")
    extra = {"mrn": mrn or decl.get("mrn")}
    path = [event]
    if event == "released" and st in ("approved", "submitted"):
        path = (["submitted"] if st == "approved" else []) + ["accepted", "released"]
    elif event == "accepted" and st == "approved":
        path = ["submitted", "accepted"]
    elif event == "rejected" and st == "approved":
        path = ["submitted", "rejected"]
    cur_st = st
    for step in path:  # check the whole path before writing anything
        if step != cur_st and step not in TRANSITIONS.get(cur_st, set()):
            raise HTTPException(409, f"A declaration that is {cur_st.replace('_', ' ')} cannot become {step.replace('_', ' ')}")
        cur_st = step
    msg = {"id": new_id(), "direction": "in", "type": event, "at": at or now_iso(), "recorded_by": actor_ref(actor),
           "channel": "manual", "mrn": mrn, "note": note, "document_id": document_id}
    if decl.get("submission"):
        sub_set = {"submission.channel": decl["submission"].get("channel") or "manual"}
        if decl["submission"].get("state") in ("awaiting_manual_filing", None):
            sub_set["submission.state"] = "filed_manually"  # Tulli answered, so the broker filed it
        await db.customs_declarations.update_one(
            {"id": decl["id"]}, {"$push": {"submission.messages": msg}, "$set": sub_set})
    else:
        await db.customs_declarations.update_one(
            {"id": decl["id"]}, {"$set": {"submission": {"channel": "manual", "state": "filed_manually", "messages": [msg]}}})
    for step in path:
        cur = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
        stamp = {}
        if step == "submitted":
            stamp = {"submission.submitted_at": at or now_iso()}
        if step == "accepted":
            stamp = {"accepted_at": at or now_iso()}
        if step == "released":
            stamp = {"released_at": at or now_iso()}
        decl = await transition(cur, step, actor, note or (f"MRN {mrn}" if mrn else None), {**extra, **stamp})
    if event == "released":
        await on_released(decl, actor)
    elif event in ("rejected", "not_released", "under_control"):
        if decl.get("company_id"):
            event_fi = {"rejected": "hylätty", "not_released": "ei luovutettu", "under_control": "tarkastuksessa"}[event]
            await notify_company(decl["company_id"], bi(f"Tulli: {event_fi} ({decl['lrn']})",
                                                        f"Customs: {event.replace('_', ' ')} ({decl['lrn']})"), same(note or ""),
                                 link=f"/portal/customs/{decl['id']}")
    return decl


async def on_released(decl: dict, actor: dict | None):
    await emit("customs.released", {"declaration": decl})
    if decl.get("company_id"):
        ref = decl.get("shipment_ref") or decl["lrn"]
        await notify_company(decl["company_id"], bi(f"Tulli luovutti: {ref}", f"Released by Customs: {ref}"),
                             same(f"MRN {decl.get('mrn')}"), link=f"/portal/customs/{decl['id']}")
    sid = decl.get("shipment_id")
    if not sid:
        return
    try:
        from services import shipments as ship_svc  # V1, lazy: may not exist yet

        s = await db.shipments.find_one({"id": sid}, NO_ID)
        if s and any(h.get("type") == "customs" for h in s.get("holds") or []) and hasattr(ship_svc, "release_hold"):
            await ship_svc.release_hold(sid, "customs", actor, source="customs")
            s = await db.shipments.find_one({"id": sid}, NO_ID)
        if s and s.get("status") == "customs_hold" and hasattr(ship_svc, "set_status"):
            await ship_svc.set_status(sid, "at_hub", actor=actor, note=f"Released by Tulli, MRN {decl.get('mrn')}",
                                      milestone="customs_released", source="customs")
    except ImportError:
        log.warning("services.shipments not available; shipment %s hold not released", sid)
    except Exception:  # noqa: BLE001
        log.exception("releasing shipment %s after customs release failed", sid)


# ---------------------------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------------------------
CUSTOMER_ITEM_FIELDS = {"marks", "description", "origin_country", "net_kg", "gross_kg", "packages", "package_type", "quantity", "unit"}
BROKER_ITEM_FIELDS = CUSTOMER_ITEM_FIELDS | {"hs_code", "cn_code", "taric_code", "taric_additional_codes", "duty_rate_pct",
                                             "vat_rate_pct", "preference", "cn_confirmed", "value_cents", "invoice_amount", "trade_defence_clearance_reason", "origin_proofs", "bom", "documents"}
BROKER_FIELDS = {"office", "location_of_goods", "goods_location", "representation_type", "valuation", "delivery_terms", "transport", "origin_proofs", "submitter", "transaction_nature"}


def _coerce_item(field: str, value):
    if field == "documents":
        if not isinstance(value, list):
            raise HTTPException(400, "Documents must be a list of DE 2/3 codes and references")
        docs = []
        for d in value:
            if not isinstance(d, dict) or not re.fullmatch(r"[A-Z0-9]{4}", str(d.get("code") or "").upper()):
                raise HTTPException(400, "A document needs a four-character DE 2/3 code")
            ref = str(d.get("reference") or "").strip()
            if not ref:
                raise HTTPException(400, "A document or exemption needs a reference or statement")
            docs.append({"code": str(d["code"]).upper(), "reference": ref[:512], "source": "declared"})
        return docs
    if value in ("", None):
        return None
    if field == "origin_proofs":
        if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
            raise HTTPException(400, "Origin proofs must be an array of records")
        return value
    if field == "bom":
        if not isinstance(value, dict):
            raise HTTPException(400, "Bill of materials must be an object")
        return value
    if field in ("net_kg", "gross_kg", "quantity", "duty_rate_pct", "vat_rate_pct", "invoice_amount"):
        n = _num(value)
        if n is None or n < 0:
            raise HTTPException(400, f"{field.replace('_', ' ')} must be a positive number")
        return n
    if field in ("packages", "value_cents"):
        n = _num(value)
        if n is None or n < 0:
            raise HTTPException(400, f"{field.replace('_', ' ')} must be a positive number")
        return int(n)
    if field in ("hs_code", "cn_code", "taric_code"):
        d = re.sub(r"\D", "", str(value))
        want = {"hs_code": 6, "cn_code": 8, "taric_code": 10}[field]
        if len(d) != want:
            raise HTTPException(400, f"{field.replace('_', ' ').upper()} must have {want} digits")
        from services import hs as hs_service

        if field == "hs_code" and not hs_service.hs_entry(d):
            raise HTTPException(400, f"{d[:4]}.{d[4:]} is not an HS 2022 subheading")
        if field == "cn_code" and not hs_service.cn_entry(d):
            opts = hs_service.cn_options(d[:6])
            raise HTTPException(400, f"{d[:4]} {d[4:6]} {d[6:]} is not a {hs_service.CN_EDITION} code"
                                     + (f"; under {d[:6]} choose " + ", ".join(o["code"] for o in opts[:8]) if opts else ""))
        return d
    if field == "origin_country":
        s = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", s):
            raise HTTPException(400, "Country of origin must be a 2-letter ISO code, e.g. CN")
        return s
    if field == "cn_confirmed":
        return bool(value)
    if field == "taric_additional_codes":
        if not isinstance(value, list) or any(not re.fullmatch(r"[A-Z0-9]{4}", str(x).strip().upper()) for x in value):
            raise HTTPException(400, "TARIC additional codes must each contain four letters or digits")
        return [str(x).strip().upper() for x in value]
    if field == "trade_defence_clearance_reason":
        if len(str(value).strip()) < 10:
            raise HTTPException(400, "Explain the TARIC evidence clearing the anti-dumping risk (at least 10 characters)")
        return str(value).strip()[:1000]
    return str(value).strip()[:512]


async def apply_patch(decl: dict, body: dict, user: dict) -> dict:
    internal = user.get("org_type") == "internal"
    allowed_status = EDITABLE_BROKER if internal else EDITABLE_CUSTOMER
    if decl["status"] not in allowed_status:
        raise HTTPException(409, f"A declaration that is {decl['status'].replace('_', ' ')} cannot be edited"
                            + ("" if internal else "; message the customs team for corrections"))
    item_fields = BROKER_ITEM_FIELDS if internal else CUSTOMER_ITEM_FIELDS
    changes = []
    for patch in body.get("items") or []:
        seq = patch.get("seq")
        it = next((i for i in decl.get("items") or [] if i["seq"] == seq), None)
        if not it:
            raise HTTPException(404, f"Goods item {seq} not found")
        for k, v in patch.items():
            if k == "seq":
                continue
            if k not in item_fields:
                raise HTTPException(403, f"You cannot change {k.replace('_', ' ')}")
            nv = _coerce_item(k, v)
            if it.get(k) != nv:
                changes.append({"item": seq, "field": k, "from": it.get(k), "to": nv})
                it[k] = nv
                if k not in ("cn_confirmed",):
                    it.setdefault("edited", [])
                    if k not in it["edited"]:
                        it["edited"].append(k)
                if k == "hs_code":
                    it["hs_source"] = "broker"
                    if it.get("cn_code") and not it["cn_code"].startswith(nv or ""):
                        it["cn_code"], it["cn_confirmed"] = None, False
                    it["flags"] = []
                    it["taric_url"] = None
                if k == "cn_code":
                    it["cn_confirmed"] = bool(patch.get("cn_confirmed", False))
                    if nv and not nv.startswith(it.get("hs_code") or nv[:6]):
                        it["hs_code"] = nv[:6]
                        it["flags"] = []
                    it["cn_description"] = None
                    it["taric_url"] = None
                if k == "cn_confirmed" and nv:
                    if not it.get("cn_code"):
                        raise HTTPException(400, f"Item {seq}: enter the CN code before confirming it")
                    it["cn_confirmed_by"] = actor_ref(user)
                    it["cn_confirmed_at"] = now_iso()
                if k == "trade_defence_clearance_reason":
                    it["trade_defence_cleared_by"] = actor_ref(user)
                    it["trade_defence_cleared_at"] = now_iso()
                if k in ("hs_code", "cn_code", "origin_country"):
                    it.pop("trade_defence_clearance_reason", None)
                    it.pop("trade_defence_cleared_by", None)
                    it.pop("trade_defence_cleared_at", None)
                if k == "duty_rate_pct":
                    it["duty_source"] = "broker"
                if k == "origin_country":
                    it["origin_source"] = "broker" if internal else "customer"
                    it["flags"] = []
    for k in BROKER_FIELDS:
        if k not in body:
            continue
        if not internal:
            raise HTTPException(403, f"Only the customs team can change {k.replace('_', ' ')}")
        v = body[k]
        if k in ("goods_location", "submitter") and v is not None and not isinstance(v, dict):
            raise HTTPException(400, {"fi": f"{k}: anna rakenteinen tietue.", "en": f"{k}: supply a structured object."})
        if k == "origin_proofs":
            v = _coerce_item(k, v)
        if k == "office":
            code = re.sub(r"\s", "", str((v or {}).get("code") if isinstance(v, dict) else v or "")).upper()
            if code and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{6}", code):
                raise HTTPException(400, "Customs office code has 8 characters, e.g. FI002000")
            v = {"code": code, "name": (v or {}).get("name") if isinstance(v, dict) else None} if code else None
            if v:
                from services import customs_offices
                row = await customs_offices.office(code)
                if row and customs_offices.fresh(row):
                    v.update(name=row['name'], source='EU COL', source_url=row['source_url'],
                             fetched_at=row['fetched_at'], sha256=row['sha256'])
                elif isinstance(body[k], dict) and body[k].get('source') == 'offline_fallback':
                    v.update(source='offline_fallback', verified=False)
        if k == "representation_type":
            if v not in ("direct", "indirect"):
                raise HTTPException(400, "Representation is direct or indirect")
            decl["representative_status_code"] = "2" if v == "direct" else "3"
            decl["declarant"] = decl.get("importer") if v == "direct" else _forwarder_party()
        if k == "valuation":
            cur = dict(decl.get("valuation") or {})
            for vk in ("freight_to_border_cents", "insurance_cents", "post_border_cents"):
                if vk in (v or {}):
                    n = _num(v[vk])
                    if n is None or n < 0:
                        raise HTTPException(400, f"{vk.replace('_', ' ')} must be zero or more")
                    cur[vk] = int(n)
                    cur["freight_known"] = True
            if (v or {}).get("exchange_rate") is not None:
                r = _num(v["exchange_rate"])
                if not r or r <= 0:
                    raise HTTPException(400, "Exchange rate must be a positive number of currency units per EUR")
                cur["exchange_rate"] = {"currency": cur.get("invoice_currency"), "rate": r, "source": "Entered by broker "
                                        "(Tulli monthly rate)", "rate_date": now_iso()[:10]}
            cur.setdefault("notes", []).append(f"Edited by {user.get('name')} on {now_iso()[:10]}")
            v = cur
        if k == "delivery_terms":
            inc = str((v or {}).get("incoterm") or "").upper() or None
            if inc and inc not in INCOTERM_GROUP:
                raise HTTPException(400, f"{inc} is not an Incoterms 2020 rule")
            v = {"incoterm": inc, "place": (v or {}).get("place")}
            decl.setdefault("valuation", {})["incoterm"] = inc
            decl["valuation"]["incoterm_group"] = INCOTERM_GROUP.get(inc or "")
        if k == "transport":
            cur = dict(decl.get("transport") or {})
            td = dict(cur.get("transport_document") or {})
            if "transport_document_number" in (v or {}):
                td["number"] = (v.get("transport_document_number") or "").strip() or None
            if "transport_document_type" in (v or {}):
                tt = (v.get("transport_document_type") or "").strip().lower() or None
                if tt and tt not in TRANSPORT_DOC_TYPES:
                    raise HTTPException(400, "Transport document type must be one of " + ", ".join(sorted(TRANSPORT_DOC_TYPES)))
                td["type"] = tt
            cur["transport_document"] = td
            for tk in ("means_on_arrival", "container_numbers", "dispatch_country", "destination_country", "mode_at_border", "inland_mode", "identification_type", "nationality"):
                if tk in (v or {}):
                    cur[tk] = v[tk]
            v = cur
        changes.append({"field": k, "to": v})
        decl[k] = v
    if not changes:
        return decl
    decl.setdefault("edits", []).append({"at": now_iso(), "by": actor_ref(user), "changes": changes[:50]})
    return await save_and_recompute(decl, user, "edited", {"changes": changes[:20]})


async def reclassify_item(decl: dict, seq: int, user: dict) -> dict:
    from services import hs as hs_service

    it = next((i for i in decl.get("items") or [] if i["seq"] == seq), None)
    if not it:
        raise HTTPException(404, "Goods item not found")
    # classify with the other invoice lines as context (a "disc brake set" alone reads as a car part; next to a
    # derailleur and a bicycle chain it is a bicycle brake) and use the answer for this item only
    items = decl.get("items") or []
    results = await hs_service.classify_items(
        [{"description": i.get("description") or "", "origin_country": i.get("origin_country"), "hs_hint": i.get("hs_hint")}
         for i in items], ref={"type": "customs_declaration", "id": decl["id"]}, company_id=decl.get("company_id"))
    res = results[items.index(it)] if results and len(results) == len(items) else None
    for k in ("hs_code", "cn_code", "duty_rate_pct"):
        if k in (it.get("edited") or []):
            it["edited"].remove(k)
    apply_hs_result(it, res)
    return await save_and_recompute(decl, user, "reclassified", {"item": seq})


async def screen_seller(decl: dict) -> dict:
    # Compatibility entry point used by drafts, redrafts and existing API callers.
    from services.customs_screening import screen_parties
    return await screen_parties(decl, kind="import")


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------
@on("document.extracted")
async def _auto_draft(p):
    doc = p.get("document") or {}
    if doc.get("type") not in ("commercial_invoice", "packing_list") or not doc.get("shipment_id"):
        return
    shipment = await db.shipments.find_one({"id": doc["shipment_id"]}, {"_id": 0, "direction": 1, "origin": 1,
                                                                        "destination": 1, "id": 1})
    if not shipment or shipment.get("direction") != "import":
        return
    o = ((shipment.get("origin") or {}).get("country") or "").upper()
    if o and o in EU_COUNTRIES:
        return  # intra-EU: no import declaration
    has_invoice = doc.get("type") == "commercial_invoice" or await db.documents.find_one(
        {"shipment_id": doc["shipment_id"], "type": "commercial_invoice", "extraction.status": {"$in": ["done", "needs_review"]}})
    if not has_invoice:
        return
    decl = await draft_declaration_for_shipment(doc["shipment_id"], agent_actor("customs_drafter"))
    if decl and decl["status"] in ("draft", "needs_info"):
        scr = await screen_seller(decl)
        if scr:
            await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"screening": scr}})
            d2 = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
            await save_and_recompute(d2, agent_actor("customs_drafter"), "screened")


def list_projection() -> dict:
    return {"_id": 0, "history": 0, "edits": 0, "documents": 0, "submission.messages": 0}


def summarize(decl: dict) -> dict:
    """Row for lists: what the queue needs to decide, with SLA age."""
    t = decl.get("totals") or {}
    vals = decl.get("validations") or []
    created = decl.get("created_at")
    items = decl.get("items") or []
    low_conf = [i["seq"] for i in items if (i.get("hs_suggestion") or {}).get("confidence") is not None
                and (i["hs_suggestion"]["confidence"] or 0) < 0.7 and "hs_code" not in (i.get("edited") or [])]
    return {
        "id": decl["id"], "lrn": decl.get("lrn"), "mrn": decl.get("mrn"), "status": decl.get("status"),
        "shipment_id": decl.get("shipment_id"), "shipment_ref": decl.get("shipment_ref"), "company_id": decl.get("company_id"),
        "company_name": decl.get("company_name"), "type": decl.get("type"), "representation_type": decl.get("representation_type"),
        "items_count": len(items), "cn_codes": [i.get("cn_code") or i.get("hs_code") for i in items][:6],
        "low_confidence_items": low_conf, "customs_value_cents": t.get("customs_value_cents"),
        "duty_cents": t.get("duty_cents"), "vat_cents": t.get("vat_cents"), "vat_route": decl.get("vat_route"),
        "duty_complete": t.get("duty_complete") is not False and all(i.get("duty_cents") is not None for i in items),
        "vat_complete": t.get("duty_complete") is not False and t.get("vat_complete") is not False
                        and all(i.get("duty_cents") is not None and i.get("vat_cents") is not None for i in items),
        "blocking_count": len([v for v in vals if not v["ok"] and v["severity"] == "blocking"]),
        "blocking_customer": len([v for v in vals if not v["ok"] and v["severity"] == "blocking" and v["owner"] == "customer"]),
        "advisory_count": len([v for v in vals if not v["ok"] and v["severity"] == "advisory"]),
        "flags": sorted({f.get("kind") for i in items for f in i.get("flags") or []}),
        "created_at": created, "updated_at": decl.get("updated_at"), "approval_id": decl.get("approval_id"),
        "owner": (decl.get("assignee") or {}).get("name"), "assignee": decl.get("assignee"),
        "eta": decl.get("eta"), "demo": decl.get("demo"), "ai_drafted": bool(decl.get("ai")),
    }
