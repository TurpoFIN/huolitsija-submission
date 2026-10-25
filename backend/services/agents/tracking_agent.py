"""Tracking agent: turns carrier tracking feeds into milestones. One normalised event shape for every source (a
carrier's webhook, a polled API, the movement simulator for sandbox test companies):

    {"event_id": "...", "shipment_ref" | "transport_document_number" | "container_number": "...",
     "code": picked_up|received|export_cleared|departed|transshipment|arrived|customs_released|out_for_delivery|
             delivered|eta|exception,
     "at": ISO time, "leg_seq": int?, "location": {name, unlocode, lat, lon}?, "vessel": {name, imo, mmsi}?,
     "eta": ISO time? (code eta), "exception": {type, severity, title, description, title_fi, description_fi}?,
     "pod": {"signed_by": "...", "at": "..."}? (code delivered), "note": "..."?}

Events are deduplicated per provider event id, so a feed may resend them safely."""
import logging

from fastapi import HTTPException

from core.db import db, register_index
from core.jobs import JobFailed, enqueue, job
from core.util import NO_ID, now_iso

log = logging.getLogger("tracking_agent")
AGENT = "tracking_agent"
MILESTONES = {"picked_up", "received", "export_cleared", "departed", "transshipment", "arrived", "customs_released",
              "out_for_delivery", "delivered"}
register_index("feed_events", "key", unique=True)


async def find_shipment(ev: dict) -> dict | None:
    if ev.get("shipment_ref"):
        s = await db.raw.shipments.find_one({"reference": ev["shipment_ref"]}, NO_ID)
        if s:
            return s
    if ev.get("shipment_id"):
        s = await db.raw.shipments.find_one({"id": ev["shipment_id"]}, NO_ID)
        if s:
            return s
    if ev.get("transport_document_number"):
        s = await db.raw.shipments.find_one({"legs.transport_doc_no": ev["transport_document_number"]}, NO_ID)
        if s:
            return s
    if ev.get("container_number"):
        return await db.raw.shipments.find_one({"legs.container_numbers": ev["container_number"]}, NO_ID)
    return None


def current_leg(s: dict) -> int | None:
    for lg in s.get("legs") or []:
        if not lg.get("ata"):
            return lg["seq"]
    legs = s.get("legs") or []
    return legs[-1]["seq"] if legs else None


async def store_feed_event(provider: str, ev: dict, *, verified: bool) -> tuple[dict, bool]:
    """Durably store one inbound event and queue it. Returns (record, new)."""
    from pymongo.errors import DuplicateKeyError
    key = f"{provider}:{ev.get('event_id')}"
    rec = {"key": key, "provider": provider, "event": ev, "verified": verified, "received_at": now_iso(), "status": "queued"}
    try:
        await db.raw.feed_events.insert_one(dict(rec))
    except DuplicateKeyError:
        return await db.raw.feed_events.find_one({"key": key}, {"_id": 0}), False
    await enqueue("tracking.ingest", {"provider": provider, "key": key}, key=f"feed:{key}", source="feed")
    return rec, True


@job("tracking.ingest", max_attempts=5, backoff=(10, 60, 300, 900, 3600), timeout=120,
     label_fi="Seurantatapahtuma", label_en="Tracking event")
async def ingest(j):
    """Not an agent decision: a carrier telling us where the goods are. Runs even while the kill switch is on."""
    rec = await db.raw.feed_events.find_one({"key": j["payload"]["key"]}, {"_id": 0})
    if not rec:
        raise JobFailed("feed event missing")
    ev, provider = rec["event"], rec["provider"]
    s = await find_shipment(ev)
    if not s:
        await db.raw.feed_events.update_one({"key": rec["key"]}, {"$set": {"status": "unmatched"}})
        raise JobFailed(f"No shipment matches {ev.get('shipment_ref') or ev.get('transport_document_number') or ev.get('container_number')}")
    code = ev.get("code")
    source = "simulator" if provider == "simulator" else "carrier_api"
    actor = {"type": "system", "id": f"feed:{provider}", "name": "Simulaattori / Simulator" if provider == "simulator" else f"{provider} feed"}
    raw_id = f"{provider}:{ev.get('event_id')}"
    if rec.get("status") == "applied":
        return {"duplicate": True}
    leg_seq = ev.get("leg_seq") if ev.get("leg_seq") is not None else current_leg(s)
    try:
        result = await _apply(s, ev, provider, code, source, actor, raw_id, leg_seq)
    except HTTPException as exc:
        # The event does not fit the shipment (a milestone after delivery, an unknown leg): keep it, tell ops.
        await db.raw.feed_events.update_one({"key": rec["key"]}, {"$set": {"status": "rejected", "error": str(exc.detail),
                                                                           "shipment_id": s["id"]}})
        raise JobFailed(f"{s.get('reference')}: {exc.detail}")
    await db.raw.feed_events.update_one({"key": rec["key"]}, {"$set": {"status": "applied", "applied_at": now_iso(),
                                                                       "shipment_id": s["id"]}})
    return result


