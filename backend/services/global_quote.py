"""Any-city quotes (s5): every city pair on earth gets priced options, never a dead end.

    q = await quote_any(request, company=None, source="email")   # stored quote (see shared/interfaces.md, s5)
    priced = await preview(request)                              # same pricing, not stored

How an option is built after the rate-table engine (services.pricing.build_options) has run:
1. Routings per mode for the cargo: road door-to-door (same road landmass), sea via the nearest container gateways
   (services.hubs, data/gateway_ports.json), air via the nearest cargo airports, rail between terminals of one rail
   network (China–Europe over the Middle Corridor), courier for small parcels. Pre- and on-carriage connect the
   places to the hubs.
2. Each leg is priced in this order and says which one it got (`legs[].pricing.source`):
   a. `rate_cards` (seeded, ops-entered and Sourcing Autopilot cards; region cards cover a whole country),
   b. otherwise the leg is marked for procurement and s3's `request_lane_rates` is asked for a live rate (one
      request per lane, shared by every quote on it; the quote is re-priced when `partner.rate_loaded` fires),
   c. meanwhile — and for good if no rate can be had — a benchmark estimate (services.benchmarks: dated
      WCI / FBX / air index levels, per-km road and rail models), every line marked `estimated`.
3. `firmness`: firm (every freight leg from a rate card) | partial (main leg firm, a local leg estimated) |
   estimate (main leg estimated). Only firm options are bookable.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.db import db
from core.events import emit, on
from core.jobs import job, schedule, enqueue
from core.settings import get_settings
from core.util import NO_ID, iso, now, now_iso
from services import benchmarks as bm
from services import hubs
from services import pricing

log = logging.getLogger("global_quote")
BOT = {"id": "global_quote", "name": "Global quoting", "org_type": "internal", "role": "system", "permissions": []}
HEL = ZoneInfo("Europe/Helsinki")

ROAD_MAX_KM = 5000          # longest door-to-door truck leg offered
PRECARRIAGE_MAX_KM = 2600   # city -> port (landlocked capitals by road; North American / Asian interiors by rail)
RAIL_PRECARRIAGE_KM = 800   # container pre-/on-carriage longer than this goes by rail inside a rail network
AIRPORT_MAX_KM = {1: 350, 2: 220}
RAIL_MIN_KM = 400
SEA_MIN_KM_IF_ROAD = 2500   # below this a road-connected pair is not offered by sea (unless sea is asked for)
AIR_MIN_KM = 400
LOCAL_LEG_KM = 150          # pre/on-carriage legs this short are not sent to procurement
COURIER_MAX_KG, COURIER_MAX_PIECE_KG = 300.0, 70.0
CIRCUITY_ROAD = {"AFRICA": 1.45, "SOUTH_AMERICA": 1.4}
DEFAULT_REPLY_HOURS = {"road": 6, "air": 8, "sea": 24, "rail": 48}
SOURCEABLE = {"ROAD_FTL": "road", "FCL": "sea", "AIR": "air", "RAIL_FCL": "rail", "HAULAGE": "road"}
FINAL_STATUSES = ("booked", "accepted", "expired", "rejected", "booking")


# ------------------------------------------------------------------------------------------------ helpers
def _km_road(a: dict, b: dict) -> float:
    """Approximate driven distance (great circle × circuity of the landmass). No network call on the quote path."""
    lm = hubs.landmass(a.get("country")) or ""
    return round(hubs.haversine_km(a, b) * CIRCUITY_ROAD.get(lm, 1.3), 1)


def _km_sea(a: dict, b: dict) -> float:
    from services import carriers as net

    try:
        return float(net.leg_distance_km("sea", a, b))
    except Exception:  # noqa: BLE001
        return hubs.haversine_km(a, b) * 1.35


def _loc(x: dict) -> dict:
    out = pricing._leg_loc(x) or {}
    for k in ("kind", "iata", "region", "tier"):
        if x.get(k) is not None:
            out[k] = x[k]
    return out


def _line(code: str, stage: str, basis: str, qty: float, unit_cost_eur: float, *, leg_seq: int | None, note: str | None,
          source: dict, estimated: bool = True, description: str | None = None) -> dict:
    ln = pricing._cost_line(code, stage, basis, qty, unit_cost_eur * 100.0, note=note, leg_seq=leg_seq, description=description)
    ln.update(estimated=estimated, source=source)
    return ln


def _card_source(card: dict, carriers: dict) -> dict:
    src = "partner_rate" if card.get("source") == "partner_network" else "rate_card"
    car = carriers.get(card.get("carrier_id"), {})
    return {"kind": src, "rate_card_id": card["id"], "carrier": car.get("name"), "valid_to": card.get("valid_to"),
            "rate_source": card.get("source") or "indicative"}


def _qty_containers(m: dict) -> int:
    return sum(c["count"] for c in m.get("containers") or []) or 1


def courier_eligible(cargo: dict, m: dict) -> bool:
    w = m.get("weight_kg") or 0
    n = cargo.get("packages") or 1
    if m.get("is_fcl") or cargo.get("dangerous_goods") or not w or w > COURIER_MAX_KG or w / n > COURIER_MAX_PIECE_KG:
        return False
    return (m.get("volume_m3") or 0) <= 2.0 and cargo.get("package_type") not in ("pallet_eur", "pallet_fin") or w <= 70


# ------------------------------------------------------------------------------------------------ rate-table lookup
class Tables:
    """Live, accepted rate cards of active carriers (loaded once per quote)."""

    def __init__(self, cards: list[dict], carriers: dict, ready: date):
        self.carriers = carriers
        self.cards = [c for c in cards if c.get("carrier_id") in carriers and pricing.card_valid_on(c, ready)]

    @classmethod
    async def load(cls, ready: date) -> "Tables":
        carriers = {c["id"]: c async for c in db.carriers.find({"active": {"$ne": False}}, NO_ID)}
        cards = [c async for c in db.rate_cards.find({"archived": {"$ne": True}, "status": {"$nin": ["pending", "rejected", "revoked"]}}, NO_ID)]
        codes = set()
        for c in cards:
            codes |= {c["origin"].get("unlocode"), c["destination"].get("unlocode"), *(c.get("via") or [])}
        await pricing.load_network_coordinates({x for x in codes if x})
        return cls(cards, carriers, ready)

    def best(self, services: set[str], a: dict, b: dict, m: dict, *, km: float, kind: str, freight_code: str,
             leg_seq: int, allow_zone: bool = False) -> tuple[dict, list[dict]] | None:
        """Cheapest card of these services whose origin covers a and destination covers b."""
        best = None
        for card in self.cards:
            if card["service"] not in services:
                continue
            oc, dc = pricing.cover(a, card, "origin"), pricing.cover(b, card, "destination")
            ok = {"direct", "zone"} if allow_zone else {"direct"}
            if not oc or not dc or oc[0] not in ok or dc[0] not in ok:
                continue
            mm = m
            if card["service"] in ("ROAD_FTL", "RORO") and m.get("is_fcl"):
                mm = {**m, "ldm": pricing.TRAILER_LDM * _qty_containers(m)}  # one container per truck
            lines = pricing.price_card(card, mm, km=km, kind=kind, leg_seq=leg_seq, freight_code=freight_code)
            if not lines:
                continue
            cost = sum(ln["unit_cost_cents"] * ln["qty"] for ln in lines)
            if best is None or cost < best[2]:
                best = (card, lines, cost)
        return (best[0], best[1]) if best else None


# ------------------------------------------------------------------------------------------------ estimate legs
async def road_leg_estimate(a: dict, b: dict, m: dict, *, service: str, kind: str, leg_seq: int, km: float) -> tuple[list[dict], dict, float]:
    """Road leg from the per-km model -> (cost lines, pricing source, transit days)."""
    rate, src = await bm.road_eur_per_km(a.get("country"), b.get("country"))
    borders = hubs.border_crossings(a, b)
    stage = {"pre": "origin", "on": "destination"}.get(kind, "main")
    code = {"pre": "PICKUP", "on": "DELIVERY"}.get(kind, "ROAD_FREIGHT")
    lines = []
    if service == "ROAD_LTL" and not m.get("is_fcl"):
        ch = pricing.chargeable("HAULAGE_LTL", m)
        truck = max(bm.ROAD_MIN_EUR, rate * km + bm.ROAD_FIXED_EUR)
        cost = max(bm.LTL_MIN_EUR, truck * min(1.0, ch["qty"] / pricing.TRAILER_KG) * bm.ltl_factor(ch["qty"]))
        cost = min(cost, truck)
        lines.append(_line(code, stage, "PER_SHIPMENT", 1, cost, leg_seq=leg_seq, source=src,
                           note=f"{bm.num_en(km)} km, {ch['label']}, groupage share of a truck"))
    else:
        units = _qty_containers(m) if m.get("is_fcl") else pricing.chargeable("ROAD_FTL", m)["vehicles"]
        per = max(bm.ROAD_MIN_EUR, rate * km * (1.05 if m.get("is_fcl") else 1.0) + bm.ROAD_FIXED_EUR)
        what = "container truck" if m.get("is_fcl") else "full trailer (13.6 ldm)"
        lines.append(_line(code, stage, "PER_SHIPMENT", units, per, leg_seq=leg_seq, source=src,
                           note=f"{bm.num_en(km)} km × EUR {rate:.2f}/km, {units} × {what}"))
    if borders:
        lines.append(_line("BORDER", stage, "PER_SHIPMENT", borders, bm.BORDER_EUR, leg_seq=leg_seq, source=src,
                           note="customs border, transit documents"))
    days = max(1, math.ceil(km / bm.ROAD_KM_PER_DAY)) + borders
    return lines, src, days


def _ets_applies(a: dict, b: dict) -> bool:
    from services import carriers as net

    eea = net.EU_COUNTRIES | {"NO", "IS"}
    return a.get("country") in eea or b.get("country") in eea


async def _surcharges(pol: dict, pod: dict, on: date) -> list[dict]:
    """Carrier notices in force on the sailing date for this trade (war risk, PSS) from the stored levels. Used only for
    lanes without their own index level: WCI/FBX levels already contain PSS, war risk and emergency surcharges."""
    lv = await bm.levels()
    out = []
    day = on.isoformat()
    o_reg, d_reg = pol.get("region"), pod.get("region")
    o_cc, d_cc = (pol.get("country") or "").upper(), (pod.get("country") or "").upper()
    best: dict[str, dict] = {}
    for lvl in lv.values():
        if lvl.get("mode") != "sea_surcharge":
            continue
        if (lvl.get("valid_to") and lvl["valid_to"] < day) or (lvl.get("valid_from") and lvl["valid_from"] > day):
            continue
        if lvl.get("regions_any") and not ({o_reg, d_reg} & set(lvl["regions_any"])):
            continue
        if lvl.get("origin_regions") and o_reg not in lvl["origin_regions"]:
            continue
        if lvl.get("destination_regions") and d_reg not in lvl["destination_regions"]:
            continue
        if lvl.get("origin_countries") and o_cc not in lvl["origin_countries"]:
            continue
        if lvl.get("destination_countries") and d_cc not in lvl["destination_countries"]:
            continue
        code = lvl.get("code") or "WRS"
        # several carriers announce the same kind of charge: the estimate uses the lowest notice
        per_feu = float(lvl["value"]) * (2 if "TEU" in (lvl.get("unit") or "").upper() else 1)
        if code not in best or per_feu < best[code]["_per_feu"]:
            best[code] = {**lvl, "_per_feu": per_feu}
    out = list(best.values())
    return out


async def sea_leg_estimate(pol: dict, pod: dict, m: dict, *, service: str, leg_seq: int, sea_km: float,
                           sail_on: date | None = None) -> tuple[list[dict], dict, float, list[str]]:
    eur_usd, fx = await bm.eur_per_usd()
    o_reg, d_reg = pol.get("region") or "NEU", pod.get("region") or "NEU"
    usd_feu, src = await bm.sea_feu_usd(o_reg, d_reg, sea_km)
    src = {**src, "fx": {"EURUSD": fx.get("value"), "as_of": fx.get("as_of")}}
    lines: list[dict] = []
    direct = frozenset((o_reg, d_reg)) in bm.DIRECT_PAIRS
    feeder = (pol.get("tier") or 3) >= 3 or (pod.get("tier") or 3) >= 3
    incl = "index all-in: base freight incl. BAF/CAF/PSS"
    thc_in = set(src.get("thc_included") or [])
    if service == "FCL":
        for c in m["containers"]:
            f = bm.EQUIPMENT_FACTOR.get(c["type"], 1.0)
            unit = usd_feu * f * eur_usd + (300.0 * eur_usd * f if feeder else 0.0)
            lines.append(_line("OCEAN_FREIGHT", "main", "PER_CONTAINER", c["count"], unit, leg_seq=leg_seq, source=src,
                               note=f"{pricing.CONTAINERS[c['type']]['label']}, {incl}" + (", feeder port" if feeder else "")))
            if "origin" not in thc_in:
                lines.append(_line("OTHC", "origin", "PER_CONTAINER", c["count"], bm.thc_usd(o_reg, c["type"]) * eur_usd,
                                   leg_seq=leg_seq, source=bm._src(None, kind="model", note="Typical terminal handling by region", note_fi="Tyypillinen terminaalikäsittely alueittain"),
                                   note=f"{pol.get('name')}"))
            if "destination" not in thc_in:
                lines.append(_line("DTHC", "destination", "PER_CONTAINER", c["count"], bm.thc_usd(d_reg, c["type"]) * eur_usd,
                                   leg_seq=leg_seq, source=bm._src(None, kind="model", note="Typical terminal handling by region", note_fi="Tyypillinen terminaalikäsittely alueittain"),
                                   note=f"{pod.get('name')}"))
            if _ets_applies(pol, pod):
                teu = pricing.CONTAINERS[c["type"]]["teu"]
                lines.append(_line("ETS", "main", "PER_CONTAINER", c["count"], 45.0 * teu, leg_seq=leg_seq,
                                   source=bm._src(None, kind="model", note="EU ETS 2026, 50 % of an extra-EU voyage", note_fi="EU:n päästökauppa 2026, 50 % EU:n ulkopuolisesta matkasta"),
                                   note="EU ETS 2026"))
        n_bl = 1
    else:  # LCL
        ch = pricing.chargeable("LCL", m)
        per_wm = usd_feu / 25.0 * eur_usd + (12.0 if feeder else 0.0)
        lines.append(_line("OCEAN_FREIGHT", "main", "PER_WM", ch["qty"], per_wm, leg_seq=leg_seq, source=src,
                           note=f"{ch['label']}, 1/25 of the 40' level"))
        lines.append(_line("CFS_O", "origin", "PER_WM", ch["qty"], bm.CFS_USD_PER_WM["origin"] * eur_usd, leg_seq=leg_seq,
                           source=bm._src(None, kind="model", note="Typical CFS charge", note_fi="Tyypillinen CFS-maksu"), note=pol.get("name")))
        lines.append(_line("CFS_D", "destination", "PER_WM", ch["qty"], bm.CFS_USD_PER_WM["destination"] * eur_usd,
                           leg_seq=leg_seq, source=bm._src(None, kind="model", note="Typical CFS charge", note_fi="Tyypillinen CFS-maksu"), note=pod.get("name")))
        n_bl = 1
    lines.append(_line("DOC", "origin", "PER_BL", n_bl, bm.DOC_USD * eur_usd, leg_seq=leg_seq,
                       source=bm._src(None, kind="model", note="Typical B/L fee", note_fi="Tyypillinen konossementtimaksu"), note=None))
    lines.append(_line("DO", "destination", "PER_BL", n_bl, bm.DOC_USD * eur_usd, leg_seq=leg_seq,
                       source=bm._src(None, kind="model", note="Typical D/O fee", note_fi="Tyypillinen luovutusmääräysmaksu"), note=None))
    if src.get("kind") != "benchmark":  # index levels already contain PSS / war risk / emergency surcharges
        for s in await _surcharges(pol, pod, sail_on or date.today()):
            unit_eur = float(s["value"]) * (eur_usd if (s.get("unit") or "").upper().startswith("USD") else 1.0)
            per_teu = "TEU" in (s.get("unit") or "").upper()
            note = f"{s.get('index')} notice ({s.get('as_of')})"
            if service == "FCL":
                for c in m["containers"]:
                    teu = pricing.CONTAINERS[c["type"]]["teu"]
                    unit = unit_eur * (teu if per_teu else (0.5 if c["type"].startswith("20") else 1.0))
                    lines.append(_line(s.get("code") or "WRS", "main", "PER_CONTAINER", c["count"], unit, leg_seq=leg_seq,
                                       source=bm._src(s, kind="benchmark", note=s.get("label") or ""), note=note))
            else:  # LCL: the share of a 40' (1/25 per W/M)
                ch = pricing.chargeable("LCL", m)
                feu = unit_eur * (2 if per_teu else 1)
                lines.append(_line(s.get("code") or "WRS", "main", "PER_WM", ch["qty"], feu / 25.0, leg_seq=leg_seq,
                                   source=bm._src(s, kind="benchmark", note=s.get("label") or ""), note=note + ", 1/25 of the 40' charge"))
    via = []
    days = max(2, math.ceil(sea_km / 550.0)) + 2 + (0 if direct else 5) + (3 if feeder else 0)
    if not direct:
        hub = await _transshipment_hub(pol, pod)
        if hub:
            via.append(hub)
    return lines, src, days, via


async def _transshipment_hub(pol: dict, pod: dict) -> str | None:
    for side in (pol, pod):
        if (side.get("tier") or 3) == 1:
            continue
        cand = await hubs.nearest("port", side, limit=1, max_km=4000, same_landmass=False, min_tier=1)
        if cand:
            return cand[0]["unlocode"]
    return None


async def air_leg_estimate(apt_o: dict, apt_d: dict, m: dict, *, leg_seq: int) -> tuple[list[dict], dict, float]:
    eur_usd, fx = await bm.eur_per_usd()
    km = hubs.haversine_km(apt_o, apt_d) + 95.0
    usd_kg, src = await bm.air_usd_per_kg(apt_o.get("country"), apt_d.get("country"), km)
    src = {**src, "fx": {"EURUSD": fx.get("value"), "as_of": fx.get("as_of")}}
    ch = pricing.chargeable("AIR", m)
    brk = bm.air_break(ch["qty"])
    unit = usd_kg * brk * eur_usd
    lines = [_line("AIR_FREIGHT", "main", "PER_KG", ch["qty"], unit, leg_seq=leg_seq, source=src,
                   note=f"{ch['label']}" + (f", small-shipment break × {brk:g}" if brk != 1.0 else "") + ", index all-in incl. fuel/security")]
    if unit * ch["qty"] < 75.0:
        lines = [_line("AIR_FREIGHT", "main", "PER_SHIPMENT", 1, 75.0, leg_seq=leg_seq, source=src, note=f"{ch['label']}, minimum charge")]
    model = bm._src(None, kind="model", note="Typical airport handling and AWB fee", note_fi="Tyypillinen lentoasemakäsittely ja AWB-maksu")
    lines.append(_line("AWB", "origin", "PER_SHIPMENT", 1, 35.0, leg_seq=leg_seq, source=model, note=None))
    lines.append(_line("HANDLING_D", "destination", "PER_KG", ch["qty"], 0.15, leg_seq=leg_seq, source=model, note=apt_d.get("name")))
    days = 2 + (1 if max(apt_o.get("tier") or 2, apt_d.get("tier") or 2) >= 2 else 0) + math.ceil(km / 9000.0)
    return lines, src, days


async def rail_leg_estimate(t_o: dict, t_d: dict, m: dict, network: str, *, leg_seq: int) -> tuple[list[dict], dict, float, float]:
    km = round(hubs.haversine_km(t_o, t_d) * 1.35, 1)
    if hubs.middle_corridor(t_o, t_d):
        eur, src, days, km, segs = await bm.rail_middle_corridor(t_o, t_d)
        src = {**src, "segments": segs}
    else:
        eur, src, days = await bm.rail_eur_per_feu(network, t_o.get("country"), t_d.get("country"), km, False)
    lines = []
    for c in m["containers"]:
        f = 0.65 if c["type"].startswith("20") else (1.12 if c["type"] == "45HC" else 1.0)
        lines.append(_line("RAIL_FREIGHT", "main", "PER_CONTAINER", c["count"], eur * f, leg_seq=leg_seq, source=src,
                           note=f"{pricing.CONTAINERS[c['type']]['label']}, {bm.num_en(km)} km"))
        model = bm._src(None, kind="model", note="Typical terminal lift on/off", note_fi="Tyypillinen terminaalinosto ja -lasku")
        lines.append(_line("RAIL_LIFT", "origin", "PER_CONTAINER", c["count"], bm.RAIL_LIFT_EUR, leg_seq=leg_seq, source=model, note=t_o.get("name")))
        lines.append(_line("RAIL_LIFT", "destination", "PER_CONTAINER", c["count"], bm.RAIL_LIFT_EUR, leg_seq=leg_seq, source=model, note=t_d.get("name")))
    return lines, src, math.ceil(days), km


async def courier_estimate(origin: dict, dest: dict, m: dict, cargo: dict) -> tuple[list[dict], dict, int] | None:
    tariff = await bm.courier_price(origin.get("country"), dest.get("country"), m.get("weight_kg") or 0, m.get("volume_m3") or 0)
    if not tariff:
        tariff = await bm.courier_model(origin, dest, m.get("weight_kg") or 0, m.get("volume_m3") or 0)
    eur, src, days, label = tariff
    return [_line("COURIER", "main", "PER_SHIPMENT", 1, eur, leg_seq=1, source=src, note=label)], src, days


# ------------------------------------------------------------------------------------------------ routing
def _family_services(m: dict, cargo: dict, mode: str | None) -> list[tuple[str, str]]:
    """(mode, service) pairs the hub router may build for this cargo."""
    services, _extras = pricing.eligible_services(m, mode, cargo)
    out = []
    if m["is_fcl"]:
        if "FCL" in services:
            out.append(("sea", "FCL"))
        if "RAIL_FCL" in services:
            out.append(("rail", "RAIL_FCL"))
        if "HAULAGE" in services or (not mode or mode == "road"):
            out.append(("road", "ROAD_FTL"))
    else:
        if "LCL" in services:
            out.append(("sea", "LCL"))
        if "AIR" in services:
            out.append(("air", "AIR"))
        if "ROAD_FTL" in services:
            out.append(("road", "ROAD_FTL"))
        if "ROAD_LTL" in services:
            out.append(("road", "ROAD_LTL"))
        if (not mode or mode == "air") and courier_eligible(cargo, m):
            out.append(("courier", "COURIER"))
    return out


async def _port_candidates(place: dict) -> list[dict]:
    ports = await hubs.nearest("port", place, limit=8, max_km=PRECARRIAGE_MAX_KM)
    if not ports:
        return []
    # Own country's ports first (exports clear at home); a foreign port only when it is far closer or the place is
    # landlocked (Ouagadougou -> Lomé / Tema).
    domestic = [p for p in ports if p["country"] == place.get("country")]
    if domestic:
        ports = domestic + [p for p in ports if p["country"] != place.get("country") and p["km"] < 0.4 * domestic[0]["km"]]
        ports.sort(key=lambda p: p["km"])
    # nearest few, plus the nearest deep-sea gateway (tier <= 2) if the nearest are feeder ports
    out = ports[:3]
    if all((p.get("tier") or 3) >= 3 for p in out):
        gw = next((p for p in ports if (p.get("tier") or 3) <= 2), None)
        if gw:
            out.append(gw)
    return out


async def _airport_candidates(place: dict) -> list[dict]:
    big = await hubs.nearest("airport", place, limit=2, max_km=AIRPORT_MAX_KM[1], min_tier=1)
    if big:
        return big
    return await hubs.nearest("airport", place, limit=2, max_km=AIRPORT_MAX_KM[2])


async def plan(req: dict, m: dict) -> list[dict]:
    """Candidate routings (no prices yet), best few per (mode, service)."""
    origin, dest = req["origin"], req["destination"]
    cargo = req["cargo"]
    mode_req = req.get("mode")
    road_ok = hubs.road_connected(origin, dest)
    road_km = _km_road(origin, dest)
    gc_km = hubs.haversine_km(origin, dest)
    out: list[dict] = []
    for mode, service in _family_services(m, cargo, mode_req):
        if mode == "road":
            if road_ok and road_km <= ROAD_MAX_KM:
                out.append({"mode": "road", "service": service, "main": (origin, dest), "pre": None, "on": None, "km": road_km})
        elif mode == "courier":
            out.append({"mode": "courier", "service": service, "main": (origin, dest), "pre": None, "on": None, "km": gc_km})
        elif mode == "sea":
            if road_ok and road_km < SEA_MIN_KM_IF_ROAD and mode_req != "sea":
                continue
            pols, pods = await _port_candidates(origin), await _port_candidates(dest)
            combos = []
            for pol in pols:
                for pod in pods:
                    if pol["unlocode"] == pod["unlocode"]:
                        continue
                    pre, on_ = _km_road(origin, pol), _km_road(pod, dest)
                    proxy = (pre + on_) * 1.6 + hubs.haversine_km(pol, pod) * 0.08 + 250 * ((pol.get("tier") or 3) >= 3) \
                        + 250 * ((pod.get("tier") or 3) >= 3)
                    combos.append((proxy, pol, pod, pre, on_))
            combos.sort(key=lambda x: x[0])
            if road_ok:  # a sea routing between road-connected places must not be a long detour (Ulaanbaatar–Almaty via Karachi)
                combos = [c for c in combos if c[3] + c[4] + hubs.haversine_km(c[1], c[2]) * 1.3 <= 2.0 * road_km]
            for proxy, pol, pod, pre, on_ in combos[:2]:
                out.append({"mode": "sea", "service": service, "main": (pol, pod), "pre": pol if pre > 5 else None,
                            "on": pod if on_ > 5 else None, "km": None})
        elif mode == "air":
            if gc_km < AIR_MIN_KM and mode_req != "air":
                continue
            ao, ad = await _airport_candidates(origin), await _airport_candidates(dest)
            pairs = [(a, b) for a in ao for b in ad if a["code"] != b["code"]]
            pairs.sort(key=lambda p: _km_road(origin, p[0]) + _km_road(p[1], dest) - 40 * ((p[0].get("tier") or 2) == 1)
                       - 40 * ((p[1].get("tier") or 2) == 1))
            for a, b in pairs[:1]:
                out.append({"mode": "air", "service": "AIR", "main": (a, b), "pre": a if _km_road(origin, a) > 5 else None,
                            "on": b if _km_road(b, dest) > 5 else None, "km": None})
        elif mode == "rail":
            network = hubs.rail_network(origin, dest)
            if not network or road_km < RAIL_MIN_KM:
                continue
            to = await hubs.nearest("rail", origin, limit=3, max_km=250)
            td = await hubs.nearest("rail", dest, limit=3, max_km=250)
            to = [t for t in to if t["country"] == origin["country"]] or to
            td = [t for t in td if t["country"] == dest["country"]] or td
            if to and td and to[0]["code"] != td[0]["code"]:
                out.append({"mode": "rail", "service": "RAIL_FCL", "main": (to[0], td[0]),
                            "pre": to[0] if _km_road(origin, to[0]) > 5 else None,
                            "on": td[0] if _km_road(td[0], dest) > 5 else None, "network": network, "km": None})
    return out


# ------------------------------------------------------------------------------------------------ pricing a routing
async def _local_leg(tables: Tables, a: dict, b: dict, m: dict, *, family: str, kind: str, seq: int) -> dict:
    """Pre/on-carriage leg: domestic tariff (region card) when one covers it, else the road model."""
    km = _km_road(a, b)
    want = {"fcl": {"HAULAGE", "ROAD_FTL"}, "ftl": {"ROAD_FTL"}, "loose": {"ROAD_LTL"}}[family]
    code = "PICKUP" if kind == "pre" else "DELIVERY"
    hit = tables.best(want, a, b, m, km=km, kind="pre", freight_code="PICKUP", leg_seq=seq)
    if hit:
        card, lines = hit
        en, fi = pricing.line_text(("PICKUP_FI" if a.get("country") == "FI" else "PICKUP") if kind == "pre"
                                   else ("DELIVERY_FI" if b.get("country") == "FI" else "DELIVERY"))
        src = _card_source(card, tables.carriers)
        for ln in lines:
            ln.update(leg_seq=seq, stage="origin" if kind == "pre" else "destination", source=src, estimated=False)
            if ln["code"] == "PICKUP":
                ln.update(code=code, description=en, description_fi=fi)
        car = tables.carriers.get(card["carrier_id"], {})
        return {"lines": lines, "pricing": src, "carrier_id": card["carrier_id"], "carrier_name": car.get("name"),
                "transit_days": 1 if km <= 450 else 2, "km": km, "service": card["service"], "rate_card_id": card["id"],
                "_co2": car.get("co2_factor_g_tkm") or pricing.net.DEFAULT_CO2_G_TKM["HAULAGE"], "_valid_to": card.get("valid_to"),
                "firm": True}
    network = hubs.rail_network(a, b) if family == "fcl" and km > RAIL_PRECARRIAGE_KM and a.get("country") == b.get("country") else None
    if network:
        rail_km = round(hubs.haversine_km(a, b) * 1.35, 1)
        eur, src, rdays = await bm.rail_eur_per_feu(network, a.get("country"), b.get("country"), rail_km, False)
        stage = "origin" if kind == "pre" else "destination"
        lines = []
        for c in m["containers"]:
            f = 0.65 if c["type"].startswith("20") else 1.0
            lines.append(_line(code, stage, "PER_CONTAINER", c["count"], eur * f + 2 * bm.RAIL_LIFT_EUR, leg_seq=seq, source=src,
                               note=f"intermodal rail {bm.num_en(rail_km)} km incl. lifts, {pricing.CONTAINERS[c['type']]['label']}"))
        en, fi = pricing.line_text(code)
        for ln in lines:
            ln.update(description=en, description_fi=fi)
        return {"lines": lines, "pricing": src, "carrier_id": None, "carrier_name": None, "transit_days": math.ceil(rdays) + 1,
                "km": rail_km, "service": "RAIL_FCL", "rate_card_id": None, "mode": "rail",
                "_co2": pricing.net.DEFAULT_CO2_G_TKM["RAIL_FCL"], "_valid_to": None, "firm": False,
                "_need": {"mode": "rail", "service": "RAIL_FCL", "from": a, "to": b, "km": rail_km}}
    service = "ROAD_LTL" if family == "loose" else "ROAD_FTL"
    lines, src, days = await road_leg_estimate(a, b, m, service=service, kind=kind, leg_seq=seq, km=km)
    en, fi = pricing.line_text(code)
    for ln in lines:
        if ln["code"] == code:
            ln.update(description=en, description_fi=fi)
    return {"lines": lines, "pricing": src, "carrier_id": None, "carrier_name": None, "transit_days": days, "km": km,
            "service": service, "rate_card_id": None, "_co2": pricing.net.DEFAULT_CO2_G_TKM["ROAD_LTL" if family == "loose" else "HAULAGE"],
            "_valid_to": None, "firm": False,
            "_need": {"mode": "road", "service": "ROAD_FTL", "from": a, "to": b, "km": km} if family != "loose" else
                     {"mode": "road", "service": "ROAD_LTL", "from": a, "to": b, "km": km, "groupage": True}}


SERVICE_NAMES = {"FCL": ("FCL", "FCL"), "LCL": ("LCL", "LCL"), "AIR": ("Air", "Lentorahti"), "ROAD_FTL": ("Full truck", "Täysi kuorma"),
                 "ROAD_LTL": ("Road groupage", "Kappaletavara maanteitse"), "RAIL_FCL": ("Rail container", "Konttijuna"),
                 "COURIER": ("Express courier", "Pikakuriiri")}

async def price_routing(req, m, r, tables, settings, company, *, rejections=None):
    """Exclude an incomplete carrier candidate before it can claim route coverage.

    Retry the existing candidate selector with that card removed. Its normal
    benchmark branch stays an independent, unbookable estimate; no price is
    invented for the failing carrier. The loop is bounded by the loaded cards.
    """
    candidates = tables
    for _ in range(len(tables.cards) + 1):
        opt = await _price_routing_candidate(req, m, r, candidates, settings, company)
        if not opt or not opt.get("_public_surcharge_errors"):
            return opt
        rejection = opt.get("_not_quoted") or {}
        if rejections is not None:
            rejections.append(rejection)
        failed_id = opt.get("rate_card_id")
        remaining = [c for c in candidates.cards if c.get("id") != failed_id]
        if len(remaining) == len(candidates.cards):
            return None  # no removable candidate; never emit the incomplete option
        candidates = Tables(remaining, tables.carriers, date.fromisoformat(req["ready_date"]))
    return None


async def _price_routing_candidate(req: dict, m: dict, r: dict, tables: Tables, settings: dict, company: dict | None) -> dict | None:
    origin, dest = req["origin"], req["destination"]
    fam = "fcl" if m["is_fcl"] else ("ftl" if r["service"] == "ROAD_FTL" else "loose")
    legs, lines, needs = [], [], []
    seq = 1
    net = pricing.net
    main_carrier = {"code": "MARKET", "name": "Market estimate", "integration": {"type": "manual"}}

    if r.get("pre"):
        lg = await _local_leg(tables, origin, r["pre"], m, family="fcl" if m["is_fcl"] else ("loose" if r["mode"] in ("air", "sea") and not m["is_fcl"] and r["service"] != "ROAD_FTL" else fam),
                              kind="pre", seq=seq)
        lines += lg["lines"]
        legs.append({"seq": seq, "kind": "pre_carriage", "role": "pickup", "mode": lg.get("mode", "road"), "from": _loc(origin), "to": _loc(r["pre"]),
                     "carrier_id": lg["carrier_id"], "carrier_name": lg["carrier_name"], "transit_days": lg["transit_days"],
                     "distance_km": round(lg["km"]), "service": lg["service"], "rate_card_id": lg["rate_card_id"],
                     "pricing": lg["pricing"], "firm": lg["firm"], "_co2_parts": [(lg["km"], lg["_co2"])], "_valid_to": lg["_valid_to"]})
        if not lg["firm"] and lg.get("_need"):
            needs.append({**lg["_need"], "leg_seq": seq})
        seq += 1

    a, b = r["main"]
    main_seq = seq
    mode, service = r["mode"], r["service"]
    via: list[str] = []
    card = None
    if mode == "road":
        km = r["km"]
        svc = {"ROAD_LTL": {"ROAD_LTL"}, "ROAD_FTL": {"ROAD_FTL", "HAULAGE"} if m["is_fcl"] else {"ROAD_FTL"}}[service]
        hit = tables.best(svc, a, b, m, km=km, kind="main", freight_code="ROAD_FREIGHT", leg_seq=seq, allow_zone=service == "ROAD_LTL")
        if hit:
            card, ml = hit
            src = _card_source(card, tables.carriers)
            days = int(card.get("transit_days") or 0) + (max(1, math.ceil(km / float(card["km_per_day"]))) if card.get("km_per_day") else 0) or max(1, math.ceil(km / 600))
        else:
            ml, src, days = await road_leg_estimate(a, b, m, service=service, kind="main", leg_seq=seq, km=km)
            needs.append({"mode": "road", "service": service, "from": a, "to": b, "km": km, "leg_seq": seq,
                          "groupage": service == "ROAD_LTL"})
        dist = km
        co2 = net.DEFAULT_CO2_G_TKM["ROAD_LTL" if service == "ROAD_LTL" else "ROAD_FTL"]
    elif mode == "courier":
        dist = hubs.haversine_km(a, b) + 95
        hit = tables.best({"COURIER"}, a, b, m, km=dist, kind="main", freight_code="COURIER", leg_seq=seq)
        if hit:
            card, ml = hit
            src = _card_source(card, tables.carriers)
            days = int(card.get("transit_days") or 4)
        else:
            res = await courier_estimate(origin, dest, m, req["cargo"])
            if not res:
                return None
            ml, src, days = res
            needs.append({"mode": "air", "service": "COURIER", "from": a, "to": b, "km": dist, "leg_seq": seq})
        co2 = net.DEFAULT_CO2_G_TKM["AIR"]
    elif mode == "sea":
        dist = _km_sea(a, b)
        freight = "OCEAN_FREIGHT"
        hit = tables.best({service}, a, b, m, km=dist, kind="main", freight_code=freight, leg_seq=seq)
        if hit:
            card, ml = hit
            src = _card_source(card, tables.carriers)
            days = int(card.get("transit_days") or 0) or max(2, math.ceil(dist / 550.0)) + 2
            via = list(card.get("via") or [])
        else:
            ml, src, days, via = await sea_leg_estimate(a, b, m, service=service, leg_seq=seq, sea_km=dist,
                                                        sail_on=date.fromisoformat(req["ready_date"]))
            needs.append({"mode": "sea", "service": service, "from": a, "to": b, "km": dist, "leg_seq": seq})
        co2 = net.DEFAULT_CO2_G_TKM["FCL" if service == "FCL" else "LCL"]
    elif mode == "air":
        dist = hubs.haversine_km(a, b) + 95.0
        hit = tables.best({"AIR"}, a, b, m, km=dist, kind="main", freight_code="AIR_FREIGHT", leg_seq=seq)
        if hit:
            card, ml = hit
            src = _card_source(card, tables.carriers)
            days = int(card.get("transit_days") or 0) or 3
        else:
            ml, src, days = await air_leg_estimate(a, b, m, leg_seq=seq)
            needs.append({"mode": "air", "service": "AIR", "from": a, "to": b, "km": dist, "leg_seq": seq})
        co2 = net.DEFAULT_CO2_G_TKM["AIR_SHORT" if dist < 1500 else "AIR"]
    else:  # rail
        hit = tables.best({"RAIL_FCL"}, a, b, m, km=hubs.haversine_km(a, b) * 1.35, kind="main", freight_code="RAIL_FREIGHT", leg_seq=seq)
        if hit:
            card, ml = hit
            src = _card_source(card, tables.carriers)
            dist = hubs.haversine_km(a, b) * 1.35
            days = int(card.get("transit_days") or 0) or math.ceil(dist / 600)
        else:
            ml, src, days, dist = await rail_leg_estimate(a, b, m, r["network"], leg_seq=seq)
            needs.append({"mode": "rail", "service": "RAIL_FCL", "from": a, "to": b, "km": dist, "leg_seq": seq})
        co2 = net.DEFAULT_CO2_G_TKM["RAIL_FCL"]
    if not ml:
        return None
    main_firm = card is not None
    for ln in ml:
        ln.setdefault("source", src)
        ln.setdefault("estimated", not main_firm)
        if main_firm:
            ln["estimated"] = False
            ln["source"] = src
    lines += ml
    if card:
        car = tables.carriers.get(card["carrier_id"], {})
        main_carrier = car
        co2 = car.get("co2_factor_g_tkm") or co2
    legs.append({"seq": seq, "kind": "main", "role": "main", "mode": "air" if mode == "courier" else mode, "from": _loc(a), "to": _loc(b),
                 "carrier_id": card["carrier_id"] if card else None,
                 "carrier_name": (tables.carriers.get(card["carrier_id"], {}).get("name") if card else None),
                 "transit_days": int(days), "distance_km": round(dist), "service": service,
                 "rate_card_id": card["id"] if card else None, "via": via, "pricing": src, "firm": main_firm,
                 "_co2_parts": [(dist, co2)], "_valid_to": (card or {}).get("valid_to"),
                 "_weekdays": (card or {}).get("departure_weekdays") or []})
    seq += 1

    if r.get("on"):
        lg = await _local_leg(tables, r["on"], dest, m, family="fcl" if m["is_fcl"] else ("loose" if mode in ("air", "sea") and service != "ROAD_FTL" else fam),
                              kind="on", seq=seq)
        lines += lg["lines"]
        legs.append({"seq": seq, "kind": "on_carriage", "role": "delivery", "mode": lg.get("mode", "road"), "from": _loc(r["on"]), "to": _loc(dest),
                     "carrier_id": lg["carrier_id"], "carrier_name": lg["carrier_name"], "transit_days": lg["transit_days"],
                     "distance_km": round(lg["km"]), "service": lg["service"], "rate_card_id": lg["rate_card_id"],
                     "pricing": lg["pricing"], "firm": lg["firm"], "_co2_parts": [(lg["km"], lg["_co2"])], "_valid_to": lg["_valid_to"]})
        if not lg["firm"] and lg.get("_need"):
            needs.append({**lg["_need"], "leg_seq": seq})

    firm_legs = [lg["firm"] for lg in legs]
    firmness = "firm" if all(firm_legs) else ("partial" if main_firm else "estimate")
    name, name_fi = SERVICE_NAMES[service]
    hubs_txt = f"{a.get('name')} – {b.get('name')}" if mode in ("sea", "air", "rail") else ""
    opt = {
        "mode": "air" if mode == "courier" else mode, "service": service,
        "service_name": f"{name} via {hubs_txt}" if hubs_txt else name,
        "service_name_fi": f"{name_fi}, {hubs_txt}" if hubs_txt else name_fi,
        "carrier_id": card["carrier_id"] if card else None,
        "carrier_name": (main_carrier.get("name") if card else None) or ("Market estimate" if not card else None),
        "carrier_name_fi": None if card else "Markkina-arvio",
        "carrier_code": main_carrier.get("code"), "rate_card_id": card["id"] if card else None, "legs": legs, "via": via,
        "rate_source": (card or {}).get("source") or ("indicative" if card else "benchmark"),
        "on_time_pct": main_carrier.get("on_time_pct"),
        "_cargo_metrics": m, "_lines": lines, "_partial": [], "_carrier": main_carrier, "_card": card or {}, "_c2": None,
        "_dedupe": "|".join(["global", mode, service, a.get("unlocode") or a.get("code") or a.get("name") or "",
                             b.get("unlocode") or b.get("code") or b.get("name") or "", str(bool(m["is_fcl"]))]),
    }
    if m.get("containers") and not req["cargo"].get("containers"):
        opt["suggested_containers"] = m["containers"]
    opt = await pricing._finish_option(req, opt, settings, company)
    if opt.get("_not_quoted"):
        return opt
    opt.update(firmness=firmness, bookable=firmness == "firm", routing="hub" if mode in ("sea", "air", "rail") else "direct",
               price_basis={"firm": "rate_table", "partial": "rate_table_with_estimated_local_legs",
                            "estimate": "benchmark_estimate"}[firmness])
    if firmness != "firm":
        opt["warnings"].append(pricing._warn("option.estimate" if firmness == "estimate" else "option.partial_estimate",
                                             "Estimated from benchmark indices; the firm price follows from partner rates."
                                             if firmness == "estimate" else
                                             "Main carriage from our rate table; local transport is estimated until a partner rate arrives.",
                                             "info"))
    for need in needs:
        need["cargo"] = {**req["cargo"], "containers": m.get("containers") or [],
                         "load_type": "FCL" if m.get("is_fcl") else req["cargo"]["load_type"]}
        if need["service"] == "ROAD_FTL" and m.get("is_fcl"):
            need["cargo"]["ldm"] = pricing.TRAILER_LDM * _qty_containers(m)
    opt["_sourcing"] = needs
    return opt


# ------------------------------------------------------------------------------------------------ entry points
async def extend_options(req: dict, m: dict, settings: dict, company: dict | None, options: list[dict], *, not_quoted: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Options the hub router adds for (mode, service) pairs the rate-table engine left empty. (options, warnings)"""
    if not settings.get("global_quoting_enabled", True):
        return [], []
    covered = {(o["mode"], o["service"]) for o in options}
    covered_modes = {o["mode"] for o in options}
    ready = date.fromisoformat(req["ready_date"])
    tables = await Tables.load(ready)
    variants = [m]
    if not m["is_fcl"]:
        _svc, extras = pricing.eligible_services(m, req.get("mode"), req["cargo"])
        if extras:
            alt = dict(m)
            alt.update(containers=extras[0]["containers"], is_fcl=True,
                       teu=sum(pricing.CONTAINERS[c["type"]]["teu"] * c["count"] for c in extras[0]["containers"]))
            variants.append(alt)
    out: list[dict] = []
    warnings: list[dict] = []
    rejections: list[dict] = []
    for mm in variants:
        for r in await plan(req, mm):
            if (r["mode"], r["service"]) in covered or (r["mode"] == "road" and "road" in covered_modes and r["service"] == "ROAD_FTL"
                                                        and ("road", "ROAD_LTL") in covered):
                continue
            try:
                opt = await price_routing(req, mm, r, tables, settings, company, rejections=rejections)
            except Exception:  # noqa: BLE001 - one bad routing must not lose the others
                log.exception("routing %s %s failed", r.get("mode"), r.get("service"))
                continue
            if opt:
                out.append(opt)
    for rejected in rejections:
        if not_quoted is not None:
            not_quoted.append(rejected)
        warnings.append(pricing._warn("public_tariff.unavailable",
                                     "A carrier option was excluded because a required public surcharge is unavailable.",
                                     "warning", **{k:v for k,v in rejected.items() if k not in ("code", "reason")}))
    out = _dedupe(out)
    stale = await bm.staleness(int(settings.get("global_benchmark_stale_days", 21)))
    if stale and any(o["firmness"] != "firm" for o in out):
        oldest = min(s["as_of"] for s in stale)
        days = int(settings.get("global_benchmark_stale_days", 21))
        warnings.append(pricing._warn("benchmarks.stale", f"Some benchmark levels are older than {days} days (oldest {oldest}).",
                                      "info", oldest=oldest, days=days))
    return out, warnings


