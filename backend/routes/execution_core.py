"""Execution API: execution record per shipment, SI, VGM, cut-offs, alerts, and the DCSA carrier callbacks."""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.db import db
from core.deps import require_perm
from core.util import NO_ID
from services.execution import cutoffs, dcsa, si_vgm
from core.settings import get_settings

router = APIRouter(prefix="/api/execution", tags=["execution"])
read = require_perm("shipments.read_all")
write = require_perm("shipments.write")


def _public_si(si: dict | None) -> dict | None:
    if not si:
        return None
    return {k: si.get(k) for k in ("id", "version", "spec", "status", "channel", "carrier_reference", "carrier_status", "feedbacks",
                                   "validation_errors", "submitted_at", "email_status", "message_id", "history", "http", "error",
                                   "created_at", "updated_at", "payload")}


async def _view(shipment_id: str) -> dict:
    rec = await si_vgm.get_record(shipment_id)
    if not rec:
        return {"record": None, "cutoffs": [], "si": None, "vgm": [], "channel": None}
    settings = await get_settings()
    si = await db.shipping_instructions.find_one({"record_id": rec["id"]}, NO_ID, sort=[("version", -1)])
    vgm = await db.vgm_declarations.find({"record_id": rec["id"]}, NO_ID).sort("created_at", -1).to_list(50)
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    rec.pop("booking", None)
    return {"record": rec, "cutoffs": cutoffs.status(rec, settings.get("execution_cutoff_alert_hours")), "si": _public_si(si),
            "vgm": vgm, "channel": dcsa.channel_status(rec.get("carrier_code") or ""),
            "si_missing": si_vgm.si_missing(rec, s) if rec.get("si_required") else [],
            "vgm_missing": [m for c in rec["containers"] for m in si_vgm.vgm_missing(rec, c)
                            if c.get("vgm_status") not in ("submitted", "accepted")] if rec.get("vgm_required") else []}


@router.get("/shipments/{shipment_id}")
async def execution_for_shipment(shipment_id: str, user: dict = Depends(read)):
    return await _view(shipment_id)


class StartIn(BaseModel):
    carrier_booking_reference: str | None = Field(default=None, max_length=35)
    carrier_code: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    etd: str | None = None
    equipment: list[dict] | None = None
    container_numbers: list[str] | None = None
    cutoffs: list[dict] | None = None
    vgm_signatory: str | None = Field(default=None, max_length=255)


@router.post("/shipments/{shipment_id}/start")
async def start_execution(shipment_id: str, body: StartIn, user: dict = Depends(write)):
    """Start execution from a carrier booking confirmation (the carrier booking agent calls the same function on carrier.booked)."""
    await si_vgm.start_for_booking(shipment_id, body.model_dump(exclude_none=True), actor=user)
    return await _view(shipment_id)


class ContainerIn(BaseModel):
    equipment_reference: str | None = Field(default=None, max_length=11)
    seal_number: str | None = Field(default=None, max_length=15)
    iso_type: str | None = Field(default=None, max_length=6)
    tare_kg: float | None = None
    cargo_gross_kg: float | None = None
    packages: int | None = Field(default=None, ge=1)
    weighbridge_gross_kg: float | None = None
    weighed_at: str | None = None
    certificate: str | None = Field(default=None, max_length=70)
    vgm_signatory: str | None = Field(default=None, max_length=255)


@router.post("/shipments/{shipment_id}/containers/{index}")
async def update_container(shipment_id: str, index: int, body: ContainerIn, user: dict = Depends(write)):
    await si_vgm.update_container(shipment_id, index, body.model_dump(exclude_none=True), user)
    return await _view(shipment_id)


@router.post("/shipments/{shipment_id}/si")
async def submit_si(shipment_id: str, user: dict = Depends(write)):
    result = await si_vgm.submit_si(shipment_id, user)
    return {"result": {k: v for k, v in result.items() if k != "payload"}, **(await _view(shipment_id))}


@router.post("/si/{si_id}/refresh")
async def refresh_si(si_id: str, user: dict = Depends(write)):
    return _public_si(await si_vgm.refresh_si(si_id))


@router.post("/shipments/{shipment_id}/vgm")
async def submit_vgm(shipment_id: str, container: str | None = None, user: dict = Depends(write)):
    result = await si_vgm.submit_vgm(shipment_id, container, user)
    return {"result": result, **(await _view(shipment_id))}


class GateIn(BaseModel):
    at: str | None = None


@router.post("/shipments/{shipment_id}/gate-in")
async def gate_in(shipment_id: str, body: GateIn, user: dict = Depends(write)):
    await si_vgm.gate_in(shipment_id, body.at, user)
    return await _view(shipment_id)


