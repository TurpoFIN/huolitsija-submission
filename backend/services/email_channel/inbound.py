"""Inbound customer mail: one idempotent ingest (keyed by Message-ID) fed by the IMAP poller and the webhook, and
the durable processing queue over email_messages (lease, retries with backoff, owner queue after the last try)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import imaplib
import logging
import ssl
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core.db import db
from core.events import emit
from core.storage import put_file
from core.util import iso, new_id, now, now_iso, span

from . import conf, mime

log = logging.getLogger("email_channel")
MAX_ATTEMPTS = 5
BACKOFF_S = (30, 120, 600, 1800, 3600)
LEASE_S = 300
MAX_PER_POLL = 50


async def ingest(raw: bytes, *, source: str) -> tuple[dict, bool]:
    """Store one raw RFC 822 message. -> (row, created). A Message-ID seen before returns the stored row."""
    m = mime.parse(raw)
    if not m["message_id"]:
        m["message_id"] = f"<sha256-{hashlib.sha256(raw).hexdigest()[:40]}@inbound.local>"
    existing = await db.email_messages.find_one({"message_id": m["message_id"], "direction": "in"}, {"_id": 0})
    if existing:
        return existing, False
    raw_id = await put_file(raw, "message.eml", "message/rfc822", {"kind": "email_inbound"})
    atts = []
    for a in m.pop("attachments"):
        fid = await put_file(a["data"], a["filename"], a["content_type"], {"kind": "email_attachment"})
        atts.append({"filename": a["filename"], "content_type": a["content_type"], "size": a["size"], "file_id": fid,
                     "document_id": None})
    row = {"id": new_id(), "thread_id": None, "direction": "in", **m, "attachments": atts, "raw_file_id": raw_id,
           "source": source, "status": "received", "attempts": 0, "next_attempt_at": now_iso(), "intent": None,
           "parse": None, "decision": None, "automatic": mime.is_automatic(m), "created_at": now_iso()}
    try:
        await db.email_messages.insert_one(dict(row))
    except DuplicateKeyError:
        return await db.email_messages.find_one({"message_id": m["message_id"], "direction": "in"}, {"_id": 0}), False
    await emit("email.received", {"message": {k: v for k, v in row.items() if k != "text"}})
    return row, True


def postmark_to_raw(payload: dict) -> bytes:
    """Postmark inbound JSON -> RFC 822 bytes, so both inputs go through one parser."""
    from email.message import EmailMessage

    if payload.get("RawEmail"):
        return payload["RawEmail"].encode("utf-8")
    msg = EmailMessage()
    for h in payload.get("Headers") or []:
        if h.get("Name", "").lower() not in ("content-type", "content-transfer-encoding", "mime-version"):
            try:
                msg[h["Name"]] = h.get("Value", "")
            except Exception:  # noqa: BLE001
                pass
    for name, key in (("From", "From"), ("To", "To"), ("Subject", "Subject"), ("Message-ID", "MessageID"), ("Date", "Date")):
        if payload.get(key) and name not in msg:
            v = payload[key]
            msg[name] = v if name != "Message-ID" or v.startswith("<") else f"<{v}>"
    msg.set_content(payload.get("TextBody") or payload.get("StrippedTextReply") or "")
    if payload.get("HtmlBody"):
        msg.add_alternative(payload["HtmlBody"], subtype="html")
    for a in payload.get("Attachments") or []:
        ct = (a.get("ContentType") or "application/octet-stream").split("/", 1)
        msg.add_attachment(base64.b64decode(a.get("Content") or ""), maintype=ct[0], subtype=ct[1] if len(ct) > 1 else "octet-stream",
                           filename=a.get("Name") or "attachment")
    return msg.as_bytes()


# ------------------------------------------------------------------------------------------------ IMAP

def _connect(c: dict) -> imaplib.IMAP4:
    if c["security"] == "ssl":
        conn = imaplib.IMAP4_SSL(c["host"], c["port"], ssl_context=ssl.create_default_context(), timeout=20)
    else:
        conn = imaplib.IMAP4(c["host"], c["port"], timeout=20)
        if c["security"] == "starttls":
            conn.starttls(ssl.create_default_context())
    conn.login(c["user"], c["password"] or "-")
    return conn


def _fetch_blocking(c: dict) -> list[tuple[str, bytes]]:
    conn = _connect(c)
    try:
        conn.select(c["folder"])
        typ, data = conn.uid("search", None, "UNSEEN")
        out = []
        for uid in (data[0] or b"").split()[:MAX_PER_POLL] if typ == "OK" else []:
            typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if typ == "OK" and raw:
                out.append((uid.decode(), raw))
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _mark_seen_blocking(c: dict, uids: list[str]):
    conn = _connect(c)
    try:
        conn.select(c["folder"])
        for uid in uids:
            conn.uid("store", uid.encode(), "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def poll_imap() -> dict:
    """Fetch unseen mail (BODY.PEEK) -> ingest -> mark seen. A crash between the two re-reads; ingest dedupes."""
    c = conf.imap()
    if not c:
        return {"configured": False}
    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(None, _fetch_blocking, c)
    except Exception as exc:  # noqa: BLE001
        await db.system.update_one({"key": "email_channel:imap"}, {"$set": {"last_error": str(exc)[:300], "last_error_at": now_iso()}},
                                   upsert=True)
        log.warning("email channel IMAP poll failed: %s", exc)
        return {"configured": True, "error": str(exc)}
    new = 0
    for _uid, raw in items:
        _row, created = await ingest(raw, source="imap")
        new += int(created)
    if items:
        await loop.run_in_executor(None, _mark_seen_blocking, c, [u for u, _ in items])
    await db.system.update_one({"key": "email_channel:imap"}, {"$set": {"last_poll_at": now_iso(), "last_fetched": len(items),
                                                                         "last_new": new}}, upsert=True)
    return {"configured": True, "fetched": len(items), "new": new}


# ------------------------------------------------------------------------------------------------ processing queue

async def claim_next(owner: str) -> dict | None:
    t = now_iso()
    return await db.email_messages.find_one_and_update(
        {"direction": "in", "$or": [{"status": {"$in": ["received", "retry"]}, "next_attempt_at": {"$lte": t}},
                                    {"status": "processing", "lease_until": {"$lt": t}}]},
        {"$set": {"status": "processing", "lease_owner": owner, "lease_until": iso(now() + span(LEASE_S))},
         "$inc": {"attempts": 1}},
        sort=[("created_at", 1)], projection={"_id": 0}, return_document=ReturnDocument.AFTER)


async def process_pending(owner: str = "worker", limit: int = 20) -> int:
    from . import flow

    done = 0
    while done < limit:
        row = await claim_next(owner)
        if not row:
            break
        done += 1
        try:
            await flow.process(row)
        except Exception as exc:  # noqa: BLE001 - retry with backoff, then the owner decides
            log.exception("email %s failed (attempt %s)", row.get("message_id"), row.get("attempts"))
            if row["attempts"] >= MAX_ATTEMPTS:
                await db.email_messages.update_one({"id": row["id"]}, {"$set": {"status": "failed", "error": str(exc)[:500]}})
                try:
                    await flow.to_owner(await _thread(row), row, "processing_failed", ack=False, summary=str(exc)[:300])
                except flow.Stop:
                    pass
            else:
                delay = BACKOFF_S[min(row["attempts"] - 1, len(BACKOFF_S) - 1)]
                await db.email_messages.update_one({"id": row["id"]}, {"$set": {
                    "status": "retry", "error": str(exc)[:500], "next_attempt_at": iso(now() + timedelta(seconds=delay))}})
        finally:
            await db.email_messages.update_one({"id": row["id"]}, {"$unset": {"lease_owner": "", "lease_until": ""}})
    return done


async def _thread(row: dict):
    fresh = await db.email_messages.find_one({"id": row["id"]}, {"_id": 0}) or row
    return await db.email_threads.find_one({"id": fresh.get("thread_id")}, {"_id": 0}) if fresh.get("thread_id") else None
