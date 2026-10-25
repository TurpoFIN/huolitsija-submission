"""Requester side of a demo run. The person types the request and, with it, the facts they would answer by e-mail (the
profile). Every mail to the requester really goes out through SMTP2GO to the requester mailbox (mail.tm). The requester
persona (a simulated actor) reads Huolitsija's outbox copy of a mail once SMTP2GO has accepted it, and during the quote
phase answers from the profile only, through Huolitsija's inbound path (dispatch_raw), From the requester mailbox. The
mailbox itself is read separately as proof: each real arrival is logged with its delay. From the offer on, the world
simulator's w:customer acts for the requester."""
from __future__ import annotations

import asyncio
import email
import logging
import random
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from core.db import db
from core.llm import llm_text
from core.util import new_id, now_iso

from . import conf
from .events import emit
from .mail import addr, requester_box

log = logging.getLogger("demo_autoquote.requester")

SYSTEM = """You are the logistics contact of a company that asked Huolitsija (a freight forwarder) for a quote. Reply to
their questions by e-mail, briefly and naturally, like a busy shipper. Answer ONLY from the facts you are given; for
anything not in the facts say you have no preference or will confirm later. Never invent numbers, addresses or dates
that are not in the facts. If the facts say who handles the customs formalities, say so in your reply in your own words
without changing its meaning, even if they did not ask. Plain text, no markdown, no subject line. Sign with your name
and company."""


def _profile_text(profile: dict) -> str:
    labels = {"company": "Company", "contact_name": "Your name", "cargo_description": "Goods", "packages": "Packages",
              "weight_kg": "Total gross weight (kg)", "volume_m3": "Volume (m³)", "dimensions": "Dimensions",
              "stackable": "Stackable", "dangerous_goods": "Dangerous goods", "value_eur": "Goods value (EUR)",
              "ready_date": "Ready for pickup", "incoterm": "Incoterm", "pickup_address": "Pickup address",
              "delivery_address": "Delivery address", "customs": "Customs formalities", "notes": "Other"}
    return "\n".join(f"- {labels.get(k, k)}: {v}" for k, v in profile.items() if v not in (None, "", []))


