"""Any city on earth -> coordinates (s5).

    await resolve_text("Lagos, Nigeria")         -> place | None     (decision against the hub dataset, see below)
    lookup("Tukholma")                           -> [place, ...]      (offline, exact name/alias, biggest first)
    suggest("ouag", limit=5)                     -> [place, ...]      (autocomplete, prefix)

Order of sources (each answer records `geocode.source`):
1. data/places_geonames.json — GeoNames cities15000 (CC BY 4.0, every city with >= 15 000 inhabitants), with
   Latin-script alternate names and Finnish exonyms from Wikidata; data/countries_i18n.json (CLDR country names in
   English and Finnish) for "City, Country" queries. In memory, ~34k cities.
   data/places_geonames_towns.tsv.gz — the towns tier: GeoNames cities1000 places below 15 000 inhabitants (~130k,
   name and ASCII name only), held in columns (~25 MB). Exact names always; prefixes only when the cities run short.
2. OpenStreetMap Nominatim free-text search (usage policy: identifying User-Agent, at most 1 request per second —
   the same throttle as services.geo), then Photon (komoot) when Nominatim refuses. Answers, including "not found",
   are cached in Mongo `geocode_cache` (keys `place1:`); refusals (HTTP 429/5xx, timeouts) are never cached.
"""
from __future__ import annotations

import asyncio
import bisect
import gzip
import json
import sys
import threading
from array import array
import logging
import re
from functools import lru_cache
from pathlib import Path

from services.hubs import haversine_km

log = logging.getLogger("gazetteer")


class GeocoderBusy(LookupError):
    """Not in the offline data, and both live geocoders refused (rate limit, outage): "try again", not "no such place"."""

DATA = Path(__file__).resolve().parent.parent / "data"
MAX_QUERY = 120


def fold(s: str | None) -> str:
    from services.geo import fold as _fold

    return re.sub(r"[\s\-‐-―'’.]+", " ", _fold(s)).strip()


# ------------------------------------------------------------------------------------------------ data
@lru_cache(maxsize=1)
def _countries() -> dict:
    path = DATA / "countries_i18n.json"
    raw = json.loads(path.read_text(encoding="utf-8")).get("countries", {}) if path.exists() else {}
    alias: dict[str, str] = {}
    for code, c in raw.items():
        for name in [code, c.get("en"), c.get("fi"), *(c.get("aliases") or [])]:
            if name:
                alias.setdefault(fold(name), code.upper())
    return {"by_code": raw, "alias": alias}


def country_name(code: str | None, lang: str = "en") -> str | None:
    c = _countries()["by_code"].get((code or "").upper())
    return (c or {}).get(lang) or (c or {}).get("en") or code


def country_code(text: str | None) -> str | None:
    t = fold(text)
    if not t:
        return None
    hit = _countries()["alias"].get(t)
    if hit:
        return hit
    if len(t) == 2 and t.upper() in _countries()["by_code"]:
        return t.upper()
    return None


TOWN_COLUMNS = ("id", "name", "ascii", "country", "admin1", "lat", "lon", "population")


class Towns:
    """GeoNames cities1000 below 15 000 inhabitants, column-wise. Global row index = n_cities + town index."""

    def __init__(self, path: Path):
        self.version = None
        self.ids, self.pop = array("l"), array("l")
        self.lat, self.lon = array("d"), array("d")
        self.name: list[str] = []
        self.cc: list[str] = []
        self.admin1: list[str] = []
        pairs: list[tuple[str, int]] = []
        if not path.exists():
            log.warning("%s missing: towns below 15 000 inhabitants need the online geocoder", path.name)
        else:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("# "):
                        head = json.loads(line[2:])
                        assert tuple(head["columns"]) == TOWN_COLUMNS, head["columns"]
                        self.version = head.get("version")
                        continue
                    gid, name, ascii_, cc, adm, lat, lon, pop = line.rstrip("\n").split("\t")
                    k = len(self.name)
                    self.ids.append(int(gid)); self.pop.append(int(pop or 0))  # noqa: E702
                    self.lat.append(float(lat)); self.lon.append(float(lon))  # noqa: E702
                    self.name.append(name); self.cc.append(sys.intern(cc)); self.admin1.append(sys.intern(adm))  # noqa: E702
                    for t in {fold(name), fold(ascii_)}:
                        if len(t) >= 2:
                            pairs.append((t, k))
        pairs.sort()
        self.keys = [t for t, _ in pairs]
        self.idx = array("l", (k for _, k in pairs))

    def __len__(self):
        return len(self.name)

    def row(self, k: int) -> dict:
        return {"id": self.ids[k], "name": self.name[k], "country": self.cc[k], "admin1": self.admin1[k],
                "lat": self.lat[k], "lon": self.lon[k], "population": self.pop[k], "tier": "town"}

    def exact(self, term: str) -> list[int]:
        lo = bisect.bisect_left(self.keys, term)
        hi = bisect.bisect_right(self.keys, term, lo)
        return [self.idx[j] for j in range(lo, hi)]

    def prefix(self, term: str, cap: int = 4000) -> list[int]:
        lo = bisect.bisect_left(self.keys, term)
        out = []
        for j in range(lo, min(lo + cap, len(self.keys))):
            if not self.keys[j].startswith(term):
                break
            out.append(self.idx[j])
        return out


