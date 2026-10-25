"""Benchmark freight levels and estimate models for lanes without a rate (s5).

Stored levels (Mongo `benchmark_indices`, one document per index lane and assessment date; the newest is used):
- sea: Drewry World Container Index (WCI) lanes and Freightos Baltic Index (FBX) lanes, USD per 40' container;
- air: TAC Index / Baltic Air Freight Index and Freightos Air Index lanes, USD per chargeable kg;
- road / rail: published per-km and per-container levels where they exist (Upply × Ti × IRU European Road Freight
  Rate Benchmark, DAT, China–Europe rail), else Huolitsija's own per-km model, each with its date and basis.
Seed values: data/benchmark_indices.json (every value with its publication date, URL and the quoted sentence).
FX: ECB euro reference rates from Mongo `fx_rates` — the store services.customs keeps from the ECB's public XML
({date, rates{USD, ...}}); this module only reads it and asks customs to refresh it.

Models (all results say how they were derived and carry `as_of`, so a quote never shows a modelled price as a
carrier rate):
- sea FCL: the index lane of the region pair when it exists; otherwise a calibrated route-length model
  k · (0.35 + 0.65 · nm/11 000) · direction · thin-trade, with k from the index lane nearest in market (same
  destination region, else same origin region, else the median of all lanes), plus a transshipment premium when the
  region pair has no direct mainline service. 20' = 0.6 × 40'; 40' HC = 40'; 45' = 1.15 ×; reefers 1.1 / 1.9 ×.
- sea LCL: 40' level / 25 per W/M (min 1) + CFS both ends.
- air: index lane, else k · (0.6 + 0.25 · km/1000) · direction, weight-break multipliers for small shipments.
- road FTL: EUR/km by region, minimum per trip, +EUR 120 and +1 day per customs border; groupage (LTL) as a share
  of the truck by chargeable weight (333 kg/m³, 1 850 kg/ldm) with groupage factors; container haulage per box-km.
- rail: EUR per FEU-km by network, China–Europe (Middle Corridor) from its published per-FEU level, lifts per end.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.db import db, register_index
from core.lifecycle import on_startup
from core.jobs import job, schedule
from core.util import new_id, now_iso

log = logging.getLogger("benchmarks")
DATA = Path(__file__).resolve().parent.parent / "data"

register_index("benchmark_indices", [("lane_code", 1), ("as_of", -1)])
register_index("benchmark_indices", "id", unique=True)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
DREWRY_WCI_URL = "https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry"

# ------------------------------------------------------------------------------------------------ region rules
PRODUCERS = {"FEA", "SEA", "ISC"}
CONSUMERS = {"NEU", "MED", "NAW", "NAE", "USG"}
THIN = {"WAF": 1.35, "EAF": 1.3, "RSE": 1.2, "SAW": 1.1, "OCE": 1.15, "CAR": 1.1, "SAF": 1.1, "SAE": 1.05}
# Region pairs with direct deep-sea mainline services (either direction). Others need one transshipment.
_DIRECT = {
    "FEA": "SEA ISC MEG RSE MED NEU WAF SAF EAF NAW NAE USG CAR SAE SAW OCE",
    "SEA": "ISC MEG RSE MED NEU WAF SAF EAF NAW NAE USG SAE OCE",
    "ISC": "MEG RSE MED NEU NAE USG EAF",
    "MEG": "RSE MED NEU EAF NAE",
    "MED": "NEU NAE USG CAR SAE WAF RSE",
    "NEU": "NAE USG CAR SAE SAW WAF SAF EAF OCE",
    "NAE": "CAR SAE",
    "NAW": "SAW CAR OCE",
    "USG": "CAR SAE",
    "CAR": "SAW SAE",
}
DIRECT_PAIRS = {frozenset((a, b)) for a, bs in _DIRECT.items() for b in bs.split()}

# Map an index lane's places to trade regions (sea) / air regions.
SEA_REGION_OF_PLACE = {
    "shanghai": "FEA", "china": "FEA", "east asia": "FEA", "china/east asia": "FEA", "rotterdam": "NEU",
    "north europe": "NEU", "n. europe": "NEU", "northern europe": "NEU", "europe": "NEU", "genoa": "MED",
    "mediterranean": "MED", "los angeles": "NAW", "north america west coast": "NAW", "us west coast": "NAW",
    "new york": "NAE", "north america east coast": "NAE", "us east coast": "NAE", "south america east coast": "SAE",
    "south america west coast": "SAW",
}
AIR_REGION = {}
for _r, _cs in {
    "EUR": "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS CH GB TR UA MD RS BA ME MK AL",
    "NAM": "US CA",
    "LAM": "MX GT BZ SV HN NI CR PA CO VE EC PE BO CL AR PY UY BR GY SR CU DO HT JM PR TT BS BB",
    "FEA": "CN HK MO TW KR JP MN",
    "SEA": "VN TH MY SG ID PH KH MM LA BN",
    "ISC": "IN PK BD LK NP MV",
    "MEA": "AE SA QA OM BH KW IQ JO LB IL IR GE AM AZ KZ UZ TM KG TJ AF",
    "OCE": "AU NZ PG FJ NC PF",
}.items():
    for _c in _cs.split():
        AIR_REGION[_c] = _r
AIR_REGION_OF_PLACE = {"hong kong": "FEA", "shanghai": "FEA", "china": "FEA", "asia": "FEA", "frankfurt": "EUR",
                       "europe": "EUR", "london": "EUR", "north america": "NAM", "us": "NAM", "usa": "NAM",
                       "chicago": "NAM", "new york": "NAM", "southeast asia": "SEA", "south asia": "ISC",
                       "middle east": "MEA", "latin america": "LAM", "south america": "LAM", "africa": "AFR"}
AIR_PRODUCERS = {"FEA", "SEA", "ISC"}


def air_region(country: str | None) -> str:
    return AIR_REGION.get((country or "").upper(), "AFR")


# Local charges by trade region, USD per 20'/40' (terminal handling) — Huolitsija model, typical 2026 tariffs.
THC_USD = {"FEA": (120, 180), "SEA": (110, 165), "ISC": (110, 170), "MEG": (150, 220), "RSE": (180, 260),
           "MED": (200, 280), "NEU": (220, 300), "WAF": (290, 420), "SAF": (210, 300), "EAF": (250, 360),
           "NAW": (360, 470), "NAE": (360, 470), "USG": (330, 430), "CAR": (210, 300), "SAE": (240, 340),
           "SAW": (200, 300), "OCE": (300, 400)}
CFS_USD_PER_WM = {"origin": 25.0, "destination": 45.0}
DOC_USD = 60.0

# Road: EUR per km for a full 13.6 m trailer, by country group (Huolitsija model unless an index level overrides it).
ROAD_GROUPS = {
    "NORDIC": "FI SE NO DK IS",
    "WEST_EU": "DE NL BE LU FR AT CH LI IT ES PT IE GB MC AD SM",
    "EAST_EU": "PL CZ SK HU SI HR RO BG LT LV EE GR RS BA ME MK AL MD UA TR XK CY MT",
    "NORTH_AMERICA": "US CA",
    "MEXICO_CA": "MX GT BZ SV HN NI CR PA",
    "SOUTH_AMERICA": "CO VE EC PE BO CL AR PY UY BR GY SR GF",
    "AFRICA": "",  # default for African countries below
    "MIDDLE_EAST": "AE SA QA OM BH KW IQ JO LB IL PS YE",
    "CAUCASUS_CA": "GE AM AZ KZ UZ TM KG TJ AF MN",
    "SOUTH_ASIA": "IN PK BD LK NP BT MV",
    "CHINA": "CN HK MO",
    "SE_ASIA": "VN TH MY SG ID PH KH MM LA BN TL",
    "JAPAN": "JP", "KOREA": "KR", "TAIWAN": "TW",
    "OCEANIA": "AU NZ PG FJ",
}
ROAD_GROUP_OF = {c: g for g, cs in ROAD_GROUPS.items() for c in cs.split()}
ROAD_GROUP_NAMES = {"NORDIC": ("Nordic", "Pohjoismaat"), "WEST_EU": ("Western Europe", "Länsi-Eurooppa"),
                    "EAST_EU": ("Eastern Europe", "Itä-Eurooppa"), "NORTH_AMERICA": ("North America", "Pohjois-Amerikka"),
                    "MEXICO_CA": ("Mexico and Central America", "Meksiko ja Keski-Amerikka"),
                    "SOUTH_AMERICA": ("South America", "Etelä-Amerikka"), "AFRICA": ("Africa", "Afrikka"),
                    "MIDDLE_EAST": ("Middle East", "Lähi-itä"), "CAUCASUS_CA": ("Caucasus and Central Asia", "Kaukasia ja Keski-Aasia"),
                    "SOUTH_ASIA": ("South Asia", "Etelä-Aasia"), "CHINA": ("China", "Kiina"),
                    "SE_ASIA": ("Southeast Asia", "Kaakkois-Aasia"), "JAPAN": ("Japan", "Japani"), "KOREA": ("South Korea", "Etelä-Korea"),
                    "TAIWAN": ("Taiwan", "Taiwan"), "OCEANIA": ("Oceania", "Oseania")}
ROAD_EUR_PER_KM = {"NORDIC": 1.95, "WEST_EU": 1.80, "EAST_EU": 1.40, "NORTH_AMERICA": 1.30, "MEXICO_CA": 1.10,
                   "SOUTH_AMERICA": 1.20, "AFRICA": 2.00, "MIDDLE_EAST": 1.10, "CAUCASUS_CA": 1.30, "SOUTH_ASIA": 0.70,
                   "CHINA": 0.85, "SE_ASIA": 0.95, "JAPAN": 2.60, "KOREA": 1.60, "TAIWAN": 1.50, "OCEANIA": 2.10}
ROAD_MIN_EUR = 320.0
ROAD_FIXED_EUR = 150.0  # loading / unloading time per full truck
BORDER_EUR = 120.0
ROAD_KM_PER_DAY = 600.0
# Groupage share of a full truck by chargeable kg: (max kg, factor on the pro-rata truck cost)
LTL_FACTORS = [(500, 3.0), (2500, 2.4), (5000, 1.9), (10000, 1.6), (None, 1.35)]
LTL_MIN_EUR = 65.0

RAIL_EUR_PER_FEU_KM = {"EURASIA": 0.95, "NORTH_AMERICA": 0.55, "INDIA": 0.45, "AUSTRALIA": 0.75,
                       "SOUTHERN_AFRICA": 0.90, "JAPAN": 1.20, "KOREA": 0.90, "CHINA": 0.40}
RAIL_NETWORK_NAMES = {"EURASIA": ("Eurasia", "Euraasia"), "NORTH_AMERICA": ("North America", "Pohjois-Amerikka"),
                      "INDIA": ("India", "Intia"), "AUSTRALIA": ("Australia", "Australia"),
                      "SOUTHERN_AFRICA": ("Southern Africa", "eteläinen Afrikka"), "JAPAN": ("Japan", "Japani"),
                      "KOREA": ("South Korea", "Etelä-Korea"), "CHINA": ("China", "Kiina")}
RAIL_KM_PER_DAY = {"EURASIA": 550.0, "NORTH_AMERICA": 900.0, "INDIA": 450.0, "AUSTRALIA": 900.0,
                   "SOUTHERN_AFRICA": 350.0, "JAPAN": 700.0, "KOREA": 500.0, "CHINA": 700.0}
RAIL_LIFT_EUR = 65.0
MODEL_AS_OF = "2026-10-24"
MODEL_SOURCE = "Huolitsija cost model (per-km levels by region, reviewed 2026-10-24)"


def road_group(country: str | None) -> str:
    c = (country or "").upper()
    if c in ROAD_GROUP_OF:
        return ROAD_GROUP_OF[c]
    from services.hubs import LANDMASS

    return "AFRICA" if LANDMASS.get(c) == "AFRICA" else "SE_ASIA" if c else "WEST_EU"


# ------------------------------------------------------------------------------------------------ storage
_cache: dict = {"at": 0.0, "levels": None, "fx": None}


def _seed_file() -> dict:
    path = DATA / "benchmark_indices.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"series": [], "fx": []}


@on_startup(order=14)
async def load_seed():
    """Insert the seed levels that are not stored yet (by lane and date). Newer stored levels always win."""
    seed = _seed_file()
    added = 0
    for s in seed.get("series") or []:
        if s.get("value") is None or not s.get("lane_code") or not s.get("as_of"):
            continue
        if await db.benchmark_indices.find_one({"lane_code": s["lane_code"], "as_of": s["as_of"]}):
            continue
        await db.benchmark_indices.insert_one({**s, "id": new_id(), "loaded_at": now_iso(), "loaded_by": "seed"})
        added += 1
    by_day: dict[str, dict] = {}
    for f in seed.get("fx") or []:  # same shape as services.customs stores ECB days
        if f.get("value") and f.get("as_of") and str(f.get("pair", "")).startswith("EUR"):
            by_day.setdefault(f["as_of"], {})[f["pair"][3:]] = float(f["value"])
    for day, rates in by_day.items():
        if not await db.fx_rates.find_one({"date": day}):
            await db.fx_rates.update_one({"date": day}, {"$setOnInsert": {"date": day, "rates": rates, "source": "ECB",
                                                                           "seeded_from": "benchmark_indices.json"}}, upsert=True)
    if added:
        log.info("benchmark seed: %s levels added", added)
    _cache["at"] = 0.0


async def levels(force: bool = False) -> dict:
    """lane_code -> newest stored level (cached 60 s)."""
    if not force and _cache["levels"] is not None and time.monotonic() - _cache["at"] < 60:
        return _cache["levels"]
    out: dict[str, dict] = {}
    async for s in db.benchmark_indices.find({"status": {"$ne": "rejected"}}, {"_id": 0}).sort("as_of", -1):
        out.setdefault(s["lane_code"], s)
    fx = await db.fx_rates.find_one({"rates.USD": {"$exists": True}}, {"_id": 0}, sort=[("date", -1)])
    _cache.update(at=time.monotonic(), levels=out, fx=fx)
    return out


async def eur_per(currency: str) -> tuple[float, dict]:
    """EUR for one unit of `currency` from the newest ECB day -> (factor, {pair, value, as_of, source_url})."""
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return 1.0, {"pair": "EUREUR", "value": 1.0, "as_of": None}
    await levels()
    day = _cache["fx"] or {}
    rate = (day.get("rates") or {}).get(cur)
    if not rate:
        raise LookupError(f"No ECB rate for {cur}")
    return 1.0 / float(rate), {"pair": f"EUR{cur}", "value": float(rate), "as_of": day.get("date"), "source_url": ECB_URL}


async def eur_per_usd() -> tuple[float, dict]:
    try:
        return await eur_per("USD")
    except LookupError:
        return 1 / 1.1193, {"pair": "EURUSD", "value": 1.1193, "as_of": "2026-10-23", "source_url": ECB_URL,
                            "note": "seed value: fx_rates empty"}


# Finnish for the lane labels of the index series (data/benchmark_indices.json and ops-loaded levels), longest first.
LABEL_FI = [
    ("DHL Express Worldwide export from Finland, zone", "DHL Express Worldwide -vienti Suomesta, vyöhyke"),
    ("US/Canada full truck (DAT dry van spot incl. fuel)", "Yhdysvallat ja Kanada, täysi kuorma (DAT dry van spot, sis. polttoaineen)"),
    ("India full truck (Shriram Delhi–Kolkata–Delhi rental)", "Intia, täysi kuorma (Shriram, Delhi–Kolkata–Delhi-vuokra)"),
    ("China–Europe rail, average cost (COC)", "Kiina–Eurooppa-rautatie, keskihinta (COC)"),
    ("All cargo to/from/across the Gulf region", "kaikki rahti Persianlahden alueelle, alueelta ja sen kautta"),
    ("(monthly avg, spot+contract)", "(kuukausikeskiarvo, spot ja sopimus)"),
    ("North America West Coast", "Pohjois-Amerikan länsirannikko"), ("North America East Coast", "Pohjois-Amerikan itärannikko"),
    ("South America West Coast", "Etelä-Amerikan länsirannikko"), ("South America East Coast", "Etelä-Amerikan itärannikko"),
    ("Northern Europe → United States", "Pohjois-Eurooppa → Yhdysvallat"), ("South China → Durban (South Africa)", "Etelä-Kiina → Durban (Etelä-Afrikka)"),
    ("Composite Index", "yhdistelmäindeksi"), ("Global container index", "maailmanlaajuinen konttiindeksi"),
    ("China/East Asia", "Kiina ja Itä-Aasia"), ("North Europe", "Pohjois-Eurooppa"), ("North America", "Pohjois-Amerikka"),
    ("Mediterranean", "Välimeri"), ("Europe", "Eurooppa"), ("China", "Kiina"), ("Hong Kong", "Hongkong"), ("Genoa", "Genova"),
    ("(weekly)", "(viikoittainen)"),
]


def label_fi(label: str | None) -> str | None:
    if not label:
        return label
    out = label
    for en, fi in LABEL_FI:
        out = out.replace(en, fi)
    return out


def date_fi(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "")
    return f"{d.day}.{d.month}.{d.year}"


def num_en(x: float) -> str:
    """12345.6 -> "12 346": thin grouping that reads the same in English and Finnish notes."""
    return f"{x:,.0f}".replace(",", " ")


def num_fi(x: float, digits: int = 0) -> str:
    """1234.5 -> "1 234,50" style (Finnish grouping, decimal comma)."""
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def _src(level: dict | None, *, kind: str, note: str, note_fi: str | None = None, derived_from: list[dict] | None = None) -> dict:
    if level:
        return {"kind": kind, "index": level.get("index"), "lane_code": level.get("lane_code"), "lane": level.get("label"),
                "lane_fi": label_fi(level.get("label")), "as_of": level.get("as_of"), "url": level.get("source_url"),
                "value": level.get("value"), "unit": level.get("unit"), "note": note, "note_fi": note_fi or label_fi(note),
                "derived_from": [{k: d.get(k) for k in ("index", "lane_code", "label", "value", "unit", "as_of")}
                                 | {"label_fi": label_fi(d.get("label"))} for d in derived_from or []]}
    return {"kind": kind, "index": None, "as_of": MODEL_AS_OF, "url": None, "note": note, "note_fi": note_fi or note,
            "model": MODEL_SOURCE}


def _region_pair(level: dict, table: dict) -> tuple[str | None, str | None]:
    if level.get("origin_region") and level.get("destination_region"):
        return level["origin_region"], level["destination_region"]

    def reg(text):
        t = (text or "").strip().lower()
        if t in table:
            return table[t]
        for k, v in table.items():
            if k in t:
                return v
        return None

    return reg(level.get("origin")), reg(level.get("destination"))


# ------------------------------------------------------------------------------------------------ sea
def nm_between(km: float) -> float:
    return km / 1.852


def _sea_r(o_reg: str, d_reg: str, km: float) -> float:
    return (0.35 + 0.65 * nm_between(km) / 11000.0) * _direction(o_reg, d_reg, PRODUCERS, CONSUMERS) * max(
        THIN.get(o_reg, 1.0), THIN.get(d_reg, 1.0))


def _direction(o: str, d: str, producers: set, consumers: set | None = None) -> float:
    if o in producers and d not in producers:
        return 1.0
    if d in producers and o not in producers:
        return 0.55
    return 0.8


SEA_REF_KM = {("FEA", "NEU"): 21500, ("FEA", "MED"): 16500, ("FEA", "NAW"): 10500, ("FEA", "NAE"): 19500,
              ("NEU", "NAE"): 6500, ("NEU", "SAE"): 10500, ("NEU", "SAW"): 11500, ("FEA", "SAE"): 20000,
              ("FEA", "SAW"): 18500, ("FEA", "OCE"): 8500, ("FEA", "WAF"): 20000, ("FEA", "EAF"): 11500,
              ("FEA", "ISC"): 7000, ("FEA", "MEG"): 10500, ("FEA", "SEA"): 3000}


def _sea_anchor_k(lv: dict) -> tuple[dict[tuple, dict], list[tuple[float, dict]]]:
    pairs: dict[tuple, dict] = {}
    ks: list[tuple[float, dict]] = []
    for lvl in lv.values():
        if lvl.get("mode") != "sea" or (lvl.get("unit") or "").upper() not in ("USD/FEU", "USD/40FT"):
            continue
        o, d = _region_pair(lvl, SEA_REGION_OF_PLACE)
        if not o or not d or o == d:
            continue
        key = (o, d)
        # WCI preferred over FBX for the same pair (it names exact port pairs); newest otherwise
        cur = pairs.get(key)
        if cur is None or (lvl.get("index") == "WCI" and cur.get("index") != "WCI") or (
                lvl.get("index") == cur.get("index") and lvl.get("as_of", "") > cur.get("as_of", "")):
            pairs[key] = lvl
    for (o, d), lvl in pairs.items():
        ref = SEA_REF_KM.get((o, d)) or SEA_REF_KM.get((d, o)) or 12000
        ks.append((float(lvl["value"]) / _sea_r(o, d, ref), lvl))
    return pairs, ks


async def sea_feu_usd(o_reg: str, d_reg: str, sea_km: float) -> tuple[float, dict]:
    """Ocean freight for one 40' container between trade regions -> (USD, source)."""
    lv = await levels()
    pairs, ks = _sea_anchor_k(lv)
    if (o_reg, d_reg) in pairs:
        lvl = pairs[(o_reg, d_reg)]
        return float(lvl["value"]), _src(lvl, kind="benchmark", note=f"{lvl.get('index')} {lvl.get('label')}")
    if not ks:
        usd = 2200.0 * _sea_r(o_reg, d_reg, sea_km)
        return usd, _src(None, kind="model", note="No index level stored; route-length model at USD 2 200 per FEU scale",
                         note_fi="Indeksitasoa ei ole tallennettu; reitin pituuteen perustuva malli, taso 2 200 USD/FEU")
    same_d = [(k, l) for k, l in ks if _region_pair(l, SEA_REGION_OF_PLACE)[1] == d_reg]
    same_o = [(k, l) for k, l in ks if _region_pair(l, SEA_REGION_OF_PLACE)[0] == o_reg]
    pool = same_d or same_o or ks
    pool = sorted(pool, key=lambda x: x[0])
    k, ref = pool[len(pool) // 2]
    usd = k * _sea_r(o_reg, d_reg, sea_km)
    if frozenset((o_reg, d_reg)) not in DIRECT_PAIRS:
        usd += 450.0  # one transshipment (hub handling + feeder slot)
    note = (f"Derived from {ref.get('index')} {ref.get('label')} ({ref.get('as_of')}) scaled by route length "
            f"{num_en(nm_between(sea_km))} nm, trade direction and lane density")
    fi = (f"Johdettu indeksistä {ref.get('index')} {label_fi(ref.get('label'))} ({date_fi(ref.get('as_of'))}): skaalattu "
          f"reitin pituudella {num_fi(nm_between(sea_km))} mpk, kauppasuunnalla ja reitin liikennemäärällä")
    return usd, _src(ref, kind="benchmark_derived", note=note, note_fi=fi, derived_from=[ref])


EQUIPMENT_FACTOR = {"20GP": 0.6, "40GP": 1.0, "40HC": 1.0, "45HC": 1.15, "20RF": 1.1, "40RF": 1.9}


def thc_usd(region: str | None, ctype: str) -> float:
    a, b = THC_USD.get(region or "", (220, 300))
    return float(a if ctype.startswith("20") else b)


# ------------------------------------------------------------------------------------------------ air
def _air_r(o: str, d: str, km: float) -> float:
    return (0.6 + 0.25 * km / 1000.0) * _direction(o, d, AIR_PRODUCERS)


AIR_REF_KM = {("FEA", "EUR"): 9200, ("FEA", "NAM"): 11500, ("EUR", "NAM"): 7000, ("SEA", "EUR"): 10000,
              ("EUR", "FEA"): 9200, ("NAM", "FEA"): 11500, ("NAM", "EUR"): 7000, ("FEA", "SEA"): 2600}
AIR_BREAKS = [(45, 1.6), (100, 1.35), (300, 1.2), (500, 1.1), (1000, 1.05), (None, 1.0)]


def air_break(kg: float) -> float:
    for mx, f in AIR_BREAKS:
        if mx is None or kg < mx:
            return f
    return 1.0


async def air_usd_per_kg(o_country: str, d_country: str, km: float) -> tuple[float, dict]:
    lv = await levels()
    o, d = air_region(o_country), air_region(d_country)
    pairs: dict[tuple, dict] = {}
    for lvl in lv.values():
        if lvl.get("mode") != "air" or "kg" not in (lvl.get("unit") or "").lower():
            continue
        ro, rd = _region_pair(lvl, AIR_REGION_OF_PLACE)
        if ro and rd:
            cur = pairs.get((ro, rd))
            if cur is None or lvl.get("as_of", "") > cur.get("as_of", ""):
                pairs[(ro, rd)] = lvl
    if (o, d) in pairs:
        lvl = pairs[(o, d)]
        return float(lvl["value"]), _src(lvl, kind="benchmark", note=f"{lvl.get('index')} {lvl.get('label')}")
    ks = []
    for (ro, rd), lvl in pairs.items():
        ref = AIR_REF_KM.get((ro, rd)) or 8000
        ks.append((float(lvl["value"]) / _air_r(ro, rd, ref), lvl, ro, rd))
    if not ks:
        return 3.0 * _air_r(o, d, km) / _air_r("FEA", "EUR", 9200), _src(None, kind="model", note="No air index stored; distance model",
                                                                           note_fi="Lentorahti-indeksiä ei ole tallennettu; etäisyysmalli")
    pool = [x for x in ks if x[2] == o] or [x for x in ks if x[3] == d] or ks
    pool.sort(key=lambda x: x[0])
    k, ref, _, _ = pool[len(pool) // 2]
    usd = k * _air_r(o, d, km)
    note = f"Derived from {ref.get('index')} {ref.get('label')} ({ref.get('as_of')}) scaled by distance {num_en(km)} km and trade direction"
    fi = (f"Johdettu indeksistä {ref.get('index')} {label_fi(ref.get('label'))} ({date_fi(ref.get('as_of'))}): skaalattu "
          f"etäisyydellä {num_fi(km)} km ja kauppasuunnalla")
    return usd, _src(ref, kind="benchmark_derived", note=note, note_fi=fi, derived_from=[ref])


# ------------------------------------------------------------------------------------------------ road / rail
async def road_eur_per_km(a_country: str, b_country: str) -> tuple[float, dict]:
    """Full-truck EUR/km: the higher of the two ends' regional levels (index level when stored for the group)."""
    lv = await levels()
    rates = []
    for c in (a_country, b_country):
        g = road_group(c)
        lvl = lv.get(f"ROAD-{g}")
        per_km = await _per_km_eur(lvl) if lvl else None
        if per_km:
            note = f"{lvl.get('index')} {lvl.get('label')}: {lvl.get('value')} {lvl.get('unit')} = EUR {per_km:.2f}/km"
            fi = (f"{lvl.get('index')} {label_fi(lvl.get('label'))}: {num_fi(float(lvl.get('value')), 2)} {lvl.get('unit')} = "
                  f"{num_fi(per_km, 2)} EUR/km")
            rates.append((per_km, _src(lvl, kind="benchmark", note=note, note_fi=fi)))
        else:
            en_name, fi_name = ROAD_GROUP_NAMES.get(g, (g, g))
            rates.append((ROAD_EUR_PER_KM[g], _src(None, kind="model", note=f"Road model {en_name}: "
                                                                            f"EUR {ROAD_EUR_PER_KM[g]:.2f}/km per full trailer",
                                                   note_fi=f"Maantiemalli, {fi_name}: {num_fi(ROAD_EUR_PER_KM[g], 2)} EUR/km "
                                                           f"täysi perävaunu")))
    return max(rates, key=lambda x: x[0])


async def _per_km_eur(lvl: dict) -> float | None:
    """EUR per km from a stored road level in EUR/km, <CUR>/km or USD/mile (ECB rate of the newest day)."""
    unit = (lvl.get("unit") or "").upper().replace(" ", "")
    m = re.fullmatch(r"([A-Z]{3})/(KM|MILE)", unit)
    if not m:
        return None
    try:
        factor, _fx = await eur_per(m.group(1))
    except LookupError:
        return None
    v = float(lvl["value"]) * factor
    return v / 1.609344 if m.group(2) == "MILE" else v


def ltl_factor(kg: float) -> float:
    for mx, f in LTL_FACTORS:
        if mx is None or kg <= mx:
            return f
    return LTL_FACTORS[-1][1]


async def rail_eur_per_feu(network: str, a_country: str, b_country: str, km: float, middle_corridor: bool) -> tuple[float, dict, float]:
    """(EUR per FEU, source, transit days) for a rail main leg."""
    lv = await levels()
    eur_usd, _fx = await eur_per_usd()
    if middle_corridor:
        raise ValueError("use rail_middle_corridor()")
    net_key = "CHINA" if a_country == b_country == "CN" else network
    rate = RAIL_EUR_PER_FEU_KM.get(net_key, 0.95)
    lvl = lv.get(f"RAIL-{net_key}")
    if lvl and (lvl.get("unit") or "").upper() == "EUR/FEU-KM":
        rate = float(lvl["value"])
        src = _src(lvl, kind="benchmark", note=f"{lvl.get('index')} {lvl.get('label')}")
    else:
        en_name, fi_name = RAIL_NETWORK_NAMES.get(net_key, (net_key, net_key))
        src = _src(None, kind="model", note=f"Rail model {en_name}: EUR {rate:.2f} per FEU-km",
                   note_fi=f"Rautatiemalli, {fi_name}: {num_fi(rate, 2)} EUR / FEU-km")
    days = 1 + km / RAIL_KM_PER_DAY.get(net_key, 600.0)
    return rate * km, src, days


KHORGOS = {"lat": 44.21, "lon": 80.41, "country": "KZ", "name": "Altynkol / Khorgos"}
CONSTANTA = {"lat": 44.17, "lon": 28.65, "country": "RO", "name": "Constanța"}
ISTANBUL = {"lat": 41.01, "lon": 28.97, "country": "TR", "name": "Istanbul"}


async def rail_middle_corridor(t_o: dict, t_d: dict) -> tuple[float, dict, float, float, list[dict]]:
    """China/Central Asia <-> Europe by rail over the Trans-Caspian route: Chinese rail to Altynkol (model), the TITR
    published through tariff Altynkol–Constanța/Istanbul (rail + Caspian ferry), European rail from the Black Sea
    gateway (model). -> (EUR per FEU, source, transit days, km, segments)"""
    from services.hubs import MIDDLE_CORRIDOR, haversine_km

    lv = await levels()
    eur_usd, fx = await eur_per_usd()
    westbound = (t_o.get("country") or "").upper() in MIDDLE_CORRIDOR
    asia, europe = (t_o, t_d) if westbound else (t_d, t_o)
    to_tr = (europe.get("country") or "").upper() == "TR"
    gate = ISTANBUL if to_tr else CONSTANTA
    code = {(True, False): "RAIL-MIDCOR-WEST", (True, True): "RAIL-MIDCOR-WEST-TR",
            (False, False): "RAIL-MIDCOR-EAST", (False, True): "RAIL-MIDCOR-EAST-TR"}[(westbound, to_tr)]
    lvl = lv.get(code)
    km_asia = haversine_km(asia, KHORGOS) * 1.3 if (asia.get("country") or "").upper() == "CN" else haversine_km(asia, KHORGOS) * 1.3
    km_eu = haversine_km(gate, europe) * 1.35
    km_titr = 4200.0 if not to_tr else 4500.0
    china = RAIL_EUR_PER_FEU_KM["CHINA"] * km_asia
    eu = RAIL_EUR_PER_FEU_KM["EURASIA"] * km_eu
    if lvl:
        titr = float(lvl["value"]) * eur_usd
        src = _src(lvl, kind="benchmark_derived", note=f"TITR through tariff {lvl.get('label')} USD {num_en(float(lvl['value']))}/40' "
                                               f"+ rail model to/from Khorgos and {gate['name']}",
                   note_fi=f"TITR-läpikulkutariffi {lvl.get('label')} {num_fi(float(lvl['value']))} USD/40' + rautatiemalli "
                           f"Khorgosiin ja Khorgosista sekä {gate['name']}")
    else:
        titr = 0.75 * km_titr
        src = _src(None, kind="model", note="Middle Corridor model EUR 0.75/FEU-km (no TITR tariff stored)",
                   note_fi="Keskikäytävän malli 0,75 EUR / FEU-km (TITR-tariffia ei ole tallennettu)")
    src = {**src, "fx": {"EURUSD": fx.get("value"), "as_of": fx.get("as_of")}}
    segments = [{"mode": "rail", "km": round(km_asia), "from": asia.get("name"), "to": KHORGOS["name"], "eur": round(china)},
                {"mode": "rail+sea", "km": round(km_titr), "from": KHORGOS["name"], "to": gate["name"], "eur": round(titr)},
                {"mode": "rail", "km": round(km_eu), "from": gate["name"], "to": europe.get("name"), "eur": round(eu)}]
    if not westbound:
        segments.reverse()
    days = km_asia / 700.0 + 20 + km_eu / 550.0
    return china + titr + eu, src, days, km_asia + km_titr + km_eu, segments


async def staleness(max_days: int) -> list[dict]:
    """Index levels older than max_days (shown as a warning on quotes that use them)."""
    lv = await levels()
    out = []
    cutoff = (date.today() - timedelta(days=max_days)).isoformat()
    for lvl in lv.values():
        if lvl.get("mode") in ("courier", "sea_surcharge") or lvl.get("lane_code", "").startswith(("TITR", "RAIL-MIDCOR")):
            continue  # tariffs and notices carry their own validity
        if lvl.get("as_of") and lvl["as_of"] < cutoff:
            out.append({"lane_code": lvl["lane_code"], "as_of": lvl["as_of"]})
    return out


# ------------------------------------------------------------------------------------------------ refresh
async def store_level(level: dict, *, by: str) -> dict:
    """Validated insert of one level (POST /api/global/benchmarks and the refresher)."""
    need = ("index", "lane_code", "label", "mode", "unit", "value", "as_of")
    missing = [k for k in need if level.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")
    value = float(level["value"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Value must be a positive number")
    date.fromisoformat(str(level["as_of"])[:10])
    prev = (await levels(force=True)).get(level["lane_code"])
    if prev and prev.get("as_of", "") >= level["as_of"] and float(prev["value"]) == value:
        return prev
    if prev and by != "ops" and not (0.4 <= value / float(prev["value"]) <= 1.6):
        raise ValueError(f"{level['lane_code']}: {value} is outside ±60 % of the previous level {prev['value']} ({prev['as_of']})")
    doc = {k: level.get(k) for k in need + ("origin", "destination", "origin_region", "destination_region", "source_url", "snippet")}
    doc.update(value=value, as_of=str(level["as_of"])[:10], id=new_id(), loaded_at=now_iso(), loaded_by=by)
    await db.benchmark_indices.insert_one(dict(doc))
    _cache["at"] = 0.0
    return doc


async def refresh_fx() -> dict | None:
    """Ask services.customs to refresh its ECB store (one ECB client in the app), then report the newest day."""
    try:
        from services import customs

        await customs._ecb_rates()
    except Exception as exc:  # noqa: BLE001
        log.warning("ECB FX refresh failed: %s", exc)
    _cache["at"] = 0.0
    await levels()
    day = _cache["fx"]
    return {"date": day.get("date"), "USD": (day.get("rates") or {}).get("USD")} if day else None


WCI_LANES = {"shanghai rotterdam": ("WCI-SHA-RTM", "Shanghai–Rotterdam", "FEA", "NEU"),
             "rotterdam shanghai": ("WCI-RTM-SHA", "Rotterdam–Shanghai", "NEU", "FEA"),
             "shanghai genoa": ("WCI-SHA-GOA", "Shanghai–Genoa", "FEA", "MED"),
             "genoa shanghai": ("WCI-GOA-SHA", "Genoa–Shanghai", "MED", "FEA"),
             "shanghai los angeles": ("WCI-SHA-LAX", "Shanghai–Los Angeles", "FEA", "NAW"),
             "los angeles shanghai": ("WCI-LAX-SHA", "Los Angeles–Shanghai", "NAW", "FEA"),
             "shanghai new york": ("WCI-SHA-NYC", "Shanghai–New York", "FEA", "NAE"),
             "new york rotterdam": ("WCI-NYC-RTM", "New York–Rotterdam", "NAE", "NEU"),
             "rotterdam new york": ("WCI-RTM-NYC", "Rotterdam–New York", "NEU", "NAE")}


async def refresh_wci() -> dict:
    """Read Drewry's public WCI page with the shared LLM switch and store validated lane levels."""
    import httpx

    from core.llm import LLMUnavailable, llm_json

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Huolitsija/1.0 (+https://huolitsija.fi; ops@huolitsija.fi)"}) as client:
            r = await client.get(DREWRY_WCI_URL)
        if r.status_code != 200:
            return {"status": "failed", "reason": f"Drewry HTTP {r.status_code}"}
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        i = text.lower().find("world container index")
        text = text[max(0, i - 200): i + 12000]
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"fetch: {exc}"}
    try:
        out = await llm_json("benchmarks", "Extract Drewry World Container Index values from the page text. Return JSON "
                             "{assessment_date:'YYYY-MM-DD', composite_usd_per_40ft:number|null, lanes:[{from, to, usd_per_40ft}]} "
                             "using only numbers printed on the page for the latest assessment. No estimates.",
                             f"<page>\n{text}\n</page>", ref={"type": "benchmarks", "id": "wci"})
    except LLMUnavailable:
        return {"status": "needs_person", "reason": "AI provider unavailable"}
    as_of = str(out.get("assessment_date") or "")[:10]
    try:
        date.fromisoformat(as_of)
    except ValueError:
        return {"status": "failed", "reason": "no assessment date on the page"}
    stored, rejected = [], []
    rows = list(out.get("lanes") or [])
    if out.get("composite_usd_per_40ft"):
        rows.append({"from": "composite", "to": "", "usd_per_40ft": out["composite_usd_per_40ft"]})
    for row in rows:
        k = re.sub(r"[^a-z ]", " ", f"{row.get('from', '')} {row.get('to', '')}".lower())
        k = re.sub(r"\s+", " ", k).strip()
        if k == "composite":
            spec = ("WCI-COMPOSITE", "WCI composite", None, None)
        else:
            spec = WCI_LANES.get(k)
        if not spec:
            continue
        try:
            doc = await store_level({"index": "WCI", "lane_code": spec[0], "label": spec[1], "mode": "sea", "unit": "USD/FEU",
                                     "value": row.get("usd_per_40ft"), "as_of": as_of, "origin_region": spec[2],
                                     "destination_region": spec[3], "source_url": DREWRY_WCI_URL,
                                     "snippet": f"{spec[1]} USD {row.get('usd_per_40ft')} per 40ft (WCI {as_of})"}, by="refresh")
            stored.append(doc["lane_code"])
        except ValueError as exc:
            rejected.append(str(exc))
    await db.system.update_one({"key": "benchmarks:last_refresh"}, {"$set": {"value": {
        "at": now_iso(), "as_of": as_of, "stored": stored, "rejected": rejected}}}, upsert=True)
    return {"status": "ran", "as_of": as_of, "stored": stored, "rejected": rejected}


@schedule("global.benchmarks_refresh", job="global.benchmarks_refresh", every_seconds=3600,
          label_fi="Rahtivertailuhintojen päivitys", label_en="Freight benchmark refresh")
@job("global.benchmarks_refresh", agent=True, label_fi="Rahtivertailuhintojen päivitys", label_en="Freight benchmark refresh")
async def benchmarks_refresh(j=None):
    """Hourly check; FX once a day, WCI on Thursdays/Fridays after Drewry's 12:00 UK publication (once per week)."""
    now = datetime.now(timezone.utc)
    marks = (await db.system.find_one({"key": "benchmarks:refresh_marks"}) or {}).get("value") or {}
    today = now.date().isoformat()
    if marks.get("fx") != today and now.hour >= 15:
        if await refresh_fx():
            marks["fx"] = today
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"
    if now.weekday() in (3, 4) and now.hour >= 13 and marks.get("wci") != week:
        res = await refresh_wci()
        if res.get("status") == "ran" and res.get("stored"):
            marks["wci"] = week
    await db.system.update_one({"key": "benchmarks:refresh_marks"}, {"$set": {"value": marks}}, upsert=True)


# ------------------------------------------------------------------------------------------------ courier
COURIER_KG_PER_M3 = 200.0  # express volumetric 1:5000
COURIER_MODEL = [  # (max great-circle km, base EUR, EUR per kg) — used only when no published tariff is stored
    (2500, 38.0, 4.2), (6000, 62.0, 8.5), (None, 75.0, 11.0)]


def _interp(prices: dict, kg: float) -> float | None:
    pts = sorted((float(k), float(v)) for k, v in prices.items() if v is not None)
    if not pts:
        return None
    for (k1, v1), (k2, v2) in zip(pts, pts[1:]):
        if kg <= k1:
            return v1
        if kg <= k2:
            return v1 + (v2 - v1) * (kg - k1) / (k2 - k1)
    if kg <= pts[0][0]:
        return pts[0][1]
    if len(pts) >= 2:
        (k1, v1), (k2, v2) = pts[-2], pts[-1]
        return v2 + (v2 - v1) / (k2 - k1) * (kg - k2)
    return pts[-1][1] * kg / pts[-1][0]


async def courier_price(o_country: str | None, d_country: str | None, kg: float, m3: float) -> tuple[float, dict, int, str] | None:
    """Door-to-door express parcel price -> (EUR excl. VAT, source, transit days, note) or None if not deliverable."""
    if not kg:
        return None
    ch = max(kg, m3 * COURIER_KG_PER_M3)
    ch = math.ceil(ch * 2) / 2
    o, d = (o_country or "").upper(), (d_country or "").upper()
    other = d if o == "FI" else o if d == "FI" else d
    lv = await levels()
    tariffs = [x for x in lv.values() if x.get("mode") == "courier" and other in (x.get("countries") or [])]
    from core.util import now as _now  # noqa: F401  (keeps import style local)
    if tariffs:
        t = sorted(tariffs, key=lambda x: x.get("as_of", ""), reverse=True)[0]
        price = _interp(t.get("prices") or {}, ch)
        if price:
            if t.get("currency") == "USD":
                price *= (await eur_per_usd())[0]
            if t.get("vat_included"):
                price /= 1.255
            kind = "benchmark" if "FI" in (o, d) else "benchmark_derived"
            note = f"{t.get('label')}, {ch:g} kg chargeable (1:5000)" + ("" if "FI" in (o, d) else ", Finnish export tariff of the destination zone")
            fi = (f"{label_fi(t.get('label'))}, rahdituspaino {num_fi(ch, 1 if ch % 1 else 0)} kg (1:5000)"
                  + ("" if "FI" in (o, d) else ", kohdevyöhykkeen vientitariffi Suomesta"))
            days = int(t.get("transit_days") or (2 if t.get("zone") in ("1", "2") else 4))
            return round(price, 2), _src(t, kind=kind, note=note, note_fi=fi), days, note
    from services.hubs import haversine_km  # noqa: F401
    return None


async def courier_model(o: dict, d: dict, kg: float, m3: float) -> tuple[float, dict, int, str]:
    from services.hubs import haversine_km

    ch = math.ceil(max(kg, m3 * COURIER_KG_PER_M3) * 2) / 2
    km = haversine_km(o, d)
    for mx, base, per in COURIER_MODEL:
        if mx is None or km <= mx:
            price = base + per * ch
            days = 2 if km <= 2500 else 4 if km <= 6000 else 5
            note = f"{ch:g} kg chargeable (1:5000), express model EUR {base:.0f} + {per:.1f}/kg"
            fi = f"rahdituspaino {num_fi(ch, 1 if ch % 1 else 0)} kg (1:5000), pikalähetysmalli {base:.0f} EUR + {num_fi(per, 1)} EUR/kg"
            return price, _src(None, kind="model", note=note, note_fi=fi), days, note
    raise AssertionError
