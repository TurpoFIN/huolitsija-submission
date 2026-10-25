"""Carrier bookings (s2 autopilot operations): rank the carriers for a shipment leg and book the leg with one of them.

One code path for every channel; only the adapter differs:

    sandbox         SandboxAdapter       sandbox companies (core.sandbox) or CARRIER_BOOKING_MODE=sandbox: confirms at once
                                         with clearly synthetic references, every result marked sandbox=True
    live            DcsaBookingAdapter   CARRIER_BOOKING_MODE=live and the carrier's DCSA integration is live
                                         (services.carriers.integration_status): DCSA Booking 2.0 POST/GET/PATCH /v2/bookings
    partner_portal  PartnerPortalAdapter the carrier has active partner users: they are asked to accept the leg in the partner
                                         portal; POST /api/partner/shipments/{id}/legs/{seq}/accept confirms the booking
    log             LogAdapter           a bilingual booking request e-mail to carrier.contact_email (core.email; with
                                         EMAIL_PROVIDER=log it is only stored in email_outbox); ops or an inbound e-mail
                                         confirms it with confirm_booking(). Nothing is ever invented.

CARRIER_BOOKING_MODE: log (default: never call a carrier API), live (use live adapters where configured), sandbox.
Live DCSA env per carrier (CODE as in services.carriers.integration_env_names): CARRIER_<CODE>_BASE_URL, _API_KEY,
_ENABLED, optional _BOOKING_URL (booking base when it differs from the schedules base) and _BOOKING_AUTH_HEADER/_BOOKING_AUTH_VALUE (optional provider-specific authentication) and
_CONTRACT_REF (the service
contract or quotation reference the carrier priced us under; DCSA requires one of them).

Public API (the carrier agent calls these):
    await rank_carriers(shipment, leg, *, exclude=frozenset()) -> {"ranked": [...], "choice": row | None, "confidence"}
    await book_leg(shipment_id, leg_seq, carrier_id, actor, *, reason="", cost_cents=None) -> carrier_bookings doc
    await confirm_booking(booking_id, *, carrier_ref, actor, details=None) -> doc
    await poll_booking(booking_id) -> doc
    await cancel_booking(booking_id, actor, reason) -> doc
    await rebook_leg(shipment_id, leg_seq, actor, *, reason, exclude_carrier_ids=(), not_before=None) -> dict
    await leg_cost_cents(shipment, leg) -> int | None
    await adapter_for(carrier, company, *, shipment=None, leg=None) -> CarrierBookingAdapter
    dcsa_booking_request(shipment, leg, carrier, *, booking_id, contract_ref=None) -> DCSA Booking 2.0 CreateBooking body

Events: carrier.booked [shipment, booking] on confirmation; carrier.booking_failed [shipment, booking] when a booking
fails or the carrier rejects it; carrier.booking_cancelled [shipment, booking].
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import date, datetime, timedelta

import httpx
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from core import config
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.email import render, send_email
from core.events import emit
from core.notify import bi, notify_user
from core.sandbox import is_sandbox_company
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import carriers as net
from services import pricing
from services import shipments as svc

log = logging.getLogger("carrier_booking")

register_index("carrier_bookings", "id", unique=True)
register_index("carrier_bookings", [("shipment_id", 1), ("leg_seq", 1), ("created_at", -1)])
register_index("carrier_bookings", [("status", 1), ("updated_at", -1)])
# Set only while a booking is requested/confirmed: one active booking per shipment + leg + carrier, even under races.
register_index("carrier_bookings", "active_key", unique=True, sparse=True)
register_index("carrier_test_events", [("shipment_id", 1), ("booking_id", 1), ("at", 1)])

STATUSES = ("requested", "confirmed", "rejected", "cancelled", "failed")
ACTIVE = ("requested", "confirmed")
CHANNELS = ("sandbox", "live", "partner_portal", "log")
READINESS = {"live": 1.0, "sandbox": 0.7, "partner_portal": 0.7, "log": 0.4}
WEIGHTS = {"cost": 0.45, "reliability": 0.25, "transit": 0.15, "readiness": 0.15}
SAILING_DEFAULT_DAYS = {"sea": 7, "road": 1, "air": 1, "rail": 1}
FREIGHT_STAGES = ("origin", "main", "destination")
EVENT_SOURCE = {"sandbox": "sandbox", "live": "carrier_api", "partner_portal": "partner", "log": "manual"}
LEG_DETAIL_KEYS = ("vessel", "voyage", "transport_doc_no", "etd", "eta", "flight", "train", "truck_plate", "container_numbers", "nationality", "identification_type", "exit_office")

CHANNEL_TEXT = {"live": ("rajapinta (DCSA)", "API (DCSA)"), "sandbox": ("testiympäristö", "sandbox"),
                "partner_portal": ("kumppaniportaali", "partner portal"), "log": ("sähköposti", "e-mail")}
MODE_TEXT = {"sea": ("merikuljetus", "sea freight"), "road": ("maantiekuljetus", "road freight"),
             "air": ("lentorahti", "air freight"), "rail": ("rautatiekuljetus", "rail freight")}
DOC_TEXT = {"sea": ("konossementti", "B/L"), "road": ("CMR-rahtikirja", "CMR consignment note"),
            "air": ("lentorahtikirja (AWB)", "air waybill (AWB)"), "rail": ("CIM-rahtikirja", "CIM consignment note")}
PACKAGE_FI = {"pallet_eur": "EUR-lava", "pallet_fin": "FIN-lava", "carton": "pahvilaatikko", "crate": "puulaatikko",
              "other": "kolli"}
# UN/ECE Recommendation 21 package codes, ISO 6346 size-type codes
PACKAGE_CODE = {"pallet_eur": "PX", "pallet_fin": "PX", "carton": "CT", "crate": "CR", "other": "PK"}
ISO_EQUIPMENT = {"20GP": "22G1", "40GP": "42G1", "40HC": "45G1", "45HC": "L5G1", "20RF": "22R1", "40RF": "45R1"}
INCOTERMS = {"EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"}
# IATA airline prefixes of the seeded air carriers (sandbox AWBs use them; any other carrier gets the unassigned 000)
AWB_PREFIX = {"FINNAIR": "105", "LUFTHANSA": "020", "TURKISH": "235", "QATAR": "157"}
DCSA_STATUS = {"RECEIVED": "requested", "PENDING_UPDATE": "requested", "UPDATE_RECEIVED": "requested",
               "CONFIRMED": "confirmed", "PENDING_AMENDMENT": "confirmed", "COMPLETED": "confirmed",
               "REJECTED": "rejected", "DECLINED": "rejected", "CANCELLED": "cancelled"}


def _t(code: str, fi: str, en: str) -> dict:
    """A reason / status text in both UI languages (Finnish first)."""
    return {"code": code, "fi": fi, "en": en}


def booking_mode() -> str:
    m = config.env("CARRIER_BOOKING_MODE", "log").lower()
    return m if m in ("log", "live", "sandbox") else "log"


def _code(carrier: dict) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(carrier.get("code") or "").upper()) or "CARRIER"


def _env_prefix(carrier: dict) -> str:
    return f"CARRIER_{net.integration_status(carrier)['code']}"


def _digits(seed: str, n: int, salt: str = "") -> str:
    h = int(hashlib.sha1(f"{salt}:{seed}".encode()).hexdigest(), 16)
    return str(h % (10 ** n)).zfill(n)


def _active_key(shipment_id: str, seq: int, carrier_id: str) -> str:
    return f"{shipment_id}:{int(seq)}:{carrier_id}"


def _eur(cents: int | None, lang: str) -> str:
    s = f"{(cents or 0) / 100:,.2f}"
    return f"{s.replace(',', ' ').replace('.', ',')} €" if lang == "fi" else f"EUR {s}"


def _kg(v, lang: str) -> str:
    s = f"{float(v):,.0f}"
    return f"{s.replace(',', ' ')} kg" if lang == "fi" else f"{s} kg"


def _when(v, lang: str) -> str:
    dt = parse_iso(v) if v else None
    if not dt:
        return "—"
    return f"{dt.day}.{dt.month}.{dt.year} klo {dt:%H.%M} UTC" if lang == "fi" else f"{dt:%Y-%m-%d %H:%M} UTC"


def _day(v, lang: str) -> str:
    dt = parse_iso(v) if v else None
    if not dt:
        return "—"
    return f"{dt.day}.{dt.month}.{dt.year}" if lang == "fi" else f"{dt:%Y-%m-%d}"


def _place_label(loc: dict | None) -> str:
    if not loc:
        return "—"
    name = loc.get("city") or loc.get("name") or loc.get("unlocode") or "—"
    code = loc.get("unlocode")
    return f"{name} ({code})" if code and code not in name else name


def _address_line(addr: dict | None) -> str | None:
    if not addr:
        return None
    parts = [addr.get("street"), " ".join(x for x in [addr.get("postal_code") or addr.get("postcode") or addr.get("zip"),
                                                      addr.get("city")] if x), addr.get("country")]
    return ", ".join(p for p in parts if p) or None


def _leg(shipment: dict, seq: int) -> dict:
    leg = next((lg for lg in shipment.get("legs") or [] if int(lg.get("seq", -1)) == int(seq)), None)
    if not leg:
        raise HTTPException(404, f"Shipment {shipment.get('reference')} has no leg {seq}.")
    return leg


async def _shipment(shipment_id: str) -> dict:
    s = await db.raw.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


async def _booking(booking_id: str) -> dict:
    b = await db.raw.carrier_bookings.find_one({"id": booking_id}, NO_ID)
    if not b:
        raise HTTPException(404, "Carrier booking not found")
    return b


_indexes_ready = False


async def _ensure_indexes():
    """The unique active_key index is what makes book_leg race-safe; create it even if startup has not run yet."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await db.raw.carrier_bookings.create_index("active_key", unique=True, sparse=True)
        await db.raw.carrier_bookings.create_index("id", unique=True)
        _indexes_ready = True
    except Exception:  # noqa: BLE001 - an index conflict must not stop bookings; the find-first check still applies
        log.exception("carrier_bookings index creation failed")


# ------------------------------------------------------------------------------------------------
# Cost of a leg: accepted quote option lines first, then the rate card
# ------------------------------------------------------------------------------------------------
async def _accepted_option(shipment: dict) -> dict | None:
    if not shipment.get("quote_id"):
        return None
    q = await db.raw.quotes.find_one({"id": shipment["quote_id"]}, {"_id": 0, "options": 1, "accepted_option_id": 1, "request": 1})
    if not q:
        return None
    oid = shipment.get("option_id") or q.get("accepted_option_id")
    option = next((o for o in q.get("options") or [] if o.get("id") == oid), None)
    if option and not option.get("legs") and option.get("mode") == "road":
        req = q.get("request") or {}
        def point(value):
            return value if isinstance(value, dict) else {"unlocode": value}
        option = {**option, "legs": [{"seq": 1, "mode": "road", "from": point(req.get("origin")),
                   "to": point(req.get("destination")), "carrier_id": option.get("carrier_id"),
                   "rate_card_id": option.get("rate_card_id"), "ferry_coverage": option.get("ferry_coverage")}]}
    return option


def _endpoint_gap_km(a: dict | None, b: dict | None) -> float:
    a, b = a or {}, b or {}
    if a.get("unlocode") and a.get("unlocode") == b.get("unlocode"):
        return 0.0
    if a.get("lat") is not None and b.get("lat") is not None:
        return net.haversine_km(a, b)
    return 5000.0


def _option_leg_for(option: dict | None, leg: dict) -> dict | None:
    """The quote option leg a shipment leg was built from: same carrier first, else the same mode and places
    (a leg whose carrier was changed after booking still maps to the leg the customer accepted)."""
    if leg.get("quote_leg_seq") is not None:
        return next((ol for ol in (option or {}).get("legs") or [] if ol.get("seq") == leg["quote_leg_seq"]
                     and ol.get("mode", leg.get("mode")) == leg.get("mode")), None)
    legs = [ol for ol in (option or {}).get("legs") or [] if ol.get("mode", leg.get("mode")) == leg.get("mode")]
    if not legs:
        return None
    same = [ol for ol in legs if leg.get("carrier_id") and ol.get("carrier_id") == leg.get("carrier_id")]

    def gap(ol):
        return _endpoint_gap_km(ol.get("from"), leg.get("from")) + _endpoint_gap_km(ol.get("to"), leg.get("to"))

    best = min(same or legs, key=gap)
    if not same and gap(best) > 300:
        return None
    return best