def _dedupe(options: list[dict]) -> list[dict]:
    """One option per (mode, service, firmness) unless the hubs differ by more than 60 km (Apapa vs Tin Can is one)."""
    kept: list[dict] = []
    for o in sorted(options, key=lambda x: x["total_cents"]):
        main = next((lg for lg in o["legs"] if lg["kind"] == "main"), None)
        twin = None
        for k in kept:
            if (k["mode"], k["service"], k["firmness"]) != (o["mode"], o["service"], o["firmness"]):
                continue
            km = next((lg for lg in k["legs"] if lg["kind"] == "main"), None)
            if not main or not km or o["mode"] == "road":
                twin = k
                break
            if hubs.haversine_km(main["from"], km["from"]) < 60 and hubs.haversine_km(main["to"], km["to"]) < 60:
                twin = k
                break
        if twin is None:
            kept.append(o)
    return kept


def prefer_firm(options: list[dict]) -> list[dict]:
    """After pricing.rank(): recommend the best firm option when one exists; estimates rank after firm options."""
    firm = [o for o in options if o.get("firmness", "firm") == "firm"]
    if firm and options and not options[0].get("firmness", "firm") == "firm" or (firm and not any(o.get("recommended") for o in firm)):
        for o in options:
            if o.get("recommended"):
                o["recommended"] = False
                o["tags"] = [t for t in o.get("tags") or [] if t != "best_value"]
                o["rank_reason"] = next((t for t in ("cheapest", "fastest", "lowest_co2") if t in o["tags"]), None)
        best = firm[0]
        best["recommended"] = True
        best["tags"] = ["best_value"] + [t for t in best.get("tags") or [] if t != "best_value"]
        best["rank_reason"] = "best_value"
    order = {"firm": 0, "partial": 1, "estimate": 2}
    return sorted(options, key=lambda o: (not o.get("recommended"), order.get(o.get("firmness", "firm"), 0)))


