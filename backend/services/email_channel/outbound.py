"""Outbound mail of the e-mail channel: every reply is threaded (Message-ID, In-Reply-To, References, the quote or
shipment reference in the subject), logged in email_messages and sent through core.email.send_email."""
from __future__ import annotations

import re
from datetime import timedelta

from core.db import db
from core.email import render, send_email
from core.security import random_token
from core.storage import put_file
from core.util import iso, now, now_iso

from . import conf, store, texts

PORTAL_LINK = re.compile(r"(https?://[^\s'\"<>]+?)(/portal/[^\s'\"<>]*)")


async def base_url() -> str:
    from services.billing import public_base_url

    return (await public_base_url()).rstrip("/")


async def issue_link(user_id: str, next_path: str, *, days: int | None = None, kind: str = "email_link",
                     data: dict | None = None) -> str:
    """A portal link that signs the customer in without a password (they proved the mailbox by writing from it)."""
    token = random_token(24)
    await db.tokens.insert_one({"token": token, "kind": kind, "user_id": user_id,
                                "data": {"next": next_path, **(data or {})}, "used": False, "uses": 0,
                                "expires_at": iso(now() + timedelta(days=days or conf.link_days())), "created_at": now_iso()})
    return token


async def link_url(user_id: str, next_path: str) -> str:
    return f"{await base_url()}/api/email-channel/go/{await issue_link(user_id, next_path)}"


async def pay_url(user_id: str, invoice_id: str) -> str:
    tok = await db.tokens.find_one({"kind": "email_pay", "data.invoice_id": invoice_id, "user_id": user_id}, {"token": 1, "_id": 0})
    token = tok["token"] if tok else await issue_link(user_id, f"/portal/invoices/{invoice_id}", days=120, kind="email_pay",
                                                      data={"invoice_id": invoice_id})
    return f"{await base_url()}/api/email-channel/pay/{token}"


async def rewrite_portal_links(text: str, html: str, user_id: str) -> tuple[str, str]:
    """Portal links in a mail to an e-mail-only customer become one-click sign-in links (same target page)."""
    targets = {m.group(0): m.group(2) for m in PORTAL_LINK.finditer(text + " " + html)}
    for full, path in targets.items():
        url = await link_url(user_id, path)
        text = text.replace(full, url)
        html = html.replace(full, url)
    return text, html


def _headers(message_id: str, in_reply_to: str | None, references: list[str]) -> dict:
    h = {"Message-ID": message_id, "Auto-Submitted": "auto-replied" if in_reply_to else "auto-generated",
         "X-Huolitsija-Channel": "email"}
    if in_reply_to:
        h["In-Reply-To"] = in_reply_to
        h["References"] = " ".join(references[-10:])
    return h


async def stored_attachments(attachments) -> list[dict]:
    rows = []
    for name, content_type, data in attachments or []:
        fid = await put_file(data, name, content_type, {"kind": "email_outbound_attachment"})
        rows.append({"filename": name, "content_type": content_type, "size": len(data), "file_id": fid})
    return rows


async def send(thread: dict, title: str, lines: list[str], *, tag: str, subject: str | None = None,
               action: tuple[str, str] | None = None, attachments: list[tuple[str, str, bytes]] | None = None,
               kind: str = "reply", to: str | None = None, idempotency_key: str | None = None,
               reply_to_message: dict | None = None) -> dict:
    lang = thread.get("language") or "fi"
    to = to or thread["customer_email"]
    in_reply_to, refs = await store.last_outbound_id(thread["id"])
    message_id = store.new_message_id()
    subj = store.with_ref(subject or thread.get("subject") or title, thread.get("ref"), reply=bool(in_reply_to))
    if reply_to_message:
        in_reply_to = reply_to_message["message_id"]
        refs = list(dict.fromkeys([*refs, *(reply_to_message.get("references") or []), in_reply_to]))
        original_subject = subject or reply_to_message.get("subject") or thread.get("subject") or title
        normalized = re.sub(r"^((re|vs|sv|aw|fw|fwd)\s*:\s*)+", "", original_subject.strip(), flags=re.I)
        subj = "Re: " + normalized
    html, text = render(title, lines, action[0] if action else None, action[1] if action else None, footer=texts.footer(lang))
    row = await store.log_outbound(thread, message_id=message_id, in_reply_to=in_reply_to, references=refs, to=to,
                                   subject=subj, text=text, tag=tag, kind=kind, provider=conf.provider(),
                                   attachments=await stored_attachments(attachments))
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"retry_payload": {
        "to": to, "subject": subj, "html": html, "text": text, "tag": tag, "company_id": thread.get("company_id"),
        "headers": _headers(message_id, in_reply_to, refs), "idempotency_key": idempotency_key}}})
    rec = await send_email(to, subj, html, text, tag=tag, company_id=thread.get("company_id"),
                           ref={"type": "email_thread", "id": thread["id"], "email_message_id": row["id"]},
                           headers=_headers(message_id, in_reply_to, refs), reply_to=conf.address(),
                           from_addr=conf.sender(), provider=conf.provider(), smtp=conf.smtp(), attachments=attachments,
                           idempotency_key=idempotency_key)
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"status": rec["status"], "outbox_id": rec["id"],
                                                                     "error": rec.get("error")}})
    row.update(status=rec["status"], outbox_id=rec["id"])
    return row


async def thread_mail(thread: dict, mail: dict) -> dict:
    """core.email outbound hook body: thread a mail another part of the app sends to this customer."""
    in_reply_to, refs = await store.last_outbound_id(thread["id"])
    message_id = store.new_message_id()
    subj = store.with_ref(mail["subject"], thread.get("ref"), reply=bool(in_reply_to))
    row = await store.log_outbound(thread, message_id=message_id, in_reply_to=in_reply_to, references=refs, to=mail["to"],
                                   subject=subj, text=mail["text"], tag=mail.get("tag") or "general", kind="system",
                                   provider=conf.provider(),
                                   attachments=await stored_attachments(mail.get("attachments")))
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"status": "sent_via_app", "retry_payload": {
        "to": mail["to"], "subject": subj, "html": mail["html"], "text": mail["text"], "tag": mail.get("tag") or "general",
        "company_id": mail.get("company_id"), "headers": _headers(message_id, in_reply_to, refs)}}})
    return {**mail, "subject": subj, "headers": {**(mail.get("headers") or {}), **_headers(message_id, in_reply_to, refs)},
            "reply_to": conf.address(), "from_addr": conf.sender(), "provider": conf.provider(), "smtp": conf.smtp(),
            "ref": {**(mail.get("ref") or {}), "email_thread_id": thread["id"], "email_message_id": row["id"]}}
