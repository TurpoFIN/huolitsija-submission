"""Shipments API: scoped lists, detail with tracking, ops actions, customer references."""
import math
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.audit import audit
from core.db import db, register_index
from core.deps import company_scope, current_user, is_internal, require_internal, require_perm
from core.util import NO_ID, new_id, now, now_iso, parse_iso
from services import shipments as svc
from services import tracking

import services.seed_shipments  # noqa: F401  (registers the demo seed, startup order 30)
import services.positions  # noqa: F401  (registers the position loop)

register_index("shipment_notes", [("shipment_id", 1), ("at", -1)])

router = APIRouter(prefix="/api", tags=["shipments"])

LIST_FIELDS = {"_id": 0, "legs.path": 0, "milestones.location": 0}


# --------------------------------------------------------------------------------------------- shaping

def delay_days(s: dict) -> int:
    eta, planned = parse_iso(s.get("eta")), parse_iso(s.get("eta_planned"))
    if not eta or not planned:
        return 0
    d = (eta - planned).total_seconds() / 86400
    return math.ceil(d - 0.25) if d > 0.25 else 0


def main_leg(s: dict) -> dict | None:
    legs = s.get("legs") or []
    mains = [lg for lg in legs if lg.get("role") not in ("pickup", "delivery")]
    return (mains or legs or [None])[0] if not mains else max(mains, key=lambda lg: lg.get("distance_km") or 0)


def needs_action(s: dict) -> list[str]:
    """Reasons ops must act; empty when the shipment runs on plan."""
    if s["status"] in ("delivered", "cancelled"):
        return []
    out = []
    if s["status"] == "exception" or s.get("has_exception"):
        out.append("exception")
    if s.get("holds"):
        out.append("hold")
    soon = now() + timedelta(days=3)
    if any(not lg.get("carrier") and not lg.get("atd") and parse_iso(lg.get("etd")) and parse_iso(lg["etd"]) < soon
           for lg in s.get("legs") or []):
        out.append("missing_carrier")  # a leg starting within 3 days has no carrier
    eta = parse_iso(s.get("eta"))
    if eta and eta < now():
        out.append("overdue")
    elif delay_days(s) >= 1:
        out.append("late")
    nxt = next((m for m in s.get("milestones") or [] if not m.get("at")), None)
    if nxt and nxt.get("planned") and parse_iso(nxt["planned"]) < now() - timedelta(hours=12) \
            and nxt["code"] not in svc.SCHEDULE_ACHIEVABLE:
        out.append("milestone_overdue")
    return out


CUSTOMER_ACTIONS = {"exception", "hold", "overdue", "late"}


def summary(s: dict, last_event: dict | None = None, *, internal: bool = True) -> dict:
    ml = main_leg(s) or {}
    nxt = next((m for m in s.get("milestones") or [] if not m.get("at")), None)
    last = last_event or next((m for m in reversed(s.get("milestones") or []) if m.get("at")), None)
    return {
        "id": s["id"], "reference": s["reference"], "company_id": s["company_id"], "customer_name": s.get("customer_name"),
        "mode": s["mode"], "direction": s.get("direction"), "status": s["status"], "milestone": s.get("milestone"),
        "origin": {k: s["origin"].get(k) for k in ("name", "code", "unlocode", "country", "city")},
        "destination": {k: s["destination"].get(k) for k in ("name", "code", "unlocode", "country", "city")},
        "modes": list(dict.fromkeys(lg["mode"] for lg in s.get("legs") or [])),
        "carrier": ml.get("carrier"), "carrier_id": ml.get("carrier_id"),
        "vessel": (ml.get("vessel") or {}).get("name") if ml.get("vessel") else None, "flight": ml.get("flight"),
        "etd": s.get("etd"), "eta": s.get("eta"), "eta_planned": s.get("eta_planned"), "atd": s.get("atd"), "delivered_at": s.get("delivered_at"),
        "delay_days": delay_days(s), "progress": s.get("progress"),
        "position": {k: (s.get("position") or {}).get(k) for k in ("source", "updated_at")},
        "next_milestone": {"code": nxt["code"], "label": nxt["label"], "planned": nxt.get("planned")} if nxt else None,
        "last_event": {"code": last.get("code"), "label": last.get("label"), "at": last.get("at"), "source": last.get("source")} if last else None,
        "updated_at": s.get("updated_at"), "has_exception": bool(s.get("has_exception")), "holds": [h["type"] for h in s.get("holds") or []],
        "needs_action": [r for r in needs_action(s) if internal or r in CUSTOMER_ACTIONS], "references": s.get("references") or {},
        "weight_kg": s.get("weight_kg"), "teu": s.get("teu"),
        "volume_m3": s.get("volume_m3"), "cargo_description": (s.get("cargo") or {}).get("description"), "legs_count": len(s.get("legs") or []),
        "demo": bool(s.get("demo")),
    }