@router.get("/overview")
async def overview(user: dict = Depends(read)):
    """Open execution work across shipments: records with cut-off state, SI/VGM status and open alerts."""
    settings = await get_settings()
    hours = settings.get("execution_cutoff_alert_hours")
    recs = await db.execution_records.find({"state": {"$nin": ["cancelled"]}}, {"_id": 0, "booking": 0, "history": 0}).sort(
        "created_at", -1).to_list(300)
    si_ids = [r["si_id"] for r in recs if r.get("si_id")]
    sis = {s["id"]: s for s in await db.shipping_instructions.find({"id": {"$in": si_ids}}, {"_id": 0, "payload": 0}).to_list(None)}
    items = []
    for r in recs:
        cs = cutoffs.status(r, hours)
        nxt = next((c for c in cs if c["state"] not in ("done",)), None)
        items.append({"shipment_id": r["shipment_id"], "reference": r["reference"], "carrier": r.get("carrier_name"),
                      "carrier_booking_reference": r["carrier_booking_reference"], "state": r["state"], "channel": r.get("channel"),
                      "containers": len(r.get("containers") or []), "si_status": (sis.get(r.get("si_id")) or {}).get("status"),
                      "vgm_done": sum(1 for c in r.get("containers") or [] if c.get("vgm_status") in ("submitted", "accepted")),
                      "next_cutoff": nxt, "cutoffs": cs, "etd": r.get("etd")})
    alerts = await db.execution_alerts.find({"status": "open"}, NO_ID).sort("created_at", -1).to_list(100)
    return {"items": items, "alerts": alerts,
            "counts": {"records": len(items), "missed": sum(1 for i in items for c in i["cutoffs"] if c["state"] == "missed"),
                       "due_soon": sum(1 for i in items for c in i["cutoffs"] if c["state"] == "due_soon"), "alerts": len(alerts)}}


@router.get("/alerts")
async def alerts(status: str = "open", user: dict = Depends(read)):
    return {"items": await db.execution_alerts.find({"status": status}, NO_ID).sort("created_at", -1).to_list(200)}


@router.get("/messages")
async def messages(ref_id: str | None = None, kind: str | None = None, user: dict = Depends(read)):
    q = {}
    if ref_id:
        q["ref_id"] = ref_id
    if kind:
        q["kind"] = kind
    return {"items": await db.execution_messages.find(q, NO_ID).sort("created_at", -1).to_list(200)}


# ------------------------------------------------------------------------------------------------- DCSA callbacks

def _check_key(code: str, request: Request):
    expected = dcsa.carrier_conf(code)["callback_key"]
    got = request.headers.get("x-huolitsija-dcsa-key", "")
    if not expected or not hmac.compare_digest(expected, got):
        raise HTTPException(403, "Unknown caller")


@router.post("/dcsa/{code}/v3/shipping-instructions-notifications", status_code=204)
async def dcsa_si_notification(code: str, request: Request):
    _check_key(code, request)
    await si_vgm.dcsa_si_notification(dcsa._code(code), await request.json())
    return Response(status_code=204)


@router.post("/dcsa/{code}/v3/transport-document-notifications", status_code=204)
async def dcsa_td_notification(code: str, request: Request):
    """Stored for the document-issuance slice; s9 records it against the SI's shipment."""
    _check_key(code, request)
    body = await request.json()
    from core.events import emit
    from core.util import new_id, now_iso

    await db.execution_messages.insert_one({"id": new_id(), "direction": "in", "kind": "dcsa_td_notification", "from": f"dcsa:{code}",
                                            "text": str(body)[:20000], "status": "received", "created_at": now_iso()})
    await emit("transport_document.notified", {"carrier_code": dcsa._code(code), "notification": body}, background=True)
    return Response(status_code=204)


@router.get("/dcsa/{code}/vgm-declarations")
async def dcsa_vgm_pull(code: str, request: Request, carrierBookingReference: str | None = None,  # noqa: N803
                        equipmentReference: str | None = None, limit: int = 100):  # noqa: N803
    """VGM 1.0 GET for carriers that pull declarations from us (GetVGMDeclarationsResponse)."""
    _check_key(code, request)
    q = {"carrier_code": dcsa._code(code), "status": {"$in": ["submitted", "accepted"]}}
    if carrierBookingReference:
        q["carrier_booking_reference"] = carrierBookingReference
    if equipmentReference:
        q["equipment_reference"] = equipmentReference
    rows = await db.vgm_declarations.find(q, NO_ID).sort("created_at", -1).to_list(min(limit, 500))
    return Response(content=__import__("json").dumps({"VGMDeclarations": [r["declaration"] for r in rows]}),
                    media_type="application/json", headers={"API-Version": dcsa.API_VERSION["vgm"]})
