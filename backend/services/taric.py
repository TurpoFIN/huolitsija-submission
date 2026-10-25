"""Live EU TARIC: measures, duty maths and tariff quotas from the official TARIC consultation.

Source: the European Commission's TARIC consultation (DG TAXUD, "Data Dissemination System"),
https://ec.europa.eu/taxation_customs/dds2/taric/ . No rate in this module is invented or hard coded: every measure,
duty expression, condition, certificate, additional code, regulation and quota balance is read from the official pages
for the requested code, country and date.

How the consultation is read (verified against the live site, 2026-10-24):
- `measures.jsp?Taric=<10 digits>&Area=<ISO2 or geo id>&SimDate=YYYYMMDD&Lang=en&Expand=true` lists the declarable
  codes under the requested code; for each one the page carries a `measures_details.jsp?...&FrameId=taric_<code>_<ts>`
  URL (with an opaque `Sid`) that the browser loads into an iframe. We follow those official URLs as they are.
- `measures_details.jsp` renders the measures grouped by geographical area: type text, validity, duty expression
  (`span.duty_rate`), footnotes, regulation, excluded countries, additional code, quota order number and a
  `measures_conditions.jsp?MeasureSid=<sid>` link when the measure has conditions.
- `measures_conditions.jsp` gives the condition lines (condition code + sequence, certificate, action, duty) and the
  official descriptions of the certificates / documents (e.g. "D008 Invoice with a signed declaration").
- The measure-type codes come from the consultation's own list `meas_type_combo/meas_typ_combo_en_<date>.js`; a copy
  captured from it is committed in `data/taric/measure_types_en.json` for when the site is unreachable.
- Tariff quotas: `quota_number_list.jsp?Code=<order>&Year=<yyyy>&Lang=en` (the "Order number" link on a quota measure).

Entry-price and Meursing (EA / ADSZ / ADFM) duties are never guessed: `compute_duty` returns a blocking status with the
official explanation instead of an amount.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import lxml.html

from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("taric")

BASE = "https://ec.europa.eu/taxation_customs/dds2/taric/"
SOURCE = "EU TARIC consultation"
# The Commission's web filter answers 502 to the default python-httpx agent; identify ourselves plainly instead.
USER_AGENT = "Mozilla/5.0 (compatible; Huolitsija-customs/1.0; +https://huolitsija.fi)"
CACHE_HOURS = 24
TIMEOUT = 45.0
HELSINKI = ZoneInfo("Europe/Helsinki")
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "taric"

register_index("taric_cache", "key", unique=True)
register_index("taric_cache", "expires_at", expireAfterSeconds=0)

_SEMS: dict = {}  # one semaphore per event loop: at most 4 requests at a time to the Commission's server


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _SEMS:
        _SEMS.clear()
        _SEMS[loop] = asyncio.Semaphore(4)
    return _SEMS[loop]


class TaricUnavailable(Exception):
    """The official TARIC consultation could not be reached or returned an error page."""


# ---------------------------------------------------------------------------------------------
# Measure types (official list)
# ---------------------------------------------------------------------------------------------
_TYPES: dict = {"rows": None, "loaded_on": None}

# Kinds by official measure-type code (TARIC measure type series).
THIRD_COUNTRY = {"103", "105", "106"}
PREFERENCE = {"142", "145"}
QUOTA_NON_PREF = {"122", "123"}
QUOTA_PREF = {"143", "146"}
QUOTA = QUOTA_NON_PREF | QUOTA_PREF | {"147"}
SUSPENSION = {"112", "115", "117", "119"}
TRADE_DEFENCE = {"551", "552", "553", "554", "555", "561", "562", "564", "565", "566", "570"}
TRADE_DEFENCE_DUTY = {"551", "552", "553", "554", "555"}
ADDITIONAL_DUTY = {"651", "652", "653", "654", "656", "672", "673", "674", "690", "692", "693", "695", "696"}
PROHIBITION = {"277", "278", "706"}
SUPPLEMENTARY_UNIT = {"109", "110", "111"}


def _parse_type_js(text: str) -> list[list[str]]:
    start, end = text.index("["), text.rindex("]") + 1
    return json.loads(text[start:end])


def _type_rows() -> list[list[str]]:
    if _TYPES["rows"] is None:
        try:
            _TYPES["rows"] = json.loads((DATA_DIR / "measure_types_en.json").read_text())["types"]
        except Exception:  # noqa: BLE001
            _TYPES["rows"] = []
    return _TYPES["rows"]


async def refresh_measure_types(on: str | None = None, client: httpx.AsyncClient | None = None) -> int:
    """Load the official measure-type list of the consultation for a date (once a day per process)."""
    d = _simdate(on)
    if _TYPES.get("loaded_on") == d and _TYPES["rows"]:
        return len(_TYPES["rows"])
    try:
        text = await _get(f"meas_type_combo/meas_typ_combo_en_{d}.js", client=client)
        rows = _parse_type_js(text)
        if rows:
            _TYPES["rows"], _TYPES["loaded_on"] = rows, d
    except Exception as e:  # noqa: BLE001 - the committed official copy is used instead
        log.info("TARIC measure types not refreshed: %s", e)
        _type_rows()
    return len(_TYPES["rows"] or [])


def type_candidates(text: str, flow: str | None = None) -> list[str]:
    """Official measure-type codes whose description equals the text shown on the measure (several types share a
    description, e.g. "Export control"; the flow narrows them)."""
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    rows = [r for r in _type_rows() if r[1].strip().lower() == t]
    if flow and len(rows) > 1:
        narrowed = [r for r in rows if flow.lower() in r[3].lower()]
        rows = narrowed or rows
    return sorted({r[0] for r in rows})


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def _simdate(on=None) -> str:
    if on is None or on == "":
        return datetime.now(HELSINKI).strftime("%Y%m%d")
    if isinstance(on, (date, datetime)):
        return on.strftime("%Y%m%d")
    d = re.sub(r"\D", "", str(on))[:8]
    if len(d) != 8:
        raise ValueError(f"bad date {on!r}")
    datetime.strptime(d, "%Y%m%d")
    return d


def _iso(simdate: str) -> str:
    return f"{simdate[:4]}-{simdate[4:6]}-{simdate[6:8]}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                             follow_redirects=True)


# Capture / replay of official pages (scripts/taric_capture.py records them, tests replay the captured files).
_RECORDER: list = []   # callables (url, text) called after every successful fetch
_REPLAY: dict | None = None  # url -> page text; when set, nothing goes to the network


def replay_key(url: str) -> str:
    """Captured-page key: the consultation stamps iframe ids with the time of the request; drop that stamp."""
    return re.sub(r"(FrameId=taric_\d{10})_\d+", r"\1_0", url)


def full_url(path: str, params: dict | None = None) -> str:
    url = path if path.startswith("http") else BASE + path
    return str(httpx.URL(url, params=params)) if params else url


async def _get(path: str, params: dict | None = None, *, client: httpx.AsyncClient | None = None,
               attempts: int = 3) -> str:
    url = full_url(path, params)
    params = None
    if _REPLAY is not None:
        if replay_key(url) in _REPLAY:
            return _REPLAY[replay_key(url)]
        raise TaricUnavailable(f"{url}: not in the captured fixtures")
    own = client is None
    c = client or _client()
    err = None
    try:
        for i in range(attempts):
            try:
                async with _sem():
                    r = await c.get(url, params=params)
                if r.status_code == 200 and r.text:
                    for rec in _RECORDER:
                        rec(url, r.text)
                    return r.text
                err = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    break
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * (i + 1))
    finally:
        if own:
            await c.aclose()
    raise TaricUnavailable(f"{url}: {err}")


def consultation_url(code: str, country: str | None, simdate: str) -> str:
    q = f"Lang=en&SimDate={simdate}&Taric={code}&Expand=true"
    if country:
        q += f"&Area={country}"
    return BASE + "measures.jsp?" + q


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------
def _txt(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.text_content().replace("\xa0", " ")).strip()


def _doc(text: str):
    return lxml.html.fromstring(text)


def parse_detail_urls(page: str) -> list[dict]:
    """Declarable goods codes under the requested code, each with its official measures_details URL."""
    out, seen = [], set()
    for raw in re.findall(r"measures_details\.jsp\?[^'\"]*", page):
        url = html_lib.unescape(raw)
        m = re.search(r"FrameId=taric_(\d{10})_", url)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append({"code": m.group(1), "url": BASE + url})
    return out


_DATE = r"(\d{2})-(\d{2})-(\d{4})"


def _dates(s: str) -> tuple[str | None, str | None]:
    m = re.search(rf"\(\s*{_DATE}\s*-\s*(?:{_DATE})?\s*\)", s)
    if not m:
        return None, None
    start = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    end = f"{m.group(6)}-{m.group(5)}-{m.group(4)}" if m.group(4) else None
    return start, end


def parse_geo(label: str) -> dict:
    """'China (CN)' -> CN; 'ERGA OMNES (ERGA OMNES 1011)' -> 1011; 'GSP - General arrangements (SPGL 2020)' -> 2020."""
    label = (label or "").strip()
    m = re.match(r"^(.*)\(([^()]*)\)\s*$", label)
    if not m:
        return {"geo_code": None, "geo_name": label, "geo_group": False}
    name, inner = m.group(1).strip(), m.group(2).strip()
    g = re.search(r"(\d{4})$", inner)
    if g:
        return {"geo_code": g.group(1), "geo_name": name, "geo_group": True, "geo_abbr": inner[: -4].strip() or None}
    return {"geo_code": inner, "geo_name": name, "geo_group": False}


# Duty expressions as printed by the consultation, e.g. "14.00 %", "32.00 EUR / hl", "2.30 % + 4.74 EUR / 100 kg",
# "9.00 % + EA(1) MAX 24.20 % +ADSZ(1)", "12.00 % MIN 1.50 EUR / p/st", "73.18 EurUP / 100 kg".
_AGRI = re.compile(r"\b(EAR|EA|ADSZR|ADSZ|ADFMR|ADFM|ACR|AC)\b")
_PCT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")
_AMT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([A-Za-z]{3,6})(?:\s*/\s*(.+))?$")


def parse_duty(expr: str | None) -> dict | None:
    """Structure a TARIC duty expression. Returns None when the text is empty."""
    raw = re.sub(r"\s+", " ", (expr or "").replace("\xa0", " ")).strip()
    if not raw:
        return None
    out = {"raw": raw, "kind": None, "pct": None, "amount": None, "currency": None, "unit": None, "min": None,
           "max": None, "components": [], "agri_codes": []}
    agri = _AGRI.findall(raw)
    if agri:
        out["agri_codes"] = sorted(set(agri))
    if re.search(r"\bNIHIL\b", raw, re.I):
        out.update(kind="ad_valorem", pct=0.0)
        return out
    # split into segments: base terms, then MIN/MAX clauses
    parts = re.split(r"\s*\b(MIN|MAX)\b\s*", raw)
    base, clauses = parts[0], list(zip(parts[1::2], parts[2::2]))

    def terms(s: str) -> list[dict]:
        res = []
        for sign, tok in re.findall(r"([+-]?)\s*([^+]+?)(?=\s*\+|\s*$)", s.strip()):
            tok = tok.strip()
            if not tok:
                continue
            if _AGRI.match(tok):
                res.append({"type": "agri", "code": _AGRI.match(tok).group(1), "raw": tok})
                continue
            m = _PCT.match(tok)
            if m:
                res.append({"type": "pct", "pct": float(m.group(1))})
                continue
            m = _AMT.match(tok)
            if m:
                cur = m.group(2)
                res.append({"type": "amount", "amount": float(m.group(1)), "currency": cur.upper()[:3] if cur.upper().startswith("EUR") else cur,
                            "unit": _norm_unit(m.group(3)), "unit_raw": (m.group(3) or None), "reference": cur == "EurUP"})
                continue
            res.append({"type": "unknown", "raw": tok})
        return res

    bt = terms(base)
    out["components"] = bt
    for which, s in clauses:
        t = terms(s)
        if t:
            out[which.lower()] = t[0] if len(t) == 1 else {"type": "compound", "terms": t}
    pcts = [t for t in bt if t["type"] == "pct"]
    amts = [t for t in bt if t["type"] == "amount"]
    if pcts:
        out["pct"] = pcts[0]["pct"]
    if amts:
        out["amount"], out["currency"], out["unit"] = amts[0]["amount"], amts[0]["currency"], amts[0]["unit"]
    if agri:
        out["kind"] = "agri_components"
    elif any(t["type"] == "unknown" for t in bt):
        out["kind"] = "unknown"
    elif amts and amts[0].get("reference"):
        out["kind"] = "reference_price"
    elif out["min"] is not None or out["max"] is not None:
        out["kind"] = "mixed_min_max"
    elif pcts and amts:
        out["kind"] = "compound"
    elif amts:
        out["kind"] = "specific"
    elif pcts:
        out["kind"] = "ad_valorem"
    else:
        out["kind"] = "unknown"
    return out


def _norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = re.sub(r"\s+", " ", u).strip()
    return u


def _flow_of(img_title: str) -> str:
    t = (img_title or "").lower()
    if "import" in t and "export" in t:
        return "both"
    if "export" in t:
        return "export"
    if "import" in t:
        return "import"
    return "both"


def parse_measures(page: str, goods_code: str) -> dict:
    """Measures for one declarable code from a measures_details.jsp page."""
    doc = _doc(page)
    footnote_text = {}
    for code, text in re.findall(r'new Footnote\("([A-Z]{2}\d{3,5})",\s*"((?:[^"\\]|\\.)*)"', page):
        try:
            footnote_text[code] = json.loads(f'"{text}"')
        except Exception:  # noqa: BLE001
            footnote_text[code] = text
    root = doc.xpath('//div[contains(@class,"measures_detail")]')
    root = root[0] if root else doc
    measures = []
    geo = {"geo_code": None, "geo_name": None, "geo_group": False}
    for el in root.iter("div"):
        eid = el.get("id") or ""
        if eid == "geographical_area" or "measure_area" in (el.get("class") or ""):
            geo = parse_geo(_txt(el))
            continue
        m = re.fullmatch(r"measure_(\d+)", eid)
        if not m:
            continue
        sid = m.group(1)
        line = el.xpath('.//table[contains(@class,"table_measure_line")]')
        if not line:
            continue
        line = line[0]
        img = line.xpath('.//td[contains(@class,"td_measure_picture")]//img/@title')
        flow = _flow_of(img[0] if img else "")
        desc_td = line.xpath('.//td[contains(@class,"td_measure_description")]')
        desc_td = desc_td[0] if desc_td else None
        cells = desc_td.xpath('./table//td') if desc_td is not None else []
        head = _txt(cells[0]) if cells else _txt(desc_td)
        type_text = re.split(r"\(\s*\d{2}-\d{2}-\d{4}", head)[0].strip().rstrip(":").strip()
        start, end = _dates(head)
        duty_el = desc_td.xpath('.//span[contains(@class,"duty_rate")]') if desc_td is not None else []
        duty_raw = _txt(duty_el[0]) if duty_el else None
        notes = [a.text_content().strip().strip("()") for a in (desc_td.xpath('.//a[contains(@class,"footnote_link")]')
                                                                 if desc_td is not None else [])]
        desc_all = _txt(desc_td)
        on_m = re.search(r"Order number:\s*(\d{6})", desc_all)
        order = on_m.group(1) if on_m else None
        reg_el = line.xpath('.//a[contains(@class,"regulation_link")]')
        regulation = _txt(reg_el[0]) if reg_el else None
        reg_db = line.xpath('.//a[starts-with(@id,"db_regulation_id_")]')
        reg_role = line.xpath('.//a[starts-with(@id,"regulation_role_type_")]')
        excluded = []
        for sp in el.xpath('.//span[contains(@class,"excluded_countries")]'):
            t = _txt(sp).lstrip(",").strip()
            if t and not t.lower().startswith("excluding"):
                g = parse_geo(t)
                excluded.append({"code": g["geo_code"], "name": g["geo_name"]})
        add_codes = []
        for tbl in el.xpath('.//table[contains(@class,"additional_codes_table")]'):
            for tr in tbl.xpath('.//tr'):
                tds = tr.xpath('./td')
                if not tds:
                    continue
                am = re.search(r"Additional Code\s*([A-Z0-9]{4})", _txt(tds[0]))
                if am:
                    desc = _txt(tds[1]) if len(tds) > 1 else ""
                    a_notes = re.findall(r"\(([A-Z]{2}\d{3,5})\)", desc)
                    desc = re.sub(r"\([A-Z]{2}\d{3,5}\)", "", desc).strip()
                    add_codes.append({"code": am.group(1), "description": desc, "footnotes": a_notes,
                                      "residual": _is_residual(am.group(1), desc)})
        cond = re.search(r"measures_conditions\.jsp\?MeasureSid=(\d+)", html_lib.unescape(lxml.html.tostring(el, encoding="unicode")))
        cands = type_candidates(type_text, None if flow == "both" else flow)
        measures.append({
            "sid": sid, "goods_code": goods_code, "flow": flow,
            "type": type_text, "type_code": cands[0] if len(cands) == 1 else None, "type_code_candidates": cands,
            "geo_code": geo["geo_code"], "geo_name": geo["geo_name"], "geo_group": geo["geo_group"],
            "excluded": excluded, "start": start, "end": end,
            "duty_expression": duty_raw, "duty": parse_duty(duty_raw),
            "additional_codes": add_codes, "order_number": order,
            "regulation": regulation, "regulation_id": _txt(reg_db[0]) if reg_db else None,
            "regulation_role": _txt(reg_role[0]) if reg_role else None,
            "footnotes": [{"code": n, "text": footnote_text.get(n)} for n in notes],
            "has_conditions": bool(cond), "conditions": [], "certificates": {},
            "conditions_url": (BASE + f"measures_conditions.jsp?MeasureSid={cond.group(1)}&Lang=en") if cond else None,
        })
    return {"goods_code": goods_code, "measures": measures}


def _is_residual(code: str, desc: str) -> bool:
    d = (desc or "").strip().lower()
    return code.endswith("999") or d in ("other", "all other companies", "others") or d.startswith("all other compan")


def parse_conditions(page: str) -> dict:
    """Condition lines and the official certificate descriptions from measures_conditions.jsp."""
    doc = _doc(page)
    conds = []
    for tr in doc.xpath('//table[contains(@class,"condition_table")]//tr'):
        tds = tr.xpath('./td')
        if len(tds) < 3:
            continue
        code_seq = _txt(tds[0])
        m = re.fullmatch(r"([A-Z])\s*(\d+)", code_seq)
        if not m:
            continue
        cert_el = tds[1].xpath('.//span[contains(@class,"condition_bold")]')
        cert = re.sub(r"\s+", "", _txt(cert_el[0])) if cert_el else None
        cond_text = _txt(tds[1])
        if cert_el:
            cond_text = cond_text.replace(_txt(cert_el[0]), "").strip()
        act_el = tds[2].xpath('.//span[contains(@class,"condition_bold")]')
        act_amount = _txt(act_el[0]) if act_el else None
        act_text = _txt(tds[2])
        if act_amount:
            act_text = act_text.replace(act_amount, "").strip()
        conds.append({"code": m.group(1), "sequence": int(m.group(2)), "condition": cond_text,
                      "certificate": cert if cert and re.fullmatch(r"[A-Z0-9]\d{3}|[A-Z0-9][A-Z0-9]\d{2}", cert) else None,
                      "requirement": cert if cert and not re.fullmatch(r"[A-Z0-9]\d{3}|[A-Z0-9][A-Z0-9]\d{2}", cert) else None,
                      "action": act_text, "action_kind": _action_kind(act_text),
                      "duty_expression": act_amount, "duty": parse_duty(act_amount) if act_amount else None})
    certs = {}
    for tr in doc.xpath('//tr'):
        tds = tr.xpath('./td')
        if len(tds) >= 2 and "condition_table" not in (tr.getparent().get("class") or ""):
            c = _txt(tds[0])
            if re.fullmatch(r"[A-Z0-9]\d{3}|[A-Z0-9][A-Z0-9]\d{2}", c) and _txt(tds[1]):
                certs[c] = _txt(tds[1])
    for c in conds:
        if c["certificate"]:
            c["text"] = certs.get(c["certificate"])
        else:
            c["text"] = None
    return {"conditions": conds, "certificates": certs}


def _action_kind(t: str) -> str:
    s = (t or "").lower()
    if "not allowed" in s or "not authorised" in s or "prohibit" in s:
        return "not_allowed"
    if "measure not applicable" in s:
        return "not_applicable"
    if "apply the amount" in s or "apply the mentioned duty" in s or "apply the difference" in s:
        return "apply_amount"
    if "allowed" in s or "authorised" in s:
        return "allowed"
    if "declared subheading not allowed" in s:
        return "not_allowed"
    return "other"


# ---------------------------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------------------------
def _has(m: dict, codes: set) -> bool:
    return bool(set(m.get("type_code_candidates") or []) & codes) or m.get("type_code") in codes


def _kind(m: dict) -> str:
    t = (m.get("type") or "").lower()
    if _has(m, THIRD_COUNTRY) or t.startswith("third country duty") or t.startswith("non preferential duty under end-use"):
        return "third_country"
    if _has(m, QUOTA) or "tariff quota" in t:
        return "quota"
    if _has(m, PREFERENCE) or t in ("tariff preference", "preference under end-use"):
        return "preference"
    if _has(m, TRADE_DEFENCE) or "anti-dumping" in t or "countervailing" in t:
        return "trade_defence"
    if _has(m, SUSPENSION) or t.startswith("suspension") or "tariff suspension" in t:
        return "suspension"
    if _has(m, SUPPLEMENTARY_UNIT) or t == "supplementary unit":
        return "supplementary_unit"
    if _has(m, ADDITIONAL_DUTY) or "additional dut" in t or "agricultural component" in t:
        return "additional_duty"
    if "low-value consignment" in t:
        return "low_value_consignment"
    if t == "unit price" or "entry price" in t or "standard import value" in t:
        return "reference_price"
    if _has(m, PROHIBITION) or "prohibition" in t:
        return "prohibition"
    if any(w in t for w in ("control", "restriction", "authorization", "authorisation", "surveillance", "licen",
                            "official control", "declaration of subheading", "cbam", "monitoring")):
        return "restriction"
    return "other"


def summarise(measures: list[dict], *, country: str | None) -> dict:
    third = [m for m in measures if m["kind"] == "third_country" and m.get("duty")]
    tc = None
    if third:
        m = sorted(third, key=lambda x: (x.get("type_code") != "103", x.get("start") or ""))[0]
        tc = {"duty_expression": m["duty_expression"], "duty": m["duty"], "type_code": m.get("type_code"),
              "regulation": m.get("regulation"), "start": m.get("start"), "end": m.get("end")}
    prefs = [_brief(m) for m in measures if m["kind"] in ("preference", "quota")]
    td = []
    for m in measures:
        if m["kind"] != "trade_defence":
            continue
        b = _brief(m)
        if m.get("has_conditions") and not m.get("duty"):
            b["conditional"] = True
        td.append(b)
    restr = []
    for m in measures:
        if m["kind"] in ("restriction", "prohibition"):
            b = _brief(m)
            b["certificates"] = m.get("certificates") or {}
            b["conditions"] = m.get("conditions") or []
            restr.append(b)
    sup = next((m for m in measures if m["kind"] == "supplementary_unit" and m.get("duty_expression")), None)
    return {"third_country_duty": tc, "preferences": prefs, "trade_defence": td, "restrictions": restr,
            "supplementary_unit": sup["duty_expression"] if sup else None,
            "other_duties": [_brief(m) for m in measures if m["kind"] in ("additional_duty", "low_value_consignment",
                                                                             "suspension", "reference_price")]}


def _brief(m: dict) -> dict:
    return {k: m.get(k) for k in ("sid", "type", "type_code", "type_code_candidates", "kind", "geo_code", "geo_name",
                                  "excluded", "duty_expression", "duty", "additional_codes", "order_number",
                                  "regulation", "start", "end", "footnotes", "has_conditions", "conditions")}


# ---------------------------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------------------------
def _norm_code(goods_code: str) -> str:
    d = re.sub(r"\D", "", str(goods_code or ""))
    if len(d) < 4 or len(d) > 10:
        raise ValueError(f"goods code must have 4-10 digits, got {goods_code!r}")
    return d.ljust(10, "0")


def _norm_country(country: str | None) -> str | None:
    c = (country or "").strip().upper()
    if not c:
        return None
    if not re.fullmatch(r"[A-Z]{2}|\d{4}", c):
        raise ValueError(f"country must be ISO alpha-2 or a TARIC geo id, got {country!r}")
    return c


async def _fetch(code: str, country: str | None, simdate: str) -> dict:
    """Fetch and parse every measure for the code (all flows) from the live consultation."""
    url = consultation_url(code, country, simdate)
    async with _client() as c:
        await refresh_measure_types(simdate, client=c)
        page = await _get(url, client=c)
        if "TARIC Measure Information" not in page and "measures_details" not in page:
            raise TaricUnavailable(f"{url}: unexpected page")
        subs = parse_detail_urls(page)
        if not subs:
            return {"code": code, "subcodes": [], "pages": {}, "url": url, "unknown_code": True}
        own = [s for s in subs if s["code"] == code]
        targets = own or subs[:25]
        pages = await asyncio.gather(*[_get(s["url"], client=c) for s in targets])
        parsed = {s["code"]: parse_measures(p, s["code"]) for s, p in zip(targets, pages)}
        # conditions of every measure that has them (one page per measure sid, shared between subcodes)
        sids = {}
        for p in parsed.values():
            for m in p["measures"]:
                if m["conditions_url"]:
                    sids[m["sid"]] = m["conditions_url"] + f"&SimDate={simdate}"
        cpages = await asyncio.gather(*[_get(u, client=c) for u in sids.values()], return_exceptions=True)
        conds = {}
        for sid, cp in zip(sids, cpages):
            if not isinstance(cp, str):
                raise TaricUnavailable(f"Conditions for measure {sid} unavailable: {cp}")
            conds[sid] = parse_conditions(cp)
            if not conds[sid].get("conditions"):
                raise TaricUnavailable(f"Conditions for measure {sid}: unexpected empty page")
        for p in parsed.values():
            for m in p["measures"]:
                cinfo = conds.get(m["sid"])
                if cinfo:
                    m["conditions"] = cinfo.get("conditions") or []
                    m["certificates"] = cinfo.get("certificates") or {}
                    if cinfo.get("error"):
                        m["conditions_error"] = cinfo["error"]
                m["kind"] = _kind(m)
                if not m.get("duty") and any(c["code"] == "V" for c in m.get("conditions") or []):
                    # Entry price system: the duty depends on the declared entry price (condition V lines).
                    m["duty"] = {"raw": "Entry price system (TARIC condition V)", "kind": "entry_price", "pct": None,
                                 "amount": None, "currency": None, "unit": None, "min": None, "max": None,
                                 "components": [], "agri_codes": [],
                                 "entry_price_lines": [{"sequence": c["sequence"], "entry_price": c.get("requirement"),
                                                        "duty_expression": c.get("duty_expression")}
                                                       for c in m["conditions"] if c["code"] == "V"]}
                if m["kind"] == "supplementary_unit" and m.get("duty_expression"):
                    m["duty"] = {"raw": m["duty_expression"], "kind": "supplementary_unit", "unit": m["duty_expression"],
                                 "pct": None, "amount": None, "currency": None, "min": None, "max": None,
                                 "components": [], "agri_codes": []}
        return {"code": code, "subcodes": [s["code"] for s in subs], "pages": parsed, "url": url,
                "declarable": bool(own) and len(subs) == 1}


def _for_flow(measures: list[dict], flow: str) -> list[dict]:
    return [m for m in measures if m["flow"] in (flow, "both")]


def _applies_to(m: dict, country: str | None) -> bool:
    if not country:
        return True
    return not any(x.get("code") == country for x in m.get("excluded") or [])


def _assemble(raw: dict, code: str, country: str | None, flow: str, simdate: str, fetched_at: str) -> dict:
    pages = raw.get("pages") or {}
    sub_summ = []
    if code in pages:
        measures = pages[code]["measures"]
    elif pages:
        # Not a declarable code: keep the measures every declarable subcode shares (same measure sid).
        sets = [{m["sid"] for m in p["measures"]} for p in pages.values()]
        common = set.intersection(*sets) if sets else set()
        first = next(iter(pages.values()))["measures"]
        measures = [m for m in first if m["sid"] in common]
        for sc, p in pages.items():
            ms = [m for m in _for_flow(p["measures"], flow) if _applies_to(m, country)]
            s = summarise(ms, country=country)
            sub_summ.append({"code": sc, "third_country_duty": s["third_country_duty"],
                             "trade_defence": len(s["trade_defence"]), "preferences": len(s["preferences"])})
    else:
        measures = []
    measures = [m for m in _for_flow(measures, flow) if _applies_to(m, country)]
    out = {"goods_code": code, "country": country, "flow": flow, "date": _iso(simdate),
           "declarable": raw.get("declarable", False), "subcodes": raw.get("subcodes") or [],
           "subcode_summaries": sub_summ, "known_code": not raw.get("unknown_code"),
           "measures": measures, **summarise(measures, country=country),
           "source": SOURCE, "source_url": raw.get("url"), "fetched_at": fetched_at}
    return out


async def lookup(goods_code: str, *, country: str | None = None, flow: str = "import", on=None,
                 use_cache: bool = True) -> dict:
    """All TARIC measures for a goods code, origin/destination country, flow and date, from the live consultation
    (cached 24 h in `taric_cache`). Raises TaricUnavailable when the site cannot be reached and nothing is cached."""
    if flow not in ("import", "export"):
        raise ValueError("flow must be import or export")
    code = _norm_code(goods_code)
    country = _norm_country(country)
    simdate = _simdate(on)
    key = f"{code}|{country or '-'}|{flow}|{simdate}"
    if use_cache:
        hit = await db.taric_cache.find_one({"key": key, "expires_at": {"$gt": datetime.now(timezone.utc)}}, NO_ID)
        if hit and hit.get("result"):
            res = hit["result"]
            res["cache"] = {"hit": True, "cached_at": hit.get("cached_at")}
            return res
    raw = await _fetch(code, country, simdate)
    fetched_at = now_iso()
    now = datetime.now(timezone.utc)
    result = None
    for fl in ("import", "export"):
        r = _assemble(raw, code, country, fl, simdate, fetched_at)
        k = f"{code}|{country or '-'}|{fl}|{simdate}"
        await db.taric_cache.update_one({"key": k}, {"$set": {
            "key": k, "kind": "measures", "goods_code": code, "country": country, "flow": fl, "date": _iso(simdate),
            "result": r, "cached_at": fetched_at, "expires_at": now + timedelta(hours=CACHE_HOURS)}}, upsert=True)
        if fl == flow:
            result = r
    result["cache"] = {"hit": False, "cached_at": fetched_at}
    return result


async def conditions(measure_sid: str, *, on=None, lang: str = "en") -> dict:
    """Condition lines and official certificate descriptions of one measure in a language (fi for Finnish texts),
    from measures_conditions.jsp. Cached like lookups."""
    sid = re.sub(r"\D", "", str(measure_sid or ""))
    if not sid:
        raise ValueError("measure sid")
    lang = (lang or "en").lower()[:2]
    simdate = _simdate(on)
    key = f"cond|{sid}|{lang}|{simdate}"
    hit = await db.taric_cache.find_one({"key": key, "expires_at": {"$gt": datetime.now(timezone.utc)}}, NO_ID)
    if hit and hit.get("result"):
        return hit["result"]
    url = BASE + f"measures_conditions.jsp?MeasureSid={sid}&Lang={lang}&SimDate={simdate}"
    res = {**parse_conditions(await _get(url)), "source_url": url, "lang": lang}
    await db.taric_cache.update_one({"key": key}, {"$set": {
        "key": key, "kind": "conditions", "result": res, "cached_at": now_iso(),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=CACHE_HOURS)}}, upsert=True)
    return res


# ---------------------------------------------------------------------------------------------
# Duty maths
# ---------------------------------------------------------------------------------------------
TEXTS = {
    "needs_agri_components": {
        "fi": "Tulliin sisältyy maatalousosa (EA) ja/tai sokeri- tai jauholisätulli (ADSZ/ADFM), jotka määräytyvät "
              "tavaran koostumuksen mukaisesta Meursing-lisäkoodista (CN:n kolmannen osan liite 1). Tullia ei arvioida: "
              "ilmoita Meursing-lisäkoodi (7xxx) koostumuksen perusteella.",
        "en": "The duty includes an agricultural component (EA) and/or additional duty on sugar or flour (ADSZ/ADFM) "
              "set by the Meursing additional code for the goods' composition (Annex 1 to Part Three of the Combined "
              "Nomenclature). The duty is not estimated: declare the Meursing additional code (7xxx) from the composition."},
    "entry_price": {
        "fi": "Tuotteeseen sovelletaan tulohintajärjestelmää (CN:n liite 2; komission delegoitu asetus (EU) 2017/891 ja "
              "täytäntöönpanoasetus (EU) 2017/892). Tulli riippuu ilmoitetusta tulohinnasta 100 kg:aa kohden eikä sitä "
              "voida laskea pelkästä tullausarvosta. Tullia ei arvioida.",
        "en": "The goods fall under the entry price system (CN Annex 2; Commission Delegated Regulation (EU) 2017/891 and "
              "Implementing Regulation (EU) 2017/892). The duty depends on the declared entry price per 100 kg and cannot "
              "be calculated from the customs value alone. The duty is not estimated."},
    "needs_quantity": {
        "fi": "Tulli lasketaan määrän mukaan ({unit}); ilmoita tavaraerän määrä tässä yksikössä.",
        "en": "The duty is charged per quantity ({unit}); declare the quantity in this unit."},
    "unknown": {"fi": "TARICin tulliperustetta ei voitu tulkita: {raw}. Tarkista tulli TARICista.",
                "en": "The TARIC duty expression could not be interpreted: {raw}. Check the duty in TARIC."},
}


def _msg(code: str, **kw) -> dict:
    t = TEXTS[code]
    return {"code": code, "fi": t["fi"].format(**kw), "en": t["en"].format(**kw)}


# Quantity for a TARIC measurement unit: (source, divisor). source: net_kg, qty (pieces/pairs/litres...), unit_qty.
def _quantity_for(unit: str | None, *, net_kg, qty, unit_qty, qty_unit=None) -> float | None:
    if unit_qty is not None:
        return float(unit_qty)
    if not unit:
        return 1.0  # amount per declaration item (e.g. the EUR 3 low-value consignment duty)
    u = unit.strip().lower()
    mass = {"kg": 1, "100 kg": 100, "1000 kg": 1000, "tonne": 1000, "t": 1000, "dtn": 100}
    base = re.sub(r"\s*(net|/ ?net drained wt|net drained wt|gross|eda)\b.*$", "", u).strip()
    if base in mass:
        return None if net_kg is None else float(net_kg) / mass[base]
    if qty is None:
        return None
    qu = (qty_unit or "").strip().lower()
    vol = {"hl": 100.0, "l": 1.0, "1000 l": 1000.0}
    if base in vol:
        if qu in ("l", "litre", "liter", "ltr"):
            return float(qty) / vol[base]
        if qu == "hl":
            return float(qty) * 100 / vol[base]
        return None
    count = {"p/st": 1, "pa": 1, "1000 p/st": 1000, "100 p/st": 100, "pce": 1, "items": 1}
    if base in count:
        if qu and qu not in ("p/st", "pcs", "pce", "pc", "pa", "pair", "pairs", "kpl", "items", "item", "st", "ea", "u", "unit", "units"):
            return None
        return float(qty) / count[base]
    return None


def compute_duty(duty: dict | None, customs_value_cents: int | None, net_kg: float | None = None,
                 qty: float | None = None, unit_qty: float | None = None, *, qty_unit: str | None = None) -> dict:
    """Duty in cents for a parsed TARIC duty. Never guesses: entry-price and Meursing duties return a blocking status.
    -> {duty_cents | None, status: ok|needs_quantity|needs_agri_components|entry_price|needs_value|unknown, basis, reason}"""
    if not duty:
        return {"duty_cents": None, "status": "unknown", "basis": None, "reason": _msg("unknown", raw="-")}
    kind = duty.get("kind")
    raw = duty.get("raw")
    if kind == "agri_components" or duty.get("agri_codes"):
        return {"duty_cents": None, "status": "needs_agri_components", "basis": raw, "reason": _msg("needs_agri_components")}
    if kind == "entry_price":
        return {"duty_cents": None, "status": "entry_price", "basis": raw, "reason": _msg("entry_price")}
    if kind in ("unknown", "reference_price") or kind is None:
        return {"duty_cents": None, "status": "unknown", "basis": raw, "reason": _msg("unknown", raw=raw)}

    def term_cents(t: dict) -> tuple[float | None, str | None]:
        if t["type"] == "pct":
            if customs_value_cents is None:
                return None, "needs_value"
            return customs_value_cents * t["pct"] / 100.0, None
        if t["type"] == "amount":
            if t.get("currency") != "EUR":
                return None, "unknown"
            q = _quantity_for(t.get("unit"), net_kg=net_kg, qty=qty, unit_qty=unit_qty, qty_unit=qty_unit)
            if q is None:
                return None, "needs_quantity"
            return t["amount"] * 100.0 * q, None
        if t["type"] == "compound":
            tot = 0.0
            for x in t["terms"]:
                v, s = term_cents(x)
                if s:
                    return None, s
                tot += v
            return tot, None
        return None, "unknown"

    total = 0.0
    for t in duty.get("components") or []:
        v, s = term_cents(t)
        if s:
            unit = t.get("unit") or "-"
            reason = _msg("needs_quantity", unit=unit) if s == "needs_quantity" else (
                _msg("unknown", raw=raw) if s == "unknown" else
                {"code": "needs_value", "fi": "Tullausarvo puuttuu.", "en": "The customs value is missing."})
            return {"duty_cents": None, "status": s, "basis": raw, "reason": reason}
        total += v
    applied = None
    for which in ("min", "max"):
        lim = duty.get(which)
        if lim is None:
            continue
        v, s = term_cents(lim)
        if s:
            unit = lim.get("unit") or "-"
            return {"duty_cents": None, "status": s, "basis": raw,
                    "reason": _msg("needs_quantity", unit=unit) if s == "needs_quantity" else _msg("unknown", raw=raw)}
        if which == "min" and total < v:
            total, applied = v, "min"
        if which == "max" and total > v:
            total, applied = v, "max"
    return {"duty_cents": int(round(total)), "status": "ok", "basis": raw, "limit_applied": applied, "reason": None}


# ---------------------------------------------------------------------------------------------
# Duty for a declaration item
# ---------------------------------------------------------------------------------------------
def item_code(item: dict) -> str | None:
    """The goods code TARIC is asked for: the 10-digit TARIC code, else the 8-digit CN code + "00". An HS6 code alone
    is not enough to know the duty."""
    for k, n in (("taric_code", 10), ("cn_code", 8)):
        d = re.sub(r"\D", "", str(item.get(k) or ""))
        if len(d) >= n:
            return d[:n].ljust(10, "0")
    return None


def _pref_family(preference: str) -> tuple[str, bool, bool]:
    p = re.sub(r"\D", "", str(preference or "100")) or "100"
    first, rest = p[0], p[1:]
    quota = rest == "20" or rest == "23"
    end_use = rest in ("15", "23", "40", "50")
    fam = {"1": "third_country", "2": "preference", "3": "preference", "4": "preference"}.get(first, "third_country")
    return fam, quota, end_use


def _resolve_duty(m: dict, docs: set) -> tuple[dict | None, dict | None]:
    """The duty of a measure; for a conditional measure (duty only in its condition lines) the line whose certificate
    is presented, else the line that needs no certificate (TARIC evaluates condition lines in sequence)."""
    d = m.get("duty")
    if d or not m.get("conditions"):
        return d, None
    lines = [c for c in m["conditions"] if c.get("duty")]
    hit = next((c for c in lines if c.get("certificate") and c["certificate"] in docs), None)
    fallback = next((c for c in lines if not c.get("certificate") and not c.get("requirement")), None)
    use = hit or fallback
    return (use.get("duty") if use else None), use


def _measure_rate(m: dict) -> float | None:
    d = m.get("duty") or {}
    if d.get("kind") == "ad_valorem":
        return d.get("pct")
    return None


async def duty_for_item(item: dict, *, origin: str | None, preference: str = "100", on=None,
                        additional_code: str | None = None, documents: list[str] | None = None) -> dict:
    """The TARIC duty for one declaration item: the measure picked by the preference code (DE 4/17) plus anti-dumping /
    countervailing duty selected by the TARIC additional code (the residual "all other companies" code when the
    producer is not known). -> {rate_pct, duty_cents, measure_type, order_number, trade_defence[], basis, source,
    source_url, fetched_at, status, reasons[]}"""
    code = item_code(item)
    base = {"rate_pct": None, "duty_cents": None, "measure_type": None, "order_number": None, "trade_defence": [],
            "additional_duties": [], "basis": None, "source": "taric", "source_url": None, "fetched_at": None,
            "status": None, "reasons": [], "goods_code": code, "origin": origin, "preference": preference}
    if not code:
        return {**base, "status": "needs_code", "reasons": [{"code": "needs_code", "fi": "CN-koodi puuttuu.",
                                                               "en": "The CN code is missing."}]}
    r = await lookup(code, country=origin, flow="import", on=on)
    base.update(source_url=r["source_url"], fetched_at=r["fetched_at"], taric_date=r["date"])
    if not r.get("known_code"):
        return {**base, "status": "unknown_code", "reasons": [{"code": "unknown_code",
                "fi": f"Koodia {code} ei löydy TARICista päivälle {r['date']}.",
                "en": f"Code {code} is not in TARIC on {r['date']}."}]}
    if not r.get("declarable") and r.get("subcodes") and code not in r["subcodes"]:
        return {**base, "status": "not_declarable", "subcodes": r["subcodes"], "reasons": [{"code": "not_declarable",
                "fi": f"{code} ei ole ilmoituskelpoinen TARIC-koodi; valitse alanimike: {', '.join(r['subcodes'])}.",
                "en": f"{code} is not a declarable TARIC code; choose a subcode: {', '.join(r['subcodes'])}."}]}
    ms = r["measures"]
    fam, quota, end_use = _pref_family(preference)
    chosen = None
    if quota:
        pool = [m for m in ms if m["kind"] == "quota"]
        want = QUOTA_NON_PREF if fam == "third_country" else QUOTA_PREF
        pool = [m for m in pool if _has(m, want)]
        if end_use:
            pool = [m for m in pool if _has(m, {"123", "146"})] or pool
        chosen = pool[0] if pool else None
    elif fam == "preference":
        pool = [m for m in ms if m["kind"] == "preference"]
        if str(preference).startswith("4") and not pool:
            pool = [m for m in ms if _has(m, {"106"})]  # customs-union measure when published
        if end_use:
            pool = [m for m in pool if _has(m, {"145"})] or pool
        else:
            pool = [m for m in pool if _has(m, {"142"})] or pool
        chosen = pool[0] if pool else None
    elif fam == "customs_union":
        pool = [m for m in ms if _has(m, {"106"})]
        chosen = pool[0] if pool else None
    else:
        pool = [m for m in ms if m["kind"] == "third_country"]
        if end_use:
            pool = [m for m in pool if _has(m, {"105"})] or pool
        else:
            pool = [m for m in pool if _has(m, {"103"})] or pool
        chosen = pool[0] if pool else None
    assessment = item.get("origin_assessment") or {}
    if assessment.get("preference_code") == str(preference) and str(preference) != "100" and assessment.get("selected_measure_sid"):
        chosen = next((m for m in pool if m.get("sid") == assessment["selected_measure_sid"]), None)
    if quota and item.get("quota_order_number"):
        chosen = next((m for m in pool if m.get("order_number") == item["quota_order_number"]
                       and (not assessment.get("selected_measure_sid") or m.get("sid") == assessment["selected_measure_sid"])), None)
    if not chosen:
        wanted = {"third_country": "103/105", "preference": "142/145", "customs_union": "106"}[fam] if not quota else \
            ("122/123" if fam == "third_country" else "143/146")
        return {**base, "status": "no_measure", "reasons": [{"code": "no_measure",
                "fi": f"TARICissa ei ole etuuskoodia {preference} vastaavaa toimenpidettä ({wanted}) alkuperämaalle {origin}.",
                "en": f"TARIC has no measure ({wanted}) for preference code {preference} and origin {origin}."}]}
    docs = {str(d).upper() for d in (documents if documents is not None else
                                    [d.get("code") for d in item.get("documents") or [] if d.get("reference")]) if d}
    chosen_duty, chosen_condition = _resolve_duty(chosen, docs)
    calc = compute_duty(chosen_duty, item.get("customs_value_cents"), item.get("net_kg"),
                        item.get("quantity"), item.get("unit_qty"), qty_unit=item.get("unit"))
    reasons = [calc["reason"]] if calc.get("reason") else []
    status = calc["status"]
    # trade defence on top, by additional code
    td_out = []
    td_cents = 0
    td_ok = True
    td_measures = [m for m in ms if m["kind"] == "trade_defence" and _has(m, TRADE_DEFENCE_DUTY)]
    if td_measures:
        want_code = (additional_code or next(iter(item.get("taric_additional_codes") or []), "")).upper() or None
        by_code = {a["code"] for m in td_measures for a in m.get("additional_codes") or []}
        picked = []
        if want_code and want_code in by_code:
            picked = [(want_code, m, False) for m in td_measures
                      if any(a["code"] == want_code for a in m.get("additional_codes") or [])]
            covered = {m.get("type_code") for _, m, _ in picked}
            picked += [(a["code"], m, True) for m in td_measures
                       if m.get("type_code") not in covered
                       for a in m.get("additional_codes") or [] if a.get("residual")]
            picked += [(None, m, False) for m in td_measures if not m.get("additional_codes")]
        else:
            res = [(a["code"], m) for m in td_measures for a in m.get("additional_codes") or [] if a.get("residual")]
            no_code = [m for m in td_measures if not m.get("additional_codes")]
            picked = [(c, m, True) for c, m in res] + [(None, m, False) for m in no_code]
        for acode, m, assumed in picked:
            d, rule = _resolve_duty(m, docs)
            calc_td = compute_duty(d, item.get("customs_value_cents"), item.get("net_kg"), item.get("quantity"),
                                   item.get("unit_qty"), qty_unit=item.get("unit")) if d else \
                {"duty_cents": None, "status": "unknown", "reason": _msg("unknown", raw=m.get("type"))}
            entry = {"additional_code": acode, "additional_code_assumed_residual": assumed,
                     "rate_pct": (d or {}).get("pct") if (d or {}).get("kind") == "ad_valorem" else None,
                     "duty_expression": (d or {}).get("raw"), "duty_cents": calc_td.get("duty_cents"),
                     "status": calc_td["status"], "regulation": m.get("regulation"), "type": m.get("type"),
                     "type_code": m.get("type_code"), "condition": ({"code": rule["code"], "certificate": rule.get("certificate"),
                                                                    "text": rule.get("text")} if rule else None)}
            td_out.append(entry)
            if calc_td.get("duty_cents") is None:
                td_ok = False
                if calc_td.get("reason"):
                    reasons.append(calc_td["reason"])
            else:
                td_cents += calc_td["duty_cents"]
        if any(e["additional_code_assumed_residual"] for e in td_out) and not want_code:
            reasons.append({"code": "td_residual",
                            "fi": "Polkumyyntitulli laskettiin jäännöskoodilla (muut yritykset), koska valmistajan "
                                  "TARIC-lisäkoodia ei ole ilmoitettu.",
                            "en": "Anti-dumping/countervailing duty was taken at the residual additional code (all other "
                                  "companies) because the producer's TARIC additional code was not declared."})
        if not picked:
            td_ok = False
            reasons.append({"code": "unknown", "fi": "Polkumyyntitullin lisäkoodia ei voitu ratkaista.",
                            "en": "No applicable producer or residual trade-defence additional code could be resolved."})
    # unconditional additional duties (e.g. out-of-quota steel duty) unless the quota of the same act is used
    add_out = []
    for m in ms:
        if m["kind"] != "additional_duty":
            continue
        if quota and chosen and m.get("regulation") == chosen.get("regulation"):
            continue  # the out-of-quota duty of the same act does not apply to goods under its quota
        d, rule = _resolve_duty(m, docs)
        if not d:
            continue
        c2 = compute_duty(d, item.get("customs_value_cents"), item.get("net_kg"), item.get("quantity"),
                          item.get("unit_qty"), qty_unit=item.get("unit"))
        add_out.append({"type": m["type"], "type_code": m.get("type_code"),
                        "type_code_candidates": m.get("type_code_candidates"), "duty_expression": d.get("raw"),
                        "rate_pct": d.get("pct") if d.get("kind") == "ad_valorem" else None,
                        "duty_cents": c2.get("duty_cents"), "status": c2["status"], "regulation": m.get("regulation"),
                        "condition": ({"code": rule["code"], "certificate": rule.get("certificate"),
                                       "text": rule.get("text")} if rule else None)})
        if c2.get("duty_cents") is None:
            td_ok = False
            if c2.get("reason"):
                reasons.append(c2["reason"])
        else:
            td_cents += c2["duty_cents"]
    duty_cents = None
    if status == "ok" and td_ok:
        duty_cents = calc["duty_cents"] + td_cents
    elif status == "ok" and not td_ok:
        status = next((e["status"] for e in td_out + add_out if e["status"] != "ok"), "unknown")
    rate = _measure_rate(chosen)
    total_rate = None
    if rate is not None and all(e.get("rate_pct") is not None for e in td_out + add_out):
        total_rate = round(rate + sum(e["rate_pct"] for e in td_out + add_out), 4)
    return {**base, "rate_pct": total_rate, "measure_rate_pct": rate, "duty_cents": duty_cents,
            "measure_duty_cents": calc.get("duty_cents"), "measure_type": chosen.get("type_code"),
            "measure_type_text": chosen.get("type"), "measure_sid": chosen.get("sid"),
            "duty_expression": chosen.get("duty_expression"), "duty": chosen.get("duty"),
            "order_number": chosen.get("order_number"), "regulation": chosen.get("regulation"),
            "geo_code": chosen.get("geo_code"), "geo_name": chosen.get("geo_name"),
            "trade_defence": td_out, "additional_duties": add_out,
            "basis": calc.get("basis"), "status": status, "reasons": reasons,
            "supplementary_unit": r.get("supplementary_unit")}


# ---------------------------------------------------------------------------------------------
# Tariff quotas
# ---------------------------------------------------------------------------------------------
def _num_unit(t: str) -> tuple[float | None, str | None]:
    m = re.match(r"^\s*(-?[\d,.]+)\s*(.*)$", (t or "").replace("\xa0", " "))
    if not m:
        return None, None
    try:
        return float(m.group(1).replace(",", "")), (m.group(2).strip() or None)
    except ValueError:
        return None, None


def _dmy(t: str | None) -> str | None:
    m = re.search(_DATE, t or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_quota_list(page: str) -> list[dict]:
    """Periods of one order number from quota_list.jsp."""
    doc = _doc(page)
    out = []
    for tr in doc.xpath('//tr'):
        cells = {}
        for td in tr.xpath('./td[@data-ecl-table-header]'):
            cells[re.sub(r"\s+", " ", td.get("data-ecl-table-header").replace("&nbsp", "")).strip().lower()] = td
        if "start date" not in cells or "balance" not in cells:
            continue
        link = tr.xpath('.//a[contains(@href,"quota_tariff_details.jsp")]/@href')
        bal, unit = _num_unit(_txt(cells["balance"]))
        out.append({"order_number": _txt(cells.get("order number")) or None,
                    "origins": _txt(cells.get("origins")) or None,
                    "start": _dmy(_txt(cells["start date"])), "end": _dmy(_txt(cells.get("end date"))),
                    "balance": bal, "unit": unit, "details_url": html_lib.unescape(link[0]).strip() if link else None})
    return out


def parse_quota_details(page: str) -> dict:
    """Label/value table of quota_tariff_details.jsp."""
    i = page.find('id="quotaDetailsMarkedUpContainer"')
    frag = page[page.rfind("<div", 0, i):] if i >= 0 else page
    j = frag.find('class="top-link"')
    frag = frag[:j] if j > 0 else frag
    doc = lxml.html.fromstring(frag)
    out = {}
    for tr in doc.xpath('//tr[contains(@class,"ecl-table__row")]'):
        tds = tr.xpath('./td')
        if len(tds) < 2 or "label" not in (tds[0].get("class") or ""):
            continue
        label = re.sub(r"\s+", " ", _txt(tds[0])).strip()
        if label.lower().startswith("associated taric code"):
            out[label] = [re.sub(r"\s+", "", _txt(a)) for a in tds[1].xpath('.//a')]
        else:
            out[label] = _txt(tds[1])
    return out


async def quota(order_number: str, on=None) -> dict:
    """Tariff-quota situation from the EU tariff quota consultation (QUOTA): the period that contains the date,
    its balance, status (open | exhausted | critical | blocked | suspended | no_period), and the official details."""
    code = re.sub(r"\D", "", str(order_number or ""))
    if len(code) != 6:
        raise ValueError("a tariff quota order number has 6 digits")
    simdate = _simdate(on)
    day = _iso(simdate)
    list_url = full_url("quota_list.jsp", {"Lang": "en", "Code": code, "Year": simdate[:4], "Critical": "", "Status": "",
                                           "Origin": "", "Expand": "true", "Offset": "0"})
    periods = parse_quota_list(await _get(list_url))
    cur = next((p for p in periods if p["start"] and p["start"] <= day and (not p["end"] or day <= p["end"])), None)
    out = {"order_number": code, "date": day, "periods": periods, "period": None, "status": "no_period",
           "balance": None, "unit": None, "initial_amount": None, "critical": None, "exhaustion_date": None,
           "associated_codes": [], "details": {}, "source": "EU tariff quota consultation (QUOTA)",
           "source_url": list_url, "fetched_at": now_iso()}
    if not cur:
        return out
    det = parse_quota_details(await _get(cur["details_url"])) if cur.get("details_url") else {}
    bal, unit = _num_unit(det.get("Balance", "")) if det.get("Balance") else (cur["balance"], cur["unit"])
    init, _u = _num_unit(det.get("Initial amount", ""))
    critical = (det.get("Critical") or "").strip().lower() == "yes"
    exhausted_on = _dmy(det.get("Exhaustion date"))
    blocking = det.get("Blocking period") or ""
    suspension = det.get("Suspension period") or ""
    status = "open"
    if bal is not None and bal <= 0 or (exhausted_on and exhausted_on <= day):
        status = "exhausted"
    elif blocking.strip():
        status = "blocked"
    elif suspension.strip():
        status = "suspended"
    elif critical:
        status = "critical"
    out.update(period={"start": cur["start"], "end": cur["end"], "origins": cur["origins"]}, status=status,
               balance=bal, unit=unit or cur["unit"], initial_amount=init, critical=critical,
               exhaustion_date=exhausted_on, associated_codes=det.get("Associated TARIC code") or [],
               details={k: v for k, v in det.items() if k != "Associated TARIC code"},
               details_url=cur.get("details_url"))
    return out


# ---------------------------------------------------------------------------------------------
# Declaration items (services.customs.recompute / validate)
# ---------------------------------------------------------------------------------------------
async def quota_cached(order_number: str, on=None) -> dict:
    """quota() cached for an hour in taric_cache (balances move during the day)."""
    simdate = _simdate(on)
    key = f"quota|{order_number}|{simdate}"
    hit = await db.taric_cache.find_one({"key": key, "expires_at": {"$gt": datetime.now(timezone.utc)}}, NO_ID)
    if hit and hit.get("result"):
        return hit["result"]
    q = await quota(order_number, on=on)
    await db.taric_cache.update_one({"key": key}, {"$set": {
        "key": key, "kind": "quota", "result": q, "cached_at": now_iso(),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}}, upsert=True)
    return q


_KEEP = ("status", "rate_pct", "measure_rate_pct", "duty_cents", "measure_duty_cents", "measure_type", "measure_type_text",
         "duty_expression", "order_number", "regulation", "geo_code", "geo_name", "trade_defence", "additional_duties",
         "basis", "reasons", "source", "source_url", "fetched_at", "taric_date", "goods_code", "subcodes",
         "supplementary_unit", "preference", "origin")


async def apply_to_item(it: dict, *, on=None, timeout: float = 40.0) -> dict:
    """Set an import declaration item's duty from TARIC (duty_source "taric"). When TARIC cannot be reached the AI
    estimate of the HS agent stays as a fallback marked "ai_estimate" and it["taric"]["status"] = "unavailable"
    (rule TARIC-1 blocks filing). Official measures supersede a manually entered percentage too."""
    edited = set(it.get("edited") or [])
    if not item_code(it):
        it["taric"] = {"status": "needs_code", "at": now_iso(), "reasons": [{
            "code": "needs_code", "fi": "Valitse 8-numeroinen CN-koodi tai 10-numeroinen TARIC-koodi.",
            "en": "Choose the 8-digit CN code or the 10-digit TARIC code."}]}
        return it["taric"]
    add = [c for c in (it.get("taric_additional_codes") or []) if c]
    docs = [d.get("code") for d in it.get("documents") or [] if d.get("code")]
    try:
        res = await asyncio.wait_for(duty_for_item(it, origin=it.get("origin_country"), preference=it.get("preference") or "100",
                                                   on=on, additional_code=add[0] if add else None, documents=docs), timeout)
    except (TaricUnavailable, asyncio.TimeoutError, ValueError) as e:
        it["taric"] = {"status": "unavailable", "error": f"{type(e).__name__}: {e}"[:300], "at": now_iso()}
        if "duty_rate_pct" not in edited:
            ai = ((it.get("hs_suggestion") or {}).get("duty") or {})
            it["duty_rate_pct"] = ai.get("rate_pct")
            it["duty_source"] = "ai_estimate" if ai.get("rate_pct") is not None else None
        return it["taric"]
    t = {k: res.get(k) for k in _KEEP}
    t["at"] = now_iso()
    if res.get("order_number"):
        try:
            q = await quota_cached(res["order_number"], on=on)
            t["quota"] = {k: q.get(k) for k in ("status", "balance", "unit", "period", "critical", "exhaustion_date",
                                                "source_url")}
        except (TaricUnavailable, ValueError) as e:
            t["quota"] = {"status": "unavailable", "error": str(e)[:200]}
    it["taric"] = t
    it["duty_rate_pct"] = res.get("rate_pct")
    it["duty_source"] = "taric"
    it["duty_label"] = (res.get("measure_type_text") or "") + (f" {res['duty_expression']}" if res.get("duty_expression") else "")
    it["taric_url"] = res.get("source_url") or it.get("taric_url")
    # TARIC's own anti-dumping / countervailing measures replace the offline advisory list of services.hs
    it["flags"] = [f for f in (it.get("flags") or []) if f.get("kind") != "trade_defence"]
    return t


def item_duty_cents(it: dict) -> int | None:
    """Duty for recompute: TARIC's own amount when TARIC answered (handles specific/compound duties and trade
    defence); None when TARIC did not give one."""
    t = it.get("taric") or {}
    if it.get("duty_source") == "taric" and t.get("status") == "ok":
        return t.get("duty_cents")
    return None


_BLOCKING_STATUS = {"needs_quantity", "needs_agri_components", "entry_price", "not_declarable", "no_measure",
                    "unknown_code", "unknown", "needs_value"}


def rules(decl: dict) -> list[dict]:
    """TARIC-1 (TARIC unreachable: filing waits), TARIC-2 (TARIC answered but the duty needs a person: entry price,
    Meursing code, quantity, TARIC subcode, no measure for the preference), TD-1 (producer's TARIC additional code for
    anti-dumping/countervailing measures), QUOTA-1 (tariff quota exhausted / blocked / critical)."""
    out = []
    for it in decl.get("items") or []:
        n = it.get("seq")
        t = it.get("taric") or {}
        st = t.get("status")
        if st in (None, "unavailable", "needs_code"):
            en = ("The EU TARIC consultation could not be reached; the duty shown is an AI estimate. Filing waits until "
                  f"TARIC answers ({t.get('error') or 'no answer'}).")
            fi = ("EU:n TARIC-palveluun ei saatu yhteyttä; näytetty tulli on tekoälyn arvio. Ilmoitusta ei lähetetä "
                  "ennen kuin TARIC vastaa.")
            out.append(_rule("TARIC-1", False, en, fi, item=n))
            continue
        out.append(_rule("TARIC-1", True, f"Duty from the EU TARIC consultation on {t.get('taric_date')}: "
                                          f"{t.get('measure_type_text') or ''} {t.get('duty_expression') or ''}".strip(),
                         f"Tulli EU:n TARIC-palvelusta {t.get('taric_date')}: {t.get('duty_expression') or ''}".strip(),
                         item=n, severity="advisory"))
        if st in _BLOCKING_STATUS or st == "needs_code":
            rs = t.get("reasons") or []
            en = " ".join(r.get("en", "") for r in rs) or f"TARIC duty status: {st}"
            fi = " ".join(r.get("fi", "") for r in rs) or f"TARIC-tullin tila: {st}"
            out.append(_rule("TARIC-2", False, en, fi, item=n))
        td = t.get("trade_defence") or []
        if td and (not (it.get("taric_additional_codes") or []) or any(
                e.get("additional_code_assumed_residual") and e.get("additional_code") not in it.get("taric_additional_codes", [])
                for e in td)):
            codes = sorted({e.get("additional_code") for e in td if e.get("additional_code")})
            regs = sorted({e.get("regulation") for e in td if e.get("regulation")})
            en = (f"Anti-dumping/countervailing measures ({', '.join(regs)}) apply to this origin. The duty includes the "
                  f"residual rate (additional code {', '.join(codes) or '-'}); enter the producer's TARIC additional code "
                  "(DE 6/16) or confirm the residual code.")
            fi = (f"Alkuperämaahan kohdistuu polkumyynti- tai tasoitustulli ({', '.join(regs)}). Tulliin sisältyy "
                  f"jäännöskoodin tulli (lisäkoodi {', '.join(codes) or '-'}); ilmoita valmistajan TARIC-lisäkoodi "
                  "(DE 6/16) tai vahvista jäännöskoodi.")
            out.append(_rule("TD-1", False, en, fi, item=n, field="taric_additional_codes"))
        q = t.get("quota") or {}
        if t.get("order_number") and q:
            qs = q.get("status")
            if qs in ("exhausted", "blocked", "suspended", "no_period", "unavailable"):
                en = (f"Tariff quota {t['order_number']} is {qs.replace('_', ' ')} (EU quota consultation, balance "
                      f"{q.get('balance')} {q.get('unit') or ''}). Declare without the quota (preference 100) or "
                      "confirm the allocation.").replace("  ", " ")
                fi = (f"Tariffikiintiö {t['order_number']}: tila {qs} (saldo {q.get('balance')} {q.get('unit') or ''}). "
                      "Ilmoita ilman kiintiötä (etuuskoodi 100) tai vahvista jako.").replace("  ", " ")
                out.append(_rule("QUOTA-1", False, en, fi, item=n))
            elif qs == "critical":
                out.append(_rule("QUOTA-1", False, f"Tariff quota {t['order_number']} is critical (balance "
                                                   f"{q.get('balance')} {q.get('unit') or ''}).",
                                 f"Tariffikiintiö {t['order_number']} on kriittinen.", item=n, severity="advisory"))
    return out


def _rule(rule, ok, en, fi=None, *, item=None, severity="blocking", owner="broker", field=None):
    return {"rule": rule, "ok": bool(ok), "severity": severity, "owner": owner, "message": en, "item": item,
            "field": field, "message_i18n": {"en": en, "fi": fi or en}}


# ---------------------------------------------------------------------------------------------
# Daily refresh (s2 durable jobs)
# ---------------------------------------------------------------------------------------------
OPEN_STATUSES = ["draft", "needs_info", "ready_for_review", "approved", "rejected"]


async def refresh_open(on=None) -> dict:
    """Re-fetch TARIC for open import and export declarations, bypassing the cache."""
    seen, done, failed = set(), 0, []
    for collection, flow in (("customs_declarations", "import"), ("export_declarations", "export")):
        async for d in db[collection].find({"status": {"$in": OPEN_STATUSES}},
                                         {"_id": 0, "items": 1, "destination_country": 1}):
            for it in d.get("items") or []:
                code = item_code(it)
                if not code:
                    continue
                country = it.get("origin_country") if flow == "import" else d.get("destination_country")
                k = (code, (country or "").upper() or None, flow)
                if k in seen:
                    continue
                seen.add(k)
                try:
                    await lookup(code, country=k[1], flow=flow, on=on, use_cache=False)
                    done += 1
                except (TaricUnavailable, ValueError) as e:
                    failed.append({"code": code, "country": k[1], "flow": flow, "error": str(e)[:200]})
    await refresh_measure_types(on)
    return {"codes": len(seen), "refreshed": done, "failed": failed[:50]}


try:  # s2 durable jobs (feat/customs is based on s2)
    from core.jobs import job as _job, schedule as _schedule

    @_job("taric.refresh", max_attempts=4, backoff=(600, 1800, 3600), timeout=1800, agent=True,
          label_fi="TARIC-tietojen päivitys", label_en="TARIC refresh")
    async def _refresh_job(j: dict) -> dict:
        return await refresh_open((j.get("payload") or {}).get("on"))

    _schedule("taric.refresh_daily", job="taric.refresh", daily_at="05:15",
              label_fi="TARIC-tietojen päivitys (päivittäin)", label_en="TARIC refresh (daily)")()
except ImportError:  # pragma: no cover
    pass