def _quoted_cost(shipment: dict, leg: dict, option: dict | None, carrier_id: str | None) -> int | None:
    """Freight cost of the option leg quoted with this carrier, shared by distance when the option leg was split into
    several shipment legs (a road leg around a ferry)."""
    ol = _option_leg_for(option, leg)
    if not ol or not carrier_id or ol.get("carrier_id") != carrier_id:
        return None
    lines = [ln for ln in option.get("lines") or [] if ln.get("leg_seq") == ol.get("seq")
             and ln.get("stage") in FREIGHT_STAGES and not ln.get("is_pass_through")]
    if not lines:
        return None
    total = sum(int(ln["cost_cents"]) if ln.get("cost_cents") is not None
                else int(round(float(ln.get("unit_cost_cents") or 0) * float(ln.get("qty") or 0))) for ln in lines)
    siblings = [lg for lg in shipment.get("legs") or [] if lg.get("carrier_id") == leg.get("carrier_id")
                and (_option_leg_for(option, lg) or {}).get("seq") == ol.get("seq")]
    if len(siblings) > 1 and not leg.get("booking_group"):
        km = sum(float(lg.get("distance_km") or 0) for lg in siblings)
        share = float(leg.get("distance_km") or 0) / km if km else 1 / len(siblings)
        total = total * share
    return int(round(total))


def booking_leg(shipment: dict, leg: dict) -> dict:
    """The purchase route sent to the road supplier; drawing legs keep their own modes and operators."""
    group = leg.get("booking_group")
    if not group:
        return leg
    parts = [_leg(shipment, seq) for seq in group["leg_seqs"]]
    return {**leg, "mode": "road", "carrier_id": group["carrier_id"], "from": parts[0]["from"],
            "to": parts[-1]["to"], "etd": parts[0].get("etd"), "eta": parts[-1].get("eta"),
            "distance_km": sum(float(p.get("distance_km") or 0) for p in parts if p["mode"] == "road"),
            "covered_legs": [{"seq": p["seq"], "mode": p["mode"], "from": p["from"], "to": p["to"],
                              "operator": (p.get("ferry_crossing") or {}).get("operator")} for p in parts]}


async def _group_covered(shipment: dict, leg: dict, option: dict | None) -> bool:
    group = leg.get("booking_group") or {}
    ol = _option_leg_for(option, leg)
    if not group or not ol or ol.get("mode") != "road" or ol.get("carrier_id") != group.get("carrier_id"):
        return False
    if _endpoint_gap_km(ol.get("from"), leg.get("from")) > 3 or _endpoint_gap_km(ol.get("to"), leg.get("to")) > 3:
        return False
    coverage = ol.get("ferry_coverage")
    if not coverage:
        card = await db.raw.rate_cards.find_one({"id": ol["rate_card_id"]}, NO_ID) if ol.get("rate_card_id") else None
        coverage = pricing.ferry_coverage(card or {}, [ln for ln in (option or {}).get("lines") or []
                                                     if ln.get("leg_seq") == ol.get("seq")])
    parts = [_leg(shipment, seq) for seq in group.get("leg_seqs") or []]
    all_parts = [p for p in shipment.get("legs") or [] if p.get("quote_leg_seq") == ol.get("seq")]
    quoted_crossings = [(s["from"], s["to"]) for s in ol.get("segments") or [] if s.get("mode") == "sea"]
    actual_crossings = [((p.get("from") or {}).get("unlocode"), (p.get("to") or {}).get("unlocode"))
                        for p in parts if p.get("ferry_crossing")]
    return bool(coverage.get("included") is True and parts and parts[0]["seq"] == leg["seq"]
                and [p["seq"] for p in all_parts] == group.get("leg_seqs")
                and (not quoted_crossings or quoted_crossings == actual_crossings)
                and all(p.get("quote_leg_seq") == ol.get("seq") for p in parts)
                and all(p.get("carrier_id") == group["carrier_id"] for p in parts if p["mode"] == "road")
                and all(_endpoint_gap_km(a.get("to"), b.get("from")) <= 3 for a, b in zip(parts, parts[1:])))


async def guard_started_group(shipment: dict, leg: dict | None = None, *, operation: str) -> None:
    """A group is one purchase: check every drawing leg before *any* purchase mutation or carrier call.

    A person must arrange the remaining carriage with the supplier once any part has started. The pending
    decision is in-app only and deliberately has no automatic booking/cancellation action on approval.
    """
    if leg and leg.get("booking_parent_seq"):
        leg = _leg(shipment, leg["booking_parent_seq"])
    parents = [leg] if leg else shipment.get("legs") or []
    for parent in parents:
        group = parent.get("booking_group") or {}
        parts = [_leg(shipment, seq) for seq in group.get("leg_seqs") or []]
        started = [p["seq"] for p in parts if p.get("atd") or p.get("ata") or p.get("status") in ("in_progress", "completed")]
        if not started:
            continue
        from core.agent_runtime import decide, policy_reason
        en = "Group booking already in transit; ferry leg must be rescheduled with the carrier by a person"
        fi = "Ryhmävarauksen kuljetus on jo alkanut; henkilön on sovittava lauttaosuuden muutos kuljetusliikkeen kanssa"
        await decide("carrier_agent", "change_started_group", subject=("shipment", shipment["id"]),
                     company_id=shipment.get("company_id"), inputs={"operation": operation, "started_leg_seqs": started},
                     extra_reasons=[policy_reason("group_booking_in_transit", fi, en)],
                     approval_kind="carrier_booking", title=en, title_fi=fi, link=f"/ops/shipments/{shipment['id']}",
                     approval_data={"shipment_id": shipment["id"], "leg_seq": parent["seq"], "reason": en,
                                    "manual_group_change": True, "blocked_operation": operation, "notify_owner_email": False})
        raise HTTPException(409, en)


def _metrics(shipment: dict) -> tuple[dict, dict]:
    raw = dict(shipment.get("cargo") or {})
    try:
        cargo = pricing.normalize_cargo(raw)
    except pricing.QuoteInputError:
        containers = [c for c in raw.get("containers") or [] if str(c.get("type") or "").upper() in pricing.CONTAINERS]
        cargo = {"load_type": "FCL" if containers else "loose", "weight_kg": raw.get("weight_kg") or 1000,
                 "volume_m3": raw.get("volume_m3"), "ldm": raw.get("ldm"), "packages": raw.get("packages"),
                 "package_type": raw.get("package_type"), "containers": [{"type": str(c["type"]).upper(), "count": int(c.get("count") or 1)}
                                                                         for c in containers],
                 "dangerous_goods": bool(raw.get("dangerous_goods")), "dg": raw.get("dg")}
    return cargo, pricing.cargo_metrics(cargo)


def _card_cost(card: dict, leg: dict, m: dict) -> int | None:
    km = float(leg.get("distance_km") or 0) or net.leg_distance_km(card.get("mode") or leg.get("mode"), leg.get("from") or {},
                                                                   leg.get("to") or {})
    lines = pricing.price_card(card, m, km=km, kind="main", leg_seq=int(leg.get("seq") or 1),
                               freight_code=pricing.FREIGHT_CODE.get(card.get("service"), "MANUAL"))
    if not lines:
        return None
    return int(round(sum(ln["unit_cost_cents"] * ln["qty"] for ln in lines)))


def _card_transit(card: dict, leg: dict) -> int | None:
    days = int(card.get("transit_days") or 0)
    if card.get("km_per_day") and leg.get("distance_km"):
        days += max(1, math.ceil(float(leg["distance_km"]) / float(card["km_per_day"])))
    return days or None


def _planned_days(leg: dict) -> int | None:
    a, b = parse_iso(leg.get("etd")), parse_iso(leg.get("eta"))
    if not a or not b or b <= a:
        return None
    return max(1, math.ceil((b - a).total_seconds() / 86400))


def _leg_day(leg: dict) -> date:
    etd = parse_iso(leg.get("etd"))
    today = now().date()
    return etd.date() if etd and etd.date() > today else today


def _place(loc: dict | None) -> dict | None:
    if not loc or loc.get("lat") is None:
        return None
    country = loc.get("country") or (loc.get("unlocode") or "")[:2] or None
    return {**loc, "country": country}


async def _lane_cards(shipment: dict, leg: dict, cargo: dict, m: dict) -> dict[str, list[dict]]:
    """carrier_id -> [{card, state valid|expired|upcoming, cost_cents, transit_days}] for cards that serve this leg
    directly (UN/LOCODE, included radius, groupage zone or country region) with a service that fits the cargo."""
    mode = leg.get("mode") or shipment.get("mode")
    a, b = _place(leg.get("from")), _place(leg.get("to"))
    if not a or not b or not a.get("country") or not b.get("country"):
        return {}
    services, _extra = pricing.eligible_services(m, mode, cargo)
    cards = [c async for c in db.raw.rate_cards.find({"archived": {"$ne": True}, "mode": mode,
                                                      "status": {"$nin": ["pending", "rejected", "revoked"]}}, NO_ID)]
    codes = set()
    for c in cards:
        codes |= {c["origin"].get("unlocode"), c["destination"].get("unlocode")}
    await pricing.load_network_coordinates({x for x in codes if x})
    day = _leg_day(leg)
    out: dict[str, list[dict]] = {}
    for c in cards:
        if services and c.get("service") not in services:
            continue
        try:
            oc, dc = pricing.cover(a, c, "origin"), pricing.cover(b, c, "destination")
        except (KeyError, TypeError, ValueError):
            continue
        if not oc or not dc or oc[0] == "haul" or dc[0] == "haul":
            continue
        if pricing.card_valid_on(c, day):
            state = "valid"
        elif c.get("valid_to") and date.fromisoformat(c["valid_to"][:10]) < day:
            state = "expired"
        else:
            state = "upcoming"
        out.setdefault(c["carrier_id"], []).append({"card": c, "state": state, "cost_cents": _card_cost(c, leg, m),
                                                    "transit_days": _card_transit(c, leg)})
    return out


def _best_card(rows: list[dict]) -> dict | None:
    valid = [r for r in rows if r["state"] == "valid" and r["cost_cents"] is not None]
    return min(valid, key=lambda r: r["cost_cents"]) if valid else None


async def leg_cost_cents(shipment: dict, leg: dict) -> int | None:
    """Our purchase cost for this leg with its current carrier: the accepted quote option's freight lines for that leg
    and carrier, else the carrier's rate card for the leg (the card the option used, then its best valid lane card).
    None when neither is known. Async: it reads the quote and the rate cards."""
    if leg.get("booking_parent_seq"):
        return None  # Included in the parent's purchase; never create another spend for the drawing leg.
    leg = booking_leg(shipment, leg)
    option = await _accepted_option(shipment)
    cid = leg.get("carrier_id")
    cost = _quoted_cost(shipment, leg, option, cid)
    if cost is not None or not cid:
        return cost
    cargo, m = _metrics(shipment)
    ol = _option_leg_for(option, leg)
    if ol and ol.get("carrier_id") == cid and ol.get("rate_card_id"):
        card = await db.raw.rate_cards.find_one({"id": ol["rate_card_id"]}, NO_ID)
        if card:
            c = _card_cost(card, leg, m)
            if c is not None:
                return c
    best = _best_card((await _lane_cards(shipment, leg, cargo, m)).get(cid) or [])
    return best["cost_cents"] if best else None


async def _carrier_cost(shipment: dict, leg: dict, carrier_id: str) -> int | None:
    """Purchase cost of this leg with a given carrier: the quoted cost when it is the quoted carrier, else its card."""
    option = await _accepted_option(shipment)
    ol = _option_leg_for(option, leg)
    if ol and ol.get("carrier_id") == carrier_id:
        cost = _quoted_cost(shipment, {**leg, "carrier_id": carrier_id}, option, carrier_id)
        if cost is not None:
            return cost
    cargo, m = _metrics(shipment)
    best = _best_card((await _lane_cards(shipment, leg, cargo, m)).get(carrier_id) or [])
    return best["cost_cents"] if best else None


