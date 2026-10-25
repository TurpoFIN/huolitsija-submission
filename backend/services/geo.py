"""Locations and lane geometry.

    await find_location("FIHEL") / await find_location("Vuosaari") -> location dict | None
    route_path("sea", "CNSHA", "FIHEL") -> [[lon, lat], ...]     (sync; see route_info for source + distance)
    await route_info_async(mode, origin, destination)          (runs the sync router in a worker thread)
    path_length_km(path), interpolate(path, fraction), project_fraction(path, lon, lat)

Lane sources (recorded on every leg as path_source so nothing pretends to be more precise than it is):
- sea:  `searoute` (Eurostat SeaRoute maritime network). Red Sea / Bab-el-Mandeb avoided by default
        (SEA_AVOID_RED_SEA=true) because the large carriers still route Asia–Europe via the Cape.
- road: OSRM public server (car profile, includes ferries) cached in Mongo `route_cache`; on failure a great
        circle with path_source "great_circle" and the failure reason in path_note.
- rail: real line geometry from data/rail_corridors.json (Digitraffic station sequences for Finland, curated
        corridors abroad) with Dijkstra over the shared junction nodes; else great circle.
- air:  great-circle interpolation.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import os
import re
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path

import httpx

from core import config
from core.db import db, register_index
from core.lifecycle import on_startup
from core.util import now_iso

log = logging.getLogger("geo")
DATA = Path(__file__).resolve().parent.parent / "data"
EARTH_KM = 6371.0088
MAX_PATH_POINTS = 400
MAX_SEGMENT_KM = 40  # map contract consumers (3D globe) need segments of at most 40 km
PATH_VERSION = 2  # v2: densified, Asia–Europe sea via the Cape (no Panama), air around closed airspace, road ferries noted
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
SEA_AVOID_RED_SEA = os.environ.get("SEA_AVOID_RED_SEA", "true").strip().lower() in ("1", "true", "yes", "on")
GEOCODER_URL = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org").rstrip("/")
PHOTON_URL = os.environ.get("PHOTON_URL", "https://photon.komoot.io").rstrip("/")
USER_AGENT = "Huolitsija/1.0 (+https://huolitsija.fi; ops@huolitsija.fi)"

register_index("locations", "code", unique=True)
register_index("locations", "unlocode")
register_index("locations", "iata")
register_index("locations", "terms")
register_index("locations", [("country", 1), ("rank", -1)])
register_index("route_cache", "key", unique=True)
register_index("geocode_cache", "key", unique=True)


# --------------------------------------------------------------------------------------------- dataset

def fold(s: str | None) -> str:
    s = s or ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().strip()


@lru_cache(maxsize=1)
def _dataset() -> dict:
    raw = json.loads((DATA / "locations_unlocode.json").read_text(encoding="utf-8"))
    cols = raw["columns"]
    by_code: dict[str, dict] = {}
    by_unlocode: dict[str, dict] = {}
    by_iata: dict[str, dict] = {}
    for row in raw["rows"]:
        d = {k: v for k, v in zip(cols, row)}
        d["aliases"] = d.get("aliases") or []
        d["terms"] = sorted({fold(x) for x in [d["code"], d.get("unlocode"), d.get("iata"), d["name"], d.get("city"),
                                               *d["aliases"]] if x})
        by_code[d["code"]] = d
    for d in by_code.values():
        u = d.get("unlocode")
        if u:
            cur = by_unlocode.get(u)
            # primary record per UN/LOCODE: the record whose code is the LOCODE itself, else the highest rank
            if cur is None or (d["code"] == u and cur["code"] != u) or (cur["code"] != u and d["rank"] > cur["rank"]):
                by_unlocode[u] = d
        if d.get("iata") and "airport" in d["kinds"]:
            cur = by_iata.get(d["iata"])
            if cur is None or d["code"] == d["iata"]:
                by_iata[d["iata"]] = d
    return {"version": raw["version"], "by_code": by_code, "by_unlocode": by_unlocode, "by_iata": by_iata}


def public_location(d: dict | None) -> dict | None:
    if not d:
        return None
    out = {k: d.get(k) for k in ("code", "unlocode", "iata", "name", "city", "country", "subdivision", "lat", "lon",
                               "kinds", "aliases", "terminal", "rank", "source")}
    if d.get("territory_code") is not None:
        out["territory_code"] = d["territory_code"]
    return out


def location_ref(d: dict | None, **extra) -> dict | None:
    """Compact shipment/leg location, including the structured customs-territory evidence."""
    if not d:
        return None
    out = {"name": d.get("name"), "code": d.get("code"), "unlocode": d.get("unlocode") or d.get("code"),
           "lat": d.get("lat"), "lon": d.get("lon"), "country": d.get("country")}
    for field in ("subdivision", "territory_code"):
        if d.get(field) is not None:
            out[field] = d[field]
    if d.get("city"):
        out["city"] = d["city"]
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def lookup_code(code: str | None) -> dict | None:
    """Exact lookup by our code, UN/LOCODE or IATA (sync, in-memory)."""
    if not code:
        return None
    c = code.strip().upper()
    ds = _dataset()
    hit = ds["by_code"].get(c) or ds["by_unlocode"].get(c) or ds["by_iata"].get(c)
    if hit or len(c) != 5:
        return hit
    return _network_location(c)


def _network_location(c: str) -> dict | None:
    """V2's rate network names a few hubs with its own codes (e.g. KZHRG Khorgos, AZBAK Baku): accept them."""
    try:
        from services.carriers import NETWORK_LOCATIONS
    except Exception:  # noqa: BLE001 - V2 not deployed
        return None
    n = NETWORK_LOCATIONS.get(c)
    if not n or n.get("lat") is None:
        return None
    return {"code": c, "unlocode": None, "iata": None, "name": n["name"], "city": n["name"], "country": n.get("country"),
            "subdivision": None, "lat": n["lat"], "lon": n["lon"], "kinds": [k for k in n.get("kinds") or [] if k != "city"] or ["road"],
            "aliases": n.get("aliases") or [], "rank": 30, "source": "v2_network", "terminal": None, "terms": [c.lower()]}


