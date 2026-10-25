"""Customer <-> ops messaging: threads per shipment (or free subject), AI drafts behind approval.

    thread  {id, company_id, shipment_id, subject, status open|closed, last_message_at, unread_customer, unread_ops, ...}
    message {id, thread_id, company_id, author{type customer|ops|ai|system, id, name}, body, attachments[],
             status sent|pending_approval|rejected, approval_id, visibility customer|internal, kind message|update, ...}

Customers only ever see `sent` + `customer` messages. AI text reaches a customer only through an approved
`ai_message` approval, or as a routine update when settings.ai_auto_send_routine_messages allows it.
"""
from __future__ import annotations

import logging

from core.approvals import cancel_approvals, request_approval
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now_iso

log = logging.getLogger("messaging")

register_index("threads", "id", unique=True)
register_index("threads", [("company_id", 1), ("last_message_at", -1)])
register_index("threads", "shipment_id")
register_index("messages", "id", unique=True)
register_index("messages", [("thread_id", 1), ("created_at", 1)])
register_index("messages", "routine_key", sparse=True)

AUTHOR_TYPES = ("customer", "ops", "ai", "system")
MAX_BODY = 8000


class MessagingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def author_from_user(user: dict) -> dict:
    t = "customer" if user.get("org_type") == "customer" else "ops" if user.get("org_type") == "internal" else "partner"
    return {"type": t, "id": user.get("id"), "name": user.get("name") or user.get("email"), "role": user.get("role")}


def _preview(body: str) -> str:
    b = " ".join((body or "").split())
    return b[:160] + ("…" if len(b) > 160 else "")


async def _shipment_brief(shipment_id: str | None) -> dict | None:
    if not shipment_id:
        return None
    return await db.shipments.find_one({"id": shipment_id}, {"_id": 0, "id": 1, "reference": 1, "company_id": 1,
                                                             "status": 1, "eta": 1, "delivered_at": 1, "mode": 1, "origin": 1,
                                                             "destination": 1, "customer_name": 1, "has_exception": 1})


async def get_or_create_thread(*, company_id: str, shipment_id: str | None, subject: str | None,
                               created_by: dict | None, demo: bool = False, new: bool = False) -> dict:
    if shipment_id and not new:
        t = await db.threads.find_one({"company_id": company_id, "shipment_id": shipment_id}, NO_ID,
                                      sort=[("last_message_at", -1)])
        if t:
            return t
    s = await _shipment_brief(shipment_id)
    if shipment_id and (not s or s.get("company_id") != company_id):
        raise MessagingError("Shipment not found for this company", 404)
    company = await db.companies.find_one({"id": company_id}, {"_id": 0, "name": 1, "demo": 1}) or {}
    t = {"id": new_id(), "company_id": company_id, "company_name": company.get("name"), "shipment_id": shipment_id,
         "shipment_reference": (s or {}).get("reference"), "subject": (subject or (s or {}).get("reference") or "Viesti")[:200],
         "status": "open", "created_at": now_iso(), "created_by": created_by, "last_message_at": now_iso(),
         "last_message_preview": "", "last_author_type": None, "unread_customer": 0, "unread_ops": 0,
         "needs_reply": False, "pending_ai_count": 0, "ai_status": None, "demo": bool(demo or company.get("demo"))}
    await db.threads.insert_one(dict(t))
    return t


async def _attachments(company_id: str, ids: list[str] | None) -> list[dict]:
    out = []
    for doc_id in ids or []:
        d = await db.documents.find_one({"id": doc_id}, NO_ID)
        if not d or d.get("company_id") != company_id:
            raise MessagingError("An attached document was not found", 404)
        out.append({"document_id": d["id"], "filename": d.get("filename"), "content_type": d.get("content_type"),
                    "size": d.get("size"), "type": d.get("type")})
    return out


