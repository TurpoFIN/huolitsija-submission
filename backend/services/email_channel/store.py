"""Threads and the full in/out message log of the e-mail channel.

email_threads  {id, ref, subject, customer_email, customer_name, company_id, user_id, quote_id, quote_number,
                shipment_id, shipment_reference, language, state, pending{...}, last_message_at, counts{in,out},
                needs_owner, created_at, updated_at}
email_messages {id, thread_id, direction in|out, message_id, in_reply_to, references[], from_email, from_name,
                to[], subject, text, new_text, attachments[{filename, content_type, size, file_id, document_id}],
                raw_file_id, source imap|webhook|smtp, status received|processing|processed|needs_owner|ignored|failed|
                sent|logged, intent, parse{}, decision, error, attempts, lease_until, approval_id, tag, created_at}
"""
from __future__ import annotations

import re
from email.utils import parseaddr

from core.db import db, register_index
from core.util import NO_ID, new_id, now_iso

from . import conf

REF = re.compile(r"\[(Q-\d{4}-\d{6}|HUO-\d{4}-\d{6}|HUO-INV-\d{4}-\d{6})\]")
BARE_REF = re.compile(r"\b(Q-\d{4}-\d{6}|HUO-(?!INV)\d{4}-\d{6}|HUO-INV-\d{4}-\d{6})\b")

register_index("email_messages", [("message_id", 1), ("direction", 1)], unique=True,
               partialFilterExpression={"message_id": {"$type": "string"}})
register_index("email_messages", [("thread_id", 1), ("created_at", 1)])
register_index("email_messages", [("status", 1), ("next_attempt_at", 1)])
register_index("email_threads", [("customer_email", 1), ("last_message_at", -1)])
register_index("email_threads", [("shipment_id", 1)])
register_index("email_threads", [("quote_id", 1)])


def new_message_id() -> str:
    return f"<em-{new_id()}@{conf.domain()}>"


def subject_ref(subject: str | None) -> str | None:
    m = REF.search(subject or "") or BARE_REF.search(subject or "")
    return m.group(1) if m else None


def with_ref(subject: str, ref: str | None, reply: bool = True) -> str:
    s = re.sub(r"^((re|vs|sv|aw|fw|fwd)\s*:\s*)+", "", (subject or "").strip(), flags=re.I)
    s = REF.sub("", s).strip()
    if ref:
        s = re.sub(r"\s+:", ":", s.replace(ref, "")).strip(" :–-")
        s = f"{s} [{ref}]".strip()
    return f"Re: {s}" if reply else s


async def thread_by_id(thread_id: str) -> dict | None:
    return await db.email_threads.find_one({"id": thread_id}, NO_ID)


async def find_thread_for_inbound(m: dict) -> tuple[dict | None, str]:
    """-> (thread, how). how: header (In-Reply-To/References matched one of our Message-IDs), subject (only the
    reference in the subject, from the thread's own customer), none."""
    ids = [x for x in [m.get("in_reply_to"), *(m.get("references") or [])] if x]
    if ids:
        hit = await db.email_messages.find_one({"$or": [{"message_id": {"$in": ids}}, {"provider_message_ids": {"$in": ids}}],
                                               "thread_id": {"$ne": None}}, NO_ID,
                                               sort=[("created_at", -1)])
        if hit:
            return await thread_by_id(hit["thread_id"]), "header"
    ref = subject_ref(m.get("subject"))
    if ref:
        t = await db.email_threads.find_one({"$or": [{"ref": ref}, {"quote_number": ref}, {"shipment_reference": ref}],
                                             "customer_email": m.get("from_email")}, NO_ID, sort=[("updated_at", -1)])
        if not t and ref.startswith("HUO-INV-"):
            inv = await db.invoices.find_one({"number": ref}, {"shipment_id": 1, "_id": 0})
            if inv and inv.get("shipment_id"):
                t = await db.email_threads.find_one({"shipment_id": inv["shipment_id"], "customer_email": m.get("from_email")}, NO_ID)
        if t:
            return t, "subject"
    return None, "none"


async def create_thread(m: dict, *, language: str) -> dict:
    t = {"id": new_id(), "ref": None, "subject": with_ref(m.get("subject") or "", None, reply=False) or "(no subject)",
         "customer_email": m.get("from_email"), "customer_name": m.get("from_name") or None, "company_id": None,
         "user_id": None, "quote_id": None, "quote_number": None, "shipment_id": None, "shipment_reference": None,
         "language": language, "state": "new", "pending": {}, "needs_owner": False, "counts": {"in": 0, "out": 0},
         "last_message_at": now_iso(), "created_at": now_iso(), "updated_at": now_iso()}
    await db.email_threads.insert_one(dict(t))
    return t


async def update_thread(thread_id: str, **fields) -> dict:
    fields["updated_at"] = now_iso()
    await db.email_threads.update_one({"id": thread_id}, {"$set": fields})
    return await thread_by_id(thread_id)


async def threads_for(*, shipment_id: str | None = None, quote_id: str | None = None, company_id: str | None = None) -> list[dict]:
    q = []
    if shipment_id:
        q.append({"shipment_id": shipment_id})
    if quote_id:
        q.append({"quote_id": quote_id})
    if company_id:
        q.append({"company_id": company_id})
    if not q:
        return []
    return await db.email_threads.find({"$or": q}, NO_ID).sort("updated_at", -1).to_list(50)


async def last_outbound_id(thread_id: str) -> tuple[str | None, list[str]]:
    rows = await db.email_messages.find({"thread_id": thread_id, "message_id": {"$type": "string"}},
                                        {"message_id": 1, "direction": 1, "provider_message_ids": 1, "_id": 0}).sort("created_at", 1).to_list(200)
    refs = [((r.get("provider_message_ids") or [r["message_id"]])[-1]
             if r.get("direction") == "out" else r["message_id"]) for r in rows][-20:]
    return (refs[-1] if refs else None), refs


async def log_outbound(thread: dict | None, *, message_id: str, in_reply_to: str | None, references: list[str], to: str,
                       subject: str, text: str, tag: str, attachments: list[dict] | None = None, kind: str = "reply",
                       provider: str | None = None) -> dict:
    row = {"id": new_id(), "thread_id": (thread or {}).get("id"), "direction": "out", "message_id": message_id,
           "in_reply_to": in_reply_to, "references": references, "from_email": parseaddr(conf.sender())[1], "from_name": parseaddr(conf.sender())[0] or None,
           "to": [to], "subject": subject, "text": text[:20000], "new_text": None,
           "attachments": [{k: a.get(k) for k in ("filename", "content_type", "size", "file_id", "document_id")}
                           for a in attachments or []],
           "status": "sending", "tag": tag, "kind": kind, "provider": provider, "created_at": now_iso()}
    await db.email_messages.insert_one(dict(row))
    if thread:
        await db.email_threads.update_one({"id": thread["id"]}, {"$set": {"last_message_at": now_iso(), "updated_at": now_iso()},
                                                                  "$inc": {"counts.out": 1}})
    return row