def search(q: str = "", kind: str | None = None, limit: int = 20, country: str | None = None) -> list[dict]:
    """Ranked search over name, aliases, code, UN/LOCODE, IATA and country code (in-memory, ~27k records)."""
    ds = _dataset()
    qf = fold(q)
    limit = max(1, min(int(limit or 20), 100))
    out: list[tuple] = []
    if not qf:
        for d in ds["by_code"].values():
            if (not kind or kind in d["kinds"]) and (not country or d["country"] == country.upper()):
                out.append((0, -d["rank"], d["name"], d))
        out.sort(key=lambda x: x[:3])
        return [public_location(x[3]) for x in out[:limit]]
    words = qf.split()
    cc_hint = None
    if len(words) > 1 and len(words[-1]) == 2 and words[-1].isalpha():
        cc_hint = words[-1].upper()  # "Hamburg DE"
    for d in ds["by_code"].values():
        if kind and kind not in d["kinds"]:
            continue
        if country and d["country"] != country.upper():
            continue
        terms = d["terms"]
        score = None
        code_l = d["code"].lower()
        if qf == code_l or qf == (d.get("unlocode") or "").lower() or qf == (d.get("iata") or "").lower():
            score = 0
        elif any(t == qf for t in terms):
            score = 1
        elif any(t.startswith(qf) for t in terms):
            score = 2
        elif cc_hint and d["country"] == cc_hint and any(t.startswith(" ".join(words[:-1])) for t in terms):
            score = 2
        elif len(qf) >= 3 and any(qf in t for t in terms):
            score = 3
        elif len(qf) == 2 and d["country"].lower() == qf:
            score = 4
        if score is not None:
            out.append((score, -d["rank"], d["name"], d))
    out.sort(key=lambda x: x[:3])
    return [public_location(x[3]) for x in out[:limit]]


async def find_location(query_or_locode) -> dict | None:
    """Resolve a UN/LOCODE, IATA code, our code, free-text name, or an existing location dict."""
    if query_or_locode is None:
        return None
    if isinstance(query_or_locode, dict):
        q = query_or_locode
        code = q.get("code") or q.get("unlocode") or q.get("locode") or q.get("iata")
        hit = lookup_code(code) if code else None
        if hit:
            return public_location(hit)
        if q.get("lat") is not None and q.get("lon") is not None:
            return {"code": code, "unlocode": q.get("unlocode") or code, "name": q.get("name") or code or "Location",
                    "city": q.get("city") or q.get("name"), "country": q.get("country"), "lat": float(q["lat"]),
                    "lon": float(q["lon"]), "kinds": q.get("kinds") or [], "aliases": [], "source": "given",
                    **{k:q[k] for k in ("subdivision", "territory_code") if q.get(k) is not None}}
        if q.get("name"):
            return await find_location(q["name"])
        return None
    s = str(query_or_locode).strip()
    if not s:
        return None
    hit = lookup_code(s) if re.fullmatch(r"[A-Za-z0-9]{3,5}", s) else None
    if hit:
        return public_location(hit)
    res = search(s, limit=1)
    if res:
        return res[0]
    doc = await db.locations.find_one({"$or": [{"code": s.upper()}, {"terms": fold(s)}]}, {"_id": 0})
    return public_location(doc) if doc else None


