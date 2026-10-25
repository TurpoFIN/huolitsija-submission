"""Public-demo admission, scoped requester identity and durable reply recovery.

All limits use real UTC, independent of the accelerated world clock. Mongo's
unique _id is the serialization point; two workers cannot claim the same slot.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from core import config
from core.db import db, register_index
from core.jobs import job, schedule
from . import conf

register_index("demo_public_limits", "expires_at", expireAfterSeconds=0)


def wall_now():
    return datetime.now(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def email_address(value: str) -> str:
    from disposable_email_domains import blocklist
    from email_validator import EmailNotValidError, validate_email
    try:
        address = validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise HTTPException(422, "Please enter a valid email address so we can send your quote.")
    domain = address.rpartition("@")[2]
    allowed = {x.strip().lower() for x in config.env("DEMO_REQUESTER_ALLOW_DOMAINS").split(",") if x.strip()}
    if domain in allowed:
        return address
    extra = {x.strip().lower() for x in config.env("DEMO_DISPOSABLE_DOMAINS").split(",") if x.strip()}
    labels = domain.split(".")
    if any(".".join(labels[i:]) in blocklist | extra for i in range(len(labels))):
        raise HTTPException(422, "Please use your regular email address so you can receive and reply to your quote.")
    return address


async def slot(bucket: str, count: int, *, owner: str, expires_at: datetime) -> bool:
    for n in range(count):
        try:
            await db.raw.demo_public_limits.insert_one({"_id": f"{bucket}:{n}", "owner": owner,
                                                       "expires_at": expires_at})
            return True
        except DuplicateKeyError:
            if await db.raw.demo_public_limits.find_one({"_id": f"{bucket}:{n}", "owner": owner}):
                return True
    return False


async def rfq_mode(run: dict) -> dict:
    now = wall_now()
    day = now.date().isoformat()
    if config.env_bool("DEMO_CAPTURE_RFQS", False):
        return {"rfq_mode": "captured_quota", "rfq_day": day, "rfq_capture_reason": "quote_only_gate"}
    # Retain daily RFQ slots for audit. Never reclaim one on the same UTC day.
    if await slot(f"rfq:{day}", 2, owner=run["id"], expires_at=now + timedelta(days=90)):
        return {"rfq_mode": "real", "rfq_day": day}
    # Reuse only identical ended shipments. Never price a new lane from unrelated rates.
    old = await db.raw.demo_runs.find_one({"id": {"$ne": run["id"]}, "request_text": run["request_text"],
        "profile": run.get("profile") or {}, "status": {"$in": ["done", "failed", "stopped"]},
        "partners.0": {"$exists": True}}, {"_id": 0}, sort=[("created_at", -1)])
    if old and not await db.raw.partner_outreach.count_documents({"reused_from_run": old["id"]}):
        return {"rfq_mode": "takeover", "reuse_from": old["id"], "rfq_day": day}
    return {"rfq_mode": "captured_quota", "rfq_day": day}


def check_token(run: dict, token: str):
    if not token or not hmac.compare_digest(digest(token), run.get("access_token_hash") or ""):
        raise HTTPException(404, "Request not found")


async def reply_run(row: dict) -> dict | None:
    """Bind by stored Message-ID/References. Never infer ownership from sender alone."""
    from services.email_channel import store
    thread, binding = await store.find_thread_for_inbound(row)
    if not thread:
        ids = [x for x in [row.get("in_reply_to"), *(row.get("references") or [])] if x]
        outbox = await db.raw.email_outbox.find_one({"message_ids": {"$in": ids}}, {"_id": 0}) if ids else None
        if outbox:
            from services.demo_world.boundary import related_run
            run = await related_run(outbox.get("ref"))
            if run and run.get("real_requester"):
                return {**run, "_reply_binding": "header"}
    run = await db.raw.demo_runs.find_one({"thread_id": thread["id"], "real_requester": True}, {"_id": 0}) if thread else None
    return {**run, "_reply_binding": binding} if run else None


async def handle_reply(row: dict) -> bool:
    """Claim quote-phase replies for the demo driver; leave lifecycle replies to native flow."""
    from services.demo_world import boundary
    if not conf.real_requester_enabled() or not boundary.on():
        return False
    run = await reply_run(row)
    if not run:
        return False
    from .events import emit
    mismatch = "sender" if (row.get("from_email") or "").lower() != conf.requester_address(run) else \
        "thread" if run.get("_reply_binding") != "header" else None
    if mismatch:
        await db.raw.email_messages.update_one({"id": row["id"]}, {"$set": {
            "status": "ignored", "decision": f"demo_requester_{mismatch}_mismatch", "demo_run_id": run["id"]}})
        await emit(run["id"], "clarify", "Unrelated reply ignored", kind="tool",
                   data={"reasoning": "Reply must reference a stored outbound Message-ID and match the original requester."})
        return True
    if run.get("phase") != "quote":
        return False
    await db.raw.email_messages.update_one({"id": row["id"]}, {"$set": {
        "status": "demo_waiting", "demo_run_id": run["id"], "thread_id": run.get("thread_id")}})
    await resume_waiting(run["id"])
    return True


async def resume_waiting(run_id: str | None = None) -> int:
    from . import driver
    from services.demo_world import boundary
    from pymongo import ReturnDocument
    query = {"status": "awaiting_requester", **({"id": run_id} if run_id else {})}
    if not conf.real_requester_enabled() or not boundary.on():
        query["real_requester"] = {"$ne": True}
    resumed = 0
    async for run in db.raw.demo_runs.find(query, {"_id": 0}):
        ids = run.get("pending_requester_ids") or ((run.get("clarify") or [{}])[-1].get("message_ids") or [])
        reply = await driver._requester_reply(run, ids)
        if not reply:
            continue
        claimed = await db.raw.demo_runs.find_one_and_update({"id": run["id"], "status": "awaiting_requester"},
            {"$set": {"status": "running", "pending_requester_ids": ids}}, return_document=ReturnDocument.AFTER)
        if claimed:
            driver._tasks[run["id"]] = asyncio.create_task(driver._main(run["id"]))
            resumed += 1
    return resumed


@job("demo.resume_requester")
async def resume_job(j):
    if not conf.enabled():
        return {"off": True}
    from . import driver, clock
    from services.demo_world import boundary
    resumed = await resume_waiting()
    if not conf.real_requester_enabled() or not boundary.on():
        return {"resumed": resumed}
    run = await db.raw.demo_runs.find_one({"status": "deferred", "real_requester": True}, {"_id": 0}, sort=[("created_real", 1)])
    if run:
        try:
            await clock.reset_now(run["id"])
        except ValueError:
            pass
        else:
            result = await db.raw.demo_runs.update_one({"id": run["id"], "status": "deferred"}, {"$set": {
                "status": "running", "started_real": wall_now().isoformat(),
                "deferred_seconds": max(0, (wall_now() - datetime.fromisoformat(run["created_real"])).total_seconds())}})
            if result.modified_count:
                driver._tasks[run["id"]] = asyncio.create_task(driver._main(run["id"]))
                resumed += 1
    return {"resumed": resumed}


schedule("demo.resume_requester", job="demo.resume_requester", every_seconds=15)()
