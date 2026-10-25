"""Public ADS-B and AISStream observations; all persisted positions retain source timestamps.

AISStream protocol: https://aisstream.io/documentation
ADS-B REST schema: https://api.adsb.lol/docs
"""
import asyncio
import json
import math
import os
from datetime import datetime, timezone, timedelta

import httpx
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError

from core.db import db, register_index
from core.jobs import job, schedule
from core.util import NO_ID, iso, now, now_iso, parse_iso

for collection, key in (("vessel_positions", "mmsi"), ("flight_positions", "icao24")):
    register_index(collection, key, unique=True)
register_index("vessel_track", "expires_at", expireAfterSeconds=0)
register_index("vessel_track", [("mmsi", 1), ("at", -1)])
register_index("visibility_feeds", "provider", unique=True)


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def coordinates(lat, lon):
    lat, lon = number(lat), number(lon)
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


async def health(provider, **fields):
    update = {"$set": {**fields, "checked_at": now_iso()}}
    if "state" in fields:
        stale = {key: "" for key in ("error", "http_status", "retry_after_seconds") if key not in fields}
        if stale:
            update["$unset"] = stale
    await db.raw.visibility_feeds.update_one({"provider": provider}, update, upsert=True)


def flight_failure_fields(exc):
    """Retain actionable HTTP diagnostics without exception text, URLs or bodies."""
    fields = {"error": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        fields["http_status"] = exc.response.status_code
        retry = exc.response.headers.get("retry-after", "")
        if retry.isascii() and retry.isdecimal() and len(retry) <= 5 and 0 <= int(retry) <= 86400:
            fields["retry_after_seconds"] = int(retry)
    return fields


FLIGHT_AREA_INTERVAL = 240  # 360 area requests/day, leaving room for callsign lookups within 500/day


def rate_limit_wait(response):
    """Seconds the provider asks us to wait: Retry-After, else the first x-ratelimit-reset value."""
    for header in ("retry-after", "x-ratelimit-reset"):
        value = (response.headers.get(header) or "").split(",")[0].strip()
        if value.isascii() and value.isdecimal() and len(value) <= 6:
            return min(int(value), 86400)
    return None


def ais_message(message):
    kind = message.get("MessageType")
    if kind not in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        return None
    p = (message.get("Message") or {}).get(kind) or {}
    meta = message.get("MetaData") or {}
    lat, lon = p.get("Latitude", meta.get("latitude", meta.get("Latitude"))), p.get("Longitude", meta.get("longitude", meta.get("Longitude")))
    mmsi = str(meta.get("MMSI") or p.get("UserID") or "")
    if p.get("Valid") is False or not coordinates(lat, lon) or len(mmsi) != 9 or not mmsi.isdigit():
        return None
    # AISStream time_utc is the receive time; AIS UTC-second alone is insufficient for an absolute timestamp.
    raw_time = meta.get("time_utc")
    if not raw_time:
        return None
    try:
        at = datetime.fromisoformat(str(raw_time).replace(" UTC", "").replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    sog, heading, cog = number(p.get("Sog")), number(p.get("TrueHeading")), number(p.get("Cog"))
    return {"mmsi": mmsi, "name": (meta.get("ShipName") or "").strip() or None,
            "lat": float(lat), "lon": float(lon), "sog": sog if sog is not None and 0 <= sog < 102.3 else None,
            "cog": cog if cog is not None and 0 <= cog < 360 else None,
            "heading": heading if heading is not None and 0 <= heading < 360 else None,
            "nav_status": p.get("NavigationalStatus"), "at": iso(at), "source": "aisstream"}


async def store_vessel(row):
    if not row or not parse_iso(row.get("at")) or parse_iso(row["at"]) > now() + timedelta(seconds=60):
        return False
    # Atomic monotonic latest write; delayed frames cannot overwrite a newer position.
    try:
        result = await db.raw.vessel_positions.update_one(
            {"mmsi": row["mmsi"], "$or": [{"at": {"$lt": row["at"]}}, {"at": {"$exists": False}}]},
            {"$set": row}, upsert=True)
    except DuplicateKeyError:
        return False
    if result.modified_count or result.upserted_id:
        await db.raw.vessel_track.insert_one({**row, "expires_at": now() + timedelta(days=14)})
    return True


async def store_vessels(rows):
    """Bulk form of store_vessel for the public job: 3 round trips instead of 2 per row (remote Mongo)."""
    limit = now() + timedelta(seconds=60)
    rows = [r for r in rows if r and parse_iso(r.get("at")) and parse_iso(r["at"]) <= limit]
    if not rows:
        return 0
    known = {d["mmsi"]: d.get("at") async for d in db.raw.vessel_positions.find(
        {"mmsi": {"$in": [r["mmsi"] for r in rows]}}, {"_id": 0, "mmsi": 1, "at": 1})}
    newer = [r for r in rows if not known.get(r["mmsi"]) or r["at"] > known[r["mmsi"]]]
    if not newer:
        return 0
    try:
        await db.raw.vessel_positions.bulk_write([UpdateOne(
            {"mmsi": r["mmsi"], "$or": [{"at": {"$lt": r["at"]}}, {"at": {"$exists": False}}]}, {"$set": r}, upsert=True)
            for r in newer], ordered=False)
    except BulkWriteError as exc:  # a concurrent newer write wins (duplicate upsert); anything else is real
        if any(e.get("code") != 11000 for e in exc.details.get("writeErrors", [])):
            raise
    expires = now() + timedelta(days=14)
    await db.raw.vessel_track.insert_many([{**r, "expires_at": expires} for r in newer], ordered=False)
    return len(newer)


async def latest_vessel(mmsi):
    row = await db.raw.vessel_positions.find_one({"mmsi": str(mmsi)}, NO_ID)
    if row:
        age = (now() - parse_iso(row["at"])).total_seconds()
        if 0 <= age < 900:
            return {**row, "age_s": age}
    return None


VESSELAPI = "https://api.vesselapi.com/v1"
VESSELAPI_REFRESH_S = 6 * 3600  # per vessel; the free plan allows 150 calls a month
VESSELAPI_FRESH_S = 6 * 3600    # oldest worldwide fix shown on the map; ETA keeps its own 15 min rule
VESSELAPI_MISS_S = 72 * 3600    # "no position found" for a vessel: ask again after 3 days, not every 6 h


def vesselapi_row(data):
    """VesselAPI /vessel/{id}/position (https://vesselapi.com/docs/vessels). Glitches and missing times are dropped."""
    p = (data or {}).get("vesselPosition") or {}
    mmsi = str(p.get("mmsi") or "")
    at = parse_iso(p.get("timestamp"))
    if p.get("suspected_glitch") or not at or len(mmsi) != 9 or not mmsi.isdigit() or not coordinates(p.get("latitude"), p.get("longitude")):
        return None
    sog, cog, heading = number(p.get("sog")), number(p.get("cog")), number(p.get("heading"))
    return {"mmsi": mmsi, "name": (p.get("vessel_name") or "").strip() or None, "lat": float(p["latitude"]), "lon": float(p["longitude"]),
            "sog": sog if sog is not None and 0 <= sog < 102.3 else None, "cog": cog if cog is not None and 0 <= cog < 360 else None,
            "heading": heading if heading is not None and 0 <= heading < 360 else None, "nav_status": p.get("nav_status"),
            "at": iso(at), "source": "vesselapi"}


async def _vesselapi_budget():
    """Atomically take one call from today's budget (VESSELAPI_DAILY_CALLS, default 4)."""
    cap = int(os.getenv("VESSELAPI_DAILY_CALLS", "4") or 4)
    day = now().date().isoformat()
    await db.raw.visibility_feeds.update_one({"provider": "vesselapi", "budget_day": {"$ne": day}},
                                            {"$set": {"budget_day": day, "calls_today": 0}})
    taken = await db.raw.visibility_feeds.find_one_and_update(
        {"provider": "vesselapi", "budget_day": day, "calls_today": {"$lt": cap},
         "$or": [{"next_attempt_at": {"$exists": False}}, {"next_attempt_at": {"$lte": now_iso()}}]},
        {"$inc": {"calls_today": 1}})
    return taken is not None


async def vesselapi_location(mmsi):
    """Worldwide AIS fallback for a shipment vessel outside Digitraffic coverage: {..., age_s} or None."""
    mmsi = str(mmsi or "")
    if not os.getenv("VESSELAPI_API_KEY", "").strip():
        await health("vesselapi", configured=False, state="not_configured")
        return None
    if len(mmsi) != 9 or not mmsi.isdigit():
        return None
    row = await db.raw.vessel_positions.find_one({"mmsi": mmsi}, NO_ID)
    lookup = await db.raw.vesselapi_lookups.find_one({"mmsi": mmsi}, NO_ID)
    wait_s = VESSELAPI_MISS_S if (lookup or {}).get("missing") else VESSELAPI_REFRESH_S
    due = not lookup or (now() - parse_iso(lookup["checked_at"])).total_seconds() >= wait_s
    await db.raw.visibility_feeds.update_one({"provider": "vesselapi"}, {"$setOnInsert": {"configured": True}}, upsert=True)
    if due and await _vesselapi_budget():
        await db.raw.vesselapi_lookups.update_one({"mmsi": mmsi}, {"$set": {"checked_at": now_iso()}}, upsert=True)
        try:
            async with httpx.AsyncClient(timeout=15, headers={"Authorization": "Bearer " + os.getenv("VESSELAPI_API_KEY", "").strip(),
                                                            "Accept": "application/json"}) as c:
                r = await c.get(f"{VESSELAPI}/vessel/{mmsi}/position", params={"filter.idType": "mmsi"})
                r.raise_for_status()
                fresh = vesselapi_row(r.json())
            if fresh and fresh["mmsi"] == mmsi:
                await store_vessel(fresh)
            await health("vesselapi", configured=True, state="ok", coverage="worldwide (terrestrial + provider network)",
                         last_observation_at=fresh["at"] if fresh else None)
            row = await db.raw.vessel_positions.find_one({"mmsi": mmsi}, NO_ID)
            await db.raw.vesselapi_lookups.update_one({"mmsi": mmsi}, {"$set": {"missing": False}})
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                # The provider answered: it has no position for this vessel. That is the vessel's state, not an outage.
                await db.raw.vesselapi_lookups.update_one({"mmsi": mmsi}, {"$set": {"missing": True}})
                await health("vesselapi", configured=True, state="ok", coverage="worldwide (terrestrial + provider network)",
                             last_miss={"mmsi": mmsi, "http_status": 404, "at": now_iso()})
                return None
            fields = flight_failure_fields(exc)
            if isinstance(exc, httpx.HTTPStatusError) and (wait := rate_limit_wait(exc.response)):
                fields["next_attempt_at"] = iso(now() + timedelta(seconds=wait))
            await health("vesselapi", configured=True, state="unavailable", **fields)
    if row and row.get("source") == "vesselapi":  # older fixes from other feeds keep their own 15 min rule
        age = (now() - parse_iso(row["at"])).total_seconds()
        if 0 <= age < VESSELAPI_FRESH_S:
            return {**row, "age_s": age}
    return None


def digitraffic_rows(data):
    """Official GeoJSON with source time; AIS sentinel values are unavailable."""
    rows = []
    for feature in data.get("features") or []:
        p = feature.get("properties") or {}
        point = (feature.get("geometry") or {}).get("coordinates") or []
        mmsi = str(p.get("mmsi") or "")
        stamp = number(p.get("timestampExternal"))
        if len(point) < 2 or not coordinates(point[1], point[0]) or len(mmsi) != 9 or not mmsi.isdigit() or not stamp:
            continue
        if not 0 <= now().timestamp() - stamp / 1000 < 900:
            continue
        sog, cog, heading = (number(p.get(k)) for k in ("sog", "cog", "heading"))
        rows.append({"mmsi": mmsi, "lon": float(point[0]), "lat": float(point[1]),
                     "sog": sog if sog is not None and 0 <= sog < 102.3 else None,
                     "cog": cog if cog is not None and 0 <= cog < 360 else None,
                     "heading": heading if heading is not None and 0 <= heading < 360 else None,
                     "nav_status": p.get("navStat"), "at": iso(datetime.fromtimestamp(stamp / 1000, timezone.utc)),
                     "source": "digitraffic"})
    return sorted(rows, key=lambda row: row["at"], reverse=True)


async def store_flight(row):
    """Concurrent area and callsign fetches cannot regress the last observation."""
    try:
        await db.raw.flight_positions.update_one(
            {"icao24": row["icao24"], "$or": [{"at": {"$lt": row["at"]}}, {"at": {"$exists": False}}]},
            {"$set": row}, upsert=True)
    except DuplicateKeyError:
        return False
    return True


@job("visibility.public", timeout=90, label_fi="Julkiset liikennehavainnot", label_en="Public traffic observations")
@schedule("visibility.public", job="visibility.public", every_seconds=120)
async def public_observations(j):
    """Real public observations, independent of customer or demo cargo.

    AIS covers Digitraffic's Finnish waterways. The flight overview covers 250nm
    around Helsinki; shipment callsign lookups retain worldwide provider coverage.
    Neither layer establishes cargo loading, milestones or customer ownership.
    """
    from core import config
    from services.positions import DIGITRAFFIC_HEADERS, MERI

    async def vessels():
        if not config.DIGITRAFFIC_ENABLED:
            await health("digitraffic", configured=False, state="disabled")
            return 0
        try:
            async with httpx.AsyncClient(timeout=25, headers=DIGITRAFFIC_HEADERS) as c:
                r = await c.get(f"{MERI}/api/ais/v1/locations")
                r.raise_for_status()
                # Several MB of GeoJSON: parse off the event loop so requests keep being served.
                rows = await asyncio.to_thread(lambda: digitraffic_rows(r.json())[:500])
            await store_vessels(rows)
            await health("digitraffic", configured=True, state="ok", observations=len(rows),
                         coverage="Finnish waterways", last_observation_at=rows[0]["at"] if rows else None)
            return len(rows)
        except Exception as exc:
            await health("digitraffic", configured=True, state="unavailable", error=type(exc).__name__)
            return 0

    async def flights():
        # adsb.lol allows 500 requests per IP per day (x-ratelimit-limit "500;w=86400"); a 60 s poll
        # exhausts that on a shared host egress IP. Poll the area at most every FLIGHT_AREA_INTERVAL
        # and honour the provider's Retry-After / reset before trying again.
        state = await db.raw.visibility_feeds.find_one({"provider": "adsb.lol.area"}, NO_ID) or {}
        due = state.get("next_attempt_at")
        if due and parse_iso(due) > now():
            return 0
        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Huolitsija/1.0 (marc@heliosone.fi)"}) as c:
                r = await c.get("https://api.adsb.lol/v2/point/60.3/24.9/250")
                r.raise_for_status()
                rows = adsb_rows(r.json(), "adsb.lol")[:500]
            for row in rows:
                await store_flight(row)
            wait = FLIGHT_AREA_INTERVAL
            if (r.headers.get("x-ratelimit-remaining") or "").strip() == "0":
                wait = max(wait, rate_limit_wait(r) or 0)
            await health("adsb.lol.area", configured=True, state="ok", observations=len(rows), failures=0,
                         next_attempt_at=iso(now() + timedelta(seconds=wait)),
                         coverage="250nm around Helsinki", last_observation_at=max((row["at"] for row in rows), default=None))
            return len(rows)
        except Exception as exc:
            failures = int(state.get("failures") or 0) + 1
            wait = None
            if isinstance(exc, httpx.HTTPStatusError):
                wait = rate_limit_wait(exc.response)
            wait = min(max(wait or FLIGHT_AREA_INTERVAL * 2 ** min(failures - 1, 4), FLIGHT_AREA_INTERVAL), 86400)
            await health("adsb.lol.area", configured=True, state="unavailable", failures=failures,
                         next_attempt_at=iso(now() + timedelta(seconds=wait)), **flight_failure_fields(exc))
            return 0

    v, f = await asyncio.gather(vessels(), flights())
    return {"vessels": v, "flights": f}


@job("visibility.ais", timeout=80, label_fi="Maailman AIS", label_en="Global AIS")
@schedule("visibility.ais", job="visibility.ais", every_seconds=65)
async def ais_poll(j):
    """Bounded stream session under the durable job lease. No detached unmanaged websocket."""
    key = os.getenv("AISSTREAM_API_KEY", "").strip()
    if not key:
        await health("aisstream", configured=False, state="not_configured")
        return {"state": "not_configured"}
    from websockets.asyncio.client import connect
    # Only subscribed shipment vessels (max 200 per protocol); rotate batches on each minute.
    ids = sorted({str((lg.get("vessel") or {}).get("mmsi"))
                  async for s in db.raw.shipments.find({"status": {"$nin": ["cancelled", "delivered"]}}, {"legs": 1})
                  for lg in s.get("legs", []) if (lg.get("vessel") or {}).get("mmsi")})
    if not ids:
        await health("aisstream", configured=True, state="no_subscriptions")
        return {"state": "no_subscriptions"}
    batches = [ids[i:i+200] for i in range(0, len(ids), 200)]
    batch = batches[int(now().timestamp() // 65) % len(batches)]
    count = 0
    try:
        async with connect("wss://stream.aisstream.io/v0/stream", open_timeout=10, compression="deflate", max_size=1048576) as ws:
            await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": [[[-90, -180], [90, 180]]],
                                      "FiltersShipMMSI": batch, "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"]}))
            await health("aisstream", configured=True, state="connected", subscribed=len(batch))
            deadline = asyncio.get_running_loop().time() + 50
            while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
                if msg.get("error") or msg.get("Error"):
                    raise ValueError("provider_rejected_subscription")
                row = ais_message(msg)
                if row and await store_vessel(row):
                    count += 1
                    await health("aisstream", last_observation_at=row["at"], state="receiving")
        await health("aisstream", state="between_sessions", messages=count)
    except Exception as exc:
        # Never serialize exception URLs or a subscription containing the credential.
        await health("aisstream", configured=True, state="unavailable", error=type(exc).__name__)
        raise RuntimeError("AISStream unavailable; see feed status") from None
    return {"observations": count}