@on_startup(order=12)
async def load_locations():
    """Keep the `locations` collection in step with the dataset files (reloads when the build version changes)."""
    ds = _dataset()
    marker = f"locations:{ds['version']}"
    if await db.system.find_one({"key": "locations:version", "value": marker}) and await db.locations.estimated_document_count() > 1000:
        return
    docs = list(ds["by_code"].values())
    await db.locations.delete_many({"source": {"$in": ["unlocode", "ourairports", "curated"]}})
    batch = []
    for d in docs:
        batch.append({k: d.get(k) for k in ("code", "unlocode", "iata", "name", "city", "country", "subdivision", "lat",
                                            "lon", "kinds", "aliases", "terminal", "rank", "source", "terms")})
        if len(batch) >= 2000:
            await db.locations.insert_many(batch, ordered=False)
            batch = []
    if batch:
        await db.locations.insert_many(batch, ordered=False)
    await db.system.update_one({"key": "locations:version"}, {"$set": {"value": marker, "at": now_iso(), "count": len(docs)}},
                               upsert=True)
    log.info("locations loaded: %s records (%s)", len(docs), marker)


# --------------------------------------------------------------------------------------------- geometry

def haversine_km(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def path_length_km(path) -> float:
    if not path or len(path) < 2:
        return 0.0
    return round(sum(haversine_km(path[i], path[i + 1]) for i in range(len(path) - 1)), 1)


def bearing_deg(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def interpolate(path, fraction: float) -> dict:
    """Point at `fraction` (0..1) of the path length: {lon, lat, bearing}."""
    if not path:
        return {"lon": None, "lat": None, "bearing": None}
    if len(path) == 1:
        return {"lon": path[0][0], "lat": path[0][1], "bearing": None}
    f = min(1.0, max(0.0, float(fraction or 0)))
    seg = [haversine_km(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg)
    if total <= 0:
        return {"lon": path[0][0], "lat": path[0][1], "bearing": None}
    target = f * total
    acc = 0.0
    for i, s in enumerate(seg):
        if acc + s >= target or i == len(seg) - 1:
            t = 0 if s == 0 else (target - acc) / s
            t = min(1.0, max(0.0, t))
            a, b = path[i], path[i + 1]
            lon = a[0] + (b[0] - a[0]) * t
            lat = a[1] + (b[1] - a[1]) * t
            return {"lon": round(_wrap_lon(lon), 5), "lat": round(lat, 5), "bearing": round(bearing_deg(a, b))}
        acc += s
    return {"lon": path[-1][0], "lat": path[-1][1], "bearing": None}


def project_fraction(path, lon: float, lat: float) -> tuple[float, float]:
    """Fraction along the path closest to (lon, lat) and the distance (km) from the path."""
    if not path or len(path) < 2:
        return 0.0, 0.0
    seg = [haversine_km(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg) or 1.0
    best = (float("inf"), 0.0)
    acc = 0.0
    coslat = math.cos(math.radians(lat))
    for i, s in enumerate(seg):
        a, b = path[i], path[i + 1]
        ax, ay = _wrap_lon(a[0]) * coslat, a[1]
        bx, by = _wrap_lon(b[0]) * coslat, b[1]
        px, py = _wrap_lon(lon) * coslat, lat
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        q = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        d = haversine_km(q, [lon, lat])
        if d < best[0]:
            best = (d, (acc + s * t) / total)
        acc += s
    return round(best[1], 4), round(best[0], 1)


def _wrap_lon(lon: float) -> float:
    return ((lon + 180) % 360) - 180


def unwrap(path):
    """Keep longitudes continuous across the antimeridian so maps draw one line, not a world-wide jump."""
    if not path:
        return path
    out = [[path[0][0], path[0][1]]]
    for lon, lat in ((p[0], p[1]) for p in path[1:]):
        prev = out[-1][0]
        while lon - prev > 180:
            lon -= 360
        while lon - prev < -180:
            lon += 360
        out.append([lon, lat])
    return out


def great_circle(a, b, n: int | None = None):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(math.sqrt(math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2))
    if d == 0:
        return [[a[0], a[1]], [b[0], b[1]]]
    if n is None:
        n = int(max(16, min(128, d * EARTH_KM / 80)))
    pts = []
    for i in range(n + 1):
        f = i / n
        A = math.sin((1 - f) * d) / math.sin(d)
        B = math.sin(f * d) / math.sin(d)
        x = A * math.cos(lat1) * math.cos(lon1) + B * math.cos(lat2) * math.cos(lon2)
        y = A * math.cos(lat1) * math.sin(lon1) + B * math.cos(lat2) * math.sin(lon2)
        z = A * math.sin(lat1) + B * math.sin(lat2)
        pts.append([math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))])
    pts[0] = [a[0], a[1]]
    pts[-1] = [b[0], b[1]]
    return unwrap(pts)


def simplify(path, max_points: int = MAX_PATH_POINTS):
    """Douglas–Peucker with a growing tolerance until the path has at most max_points vertices."""
    pts = [[round(p[0], 5), round(p[1], 5)] for p in path or []]
    if len(pts) <= max_points:
        return pts
    tol = 0.002
    out = pts
    while len(out) > max_points and tol < 5:
        out = _dp(pts, tol)
        tol *= 1.8
    if len(out) > max_points:  # last resort: uniform sampling keeping the ends
        step = len(out) / (max_points - 1)
        out = [out[int(i * step)] for i in range(max_points - 1)] + [out[-1]]
    return out


def _dp(pts, tol):
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = pts[s]
        bx, by = pts[e]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-12
        idx, dmax = None, -1.0
        for i in range(s + 1, e):
            px, py = pts[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / norm if (dx or dy) else math.hypot(px - ax, py - ay)
            if d > dmax:
                idx, dmax = i, d
        if idx is not None and dmax > tol:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(pts, keep) if k]


def _gc_point(a, b, f: float):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(min(1.0, math.sqrt(math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)))
    if d == 0:
        return [a[0], a[1]]
    A = math.sin((1 - f) * d) / math.sin(d)
    B = math.sin(f * d) / math.sin(d)
    x = A * math.cos(lat1) * math.cos(lon1) + B * math.cos(lat2) * math.cos(lon2)
    y = A * math.cos(lat1) * math.sin(lon1) + B * math.cos(lat2) * math.sin(lon2)
    z = A * math.sin(lat1) + B * math.sin(lat2)
    return [math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))]