def _text_of(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


async def answer(run: dict, received: email.message.Message, mailtm_id: str | None, *, outbox_id: str | None = None) -> dict:
    if run.get("real_requester"):
        raise ValueError("A real requester must answer from their own mailbox")
    question = _text_of(received)
    profile = run.get("profile") or {}
    prompt = f"Facts:\n{_profile_text(profile)}\n\nTheir e-mail:\n\"\"\"\n{question[:4000]}\n\"\"\""
    body = await llm_text("demo_requester_sandbox", SYSTEM, prompt, max_tokens=600, ref={"type": "demo_run", "id": run["id"]})
    out = EmailMessage()
    mid = make_msgid(idstring="req", domain=conf.requester_address().split("@")[-1] or "requester.local")
    name = profile.get("contact_name") or "Requester"
    out["Message-ID"] = mid
    out["Date"] = formatdate(localtime=False)
    out["From"] = f"{name} <{conf.requester_address()}>"
    out["To"] = conf.inbound_address()
    subj = received.get("Subject") or ""
    out["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    rid = (received.get("Message-ID") or "").strip()
    if rid:
        out["In-Reply-To"] = rid
        refs = " ".join(x for x in [(received.get("References") or "").strip(), rid] if x)
        from core.email import fit_references
        out["References"] = fit_references(refs)
    out["X-Huolitsija-Demo-Simulated"] = "1"
    out["Auto-Submitted"] = "auto-replied"  # RFC 3834: an automatic responder; Huolitsija's e-mail agent leaves it alone
    out.set_content(body.strip())
    from services.email_channel.dispatch import dispatch_raw
    res = await dispatch_raw(out.as_bytes(), provider_id=f"demo-sim:{new_id()}", provider_authenticated=True,
                             source="demo_requester")
    await db.demo_runs.update_one({"id": run["id"]}, {"$push": {"requester_replies": {
        "at": now_iso(), "message_id": mid, "in_reply_to": rid, "mailtm_id": mailtm_id, "outbox_id": outbox_id,
        "email_message_id": res.get("id")}}})
    return {"message_id": mid, "text": body.strip(), "email_message_id": res.get("id"), "in_reply_to": rid}


OFFER_RX = re.compile(r"\[Q-\d{4}-\d{6}\]")
PERSONA_TAGS = {"email_need_info": "clarify", "email_quote": "offer"}
PERSONA_NOTE = "requester persona read outbox copy (simulated actor)"
WATCH_HOURS = 3


async def _accepted_sends(run: dict) -> list[dict]:
    """Mail Huolitsija sent to the requester in this run that SMTP2GO accepted, with the provider's message id recorded."""
    scope = [{"demo_run_id": run["id"]}] + ([{"ref.id": run["thread_id"]}] if run.get("thread_id") else [])
    return await db.email_outbox.find({"to": conf.requester_address(), "provider": "smtp2go", "status": "sent",
                                       "provider_id": {"$nin": [None, ""]}, "message_ids.0": {"$exists": True},
                                       "$or": scope}, {"_id": 0}).sort("created_at", 1).to_list(100)


def _copy(row: dict) -> EmailMessage:
    """The requester's copy of one accepted send, from Huolitsija's own outbox record (same ids, subject and text)."""
    m = EmailMessage()
    m["Message-ID"] = row["message_ids"][0]
    m["From"] = row.get("from") or conf.sender()
    m["To"] = row["to"]
    m["Subject"] = row.get("subject") or ""
    m.set_content(row.get("text") or "")
    return m


def _seconds_since(iso: str | None) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat((iso or "").replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 0.0


async def watch(run_id: str) -> None:
    """The requester side of a run.

    Persona: once SMTP2GO has accepted a mail to the requester (provider id recorded), the requester persona (a simulated
    actor) reads Huolitsija's outbox copy of it and, during the quote phase, answers the questions from the profile only.
    Proof: the requester mailbox (mail.tm) is read independently; each real arrival is logged with its delay after sending.
    """
    box = requester_box()
    while True:
        run = await db.demo_runs.find_one({"id": run_id}, {"_id": 0})
        if run and run.get("real_requester"):
            return
        if not run or run.get("status") in ("failed", "stopped", "interrupted") or \
                _seconds_since(run.get("created_real") or run.get("created_at")) > WATCH_HOURS * 3600:
            return
        read, arrived = set(run.get("requester_copy_ids") or []), set(run.get("requester_mail_ids") or [])
        try:
            sends = await _accepted_sends(run)
            for row in sends:
                if row["id"] in read:
                    continue
                await db.demo_runs.update_one({"id": run_id}, {"$addToSet": {"requester_copy_ids": row["id"]}})
                kind = PERSONA_TAGS.get(row.get("tag"))
                if kind == "offer" or (kind and OFFER_RX.search(row.get("subject") or "")):
                    await emit(run_id, "requester", "Offer reached the requester", state="done", kind="email_in", simulated=True,
                               data={"emails": [_email_data(row)], "read_from": "outbox copy", "reasoning": PERSONA_NOTE})
                elif kind == "clarify" and run.get("status") == "running":
                    await emit(run_id, "requester", "Questions reached the requester", state="active", kind="email_in",
                               simulated=True, data={"emails": [_email_data(row)], "read_from": "outbox copy",
                                                     "reasoning": PERSONA_NOTE})
                    if conf.requester_auto_answer():
                        await asyncio.sleep(random.Random(f"{run.get('seed')}:req:{row['id']}").uniform(6, 16))
                        msg = _copy(row)
                        sent = await answer(run, msg, None, outbox_id=row["id"])
                        await emit(run_id, "requester", "Requester answered", state="done", kind="email_out",
                                   edge=("requester", "clarify"), simulated=True,
                                   data={"emails": [{"direction": "out", "from": conf.requester_address(),
                                                     "to": conf.inbound_address(), "subject": "Re: " + (msg.get("Subject") or ""),
                                                     "text": sent["text"], "message_id": sent["message_id"],
                                                     "email_message_id": sent["email_message_id"], "simulated": True}],
                                         "reasoning": "The requester persona answers only from the facts typed with the request."})
            if box is not None:
                by_mid = {mid: row for row in sends for mid in row.get("message_ids") or []}
                for m in await box.messages():
                    if m["id"] in arrived or run["ref"] not in (m.get("subject") or ""):
                        continue
                    msg = email.message_from_bytes(await box.source(m["id"]))
                    row = by_mid.get((msg.get("Message-ID") or "").strip())
                    await db.demo_runs.update_one({"id": run_id}, {"$addToSet": {"requester_mail_ids": m["id"]}})
                    arrived.add(m["id"])
                    delay = _seconds_since((row or {}).get("created_at")) if row else None
                    await emit(run_id, "requester", "Delivered to the requester mailbox" +
                               (f" · {delay / 60:.0f} min after sending" if delay is not None else ""), kind="tool",
                               data={"mailbox_proof": {"mailbox": f"{conf.requester_address()} (mail.tm)", "mailtm_id": m["id"],
                                                       "message_id": (msg.get("Message-ID") or "").strip(),
                                                       "subject": msg.get("Subject"), "outbox_id": (row or {}).get("id"),
                                                       "sent_at": (row or {}).get("created_at"), "observed_at": now_iso(),
                                                       "delay_s": round(delay) if delay is not None else None}})
                all_in = sends and len({r["id"] for r in sends}) <= len(arrived)
                if all_in and run.get("status") not in ("running",) and (run.get("offer") or {}).get("outbox_id") in {r["id"] for r in sends}:
                    return
        except Exception as exc:  # noqa: BLE001 - a mailbox or database hiccup is retried on the next poll
            log.warning("requester watch failed: %s", exc)
        await asyncio.sleep(5)


def _email_data(row: dict) -> dict:
    return {"direction": "in", "from": row.get("from") or conf.sender(), "to": row.get("to"), "subject": row.get("subject"),
            "text": (row.get("text") or "")[:6000], "message_id": (row.get("message_ids") or [None])[0],
            "outbox_id": row["id"], "provider": row.get("provider"), "provider_id": row.get("provider_id")}
