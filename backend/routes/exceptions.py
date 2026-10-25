"""Shipment exceptions: ops queue, customer view, manual reports (ops/partner), AI assessment, resolve."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.audit import actor_ref, audit
from core.db import db
from core.deps import current_user, is_internal, require_perm
from core.util import NO_ID, now_iso
from services import exceptions_agent as xa

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])

CUSTOMER_FIELDS = ("id", "shipment_id", "shipment_reference", "type", "severity", "status", "title", "description", "title_i18n", "description_i18n",
                   "created_at", "resolved_at", "resolution")


def _for_customer(ex: dict) -> dict:
    return {k: ex.get(k) for k in CUSTOMER_FIELDS}


async def _partner_shipment_ids(user: dict) -> list[str]:
    if not user.get("carrier_id"):
        return []
    return [s["id"] async for s in db.shipments.find({"legs.carrier_id": user["carrier_id"]}, {"_id": 0, "id": 1})]


async def _scope(user: dict) -> dict:
    if is_internal(user):
        return {}
    if user.get("org_type") == "customer":
        return {"company_id": user.get("company_id"), "customer_visible": {"$ne": False}}
    return {"shipment_id": {"$in": await _partner_shipment_ids(user)}}


@router.get("")
async def list_exceptions(status: str | None = "active", severity: str | None = None, type: str | None = None,
                          shipment_id: str | None = None, owner: str | None = None, sla: str | None = None,
                          limit: int = Query(300, le=1000), user: dict = Depends(current_user)):
    base = await _scope(user)
    if shipment_id:
        base["shipment_id"] = shipment_id
    q = dict(base)
    if status == "active":
        q["status"] = {"$in": ["open", "in_progress"]}
    elif status and status != "all":
        q["status"] = status
    if severity:
        q["severity"] = {"$in": severity.split(",")}
    if type:
        q["type"] = type
    if owner == "me":
        q["owner.id"] = user["id"]
    elif owner == "none":
        q["owner"] = None
    if sla == "overdue":  # SLA passed on an exception that is still being worked
        q["sla_due"] = {"$lt": now_iso()}
        q.setdefault("status", {"$in": ["open", "in_progress"]})
    items = await db.exceptions.find(q, NO_ID).to_list(limit)
    rank = xa.SEV_RANK
    # Most severe first, then least SLA time left.
    items.sort(key=lambda e: (e["status"] == "resolved", -rank.get(e["severity"], 0), e.get("sla_due") or ""))
    counts = {"status": {}, "severity": {}}
    async for row in db.exceptions.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts["status"][row["_id"]] = row["n"]
    async for row in db.exceptions.aggregate([{"$match": {**base, "status": {"$in": ["open", "in_progress"]}}},
                                              {"$group": {"_id": "$severity", "n": {"$sum": 1}}}]):
        counts["severity"][row["_id"]] = row["n"]
    counts["active"] = counts["status"].get("open", 0) + counts["status"].get("in_progress", 0)
    counts["overdue_sla"] = await db.exceptions.count_documents({**base, "status": {"$in": ["open", "in_progress"]},
                                                                 "sla_due": {"$lt": now_iso()}})
    if is_internal(user):
        counts["unassigned"] = await db.exceptions.count_documents({**base, "status": {"$in": ["open", "in_progress"]}, "owner": None})
        out = items
    else:
        out = [_for_customer(e) for e in items]
    return {"items": out, "total": len(out), "counts": counts}


async def _load(exception_id: str, user: dict) -> dict:
    ex = await db.exceptions.find_one({"id": exception_id}, NO_ID)
    if not ex:
        raise HTTPException(404, "Exception not found")
    scope = await _scope(user)
    if scope:
        if "company_id" in scope and (ex.get("company_id") != scope["company_id"] or ex.get("customer_visible") is False):
            raise HTTPException(404, "Exception not found")
        if "shipment_id" in scope and ex.get("shipment_id") not in scope["shipment_id"]["$in"]:
            raise HTTPException(404, "Exception not found")
    return ex


@router.get("/assignees")
async def assignees(user: dict = Depends(require_perm("exceptions.write"))):
    from core.permissions import permissions_for

    out = []
    async for u in db.users.find({"org_type": "internal", "disabled": {"$ne": True}}, {"_id": 0, "id": 1, "name": 1, "role": 1,
                                                                                        "email": 1, "extra_permissions": 1}):
        if "exceptions.write" in permissions_for(u):
            out.append({"id": u["id"], "name": u.get("name") or u.get("email"), "role": u.get("role")})
    return {"items": out}


@router.get("/{exception_id}")
async def get_exception(exception_id: str, user: dict = Depends(current_user)):
    ex = await _load(exception_id, user)
    if not is_internal(user):
        return _for_customer(ex)
    if ex.get("ai_message_id"):
        ex["ai_message"] = await db.messages.find_one({"id": ex["ai_message_id"]}, NO_ID)
    if ex.get("shipment_id"):
        ex["shipment"] = await db.shipments.find_one({"id": ex["shipment_id"]}, {
            "_id": 0, "id": 1, "reference": 1, "status": 1, "eta": 1, "mode": 1, "origin": 1, "destination": 1,
            "customer_name": 1, "company_id": 1})
    return ex


class ExceptionIn(BaseModel):
    shipment_id: str
    type: str
    severity: str = "medium"
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=4000)
    customer_visible: bool = True


@router.post("")
async def create(body: ExceptionIn, user: dict = Depends(current_user)):
    if is_internal(user):
        if "exceptions.write" not in user["permissions"]:
            raise HTTPException(403, "Missing permission: exceptions.write")
        source = "ops"
    elif user.get("org_type") == "partner":
        if body.shipment_id not in await _partner_shipment_ids(user):
            raise HTTPException(404, "Shipment not found among your assigned legs")
        source = "partner"
    else:
        raise HTTPException(403, "Customers report problems through messages or claims")
    try:
        ex, _ = await xa.create_exception(shipment_id=body.shipment_id, type=body.type, severity=body.severity,
                                          title=body.title, description=body.description, detected_by=source, actor=user,
                                          customer_visible=body.customer_visible,
                                          owner={"id": user["id"], "name": user.get("name")} if source == "ops" else None)
    except xa.ExceptionError as exc:
        raise HTTPException(exc.status, str(exc))
    return ex


class ExceptionPatch(BaseModel):
    status: str | None = None
    severity: str | None = None
    owner_id: str | None = None
    unassign: bool = False
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    customer_visible: bool | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.patch("/{exception_id}")
async def patch(exception_id: str, body: ExceptionPatch, user: dict = Depends(require_perm("exceptions.write"))):
    ex = await _load(exception_id, user)
    changes: dict = {}
    hist = []
    if body.status:
        if body.status not in ("open", "in_progress"):
            raise HTTPException(400, "Use resolve to close an exception; status can be open or in_progress")
        if ex["status"] == "resolved" and body.status in ("open", "in_progress"):
            changes.update(resolved_at=None, resolution=None, resolved_by=None)
        changes["status"] = body.status
        hist.append(f"status -> {body.status}")
    if body.severity:
        if body.severity not in xa.SEVERITIES:
            raise HTTPException(400, "Unknown severity")
        changes["severity"] = body.severity
        changes["sla_due"] = xa.sla_due(body.severity)
        hist.append(f"severity -> {body.severity}")
    if body.owner_id:
        u = await db.users.find_one({"id": body.owner_id, "org_type": "internal"}, {"_id": 0, "id": 1, "name": 1})
        if not u:
            raise HTTPException(404, "Team member not found")
        changes["owner"] = {"id": u["id"], "name": u.get("name")}
        if ex["status"] == "open" and not body.status:
            changes["status"] = "in_progress"
        hist.append(f"owner -> {u.get('name')}")
    if body.unassign:
        changes["owner"] = None
        hist.append("unassigned")
    for k in ("title", "description", "customer_visible"):
        v = getattr(body, k)
        if v is not None:
            changes[k] = v
    if not changes and not body.note:
        return ex
    changes["updated_at"] = now_iso()
    push = {"history": {"$each": [{"at": now_iso(), "by": user.get("name"), "action": h} for h in hist] +
                        ([{"at": now_iso(), "by": user.get("name"), "action": "note", "note": body.note}] if body.note else [])}}
    await db.exceptions.update_one({"id": exception_id}, {"$set": changes, "$push": push})
    await xa._set_has_exception(ex.get("shipment_id"))
    await audit(user, "exception_updated", "exception", exception_id, {"changes": hist, "note": body.note},
                company_id=ex.get("company_id"))
    return await db.exceptions.find_one({"id": exception_id}, NO_ID)


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)
    customer_message: str | None = Field(default=None, max_length=4000)


@router.post("/{exception_id}/resolve")
async def resolve(exception_id: str, body: ResolveIn, user: dict = Depends(require_perm("exceptions.write"))):
    ex = await _load(exception_id, user)
    try:
        ex = await xa.resolve_exception(exception_id, resolution=body.resolution, actor=user)
    except xa.ExceptionError as exc:
        raise HTTPException(exc.status, str(exc))
    sent = None
    if body.customer_message and body.customer_message.strip() and ex.get("company_id"):
        # Written (or edited and confirmed) by the signed-in person, so it is sent directly.
        from services import messaging

        if ex.get("ai_message_id"):
            await messaging.supersede_ai_drafts_for_message(ex["ai_message_id"], reason="Resolved with a person's message")
        thread = await messaging.get_or_create_thread(company_id=ex["company_id"], shipment_id=ex.get("shipment_id"),
                                                      subject=ex.get("shipment_reference") or ex["title"],
                                                      created_by=messaging.author_from_user(user))
        sent = await messaging.add_message(thread, body=body.customer_message, author=messaging.author_from_user(user))
    return {**ex, "customer_message": sent}


@router.post("/{exception_id}/assess")
async def assess(exception_id: str, user: dict = Depends(require_perm("exceptions.write"))):
    await _load(exception_id, user)
    ex = await xa.assess_exception(exception_id, requested_by=actor_ref(user))
    if not ex:
        ex = await db.exceptions.find_one({"id": exception_id}, NO_ID)
    return ex


@router.post("/detect")
async def detect(user: dict = Depends(require_perm("exceptions.write"))):
    """Run the detection rules now (also runs every 5 minutes)."""
    return await xa.run_detection()