async def _apply(s: dict, ev: dict, provider: str, code: str, source: str, actor: dict, raw_id: str, leg_seq) -> dict:
    from services import shipments as svc
    result = {"shipment": s.get("reference"), "code": code}
    if code == "eta" and ev.get("eta") and leg_seq is not None:
        await svc.update_leg(s["id"], leg_seq, {"eta": ev["eta"]}, actor, tracking_update=True)
    elif code == "exception":
        from services.exceptions_agent import create_exception
        x = ev.get("exception") or {}
        exc, created = await create_exception(
            shipment_id=s["id"], type=x.get("type") or "delay", severity=x.get("severity") or "medium",
            title=x.get("title") or "Carrier reported an exception", description=x.get("description") or "",
            detected_by="carrier", actor=actor, rule_key=f"feed:{raw_id}", company_id=s.get("company_id"),
            evidence={"provider": provider, "event": ev, "leg_seq": leg_seq}, assess=False,
            title_i18n={"fi": x.get("title_fi") or x.get("title"), "en": x.get("title")} if x.get("title") else None,
            description_i18n={"fi": x.get("description_fi") or x.get("description"), "en": x.get("description")}
            if x.get("description") else None)
        result["exception_id"] = exc["id"]
        if x.get("new_eta") and leg_seq is not None:
            await svc.update_leg(s["id"], leg_seq, {"eta": x["new_eta"]}, actor, tracking_update=True)
    elif code in MILESTONES:
        if ev.get("vessel") and leg_seq is not None:
            await svc.update_leg(s["id"], leg_seq, {"vessel": ev["vessel"]}, actor)
        if code == "delivered" and ev.get("pod"):
            await _store_pod(s, ev, provider)
        await svc.record_milestone(s["id"], code, actor=actor, source=source, note=ev.get("note"),
                                   location=ev.get("location") if (ev.get("location") or {}).get("lat") is not None else None,
                                   at=ev.get("at"), leg_seq=leg_seq)
    else:
        raise HTTPException(422, f"Unknown tracking code {code}")
    return result


async def _store_pod(s: dict, ev: dict, provider: str):
    """Proof of delivery from the carrier's feed: a one-page PDF receipt stored as the shipment's POD document."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from services import documents
    if await db.raw.documents.find_one({"shipment_id": s["id"], "type": "pod"}, {"_id": 1}):
        return
    pod = ev.get("pod") or {}
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    lines = [("Toimituskuittaus / Proof of delivery", 15),
             (f"Lähetys / Shipment: {s.get('reference')}", 11),
             (f"Vastaanottaja / Consignee: {((s.get('parties') or {}).get('consignee') or {}).get('name') or '—'}", 11),
             (f"Kuittaaja / Signed by: {pod.get('signed_by') or '—'}", 11),
             (f"Aika / Time: {pod.get('at') or ev.get('at')}", 11),
             (f"Lähde / Source: {provider} event {ev.get('event_id')}", 9)]
    if provider == "simulator":
        lines.append(("SANDBOX: simuloitu testiyrityksen toimitus / simulated delivery for a test company", 9))
    for text, size in lines:
        c.setFont("Helvetica-Bold" if size > 12 else "Helvetica", size)
        c.drawString(56, y, text)
        y -= size + 12
    c.showPage()
    c.save()
    await documents.create_document(company_id=s["company_id"], shipment_id=s["id"], doc_type="pod",
                                    filename=f"POD-{s.get('reference')}.pdf", data=buf.getvalue(),
                                    actor={"type": "system", "id": f"feed:{provider}", "name": f"{provider} feed"},
                                    source="carrier", extract=False)