# ------------------------------------------------------------------------------------------------
# Channel selection
# ------------------------------------------------------------------------------------------------
def _dcsa_live(carrier: dict) -> bool:
    st = net.integration_status(carrier)
    p = f"CARRIER_{st['code']}"
    return (st["type"] == "dcsa" and config.env_bool(f"{p}_ENABLED")
            and bool(config.env(f"{p}_BOOKING_URL") or config.env(f"{p}_BASE_URL"))
            and bool(config.env(f"{p}_BOOKING_AUTH_VALUE") or config.env(f"{p}_API_KEY")))


def _dcsa_fits(shipment: dict | None, leg: dict | None) -> bool:
    """DCSA Booking 2.0 books container sea legs (requestedEquipments)."""
    if shipment is None or leg is None:
        return True
    return leg.get("mode") == "sea" and bool((shipment.get("cargo") or {}).get("containers"))


def _test_api(carrier: dict) -> bool:
    return config.env_bool(f"{_env_prefix(carrier)}_BOOKING_TEST_API")


def _test_data(company: dict | None, shipment: dict | None) -> bool:
    if not company or any(company.get(k) is False for k in ("test_tenant", "test")):
        return False
    return (company.get("test_tenant") is True or company.get("test") is True
            or (company.get("demo") is True and (shipment or {}).get("demo") is True))


def channel_for(carrier: dict, company: dict | None, *, has_partner_users: bool, shipment: dict | None = None,
                leg: dict | None = None) -> str:
    if _test_api(carrier) and _test_data(company, shipment) and _dcsa_live(carrier) and _dcsa_fits(shipment, leg):
        return "live"  # HTTP transport to the official test API, with persisted test provenance
    if booking_mode() == "sandbox" or is_sandbox_company(company):
        return "sandbox"
    if booking_mode() == "live" and _dcsa_live(carrier) and _dcsa_fits(shipment, leg):
        return "live"
    if has_partner_users:
        return "partner_portal"
    return "log"


async def _partner_users(carrier_id: str) -> list[dict]:
    return [u async for u in db.raw.users.find({"org_type": "partner", "carrier_id": carrier_id, "disabled": {"$ne": True}},
                                               {"_id": 0, "password_hash": 0})]


async def _partner_carrier_ids() -> set[str]:
    return {u["carrier_id"] async for u in db.raw.users.find({"org_type": "partner", "carrier_id": {"$ne": None},
                                                              "disabled": {"$ne": True}}, {"_id": 0, "carrier_id": 1})}


async def adapter_for(carrier: dict, company: dict | None, *, shipment: dict | None = None,
                      leg: dict | None = None) -> "CarrierBookingAdapter":
    """Sandbox (sandbox company or CARRIER_BOOKING_MODE=sandbox) > live DCSA (mode live, integration live, container sea
    leg) > partner portal (active partner users for the carrier) > e-mail. Async: it looks up the partner users."""
    if _test_api(carrier) and not _test_data(company, shipment):
        raise HTTPException(409, "Production customers cannot use a carrier test API.")
    users = await _partner_users(carrier["id"])
    ch = channel_for(carrier, company, has_partner_users=bool(users), shipment=shipment, leg=leg)
    if ch == "sandbox":
        return SandboxAdapter(carrier)
    if ch == "live":
        return DcsaBookingAdapter.from_env(carrier)
    if ch == "partner_portal":
        return PartnerPortalAdapter(carrier, users)
    return LogAdapter(carrier)


async def _adapter_for_booking(b: dict) -> "CarrierBookingAdapter":
    carrier = await db.raw.carriers.find_one({"id": b["carrier_id"]}, NO_ID) or {"id": b["carrier_id"], "name": b.get("carrier_name")}
    name = b.get("adapter")
    if name == "sandbox":
        return SandboxAdapter(carrier)
    if name == "dcsa":
        adapter = DcsaBookingAdapter.from_env(carrier)
        if (b.get("provider_environment") == "test") != adapter.test_api:
            raise HTTPException(409, "Carrier API environment changed; reconcile the existing booking before continuing.")
        if adapter.test_api:
            shipment = await _shipment(b["shipment_id"])
            company = await db.raw.companies.find_one({"id": shipment.get("company_id")}, NO_ID)
            if not _test_data(company, shipment):
                raise HTTPException(409, "Production customers cannot use a carrier test API.")
        return adapter
    if name == "partner_portal":
        return PartnerPortalAdapter(carrier, await _partner_users(carrier["id"]))
    return LogAdapter(carrier)


# ------------------------------------------------------------------------------------------------
# Ranking
# ------------------------------------------------------------------------------------------------
def _dg_text(carrier: dict, dg: dict) -> tuple[bool, dict | None]:
    ok, why = net.dg_acceptance(carrier, dg or {})
    if ok:
        return True, None
    un = (dg.get("un_number") or "").upper()
    cls = str(dg.get("class") or "")
    name = carrier.get("name") or carrier.get("code")
    if un and un.replace(" ", "") in [u.upper() for u in carrier.get("dg_un_blocked") or []]:
        fi = f"{name} ei kuljeta ainetta {un}"
    elif not carrier.get("dg_classes_accepted"):
        fi = f"{name} ei ota vaarallisia aineita suoraan varattavaksi; kysy operatiiviselta tiimiltä"
    else:
        fi = f"{name} ei hyväksy VAK-luokkaa {cls}"
    return False, _t("dg_not_accepted", fi, why or f"{name} does not accept this dangerous goods class")


async def rank_carriers(shipment: dict, leg: dict, *, exclude: set[str] | frozenset = frozenset()) -> dict:
    """Rank carriers for one shipment leg: rate (purchase cost), lane coverage (an active rate card valid on the leg's
    departure day for its mode and places or region), reliability (carrier_performance on-time %), booking channel
    readiness (live > sandbox / partner portal > e-mail), transit days and DG acceptance. The carrier quoted on the
    accepted option is preferred unless excluded. choice = first eligible row; confidence = how clearly it wins."""
    if leg.get("booking_parent_seq"):
        leg = _leg(shipment, leg["booking_parent_seq"])
    leg = booking_leg(shipment, leg)
    exclude = set(exclude or ())
    mode = leg.get("mode") or shipment.get("mode")
    cargo, m = _metrics(shipment)
    is_dg = bool(cargo.get("dangerous_goods"))
    company = await db.raw.companies.find_one({"id": shipment.get("company_id")}, NO_ID)
    option = await _accepted_option(shipment)
    group = leg.get("booking_group")
    group_covered = await _group_covered(shipment, leg, option) if group else False
    ol = _option_leg_for(option, leg)
    quoted_id = (ol or {}).get("carrier_id")
    lanes = await _lane_cards(shipment, leg, cargo, m)
    partner_ids = await _partner_carrier_ids()
    carriers = [c async for c in db.raw.carriers.find({"active": {"$ne": False}}, NO_ID)]
    candidates = [c for c in carriers if mode in (c.get("modes") or []) or c["id"] in lanes or c["id"] == quoted_id]
    rows = []
    for c in candidates:
        cid = c["id"]
        reasons: list[dict] = []
        cards = lanes.get(cid) or []
        best = _best_card(cards)
        quoted = cid == quoted_id
        quoted_cost = _quoted_cost(shipment, {**leg, "carrier_id": cid}, option, cid) if quoted else None
        cost = quoted_cost if quoted_cost is not None else (best or {}).get("cost_cents")
        lane_ok = best is not None
        if quoted:
            reasons.append(_t("quoted", "Hyväksytyssä tarjouksessa tarjottu kuljetusliike", "Carrier quoted on the accepted option"))
        if lane_ok:
            card = best["card"]
            reasons.append(_t("lane_valid", f"Voimassa oleva hinnasto tälle osuudelle {_day(card.get('valid_to'), 'fi')} asti",
                              f"Valid rate card for this leg until {_day(card.get('valid_to'), 'en')}"))
        elif any(r["state"] == "expired" for r in cards):
            vt = max(r["card"].get("valid_to") or "" for r in cards if r["state"] == "expired")
            reasons.append(_t("lane_expired", f"Hinnasto vanhentui {_day(vt, 'fi')}", f"Rate card expired on {_day(vt, 'en')}"))
        elif any(r["state"] == "upcoming" for r in cards):
            vf = min(r["card"].get("valid_from") or "" for r in cards if r["state"] == "upcoming")
            reasons.append(_t("lane_upcoming", f"Hinnasto voimassa vasta {_day(vf, 'fi')} alkaen",
                              f"Rate card only valid from {_day(vf, 'en')}"))
        elif cards:
            reasons.append(_t("equipment", "Hinnasto ei kata tätä kalustoa", "The rate card does not offer this equipment"))
        else:
            reasons.append(_t("no_lane", "Ei hinnastoa tälle osuudelle", "No rate card for this leg"))
        if cost is not None:
            reasons.append(_t("cost", f"Ostohinta {_eur(cost, 'fi')}", f"Purchase cost {_eur(cost, 'en')}"))
        dg_ok, dg_reason = _dg_text(c, cargo.get("dg") or {}) if is_dg else (True, None)
        if dg_reason:
            reasons.append(dg_reason)
        perf = await net.carrier_performance(c)
        on_time = perf.get("on_time_pct")
        if on_time is not None:
            src_fi, src_en = ("omat toimitukset", "own shipments") if perf.get("source") == "own_shipments" else ("vertailuluku", "benchmark")
            reasons.append(_t("reliability", f"Täsmällisyys {on_time:g} % ({src_fi})", f"On time {on_time:g} % ({src_en})"))
        else:
            reasons.append(_t("reliability_unknown", "Täsmällisyydestä ei ole tietoa", "No on-time data"))
        channel = channel_for(c, company, has_partner_users=cid in partner_ids, shipment=shipment, leg=leg)
        reasons.append(_t("channel", f"Varauskanava: {CHANNEL_TEXT[channel][0]}", f"Booking channel: {CHANNEL_TEXT[channel][1]}"))
        transit = (best or {}).get("transit_days") or (_planned_days(leg) if quoted else None)
        excluded = cid in exclude
        if excluded:
            reasons.append(_t("excluded", "Rajattu pois tästä varauksesta", "Excluded from this booking"))
        # The quoted carrier stays bookable at the quoted cost even when its card lapsed after the quote.
        eligible = dg_ok and not excluded and cost is not None and (lane_ok or quoted_cost is not None)
        if group:
            eligible = eligible and group_covered and cid == group["carrier_id"]
            reasons.append(_t("quoted_booking_group", "Hyväksytyn tarjouksen koko reitti ja lauttamatka",
                              "Full route and ferry crossing covered by the accepted quote") if group_covered else
                           _t("outside_quoted_coverage", "Osuus ei kuulu hyväksytyn tarjouksen kattavuuteen",
                              "The leg is outside the accepted quote's coverage"))
        rows.append({"carrier_id": cid, "carrier_name": c.get("name"), "carrier_code": c.get("code"), "score": 0.0,
                     "cost_cents": cost, "transit_days": transit, "on_time_pct": on_time,
                     "on_time_source": perf.get("source"), "integration_status": net.integration_status(c)["status"],
                     "mode": channel, "dg_ok": dg_ok, "lane_ok": lane_ok, "quoted": quoted, "excluded": excluded,
                     "eligible": eligible, "rate_card_id": (best or {}).get("card", {}).get("id"),
                     "valid_to": (best or {}).get("card", {}).get("valid_to"), "reasons": reasons})
    pool = [r for r in rows if r["eligible"]]
    min_cost = min((r["cost_cents"] for r in pool if r["cost_cents"]), default=None)
    min_days = min((r["transit_days"] for r in pool if r["transit_days"]), default=None)
    for r in rows:
        cost_s = (min_cost / r["cost_cents"]) if (min_cost and r["cost_cents"]) else 0.5
        rel_s = (r["on_time_pct"] if r["on_time_pct"] is not None else 70) / 100.0
        days_s = (min_days / r["transit_days"]) if (min_days and r["transit_days"]) else 0.7
        r["score"] = round(WEIGHTS["cost"] * min(cost_s, 1.0) + WEIGHTS["reliability"] * rel_s
                           + WEIGHTS["transit"] * min(days_s, 1.0) + WEIGHTS["readiness"] * READINESS[r["mode"]], 4)
    # s3 procurement: partner scorecards and carrier allocations (services.scorecards.selection_inputs)
    from services import scorecards
    etd = parse_iso(leg.get("etd") or shipment.get("etd")) if (leg.get("etd") or shipment.get("etd")) else None
    teu = float(m.get("teu") or 0)
    sel = await scorecards.selection_inputs([r["carrier_id"] for r in rows], mode, leg.get("from") or shipment.get("origin"),
                                            leg.get("to") or shipment.get("destination"), etd.date() if etd else None,
                                            unit_qty={"TEU": teu, "FEU": teu / 2, "shipments": 1.0, "loads": 1.0,
                                                      "kg": float(m.get("weight_kg") or 0)},
                                            company_id=shipment.get("company_id"))
    for r in rows:
        x = sel.get(r["carrier_id"]) or {}
        r["score"] = round(r["score"] + x.get("adjust", 0.0), 4)
        r["scorecard"], r["allocation"] = x.get("scorecard"), x.get("allocation")
        r["reasons"].extend(x.get("reasons") or [])
    rows.sort(key=lambda r: (not r["eligible"], not (r["quoted"] and r["eligible"]), -r["score"], r["carrier_name"] or ""))
    choice = rows[0] if rows and rows[0]["eligible"] else None
    confidence = 0.0
    if choice:
        missing = sum(1 for k in ("on_time_pct", "transit_days") if choice[k] is None)
        others = [r for r in rows[1:] if r["eligible"]]
        if choice["quoted"] and (choice["lane_ok"] or group_covered):
            confidence = 0.95
        elif choice["quoted"]:
            confidence = 0.75  # booked at the quoted cost; the rate card no longer covers the leg
        elif not others:
            confidence = 0.9 - 0.1 * missing
        else:
            gap = (choice["score"] - others[0]["score"]) / choice["score"] if choice["score"] else 0.0
            confidence = 0.6 + min(0.35, max(0.0, gap) * 3.5) - 0.1 * missing
        confidence = round(max(0.3, min(0.95, confidence)), 2)
    return {"leg_seq": leg.get("seq"), "mode": mode, "dangerous_goods": is_dg, "quoted_carrier_id": quoted_id,
            "ranked": rows, "choice": choice, "confidence": confidence}


