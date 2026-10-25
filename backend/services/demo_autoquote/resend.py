"""Apply only authenticated provider receipts to real demo requester mail.

Sending-only keys cannot retrieve SES's rewritten Message-ID. The existing
Svix-verified webhook records it durably, even when it beats the send response.
"""
from core import config
from core.db import db
from core.util import now_iso
from . import conf


def enabled() -> bool:
    return conf.real_requester_enabled() and config.env_bool("DEMO_WORLD_SANDBOX", False)


async def record_sent(event: dict) -> bool:
    if not enabled() or event.get("type") != "email.sent":
        return False
    data = event.get("data") or {}
    provider_id, message_id = data.get("email_id"), data.get("message_id")
    if not provider_id or not message_id or any(c in message_id for c in "\r\n"):
        return False
    message_id = f"<{message_id.strip('<>')}>"
    await db.raw.demo_resend_receipts.update_one({"_id": provider_id}, {
        "$addToSet": {"message_ids": message_id},
        "$set": {"sent_at": event.get("created_at") or now_iso()}}, upsert=True)
    return await reconcile(provider_id)


async def reconcile(provider_id: str) -> bool:
    if not enabled():
        return False
    receipt = await db.raw.demo_resend_receipts.find_one({"_id": provider_id})
    row = await db.raw.email_outbox.find_one({"provider": "resend", "provider_id": provider_id})
    if not receipt or not row:
        return False
    from services.demo_world.boundary import real_requester_run
    run = await real_requester_run(row["to"], row.get("ref"), row.get("tag"))
    if not run:
        return False
    ids = list(dict.fromkeys([value for value in [*(row.get("message_ids") or []), row.get("message_id"),
                                                *receipt["message_ids"]] if value]))
    await db.raw.email_outbox.update_one({"id": row["id"]}, {
        "$addToSet": {"message_ids": {"$each": ids}},
        "$set": {"message_id": receipt["message_ids"][-1], "status": "sent", "provider_sent_at": receipt["sent_at"]}})
    native = [{"outbox_id": row["id"]}]
    if (row.get("ref") or {}).get("email_message_id"):
        native.append({"id": row["ref"]["email_message_id"]})
    await db.raw.email_messages.update_many({"direction": "out", "$or": native}, {
        "$addToSet": {"provider_message_ids": {"$each": ids}}})
    await db.raw.demo_clarification_acks.update_many({"outbox_id": row["id"]}, {
        "$set": {"status": "sent", "provider_sent_at": receipt["sent_at"]}})
    if row.get("tag") == "email_ack":
        await db.raw.demo_runs.update_one({"id": run["id"]}, {"$set": {"acknowledged_at": receipt["sent_at"]}})
    # A delayed receipt must also recover a reply already waiting in the inbox.
    from . import public_requester
    async for incoming in db.raw.email_messages.find({"direction": "in", "from_email": conf.requester_address(run),
        "demo_consumed": {"$ne": True}, "$or": [{"in_reply_to": {"$in": ids}}, {"references": {"$in": ids}}]}):
        await public_requester.handle_reply(incoming)
    return True
