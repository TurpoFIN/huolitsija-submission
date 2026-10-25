"""Transport hubs for any-city routing (s5): container gateway ports, cargo airports and rail terminals.

    await nearest("port", place, limit=4, max_km=1800)   -> [hub, ...] nearest first, each with "km" (great circle)
    road_connected(a, b) / rail_network(a, b)             -> can a truck / a train link the two places?

Sources, loaded into Mongo `hubs` (2dsphere on `geo`) at startup and kept in step with the files:
- ports: data/gateway_ports.json — curated container gateways (UN/LOCODE, trade region, tier 1 hub / 2 deep-sea
  gateway / 3 feeder), coordinates from the NGA World Port Index where it has the harbour.
- airports: the OurAirports records of data/locations_unlocode.json (rank 60 = large airport, 40 = medium with
  scheduled service), i.e. the same records V1 uses for maps.
- rail: UN/LOCODE rail locations of the same dataset in countries with a container rail network, plus the nodes of
  data/rail_corridors.json (Digitraffic stations, curated corridors).
Nothing here is invented per request: a hub is always a real, named place from those files.
"""
from __future__ import annotations

import json
import logging
import math
from functools import lru_cache
from pathlib import Path

from core.db import db, register_index
from core.lifecycle import on_startup
from core.util import now_iso

log = logging.getLogger("hubs")
DATA = Path(__file__).resolve().parent.parent / "data"
EARTH_KM = 6371.0088

register_index("hubs", [("geo", "2dsphere")])
register_index("hubs", [("kind", 1), ("country", 1)])
register_index("hubs", "key", unique=True)

# ------------------------------------------------------------------------------------------------ geography rules
# Road landmasses: a truck can link two places in the same group (ferries that are part of normal trucking, e.g.
# the Channel, the Baltic and Øresund, are included). Countries not listed form their own group (islands such as
# JP, TW, PH, LK, AU, NZ, IS, CY, MT, MG and the Caribbean/Pacific islands).
_EUROPE = ("AT BE BG HR CZ DK EE FI FR DE GR HU IE IT LV LT LU NL PL PT RO SK SI ES SE NO CH LI GB TR UA MD RS BA ME MK "
           "AL XK AD MC SM VA GI").split()
_ASIA_MAINLAND = ("GE AM AZ KZ UZ TM KG TJ AF PK IN NP BT BD MM TH LA KH VN MY SG CN MN HK MO JO LB IL PS SA AE OM YE "
                  "QA KW BH IQ IR SY RU BY").split()
_AFRICA = ("EG LY TN DZ MA EH MR ML SN GM GW GN SL LR CI BF GH TG BJ NE NG TD CM CF GQ GA CG CD AO NA ZA LS SZ BW ZW "
           "ZM MW MZ TZ KE UG RW BI SS SD ET ER DJ SO").split()
_NORTH_AMERICA = "US CA MX GT BZ SV HN NI CR PA".split()  # the Darién Gap separates PA from CO
_SOUTH_AMERICA = "CO VE EC PE BO CL AR PY UY BR GY SR GF".split()
LANDMASS: dict[str, str] = {}
for _group, _codes in (("EURASIA", _EUROPE + _ASIA_MAINLAND), ("AFRICA", _AFRICA), ("NORTH_AMERICA", _NORTH_AMERICA),
                       ("SOUTH_AMERICA", _SOUTH_AMERICA)):
    for _c in _codes:
        LANDMASS[_c] = _group

# Container rail networks (intermodal terminals and regular block trains). EURASIA joins Europe and China over the
# Middle Corridor (Kazakhstan – Caspian – Azerbaijan – Georgia – Türkiye); RU/BY transit is not offered (RULE K-2).
_RAIL_EUROPE = ("AT BE BG HR CZ DK EE FI FR DE GR HU IT LV LT LU NL PL PT RO SK SI ES SE NO CH GB TR RS BA ME MK AL "
                "UA MD").split()
RAIL_NETWORK: dict[str, str] = {c: "EURASIA" for c in _RAIL_EUROPE + "CN KZ UZ KG TM AZ GE MN".split()}
RAIL_NETWORK.update({c: "NORTH_AMERICA" for c in ("US", "CA", "MX")})
RAIL_NETWORK.update({c: "SOUTHERN_AFRICA" for c in ("ZA", "BW", "ZW", "ZM", "MZ", "NA", "SZ")})
RAIL_NETWORK.update({"IN": "INDIA", "AU": "AUSTRALIA", "JP": "JAPAN", "KR": "KOREA"})
MIDDLE_CORRIDOR = {"CN", "KZ", "UZ", "KG", "TM", "MN"}  # Asian side of the China–Europe rail bridge