def _for_customer(s: dict) -> dict:
    s = dict(s)
    s.pop("created_by", None)
    for k in ("compliance_hold_code", "compliance_status", "compliance_screening_id"):  # s12: the customer sees the neutral reason only
        s.pop(k, None)
    s["legs"] = [{k: v for k, v in lg.items() if k not in ("last_report",)} for lg in s.get("legs") or []]
    return s


async def _get_scoped(shipment_id: str, user: dict) -> dict:
    if user.get("org_type") == "partner":
        raise HTTPException(403, "Partners use the assigned-legs view.")
    s = await db.shipments.find_one({"id": shipment_id, **company_scope(user)}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


# --------------------------------------------------------------------------------------------- list & detail

@router.get("/shipments/meta")
async def shipments_meta(user: dict = Depends(current_user)):
    """State machine and vocabularies for the UI (the same constants the server enforces)."""
    return {"statuses": svc.STATUSES, "transitions": {k: sorted(v) for k, v in svc.TRANSITIONS.items()},
            "milestones": svc.MILESTONE_LABELS, "status_for_milestone": svc.STATUS_FOR_MILESTONE,
            "hold_types": sorted(svc.HOLD_TYPES), "customs_hold_types": sorted(svc.CUSTOMS_HOLD_TYPES)}


@router.get("/shipments")
async def list_shipments(status: str | None = None, mode: str | None = None, q: str | None = None, company_id: str | None = None,
                         needs_action_only: bool = Query(False, alias="needs_action"), include_cancelled: bool = False,
                         limit: int = 200, offset: int = 0, user: dict = Depends(current_user)):
    if user.get("org_type") == "partner":
        raise HTTPException(403, "Partners use the assigned-legs view.")
    base = dict(company_scope(user))
    if company_id and is_internal(user):
        base["company_id"] = company_id
    if mode:
        base["$or"] = [{"mode": mode}, {"legs.mode": mode}] if mode != "multimodal" else [{"mode": "multimodal"}]
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        text_or = [{"reference": rx}, {"customer_name": rx}, {"origin.name": rx}, {"destination.name": rx}, {"origin.unlocode": rx},
                   {"destination.unlocode": rx}, {"references.customer_ref": rx}, {"references.po_numbers": rx},
                   {"cargo.description": rx}, {"legs.carrier": rx}, {"legs.vessel.name": rx}, {"legs.transport_doc_no": rx},
                   {"legs.container_numbers": rx}]
        base = {"$and": [base, {"$or": text_or}]} if "$or" in base else {**base, "$or": text_or}
    unfiltered = base
    if not include_cancelled and status != "cancelled":
        base = {"$and": [base, {"status": {"$ne": "cancelled"}}]}
    docs = await db.shipments.find(base, LIST_FIELDS).sort("created_at", -1).to_list(2000)
    items_all = [summary(s, internal=is_internal(user)) for s in docs]
    counts = {"all": len(items_all), "needs_action": sum(1 for i in items_all if i["needs_action"])}
    for st in svc.STATUSES:
        counts[st] = sum(1 for i in items_all if i["status"] == st)
    if not counts["cancelled"]:  # cancelled ones are hidden from "all" but still counted for their own tab
        counts["cancelled"] = await db.shipments.count_documents({"$and": [unfiltered, {"status": "cancelled"}]})
    counts["active"] = sum(1 for i in items_all if i["status"] not in ("delivered", "cancelled"))
    items = items_all
    if status == "active":
        items = [i for i in items if i["status"] not in ("delivered", "cancelled")]
    elif status:
        items = [i for i in items if i["status"] == status]
    if needs_action_only:
        items = [i for i in items if i["needs_action"]]
    total = len(items)
    return {"items": items[offset: offset + max(1, min(limit, 1000))], "total": total, "counts": counts, "generated_at": now_iso()}


@router.get("/shipments/{shipment_id}")
async def get_shipment(shipment_id: str, user: dict = Depends(current_user)):
    s = await _get_scoped(shipment_id, user)
    events = await db.tracking_events.find({"shipment_id": s["id"], "code": {"$ne": "position"}}, {"_id": 0, "raw": 0}) \
        .sort([("at", 1), ("_id", 1)]).to_list(500)  # _id keeps insertion order for events in the same second
    out = s if is_internal(user) else _for_customer(s)
    out["tracking_events"] = events
    out["delay_days"] = delay_days(s)
    out["needs_action"] = [r for r in needs_action(s) if is_internal(user) or r in CUSTOMER_ACTIONS]
    if is_internal(user):
        out["allowed_transitions"] = sorted(svc.TRANSITIONS.get(s["status"], set()))
        out["internal_notes"] = await db.shipment_notes.find({"shipment_id": s["id"]}, {"_id": 0, "shipment_id": 0}) \
            .sort("at", -1).to_list(200)
    else:
        out.pop("internal_notes", None)
    if is_internal(user):
        company = await db.companies.find_one({"id": s["company_id"]}, {"_id": 0, "id": 1, "name": 1, "kyc_status": 1, "sanctions_status": 1,
                                                                        "business_id": 1})
        out["company"] = company
        out["tracking_adapters"] = []
        for lg in s.get("legs") or []:
            a = await tracking.adapter_for(lg)
            out["tracking_adapters"].append({"seq": lg["seq"], "adapter": a.key if a else None, "name": a.name if a else None,
                                             "configured": a.configured() if a else False})
    return out


@router.get("/shipments/{shipment_id}/events")
async def shipment_events(shipment_id: str, user: dict = Depends(current_user)):
    s = await _get_scoped(shipment_id, user)
    events = await db.tracking_events.find({"shipment_id": s["id"]}, {"_id": 0, "raw": 0}).sort([("at", 1), ("_id", 1)]).to_list(1000)
    return {"items": events, "total": len(events)}


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/shipments/{shipment_id}/notes")
async def post_note(shipment_id: str, body: NoteIn, user: dict = Depends(require_internal)):
    """Internal note for the Huolitsija team (never shown to the customer or the partner)."""
    s = await _get_scoped(shipment_id, user)
    note = {"id": new_id(), "shipment_id": s["id"], "text": body.text.strip(), "at": now_iso(),
            "by": {"id": user["id"], "name": user.get("name") or user.get("email")}}
    if not note["text"]:
        raise HTTPException(422, "Write the note first.")
    await db.shipment_notes.insert_one(dict(note))
    await audit(user, "shipment.note", "shipment", s["id"], {"note_id": note["id"]}, company_id=s["company_id"])
    note.pop("shipment_id", None)
    note.pop("_id", None)
    return note


# --------------------------------------------------------------------------------------------- ops create

class ManualShipmentIn(BaseModel):
    company_id: str
    mode: str = Field(pattern="^(sea|road|air|rail)$")
    origin: str | dict
    destination: str | dict
    incoterm: str | None = None
    incoterm_place: str | None = None
    direction: str | None = None
    etd: str | None = None
    eta: str | None = None
    transit_days: float | None = None
    carrier: str | None = None
    carrier_id: str | None = None
    service: str | None = None
    pickup_address: dict | None = None
    delivery_address: dict | None = None
    cargo: dict = Field(default_factory=dict)
    parties: dict = Field(default_factory=dict)
    references: dict = Field(default_factory=dict)
    customs_clearance: bool = True
    total_cents: int | None = None


@router.post("/shipments")
async def post_manual_shipment(body: ManualShipmentIn, user: dict = Depends(require_perm("shipments.write"))):
    """Answers within 25 s; a slower create finishes in the background and is polled at /api/requests/{id}."""
    from core.bounded import bounded
    return await bounded("shipment.create", create_manual_shipment(body, user), user=user)


async def create_manual_shipment(body: ManualShipmentIn, user: dict):
    """Ops books a shipment by hand (phone/e-mail booking). Same planning and guards as a portal booking."""
    quote = {"id": None, "source": "ops", "request": {"mode": body.mode, "origin": body.origin, "destination": body.destination,
                                                     "incoterm": body.incoterm, "incoterm_place": body.incoterm_place,
                                                     "direction": body.direction, "cargo": body.cargo,
                                                     "customs_clearance": body.customs_clearance, "ready_date": body.etd}}
    option = {"id": None, "mode": body.mode, "carrier_name": body.carrier, "carrier_id": body.carrier_id, "service": body.service,
              "etd": body.etd, "eta": body.eta, "transit_days": body.transit_days, "total_cents": body.total_cents}
    booking = {"pickup_address": body.pickup_address, "delivery_address": body.delivery_address, "parties": body.parties,
               "references": body.references}
    shipment = await svc.create_shipment(company_id=body.company_id, quote=quote, option=option, booking=booking, actor=user)
    from services import party_screening  # s12: screen every party; a potential match holds the shipment

    await party_screening.screen_booked_shipment(shipment["id"], user)
    return await svc.get_shipment(shipment["id"])


# --------------------------------------------------------------------------------------------- ops actions

class StatusIn(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=1000)
    milestone: str | None = None
    location: dict | None = None
    leg_seq: int | None = None


class MilestoneIn(BaseModel):
    code: str
    note: str | None = Field(default=None, max_length=1000)
    at: str | None = None
    leg_seq: int | None = None
    location: dict | None = None


class HoldIn(BaseModel):
    type: str
    reason: str = Field(min_length=2, max_length=500)


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ReferencesIn(BaseModel):
    customer_ref: str | None = Field(default=None, max_length=80)
    po_numbers: list[str] | None = None


def _check_at(at: str | None) -> str | None:
    if not at:
        return None
    dt = parse_iso(at)
    if not dt:
        raise HTTPException(422, "Time must be an ISO date and time.")
    if dt > now() + timedelta(minutes=10):
        raise HTTPException(422, "An achieved milestone cannot be in the future.")
    from core.util import iso

    return iso(dt)


@router.post("/shipments/{shipment_id}/status")
async def post_status(shipment_id: str, body: StatusIn, user: dict = Depends(require_perm("shipments.write"))):
    if body.status == "exception" and not (body.note or "").strip():
        raise HTTPException(422, "Describe the exception so the customer and the team know what happened.")
    return await svc.set_status(shipment_id, body.status, actor=user, note=body.note, milestone=body.milestone, location=body.location,
                                leg_seq=body.leg_seq)


@router.post("/shipments/{shipment_id}/milestones")
async def post_milestone(shipment_id: str, body: MilestoneIn, user: dict = Depends(require_perm("shipments.write"))):
    if body.code not in svc.MILESTONE_LABELS:
        raise HTTPException(422, f"Unknown milestone {body.code}.")
    if body.code in ("cancelled", "exception", "customs_hold", "hold_added", "hold_released", "leg_accepted", "position", "eta_changed",
                     "note", "booked"):
        raise HTTPException(422, "Use the status, hold or cancel actions for this.")
    return await svc.record_milestone(shipment_id, body.code, actor=user, note=body.note, at=_check_at(body.at), leg_seq=body.leg_seq,
                                      location=body.location)


@router.patch("/shipments/{shipment_id}/legs/{seq}")
async def patch_leg(shipment_id: str, seq: int, body: dict, user: dict = Depends(require_perm("shipments.write"))):
    return await svc.update_leg(shipment_id, seq, body, user)


@router.post("/shipments/{shipment_id}/holds")
async def post_hold(shipment_id: str, body: HoldIn, user: dict = Depends(require_internal)):
    if body.type == "sanctions" and "kyc.approve" not in (user.get("permissions") or []) and "shipments.write" not in (user.get("permissions") or []):
        raise HTTPException(403, "Missing permission: shipments.write")
    if body.type != "sanctions" and "shipments.write" not in (user.get("permissions") or []) and "customs.review" not in (user.get("permissions") or []):
        raise HTTPException(403, "Missing permission: shipments.write")
    return await svc.add_hold(shipment_id, body.type, body.reason, user)


@router.delete("/shipments/{shipment_id}/holds/{hold_type}")
async def delete_hold(shipment_id: str, hold_type: str, user: dict = Depends(require_internal)):
    perms = set(user.get("permissions") or [])
    if hold_type == "sanctions" and "kyc.approve" not in perms:
        raise HTTPException(403, "Only compliance (kyc.approve) can release a sanctions hold.")
    if hold_type != "sanctions" and not perms & {"shipments.write", "customs.review"}:
        raise HTTPException(403, "Missing permission: shipments.write")
    return await svc.release_hold(shipment_id, hold_type, user)


@router.post("/shipments/{shipment_id}/cancel")
async def post_cancel(shipment_id: str, body: CancelIn, user: dict = Depends(require_perm("shipments.write"))):
    return await svc.cancel_shipment(shipment_id, actor=user, reason=body.reason)


@router.patch("/shipments/{shipment_id}/references")
async def patch_references(shipment_id: str, body: ReferencesIn, user: dict = Depends(current_user)):
    s = await _get_scoped(shipment_id, user)
    refs = dict(s.get("references") or {})
    if body.customer_ref is not None:
        refs["customer_ref"] = body.customer_ref.strip() or None
    if body.po_numbers is not None:
        pos = []
        for p in body.po_numbers:
            p = (p or "").strip()
            if p and p not in pos:
                if len(p) > 40:
                    raise HTTPException(422, "A PO number can be at most 40 characters.")
                pos.append(p)
        if len(pos) > 30:
            raise HTTPException(422, "Add at most 30 PO numbers.")
        refs["po_numbers"] = pos
    await db.shipments.update_one({"id": s["id"]}, {"$set": {"references": refs, "updated_at": now_iso()}})
    await audit(user, "shipment.references", "shipment", s["id"], {"references": refs}, company_id=s["company_id"])
    return {"references": refs}


@router.get("/tracking/adapters")
async def tracking_adapters(user: dict = Depends(require_internal)):
    return {"items": tracking.adapter_statuses()}


class CarrierTestBookingBody(BaseModel):
    leg_seq: int = Field(ge=1)
    carrier_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="Operator carrier API acceptance test", max_length=500)


