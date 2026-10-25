"""Partner portal (carriers / hauliers): assigned legs, milestone updates, POD upload, GPS position reports.

A partner user carries `carrier_id`; it sees only legs whose carrier_id matches, and only what a carrier needs
(route, cargo, addresses, references). Prices, margins and customs data are never returned.
"""
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from core.audit import audit
from core.db import db, register_index
from core.deps import current_user
from core.events import emit
from core.storage import put_file
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import geo, positions
from services import shipments as svc

router = APIRouter(prefix="/api/partner", tags=["partner"])
register_index("position_reports", [("shipment_id", 1), ("at", -1)])

POD_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
POD_MAX_BYTES = 15 * 1024 * 1024
# partner action -> milestone code per leg role
ACTIONS = {
    "pickup": {"picked_up": "picked_up", "departed": "picked_up", "arrived": "received", "delivered": "received"},
    "delivery": {"picked_up": "out_for_delivery", "departed": "out_for_delivery", "arrived": "delivered", "delivered": "delivered"},
    "main": {"picked_up": "picked_up", "departed": "departed", "arrived": "arrived", "delivered": "delivered"},
    "onward": {"picked_up": "departed", "departed": "departed", "arrived": "transshipment", "delivered": "transshipment"},
    "feeder": {"picked_up": "departed", "departed": "departed", "arrived": "arrived", "delivered": "arrived"},
}


async def require_partner_user(user: dict = Depends(current_user)) -> dict:
    if user.get("org_type") != "partner":
        raise HTTPException(403, "Not available for this account")
    if not user.get("carrier_id"):
        raise HTTPException(403, "This partner account is not linked to a carrier yet. Ask Huolitsija ops to link it.")
    return user


def _party(p: dict | None) -> dict | None:
    if not p:
        return None
    return {k: p.get(k) for k in ("name", "address", "contact_name", "phone", "email", "opening_hours", "instructions") if p.get(k)}


def _leg_view(lg: dict) -> dict:
    return {k: lg.get(k) for k in ("seq", "mode", "role", "service", "from", "to", "etd", "eta", "atd", "ata", "status", "truck_plate",
                                   "transport_doc_no", "container_numbers", "distance_km", "train", "flight", "voyage", "vessel",
                                   "last_report", "path", "accepted_at", "accepted_by")}


def _role(lg: dict) -> str:
    return lg.get("role") or "main"


def _next_action(s: dict, lg: dict) -> str | None:
    if s["status"] in ("delivered", "cancelled"):
        return None
    if lg.get("ata"):
        return None
    if not lg.get("atd"):
        return "picked_up"
    return "delivered" if _role(lg) in ("delivery", "main") and lg["seq"] == s["legs"][-1]["seq"] else "arrived"


async def _assigned(shipment_id: str, user: dict) -> tuple[dict, list[dict]]:
    s = await db.shipments.find_one({"id": shipment_id, "legs.carrier_id": user["carrier_id"]}, NO_ID)
    if not s:
        raise HTTPException(404, "No assigned leg on this shipment")
    legs = [lg for lg in s["legs"] if lg.get("carrier_id") == user["carrier_id"]]
    return s, legs


def _leg(legs: list[dict], seq: int) -> dict:
    lg = next((x for x in legs if x["seq"] == seq), None)
    if not lg:
        raise HTTPException(404, "This leg is not assigned to your company")
    return lg


