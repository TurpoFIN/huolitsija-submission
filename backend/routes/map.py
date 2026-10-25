"""Map data API, fixed contract: shared/map-data-contract.md.

GET /api/map/shipments?scope=customer|ops   active + recently delivered shipments with lane geometry and positions
                                            (partner accounts: scope=partner, shipments with a leg assigned to them)
GET /api/map/stream?scope=customer|ops      Server-Sent Events: event "position" {id, position, progress, status};
                                            ": heartbeat" comment at least every 10 s. Clients fall back to polling every 30 s.
"""
import asyncio
import json
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.db import db
from core.deps import current_user, is_internal
from core.util import iso, now, now_iso
from services import geo, positions

router = APIRouter(prefix="/api", tags=["map"])
HEARTBEAT_S = 10
RECENT_DELIVERED_DAYS = 14
MAP_STATUSES = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception"]


def _scope_filter(user: dict, scope: str | None, company_id: str | None = None) -> tuple[str, dict]:
    org = user.get("org_type")
    if scope == "portal":  # the portal's name for the customer scope
        scope = "customer"
    if org == "partner":
        # a haulier sees the shipments that have a leg assigned to its carrier (same contract shape)
        if scope not in (None, "partner"):
            raise HTTPException(403, "Partner accounts see the partner map (scope=partner).")
        if not user.get("carrier_id"):
            raise HTTPException(403, "This partner account is not linked to a carrier yet.")
        return "partner", {"legs.carrier_id": user["carrier_id"]}
    scope = scope or ("ops" if org == "internal" else "customer")
    if scope == "partner":
        raise HTTPException(403, "The partner map is for partner accounts.")
    if scope not in ("customer", "ops"):
        raise HTTPException(422, "scope must be customer, ops or partner")
    if scope == "ops":
        if not is_internal(user):
            raise HTTPException(403, "The ops map is for Huolitsija staff.")
        return scope, ({"company_id": company_id} if company_id else {})
    if is_internal(user):
        return scope, ({"company_id": company_id} if company_id else {})
    return scope, {"company_id": user.get("company_id")}


UNLOCODE_RE = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
UNASSIGNED_CARRIER = "Not assigned"


def _unlocode(p: dict) -> str | None:
    for c in (p.get("unlocode"), p.get("code"), p.get("terminal_code")):
        if isinstance(c, str) and UNLOCODE_RE.match(c):
            return c
    # e.g. a rail station or a street address: the UN/LOCODE of its town
    hit = geo.search(p.get("city") or p.get("name") or "", limit=1, country=p.get("country")) if p.get("name") else []
    return hit[0]["unlocode"] if hit and hit[0].get("unlocode") and UNLOCODE_RE.match(hit[0]["unlocode"]) else None


def _place(p: dict) -> dict:
    return {"name": p.get("name"), "unlocode": _unlocode(p), "lat": p.get("lat"), "lon": p.get("lon")}


def _leg_mode_summary(legs: list[dict], fallback: str) -> str:
    modes = {lg["mode"] for lg in legs}
    return "multimodal" if len(modes) > 1 else (next(iter(modes)) if modes else fallback)


def contract_item(s: dict) -> dict:
    """One shipment exactly as shared/map-data-contract.md (validated by s2's fixtures/validate.mjs).

    Contract choices: `mode` describes the legs on the map (door-to-door sea freight with trucking is "multimodal"; the
    shipment API keeps the booked product mode); `has_exception` preserves the stored exception flag even while freight is moving;
    the map ring means an open exception needs attention; a leg without a carrier yet
    reads "Not assigned"; paths are densified to <= 40 km segments with longitudes in [-180, 180]."""
    pos = s.get("position") or {}
    legs = s.get("legs") or []
    status = s["status"]
    return {
        "id": s["id"], "reference": s["reference"], "customer_name": s.get("customer_name"),
        "mode": _leg_mode_summary(legs, s["mode"]), "status": status,
        "origin": _place(s["origin"]), "destination": _place(s["destination"]),
        "legs": [{"seq": lg["seq"], "mode": lg["mode"],
                  "from": {"name": lg["from"].get("name"), "lat": lg["from"].get("lat"), "lon": lg["from"].get("lon")},
                  "to": {"name": lg["to"].get("name"), "lat": lg["to"].get("lat"), "lon": lg["to"].get("lon")},
                  "path": geo.wrap_path(geo.densify(lg.get("path") or [[lg["from"]["lon"], lg["from"]["lat"]], [lg["to"]["lon"], lg["to"]["lat"]]])),
                  "carrier": lg.get("carrier") or UNASSIGNED_CARRIER, "etd": lg.get("etd"), "eta": lg.get("eta"),
                  "atd": lg.get("atd"), "ata": lg.get("ata")} for lg in legs],
        "position": {"lat": pos.get("lat"), "lon": pos.get("lon"), "updated_at": pos.get("updated_at"), "source": pos.get("source")},
        "progress": s.get("progress") if s.get("progress") is not None else 0.0,
        "eta_prediction": s.get("eta_prediction"),
        "eta": s.get("eta"), "weight_kg": s.get("weight_kg"), "teu": s.get("teu"),
        "has_exception": status == "exception" or bool(s.get("has_exception")),
    }


def map_query(base: dict) -> dict:
    since = iso(now() - timedelta(days=RECENT_DELIVERED_DAYS))
    return {**base, "$or": [{"status": {"$in": [s for s in MAP_STATUSES if s != "delivered"]}},
                            {"status": "delivered", "delivered_at": {"$gte": since}}]}


@router.get("/map/shipments")
async def map_shipments(scope: str | None = None, company_id: str | None = None, user: dict = Depends(current_user)):
    _, base = _scope_filter(user, scope, company_id)
    docs = await db.shipments.find(map_query(base), {"_id": 0, "milestones": 0}).sort("eta", 1).to_list(2000)
    return {"generated_at": now_iso(), "shipments": [contract_item(s) for s in docs]}


@router.get("/map/stream")
async def map_stream(request: Request, scope: str | None = None, company_id: str | None = None, user: dict = Depends(current_user)):
    sc, base = _scope_filter(user, scope, company_id)
    only_company = base.get("company_id")
    allowed: set | None = None
    if sc == "partner":
        allowed = {d["id"] async for d in db.shipments.find(base, {"_id": 0, "id": 1})}
    q = positions.subscribe()

    async def gen():
        try:
            yield "retry: 5000\n\n"
            # Current positions first, so a client that connects mid-way has a complete picture.
            async for s in db.shipments.find(map_query(base), {"_id": 0, "id": 1, "position": 1, "progress": 1, "status": 1, "company_id": 1}):
                yield _sse(positions.event_payload(s))
            loop = asyncio.get_running_loop()
            last_sent = loop.time()
            while True:
                if await request.is_disconnected():
                    break
                # The ping clock runs from the last byte this client received: events for other companies are
                # filtered out below and must not postpone it.
                wait = HEARTBEAT_S - (loop.time() - last_sent)
                ev = None
                if wait > 0:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=wait)
                    except asyncio.TimeoutError:
                        ev = None
                if ev is not None and not (only_company and ev.get("company_id") != only_company) \
                        and not (allowed is not None and ev.get("id") not in allowed):
                    yield _sse({k: ev[k] for k in ("id", "position", "progress", "status")})
                    last_sent = loop.time()
                elif loop.time() - last_sent >= HEARTBEAT_S:
                    yield f": heartbeat {now_iso()}\n\n"
                    last_sent = loop.time()
        finally:
            positions.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _sse(data: dict) -> str:
    return f"event: position\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