EU_EXTERNAL_BORDER_FREE = set("AT BE BG HR CZ DK EE FI FR DE GR HU IT LV LT LU NL PL PT RO SK SI ES SE IE CY MT".split()) | {"NO", "CH", "LI", "IS"}

TRADE_REGIONS = {
    "FEA": "Far East", "SEA": "South-East Asia", "ISC": "Indian subcontinent", "MEG": "Middle East Gulf",
    "RSE": "Red Sea", "MED": "Mediterranean & Black Sea", "NEU": "North Europe", "WAF": "West & Central Africa",
    "SAF": "Southern Africa", "EAF": "East Africa & Indian Ocean", "NAW": "North America West Coast",
    "NAE": "North America East Coast", "USG": "US & Mexico Gulf", "CAR": "Caribbean & Central America",
    "SAE": "South America East Coast", "SAW": "South America West Coast", "OCE": "Oceania",
}
TRADE_REGIONS_FI = {
    "FEA": "Kaukoitä", "SEA": "Kaakkois-Aasia", "ISC": "Intian niemimaa", "MEG": "Persianlahti",
    "RSE": "Punainenmeri", "MED": "Välimeri ja Mustameri", "NEU": "Pohjois-Eurooppa", "WAF": "Länsi- ja Keski-Afrikka",
    "SAF": "Eteläinen Afrikka", "EAF": "Itä-Afrikka", "NAW": "Pohjois-Amerikan länsirannikko",
    "NAE": "Pohjois-Amerikan itärannikko", "USG": "Meksikonlahti", "CAR": "Karibia ja Keski-Amerikka",
    "SAE": "Etelä-Amerikan itärannikko", "SAW": "Etelä-Amerikan länsirannikko", "OCE": "Oseania",
}


def landmass(country: str | None) -> str | None:
    c = (country or "").upper()
    return LANDMASS.get(c, f"ISLAND:{c}") if c else None


def road_connected(a: dict, b: dict) -> bool:
    """Same road landmass (truck possible, with routine ferries)."""
    la, lb = landmass(a.get("country")), landmass(b.get("country"))
    return bool(la and la == lb)


def rail_network(a: dict, b: dict) -> str | None:
    na, nb = RAIL_NETWORK.get((a.get("country") or "").upper()), RAIL_NETWORK.get((b.get("country") or "").upper())
    if not na or na != nb:
        return None
    if na in ("JAPAN", "KOREA", "INDIA", "AUSTRALIA") and a.get("country") != b.get("country"):
        return None
    return na


def middle_corridor(a: dict, b: dict) -> bool:
    ca, cb = (a.get("country") or "").upper(), (b.get("country") or "").upper()
    return (ca in MIDDLE_CORRIDOR) != (cb in MIDDLE_CORRIDOR) and rail_network(a, b) == "EURASIA"


def border_crossings(a: dict, b: dict) -> int:
    """Customs borders on a road leg (0 inside one country or inside the EU/EFTA single market)."""
    ca, cb = (a.get("country") or "").upper(), (b.get("country") or "").upper()
    if ca == cb or (ca in EU_EXTERNAL_BORDER_FREE and cb in EU_EXTERNAL_BORDER_FREE):
        return 0
    return 1