@router.get("/legs")
async def partner_legs(state: str = "active", user: dict = Depends(require_partner_user)):
    q = {"legs.carrier_id": user["carrier_id"], "status": {"$ne": "cancelled"}}
    docs = await db.shipments.find(q, {"_id": 0, "legs.path": 0}).sort("etd", 1).to_list(1000)
    items = []
    for s in docs:
        for lg in s["legs"]:
            if lg.get("carrier_id") != user["carrier_id"]:
                continue
            done = bool(lg.get("ata")) or s["status"] == "delivered"
            if (state == "active" and done) or (state == "done" and not done):
                continue
            cargo = s.get("cargo") or {}
            items.append({
                "shipment_id": s["id"], "reference": s["reference"], "shipment_status": s["status"], "mode": s["mode"],
                "leg": {k: lg.get(k) for k in ("seq", "mode", "role", "service", "etd", "eta", "atd", "ata", "status", "truck_plate",
                                               "transport_doc_no", "distance_km", "accepted_at", "accepted_by")},
                "from": {k: lg["from"].get(k) for k in ("name", "city", "country", "unlocode", "address")},
                "to": {k: lg["to"].get(k) for k in ("name", "city", "country", "unlocode", "address")},
                "cargo": {k: cargo.get(k) for k in ("description", "packages", "package_type", "weight_kg", "volume_m3", "dangerous_goods",
                                                    "containers", "ldm")},
                "next_action": _next_action(s, lg), "holds": [h["type"] for h in s.get("holds") or []],
                "late": bool(lg.get("eta") and not lg.get("ata") and parse_iso(lg["eta"]) < now()),
            })
    items.sort(key=lambda i: (i["leg"].get("ata") is not None, i["leg"].get("etd") or ""))
    counts = {"active": 0, "done": 0, "to_accept": sum(1 for i in items if not i["leg"].get("accepted_at") and not i["leg"].get("atd"))}
    for s in docs:
        for lg in s["legs"]:
            if lg.get("carrier_id") == user["carrier_id"]:
                counts["done" if (lg.get("ata") or s["status"] == "delivered") else "active"] += 1
    carrier = await db.carriers.find_one({"id": user["carrier_id"]}, {"_id": 0, "id": 1, "name": 1, "code": 1})
    return {"items": items, "total": len(items), "counts": counts, "carrier": carrier or {"id": user["carrier_id"]}}


@router.get("/shipments/{shipment_id}")
async def partner_shipment(shipment_id: str, user: dict = Depends(require_partner_user)):
    s, legs = await _assigned(shipment_id, user)
    seqs = {lg["seq"] for lg in legs}
    cargo = s.get("cargo") or {}
    parties = s.get("parties") or {}
    refs = s.get("references") or {}
    pods = await db.documents.find({"shipment_id": s["id"], "type": "pod", "carrier_id": user["carrier_id"]},
                                   {"_id": 0, "id": 1, "filename": 1, "created_at": 1, "leg_seq": 1, "signed_by": 1, "size": 1}).to_list(50)
    return {
        "id": s["id"], "reference": s["reference"], "status": s["status"], "mode": s["mode"], "incoterm": s.get("incoterm"),
        "legs": [{**_leg_view(lg), "next_action": _next_action(s, lg)} for lg in legs],
        "other_legs": [{k: lg.get(k) for k in ("seq", "mode", "role", "from", "to", "etd", "eta", "atd", "ata")}
                       for lg in s["legs"] if lg["seq"] not in seqs],
        "cargo": {k: cargo.get(k) for k in ("description", "packages", "package_type", "weight_kg", "volume_m3", "dangerous_goods", "dg",
                                            "containers", "ldm", "temperature", "stackable")},
        "parties": {"shipper": _party(parties.get("shipper")), "consignee": _party(parties.get("consignee"))},
        "references": {"reference": s["reference"], "customer_ref": refs.get("customer_ref"), "po_numbers": refs.get("po_numbers") or [],
                       "transport_doc_nos": [lg.get("transport_doc_no") for lg in legs if lg.get("transport_doc_no")]},
        "milestones": [{k: m.get(k) for k in ("code", "label", "planned", "at", "source", "leg_seq")}
                       for m in s["milestones"] if m.get("leg_seq") in seqs],
        "holds": [{"type": h["type"]} for h in s.get("holds") or []],
        "pods": pods,
    }


class AcceptIn(BaseModel):
    truck_plate: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