# ------------------------------------------------------------------------------------------------
# Adapters
# ------------------------------------------------------------------------------------------------
class CarrierBookingAdapter:
    """One booking channel. request = {booking_id, shipment, leg, carrier, company, cost_cents, reason, actor}.
    book/status/cancel return {status: requested|confirmed|rejected|cancelled|failed, carrier_ref?, carrier_request_ref?,
    transport_doc_no?, vessel?, voyage?, etd?, eta?, sandbox, request?, response?, failure? {code, fi, en}}."""

    name = "base"
    mode = "log"

    def __init__(self, carrier: dict):
        self.carrier = carrier

    async def book(self, request: dict) -> dict:
        raise NotImplementedError

    async def status(self, booking: dict) -> dict:
        return {"status": booking["status"]}

    async def cancel(self, booking: dict) -> dict:
        return {"status": "cancelled"}


def awb_number(prefix: str, serial7: int) -> str:
    """IATA air waybill: 3-digit airline prefix, 7-digit serial and its modulus-7 check digit."""
    serial7 = int(serial7) % 10_000_000
    return f"{str(prefix).zfill(3)[:3]}-{serial7:07d}{serial7 % 7}"


def awb_check_ok(awb: str) -> bool:
    digits = re.sub(r"\D", "", awb or "")
    return len(digits) == 11 and int(digits[3:10]) % 7 == int(digits[10])


class SandboxAdapter(CarrierBookingAdapter):
    """Confirms at once with synthetic references (SBX-...), no vessel identifiers that AIS could match, and the leg's
    planned ETD/ETA. Deterministic per booking id."""

    name = "sandbox"
    mode = "sandbox"

    async def book(self, request: dict) -> dict:
        bid, leg = request["booking_id"], request["leg"]
        code = _code(self.carrier)
        mode = leg.get("mode")
        if mode == "sea":
            doc = f"SBX{code}{_digits(bid, 8, 'bl')}"
        elif mode == "air":
            doc = awb_number(AWB_PREFIX.get(code, "000"), int(_digits(bid, 7, "awb")))
        elif mode == "rail":
            doc = f"SBXCIM{_digits(bid, 8, 'cim')}"
        else:
            doc = f"SBXCMR{_digits(bid, 8, 'cmr')}"
        out = {"status": "confirmed", "carrier_ref": f"SBX-{code}-{_digits(bid, 6, 'ref')}", "transport_doc_no": doc,
               "etd": leg.get("etd"), "eta": leg.get("eta"), "sandbox": True, "source": "sandbox"}
        if mode == "sea":
            out["vessel"] = {"name": f"SANDBOX {code} 1", "imo": None, "mmsi": None}
            out["voyage"] = f"SBX{_digits(bid, 3, 'voy')}"
        out["request"] = {"source": "sandbox", "leg_seq": leg.get("seq"), "from": (leg.get("from") or {}).get("unlocode"),
                          "to": (leg.get("to") or {}).get("unlocode"), "etd": leg.get("etd"), "eta": leg.get("eta")}
        out["response"] = {"source": "sandbox", "carrier_ref": out["carrier_ref"], "transport_doc_no": doc}
        return out

    async def cancel(self, booking: dict) -> dict:
        return {"status": "cancelled", "sandbox": True, "response": {"source": "sandbox"}}


def _cargo_lines(shipment: dict, lang: str) -> list[str]:
    cargo = shipment.get("cargo") or {}
    fi = lang == "fi"
    out = []
    if cargo.get("description"):
        out.append(f"{'Lasti' if fi else 'Cargo'}: {cargo['description']}")
    if cargo.get("containers"):
        cont = ", ".join(f"{c.get('count') or 1} × {pricing.CONTAINERS.get(str(c.get('type')).upper(), {}).get('label', c.get('type'))}"
                         for c in cargo["containers"])
        out.append(f"{'Kontit' if fi else 'Containers'}: {cont}")
    if cargo.get("packages"):
        pt = cargo.get("package_type") or "other"
        label = PACKAGE_FI.get(pt, "kolli") if fi else pricing.PACKAGE_LABEL.get(pt, "package")
        out.append(f"{'Kollit' if fi else 'Packages'}: {cargo['packages']} × {label}")
    if cargo.get("weight_kg"):
        out.append(f"{'Bruttopaino' if fi else 'Gross weight'}: {_kg(cargo['weight_kg'], lang)}")
    if cargo.get("volume_m3"):
        vol = f"{float(cargo['volume_m3']):.1f}".replace(".", "," if fi else ".")
        out.append(f"{'Tilavuus' if fi else 'Volume'}: {vol} m³")
    if cargo.get("ldm"):
        out.append(f"{'Lavametrit' if fi else 'Loading metres'}: {cargo['ldm']}")
    if cargo.get("dangerous_goods"):
        dg = cargo.get("dg") or {}
        bits = [dg.get("un_number"), (f"luokka {dg['class']}" if fi else f"class {dg['class']}") if dg.get("class") else None,
                (f"pakkausryhmä {dg['packing_group']}" if fi else f"packing group {dg['packing_group']}") if dg.get("packing_group") else None,
                dg.get("proper_shipping_name")]
        phone = dg.get("emergency_phone")
        txt = ", ".join(b for b in bits if b)
        if phone:
            txt += f". {'Hätäpuhelin' if fi else 'Emergency phone'} {phone}"
        out.append(f"{'Vaarallinen aine' if fi else 'Dangerous goods'}: {txt}")
    else:
        out.append("Ei vaarallisia aineita." if fi else "No dangerous goods.")
    return out


def _request_mail(request: dict) -> tuple[str, str, str]:
    s, leg, carrier = request["shipment"], request["leg"], request["carrier"]
    bid, seq = request["booking_id"], leg.get("seq")
    ref = s.get("reference")
    route = f"{_place_label(leg.get('from'))} → {_place_label(leg.get('to'))}"
    refs = s.get("references") or {}
    blocks = []
    for lang in ("fi", "en"):
        fi = lang == "fi"
        mode_txt = MODE_TEXT.get(leg.get("mode"), (leg.get("mode"), leg.get("mode")))[0 if fi else 1]
        lines = [f"{config.COMPANY_NAME} pyytää varausta seuraavalle kuljetukselle." if fi
                 else f"{config.COMPANY_NAME} requests a booking for the following transport.",
                 f"{'Reitti' if fi else 'Route'}: {route}",
                 f"{'Kuljetusmuoto' if fi else 'Mode'}: {mode_txt}" + (f" ({leg['service']})" if leg.get("service") else ""),
                 f"{'Lähtö (suunniteltu)' if fi else 'Departure (planned)'}: {_when(leg.get('etd'), lang)}",
                 f"{'Saapuminen (suunniteltu)' if fi else 'Arrival (planned)'}: {_when(leg.get('eta'), lang)}"]
        for part in leg.get("covered_legs") or []:
            if part["mode"] == "sea":
                crossing = f"{_place_label(part['from'])} → {_place_label(part['to'])}"
                lines.append(f"Lauttamatka sisältyy kuljetusliikkeen varaukseen: {crossing}, {part.get('operator') or 'operaattori vahvistettava'}."
                             if fi else f"Ferry crossing included in the road carrier booking: {crossing}, "
                             f"{part.get('operator') or 'operator to be confirmed'}.")
        pick, drop = _address_line((leg.get("from") or {}).get("address")), _address_line((leg.get("to") or {}).get("address"))
        if pick:
            lines.append(f"{'Nouto-osoite' if fi else 'Pickup address'}: {pick}")
        if drop:
            lines.append(f"{'Toimitusosoite' if fi else 'Delivery address'}: {drop}")
        lines += _cargo_lines(s, lang)
        ref_bits = [f"{'lähetys' if fi else 'shipment'} {ref}"]
        if refs.get("customer_ref"):
            ref_bits.append(f"{'asiakkaan viite' if fi else 'customer reference'} {refs['customer_ref']}")
        if refs.get("po_numbers"):
            ref_bits.append(f"{'tilausnumerot' if fi else 'PO numbers'} {', '.join(refs['po_numbers'])}")
        lines.append(f"{'Viitteet' if fi else 'References'}: {'; '.join(ref_bits)}")
        if request.get("cost_cents"):
            lines.append(f"{'Sovittu hinta' if fi else 'Agreed rate'}: {_eur(request['cost_cents'], lang)} "
                         f"({'alv 0 %, hinnastonne mukaan' if fi else 'excl. VAT, per your rate card'})")
        lines.append(f"{'Varaustunnuksemme' if fi else 'Our booking id'}: {bid}")
        lines.append("Vahvistakaa varaus vastaamalla tähän viestiin: varausnumeronne, kuljetusasiakirjan numero "
                     f"({DOC_TEXT.get(leg.get('mode'), ('rahtikirja', ''))[0]}) sekä vahvistetut lähtö- ja saapumisajat. "
                     f"Yhteys: {config.COMPANY_EMAIL}" if fi else
                     "Please confirm by replying to this message with your booking number, the transport document number "
                     f"({DOC_TEXT.get(leg.get('mode'), ('', 'consignment note'))[1]}) and the confirmed departure and arrival. "
                     f"Contact: {config.COMPANY_EMAIL}")
        blocks.append(lines)
    subject = f"Varauspyyntö / Booking request {ref} – osuus / leg {seq}: {route}"
    html, text = render(subject, blocks[0] + ["— In English —"] + blocks[1])
    return subject, html, text


