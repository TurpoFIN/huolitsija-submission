"""Shipments: leg planning, milestones, state machine, holds.

Cross-slice contract (ARCHITECTURE.md):
    create_shipment(*, company_id, quote, option, booking, actor) -> shipment
    set_status(shipment_id, status, *, actor, note=None, milestone=None, location=None, source="manual") -> shipment
    add_hold(shipment_id, hold_type, reason, actor) / release_hold(shipment_id, hold_type, actor)

Domain rules implemented here (shared/s3/domain-spec.md):
- 13.2 shipment state machine; BOOKED requires kyc_status=approved and sanctions_status=clear (409 otherwise).
- RULE X-3 liability regime by mode/leg; RULE N-1 forwarder_role; RULE N-4 lien blocks delivery; RULE K-1 a
  sanctions hold blocks every movement until compliance clears it.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

from fastapi import HTTPException

from core import config
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.lifecycle import on_startup
from core.events import emit
from core.notify import bi, notify_company
from core.refs import next_ref
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import geo

log = logging.getLogger("shipments")

register_index("shipments", "id", unique=True)
register_index("shipments", "reference", unique=True)
register_index("shipments", [("company_id", 1), ("created_at", -1)])
register_index("shipments", [("status", 1), ("eta", 1)])
register_index("shipments", "legs.carrier_id")
register_index("shipments", [("quote_id", 1), ("option_id", 1)])
register_index("tracking_events", [("shipment_id", 1), ("at", -1)])
register_index("tracking_events", "id", unique=True)

STATUSES = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception", "cancelled"]
ACTIVE = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception"]
TRANSITIONS: dict[str, set[str]] = {
    "booked": {"picked_up", "in_transit", "at_hub", "customs_hold", "exception", "cancelled"},
    "picked_up": {"in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception", "cancelled"},
    "in_transit": {"at_hub", "customs_hold", "out_for_delivery", "delivered", "exception"},
    "at_hub": {"in_transit", "customs_hold", "out_for_delivery", "delivered", "exception"},
    "customs_hold": {"booked", "picked_up", "at_hub", "in_transit", "out_for_delivery", "exception"},
    "out_for_delivery": {"delivered", "at_hub", "exception"},
    "exception": {"booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}
STATUS_FOR_MILESTONE = {
    "booked": "booked", "picked_up": "picked_up", "received": "picked_up", "departed": "in_transit",
    "transshipment": "at_hub", "arrived": "at_hub", "customs_hold": "customs_hold", "out_for_delivery": "out_for_delivery",
    "delivered": "delivered", "exception": "exception", "cancelled": "cancelled",
}
DEFAULT_MILESTONE = {
    "booked": "booked", "picked_up": "picked_up", "in_transit": "departed", "at_hub": "arrived", "customs_hold": "customs_hold",
    "out_for_delivery": "out_for_delivery", "delivered": "delivered", "exception": "exception", "cancelled": "cancelled",
}
MILESTONE_LABELS = {
    "booked": "Booking confirmed", "picked_up": "Picked up", "received": "Received at terminal", "export_cleared": "Export cleared",
    "departed": "Departed", "transshipment": "At transshipment hub", "arrived": "Arrived", "customs_released": "Customs released",
    "customs_hold": "Customs hold", "out_for_delivery": "Out for delivery", "delivered": "Delivered", "exception": "Exception",
    "cancelled": "Cancelled", "hold_added": "Hold added", "hold_released": "Hold released", "position": "Position report",
    "leg_accepted": "Leg accepted by carrier",
    "eta_changed": "ETA changed", "note": "Note",
}
# Milestones the schedule may mark achieved when their planned time passes (no carrier feed yet). Customs release,
# out-for-delivery and delivery always need evidence from a person, a partner or a carrier feed.
SCHEDULE_ACHIEVABLE = {"picked_up", "received", "departed", "transshipment", "arrived"}
HOLD_TYPES = {"customs", "inspection", "documents", "sanctions", "lien", "payment", "other"}
CUSTOMS_HOLD_TYPES = {"customs", "inspection", "documents"}
DELIVERY_BLOCKING = {"customs", "inspection", "documents", "sanctions", "lien", "payment"}

# RULE X-3 + domain spec 7 (limits as published; SDR converted at the daily IMF/ECB rate in the UI layer).
LIABILITY = {
    "CMR": {"label": "CMR (international road)", "limit": "8.33 SDR/kg gross", "sdr_per_kg": 8.33},
    "MONTREAL": {"label": "Montreal Convention 1999 (air)", "limit": "26 SDR/kg", "sdr_per_kg": 26},  # ICAO revision, in force 2024-12-28
    "HAGUE_VISBY": {"label": "Finnish Maritime Code 30 § / Hague-Visby (sea)", "limit": "667 SDR per package or 2 SDR/kg, whichever is higher",
                    "sdr_per_kg": 2, "sdr_per_package": 667},
    "CIM": {"label": "CIM / COTIF (rail)", "limit": "17 SDR/kg", "sdr_per_kg": 17},
    "NSAB": {"label": "NSAB 2015 (forwarder)", "limit": "8.33 SDR/kg (principal); SDR 50,000 per assignment (intermediary)", "sdr_per_kg": 8.33},
}
EU_EEA = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS LI".split())
CONTINENT_RORO = set("DE NL BE FR IT AT CH DK LU CZ ES PT SI HU SK".split())  # road to Finland = trailer on ro-ro
SEA_HUB_BY_CARRIER = [("maersk", "DEBRV"), ("msc", "DEBRV"), ("hapag", "DEHAM"), ("cma", "NLRTM"), ("cosco", "NLRTM"),
                      ("oocl", "NLRTM"), ("evergreen", "NLRTM"), ("one", "NLRTM"), ("hmm", "NLRTM"), ("zim", "NLRTM"),
                      ("yang ming", "NLRTM")]
DEFAULT_HUB = "DEHAM"


# --------------------------------------------------------------------------------------------- helpers

def _loc(d: dict | None, address: dict | None = None) -> dict | None:
    if not d:
        return None
    ref = geo.location_ref(d)
    if address:
        ref["address"] = address
    return ref


def _hours(td: timedelta) -> float:
    return td.total_seconds() / 3600


def liability_for(mode: str, legs: list[dict], forwarder_role: str = "principal") -> str:
    main_modes = [lg["mode"] for lg in legs if lg.get("role") not in ("pickup", "delivery")] or [mode]
    distinct = set(main_modes)
    if len(distinct) > 1:
        return "NSAB"
    m = main_modes[0]
    if m == "air":
        return "MONTREAL"
    if m == "sea":
        return "HAGUE_VISBY"
    if m == "rail":
        return "CIM"
    if m == "road":
        countries = {(lg.get("from") or {}).get("country") for lg in legs} | {(lg.get("to") or {}).get("country") for lg in legs}
        countries.discard(None)
        return "CMR" if len(countries) > 1 else "NSAB"
    return "NSAB"


def leg_regime(leg: dict) -> str:
    m = leg["mode"]
    if m == "road":
        return "CMR" if (leg.get("from") or {}).get("country") != (leg.get("to") or {}).get("country") else "NSAB"
    return {"sea": "HAGUE_VISBY", "air": "MONTREAL", "rail": "CIM"}.get(m, "NSAB")


def infer_direction(o: dict, d: dict) -> str:
    oc, dc = (o or {}).get("country"), (d or {}).get("country")
    if oc == "FI" and dc == "FI":
        return "domestic"
    if dc == "FI":
        return "import"
    if oc == "FI":
        return "export"
    return "cross_trade"


def hub_for(carrier_name: str | None) -> str:
    c = (carrier_name or "").lower()
    for key, hub in SEA_HUB_BY_CARRIER:
        if key in c:
            return hub
    return DEFAULT_HUB


def is_europe(loc: dict | None) -> bool:
    cc = (loc or {}).get("country")
    return cc in EU_EEA or cc in {"GB", "CH", "TR", "UA", "RS", "BA", "ME", "MK", "AL", "MD", "GE"}


def baltic_or_nordic(loc: dict | None) -> bool:
    return (loc or {}).get("country") in {"FI", "SE", "EE", "LV", "LT", "PL", "DK", "NO"}


# --------------------------------------------------------------------------------------------- leg planning

def leg_spec(mode: str, frm, to, role: str = "main", **extra) -> dict:
    return {"mode": mode, "from": frm, "to": to, "role": role, **extra}


async def _resolve(x, address: dict | None = None) -> dict:
    if isinstance(x, dict) and x.get("lat") is not None and x.get("lon") is not None and x.get("name"):
        ref = dict(x)
        if address and not ref.get("address"):
            ref["address"] = address
        return ref
    from services import gazetteer

    await gazetteer.ready()  # cold UN/LOCODE table is built in a thread, never on the event loop (see 0dfd30a)
    d = await geo.find_location(x)
    if not d:
        raise HTTPException(422, f"Unknown location: {x}. Use a UN/LOCODE (e.g. FIHEL) or pick a place from the list.")
    return _loc(d, address)


async def build_legs(specs: list[dict]) -> list[dict]:
    """Resolve locations and compute real lane geometry for each leg (concurrently, in worker threads)."""
    resolved = []
    for s in specs:
        frm = await _resolve(s["from"], s.get("from_address"))
        to = await _resolve(s["to"], s.get("to_address"))
        if s["mode"] == "road":
            frm = await _at_door(frm, s.get("from_address"))
            to = await _at_door(to, s.get("to_address"))
        resolved.append((s, frm, to))
    sem = asyncio.Semaphore(3)

    async def route(s, frm, to):
        async with sem:
            via = [[v["lon"], v["lat"]] for v in [await _resolve(x) for x in s.get("via") or []]]
            return await geo.route_info_async(s["mode"], frm, to, via=via or None)

    infos = await asyncio.gather(*(route(s, f, t) for s, f, t in resolved))
    legs = []
    for i, ((s, frm, to), info) in enumerate(zip(resolved, infos), start=1):
        leg = {
            "seq": i, "mode": s["mode"], "role": s.get("role", "main"), "service": s.get("service"),
            "from": frm, "to": to, "path": info["path"], "path_source": info["source"], "path_note": info["note"],
            "distance_km": info["distance_km"], "corridors": info.get("corridors") or [], "path_version": geo.PATH_VERSION,
            "carrier_id": s.get("carrier_id"), "carrier": s.get("carrier"),
            "vessel": s.get("vessel"), "voyage": s.get("voyage"), "flight": s.get("flight"), "train": s.get("train"),
            "truck_plate": s.get("truck_plate"), "transport_doc_no": s.get("transport_doc_no"),
            "container_numbers": s.get("container_numbers") or [],
            **{key: s[key] for key in ("quote_leg_seq", "ferry_crossing", "booking_group", "booking_parent_seq") if key in s},
            "etd": s.get("etd"), "eta": s.get("eta"), "atd": None, "ata": None, "status": "planned",
            "_natural_h": _natural_hours(s["mode"], s.get("role", "main"), info, s.get("service")),
        }
        if s.get("via"):
            leg["via"] = [await _resolve(x) for x in s["via"]]
        leg["liability_regime"] = leg_regime(leg)
        legs.append(leg)
    return legs


async def _at_door(loc: dict, address: dict | None) -> dict:
    """Move a road leg's end to the geocoded street address (door pickup / delivery)."""
    if not address:
        return loc
    g = await geo.geocode_async(address)
    if not g:
        return {**loc, "address": address}
    return {**loc, "name": address.get("city") or loc.get("name"), "lat": g["lat"], "lon": g["lon"], "address": address,
            "geocoded": "approximate" if g.get("approximate") else "street"}


