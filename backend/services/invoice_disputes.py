"""Customer invoice disputes. The invoice owns the dispute and its recovery hold atomically.

This records a billing disagreement, not a cargo loss or an insurer settlement.
Credits must already have been issued through billing; no money is moved here.
"""
from fastapi import HTTPException
from pymongo import ReturnDocument

from core.audit import audit
from core.db import db
from core.deps import is_internal
from core.notify import bi, notify_company, notify_role
from core.util import NO_ID, new_id, now_iso

OPEN = ("submitted", "in_review")


def is_open(invoice: dict) -> bool:
    return (invoice.get("dispute") or {}).get("status") in OPEN


async def load(invoice_id: str, actor: dict) -> dict:
    q = {"id": invoice_id}
    if not is_internal(actor):
        if actor.get("org_type") != "customer" or not actor.get("company_id"):
            raise HTTPException(404, "Invoice not found")
        q["company_id"] = actor["company_id"]
    inv = await db.invoices.find_one(q, NO_ID)
    if not inv or inv.get("kind") != "invoice" or not inv.get("number") or inv.get("status") == "draft":
        raise HTTPException(404, "Issued invoice not found")
    return inv


def _writer(actor: dict):
    needed = {"billing.write"} if is_internal(actor) else {"company.book", "company.manage"}
    if not needed.intersection(actor.get("permissions") or []):
        raise HTTPException(403, "No permission to change this dispute")


def _text(text: str) -> str:
    text = (text or "").strip()
    if not 10 <= len(text) <= 4000:
        raise HTTPException(400, "Explain the issue in 10–4000 characters / Kuvaa asia 10–4000 merkillä")
    return text


def _message(actor: dict, text: str, action: str) -> dict:
    return {"id": new_id(), "at": now_iso(), "text": text, "action": action,
            "by": actor.get("name") or actor["id"], "side": "finance" if is_internal(actor) else "customer"}


async def _notify(inv: dict, actor: dict):
    from services.billing import public_base_url
    base = await public_base_url()
    title = bi(f"Laskureklamaatio: {inv['number']}", f"Invoice dispute: {inv['number']}")
    body = bi("Reklamaatio on päivitetty. Avaa lasku nähdäksesi viestit ja käsittelyn tilanteen.",
              "The dispute was updated. Open the invoice to see its messages and review status.")
    if is_internal(actor):
        await notify_company(inv["company_id"], title, body, link=f"/portal/invoices/{inv['id']}",
                             email=not inv.get("demo"), kind="invoice_dispute", base_url=base)
    else:
        await notify_role("finance", title, body, link=f"/ops/billing/{inv['id']}",
                          email=not inv.get("demo"), kind="invoice_dispute", base_url=base)


async def submit(invoice_id: str, actor: dict, *, reason: str, amount_cents: int) -> dict:
    inv = await load(invoice_id, actor)
    _writer(actor)
    reason = _text(reason)
    if inv.get("status") in ("void", "credited"):
        raise HTTPException(409, "This invoice is void or fully credited")
    if type(amount_cents) is not int or not 0 < amount_cents <= int(inv.get("total_cents") or 0):
        raise HTTPException(400, "Disputed amount must be positive and no greater than the invoice total")
    dispute = {"id": new_id(), "status": "submitted", "amount_cents": amount_cents,
               "currency": inv.get("currency") or "EUR", "reason": reason, "created_at": now_iso(),
               "messages": [_message(actor, reason, "submitted")], "resolution": None}
    query = {"id": inv["id"], "company_id": inv["company_id"], "status": inv["status"],
             "dispute.status": {"$nin": list(OPEN)}}
    update = {"$set": {"dispute": dispute, "updated_at": now_iso()}}
    if inv.get("dispute"):
        query["dispute.id"] = inv["dispute"]["id"]
        update["$push"] = {"dispute_history": inv["dispute"]}
    row = await db.invoices.find_one_and_update(query, update, projection=NO_ID,
                                               return_document=ReturnDocument.AFTER)
    if not row:
        raise HTTPException(409, "An open dispute already exists or the invoice changed; reload it")
    await audit(actor, "invoice_dispute_submitted", "invoice", inv["id"],
                {"dispute_id": dispute["id"], "amount_cents": amount_cents}, company_id=inv["company_id"])
    await _notify(row, actor)
    return row["dispute"]


async def respond(invoice_id: str, dispute_id: str, actor: dict, *, text: str) -> dict:
    inv = await load(invoice_id, actor)
    _writer(actor)
    changes = {"updated_at": now_iso()}
    if is_internal(actor):
        changes["dispute.status"] = "in_review"
    row = await db.invoices.find_one_and_update(
        {"id": inv["id"], "dispute.id": dispute_id, "dispute.status": {"$in": list(OPEN)}},
        {"$set": changes, "$push": {"dispute.messages": _message(actor, _text(text), "message")}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not row:
        raise HTTPException(409, "The dispute is no longer open; reload the invoice")
    await audit(actor, "invoice_dispute_message", "invoice", inv["id"],
                {"dispute_id": dispute_id}, company_id=inv["company_id"])
    await _notify(row, actor)
    return row["dispute"]


async def resolve(invoice_id: str, dispute_id: str, actor: dict, *, outcome: str, note: str,
                  credit_note_id: str | None = None) -> dict:
    inv = await load(invoice_id, actor)
    _writer(actor)
    note = _text(note)
    if outcome not in ("withdrawn", "rejected", "credited"):
        raise HTTPException(400, "Choose withdrawn, rejected or credited")
    if outcome == "withdrawn" and is_internal(actor):
        raise HTTPException(403, "Only the customer can withdraw a dispute")
    if outcome != "withdrawn" and not is_internal(actor):
        raise HTTPException(403, "Only finance can decide a dispute")
    current = inv.get("dispute") or {}
    if current.get("id") != dispute_id or current.get("status") not in OPEN:
        raise HTTPException(409, "The dispute is no longer open; reload the invoice")
    resolution = {"outcome": outcome, "note": note, "at": now_iso(), "by": actor.get("name") or actor["id"]}
    if outcome == "credited":
        cn = await db.invoices.find_one({"id": credit_note_id, "company_id": inv["company_id"],
                                         "kind": "credit_note", "credit_note_for": inv["id"]}, NO_ID)
        if (not cn or not cn.get("number") or cn.get("status") in ("draft", "void")
                or cn.get("currency", "EUR") != inv.get("currency", "EUR")
                or int(cn.get("total_cents") or 0) >= 0
                or abs(cn["total_cents"]) < current["amount_cents"]):
            raise HTTPException(409, "Issue a linked credit note covering the disputed amount before resolving")
        resolution.update(credit_note_id=cn["id"], credit_note_number=cn["number"], amount_cents=abs(cn["total_cents"]))
    row = await db.invoices.find_one_and_update(
        {"id": inv["id"], "dispute.id": dispute_id, "dispute.status": {"$in": list(OPEN)}},
        {"$set": {"dispute.status": outcome, "dispute.resolution": resolution, "updated_at": now_iso()},
         "$push": {"dispute.messages": _message(actor, note, outcome)}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not row:
        raise HTTPException(409, "The dispute changed; reload the invoice")
    await audit(actor, "invoice_dispute_resolved", "invoice", inv["id"],
                {"dispute_id": dispute_id, **resolution}, company_id=inv["company_id"])
    await _notify(row, actor)
    return row["dispute"]
