"""Delivery agent: confirms each delivery against a proof of delivery. A carrier feed (or the simulator) that reports
the delivery with a POD stores it as the shipment's POD document; the agent then marks the delivery confirmed. A
delivery without a POD waits DELIVERY_POD_WAIT_HOURS [24] for one, then goes to the owner queue."""
from core import config
from core.agent_runtime import decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.util import NO_ID, now, now_iso, parse_iso
from services.agents.base import agents_epoch, mode_key, sweeper

AGENT = "delivery_agent"
POD_WAIT_HOURS = float(config.env("DELIVERY_POD_WAIT_HOURS", "24"))


@job("delivery.confirm", agent=True, max_attempts=4, timeout=120, label_fi="Toimituksen vahvistus", label_en="Delivery confirmation")
async def confirm(j):
    sid = j["payload"]["shipment_id"]
    s = await db.raw.shipments.find_one({"id": sid}, NO_ID)
    if not s or s.get("demo") or s.get("status") != "delivered":
        return {"skipped": "not delivered"}
    if (s.get("delivery_confirmation") or {}).get("status") == "confirmed":
        return {"skipped": "confirmed"}
    pod = await db.raw.documents.find_one({"shipment_id": sid, "type": "pod"}, {"_id": 0, "id": 1, "filename": 1, "source": 1},
                                          sort=[("uploaded_at", -1)])
    delivered = next((m for m in reversed(s.get("milestones") or []) if m.get("code") == "delivered" and m.get("at")), {})
    by_person = delivered.get("source") == "manual"
    reasons = []
    if not pod and not by_person:
        since = parse_iso(s.get("delivered_at")) or now()
        if (now() - since).total_seconds() < POD_WAIT_HOURS * 3600:
            raise JobDeferred(1800, "waiting for the proof of delivery")
        reasons.append(policy_reason("no_pod", f"Toimituskuittausta ei ole saatu {POD_WAIT_HOURS:.0f} tunnissa",
                                     f"No proof of delivery within {POD_WAIT_HOURS:.0f} hours"))
    d = await decide(AGENT, "confirm_delivery", subject=("shipment", sid), subject_label=s.get("reference"),
                     company_id=s.get("company_id"),
                     inputs={"delivered_at": s.get("delivered_at"), "source": delivered.get("source"),
                             "pod_document_id": (pod or {}).get("id")},
                     decision={"confirmed": True}, confidence=None, extra_reasons=reasons, approval_kind="exception_action",
                     title=f"Proof of delivery missing for {s.get('reference')}",
                     title_fi=f"Toimituskuittaus puuttuu: {s.get('reference')}", link=f"/ops/shipments/{sid}",
                     approval_data={"shipment_id": sid, "action": "confirm_delivery"})
    if not d.auto:
        return {"owner_queue": d.approval and d.approval["id"]}
    await db.raw.shipments.update_one({"id": sid}, {"$set": {"delivery_confirmation": {
        "status": "confirmed", "at": now_iso(), "pod_document_id": (pod or {}).get("id"), "decision_id": d.id,
        "basis": "pod" if pod else "recorded_by_person"}}})
    return {"confirmed": True, "pod": (pod or {}).get("id")}


@on("shipment.delivered")
async def _on_delivered(p):
    s = p.get("shipment") or {}
    if s.get("id") and not s.get("demo"):
        await enqueue("delivery.confirm", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"delivery:{s['id']}:{await mode_key()}")


@on("approval.decided")
async def _on_decided(p):
    a = p["approval"]
    if a.get("kind") != "exception_action" or (a.get("data") or {}).get("action") != "confirm_delivery" or a["status"] != "approved":
        return
    actor = p.get("actor") or {}
    await db.raw.shipments.update_one({"id": a["data"]["shipment_id"]}, {"$set": {"delivery_confirmation": {
        "status": "confirmed", "at": now_iso(), "basis": "owner", "by": {"id": actor.get("id"), "name": actor.get("name")}}}})


@sweeper
async def sweep_deliveries():
    n = 0
    mk = await mode_key()
    async for s in db.raw.shipments.find({"status": "delivered", "demo": {"$ne": True}, "created_at": {"$gte": await agents_epoch()},
                                          "delivery_confirmation.status": {"$ne": "confirmed"}},
                                         {"_id": 0, "id": 1, "company_id": 1}).limit(100):
        await enqueue("delivery.confirm", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"delivery:{s['id']}:{mk}", source="sweep")
        n += 1
    return n