def _natural_hours(mode: str, role: str, info: dict, service: str | None) -> float:
    dist = info.get("distance_km") or 0
    if mode == "road":
        drive = (info.get("duration_h") or dist / 65) * 1.25
        rests = math.floor(drive / 9) * 11 if drive > 9 else 0  # EU 561/2006: 9 h driving, 11 h daily rest
        return drive + rests + 2
    if mode == "sea":
        svc = (service or "").lower()
        kn = 19 if "ro-ro" in svc or "roro" in svc or "ro-pax" in svc else (15 if role == "main" and dist > 4000 else 14)
        factor = 1.15 if dist > 4000 else 1.05
        return dist / (kn * 1.852) * factor + 6
    if mode == "air":
        return dist / 750 + 4
    if mode == "rail":
        return dist / 40 + 6 if dist < 1500 else dist / 24 + 24
    return dist / 50


def _dwell_hours(prev: dict, nxt: dict) -> float:
    if prev["mode"] == "road" and prev["role"] == "pickup":
        return {"sea": 36, "air": 14, "rail": 18}.get(nxt["mode"], 8)
    if prev["mode"] == "sea" and nxt["mode"] == "sea":
        return 72  # transshipment at hub
    if nxt.get("role") == "delivery":
        return {"sea": 46, "air": 18, "rail": 24}.get(prev["mode"], 6)
    if prev["mode"] == "road" and nxt["mode"] == "sea":
        return 6
    if prev["mode"] == "sea" and nxt["mode"] == "road":
        return 4
    if prev["mode"] == "rail" and nxt["mode"] == "road":
        return 20
    return 12


def schedule_legs(legs: list[dict], etd: datetime, eta: datetime | None = None) -> tuple[datetime, datetime]:
    """Assign planned etd/eta to each leg. When the option promises an ETA the plan is scaled to meet it."""
    natural = [lg.pop("_natural_h", 24) for lg in legs]
    dwell = [_dwell_hours(legs[i - 1], legs[i]) if i else 0 for i in range(len(legs))]
    total = sum(natural) + sum(dwell)
    scale = 1.0
    if eta and total > 0:
        scale = max(0.4, min(3.0, _hours(eta - etd) / total))
    t = etd
    for lg, n, w in zip(legs, natural, dwell):
        t = t + timedelta(hours=w * scale)
        if lg.get("etd") and parse_iso(lg["etd"]):
            t = parse_iso(lg["etd"])
        lg["etd"] = iso(t)
        end = t + timedelta(hours=max(1.0, n * scale))
        if lg.get("eta") and parse_iso(lg["eta"]):
            end = parse_iso(lg["eta"])
        lg["eta"] = iso(end)
        t = end
    return parse_iso(legs[0]["etd"]), parse_iso(legs[-1]["eta"])