@lru_cache(maxsize=1)
def _places() -> dict:
    """Cities (dict rows 0..n_cities-1, with aliases) and the towns tier (global index n_cities + k); use row()."""
    towns = Towns(DATA / "places_geonames_towns.tsv.gz")
    path = DATA / "places_geonames.json"
    if not path.exists():
        log.warning("data/places_geonames.json missing: free-text cities fall back to Nominatim only")
        return {"rows": [], "terms": [], "exact": {}, "n_cities": 0, "towns": towns, "version": "missing"}
    raw = json.loads(path.read_text(encoding="utf-8"))
    cols = raw["columns"]
    rows = [dict(zip(cols, r)) for r in raw["rows"]]
    exact: dict[str, list[int]] = {}
    terms: list[tuple[str, int]] = []
    for i, r in enumerate(rows):
        names = {fold(x) for x in [r.get("name"), r.get("ascii"), r.get("name_fi"), *(r.get("aliases") or [])] if x}
        for t in names:
            if len(t) < 2:
                continue
            exact.setdefault(t, []).append(i)
            terms.append((t, i))
    terms.sort()
    for t, idx in exact.items():
        idx.sort(key=lambda i: -(rows[i].get("population") or 0))
    return {"rows": rows, "terms": terms, "exact": exact, "n_cities": len(rows), "towns": towns,
            "version": raw.get("version"), "towns_version": towns.version}


def row(i: int) -> dict:
    p = _places()
    return p["rows"][i] if i < p["n_cities"] else p["towns"].row(i - p["n_cities"])


def population(i: int) -> int:
    p = _places()
    return int(p["rows"][i].get("population") or 0) if i < p["n_cities"] else p["towns"].pop[i - p["n_cities"]]


def exact_idx(term: str) -> list[int]:
    """Cities first (most populous first), then towns of that exact name (most populous first)."""
    p = _places()
    n = p["n_cities"]
    towns = sorted((n + k for k in p["towns"].exact(term)), key=lambda i: -population(i))
    return list(p["exact"].get(term, [])) + towns


def place_from_row(r: dict, *, match: str = "exact") -> dict:
    cc = (r.get("country") or "").upper()
    return {"unlocode": None, "code": None, "name": r["name"], "name_fi": r.get("name_fi") or None, "city": r["name"],
            "country": cc, "admin1": r.get("admin1") or None, "lat": float(r["lat"]), "lon": float(r["lon"]),
            "population": int(r.get("population") or 0), "kinds": ["city"], "geonameid": r.get("id"),
            "geocode": {"source": "geonames", "match": match, "confidence": 0.95 if match == "exact" else 0.7,
                        **({"tier": "town"} if r.get("tier") == "town" else {})}}


# ------------------------------------------------------------------------------------------------ parsing
def parse_query(text: str) -> tuple[str, str | None, str | None]:
    """"Portland, Oregon, USA" -> ("portland", "US", "oregon"); "Lagos NG" -> ("lagos", "NG", None)."""
    parts = [p.strip() for p in re.split(r"[,;/]+", text or "") if p.strip()]
    if not parts:
        return "", None, None
    cc = None
    admin = None
    if len(parts) > 1:
        cc = country_code(parts[-1])
        if cc:
            parts = parts[:-1]
        if len(parts) > 1:
            admin = fold(parts[-1])
            parts = parts[:-1]
    city = parts[0]
    words = city.split()
    if not cc and len(words) > 1:
        tail = country_code(words[-1]) if len(words[-1]) == 2 or fold(words[-1]) in _countries()["alias"] else None
        if tail and not exact_idx(fold(" ".join(words))):
            cc, city = tail, " ".join(words[:-1])
    return fold(city), cc, admin


