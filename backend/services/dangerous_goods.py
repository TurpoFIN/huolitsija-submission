"""Dangerous goods (vaaralliset aineet): UN-number lookup and cargo checks at quote and at booking (s12).

Data: backend/data/dg/un_numbers.json + segregation.json, built by scripts/build_dg_data.py from official sources
(SOURCES.md has URL, retrieval date and sha256 of each):
- US 49 CFR 172.101 Hazardous Materials Table (eCFR, public domain): name, class/division, label codes, packing group,
  special provisions, column 9A passenger aircraft / 9B cargo aircraft only (per package), 10A/10B vessel stowage.
- 49 CFR 176.83(b) General Segregation Table (mirrors IMDG Code 7.2.4) and 176.84 stowage codes.
- ADR 2025 Table A (UNECE ECE/TRANS/352): classification code, transport category, tunnel restriction code, limited
  quantity, hazard identification number; ADR 8.6.4 for what a tunnel code forbids.
Not shipped: IMDG EmS (IMO copyright) and IATA DGR tables (licensed). Air limits follow 49 CFR 9A/9B (aligned with
ICAO TI); IATA_DGR_DATA_PATH pointing to a licensed table switches the air limits to it (stub until a licence exists).

check_cargo(cargo, mode, legs) is a pure function: the same input and dataset give the same findings, so a decision can
be replayed for an audit. Severities: info (shown), warn (a person reviews: status "review"), block (status
"forbidden"). Classes 1 and 7 always go to review (licences, competent-authority approvals). It runs even while the
autopilot kill switch is on: it is a safety gate, not an autonomous action. The callers record the result with
record_check() (audit row, actor agent "compliance").
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dg")
TUNNEL_ORDER = ["B", "B1000C", "B/D", "B/E", "C", "C5000D", "C/D", "C/E", "D", "D/E", "E", "-"]  # ADR 8.6.4, strictest first
SEG_RANK = {"4": 4, "3": 3, "2": 2, "1": 1}
SEG_FI = {"1": "erillään (away from)", "2": "eristettynä (separated from)",
          "3": "eri lastiruumassa tai -osastossa (separated by a complete compartment or hold from)",
          "4": "pituussuunnassa erillään, välissä kokonainen lastiruuma (separated longitudinally by an intervening "
               "complete compartment or hold from)"}
PASSENGER_VESSEL_LIMITED = {"D", "E", "03", "04", "05"}  # 172.101(k): prohibited on (larger) passenger vessels
UNIT_ALIASES = {"l": ("L", 1.0), "litre": ("L", 1.0), "liter": ("L", 1.0), "ltr": ("L", 1.0), "ml": ("L", 0.001),
                "kg": ("kg", 1.0), "g": ("kg", 0.001), "kgs": ("kg", 1.0)}


# ------------------------------------------------------------------------------------------------
# Dataset
# ------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _data() -> dict:
    with open(os.path.join(DATA_DIR, "un_numbers.json"), encoding="utf-8") as f:
        un = json.load(f)
    with open(os.path.join(DATA_DIR, "segregation.json"), encoding="utf-8") as f:
        seg = json.load(f)
    by_un: dict[str, list[dict]] = {}
    for r in un["records"]:
        by_un.setdefault(r["un"], []).append(r)
    return {"meta": un["meta"], "by_un": by_un, "seg": seg}


@lru_cache(maxsize=1)
def _iata_table() -> dict | None:
    """Licensed IATA DGR air limits (stub: no licence yet). JSON {"UN3480|": {"passenger": "Forbidden", "cargo": "35 kg"}}."""
    path = os.environ.get("IATA_DGR_DATA_PATH")
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dataset_info() -> dict:
    m = _data()["meta"]
    return {"counts": m["counts"], "sources": _sources(), "not_included": m["not_included"],
            "air_source": "IATA DGR (licensed table)" if _iata_table() else "49 CFR 172.101 columns 9A/9B"}


def _sources() -> list[dict]:
    b = _data()["meta"]["built_from"]
    labels = {"49cfr_172_101": "49 CFR 172.101 Hazardous Materials Table", "49cfr_176_83": "49 CFR 176.83 Segregation",
              "49cfr_176_84": "49 CFR 176.84 Stowage codes", "adr_2025": "ADR 2025 Table A (UNECE)"}
    return [{"key": k, "title": labels.get(k, k), "url": s["url"], "sha256": s["sha256"], "retrieved_at": s["retrieved_at"],
             "licence": s["licence"], **({"ecfr_date": s["ecfr_date"]} if s.get("ecfr_date") else {})} for k, s in sorted(b.items())]


def norm_un(value) -> str | None:
    s = re.sub(r"[\s\-]+", "", str(value or "").upper())
    if re.fullmatch(r"\d{4}", s):
        s = f"UN{s}"
    return s if re.fullmatch(r"(UN|NA|ID)\d{4}", s) else None


def _hmt_entries(recs: list[dict]) -> list[tuple[str | None, dict]]:
    """49 CFR entries for international use: drop "D" (US domestic only) entries when another entry exists."""
    rows = [(r["pg"], h) for r in recs for h in r["hmt"]]
    intl = [(pg, h) for pg, h in rows if "D" not in h["symbols"]]
    return intl or rows


def _qty_view(q: dict) -> dict:
    return {"text": q.get("text"), "value": q.get("value"), "unit": q.get("unit"), "gross": q.get("gross"),
            "forbidden": bool(q.get("forbidden")), "no_limit": bool(q.get("no_limit"))}


def _air(un: str, pg: str | None, h: dict) -> dict:
    p, c, src = h["air"]["passenger"], h["air"]["cargo"], "49 CFR 172.101 9A/9B"
    iata = _iata_table()
    if iata and f"{un}|{pg or ''}" in iata:
        from_iata = iata[f"{un}|{pg or ''}"]
        p, c, src = _parse_qty(from_iata.get("passenger")), _parse_qty(from_iata.get("cargo")), "IATA DGR (licensed)"
    return {"passenger_limit": p.get("text"), "cargo_limit": c.get("text"), "passenger_forbidden": bool(p.get("forbidden")),
            "cargo_forbidden": bool(c.get("forbidden")), "passenger": _qty_view(p), "cargo": _qty_view(c), "source": src}


def _parse_qty(text) -> dict:
    t = str(text or "").strip()
    out = {"text": t or None, "forbidden": t.lower() == "forbidden", "no_limit": t.lower() == "no limit",
           "value": None, "unit": None, "gross": "gross" in t.lower()}
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(L|kg|mL|g)(?: gross)?(?: or .*)?", t)
    if m:
        v, u = float(m.group(1)), m.group(2)
        out["value"], out["unit"] = (v / 1000, "L") if u == "mL" else ((v / 1000, "kg") if u == "g" else (v, u))
    return out


def _adr_view(a: dict) -> dict:
    return {"name": a["name"], "class": a["class"], "classification_code": a["classification_code"], "labels": a["labels"],
            "tunnel_code": a["tunnel_code"], "transport_category": a["transport_category"],
            "limited_quantity": a["limited_quantity"], "excepted_quantity": a["excepted_quantity"],
            "hazard_id": a["hazard_id"], "special_provisions": a["special_provisions"],
            "carriage_prohibited": a["carriage_prohibited"], "not_subject_to_adr": a["not_subject_to_adr"],
            "source_page": a["page"]}


def strictest_tunnel(codes) -> str | None:
    known = [c for c in codes if c in TUNNEL_ORDER]
    return min(known, key=TUNNEL_ORDER.index) if known else None


def lookup(un_number) -> dict | None:
    """Merged record for a UN number: names, class, subsidiary hazards, packing groups, per-PG variants (air/vessel/ADR)."""
    un = norm_un(un_number)
    recs = _data()["by_un"].get(un or "")
    if not recs:
        return None
    seg = _data()["seg"]
    hmt = _hmt_entries(recs)
    adr_rows = [(r["pg"], a) for r in recs for a in r["adr"]]
    names = []
    for _, h in hmt:
        names.append(h["name"])
    for _, a in adr_rows:
        names.append(a["name"])
    names = list(dict.fromkeys(n for n in names if n))
    divisions = list(dict.fromkeys(h["division"] for _, h in hmt))
    cls = divisions[0] if divisions else _adr_division(adr_rows[0][1])
    subsidiary = list(dict.fromkeys(s for _, h in hmt for s in h["subsidiary"])) or \
        list(dict.fromkeys(lab for _, a in adr_rows for lab in a["labels"][1:]))
    pgs = sorted({pg for pg, _ in hmt + adr_rows if pg}, key=lambda p: len(p))
    variants = []
    for pg, h in hmt:
        stow = h["vessel"]["stowage"]
        variants.append({
            "pg": pg, "name": h["name"], "description": h["description"], "division": h["division"],
            "subsidiary": h["subsidiary"], "labels": h["labels"], "special_provisions": h["special_provisions"],
            "domestic_only": "D" in h["symbols"], "air": _air(un, pg, h),
            "vessel": {"stowage": stow, "stowage_text": seg["stowage_categories"].get(stow or ""),
                       "other": [{"code": c, "text": seg["stowage_codes"].get(c)} for c in h["vessel"]["other"]]},
            "adr": [_adr_view(a) for apg, a in adr_rows if (apg or None) == (pg or None)]})
    for pg, a in adr_rows:  # ADR-only packing groups (not in the US table)
        if not any((v["pg"] or None) == (pg or None) for v in variants):
            variants.append({"pg": pg, "name": a["name"], "description": a["name"], "division": _adr_division(a),
                             "subsidiary": a["labels"][1:], "labels": a["labels"], "special_provisions": a["special_provisions"],
                             "domestic_only": False, "air": None, "vessel": None, "adr": [_adr_view(a)]})
    adr_all = [a for _, a in adr_rows]
    first = adr_all[0] if adr_all else {}
    adr = {"tunnel_code": strictest_tunnel(a["tunnel_code"] for a in adr_all),
           "tunnel_codes": list(dict.fromkeys(a["tunnel_code"] for a in adr_all if a["tunnel_code"])),
           "transport_category": first.get("transport_category"), "classification_code": first.get("classification_code"),
           "limited_quantity": first.get("limited_quantity"), "hazard_id": first.get("hazard_id"),
           "carriage_prohibited": any(a["carriage_prohibited"] for a in adr_all),
           "not_subject_to_adr": bool(adr_all) and all(a["not_subject_to_adr"] for a in adr_all)} if adr_all else None
    srcs = [s for s in _sources() if (s["key"] == "adr_2025" and adr_all) or (s["key"] == "49cfr_172_101" and hmt)]
    return {"un": un, "names": names, "name": names[0] if names else None, "class": cls, "class_main": (cls or "").split(".")[0],
            "divisions": divisions, "subsidiary": subsidiary, "packing_groups": pgs, "variants": variants, "adr": adr,
            "imdg": {"segregation_group": seg_group(cls), "ems": None,
                     "note": "Segregation from 49 CFR 176.83 (IMDG 7.2.4). EmS is IMO copyright and not included."},
            "sources": srcs}


def _adr_division(a: dict) -> str | None:
    if a.get("class") == "1" and a.get("classification_code"):
        return a["classification_code"]
    if a.get("class") == "2" and a.get("labels"):
        return a["labels"][0]
    return a.get("class")


def search(q: str, limit: int = 20) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    un = norm_un(q)
    digits = re.sub(r"^UN", "", re.sub(r"\s", "", q.upper()))
    digits = digits if digits.isdigit() else ""
    out, ql = [], q.lower()
    for key in sorted(_data()["by_un"]):
        recs = _data()["by_un"][key]
        names = [h["name"] for r in recs for h in r["hmt"]] + [a["name"] for r in recs for a in r["adr"]]
        hit = (un and key == un) or (digits and key.startswith("UN") and key[2:].startswith(digits)) \
            or (not digits and any(ql in (n or "").lower() for n in names))
        if hit:
            rec = lookup(key)
            out.append({"un": key, "name": rec["name"], "class": rec["class"], "subsidiary": rec["subsidiary"],
                        "packing_groups": rec["packing_groups"], "tunnel_code": (rec["adr"] or {}).get("tunnel_code")})
            if len(out) >= limit:
                break
    out.sort(key=lambda r: (r["un"] != un, r["un"]))
    return out


# ------------------------------------------------------------------------------------------------
# Segregation (49 CFR 176.83(b) / IMDG 7.2.4)
# ------------------------------------------------------------------------------------------------
def seg_group(division: str | None) -> str | None:
    if not division:
        return None
    base = re.sub(r"[A-Z]$", "", str(division).strip().upper())
    if base in ("1", "1.1", "1.2", "1.5"):
        return "1.1, 1.2, 1.5"
    if base in ("1.4", "1.6"):
        return "1.4, 1.6"
    cols = _data()["seg"]["columns"]
    return base if base in cols else None


def _groups_for(item: dict) -> list[str]:
    """Primary + subsidiary hazards (176.83(a)(6)); an explosive subsidiary counts as 1.4 (organic peroxides: 1.3)."""
    out = [seg_group(item["division"])]
    for s in item.get("subsidiary") or []:
        if s.split(".")[0] == "1":
            out.append("1.3" if item["division"].startswith("5.2") else "1.4, 1.6")
        else:
            out.append(seg_group(s))
    return [g for g in dict.fromkeys(out) if g]


def segregation_between(a: dict, b: dict) -> dict:
    """Most restrictive general segregation code between two checked items -> {code, term, term_fi}."""
    seg = _data()["seg"]
    if a["division"].split(".")[0] == b["division"].split(".")[0] and a["division"][0] != "1":
        return {"code": "X", "term": "No general segregation within the same class (176.83(a)(8))", "term_fi": "Ei yleistä erottelua saman luokan sisällä"}
    best = "X"
    for ga in _groups_for(a):
        for gb in _groups_for(b):
            code = seg["matrix"].get(ga, {}).get(gb, "X")
            if code == "(*)" or code == "*":
                if best not in SEG_RANK:
                    best = "*"
                continue
            if SEG_RANK.get(code, 0) > SEG_RANK.get(best, 0):
                best = code
    legend = seg["legend"]
    return {"code": best, "term": legend.get(best) or legend.get("X"), "term_fi": SEG_FI.get(best, "")}


# ------------------------------------------------------------------------------------------------
# Cargo check
# ------------------------------------------------------------------------------------------------
def _f(code: str, severity: str, fi: str, en: str, **data) -> dict:
    return {"code": code, "severity": severity, "fi": fi, "en": en, **({"data": data} if data else {})}


def cargo_items(cargo: dict | None) -> list[dict]:
    """DG lines of a cargo dict: cargo["dg_items"] (list) or the single cargo["dg"] when dangerous_goods is set."""
    c = cargo or {}
    items = [dict(x) for x in c.get("dg_items") or [] if isinstance(x, dict)]
    if not items and c.get("dangerous_goods") and isinstance(c.get("dg"), dict):
        items = [dict(c["dg"])]
    return items


def _declared_class(it: dict) -> str:
    return str(it.get("class") or it.get("dg_class") or "").strip().upper()


def _class_match(declared: str, divisions: list[str]) -> str:
    """'exact' | 'partial' (only the class digit given) | 'mismatch'."""
    for div in divisions:
        base = re.sub(r"[A-Z]$", "", div)
        if declared in (div, base):
            return "exact"
    for div in divisions:
        if declared == div.split(".")[0]:
            return "partial"
    return "mismatch"


def _pick_variant(rec: dict, pg: str | None, psn: str) -> dict | None:
    vs = [v for v in rec["variants"] if (v["pg"] or None) == (pg or None)] or \
        ([v for v in rec["variants"] if not v["pg"]] if not pg else [])
    if not vs and len({v["pg"] for v in rec["variants"]}) == 1:
        vs = rec["variants"]
    vs = [v for v in vs if not v["domestic_only"]] or vs
    if psn:
        named = [v for v in vs if v["name"] and (v["name"].lower() in psn.lower() or psn.lower() in v["name"].lower())]
        if named:
            return named[0]
    with_air = [v for v in vs if v["air"]]
    if len(with_air) > 1:  # most restrictive passenger limit first (forbidden < small < no limit)
        def key(v):
            p = v["air"]["passenger"]
            return (0 if p["forbidden"] else 1, p["value"] if p["value"] is not None else 1e9)
        return sorted(with_air, key=key)[0]
    return (with_air or vs or [None])[0]


def _qty(it: dict) -> tuple[float | None, str | None]:
    v = it.get("net_quantity", it.get("quantity_per_package"))
    if v in (None, ""):
        return None, None
    try:
        val = float(str(v).replace(",", "."))
    except ValueError:
        return None, None
    unit = str(it.get("unit") or it.get("net_quantity_unit") or "").strip().lower()
    if unit not in UNIT_ALIASES:
        return val, None
    u, f = UNIT_ALIASES[unit]
    return val * f, u


def _modes(mode: str | None, legs: list | None) -> list[str]:
    ms = [m for m in [mode] + [(lg or {}).get("mode") for lg in legs or []] if m]
    return list(dict.fromkeys(m.lower() for m in ms))


def _tunnel_explain(code: str, tank: bool) -> tuple[str, str, list[str]]:
    rule = (_data()["seg"]["tunnel_restrictions"] or {}).get(code)
    if not rule:
        return "", "", []
    if code in ("B1000C", "C5000D"):  # the axis is the net explosive mass, not tank / other carriage
        heavy, light, lim = ", ".join(rule["tank"]), ", ".join(rule["other"]), code[1:5]
        return (f"Läpikulku kielletty tunneliluokissa {heavy}, jos räjähteiden nettomassa ylittää {lim} kg kuljetusyksikköä "
                f"kohden; muuten luokissa {light} (ADR 8.6.4).",
                f"Passage forbidden through tunnels of category {heavy} if the net explosive mass exceeds {lim} kg per "
                f"transport unit; otherwise through categories {light} (ADR 8.6.4).", rule["tank"])
    cats = rule["tank" if tank else "other"]
    if not cats:
        return "Ei tunnelirajoituksia.", "No tunnel restrictions.", []
    lst = ", ".join(cats)
    return (f"Läpikulku kielletty tunneliluokissa {lst} (ADR 8.6.4).",
            f"Passage forbidden through tunnels of category {lst} (ADR 8.6.4).", cats)


def check_cargo(cargo: dict, mode: str | None, legs: list | None = None) -> dict:
    """Check the DG lines of a cargo for a transport mode (and the modes of the legs). Pure; see the module docstring."""
    items = cargo_items(cargo)
    findings: list[dict] = []
    checked: list[dict] = []
    modes = _modes(mode, legs)
    tank = bool((cargo or {}).get("tank")) or any(bool(it.get("tank")) for it in items)
    aircraft = None
    tunnel_codes = []
    if not items:
        if (cargo or {}).get("dangerous_goods"):
            findings.append(_f("dg.details_missing", "warn", "Vaarallisten aineiden tiedot puuttuvat: anna UN-numero, luokka ja pakkausryhmä.",
                               "Dangerous goods details are missing: give the UN number, class and packing group."))
        return _result(findings, checked, modes, None, [], aircraft)
    for idx, it in enumerate(items):
        un = norm_un(it.get("un_number") or it.get("un"))
        label = un or f"#{idx + 1}"
        if not un:
            findings.append(_f("dg.un_missing", "warn", f"Rivi {label}: UN-numero puuttuu, tiedot tarkistetaan käsin.",
                               f"Line {label}: the UN number is missing; our team checks the details by hand.", item=idx))
            continue
        rec = lookup(un)
        if not rec:
            findings.append(_f("dg.un_unknown", "block", f"{un} ei ole vaarallisten aineiden luettelossa (49 CFR 172.101, ADR 2025). Tarkista UN-numero.",
                               f"{un} is not in the dangerous goods list (49 CFR 172.101, ADR 2025). Check the UN number.", un=un))
            continue
        declared = _declared_class(it)
        divisions = rec["divisions"] or [rec["class"]]
        if declared:
            m = _class_match(declared, divisions)
            if m == "mismatch":
                findings.append(_f("dg.class_mismatch", "block",
                                   f"{un} on luokkaa {rec['class']} vaarallisten aineiden luettelossa, ei luokkaa {declared}. Tarkista luokka tai UN-numero.",
                                   f"{un} is class {rec['class']} in the dangerous goods list, not class {declared}. Check the class or the UN number.",
                                   un=un, declared=declared, listed=divisions))
                continue
            if m == "partial":
                findings.append(_f("dg.division_missing", "info", f"{un}: tarkenna luokka {rec['class']}.",
                                   f"{un}: give the division, {rec['class']}.", un=un))
        pg = str(it.get("packing_group") or it.get("pg") or "").strip().upper() or None
        if pg and rec["packing_groups"] and pg not in rec["packing_groups"]:
            findings.append(_f("dg.pg_mismatch", "block",
                               f"{un}: pakkausryhmää {pg} ei ole; luettelossa on {', '.join(rec['packing_groups'])}.",
                               f"{un} has no packing group {pg}; the list has {', '.join(rec['packing_groups'])}. Check the packing group.",
                               un=un, declared=pg, listed=rec["packing_groups"]))
            continue
        if pg and not rec["packing_groups"]:
            findings.append(_f("dg.pg_not_applicable", "warn", f"{un}: aineella ei ole pakkausryhmää; poista pakkausryhmä {pg} ilmoituksesta.",
                               f"{un} has no packing group in 49 CFR 172.101 or ADR 2025; remove packing group {pg} from the declaration.", un=un))
            pg = None
        if not pg and len(rec["packing_groups"]) > 0:
            findings.append(_f("dg.pg_missing", "warn", f"{un}: anna pakkausryhmä ({', '.join(rec['packing_groups'])}).",
                               f"{un}: give the packing group ({', '.join(rec['packing_groups'])}).", un=un))
        psn = str(it.get("proper_shipping_name") or "").strip()
        var = _pick_variant(rec, pg, psn)
        if psn and not any(n and (n.lower() in psn.lower() or psn.lower() in n.lower()) for n in rec["names"]):
            findings.append(_f("dg.name_differs", "info", f"{un}: virallinen nimi on \"{rec['name']}\".",
                               f"{un}: the proper shipping name in the list is \"{rec['name']}\".", un=un))
        division = (var or {}).get("division") or rec["class"]
        item = {"index": idx, "un": un, "pg": pg, "division": division, "class": division.split(".")[0],
                "subsidiary": (var or {}).get("subsidiary") or rec["subsidiary"], "name": (var or {}).get("name") or rec["name"]}
        checked.append(item)
        main = item["class"]
        if main == "1":
            findings.append(_f("dg.class1_review", "warn", f"{un}: räjähteet (luokka 1) vaativat aina luvan ja käsittelijän tarkistuksen.",
                               f"{un}: explosives (class 1) always need licences and a review by our team.", un=un))
        if main == "7":
            findings.append(_f("dg.class7_review", "warn", f"{un}: radioaktiiviset aineet (luokka 7) vaativat aina luvan ja käsittelijän tarkistuksen.",
                               f"{un}: radioactive material (class 7) always needs licences and a review by our team.", un=un))
        qty, unit = _qty(it)
        for m in modes:
            if m == "air":
                aircraft = _air_check(findings, un, var, qty, unit, it, aircraft)
            elif m == "road":
                adr_rows = (var or {}).get("adr") or (rec["adr"] and [rec["adr"]]) or []
                _road_check(findings, un, adr_rows, tank, tunnel_codes)
            elif m == "sea":
                _sea_check(findings, un, var)
            elif m == "rail":
                adr_rows = (var or {}).get("adr") or []
                if any(a.get("carriage_prohibited") for a in adr_rows):
                    findings.append(_f("dg.rail_review", "warn", f"{un}: rautatiekuljetus (RID) tarkistetaan käsin.",
                                       f"{un}: rail carriage (RID) is checked by hand; ADR lists this entry as prohibited.", un=un))
    segregation = _segregation(findings, checked, cargo, modes)
    return _result(findings, checked, modes, strictest_tunnel(tunnel_codes), segregation, aircraft)


def _air_check(findings, un, var, qty, unit, it, aircraft):
    air = (var or {}).get("air")
    if not air:
        findings.append(_f("dg.air_no_data", "warn", f"{un}: lentorahdin rajoja ei ole aineistossa; tarkistetaan käsin.",
                           f"{un}: no air quantity limits in the dataset; checked by hand.", un=un))
        return aircraft
    p, c = air["passenger"], air["cargo"]
    src = air["source"]
    if p["forbidden"] and c["forbidden"]:
        findings.append(_f("dg.air_forbidden", "block", f"{un} on kielletty lentorahdissa sekä matkustaja- että rahtikoneissa ({src}).",
                           f"{un} is forbidden by air on both passenger and cargo aircraft ({src}). Choose sea or road.", un=un))
        return "forbidden"
    state = aircraft or "any"
    if p["forbidden"]:
        findings.append(_f("dg.air_cargo_only", "warn", f"{un} on kielletty matkustajakoneissa: vain rahtikone (CAO), raja {c['text']} / pakkaus ({src}).",
                           f"{un} is forbidden on passenger aircraft: cargo aircraft only (CAO), up to {c['text']} per package ({src}).", un=un))
        state = "cargo_only" if state != "forbidden" else state
    if qty is None:
        findings.append(_f("dg.air_qty_missing", "info", f"{un}: anna nettomäärä pakkausta kohden, niin tarkistamme lentorajat ({p['text']} / {c['text']}).",
                           f"{un}: give the net quantity per package to check the air limits (passenger {p['text']}, cargo aircraft {c['text']}).", un=un))
        return state
    for lim, kind in ((p, "passenger"), (c, "cargo")):
        if lim["forbidden"] or lim["no_limit"]:
            continue
        if lim["value"] is None:
            findings.append(_f("dg.air_limit_manual", "warn", f"{un}: lentoraja \"{lim['text']}\" tarkistetaan käsin.",
                               f"{un}: the air limit \"{lim['text']}\" is checked by hand.", un=un))
            continue
        if unit is None or unit != lim["unit"]:
            findings.append(_f("dg.air_unit_mismatch", "warn", f"{un}: määrän yksikkö ei vastaa lentorajaa ({lim['text']}); tarkistetaan käsin.",
                               f"{un}: the quantity unit does not match the air limit ({lim['text']}); checked by hand.", un=un))
            return state
        if qty > lim["value"] + 1e-9:
            if kind == "passenger":
                findings.append(_f("dg.air_over_passenger_limit", "warn",
                                   f"{un}: {qty:g} {unit} pakkausta kohden ylittää matkustajakoneen rajan {lim['text']}: vain rahtikone (CAO).",
                                   f"{un}: {qty:g} {unit} per package is above the passenger aircraft limit of {lim['text']}: cargo aircraft only.",
                                   un=un))
                state = "cargo_only" if state != "forbidden" else state
            else:
                findings.append(_f("dg.air_over_cargo_limit", "block",
                                   f"{un}: {qty:g} {unit} pakkausta kohden ylittää rahtikoneen rajan {lim['text']}. Jaa pienempiin pakkauksiin tai valitse meri- tai maantiekuljetus.",
                                   f"{un}: {qty:g} {unit} per package is above the cargo aircraft limit of {lim['text']}. Use smaller packages or choose sea or road.",
                                   un=un))
                return "forbidden"
    return state


def _road_check(findings, un, adr_rows, tank, tunnel_codes):
    if not adr_rows:
        findings.append(_f("dg.adr_no_entry", "warn", f"{un}: ei ADR-taulukon A riviä tälle pakkausryhmälle; tarkistetaan käsin.",
                           f"{un}: no ADR Table A entry for this packing group; checked by hand.", un=un))
        return
    if any(a.get("carriage_prohibited") for a in adr_rows):
        findings.append(_f("dg.adr_prohibited", "block", f"{un}: kuljetus kielletty ADR:n mukaan (taulukko A).",
                           f"{un}: carriage prohibited by ADR (Table A). Choose another mode or contact our ops team.", un=un))
        return
    if all(a.get("not_subject_to_adr") for a in adr_rows):
        findings.append(_f("dg.adr_not_subject", "info", f"{un}: ei ADR-määräysten alainen maantiekuljetuksessa.",
                           f"{un}: not subject to ADR by road.", un=un))
        return
    code = strictest_tunnel(a.get("tunnel_code") for a in adr_rows)
    if not code:
        return
    tunnel_codes.append(code)
    fi, en, cats = _tunnel_explain(code, tank)
    sev = "warn" if ({"B", "C"} & set(cats)) else "info"
    tc = adr_rows[0].get("transport_category")
    findings.append(_f("dg.adr_tunnel", sev, f"{un}: tunnelirajoituskoodi ({code}), kuljetusluokka {tc}. {fi}",
                       f"{un}: tunnel restriction code ({code}), transport category {tc}. {en}",
                       un=un, tunnel_code=code, forbidden_tunnel_categories=cats, transport_category=tc))


def _sea_check(findings, un, var):
    vessel = (var or {}).get("vessel")
    if not vessel:
        return
    stow = vessel.get("stowage")
    if stow in PASSENGER_VESSEL_LIMITED:
        findings.append(_f("dg.vessel_passenger_restricted", "warn",
                           f"{un}: ahtausluokka {stow}: kielletty matkustaja-aluksilla, joilla on rajaa enemmän matkustajia (ro-pax-lautat tarkistetaan).",
                           f"{un}: stowage category {stow}: prohibited on passenger vessels above the passenger limit (ro-pax ferries are checked).",
                           un=un, stowage=stow, text=vessel.get("stowage_text")))
    elif stow:
        findings.append(_f("dg.vessel_stowage", "info", f"{un}: ahtausluokka {stow}.", f"{un}: vessel stowage category {stow}.",
                           un=un, stowage=stow, text=vessel.get("stowage_text")))


def _segregation(findings, checked, cargo, modes) -> list[dict]:
    if len(checked) < 2:
        return []
    containers = sum(int(c.get("count") or 1) for c in (cargo or {}).get("containers") or [])
    one_unit = containers <= 1
    sea = "sea" in modes
    out = []
    for i, a in enumerate(checked):
        for b in checked[i + 1:]:
            s = segregation_between(a, b)
            row = {"a": a["un"], "b": b["un"], "a_class": a["division"], "b_class": b["division"], **s}
            out.append(row)
            if s["code"] == "*":
                findings.append(_f("dg.segregation_class1", "warn", f"{a['un']} ja {b['un']}: räjähteiden keskinäinen erottelu (49 CFR 176.144) tarkistetaan.",
                                   f"{a['un']} and {b['un']}: segregation within class 1 (49 CFR 176.144) is checked by our team.", a=a["un"], b=b["un"]))
                continue
            if s["code"] not in SEG_RANK:
                continue
            if one_unit and s["code"] != "1":
                sev = "block" if sea else "warn"
                en = (f"{a['un']} (class {a['division']}) and {b['un']} (class {b['division']}) must be \"{s['term']}\" each other "
                      f"(49 CFR 176.83, IMDG 7.2.4); they cannot travel in the same container. Book them as separate shipments or containers.")
                fi = (f"{a['un']} (luokka {a['division']}) ja {b['un']} (luokka {b['division']}) on pidettävä {SEG_FI[s['code']]} toisistaan; "
                      f"ne eivät voi olla samassa kontissa. Varaa ne erillisinä lähetyksinä tai kontteina.")
            elif one_unit:  # "away from" in one container: only with the competent authority's approval (IMDG 7.3.4)
                sev = "warn"
                en = (f"{a['un']} (class {a['division']}) and {b['un']} (class {b['division']}) must be \"{s['term']}\" each other "
                      f"(49 CFR 176.83, IMDG 7.2.4); in one container only with the competent authority's approval. Our team checks it.")
                fi = (f"{a['un']} (luokka {a['division']}) ja {b['un']} (luokka {b['division']}) on pidettävä {SEG_FI[s['code']]} toisistaan; "
                      f"samassa kontissa vain viranomaisen luvalla. Tarkistamme asian.")
            else:
                sev = "warn"
                en = (f"{a['un']} (class {a['division']}) and {b['un']} (class {b['division']}): segregation \"{s['term']}\" "
                      f"(49 CFR 176.83, IMDG 7.2.4); separate containers and stowage are planned with the carrier.")
                fi = (f"{a['un']} (luokka {a['division']}) ja {b['un']} (luokka {b['division']}): erottelu {SEG_FI[s['code']]}; "
                      f"erilliset kontit ja ahtaus sovitaan kuljettajan kanssa.")
            row["severity"] = sev
            findings.append(_f("dg.segregation", sev, fi, en, a=a["un"], b=b["un"], segregation_code=s["code"]))
    return out


def _result(findings, checked, modes, tunnel, segregation, aircraft) -> dict:
    sev = {f["severity"] for f in findings}
    status = "forbidden" if "block" in sev else ("review" if "warn" in sev else "ok")
    return {"status": status, "findings": findings, "tunnel_code": tunnel, "segregation": segregation,
            "aircraft": aircraft, "modes": modes, "items": checked,
            "dataset": {"sha256": {s["key"]: s["sha256"][:16] for s in _sources()},
                        "air_source": "IATA DGR (licensed table)" if _iata_table() else "49 CFR 172.101 9A/9B"}}


def first_block(res: dict) -> dict | None:
    return next((f for f in res.get("findings") or [] if f["severity"] == "block"), None)


# ------------------------------------------------------------------------------------------------
# Quote and booking integration (called from services/pricing.py and routes/quotes.py)
# ------------------------------------------------------------------------------------------------
async def apply_to_quote(req: dict, options: list[dict], warnings: list[dict], not_quoted: list[dict]) -> list[dict]:
    """Check every option's modes; drop options DG rules forbid, add findings to the warnings and req["dg_check"].
    Raises ValueError(plain sentence) when the DG lines are wrong for every mode (unknown UN, class mismatch) or the
    requested mode is forbidden."""
    from core.db import db
    from core.util import NO_ID

    cargo = req["cargo"]
    if not cargo_items(cargo) and not cargo.get("dangerous_goods"):
        return options
    base = check_cargo(cargo, None)
    blk = first_block(base)
    if blk:
        raise ValueError(blk["en"])
    asked = req.get("mode")
    by_mode = {m: check_cargo(cargo, m) for m in sorted({o.get("mode") for o in options if o.get("mode")} | ({asked} if asked else set()))}
    if asked and by_mode[asked]["status"] == "forbidden":
        raise ValueError(first_block(by_mode[asked])["en"])
    ids = list({o.get("carrier_id") for o in options if o.get("mode") == "air" and o.get("carrier_id")})
    carriers = {c["id"]: c async for c in db.carriers.find({"id": {"$in": ids}}, NO_ID)} if ids else {}
    kept = []
    for o in options:
        res = check_cargo(cargo, o.get("mode"), o.get("legs"))
        reason = reason_fi = None
        if res["status"] == "forbidden":
            reason, reason_fi = first_block(res)["en"], first_block(res)["fi"]
        elif res["aircraft"] == "cargo_only" and (carriers.get(o.get("carrier_id")) or {}).get("air_aircraft") == "passenger":
            reason = f"{o.get('carrier_name')} flies passenger aircraft only; this dangerous goods line needs a cargo aircraft"
            reason_fi = f"{o.get('carrier_name')} lentää vain matkustajakoneilla; tämä vaarallisten aineiden erä vaatii rahtikoneen"
        if reason:
            nq = {"carrier_name": o.get("carrier_name"), "mode": o.get("mode"), "service_name": o.get("service_name"),
                  "reason": reason, "reason_fi": reason_fi, "code": "dg_forbidden"}
            if nq not in not_quoted:
                not_quoted.append(nq)
            continue
        o["dg_check"] = {"status": res["status"], "aircraft": res["aircraft"], "tunnel_code": res["tunnel_code"]}
        kept.append(o)
    seen = set()
    for res in [base, *by_mode.values()]:
        for f in res["findings"]:
            key = (f["code"], f["en"])
            if key in seen:  # a block of a removed mode stays: it tells the customer why that mode is missing
                continue
            seen.add(key)
            warnings.append({"code": f["code"], "level": "warning" if f["severity"] in ("warn", "block") else "info",
                             "message": f["en"], "params": {"fi": f["fi"], "severity": f["severity"]}})
    req["dg_check"] = {"status": _worst([r["status"] for r in by_mode.values()] or [base["status"]]),
                       "by_mode": {m: {k: r[k] for k in ("status", "findings", "tunnel_code", "segregation", "aircraft")}
                                   for m, r in by_mode.items()},
                       "items": base["items"], "dataset": base["dataset"]}
    return kept


def _worst(statuses: list[str]) -> str:
    order = ["forbidden", "review", "ok"]
    return min(statuses, key=order.index) if statuses else "ok"


def booking_mode(opt: dict) -> tuple[str | None, list]:
    return opt.get("mode"), opt.get("legs") or []


async def record_check(res: dict, *, stage: str, entity: str, entity_id: str | None, company_id: str | None = None):
    from core.audit import agent_actor, audit
    await audit(agent_actor("compliance"), f"dg_check_{stage}", entity, entity_id,
                {"status": res["status"], "modes": res.get("modes"), "tunnel_code": res.get("tunnel_code"),
                 "aircraft": res.get("aircraft"), "items": res.get("items"),
                 "findings": [{"code": f["code"], "severity": f["severity"], "en": f["en"]} for f in res.get("findings") or []],
                 "dataset": res.get("dataset")}, company_id=company_id)


async def after_booking(res: dict, shipment: dict | None, *, company_id: str | None, user: dict | None) -> dict | None:
    """Review result at booking: flag the shipment and open an approval card (kind dg_review, legal point dangerous_goods)."""
    if not shipment or not shipment.get("id"):
        return None
    from core.approvals import request_approval
    from core.db import db
    from core.util import now_iso

    await db.shipments.update_one({"id": shipment["id"]}, {"$set": {"dg_check": res, "dg_review_required": res["status"] == "review",
                                                                    "updated_at": now_iso()}})
    await record_check(res, stage="booking", entity="shipment", entity_id=shipment["id"], company_id=company_id)
    if res["status"] != "review":
        return None
    reasons = [f for f in res["findings"] if f["severity"] == "warn"]
    ref = shipment.get("reference") or shipment["id"]
    return await request_approval(
        "dg_review", "shipment", shipment["id"], f"Dangerous goods review {ref}",
        summary="; ".join(f["en"] for f in reasons)[:1500], reason=reasons[0]["en"] if reasons else "",
        company_id=company_id, requested_by=user, link=f"/ops/shipments/{shipment['id']}",
        data={"legal_point": "dangerous_goods", "dg_check": {k: res[k] for k in ("status", "modes", "tunnel_code", "aircraft", "items")},
              "reasons": [{"code": f["code"], "fi": f["fi"], "en": f["en"]} for f in reasons],
              "title_fi": f"Vaarallisten aineiden tarkistus {ref}"})


def _register_decision_handler():
    """A person's decision on a dg_review card: approved = the shipment may move (agents may book it), rejected = it
    stays flagged and ops are told. Until then core.agent_runtime.decide() stops agents on it (legal point)."""
    from core.audit import audit
    from core.db import db
    from core.events import on
    from core.util import now_iso

    @on("approval.decided")
    async def _on_dg_review_decided(p: dict):
        a = p["approval"]
        if a.get("kind") != "dg_review" or a.get("status") not in ("approved", "rejected"):
            return
        ok = a["status"] == "approved"
        await db.shipments.update_one({"id": a["ref_id"]}, {"$set": {
            "dg_review_required": not ok, "dg_review_status": "approved" if ok else "rejected",
            "dg_review_decided_at": now_iso(), "dg_review_approval_id": a["id"], "updated_at": now_iso()}})
        await audit(p.get("actor"), "dg_review_decided", "shipment", a["ref_id"],
                    {"approval_id": a["id"], "status": a["status"], "note": a.get("decision_note")}, company_id=a.get("company_id"))


_register_decision_handler()