async def plan_route_specs(*, mode: str, origin: dict, destination: dict, incoterm: str | None, carrier: str | None,
                           carrier_id: str | None, service: str | None, pickup_address: dict | None,
                           delivery_address: dict | None) -> list[dict]:
    """Realistic leg sequence for a booking (pre-carriage, main/feeder, on-carriage)."""
    inc = (incoterm or "").upper()
    o, d = origin, destination
    o_kinds, d_kinds = set(o.get("kinds") or []), set(d.get("kinds") or [])
    door_pickup = bool(pickup_address) or inc == "EXW"
    door_delivery = bool(delivery_address)
    specs: list[dict] = []

    if mode == "air":
        o_air = o if "airport" in o_kinds else await _nearest_airport(o)
        d_air = d if "airport" in d_kinds else await _nearest_airport(d)
        if o_air["code"] != o["code"] or door_pickup:
            specs.append(leg_spec("road", o, o_air, "pickup", from_address=pickup_address))
        specs.append(leg_spec("air", o_air, d_air, "main", carrier=carrier, carrier_id=carrier_id, service=service))
        if d_air["code"] != d["code"] or door_delivery:
            specs.append(leg_spec("road", d_air, d, "delivery", to_address=delivery_address))
        return specs

    if mode == "road":
        return road_specs(o, d, carrier=carrier, carrier_id=carrier_id, service=service,
                          from_address=pickup_address, to_address=delivery_address)

    if mode == "rail":
        segs = geo.rail_segments(o, d, o, d)
        if segs:
            if door_pickup:
                specs.append(leg_spec("road", o, o, "pickup", from_address=pickup_address))
            specs += _rail_leg_specs(segs, o, d, carrier=carrier, carrier_id=carrier_id, service=service)
            if door_delivery:
                specs.append(leg_spec("road", d, d, "delivery", to_address=delivery_address))
            return specs
        if not is_europe(o) and is_europe(d):  # China–Europe train to Duisburg, then trailer onwards
            segs = geo.rail_segments(o, "DEDUI", o) or [{"mode": "rail", "from": None, "to": None}]
            specs += _rail_leg_specs(segs, o, "DEDUI", carrier=carrier, carrier_id=carrier_id, service=service or "China–Europe block train")
            if d.get("country") == "FI":
                specs += [leg_spec("road", "DEDUI", "DETRV", "onward"),
                          leg_spec("sea", "DETRV", "FIHEL", "onward", carrier="Finnlines", service="Ro-ro trailer (unaccompanied)"),
                          leg_spec("road", "FIHEL", d, "delivery", to_address=delivery_address)]
            else:
                specs.append(leg_spec("road", "DEDUI", d, "delivery", to_address=delivery_address))
            return specs
        specs.append(leg_spec("rail", o, d, "main", carrier=carrier, carrier_id=carrier_id, service=service))
        return specs

    # sea (and multimodal default)
    pol = o if "port" in o_kinds else await _nearest_port(o)
    pod = d if "port" in d_kinds else await _nearest_port(d)
    if pol["code"] != o["code"] or door_pickup:
        specs.append(leg_spec("road", o, pol, "pickup", from_address=pickup_address))
    deep_sea = not is_europe(pol) and baltic_or_nordic(pod)
    deep_sea_export = baltic_or_nordic(pol) and not is_europe(pod)
    if deep_sea:
        hub = hub_for(carrier)
        specs.append(leg_spec("sea", pol, hub, "main", carrier=carrier, carrier_id=carrier_id, service=service or "Deep-sea mainline"))
        specs.append(leg_spec("sea", hub, pod, "feeder", carrier=carrier, carrier_id=carrier_id, service="Feeder"))
    elif deep_sea_export:
        hub = hub_for(carrier)
        specs.append(leg_spec("sea", pol, hub, "feeder", carrier=carrier, carrier_id=carrier_id, service="Feeder"))
        specs.append(leg_spec("sea", hub, pod, "main", carrier=carrier, carrier_id=carrier_id, service=service or "Deep-sea mainline"))
    else:
        specs.append(leg_spec("sea", pol, pod, "main", carrier=carrier, carrier_id=carrier_id, service=service))
    if pod["code"] != d["code"] or door_delivery:
        specs.append(leg_spec("road", pod, d, "delivery", to_address=delivery_address))
    return specs


def _rail_leg_specs(segs: list[dict], o, d, *, carrier=None, carrier_id=None, service=None) -> list[dict]:
    out = []
    for sg in segs:
        frm = sg["from"] or o
        to = sg["to"] or d
        if sg["mode"] == "sea":
            out.append(leg_spec("sea", frm, to, "ferry", carrier=sg.get("carrier"), service=sg.get("service")))
        else:
            out.append(leg_spec("rail", frm, to, "main", carrier=carrier, carrier_id=carrier_id, service=service))
    return out


# Road to and from Finland crosses the Baltic: the trailer goes on a ferry, which is its own (sea) leg.
# (foreign port, Finnish port, operator, service). The pair with the least road distance at both ends wins.
ROAD_FERRIES = {
    "continent": [("DETRV", "FIHEL", "Finnlines", "Ro-ro trailer (unaccompanied) Travemünde–Helsinki")],
    "baltic": [("EEMUG", "FIHEL", "Eckerö Line (Finbo Cargo)", "Ro-pax, accompanied trailer Muuga–Vuosaari")],
    "sweden": [("SEKPS", "FINLI", "Finnlines", "Ro-pax Kapellskär–Naantali"),
               ("SESTO", "FITKU", "Viking Line", "Ro-pax Stockholm–Turku"),
               ("SESTO", "FIHEL", "Tallink Grupp", "Ro-pax Stockholm–Helsinki"),
               ("SEUME", "FIVAA", "Wasaline", "Ro-pax Umeå–Vaasa")],
}
BALTIC_OVERLAND = set("PL LT LV EE RO BG GR TR RS BA MK AL ME MD HR".split())  # Via Baltica + Tallinn ferry


def _ferry_group(loc: dict, fi_end: dict) -> str | None:
    cc = loc.get("country")
    if cc in ("SE", "NO"):
        # northern Sweden/Norway and Finnish Lapland: overland via Haparanda–Tornio or Kilpisjärvi
        if (loc.get("lat") or 0) > 63.5 or (fi_end.get("lat") or 0) > 64.5:
            return None
        return "sweden"
    if cc in CONTINENT_RORO:
        return "continent"
    if cc in BALTIC_OVERLAND:
        return "baltic"
    return None


def road_specs(o: dict, d: dict, *, carrier=None, carrier_id=None, service=None, from_address=None, to_address=None) -> list[dict]:
    """Road booking between Finland and abroad: road to the ferry port, the ferry crossing, road from the port."""
    fi_import = d.get("country") == "FI" and o.get("country") != "FI"
    fi_export = o.get("country") == "FI" and d.get("country") != "FI"
    group = _ferry_group(o, d) if fi_import else _ferry_group(d, o) if fi_export else None
    if not group:
        return [leg_spec("road", o, d, "main", carrier=carrier, carrier_id=carrier_id, service=service,
                         from_address=from_address, to_address=to_address)]
    foreign, fi_end = (o, d) if fi_import else (d, o)

    def road_km(pair):
        fp, fip = geo.lookup_code(pair[0]), geo.lookup_code(pair[1])
        return geo.haversine_km([foreign["lon"], foreign["lat"]], [fp["lon"], fp["lat"]]) + \
            geo.haversine_km([fi_end["lon"], fi_end["lat"]], [fip["lon"], fip["lat"]])

    fp, fip, operator, ferry_service = min(ROAD_FERRIES[group], key=road_km)
    a, b = (fp, fip) if fi_import else (fip, fp)
    # The truck that collects the goods drives them to the ferry: that leg is main carriage ("Collected at ..."), not a
    # pre-carriage pickup, so the shipment is in transit while it drives.
    return [leg_spec("road", o, a, "main", carrier=carrier, carrier_id=carrier_id, service=service, from_address=from_address),
            leg_spec("sea", a, b, "main", carrier=operator, service=ferry_service,
                     ferry_crossing={"operator": operator, "included": None, "source": "unproven"}),
            leg_spec("road", b, d, "delivery", carrier=carrier, carrier_id=carrier_id, to_address=to_address)]


async def _nearest_of_kind(loc: dict, kind: str, min_rank: int) -> dict:
    best, bd = None, float("inf")
    for cand in geo.search("", kind=kind, limit=100, country=loc.get("country")) or []:
        if (cand.get("rank") or 0) < min_rank:
            continue
        dd = geo.haversine_km([loc["lon"], loc["lat"]], [cand["lon"], cand["lat"]])
        if dd < bd:
            best, bd = cand, dd
    if not best:
        raise HTTPException(422, f"No {kind} found near {loc.get('name')}. Choose the {kind} explicitly.")
    return geo.location_ref(best)


async def _nearest_port(loc: dict) -> dict:
    return await _nearest_of_kind(loc, "port", 60)


async def _nearest_airport(loc: dict) -> dict:
    return await _nearest_of_kind(loc, "airport", 40)


# --------------------------------------------------------------------------------------------- milestones

def place(loc: dict | None) -> dict | None:
    if not loc:
        return None
    return {"name": loc.get("name"), "lat": loc.get("lat"), "lon": loc.get("lon"), "unlocode": loc.get("unlocode")}


