"""Messages: threads per shipment, customer and ops sides, AI drafts with approval."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.db import db
from core.deps import current_user, is_internal, require_internal, require_perm
from core.util import NO_ID
from core.web import base_url
from services import comms_agent, messaging
from services.billing import remember_base_url

router = APIRouter(prefix="/api/messages", tags=["messages"])


def _side(user: dict) -> str:
    if is_internal(user):
        return "ops"
    if user.get("org_type") == "customer":
        return "customer"
    raise HTTPException(403, "Messages are not available for this account")


async def _thread(thread_id: str, user: dict) -> dict:
    t = await db.threads.find_one({"id": thread_id}, NO_ID)
    if not t or (_side(user) == "customer" and t["company_id"] != user.get("company_id")):
        raise HTTPException(404, "Conversation not found")
    return t


def _filter_for(side: str, f: str | None) -> dict:
    unread = "unread_customer" if side == "customer" else "unread_ops"
    return {
        None: {}, "all": {}, "open": {"status": "open"}, "closed": {"status": "closed"},
        "unread": {unread: {"$gt": 0}},
        "needs_reply": {"needs_reply": True, "status": "open"},
        "ai_pending": {"pending_ai_count": {"$gt": 0}},
        "needs_person": {"ai_status": "needs_person", "needs_reply": True},
    }.get(f, {})


@router.get("/threads")
async def list_threads(request: Request, filter: str | None = None, shipment_id: str | None = None,
                       company_id: str | None = None, q: str | None = None, limit: int = Query(200, le=500),
                       user: dict = Depends(current_user)):
    await remember_base_url(base_url(request))
    side = _side(user)
    base: dict = {"company_id": user["company_id"]} if side == "customer" else ({"company_id": company_id} if company_id else {})
    if side == "customer":
        base["last_author_type"] = {"$ne": None}  # threads that only hold unapproved AI drafts stay invisible
    if shipment_id:
        base["shipment_id"] = shipment_id
    query = {**base, **_filter_for(side, filter)}
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"subject": rx}, {"shipment_reference": rx}, {"company_name": rx}, {"last_message_preview": rx}]
    items = await db.threads.find(query, NO_ID).sort("last_message_at", -1).to_list(limit)
    if side == "customer":
        for t in items:
            for k in ("pending_ai_count", "ai_status", "ai_note", "unread_ops", "needs_reply"):
                t.pop(k, None)
    counts = {}
    for key in (("all", "unread", "needs_reply", "ai_pending", "needs_person", "closed") if side == "ops" else ("all", "unread", "closed")):
        counts[key] = await db.threads.count_documents({**base, **_filter_for(side, key)})
    return {"items": items, "total": len(items), "counts": counts}


@router.get("/summary")
async def summary(user: dict = Depends(current_user)):
    side = _side(user)
    if side == "customer":
        n = await db.threads.count_documents({"company_id": user["company_id"], "unread_customer": {"$gt": 0}})
        return {"unread": n}
    return {"unread": await db.threads.count_documents({"unread_ops": {"$gt": 0}}),
            "needs_reply": await db.threads.count_documents({"needs_reply": True, "status": "open"}),
            "ai_pending": await db.threads.count_documents({"pending_ai_count": {"$gt": 0}})}


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, user: dict = Depends(current_user)):
    side = _side(user)
    t = await _thread(thread_id, user)
    q: dict = {"thread_id": thread_id}
    if side == "customer":
        q.update(status="sent", visibility="customer")
    msgs = await db.messages.find(q, NO_ID).sort("created_at", 1).to_list(1000)
    if side == "customer":
        for m in msgs:
            m.pop("ai", None)
            m.pop("approval_id", None)
            m.pop("original_body", None)
            if m["author"]["type"] == "ops":
                m["author"] = {"type": "ops", "name": m["author"].get("name"), "id": None}
        for k in ("pending_ai_count", "ai_status", "ai_note", "unread_ops", "needs_reply"):
            t.pop(k, None)
    shipment = await messaging._shipment_brief(t.get("shipment_id"))
    if shipment and side == "customer" and shipment.get("company_id") != user.get("company_id"):
        shipment = None
    return {"thread": t, "messages": msgs, "shipment": shipment}


class NewThreadIn(BaseModel):
    subject: str | None = Field(default=None, max_length=200)
    shipment_id: str | None = None
    company_id: str | None = None
    body: str = Field(default="", max_length=messaging.MAX_BODY)
    attachments: list[str] = []


@router.post("/threads")
async def new_thread(body: NewThreadIn, request: Request, user: dict = Depends(current_user)):
    await remember_base_url(base_url(request))
    side = _side(user)
    if side == "ops" and "messages.ops" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: messages.ops")
    company_id = user["company_id"] if side == "customer" else body.company_id
    if side == "ops" and not company_id and body.shipment_id:
        s = await db.shipments.find_one({"id": body.shipment_id}, {"_id": 0, "company_id": 1})
        company_id = (s or {}).get("company_id")
    if not company_id:
        raise HTTPException(400, "Choose the customer")
    if not body.shipment_id and not (body.subject or "").strip():
        raise HTTPException(400, "Give the conversation a subject or pick a shipment")
    try:
        # One conversation per shipment: a message about a shipment joins its existing thread unless the writer
        # gave the new conversation its own subject.
        own_subject = bool((body.subject or "").strip())
        t = await messaging.get_or_create_thread(company_id=company_id, shipment_id=body.shipment_id, subject=body.subject,
                                                 created_by=messaging.author_from_user(user), new=own_subject)
        msg = await messaging.add_message(t, body=body.body, author=messaging.author_from_user(user),
                                          attachments=body.attachments)
    except messaging.MessagingError as exc:
        raise HTTPException(exc.status, str(exc))
    return {"thread": await db.threads.find_one({"id": t["id"]}, NO_ID), "message": msg}


class PostIn(BaseModel):
    body: str = Field(default="", max_length=messaging.MAX_BODY)
    attachments: list[str] = []
    visibility: str = "customer"


@router.post("/threads/{thread_id}/messages")
async def post(thread_id: str, body: PostIn, request: Request, user: dict = Depends(current_user)):
    await remember_base_url(base_url(request))
    side = _side(user)
    if side == "ops" and "messages.ops" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: messages.ops")
    t = await _thread(thread_id, user)
    try:
        msg = await messaging.add_message(t, body=body.body, author=messaging.author_from_user(user),
                                          attachments=body.attachments,
                                          visibility=body.visibility if side == "ops" else "customer")
    except messaging.MessagingError as exc:
        raise HTTPException(exc.status, str(exc))
    if side == "customer":
        await messaging.mark_read(thread_id, "customer")
    else:
        await messaging.mark_read(thread_id, "ops")
    return msg


@router.post("/threads/{thread_id}/read")
async def read(thread_id: str, user: dict = Depends(current_user)):
    await _thread(thread_id, user)
    await messaging.mark_read(thread_id, _side(user))
    return {"ok": True}


class ThreadPatch(BaseModel):
    status: str | None = None
    subject: str | None = Field(default=None, max_length=200)


@router.patch("/threads/{thread_id}")
async def patch_thread(thread_id: str, body: ThreadPatch, user: dict = Depends(current_user)):
    t = await _thread(thread_id, user)
    changes = {}
    if body.status:
        if body.status not in ("open", "closed"):
            raise HTTPException(400, "status must be open or closed")
        changes["status"] = body.status
        if body.status == "closed":
            changes["needs_reply"] = False
    if body.subject and _side(user) == "ops":
        changes["subject"] = body.subject.strip()
    if changes:
        await db.threads.update_one({"id": t["id"]}, {"$set": changes})
    return await db.threads.find_one({"id": t["id"]}, NO_ID)


@router.post("/threads/{thread_id}/ai-draft")
async def ai_draft(thread_id: str, user: dict = Depends(require_perm("messages.ops"))):
    await _thread(thread_id, user)
    msg = await comms_agent.draft_reply(thread_id, requested_by={"id": user["id"], "name": user.get("name")})
    t = await db.threads.find_one({"id": thread_id}, NO_ID)
    if not msg:
        return {"message": None, "ai_status": t.get("ai_status"), "ai_note": t.get("ai_note") or
                "Nothing to answer: the last message is not from the customer"}
    return {"message": msg, "ai_status": t.get("ai_status")}


class DraftPatch(BaseModel):
    body: str = Field(min_length=1, max_length=messaging.MAX_BODY)


async def _pending(message_id: str) -> dict:
    m = await db.messages.find_one({"id": message_id}, NO_ID)
    if not m:
        raise HTTPException(404, "Message not found")
    if m["status"] != "pending_approval":
        raise HTTPException(409, f"This draft is already {m['status']}")
    return m


@router.patch("/{message_id}")
async def edit_draft(message_id: str, body: DraftPatch, user: dict = Depends(require_perm("messages.ops"))):
    m = await _pending(message_id)
    changes = {"body": body.body.strip(), "edited_by": {"id": user["id"], "name": user.get("name")}}
    if not m.get("original_body"):
        changes["original_body"] = m["body"]
    await db.messages.update_one({"id": message_id}, {"$set": changes})
    if m.get("approval_id"):
        await db.approvals.update_one({"id": m["approval_id"], "status": "pending"},
                                      {"$set": {"data.body": changes["body"], "summary": messaging._preview(changes["body"])}})
    return await db.messages.find_one({"id": message_id}, NO_ID)


class DecideIn(BaseModel):
    body: str | None = Field(default=None, max_length=messaging.MAX_BODY)
    note: str | None = None


async def _decide(message_id: str, decision: str, body: DecideIn, user: dict) -> dict:
    """Decides through the core approval queue (same path as /ops/approvals: audit + approval.decided)."""
    from routes.admin import DecisionIn, decide

    m = await _pending(message_id)
    if not m.get("approval_id"):
        raise HTTPException(409, "This draft has no approval request")
    data = {"body": body.body.strip()} if (decision == "approved" and body.body and body.body.strip()) else None
    await decide(m["approval_id"], DecisionIn(decision=decision, note=body.note, data=data), user)
    return await db.messages.find_one({"id": message_id}, NO_ID)


@router.post("/{message_id}/approve")
async def approve(message_id: str, body: DecideIn, user: dict = Depends(require_internal)):
    return await _decide(message_id, "approved", body, user)


@router.post("/{message_id}/reject")
async def reject(message_id: str, body: DecideIn, user: dict = Depends(require_internal)):
    return await _decide(message_id, "rejected", body, user)