class LogAdapter(CarrierBookingAdapter):
    """Booking request e-mail to the carrier's booking desk (carrier.contact_email). Stays requested until ops or an
    inbound e-mail confirms it; no confirmation is invented."""

    name = "log"
    mode = "log"

    async def book(self, request: dict) -> dict:
        to = (self.carrier.get("contact_email") or "").strip()
        name = self.carrier.get("name") or self.carrier.get("code")
        if not to:
            return {"status": "failed", "sandbox": False,
                    "failure": _t("no_contact_email", f"Kuljetusliikkeellä {name} ei ole varaussähköpostiosoitetta. "
                                                      "Lisää se kuljetusliikkeen tietoihin tai varaa käsin.",
                                  f"{name} has no booking e-mail address. Add it to the carrier or book by hand.")}
        subject, html, text = _request_mail(request)
        from email.utils import make_msgid
        from services.email_channel import conf as mail_conf
        mid = make_msgid(idstring="cb-" + request["booking_id"], domain=mail_conf.domain())
        subject += f" [CB:{request['booking_id']}]"
        s = request["shipment"]
        rec = await send_email(to, subject, html, text, tag="carrier_booking", company_id=s.get("company_id"),
                               headers={"Message-ID": mid}, reply_to=mail_conf.address(),
                               ref={"booking_id": request["booking_id"], "shipment_id": s["id"],
                                    "leg_seq": request["leg"].get("seq"), "carrier_id": self.carrier["id"]})
        if rec.get("status") == "failed":
            return {"status": "failed", "sandbox": False, "request": {"to": to, "subject": subject, "email_id": rec["id"]},
                    "response": {"email_status": rec.get("status"), "error": rec.get("error")},
                    "failure": _t("email_failed", f"Varauspyynnön lähetys osoitteeseen {to} epäonnistui.",
                                  f"The booking request e-mail to {to} failed.")}
        return {"status": "requested", "sandbox": False, "email_id": rec["id"],
                "request": {"to": to, "subject": subject, "email_id": rec["id"], "message_id": mid},
                "response": {"email_status": rec.get("status"), "provider": rec.get("provider")}}

    async def cancel(self, booking: dict) -> dict:
        to = ((booking.get("request") or {}).get("to") or self.carrier.get("contact_email") or "").strip()
        if not to:
            return {"status": "cancelled", "response": {"email": None}}
        ref = booking.get("shipment_reference")
        seq = booking.get("leg_seq")
        their = booking.get("carrier_ref")
        lines_fi = [f"Peruutamme varauksen: lähetys {ref}, osuus {seq}." + (f" Varausnumeronne {their}." if their else ""),
                    f"Varaustunnuksemme: {booking['id']}", f"Yhteys: {config.COMPANY_EMAIL}"]
        lines_en = [f"We cancel the booking for shipment {ref}, leg {seq}." + (f" Your booking number {their}." if their else ""),
                    f"Our booking id: {booking['id']}", f"Contact: {config.COMPANY_EMAIL}"]
        subject = f"Varauksen peruutus / Booking cancellation {ref} – osuus / leg {seq}"
        html, text = render(subject, lines_fi + ["— In English —"] + lines_en)
        rec = await send_email(to, subject, html, text, tag="carrier_booking_cancel", company_id=booking.get("company_id"),
                               ref={"booking_id": booking["id"], "shipment_id": booking["shipment_id"], "leg_seq": seq})
        return {"status": "cancelled", "response": {"email_id": rec["id"], "email_status": rec.get("status")}}


class PartnerPortalAdapter(CarrierBookingAdapter):
    """Asks the carrier's partner users to accept the leg in the partner portal; the accept endpoint confirms."""

    name = "partner_portal"
    mode = "partner_portal"

    def __init__(self, carrier: dict, users: list[dict]):
        super().__init__(carrier)
        self.users = users

    async def book(self, request: dict) -> dict:
        s, leg = request["shipment"], request["leg"]
        if not self.users:
            return {"status": "failed", "sandbox": False,
                    "failure": _t("no_partner_users", "Kuljetusliikkeellä ei ole aktiivisia kumppaniportaalin käyttäjiä.",
                                  "The carrier has no active partner portal users.")}
        route = f"{_place_label(leg.get('from'))} → {_place_label(leg.get('to'))}"
        crossings = [f"{_place_label(p['from'])} → {_place_label(p['to'])}" for p in leg.get("covered_legs") or [] if p["mode"] == "sea"]
        included_fi = f" Lauttamatka sisältyy: {'; '.join(crossings)}." if crossings else ""
        included_en = f" Ferry crossing included: {'; '.join(crossings)}." if crossings else ""
        ref, seq = s.get("reference"), leg.get("seq")
        etd_fi, etd_en = _when(leg.get("etd"), "fi"), _when(leg.get("etd"), "en")
        for u in self.users:
            await notify_user(u, bi(f"Uusi kuljetuspyyntö {ref} (osuus {seq})", f"New transport request {ref} (leg {seq})"),
                              bi(f"{route}, lähtö {etd_fi}.{included_fi} Hyväksy osuus kumppaniportaalissa, niin varaus vahvistuu.",
                                 f"{route}, departure {etd_en}.{included_en} Accept the leg in the partner portal to confirm the booking."),
                              link=f"/partner/shipments/{s['id']}", email=True, kind="carrier_booking")
        return {"status": "requested", "sandbox": False,
                "request": {"partner_user_ids": [u["id"] for u in self.users], "link": f"/partner/shipments/{s['id']}"},
                "response": {"notified": len(self.users)}}

    async def cancel(self, booking: dict) -> dict:
        ref, seq = booking.get("shipment_reference"), booking.get("leg_seq")
        for u in self.users:
            await notify_user(u, bi(f"Kuljetuspyyntö peruttu {ref} (osuus {seq})", f"Transport request cancelled {ref} (leg {seq})"),
                              bi(f"Varaus {ref} osuudelle {seq} on peruttu, eikä osuutta tarvitse ajaa.",
                                 f"The booking for {ref} leg {seq} is cancelled; the leg is no longer needed."),
                              link=f"/partner/shipments/{booking['shipment_id']}", email=True, kind="carrier_booking")
        return {"status": "cancelled", "response": {"notified": len(self.users)}}


def _date_with_time(day: str | None, planned: str | None) -> str | None:
    """Carrier dates are dates (DCSA plannedDepartureDate); keep the planned time of day when the date matches format."""
    if not day:
        return None
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    p = parse_iso(planned)
    if p:
        return iso(p.replace(year=d.year, month=d.month, day=d.day))
    return iso(datetime(d.year, d.month, d.day, 12))


def dcsa_booking_request(shipment: dict, leg: dict, carrier: dict, *, booking_id: str, contract_ref: str | None = None,
                         contract_kind: str = "quotation") -> dict:
    """DCSA Booking 2.0 CreateBooking body for one container sea leg (field names as published in the DCSA OpenAPI
    specification DCSA_BKG 2.0.0). We deliver the containers to the carrier's yard and collect them there (CY/CY): our
    own pre- and on-carriage legs are booked separately. Raises ValueError when data the schema requires is missing."""
    cargo = shipment.get("cargo") or {}
    containers = cargo.get("containers") or []
    if leg.get("mode") != "sea" or not containers:
        raise ValueError("DCSA Booking 2.0 is used for container sea legs only")
    pol, pod = leg.get("from") or {}, leg.get("to") or {}
    for side, loc in (("from", pol), ("to", pod)):
        if not re.fullmatch(r"[A-Z]{2}[A-Z2-9]{3}", str(loc.get("unlocode") or "")):
            raise ValueError(f"leg {side} has no UN/LOCODE")
    if not leg.get("etd"):
        raise ValueError("leg has no planned departure")
    if not contract_ref:
        raise ValueError("no service contract or quotation reference for this carrier (CARRIER_<CODE>_CONTRACT_REF)")
    total_teu = sum(pricing.CONTAINERS.get(str(c.get("type")).upper(), {}).get("teu", 1) * int(c.get("count") or 1)
                    for c in containers) or 1
    commodity_type = (cargo.get("description") or "Freight all kinds (FAK)")[:550]
    hs = re.sub(r"\D", "", str(cargo.get("hs_hint") or ""))
    dg_rows = []
    if cargo.get("dangerous_goods"):
        dg = cargo.get("dg") or {}
        un = re.sub(r"\D", "", str(dg.get("un_number") or ""))
        if not re.fullmatch(r"\d{4}", un) or not dg.get("proper_shipping_name") or not dg.get("class") or not dg.get("emergency_phone"):
            raise ValueError("dangerous goods need the UN number, proper shipping name, class and emergency phone")
        gross = dg.get("gross_weight_kg") or cargo.get("weight_kg")
        if not gross:
            raise ValueError("dangerous goods need the gross weight")
        row = {"UNNumber": un, "properShippingName": str(dg["proper_shipping_name"])[:250], "imoClass": str(dg["class"])[:4],
               "isMarinePollutant": bool(dg.get("marine_pollutant")), "isLimitedQuantity": bool(dg.get("limited_quantity")),
               "isExceptedQuantity": bool(dg.get("excepted_quantity")), "isSalvagePackings": False,
               "isEmptyUncleanedResidue": False, "isWaste": bool(dg.get("waste")), "isHot": False,
               "isCompetentAuthorityApprovalRequired": False, "isReportableQuantity": False,
               "emergencyContactDetails": {"contact": str(dg.get("emergency_contact") or config.COMPANY_NAME)[:255],
                                           "phone": str(dg["emergency_phone"]).strip()[:30]},
               "grossWeight": {"value": float(gross), "unit": "KGM"}}
        pg = {"I": 1, "II": 2, "III": 3}.get(str(dg.get("packing_group") or "").upper())
        if pg:
            row["packingGroup"] = pg
        if dg.get("ems"):
            row["EMSNumber"] = str(dg["ems"])[:7]
        dg_rows.append(row)
    equipments = []
    for c in containers:
        ctype = str(c.get("type") or "").upper()
        if ctype not in ISO_EQUIPMENT:
            raise ValueError(f"unknown container type {ctype}")
        units = int(c.get("count") or 1)
        share = pricing.CONTAINERS[ctype]["teu"] * units / total_teu
        commodity: dict = {"commodityType": commodity_type}
        if len(hs) >= 6:
            commodity["HSCodes"] = [hs[:10]]
        if cargo.get("weight_kg"):
            commodity["cargoGrossWeight"] = {"value": round(float(cargo["weight_kg"]) * share, 1), "unit": "KGM"}
        if cargo.get("volume_m3"):
            commodity["cargoGrossVolume"] = {"value": round(float(cargo["volume_m3"]) * share, 2), "unit": "MTQ"}
        if cargo.get("packages") or dg_rows:
            pack: dict = {}
            if cargo.get("packages"):
                pack["numberOfPackages"] = max(1, int(round(int(cargo["packages"]) * share)))
                pack["packageCode"] = PACKAGE_CODE.get(cargo.get("package_type") or "other", "PK")
                pack["description"] = pricing.PACKAGE_LABEL.get(cargo.get("package_type") or "other", "package")[:100]
            if dg_rows:
                pack["dangerousGoods"] = dg_rows
            commodity["outerPackaging"] = pack
        eq: dict = {"ISOEquipmentCode": ISO_EQUIPMENT[ctype], "units": units, "isShipperOwned": False,
                    "commodities": [commodity]}
        if cargo.get("weight_kg"):
            eq["cargoGrossWeight"] = {"value": round(float(cargo["weight_kg"]) * share, 1), "unit": "KGM"}
        if ctype.endswith("RF"):
            temp = cargo.get("temperature_c", cargo.get("temperature"))
            try:
                eq["activeReeferSettings"] = {"temperatureSetpoint": float(temp), "temperatureUnit": "CEL"}
            except (TypeError, ValueError):
                raise ValueError("reefer containers need the temperature setpoint") from None
        equipments.append(eq)
    origin_cc = (shipment.get("origin") or {}).get("country") or str(pol["unlocode"])[:2]
    dest_cc = (shipment.get("destination") or {}).get("country") or str(pod["unlocode"])[:2]
    contact = {"name": str(config.COMPANY_NAME)[:35], "email": config.COMPANY_EMAIL}
    body: dict = {
        "receiptTypeAtOrigin": "CY", "deliveryTypeAtDestination": "CY",
        "cargoMovementTypeAtOrigin": "FCL", "cargoMovementTypeAtDestination": "FCL",
        "freightPaymentTermCode": "PRE",
        "isPartialLoadAllowed": False,
        "isExportDeclarationRequired": origin_cc in net.EU_COUNTRIES and dest_cc not in net.EU_COUNTRIES,
        "isEquipmentSubstitutionAllowed": False,
        "expectedDepartureDate": str(leg["etd"])[:10],
        "references": [{"type": "CR", "value": str(shipment.get("reference") or booking_id)[:35]}],
        "documentParties": {"bookingAgent": {"partyName": str(config.COMPANY_NAME)[:70], "partyContactDetails": [contact],
                                             "reference": booking_id.replace("-", "")[:35]}},
        "partyContactDetails": [contact],
        "shipmentLocations": [
            {"location": {"locationName": str(pol.get("name") or pol["unlocode"])[:100], "UNLocationCode": pol["unlocode"]},
             "locationTypeCode": "POL"},
            {"location": {"locationName": str(pod.get("name") or pod["unlocode"])[:100], "UNLocationCode": pod["unlocode"]},
             "locationTypeCode": "POD"}],
        "requestedEquipments": equipments,
    }
    if contract_kind == "service_contract":
        body["serviceContractReference"] = str(contract_ref)[:30]
    else:
        body["contractQuotationReference"] = str(contract_ref)[:35]
    if str(shipment.get("incoterm") or "").upper() in INCOTERMS:
        body["incoTerms"] = str(shipment["incoterm"]).upper()
    if carrier.get("scac"):
        body["carrierCode"], body["carrierCodeListProvider"] = str(carrier["scac"]).upper()[:4], "NMFTA"
    parties = shipment.get("parties") or {}
    for role in ("shipper", "consignee"):
        p = parties.get(role) or {}
        if not (p.get("name") or "").strip():
            continue
        party: dict = {"partyName": p["name"].strip()[:70]}
        a = p.get("address") or {}
        if a.get("street") and a.get("city") and a.get("country"):
            party["address"] = {"street": str(a["street"])[:70], "city": str(a["city"])[:35], "countryCode": str(a["country"]).upper()[:2]}
            pc = a.get("postal_code") or a.get("postcode") or a.get("zip")
            if pc:
                party["address"]["postCode"] = str(pc)[:10]
        body["documentParties"][role] = party
    return body