def planned_milestones(legs: list[dict], direction: str, *, customs: bool) -> list[dict]:
    ms: list[dict] = []

    def add(code, planned, loc, leg_seq=None, label=None):
        ms.append({"code": code, "label": label or MILESTONE_LABELS[code], "planned": planned, "at": None, "source": None,
                   "location": place(loc), "leg_seq": leg_seq, "by": None})

    main_idx = [i for i, lg in enumerate(legs) if lg["role"] not in ("pickup", "delivery")]
    last_main = main_idx[-1] if main_idx else None
    for i, lg in enumerate(legs):
        if lg["role"] == "pickup":
            add("picked_up", lg["etd"], lg["from"], lg["seq"], f"Picked up at {lg['from']['name']}")
            add("received", lg["eta"], lg["to"], lg["seq"], f"Received at {lg['to']['name']}")
            if direction == "export" and customs:
                add("export_cleared", lg["eta"], lg["to"], lg["seq"])
        elif lg["role"] == "delivery":
            if i > 0 and customs and direction == "import" and not any(m["code"] == "customs_released" for m in ms):
                add("customs_released", lg["etd"], lg["from"], lg["seq"])
            add("out_for_delivery", lg["etd"], lg["from"], lg["seq"], f"Out for delivery from {lg['from']['name']}")
            add("delivered", lg["eta"], lg["to"], lg["seq"], f"Delivered to {lg['to']['name']}")
        else:
            if i == 0 and direction == "export" and customs:
                add("export_cleared", lg["etd"], lg["from"], lg["seq"])
            door_road = lg["mode"] == "road" and lg["role"] == "main"
            label = f"Collected at {lg['from']['name']}" if door_road and i == 0 else f"Departed {lg['from']['name']}"
            add("departed", lg["etd"], lg["from"], lg["seq"], label)
            if door_road and i == len(legs) - 1:
                if customs and direction == "import":
                    add("customs_released", iso(parse_iso(lg["eta"]) - timedelta(hours=4)), lg["to"], lg["seq"])
                add("delivered", lg["eta"], lg["to"], lg["seq"], f"Delivered to {lg['to']['name']}")
            elif i == last_main:
                add("arrived", lg["eta"], lg["to"], lg["seq"], f"Arrived {lg['to']['name']}")
            else:
                add("transshipment", lg["eta"], lg["to"], lg["seq"], f"At {lg['to']['name']}")
    if legs and legs[-1]["role"] != "delivery" and not (legs[-1]["mode"] == "road" and legs[-1]["role"] == "main"):
        last = legs[-1]
        if customs and direction == "import":
            add("customs_released", iso(parse_iso(last["eta"]) + timedelta(hours=24)), last["to"], last["seq"])
        add("delivered", iso(parse_iso(last["eta"]) + timedelta(hours=36)), last["to"], last["seq"],
            f"Released to consignee at {last['to']['name']}")
    return ms


# --------------------------------------------------------------------------------------------- create

LEG_KEYS = ("carrier", "carrier_id", "service", "vessel", "voyage", "flight", "train", "via")


def _xy(x) -> list[float] | None:
    if isinstance(x, dict) and x.get("lat") is not None:
        return [x["lon"], x["lat"]]
    hit = geo.lookup_code(x) if isinstance(x, str) else None
    return [hit["lon"], hit["lat"]] if hit else None


def _same_place(a, b, km: float = 3.0) -> bool:
    pa, pb = _xy(a), _xy(b)
    return bool(pa and pb and geo.haversine_km(pa, pb) <= km)


def _loc_dict(x) -> dict | None:
    if isinstance(x, dict) and x.get("lat") is not None:
        return x
    hit = geo.lookup_code(x) if isinstance(x, str) else None
    return geo.location_ref(hit) if hit else None


def _road_purchase_specs(parts: list[dict], quoted: dict, offset: int) -> list[dict]:
    """Route geometry may split a purchase, but must not create extra purchases or suppliers."""
    coverage = quoted.get("ferry_coverage") or {"included": None, "source": "unproven"}
    for part in parts:
        part["quote_leg_seq"] = quoted.get("seq")
        if part["mode"] == "sea":
            part["ferry_crossing"] = {**coverage, "operator": part.get("carrier")}
    if any(p["mode"] == "sea" for p in parts) and coverage.get("included") is True and quoted.get("carrier_id"):
        parts[0]["booking_group"] = {"carrier_id": quoted["carrier_id"], "quote_leg_seq": quoted.get("seq"),
                                     "coverage": coverage, "leg_seqs": list(range(offset + 1, offset + len(parts) + 1))}
        for part in parts[1:]:
            part["booking_parent_seq"] = offset + 1
    return parts


def _option_leg_specs(option_legs: list[dict], mode: str, pickup_address: dict | None, delivery_address: dict | None) -> list[dict]:
    """Quote option legs (V2) -> leg specs. A road leg that crosses the Baltic is split into road, ferry and road legs
    (from the option's own `segments` when it lists the sea crossing, else from ROAD_FERRIES), so no truck is drawn
    driving over the sea. The first/last road legs get the door addresses."""
    specs: list[dict] = []
    for lg in option_legs:
        offset = len(specs)
        lmode = lg.get("mode", mode)
        role = lg.get("role", "main")
        extra = {k: lg.get(k) for k in LEG_KEYS}
        extra["carrier"] = lg.get("carrier") or lg.get("carrier_name")
        sea_segs = [sg for sg in lg.get("segments") or [] if sg.get("mode") == "sea" and sg.get("from") and sg.get("to")]
        if lmode == "road" and sea_segs:
            start = lg["from"]
            for sg in sea_segs:
                # a door pickup needs the truck to the ferry even when the quoted place is the port itself
                door = pickup_address and lg is option_legs[0] and sg is sea_segs[0]
                if door or not _same_place(start, sg["from"]):
                    specs.append(leg_spec("road", start, sg["from"], role if role != "delivery" else "main", **extra))
                pair = next((f for grp in ROAD_FERRIES.values() for f in grp if {f[0], f[1]} == {sg["from"], sg["to"]}), None)
                specs.append(leg_spec("sea", sg["from"], sg["to"], "main", carrier=pair[2] if pair else sg.get("operator") or sg.get("carrier"),
                                      service=pair[3] if pair else f"Ferry {sg['from']}–{sg['to']}"))
                start = sg["to"]
            if (delivery_address and lg is option_legs[-1]) or not _same_place(start, lg["to"]):
                specs.append(leg_spec("road", start, lg["to"], "delivery" if lg is option_legs[-1] else "main", **extra))
            specs[offset:] = _road_purchase_specs(specs[offset:], lg, offset)
            continue
        if lmode == "road":
            o, d = _loc_dict(lg["from"]), _loc_dict(lg["to"])
            if o and d and o.get("country") and d.get("country"):
                split = road_specs(o, d, **{k: extra.get(k) for k in ("carrier", "carrier_id", "service")})
                if len(split) > 1:
                    split[0]["from"], split[-1]["to"] = lg["from"], lg["to"]
                    specs += _road_purchase_specs(split, lg, offset)
                    continue
        specs.append(leg_spec(lmode, lg["from"], lg["to"], role, quote_leg_seq=lg.get("seq"), **extra))
    if specs and specs[0]["mode"] == "road" and pickup_address and not specs[0].get("from_address"):
        specs[0]["from_address"] = pickup_address
    if specs and specs[-1]["mode"] == "road" and delivery_address and not specs[-1].get("to_address"):
        specs[-1]["to_address"] = delivery_address
    return specs


async def _company_guard(company_id: str) -> dict:
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    if company.get("kyc_status") != "approved":
        raise HTTPException(409, "Booking needs an approved company check (KYC). Finish the company verification first.")
    if company.get("sanctions_status") != "clear":
        raise HTTPException(409, "Booking is on hold until compliance clears the sanctions screening for this company.")
    return company


def _cargo_summary(cargo: dict) -> dict:
    cargo = dict(cargo or {})
    teu = 0.0
    for c in cargo.get("containers") or []:
        t = str(c.get("type", "")).upper()
        n = int(c.get("count") or 1)
        teu += n * (1 if t.startswith("20") else 2)
    cargo.setdefault("teu", teu or None)
    return cargo