def haversine_km(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


# ------------------------------------------------------------------------------------------------ dataset
@lru_cache(maxsize=1)
def _ports_file() -> dict:
    path = DATA / "gateway_ports.json"
    if not path.exists():
        log.warning("data/gateway_ports.json missing: no container gateways for any-city routing")
        return {"version": "missing", "ports": []}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _rail_nodes() -> set[str]:
    try:
        raw = json.loads((DATA / "rail_corridors.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    codes: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("code", "unlocode", "locode") and isinstance(v, str) and len(v) == 5:
                    codes.add(v.upper())
                walk(v)
        elif isinstance(x, list):
            for v in x:
                if isinstance(v, str) and len(v) == 5 and v[:2].isalpha() and v.isupper():
                    codes.add(v)
                walk(v)

    walk(raw)
    codes |= {k.upper() for k in (raw.get("terminals") or {}) if isinstance(k, str) and len(k) == 5}
    return codes


def build_records() -> list[dict]:
    """Every hub record (ports, airports, rail terminals) from the data files."""
    from services import geo

    out: dict[str, dict] = {}
    for p in _ports_file().get("ports") or []:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        key = f"port:{p['unlocode']}"
        out[key] = {"key": key, "kind": "port", "code": p["unlocode"], "unlocode": p["unlocode"], "iata": None,
                    "name": p["name"], "city": (p.get("aliases") or [p["name"]])[0], "country": p["country"],
                    "region": p.get("region"), "tier": int(p.get("tier") or 3), "lat": float(p["lat"]), "lon": float(p["lon"]),
                    "aliases": p.get("aliases") or [], "rank": {1: 100, 2: 80, 3: 60}.get(int(p.get("tier") or 3), 60),
                    "source": "gateway_ports"}
    ds = geo._dataset()
    rail_nodes = _rail_nodes()
    for d in ds["by_code"].values():
        kinds = d.get("kinds") or []
        if "airport" in kinds and d.get("iata") and d.get("source") in ("ourairports", "curated") and (d.get("rank") or 0) >= 40:
            key = f"airport:{d['iata']}"
            if key not in out or (d.get("rank") or 0) > out[key]["rank"]:
                out[key] = {"key": key, "kind": "airport", "code": d["iata"], "unlocode": d.get("unlocode"), "iata": d["iata"],
                            "name": d["name"], "city": d.get("city") or d["name"], "country": d["country"], "region": None,
                            "tier": 1 if (d.get("rank") or 0) >= 60 else 2, "lat": float(d["lat"]), "lon": float(d["lon"]),
                            "aliases": [], "rank": int(d.get("rank") or 40), "source": d.get("source")}
        if "rail" in kinds and d.get("unlocode") and d["country"] in RAIL_NETWORK:
            on_corridor = d["unlocode"] in rail_nodes or d["code"] in rail_nodes
            if not on_corridor and (d.get("rank") or 0) < 30:
                continue  # minor stations: no container terminal
            key = f"rail:{d['unlocode']}"
            if key not in out or (d.get("rank") or 0) > out[key]["rank"]:
                out[key] = {"key": key, "kind": "rail", "code": d["unlocode"], "unlocode": d["unlocode"], "iata": None,
                            "name": d["name"], "city": d.get("city") or d["name"], "country": d["country"], "region": None,
                            "tier": 1 if on_corridor else 2, "lat": float(d["lat"]), "lon": float(d["lon"]), "aliases": [],
                            "rank": int(d.get("rank") or 0) + (40 if on_corridor else 0), "source": d.get("source")}
    for r in out.values():
        r["geo"] = {"type": "Point", "coordinates": [r["lon"], r["lat"]]}
    return list(out.values())


def dataset_version() -> str:
    from services import geo

    return f"hubs:v1:{_ports_file().get('version')}:{geo._dataset()['version']}"


@on_startup(order=13)
async def load_hubs():
    marker = dataset_version()
    if await db.system.find_one({"key": "hubs:version", "value": marker}) and await db.hubs.estimated_document_count() > 100:
        return
    docs = build_records()
    await db.hubs.delete_many({})
    for i in range(0, len(docs), 2000):
        await db.hubs.insert_many([dict(d) for d in docs[i:i + 2000]], ordered=False)
    await db.system.update_one({"key": "hubs:version"}, {"$set": {"value": marker, "at": now_iso(), "count": len(docs)}},
                               upsert=True)
    log.info("hubs loaded: %s records (%s)", len(docs), marker)


# ------------------------------------------------------------------------------------------------ queries
_PUBLIC = {"_id": 0, "geo": 0}


async def nearest(kind: str, place: dict, *, limit: int = 4, max_km: float = 1500, same_landmass: bool = True,
                  min_tier: int | None = None) -> list[dict]:
    """Nearest hubs of a kind to a place (Mongo $nearSphere on the 2dsphere index), nearest first."""
    q: dict = {"kind": kind, "geo": {"$nearSphere": {"$geometry": {"type": "Point",
                                                                   "coordinates": [float(place["lon"]), float(place["lat"])]},
                                                     "$maxDistance": float(max_km) * 1000}}}
    if min_tier:
        q["tier"] = {"$lte": min_tier}
    out = []
    async for h in db.hubs.find(q, _PUBLIC).limit(limit * 6):
        if same_landmass and kind != "airport" and not road_connected(place, h):
            continue
        if kind == "rail" and not RAIL_NETWORK.get(h["country"]):
            continue
        h["km"] = round(haversine_km(place, h), 1)
        out.append(h)
        if len(out) >= limit:
            break
    return out


async def get(kind: str, code: str) -> dict | None:
    return await db.hubs.find_one({"key": f"{kind}:{(code or '').upper()}"}, _PUBLIC)


def hub_ref(h: dict | None) -> dict | None:
    """Compact form used on legs (same keys as pricing._leg_loc + kind/region/tier)."""
    if not h:
        return None
    return {"unlocode": h.get("unlocode"), "iata": h.get("iata"), "name": h.get("name"), "city": h.get("city"),
            "country": h.get("country"), "lat": h.get("lat"), "lon": h.get("lon"), "kind": h.get("kind"),
            "region": h.get("region"), "tier": h.get("tier")}
