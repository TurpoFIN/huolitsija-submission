"""Explicit world-bank authorization, gated on actual customer invoice delivery."""
from fastapi import HTTPException

from core.db import db
from core.jobs import enqueue
from . import conf


async def payment_state(run: dict, invoice_id: str | None = None) -> dict:
    query = {"$or": []}
    if run.get("quote_id"):
        query["$or"].append({"quote_id": run["quote_id"]})
    if run.get("shipment_id"):
        query["$or"].append({"shipment_id": run["shipment_id"]})
    invoice = None
    if query["$or"]:
        if invoice_id:
            query["id"] = invoice_id
        invoice = await db.raw.invoices.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    state = {"available": False, "status": "none", "invoice_id": None, "invoice_number": None}
    if not invoice:
        return state
    state.update(invoice_id=invoice["id"], invoice_number=invoice.get("number"))
    sent = await db.raw.email_outbox.find_one({"to": conf.requester_address(run), "tag": "invoice_issued",
        "status": "sent", "provider": {"$in": ["smtp2go", "resend", "mailslurp", "gmail"]}, "ref.type": "invoice", "ref.id": invoice["id"]})
    if invoice.get("status") == "paid":
        state["status"] = "paid"
    elif sent and invoice.get("status") in ("open", "overdue", "partially_paid"):
        state["available"] = True
        if await db.raw.jobs.find_one({"key": "world:public-pay:" + invoice["id"]}):
            state["status"] = "queued"
    return state


async def simulate_payment(run: dict, invoice_id: str | None = None) -> dict:
    from services.demo_world import boundary
    if not conf.real_requester_enabled() or not boundary.on() or not run.get("real_requester"):
        raise HTTPException(404, "Request not found")
    state = await payment_state(run, invoice_id)
    if state["status"] == "paid":
        return {"ok": True, **state}
    if not state["available"] or run.get("phase") != "lifecycle" or run.get("status") in ("stopped", "failed", "done"):
        raise HTTPException(409, "Your invoice is not ready yet. Please check again shortly.")
    await enqueue("world.pay", {"run_id": run["id"], "invoice_id": state["invoice_id"], "explicit_request": True},
                  key="world:public-pay:" + state["invoice_id"], source="world")
    return {"ok": True, **state, "status": "queued"}
