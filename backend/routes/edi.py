"""EDI gateway API (s14).

Ops (internal): partners, message log, raw view, reprocess / resend, poll now, status.
Partner machine endpoints (HMAC: X-Huolitsija-Partner = partner id, X-Huolitsija-Signature = hex HMAC-SHA256 of the raw
body with the partner's secret; GET requests sign the path + query string):
  POST /api/edi/inbound            raw EDIFACT interchange (IFTMIN) → 200 with the stored record and results
  POST /api/edi/dcsa/bookings      DCSA Booking 2.0 request JSON → DCSA-shaped booking status
  GET  /api/edi/dcsa/events        DCSA Track & Trace 2.2 events for the partner's shipments
  GET  /api/edi/partner/outbox     outbound messages kept for pickup (transport "none")
  POST /api/edi/as2/inbound        AS2 (signed + encrypted) with synchronous signed MDN
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from core import config
from core.db import db
from core.deps import require_internal, require_perm
from core.util import NO_ID, new_id, now_iso
from services.edi import dcsa, service, transport

router = APIRouter(prefix="/api/edi", tags=["edi"])


class PartnerIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    company_id: str | None = None
    unb_id: str | None = None
    unb_qual: str | None = None
    transport: str | None = None
    sftp: dict | None = None
    as2: dict | None = None
    formats: list[str] | None = None
    status_messages: bool | None = None
    status_scope: str | None = None
    status_code_map: dict | None = None
    active: bool | None = None
    test: bool | None = None
    locale: str | None = None
    accept_terms_for_company: bool | None = None


def _terms(body: PartnerIn, user: dict) -> dict:
    data = body.model_dump(exclude_none=True)
    if data.pop("accept_terms_for_company", False):
        from core.settings import DEFAULTS

        data["terms_accepted"] = {"version": DEFAULTS["terms_version"], "at": now_iso(),
                                  "recorded_by": {"id": user.get("id"), "name": user.get("name")},
                                  "basis": "EDI interchange agreement signed by the customer"}
    return data


# ------------------------------------------------------------------------------------------------ ops

@router.get("/status")
async def edi_status(user: dict = Depends(require_internal)):
    return {"our_unb_id": config.EDI_OUR_UNB_ID, "sftp_enabled": config.EDI_SFTP_ENABLED, "poll_seconds": config.EDI_POLL_SECONDS,
            "as2_enabled": config.EDI_AS2_ENABLED, "as2_ready": transport.as2_ready(), "as2_id": config.EDI_AS2_ID,
            "partners": await db.edi_partners.count_documents({"active": {"$ne": False}}),
            "messages": {d: await db.edi_messages.count_documents({"direction": d}) for d in ("in", "out")}}


@router.get("/partners")
async def list_partners(user: dict = Depends(require_internal)):
    items = await db.edi_partners.find({}, service.PARTNER_PUBLIC).sort("created_at", -1).to_list(500)
    names = {c["id"]: c.get("name") for c in await db.companies.find(
        {"id": {"$in": [p.get("company_id") for p in items if p.get("company_id")]}}, {"_id": 0, "id": 1, "name": 1}).to_list(None)}
    last = {r["_id"]: r for r in await db.edi_messages.aggregate([
        {"$match": {"partner_id": {"$in": [p["id"] for p in items]}}},
        {"$group": {"_id": "$partner_id", "at": {"$max": "$created_at"}, "count": {"$sum": 1}}}]).to_list(None)}
    for p in items:  # for the ops page: whose partner this is and when it last exchanged anything
        p["company_name"] = names.get(p.get("company_id"))
        p["last_message_at"] = (last.get(p["id"]) or {}).get("at")
        p["message_count"] = (last.get(p["id"]) or {}).get("count", 0)
    return {"items": items}


@router.post("/partners")
async def create_partner(body: PartnerIn, user: dict = Depends(require_perm("settings.write"))):
    p, secret = await service.create_partner(_terms(body, user), user)
    return {"partner": p, "hmac_secret": secret, "note": "The HMAC secret is shown once."}


@router.patch("/partners/{partner_id}")
async def patch_partner(partner_id: str, body: PartnerIn, user: dict = Depends(require_perm("settings.write"))):
    return {"partner": await service.update_partner(partner_id, _terms(body, user), user)}


@router.post("/partners/{partner_id}/poll")
async def poll_now(partner_id: str, user: dict = Depends(require_perm("settings.write"))):
    p = await db.edi_partners.find_one({"id": partner_id}, NO_ID)
    if not p:
        raise HTTPException(404, "EDI partner not found.")
    if p.get("transport") != "sftp":
        raise HTTPException(400, "This partner does not use SFTP.")
    try:
        return {"files": await service.poll_partner(p)}
    except transport.TransportError as exc:
        raise HTTPException(502, str(exc)) from None


@router.post("/status-sweep")
async def status_sweep(user: dict = Depends(require_perm("settings.write"))):
    sent = await service.send_status_updates()
    return {"sent": len(sent), "items": [{"id": s.get("id"), "status": s.get("status"), "filename": s.get("filename")} for s in sent]}


@router.get("/messages")
async def list_messages(partner_id: str | None = None, direction: str | None = None, type: str | None = None,
                        status: str | None = None, shipment_id: str | None = None, limit: int = 100,
                        user: dict = Depends(require_internal)):
    q: dict = {}
    for k, v in (("partner_id", partner_id), ("direction", direction), ("type", type), ("status", status),
                 ("related.shipment_id", shipment_id)):
        if v:
            q[k] = v
    items = await db.edi_messages.find(q, {"_id": 0, "raw": 0}).sort("created_at", -1).to_list(min(max(limit, 1), 500))
    names = {p["id"]: p.get("name") for p in await db.edi_partners.find(
        {"id": {"$in": list({m.get("partner_id") for m in items if m.get("partner_id")})}}, {"_id": 0, "id": 1, "name": 1}).to_list(None)}
    for m in items:
        m["partner_name"] = names.get(m.get("partner_id"))
    return {"items": items}


@router.get("/messages/{message_id}")
async def get_message(message_id: str, user: dict = Depends(require_internal)):
    m = await db.edi_messages.find_one({"id": message_id}, NO_ID)
    if not m:
        raise HTTPException(404, "EDI message not found.")
    return m


@router.post("/messages/{message_id}/reprocess")
async def reprocess(message_id: str, user: dict = Depends(require_perm("settings.write"))):
    m = await db.edi_messages.find_one({"id": message_id}, NO_ID)
    if not m:
        raise HTTPException(404, "EDI message not found.")
    partner = await db.edi_partners.find_one({"id": m.get("partner_id")}, NO_ID) if m.get("partner_id") else None
    if m["direction"] == "out":
        return await service.deliver(m, partner)
    if m["standard"] != "edifact":
        raise HTTPException(400, "Only EDIFACT inbound messages can be reprocessed here.")
    # a fresh copy (new dedupe key) so the original stays as received
    await db.edi_messages.update_one({"id": m["id"]}, {"$set": {"dedupe_key": f"{m['dedupe_key']}:reprocessed:{m['id']}"}})
    return await service.receive_edifact(m["raw"].encode(), partner=partner, transport_name=m.get("transport") or "manual",
                                         filename=m.get("filename"))


@router.get("/shipments/{shipment_id}/dcsa-events")
async def shipment_dcsa_events(shipment_id: str, user: dict = Depends(require_internal)):
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    evs = await db.tracking_events.find({"shipment_id": shipment_id}, NO_ID).sort("at", 1).to_list(500)
    return dcsa.tnt_events(s, evs)


# ------------------------------------------------------------------------------------------------ partner machine endpoints

async def _partner_from_hmac(request: Request, body: bytes) -> dict:
    pid = request.headers.get("x-huolitsija-partner")
    sig = request.headers.get("x-huolitsija-signature")
    p = await db.edi_partners.find_one({"id": pid, "active": {"$ne": False}}, NO_ID) if pid else None
    signed = body if request.method != "GET" else (request.url.path + ("?" + request.url.query if request.url.query else "")).encode()
    if not p or not service.verify_hmac(p, signed, sig):
        raise HTTPException(401, "Unknown partner or bad signature (X-Huolitsija-Partner / X-Huolitsija-Signature).")
    return p


@router.post("/inbound")
async def inbound_edifact(request: Request):
    body = await request.body()
    p = await _partner_from_hmac(request, body)
    res = await service.receive_edifact(body, partner=p, transport_name="https", filename=request.headers.get("x-filename"))
    return {k: v for k, v in res.items() if k not in ("raw", "dedupe_key")}


@router.post("/dcsa/bookings")
async def dcsa_booking(request: Request, response: Response):
    import json

    body = await request.body()
    p = await _partner_from_hmac(request, body)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "The body is not JSON.") from None
    out = await service.receive_dcsa_booking(data, p)
    response.status_code = 201 if out.get("bookingStatus") in ("CONFIRMED", "PENDING UPDATE") else 200
    return out


@router.get("/dcsa/events")
async def dcsa_events(request: Request, carrierBookingReference: str | None = None, limit: int = 100):  # noqa: N803 (DCSA name)
    p = await _partner_from_hmac(request, b"")
    q: dict = {"company_id": p["company_id"]}
    if carrierBookingReference:
        q["reference"] = carrierBookingReference
    shipments = await db.shipments.find(q, NO_ID).sort("updated_at", -1).to_list(50)
    out = []
    for s in shipments:
        evs = await db.tracking_events.find({"shipment_id": s["id"]}, NO_ID).sort("at", 1).to_list(500)
        out.extend(dcsa.tnt_events(s, evs))
    return out[: max(1, min(limit, 1000))]


@router.get("/partner/outbox")
async def partner_outbox(request: Request):
    p = await _partner_from_hmac(request, b"")
    items = await db.edi_messages.find({"partner_id": p["id"], "direction": "out"}, {"_id": 0, "dedupe_key": 0}) \
        .sort("created_at", -1).to_list(100)
    return {"items": items}


@router.get("/as2/certificate")
async def as2_certificate():
    """Our public AS2 certificate (partners encrypt to it and verify our signatures with it)."""
    if not transport.as2_ready():
        raise HTTPException(404, "AS2 is not switched on.")
    return Response(content=transport.pem(config.EDI_AS2_CERT_PEM), media_type="application/x-pem-file",
                    headers={"X-AS2-Id": config.EDI_AS2_ID})


@router.post("/as2/inbound")
async def as2_inbound(request: Request):
    if not transport.as2_ready():
        raise HTTPException(503, "AS2 is not switched on (EDI_AS2_ENABLED with EDI_AS2_KEY_PEM / EDI_AS2_CERT_PEM).")
    body = await request.body()
    as2_from = (request.headers.get("as2-from") or "").strip().strip('"')
    partner = await db.edi_partners.find_one({"as2.as2_id": as2_from, "active": {"$ne": False}}, NO_ID)
    raw = b"".join(f"{k}: {v}\r\n".encode() for k, v in request.headers.items()) + b"\r\n" + body
    status, payload, mdn, message_id, error = transport.as2_parse_inbound(raw, partner)
    if status == "processed" and payload and partner:
        await service.receive_edifact(payload if isinstance(payload, bytes) else payload.encode(), partner=partner,
                                      transport_name="as2", filename=f"as2-{message_id}")
    elif partner:
        await db.edi_messages.insert_one({"id": new_id(), "direction": "in",
                                          "standard": "as2", "type": "AS2_ERROR", "partner_id": partner["id"],
                                          "company_id": partner["company_id"], "status": "rejected", "error": error or status,
                                          "created_at": now_iso()})
    if mdn is None:
        return Response(status_code=200 if status == "processed" else 400, content=(error or status).encode())
    return Response(content=mdn.content, headers={k: v for k, v in mdn.headers.items()}, status_code=200)
