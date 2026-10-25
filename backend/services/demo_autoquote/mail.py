"""Mail of the demo. Every message leaves through core.email.send_email (the app's provider and email_outbox, i.e. the
sent folder) and every external recipient is reserved against the run's cap first, in one atomic update. Replies come
back through Huolitsija's own inbound path (mail.tm poller -> email_channel.dispatch.dispatch_raw)."""
from __future__ import annotations

import logging
from email.utils import make_msgid, parseaddr

import httpx

from core.db import db
from core.email import render, send_email

from . import conf

log = logging.getLogger("demo_autoquote.mail")
MAILTM_API = "https://api.mail.tm"


def addr(value: str | None) -> str:
    return parseaddr(value or "")[1].lower()


def new_message_id() -> str:
    return make_msgid(idstring="demo", domain="huolitsija-demo.local")


def smtp2go_message_id(provider_id: str | None) -> str | None:
    """SMTP2GO replaces our Message-ID with its own; replies reference this one."""
    return f"<E{provider_id}@message-id.smtpcorp.com>" if provider_id else None


async def reserve_recipient(run_id: str, email: str) -> bool:
    """True when `email` may receive mail in this run: already reserved, or the run is below the cap. Atomic."""
    cap = conf.max_recipients()
    email = (email or "").strip().lower()
    if not email or cap <= 0:
        return False
    res = await db.demo_runs.update_one(
        {"id": run_id, "$or": [{"recipients": email}, {f"recipients.{cap - 1}": {"$exists": False}}]},
        {"$addToSet": {"recipients": email}})
    return res.matched_count == 1


async def send(run_id: str, to: str, subject: str, lines: list[str], *, kind: str, external: bool,
               headers: dict | None = None, title: str | None = None, footer: str | None = None,
               ref: dict | None = None, attachments: list | None = None) -> dict:
    """Send one demo mail. external=True for anyone outside the demo's own mailboxes (cap + optional sandbox).
    Returns the email_outbox record plus `to_original`, `message_ids` (every id a reply may reference)."""
    to = addr(to)
    to_original = to
    if external:
        if not await reserve_recipient(run_id, to):
            return {"status": "held", "to": to, "to_original": to, "message_ids": []}
        box = conf.partner_sandbox()
        if box:
            to = box[0]
    mid = new_message_id()
    hdrs = {"Message-ID": mid, "X-Huolitsija-Demo-Run": run_id, **(headers or {})}
    if to != to_original:
        hdrs["X-Huolitsija-Original-To"] = to_original
    html, text = render(title or subject, lines, footer=footer)
    rec = await send_email(to, subject, html, text, tag=f"demo_{kind}", provider=conf.provider(), from_addr=conf.sender(),
                           reply_to=conf.inbound_address() or None, headers=hdrs,
                           ref={"type": "demo_run", "id": run_id, "kind": kind, **(ref or {})}, attachments=attachments)
    ids = list(dict.fromkeys([mid, *(rec.get("message_ids") or [])] +
        ([smtp2go_message_id(rec.get("provider_id"))] if rec.get("provider") == "smtp2go" and rec.get("provider_id") else [])))
    await db.email_outbox.update_one({"id": rec["id"]}, {"$addToSet": {"message_ids": {"$each": ids}}, "$set": {"to_original": to_original,
                                                                  "demo_run_id": run_id}})
    return {**rec, "to_original": to_original, "message_ids": ids, "text": text}


class MailTm:
    """Read-only client of one mail.tm mailbox (the requester sandbox, the partner rehearsal sandbox)."""

    def __init__(self, address: str, password: str):
        self.address, self.password, self._token = address, password, None

    async def _auth(self, client: httpx.AsyncClient) -> dict:
        if not self._token:
            r = await client.post(MAILTM_API + "/token", json={"address": self.address, "password": self.password})
            r.raise_for_status()
            self._token = r.json()["token"]
        return {"Authorization": "Bearer " + self._token}

    async def messages(self, page: int = 1) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(MAILTM_API + "/messages", headers=await self._auth(client), params={"page": page})
            if r.status_code == 401:
                self._token = None
                r = await client.get(MAILTM_API + "/messages", headers=await self._auth(client), params={"page": page})
            r.raise_for_status()
            return r.json().get("hydra:member", [])

    async def source(self, message_id: str) -> bytes:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{MAILTM_API}/sources/{message_id}", headers=await self._auth(client))
            r.raise_for_status()
            return (r.json().get("data") or "").encode("utf-8")

    async def mark_seen(self, message_id: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.patch(f"{MAILTM_API}/messages/{message_id}", json={"seen": True},
                               headers={**await self._auth(client), "Content-Type": "application/merge-patch+json"})


def requester_box() -> MailTm | None:
    if not (conf.requester_address() and conf.requester_password()):
        return None
    return MailTm(conf.requester_address(), conf.requester_password())