async def add_message(thread: dict, *, body: str, author: dict, attachments: list[str] | None = None,
                      needs_approval: bool = False, visibility: str = "customer", kind: str = "message",
                      ai: dict | None = None, routine_key: str | None = None, created_at: str | None = None,
                      notify: bool = True, demo: bool = False, approval_reason: str | None = None,
                      hold_for_owner: bool = False) -> dict:
    from core.settings import get_settings
    from core.autonomy import action
    ai_customer_reply = author.get("type") == "ai" and visibility == "customer"
    settings = (await get_settings()) if ai_customer_reply else {}
    from core.autonomy import autopilot_scope  # s6 r94: global Autopilot or this test tenant's grant
    autonomous = bool(ai_customer_reply and not hold_for_owner
                      and await autopilot_scope(thread.get("company_id"), settings, agent="comms_agent") != "off")
    if ai_customer_reply:
        # Owning write boundary: legacy flags or callers cannot bypass the off-state approval gate. hold_for_owner:
        # the comms agent's decision gate (core.agent_runtime.decide) kept this reply for a person.
        needs_approval = not autonomous
    body = (body or "").strip()
    if not body and not attachments:
        raise MessagingError("Write a message or attach a document")
    if len(body) > MAX_BODY:
        raise MessagingError(f"Messages are limited to {MAX_BODY} characters")
    if author.get("type") not in AUTHOR_TYPES:
        raise MessagingError("Unknown author type")
    if visibility not in ("customer", "internal"):
        raise MessagingError("visibility must be customer or internal")
    if visibility == "internal" and author["type"] == "customer":
        raise MessagingError("Customers cannot post internal notes", 403)
    msg = {"id": new_id(), "thread_id": thread["id"], "company_id": thread["company_id"],
           "shipment_id": thread.get("shipment_id"), "author": author, "body": body,
           "attachments": await _attachments(thread["company_id"], attachments), "visibility": visibility, "kind": kind,
           "status": "pending_approval" if needs_approval else "sent", "approval_id": None, "ai": ai,
           "routine_key": routine_key, "created_at": created_at or now_iso(), "sent_at": None, "demo": demo}
    if not needs_approval:
        msg["sent_at"] = msg["created_at"]
    await db.messages.insert_one(dict(msg))
    if needs_approval:
        appr = await request_approval(
            "ai_message", "message", msg["id"], f"Reply to {thread.get('company_name')}: {thread.get('subject')}",
            summary=_preview(body), reason=approval_reason or "AI-drafted text to a customer needs a person's approval",
            company_id=thread["company_id"], requested_by=author if author.get("type") in ("ai", "system") else None,
            data={"body": body, "thread_id": thread["id"], "message_id": msg["id"],
                  "shipment_reference": thread.get("shipment_reference")},
            link=f"/ops/messages/{thread['id']}",
            extra={"title_fi": f"Vastaus asiakkaalle {thread.get('company_name')}: {thread.get('subject')}"})
        msg["approval_id"] = appr["id"]
        await db.messages.update_one({"id": msg["id"]}, {"$set": {"approval_id": appr["id"]}})
        await _refresh_pending(thread["id"])
        return msg
    await _after_sent(thread, msg, notify=notify)
    if autonomous:
        await action("customer_reply_sent", "message", msg["id"], company_id=thread["company_id"],
                     data={"thread_id": thread["id"], "email_recall_possible": False}, reversible=False)
    return msg


async def _refresh_pending(thread_id: str):
    n = await db.messages.count_documents({"thread_id": thread_id, "status": "pending_approval"})
    await db.threads.update_one({"id": thread_id}, {"$set": {"pending_ai_count": n}})


async def _after_sent(thread: dict, msg: dict, *, notify: bool = True):
    """Thread counters, notifications and the message.posted event for a message that just became visible."""
    a_type = msg["author"]["type"]
    upd: dict = {"$set": {"last_message_at": msg["sent_at"] or now_iso(), "updated_at": now_iso()}}
    if msg["visibility"] == "customer":
        upd["$set"].update(last_message_preview=_preview(msg["body"]) or "📎", last_author_type=a_type)
        if a_type == "customer":
            upd["$inc"] = {"unread_ops": 1}
            upd["$set"].update(needs_reply=True, status="open")
        else:
            upd["$inc"] = {"unread_customer": 1}
            if a_type in ("ops", "ai"):
                upd["$set"]["needs_reply"] = False
    await db.threads.update_one({"id": thread["id"]}, upd)
    thread = await db.threads.find_one({"id": thread["id"]}, NO_ID)
    if a_type == "ops" and msg["visibility"] == "customer":
        await supersede_ai_drafts(thread["id"], reason="A person replied")
    await _refresh_pending(thread["id"])
    if notify:
        await _notify(thread, msg)
    await emit("message.posted", {"thread": thread, "message": msg})


async def _notify(thread: dict, msg: dict):
    from core.notify import bi, notify_role, notify_user, same
    from services.billing import public_base_url

    a_type = msg["author"]["type"]
    if a_type == "customer":
        await notify_role(["ops_agent"], same(f"{thread.get('company_name')}: {thread.get('subject')}"), same(_preview(msg["body"])),
                          link=f"/ops/messages/{thread['id']}", kind="message")
        return
    if msg["visibility"] != "customer" or msg.get("demo"):
        return
    base = await public_base_url()
    async for u in db.users.find({"company_id": thread["company_id"], "disabled": {"$ne": True}}, {"_id": 0, "password_hash": 0}):
        if msg.get("kind") == "update":
            ref = thread.get("shipment_reference") or thread.get("subject")
            title = bi(f"{ref}: tilannepäivitys", f"{ref}: status update")
        else:
            title = bi("Uusi viesti: " + (thread.get("subject") or ""), "New message: " + (thread.get("subject") or ""))
        await notify_user(u, title, same(_preview(msg["body"])), f"/portal/messages/{thread['id']}", email=True, kind="message",
                          base_url=base)