def sourcing_needs(options: list[dict]) -> list[dict]:
    """Legs to procure: every open leg of the recommended option and the main legs of the best option per mode."""
    needs: dict[str, dict] = {}
    best_mode: set[str] = set()
    for o in options:
        legs = o.pop("_sourcing", None) or []
        first_of_mode = (o["mode"], o["service"]) not in best_mode
        best_mode.add((o["mode"], o["service"]))
        for n in legs:
            if n["mode"] not in ("road", "sea", "air", "rail"):
                continue
            is_main = n["leg_seq"] == next((lg["seq"] for lg in o["legs"] if lg["kind"] == "main"), None)
            if not (o.get("recommended") or (first_of_mode and is_main)):
                continue
            a, b = n["from"], n["to"]
            lane = f"{n['mode']}:{(a.get('country') or '').upper()}>{(b.get('country') or '').upper()}"
            leg_cost = sum(ln.get("cost_cents") or 0 for ln in o.get("lines") or [] if ln.get("leg_seq") == n["leg_seq"])
            leg_cost += sum(x.get("amount_cents") or 0 for x in o.get("not_included") or [] if x.get("leg_seq") == n["leg_seq"])
            scope_key = f"{lane}:{n['service']}:{a['lat']}:{a['lon']}:{b['lat']}:{b['lon']}"
            cur = needs.get(scope_key)
            spec = {"lane": lane, "mode": n["mode"], "service": n["service"],
                    "origin": a.get("unlocode") or (a.get("country") or "").upper(),
                    "destination": b.get("unlocode") or (b.get("country") or "").upper(),
                    "origin_place": a.get("name"), "destination_place": b.get("name"), "km": round(n["km"]),
                    "origin_location": _loc(a), "destination_location": _loc(b), "cargo": n.get("cargo") or {},
                    "estimate_cost_cents": int(leg_cost), "option_id": o["id"], "leg_seq": n["leg_seq"],
                    "groupage": bool(n.get("groupage"))}
            if not cur or spec["estimate_cost_cents"] > cur["estimate_cost_cents"]:
                needs[scope_key] = spec
    return list(needs.values())