def adsb_rows(data, provider):
    timestamp = number(data.get("now"))
    if timestamp is None:
        return []
    if timestamp > 1e12:
        timestamp /= 1000
    out = []
    for p in data.get("ac", data.get("aircraft", [])):
        seen = number(p.get("seen_pos"))
        if seen is None or not 0 <= seen <= 120 or not coordinates(p.get("lat"), p.get("lon")):
            continue
        ident = str(p.get("hex") or "").lower()
        if not ident or ident.startswith("~"):
            continue
        at = datetime.fromtimestamp(timestamp - seen, timezone.utc)
        if not -30 <= (now() - at).total_seconds() <= 180:
            continue
        out.append({"icao24": ident, "callsign": (p.get("flight") or "").strip(), "lat": p["lat"], "lon": p["lon"],
                    "gs_kn": number(p.get("gs")), "alt_ft": number(p.get("alt_baro")), "track": number(p.get("track")),
                    "on_ground": p.get("alt_baro") == "ground", "at": iso(at), "source": provider})
    return out


async def fetch_flights(callsign):
    import re
    if not re.fullmatch(r"[A-Z0-9]{2,10}", callsign):
        return []
    for provider, base in (("adsb.lol", "https://api.adsb.lol/v2"), ("adsb.fi", "https://opendata.adsb.fi/api/v2")):
        try:
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Huolitsija/1.0 (marc@heliosone.fi)"}) as c:
                r = await c.get(f"{base}/callsign/{callsign}")
                r.raise_for_status()
                rows = adsb_rows(r.json(), provider)
            await health(provider, configured=True, state="ok", observations=len(rows))
            rows = [p for p in rows if p["callsign"] == callsign]
            for p in rows:
                await store_flight(p)
            if rows:
                return rows
        except Exception as exc:
            await health(provider, configured=True, state="unavailable", error=type(exc).__name__)
    return []


async def flight_fix(leg):
    flight = leg.get("flight") or {}
    callsign = str(flight.get("callsign") or "").strip().upper()
    # Callsigns are reused daily. Require a dated flight, not just a flight number.
    if not callsign or flight.get("date") != now().date().isoformat():
        return None
    query = {"callsign": callsign}
    if flight.get("icao24"):
        query["icao24"] = str(flight["icao24"]).lower()
    row = await db.raw.flight_positions.find_one(query, NO_ID, sort=[("at", -1)])
    if row and 0 <= (now() - parse_iso(row["at"])).total_seconds() < 120:
        return row
    rows = await fetch_flights(callsign)
    return next((p for p in rows if not flight.get("icao24") or p["icao24"] == str(flight["icao24"]).lower()), None)