@router.post("/shipments/{shipment_id}/legs/{seq}/accept")
async def partner_accept(shipment_id: str, seq: int, body: AcceptIn | None = None, user: dict = Depends(require_partner_user)):
    """The haulier confirms it will run the assigned leg (optionally with the truck plate). Ops sees who and when."""
    s, legs = await _assigned(shipment_id, user)
    lg = _leg(legs, seq)
    if s["status"] in ("cancelled", "delivered") or lg.get("ata"):
        raise HTTPException(409, "This leg is already complete or cancelled.")
    body = body or AcceptIn()
    sets = {"legs.$.accepted_at": lg.get("accepted_at") or now_iso(), "legs.$.accepted_by": user.get("name") or user["email"]}
    if body.truck_plate and body.truck_plate.strip():
        sets["legs.$.truck_plate"] = body.truck_plate.strip().upper()
    await db.shipments.update_one({"id": s["id"], "legs.seq": seq}, {"$set": sets})
    await audit(user, "shipment.leg_accepted", "shipment", s["id"], {"seq": seq, "truck_plate": sets.get("legs.$.truck_plate")},
                company_id=s["company_id"])
    await db.tracking_events.insert_one({"id": new_id(), "shipment_id": s["id"], "company_id": s["company_id"], "code": "leg_accepted",
                                         "label": f"Leg {seq} accepted by {user.get('carrier_name') or 'carrier'}", "at": now_iso(),
                                         "location": None, "source": "partner", "leg_seq": seq, "note": (body.note or "").strip() or None,
                                         "by": user.get("name") or user["email"], "raw": None})
    try:  # s2: a carrier booking requested through the partner portal is confirmed by this acceptance
        b = await db.raw.carrier_bookings.find_one({"shipment_id": s["id"], "leg_seq": lg.get("booking_parent_seq") or seq,
                                                    "carrier_id": user["carrier_id"],
                                                    "status": "requested"}, NO_ID)
        if b:
            from services import carrier_booking

            await carrier_booking.confirm_booking(
                b["id"], carrier_ref=f"PARTNER-{b['id'][:8].upper()}",
                actor={**user, "name": f"{user.get('name') or user.get('email')} (partner)"},
                details={"truck_plate": sets["legs.$.truck_plate"]} if sets.get("legs.$.truck_plate") else None)
    except Exception:  # noqa: BLE001 - the acceptance itself is already recorded; never fail it on the booking step
        logging.getLogger("partner").exception("carrier booking confirmation on leg accept failed")
    return await partner_shipment(shipment_id, user)


class PartnerMilestoneIn(BaseModel):
    action: str = Field(pattern="^(picked_up|departed|arrived|at_hub|delivered)$")  # at_hub = arrived at the leg's end terminal
    note: str | None = Field(default=None, max_length=500)
    at: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


def _at(at: str | None) -> str:
    if not at:
        return now_iso()
    dt = parse_iso(at)
    if not dt or dt > now() + timedelta(minutes=10):
        raise HTTPException(422, "The time must be now or in the past.")
    if dt < now() - timedelta(days=7):
        raise HTTPException(422, "Report events within 7 days; contact ops for older corrections.")
    return iso(dt)


async def _apply(s: dict, lg: dict, action: str, *, user: dict, note: str | None, at: str, lat=None, lon=None) -> dict:
    code = ACTIONS.get(_role(lg), ACTIONS["main"])[action]
    if code == "picked_up" and lg["mode"] == "road" and _role(lg) == "main":
        code = "departed"  # door-to-door truck: collecting the goods is the departure ("Collected at ...")
    if code == "delivered" and lg["seq"] != s["legs"][-1]["seq"]:
        code = "arrived"
    if code == "delivered" and not await db.documents.find_one({"shipment_id": s["id"], "type": "pod"}):
        raise HTTPException(409, "Upload the signed proof of delivery (POD) to confirm delivery.")
    loc = None
    if lat is not None and lon is not None:
        loc = {"name": f"{lat:.4f}, {lon:.4f}", "lat": lat, "lon": lon}
    elif code in ("received", "arrived", "delivered", "transshipment"):
        loc = lg["to"]
    elif code in ("picked_up", "departed", "out_for_delivery"):
        loc = lg["from"]
    actor = {**user, "name": f"{user.get('name') or user.get('email')} (partner)"}
    return await svc.record_milestone(s["id"], code, actor=actor, source="partner", note=note, at=at, leg_seq=lg["seq"], location=loc)


@router.post("/shipments/{shipment_id}/legs/{seq}/milestones")
async def partner_milestone(shipment_id: str, seq: int, body: PartnerMilestoneIn, user: dict = Depends(require_partner_user)):
    s, legs = await _assigned(shipment_id, user)
    lg = _leg(legs, seq)
    action = "arrived" if body.action == "at_hub" else body.action
    await _apply(s, lg, action, user=user, note=body.note, at=_at(body.at), lat=body.lat, lon=body.lon)
    return await partner_shipment(shipment_id, user)


