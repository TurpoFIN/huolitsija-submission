"""Carrier allocations: minimum quantity commitments (MQC) and capacity caps per carrier, lane and period.

Usage comes from real bookings only: every `shipment.created` is matched by carrier, lane and date and recorded once in
`allocation_usage`; a cancelled shipment releases it. s9's `carrier.booked` updates the quantity from the booked equipment.
Pricing reads `for_option()` so that a carrier behind its commitment is preferred (the avoidable shortfall penalty is a
ranking credit, the customer price is unchanged) and a carrier at its cap drops in rank.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta

from fastapi import HTTPException

from core.audit import audit
from core.db import db, register_index
from core.events import on
from core.test_scope import in_scope, owner_fields, scope_query
from core.util import NO_ID, new_id, now, now_iso, parse_iso

log = logging.getLogger("allocations")

register_index("carrier_allocations", "id", unique=True)
register_index("carrier_allocations", [("carrier_id", 1), ("status", 1)])
register_index("allocation_usage", [("allocation_id", 1), ("shipment_id", 1)], unique=True, name="alloc_usage_once")
register_index("allocation_usage", [("allocation_id", 1), ("period_key", 1)])
register_index("allocation_usage", "shipment_id")

UNITS = ("TEU", "FEU", "shipments", "loads", "kg")
PERIODS = ("week", "month", "quarter")
MODES = ("road", "sea", "air", "rail")
TEU = {"20": 1.0, "40": 2.0, "45": 2.25}


# ------------------------------------------------------------------------------------------------- lanes and periods
def parse_lane(lane: str) -> tuple[str, str, str]:
    """'sea:CN>FI' or 'sea:CNSHA>FIHEL' -> (mode, origin, destination). Raises ValueError."""
    mode, pair = (lane or "").split(":", 1)
    origin, destination = pair.split(">", 1)
    origin, destination = origin.strip().upper(), destination.strip().upper()
    if mode not in MODES or len(origin) not in (2, 5) or len(destination) not in (2, 5):
        raise ValueError("Lane is mode:origin>destination with ISO countries or UN/LOCODEs, e.g. sea:CN>FI")
    return mode, origin, destination


def _end_matches(code: str, place: dict | None) -> bool:
    place = place or {}
    if len(code) == 2:
        return (place.get("country") or "").upper() == code
    return (place.get("unlocode") or place.get("code") or "").upper() == code


def lane_matches(alloc: dict, mode: str, origin: dict, destination: dict) -> bool:
    try:
        a_mode, a_o, a_d = parse_lane(alloc["lane"])
    except (ValueError, KeyError):
        return False
    return a_mode == mode and _end_matches(a_o, origin) and _end_matches(a_d, destination)


def period_bounds(period: str, day: date) -> tuple[str, date, date]:
    if period == "week":
        start = day - timedelta(days=day.weekday())
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}", start, start + timedelta(days=6)
    if period == "quarter":
        q = (day.month - 1) // 3
        start = date(day.year, 3 * q + 1, 1)
        end_month = 3 * q + 3
        return f"{day.year}-Q{q + 1}", start, date(day.year, end_month, calendar.monthrange(day.year, end_month)[1])
    start = date(day.year, day.month, 1)
    return f"{day:%Y-%m}", start, date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def shipment_qty(unit: str, shipment: dict, equipment: list[dict] | None = None) -> float:
    if unit in ("shipments", "loads"):
        return 1.0
    if unit == "kg":
        return float(shipment.get("weight_kg") or (shipment.get("cargo") or {}).get("weight_kg") or 0)
    teu = 0.0
    if equipment:  # s9 booking equipment [{iso_type "45G1", count}]
        for e in equipment:
            teu += TEU.get(str(e.get("iso_type") or "")[:2], 2.0 if str(e.get("iso_type") or "").startswith("4") else 1.0) * int(e.get("count") or 1)
    else:
        teu = float(shipment.get("teu") or (shipment.get("cargo") or {}).get("teu") or 0)
    return teu / 2 if unit == "FEU" else teu


def _day(value) -> date:
    dt = parse_iso(value) if isinstance(value, str) else None
    return dt.date() if dt else now().date()


def _active_on(alloc: dict, day: date) -> bool:
    return alloc.get("status") == "active" and alloc["valid_from"] <= day.isoformat() <= alloc["valid_to"]


# ------------------------------------------------------------------------------------------------- CRUD
def _clean(body: dict, partial: bool = False) -> dict:
    out = {}
    if "lane" in body or not partial:
        mode, o, d = parse_lane(body.get("lane") or "")
        out["lane"] = f"{mode}:{o}>{d}"
    for k in ("committed_qty", "max_qty", "shortfall_cents_per_unit"):
        if k in body or (not partial and k == "committed_qty"):
            v = body.get(k)
            if v is None and k != "committed_qty":
                out[k] = None if k == "max_qty" else 0
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{k} must be a non-negative number")
            out[k] = int(v) if k == "shortfall_cents_per_unit" else float(v)
    if "unit" in body or not partial:
        if body.get("unit") not in UNITS:
            raise ValueError("unit must be one of " + ", ".join(UNITS))
        out["unit"] = body["unit"]
    if "period" in body or not partial:
        if body.get("period") not in PERIODS:
            raise ValueError("period must be week, month or quarter")
        out["period"] = body["period"]
    for k in ("valid_from", "valid_to"):
        if k in body or not partial:
            try:
                out[k] = date.fromisoformat(str(body.get(k))).isoformat()
            except ValueError:
                raise ValueError(f"{k} must be a date YYYY-MM-DD")
    if "status" in body:
        if body["status"] not in ("active", "paused", "ended"):
            raise ValueError("status must be active, paused or ended")
        out["status"] = body["status"]
    for k in ("service", "notes"):
        if k in body:
            out[k] = (str(body[k]).strip() or None) if body[k] is not None else None
    if "rate_card_ids" in body:
        out["rate_card_ids"] = [str(x) for x in body.get("rate_card_ids") or []]
    if out.get("max_qty") is not None and out.get("committed_qty") is not None and out["max_qty"] < out["committed_qty"]:
        raise ValueError("max_qty cannot be below committed_qty")
    return out


async def create(body: dict, actor: dict) -> dict:
    carrier = await db.carriers.find_one({"id": body.get("carrier_id")}, NO_ID)
    if not carrier:
        raise HTTPException(404, "Carrier not found")
    try:
        data = _clean(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if data["valid_to"] < data["valid_from"]:
        raise HTTPException(400, "valid_to is before valid_from")
    owner = await owner_fields(body, carrier)
    row = {"id": new_id(), "carrier_id": carrier["id"], "carrier_name": carrier["name"], "max_qty": None,
           "shortfall_cents_per_unit": 0, "service": None, "notes": None, "rate_card_ids": [], "status": "active",
           **data, **owner, "created_at": now_iso(), "updated_at": now_iso(),
           "created_by": {"id": actor.get("id"), "name": actor.get("name")}}
    await db.carrier_allocations.insert_one(dict(row))
    await audit(actor, "allocation.created", "carrier_allocation", row["id"], {k: row.get(k) for k in ("carrier_name", "lane", "unit", "committed_qty", "period", "company_id")})
    await backfill(row["id"])
    return await get(row["id"])


async def update(allocation_id: str, body: dict, actor: dict) -> dict:
    before = await db.carrier_allocations.find_one({"id": allocation_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Allocation not found")
    try:
        data = _clean({k: v for k, v in body.items() if k not in ("id", "carrier_id", "carrier_name", "company_id", "test_tenant")},
                      partial=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    merged = {**before, **data}
    if merged["valid_to"] < merged["valid_from"]:
        raise HTTPException(400, "valid_to is before valid_from")
    if merged.get("max_qty") is not None and merged["max_qty"] < merged["committed_qty"]:
        raise HTTPException(400, "max_qty cannot be below committed_qty")
    await db.carrier_allocations.update_one({"id": allocation_id}, {"$set": {**data, "updated_at": now_iso()}})
    await audit(actor, "allocation.updated", "carrier_allocation", allocation_id, {"before": {k: before.get(k) for k in data}, "after": data})
    if any(k in data for k in ("lane", "unit", "valid_from", "valid_to")):
        await db.allocation_usage.delete_many({"allocation_id": allocation_id})
        await backfill(allocation_id)
    return await get(allocation_id)


async def get(allocation_id: str, day: date | None = None) -> dict | None:
    row = await db.carrier_allocations.find_one({"id": allocation_id}, NO_ID)
    if not row:
        return None
    usage = await db.allocation_usage.find({"allocation_id": allocation_id}, NO_ID).sort("at", -1).to_list(500)
    periods = sorted({u["period_key"] for u in usage if not u.get("released")})
    return {**row, "usage": usage, "status_now": await status(row, day), "periods_with_usage": periods}


async def list_all(carrier_id: str | None = None) -> list[dict]:
    q = {"carrier_id": carrier_id} if carrier_id else {}
    rows = await db.carrier_allocations.find(q, NO_ID).sort([("status", 1), ("valid_to", -1)]).to_list(1000)
    return [{**r, "status_now": await status(r)} for r in rows]


# ------------------------------------------------------------------------------------------------- usage
async def status(alloc: dict | str, day: date | None = None) -> dict:
    from core.record_provenance import operational_query
    if isinstance(alloc, str):
        alloc = await db.carrier_allocations.find_one({"id": alloc}, NO_ID)
    day = day or now().date()
    key, start, end = period_bounds(alloc["period"], day)
    # the period is clipped to the allocation's own validity
    start = max(start, date.fromisoformat(alloc["valid_from"]))
    end = min(end, date.fromisoformat(alloc["valid_to"]))
    used = 0.0
    usage = await db.allocation_usage.find(
        {"allocation_id": alloc["id"], "period_key": key, "released": {"$ne": True}},
        {"qty": 1, "shipment_id": 1}).to_list(None)
    eligible = {s["id"] async for s in db.raw.shipments.find(
        {**await operational_query(), "id": {"$in": [u["shipment_id"] for u in usage]},
         "status": {"$ne": "cancelled"}}, {"id": 1})} if usage else set()
    for u in usage:
        if u["shipment_id"] in eligible:
            used += float(u.get("qty") or 0)
    committed = float(alloc["committed_qty"])
    total_days = max(1, (end - start).days + 1)
    elapsed = min(total_days, max(0, (day - start).days + 1))
    frac = elapsed / total_days
    expected = round(committed * frac, 2)
    projected = used / frac if frac > 0 else used
    shortfall = round(max(0.0, committed - projected), 2)
    cap = alloc.get("max_qty")
    return {"period_key": key, "period_start": start.isoformat(), "period_end": end.isoformat(), "committed": committed,
            "used": round(used, 2), "remaining": round(max(0.0, committed - used), 2), "max_qty": cap,
            "capacity_left": None if cap is None else round(max(0.0, cap - used), 2),
            "pace_pct": round(100 * used / expected, 1) if expected else None, "expected_by_now": expected,
            "behind_pace": used < expected, "shortfall_forecast": shortfall,
            "exposure_cents": int(round(shortfall * (alloc.get("shortfall_cents_per_unit") or 0))),
            "full": cap is not None and used >= cap, "active": _active_on(alloc, day)}


async def record_shipment(shipment: dict, equipment: list[dict] | None = None) -> list[dict]:
    """Match a booked shipment to every active allocation of its carriers and record the quantity once."""
    from core.record_provenance import operational_record
    if shipment.get("status") == "cancelled" or shipment.get("demo"):
        return []
    operational = await operational_record(shipment)
    carrier_ids = {lg.get("carrier_id") for lg in shipment.get("legs") or [] if lg.get("carrier_id")}
    if not carrier_ids:
        return []
    day = _day(shipment.get("etd") or shipment.get("created_at"))
    out = []
    async for alloc in db.carrier_allocations.find({"carrier_id": {"$in": list(carrier_ids)}, "status": "active"}, NO_ID):
        # a test tenant's allocation counts only that company's bookings; production allocations count operational ones only
        if not (in_scope(alloc, shipment.get("company_id")) if alloc.get("test_tenant") else operational):
            continue
        if not _active_on(alloc, day) or not lane_matches(alloc, shipment.get("mode"), shipment.get("origin"), shipment.get("destination")):
            continue
        qty = shipment_qty(alloc["unit"], shipment, equipment)
        if qty <= 0:
            continue
        key = period_bounds(alloc["period"], day)[0]
        await db.allocation_usage.update_one(
            {"allocation_id": alloc["id"], "shipment_id": shipment["id"]},
            {"$set": {"qty": qty, "period_key": key, "shipment_ref": shipment.get("reference"), "day": day.isoformat(),
                      "source": "booking_equipment" if equipment else "shipment", "released": False, "updated_at": now_iso()},
             "$setOnInsert": {"at": now_iso()}}, upsert=True)
        out.append({"allocation_id": alloc["id"], "qty": qty, "period_key": key})
    return out


async def backfill(allocation_id: str) -> int:
    """Count bookings that already exist inside the allocation's validity (new or edited allocation)."""
    alloc = await db.carrier_allocations.find_one({"id": allocation_id}, NO_ID)
    n = 0
    async for s in db.shipments.find({"legs.carrier_id": alloc["carrier_id"], "status": {"$ne": "cancelled"}, "demo": {"$ne": True}}, NO_ID):
        n += len([r for r in await record_shipment(s) if r["allocation_id"] == allocation_id])
    return n