async def create_shipment(*, company_id: str, quote: dict, option: dict, booking: dict, actor: dict) -> dict:
    """Book a shipment from an accepted quote option. Idempotent per (quote, option)."""
    quote = quote or {}
    option = option or {}
    booking = booking or {}
    company = await _company_guard(company_id)
    if quote.get("id") and option.get("id"):
        existing = await db.shipments.find_one({"quote_id": quote["id"], "option_id": option["id"]}, NO_ID)
        if existing:
            return existing
    req = quote.get("request") or {}
    mode = option.get("mode") or req.get("mode") or "sea"
    origin_in = booking.get("origin") or req.get("origin")
    dest_in = booking.get("destination") or req.get("destination")
    if not origin_in or not dest_in:
        raise HTTPException(422, "Origin and destination are required to book a shipment.")
    pickup_address = booking.get("pickup_address") or (origin_in.get("address") if isinstance(origin_in, dict) else None)
    delivery_address = booking.get("delivery_address") or (dest_in.get("address") if isinstance(dest_in, dict) else None)
    origin = await _resolve(origin_in, pickup_address)
    destination = await _resolve(dest_in, delivery_address)
    origin["kinds"] = (await geo.find_location(origin.get("code") or origin.get("unlocode")) or {}).get("kinds") or origin.get("kinds") or []
    destination["kinds"] = (await geo.find_location(destination.get("code") or destination.get("unlocode")) or {}).get("kinds") or destination.get("kinds") or []
    from services import customs_border
    customs = customs_border.apply({**req, "origin": origin, "destination": destination})
    if customs["customs_required"] is None:
        raise HTTPException(409, {"code":"territory_unverified", "message":customs_border.UNVERIFIED_EN, "message_fi":customs_border.UNVERIFIED_FI})
    direction = req.get("direction") or booking.get("direction") or infer_direction(origin, destination)
    incoterm = booking.get("incoterm") or req.get("incoterm")
    carrier = option.get("carrier_name") or option.get("carrier")
    option_legs = [dict(lg) for lg in option.get("legs") or []]
    if not option_legs and mode == "road":
        option_legs = [{"seq": 1, "mode": "road", "from": origin, "to": destination,
                        "carrier_id": option.get("carrier_id"), "carrier_name": carrier,
                        "service": option.get("service"), "rate_card_id": option.get("rate_card_id"),
                        "ferry_coverage": option.get("ferry_coverage")}]
    for lg in option_legs:
        if lg.get("mode", mode) == "road" and not lg.get("ferry_coverage"):
            from services.pricing import ferry_coverage
            card = await db.rate_cards.find_one({"id": lg["rate_card_id"]}, NO_ID) if lg.get("rate_card_id") else None
            purchase_lines = [ln for ln in option.get("lines") or [] if ln.get("leg_seq") == lg.get("seq")]
            lg["ferry_coverage"] = ferry_coverage(card or {}, purchase_lines)
    if option_legs:
        specs = _option_leg_specs(option_legs, mode, pickup_address, delivery_address)
    else:
        specs = await plan_route_specs(mode=mode, origin=origin, destination=destination, incoterm=incoterm, carrier=carrier,
                                       carrier_id=option.get("carrier_id"), service=option.get("service"),
                                       pickup_address=pickup_address, delivery_address=delivery_address)
    legs = await build_legs(specs)
    etd = parse_iso(option.get("etd") or booking.get("ready_date") or req.get("ready_date")) or (now() + timedelta(days=2))
    if etd < now() - timedelta(days=1):
        etd = now() + timedelta(hours=24)
    eta = parse_iso(option.get("eta"))
    if not eta and option.get("transit_days"):
        eta = etd + timedelta(days=float(option["transit_days"]))
    etd, eta = schedule_legs(legs, etd, eta)
    shipment = await _assemble(
        company=company, mode=mode, direction=direction, incoterm=incoterm, incoterm_place=req.get("incoterm_place") or booking.get("incoterm_place"),
        origin=origin, destination=destination, legs=legs, etd=etd, eta=eta, cargo=req.get("cargo") or booking.get("cargo") or {},
        parties=booking.get("parties") or {}, references=booking.get("references") or {},
        customs=customs["customs_required"], insurance=booking.get("insurance") or req.get("insurance") or {},
        price={"total_cents": option.get("total_cents"), "currency": option.get("currency", "EUR")},
        liability_regime=option.get("liability_regime"), forwarder_role=booking.get("forwarder_role") or option.get("forwarder_role"),
        quote_id=quote.get("id"), option_id=option.get("id"), source=quote.get("source") or "portal", actor=actor,
        co2e_kg=option.get("co2e_kg"), service_label=option.get("service"),
    )
    shipment.update({key: customs.get(key) for key in ("customs_required", "customs_territory_verified", "customs_handling", "vat_excise_formalities", "customs_notice", "customs_direction", "customs_border")})
    shipment["customs"] = customs["customs_required"]
    shipment["customs_filing_status"] = "customer_arranged" if customs["customs_handling"] == "customer_broker" else "postponed" if customs["customs_required"] else "not_required"
    # A fiscal border can be domestic in transport terms (mainland FI -> Åland).
    # Keep its formalities visible as a planned step without claiming a release.
    milestones = shipment["milestones"]
    if customs["customs_required"] and not any(m["code"] in ("export_cleared", "customs_released") for m in milestones):
        delivery_index = next((i for i, m in enumerate(milestones) if m["code"] == "delivered"), len(milestones))
        milestones.insert(delivery_index, {"code": "customs_released", "label": MILESTONE_LABELS["customs_released"],
                          "planned": shipment["eta"], "at": None, "source": None, "location": place(destination),
                          "leg_seq": legs[-1]["seq"] if legs else None, "by": None})
    await db.shipments.insert_one(dict(shipment))
    await _event(shipment, "booked", "Booking confirmed", source="manual", actor=actor, location=shipment["origin"])
    await audit(actor, "shipment.created", "shipment", shipment["id"], {"reference": shipment["reference"], "quote_id": quote.get("id")},
                company_id=company_id)
    await emit("shipment.created", {"shipment": shipment, "actor": actor})
    from services import positions

    await positions.refresh_shipment(shipment["id"])
    return await get_shipment(shipment["id"])


async def _assemble(*, company: dict, mode: str, direction: str, incoterm, incoterm_place, origin, destination, legs, etd, eta,
                    cargo, parties, references, customs: bool, insurance, price, liability_regime, forwarder_role, quote_id,
                    option_id, source, actor, co2e_kg=None, service_label=None, reference: str | None = None, demo: bool = False) -> dict:
    regime = liability_regime or liability_for(mode, legs)
    milestones = planned_milestones(legs, direction, customs=customs)
    booked_at = now_iso()
    milestones.insert(0, {"code": "booked", "label": MILESTONE_LABELS["booked"], "planned": booked_at, "at": booked_at,
                          "source": "manual", "location": place(origin), "leg_seq": None, "by": actor_ref(actor)["name"]})
    cargo = _cargo_summary(cargo)
    refs = {"customer_ref": None, "po_numbers": [], **(references or {})}
    return {
        "id": new_id(), "reference": reference or await next_ref("HUO"), "company_id": company["id"], "customer_name": company["name"],
        "quote_id": quote_id, "option_id": option_id, "source": source, "mode": mode, "direction": direction,
        "incoterm": incoterm, "incoterm_place": incoterm_place, "forwarder_role": forwarder_role or "principal",
        "status": "booked", "milestone": "booked", "milestones": milestones,
        "origin": origin, "destination": destination, "legs": legs,
        "position": {"lat": origin["lat"], "lon": origin["lon"], "updated_at": booked_at, "source": "eta_interpolation", "leg_seq": legs[0]["seq"]},
        "progress": 0.0, "etd": iso(etd), "eta": iso(eta), "eta_planned": iso(eta), "atd": None, "ata": None, "delivered_at": None,
        "distance_km": round(sum(lg["distance_km"] or 0 for lg in legs), 1),
        "cargo": cargo, "weight_kg": cargo.get("weight_kg"), "volume_m3": cargo.get("volume_m3"), "teu": cargo.get("teu"),
        "parties": parties or {}, "references": refs, "has_exception": False, "exception_note": None, "holds": [],
        "price": price or {}, "liability_regime": regime, "liability": LIABILITY.get(regime), "insurance": insurance or {},
        "customs_clearance": customs, "sanctions_cleared": company.get("sanctions_status") == "clear", "lien_exercised": False,
        "customs_declaration_ids": [], "invoice_ids": [], "co2e_kg": co2e_kg, "service": service_label,
        "created_at": booked_at, "updated_at": booked_at, "created_by": actor_ref(actor), "demo": bool(demo or company.get("demo")),
        "test": bool(company.get("test")), "test_tenant": bool(company.get("test_tenant")),
    }


