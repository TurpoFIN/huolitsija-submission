"""Booking agent: confirms a new booking against the customer's credit limit and the KYC/sanctions state, asks the
customer for the documents the shipment needs, and hands the shipment to the carrier agent. Over the credit limit,
dangerous goods (legal point) or Autopilot off -> owner queue; a rejected booking is cancelled before departure."""
from core.agent_runtime import agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import enqueue, job
from core.util import NO_ID, now_iso
from services.agents.base import agents_epoch, customer_message, mode_key, sweeper

AGENT = "booking_agent"


async def _confirm(s: dict, decision_id: str | None, by: dict):
    await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"booking_confirmation": {
        "status": "confirmed", "at": now_iso(), "by": {"id": by["id"], "name": by["name"]}, "decision_id": decision_id}}})
    await enqueue("carrier.select_and_book", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                  subject=("shipment", s["id"]), key=f"carrier:{s['id']}:initial")
    needs_docs = s.get("direction") == "import" and s.get("customs")
    if needs_docs and not await db.raw.documents.find_one({"shipment_id": s["id"], "type": "commercial_invoice"}, {"_id": 1}):
        ref = s.get("reference")
        await customer_message(
            s["company_id"], s["id"],
            f"Varaus {ref} on vahvistettu. Tullausta varten tarvitsemme kauppalaskun ja pakkausluettelon: lataa ne "
            f"lähetyksen Asiakirjat-välilehdelle. Laadimme tuonti-ilmoituksen niistä automaattisesti.",
            f"Booking {ref} is confirmed. For customs clearance we need the commercial invoice and the packing list: "
            f"upload them on the shipment's Documents tab and we draft the import declaration from them.",
            subject=f"{ref}: asiakirjat / documents", key=f"docs-request:{s['id']}")


@job("booking.confirm", agent=True, max_attempts=5, timeout=120, label_fi="Varauksen vahvistus", label_en="Booking confirmation")
async def confirm_booking(j):
    s = await db.raw.shipments.find_one({"id": j["payload"]["shipment_id"]}, NO_ID)
    if not s or s.get("demo") or s.get("status") == "cancelled":
        return {"skipped": "no shipment"}
    bc = s.get("booking_confirmation") or {}
    if bc.get("status") in ("confirmed", "rejected"):
        return {"skipped": bc.get("status")}
    from services.agents.quote_agent import autosend
    gate = await autosend(shipment=s)
    if not gate.auto:
        await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"booking_confirmation": {
            "status": "pending_owner", "approval_id": gate.approval["id"], "decision_id": gate.id}}})
        return {"owner_queue": True}
    c = await db.raw.companies.find_one({"id": s.get("company_id")}, NO_ID) or {}
    extra = []
    if c.get("kyc_status") != "approved" or c.get("sanctions_status") != "clear":
        extra.append(policy_reason("kyc", "Asiakastuntemus tai pakoteseulonta ei ole kunnossa", "KYC or sanctions screening is not clear"))
    cargo = s.get("cargo") or {}
    price = int((s.get("price") or {}).get("total_cents") or 0)
    d = await decide(
        AGENT, "confirm_booking", subject=("shipment", s["id"]), subject_label=s.get("reference"), company_id=s.get("company_id"),
        inputs={"price_cents": price, "mode": s.get("mode"), "route": [(s.get("origin") or {}).get("unlocode"),
                                                                       (s.get("destination") or {}).get("unlocode")],
                "kyc": c.get("kyc_status"), "sanctions": c.get("sanctions_status"), "dg": bool(cargo.get("dangerous_goods"))},
        decision={"confirm": True}, confidence=None, exposure_cents=price, exclude_shipment_id=s["id"],
        legal_point="dangerous_goods" if cargo.get("dangerous_goods") else None, extra_reasons=extra,
        approval_kind="booking_review", title=f"Confirm booking {s.get('reference')}",
        title_fi=f"Vahvista varaus {s.get('reference')}",
        summary=f"{s.get('customer_name')}: {s.get('mode')} {(s.get('origin') or {}).get('name')} → "
                f"{(s.get('destination') or {}).get('name')}, {price / 100:,.2f} EUR",
        summary_fi=f"{s.get('customer_name')}: {(s.get('origin') or {}).get('name')} → {(s.get('destination') or {}).get('name')}, "
                   f"{price / 100:,.2f} €".replace(",", " "),
        link=f"/ops/shipments/{s['id']}", amount_cents=price, approval_data={"shipment_id": s["id"]})
    if d.auto:
        await _confirm(s, d.id, agent_bot(AGENT))
        return {"confirmed": True}
    await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"booking_confirmation": {
        "status": "pending_owner", "at": now_iso(), "approval_id": d.approval and d.approval["id"], "decision_id": d.id}}})
    return {"owner_queue": True}


@on("shipment.created")
async def _on_created(p):
    s = p["shipment"]
    await enqueue("booking.confirm", {"shipment_id": s["id"]}, company_id=s.get("company_id"), subject=("shipment", s["id"]),
                  key=f"booking:{s['id']}:{await mode_key()}")


@on("approval.decided")
async def _on_decided(p):
    a = p["approval"]
    if a.get("kind") != "booking_review":
        return
    s = await db.raw.shipments.find_one({"id": a["ref_id"]}, NO_ID)
    if not s or (s.get("booking_confirmation") or {}).get("status") in ("confirmed", "rejected"):
        return
    actor = p.get("actor") or {}
    if a["status"] == "approved":
        await _confirm(s, (a.get("data") or {}).get("agent_decision_id"), {"id": actor.get("id"), "name": actor.get("name")})
    elif a["status"] == "rejected":
        from services import shipments as svc
        await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"booking_confirmation": {
            "status": "rejected", "at": now_iso(), "by": {"id": actor.get("id"), "name": actor.get("name")},
            "note": a.get("decision_note")}}})
        try:
            await svc.cancel_shipment(s["id"], actor=actor, reason=a.get("decision_note") or "Booking not confirmed")
        except Exception:  # noqa: BLE001
            pass


@sweeper
async def sweep_bookings():
    n = 0
    mk = await mode_key()
    async for s in db.raw.shipments.find({"status": "booked", "demo": {"$ne": True}, "created_at": {"$gte": await agents_epoch()},
                                          "booking_confirmation.status": {"$nin": ["confirmed", "rejected"]}},
                                         {"_id": 0, "id": 1, "company_id": 1}).limit(100):
        await enqueue("booking.confirm", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"booking:{s['id']}:{mk}", source="sweep")
        n += 1
    return n
