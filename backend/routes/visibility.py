"""Read-only visibility projections, same company/partner scope as the existing map."""
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from core.db import db
from core.deps import current_user, require_perm
from core.util import NO_ID, now, parse_iso, iso
from datetime import timedelta
from routes.map import _scope_filter
from services.visibility import scheduling  # registers durable jobs
from services.visibility.eta import port_congestion
from services.tracking import adapter_statuses

router = APIRouter(prefix="/api/visibility", tags=["visibility"])


def milestones(s):
    return [{**m, "state": "achieved" if m.get("at") and m.get("source") != "schedule" else "planned",
             "overdue": bool(not m.get("at") and parse_iso(m.get("planned")) and parse_iso(m["planned"]) < now())}
            for m in s.get("milestones") or []]


@router.get("/status")
async def status(user=Depends(require_perm("shipments.read_all"))):
    rows = await db.raw.visibility_feeds.find({}, NO_ID).to_list(30)
    feeds = {x["provider"]: x for x in rows}
    for name, configured in (("aisstream", bool(os.getenv("AISSTREAM_API_KEY"))), ("vesselapi", bool(os.getenv("VESSELAPI_API_KEY"))),
                             ("adsb.lol", True), ("adsb.fi", True)):
        feeds.setdefault(name, {"provider": name, "configured": configured, "state": "not_observed" if configured else "not_configured"})
    return {"feeds": list(feeds.values()), "carriers": adapter_statuses(), "generated_at": iso(now())}


@router.get("/shipments/{shipment_id}")
async def shipment(shipment_id: str, user=Depends(current_user)):
    _, scope = _scope_filter(user, None)
    s = await db.shipments.find_one({**scope, "id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    exceptions = await db.exceptions.find({"shipment_id": shipment_id, "customer_visible": True,
                                           "status": {"$in": ["open", "in_progress"]}}, NO_ID).to_list(30)
    return {"shipment_id": s["id"], "reference": s["reference"], "position": s.get("position"),
            "eta_prediction": s.get("eta_prediction") or {"state": "unavailable", "reason": "not_yet_observed"},
            "milestones": milestones(s), "exceptions": [{k: e.get(k) for k in ("id", "rule", "title", "title_i18n", "status")} for e in exceptions]}


@router.get("/vessels")
async def vessels(user=Depends(require_perm("shipments.read_all"))):
    at = now()
    return {"positions": await db.raw.vessel_positions.find({"at": {"$gte": iso(at-timedelta(minutes=15)), "$lte": iso(at)}}, NO_ID).sort("at", -1).limit(500).to_list(500)}


@router.get("/flights")
async def flights(user=Depends(require_perm("shipments.read_all"))):
    at = now()
    # Area polls run every 240 s (provider daily limit); each position keeps its own source timestamp.
    return {"positions": await db.raw.flight_positions.find({"at": {"$gte": iso(at-timedelta(minutes=5)), "$lte": iso(at)}}, NO_ID).sort("at", -1).limit(500).to_list(500)}


@router.get("/ports/{unlocode}/congestion")
async def congestion(unlocode: str, user=Depends(require_perm("shipments.read_all"))):
    return await port_congestion(unlocode.upper())