async def _carrier_test_scope(shipment_id: str, carrier_id: str, user: dict):
    """Only explicit provider-test configuration may be exercised manually here.

    Live customer bookings continue through the approved booking/agent workflow.
    No request field can turn a production customer into a test tenant.
    """
    from services import carrier_booking as cb
    if not is_internal(user):
        raise HTTPException(403, "Internal operators only.")
    shipment = await _get_scoped(shipment_id, user)
    company = await db.raw.companies.find_one({"id": shipment.get("company_id")}, NO_ID)
    carrier = await db.raw.carriers.find_one({"id": carrier_id}, NO_ID)
    if not carrier:
        raise HTTPException(404, "Carrier not found")
    if not cb._test_data(company, shipment):
        raise HTTPException(409, "Production customers cannot use a carrier test API.")
    if not cb._test_api(carrier) or not cb._dcsa_live(carrier):
        raise HTTPException(409, "An enabled official carrier test API is required for manual acceptance testing.")
    return shipment, carrier


@router.post("/shipments/{shipment_id}/carrier-test-bookings")
async def carrier_test_book(shipment_id: str, body: CarrierTestBookingBody,
                            user: dict = Depends(require_perm("shipments.write"))):
    from services import carrier_booking as cb
    shipment, carrier = await _carrier_test_scope(shipment_id, body.carrier_id, user)
    leg = next((leg for leg in shipment.get("legs", []) if leg.get("seq") == body.leg_seq), None)
    if not leg:
        raise HTTPException(404, "Shipment leg not found")
    if not cb._dcsa_fits(shipment, leg):
        raise HTTPException(409, "DCSA booking requires a container sea leg.")
    return await cb.book_leg(shipment_id, body.leg_seq, carrier["id"], user, reason=body.reason)