# --------------------------------------------------------------------------------------------- reads

async def get_shipment(shipment_id: str) -> dict | None:
    return await db.shipments.find_one({"id": shipment_id}, NO_ID)


async def _require(shipment_id: str) -> dict:
    s = await get_shipment(shipment_id)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


# --------------------------------------------------------------------------------------------- events & state

async def _event(shipment: dict, code: str, label: str, *, source: str, actor: dict | None = None, location: dict | None = None,
                 at: str | None = None, leg_seq: int | None = None, note: str | None = None, raw: dict | None = None) -> dict:
    ev = {"id": new_id(), "shipment_id": shipment["id"], "company_id": shipment["company_id"], "code": code, "label": label,
          "at": at or now_iso(), "location": place(location) if location else None, "source": source, "leg_seq": leg_seq,
          "note": note, "by": actor_ref(actor)["name"] if actor else None, "raw": raw}
    await db.tracking_events.insert_one(dict(ev))
    return ev


def _achieve(milestones: list[dict], code: str, *, at: str, source: str, by: str | None, location: dict | None,
             leg_seq: int | None = None, note: str | None = None, label: str | None = None) -> dict:
    """Mark the first open planned milestone with this code achieved, or append a new one."""
    for m in milestones:
        if m["code"] == code and not m.get("at") and (leg_seq is None or m.get("leg_seq") in (None, leg_seq)):
            m.update(at=at, source=source, by=by)
            if location:
                m["location"] = place(location)
            if note:
                m["note"] = note
            return m
    m = {"code": code, "label": label or MILESTONE_LABELS.get(code, code.replace("_", " ").capitalize()), "planned": None, "at": at,
         "source": source, "location": place(location) if location else None, "leg_seq": leg_seq, "by": by, "note": note}
    milestones.append(m)
    return m


def _current_leg(s: dict) -> dict | None:
    for lg in s["legs"]:
        if not lg.get("ata"):
            return lg
    return s["legs"][-1] if s["legs"] else None


def _apply_leg_effects(s: dict, code: str, at: str, leg_seq: int | None):
    legs = s["legs"]
    if not legs:
        return
    if leg_seq is None:
        if code in ("picked_up", "departed"):
            lg = next((x for x in legs if not x.get("atd")), None)
        elif code in ("received", "transshipment", "arrived"):
            lg = next((x for x in legs if x.get("atd") and not x.get("ata")), None) or next((x for x in legs if not x.get("ata")), None)
        elif code == "out_for_delivery":
            lg = next((x for x in legs if x["role"] == "delivery"), None)
        elif code == "delivered":
            lg = legs[-1]
        else:
            lg = None
    else:
        lg = next((x for x in legs if x["seq"] == leg_seq), None)
    if not lg:
        return
    # Collection and terminal receipt only move a pickup leg (or a road leg, where collection is the departure). On a
    # sea, air or rail leg they happen before it departs: stamping its ATD/ATA would put the map at the destination.
    if code == "picked_up" and lg["role"] != "pickup" and lg["mode"] != "road":
        return
    if code == "received" and lg["role"] != "pickup":
        return
    if code in ("picked_up", "departed", "out_for_delivery"):
        for prev in legs:  # earlier legs are necessarily complete
            if prev["seq"] < lg["seq"]:
                prev["atd"] = prev.get("atd") or at
                prev["ata"] = prev.get("ata") or at
                prev["status"] = "completed"
        lg["atd"] = lg.get("atd") or at
        lg["status"] = "in_progress"
        if not s.get("atd"):
            s["atd"] = at
    elif code in ("received", "transshipment", "arrived"):
        lg["atd"] = lg.get("atd") or at
        lg["ata"] = at
        lg["status"] = "completed"
        if code == "arrived":
            s["ata"] = at
    elif code == "delivered":
        for x in legs:
            x["atd"] = x.get("atd") or at
            x["ata"] = x.get("ata") or at
            x["status"] = "completed"
        s["delivered_at"] = at


def _check_transition(s: dict, new: str, *, milestone: str | None, note: str | None):
    old = s["status"]
    if new not in STATUSES:
        raise HTTPException(422, f"Unknown status {new}.")
    if new == old:
        if not (milestone or note):
            raise HTTPException(409, f"The shipment is already {old.replace('_', ' ')}.")
        return
    if new not in TRANSITIONS.get(old, set()):
        if old in ("delivered", "cancelled"):
            raise HTTPException(409, f"The shipment is {old}; its status can no longer change.")
        if new == "cancelled":
            raise HTTPException(409, "A shipment that has left origin cannot be cancelled. Record an exception and arrange a return instead.")
        raise HTTPException(409, f"Cannot move a shipment from {old.replace('_', ' ')} to {new.replace('_', ' ')}.")
    holds = {h["type"] for h in s.get("holds") or []}
    if "sanctions" in holds and new not in ("exception", "cancelled", "customs_hold"):
        raise HTTPException(409, "A sanctions hold is active. Compliance must clear it before the shipment can move.")
    if new in ("out_for_delivery", "delivered"):
        blocking = sorted(holds & DELIVERY_BLOCKING)
        if blocking:
            raise HTTPException(409, f"Release the {', '.join(blocking)} hold before delivery.")
        if s.get("lien_exercised"):
            raise HTTPException(409, "A lien is exercised on this cargo (NSAB § 14). Release it before delivery.")


async def set_status(shipment_id: str, status: str, *, actor: dict | None, note: str | None = None, milestone: str | None = None,
                     location: dict | None = None, source: str = "manual", at: str | None = None, leg_seq: int | None = None,
                     notify: bool = True) -> dict:
    s = await _require(shipment_id)
    if status == "cancelled":
        from services.carrier_booking import guard_started_group
        await guard_started_group(s, operation="cancel_shipment")
    _check_transition(s, status, milestone=milestone, note=note)
    old = s["status"]
    at = at or now_iso()
    code = milestone or DEFAULT_MILESTONE.get(status, status)
    by = actor_ref(actor)["name"] if actor else ("Schedule" if source == "schedule" else source)
    loc = location
    if loc is None:
        planned = next((m for m in s["milestones"] if m["code"] == code and not m.get("at")), None)
        loc = (planned or {}).get("location")
        if not loc and status == "delivered":
            loc = s["destination"]
    m = _achieve(s["milestones"], code, at=at, source=source, by=by, location=loc, leg_seq=leg_seq, note=note)
    _apply_leg_effects(s, code, at, leg_seq if leg_seq is not None else m.get("leg_seq"))
    upd = {"status": status, "milestone": code, "milestones": s["milestones"], "legs": s["legs"], "atd": s.get("atd"),
           "ata": s.get("ata"), "delivered_at": s.get("delivered_at"), "updated_at": now_iso()}
    if status == "exception":
        upd["has_exception"] = True
        upd["exception_note"] = note or s.get("exception_note")
    elif old == "exception":
        open_exc = await db.exceptions.count_documents({"shipment_id": s["id"], "status": {"$nin": ["resolved", "closed"]}})
        upd["has_exception"] = open_exc > 0
        if not open_exc:
            upd["exception_note"] = None
    if status == "delivered":
        upd["position"] = {"lat": s["destination"]["lat"], "lon": s["destination"]["lon"], "updated_at": at, "source": _pos_source(source),
                           "leg_seq": s["legs"][-1]["seq"] if s["legs"] else None}
        upd["progress"] = 1.0
    elif loc and loc.get("lat") is not None and source in ("gps", "ais", "carrier_api", "partner", "manual"):
        upd["position"] = {"lat": loc["lat"], "lon": loc["lon"], "updated_at": at, "source": _pos_source(source),
                           "leg_seq": m.get("leg_seq")}
    await db.shipments.update_one({"id": s["id"]}, {"$set": upd})
    s.update(upd)
    await _event(s, code, m["label"], source=source, actor=actor, location=loc, at=at, leg_seq=m.get("leg_seq"), note=note)
    if status != old:
        await audit(actor, "shipment.status", "shipment", s["id"], {"from": old, "to": status, "milestone": code, "note": note,
                                                                     "source": source}, company_id=s["company_id"])
        await emit("shipment.status_changed", {"shipment": s, "old_status": old, "new_status": status, "actor": actor})
        if status == "delivered":
            await emit("shipment.delivered", {"shipment": s, "actor": actor})
        if notify and status in ("delivered", "exception", "customs_hold", "out_for_delivery"):
            if status in V4_ROUTINE_STATUSES and await _routine_updates_on():
                log.info("customer %s notice for %s left to V4's routine update", status, s["reference"])
            else:
                await _notify_customer(s, status, note)
    from services import positions

    await positions.refresh_shipment(s["id"])
    return await get_shipment(s["id"])


