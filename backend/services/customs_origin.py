"""Rules of origin and tariff preference for an EU (Finnish) importer / exporter.

Everything here is decided by rules from official data, never guessed:

* Arrangement for an origin country: the preferential measures in the live EU TARIC consultation (services.taric:
  measure types "Tariff preference", "Preferential tariff quota", "Customs Union Duty", ... and their geographical
  area, e.g. GSP group 2020 / GSP+ 2027 / EBA 2005 or the partner country itself) plus DG TRADE's rules-of-origin
  dataset (Access2Markets ROO API: partner -> origin ruleset JP, VN, PEM2021, GSPB, ...), built into
  data/origin/arrangements.json by data/origin/build_arrangements.py (sources, dates and sha256 in that file).
* Proof of origin per arrangement and its DE 2/3 document code: arrangements.json (the agreement texts) and
  data/origin/codes.json (official TARIC / UCC code lists), plus the certificate codes in the TARIC conditions of the
  preference measure when TARIC carries them.
* Product-specific rules: the live Access2Markets ROSA endpoint used by trade.ec.europa.eu/access-to-markets
  (`/access-to-markets/rosa/productSpecificRules/lang/EN/product/<hs6>/origin/<iso>/destination/FI`).
* Tariff quota status: the live TARIC quota consultation (quota_list.jsp + quota_tariff_details.jsp).
* REX numbers: the EU public REX validation service; EORIs: services.kyc (EU EOS) / HMRC for GB.
* DE 4/17 preference code: UCC Annex B code list text (codes.json): first digit 1 erga omnes / 2 GSP / 3 other
  preference / 4 customs union; next two digits by the kind of measure (00, 10, 15, 20, 25, 40, ...).

A preference is claimed only when an arrangement exists, the preferential duty is lower than the third-country duty,
a valid proof is present and the product-specific rule is met (or covered by the exporter's proof). Otherwise the
code stays 100 and `reasons` says why, in Finnish and English.
"""
from __future__ import annotations

import html as _html
import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from core.db import db, register_index
from core.util import now_iso