def booked_payload(b: dict, s: dict | None) -> dict:
    """The booking as `carrier.booked` carries it: the stored booking plus the keys execution reads
    (shared/interfaces.md s9): carrier_booking_reference, equipment [{iso_type, count}] from the shipment's containers,
    cutoffs [{code, at, source}] in DCSA shipmentCutOffTimes codes when the carrier returned them (else [])."""
    equipment = []
    for c in ((s or {}).get("cargo") or {}).get("containers") or []:
        ctype = str(c.get("type") or "").upper()
        equipment.append({"type": ctype, "iso_type": ISO_EQUIPMENT.get(ctype), "count": int(c.get("count") or 1)})
    return {**b, "carrier_booking_reference": b.get("carrier_ref"), "equipment": equipment, "cutoffs": b.get("cutoffs") or []}


def _dcsa_details(data: dict, leg: dict) -> dict:
    """Our fields from a DCSA Booking 2.0 Booking (GET response)."""
    plan = [t for t in data.get("transportPlan") or [] if t.get("transportPlanStage") == "MNC"] or list(data.get("transportPlan") or [])
    first, last = (plan[0], plan[-1]) if plan else ({}, {})
    vessel_name = first.get("vesselName") or (data.get("vessel") or {}).get("name")
    imo = first.get("vesselIMONumber") or (data.get("vessel") or {}).get("vesselIMONumber")
    out = {"carrier_ref": data.get("carrierBookingReference"), "carrier_request_ref": data.get("carrierBookingRequestReference"),
           "dcsa_status": data.get("bookingStatus"), "voyage": first.get("carrierExportVoyageNumber") or data.get("carrierExportVoyageNumber"),
           "transport_doc_no": data.get("transportDocumentReference")}
    if vessel_name:
        out["vessel"] = {"name": vessel_name, "imo": imo, "mmsi": None}
    etd = _date_with_time(first.get("plannedDepartureDate") or data.get("expectedDepartureDate"), leg.get("etd"))
    eta = _date_with_time(last.get("plannedArrivalDate"), leg.get("eta"))
    if etd:
        out["etd"] = etd
    if eta:
        out["eta"] = eta
    cutoffs = [{"code": c.get("cutOffDateTimeCode"), "at": c.get("cutOffDateTime"), "source": "carrier"}
               for c in data.get("shipmentCutOffTimes") or [] if c.get("cutOffDateTimeCode") and c.get("cutOffDateTime")]
    if cutoffs:
        out["cutoffs"] = cutoffs
    return {k: v for k, v in out.items() if v is not None}


