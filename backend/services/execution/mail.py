"""E-mail for execution: SI/VGM to carrier booking desks, slot requests to hauliers, advices to warehouses, and the
replies that come back.

Outbound goes over SMTP (`EXECUTION_SMTP_*`, same shape as s3's smtp_settings(prefix)) with attachments (DCSA JSON,
VERMAS EDI, load plans). Without EXECUTION_SMTP_HOST the message is stored only (status "logged"), like core.email's
`log` provider. `EXECUTION_ALLOWED_RECIPIENT_DOMAINS` (comma-separated; go-live = unset) mails only those domains and stores
the rest as logged. `EXECUTION_SANDBOX_DOMAIN` rewrites every external recipient to a mailbox on that domain (the real
address is kept on the record) until the company mailbox exists.

Inbound: IMAP (`EXECUTION_IMAP_*`), matched to the outbound message by In-Reply-To/References, else by the
`[HUO-… KIND]` tag in the subject. Each reply is stored in `execution_messages` (direction "in") and handed to the
reply handler registered for the outbound message's kind (si, vgm, slot, warehouse).
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import hashlib
import imaplib
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

from core import config
from core.db import db, register_index
from core.util import new_id, now_iso

log = logging.getLogger("execution.mail")
register_index("execution_messages", "message_id", unique=True, sparse=True)
register_index("execution_messages", [("ref_type", 1), ("ref_id", 1), ("created_at", -1)])

_REPLY_HANDLERS: dict[str, object] = {}


def reply_handler(kind: str):
    def deco(fn):
        _REPLY_HANDLERS[kind] = fn
        return fn

    return deco


def smtp_conf() -> dict:
    p = "EXECUTION_SMTP"
    return {"host": config.env(f"{p}_HOST"), "port": config.env(f"{p}_PORT"), "user": config.env(f"{p}_USER"),
            "password": config.env(f"{p}_PASSWORD"), "security": config.env(f"{p}_SECURITY", "starttls")}


def imap_conf() -> dict:
    p = "EXECUTION_IMAP"
    return {"host": config.env(f"{p}_HOST"), "port": config.env(f"{p}_PORT"), "user": config.env(f"{p}_USER"),
            "password": config.env(f"{p}_PASSWORD"), "security": config.env(f"{p}_SECURITY", "ssl"),
            "folder": config.env(f"{p}_FOLDER", "INBOX")}


def sender() -> str:
    return config.env("EXECUTION_FROM") or config.EMAIL_FROM


def sender_address() -> str:
    return parseaddr(sender())[1] or "ops@huolitsija.fi"


def domain() -> str:
    return config.env("EXECUTION_DOMAIN") or sender_address().split("@")[-1]


def sandbox_recipient(real: str) -> str:
    sb = config.env("EXECUTION_SANDBOX_DOMAIN")
    if not sb:
        return real
    local, _, dom = parseaddr(real)[1].partition("@")
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731
    return f"{slug(local)}.{slug(dom)}@{sb}"


def _smtp_send(msg: EmailMessage, smtp: dict):
    security = (smtp.get("security") or "starttls").lower()
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], int(smtp.get("port") or 465), timeout=20, context=context)
    else:
        server = smtplib.SMTP(smtp["host"], int(smtp.get("port") or 587), timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if smtp.get("user"):
            server.login(smtp["user"], smtp.get("password") or "")
        refused = server.send_message(msg)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def tag(reference: str, kind: str) -> str:
    return f"[{reference} {kind.upper()}]"


async def send(to: str, subject: str, text: str, *, kind: str, ref_type: str, ref_id: str, reference: str = "",
               html: str | None = None, attachments: list[tuple[str, str, bytes]] | None = None,
               company_id: str | None = None, in_reply_to: str | None = None, extra: dict | None = None) -> dict:
    """Send one execution e-mail. Returns the `execution_messages` record (status sent | logged | failed)."""
    real_to = to
    actual_to = sandbox_recipient(to)
    mid = make_msgid(idstring=f"ex{new_id()[:12]}", domain=domain())
    full_subject = f"{subject} {tag(reference, kind)}" if reference else subject
    rec = {"id": new_id(), "direction": "out", "kind": kind, "ref_type": ref_type, "ref_id": ref_id, "reference": reference,
           "company_id": company_id, "from": sender(), "to": actual_to, "to_real": real_to, "subject": full_subject,
           "text": text[:20000], "message_id": mid, "in_reply_to": in_reply_to, "status": "queued", "created_at": now_iso(),
           "attachments": [{"name": n, "content_type": c, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                           for n, c, b in (attachments or [])], "extra": extra or {}}
    from services.demo_world import boundary
    world = await boundary.capture_mail(to=actual_to, subject=full_subject, text=text, html=html, from_addr=sender(),
                                        reply_to=sender_address(), headers={"X-Huolitsija-Ref": f"{ref_type}:{ref_id}"},
                                        attachments=attachments, message_id=mid, in_reply_to=in_reply_to,
                                        channel="execution", ref={"type": ref_type, "id": ref_id}, company_id=company_id,
                                        tag=f"execution_{kind}")
    if world:
        rec.update(status="sent", provider="world", sent_at=world["sent_at"], to=real_to, world=world["world"])
        await db.execution_messages.insert_one(dict(rec))
        await db.email_outbox.insert_one({"id": new_id(), "to": real_to, "subject": full_subject, "tag": f"execution_{kind}",
                                          "provider": "world", "company_id": company_id, "status": "sent",
                                          "ref": {"type": ref_type, "id": ref_id}, "created_at": rec["created_at"],
                                          "text": text[:20000], "message_id": mid})
        rec.pop("_id", None)
        return rec
    smtp = smtp_conf()
    allowed = [d.strip().lower() for d in config.env("EXECUTION_ALLOWED_RECIPIENT_DOMAINS").split(",") if d.strip()]
    if allowed and parseaddr(actual_to)[1].rpartition("@")[2].lower() not in allowed:
        smtp = {**smtp, "host": None}  # outside the allowlist: stored only (status logged), never mailed
        rec["note"] = "recipient outside EXECUTION_ALLOWED_RECIPIENT_DOMAINS"
    try:
        if smtp["host"]:
            msg = EmailMessage()
            msg["Message-ID"] = mid
            msg["Date"] = formatdate(localtime=False)
            msg["From"], msg["To"], msg["Subject"] = sender(), actual_to, full_subject
            msg["Reply-To"] = sender_address()
            msg["X-Huolitsija-Ref"] = f"{ref_type}:{ref_id}"
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = in_reply_to
            msg.set_content(text)
            if html:
                msg.add_alternative(html, subtype="html")
            for name, ctype, data in attachments or []:
                maintype, _, subtype = ctype.partition("/")
                msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
            await asyncio.get_running_loop().run_in_executor(None, _smtp_send, msg, smtp)
            rec.update(status="sent", provider="smtp", sent_at=now_iso())
        else:
            rec.update(status="logged", provider="log")
    except Exception as exc:  # noqa: BLE001
        log.warning("execution mail to %s failed: %s", actual_to, exc)
        rec.update(status="failed", error=str(exc)[:500])
    await db.execution_messages.insert_one(dict(rec))
    await db.email_outbox.insert_one({"id": new_id(), "to": actual_to, "subject": full_subject, "tag": f"execution_{kind}",
                                      "provider": rec.get("provider"), "company_id": company_id, "status": rec["status"],
                                      "ref": {"type": ref_type, "id": ref_id}, "created_at": rec["created_at"], "text": text[:20000]})
    rec.pop("_id", None)
    return rec


# ------------------------------------------------------------------------------------------------- inbound

def _imap_fetch_unseen(conf: dict, limit: int = 50) -> list[tuple[bytes, bytes]]:
    security = (conf.get("security") or "ssl").lower()
    port = int(conf.get("port") or (993 if security == "ssl" else 143))
    if security == "ssl":
        box = imaplib.IMAP4_SSL(conf["host"], port, ssl_context=ssl.create_default_context())
    else:
        box = imaplib.IMAP4(conf["host"], port)
        if security == "starttls":
            box.starttls(ssl_context=ssl.create_default_context())
    out = []
    try:
        box.login(conf["user"], conf.get("password") or "x")
        box.select(conf.get("folder") or "INBOX")
        typ, data = box.uid("search", None, "UNSEEN")
        for uid in (data[0].split() if data and data[0] else [])[:limit]:
            typ, msg = box.uid("fetch", uid, "(RFC822)")
            if msg and msg[0]:
                out.append((uid, msg[0][1]))
                box.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def parse_raw(raw: bytes) -> dict:
    m = email.message_from_bytes(raw, policy=email.policy.default)
    body = m.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    atts = []
    for part in m.iter_attachments():
        data = part.get_payload(decode=True) or b""
        atts.append({"name": part.get_filename() or "attachment", "content_type": part.get_content_type(), "size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest(), "data": data})
    refs = " ".join(filter(None, [m.get("In-Reply-To", ""), m.get("References", "")]))
    return {"message_id": (m.get("Message-ID") or "").strip(), "from": parseaddr(m.get("From", ""))[1].lower(),
            "to": [a for _, a in getaddresses(m.get_all("To", []))], "subject": m.get("Subject", ""), "text": text,
            "references": re.findall(r"<[^>]+>", refs), "attachments": atts, "date": m.get("Date")}


async def match_outbound(parsed: dict) -> dict | None:
    if parsed["references"]:
        out = await db.execution_messages.find_one({"direction": "out", "message_id": {"$in": parsed["references"]}}, {"_id": 0},
                                                   sort=[("created_at", -1)])
        if out:
            return out
    m = re.search(r"\[([A-Z]{2,4}-\d{4}-\d{3,8}) ([A-Z_]+)\]", parsed["subject"] or "")
    if m:
        return await db.execution_messages.find_one({"direction": "out", "reference": m.group(1), "kind": m.group(2).lower()},
                                                    {"_id": 0}, sort=[("created_at", -1)])
    return None


async def ingest(raw: bytes) -> dict | None:
    """Store one inbound message (idempotent per Message-ID) and run its reply handler."""
    parsed = parse_raw(raw)
    if parsed["message_id"] and await db.execution_messages.find_one({"message_id": parsed["message_id"]}, {"_id": 1}):
        return None
    out = await match_outbound(parsed)
    rec = {"id": new_id(), "direction": "in", "kind": (out or {}).get("kind") or "unmatched", "ref_type": (out or {}).get("ref_type"),
           "ref_id": (out or {}).get("ref_id"), "reference": (out or {}).get("reference"), "company_id": (out or {}).get("company_id"),
           "from": parsed["from"], "to": ", ".join(parsed["to"]), "subject": parsed["subject"], "text": parsed["text"][:20000],
           "message_id": parsed["message_id"] or None, "in_reply_to": (out or {}).get("message_id"), "status": "received",
           "attachments": [{k: v for k, v in a.items() if k != "data"} for a in parsed["attachments"]], "created_at": now_iso()}
    await db.execution_messages.insert_one(dict(rec))
    rec.pop("_id", None)
    handler = _REPLY_HANDLERS.get(rec["kind"])
    if handler and out and parsed["from"].lower() in {parseaddr(out.get(k, ""))[1].lower() for k in ("to", "to_real")}:
        try:
            result = await handler(rec, out, parsed)
            await db.execution_messages.update_one({"id": rec["id"]}, {"$set": {"status": "processed", "result": result}})
            rec.update(status="processed", result=result)
        except Exception as exc:  # noqa: BLE001
            log.exception("reply handler %s failed", rec["kind"])
            await db.execution_messages.update_one({"id": rec["id"]}, {"$set": {"status": "failed", "error": str(exc)[:500]}})
            rec.update(status="failed", error=str(exc)[:500])
    return rec


async def poll_inbox() -> int:
    conf = imap_conf()
    if not conf["host"] or not conf["user"]:
        return 0
    msgs = await asyncio.get_running_loop().run_in_executor(None, _imap_fetch_unseen, conf)
    n = 0
    for _uid, raw in msgs:
        if await ingest(raw):
            n += 1
    return n
