"""Purchase-invoice inbox (ostolaskut@...): carriers e-mail PDF or Finvoice/PEPPOL XML invoices; we read them over IMAP.

Env: AP_IMAP_HOST, AP_IMAP_PORT (993 ssl / 143 otherwise), AP_IMAP_USER, AP_IMAP_PASSWORD,
AP_IMAP_SECURITY ssl|starttls|none (default ssl), AP_IMAP_FOLDER (INBOX). AP_INBOUND_REQUIRE_AUTH=true (production)
only accepts mail whose Authentication-Results header (added by the receiving mail server) says dkim=pass or
dmarc=pass; anything else is quarantined to an owner card. Sandbox: greenmail on 127.0.0.1:3143, security none, auth
not required. Going live = set the AP_IMAP_* variables to the real mailbox; nothing else changes.

Each UNSEEN message: attachments (pdf, xml, png, jpg) -> services.carrier_invoices.ingest_file(source="email"),
one `ap_messages` row {id, message_id, from, subject, received_at, attachments[], carrier_invoice_ids[], status},
then the message is flagged \\Seen. A message already in `ap_messages` is never ingested twice.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import os
import re
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from core.approvals import request_approval
from core.audit import audit
from core.db import db, register_index
from core.events import on
from core.storage import get_file, put_file
from core.util import NO_ID, iso, new_id, now_iso
from services import finance_core as fc

log = logging.getLogger("finance.ap_mailbox")
register_index("ap_messages", "id", unique=True)
register_index("ap_messages", "message_id", unique=True, partialFilterExpression={"message_id": {"$type": "string"}})

ATTACHMENT_EXT = {".pdf": "application/pdf", ".xml": "application/xml", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg"}
ATTACHMENT_TYPES = {"application/pdf", "application/xml", "text/xml", "image/png", "image/jpeg", "image/jpg"}
_lock = asyncio.Lock()


def configured() -> bool:
    return bool(os.environ.get("AP_IMAP_HOST"))


def _cfg() -> dict:
    sec = (os.environ.get("AP_IMAP_SECURITY") or "ssl").lower()
    port = int(os.environ.get("AP_IMAP_PORT") or (993 if sec == "ssl" else 143))
    return {"host": os.environ.get("AP_IMAP_HOST"), "port": port, "security": sec,
            "user": os.environ.get("AP_IMAP_USER") or "", "password": os.environ.get("AP_IMAP_PASSWORD") or "",
            "folder": os.environ.get("AP_IMAP_FOLDER") or "INBOX"}


def require_auth() -> bool:
    return (os.environ.get("AP_INBOUND_REQUIRE_AUTH") or "false").strip().lower() in ("1", "true", "yes", "on")


def _connect(c: dict) -> imaplib.IMAP4:
    if c["security"] == "ssl":
        m = imaplib.IMAP4_SSL(c["host"], c["port"], timeout=30)
    else:
        m = imaplib.IMAP4(c["host"], c["port"], timeout=30)
        if c["security"] == "starttls":
            m.starttls()
    m.login(c["user"], c["password"])
    return m


def _fetch_unseen(c: dict, limit: int) -> list[tuple[bytes, bytes]]:
    m = _connect(c)
    try:
        typ, _ = m.select(c["folder"])
        if typ != "OK":
            raise RuntimeError(f"IMAP folder {c['folder']} cannot be opened")
        typ, data = m.uid("SEARCH", None, "UNSEEN")
        uids = (data[0] or b"").split()[:limit]
        out = []
        for uid in uids:
            typ, msg = m.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((part[1] for part in msg or [] if isinstance(part, tuple)), None)
            if typ == "OK" and raw:
                out.append((uid, raw))
        return out
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _mark_seen(c: dict, uids: list[bytes]):
    if not uids:
        return
    m = _connect(c)
    try:
        m.select(c["folder"])
        for uid in uids:
            m.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _auth_ok(msg) -> tuple[bool, str]:
    results = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or []))
    ok = bool(re.search(r"\bdkim=pass\b", results, re.I) or re.search(r"\bdmarc=pass\b", results, re.I))
    return ok, results[:500]


def _attachments(msg) -> list[dict]:
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        ctype = (part.get_content_type() or "").lower()
        ext = os.path.splitext(name.lower())[1]
        if not (ext in ATTACHMENT_EXT or (ctype in ATTACHMENT_TYPES and part.get_content_disposition() == "attachment")):
            continue
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        out.append({"filename": name or f"attachment{ext or '.bin'}", "content_type": ATTACHMENT_EXT.get(ext, ctype),
                    "data": data, "size": len(data)})
    return out


async def _handle(raw: bytes, actor: dict | None, *, delivery_id: str | None = None,
                  quarantine_unverified_sender: bool = False) -> dict:
    from services.carrier_invoices import ingest_file

    msg = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = delivery_id or (msg.get("Message-ID") or "").strip() or None
    existing = await db.ap_messages.find_one({"message_id": message_id}, NO_ID) if message_id else None
    if existing:
        # Repair the storage -> owner-card boundary on worker retry.
        if existing.get("status") == "quarantined":
            await _quarantine_review(existing)
        return existing if delivery_id else {"message_id": message_id, "status": "already_processed"}
    sender = parseaddr(str(msg.get("From") or ""))[1] or None
    try:
        received = iso(parsedate_to_datetime(str(msg.get("Date")))) if msg.get("Date") else now_iso()
    except (TypeError, ValueError):
        received = now_iso()
    atts = _attachments(msg)
    row = {"id": new_id(), "message_id": message_id, "from": sender, "subject": str(msg.get("Subject") or "")[:300],
           "received_at": received, "fetched_at": now_iso(),
           "attachments": [{"filename": a["filename"], "content_type": a["content_type"], "size": a["size"]} for a in atts],
           "carrier_invoice_ids": [], "status": "processed", "errors": []}
    auth_ok, auth_hdr = _auth_ok(msg)
    row["authentication"] = {"required": require_auth() or quarantine_unverified_sender,
                             "pass": auth_ok and not quarantine_unverified_sender, "results": auth_hdr or None}
    if delivery_id:
        row["original_message_id"] = str(msg.get("Message-ID") or "") or None
        row["provider"] = "postmark"
        row["authentication"]["provider_authenticated"] = True
        row["authentication"]["reason"] = "sender_authentication_unverified"
    if (atts and quarantine_unverified_sender) or (not quarantine_unverified_sender and require_auth() and not auth_ok):
        # Quarantine: keep the files, let a person decide (spoofed carrier mail is the classic invoice fraud).
        for a, meta in zip(atts, row["attachments"]):
            meta["file_id"] = await put_file(a["data"], a["filename"], a["content_type"], {"kind": "ap_quarantine"})
        row["status"] = "quarantined"
        await db.ap_messages.insert_one(dict(row))
        await _quarantine_review(row)
        await audit(fc.AGENT_ACTOR, "ap_message_quarantined", "ap_message", row["id"], {"from": sender, "auth": auth_hdr})
        row.pop("_id", None)
        return row
    if not atts:
        row["status"] = "no_attachments"
    for a in atts:
        try:
            ci = await ingest_file(a["data"], a["filename"], a["content_type"], source="email", actor=actor,
                                   message_id=message_id, sender=sender,
                                   email_meta={"from": sender, "subject": row["subject"], "received_at": received})
            row["carrier_invoice_ids"].append(ci["id"])
        except ValueError as exc:
            row["errors"].append({"filename": a["filename"], "error": str(exc)})
    await db.ap_messages.insert_one(dict(row))
    row.pop("_id", None)
    return row


async def _quarantine_review(row: dict):
    sender = row.get("from") or "unavailable"
    await request_approval(
        "carrier_invoice_review", "ap_message", row["id"], f"Karanteeni: ostolaskuviesti {sender}",
        summary=f"Viestin '{row['subject']}' lähettäjää ei ole varmistettu. Tarkista lähettäjä ennen vapautusta.",
        reason="Lähettäjän varmennus puuttuu", requested_by=fc.AGENT_ACTOR,
        data={"title_en": f"Quarantine: invoice email from {sender}",
              "summary_en": f"The sender of '{row['subject']}' is unverified. Verify before releasing the invoice.",
              "ap_message_id": row["id"], "from": row.get("from"), "authentication": row["authentication"],
              "attachments": row["attachments"]}, link="/ops/finance/carrier-invoices")


async def poll(*, actor: dict | None = None, limit: int = 50) -> dict:
    """Read UNSEEN messages once. Raises RuntimeError with a plain sentence when the mailbox cannot be read."""
    if not configured():
        return {"configured": False, "messages": 0, "invoices": 0, "items": []}
    c = _cfg()
    async with _lock:
        try:
            fetched = await asyncio.to_thread(_fetch_unseen, c, limit)
        except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
            raise RuntimeError(f"The invoice mailbox {c['user']} at {c['host']}:{c['port']} could not be read: {exc}") from None
        items, seen = [], []
        for uid, raw in fetched:
            try:
                items.append(await _handle(raw, actor))
                seen.append(uid)
            except Exception as exc:  # noqa: BLE001 - leave the message unseen; it is retried on the next poll
                log.exception("AP message %s failed", uid)
                items.append({"uid": uid.decode(), "status": "failed", "error": str(exc)[:300]})
        await asyncio.to_thread(_mark_seen, c, seen)
    res = {"configured": True, "messages": len(fetched), "invoices": sum(len(i.get("carrier_invoice_ids") or []) for i in items),
           "items": items, "at": now_iso()}
    await db.system.update_one({"key": "ap_mailbox_poll"}, {"$set": {"key": "ap_mailbox_poll", "at": res["at"],
                                                                     "messages": res["messages"], "invoices": res["invoices"]}},
                               upsert=True)
    return res


@fc.finance_job("finance.ap_mailbox_poll", every_seconds=120 if configured() else None,
                label_fi="Ostolaskupostin luku", label_en="Read the purchase-invoice inbox")
async def poll_job(job: dict):
    if not configured():
        return {"configured": False}
    res = await poll(actor=fc.AGENT_ACTOR)
    return {k: v for k, v in res.items() if k != "items"}


async def release_quarantined(ap_message_id: str, actor: dict | None) -> dict:
    from services.carrier_invoices import ingest_file

    row = await db.ap_messages.find_one({"id": ap_message_id}, NO_ID)
    if not row or row.get("status") != "quarantined":
        return row or {}
    ids = []
    for a in row.get("attachments") or []:
        if not a.get("file_id"):
            continue
        data, name, ctype = await get_file(a["file_id"])
        ci = await ingest_file(data, name, ctype, source="email", actor=actor, message_id=row.get("message_id"),
                               sender=row.get("from"), email_meta={"from": row.get("from"), "subject": row.get("subject"),
                                                                   "received_at": row.get("received_at"), "released_from_quarantine": True})
        ids.append(ci["id"])
    await db.ap_messages.update_one({"id": ap_message_id}, {"$set": {"status": "released", "carrier_invoice_ids": ids,
                                                                     "released_at": now_iso()}})
    await audit(actor, "ap_message_released", "ap_message", ap_message_id, {"carrier_invoice_ids": ids})
    return await db.ap_messages.find_one({"id": ap_message_id}, NO_ID)


@on("approval.decided")
async def _on_decided(payload: dict):
    a = payload.get("approval") or {}
    if a.get("kind") != "carrier_invoice_review" or a.get("ref_type") != "ap_message":
        return
    if a.get("status") == "approved":
        await release_quarantined(a["ref_id"], payload.get("actor"))
    elif a.get("status") == "rejected":
        await db.ap_messages.update_one({"id": a["ref_id"]}, {"$set": {"status": "discarded", "discarded_at": now_iso()}})
        await audit(payload.get("actor"), "ap_message_discarded", "ap_message", a["ref_id"], {})