@router.post("/shipments/{shipment_id}/legs/{seq}/pod")
async def partner_pod(shipment_id: str, seq: int, file: UploadFile = File(...), signed_by: str = Form(""), note: str = Form(""),
                      user: dict = Depends(require_partner_user)):
    s, legs = await _assigned(shipment_id, user)
    lg = _leg(legs, seq)
    if s["status"] in ("cancelled",):
        raise HTTPException(409, "The shipment is cancelled.")
    data = await file.read()
    ctype = (file.content_type or "").lower()
    if ctype not in POD_TYPES:
        raise HTTPException(415, "Upload the POD as a PDF or a photo (JPG, PNG, WEBP, HEIC).")
    if not data:
        raise HTTPException(422, "The file is empty.")
    if len(data) > POD_MAX_BYTES:
        raise HTTPException(413, "The file is larger than 15 MB. Send a smaller scan or photo.")
    fname = (file.filename or f"POD-{s['reference']}.pdf")[:180]
    file_id = await put_file(data, fname, ctype, {"shipment_id": s["id"], "type": "pod", "carrier_id": user["carrier_id"]})
    doc = {"id": new_id(), "company_id": s["company_id"], "shipment_id": s["id"], "type": "pod", "filename": fname, "content_type": ctype,
           "size": len(data), "file_id": file_id, "uploaded_by": {"id": user["id"], "name": user.get("name"), "org_type": "partner"},
           "source": "partner_portal", "carrier_id": user["carrier_id"], "leg_seq": lg["seq"], "signed_by": signed_by.strip() or None,
           "note": note.strip() or None, "extraction": {"status": "pending"}, "checks": [], "created_at": now_iso()}
    await db.documents.insert_one(dict(doc))
    await audit(user, "document.uploaded", "document", doc["id"], {"type": "pod", "shipment_id": s["id"]}, company_id=s["company_id"])
    await emit("document.uploaded", {"document": doc, "actor": user})
    doc.pop("_id", None)
    delivered = None
    if lg["seq"] == s["legs"][-1]["seq"] and s["status"] not in ("delivered",):
        delivered = await _apply(s, lg, "delivered", user=user, note=(f"POD signed by {signed_by.strip()}" if signed_by.strip() else "POD uploaded"),
                                 at=now_iso())
    return {"document": doc, "shipment_status": (delivered or s)["status"]}


class PositionIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    at: str | None = None
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)


@router.post("/shipments/{shipment_id}/legs/{seq}/position")
async def partner_position(shipment_id: str, seq: int, body: PositionIn, user: dict = Depends(require_partner_user)):
    s, legs = await _assigned(shipment_id, user)
    lg = _leg(legs, seq)
    if lg.get("ata") or s["status"] in ("delivered", "cancelled"):
        raise HTTPException(409, "This leg is complete; position reports are closed.")
    at = _at(body.at)
    path = lg.get("path") or []
    _frac, off_km = geo.project_fraction(path, body.lon, body.lat) if len(path) > 1 else (0, 0)
    report = {"lat": round(body.lat, 6), "lon": round(body.lon, 6), "at": at, "by": user.get("name") or user["email"],
              "accuracy_m": body.accuracy_m, "off_lane_km": off_km}
    await db.shipments.update_one({"id": s["id"], "legs.seq": seq}, {"$set": {"legs.$.last_report": report}})
    await db.position_reports.insert_one({"id": new_id(), "shipment_id": s["id"], "leg_seq": seq, "carrier_id": user["carrier_id"], **report})
    if not lg.get("atd") or (s["status"] == "picked_up" and _role(lg) == "main"):
        # a moving truck reporting its position has evidently left: record the departure with the partner as the source
        try:
            await _apply(s, lg, "departed", user=user, note="First GPS report", at=at, lat=body.lat, lon=body.lon)
        except HTTPException:
            pass
    s2 = await positions.refresh_shipment(s["id"])
    return {"position": (s2 or {}).get("position"), "progress": (s2 or {}).get("progress"), "off_lane_km": off_km}