# ------------------------------------------------------------------------------------------------ procurement (s3)
def _procurement():
    try:
        from services import partner_network as pn
    except Exception:  # noqa: BLE001
        return None
    return pn if hasattr(pn, "request_lane_rates") else None


async def _eta_hours(mode: str, lane: str) -> tuple[float, str]:
    pn = _procurement()
    fn = getattr(pn, "expected_reply_hours", None) if pn else None
    if fn:
        try:
            r = await fn(mode, lane)
            if r and r.get("p90_hours"):
                return float(r["p90_hours"]), f"partner reply time p90 over {r.get('samples')} RFQs"
        except Exception:  # noqa: BLE001
            log.exception("expected_reply_hours failed")
    return float(DEFAULT_REPLY_HOURS.get(mode, 24)), "default reply time for the mode (no partner history yet)"


def _fmt_eta(at: datetime, lang: str) -> str:
    t = at.astimezone(HEL)
    return t.strftime("%-d.%-m.%Y klo %H.%M") if lang == "fi" else t.strftime("%-d %b %Y %H:%M") + " Helsinki time"


async def open_sourcing(q: dict, needs: list[dict], *, actor: dict | None = None) -> dict:
    """Ask s3 for every missing lane and record the state on the quote (mutates and returns q['sourcing'])."""
    settings = await get_settings()
    prev = q.get("sourcing") or {}
    has_estimates = any(o.get("firmness") in ("partial", "estimate") for o in q.get("options") or [])
    if not needs and not has_estimates:
        status = "complete" if prev.get("request_ids") else "none"
        q["sourcing"] = {**prev, "status": status, "legs_open": 0, "resolved_at": prev.get("resolved_at") or (now_iso() if prev.get("request_ids") else None),
                         **_messages(status, None, 0)}
        return q["sourcing"]
    pn = _procurement()
    if not settings.get("partner_network_enabled") or settings.get("autopilot_halted"):
        q["sourcing"] = {**prev, "status": "off", "legs_open": len(needs), "eta_at": None, "needs": needs,
                         "reason": "autopilot_halted" if settings.get("autopilot_halted") else "sourcing_switch_off", **_messages("off", None, len(needs))}
        return q["sourcing"]
    if not pn:
        q["sourcing"] = {**prev, "status": "unavailable", "legs_open": len(needs), "eta_at": None, "needs": needs,
                         "reason": "procurement_not_deployed", **_messages("unavailable", None, len(needs))}
        return q["sourcing"]
    ids = list(prev.get("request_ids") or [])
    lanes = []
    eta = None
    eta_basis = None
    errors = []
    for n in needs:
        cargo = {**(n.get("cargo") or q["request"].get("cargo") or {}), "service": n["service"],
                 "origin_place": n["origin_place"], "destination_place": n["destination_place"],
                 "origin_location": n.get("origin_location"), "destination_location": n.get("destination_location"),
                 "distance_km": n["km"], "groupage": n["groupage"]}
        containers = cargo.get("containers") or []
        equipment = {c["type"] for c in containers}
        cargo["equipment_type"] = next(iter(equipment)) if len(equipment) == 1 else None
        metrics = pricing.cargo_metrics(cargo)
        units = pricing.chargeable(n["service"], metrics)["qty"] or 1
        try:
            r = await pn.request_lane_rates(n["origin"], n["destination"], n["mode"], cargo,
                                            service=n["service"], max_cost_cents=max(1, int(n["estimate_cost_cents"] * 1.25 / units)),
                                            quote_id=q["id"], requested_by="global_quote")
        except Exception as exc:  # noqa: BLE001 - HTTPException / ValueError from s3: record and keep the estimate
            errors.append({"lane": n["lane"], "error": str(getattr(exc, "detail", exc))[:200]})
            continue
        if r.get("id") and r["id"] not in ids:
            ids.append(r["id"])
        lanes.append({"lane": r.get("lane") or n["lane"], "request_id": r.get("id"), "status": r.get("status"), "mode": n["mode"],
                      "origin": n["origin"], "destination": n["destination"], "leg": {k: n[k] for k in ("origin_place", "destination_place", "km")}})
        hours, basis = await _eta_hours(n["mode"], n["lane"])
        created = datetime.fromisoformat(r["created_at"]) if r.get("created_at") else now()
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        at = max(created + timedelta(hours=hours), now() + timedelta(minutes=30))
        if eta is None or at > eta:
            eta, eta_basis = at, basis
    if not lanes:
        q["sourcing"] = {**prev, "status": "unavailable", "legs_open": len(needs), "eta_at": None, "needs": needs, "errors": errors,
                         "reason": "procurement_refused", **_messages("unavailable", None, len(needs))}
        return q["sourcing"]
    no_cand = [ln for ln in lanes if ln["status"] in ("no_candidates", "off")]
    if lanes and all(ln["status"] == "off" for ln in lanes):
        q["sourcing"] = {**prev, "status": "off", "eta_at": None, "needs": needs, "request_ids": ids,
                         "reason": "procurement_paused", **_messages("off", None, len(needs))}
        return q["sourcing"]
    status = "unavailable" if len(no_cand) == len(lanes) else ("partial" if any(o.get("firmness") == "firm" for o in q.get("options") or []) else "requested")
    deadline = now() + timedelta(hours=int(settings.get("global_sourcing_deadline_hours", 72)))
    q["sourcing"] = {"status": status, "requested_at": prev.get("requested_at") or now_iso(), "resolved_at": None,
                     "eta_at": iso(eta) if eta and status != "unavailable" else None, "eta_basis": eta_basis,
                     "deadline_at": prev.get("deadline_at") or iso(deadline), "request_ids": ids, "lanes": lanes,
                     "legs_open": len(lanes), "errors": errors, "needs": needs,
                     **_messages(status, eta, len(lanes))}
    return q["sourcing"]