async def supersede_ai_drafts(thread_id: str, *, reason: str) -> list[str]:
    """Pending drafts in the thread -> rejected (with reason), approvals cancelled. Returns the superseded ids."""
    ids = []
    async for m in db.messages.find({"thread_id": thread_id, "status": "pending_approval"}, {"_id": 0, "id": 1}):
        r = await db.messages.update_one({"id": m["id"], "status": "pending_approval"},
                                         {"$set": {"status": "rejected", "rejected_reason": reason, "rejected_at": now_iso()}})
        await cancel_approvals(m["id"], kind="ai_message")
        if r.modified_count:
            ids.append(m["id"])
    await _refresh_pending(thread_id)
    return ids


async def supersede_ai_drafts_for_message(message_id: str, reason: str = "Replaced by a newer draft"):
    m = await db.messages.find_one({"id": message_id}, {"_id": 0, "status": 1, "thread_id": 1})
    if not m or m["status"] != "pending_approval":
        return
    await db.messages.update_one({"id": message_id, "status": "pending_approval"},
                                 {"$set": {"status": "rejected", "rejected_reason": reason, "rejected_at": now_iso()}})
    await cancel_approvals(message_id, kind="ai_message")
    await _refresh_pending(m["thread_id"])


async def post_message(*, company_id, shipment_id=None, subject=None, body, author: dict, needs_approval=False) -> dict:
    """Cross-slice entry point (ARCHITECTURE.md). Posts into the shipment's thread (created when missing) or a new
    thread with `subject`. Returns the message (with thread_id). needs_approval -> pending ai_message approval."""
    if not company_id:
        raise MessagingError("company_id is required")
    thread = await get_or_create_thread(company_id=company_id, shipment_id=shipment_id, subject=subject,
                                        created_by=author, new=not shipment_id)
    return await add_message(thread, body=body, author=author, needs_approval=needs_approval)


async def send_pending(message_id: str, *, body: str | None, approver: dict | None) -> dict | None:
    msg = await db.messages.find_one({"id": message_id}, NO_ID)
    if not msg or msg["status"] != "pending_approval":
        return msg
    new_body = (body or "").strip() or msg["body"]
    changes = {"status": "sent", "sent_at": now_iso(), "approved_by": approver, "approved_at": now_iso()}
    # original_body = what the AI wrote (kept from the first saved edit, if any); edited = the sent text differs from it
    original = msg.get("original_body") or msg["body"]
    if new_body != msg["body"]:
        changes["body"] = new_body
    if new_body != original:
        changes.update(edited=True, original_body=original)
    res = await db.messages.update_one({"id": message_id, "status": "pending_approval"}, {"$set": changes})
    if res.modified_count == 0:
        return await db.messages.find_one({"id": message_id}, NO_ID)
    msg.update(changes)
    thread = await db.threads.find_one({"id": msg["thread_id"]}, NO_ID)
    await _after_sent(thread, msg)
    await audit(approver, "ai_message_sent", "message", message_id, {"edited": bool(changes.get("edited"))},
                company_id=msg["company_id"])
    return msg


async def mark_read(thread_id: str, side: str):
    field = "unread_customer" if side == "customer" else "unread_ops"
    await db.threads.update_one({"id": thread_id}, {"$set": {field: 0}})


@on("approval.decided")
async def _on_approval(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != "ai_message" or a.get("ref_type") != "message":
        return
    actor = p.get("actor")
    approver = {"id": (actor or {}).get("id"), "name": (actor or {}).get("name")}
    if a.get("status") == "approved":
        await send_pending(a["ref_id"], body=(a.get("decision_data") or {}).get("body"), approver=approver)
    elif a.get("status") == "rejected":
        msg = await db.messages.find_one({"id": a["ref_id"]}, NO_ID)
        if msg and msg["status"] == "pending_approval":
            await db.messages.update_one({"id": msg["id"]}, {"$set": {
                "status": "rejected", "rejected_by": approver, "rejected_at": now_iso(),
                "rejected_reason": a.get("decision_note") or "Rejected by a person"}})
            await _refresh_pending(msg["thread_id"])