def _rank(rows_idx: list[int], cc: str | None, admin: str | None) -> list[int]:
    out = [i for i in rows_idx if not cc or row(i)["country"] == cc]

    def key(i):
        admin_hit = bool(admin and fold(row(i).get("admin1")).startswith(admin))
        return (not admin_hit, -population(i))

    return sorted(out, key=key)


# ------------------------------------------------------------------------------------------------ warm-up
# Building _places() (~165k names folded and sorted) and geo._dataset() is seconds of pure CPU. Done lazily inside a request it froze the
# event loop of that process: every other request, health checks included, waited, and the edge answered 520.
# ready() builds it once per process in a worker thread; quote and autocomplete paths await it before lookups.
_build_lock = threading.Lock()
_warm: dict = {}


def _build() -> None:
    from services import geo

    with _build_lock:
        geo._dataset()  # the UN/LOCODE + airport table behind geo.search / find_location (also seconds when cold)
        _countries()
        _places()


def is_ready() -> bool:
    from services import geo

    return all(f.cache_info().currsize > 0 for f in (geo._dataset, _countries, _places))


async def ready() -> None:
    if is_ready():
        return
    loop = asyncio.get_running_loop()
    task = _warm.get(loop)
    if task is None or (task.done() and task.exception() is not None):
        task = _warm[loop] = asyncio.ensure_future(asyncio.to_thread(_build))
    await asyncio.shield(task)


def _start_warm_up() -> None:
    if not is_ready():
        asyncio.get_running_loop().create_task(ready())


def lookup(text: str, *, limit: int = 5) -> list[dict]:
    """Exact name / alias matches, honouring a trailing country (and admin area), most populous first."""
    city, cc, admin = parse_query(text)
    if not city:
        return []
    idx = _rank(exact_idx(city), cc, admin)
    return [place_from_row(row(i)) for i in idx[:limit]]


def suggest(text: str, *, limit: int = 8, min_population: int = 0) -> list[dict]:
    """Autocomplete: exact first, then prefix matches by population."""
    city, cc, admin = parse_query(text)
    if len(city) < 2:
        return []
    p = _places()
    n = p["n_cities"]
    seen: dict[int, str] = {i: "exact" for i in exact_idx(city)}

    def keep():
        return [i for i in seen if population(i) >= min_population and (not cc or row(i)["country"] == cc)]

    if len(city) >= 3:
        lo = bisect.bisect_left(p["terms"], (city, -1))
        for t, i in p["terms"][lo:lo + 4000]:
            if not t.startswith(city):
                break
            seen.setdefault(i, "prefix")
        if len(city) >= 4 and len(keep()) < limit:
            for k in p["towns"].prefix(city):  # towns complete a prefix only when the cities run short ("mazami")
                seen.setdefault(n + k, "prefix")
    idx = keep()
    # Cities before towns, so "lag" lists Lagos before the Polish village Łąg; exact before prefix within each tier.
    idx.sort(key=lambda i: (i >= n, seen[i] != "exact", -population(i)))
    return [place_from_row(row(i), match=seen[i]) for i in idx[:limit]]


# ------------------------------------------------------------------------------------------------ online fallback
def _cache_coll():
    from services.geo import _cache_db

    return _cache_db()["geocode_cache"]