def _messages(status: str, eta: datetime | None, n: int) -> dict:
    if status == "requested" or status == "partial":
        when_fi = _fmt_eta(eta, "fi") if eta else "pian"
        when_en = _fmt_eta(eta, "en") if eta else "soon"
        return {"message_fi": f"Pyysimme puuttuvat hinnat kumppaneiltamme ({n} {'osuus' if n == 1 else 'osuutta'}). Sitova hinta "
                              f"arviolta {when_fi} mennessä; tarjous päivittyy itsestään. Siihen asti hinnat ovat merkittyjä arvioita.",
                "message_en": f"We asked our partners for the missing rates ({n} {'leg' if n == 1 else 'legs'}). Firm price expected by "
                              f"{when_en}; the quote updates by itself. Until then the prices are marked estimates."}
    if status == "off":
        return {"message_fi": "Hinnat ovat vertailuindekseihin perustuvia arvioita. Kumppanihaku ei ole käynnissä, joten sitovaa hintaa ei pyydetty.",
                "message_en": "Prices are estimates from benchmark indices. Partner sourcing is not running, so no firm rate was requested."}
    if status == "unavailable":
        return {"message_fi": "Emme saaneet tälle reitille sitovaa kumppanihintaa. Alla oleva hinta on päivätty arvio vertailuindekseistä.",
                "message_en": "We could not get a firm partner rate for this route. The price below is a dated estimate from benchmark indices."}
    if status == "complete":
        return {"message_fi": "Kumppanihinnat saapuivat ja tarjous on päivitetty.", "message_en": "Partner rates arrived and the quote was updated."}
    return {"message_fi": None, "message_en": None}