def densify(path, max_km: float = MAX_SEGMENT_KM):
    """Insert great-circle points so no segment is longer than max_km (maps draw straight segments; the globe needs
    short ones, and interpolation along a short segment stays on the real lane)."""
    if not path or len(path) < 2:
        return path
    out = [[path[0][0], path[0][1]]]
    for a, b in zip(path, path[1:]):
        d = haversine_km(a, b)
        n = int(math.ceil(d / max_km)) if d > max_km else 1
        for i in range(1, n):
            q = _gc_point(a, b, i / n)
            # keep longitudes continuous with the unwrapped input
            while q[0] - out[-1][0] > 180:
                q[0] -= 360
            while q[0] - out[-1][0] < -180:
                q[0] += 360
            out.append([round(q[0], 5), round(q[1], 5)])
        out.append([b[0], b[1]])
    return out


def wrap_path(path):
    """Longitudes into [-180, 180] (the map contract); unwrapped storage keeps interpolation continuous."""
    if not path or all(-180 <= p[0] <= 180 for p in path):
        return path
    return [[round(_wrap_lon(p[0]), 5), p[1]] for p in path]


# --------------------------------------------------------------------------------------------- routing

def _point(x) -> tuple[list[float], dict | None]:
    """Accept a location dict, [lon, lat], or a code/name; return ([lon, lat], location|None)."""
    if isinstance(x, (list, tuple)) and len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
        return [float(x[0]), float(x[1])], None
    if isinstance(x, dict):
        if x.get("lat") is not None and x.get("lon") is not None:
            return [float(x["lon"]), float(x["lat"])], x
        hit = lookup_code(x.get("code") or x.get("unlocode") or x.get("iata"))
        if hit:
            return [hit["lon"], hit["lat"]], hit
    if isinstance(x, str):
        hit = lookup_code(x) or (search(x, limit=1) or [None])[0]
        if hit:
            return [hit["lon"], hit["lat"]], hit
    raise ValueError(f"Unknown location: {x!r}")


_sync_client = None
_sync_lock = threading.Lock()


def _cache_db():
    global _sync_client
    with _sync_lock:
        if _sync_client is None:
            from pymongo import MongoClient

            _sync_client = MongoClient(config.MONGO_URL, serverSelectionTimeoutMS=3000)
    return _sync_client[config.DB_NAME]


def _cache():
    return _cache_db()["route_cache"]


def _cache_get(key: str):
    try:
        return _cache().find_one({"key": key}, {"_id": 0})
    except Exception:  # noqa: BLE001 - cache is an optimisation
        return None


def _cache_put(key: str, value: dict):
    try:
        _cache().update_one({"key": key}, {"$set": {**value, "key": key, "cached_at": now_iso()}}, upsert=True)
    except Exception:  # noqa: BLE001
        pass


_geo_lock = threading.Lock()
_geo_last = [0.0]


_GEO_FAIL = object()


