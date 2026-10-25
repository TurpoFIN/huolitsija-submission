"""HS 2022 classification: nomenclature lookup, keyword retrieval, an AI classifier that picks from the
retrieved candidates, and deterministic compliance flags (CBAM, dual-use hints, RU/BY origin).

Nomenclature: data/hs2022.csv + data/hs2022_sections.csv, from
https://raw.githubusercontent.com/datasets/harmonized-system/master/data/harmonized-system.csv
(UN Comtrade HS 2022, PDDL licence), fetched 2026-10-24. 96 chapters, 1228 headings, 5612 subheadings.

How a suggestion is made (classify_items / suggest):
1. Retrieval: BM25F over every HS6 subheading. Each subheading's text = its own description (weight 1.0) +
   heading text (0.55) + chapter title (0.3) + section name (0.15). Common trade words that the legal
   texts do not use ("laptop", "t-shirt", "e-bike") and a small Finnish/Swedish glossary are expanded
   into nomenclature vocabulary before scoring. A code-looking query ("9403 60") is a direct lookup.
2. The model (core.llm, provider-agnostic) sees each item, its top candidates with their full path and the
   97 chapter titles. It answers, or asks to see every subheading of up to 4 headings it believes are
   right but missing (one expansion round per batch). One AI call per batch, two at most.
3. Validation: the code must exist in HS 2022; the CN8 must be a real CN 2026 subdivision of it (data/cn2026.csv,
   Eurostat Comext, 9793 codes) — an invented CN8 is dropped, a subheading with a single CN subdivision resolves
   to it, otherwise the result lists cn_options for a person to choose. The confidence is clipped, a code printed
   on the invoice is checked against the nomenclature and against the pick.
The CN/TARIC code must still be confirmed against TARIC on the declaration date (RULE H-1). This module's model
rate and trade-defence list are offline fallbacks only. services.customs.recompute replaces them with live
services.taric measures; an unavailable TARIC lookup blocks filing (TARIC-1).
Without a model the result carries the retrieval candidates and status "needs_review" (never invented).
"""
from __future__ import annotations

import csv
import logging
import math
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core import config
from core.llm import LLMUnavailable, llm_json, provider_name
from core.util import now_iso

log = logging.getLogger("hs")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HS_CSV = DATA_DIR / "hs2022.csv"
SECTIONS_CSV = DATA_DIR / "hs2022_sections.csv"
HS_EDITION = "HS 2022"
CN_CSV = DATA_DIR / "cn2026.csv"
CN_EDITION = "CN 2026"

CN8_NOTE = "Proposed by AI from the CN 2026 subdivisions; confirm the CN/TARIC code against TARIC on the declaration date (RULE H-1)"
DUTY_BASIS = "Third-country (erga omnes) conventional duty"
DUTY_LABEL = "Estimate — not authoritative; confirm in TARIC"
AGENT = "hs_classifier"
BATCH_SIZE = 12
N_CANDIDATES = 20
STATUS_OK_MIN_CONFIDENCE = 0.8   # below: broker review (status needs_review)
STATUS_OK_MIN_MARGIN = 0.15      # best alternative within this of the pick: broker review
HEADINGS_PER_ITEM = 3            # top retrieved headings whose subheadings are all shown
CONTEXT_HEADINGS = 2             # extra headings taken from the other lines' best matches

# ---------------------------------------------------------------------------------------------
# Nomenclature
# ---------------------------------------------------------------------------------------------
_ENTRIES: dict[str, dict] = {}       # code -> row (2, 4 and 6 digit)
_CHILDREN: dict[str, list[str]] = defaultdict(list)
_SECTIONS: dict[str, str] = {}
_HS6: list[str] = []                 # ordered HS6 codes (index = BM25 doc id)
_INDEX: dict[str, list[tuple[int, float]]] = {}
_DOC_LEN: list[float] = []
_AVG_LEN = 1.0
_LOADED = False
_CN: dict[str, str] = {}                   # CN 2026 8-digit code -> description
_CN_BY_HS6: dict[str, list[str]] = defaultdict(list)


