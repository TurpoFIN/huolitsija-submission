"""Live positions and schedule progression (background loop, POSITION_TICK_SECONDS).

Per active leg, in this order of trust:
  1. AIS from Fintraffic Digitraffic (vessel with MMSI inside the Finnish/Baltic AIS coverage) -> source "ais"
  2. Finnish rail GPS from rata.digitraffic.fi train-locations (leg.train.number)               -> source "gps"
  3. A partner's own position report on the leg (partner portal, less than 2 h old)             -> source "gps"
  4. Interpolation along the real lane by the planned/actual departure and ETA                  -> source "eta_interpolation"
Elapsed planned times never achieve milestones. Only observed evidence advances cargo. Customs release, out-for-delivery and delivery
always wait for a person, a partner or a carrier feed. Vessel legs with AIS wait for AIS evidence instead of the clock,
and Digitraffic port calls supply the agent-reported ETA/ATA for vessels calling Finnish ports.

Every change is published to SSE subscribers (routes/map.py /api/map/stream).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta

import httpx

from core import config
from core.db import db
from core.lifecycle import background_loop
from core.util import NO_ID, iso, now, now_iso, parse_iso
from services import geo

log = logging.getLogger("positions")
DIGITRAFFIC_HEADERS = {"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"}
MERI = "https://meri.digitraffic.fi"
RATA = "https://rata.digitraffic.fi/api/v1"
AIS_FRESH_S = 15 * 60
GPS_FRESH_S = 15 * 60
PARTNER_FRESH_S = 2 * 3600
RECENT_DELIVERED_DAYS = 14

# --------------------------------------------------------------------------------------------- pub/sub (SSE)

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue):
    _subscribers.discard(q)


def publish(event: dict):
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscriber_count() -> int:
    return len(_subscribers)


# --------------------------------------------------------------------------------------------- external feeds

_cache: dict[str, tuple[float, object]] = {}
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15, headers=DIGITRAFFIC_HEADERS)
    return _client


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = await fn()
    except Exception as exc:  # noqa: BLE001 - feeds are best effort; interpolation is the fallback
        log.info("feed %s failed: %s", key, exc)
        val = None
    _cache[key] = (time.monotonic(), val)
    return val


async def ais_location(mmsi) -> dict | None:
    """Latest AIS position from Digitraffic: {lon, lat, sog, cog, heading, at, age_s} or None."""
    if not config.DIGITRAFFIC_ENABLED or not mmsi or str(mmsi) in ("0", "None"):
        return None

    async def fetch():
        from services.visibility.feeds import digitraffic_rows, store_vessel
        r = await _http().get(f"{MERI}/api/ais/v1/locations", params={"mmsi": str(mmsi)})
        r.raise_for_status()
        rows = [row for row in digitraffic_rows(r.json()) if row["mmsi"] == str(mmsi)]
        if not rows:
            return None
        row = rows[0]
        await store_vessel(row)
        return {**row, "ts": parse_iso(row["at"]).timestamp()}

    v = await _cached(f"ais:{mmsi}", 50, fetch)
    if not v:
        return None
    return {**v, "age_s": time.time() - v["ts"] if v.get("ts") else None}


async def vessel_metadata(mmsi) -> dict | None:
    async def fetch():
        r = await _http().get(f"{MERI}/api/ais/v1/vessels/{mmsi}")
        if r.status_code != 200:
            return None
        return r.json()

    return await _cached(f"vessel:{mmsi}", 3600, fetch)


async def port_calls(locode: str) -> list[dict]:
    async def fetch():
        r = await _http().get(f"{MERI}/api/port-call/v1/port-calls", params={"locode": locode}, timeout=40)
        r.raise_for_status()
        return r.json().get("portCalls") or []

    return await _cached(f"portcalls:{locode}", 600, fetch) or []


async def port_call_for(locode: str, mmsi=None, imo=None, near: str | None = None) -> dict | None:
    """The vessel's call at a Finnish port closest to `near` (the leg's planned ETA): {eta, ata, atd, port_area, ...}.
    Ro-ro vessels call the same port every few days, so the call must match the leg, not just the vessel."""
    if not locode or not locode.startswith("FI"):
        return None
    target = parse_iso(near) if near else now()
    best, best_d = None, None
    for pc in await port_calls(locode):
        if not ((mmsi and str(pc.get("mmsi")) == str(mmsi)) or (imo and str(pc.get("imoLloyds")) == str(imo))):
            continue
        for a in pc.get("portAreaDetails") or []:
            cand = {"eta": a.get("eta"), "ata": a.get("ata"), "atd": a.get("atd"), "port_area": a.get("portAreaName"),
                    "berth": a.get("berthName"), "port_call_id": pc.get("portCallId"), "prev_port": pc.get("prevPort"),
                    "eta_source": a.get("etaSource"), "updated": pc.get("portCallTimestamp")}
            t = parse_iso(cand["ata"] or cand["eta"])
            if not t:
                continue
            d = abs((t - target).total_seconds())
            if d > 2.5 * 86400:
                continue
            if best is None or d < best_d:
                best, best_d = cand, d
    return best


async def train_location(number, departure_date: str | None = None) -> dict | None:
    if not config.DIGITRAFFIC_ENABLED or not number:
        return None

    async def fetch():
        r = await _http().get(f"{RATA}/train-locations/latest/{number}")
        r.raise_for_status()
        rows = r.json() or []
        rows = [x for x in rows if not departure_date or x.get("departureDate") == departure_date]
        if not rows:
            return None
        x = rows[0]
        lon, lat = x["location"]["coordinates"][:2]
        ts = parse_iso(x["timestamp"])
        return {"lon": lon, "lat": lat, "speed_kmh": x.get("speed"), "at": x["timestamp"], "ts": ts.timestamp() if ts else 0,
                "departure_date": x.get("departureDate")}

    v = await _cached(f"train:{number}:{departure_date}", 20, fetch)
    if not v:
        return None
    return {**v, "age_s": time.time() - v["ts"]}


def _dt(ts: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc)


# --------------------------------------------------------------------------------------------- position model

def _name_key(name) -> str:
    return re.sub(r"[^0-9a-z]", "", str(name or "").casefold())


def _imo_key(imo) -> str:
    d = re.sub(r"\D", "", str(imo or ""))
    return "" if d in ("", "0") else d


async def vessel_identity(booked: dict, fix: dict) -> dict:
    """Does the AIS source agree with the booked vessel for this MMSI? Name and IMO as the provider reports them
    (VesselAPI carries the name on the fix; Digitraffic from its vessel metadata). A disagreement is shown on the
    shipment, never silently resolved: the position stays, the flag says whose identity is in doubt."""
    provider = fix.get("source")
    reported_name, reported_imo = fix.get("name"), fix.get("imo")
    if provider == "digitraffic" and not (reported_name or reported_imo):
        try:
            meta = await vessel_metadata(booked.get("mmsi")) or {}
        except Exception:  # noqa: BLE001 - metadata is advisory; the fix itself is still valid
            meta = {}
        reported_name, reported_imo = meta.get("name"), meta.get("imo")
    fields = []
    if _name_key(booked.get("name")) and _name_key(reported_name) and _name_key(booked.get("name")) != _name_key(reported_name):
        fields.append("name")
    if _imo_key(booked.get("imo")) and _imo_key(reported_imo) and _imo_key(booked.get("imo")) != _imo_key(reported_imo):
        fields.append("imo")
    known = bool(_name_key(reported_name) or _imo_key(reported_imo))
    return {"state": "disagree" if fields else "agree" if known else "unknown", "fields": fields,
            "mmsi": str(booked.get("mmsi") or ""), "provider": provider,
            "booked": {"name": booked.get("name"), "imo": _imo_key(booked.get("imo")) or None},
            "reported": {"name": (str(reported_name).strip() or None) if reported_name else None,
                         "imo": _imo_key(reported_imo) or None}}


async def live_fix(leg: dict) -> dict | None:
    """Best live position for a leg in progress, or None (then the caller interpolates)."""
    v = leg.get("vessel") or {}
    if leg["mode"] == "sea" and v.get("mmsi"):
        from services.visibility.feeds import latest_vessel
        from services.visibility.feeds import vesselapi_location, VESSELAPI_FRESH_S
        a = await latest_vessel(v["mmsi"]) or await ais_location(v["mmsi"])
        fresh_s = AIS_FRESH_S
        if not (a and a.get("age_s") is not None and 0 <= a["age_s"] < AIS_FRESH_S):
            # Outside Digitraffic coverage: worldwide provider fix, shown with its own (older) source time.
            a, fresh_s = await vesselapi_location(v["mmsi"]), VESSELAPI_FRESH_S
        if a and a.get("age_s") is not None and 0 <= a["age_s"] < fresh_s:
            return {"lon": a["lon"], "lat": a["lat"], "source": "ais", "provider": a.get("source"), "at": a["at"], "speed_kn": a.get("sog"),
                    "heading": a.get("heading") if a.get("heading") not in (None, 511) else a.get("cog"),
                    "identity": await vessel_identity(v, a)}
    if leg["mode"] == "air":
        from services.visibility.feeds import flight_fix
        f = await flight_fix(leg)
        if f:
            return {"lon": f["lon"], "lat": f["lat"], "source": "adsb", "provider": f["source"],
                    "at": f["at"], "speed_kn": f.get("gs_kn"), "heading": f.get("track"),
                    "callsign": f.get("callsign"), "on_ground": f.get("on_ground")}
    t = leg.get("train") or {}
    if leg["mode"] == "rail" and isinstance(t, dict) and t.get("number"):
        g = await train_location(t["number"], t.get("departure_date"))
        if g and g["age_s"] < GPS_FRESH_S:
            return {"lon": g["lon"], "lat": g["lat"], "source": "gps", "at": g["at"], "speed_kmh": g.get("speed_kmh")}
    rep = leg.get("last_report")
    if rep and parse_iso(rep.get("at")) and (now() - parse_iso(rep["at"])).total_seconds() < PARTNER_FRESH_S:
        return {"lon": rep["lon"], "lat": rep["lat"], "source": "gps", "at": rep["at"]}
    return None


def _source_for_milestone(src: str | None) -> str:
    return {"ais": "ais", "gps": "gps", "partner": "gps", "carrier_api": "carrier_api", "port_call": "ais"}.get(src or "", "eta_interpolation")


async def compute_position(s: dict) -> dict:
    """{position, progress, current_leg} for a shipment (no writes)."""
    legs = s.get("legs") or []
    total = sum(lg.get("distance_km") or 0 for lg in legs) or 1.0
    if s["status"] == "delivered" or (legs and all(lg.get("ata") for lg in legs) and s["status"] == "delivered"):
        d = s["destination"]
        pos = dict(s.get("position") or {})
        pos.update(lat=d["lat"], lon=d["lon"], leg_seq=legs[-1]["seq"] if legs else None)
        pos.setdefault("source", "eta_interpolation")
        pos.setdefault("updated_at", s.get("delivered_at") or now_iso())
        return {"position": pos, "progress": 1.0, "current_leg": None}
    done_km = 0.0
    cur = None
    for lg in legs:
        if lg.get("ata"):
            done_km += lg.get("distance_km") or 0
            continue
        if lg.get("atd"):
            cur = lg
        break
    if cur is None:
        # stationary at the end of the last completed leg (or at origin)
        last_done = next((lg for lg in reversed(legs) if lg.get("ata")), None)
        at_loc = last_done["to"] if last_done else s["origin"]
        last_ms = next((m for m in reversed(s.get("milestones") or []) if m.get("at")), None)
        src = _source_for_milestone((last_ms or {}).get("source"))
        at = (last_ms or {}).get("at")
        if src == "eta_interpolation" or not at or (now() - parse_iso(at)).total_seconds() > 86400:
            # no live fix in the last 24 h: the position is our estimate from the milestones, computed now
            src, at = "eta_interpolation", now_iso()
        pos = {"lat": at_loc["lat"], "lon": at_loc["lon"], "updated_at": at, "source": src,
               "leg_seq": (last_done or (legs[0] if legs else {})).get("seq"), "moving": False}
        return {"position": pos, "progress": round(min(0.999999 if s["status"] != "delivered" else 1.0, done_km / total), 6), "current_leg": None}
    fix = None if s["status"] in ("exception",) and not (cur.get("vessel") or {}).get("mmsi") else await live_fix(cur)
    path = cur.get("path") or [[cur["from"]["lon"], cur["from"]["lat"]], [cur["to"]["lon"], cur["to"]["lat"]]]
    if fix:
        frac, off_km = geo.project_fraction(path, fix["lon"], fix["lat"])
        pos = {"lat": round(fix["lat"], 5), "lon": round(fix["lon"], 5), "updated_at": fix["at"] or now_iso(), "source": fix["source"],
               "leg_seq": cur["seq"], "moving": True, "off_lane_km": off_km}
        for k in ("speed_kn", "speed_kmh", "heading", "provider", "identity"):
            if fix.get(k) is not None:
                pos[k] = fix[k]
    else:
        start = parse_iso(cur.get("atd") or cur.get("etd"))
        end = parse_iso(cur.get("eta"))
        if s["status"] == "exception" or not start or not end or end <= start:
            frac = 0.0 if s["status"] != "exception" else _last_fraction(s, cur)
        else:
            frac = (now() - start).total_seconds() / (end - start).total_seconds()
            frac = max(0.0, min(0.985, frac))  # never claim arrival from the clock alone
        p = geo.interpolate(path, frac)
        pos = {"lat": p["lat"], "lon": p["lon"], "updated_at": now_iso(), "source": "eta_interpolation", "leg_seq": cur["seq"],
               "moving": s["status"] not in ("exception", "customs_hold"), "heading": p.get("bearing")}
    progress = (done_km + frac * (cur.get("distance_km") or 0)) / total
    # 6 decimals: on a 25,000 km route 1e-4 would already be 2.5 km; never 1.0 before delivery
    return {"position": pos, "progress": round(max(0.0, min(0.999999, progress)), 6), "current_leg": cur["seq"]}


def _last_fraction(s: dict, cur: dict) -> float:
    p = s.get("position") or {}
    if p.get("leg_seq") == cur["seq"] and p.get("lat") is not None:
        return geo.project_fraction(cur.get("path") or [], p["lon"], p["lat"])[0]
    return 0.0


def _position_changed(a: dict | None, b: dict) -> bool:
    if not a:
        return True
    # A stationary asset still broadcasts fresh evidence. Persist its timestamp
    # and telemetry even when its coordinates do not change. Interpolated clock
    # ticks are not new provider observations.
    if b.get("source") in ("ais", "adsb", "gps") and any(
        a.get(k) != b.get(k) for k in ("updated_at", "speed_kn", "speed_kmh", "heading", "identity")
    ):
        return True
    return (round(a.get("lat") or 0, 4), round(a.get("lon") or 0, 4), a.get("source")) != \
        (round(b.get("lat") or 0, 4), round(b.get("lon") or 0, 4), b.get("source"))


def event_payload(s: dict) -> dict:
    p = s.get("position") or {}
    return {"id": s["id"], "position": {"lat": p.get("lat"), "lon": p.get("lon"), "updated_at": p.get("updated_at"), "source": p.get("source")},
            "progress": s.get("progress"), "status": s["status"]}


async def refresh_shipment(shipment_id: str, *, publish_always: bool = True) -> dict | None:
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s or s["status"] == "cancelled":
        return s
    r = await compute_position(s)
    if await _splice_lane(s, r):
        r = await compute_position(s)
    changed = _position_changed(s.get("position"), r["position"]) or abs((s.get("progress") or 0) - r["progress"]) > 0.0005
    if changed:
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"position": r["position"], "progress": r["progress"],
                                                                 "current_leg": r["current_leg"]}})
        s.update(position=r["position"], progress=r["progress"], current_leg=r["current_leg"])
    if changed or publish_always:
        publish({**event_payload(s), "company_id": s["company_id"]})
    return s


SPLICE_OFF_KM = 2.0
SPLICE_EVERY_S = 900


async def _splice_lane(s: dict, r: dict) -> bool:
    """A vessel seen by AIS away from the modelled lane: re-route the leg through the vessel's real position (sea lanes
    from the departure port to the ship, and from the ship to the arrival port), so the map line passes through the ship.
    At most every 15 minutes per leg."""
    pos = r.get("position") or {}
    if pos.get("source") != "ais" or (pos.get("off_lane_km") or 0) <= SPLICE_OFF_KM or not r.get("current_leg"):
        return False
    leg = next((lg for lg in s["legs"] if lg["seq"] == r["current_leg"]), None)
    if not leg or leg["mode"] != "sea":
        return False
    last = parse_iso(leg.get("path_spliced_at"))
    if last and (now() - last).total_seconds() < SPLICE_EVERY_S:
        return False
    here = [pos["lon"], pos["lat"]]
    try:
        path = await asyncio.to_thread(geo.sea_path_through, [leg["from"]["lon"], leg["from"]["lat"]], here, [leg["to"]["lon"], leg["to"]["lat"]])
    except Exception as exc:  # noqa: BLE001
        log.warning("lane splice failed for %s leg %s: %s", s.get("reference"), leg["seq"], exc)
        return False
    leg.update(path=path, distance_km=geo.path_length_km(path), path_source="searoute", path_spliced_at=now_iso(),
               path_note="Lane re-routed through the vessel's AIS position", path_version=geo.PATH_VERSION)
    total = round(sum(lg.get("distance_km") or 0 for lg in s["legs"]), 1)
    await db.shipments.update_one({"id": s["id"]}, {"$set": {"legs": s["legs"], "distance_km": total}})
    s["distance_km"] = total
    return True


# --------------------------------------------------------------------------------------------- schedule + evidence

async def _advance(s: dict) -> bool:
    """Mark due milestones achieved from evidence (AIS, port calls, rail GPS) only. True if anything changed."""
    from services import shipments as svc

    if s["status"] in ("exception", "customs_hold", "cancelled", "delivered"):
        return False
    if any(h["type"] == "sanctions" for h in s.get("holds") or []):
        return False
    changed = False
    for _ in range(8):  # at most a few steps per tick
        s = await db.shipments.find_one({"id": s["id"]}, NO_ID)
        if s["status"] in ("exception", "customs_hold", "cancelled", "delivered"):
            break
        nxt = next((m for m in s["milestones"] if not m.get("at")), None)
        if not nxt or nxt["code"] not in svc.SCHEDULE_ACHIEVABLE:
            break
        leg = next((lg for lg in s["legs"] if lg["seq"] == nxt.get("leg_seq")), None)
        evidence = await _evidence(leg, nxt) if leg else None
        planned = parse_iso(nxt.get("planned"))
        if evidence == "wait":
            break
        if evidence:
            src, at, loc, *why = evidence
        else:
            break
        try:
            await svc.record_milestone(s["id"], nxt["code"], actor=None, source=src, at=at, leg_seq=nxt.get("leg_seq"),
                                       location=loc or nxt.get("location"), note=why[0] if why else None)
            changed = True
        except Exception as exc:  # noqa: BLE001
            log.info("advance %s %s: %s", s.get("reference"), nxt["code"], getattr(exc, "detail", exc))
            break
    return changed


def _ais_note(a: dict, km: float, place_: dict) -> str:
    """Provenance of an AIS-proven milestone: feed, MMSI, fix time and position, distance to the port."""
    return (f"AIS {a.get('source') or 'digitraffic'} MMSI {a.get('mmsi')}: fix {a['at'][:16]}Z at {a['lat']:.4f}, {a['lon']:.4f}, "
            f"{km:.1f} km from {place_.get('unlocode') or place_.get('name')}")


async def _evidence(leg: dict, ms: dict):
    """(source, at, location) when a live feed proves the milestone, "wait" when a live feed exists but does not prove it
    yet (so the clock must not claim it), or None (no live feed: schedule decides)."""
    v = leg.get("vessel") or {}
    if leg["mode"] == "sea" and (v.get("mmsi") or v.get("imo")):
        to = leg["to"]
        if ms["code"] in ("arrived", "transshipment") and (to.get("unlocode") or "").startswith("FI"):
            pc = await port_call_for(to["unlocode"], v.get("mmsi"), v.get("imo"), near=leg.get("eta"))
            if pc and pc.get("ata"):
                return "port_call", iso(parse_iso(pc["ata"])), to, \
                    f"Digitraffic port call {pc.get('port_call_id')} ATA ({pc.get('port_area') or to.get('unlocode')})"
        a = await ais_location(v.get("mmsi")) if v.get("mmsi") else None
        if a and a.get("age_s") is not None and 0 <= a["age_s"] < AIS_FRESH_S:
            here = [a["lon"], a["lat"]]
            if ms["code"] == "departed":
                planned = parse_iso(ms.get("planned"))
                if planned and planned > now() + timedelta(hours=6):
                    return "wait"  # the vessel may still be on its previous voyage
                km = geo.haversine_km(here, [leg["from"]["lon"], leg["from"]["lat"]])
                if km > 8:
                    return "ais", a["at"], None, _ais_note(a, km, leg["from"])
                return "wait"
            if ms["code"] in ("arrived", "transshipment"):
                km = geo.haversine_km(here, [to["lon"], to["lat"]])
                if km < 4 and (a.get("sog") or 0) < 1.5:
                    return "ais", a["at"], to, _ais_note(a, km, to)
                return "wait"
        return None
    t = leg.get("train") or {}
    if leg["mode"] == "rail" and isinstance(t, dict) and t.get("number"):
        g = await train_location(t["number"], t.get("departure_date"))
        if g and g["age_s"] < GPS_FRESH_S:
            here = [g["lon"], g["lat"]]
            if ms["code"] == "departed":
                return ("gps", g["at"], None) if geo.haversine_km(here, [leg["from"]["lon"], leg["from"]["lat"]]) > 3 else "wait"
            if ms["code"] in ("arrived", "transshipment"):
                close = geo.haversine_km(here, [leg["to"]["lon"], leg["to"]["lat"]]) < 3 and (g.get("speed_kmh") or 0) < 3
                return ("gps", g["at"], leg["to"]) if close else "wait"
    return None


async def _sync_port_call_eta(s: dict):
    """Vessel legs into Finnish ports: take the agent-reported ETA from Digitraffic port calls."""
    from services import shipments as svc

    for lg in s.get("legs") or []:
        v = lg.get("vessel") or {}
        if lg["mode"] != "sea" or lg.get("ata") or not (v.get("mmsi") or v.get("imo")):
            continue
        to = lg["to"].get("unlocode") or ""
        if not to.startswith("FI"):
            continue
        pc = await port_call_for(to, v.get("mmsi"), v.get("imo"), near=lg.get("eta"))
        if not pc or not pc.get("eta") or pc.get("ata"):
            continue
        new_eta = iso(parse_iso(pc["eta"]))
        lg["port_call"] = {k: pc.get(k) for k in ("port_call_id", "port_area", "berth", "eta_source", "updated")}
        if new_eta != lg.get("eta") and abs((parse_iso(new_eta) - parse_iso(lg["eta"])).total_seconds()) > 1800:
            # the call's provenance first, so the ETA change below is logged against it
            await db.shipments.update_one({"id": s["id"], "legs.seq": lg["seq"]}, {"$set": {"legs.$.port_call": lg["port_call"]}})
            await svc.update_leg(s["id"], lg["seq"], {"eta": new_eta}, actor={"type": "system", "id": "digitraffic",
                                                                            "name": "Digitraffic port call"})
        else:
            await db.shipments.update_one({"id": s["id"], "legs.seq": lg["seq"]}, {"$set": {"legs.$.port_call": lg["port_call"]}})


_last_slow = 0.0


@background_loop(seconds=config.POSITION_TICK_SECONDS)
async def tick():
    global _last_slow
    slow = time.monotonic() - _last_slow > 600  # port-call ETAs and carrier APIs every 10 min
    if slow:
        _last_slow = time.monotonic()
    q = {"status": {"$in": ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception"]}}
    async for s in db.shipments.find(q, NO_ID):
        try:
            if slow:
                await _sync_port_call_eta(s)
            await _advance(s)
            await refresh_shipment(s["id"], publish_always=False)
        except Exception:  # noqa: BLE001
            log.exception("position tick failed for %s", s.get("reference"))
    # Carrier ingestion is scheduled through core.jobs (visibility.carriers).