def _pos_source(source: str) -> str:
    return {"ais": "ais", "gps": "gps", "partner": "gps", "carrier_api": "carrier_api"}.get(source, "eta_interpolation")


# V4 (services/comms_agent.routine_update) owns the customer's routine status e-mails for these statuses. While it auto-sends
# them (settings.ai_auto_send_routine_messages, default on), V1 sends nothing for them, so the customer gets one e-mail.
V4_ROUTINE_STATUSES = {"delivered", "out_for_delivery"}


async def _routine_updates_on() -> bool:
    import importlib.util

    if importlib.util.find_spec("services.comms_agent") is None:
        return False
    from core.settings import get_settings

    try:
        return bool((await get_settings()).get("ai_auto_send_routine_messages", True))
    except Exception:  # noqa: BLE001 - if settings can't be read, fall back to V1's own notice
        log.exception("settings read failed")
        return False


async def _notify_customer(s: dict, status: str, note: str | None):
    titles = {  # (fi, en) title, (fi, en) body
        "delivered": (("Toimitettu", "Delivered"), ("{ref} on toimitettu kohteeseen {dest}.", "{ref} was delivered to {dest}.")),
        "exception": (("Lähetyksen poikkeama", "Shipment exception"), ("{ref}: {note}", "{ref}: {note}")),
        "customs_hold": (("Tullipysäytys", "Customs hold"), ("{ref} on pysäytetty tullissa: {note}", "{ref} is held in customs: {note}")),
        "out_for_delivery": (("Jakelussa", "Out for delivery"), ("{ref} on matkalla kohteeseen {dest}.", "{ref} is on its way to {dest}.")),
    }
    (title_fi, title_en), (body_fi, body_en) = titles[status]
    args = {"ref": s["reference"], "dest": s["destination"]["name"]}
    try:
        await notify_company(s["company_id"], bi(f"{title_fi}: {s['reference']}", f"{title_en}: {s['reference']}"),
                             bi(body_fi.format(**args, note=note or "katso lisätiedot lähetykseltä"),
                                body_en.format(**args, note=note or "see the shipment for details")),
                             link=f"/portal/shipments/{s['id']}", kind="shipment",
                             # s14: exceptions and customs holds also go out by e-mail (users can opt out in Settings)
                             email=True, base_url=config.APP_BASE_URL)
    except Exception:  # noqa: BLE001 - notification failure must not undo the status change
        log.exception("notify failed")


async def record_milestone(shipment_id: str, code: str, *, actor: dict | None, source: str = "manual", note: str | None = None,
                           location: dict | None = None, at: str | None = None, leg_seq: int | None = None) -> dict:
    """Record a milestone; moves the status when the milestone implies one (departed -> in transit, ...)."""
    s = await _require(shipment_id)
    target = STATUS_FOR_MILESTONE.get(code)
    if code in ("transshipment", "arrived") and s["status"] in ("customs_hold", "exception"):
        target = None  # an arrival does not lift a hold or an exception
    if code == "received" and s["status"] != "booked":
        target = None
    if target and target != s["status"] and target in TRANSITIONS.get(s["status"], set()):
        return await set_status(shipment_id, target, actor=actor, note=note, milestone=code, location=location, source=source,
                                at=at, leg_seq=leg_seq)
    if target == s["status"] or target is None or code in ("export_cleared", "customs_released"):
        if s["status"] in ("delivered", "cancelled") and code not in ("note",):
            raise HTTPException(409, f"The shipment is {s['status']}; milestones are closed.")
        at = at or now_iso()
        by = actor_ref(actor)["name"] if actor else source
        m = _achieve(s["milestones"], code, at=at, source=source, by=by, location=location, leg_seq=leg_seq, note=note)
        _apply_leg_effects(s, code, at, leg_seq if leg_seq is not None else m.get("leg_seq"))
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"milestones": s["milestones"], "legs": s["legs"], "milestone": code,
                                                                 "atd": s.get("atd"), "ata": s.get("ata"), "updated_at": now_iso()}})
        await _event(s, code, m["label"], source=source, actor=actor, location=location or m.get("location"), at=at,
                     leg_seq=m.get("leg_seq"), note=note)
        await audit(actor, "shipment.milestone", "shipment", s["id"], {"code": code, "note": note, "source": source}, company_id=s["company_id"])
        from services import positions

        await positions.refresh_shipment(s["id"])
        return await get_shipment(s["id"])
    raise HTTPException(409, f"{MILESTONE_LABELS.get(code, code)} does not fit a shipment that is {s['status'].replace('_', ' ')}.")


async def add_hold(shipment_id, hold_type, reason, actor, source: str = "manual"):
    if hold_type not in HOLD_TYPES:
        raise HTTPException(422, f"Unknown hold type {hold_type}. Use one of: {', '.join(sorted(HOLD_TYPES))}.")
    s = await _require(shipment_id)
    if s["status"] in ("delivered", "cancelled"):
        raise HTTPException(409, f"The shipment is {s['status']}; holds no longer apply.")
    holds = [h for h in s.get("holds") or [] if h["type"] != hold_type]
    hold = {"type": hold_type, "reason": reason or "", "since": now_iso(), "by": actor_ref(actor)["name"] if actor else "System",
            "prev_status": s["status"]}
    holds.append(hold)
    upd = {"holds": holds, "updated_at": now_iso()}
    if hold_type == "lien":
        upd["lien_exercised"] = True
    await db.shipments.update_one({"id": s["id"]}, {"$set": upd})
    await audit(actor, "shipment.hold_added", "shipment", s["id"], {"type": hold_type, "reason": reason}, company_id=s["company_id"])
    if hold_type in CUSTOMS_HOLD_TYPES and s["status"] not in ("customs_hold", "exception"):
        return await set_status(shipment_id, "customs_hold", actor=actor, note=reason, milestone="customs_hold", source=source)
    await _event(s, "hold_added", f"{hold_type.capitalize()} hold", source=source, actor=actor, note=reason)
    from services import positions

    await positions.refresh_shipment(shipment_id)
    return await get_shipment(shipment_id)


async def release_hold(shipment_id, hold_type, actor, source: str = "manual"):
    s = await _require(shipment_id)
    holds = s.get("holds") or []
    hold = next((h for h in holds if h["type"] == hold_type), None)
    if not hold:
        raise HTTPException(404, f"No {hold_type} hold on this shipment.")
    rest = [h for h in holds if h["type"] != hold_type]
    upd = {"holds": rest, "updated_at": now_iso()}
    if hold_type == "lien":
        upd["lien_exercised"] = False
    await db.shipments.update_one({"id": s["id"]}, {"$set": upd})
    await audit(actor, "shipment.hold_released", "shipment", s["id"], {"type": hold_type}, company_id=s["company_id"])
    if s["status"] == "customs_hold" and not any(h["type"] in CUSTOMS_HOLD_TYPES for h in rest):
        back = hold.get("prev_status") if hold.get("prev_status") in ("booked", "picked_up", "at_hub", "in_transit", "out_for_delivery") else "at_hub"
        if back not in TRANSITIONS["customs_hold"]:
            back = "at_hub"
        code = "customs_released" if hold_type == "customs" and s.get("direction") == "import" else "hold_released"
        return await set_status(shipment_id, back, actor=actor, note=f"{hold_type.capitalize()} hold released", milestone=code,
                                source=source)
    await _event(s, "hold_released", f"{hold_type.capitalize()} hold released", source=source, actor=actor)
    from services import positions

    await positions.refresh_shipment(shipment_id)
    return await get_shipment(shipment_id)