@router.post("/shipments/{shipment_id}/carrier-test-bookings/{booking_id}/status")
async def carrier_test_status(shipment_id: str, booking_id: str,
                              user: dict = Depends(require_perm("shipments.write"))):
    from services import carrier_booking as cb
    booking = await db.raw.carrier_bookings.find_one({"id": booking_id, "shipment_id": shipment_id}, NO_ID)
    if not booking:
        raise HTTPException(404, "Carrier booking not found")
    await _carrier_test_scope(shipment_id, booking["carrier_id"], user)
    if booking.get("provider_environment") != "test" or booking.get("adapter") != "dcsa":
        raise HTTPException(409, "Only official carrier test API bookings can be checked here.")
    return await cb.poll_booking(booking_id)


@router.get("/shipments/{shipment_id}/carrier-test-bookings/{booking_id}/events")
async def carrier_test_events(shipment_id: str, booking_id: str,
                              user: dict = Depends(require_perm("shipments.read_all"))):
    """Historical test-dispatch receipts remain readable after carrier disablement.

    Counts cover this booking's audited dispatches, not the global event bus.
    Pending receipts are reported separately and never counted as dispatched.
    """
    if not is_internal(user):
        raise HTTPException(403, "Internal operators only.")
    await _get_scoped(shipment_id, user)
    booking = await db.raw.carrier_bookings.find_one({"id": booking_id, "shipment_id": shipment_id}, NO_ID)
    if not booking:
        raise HTTPException(404, "Carrier booking not found")
    if booking.get("provider_environment") != "test":
        raise HTTPException(409, "Only carrier test API events are available here.")
    rows = await db.raw.carrier_test_events.find({"booking_id": booking_id, "shipment_id": shipment_id}, NO_ID).sort("at", 1).to_list(100)
    counts = {}
    for row in rows:
        if row.get("dispatch_status") == "dispatched":
            name = row["event"]
            counts[name] = counts.get(name, 0) + 1
    return {"items": rows, "total": len(rows), "counts": counts,
            "pending": sum(row.get("dispatch_status") != "dispatched" for row in rows),
            "production_event_count": sum(n for name, n in counts.items() if not name.startswith("carrier.test_")),
            "scope": "This test booking's audited dispatches; not global production traffic"}