# ------------------------------------------------------------------------------------------------ re-pricing when rates land
async def requote(quote_id: str, *, reason: str) -> dict | None:
    from routes import quotes as rq

    q = await db.quotes.find_one({"id": quote_id}, NO_ID)
    if not q or q.get("status") in FINAL_STATUSES:
        return None
    prev_status = (q.get("sourcing") or {}).get("status")
    prev_best = (rq.recommended_option(q) or {}).get("total_cents")
    try:
        await rq.reprice_in_place(q, BOT)
    except Exception:  # noqa: BLE001
        log.exception("requote %s failed", quote_id)
        return None
    await db.quotes.replace_one({"id": quote_id}, dict(q))
    new_status = (q.get("sourcing") or {}).get("status")
    from core.audit import audit

    await audit(BOT, "quote_requoted_sourcing", "quote", quote_id, {"reason": reason, "sourcing": new_status, "revision": q.get("revision")},
                company_id=q.get("company_id"))
    if new_status != prev_status or (rq.recommended_option(q) or {}).get("total_cents") != prev_best:
        await emit("quote.sourcing_updated", {"quote": q, "previous_status": prev_status, "actor": BOT}, background=True)
        await _notify(q)
    return q


async def _notify(q: dict):
    """Tell the customer the firm price is ready (s6 mails e-mail-channel quotes itself)."""
    s = q.get("sourcing") or {}
    if q.get("source") == "email" or s.get("status") not in ("complete", "partial", "unavailable"):
        return
    if s.get("status") == "partial" and not any(o.get("bookable") for o in q.get("options") or []):
        return
    from services.agents.quote_agent import outbound_allowed
    if not await outbound_allowed(q):
        return
    from core.email import render, send_email
    from core.notify import bi, notify_company, same
    from core.web import base_url
    from routes.quotes import route_label

    ready = s["status"] in ("complete", "partial")
    if q.get("company_id"):
        title = bi(f"Tarjouksen {q.get('number')} sitova hinta on valmis", f"Firm price for quote {q.get('number')} is ready") if ready else \
            bi(f"Tarjous {q.get('number')}: sitovaa hintaa ei saatu", f"Quote {q.get('number')}: no firm rate available")
        await notify_company(q["company_id"], title, same(route_label(q["request"])), link=f"/portal/quotes/{q['id']}",
                             email=True, kind="quote", base_url=base_url(None))
        return
    to = q.get("notify_email")
    if not to:
        return
    lang = q.get("notify_locale") or "fi"
    link = f"{base_url(None)}/quote?id={q['id']}&token={q.get('claim_token')}"
    if lang == "fi":
        subject = "Sitova hinta on valmis" if ready else "Sitovaa hintaa ei saatu"
        lines = [route_label(q["request"]), s.get("message_fi") or ""]
        label = "Avaa tarjous"
    else:
        subject = "Your firm price is ready" if ready else "No firm rate available"
        lines = [route_label(q["request"]), s.get("message_en") or ""]
        label = "Open the quote"
    markup, text = render(subject, lines, action_url=link, action_label=label)
    await send_email(to, f"Huolitsija · {subject}", markup, text, tag="quote_sourcing", ref={"type": "quote", "id": q["id"]})
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"sourcing.notified_at": now_iso()}})