class DcsaBookingAdapter(CarrierBookingAdapter):
    """DCSA Booking 2.0: POST {base}/v2/bookings -> 202 {carrierBookingRequestReference};
    GET {base}/v2/bookings/{bookingReference} -> Booking.bookingStatus; PATCH {base}/v2/bookings/{bookingReference}
    cancels (bookingStatus CANCELLED before confirmation, bookingCancellationStatus CANCELLATION_RECEIVED after)."""

    name = "dcsa"
    mode = "live"

    def __init__(self, carrier: dict, base_url: str | None, api_key: str | None, *, contract_ref: str | None = None,
                 contract_kind: str = "quotation", auth_header: str = "Authorization", auth_value: str | None = None,
                 test_api: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(carrier)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.auth_header = auth_header
        self.auth_value = auth_value if auth_value is not None else f"Bearer {self.api_key}" if self.api_key else ""
        self.contract_ref = contract_ref
        self.contract_kind = contract_kind
        self.transport = transport
        self.test_api = test_api

    @classmethod
    def from_env(cls, carrier: dict) -> "DcsaBookingAdapter":
        p = _env_prefix(carrier)
        svc_ref = config.env(f"{p}_SERVICE_CONTRACT_REF") or carrier.get("service_contract_ref")
        quote_ref = config.env(f"{p}_CONTRACT_REF") or carrier.get("contract_ref")
        return cls(carrier, config.env(f"{p}_BOOKING_URL") or config.env(f"{p}_BASE_URL"), config.env(f"{p}_API_KEY"),
                   contract_ref=svc_ref or quote_ref, contract_kind="service_contract" if svc_ref else "quotation",
                   auth_header=config.env(f"{p}_BOOKING_AUTH_HEADER", "Authorization"),
                   auth_value=config.env(f"{p}_BOOKING_AUTH_VALUE") or None, test_api=_test_api(carrier))

    def _headers(self) -> dict:
        return {"API-Version": "2.0.0", "Accept": "application/json", self.auth_header: self.auth_value}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self.transport)

    @staticmethod
    def _failure(code: str, detail: str) -> dict:
        return _t(code, f"Kuljetusliikkeen varausrajapinta ei hyväksynyt pyyntöä: {detail}",
                  f"The carrier's booking API did not accept the request: {detail}")

    async def book(self, request: dict) -> dict:
        if self.test_api and not _test_data(request.get("company"), request.get("shipment")):
            raise HTTPException(409, "Production customers cannot use a carrier test API.")
        if not self.base_url or not self.auth_value:
            return {"status": "failed", "sandbox": self.test_api,
                    "failure": _t("not_configured", "Kuljetusliikkeen DCSA-varausrajapintaa ei ole määritetty.",
                                  "The carrier's DCSA booking API is not configured.")}
        try:
            body = dcsa_booking_request(request["shipment"], request["leg"], self.carrier, booking_id=request["booking_id"],
                                        contract_ref=self.contract_ref, contract_kind=self.contract_kind)
        except ValueError as exc:
            return {"status": "failed", "sandbox": self.test_api, "failure": _t("missing_data", f"Varaukselta puuttuu tietoja: {exc}",
                                                                       f"The booking is missing data: {exc}")}
        try:
            async with self._client() as client:
                r = await client.post(f"{self.base_url}/v2/bookings", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            return {"status": "failed", "sandbox": self.test_api, "request": body, "response": {"error": str(exc)[:500]},
                    "failure": self._failure("api_unreachable", str(exc)[:200])}
        data = _json(r)
        if r.status_code in (200, 201, 202) and data.get("carrierBookingRequestReference"):
            return {"status": "requested", "sandbox": self.test_api, "carrier_request_ref": data["carrierBookingRequestReference"],
                    "request": body, "response": {"http_status": r.status_code, "body": data}}
        return {"status": "failed", "sandbox": self.test_api, "request": body, "response": {"http_status": r.status_code, "body": data},
                "failure": self._failure("api_error", f"HTTP {r.status_code} {_error_text(data)}".strip())}

    async def status(self, booking: dict) -> dict:
        ref = booking.get("carrier_ref") or booking.get("carrier_request_ref")
        if not ref or not self.base_url:
            return {"status": booking["status"], "error": "no booking reference or base URL"}
        try:
            async with self._client() as client:
                r = await client.get(f"{self.base_url}/v2/bookings/{ref}", headers=self._headers())
        except httpx.HTTPError as exc:
            return {"status": booking["status"], "error": str(exc)[:500]}
        data = _json(r)
        if r.status_code == 202:  # accepted, not yet processed by the carrier
            return {"status": "requested", "response": {"http_status": 202, "body": data}}
        if r.status_code != 200:
            return {"status": booking["status"], "error": f"HTTP {r.status_code} {_error_text(data)}".strip(),
                    "response": {"http_status": r.status_code, "body": data}}
        mapped = DCSA_STATUS.get(str(data.get("bookingStatus") or "").upper(), booking["status"])
        if mapped == "confirmed" and not str(data.get("carrierBookingReference") or "").strip():
            return {"status": booking["status"], "error": "Carrier confirmation has no carrierBookingReference",
                    "response": {"http_status": 200, "body": data}}
        out = {"status": mapped, "response": {"http_status": 200, "body": data}, **_dcsa_details(data, booking.get("_leg") or {})}
        if mapped == "rejected":
            fb = "; ".join(str(f.get("message") or "") for f in data.get("feedbacks") or [] if f.get("message"))[:300]
            out["failure"] = _t("carrier_rejected", f"Kuljetusliike hylkäsi varauksen ({data.get('bookingStatus')}). {fb}".strip(),
                                f"The carrier rejected the booking ({data.get('bookingStatus')}). {fb}".strip())
        return out

    async def cancel(self, booking: dict) -> dict:
        reason = (booking.get("_cancel_reason") or "")[:5000]
        if booking.get("status") == "confirmed" and booking.get("carrier_ref"):
            ref, body = booking["carrier_ref"], {"bookingCancellationStatus": "CANCELLATION_RECEIVED"}
        else:
            ref, body = booking.get("carrier_request_ref"), {"bookingStatus": "CANCELLED"}
        if not ref:
            return {"status": "cancelled", "response": {"reached_carrier": False}}
        if reason:
            body["reason"] = reason
        try:
            async with self._client() as client:
                r = await client.patch(f"{self.base_url}/v2/bookings/{ref}", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            return {"status": "failed", "response": {"error": str(exc)[:500]}, "failure": self._failure("api_unreachable", str(exc)[:200])}
        data = _json(r)
        if r.status_code in (200, 202, 204):
            return {"status": "cancelled", "request": body, "response": {"http_status": r.status_code, "body": data}}
        return {"status": "failed", "request": body, "response": {"http_status": r.status_code, "body": data},
                "failure": self._failure("api_error", f"HTTP {r.status_code} {_error_text(data)}".strip())}


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json() if r.content else {}
    except ValueError:
        return {"raw": r.text[:2000]}
    return data if isinstance(data, dict) else {"data": data}


def _error_text(data: dict) -> str:
    errs = data.get("errors") or []
    if errs and isinstance(errs, list):
        return "; ".join(str(e.get("message") or e.get("errorCodeMessage") or e) for e in errs if e)[:300]
    return str(data.get("statusCodeMessage") or data.get("message") or "")[:300]


# ------------------------------------------------------------------------------------------------
# Booking lifecycle
# ------------------------------------------------------------------------------------------------
def _hist(status: str, actor: dict | None, fi: str, en: str, **extra) -> dict:
    return {"at": now_iso(), "status": status, "by": actor_ref(actor)["name"], "fi": fi, "en": en, **extra}


def _leg_booking(b: dict) -> dict:
    return {"id": b["id"], "status": b["status"], "ref": b.get("carrier_ref"), "mode": b.get("mode"),
            **({"provider_environment": "test", "sandbox": True} if b.get("provider_environment") == "test" else {}),
            "carrier_id": b.get("carrier_id"), "confirmed_at": b.get("confirmed_at")}


async def _set_leg_booking(b: dict):
    for seq in b.get("covered_leg_seqs") or [b["leg_seq"]]:
        link = _leg_booking(b)
        if seq != b["leg_seq"]:
            link["covered_by_leg_seq"] = b["leg_seq"]
        await db.raw.shipments.update_one({"id": b["shipment_id"], "legs.seq": seq},
                                          {"$set": {"legs.$.booking": link, "updated_at": now_iso()}})


async def _note_event(s: dict, b: dict, *, actor: dict | None, label_fi: str, label_en: str, note_fi: str, note_en: str):
    ev = await svc._event(s, "note", label_en, source="sandbox" if b.get("sandbox") else EVENT_SOURCE.get(b.get("mode"), "manual"), actor=actor,
                          leg_seq=b["leg_seq"], note=note_en, raw={"carrier_booking_id": b["id"], "status": b["status"]})
    await db.raw.tracking_events.update_one({"id": ev["id"]}, {"$set": {
        "label_i18n": {"fi": label_fi, "en": label_en}, "note_i18n": {"fi": note_fi, "en": note_en},
        "carrier_booking_id": b["id"], "sandbox": bool(b.get("sandbox"))}})


async def _emit_booking_event(event: str, payload: dict):
    """Audit only test-provider dispatches; production dispatch stays unchanged.

    A pending receipt is not evidence of emission. Only completion of emit marks
    it dispatched. A crash leaves a visible pending receipt, never a false success.
    The booking transition and deterministic receipt ID prevent duplicate dispatch.
    """
    b = payload["booking"]
    if b.get("provider_environment") != "test":
        await emit(event, payload)
        return
    event = {"carrier.booked": "carrier.test_booked",
             "carrier.booking_failed": "carrier.test_booking_failed",
             "carrier.booking_cancelled": "carrier.test_booking_cancelled"}[event]
    receipt_id = f"{b['id']}:{event}"
    inserted = await db.raw.carrier_test_events.update_one({"_id": receipt_id}, {"$setOnInsert": {
        "id": receipt_id, "event": event, "booking_id": b["id"], "shipment_id": b["shipment_id"],
        "company_id": b.get("company_id"), "provider_environment": "test", "sandbox": True,
        "at": now_iso(), "dispatch_status": "pending", "payload": payload}}, upsert=True)
    if inserted.upserted_id is None:
        return
    await emit(event, payload)
    await db.raw.carrier_test_events.update_one({"_id": receipt_id}, {"$set": {
        "dispatch_status": "dispatched", "dispatched_at": now_iso()}})


async def _finish(b: dict, status: str, actor: dict | None, *, failure: dict | None = None, extra: dict | None = None) -> dict:
    """requested -> failed / rejected: the slot is freed (active_key unset) so another carrier can be booked."""
    fi, en = (failure or {}).get("fi") or status, (failure or {}).get("en") or status
    sets = {"status": status, "updated_at": now_iso(), "failure": failure, **(extra or {})}
    res = await db.raw.carrier_bookings.update_one({"id": b["id"], "status": {"$in": list(ACTIVE)}},
                                                   {"$set": sets, "$unset": {"active_key": ""},
                                                    "$push": {"history": _hist(status, actor, fi, en)}})
    b = await _booking(b["id"])
    if not res.modified_count:
        return b
    await _set_leg_booking(b)
    await audit(actor, f"carrier_booking.{status}", "carrier_booking", b["id"],
                {"shipment_id": b["shipment_id"], "leg_seq": b["leg_seq"], "carrier_id": b["carrier_id"], "failure": failure},
                company_id=b.get("company_id"))
    s = await svc.get_shipment(b["shipment_id"])
    await _emit_booking_event("carrier.booking_failed",
               {"shipment": s, "booking": b})
    return b


async def _confirm(b: dict, details: dict, actor: dict | None) -> dict:
    """requested -> confirmed exactly once; then the leg gets the carrier, vessel/voyage, document number and the
    confirmed ETD/ETA, a timeline note and carrier.booked."""
    sets = {"status": "confirmed", "confirmed_at": now_iso(), "updated_at": now_iso()}
    for k in ("carrier_ref", "carrier_request_ref", *LEG_DETAIL_KEYS, "cutoffs", "confirmation_message_id"):
        if details.get(k):
            sets[k] = details[k]
    if isinstance(details.get("vessel"), dict) and details["vessel"].get("name"):
        v = details["vessel"]
        sets["vessel"] = {"name": v.get("name"), "imo": v.get("imo"), "mmsi": v.get("mmsi")}
    if details.get("response") is not None:
        sets["response"] = details["response"]
    ref = sets.get("carrier_ref") or b.get("carrier_ref")
    fi = f"Vahvistettu, varausnumero {ref}" if ref else "Vahvistettu"
    en = f"Confirmed, booking number {ref}" if ref else "Confirmed"
    res = await db.raw.carrier_bookings.update_one({"id": b["id"], "status": "requested"},
                                                   {"$set": sets, "$push": {"history": _hist("confirmed", actor, fi, en)}})
    b = await _booking(b["id"])
    if not res.modified_count:
        return b  # confirmed (or closed) by someone else first: nothing more to do
    # Only what the carrier confirmed goes onto the leg (a later ops change of the plan is not overwritten).
    changes: dict = {"carrier_id": b["carrier_id"], "carrier": b.get("carrier_name")}
    for k in LEG_DETAIL_KEYS:
        if b.get("covered_leg_seqs") and k in ("etd", "eta"):
            continue  # A full-route confirmation is not the first drawing leg's arrival/departure.
        v = sets.get("vessel") if k == "vessel" else details.get(k)
        if v:
            changes[k] = v
    try:
        s = await svc.update_leg(b["shipment_id"], b["leg_seq"], changes, actor)
    except HTTPException as exc:
        log.warning("carrier booking %s confirmed but leg update failed: %s", b["id"], exc.detail)
        await db.raw.carrier_bookings.update_one({"id": b["id"]}, {"$set": {"leg_sync_error": str(exc.detail)}})
        return await _booking(b["id"])
    await _set_leg_booking(b)
    seq, carrier = b["leg_seq"], b.get("carrier_name")
    doc = b.get("transport_doc_no")
    doc_fi, doc_en = DOC_TEXT.get(b.get("leg_mode"), ("kuljetusasiakirja", "transport document"))
    sbx_fi, sbx_en = (" (testiympäristö)", " (sandbox)") if b.get("sandbox") else ("", "")
    note_fi = f"Osuus {seq} varattu: {carrier}, varausnumero {ref}" + (f", {doc_fi} {doc}" if doc else "") + sbx_fi
    note_en = f"Leg {seq} booked with {carrier}, booking number {ref}" + (f", {doc_en} {doc}" if doc else "") + sbx_en
    await _note_event(s, b, actor=actor, label_fi="Kuljetusvaraus vahvistettu", label_en="Carrier booking confirmed",
                      note_fi=note_fi, note_en=note_en)
    await audit(actor, "carrier_booking.confirmed", "carrier_booking", b["id"],
                {"shipment_id": b["shipment_id"], "leg_seq": seq, "carrier_id": b["carrier_id"], "carrier_ref": ref,
                 "transport_doc_no": doc, "mode": b.get("mode")}, company_id=b.get("company_id"))
    s = await svc.get_shipment(b["shipment_id"])
    await _emit_booking_event("carrier.booked",
               {"shipment": s, "booking": booked_payload(b, s)})
    return b


async def _apply(b: dict, result: dict, actor: dict | None) -> dict:
    status = result.get("status")
    keep = {k: result[k] for k in ("request", "response", "carrier_request_ref", "email_id") if result.get(k) is not None}
    if keep:
        await db.raw.carrier_bookings.update_one({"id": b["id"]}, {"$set": {**keep, "updated_at": now_iso()}})
        b = {**b, **keep}
    if status == "confirmed":
        return await _confirm(b, result, actor)
    if status in ("failed", "rejected"):
        return await _finish(b, status, actor, failure=result.get("failure"))
    if status == "cancelled":
        return await _finish(b, "cancelled", actor, failure=result.get("failure") or _t(
            "carrier_cancelled", "Kuljetusliike perui varauksen", "The carrier cancelled the booking"))
    return b


async def book_leg(shipment_id: str, leg_seq: int, carrier_id: str, actor: dict, *, reason: str = "",
                   cost_cents: int | None = None) -> dict:
    """Book one shipment leg with a carrier through its channel (adapter_for). Idempotent: an active (requested or
    confirmed) booking for the same shipment, leg and carrier is returned as is. A different carrier with an active
    booking on the leg is a 409 (use rebook_leg). Returns the carrier_bookings document."""
    await _ensure_indexes()
    s = await _shipment(shipment_id)
    leg = _leg(s, leg_seq)
    if leg.get("booking_parent_seq"):
        parent = _leg(s, leg["booking_parent_seq"])
        if (parent.get("booking_group") or {}).get("carrier_id") != carrier_id:
            raise HTTPException(409, "This leg is covered by the quoted road carrier; a different carrier needs a person to review coverage.")
        return await book_leg(shipment_id, parent["seq"], carrier_id, actor, reason=reason)
    leg = booking_leg(s, leg)
    if leg.get("booking_group"):
        if carrier_id != leg["booking_group"]["carrier_id"] or not await _group_covered(s, leg, await _accepted_option(s)):
            raise HTTPException(409, "The road booking group is outside the accepted quote's coverage; a person must arrange the ferry crossing.")
    seq = int(leg["seq"])
    key = _active_key(s["id"], seq, carrier_id)
    existing = await db.raw.carrier_bookings.find_one({"active_key": key}, NO_ID)
    if existing:
        if cost_cents is not None and cost_cents != existing.get("cost_cents"):
            await guard_started_group(s, leg, operation="reprice")
        return existing
    await guard_started_group(s, leg, operation="book")
    if s.get("status") in ("delivered", "cancelled"):
        raise HTTPException(409, f"The shipment is {s['status']}; its legs can no longer be booked.")
    if leg.get("atd") or leg.get("ata"):
        raise HTTPException(409, f"Leg {seq} has already departed; it cannot be booked again.")
    carrier = await db.raw.carriers.find_one({"id": carrier_id}, NO_ID)
    if not carrier:
        raise HTTPException(422, "Unknown carrier.")
    if carrier.get("active") is False:
        raise HTTPException(409, f"{carrier.get('name')} is inactive.")
    other = await db.raw.carrier_bookings.find_one({"shipment_id": s["id"], "leg_seq": seq, "status": {"$in": list(ACTIVE)}}, NO_ID)
    if other:
        raise HTTPException(409, f"Leg {seq} already has an active booking with {other.get('carrier_name')}; use rebook_leg.")
    from services.agents.quote_agent import autosend, human_actor
    if not human_actor(actor):
        gate = await autosend(shipment=s)
        if not gate.auto:
            raise HTTPException(409, "Carrier booking awaits a person's approval.")
    company = await db.raw.companies.find_one({"id": s.get("company_id")}, NO_ID)
    adapter = await adapter_for(carrier, company, shipment=s, leg=leg)
    if cost_cents is None:
        cost_cents = await _carrier_cost(s, leg, carrier_id)
    at = now_iso()
    b = {"id": new_id(), "shipment_id": s["id"], "shipment_reference": s.get("reference"), "company_id": s.get("company_id"),
         "leg_seq": seq, "leg_mode": leg.get("mode"), "carrier_id": carrier_id, "carrier_name": carrier.get("name"),
         **({"covered_leg_seqs": leg["booking_group"]["leg_seqs"], "coverage": leg["booking_group"]["coverage"]}
            if leg.get("booking_group") else {}),
         "carrier_code": carrier.get("code"), "adapter": adapter.name, "mode": adapter.mode, "status": "requested",
         "active_key": key, "carrier_ref": None, "carrier_request_ref": None, "transport_doc_no": None,
         "vessel": {"name": None, "imo": None, "mmsi": None}, "voyage": None, "etd": leg.get("etd"), "eta": leg.get("eta"),
         "cost_cents": cost_cents, "sandbox": adapter.mode == "sandbox" or getattr(adapter, "test_api", False),
         "provider_environment": "test" if getattr(adapter, "test_api", False) else "production" if adapter.name == "dcsa" else None,
         "request": None, "response": None,
         "reason": reason or None, "failure": None, "created_at": at, "updated_at": at, "confirmed_at": None,
         "cancelled_at": None, "created_by": actor_ref(actor),
         "history": [_hist("requested", actor, f"Varaus aloitettu ({CHANNEL_TEXT[adapter.mode][0]})",
                           f"Booking started ({CHANNEL_TEXT[adapter.mode][1]})")]}
    try:
        await db.raw.carrier_bookings.insert_one(dict(b))
    except DuplicateKeyError:
        return await db.raw.carrier_bookings.find_one({"active_key": key}, NO_ID) or b
    await audit(actor, "carrier_booking.requested", "carrier_booking", b["id"],
                {"shipment_id": s["id"], "leg_seq": seq, "carrier_id": carrier_id, "mode": adapter.mode, "reason": reason},
                company_id=s.get("company_id"))
    if adapter.mode == "partner_portal" and _leg(s, seq).get("carrier_id") != carrier_id:
        # partner users only see legs assigned to their carrier: assign it so they can accept it
        s = await svc.update_leg(s["id"], seq, {"carrier_id": carrier_id, "carrier": carrier.get("name")}, actor)
        leg = booking_leg(s, _leg(s, seq))
    request = {"booking_id": b["id"], "shipment": s, "leg": leg, "carrier": carrier, "company": company,
               "cost_cents": cost_cents, "reason": reason, "actor": actor}
    try:
        result = await adapter.book(request)
    except Exception as exc:  # noqa: BLE001 - a channel failure is recorded on the booking, never lost
        log.exception("carrier booking %s via %s failed", b["id"], adapter.name)
        result = {"status": "failed", "response": {"error": str(exc)[:500]},
                  "failure": _t("adapter_error", f"Varauskanava ei vastannut: {str(exc)[:200]}",
                                f"The booking channel failed: {str(exc)[:200]}")}
    b = await _apply(b, result, actor)
    if b["status"] == "requested":
        await _set_leg_booking(b)
    return b


async def confirm_booking(booking_id: str, *, carrier_ref: str, actor: dict | None, details: dict | None = None,
                          source_message_id: str | None = None) -> dict:
    """The carrier confirmed (partner portal accept, an inbound e-mail, ops). details may carry transport_doc_no,
    vessel {name, imo, mmsi}, voyage, etd, eta, flight, train, truck_plate, container_numbers.
    source_message_id is the exact native carrier_booking_messages row that confirmed it (audit reference only)."""
    b = await _booking(booking_id)
    if b["status"] == "confirmed":
        return b
    if b["status"] != "requested":
        raise HTTPException(409, f"The booking is {b['status']}; it can no longer be confirmed.")
    if not (carrier_ref or "").strip():
        raise HTTPException(422, "Give the carrier's booking number.")
    return await _confirm(b, {**(details or {}), "carrier_ref": carrier_ref.strip(),
        **({"confirmation_message_id":source_message_id} if source_message_id else {})}, actor)


async def poll_booking(booking_id: str) -> dict:
    """Ask the channel for the booking's status (live DCSA GET; sandbox, e-mail and partner portal answer as is)."""
    from core.agent_runtime import agent_bot

    b = await _booking(booking_id)
    if b["status"] != "requested":
        return b
    adapter = await _adapter_for_booking(b)
    s = await svc.get_shipment(b["shipment_id"])
    leg = next((lg for lg in (s or {}).get("legs") or [] if lg.get("seq") == b["leg_seq"]), {})
    try:
        res = await adapter.status({**b, "_leg": leg})
    except Exception as exc:  # noqa: BLE001
        log.exception("carrier booking %s status failed", booking_id)
        res = {"status": b["status"], "error": str(exc)[:500]}
    await db.raw.carrier_bookings.update_one({"id": b["id"]}, {"$set": {
        "last_polled_at": now_iso(), "last_poll": {"status": res.get("status"), "error": res.get("error"),
                                                   "dcsa_status": res.get("dcsa_status")}}})
    if res.get("status") == b["status"]:
        return await _booking(b["id"])
    return await _apply(b, res, agent_bot("carrier_agent"))


async def cancel_booking(booking_id: str, actor: dict | None, reason: str) -> dict:
    """Cancel with the carrier (e-mail, partner notice, DCSA PATCH) and free the leg. A confirmed booking's vessel,
    voyage and document number are cleared from the leg when they are still this booking's."""
    b = await _booking(booking_id)
    if b["status"] not in ACTIVE:
        return b
    s = await _shipment(b["shipment_id"])
    await guard_started_group(s, _leg(s, b["leg_seq"]), operation="cancel")
    from services.agents.quote_agent import autosend, human_actor
    if not human_actor(actor):
        gate = await autosend(shipment=s)
        if not gate.auto:
            raise HTTPException(409, "Carrier booking awaits a person's approval.")
    adapter = await _adapter_for_booking(b)
    res = await adapter.cancel({**b, "_cancel_reason": reason})
    if res.get("status") != "cancelled":
        raise HTTPException(502, (res.get("failure") or {}).get("en") or "The carrier did not accept the cancellation.")
    was = b["status"]
    fi, en = f"Peruttu: {reason}" if reason else "Peruttu", f"Cancelled: {reason}" if reason else "Cancelled"
    upd = await db.raw.carrier_bookings.update_one(
        {"id": b["id"], "status": {"$in": list(ACTIVE)}},
        {"$set": {"status": "cancelled", "cancelled_at": now_iso(), "updated_at": now_iso(), "cancel_reason": reason or None,
                  "cancel_response": res.get("response")},
         "$unset": {"active_key": ""}, "$push": {"history": _hist("cancelled", actor, fi, en)}})
    b = await _booking(b["id"])
    if not upd.modified_count:
        return b
    s = await svc.get_shipment(b["shipment_id"])
    leg = next((lg for lg in (s or {}).get("legs") or [] if lg.get("seq") == b["leg_seq"]), None)
    if s and leg and s.get("status") not in ("delivered", "cancelled"):
        if was == "confirmed" and leg.get("carrier_id") == b["carrier_id"]:
            clear = {}
            if b.get("transport_doc_no") and leg.get("transport_doc_no") == b["transport_doc_no"]:
                clear["transport_doc_no"] = None
            if (b.get("vessel") or {}).get("name") and (leg.get("vessel") or {}).get("name") == b["vessel"]["name"]:
                clear["vessel"] = {"name": None, "imo": None, "mmsi": None}
            if b.get("voyage") and leg.get("voyage") == b["voyage"]:
                clear["voyage"] = None
            if clear:
                s = await svc.update_leg(s["id"], b["leg_seq"], clear, actor)
        if (leg.get("booking") or {}).get("id") == b["id"]:
            await _set_leg_booking(b)
        ref = b.get("carrier_ref") or b["id"][:8]
        await _note_event(s, b, actor=actor, label_fi="Kuljetusvaraus peruttu", label_en="Carrier booking cancelled",
                          note_fi=f"Osuus {b['leg_seq']}: varaus {ref} ({b.get('carrier_name')}) peruttu. {reason or ''}".strip(),
                          note_en=f"Leg {b['leg_seq']}: booking {ref} with {b.get('carrier_name')} cancelled. {reason or ''}".strip())
    await audit(actor, "carrier_booking.cancelled", "carrier_booking", b["id"],
                {"shipment_id": b["shipment_id"], "leg_seq": b["leg_seq"], "carrier_id": b["carrier_id"], "reason": reason},
                company_id=b.get("company_id"))
    await _emit_booking_event("carrier.booking_cancelled",
               {"shipment": s, "booking": b})
    return b


async def _sailing_interval_days(carrier: dict, leg: dict, etd: datetime) -> int:
    """Days to the carrier's next departure after etd: carrier.sailing_interval_days, else the lane card's departure
    weekdays, else 7 days for sea and 1 day for road, air and rail."""
    if carrier.get("sailing_interval_days"):
        return max(1, int(carrier["sailing_interval_days"]))
    card = await db.raw.rate_cards.find_one({"carrier_id": carrier["id"], "mode": leg.get("mode"), "archived": {"$ne": True},
                                             "departure_weekdays.0": {"$exists": True},
                                             "origin.unlocode": (leg.get("from") or {}).get("unlocode")}, NO_ID)
    if card:
        wd = etd.weekday()
        return min(((d - wd) % 7) or 7 for d in card["departure_weekdays"])
    return SAILING_DEFAULT_DAYS.get(leg.get("mode"), 1)


async def rebook_leg(shipment_id: str, leg_seq: int, actor: dict, *, reason: str, exclude_carrier_ids=(),
                     not_before: str | None = None) -> dict:
    """Move a leg to another carrier: rank again excluding the current carrier (and carriers that rejected this leg),
    cancel the active booking, book the choice. When the current carrier is the only eligible one, book its next
    departure instead (ETD/ETA moved by its sailing interval until on or after not_before). Nothing is cancelled when
    no carrier is eligible. Returns {outcome rebooked|next_departure|no_eligible_carrier, booking, cancelled, ranking,
    shifted}."""
    s = await _shipment(shipment_id)
    leg = _leg(s, leg_seq)
    if leg.get("booking_parent_seq"):
        return await rebook_leg(shipment_id, leg["booking_parent_seq"], actor, reason=reason,
                                exclude_carrier_ids=exclude_carrier_ids, not_before=not_before)
    await guard_started_group(s, leg, operation="rebook")
    from services.agents.quote_agent import autosend, human_actor
    if not human_actor(actor):
        gate = await autosend(shipment=s)
        if not gate.auto:
            raise HTTPException(409, "Carrier booking awaits a person's approval.")
    seq = int(leg["seq"])
    active = await db.raw.carrier_bookings.find_one({"shipment_id": s["id"], "leg_seq": seq, "status": {"$in": list(ACTIVE)}},
                                                    NO_ID, sort=[("created_at", -1)])
    current = (active or {}).get("carrier_id") or leg.get("carrier_id")
    rejected = {r["carrier_id"] async for r in db.raw.carrier_bookings.find(
        {"shipment_id": s["id"], "leg_seq": seq, "status": "rejected"}, {"_id": 0, "carrier_id": 1})}
    explicit = set(exclude_carrier_ids or ()) | rejected
    ranking = await rank_carriers(s, leg, exclude=explicit | ({current} if current else set()))
    plan, shifted = None, None
    if ranking["choice"]:
        plan = ranking["choice"]["carrier_id"]
    elif current and current not in explicit:
        alone = await rank_carriers(s, leg, exclude=explicit)
        if alone["choice"] and alone["choice"]["carrier_id"] == current:
            ranking, plan = alone, current
    if not plan:
        return {"outcome": "no_eligible_carrier", "booking": None, "cancelled": None, "ranking": ranking, "shifted": None}
    cancelled = await cancel_booking(active["id"], actor, reason) if active else None
    s = await _shipment(shipment_id)
    leg = _leg(s, seq)
    floor = parse_iso(not_before) if not_before else None
    etd, eta = parse_iso(leg.get("etd")), parse_iso(leg.get("eta"))
    if plan == current and etd:
        carrier = await db.raw.carriers.find_one({"id": current}, NO_ID) or {"id": current}
        target = max(floor or etd, now())
        new_etd = etd
        for _ in range(60):
            new_etd = new_etd + timedelta(days=await _sailing_interval_days(carrier, leg, new_etd))
            if new_etd >= target:
                break
        shifted = {"from": iso(etd), "to": iso(new_etd), "days": round((new_etd - etd).total_seconds() / 86400, 2)}
    elif floor and etd and etd < floor:
        shifted = {"from": iso(etd), "to": iso(floor), "days": round((floor - etd).total_seconds() / 86400, 2)}
    if shifted:
        delta = parse_iso(shifted["to"]) - etd
        changes = {"etd": shifted["to"]}
        if eta:
            changes["eta"] = iso(eta + delta)
        await svc.update_leg(s["id"], seq, changes, actor)
    choice_cost = next((r["cost_cents"] for r in ranking["ranked"] if r["carrier_id"] == plan), None)
    b = await book_leg(s["id"], seq, plan, actor, reason=reason, cost_cents=choice_cost)
    return {"outcome": "next_departure" if plan == current else "rebooked", "booking": b, "cancelled": cancelled,
            "ranking": ranking, "shifted": shifted}