def _load():
    global _LOADED
    if _LOADED:
        return
    with open(SECTIONS_CSV, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            _SECTIONS[row["section"].strip()] = row["name"].strip()
    with open(HS_CSV, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            section, code = row["section"].strip(), row["hscode"].strip()
            # UN Comtrade adds TOTAL / 99 / 9999 / 999999 aggregates that are not HS codes.
            if section == "TOTAL" or not code.isdigit():
                continue
            _ENTRIES[code] = {"code": code, "description": row["description"].strip(), "level": len(code),
                              "parent": row["parent"].strip(), "section": section}
            if len(code) > 2:
                _CHILDREN[row["parent"].strip()].append(code)
    _HS6.extend(sorted(c for c in _ENTRIES if len(c) == 6))
    _load_cn()
    _build_index()
    _LOADED = True


def _load_cn():
    if _CN or not CN_CSV.exists():
        return
    with open(CN_CSV, encoding="utf-8", newline="") as fh:
        rows = csv.DictReader(line for line in fh if not line.startswith("#"))
        for row in rows:
            code = (row.get("code") or "").strip()
            if len(code) == 8 and code.isdigit():
                _CN[code] = (row.get("description") or "").strip()
                _CN_BY_HS6[code[:6]].append(code)


def cn_entry(code: str | None) -> dict | None:
    """A CN 2026 8-digit code with its description, or None when it is not a CN 2026 code."""
    _load()
    d = re.sub(r"\D", "", str(code or ""))[:8]
    if len(d) != 8 or d not in _CN:
        return None
    return {"code": d, "description": _CN[d], "hs6": d[:6], "edition": CN_EDITION}


def cn_options(hs6: str | None) -> list[dict]:
    """Every CN 2026 subdivision of an HS 2022 subheading."""
    _load()
    h = re.sub(r"\D", "", str(hs6 or ""))[:6]
    return [{"code": c, "description": _CN[c]} for c in _CN_BY_HS6.get(h, [])]


def resolve_cn(hs6: str | None, proposed: str | None = None) -> dict:
    """Validate a proposed CN8 against CN 2026 under hs6. Never returns a code that is not in CN 2026.
    -> {"cn8": code|None, "cn_options": [...], "note": str|None, "resolved": "valid"|"single"|"choose"|"none"}"""
    _load()
    h = re.sub(r"\D", "", str(hs6 or ""))[:6]
    p = re.sub(r"\D", "", str(proposed or ""))[:8]
    opts = cn_options(h)
    if p and len(p) == 8 and p in _CN and p.startswith(h):
        return {"cn8": p, "cn_options": opts, "note": None, "resolved": "valid"}
    bad = (f"{p[:4]} {p[4:6]} {p[6:8]} is not a {CN_EDITION} code under {h}. " if p and len(p) == 8 else "")
    if len(opts) == 1:
        return {"cn8": opts[0]["code"], "cn_options": opts, "resolved": "single",
                "note": bad + f"{h} has one CN subdivision ({opts[0]['code']}); confirm it in TARIC (RULE H-1)."}
    if opts:
        return {"cn8": None, "cn_options": opts, "resolved": "choose",
                "note": bad + f"Choose the CN subdivision of {h} ({len(opts)} options) and confirm it in TARIC (RULE H-1)."}
    return {"cn8": None, "cn_options": [], "resolved": "none",
            "note": bad + f"No {CN_EDITION} subdivision found for {h}; choose the CN/TARIC code in TARIC (RULE H-1)."}


def hs_entry(code: str) -> dict | None:
    """Full path of a 2-, 4- or 6-digit code (longer CN/TARIC codes are cut to their HS6)."""
    _load()
    digits = re.sub(r"\D", "", str(code or ""))
    if len(digits) > 6:
        digits = digits[:6]
    e = _ENTRIES.get(digits)
    if not e:
        return None
    chapter = _ENTRIES.get(digits[:2], {})
    heading = _ENTRIES.get(digits[:4], {}) if len(digits) >= 4 else {}
    return {
        "code": digits, "description": e["description"], "level": e["level"], "parent": e["parent"],
        "section": e["section"], "section_name": _SECTIONS.get(e["section"]),
        "heading": digits[:4] if len(digits) >= 4 else None, "heading_description": heading.get("description"),
        "chapter": digits[:2], "chapter_description": chapter.get("description"),
    }


def chapters() -> list[dict]:
    _load()
    return [{"code": c, "description": e["description"]} for c, e in sorted(_ENTRIES.items()) if len(c) == 2]


def subheadings(heading: str) -> list[dict]:
    """Every HS6 under a 4-digit heading (or a 2-digit chapter)."""
    _load()
    h = re.sub(r"\D", "", heading or "")
    if len(h) == 2:
        codes = [c for c in _HS6 if c.startswith(h)]
    else:
        codes = [c for c in _CHILDREN.get(h[:4], []) if len(c) == 6]
    return [_cand(c) for c in codes]


def _cand(code: str, score: float | None = None) -> dict:
    e = hs_entry(code) or {}
    out = {"code": code, "description": e.get("description"), "heading_description": e.get("heading_description"),
           "chapter_description": e.get("chapter_description")}
    if score is not None:
        out["score"] = round(score, 3)
    return out


# ---------------------------------------------------------------------------------------------
# Retrieval (BM25F)
# ---------------------------------------------------------------------------------------------
_STOP = set("""a an and or of the for with without whether not other than in on into to by its their thereof
including excluding nec n e c kind kinds used use such as be being similar article articles which this that
heading headings subheading item items no chapter etc from at are is whose more less but only all any
nes put up""".split())

# Trade words that the legal texts do not use -> nomenclature vocabulary (English).
_SYNONYMS = {
    "laptop": "automatic data processing machines portable", "notebook": "automatic data processing portable",
    "computer": "automatic data processing machines", "pc": "automatic data processing machines",
    "tablet": "automatic data processing portable", "server": "automatic data processing machines",
    "smartphone": "telephones cellular networks", "phone": "telephones", "mobile": "telephones cellular",
    "tv": "television reception", "monitor": "monitors", "printer": "printers",
    "tshirt": "t-shirts singlets vests", "tee": "t-shirts", "hoodie": "jerseys pullovers sweatshirts",
    "sweater": "jerseys pullovers", "jumper": "jerseys pullovers", "jeans": "trousers denim cotton",
    "pants": "trousers", "jacket": "anoraks wind-jackets jackets", "parka": "anoraks", "coat": "overcoats coats",
    "sneaker": "footwear sports", "shoe": "footwear", "boot": "footwear", "sandal": "footwear",
    "bike": "cycles bicycles", "bicycle": "cycles bicycles", "ebike": "cycles auxiliary motor",
    "battery": "accumulators batteries", "lamp": "luminaires lamps light fittings", "light": "luminaires lighting",
    "led": "light-emitting diode", "bulb": "lamps", "tyre": "tyres", "tire": "tyres", "car": "motor vehicles",
    "truck": "motor vehicles goods transport", "fridge": "refrigerators", "freezer": "freezers",
    "sofa": "seats upholstered", "couch": "seats upholstered", "chair": "seats", "stool": "seats",
    "desk": "furniture office", "wardrobe": "furniture bedroom", "cabinet": "furniture", "shelf": "furniture",
    "mattress": "mattresses", "drone": "unmanned aircraft", "uav": "unmanned aircraft",
    "toy": "toys", "doll": "dolls", "cable": "insulated wire cables conductors", "charger": "static converters",
    "adapter": "static converters", "router": "apparatus transmission reception data", "screw": "screws",
    "bolt": "bolts", "nut": "nuts", "washer": "washers", "fastener": "screws bolts", "pipe": "tubes pipes",
    "tube": "tubes", "coil": "coils", "sheet": "sheets plates", "wire": "wire", "rebar": "bars rods",
    "beam": "angles shapes sections", "bag": "bags", "backpack": "rucksacks", "suitcase": "trunks suitcases",
    "watch": "wrist-watches", "glasses": "spectacles", "sunglasses": "sunglasses spectacles",
    "kayak": "canoes", "canoe": "canoes", "boat": "boats vessels", "yacht": "yachts", "outboard": "outboard motors",
    "propeller": "propellers ships", "anchor": "anchors", "rope": "cordage ropes", "paint": "paints varnishes",
    "glue": "adhesives", "tent": "tents", "sleeping": "sleeping bags", "ski": "skis", "helmet": "headgear helmets",
    "glove": "gloves", "sock": "socks hosiery", "dress": "dresses", "skirt": "skirts", "shirt": "shirts",
    "blouse": "blouses", "underwear": "briefs underpants", "towel": "towels", "blanket": "blankets",
    "pallet": "pallets", "crate": "cases boxes crates", "carton": "cartons boxes", "bottle": "bottles",
    "solar": "photovoltaic", "panel": "panels", "inverter": "static converters", "pump": "pumps",
    "compressor": "compressors", "valve": "valves taps", "bearing": "bearings", "gear": "gears gearing",
    "motor": "motors", "engine": "engines", "generator": "generating sets", "transformer": "transformers",
    "chip": "electronic integrated circuits", "microchip": "electronic integrated circuits", "ic": "electronic integrated circuits",
    "pcb": "printed circuits", "sensor": "instruments measuring", "camera": "cameras", "headphone": "headphones",
    "earphone": "earphones headphones", "speaker": "loudspeakers", "keyboard": "keyboards input units",
    "mouse": "input units", "brake": "brakes", "pad": "pads", "saddle": "saddles", "pedal": "pedals",
    "chain": "chain", "rim": "rims", "spoke": "spokes", "hub": "hubs", "derailleur": "gear", "fork": "forks",
    "handlebar": "handlebars", "coffee": "coffee", "tea": "tea", "wine": "wine grapes", "beer": "beer malt",
    "salmon": "salmon fish", "cheese": "cheese", "chocolate": "chocolate cocoa", "fertilizer": "fertilisers",
    "fertiliser": "fertilisers", "aluminum": "aluminium", "stainless": "stainless steel", "plywood": "plywood",
    "timber": "wood sawn", "lumber": "wood sawn", "granite": "granite stone", "marble": "marble",
    "vacuum": "vacuum cleaners", "kettle": "electro-thermic kettles", "microwave": "microwave ovens",
    "dishwasher": "dish washing machines", "washing": "washing machines", "phonecase": "cases",
}

# Finnish / Swedish stems -> English nomenclature words. Matched as word prefixes (Finnish inflects).
_GLOSSARY = {
    # Finnish
    "tuoli": "chair seats", "istuin": "seats", "sohva": "sofa seats upholstered", "pöytä": "tables furniture",
    "poyta": "tables furniture", "kaappi": "cabinet furniture", "hylly": "shelf furniture", "sänky": "beds furniture",
    "sanky": "beds furniture", "huonekalu": "furniture", "kaluste": "furniture", "patja": "mattresses",
    "puinen": "wooden wood", "puise": "wooden wood", "puu": "wood", "tammi": "oak wood", "koivu": "birch wood",
    "teräs": "steel", "teras": "steel", "teräks": "steel", "teraks": "steel", "rauta": "iron", "raudas": "iron",
    "ruostumat": "stainless steel", "alumiini": "aluminium", "verhoil": "upholstered", "pöyd": "tables furniture",
    "poyd": "tables furniture", "ruokapöy": "dining tables", "pavu": "beans", "paid": "shirts", "t-paid": "t-shirts",
    "miesten": "men's", "naisten": "women's", "lasten": "children's", "neulottu": "knitted", "kudottu": "woven",
    "kupari": "copper", "messinki": "brass copper", "muovi": "plastics", "kumi": "rubber", "lasi": "glass",
    "keraami": "ceramic", "posliini": "porcelain", "puuvilla": "cotton", "villa": "wool", "pellava": "flax linen",
    "polyesteri": "polyester synthetic", "nahka": "leather", "kangas": "fabrics woven", "neulo": "knitted",
    "ruuv": "screws", "pult": "bolts", "mutter": "nuts", "aluslevy": "washers", "naula": "nails", "naulo": "nails",
    "putki": "tubes pipes", "levy": "sheets plates", "kela": "coils", "lanka": "wire yarn", "tanko": "bars rods",
    "polkupyör": "bicycles cycles", "pyörä": "wheels cycles", "pyora": "wheels cycles", "jarru": "brakes",
    "jarrupala": "brake pads", "satula": "saddles", "ketju": "chain", "vanne": "rims", "akku": "accumulators batteries",
    "paristo": "batteries cells", "valaisin": "luminaires lamps", "lamppu": "lamps", "pöytävalaisin": "luminaires table desk",
    "johto": "cables wire", "kaapeli": "cables", "laturi": "chargers static converters", "moottori": "motors engines",
    "kone": "machines", "laite": "apparatus", "osa": "parts", "varaosa": "parts", "tarvike": "accessories",
    "kahvi": "coffee", "paahta": "roasted", "paahtama": "not roasted", "papu": "beans", "tee": "tea",
    "suklaa": "chocolate", "juusto": "cheese", "kala": "fish", "lohi": "salmon", "liha": "meat", "viini": "wine",
    "olut": "beer", "takki": "jackets anoraks", "paita": "shirts", "t-paita": "t-shirts", "housut": "trousers",
    "hame": "skirts", "mekko": "dresses", "kenkä": "footwear", "kenka": "footwear", "kengä": "footwear",
    "saapas": "boots footwear", "sukka": "socks", "käsine": "gloves", "kasine": "gloves", "hanska": "gloves",
    "pipo": "hats headgear", "reppu": "rucksacks", "laukku": "bags handbags", "teltta": "tents",
    "makuupussi": "sleeping bags", "sukset": "skis", "suksi": "skis", "kypärä": "helmets headgear",
    "vene": "boats", "potkuri": "propellers", "perämoottori": "outboard motors", "ankkuri": "anchors",
    "köysi": "ropes cordage", "koysi": "ropes cordage", "maali": "paints", "liima": "adhesives",
    "lannoite": "fertilisers", "sementti": "cement", "tietokone": "automatic data processing machines",
    "kannettava": "portable", "puhelin": "telephones", "näyttö": "monitors displays", "naytto": "monitors displays",
    "kamera": "cameras", "lelu": "toys", "nukke": "dolls", "pumppu": "pumps", "venttiili": "valves",
    "laakeri": "bearings", "aurinkopaneeli": "photovoltaic panels", "kattila": "pans boilers", "astia": "tableware",
    "pyyhe": "towels", "peitto": "blankets", "matto": "carpets", "kello": "watches clocks",
    # Swedish
    "stol": "chair seats", "bord": "tables furniture", "möbel": "furniture", "mobel": "furniture",
    "trä": "wood wooden", "tra": "wood", "stål": "steel", "stal": "steel", "skruv": "screws", "bult": "bolts",
    "cykel": "bicycles cycles", "broms": "brakes", "batteri": "batteries accumulators", "lampa": "lamps luminaires",
    "kaffe": "coffee", "jacka": "jackets anoraks", "skjorta": "shirts", "byxor": "trousers", "sko": "footwear",
    "bomull": "cotton", "ull": "wool", "plast": "plastics", "gummi": "rubber", "glas": "glass",
    "dator": "automatic data processing machines", "bärbar": "portable", "barbar": "portable",
    "reservdel": "parts", "båt": "boats", "bat": "boats", "gödsel": "fertilisers", "godsel": "fertilisers",
    # German
    "stuhl": "chair seats", "tisch": "tables", "holz": "wood", "stahl": "steel", "schraube": "screws",
    "fahrrad": "bicycles cycles", "bremse": "brakes", "kaffee": "coffee", "jacke": "jackets anoraks",
    "baumwolle": "cotton", "kunststoff": "plastics", "leuchte": "luminaires",
}
_GLOSSARY_KEYS = sorted(_GLOSSARY, key=len, reverse=True)

_FIELD_WEIGHTS = {"own": 1.0, "cn": 0.6, "heading": 0.55, "chapter": 0.3, "section": 0.15}
_EXCL = re.compile(r"\((?:excl|other than|not incl)[^()]*(?:\([^()]*\)[^()]*)*\)", re.I)
_K1, _B = 1.2, 0.75


def _fold(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 6 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 5 and w.endswith("ed") and not w.endswith("eed"):
        return w[:-2]
    return w


def _tokens(text: str) -> list[str]:
    text = _fold((text or "").lower()).replace("t-shirt", "tshirt").replace("e-bike", "ebike")
    out = []
    for w in re.findall(r"[a-z][a-z0-9\-]*", text):
        for part in w.split("-"):
            if not part or part in _STOP or len(part) < 2:
                continue
            out.append(_stem(part))
    return out


def _expand_query(text: str) -> list[str]:
    """Raw tokens + synonym/glossary expansions (in nomenclature vocabulary).

    The Finnish/Swedish/German glossary is applied only to words the English nomenclature does not know, and only
    when most of the description is unknown (so "Bordeaux wine" is not read as Swedish "bord")."""
    raw = (text or "").lower()
    words = re.findall(r"[a-zåäöüß][a-zåäöüß0-9\-]*", raw)
    extra: list[str] = []
    unknown: list[str] = []
    for w in words:
        wf = _fold(w).replace("-", "")
        st = _stem(wf)
        if st in _SYNONYMS or wf in _SYNONYMS:
            extra.append(_SYNONYMS.get(st) or _SYNONYMS[wf])
        elif st not in _INDEX and st not in _STOP and len(st) >= 3:
            unknown.append(w)
    if words and len(unknown) / len(words) >= 0.4:
        for w in unknown:
            # Prefix match (Finnish inflects: tuoli -> tuolit), plus infix match for longer stems inside
            # compounds (ruokapöydän -> pöyd). Every matching stem contributes, longest first.
            fw = _fold(w)
            hits = 0
            for key in _GLOSSARY_KEYS:
                fk = _fold(key)
                if len(key) < min(4, len(w)):
                    continue
                if w.startswith(key) or fw.startswith(fk) or (len(fk) >= 4 and fk in fw[2:]):
                    extra.append(_GLOSSARY[key])
                    hits += 1
                    if hits >= 2:
                        break
    return _tokens(raw) + _tokens(" ".join(extra))


def _build_index():
    global _AVG_LEN
    postings: dict[str, dict[int, float]] = defaultdict(dict)
    for i, code in enumerate(_HS6):
        e = _ENTRIES[code]
        fields = {
            "own": e["description"],
            "heading": _ENTRIES.get(code[:4], {}).get("description", ""),
            "chapter": _ENTRIES.get(code[:2], {}).get("description", ""),
            "section": _SECTIONS.get(e["section"], ""),
        }
        tf: dict[str, float] = defaultdict(float)
        length = 0.0
        for f, txt in fields.items():
            w = _FIELD_WEIGHTS[f]
            for t in _tokens(txt):
                tf[t] += w
                length += w
        # CN 2026 subdivision texts name goods the HS6 text only implies ("Derailleur gears for bicycles" under
        # 8714 99 "other"). Each distinct word counts once; "(excl. ...)" clauses are dropped as noise.
        cn_words = {t for c in _CN_BY_HS6.get(code, []) for t in _tokens(_EXCL.sub(" ", _CN[c]))}
        for t in cn_words:
            tf[t] += _FIELD_WEIGHTS["cn"]
            length += _FIELD_WEIGHTS["cn"]
        _DOC_LEN.append(length)
        for t, v in tf.items():
            postings[t][i] = v
    n = len(_HS6) or 1
    _AVG_LEN = sum(_DOC_LEN) / n
    for t, docs in postings.items():
        _INDEX[t] = list(docs.items())


def _code_lookup(query: str) -> list[dict] | None:
    q = query.strip()
    if not re.fullmatch(r"[\d\s.\-]{2,14}", q):
        return None
    digits = re.sub(r"\D", "", q)
    if len(digits) < 2:
        return None
    if len(digits) >= 6:
        return [_cand(digits[:6], 1.0)] if digits[:6] in _ENTRIES else []
    return [_cand(c, 1.0) for c in _HS6 if c.startswith(digits)]


def search(query: str, limit: int = 25) -> list[dict]:
    """BM25F retrieval over the HS6 subheadings. A code-looking query is a direct lookup."""
    _load()
    direct = _code_lookup(query or "")
    if direct is not None:
        return direct[:limit]
    terms = list(dict.fromkeys(_expand_query(query)))
    if not terms:
        return []
    n = len(_HS6)
    scores: dict[int, float] = defaultdict(float)
    for t in terms:
        plist = _INDEX.get(t)
        if not plist:
            continue
        idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
        for doc, tf in plist:
            denom = tf + _K1 * (1 - _B + _B * _DOC_LEN[doc] / _AVG_LEN)
            scores[doc] += idf * tf * (_K1 + 1) / denom
    best = sorted(scores.items(), key=lambda kv: (-kv[1], _HS6[kv[0]]))[:limit]
    return [_cand(_HS6[i], s) for i, s in best]


# ---------------------------------------------------------------------------------------------
# Deterministic flags
# ---------------------------------------------------------------------------------------------
# Reg (EU) 2023/956 Annex I. Each entry: (CN prefix, goods group). Exclusions listed separately.
_CBAM_INCLUDE = [
    ("25070080", "cement"), ("252310", "cement"), ("252321", "cement"), ("252329", "cement"),
    ("252330", "cement"), ("252390", "cement"),
    ("2716", "electricity"),
    ("2808", "fertilisers"), ("2814", "fertilisers"), ("283421", "fertilisers"), ("3102", "fertilisers"),
    ("3105", "fertilisers"),
    ("260112", "iron and steel"), ("72", "iron and steel"),
    *[(h, "iron and steel") for h in ("7301", "7302", "7303", "7304", "7305", "7306", "7307", "7308", "7309",
                                      "7310", "7311", "7318", "7326")],
    *[(h, "aluminium") for h in ("7601", "7603", "7604", "7605", "7606", "7607", "7608", "7609", "7610",
                                 "7611", "7612", "7613", "7614", "7616")],
    ("280410", "hydrogen"),
]
# 7202 99 10/30/80 are all CN splits of 7202 99, so the whole subheading is excluded.
_CBAM_EXCLUDE = ["310560", "72022", "720230", "720250", "720270", "720280", "720291", "720292", "720293",
                 "720299", "7204"]

# (prefix, label, severity). Mass-market IT with cryptography is normally released by the Cryptography Note
# (Annex I, Category 5 Part 2), so those are informational only.
_DUAL_USE_HINTS = [
    ("8456", "machine tools (laser, ultrasonic, EDM and similar)", "review"), ("8457", "machining centres", "review"),
    ("8458", "lathes", "review"), ("8459", "machine tools for drilling, boring and milling", "review"),
    ("8471", "computers with cryptographic functions (mass-market items are normally released by the Cryptography Note)", "info"),
    ("847330", "computer parts with cryptographic functions (mass-market items are normally released)", "info"),
    ("851762", "network equipment with cryptographic functions (mass-market items are normally released)", "info"),
    ("8525", "cameras and transmission apparatus (thermal / high-performance imaging)", "review"),
    ("8526", "radar and radio navigation apparatus", "review"), ("8542", "integrated circuits", "review"),
    ("8543", "electrical machines with individual functions", "review"), ("9013", "lasers and optical devices", "review"),
    ("9014", "navigation instruments", "review"), ("9015", "surveying instruments", "review"), ("9027", "analysers", "review"),
    ("8401", "nuclear reactors and parts", "review"), ("2844", "radioactive elements and isotopes", "review"),
    ("3602", "prepared explosives", "review"), ("3603", "detonators and igniters", "review"),
    ("8806", "unmanned aircraft (drones)", "review"),
]
_OCCUPIED = re.compile(r"crimea|krym|sevastopol|donetsk|luhansk|lugansk|kherson|zaporizhzhia|zaporozhye", re.I)

# EU trade defence measures (anti-dumping / countervailing) checked against the Official Journal texts on 2026-10-24.
# "covered": CN 2026 codes named in the measure; "ex": CN codes only partly covered (TARIC subdivisions decide).
# The flag never computes a rate: the duty depends on the producer (TARIC additional code) and exemptions.
_COUNTRY = {"CN": "China", "ID": "Indonesia", "MY": "Malaysia", "LK": "Sri Lanka", "TN": "Tunisia", "KH": "Cambodia",
            "PK": "Pakistan", "PH": "the Philippines", "TW": "Taiwan", "VN": "Viet Nam"}
_TRADE_DEFENCE = [
    {"product": "bicycles (not motorised)", "origins": {"CN"},
     "consigned_from": {"ID", "MY", "LK", "TN", "KH", "PK", "PH"},
     "covered": {"87120030"}, "ex": {"87120070"},
     "detail": "anti-dumping duty up to 48.5 %, also extended to bicycles consigned from Indonesia, Malaysia, Sri Lanka, "
               "Tunisia, Cambodia, Pakistan and the Philippines unless made by an exempted producer",
     "ref": "Commission Implementing Regulation (EU) 2025/2146"},
    {"product": "essential bicycle parts (frames, front forks, derailleurs, crank-gear, free-wheel sprocket-wheels, "
                "brakes, brake levers, complete wheels, handlebars)", "origins": {"CN"},
     "covered": {"87149950", "87149630", "87149910"},
     "ex": {"87149110", "87149130", "87149300", "87149420", "87149490", "87149990"},
     "detail": "the bicycle anti-dumping duty (48.5 %) is extended to these parts; assemblers registered under the "
               "exemption scheme of Reg. (EC) No 88/97 can import them duty-suspended. Parts consigned from China count "
               "as Chinese origin unless another origin is proven",
     "ref": "Council Regulation (EC) No 71/97, maintained by Commission Implementing Regulation (EU) 2025/2146 art. 1(5)"},
    {"product": "electric bicycles (pedal assistance, auxiliary electric motor)", "origins": {"CN"},
     "covered": {"87116010"}, "ex": {"87116090"},
     "detail": "anti-dumping duty from 9.9 % to 70.1 % depending on the producer, alongside a countervailing duty",
     "ref": "Commission Implementing Regulation (EU) 2025/120"},
    {"product": "iron or steel fasteners (not stainless): screws, bolts with heads, washers", "origins": {"CN"},
     "covered": {"73181290", "73181491", "73181499", "73181558", "73181568", "73181582", "73181588"},
     "ex": {"73181595", "73182100", "73182200"},
     "detail": "anti-dumping duty from 22.1 % to 86.5 % depending on the producer",
     "ref": "Commission Implementing Regulation (EU) 2022/191"},
    {"product": "aluminium extrusions (bars, rods, profiles, tubes, <= 99.3 % Al)", "origins": {"CN"},
     "covered": {"76042100", "76042910", "76042990", "76082081", "76082089"},
     "ex": {"76041010", "76041090", "76081000", "76109090"},
     "detail": "anti-dumping duty from 21.2 % to 32.1 %; kept in force during the expiry review opened on 27.3.2026 "
               "(notice C/2026/1920)",
     "ref": "Commission Implementing Regulation (EU) 2021/546"},
]
for _m in _TRADE_DEFENCE:
    _m["hs6"] = {c[:6] for c in _m["covered"] | _m["ex"]}


def trade_defence_for(code: str | None, origin_country: str | None) -> list[dict]:
    """Advisory flags for goods/origins with EU anti-dumping or countervailing measures. Never a rate."""
    digits = re.sub(r"\D", "", str(code or ""))
    origin = (origin_country or "").strip().upper()[:2]
    if len(digits) < 6 or not origin:
        return []
    out = []
    for m in _TRADE_DEFENCE:
        via = "origin" if origin in m["origins"] else "consigned" if origin in m.get("consigned_from", set()) else None
        if not via:
            continue
        if len(digits) >= 8:
            cn = digits[:8]
            scope = "covered" if cn in m["covered"] else "partly" if cn in m["ex"] else None
        else:
            scope = "possible" if digits[:6] in m["hs6"] else None
        if not scope:
            continue
        where = _COUNTRY.get(origin, origin)
        lead = {"covered": "", "partly": "Only part of this CN code is covered (TARIC subdivision decides). ",
                "possible": "Depends on the CN code and the exact product. "}[scope]
        frm = (f"originating in {where}" if via == "origin" else
               f"consigned from {where} (measure extended from China)")
        out.append({"kind": "trade_defence", "rule": "H-1", "severity": "review", "scope": scope, "reference": m["ref"],
                    "message": f"EU anti-dumping/countervailing measures exist for {m['product']} {frm}; the duty "
                               f"estimate does not include them — check TARIC measures and additional codes. {lead}"
                               f"{m['detail'][0].upper() + m['detail'][1:]} ({m['ref']})."})
    return out


def _cbam_status(digits: str) -> tuple[str, str] | None:
    """('yes'|'possible', group) or None."""
    group = None
    for prefix, g in _CBAM_INCLUDE:
        if digits.startswith(prefix) or (len(digits) < len(prefix) and prefix.startswith(digits) and len(digits) >= 6):
            group = g
            if prefix.startswith(digits) and len(prefix) > len(digits):
                # e.g. HS6 250700: only CN 2507 00 80 is listed -> depends on CN8
                return "possible", g
            break
    if not group:
        return None
    for ex in _CBAM_EXCLUDE:
        if digits.startswith(ex):
            return None
        if ex.startswith(digits) and len(ex) > len(digits):
            return "possible", group
    return "yes", group


def flags_for(code: str | None, origin_country: str | None = None, description: str = "") -> list[dict]:
    """Deterministic compliance flags for a code and origin (no AI)."""
    _load()
    out: list[dict] = []
    digits = re.sub(r"\D", "", str(code or ""))
    origin = (origin_country or "").strip().upper()[:2]
    if digits:
        valid = len(digits) >= 6 and digits[:6] in _ENTRIES or len(digits) in (2, 4) and digits in _ENTRIES
        if not valid:
            out.append({"kind": "invalid_code", "rule": "H-1", "severity": "review",
                        "message": f"{code} is not a valid {HS_EDITION} code. Check the commodity code."})
        if len(digits) >= 6:
            cb = _cbam_status(digits)
            if cb:
                state, group = cb
                if state == "yes":
                    msg = (f"CBAM goods ({group}, Reg (EU) 2023/956 Annex I): the importer needs CBAM authorised "
                           f"declarant status above 50 t net per year; record net mass, origin and installation.")
                else:
                    msg = (f"Possible CBAM goods ({group}): depends on the CN8 code; check Annex I of "
                           f"Reg (EU) 2023/956 for this subheading.")
                out.append({"kind": "cbam", "rule": "B-1", "severity": "review", "message": msg})
            for prefix, label, severity in _DUAL_USE_HINTS:
                if digits.startswith(prefix):
                    out.append({"kind": "dual_use", "rule": "K-3", "severity": severity,
                                "message": f"Dual-use hint: {label}. Check the item against Annex I of Reg (EU) "
                                           f"2021/821; this is a hint, not a determination."})
                    break
            if digits.startswith("93"):
                out.append({"kind": "military", "rule": "K-3", "severity": "blocking",
                            "message": "Arms and ammunition (chapter 93): import needs a licence; compliance "
                                       "review required before filing."})
    out.extend(trade_defence_for(digits, origin))
    if origin in ("RU", "BY"):
        out.append({"kind": "ru_by_origin", "rule": "K-2", "severity": "blocking",
                    "message": f"Origin {origin}: Russia/Belarus sanctions (Reg 833/2014, 765/2006) — mandatory "
                               f"compliance review; default deny."})
    elif origin == "UA" and description and _OCCUPIED.search(description):
        out.append({"kind": "ru_by_origin", "rule": "K-2", "severity": "blocking",
                    "message": "Goods appear to come from Russian-occupied areas of Ukraine, which are under EU "
                               "embargo — compliance review required."})
    return out


def taric_url(code: str, origin_country: str | None = None, on_date: str | None = None) -> str:
    digits = re.sub(r"\D", "", str(code or ""))[:10].ljust(10, "0")
    if on_date:
        d = re.sub(r"\D", "", str(on_date))[:8]
    else:
        d = datetime.now(timezone.utc).strftime("%Y%m%d")
    url = f"https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp?Lang=en&SimDate={d}&Taric={digits}"
    origin = (origin_country or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", origin):
        url += f"&Area={origin}"
    return url + "&LangDescr=en"


# ---------------------------------------------------------------------------------------------
# AI classifier
# ---------------------------------------------------------------------------------------------
_GENERIC = set("""general cargo goods good spare part parts sample samples various misc miscellaneous gift gifts
accessories accessory equipment merchandise consolidated consolidation said contain stc fak freight all kinds
item items product products material materials stuff assorted mixed personal effects commodity commodities
shipment package packages unit units set sets kit kits""".split())

_SYSTEM = """You are a licensed EU customs classification specialist at a Finnish customs broker. Classify goods in the
Harmonized System 2022 (6 digits) and propose the EU Combined Nomenclature code (8 digits), applying the General
Interpretative Rules (GRI 1-6) and the section and chapter notes.

Per item you get: the goods description from the invoice (any language: Finnish, Swedish, English, German...), the
country of origin, an HS code printed on the invoice if any, and candidate HS 2022 subheadings found by keyword search.
The candidates can be wrong or incomplete.

Rules:
- Per item you see the candidate HEADINGS with ALL their subheadings side by side (compare them: "other" subheadings
  such as 8714 99 often hold goods named only in their CN texts), then other keyword matches. Lines from the same
  invoice are given as context only: classify each item by its own objective characteristics.
- hs6 must be one of the codes shown to you (candidates or expanded subheadings). If the right heading is missing,
  answer that item with {"i": n, "action": "expand", "headings": ["9401", ...]} (up to 4 four-digit headings); you get
  one expansion round, after which you must answer.
- cn8: the 8-digit CN 2026 code. Under a candidate you see its real CN 2026 subdivisions ("CN: ..."); cn8 must be
  one of the subdivisions listed under your hs6, chosen by their texts. Never make up a CN code. If the subdivisions of
  your hs6 are not listed, or the description does not decide between them, set cn8 to null.
- confidence: probability that hs6 is correct (0..1). 0.9+ only when the description fixes material, function and the
  subheading split; lower it when the subheading depends on facts the description does not give. Give the
  alternatives' probabilities on the same scale (they and confidence should not sum above 1).
- If the description is too vague to classify (e.g. "spare parts", "general cargo", "goods"), set
  "insufficient_description": true, confidence <= 0.3 and name the missing facts in "missing_info".
- duty_rate_pct: your best estimate of the EU third-country (erga omnes) conventional duty for cn8, percent ad valorem;
  null if specific/compound or you do not know. It is shown to people as an estimate.
- preference_note: one short sentence if the origin has an EU preferential arrangement that could lower the duty with
  proof of origin, else null.
- Check every numeric threshold in the subheading texts (thickness, width, weight, cylinder capacity, voltage,
  alcohol strength) against the values in the description before choosing; e.g. 2.5 mm is "less than 3mm".
- Parts: ready-to-fit parts (e.g. brake pads on a backing plate) go to the parts heading of the vehicle or machine
  unless a section or chapter note sends them elsewhere; unmounted materials go to their material heading.
- If a printed HS code is given, say in the rationale whether it is right.
- rationale: at most 2 sentences citing the heading text, GRI or notes. alternatives: up to 3 other plausible hs6 codes.
- Write rationale, missing_info, preference_note and alternatives[].why in Finnish. Also provide rationale_i18n
  {fi: str, en: str} for each answer, missing_info_i18n and preference_note_i18n when those fields are non-empty,
  and why_i18n {fi: str, en: str} on each alternative, preserving identical facts.

Reply with JSON only:
{"items": [{"i": 0, "action": "answer", "hs6": "940161", "cn8": "94016100", "confidence": 0.9,
  "insufficient_description": false, "missing_info": null, "rationale": "...",
  "alternatives": [{"hs6": "940169", "confidence": 0.05, "why": "..."}], "duty_rate_pct": 0.0, "preference_note": null}]}"""


def _model_label() -> str | None:
    p = provider_name()
    if p == "none":
        return None
    return f"openai/{config.LLM_MODEL}"


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _hint_check(hint: str | None, hs6: str | None) -> dict | None:
    d = _digits(hint)
    if not d:
        return None
    if len(d) >= 6:
        valid = d[:6] in _ENTRIES
    else:
        valid = len(d) in (2, 4) and d in _ENTRIES
    agrees = None
    if hs6 and valid and len(d) >= 6:
        agrees = d[:6] == hs6
    if not valid:
        note = f"Printed code {hint} does not exist in {HS_EDITION}."
    elif agrees is True:
        note = "Printed code agrees with the suggested subheading."
    elif agrees is False:
        note = f"Printed code {d[:6]} differs from the suggested subheading {hs6}; a person must decide."
    else:
        note = "Printed code exists; not compared."
    return {"code": hint, "valid": valid, "agrees": agrees, "note": note}


def _is_generic(description: str) -> bool:
    words = [_stem(w) for w in re.findall(r"[a-z]+", _fold(description.lower())) if w not in _STOP]
    return bool(words) and all(w in _GENERIC or _stem(w) in {_stem(g) for g in _GENERIC} for w in words)


def _prepare(item: dict) -> dict:
    desc = (item.get("description") or "").strip()
    origin = (item.get("origin_country") or "").strip().upper()[:2] or None
    hint = (item.get("hs_hint") or "").strip() or None
    cands = search(desc, N_CANDIDATES) if desc else []
    hd = _digits(hint)
    if hd and len(hd) >= 4 and hd[:4] in _ENTRIES:
        # The printed code (first) and its heading's other subheadings are always in front of the model.
        own = [c for c in subheadings(hd[:4]) if len(hd) >= 6 and c["code"] == hd[:6]]
        seen = {c["code"] for c in cands} | {c["code"] for c in own}
        extra = [c for c in subheadings(hd[:4]) if c["code"] not in seen]
        cands = own + extra[:8] + [c for c in cands if c["code"] not in {o["code"] for o in own}]
    return {"description": desc, "origin_country": origin, "hs_hint": hint, "candidates": cands}


def _ground(prepared: list[dict]) -> None:
    """Per item: the headings to show in full (its top retrieved headings, the printed code's heading, and the best
    headings of the other lines on the same invoice) and the invoice context. Sets p['headings'], p['context'],
    p['shown'] (every HS6 the model may answer with)."""
    tops = []
    for p in prepared:
        h = [c["code"][:4] for c in p["candidates"][:8]]
        tops.append(list(dict.fromkeys(h)))
    for i, p in enumerate(prepared):
        heads: list[str] = []
        hd = _digits(p["hs_hint"])
        if len(hd) >= 4 and hd[:4] in _ENTRIES:
            heads.append(hd[:4])
        heads += [h for h in tops[i] if h not in heads][:HEADINGS_PER_ITEM]
        others = [t[0] for j, t in enumerate(tops) if j != i and t]
        ranked = sorted(dict.fromkeys(others), key=lambda h: (-others.count(h), others.index(h)))
        heads += [h for h in ranked if h not in heads][:CONTEXT_HEADINGS]
        p["headings"] = heads
        p["context"] = [q["description"][:90] for j, q in enumerate(prepared) if j != i and q["description"]][:8]
        p["shown"] = {c["code"] for c in p["candidates"]} | {c["code"] for h in heads for c in subheadings(h)}


def _base_result(p: dict) -> dict:
    return {
        "description": p["description"], "origin_country": p["origin_country"], "status": "needs_review",
        "reason": None, "hs6": None, "hs6_description": None, "cn8": None, "cn8_note": CN8_NOTE,
        "cn_options": cn_options(_digits(p["hs_hint"])[:6]) if len(_digits(p["hs_hint"])) >= 6 else [],
        "cn8_description": None, "cn_resolution": None, "cn8_proposed": None,
        "confidence": None, "rationale": None,
        "candidates": [{"code": c["code"], "description": c["description"], "confidence": None,
                        "rationale": "Keyword match in the HS 2022 nomenclature"} for c in p["candidates"][:5]],
        "duty": {"rate_pct": None, "basis": DUTY_BASIS, "source": "ai_estimate", "label": DUTY_LABEL,
                 "preference_note": None},
        "flags": flags_for(p["hs_hint"], p["origin_country"], p["description"]) if p["hs_hint"]
        else flags_for(None, p["origin_country"], p["description"]),
        "taric_url": taric_url(p["hs_hint"], p["origin_country"]) if _digits(p["hs_hint"]) else None,
        "hs_hint_check": _hint_check(p["hs_hint"], None), "model": None, "source": "retrieval_only",
        "classified_at": now_iso(),
    }


CN_SHOWN_PER_HS6 = 8


def _cn_lines(code: str) -> list[str]:
    opts = _CN_BY_HS6.get(code, [])
    out = [f"    CN: {c} {_CN[c][:100]}" for c in opts[:CN_SHOWN_PER_HS6]]
    if len(opts) > CN_SHOWN_PER_HS6:
        out.append(f"    CN: (+{len(opts) - CN_SHOWN_PER_HS6} more subdivisions; set cn8 null if none listed fits)")
    return out


def _item_block(n: int, p: dict, extra: list[dict] | None = None) -> str:
    lines = [f"Item {n}", f"Description: {p['description'] or '(empty)'}",
             f"Origin: {p['origin_country'] or 'unknown'}"]
    if p["hs_hint"]:
        e = hs_entry(p["hs_hint"])
        lines.append(f"Printed HS code: {p['hs_hint']}" + (f" ({e['code']} {e['description']})" if e else
                                                          f" (not a valid {HS_EDITION} code)"))
        hd = _digits(p["hs_hint"])
        if len(hd) >= 8:
            lines.append(f"Printed CN code {hd[:8]}: " + (f"exists in {CN_EDITION}" if hd[:8] in _CN else
                                                         f"NOT a {CN_EDITION} code"))
    if p.get("context"):
        lines.append("Other lines on the same invoice (context only): " + "; ".join(f'"{c}"' for c in p["context"]))
    listed: set[str] = set()
    if extra:
        lines.append("Expanded subheadings you asked for:")
        for c in extra:
            lines.append(f"- {c['code']} | {c['description']}")
            lines.extend(_cn_lines(c["code"]))
            listed.add(c["code"])
    lines.append("Candidate headings, every subheading side by side, with CN 2026 subdivisions:")
    for h in p.get("headings") or []:
        he = _ENTRIES.get(h)
        if not he:
            continue
        ch = _ENTRIES.get(h[:2], {}).get("description", "")
        lines.append(f"== {h} {he['description'][:200]} (chapter {h[:2]}: {ch[:70]})")
        for c in subheadings(h):
            if c["code"] in listed:
                continue
            lines.append(f"- {c['code']} | {c['description']}")
            lines.extend(_cn_lines(c["code"]))
            listed.add(c["code"])
    rest = [c for c in p["candidates"] if c["code"] not in listed]
    if rest:
        lines.append("Other keyword matches (code | subheading | heading):")
        for c in rest:
            lines.append(f"- {c['code']} | {c['description']} | {(c.get('heading_description') or '')[:140]}")
            # Every selectable candidate needs its reference data. Omitting CN
            # texts by retrieval rank made the model report missing information
            # even for fully described goods (R7 bicycle light, residual rank 6).
            lines.extend(_cn_lines(c["code"]))
    return "\n".join(lines)


def _chapter_list() -> str:
    return "\n".join(f"{c['code']} {c['description'][:90]}" for c in chapters())


def _num(v, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x):
        return None
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _apply_answer(res: dict, p: dict, ans: dict, shown: set[str], model: str | None) -> dict:
    res = dict(res)
    res["model"] = model
    res["source"] = "ai"
    hs6 = _digits(ans.get("hs6"))[:6]
    conf = _num(ans.get("confidence"), 0.0, 1.0)
    missing = (ans.get("missing_info") or "").strip()
    # A model can supply missing facts while incorrectly leaving its boolean false.
    # Either field makes the classification unresolved; nomenclature membership
    # alone cannot establish which CN subdivision describes the goods.
    insufficient = bool(ans.get("insufficient_description") or missing)
    rationale = (ans.get("rationale") or "").strip() or None
    rationale_i18n = dict(ans.get("rationale_i18n") or {})
    if hs6 and hs6 not in _ENTRIES:
        res.update(status="needs_review", reason=f"The model proposed {hs6}, which is not a {HS_EDITION} code; pick "
                                                 f"from the candidates.", rationale=rationale)
        res["reason_i18n"] = {"fi": f"Mallin ehdottama {hs6} ei ole HS {HS_EDITION} -nimike. Valitse nimike ehdokkaista.", "en": res["reason"]}
        return res
    if hs6 and hs6 not in shown and rationale:
        rationale += " (Code chosen outside the retrieved candidates; verified to exist in HS 2022.)"
        for lang,note in {"fi":" (Nimike valittiin haettujen ehdokkaiden ulkopuolelta; olemassaolo tarkistettu HS 2022 -nimikkeistöstä.)", "en":" (Code chosen outside the retrieved candidates; verified to exist in HS 2022.)"}.items():
            if rationale_i18n.get(lang): rationale_i18n[lang] += note
    alts = []
    for a in ans.get("alternatives") or []:
        ac = _digits(a.get("hs6") or a.get("code"))[:6]
        if ac and ac in _ENTRIES and ac != hs6 and ac not in {x["code"] for x in alts}:
            alts.append({"code": ac, "description": _ENTRIES[ac]["description"],
                         "confidence": _num(a.get("confidence"), 0.0, 1.0), "rationale": (a.get("why") or "").strip() or None, "rationale_i18n": a.get("why_i18n")})
    alts.sort(key=lambda a: -(a.get("confidence") or 0))
    cands = []
    if hs6:
        cands.append({"code": hs6, "description": _ENTRIES[hs6]["description"], "confidence": conf, "rationale": rationale, "rationale_i18n": rationale_i18n or None})
    cands.extend(alts[:3])
    for c in p["candidates"]:
        if len(cands) >= 5:
            break
        if c["code"] not in {x["code"] for x in cands}:
            cands.append({"code": c["code"], "description": c["description"], "confidence": None,
                          "rationale": "Keyword match in the HS 2022 nomenclature"})
    res["candidates"] = cands
    res["rationale"] = rationale
    if rationale_i18n:
        res["rationale_i18n"] = rationale_i18n
    res["confidence"] = conf
    if hs6:
        res["hs6"] = hs6
        res["hs6_description"] = _ENTRIES[hs6]["description"]
        proposed = _digits(ans.get("cn8"))[:8] or None
        cn = resolve_cn(hs6, proposed)
        if insufficient and len(cn["cn_options"]) > 1:
            cn = resolve_cn(hs6, None)
        res["cn8"] = cn["cn8"]
        res["cn_options"] = cn["cn_options"]
        res["cn8_description"] = _CN.get(cn["cn8"] or "")
        res["cn_resolution"] = cn["resolved"]
        res["cn8_proposed"] = proposed
        if cn["resolved"] != "valid":
            res["cn8_note"] = cn["note"]
        rate = _num(ans.get("duty_rate_pct"), 0.0, 100.0)
        res["duty"] = {"rate_pct": rate, "basis": DUTY_BASIS, "source": "ai_estimate", "label": DUTY_LABEL,
                       "preference_note": (ans.get("preference_note") or None), "preference_note_i18n": ans.get("preference_note_i18n")}
        res["flags"] = flags_for(res["cn8"] or hs6, p["origin_country"], p["description"])
        if p["hs_hint"] and not (hint := _hint_check(p["hs_hint"], hs6))["valid"]:
            res["flags"].append({"kind": "invalid_code", "rule": "H-1", "severity": "review", "message": hint["note"]})
        res["taric_url"] = taric_url(res["cn8"] or hs6, p["origin_country"])
    res["hs_hint_check"] = _hint_check(p["hs_hint"], res["hs6"])
    if insufficient:
        res.update(status="needs_review", reason="Goods description is too vague to classify (RULE S-2)"
                                                 + (f": {missing}" if missing else ""))
    elif not hs6:
        res.update(status="needs_review", reason="The model could not pick a subheading; choose from the candidates.")
    elif conf is None or conf < STATUS_OK_MIN_CONFIDENCE:
        res.update(status="needs_review", reason=f"Confidence {0 if conf is None else conf:.2f} is below "
                                                 f"{STATUS_OK_MIN_CONFIDENCE:.2f}; a broker must pick the code.")
    elif alts and (alts[0].get("confidence") or 0) >= conf - STATUS_OK_MIN_MARGIN:
        res.update(status="needs_review", reason=f"Close call: {alts[0]['code']} ({alts[0]['confidence']:.2f}) is within "
                                                 f"{STATUS_OK_MIN_MARGIN:.2f} of {hs6} ({conf:.2f}); a broker must decide.")
    else:
        res.update(status="ok", reason=None)
    if res.get("reason"):
        if insufficient:
            extra = (ans.get("missing_info_i18n") or {}).get("fi") or missing
            fi_reason = "Tavarankuvaus ei riitä luokitteluun (sääntö S-2)." + (f" {extra}" if extra else "")
        elif not hs6:
            fi_reason = "AI ei löytänyt tarkkaa alanimikettä. Valitse nimike ehdokkaista."
        elif conf is None or conf < STATUS_OK_MIN_CONFIDENCE:
            confidence_fi = f"{conf or 0:.2f}".replace(".", ",")
            threshold_fi = f"{STATUS_OK_MIN_CONFIDENCE:.2f}".replace(".", ",")
            fi_reason = f"Varmuus {confidence_fi} alittaa rajan {threshold_fi}. Tulliasiantuntijan on valittava nimike."
        else:
            fi_reason = f"Nimikkeet {alts[0]['code']} ja {hs6} ovat lähellä toisiaan. Tulliasiantuntijan on ratkaistava luokittelu."
        en_reason = res["reason"]
        if insufficient and (ans.get("missing_info_i18n") or {}).get("en"):
            en_reason = "Goods description is too vague to classify (RULE S-2): " + ans["missing_info_i18n"]["en"]
        res["reason_i18n"] = {"fi": fi_reason, "en": en_reason}
    return res


async def _ask(prepared: list[tuple[int, dict]], extras: dict[int, list[dict]], final: bool,
               ref: dict | None, company_id: str | None) -> dict[int, dict]:
    blocks = [_item_block(n, p, extras.get(n)) for n, p in prepared]
    head = ("EXPANSION ROUND: answer every item now (action \"answer\"); no further expansion.\n\n" if final else "")
    prompt = (f"{head}HS 2022 chapters:\n{_chapter_list()}\n\nItems to classify:\n\n" + "\n\n".join(blocks))
    max_tokens = min(10000, 700 + 700 * len(prepared))
    # Stability comes from grounding (full sibling
    # subheadings + real CN texts) and the confidence/margin rule that sends close calls to a broker.
    data = await llm_json(AGENT, _SYSTEM, prompt, ref=ref, company_id=company_id, max_tokens=max_tokens)
    items = data.get("items") if isinstance(data, dict) else data
    out: dict[int, dict] = {}
    from services.locale_backfill import bilingual_text
    for a in items or []:
        if not isinstance(a, dict):
            continue
        if a.get("action") != "expand":
            try:
                bilingual_text(a, ("rationale",))
            except ValueError as exc:
                # The rationale is required in both languages; without it only this item goes to a person (r3 N2).
                a = {"i": a.get("i"), "action": "invalid", "error": str(exc)}
            else:
                for field in ("missing_info", "preference_note"):
                    _optional_bilingual(a, field)
                for alternative in a.get("alternatives") or []:
                    if isinstance(alternative, dict):
                        _optional_bilingual(alternative, "why")
        try:
            out[int(a.get("i"))] = a
        except (TypeError, ValueError):
            continue
    return out


def _optional_bilingual(obj: dict, field: str) -> None:
    """An optional note without both languages keeps its text and loses the pair (the locale backfill translates it
    later) instead of failing the whole batch: one omitted preference_note_i18n rejected every item on live (r3 N2)."""
    from services.locale_backfill import bilingual_text
    if obj.get(field):
        try:
            bilingual_text(obj, (field,))
        except ValueError:
            log.info("hs answer: %s without both languages; kept as written", field)
            obj[field + "_i18n"] = None


async def classify_items(items: list[dict], *, ref: dict | None = None, company_id: str | None = None) -> list[dict]:
    """Classify invoice lines. One AI call per batch of up to 12 items (+ one expansion round)."""
    _load()
    prepared = [_prepare(it or {}) for it in items]
    _ground(prepared)
    results = [_base_result(p) for p in prepared]
    todo: list[int] = []
    for i, p in enumerate(prepared):
        if not p["description"]:
            results[i]["reason"] = "Describe the goods to classify them."
        elif _is_generic(p["description"]):
            results[i]["reason"] = ("Goods description is too generic to classify (RULE S-2): say what the goods are, "
                                    "their material and use.")
        else:
            todo.append(i)
    if not todo:
        return results
    model = _model_label()
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        batch = [(n, prepared[i]) for n, i in enumerate(chunk)]
        try:
            answers = await _ask(batch, {}, False, ref, company_id)
            expand = {n: a for n, a in answers.items() if (a.get("action") or "answer") == "expand"}
            extras: dict[int, list[dict]] = {}
            if expand:
                for n, a in expand.items():
                    seen = set(batch[n][1]["shown"])
                    more: list[dict] = []
                    for h in (a.get("headings") or [])[:4]:
                        more.extend(c for c in subheadings(_digits(h)[:4]) if c["code"] not in seen)
                    extras[n] = more[:60]
                second = await _ask([batch[n] for n in sorted(expand)], extras, True, ref, company_id)
                answers.update(second)
            for n, i in enumerate(chunk):
                a = answers.get(n)
                shown = prepared[i]["shown"] | {c["code"] for c in extras.get(n, [])}
                if a and a.get("action") == "invalid":
                    results[i].update(reason=f"AI classification failed ({a['error'][:120]}); pick the code from the candidates.",
                                      model=model, source="ai")
                    continue
                if not a or (a.get("action") or "answer") != "answer":
                    results[i].update(reason="The model gave no answer for this item; pick from the candidates.",
                                      model=model, source="ai")
                    continue
                results[i] = _apply_answer(results[i], prepared[i], a, shown, model)
        except LLMUnavailable:
            for i in chunk:
                results[i]["reason"] = "AI is not configured; pick the code from the candidates."
        except Exception as exc:  # noqa: BLE001 - model/API failure must not break drafting
            log.warning("hs classification failed: %s", exc)
            for i in chunk:
                results[i]["reason"] = f"AI classification failed ({str(exc)[:120]}); pick the code from the candidates."
    from services import ebti  # official BTI precedents: an independent check of the AI's code
    await ebti.annotate(prepared, results, ref=ref, company_id=company_id)
    return results


async def suggest(description: str, origin_country: str | None = None, *, hs_hint: str | None = None,
                  ref: dict | None = None, company_id: str | None = None) -> dict:
    res = await classify_items([{"description": description, "origin_country": origin_country, "hs_hint": hs_hint}],
                               ref=ref, company_id=company_id)
    return res[0]