@on("partner.rate_loaded")
async def _rate_loaded(payload: dict):
    req = payload.get("request") or {}
    for qid in req.get("quote_ids") or []:
        await enqueue("global.requote", {"quote_id": qid, "lane": req.get("lane")},
                      key=f"global:requote:{qid}:{(payload.get('rate_card') or {}).get('id') or req.get('updated_at')}")


@schedule("global.sourcing_watch", job="global.sourcing_watch", every_seconds=300,
          label_fi="Tarjousten kuljetushintojen seuranta", label_en="Quote carrier-rate follow-up")
@job("global.sourcing_watch", agent=True, label_fi="Tarjousten kuljetushintojen seuranta", label_en="Quote carrier-rate follow-up")
async def sourcing_sweep(j=None):
    """Safety net for missed events and the deadline: re-read s3's lane requests of quotes still waiting."""
    pn = _procurement()
    cutoff = now_iso()
    async for q in db.quotes.find({"sourcing.status": {"$in": ["requested", "partial"]}, "status": {"$nin": list(FINAL_STATUSES)}},
                                  {"_id": 0, "id": 1, "sourcing": 1, "priced_at": 1}).limit(200):
        s = q.get("sourcing") or {}
        loaded = False
        if pn and hasattr(pn, "get_lane_request"):
            for rid in s.get("request_ids") or []:
                r = await pn.get_lane_request(rid)
                if r and r.get("status") == "rate_loaded" and (r.get("updated_at") or "") > (q.get("priced_at") or ""):
                    loaded = True
        if loaded:
            await requote(q["id"], reason="sweep: rate loaded")
            continue
        if s.get("deadline_at") and s["deadline_at"] < cutoff:
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"sourcing.status": "unavailable", "sourcing.reason": "deadline",
                                                                  "sourcing.resolved_at": now_iso(), "sourcing.eta_at": None,
                                                                  **{f"sourcing.{k}": v for k, v in _messages("unavailable", None, 0).items()}}})
            full = await db.quotes.find_one({"id": q["id"]}, NO_ID)
            await emit("quote.sourcing_updated", {"quote": full, "previous_status": s.get("status"), "actor": BOT}, background=True)
            await _notify(full)


