"""Customer email work on the shared durable job queue.

Inbox collection keeps ingestion/processing state across restarts. Recurring jobs recover any message
whose ingestion completed before the process stopped. The global kill switch holds processing, while
inbound storage and payment confirmation continue; no duplicate lifecycle worker runs beside these jobs.
"""
from __future__ import annotations
import logging
import os
import socket
from core.db import db
from core.jobs import job, schedule
from . import conf, inbound

log = logging.getLogger("email_channel")
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def enabled() -> bool:
    return os.environ.get("EMAIL_CHANNEL_WORKER", "true").lower() not in ("0", "false", "no", "off")


@job("email.poll_inbox", label_fi="Sähköpostien nouto", label_en="Email collection")
async def poll_inbox(j):
    return await inbound.poll_imap() if enabled() else {"enabled": False}


@job("email.process_inbound", agent=True, label_fi="Sähköpostien käsittely", label_en="Email processing")
async def process_inbound(j):
    if not enabled():
        return {"processed": 0}
    n = await inbound.process_pending(j.get("lease_owner") or OWNER)
    # Recover a KYC-approved booking whose event arrived during a halt or process restart.
    from . import flow
    async for t in db.email_threads.find({"state": "booking_pending_kyc"}, {"_id": 0}).limit(20):
        c = await db.companies.find_one({"id": t.get("company_id")}, {"kyc_status": 1, "sanctions_status": 1}) or {}
        if c.get("kyc_status") == "approved" and c.get("sanctions_status") == "clear":
            await flow.do_booking(t)
    return {"processed": n}


@job("email.payment_sync", label_fi="Sähköpostimaksujen täsmäytys", label_en="Email payment reconciliation")
async def payment_sync(j):
    from services import payments
    if not enabled() or not payments.stripe_enabled():
        return
    async for tx in db.payment_transactions.find({"status": "initiated", "email_channel": True}, {"_id": 0, "invoice_id": 1}).limit(20):
        await payments.sync_invoice(tx["invoice_id"])


schedule("email.poll_inbox", job="email.poll_inbox", every_seconds=conf.poll_seconds())()
schedule("email.process_inbound", job="email.process_inbound", every_seconds=5)()
schedule("email.payment_sync", job="email.payment_sync", every_seconds=60)()


async def run_once() -> dict:
    """Ops requests the same durable jobs; it cannot bypass the kill switch."""
    from core.jobs import enqueue
    a = await enqueue("email.poll_inbox", source="manual")
    b = await enqueue("email.process_inbound", source="manual")
    return {"jobs": [a["id"], b["id"]], "queued": True}


@job("email.retry_outbound", agent=True, max_attempts=5, backoff=(60, 300, 900, 3600, 7200),
     label_fi="Sähköpostin uudelleenlähetys", label_en="Email delivery retry")
async def retry_outbound(j):
    from core.email import send_email
    from core.storage import get_file
    row = await db.email_messages.find_one({"id": j["payload"]["message_id"]}, {"_id": 0})
    if not row or not row.get("retry_payload"):
        return {"missing": True}
    latest = await db.email_outbox.find_one({"ref.email_message_id": row["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    if latest and latest.get("status") != "failed":
        return {"status": latest["status"]}
    attachments = []
    for a in row.get("attachments") or []:
        data, name, ctype = await get_file(a["file_id"])
        attachments.append((name, ctype, data))
    rec = await send_email(**row["retry_payload"], attachments=attachments, provider=conf.provider(), smtp=conf.smtp(),
                           from_addr=conf.sender(), reply_to=conf.address(),
                           ref={"type": "email_thread", "id": row["thread_id"], "email_message_id": row["id"]})
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"status": rec["status"], "outbox_id": rec["id"], "error": rec.get("error")}})
    if rec["status"] == "failed":
        raise RuntimeError("Email provider did not accept the message")
    return {"status": rec["status"], "outbox_id": rec["id"]}


@job("email.recover_delivery", label_fi="Epäonnistuneiden sähköpostien palautus", label_en="Recover failed email delivery")
async def recover_delivery(j):
    from core.jobs import enqueue
    if not enabled():
        return
    async for rec in db.email_outbox.find({"status": "failed", "ref.email_message_id": {"$exists": True}}, {"_id": 0}).sort("created_at", -1).limit(100):
        mid = rec["ref"]["email_message_id"]
        row = await db.email_messages.find_one({"id": mid, "retry_payload": {"$exists": True}}, {"_id": 0})
        if row:
            await enqueue("email.retry_outbound", {"message_id": mid}, key=f"email-delivery:{mid}",
                          company_id=row["retry_payload"].get("company_id"), subject=("email_message", mid))


schedule("email.recover_delivery", job="email.recover_delivery", every_seconds=60)()