@on("shipment.created")
async def _on_shipment_created(payload: dict):
    try:
        await record_shipment(payload["shipment"])
    except Exception:  # noqa: BLE001 - booking must never fail because of allocation bookkeeping
        log.exception("allocation usage for shipment %s", (payload.get("shipment") or {}).get("id"))


@on("shipment.status_changed")
async def _on_status(payload: dict):
    if payload.get("new_status") == "cancelled":
        await db.allocation_usage.update_many({"shipment_id": payload["shipment"]["id"]},
                                              {"$set": {"released": True, "updated_at": now_iso()}})


@on("carrier.booked")
async def _on_carrier_booked(payload: dict):
    shipment, booking = payload.get("shipment") or {}, payload.get("booking") or {}
    if shipment.get("id") and booking.get("equipment"):
        await record_shipment(shipment, booking["equipment"])


# ------------------------------------------------------------------------------------------------- pricing hook
async def for_option(carrier_id: str, mode: str, origin: dict, destination: dict, on_date: date, qty: float = 1.0,
                     unit_qty: dict | None = None, company_id: str | None = None) -> dict | None:
    """The allocation an option on this carrier would draw from, or None. `unit_qty` maps unit -> quantity of this
    booking (TEU, shipments, kg ...). A test tenant's allocation is seen only by that company (`company_id`)."""
    best = None
    async for alloc in db.carrier_allocations.find({"carrier_id": carrier_id, "status": "active", **scope_query(company_id)}, NO_ID):
        if not _active_on(alloc, on_date) or not lane_matches(alloc, mode, origin, destination):
            continue
        st = await status(alloc, on_date)
        q = float((unit_qty or {}).get(alloc["unit"], qty) or 0)
        credit = 0
        if st["behind_pace"] and alloc.get("shortfall_cents_per_unit"):
            credit = int(round(min(q, st["shortfall_forecast"] or st["remaining"]) * alloc["shortfall_cents_per_unit"]))
        full = st["full"] or (st["capacity_left"] is not None and q > st["capacity_left"])
        row = {"allocation_id": alloc["id"], "lane": alloc["lane"], "unit": alloc["unit"], "committed": st["committed"],
               "used": st["used"], "remaining": st["remaining"], "period_key": st["period_key"], "behind_pace": st["behind_pace"],
               "full": full, "penalty_per_unit_cents": alloc.get("shortfall_cents_per_unit") or 0, "ranking_credit_cents": credit}
        if best is None or (row["ranking_credit_cents"], not row["full"]) > (best["ranking_credit_cents"], not best["full"]):
            best = row
    return best


async def annotate_options(options: list[dict], req: dict, m: dict, company_id: str | None = None) -> None:
    """Put `allocation` on every option whose carrier has an active allocation on the lane (pricing.rank reads it)."""
    if not options or not await db.carrier_allocations.count_documents({"status": "active", **scope_query(company_id)}, limit=1):
        return
    day = date.fromisoformat(req["ready_date"]) if req.get("ready_date") else now().date()
    unit_qty = {"TEU": float(m.get("teu") or 0), "FEU": float(m.get("teu") or 0) / 2, "shipments": 1.0, "loads": 1.0,
                "kg": float(m.get("weight_kg") or 0)}
    for o in options:
        a = await for_option(o["carrier_id"], o["mode"], req["origin"], req["destination"], day, unit_qty=unit_qty,
                             company_id=company_id)
        if a:
            o["allocation"] = a
