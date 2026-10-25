"""One durable acknowledgement per completed clarification round; no reload replay."""
from pymongo.errors import DuplicateKeyError

from core.db import db
from core.util import now_iso
from services.email_channel import outbound
from services.demo_world import boundary
from . import conf


async def complete(run_id: str, round_no: int) -> dict:
    run = await db.raw.demo_runs.find_one({"id": run_id}, {"_id": 0})
    state = (run or {}).get("clarification_checkpoint")
    if (not conf.real_requester_enabled() or not boundary.on() or not run or not run.get("real_requester")
        or not state or state.get("missing") != [] or state.get("all_missing", []) != []
        or run.get("clarification_round") != round_no):
        return {"status": "incomplete"}
    thread = await db.raw.email_threads.find_one({"id": run.get("thread_id")}, {"_id": 0})
    if not thread or thread.get("customer_email") != conf.requester_address(run):
        return {"status": "failed", "reason": "requester_thread_unavailable"}
    reply = await db.raw.email_messages.find_one({"id": (state.get("reply") or {}).get("id"),
        "direction": "in", "thread_id": thread["id"], "from_email": conf.requester_address(run)}, {"_id": 0})
    if not reply or not reply.get("message_id"):
        return {"status": "failed", "reason": "clarification_reply_unavailable"}
    key = f"clarification-complete:{run_id}:{round_no}"
    try:
        await db.raw.demo_clarification_acks.insert_one({"_id": key, "run_id": run_id, "round": round_no,
            "status": "reserved", "reply_message_id": reply["message_id"], "created_at": now_iso()})
    except DuplicateKeyError:
        return await db.raw.demo_clarification_acks.find_one({"_id": key}, {"_id": 0})
    # The reservation is never expired/replayed automatically. An uncertain
    # transport result needs review; it cannot silently duplicate customer mail.
    try:
        fi = run.get("locale") == "fi"
        sent = await outbound.send({**thread, "language": "fi" if fi else "en"},
            "Kiitos lisätiedoista" if fi else "Thanks for the details",
            ["Kiitos, nyt minulla on kaikki tarvittavat tiedot. Selvitän kuljetusvaihtoehtoja ja lähetän tarjouksen sähköpostitse, kun se on valmis."
             if fi else "Thanks — that's everything I need. I'm checking the available options and will email you when your quote is ready."],
            tag="email_clarification_complete", idempotency_key=key, reply_to_message=reply,
            subject=reply.get("subject") or state.get("question_subject"))
        result = {"status": sent["status"], "email_message_id": sent["id"], "outbox_id": sent.get("outbox_id")}
    except Exception as exc:
        result = {"status": "failed", "reason": type(exc).__name__}
    await db.raw.demo_clarification_acks.update_one({"_id": key}, {"$set": {**result, "updated_at": now_iso()}})
    outbox = await db.raw.email_outbox.find_one({"id": result.get("outbox_id")})
    if outbox and outbox.get("provider") == "resend" and outbox.get("provider_id"):
        from .resend import reconcile
        await reconcile(outbox["provider_id"])
    return await db.raw.demo_clarification_acks.find_one({"_id": key}, {"_id": 0})