async def cancel_shipment(shipment_id: str, *, actor: dict, reason: str) -> dict:
    s = await _require(shipment_id)
    from services.carrier_booking import guard_started_group
    await guard_started_group(s, operation="cancel_shipment")
    if s["status"] not in ("booked", "picked_up", "exception"):
        raise HTTPException(409, "Only shipments that have not left origin can be cancelled. Record an exception instead.")
    if any(lg.get("atd") and lg["role"] not in ("pickup",) for lg in s["legs"]):
        raise HTTPException(409, "Main carriage has already departed; the shipment cannot be cancelled.")
    return await set_status(shipment_id, "cancelled", actor=actor, note=reason, milestone="cancelled")


async def update_leg(shipment_id: str, seq: int, changes: dict, actor: dict, *, tracking_update: bool = False) -> dict:
    """Ops/V2: assign carrier, vessel, voyage, flight, train, plate, document numbers, ETD/ETA."""
    s = await _require(shipment_id)
    leg = next((lg for lg in s["legs"] if lg["seq"] == seq), None)
    if not leg:
        raise HTTPException(404, f"Leg {seq} not found")
    if s["status"] in ("delivered", "cancelled"):
        raise HTTPException(409, f"The shipment is {s['status']}; legs can no longer change.")
    allowed = {"carrier_id", "carrier", "vessel", "voyage", "flight", "train", "truck_plate", "transport_doc_no", "container_numbers",
               "etd", "eta", "service", "driver_phone", "nationality", "identification_type", "exit_office"}
    clean = {k: v for k, v in changes.items() if k in allowed}
    from services.tulli_codelists import entry
    for field, code_list in (("nationality", "CL008"), ("identification_type", "CL750")):
        if clean.get(field) and not entry(code_list, clean[field]):
            raise HTTPException(422, f"Invalid {field} code")
    if tracking_update and any(key not in ("eta", "vessel") for key in clean):
        raise HTTPException(422, "Tracking observations cannot change the carrier's purchase terms.")
    purchase_keys = ("carrier_id", "carrier", "service", "etd") + (() if tracking_update else ("eta",))
    if any(key in clean and clean[key] != leg.get(key) for key in purchase_keys):
        from services.carrier_booking import guard_started_group
        await guard_started_group(s, leg, operation="amend")
    if "carrier_id" in clean and clean["carrier_id"]:
        car = await db.carriers.find_one({"id": clean["carrier_id"]}, NO_ID)
        if not car:
            raise HTTPException(422, "Unknown carrier. Pick a carrier from the list.")
        from core.test_counterparty import check
        check(car, s, "carrier")
        clean.setdefault("carrier", car.get("name"))
    for k in ("etd", "eta"):
        if clean.get(k):
            dt = parse_iso(clean[k])
            if not dt:
                raise HTTPException(422, f"{k.upper()} must be a date and time.")
            clean[k] = iso(dt)
    if clean.get("vessel") and isinstance(clean["vessel"], dict):
        v = clean["vessel"]
        clean["vessel"] = {"name": (v.get("name") or "").strip() or None, "imo": str(v.get("imo") or "").strip() or None,
                           "mmsi": str(v.get("mmsi") or "").strip() or None}
    before = {k: leg.get(k) for k in clean}
    leg.update(clean)
    upd = {"legs": s["legs"], "updated_at": now_iso()}
    eta_changed = "eta" in clean and clean["eta"] != before.get("eta")
    if eta_changed and seq == s["legs"][-1]["seq"]:
        upd["eta"] = clean["eta"]
    elif eta_changed:
        # shift later legs by the same delay so the door ETA stays honest
        delta = parse_iso(clean["eta"]) - parse_iso(before["eta"]) if before.get("eta") else timedelta(0)
        for lg in s["legs"]:
            if lg["seq"] > seq and not lg.get("atd"):
                lg["etd"] = iso(parse_iso(lg["etd"]) + delta)
                lg["eta"] = iso(parse_iso(lg["eta"]) + delta)
                for m in s["milestones"]:
                    if m.get("leg_seq") == lg["seq"] and not m.get("at") and m.get("planned"):
                        m["planned"] = iso(parse_iso(m["planned"]) + delta)
        upd["eta"] = s["legs"][-1]["eta"]
        upd["milestones"] = s["milestones"]
    if eta_changed:
        for m in s["milestones"]:
            if m.get("leg_seq") == seq and not m.get("at") and m["code"] in ("arrived", "transshipment", "received", "delivered"):
                m["planned"] = clean["eta"]
        upd["milestones"] = s["milestones"]
    if "etd" in clean:
        for m in s["milestones"]:
            if m.get("leg_seq") == seq and not m.get("at") and m["code"] in ("departed", "picked_up", "out_for_delivery"):
                m["planned"] = clean["etd"]
        upd["milestones"] = s["milestones"]
    await db.shipments.update_one({"id": s["id"]}, {"$set": upd})
    await audit(actor, "shipment.leg_updated", "shipment", s["id"], {"seq": seq, "before": before, "after": clean}, company_id=s["company_id"])
    if eta_changed:
        s2 = await get_shipment(s["id"])
        feed = (actor or {}).get("type") == "system" and (actor or {}).get("id") == "digitraffic"
        leg_after = next((lg for lg in s2.get("legs") or [] if lg["seq"] == seq), {})
        await _event(s2, "eta_changed", f"ETA changed to {s2['eta'][:16].replace('T', ' ')} UTC", source="port_call" if feed else "manual",
                     actor=actor, leg_seq=seq if feed else None,
                     note=f"Digitraffic port call {(leg_after.get('port_call') or {}).get('port_call_id')}: agent-reported ETA"
                     if feed else None, raw={"port_call": leg_after.get("port_call")} if feed else None)
    from services import positions

    await positions.refresh_shipment(s["id"])
    return await get_shipment(s["id"])


# --------------------------------------------------------------------------------------------- path migration

async def migrate_paths() -> int:
    """Bring stored lane geometry up to geo.PATH_VERSION: air legs are re-routed (closed airspace), every other leg is
    densified in place (its geometry is kept: it may be a train's own station sequence or an AIS-corrected lane)."""
    from services import positions

    n = 0
    async for s in db.shipments.find({"legs": {"$elemMatch": {"path_version": {"$ne": geo.PATH_VERSION}}}}, {"_id": 0, "id": 1, "legs": 1}):
        for lg in s["legs"]:
            if lg.get("path_version") == geo.PATH_VERSION:
                continue
            if lg["mode"] == "air":
                info = await geo.route_info_async("air", lg["from"], lg["to"])
                lg.update(path=info["path"], path_source=info["source"], path_note=info["note"], distance_km=info["distance_km"])
            else:
                lg["path"] = geo.densify(lg.get("path") or [[lg["from"]["lon"], lg["from"]["lat"]], [lg["to"]["lon"], lg["to"]["lat"]]])
                lg["distance_km"] = geo.path_length_km(lg["path"])
            lg["path_version"] = geo.PATH_VERSION
        dist = round(sum(lg.get("distance_km") or 0 for lg in s["legs"]), 1)
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"legs": s["legs"], "distance_km": dist}})
        await positions.refresh_shipment(s["id"], publish_always=False)
        n += 1
    if n:
        log.info("lane geometry migrated to v%s on %s shipments", geo.PATH_VERSION, n)
    return n


@on_startup(order=31)
async def _migrate_paths_on_startup():
    await migrate_paths()
