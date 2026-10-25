"""Builds data/locations_unlocode.json from real public datasets.

Sources
- UN/LOCODE code list (UNECE), mirrored by datasets/un-locode on GitHub:
  https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv
  Coordinates are DDMM[N|S] DDDMM[E|W]; Function chars: 1 port, 2 rail, 3 road, 4 airport, B border crossing.
- OurAirports (public domain): https://davidmegginson.github.io/ourairports-data/airports.csv
  large/medium airports with scheduled service and an IATA code (accurate coordinates).
- Fintraffic Digitraffic port metadata for Finnish terminals is applied through data/locations_curated.json.

Filter: every seaport and airport worldwide that has coordinates, rail terminals in Europe/Asia/North America,
road locations in Finland and approved (AA/AC/AF/AI/AS) road locations in the rest of Europe. Russia, Belarus,
North Korea, Iran and Syria are left out (EU sanctions; domain spec RULE K-2 default-deny).

Run: ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/locations_build.py [cache_dir]
"""
import csv
import io
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "locations_unlocode.json"
UNLOCODE_URL = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

EXCLUDED = {"RU", "BY", "KP", "IR", "SY"}
EUROPE = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS CH LI GB "
             "TR UA MD RS BA ME MK AL XK GE AZ AM AD MC SM".split())
CENTRAL_ASIA = set("KZ UZ TM KG TJ MN".split())
ASIA = set("CN JP KR TW HK MO SG MY TH VN ID PH IN BD PK LK AE SA QA OM BH KW IL JO EG MA KH MM LA NP".split())
NORTH_AMERICA = set("US CA MX".split())
RAIL_REGIONS = EUROPE | CENTRAL_ASIA | ASIA | NORTH_AMERICA
ROAD_APPROVED = {"AA", "AC", "AF", "AI", "AS"}
BAD_STATUS = {"RR", "XX", "UR"}


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().strip()


def parse_coord(s: str):
    s = (s or "").strip()
    if not s or " " not in s:
        return None
    la, lo = s.split()
    try:
        lat = int(la[:2]) + int(la[2:4]) / 60.0
        if la[4] == "S":
            lat = -lat
        lon = int(lo[:3]) + int(lo[3:5]) / 60.0
        if lo[5] == "W":
            lon = -lon
    except (ValueError, IndexError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 4), round(lon, 4)


def fetch(url: str, cache: Path | None, name: str | None = None) -> str:
    fname = name or url.rsplit("/", 1)[-1]
    if cache:
        p = cache / fname
        if p.exists():
            return p.read_text(encoding="utf-8")
    r = httpx.get(url, timeout=120, follow_redirects=True, headers={"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"})
    r.raise_for_status()
    if cache:
        (cache / fname).write_text(r.text, encoding="utf-8")
    return r.text


def geocode_fi(name: str, cache: Path | None) -> tuple[float, float] | None:
    """Finnish UN/LOCODE rows without coordinates (e.g. Hämeenlinna, Seinäjoki, Salo): OpenStreetMap Nominatim,
    one request per second, cached next to the downloaded code list."""
    import time

    key = name.split("(")[0].strip()
    for nm in [key] + split_names(name):  # "Tavastehus (Hämeenlinna)": try both language versions
        st = _fi_stations(cache).get(fold_name(nm))
        if st:  # the town's passenger station (Fintraffic rail metadata): exact enough for a town-level code
            return st
    store = (cache / "fi_geocode.json") if cache else None
    known = json.loads(store.read_text()) if store and store.exists() else {}
    if known.get(key):
        return tuple(known[key])
    time.sleep(1.1)
    r = httpx.get("https://nominatim.openstreetmap.org/search",
                  params={"format": "jsonv2", "limit": 1, "countrycodes": "fi", "city": key},
                  headers={"User-Agent": "Huolitsija/1.0 (+https://huolitsija.fi; ops@huolitsija.fi)"}, timeout=30)
    if r.status_code != 200:
        print(f"nominatim {r.status_code} for {key}; left out this run", file=sys.stderr)
        return None
    rows = r.json()
    if not rows:
        return None
    val = [round(float(rows[0]["lat"]), 4), round(float(rows[0]["lon"]), 4)]
    known[key] = val
    if store:
        store.write_text(json.dumps(known, ensure_ascii=False, indent=0))
    return tuple(val)