def _throttled_get(url: str, params: dict):
    import time

    with _geo_lock:
        wait = 1.1 - (time.monotonic() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        finally:
            _geo_last[0] = time.monotonic()


def _nominatim(address: dict):
    params = {"format": "jsonv2", "limit": 1, "addressdetails": 0, "city": address.get("city")}
    if address.get("street"):
        params["street"] = address["street"]
    if address.get("postal_code"):
        params["postalcode"] = address["postal_code"]
    if address.get("country"):
        params["countrycodes"] = address["country"].lower()
    approximate = False
    for attempt in range(2):
        r = _throttled_get(f"{GEOCODER_URL}/search", params)
        if r.status_code != 200:
            log.warning("Nominatim HTTP %s", r.status_code)
            return _GEO_FAIL
        rows = r.json()
        if rows or not params.get("street"):
            break
        params.pop("street", None)  # retry without the street (new streets, typos): town-level, marked approximate
        approximate = True
    if not rows:
        return None
    return {"lat": round(float(rows[0]["lat"]), 6), "lon": round(float(rows[0]["lon"]), 6), "display_name": rows[0].get("display_name"),
            "approximate": approximate, "source": "nominatim"}


def _photon(q: str, country: str | None):
    r = _throttled_get(f"{PHOTON_URL}/api/", {"q": q, "limit": 3})
    if r.status_code != 200:
        log.warning("Photon HTTP %s", r.status_code)
        return _GEO_FAIL
    for f in r.json().get("features") or []:
        pr = f.get("properties") or {}
        if country and (pr.get("countrycode") or "").upper() != country.upper():
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        exact = bool(pr.get("housenumber") or pr.get("type") == "house")
        name = ", ".join(x for x in [pr.get("name"), " ".join(x for x in [pr.get("street"), pr.get("housenumber")] if x),
                                     " ".join(x for x in [pr.get("postcode"), pr.get("city")] if x), pr.get("country")] if x)
        return {"lat": round(lat, 6), "lon": round(lon, 6), "display_name": name, "approximate": not exact, "source": "photon"}
    return None


def geocode(address: dict | None) -> dict | None:
    """Street address -> {lat, lon, display_name, approximate, source}, cached in Mongo `geocode_cache`.

    OpenStreetMap Nominatim first (usage policy: identify the app, at most one request per second); when it refuses
    (HTTP 429/5xx) the Photon geocoder (komoot, OpenStreetMap data) answers instead. GEOCODER_URL and PHOTON_URL switch
    to self-hosted or commercial endpoints. A refusal is never cached as "not found"."""
    if not address or not (address.get("street") or address.get("postal_code")) or not address.get("city"):
        return None
    q = ", ".join(x for x in [address.get("street"), f"{address.get('postal_code') or ''} {address.get('city')}".strip(),
                              address.get("country")] if x)
    key = "geo2:" + fold(q)  # geo2: v1 cached rate-limit failures as "not found"
    try:
        hit = _cache_db()["geocode_cache"].find_one({"key": key}, {"_id": 0})
    except Exception:  # noqa: BLE001
        hit = None
    if hit:
        return hit.get("result")
    try:
        result = _nominatim(address)
        if result is _GEO_FAIL:
            result = _photon(q, address.get("country"))
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode failed for %s: %s", q, exc)
        return None
    if result is _GEO_FAIL:
        return None
    try:
        _cache_db()["geocode_cache"].update_one({"key": key}, {"$set": {"key": key, "query": q, "result": result, "cached_at": now_iso()}},
                                               upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return result


async def geocode_async(address: dict | None) -> dict | None:
    return await asyncio.to_thread(geocode, address)


def _sea_restrictions(o=None, d=None) -> list[str]:
    r = ["northwest"]  # no Northwest Passage for commercial liner services
    if SEA_AVOID_RED_SEA:
        r.append("babalmandab")
    if o is not None and d is not None and _crosses_suez_region(o, d):
        r.append("panama")  # Asia–Europe liner services go via Suez or the Cape, never trans-Pacific + Panama
    return r


# Kiel Canal (Nord-Ostsee-Kanal), Brunsbüttel locks -> Kiel-Holtenau locks -> Kiel Bight. searoute's network has no
# canal, so North Sea <-> Baltic feeders would be sent round Skagen (+700 km); feeders (<= 235 m) use the canal.
KIEL_CANAL = [[9.143, 53.892], [9.20, 53.92], [9.25, 53.975], [9.27, 54.03], [9.32, 54.08], [9.39, 54.12], [9.47, 54.16],
              [9.56, 54.22], [9.62, 54.28], [9.68, 54.305], [9.75, 54.33], [9.82, 54.36], [9.92, 54.37], [10.02, 54.36],
              [10.10, 54.365], [10.15, 54.368], [10.18, 54.41], [10.25, 54.48]]


def _north_sea(p) -> bool:
    return -5 <= p[0] <= 10.0 and 49 <= p[1] <= 57.5 and not (p[0] > 8.0 and p[1] > 55.0)  # Channel to the German Bight


def _baltic(p) -> bool:
    return p[0] >= 10.0 and p[1] >= 53.5 and not (p[0] < 12.6 and p[1] > 56.0)  # east of Denmark, Kattegat excluded


def _sea(o, d, via=None) -> tuple[list, str, str | None]:
    import searoute as sr

    stops = [o] + [list(v) for v in (via or [])] + [d]
    kiel = not via and ((_north_sea(o) and _baltic(d)) or (_baltic(o) and _north_sea(d)))
    if kiel:
        canal = KIEL_CANAL if _north_sea(o) else list(reversed(KIEL_CANAL))
        stops = [o, canal[0], canal[-1], d]
    path: list = []
    for i, (a, b) in enumerate(zip(stops, stops[1:])):
        if kiel and i == 1:
            seg = canal
        elif haversine_km(a, b) < 3:
            seg = [a, b]
        else:
            feat = sr.searoute(a, b, units="km", append_orig_dest=True, restrictions=_sea_restrictions(o, d))
            seg = feat["geometry"]["coordinates"]
        path += seg if not path else seg[1:]
    note = "Cape of Good Hope routing (Red Sea avoided)" if SEA_AVOID_RED_SEA and _crosses_suez_region(o, d) else \
        "Via the Kiel Canal" if kiel else None
    return unwrap(path), "searoute", note


def sea_path_through(o, here, d) -> list:
    """Sea lane from o to d that passes exactly through `here` (a vessel's AIS position)."""
    a, _, _ = _sea(o, here)
    b, _, _ = _sea(here, d)
    a = a[:-1] + [[here[0], here[1]]]
    return densify(simplify(unwrap(a + b[1:])))


def _crosses_suez_region(o, d) -> bool:
    east = lambda p: p[0] > 45  # noqa: E731
    europe = lambda p: -15 < p[0] < 45 and p[1] > 30  # noqa: E731
    return (east(o) and europe(d)) or (east(d) and europe(o))


# Air: EU carriers may not overfly Russia or Belarus (and Russia bars EU carriers); Ukrainian airspace is closed.
# Long-haul flights between Europe and Asia therefore fly the southern corridors. Waypoints are [lon, lat].
AIR_GATE_EU_SE = [28.5, 43.5]                     # western Black Sea coast (Bulgaria/Romania)
AIR_TO_NORDIC = [[26.5, 45.5], [21.6, 47.8], [20.5, 50.5], [18.8, 54.8], [21.5, 58.5]]  # west of UA/BY/Kaliningrad
AIR_CENTRAL_ASIA = [[108.9, 34.3], [87.6, 43.8], [76.9, 43.3], [65.5, 44.8], [51.2, 43.6], [49.8, 40.4], [44.8, 41.7],
                    [39.5, 41.6], [35.0, 42.3], [30.5, 43.2]]          # China – Kazakhstan – Caspian – Caucasus – Black Sea
AIR_MIDDLE_EAST = [[48.0, 29.5], [44.0, 33.5], [38.0, 38.5], [33.0, 41.5], [30.5, 43.2]]  # Gulf – Iraq – Turkey – Black Sea
AIR_SOUTH_ASIA = [[80.0, 13.5], [66.5, 23.0], [56.5, 26.0]]           # Bay of Bengal – Arabian Sea – Gulf of Oman


def _in_europe(p) -> bool:
    return -25 <= p[0] <= 45 and 34 <= p[1] <= 72


def _air_waypoints(o, d) -> list | None:
    """Waypoints from the Asian end towards Europe, or None for a direct great circle."""
    eu, other = (o, d) if _in_europe(o) else (d, o) if _in_europe(d) else (None, None)
    if eu is None or other[0] <= 45:
        return None
    if other[0] >= 95 and other[1] >= 18:      # China, Korea, Japan, Taiwan, Hong Kong
        way = list(AIR_CENTRAL_ASIA)
    elif other[0] >= 70 and other[1] < 18:     # South-East Asia, southern India
        way = list(AIR_SOUTH_ASIA) + list(AIR_MIDDLE_EAST)
    elif other[0] >= 70:                       # northern India, Bangladesh: Arabian Sea, then the Gulf
        way = list(AIR_SOUTH_ASIA[1:]) + list(AIR_MIDDLE_EAST)
    else:                                      # Gulf, Middle East, Central Asia west of 70E
        way = list(AIR_MIDDLE_EAST)
    way.append(AIR_GATE_EU_SE)
    if eu[1] > 46.5 and eu[0] > 14:  # Central Europe, Poland, Baltics, Nordics: stay west of Ukraine, Belarus, Kaliningrad
        way += [w for w in AIR_TO_NORDIC if w[1] < eu[1] - 0.5]
    return way if _in_europe(d) else list(reversed(way))


def _air(o, d) -> tuple[list, str, str | None]:
    way = _air_waypoints(o, d)
    if not way:
        return great_circle(o, d), "great_circle", None
    stops = [o] + way + [d]
    path: list = []
    for a, b in zip(stops, stops[1:]):
        seg = great_circle(a, b, n=max(2, int(haversine_km(a, b) / 60)))
        path += seg if not path else seg[1:]
    return unwrap(path), "great_circle", "Routed around closed Russian, Belarusian and Ukrainian airspace"


def _road(o, d) -> tuple[list, str, str | None, float | None, float | None]:
    key = f"road2:{o[0]:.4f},{o[1]:.4f};{d[0]:.4f},{d[1]:.4f}"
    hit = _cache_get(key)
    if hit and hit.get("path"):
        return hit["path"], "osrm", hit.get("note"), hit.get("distance_km"), hit.get("duration_h")
    url = f"{OSRM_URL}/route/v1/driving/{o[0]:.6f},{o[1]:.6f};{d[0]:.6f},{d[1]:.6f}"
    try:
        r = httpx.get(url, params={"overview": "full", "geometries": "geojson", "steps": "true"},
                      headers={"User-Agent": USER_AGENT}, timeout=30)
        data = r.json()
        if r.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM {r.status_code} {data.get('code')}: {data.get('message', '')}".strip())
        route = data["routes"][0]
        coords = route["geometry"]["coordinates"]
        path = simplify([[o[0], o[1]]] + coords + [[d[0], d[1]]])
        dist = round(route["distance"] / 1000, 1)
        dur = round(route["duration"] / 3600, 2)
        ferry_km = sum(st.get("distance", 0) for lg in route.get("legs") or [] for st in lg.get("steps") or [] if st.get("mode") == "ferry") / 1000
        note = f"Road route includes {ferry_km:.0f} km by ferry; book it as a separate sea leg" if ferry_km > 5 else None
        _cache_put(key, {"path": path, "distance_km": dist, "duration_h": dur, "source": "osrm", "note": note, "ferry_km": round(ferry_km, 1)})
        return path, "osrm", note, dist, dur
    except Exception as exc:  # noqa: BLE001
        log.warning("OSRM route failed %s -> %s: %s", o, d, exc)
        return great_circle(o, d), "great_circle", f"Road router unavailable ({str(exc)[:120]}); straight line shown", None, None


# ---- rail graph

@lru_cache(maxsize=1)
def _rail_graph() -> dict:
    raw = json.loads((DATA / "rail_corridors.json").read_text(encoding="utf-8"))
    nodes: dict[str, list[float]] = {}
    adj: dict[str, list[tuple[str, float, str]]] = {}
    for c in raw["corridors"]:
        pts = c["points"]
        for lon, lat, nid in pts:
            nodes.setdefault(nid, [lon, lat])
        for (lon1, lat1, n1), (lon2, lat2, n2) in zip(pts, pts[1:]):
            if n1 == n2:
                continue
            w = haversine_km([lon1, lat1], [lon2, lat2])
            adj.setdefault(n1, []).append((n2, w, c["id"]))
            adj.setdefault(n2, []).append((n1, w, c["id"]))
    return {"nodes": nodes, "adj": adj, "terminals": raw.get("terminals", {}),
            "names": {c["id"]: c["name"] for c in raw["corridors"]}}


def _nearest_rail_node(p, loc: dict | None) -> tuple[str | None, float]:
    g = _rail_graph()
    code = (loc or {}).get("code") or (loc or {}).get("unlocode")
    if code and code in g["terminals"] and g["terminals"][code] in g["nodes"]:
        nid = g["terminals"][code]
        return nid, haversine_km(p, g["nodes"][nid])
    best, bd = None, float("inf")
    for nid, q in g["nodes"].items():
        dd = haversine_km(p, q)
        if dd < bd:
            best, bd = nid, dd
    return best, bd


# Rail ferries inside corridors: a train on a ship is a sea leg (different carrier, liability regime and geography).
RAIL_FERRIES = [
    {"a": "KZ:Aktau port", "b": "AZ:Alat port", "a_code": "KZAAU", "b_code": "AZALA",
     "carrier": "ASCO (Azerbaijan Caspian Shipping)", "service": "Caspian rail ferry Aktau–Alat"},
]


def _rail_seq(o, d, o_loc=None, d_loc=None) -> tuple[list[str] | None, str | None, dict]:
    g = _rail_graph()
    a, da = _nearest_rail_node(o, o_loc)
    b, db_ = _nearest_rail_node(d, d_loc)
    if not a or not b or da > 90 or db_ > 90:
        return None, f"No rail line within 90 km (origin {da:.0f} km, destination {db_:.0f} km)", {}
    dist = {a: 0.0}
    prev: dict[str, tuple[str, str]] = {}
    pq = [(0.0, a)]
    while pq:
        cd, u = heapq.heappop(pq)
        if u == b:
            break
        if cd > dist.get(u, float("inf")):
            continue
        for v, w, cid in g["adj"].get(u, []):
            nd = cd + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (u, cid)
                heapq.heappush(pq, (nd, v))
    if b not in dist:
        return None, "No connected rail corridor between origin and destination", {}
    seq = [b]
    while seq[-1] != a:
        seq.append(prev[seq[-1]][0])
    seq.reverse()
    return seq, None, prev


def rail_segments(o, d, o_loc=None, d_loc=None) -> list[dict] | None:
    """Split a rail routing at rail-ferry crossings: [{mode, from, to, carrier?, service?}] where from/to None means the
    original origin/destination. None when there is no rail routing at all."""
    o_xy, _ = _point(o)
    d_xy, _ = _point(d)
    seq, _why, _prev = _rail_seq(o_xy, d_xy, o_loc or (o if isinstance(o, dict) else None), d_loc or (d if isinstance(d, dict) else None))
    if not seq:
        return None
    out: list[dict] = []
    start = None
    for f in RAIL_FERRIES:
        if f["a"] in seq and f["b"] in seq:
            ia, ib = seq.index(f["a"]), seq.index(f["b"])
            first, second = (f["a_code"], f["b_code"]) if ia < ib else (f["b_code"], f["a_code"])
            out.append({"mode": "rail", "from": start, "to": first})
            out.append({"mode": "sea", "from": first, "to": second, "carrier": f["carrier"], "service": f["service"], "role": "ferry"})
            start = second
    out.append({"mode": "rail", "from": start, "to": None})
    return out


def rail_route(o, d, o_loc=None, d_loc=None) -> tuple[list | None, str | None, list[str]]:
    g = _rail_graph()
    a, da = _nearest_rail_node(o, o_loc)
    b, db_ = _nearest_rail_node(d, d_loc)
    if not a or not b or da > 90 or db_ > 90:
        return None, f"No rail line within 90 km (origin {da:.0f} km, destination {db_:.0f} km)", []
    dist = {a: 0.0}
    prev: dict[str, tuple[str, str]] = {}
    pq = [(0.0, a)]
    while pq:
        cd, u = heapq.heappop(pq)
        if u == b:
            break
        if cd > dist.get(u, float("inf")):
            continue
        for v, w, cid in g["adj"].get(u, []):
            nd = cd + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (u, cid)
                heapq.heappush(pq, (nd, v))
    if b not in dist:
        return None, "No connected rail corridor between origin and destination", []
    seq = [b]
    used: list[str] = []
    while seq[-1] != a:
        u, cid = prev[seq[-1]]
        if cid not in used:
            used.append(cid)
        seq.append(u)
    seq.reverse()
    path = [g["nodes"][n] for n in seq]
    if haversine_km(o, path[0]) > 0.5:
        path = [o] + path
    if haversine_km(d, path[-1]) > 0.5:
        path = path + [d]
    return path, None, [g["names"][c] for c in reversed(used)]


def route_info(mode: str, origin, destination, *, via=None) -> dict:
    """Lane geometry with provenance: {path, source, note, distance_km, duration_h, corridors}."""
    o, o_loc = _point(origin)
    d, d_loc = _point(destination)
    note = None
    duration = None
    corridors: list[str] = []
    mode = (mode or "").lower()
    try:
        if mode == "sea":
            path, source, note = _sea(o, d, via)
        elif mode == "road":
            path, source, note, _dist, duration = _road(o, d)
        elif mode == "rail":
            path, why, corridors = rail_route(o, d, o_loc, d_loc)
            if path:
                source = "rail_corridor"
            else:
                path, source, note = great_circle(o, d), "great_circle", f"{why}; straight line shown"
        else:
            path, source, note = _air(o, d)
    except Exception as exc:  # noqa: BLE001
        log.warning("route %s %s -> %s failed: %s", mode, o, d, exc)
        path, source, note = great_circle(o, d), "great_circle", f"Routing failed ({str(exc)[:120]}); straight line shown"
    path = densify(simplify(path))
    return {"path": path, "source": source, "note": note, "distance_km": path_length_km(path), "duration_h": duration,
            "corridors": corridors}


def route_path(mode, origin, destination) -> list:
    """Cross-slice contract: [[lon, lat], ...] following the real lane for the mode."""
    return route_info(mode, origin, destination)["path"]


async def route_info_async(mode, origin, destination, *, via=None) -> dict:
    return await asyncio.to_thread(route_info, mode, origin, destination, via=via)