log = logging.getLogger("customs_origin")

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "origin"
# The EC web filter refuses the default python-httpx User-Agent (HTTP 502 "Access Denied").
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Huolitsija-customs/1.0", "Accept": "*/*"}
TIMEOUT = httpx.Timeout(45.0, connect=15.0)

ROSA_PSR_URL = ("https://trade.ec.europa.eu/access-to-markets/rosa/productSpecificRules/lang/{lang}/product/{hs6}"
                "/origin/{origin}/destination/{destination}")
ROO_VARIANTS_URL = ("https://webgate.ec.europa.eu/roo/public/v1/rules/getVariantsFromDataset/country/EU/partner/{partner}"
                    "/direction/{direction}")
A2M_PAGE_URL = "https://trade.ec.europa.eu/access-to-markets/en/results?product={hs6}&origin={origin}&destination={destination}"
TARIC_BASE = "https://ec.europa.eu/taxation_customs/dds2/taric/"
QUOTA_LIST_URL = TARIC_BASE + "quota_list.jsp"
QUOTA_DETAILS_URL = TARIC_BASE + "quota_tariff_details.jsp"

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "EL", "HU", "IE", "IT", "LV", "LT", "LU",
      "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}

register_index("origin_cache", "key", unique=True)
register_index("origin_assessments", [("at", -1)])

CACHE_TTL = {"psr": 7 * 86400, "variants": 7 * 86400, "quota": 3600, "rex": 86400, "eori": 86400}


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def R(code: str, fi: str, en: str, **extra) -> dict:
    """One bilingual reason."""
    return {"code": code, "fi": fi, "en": en, **extra}


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _day(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _on(on) -> date:
    if on is not None and _day(on) is None:
        raise ValueError("date must be a valid ISO date")
    return _day(on) or datetime.now(timezone.utc).date()


def _text(fragment: str) -> str:
    """HTML -> plain text lines."""
    t = re.sub(r"<script.*?</script>", "", fragment or "", flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"<(tr|div|li|br|p|h\d|td|th)[^>]*>", "\n", t, flags=re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


@lru_cache(maxsize=4)
def _load(name: str) -> dict:
    p = DATA_DIR / name
    if not p.exists():
        log.warning("origin data file %s missing", p)
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def arrangements_data() -> dict:
    return _load("arrangements.json")


def codes_data() -> dict:
    return _load("codes.json")


async def _cached(kind: str, key: str, fetch, *, ttl: int | None = None):
    """Mongo cache for official-source responses: {key, kind, data, fetched_at, source_url}."""
    ttl = CACHE_TTL.get(kind, 86400) if ttl is None else ttl
    ck = f"{kind}:{key}"
    row = await db.origin_cache.find_one({"key": ck}, {"_id": 0})
    if row:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))).total_seconds()
        except (KeyError, ValueError):
            age = ttl + 1
        if age <= ttl:
            data = row["data"]
            if isinstance(data, dict):
                data = {**data, "cached": True}
            return data
    data = await fetch()
    if data is not None and not (isinstance(data, dict) and data.get("error")):
        await db.origin_cache.update_one({"key": ck}, {"$set": {"key": ck, "kind": kind, "data": data,
                                                                "fetched_at": now_iso()}}, upsert=True)
    return data


async def _http_get(url: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        return await c.get(url, params=params)


# ---------------------------------------------------------------------------------------------------------------
# DG TRADE rules-of-origin dataset: partner -> origin ruleset
# ---------------------------------------------------------------------------------------------------------------
async def roo_rulesets(partner: str, direction: str = "import") -> list[str]:
    """Origin rulesets DG TRADE applies to trade between the EU and `partner` (Access2Markets ROO API),
    e.g. JP -> ["JP"], CH -> ["PEM2021"], IN -> ["GSPB"], BD -> ["GSPA"], CN -> []. Falls back to the copy in
    arrangements.json when the API is unreachable."""
    partner = (partner or "").upper()
    url = ROO_VARIANTS_URL.format(partner=partner, direction=direction)

    async def fetch():
        try:
            r = await _http_get(url)
            r.raise_for_status()
            return {"variants": r.json(), "source_url": url, "fetched_at": now_iso()}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}", "source_url": url}

    d = await _cached("variants", f"{partner}:{direction}", fetch)
    if d.get("error"):
        return list((arrangements_data().get("partners") or {}).get(partner) or [])
    return [v.get("variantDescription") for v in d.get("variants") or [] if v.get("variantDescription")]


# ---------------------------------------------------------------------------------------------------------------
# Product-specific rules (Access2Markets ROSA)
# ---------------------------------------------------------------------------------------------------------------
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|per\s*cent)", re.I)


def _criteria(rtype: str, text: str) -> list[dict]:
    """Structure of one alternative of a product-specific rule, read from the ROSA component type tokens
    ([CTH], [MAXNOM], [RVC], [WO], [AH EXCEPT], ...) and the rule text. Every criterion in the list must be met
    (they are joined by AND / RESTRICTION in ROSA); unknown kinds are kept as 'process' (needs evidence)."""
    tokens = [t.strip() for t in re.findall(r"\[([^\]]+)\]", rtype or "")]
    low = " ".join((text or "").split()).lower()
    pcts = [float(p.replace(",", ".")) for p in _PCT.findall(low)]
    out: list[dict] = []

    def basis():
        if "fob" in low:
            return "FOB"
        if "transaction value or ex-works" in low:
            return "EXW_OR_TV"
        return "EXW"

    for tok in tokens or [""]:
        t = tok.upper()
        if t == "WO" or (not tokens and "wholly obtained" in low):
            out.append({"kind": "wholly_obtained", "scope": "materials" if "materials" in low else "product"})
        elif t in ("CTH", "AH EXCEPT") or (not tokens and ("cth" in low or "except that of the product" in low)):
            out.append({"kind": "cth"})
        elif t == "CTH WHETHER PURE":
            out.append({"kind": "within_heading"})
        elif t == "CTSH":
            out.append({"kind": "ctsh"})
        elif t == "CC":
            out.append({"kind": "cc"})
        elif t == "MAXNOM":
            out.append({"kind": "maxnom", "pct": pcts[0] if pcts else None, "basis": basis(),
                        "all_materials": "non-originating" not in low and "value of all the materials" in low,
                        "same_heading_only": "of this heading" in low})
        elif t == "RVC":
            out.append({"kind": "rvc", "pct": pcts[-1] if pcts else None, "basis": basis()})
        elif t:
            out.append({"kind": "process", "token": tok})
    if not out:
        out.append({"kind": "process", "token": None})
    # "CTH; MaxNOM 50 % (EXW); RVC 55 % (FOB)" in one component would be alternatives: ROSA already splits those.
    return out


def parse_psr(raw: dict) -> list[dict]:
    """ROSA productSpecificRules JSON -> [{heading, description, alternatives[{text, type, criteria[]}]}]."""
    out = []
    for blk in raw.get("rulesDecomposed") or []:
        alts = []
        for c in blk.get("components") or []:
            txt = " ".join((c.get("text") or "").split())
            alts.append({"text": txt.rstrip(" ;"), "type": c.get("type"), "criteria": _criteria(c.get("type") or "", txt)})
        heading = re.sub(r"<footnote-ref[^>]*>.*?</footnote-ref>", "", blk.get("heading") or "").strip()
        out.append({"heading": heading, "description": " ".join((blk.get("description") or "").split()),
                    "alternatives": alts})
    return out


async def product_specific_rule(hs: str, origin: str, *, destination: str = "FI", direction: str = "IMPORT",
                                lang: str = "EN") -> dict:
    """The product-specific rule of origin for an HS code in trade with `origin` (live Access2Markets ROSA).
    For an export from Finland call with origin="FI", destination=<partner>, direction="EXPORT"."""
    hs6 = (_digits(hs) + "000000")[:6]
    origin, destination = (origin or "").upper(), (destination or "FI").upper()
    url = ROSA_PSR_URL.format(lang=lang.upper(), hs6=hs6, origin=origin, destination=destination)
    params = {"direction": direction.upper(), "variant": 1}

    async def fetch():
        try:
            r = await _http_get(url, params)
            r.raise_for_status()
            return {"raw": r.json(), "fetched_at": now_iso()}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    d = await _cached("psr", f"{hs6}:{origin}:{destination}:{direction}:{lang}", fetch)
    src = f"{url}?direction={params['direction']}&variant=1"
    if d.get("error"):
        return {"found": False, "error": d["error"], "source_url": src, "hs6": hs6}
    raw = d.get("raw") or {}
    blocks = parse_psr(raw)
    footnotes = {f.get("code"): " ".join((f.get("content") or "").split()) for f in raw.get("footnotes") or []}
    comments = [" ".join(_text(c.get("comment") or "").split()) for c in raw.get("comments") or []]
    return {
        "found": bool(blocks), "hs6": hs6, "blocks": blocks, "footnotes": footnotes, "comments": comments,
        "official_journal_url": (raw.get("officialJournalInfo") or {}).get("documentUrl"),
        "source": "Access2Markets ROSA (DG TRADE)", "source_url": src,
        "page_url": A2M_PAGE_URL.format(hs6=hs6, origin=origin, destination=destination),
        "fetched_at": d.get("fetched_at"), "cached": bool(d.get("cached")),
    }


def _bom_from(documents: list[dict], item: dict) -> dict | None:
    for d in documents or []:
        if d.get("type") in ("bill_of_materials", "bom"):
            return d
    return item.get("bom") if isinstance(item.get("bom"), dict) else None


def _evaluate_criterion(cr: dict, product_hs: str, bom: dict) -> tuple[bool | None, str]:
    mats = bom.get("materials") or []
    if not mats and bom.get("wholly_obtained") is not True:
        return None, "materials are missing"
    for m in mats:
        value = _num(m.get("value_cents"))
        if value is not None and (not math.isfinite(value) or value < 0):
            return None, "material values must be finite and non-negative"
    nonorig = [m for m in mats if not m.get("originating")]
    exw = _num(bom.get("ex_works_cents") or bom.get("exw_cents"))
    fob = _num(bom.get("fob_cents"))
    tv = _num(bom.get("transaction_value_cents"))
    k = cr["kind"]
    if k == "wholly_obtained":
        if bom.get("wholly_obtained") is True:
            return True, "declared wholly obtained"
        if nonorig:
            return False, f"{len(nonorig)} non-originating material(s)"
        return None, "wholly obtained status not stated"
    if k in ("cth", "ctsh", "cc"):
        n = {"cth": 4, "ctsh": 6, "cc": 2}[k]
        if any(len(_digits(m.get("hs"))) < n for m in nonorig):
            return None, "a non-originating material has no HS code"
        same = [m for m in nonorig if _digits(m.get("hs"))[:n] == product_hs[:n]]
        return (not same), (f"non-originating material(s) in the same {'heading' if n == 4 else 'subheading' if n == 6 else 'chapter'}"
                            f" {product_hs[:n]}" if same else "all non-originating materials change classification")
    if k == "within_heading":
        return None, "within-heading rule requires process evidence"
    if k == "maxnom":
        pct = cr.get("pct")
        base = exw if cr.get("basis") in ("EXW", "EXW_OR_TV") else None
        if base is None and cr.get("basis") == "EXW_OR_TV":
            base = tv
        if cr.get("basis") == "FOB":
            base = fob
        if pct is None or not base:
            return None, "ex-works price (or the price basis of the rule) missing"
        pool = nonorig
        if cr.get("same_heading_only"):
            pool = [m for m in nonorig if _digits(m.get("hs"))[:4] == product_hs[:4]]
        if cr.get("all_materials"):
            pool = mats
        if any(_num(m.get("value_cents")) is None for m in pool):
            return None, "a material has no value"
        share = sum(_num(m.get("value_cents")) for m in pool) / base * 100
        if share <= pct:
            return True, f"materials {share:.1f} % of the price (max {pct:g} %)"
        if cr.get("all_materials"):
            only_nonorig = sum(_num(m.get("value_cents")) or 0 for m in nonorig) / base * 100
            if only_nonorig <= pct:
                return None, (f"all materials {share:.1f} % > {pct:g} %; non-originating only {only_nonorig:.1f} %: "
                              "the rule text says 'all the materials', a broker decides")
        return False, f"materials {share:.1f} % of the price (max {pct:g} %)"
    if k == "rvc":
        pct = cr.get("pct")
        if pct is None or not fob:
            return None, "FOB value missing"
        if any(_num(m.get("value_cents")) is None for m in nonorig):
            return None, "a non-originating material has no value"
        vnm = sum(_num(m.get("value_cents")) for m in nonorig)
        rvc = (fob - vnm) / fob * 100
        return rvc >= pct, f"RVC {rvc:.1f} % (min {pct:g} %)"
    return None, "the rule needs evidence of the production process"


def evaluate_psr(psr: dict, product_hs: str, bom: dict | None) -> dict:
    """met True if any alternative is met by the bill of materials, False if every alternative fails, else None."""
    if not psr.get("found"):
        return {"met": None, "detail": []}
    if not bom or bom.get("complete") is not True:
        return {"met": None, "detail": [{"why": "a complete bill of materials is required"}]}
    if len(psr.get("blocks") or []) > 1:
        return {"met": None, "detail": [{"why": "multiple product-specific descriptions require selection of the applicable rule"}]}
    detail, results = [], []
    for blk in psr["blocks"]:
        for alt in blk["alternatives"]:
            res = [_evaluate_criterion(cr, _digits(product_hs), bom) for cr in alt["criteria"]]
            if any(r[0] is False for r in res):
                ok = False
            elif all(r[0] is True for r in res):
                ok = True
            else:
                ok = None
            results.append(ok)
            detail.append({"alternative": alt["text"], "met": ok, "why": "; ".join(r[1] for r in res)})
    met = True if any(r is True for r in results) else (False if results and all(r is False for r in results) else None)
    return {"met": met, "detail": detail}


# ---------------------------------------------------------------------------------------------------------------
# TARIC tariff quota status
# ---------------------------------------------------------------------------------------------------------------
def parse_quota_details(page: str) -> dict:
    """quota_tariff_details.jsp -> {initial, amount, balance, unit, exhaustion_date, critical, blocking, suspension,
    closed, last_update, origin, validity}."""
    lines = _text(page).splitlines()

    def after(label: str, n: int = 1) -> list[str]:
        for i, ln in enumerate(lines):
            if ln.strip().lower() == label.lower():
                return lines[i + 1:i + 1 + n]
        return []

    def amount(label):
        v = after(label, 2)
        if not v:
            return None, None
        num = _num(v[0]) if re.match(r"^-?[\d .,]+$", v[0]) else None
        unit = v[1] if num is not None and len(v) > 1 and not re.match(r"^[A-Z][a-z]+ [a-z]", v[1] or "") else None
        return num, unit

    vp = after("Validity period", 3)
    initial, unit = amount("Initial amount")
    amt, _ = amount("Amount")
    bal, bunit = amount("Balance")
    exh = after("Exhaustion date")
    crit = after("Critical")
    closed = after("Closed for drawing requests")
    upd = re.search(r"Last TARIC update:\s*([\d-]+)", "\n".join(lines))
    labels = {"Blocking period", "Suspension period", "Allocated percentage at the last allocation", "Associated TARIC code",
              "Last import date", "Last allocation date", "Transferred Amount"}
    blocking = after("Blocking period")
    suspension = after("Suspension period")
    codes = []
    for i, ln in enumerate(lines):
        if ln == "Associated TARIC code":
            for x in lines[i + 1:]:
                if re.match(r"^\d{4} \d{2} \d{2} \d{2}$", x):
                    codes.append(x.replace(" ", ""))
                else:
                    break
    return {
        "validity": {"start": vp[0] if vp else None, "end": vp[2] if len(vp) > 2 else None},
        "origin": (after("Origin") or [None])[0],
        "initial_amount": initial, "amount": amt, "balance": bal, "unit": bunit or unit,
        "exhaustion_date": exh[0] if exh and re.match(r"\d{2}-\d{2}-\d{4}", exh[0]) else None,
        "critical": (crit[0] == "Yes") if crit else None,
        "closed": (closed[0] == "Yes") if closed else None,
        "blocking_period": blocking[0] if blocking and blocking[0] not in labels else None,
        "suspension_period": suspension[0] if suspension and suspension[0] not in labels else None,
        "taric_codes": codes, "last_taric_update": upd.group(1) if upd else None,
    }


async def quota_status(order_number: str, on=None) -> dict:
    """Live status of a TARIC tariff quota on a date: open | critical | exhausted | blocked | suspended | not_found."""
    on_d = _on(on)
    num = _digits(order_number).zfill(6)

    async def fetch():
        try:
            r = await _http_get(QUOTA_LIST_URL, {"Lang": "en", "Origin": "", "Code": num, "Year": on_d.year, "Status": "",
                                                 "Critical": "", "Expand": "true", "Offset": "0"})
            r.raise_for_status()
            starts = re.findall(r"quota_tariff_details\.jsp\?Lang=en&(?:amp;)?StartDate=(\d{4}-\d{2}-\d{2})&(?:amp;)?Code=(\d+)",
                                r.text)
            periods = []
            for start, code in starts:
                d = await _http_get(QUOTA_DETAILS_URL, {"Lang": "en", "StartDate": start, "Code": code})
                d.raise_for_status()
                periods.append({"start_date": start, **parse_quota_details(d.text),
                                "source_url": f"{QUOTA_DETAILS_URL}?Lang=en&StartDate={start}&Code={code}"})
            return {"periods": periods, "fetched_at": now_iso()}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    d = await _cached("quota", f"{num}:{on_d.year}", fetch)
    base = {"order_number": num, "source": "EU TARIC quota consultation",
            "source_url": f"{QUOTA_LIST_URL}?Lang=en&Code={num}&Year={on_d.year}"}
    if d.get("error"):
        return {**base, "status": "unknown", "error": d["error"]}
    period = None
    for p in d.get("periods") or []:
        s, e = _day((p.get("validity") or {}).get("start")), _day((p.get("validity") or {}).get("end"))
        if s and e and s <= on_d <= e:
            period = p
    if not period:
        return {**base, "status": "not_found"}
    if period.get("suspension_period"):
        status = "suspended"
    elif period.get("blocking_period") or period.get("closed"):
        status = "blocked"
    elif period.get("exhaustion_date") or (period.get("balance") is not None and period["balance"] <= 0):
        status = "exhausted"
    elif period.get("critical"):
        status = "critical"
    else:
        status = "open"
    return {**base, "status": status, **{k: period.get(k) for k in (
        "balance", "unit", "initial_amount", "exhaustion_date", "critical", "validity", "last_taric_update")},
            "details_url": period.get("source_url"), "fetched_at": d.get("fetched_at")}


# ---------------------------------------------------------------------------------------------------------------
# DE 4/17 preference code (UCC Annex B code list, data/origin/codes.json)
# ---------------------------------------------------------------------------------------------------------------
# First digit by the kind of arrangement (official texts in codes.json["preference_codes"]["first_digit"]).
FIRST_DIGIT = {"erga_omnes": "1", "gsp": "2", "other_preference": "3", "customs_union": "4"}


def _suffix_texts() -> dict:
    return ((codes_data().get("preference_codes") or {}).get("suffix") or {})


def preference_suffix(*, quota: bool = False, end_use: bool = False, suspension: bool = False,
                      special_nature: bool = False) -> str:
    """The two-digit component whose official text names exactly these features (e.g. "Tariff quota" -> 20,
    "Tariff quota with specific end-use" -> 25 ...). '00' = "None of the following cases"."""
    want = {"quota": quota, "end-use": end_use, "suspension": suspension, "specific nature": special_nature}
    if not any(want.values()):
        return "00"
    best = None
    for code, t in _suffix_texts().items():
        en = ((t or {}).get("en") or "").lower()
        has = {"quota": "quota" in en, "end-use": "end-use" in en or "end use" in en,
               "suspension": "suspension" in en, "specific nature": "specific nature" in en}
        if has == want and not any(w in en for w in ("airworthiness", "outward processing")):
            if best is None or code < best:
                best = code
    if best:
        return best
    raise ValueError(f"no official DE 4/17 component for {want}")


def preference_label(code: str) -> dict:
    pc = codes_data().get("preference_codes") or {}
    first = (pc.get("first_digit") or {}).get(code[:1]) or {}
    suf = (pc.get("suffix") or {}).get(code[1:]) or {}
    return {"fi": " · ".join(x for x in (first.get("fi"), suf.get("fi")) if x) or None,
            "en": " · ".join(x for x in (first.get("en"), suf.get("en")) if x) or None}


def preference_code(first: str, suffix: str) -> str:
    code = f"{first}{suffix}"
    allowed = (codes_data().get("preference_codes") or {}).get("allowed")
    if allowed and code not in allowed:
        raise ValueError(f"DE 4/17 code {code} is not in the official list")
    return code


# ---------------------------------------------------------------------------------------------------------------
# TARIC measures -> preference candidates
# ---------------------------------------------------------------------------------------------------------------
PREF_TYPE_CODES = {"141", "142", "143", "145", "146"}
CU_TYPE_CODES = {"106", "147"}
THIRD_COUNTRY_TYPE_CODES = {"103"}


def _mtype(m: dict) -> str:
    return str(m.get("type_code") or "").strip()


def _mtext(m: dict) -> str:
    return (m.get("type") or "").lower()


def _is_pref(m: dict) -> bool:
    return _mtype(m) in PREF_TYPE_CODES or ("preferen" in _mtext(m) and "non preferential" not in _mtext(m)
                                             and "non-preferential" not in _mtext(m))


def _is_cu(m: dict) -> bool:
    return _mtype(m) in CU_TYPE_CODES or "customs union" in _mtext(m)


def _is_third(m: dict) -> bool:
    return _mtype(m) in THIRD_COUNTRY_TYPE_CODES or _mtext(m) == "third country duty"


def _features(m: dict) -> dict:
    t = _mtext(m)
    return {"quota": "quota" in t or bool(m.get("order_number")), "end_use": "end-use" in t or "end use" in t,
            "suspension": "suspension" in t}


def measure_duty(m: dict | None, item: dict) -> dict:
    """Duty of one TARIC measure for an item: ad valorem on the customs value, specific per 100 kg / tonne on the net
    mass. pct None = not ad valorem; cents None = not computable from the item data."""
    if not m:
        return {"expression": None, "pct": None, "cents": None}
    from services import taric
    d = m.get("duty") or taric.parse_duty(m.get("duty_expression") or "")
    expr = m.get("duty_expression")
    pct = d.get("pct") if d.get("kind") == "ad_valorem" else None
    value = item.get("customs_value_cents") if item.get("customs_value_cents") is not None else item.get("value_cents")
    calc = taric.compute_duty(d, value, item.get("net_kg"), item.get("quantity"), item.get("unit_qty"), qty_unit=item.get("unit"))
    cents = calc.get("duty_cents")
    return {"expression": expr, "pct": float(pct) if pct is not None else None, "cents": cents,
            "measure_type": m.get("type"), "type_code": _mtype(m), "geo_code": m.get("geo_code"),
            "geo_name": m.get("geo_name"), "regulation": m.get("regulation"), "order_number": m.get("order_number")}


def _lower(pref: dict, third: dict) -> bool | None:
    if pref.get("pct") is not None and third.get("pct") is not None and pref.get("cents") is None:
        return pref["pct"] < third["pct"]
    if pref.get("cents") is not None and third.get("cents") is not None:
        return pref["cents"] < third["cents"]
    if pref.get("pct") is not None and third.get("pct") is not None:
        return pref["pct"] < third["pct"]
    return None


# ---------------------------------------------------------------------------------------------------------------
# Arrangements
# ---------------------------------------------------------------------------------------------------------------
def arrangement_record(code: str | None) -> dict | None:
    if not code:
        return None
    a = (arrangements_data().get("arrangements") or {}).get(code)
    if not a:
        return None
    return {"code": code, **a}


def _public_arrangement(a: dict | None, *, type_override: str | None = None) -> dict | None:
    if not a:
        return None
    return {"code": a["code"], "name_fi": a.get("name_fi"), "name_en": a.get("name_en"),
            "type": type_override or a.get("type"), "legal_basis": a.get("legal_basis"),
            "source_url": a.get("source_url")}


def _cu_arrangement(origin: str) -> dict | None:
    for code, a in (arrangements_data().get("arrangements") or {}).items():
        if a.get("type") == "customs_union" and origin in (a.get("partners") or []):
            return {"code": code, **a}
    return None


async def arrangement_for(origin: str, measure: dict | None = None) -> tuple[dict | None, str | None]:
    """(arrangement, kind) for an origin country and (optionally) the TARIC measure granting the preference.
    kind: gsp | gsp_plus | eba | customs_union | fta | ... (type in arrangements.json / TARIC group)."""
    origin = (origin or "").upper()
    data = arrangements_data()
    if measure is not None and _is_cu(measure):
        a = _cu_arrangement(origin)
        return a, "customs_union"
    geo = str((measure or {}).get("geo_code") or "")
    grp = (data.get("taric_groups") or {}).get(geo)
    if grp and grp.get("arrangement"):
        return arrangement_record(grp["arrangement"]), grp.get("type")
    sets = await roo_rulesets(origin)
    for code in sets:
        a = arrangement_record(code)
        if a:
            return a, a.get("type")
    if measure is None:
        a = _cu_arrangement(origin)
        if a:
            return a, "customs_union"
    return None, None


# ---------------------------------------------------------------------------------------------------------------
# Proofs of origin
# ---------------------------------------------------------------------------------------------------------------
STATEMENT_KINDS = {"statement_on_origin", "origin_declaration", "invoice_declaration", "statement_on_origin_rex",
                   "origin_declaration_eur_med"}
PROOF_TYPES = STATEMENT_KINDS | {"eur1", "eur_med", "importers_knowledge", "atr"}
REX_RE = re.compile(r"\b([A-Z]{2}REX[A-Z0-9]{1,30})\b")


def parse_statement(text: str) -> dict:
    """Read an origin statement / origin declaration printed on an invoice (official wording, e.g. 'The exporter of
    the products covered by this document (Exporter Reference No ...) declares that, except where otherwise clearly
    indicated, these products are of ... preferential origin.')."""
    t = " ".join((text or "").split())
    low = t.lower()
    out: dict = {"text": t, "is_statement": False}
    if not t:
        return out
    out["is_statement"] = bool(re.search(r"exporter of the products covered by this document", low)
                               and "preferential origin" in low)
    m = re.search(r"\((?:exporter reference no\.?|exporter'?s? reference(?: number)?|customs or competent "
                  r"governmental authority authori[sz]ation no\.?|customs authori[sz]ation no\.?|"
                  r"authori[sz]ation no\.?|rex(?: number| no\.?)?)\s*[:.]?\s*([^)]*)\)", t, re.I)
    ref = (m.group(1).strip(" .:…") if m else "") or None
    rex = REX_RE.search(t.replace(" ", "")) if not (ref and REX_RE.search(ref)) else REX_RE.search(ref)
    out["exporter_ref"] = rex.group(1) if rex else ref
    o = re.search(r"products are of\s+(.+?)\s+preferential origin", t, re.I)
    out["origin_text"] = o.group(1).strip(" .") if o else None
    out["gsp"] = "generalised system of preferences" in low or "generalized system of preferences" in low
    c = re.search(r"origin criterion met is\s+([A-Z0-9\"“”'() ]{1,20}?)(?:[.,;]|$)", t)
    out["origin_criterion"] = c.group(1).strip(" \"“”") if c else None
    return out


def _country_iso(name: str | None) -> str | None:
    if not name:
        return None
    s = name.strip().strip(".").lower()
    if s in ("eu", "the eu", "european union", "the european union", "euroopan unioni"):
        return "EU"
    if len(s) == 2:
        return s.upper()
    names = arrangements_data().get("country_names") or {}
    for lang in ("en", "fi"):
        for iso, nm in (names.get(lang) or {}).items():
            if (nm or "").strip().lower() in (s, "the " + s) or s == ("the " + (nm or "").strip().lower()):
                return iso
    return None


def normalise_documents(documents: list[dict] | None) -> list[dict]:
    """Accepts plain proof dicts ({type, ref, date, exporter_ref, origin_country, text, value_eur_cents}) and documents
    rows from the `documents` collection (commercial_invoice with origin_declaration_text, eur1, certificate_of_origin)."""
    out = []
    for d in documents or []:
        if not isinstance(d, dict):
            continue
        typ = (d.get("type") or "").lower()
        fields = ((d.get("extraction") or {}).get("fields") or {})

        def fv(k):
            v = fields.get(k)
            return v.get("value") if isinstance(v, dict) else v

        if typ == "commercial_invoice" and fv("origin_declaration_text"):
            st = parse_statement(fv("origin_declaration_text"))
            if st["is_statement"]:
                out.append({"type": "statement_on_origin", "ref": fv("invoice_number"), "date": fv("invoice_date"),
                            "exporter_ref": st.get("exporter_ref"), "origin_country": _country_iso(st.get("origin_text")),
                            "origin_text": st.get("origin_text"), "gsp": st.get("gsp"),
                            "origin_criterion": st.get("origin_criterion"), "text": st["text"],
                            "document_id": d.get("id"), "source": "invoice"})
            continue
        if typ in ("eur1", "eur_med", "eur-med", "eur.1"):
            out.append({"type": "eur_med" if "med" in typ else "eur1",
                        "ref": d.get("ref") or fv("certificate_number") or d.get("number"),
                        "date": d.get("date") or fv("issue_date"),
                        "origin_country": (d.get("origin_country") or fv("country_of_origin") or "").upper() or None,
                        "issuer": d.get("issuer") or fv("issuing_body"), "document_id": d.get("id"),
                        "source": d.get("source") or "document", "verified": d.get("verified")})
            continue
        if typ == "certificate_of_origin":
            out.append({"type": "certificate_of_origin", "ref": fv("certificate_number") or d.get("ref"),
                        "date": fv("issue_date") or d.get("date"),
                        "origin_country": (fv("country_of_origin") or d.get("origin_country") or "").upper() or None,
                        "document_id": d.get("id"), "source": "document"})
            continue
        if typ in PROOF_TYPES or typ in ("supplier_declaration", "bill_of_materials", "bom"):
            row = dict(d)
            row["type"] = typ
            if typ in STATEMENT_KINDS and row.get("text") and not row.get("exporter_ref"):
                st = parse_statement(row["text"])
                row.setdefault("exporter_ref", st.get("exporter_ref"))
                row.setdefault("origin_country", _country_iso(st.get("origin_text")))
            out.append(row)
    return out


def document_code_for(proof_type: str, arrangement_code: str | None, measure: dict | None = None) -> dict | None:
    """DE 2/3 code for a proof: first a certificate named in the TARIC conditions of the preference measure, then the
    official code list (codes.json)."""
    codes = codes_data().get("document_codes") or {}
    want = {"statement_on_origin": STATEMENT_KINDS}.get(proof_type, {proof_type})
    if proof_type in STATEMENT_KINDS:
        want = STATEMENT_KINDS
    for c in (measure or {}).get("conditions") or []:
        cert = (c.get("certificate") or "").replace(" ", "")
        meta = codes.get(cert)
        if meta and meta.get("proof_type") in want:
            return {"code": cert, "en": meta.get("en"), "fi": meta.get("fi"), "source": "TARIC measure condition"}
    best = None
    for code, meta in codes.items():
        if meta.get("proof_type") not in want:
            continue
        arrs = meta.get("arrangements") or []
        if arrangement_code and arrangement_code in arrs:
            return {"code": code, "en": meta.get("en"), "fi": meta.get("fi"), "source": meta.get("source")}
        if not arrs and best is None:
            best = {"code": code, "en": meta.get("en"), "fi": meta.get("fi"), "source": meta.get("source")}
    return best


def _accepted(arr: dict | None) -> list[dict]:
    return list((arr or {}).get("proofs") or [])


def _accepts(arr: dict | None, ptype: str) -> dict | None:
    for p in _accepted(arr):
        pt = p.get("type")
        if pt == ptype or (ptype in STATEMENT_KINDS and pt in STATEMENT_KINDS):
            return p
    return None


def _proof_names(arr: dict | None, arr_code: str | None, measure: dict | None) -> tuple[str, str]:
    fi, en = [], []
    for p in _accepted(arr):
        dc = document_code_for(p.get("type"), arr_code, measure)
        code = f" ({dc['code']})" if dc else ""
        lim = f" ≤ {int(p['max_value_eur'])} EUR" if p.get("max_value_eur") else ""
        en.append(f"{PROOF_LABEL.get(p.get('type'), {}).get('en', p.get('type'))}{lim}{code}")
        fi.append(f"{PROOF_LABEL.get(p.get('type'), {}).get('fi', p.get('type'))}{lim}{code}")
    return ", ".join(dict.fromkeys(fi)) or "säännöt tarkastamatta", ", ".join(dict.fromkeys(en)) or "proof rules not reviewed"


PROOF_LABEL = {
    "statement_on_origin": {"fi": "alkuperäilmoitus", "en": "statement on origin"},
    "statement_on_origin_rex": {"fi": "REX-rekisteröidyn viejän alkuperäilmoitus", "en": "statement on origin (REX)"},
    "origin_declaration": {"fi": "alkuperäilmoitus (origin declaration)", "en": "origin declaration"},
    "origin_declaration_eur_med": {"fi": "EUR-MED-alkuperäilmoitus", "en": "origin declaration EUR-MED"},
    "invoice_declaration": {"fi": "kauppalaskuilmoitus", "en": "invoice declaration"},
    "eur1": {"fi": "EUR.1-tavaratodistus", "en": "EUR.1 movement certificate"},
    "eur_med": {"fi": "EUR-MED-tavaratodistus", "en": "EUR-MED movement certificate"},
    "importers_knowledge": {"fi": "tuojan tietoihin perustuva etuuskohtelu", "en": "importer's knowledge"},
    "atr": {"fi": "A.TR-tavaratodistus", "en": "A.TR movement certificate"},
}


async def evaluate_proof(arr, arr_code, origin, docs, on, *, consignment_value_cents, measure, customs_union=False):
    """Try every supplied proof; one expired certificate must not hide a valid statement."""
    candidates = [d for d in docs if d.get("type") in PROOF_TYPES]
    others = [d for d in docs if d.get("type") not in PROOF_TYPES]
    results = []
    for d in candidates or [None]:
        result = await _evaluate_proof_candidate(arr, arr_code, origin, others + ([d] if d else []), on,
            consignment_value_cents=consignment_value_cents, measure=measure, customs_union=customs_union)
        if result.get("valid") is True:
            return result
        results.append(result)
    return results[0]


async def _evaluate_proof_candidate(arr: dict | None, arr_code: str | None, origin: str, docs: list[dict], on: date, *,
                         consignment_value_cents: int | None, measure: dict | None, customs_union: bool = False) -> dict:
    """Pick and check the proof for the claim. valid True only when every rule of the arrangement holds."""
    res = {"type": None, "ref": None, "valid": None, "document_code": None, "exporter_ref": None, "exporter_check": None,
           "date": None, "valid_until": None, "reasons": []}
    wanted = [d for d in docs if d.get("type") in PROOF_TYPES]
    if customs_union:
        wanted = [d for d in wanted if d["type"] == "atr"] or wanted
    noncert = [d for d in docs if d.get("type") == "certificate_of_origin"]
    if not wanted:
        res["valid"] = False
        fi, en = _proof_names(arr, arr_code, measure)
        if noncert:
            res["reasons"].append(R("non_preferential_certificate",
                                    "Liitteenä on vain (ei-etuuskohteluun oikeuttava) alkuperätodistus; se ei oikeuta etuuskohteluun. "
                                    f"Tarvitaan: {fi}.",
                                    "Only a (non-preferential) certificate of origin is attached; it gives no preference. "
                                    f"Needed: {en}."))
        else:
            res["reasons"].append(R("proof_missing", f"Alkuperäselvitys puuttuu. Hyväksytyt: {fi}.",
                                    f"Proof of origin missing. Accepted: {en}."))
        return res
    order = {"atr": 0, "eur1": 1, "eur_med": 1, "importers_knowledge": 3}
    d = sorted(wanted, key=lambda x: order.get(x["type"], 2))[0]
    rule = _accepts(arr, d["type"])
    res.update({"type": d["type"], "ref": d.get("ref"), "exporter_ref": d.get("exporter_ref"),
                "date": d.get("date"), "document_id": d.get("document_id")})
    dc = document_code_for(rule.get("type") if rule else d["type"], arr_code, measure)
    if (arr or {}).get("type") in ("gsp", "gsp_plus", "eba") and d["type"] in STATEMENT_KINDS:
        registered = bool(REX_RE.fullmatch((d.get("exporter_ref") or "").replace(" ", "").upper()))
        c = "U165" if registered and consignment_value_cents is not None and consignment_value_cents > 600000 else "U164" if registered else "U166"
        meta = (codes_data().get("document_codes") or {}).get(c)
        dc = {"code": c, **meta} if meta else None
    elif d["type"] in STATEMENT_KINDS and (arr or {}).get("code") in ("KR", "VN"):
        c = "U162" if consignment_value_cents is not None and 0 <= consignment_value_cents <= 600000 else "N864"
        meta = (codes_data().get("document_codes") or {}).get(c)
        dc = {"code": c, **meta} if meta else None
    res["document_code"] = dc["code"] if dc else None
    res["document_code_text"] = {"fi": (dc or {}).get("fi"), "en": (dc or {}).get("en")} if dc else None
    ok = True
    if not rule:
        fi, en = _proof_names(arr, arr_code, measure)
        res["reasons"].append(R("proof_type_not_accepted",
                                f"{PROOF_LABEL.get(d['type'], {}).get('fi', d['type'])} ei käy tässä järjestelyssä. Hyväksytyt: {fi}.",
                                f"{PROOF_LABEL.get(d['type'], {}).get('en', d['type'])} is not accepted under this arrangement. "
                                f"Accepted: {en}."))
        res["valid"] = False
        return res
    # origin stated on the proof must be the claimed origin
    oc = (d.get("origin_country") or "").upper() or None
    if not customs_union and oc != origin and not (oc == "EU" and origin in EU):
        res["reasons"].append(R("proof_wrong_origin", f"Alkuperäselvityksen alkuperämaa {oc} ei ole ilmoitettu alkuperä {origin}.",
                                f"The proof states origin {oc}, not the declared origin {origin}."))
        ok = False
    if d["type"] in STATEMENT_KINDS and (arr or {}).get("type") in ("gsp", "gsp_plus", "eba") and d.get("text") \
            and d.get("gsp") is False:
        res["reasons"].append(R("statement_wrong_scheme", "Alkuperäilmoituksen sanamuoto ei ole GSP-järjestelmän (UCC IA liite 22-07).",
                                "The statement wording is not the GSP one (UCC IA Annex 22-07)."))
        ok = False
    if d["type"] in STATEMENT_KINDS:
        if d.get("verified") is not True:
            ok = False
            res["reasons"].append(R("statement_unverified", "Alkuperäilmoituksen sanamuotoa ja kattavuutta ei ole tarkastettu.",
                                    "The statement wording and goods coverage have not been checked."))
        if not d.get("text") or not d.get("ref"):
            ok = False
            res["reasons"].append(R("statement_incomplete", "Alkuperäilmoituksen teksti tai asiakirjaviite puuttuu.",
                                    "The statement text or document reference is missing."))
    if d["type"] in ("eur1", "eur_med", "atr") and d.get("verified") is not True:
        ok = False
        res["reasons"].append(R("certificate_unverified", "Tavaratodistusta ei ole tarkastettu.",
                                "The movement certificate has not been checked."))
    if not dc:
        ok = False
        res["reasons"].append(R("document_code_missing", "TARIC-asiakirjakoodi puuttuu.", "No supported TARIC document code."))
    # validity period from the agreement text
    issued = _day(d.get("date"))
    months = rule.get("validity_months")
    if issued and issued > on:
        ok = False
        res["reasons"].append(R("proof_future", "Alkuperäselvitys on päivätty tulevaisuuteen.", "Proof is future-dated."))
    if months and issued:
        until = _add_months(issued, int(months))
        res["valid_until"] = until.isoformat()
        if on > until:
            res["reasons"].append(R("proof_expired", f"Alkuperäselvitys ({issued.isoformat()}) oli voimassa {months} kk, {until.isoformat()} asti"
                                    f"{' (' + rule['article'] + ')' if rule.get('article') else ''}.",
                                    f"The proof ({issued.isoformat()}) was valid {months} months, until {until.isoformat()}"
                                    f"{' (' + rule['article'] + ')' if rule.get('article') else ''}."))
            ok = False
    elif months and not issued:
        res["reasons"].append(R("proof_date_missing", "Alkuperäselvityksen päiväys puuttuu; voimassaoloa ei voi tarkistaa.",
                                "The proof has no date; its validity cannot be checked."))
        ok = False
    if d["type"] in ("eur1", "eur_med", "atr", "importers_knowledge") and not d.get("ref"):
        res["reasons"].append(R("certificate_number_missing", "Tavaratodistuksen numero puuttuu.", "The certificate number is missing."))
        ok = False
    if d["type"] in STATEMENT_KINDS:
        ok = await _check_exporter(d, rule, arr, origin, consignment_value_cents, res) and ok
    if d["type"] == "importers_knowledge":
        res["reasons"].append(R("importers_knowledge", "Tuojan tietoihin perustuva etuus: tuojalla on oltava asiakirjat, jotka "
                                "osoittavat tuotteen alkuperäisyyden (tuoteluettelo / toimittajan ilmoitus).",
                                "Importer's knowledge: the importer must hold records showing the product is originating "
                                "(bill of materials / supplier's declaration).", severity="advisory"))
    res["valid"] = ok
    return res


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


async def _check_exporter(d: dict, rule: dict, arr: dict | None, origin: str, value_cents: int | None, res: dict) -> bool:
    """Exporter reference rules: above the any-exporter threshold the statement needs a registered / approved exporter
    reference; GSP and other REX-based schemes check it live in the EU REX register."""
    ref = (d.get("exporter_ref") or "").strip() or None
    if rule.get("who") == "national_registered_exporter" and d.get("exporter_authorisation_verified") is not True:
        res["reasons"].append(R("national_registration_unverified", "Viejän kansallista rekisteröintiä ei ole tarkastettu.",
                                "The exporter's national registration has not been checked."))
        return False
    if rule.get("who") in ("japan_exporter", "canadian_exporter"):
        # Both protocols explicitly permit a blank reference when none has been assigned.
        return True
    any_lim = None
    for p in _accepted(arr):
        if p.get("type") in STATEMENT_KINDS and p.get("who") == "any_exporter" and p.get("max_value_eur"):
            any_lim = float(p["max_value_eur"])
    small = value_cents is not None and any_lim is not None and 0 <= value_cents <= any_lim * 100
    rex_scheme = bool((arr or {}).get("type") in ("gsp", "gsp_plus", "eba") or rule.get("who") == "registered_exporter")
    if not ref:
        if small:
            return True
        lim_fi = f" yli {int(any_lim)} EUR:n lähetyksessä" if any_lim else ""
        lim_en = f" for a consignment over EUR {int(any_lim)}" if any_lim else ""
        res["reasons"].append(R("exporter_ref_missing", f"Alkuperäilmoituksesta puuttuu viejän viitenumero (REX/hyväksytty viejä){lim_fi}.",
                                f"The statement has no exporter reference (REX / approved exporter){lim_en}."))
        return False
    if rule.get("who") == "any_exporter" and not small:
        res["reasons"].append(R("statement_threshold", "Lähetyksen arvo ylittää ilmoituksen rajan tai arvo puuttuu.",
                                "Consignment value exceeds the statement threshold or is missing."))
        return False
    if rule.get("who") == "approved_exporter" and not small and d.get("exporter_authorisation_verified") is not True:
        res["reasons"].append(R("exporter_authorisation_unverified", "Hyväksytyn viejän lupaa ei ole tarkastettu.",
                                "Approved exporter authorisation has not been checked."))
        return False
    m = REX_RE.search(ref.replace(" ", "").upper())
    if m or rex_scheme:
        if not m:
            res["reasons"].append(R("rex_format", f"Viitenumero {ref} ei ole REX-numero (muoto XXREX…).",
                                    f"Reference {ref} is not a REX number (format XXREX…)."))
            return small
        chk = await rex_check(m.group(1))
        res["exporter_check"] = chk
        if chk.get("valid") is True:
            valid_from, issued = _day(chk.get("valid_from")), _day(d.get("date"))
            if valid_from and issued and issued < valid_from:
                res["reasons"].append(R("rex_not_yet_registered", "Viejä ei ollut rekisteröity ilmoituksen päivänä.",
                                        "Exporter was not registered on the statement date."))
                return False
            if chk.get("country") and chk["country"] != origin:
                res["reasons"].append(R("rex_wrong_country", f"REX-numero {m.group(1)} on maan {chk['country']}, ei {origin}.",
                                        f"REX number {m.group(1)} belongs to {chk['country']}, not {origin}."))
                return False
            return True
        if chk.get("valid") is False:
            res["reasons"].append(R("rex_invalid", f"REX-numeroa {m.group(1)} ei löydy EU:n REX-rekisteristä ({on_text(chk)}).",
                                    f"REX number {m.group(1)} is not valid in the EU REX register ({on_text(chk)})."))
            return False
        res["reasons"].append(R("rex_unverified", f"REX-numeroa {m.group(1)} ei voitu tarkistaa: {chk.get('error') or 'palvelu ei vastannut'}.",
                                f"REX number {m.group(1)} could not be checked: {chk.get('error') or 'no answer'}."))
        return False
    return True


def on_text(chk: dict) -> str:
    return chk.get("checked_at", "")[:10] or "—"


# ---------------------------------------------------------------------------------------------------------------
# REX (filled in below by rex_check) and EORI
# ---------------------------------------------------------------------------------------------------------------
REX_URL = "https://ec.europa.eu/taxation_customs/dds2/eos/rex_list.jsp"


def parse_rex(page: str, number: str) -> dict:
    """Read the matching row, never infer validity from HTTP 200 or a substring."""
    import lxml.html
    tree = lxml.html.fromstring(page)
    rows = tree.xpath('//tr[@id=$number]', number=number)
    if len(rows) != 1:
        return {"valid": None, "error": "EU REX response has no matching registration row"}
    row = rows[0]
    status = " ".join(row.xpath('./td[@data-ecl-table-header="Status"]//text()')).strip()
    status = " ".join(status.split())
    valid = {"The REX number is valid": True, "The REX number is not valid": False}.get(status)
    out = {"valid": valid, "status": status, "country": number[:2]}
    lines = _text(page).splitlines()
    for label, key in [("Date from which the REX authorisation is valid", "valid_from"),
                       ("Date of revocation of the REX authorisation", "revoked_at")]:
        if label in lines:
            i = lines.index(label)
            dt = _day(lines[i+1]) if i+1 < len(lines) else None
            out[key] = dt.isoformat() if dt else None
    if valid is None:
        out["error"] = "Unrecognised EU REX status"
    return out


async def rex_check(number: str) -> dict:
    number = number.replace(" ", "").upper()
    if not REX_RE.fullmatch(number):
        return {"number": number, "valid": False, "error": "Invalid REX number format", "checked_at": now_iso()}
    async def fetch():
        try:
            r = await _http_get(REX_URL, {"Lang": "en", "Number": number, "Type": "REX"})
            r.raise_for_status()
            return {"number": number, **parse_rex(r.text, number), "source_url": str(r.url), "checked_at": now_iso()}
        except Exception as exc:
            return {"number": number, "valid": None, "error": f"{type(exc).__name__}: {exc}",
                    "source_url": REX_URL, "checked_at": now_iso()}
    return await _cached("rex", number, fetch)


# ---------------------------------------------------------------------------------------------------------------
# assess(): the DE 4/17 decision for one goods item
# ---------------------------------------------------------------------------------------------------------------
async def _taric_lookup(code: str, origin: str, on: date) -> dict:
    from services import taric  # sub-agent A's live TARIC module (s11/taric)

    return await taric.lookup(code, country=origin, flow="import", on=on.isoformat())


def _goods_code(item: dict) -> str | None:
    for k in ("taric_code", "cn_code", "hs_code", "goods_code", "code"):
        v = _digits(item.get(k))
        if len(v) >= 6:
            return (v + "0000")[:10]
    return None


def _applies(m: dict, origin: str) -> bool:
    geo = str(m.get("geo_code") or "")
    if re.fullmatch(r"[A-Z]{2}", geo) and geo != origin:
        return False
    excl = {str(x).upper() for x in (m.get("excluded_geo") or m.get("exclusions") or [])}
    return origin not in excl


def _empty(origin: str, on: date) -> dict:
    return {"preference_code": "100", "preference_label": preference_label("100"), "origin": origin,
            "arrangement": None, "preferential_duty": None, "third_country_duty": None, "saving_cents": None,
            "proof": {"type": None, "ref": None, "valid": None, "document_code": None},
            "psr": {"rule": None, "rule_text": None, "met": None, "basis": None}, "quota": None,
            "available_preference_code": None, "reasons": [], "date": on.isoformat(), "assessed_at": now_iso(),
            "sources": []}


async def assess(item: dict, *, origin: str, documents: list[dict] | None = None, export_country: str | None = None,
                 on=None, consignment_value_cents: int | None = None) -> dict:
    """Preference decision (DE 4/17) for one goods item of an import into Finland, from official data.
    export_country: country of dispatch; use assess_export() for export advice."""
    on_d = _on(on)
    origin = (origin or "").upper()
    out = _empty(origin, on_d)
    reasons = out["reasons"]
    code = _goods_code(item)
    if not origin:
        reasons.append(R("origin_missing", "Alkuperämaa puuttuu (DE 5/15); etuuskohtelua ei voi arvioida.",
                         "Country of origin missing (DE 5/15); preference cannot be assessed."))
        return out
    if origin in EU or origin == "EU":
        reasons.append(R("eu_origin", "Tavara on EU-alkuperää; tuontietuuskohtelua ei sovelleta.",
                         "Goods of EU origin: no import preference applies."))
        return out
    if not code:
        reasons.append(R("code_missing", "Tullinimike puuttuu; etuuskohtelua ei voi arvioida.",
                         "Commodity code missing; preference cannot be assessed."))
        return out
    out["goods_code"] = code
    try:
        lk = await _taric_lookup(code, origin, on_d)
    except Exception as exc:  # noqa: BLE001
        log.warning("TARIC lookup failed for %s/%s: %s", code, origin, exc)
        reasons.append(R("taric_unavailable", f"TARIC-haku epäonnistui ({type(exc).__name__}); etuuskohtelua ei haeta.",
                         f"TARIC lookup failed ({type(exc).__name__}); no preference claimed."))
        return out
    out["sources"].append({"name": lk.get("source") or "EU TARIC consultation", "url": lk.get("source_url"),
                           "fetched_at": lk.get("fetched_at")})
    if lk.get("declarable") is False:
        reasons.append(R("code_not_declarable", "Valitse ilmoituskelpoinen TARIC-alanimike.",
                         "Select a declarable TARIC subheading.", subcodes=lk.get("subcodes")))
        return out
    measures = [m for m in lk.get("measures") or [] if _applies(m, origin)]
    thirds = [m for m in measures if _is_third(m)]
    third = next((m for m in thirds if not m.get("additional_codes")), thirds[0] if thirds else None)
    third_d = measure_duty(third, item)
    out["third_country_duty"] = third_d
    cands = [m for m in measures if (_is_pref(m) or _is_cu(m))]
    rulesets = await roo_rulesets(origin)
    if not cands:
        arr, kind = await arrangement_for(origin)
        out["arrangement"] = _public_arrangement(arr, type_override=kind)
        if arr:
            reasons.append(R("no_preferential_measure",
                             f"{arr.get('name_fi') or arr.get('name_en')}: TARICissa ei ole etuustullia nimikkeelle {code} "
                             f"alkuperälle {origin} ({on_d.isoformat()}).",
                             f"{arr.get('name_en')}: TARIC has no preferential duty for {code} from {origin} on {on_d.isoformat()}."))
        else:
            reasons.append(R("no_arrangement", f"EU:lla ei ole etuuskohtelujärjestelyä alkuperämaan {origin} kanssa "
                             f"(TARIC: ei etuustullia nimikkeelle {code}; DG TRADE: ei alkuperäsääntöjä).",
                             f"No EU preferential arrangement with {origin} (TARIC: no preferential duty for {code}; "
                             f"DG TRADE: no origin rules)."))
        return out
    # cheapest first; quotas and end-use measures only when usable
    scored = sorted(cands, key=lambda m: (measure_duty(m, item).get("pct") if measure_duty(m, item).get("pct") is not None else 999,
                                          1 if _features(m)["quota"] else 0))
    chosen, chosen_d, quota = None, None, None
    for m in scored:
        f = _features(m)
        md = measure_duty(m, item)
        if f["end_use"]:
            reasons.append(R("end_use", f"{m.get('type')} ({md.get('expression')}) edellyttää käyttötarkoituslupaa (end-use, UCC 254 art.); "
                             "ei haeta automaattisesti.",
                             f"{m.get('type')} ({md.get('expression')}) needs an end-use authorisation (UCC Art. 254); "
                             "not claimed automatically.", severity="advisory"))
            continue
        lower = _lower(md, third_d)
        if lower is False:
            reasons.append(R("no_saving", f"Etuustulli {md.get('expression')} ei ole kolmannen maan tullia "
                             f"{third_d.get('expression')} alempi.",
                             f"Preferential duty {md.get('expression')} is not lower than the third-country duty "
                             f"{third_d.get('expression')}."))
            continue
        if f["quota"] and not m.get("order_number"):
            reasons.append(R("quota_order_missing", "Kiintiön järjestysnumero puuttuu.", "Quota order number is missing."))
            continue
        if f["quota"] and m.get("order_number"):
            q = await quota_status(m["order_number"], on_d)
            if q["status"] not in ("open", "critical"):
                fi = {"exhausted": "loppunut", "blocked": "estetty", "suspended": "keskeytetty", "not_found": "ei voimassa"}.get(q["status"], "ei vahvistettu")
                reasons.append(R(f"quota_{q['status']}", f"Tariffikiintiö {q['order_number']} on {fi} "
                                 f"{('(' + q['exhaustion_date'] + ')') if q.get('exhaustion_date') else ''} – kiintiöetuutta ei voi hakea.",
                                 f"Tariff quota {q['order_number']} is {q['status'].replace('_', ' ')}"
                                 f"{(' (' + q['exhaustion_date'] + ')') if q.get('exhaustion_date') else ''}: the quota rate cannot be claimed.",
                                 quota=q))
                out["quota"] = {"order_number": q["order_number"], "status": q["status"], "balance": q.get("balance"),
                                "unit": q.get("unit"), "source_url": q.get("details_url") or q.get("source_url")}
                continue
            quota = q
        chosen, chosen_d = m, md
        break
    if not chosen:
        arr, kind = await arrangement_for(origin, scored[0])
        out["arrangement"] = _public_arrangement(arr, type_override=kind)
        return out
    out["selected_measure_sid"] = chosen.get("sid")
    out["preferential_duty"] = chosen_d
    cu = _is_cu(chosen)
    if cu and export_country and export_country.upper() != origin:
        reasons.append(R("dispatch_mismatch", "Tulliliiton lähetysmaa on tarkistettava.", "Customs union dispatch country needs review."))
        return out
    arr, kind = await arrangement_for(origin, chosen)
    out["arrangement"] = _public_arrangement(arr, type_override=kind)
    arr_code = (arr or {}).get("code")
    first = FIRST_DIGIT["customs_union"] if cu else (FIRST_DIGIT["gsp"] if kind in ("gsp", "gsp_plus", "eba") else
                                                    FIRST_DIGIT["other_preference"])
    suffix = preference_suffix(**{k: v for k, v in _features(chosen).items() if k != "end_use"})
    cand_code = preference_code(first, suffix)
    out["available_preference_code"] = cand_code
    if quota:
        out["quota"] = {"order_number": quota["order_number"], "status": quota["status"], "balance": quota.get("balance"),
                        "unit": quota.get("unit"), "source_url": quota.get("details_url") or quota.get("source_url")}
        if quota["status"] in ("critical", "unknown"):
            reasons.append(R(f"quota_{quota['status']}", f"Tariffikiintiö {quota['order_number']}: tila {quota['status']} – jako ei ole varma.",
                             f"Tariff quota {quota['order_number']}: status {quota['status']} – allocation not certain.",
                             severity="advisory"))
    if third_d.get("cents") is not None and chosen_d.get("cents") is not None:
        out["saving_cents"] = max(0, third_d["cents"] - chosen_d["cents"])
    if not arr:
        reasons.append(R("arrangement_unknown", f"TARICin etuusmittaus ({chosen.get('geo_name') or chosen.get('geo_code')}) ei vastaa "
                         "tunnettua järjestelyä; tarkista käsin.",
                         f"The TARIC preference ({chosen.get('geo_name') or chosen.get('geo_code')}) does not match a known "
                         "arrangement; check manually."))
        return out
    if arr.get("proof_policy_reviewed") is not True:
        reasons.append(R("proof_policy_unverified", "Järjestelyn alkuperäselvityssääntöjä ei ole vielä tarkastettu; etuutta ei haeta.",
                         "This arrangement's proof rules have not been reviewed; preference is not claimed."))
        return out
    docs = normalise_documents(documents)
    item_value = item.get("customs_value_cents") if item.get("customs_value_cents") is not None else item.get("value_cents")
    proof = await evaluate_proof(arr, arr_code, origin, docs, on_d, measure=chosen, customs_union=cu,
                                 consignment_value_cents=consignment_value_cents if consignment_value_cents is not None else item_value)
    out["proof"] = {k: proof.get(k) for k in ("type", "ref", "valid", "document_code", "document_code_text", "exporter_ref",
                                              "exporter_check", "date", "valid_until", "document_id")}
    reasons.extend(proof["reasons"])
    # product-specific rule
    hs6 = code[:6]
    if cu:
        out["psr"] = {"rule": None, "rule_text": None, "met": True if proof.get("valid") else None, "basis": "customs_union",
                      "note": {"fi": "Tulliliitto: tavaran on oltava vapaassa liikkeessä (A.TR), ei alkuperäsääntöä.",
                               "en": "Customs union: goods must be in free circulation (A.TR); no origin rule."}}
    else:
        psr = await product_specific_rule(hs6, origin)
        bom = _bom_from(docs, item)
        ev = evaluate_psr(psr, hs6, bom)
        alts = [a["text"] for b in psr.get("blocks") or [] for a in b["alternatives"]]
        out["psr"] = {"rule": " | ".join(alts) or None,
                      "rule_text": "\n".join(f"{b['heading']}: " + " OR ".join(a["text"] for a in b["alternatives"])
                                             for b in psr.get("blocks") or []) or None,
                      "structure": psr.get("blocks"), "met": None, "basis": None, "evaluation": ev.get("detail"),
                      "source": psr.get("source"), "source_url": psr.get("source_url"), "page_url": psr.get("page_url")}
        out["sources"].append({"name": "Access2Markets ROSA (DG TRADE)", "url": psr.get("source_url"),
                               "fetched_at": psr.get("fetched_at")})
        statement_proof = proof.get("type") in STATEMENT_KINDS | {"eur1", "eur_med"}
        if ev["met"] is False:
            out["psr"].update(met=False, basis="bill_of_materials")
            reasons.append(R("psr_not_met" if not statement_proof else "psr_conflict",
                             "Tuoteluettelon mukaan alkuperäsääntö ei täyty: " + "; ".join(x["why"] for x in ev["detail"]),
                             "By the bill of materials the product-specific rule is not met: " + "; ".join(x["why"] for x in ev["detail"])))
        elif ev["met"] is True:
            out["psr"].update(met=True, basis="bill_of_materials")
        elif statement_proof and proof.get("valid"):
            out["psr"].update(met=True, basis="proof_of_origin")
        else:
            why_fi = "täydellinen tuoteluettelo tai valmistusnäyttö puuttuu"
            why_en = "a complete bill of materials or production evidence is missing"
            reasons.append(R("psr_unknown", f"Alkuperäsäännön täyttymistä ei voi todeta ({why_fi}).",
                             f"Product-specific rule cannot be confirmed ({why_en})."))
        if not psr.get("found"):
            reasons.append(R("psr_not_found", "Access2Marketsista ei löytynyt tuotekohtaista sääntöä tälle nimikkeelle.",
                             "Access2Markets returned no product-specific rule for this code.",
                             severity="advisory", error=psr.get("error")))
    lower = _lower(chosen_d, third_d)
    claim = bool(proof.get("valid") and out["psr"].get("met") is True and lower is not False and
                 (lower is True or (chosen_d.get("pct") == 0 and (third_d.get("pct") or 0) > 0)))
    if claim:
        out["preference_code"] = cand_code
        out["preference_label"] = preference_label(cand_code)
        arrn = arr.get("name_fi") or arr.get("name_en")
        reasons.insert(0, R("preference_claimed",
                            f"Etuuskohtelu {cand_code} ({arrn}): tulli {chosen_d.get('expression')} kolmannen maan "
                            f"{third_d.get('expression')} sijaan; alkuperäselvitys {PROOF_LABEL.get(proof['type'], {}).get('fi', proof['type'])}"
                            f"{' ' + proof['document_code'] if proof.get('document_code') else ''}.",
                            f"Preference {cand_code} ({arr.get('name_en')}): duty {chosen_d.get('expression')} instead of "
                            f"{third_d.get('expression')}; proof {PROOF_LABEL.get(proof['type'], {}).get('en', proof['type'])}"
                            f"{' ' + proof['document_code'] if proof.get('document_code') else ''}.", severity="info"))
    else:
        if lower is None and not any(r["code"] == "no_saving" for r in reasons):
            reasons.append(R("duty_not_comparable", "Etuustullia ja kolmannen maan tullia ei voi verrata tämän nimikkeen tiedoilla.",
                             "The preferential and third-country duties cannot be compared with this item's data."))
        reasons.insert(0, R("preference_not_claimed", f"Etuuskohtelua {cand_code} ei haeta; käytetään koodia 100.",
                            f"Preference {cand_code} not claimed; code 100 is used.", severity="info"))
    return out


async def assess_export(item: dict, *, destination: str, on=None) -> dict:
    """Export from Finland/EU to `destination`: which arrangement, the partner's product-specific rule for EU-originating
    goods (ROSA direction EXPORT) and which proof the exporter issues."""
    on_d = _on(on)
    dest = (destination or "").upper()
    code = _goods_code(item)
    out = {"flow": "export", "destination": dest, "date": on_d.isoformat(), "arrangement": None, "psr": None,
           "proofs": [], "reasons": [], "assessed_at": now_iso()}
    sets = await roo_rulesets(dest, "export")
    arr = next((arrangement_record(c) for c in sets if arrangement_record(c)), None)
    if not arr:
        arr = _cu_arrangement(dest)
    out["arrangement"] = _public_arrangement(arr)
    if not arr:
        out["reasons"].append(R("no_arrangement", f"EU:lla ei ole etuuskohtelujärjestelyä maan {dest} kanssa.",
                                f"No EU preferential arrangement with {dest}."))
        return out
    out["proofs"] = [{**p, "document_code": (document_code_for(p.get("type"), arr["code"]) or {}).get("code")}
                     for p in _accepted(arr)]
    if code and arr.get("type") != "customs_union":
        psr = await product_specific_rule(code[:6], "FI", destination=dest, direction="EXPORT")
        out["psr"] = {"rule": " | ".join(a["text"] for b in psr.get("blocks") or [] for a in b["alternatives"]) or None,
                      "structure": psr.get("blocks"), "source_url": psr.get("source_url")}
    return out


# ---------------------------------------------------------------------------------------------------------------
# Lists, export statement, EUR.1
# ---------------------------------------------------------------------------------------------------------------
def list_arrangements() -> dict:
    data = arrangements_data()
    arrs = []
    for code, a in sorted((data.get("arrangements") or {}).items()):
        arrs.append({"code": code, "name_fi": a.get("name_fi"), "name_en": a.get("name_en"), "type": a.get("type"),
                     "partners": a.get("partners") or [], "legal_basis": a.get("legal_basis"),
                     "proofs": [{**p, "document_code": (document_code_for(p.get("type"), code) or {}).get("code")}
                                for p in a.get("proofs") or []],
                     "proof_policy_reviewed": a.get("proof_policy_reviewed", False),
                     "statement_available": {k: bool(v) for k, v in (a.get("statement_text") or {}).items()},
                     "source_url": a.get("source_url")})
    return {"arrangements": arrs, "taric_groups": data.get("taric_groups") or {},
            "sources": data.get("sources") or [], "codes_sources": codes_data().get("sources") or [],
            "generated_at": data.get("generated_at")}


# ---------------------------------------------------------------------------------------------------------------
# Import declaration hooks (services.customs.recompute / validate)
# ---------------------------------------------------------------------------------------------------------------
ASSESS_TIMEOUT_S = 90


async def _declaration_documents(decl: dict) -> list[dict]:
    """Proof candidates for a declaration: the shipment's invoice (statement on origin text), EUR.1 / certificates of
    origin and proofs a broker recorded on the declaration (decl.origin_proofs)."""
    docs: list[dict] = []
    if decl.get("shipment_id"):
        cur = db.documents.find({"shipment_id": decl["shipment_id"],
                                 "type": {"$in": ["commercial_invoice", "eur1", "certificate_of_origin"]}},
                                {"_id": 0, "id": 1, "type": 1, "number": 1, "extraction": 1, "filename": 1})
        docs.extend(await cur.to_list(50))
    docs.extend(p for p in decl.get("origin_proofs") or [] if isinstance(p, dict))
    return docs


async def apply_to_declaration(decl: dict, *, on=None) -> None:
    """Set preference (DE 4/17), preference_note and the proof document (DE 2/3) on every item of an import
    declaration from assess(). A preference a person edited is kept; its assessment still runs (ORIGIN-PREF)."""
    import asyncio
    import os

    if (os.environ.get("CUSTOMS_ORIGIN_ASSESS") or "true").lower() in ("0", "false", "no", "off"):
        return
    items = decl.get("items") or []
    if not items:
        return
    docs = await _declaration_documents(decl)
    values = [it.get("value_cents") for it in items]
    total = sum(values) if all(v is not None for v in values) else None

    async def one(it):
        mine = [d for d in docs if d.get("item_seq") in (None, it.get("seq"))]
        mine += [d for d in it.get("origin_proofs") or [] if isinstance(d, dict)]
        if it.get("bom"):
            mine.append({"type": "bill_of_materials", **it["bom"]})
        return await assess(it, origin=it.get("origin_country") or "", documents=mine, on=on,
                            consignment_value_cents=total if total is not None else -1)

    try:
        results = await asyncio.wait_for(asyncio.gather(*(one(it) for it in items), return_exceptions=True),
                                         timeout=ASSESS_TIMEOUT_S)
    except asyncio.TimeoutError:
        results = [TimeoutError("origin assessment timed out")] * len(items)
    for it, res in zip(items, results):
        # Remove only documents generated by this hook; retain declared and register evidence.
        it["documents"] = [d for d in it.get("documents") or [] if d.get("source") != "origin_assessment"]
        if isinstance(res, Exception):
            log.warning("origin assessment failed for item %s: %s", it.get("seq"), res)
            it["origin_assessment"] = {"error": f"{type(res).__name__}: {res}", "assessed_at": now_iso(),
                                       "reasons": [R("assessment_failed", "Etuuskohtelun arviointi epäonnistui; koodi 100.",
                                                     "Preference assessment failed; code 100.")]}
            it.pop("origin_proof_document", None)
            it.pop("quota_order_number", None)
            if "preference" not in (it.get("edited") or []):
                it["preference"] = "100"
            continue
        it["origin_assessment"] = res
        it.pop("origin_proof_document", None)
        if "preference" not in (it.get("edited") or []):
            it["preference"] = res["preference_code"]
        head = next((r for r in res["reasons"] if r.get("severity") != "info" or r["code"] == "preference_claimed"), None)
        main = res["reasons"][0] if res["reasons"] else None
        it["preference_note"] = (main or {}).get("en")
        it["preference_note_i18n"] = {"fi": (main or {}).get("fi"), "en": (main or {}).get("en"),
                                      "detail_fi": (head or {}).get("fi"), "detail_en": (head or {}).get("en")}
        pr = res.get("proof") or {}
        it["origin_proof_document"] = ({"code": pr.get("document_code"), "ref": pr.get("ref") or pr.get("exporter_ref"),
                                        "type": pr.get("type"), "valid": pr.get("valid"), "date": pr.get("date")}
                                       if pr.get("type") else None)
        if res["preference_code"] != "100" and pr.get("valid") and pr.get("document_code"):
            ref = pr.get("ref") or pr.get("exporter_ref")
            if ref:
                it["documents"].append({"code": pr["document_code"], "reference": ref,
                                         "source": "origin_assessment", "document_id": pr.get("document_id")})
        if res.get("quota") and res["preference_code"][1:] in ("20", "23", "25"):
            it["quota_order_number"] = res["quota"]["order_number"]
        else:
            it.pop("quota_order_number", None)


def validations(decl: dict) -> list[dict]:
    """Rule ORIGIN-PREF: blocking only when an item claims a preference (DE 4/17 not 1xx) without a valid proof that
    the assessment confirmed; otherwise advisory with the assessment's reason."""
    out = []
    for it in decl.get("items") or []:
        pref = str(it.get("preference") or "100")
        a = it.get("origin_assessment") or {}
        proof = a.get("proof") or {}
        claimed = not pref.startswith("1")
        if claimed:
            ok = bool(proof.get("valid")) and a.get("preference_code") == pref and \
                (a.get("psr") or {}).get("met") is True
            why = next((r for r in a.get("reasons") or [] if r.get("severity") not in ("info", "advisory")), None)
            msg_en = (f"Preference {pref}: proof {proof.get('type')} {proof.get('document_code') or ''} "
                      f"{proof.get('ref') or ''} valid" if ok else
                      f"Preference {pref} is claimed without a valid proof of origin / met origin rule: "
                      f"{(why or {}).get('en') or 'no assessment'} (UCC art. 64, DA art. 37)")
            msg_fi = (f"Etuuskohtelu {pref}: alkuperäselvitys {proof.get('type')} {proof.get('document_code') or ''} "
                      f"{proof.get('ref') or ''} kunnossa" if ok else
                      f"Etuuskohtelua {pref} haetaan ilman pätevää alkuperäselvitystä / täyttynyttä alkuperäsääntöä: "
                      f"{(why or {}).get('fi') or 'ei arviota'}")
            row = {"rule": "ORIGIN-PREF", "ok": ok, "severity": "blocking", "owner": "broker", "message": " ".join(msg_en.split()),
                   "item": it.get("seq"), "field": "preference",
                   "message_i18n": {"en": " ".join(msg_en.split()), "fi": " ".join(msg_fi.split())}}
        else:
            main = (a.get("reasons") or [None])[0] or {}
            extra = next((r for r in (a.get("reasons") or [])[1:]), None)
            en = " — ".join(x for x in (main.get("en"), (extra or {}).get("en")) if x) or "Preference not assessed"
            fi = " — ".join(x for x in (main.get("fi"), (extra or {}).get("fi")) if x) or "Etuuskohtelua ei arvioitu"
            row = {"rule": "ORIGIN-PREF", "ok": True, "severity": "advisory", "owner": "broker", "message": en,
                   "item": it.get("seq"), "field": "preference", "message_i18n": {"en": en, "fi": fi}}
        out.append(row)
    return out


def origin_statement_text(arrangement, exporter_id, origin_country, lang="en") -> dict:
    """Official declaration wording with the two supplied fields filled; remaining mandatory fields are explicit.
    It is a draft, never an issued origin proof. Finnish is not an authorised GSP statement language.
    """
    code = arrangement.get("code") if isinstance(arrangement, dict) else arrangement
    arr = arrangement_record(code)
    if not arr:
        raise KeyError(f"Unknown arrangement {code}")
    source = (arr.get("statement_text") or {}).get(lang.lower())
    if not source:
        raise KeyError(f"No published {lang} statement wording captured for {code}")
    text = source["text"].replace("{exporter_id}", str(exporter_id or "")).replace("{origin_country}", str(origin_country))
    return {"arrangement": code, "lang": lang.lower(), "text": text, "source_url": source["source_url"],
            "draft": True, "required_fields": source.get("additional_fields", []),
            "note": {"fi": "Luonnos: täytä vaaditut tiedot ja tarkista alkuperä ennen antamista.",
                     "en": "Draft: complete required fields and establish origin before issuance."}}


def eur1_field_data(*, exporter: dict, consignee: dict, origin_country: str, destination: str,
                    items: list[dict], invoice_ref: str, transport: str = "") -> dict:
    """EUR.1 application data only; box 11 certification belongs to the issuing customs authority."""
    return {"type": "eur1", "issued": False, "fields": {
        "1_exporter": exporter, "2_preferential_trade_between": [origin_country, destination],
        "3_consignee": consignee, "4_origin_country": origin_country, "5_destination": destination,
        "6_transport": transport, "7_remarks": "", "8_goods": [
            {k: it.get(k) for k in ("description", "packages", "package_type", "marks")} for it in items],
        "9_gross_kg": sum(it["gross_kg"] for it in items) if items and all(it.get("gross_kg") is not None for it in items) else None,
        "10_invoice": invoice_ref}, "required_before_issue": ["11_customs_endorsement", "12_exporter_declaration"],
        "source_url": arrangement_record("VN")["source_url"]}