def _online(text: str) -> dict | None:
    """Nominatim free text, Photon on refusal. Returns a place, None for "not found"; raises on refusal of both."""
    from core.util import now_iso
    from services import geo

    key = "place1:" + fold(text)
    try:
        hit = _cache_coll().find_one({"key": key}, {"_id": 0})
    except Exception:  # noqa: BLE001
        hit = None
    if hit:
        return hit.get("result")
    result = None
    refused = False
    try:
        r = geo._throttled_get(f"{geo.GEOCODER_URL}/search", {"q": text, "format": "jsonv2", "limit": 3, "addressdetails": 1,
                                                              "accept-language": "en"})
        if r.status_code == 200:
            for row in r.json() or []:
                addr = row.get("address") or {}
                cc = (addr.get("country_code") or "").upper()
                if not cc:
                    continue
                name = (addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
                        or addr.get("county") or row.get("name") or text.split(",")[0]).strip()
                result = {"unlocode": None, "code": None, "name": name, "city": name, "country": cc,
                          "admin1": addr.get("state") or addr.get("region"), "lat": round(float(row["lat"]), 5),
                          "lon": round(float(row["lon"]), 5), "population": 0, "kinds": ["city"],
                          "geocode": {"source": "nominatim", "match": "search", "confidence": 0.8,
                                      "display_name": row.get("display_name"), "osm_type": row.get("osm_type"),
                                      "osm_id": row.get("osm_id")}}
                break
        else:
            refused = True
            log.warning("Nominatim HTTP %s for %r", r.status_code, text)
    except Exception as exc:  # noqa: BLE001
        refused = True
        log.warning("Nominatim failed for %r: %s", text, exc)
    if refused:
        try:
            r = geo._throttled_get(f"{geo.PHOTON_URL}/api/", {"q": text, "limit": 3, "lang": "en"})
            if r.status_code != 200:
                raise RuntimeError(f"Photon HTTP {r.status_code}")
            for f in r.json().get("features") or []:
                pr = f.get("properties") or {}
                cc = (pr.get("countrycode") or "").upper()
                if not cc:
                    continue
                lon, lat = f["geometry"]["coordinates"][:2]
                name = pr.get("city") or pr.get("name") or text.split(",")[0]
                result = {"unlocode": None, "code": None, "name": name, "city": name, "country": cc, "admin1": pr.get("state"),
                          "lat": round(float(lat), 5), "lon": round(float(lon), 5), "population": 0, "kinds": ["city"],
                          "geocode": {"source": "photon", "match": "search", "confidence": 0.75,
                                      "display_name": ", ".join(x for x in [pr.get("name"), pr.get("state"), pr.get("country")] if x)}}
                break
        except Exception as exc:  # noqa: BLE001
            log.warning("Photon failed for %r: %s", text, exc)
            raise LookupError("geocoder unavailable") from exc
    try:
        _cache_coll().update_one({"key": key}, {"$set": {"key": key, "query": text, "result": result, "cached_at": now_iso()}},
                                 upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return result


async def online(text: str) -> dict | None:
    return await asyncio.to_thread(_online, text)


def plausible_text(text: str | None) -> bool:
    t = (text or "").strip()
    return 2 <= len(t) <= MAX_QUERY and bool(re.search(r"[^\W\d_]", t))


# ------------------------------------------------------------------------------------------------ decision
def _exact_term(loc: dict, text: str) -> bool:
    t = fold(text.split(",")[0])
    names = [loc.get("name"), loc.get("city"), loc.get("unlocode"), loc.get("code"), *(loc.get("aliases") or [])]
    return any(fold(n) == t for n in names if n)


async def resolve_text(text: str, existing: dict | None) -> dict | None:
    """Pick the place a person means by free text.

    `existing` is what the hub/LOCODE dataset matched (services.pricing.search_locations), which keeps seeded lanes
    exact ("Helsinki" -> FIHEL Vuosaari). The gazetteer wins when it is the same name in a different country or far
    away and is the bigger city ("Lagos" -> Lagos, Nigeria, not the Portuguese marina), when the dataset match was only
    a substring, or when the dataset match is an airport record standing in for a city."""
    if not plausible_text(text):
        return existing
    await ready()
    _, cc, _ = parse_query(text)
    cities = lookup(text, limit=3)
    g = cities[0] if cities else None
    if existing and cc and existing.get("country") != cc:
        existing = None  # "Portland, Maine": the hint names the country
    if existing and g:
        near = existing.get("country") == g["country"] and haversine_km(existing, g) <= 150
        if near and _exact_term(existing, text):
            if (existing.get("kinds") or []) == ["airport"]:
                return {**g, "unlocode": None}
            # The city the person named, carrying the hub's UN/LOCODE so seeded lanes for it still match exactly
            # ("Shanghai" -> Shanghai city, CNSHA; "Helsinki" -> Helsinki, FIHEL). Legs still use hub coordinates.
            return {**g, "unlocode": existing.get("unlocode") or existing.get("code"), "code": existing.get("code"),
                    "kinds": sorted(set(["city"] + list(existing.get("kinds") or []))),
                    "geocode": {**g["geocode"], "hub": existing.get("unlocode") or existing.get("code")}}
        if near:
            return g
        if not _exact_term(existing, text) or (g.get("population") or 0) >= 50000:
            return g
        return existing
    if g:
        return g
    if existing and _exact_term(existing, text):
        return {**existing, "geocode": {"source": "unlocode", "match": "exact", "confidence": 0.9}}
    try:
        found = await online(text)
    except LookupError:
        if existing:
            return existing
        raise GeocoderBusy(text) from None
    if found and cc and found.get("country") != cc:
        found = None
    return found or existing