def fold_name(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower().strip()


_STATIONS: dict | None = None


def _fi_stations(cache: Path | None) -> dict:
    global _STATIONS
    if _STATIONS is None:
        raw = fetch("https://rata.digitraffic.fi/api/v1/metadata/stations", cache, name="stations.json")
        _STATIONS = {}
        for st in json.loads(raw):
            if st.get("countryCode") == "FI" and st.get("passengerTraffic"):
                nm = fold_name(st["stationName"].replace(" asema", ""))
                _STATIONS.setdefault(nm, (round(st["latitude"], 4), round(st["longitude"], 4)))
    return _STATIONS


def split_names(name: str) -> list[str]:
    """'Uleåborg (Oulu)' -> ['Uleåborg', 'Oulu']; 'Kemi/Tornio (Kemi/Torneå)' -> parts too."""
    out = []
    base = name
    if "(" in name and name.endswith(")"):
        base, inner = name[:-1].split("(", 1)
        out.append(inner.strip())
    out.insert(0, base.strip())
    return [p for p in out if p]


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    text = fetch(UNLOCODE_URL, cache)
    rows = list(csv.DictReader(io.StringIO(text)))

    # Best row per code: prefer unchanged/updated rows with coordinates over '#' (name-change) rows.
    best: dict[str, dict] = {}
    for r in rows:
        if r["Change"] == "X" or r["Status"] in BAD_STATUS or r["Country"] in EXCLUDED or not r["Location"]:
            continue
        code = r["Country"] + r["Location"]
        # '#' marks a renamed entry: in Finland it carries the current (Finnish-first) name, e.g. "Hämeenlinna (Tavastehus)"
        score = (bool(r["Coordinates"].strip()), (r["Change"] == "#") if r["Country"] == "FI" else (r["Change"] != "#"))
        if code not in best or score > best[code][0]:
            alt = best.get(code, (None, None))[1]
            best[code] = (score, r)
            if alt is not None:
                r.setdefault("_alt", []).append(alt["Name"])
        else:
            best[code][1].setdefault("_alt", []).append(r["Name"])

    docs: dict[str, dict] = {}
    for code, (_, r) in best.items():
        cc = r["Country"]
        f = (r["Function"] + "--------")[:8]
        kinds = []
        if f[0] == "1":
            kinds.append("port")
        if f[1] == "2":
            kinds.append("rail")
        if f[2] == "3":
            kinds.append("road")
        if f[3] == "4":
            kinds.append("airport")
        border = f[7] == "B"
        coord = parse_coord(r["Coordinates"])
        if not coord and cc == "FI" and kinds:
            coord = geocode_fi(r["Name"], cache)  # home market: no Finnish town may be missing
        if not coord or not kinds:
            continue
        keep = ("port" in kinds or "airport" in kinds
                or ("rail" in kinds and cc in RAIL_REGIONS)
                or (border and cc in RAIL_REGIONS)
                or ("road" in kinds and cc == "FI")
                or ("road" in kinds and cc in EUROPE and r["Status"] in ROAD_APPROVED))
        if not keep:
            continue
        names = split_names(r["Name"])
        for alt in r.get("_alt", []):
            names += split_names(alt)
        names += split_names(r["NameWoDiacritics"])
        aliases = []
        for n in names:
            if n and n not in aliases:
                aliases.append(n)
        rank = 10
        if "port" in kinds:
            rank += 20
        if "airport" in kinds:
            rank += 10
        if "rail" in kinds:
            rank += 5
        if r["Status"] in ROAD_APPROVED:
            rank += 5
        rank += min(len(kinds), 4)
        docs[code] = {
            "code": code, "unlocode": code, "iata": r["IATA"] or None, "name": aliases[0], "city": aliases[0],
            "country": cc, "subdivision": r["Subdivision"] or None, "lat": coord[0], "lon": coord[1], "kinds": kinds,
            "aliases": aliases[1:], "border_crossing": border, "rank": rank, "source": "unlocode",
        }

    # Finnish display names: UN/LOCODE lists some Finnish places Swedish-first; Finnish-first per Digitraffic naming.
    fi_finnish_first = {"FIOUL": "Oulu", "FITKU": "Turku", "FINLI": "Naantali", "FIHEL": "Helsinki", "FIVAA": "Vaasa",
                        "FILPP": "Lappeenranta", "FIPOR": "Pori", "FIRAU": "Rauma", "FIKOK": "Kokkola", "FILHI": "Lahti",
                        "FIKEM": "Kemi", "FITOR": "Tornio", "FIHKO": "Hanko", "FIHMN": "Hamina", "FIVAT": "Vantaa",
                        "FIESP": "Espoo", "FIPRV": "Porvoo", "FIKER": "Kerava", "FIIIS": "Iisalmi", "FIJAK": "Jakobstad"}
    for code, fname in fi_finnish_first.items():
        d = docs.get(code)
        if d and d["name"] != fname:
            d["aliases"] = [d["name"]] + [a for a in d["aliases"] if a != fname]
            d["name"] = d["city"] = fname

    # Airports (OurAirports): one document per IATA code, linked to the city's UN/LOCODE when it has one.
    atext = fetch(AIRPORTS_URL, cache)
    iata_to_locode: dict[str, str] = {}
    for code, (_, r) in best.items():
        f = (r["Function"] + "--------")[:8]
        if f[3] == "4":
            iata = r["IATA"] or r["Location"]
            iata_to_locode.setdefault(f"{r['Country']}:{iata}", code)
    n_air = 0
    for a in csv.DictReader(io.StringIO(atext)):
        iata = (a.get("iata_code") or "").strip()
        if (a["type"] not in ("large_airport", "medium_airport") or a.get("scheduled_service") != "yes" or len(iata) != 3
                or a["iso_country"] in EXCLUDED):
            continue
        cc = a["iso_country"]
        locode = iata_to_locode.get(f"{cc}:{iata}")
        city = (a.get("municipality") or "").strip() or a["name"]
        aliases = [a["name"], city, iata, a.get("ident") or ""]
        aliases += [k.strip() for k in (a.get("keywords") or "").split(",") if k.strip() and len(k.strip()) < 40][:4]
        docs[iata] = {
            "code": iata, "unlocode": locode, "iata": iata, "icao": a.get("icao_code") or a.get("ident"),
            "name": a["name"], "city": city, "country": cc, "subdivision": (a.get("iso_region") or "").split("-")[-1] or None,
            "lat": round(float(a["latitude_deg"]), 5), "lon": round(float(a["longitude_deg"]), 5), "kinds": ["airport"],
            "aliases": [x for i, x in enumerate(aliases) if x and x not in aliases[:i]], "border_crossing": False,
            "rank": 60 if a["type"] == "large_airport" else 40, "source": "ourairports",
        }
        n_air += 1

    # Curated overrides (terminal-level coordinates, aliases, rank).
    curated = json.loads((HERE / "locations_curated.json").read_text(encoding="utf-8"))["locations"]
    for c in curated:
        base = docs.get(c["code"], {})
        aliases = list(dict.fromkeys(c.get("aliases", []) + base.get("aliases", []) + ([base["name"]] if base.get("name") else [])))
        aliases = [a for a in aliases if a != c["name"]]
        docs[c["code"]] = {
            "code": c["code"], "unlocode": c.get("unlocode", c["code"]), "iata": base.get("iata"), "name": c["name"],
            "city": c.get("city", c["name"]), "country": c["country"], "subdivision": base.get("subdivision"),
            "lat": c["lat"], "lon": c["lon"], "kinds": c["kinds"], "aliases": aliases, "border_crossing": base.get("border_crossing", False),
            "rank": c.get("rank", 70), "terminal": c.get("terminal"), "source": "curated",
        }

    cols = ["code", "unlocode", "iata", "name", "city", "country", "subdivision", "lat", "lon", "kinds", "aliases", "rank", "source", "terminal"]
    data = [[d.get(k) for k in cols] for d in sorted(docs.values(), key=lambda d: d["code"])]
    out = {"version": datetime.now(timezone.utc).strftime("%Y%m%d%H%M"), "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sources": [UNLOCODE_URL, AIRPORTS_URL, "data/locations_curated.json",
                       "OpenStreetMap Nominatim (coordinates for Finnish UN/LOCODEs that have none)"], "columns": cols, "rows": data}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    kinds = {}
    for d in docs.values():
        for k in d["kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    print("wrote", OUT, len(docs), "locations;", n_air, "airports;", kinds)


if __name__ == "__main__":
    main()