@job("global.requote", agent=True, label_fi="Tarjouksen hinnan päivitys", label_en="Update quote pricing")
async def requote_job(j):
    payload = j["payload"]
    return await requote(payload["quote_id"], reason=f"partner rate {payload.get('lane')}")


# ------------------------------------------------------------------------------------------------ public API (s6)
async def preview(request: dict, *, company: dict | None = None) -> dict:
    return await pricing.quote_pricing(request, company=company, strict=False)


async def quote_any(request: dict, *, company: dict | None, user: dict | None = None, source: str = "email", send: bool = True,
                    notify_email: str | None = None, locale: str = "fi", ai: dict | None = None) -> dict:
    """Create and store a quote for any city pair (contract in shared/interfaces.md, s5)."""
    from routes import quotes as rq

    if company:
        q = await rq.create_quote(request, user=user or BOT, company=company, source=source, send=send, ai=ai)
        if notify_email:
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"notify_email": notify_email, "notify_locale": locale}})
            q.update(notify_email=notify_email, notify_locale=locale)
        return q
    from core.security import random_token
    from routes.public_quote import CLAIM_DAYS

    settings = await get_settings()
    priced = await pricing.quote_pricing(request, company=None, strict=False)
    q = rq._base_doc(priced, source=source, settings=settings)
    q.update(claim_token=random_token(24), claim_expires_at=iso(datetime.now(timezone.utc) + timedelta(days=CLAIM_DAYS)),
             notify_email=notify_email, notify_locale=locale if locale in ("fi", "en") else "fi", ai=ai)
    await open_sourcing(q, priced.get("sourcing_needs") or [])
    await db.quotes.insert_one(dict(q))
    from core.audit import audit

    await audit(user or BOT, "quote_created", "quote", q["id"], {"status": q["status"], "options": len(q["options"]),
                                                                 "source": source, "sourcing": q["sourcing"]["status"]})
    await emit("quote.created", {"quote": q, "actor": user or BOT})
    return q
