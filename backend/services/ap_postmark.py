"""Durable AP intake behind the shared, authenticated Postmark inbound route.

AP_INBOUND_ADDRESSES contains exact comma-separated envelope recipients. Configure a
separate Postmark +ap address, not the shared customer/RFQ address. No subject or
attachment heuristic may turn a customer's commercial invoice into our payable.
Native provider authentication is not proof of the carrier's identity: copied MIME
Authentication-Results are untrusted. Receipts use the existing quarantine review
before invoice extraction/audit. No provider requests or outgoing mail here.
"""
from __future__ import annotations

import base64
import hashlib
import os

from pymongo.errors import DuplicateKeyError

from core.db import db
from core.jobs import enqueue, job
from core.storage import delete_file, get_file, put_file
from core.util import NO_ID, now_iso

MAX_RAW_BYTES = 30 * 1024 * 1024
MAX_ATTACHMENTS = 50


def addresses() -> set[str]:
    return {v.strip().lower() for v in os.environ.get("AP_INBOUND_ADDRESSES", "").split(",") if v.strip()}


async def try_ingest_postmark(raw: bytes, *, payload: dict | None, authenticated_provider: bool) -> dict | None:
    """None means non-AP; a dict means durably consumed, even without attachments.

    Call only after shared auth and RFQ dispatch. Errors must produce a non-200
    response so Postmark retries; acknowledge only after storage AND enqueue.
    """
    if not isinstance(payload, dict):
        return None
    recipient = payload.get("OriginalRecipient")
    if not isinstance(recipient, str) or recipient.strip().lower() not in addresses():
        return None
    if not authenticated_provider:
        raise ValueError("AP Postmark delivery requires authenticated provider credentials.")
    provider_id = payload.get("MessageID")
    if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 200:
        raise ValueError("AP Postmark delivery requires a valid MessageID.")
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError("AP message exceeds the 30 MiB limit.")
    attachments = payload.get("Attachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("AP message exceeds the attachment limit.")
    # The shared MIME converter accepts permissive base64. AP must not silently
    # accept a truncated/corrupt invoice. Validate the original provider fields.
    for a in attachments:
        if not isinstance(a, dict) or not isinstance(a.get("Content"), str):
            raise ValueError("AP attachment content is invalid.")
        data = base64.b64decode(a["Content"], validate=True)
        length = a.get("ContentLength")
        if length is not None and (type(length) is not int or length != len(data)):
            raise ValueError("AP attachment length does not match its content.")
    key = hashlib.sha256(provider_id.encode()).hexdigest()
    receipt = await db.ap_webhook_receipts.find_one({"_id": key}, NO_ID)
    created = False
    if not receipt:
        raw_id = await put_file(raw, "ap-message.eml", "message/rfc822", {"kind": "ap_inbound", "provider": "postmark"})
        receipt = {"id": key, "provider": "postmark", "provider_id": provider_id,
                   "original_recipient": recipient, "raw_file_id": raw_id,
                   "received_at": now_iso(), "status": "received"}
        try:
            await db.ap_webhook_receipts.insert_one({"_id": key, **receipt})
            created = True
        except DuplicateKeyError:
            await delete_file(raw_id)
            receipt = await db.ap_webhook_receipts.find_one({"_id": key}, NO_ID)
            if not receipt:
                raise RuntimeError("AP receipt reservation could not be recovered.") from None
    # Re-enqueue on duplicate too: repairs a crash after storage, before enqueue.
    j = await enqueue("finance.ap_postmark", {"receipt_id": key}, key=f"ap-postmark:{key}",
                      subject=("ap_message", key), source="postmark")
    return {"id": key, "job_id": j["id"], "duplicate": not created}


@job("finance.ap_postmark", agent=False, label_fi="Ostolaskuviestin vastaanotto", label_en="Receive supplier invoice email")
async def process_job(j: dict) -> dict:
    from services.ap_mailbox import _handle

    key = j["payload"]["receipt_id"]
    receipt = await db.ap_webhook_receipts.find_one({"_id": key}, NO_ID)
    if not receipt:
        raise RuntimeError("Stored AP receipt is missing.")
    raw, _, _ = await get_file(receipt["raw_file_id"])
    # Provider ID is the retry identity, not the sender-controlled MIME Message-ID.
    row = await _handle(raw, None, delivery_id=f"postmark:{receipt['provider_id']}",
                        quarantine_unverified_sender=True)
    await db.ap_webhook_receipts.update_one({"_id": key}, {"$set": {
        "status": row["status"], "ap_message_id": row["id"], "processed_at": now_iso()}})
    return {"ap_message_id": row["id"], "status": row["status"]}
