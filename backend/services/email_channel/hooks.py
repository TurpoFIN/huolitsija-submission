"""How the rest of the app reaches an e-mail-only customer.

- core.email outbound hook: any mail the app sends to such a customer (booking confirmation, invoice, reminder,
  payment receipt) is threaded into the conversation it belongs to (named thread, the shipment's / quote's thread),
  never into whichever thread is newest; with none it goes out unthreaded; portal links become one-click sign-in links; an issued
  invoice gets the Stripe payment link and its PDF. The portal's verification mail is not sent (the mailbox is proven).
- message.posted: status updates (comms agent) and answers by ops / AI in the portal message thread are mailed.
- approval.decided: owner decisions on email_triage cards; a reviewed quote (quote_threshold) is mailed once sent.
- company.kyc_changed: approved -> the waiting booking is made; in review / rejected -> the customer is told.
- quote.sourcing_updated (s5): the firm price that replaced an estimate is mailed.
"""
from __future__ import annotations

import logging

from core import email as core_email
from core.db import db
from core.events import on
from core.storage import get_file
from core.util import NO_ID, now_iso

from . import flow, outbound, store, texts

log = logging.getLogger("email_channel")


async def _thread_for_mail(user: dict, ref: dict) -> dict | None:
    ref = ref or {}
    t = None
    if ref.get("type") == "email_thread":
        t = await store.thread_by_id(ref.get("id"))
    if not t and ref.get("shipment_id"):
        t = await db.email_threads.find_one({"shipment_id": ref["shipment_id"]}, NO_ID)
    if not t and ref.get("type") == "quote":
        t = await db.email_threads.find_one({"quote_id": ref.get("id")}, NO_ID)
        if t and ref.get("shipment_id") and not t.get("shipment_id"):
            s = await db.shipments.find_one({"id": ref["shipment_id"]}, {"reference": 1, "_id": 0}) or {}
            t = await store.update_thread(t["id"], shipment_id=ref["shipment_id"], shipment_reference=s.get("reference"),
                                          ref=s.get("reference") or t.get("ref"))
    if not t and ref.get("type") == "invoice":
        inv = await db.invoices.find_one({"id": ref.get("id")}, {"shipment_id": 1, "quote_id": 1, "_id": 0}) or {}
        if inv.get("shipment_id"):
            t = await db.email_threads.find_one({"shipment_id": inv["shipment_id"]}, NO_ID)
        if not t and inv.get("quote_id"):  # prepayment invoice / receipt: no shipment until paid, the quote's own thread
            t = await db.email_threads.find_one({"quote_id": inv["quote_id"]}, NO_ID)
    # No fallback to the customer's latest thread: a notice (document, invoice, tracking) about something no thread
    # is linked to is mailed on its own, not as a reply in an unrelated conversation (DG notice ec982ea0 in 0d6ba1fd).
    return t


async def outbound_hook(mail: dict) -> dict | None:
    if (mail.get("headers") or {}).get("X-Huolitsija-Channel"):
        return None
    user = await db.users.find_one({"email": (mail.get("to") or "").lower(), "channel": "email"}, {"_id": 0, "password_hash": 0})
    if not user:
        return None
    tag = mail.get("tag") or ""
    if tag == "verify_email":
        return {**mail, "suppress": "email_channel_customer_verified_by_inbound_mail"}
    thread = await _thread_for_mail(user, mail.get("ref") or {})
    if not thread:
        return None
    text, html = await outbound.rewrite_portal_links(mail["text"], mail["html"], user["id"])
    mail = {**mail, "text": text, "html": html}
    ref = mail.get("ref") or {}
    if tag in ("invoice_issued", "invoice_reminder") and ref.get("type") == "invoice":
        inv = await db.invoices.find_one({"id": ref["id"]}, NO_ID)
        if inv and inv.get("status") in ("open", "overdue"):
            from services.payments import stripe_enabled

            if stripe_enabled():
                url = await outbound.pay_url(user["id"], inv["id"])
                line = texts.pay_line(url, thread.get("language") or "fi")
                mail["text"] = mail["text"] + "\n\n" + line
                mail["html"] = mail["html"].replace("</div>", f"<p style='margin:0 0 12px'><a href='{url}'>{line}</a></p></div>", 1) \
                    if "</div>" in mail["html"] else mail["html"] + f"<p><a href='{url}'>{line}</a></p>"
                await db.invoices.update_one({"id": inv["id"]}, {"$set": {"email_pay_link_at": now_iso()}})
            if inv.get("pdf_file_id"):
                try:
                    data, _n, _c = await get_file(inv["pdf_file_id"])
                    mail["attachments"] = list(mail.get("attachments") or []) + [(f"{inv.get('number')}.pdf", "application/pdf", data)]
                except Exception:  # noqa: BLE001
                    log.exception("invoice pdf %s not attached", inv.get("number"))
    if ref.get("type") == "invoice" and thread.get("shipment_id"):
        await store.update_thread(thread["id"], state={"invoice_issued": "invoiced", "invoice_paid": "paid"}.get(tag, thread.get("state")))
    return await outbound.thread_mail(thread, mail)


if outbound_hook not in core_email.OUTBOUND_HOOKS:
    core_email.OUTBOUND_HOOKS.append(outbound_hook)


@on("message.posted")
async def relay(p: dict):
    """Customer-visible message from ops, AI or the system (status updates) -> the customer's mail thread."""
    m, t = p.get("message") or {}, p.get("thread") or {}
    if m.get("visibility") != "customer" or m.get("status") not in (None, "sent") or (m.get("author") or {}).get("type") == "customer":
        return
    if m.get("email_relayed_at"):
        return
    user = await db.users.find_one({"company_id": t.get("company_id"), "channel": "email", "disabled": {"$ne": True}},
                                   {"_id": 0, "password_hash": 0})
    if not user:
        return
    et = None
    if t.get("email_thread_id"):
        et = await store.thread_by_id(t["email_thread_id"])
    if not et and t.get("shipment_id"):
        et = await db.email_threads.find_one({"shipment_id": t["shipment_id"]}, NO_ID)
    if not et and m.get("kind") != "update":  # a status update never lands in the company's newest unrelated thread
        et = await db.email_threads.find_one({"company_id": t.get("company_id")}, NO_ID, sort=[("updated_at", -1)])
    if not et:
        return
    lang = et.get("language") or "fi"
    is_update = m.get("kind") == "update"
    ref = et.get("shipment_reference") or et.get("ref") or ""
    title = ((f"{ref}: tilannepäivitys" if lang != "en" else f"{ref}: status update") if is_update else
             (f"Vastaus: {t.get('subject') or ref}" if lang != "en" else f"Reply: {t.get('subject') or ref}"))
    body = m.get("body") or ""
    if m.get("body_i18n") and isinstance(m["body_i18n"], dict):
        body = m["body_i18n"].get(lang) or body
    await outbound.send(et, title, [x for x in body.split("\n") if x.strip()] or [body], tag="email_update" if is_update else "email_answer",
                        kind="update" if is_update else "answer", subject=title if is_update else None)
    await db.messages.update_one({"id": m.get("id")}, {"$set": {"email_relayed_at": now_iso(), "email_thread_id": et["id"]}})


@on("approval.decided")
async def approval_decided(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") == "email_triage":
        row = await db.email_messages.find_one({"id": a.get("ref_id")}, {"_id": 0})
        if not row:
            return
        data = a.get("decision_data") or {}
        if a.get("status") in ("approved",) and data.get("intent"):
            await flow.process(row, forced_intent=data["intent"], actor=p.get("actor"))
        elif a.get("status") in ("approved", "rejected"):
            await db.email_messages.update_one({"id": row["id"]}, {"$set": {"status": "handled_by_owner", "owner_decision": a.get("status"),
                                                                            "decided_at": now_iso()}})
        if row.get("thread_id") and not await db.email_messages.count_documents({"thread_id": row["thread_id"], "status": "needs_owner"}):
            await store.update_thread(row["thread_id"], needs_owner=False)
        return
    if a.get("kind") == "quote_threshold":
        q = await db.quotes.find_one({"id": a.get("ref_id")}, NO_ID)
        if not q or not q.get("email_thread_id"):
            return
        t = await store.thread_by_id(q["email_thread_id"])
        if t and t.get("state") == "quote_review":
            await flow.send_quote_status(t, q)


@on("quote.sourcing_updated")
async def sourcing_updated(p: dict):
    q = p.get("quote") or {}
    q = await db.quotes.find_one({"id": q.get("id")}, NO_ID) if q.get("id") else None
    if not q or not q.get("email_thread_id"):
        return
    t = await store.thread_by_id(q["email_thread_id"])
    if t and not t.get("shipment_id"):
        await flow.send_quote_status(t, q)


@on("company.kyc_changed")
async def kyc_changed(p: dict):
    c = p.get("company") or {}
    company = await db.companies.find_one({"id": c.get("id")}, NO_ID) if c.get("id") else None
    if not company:
        return
    async for t in db.email_threads.find({"company_id": company["id"], "state": "booking_pending_kyc"}, NO_ID):
        status = company.get("kyc_status")
        if status == "approved" and company.get("sanctions_status") == "clear":
            await flow.do_booking(t)
        elif status in ("in_review", "rejected") and (t.get("pending") or {}).get("kyc_state_mailed") != status:
            title, lines = texts.kyc_state(status, t.get("language") or "fi")
            await outbound.send(t, title, lines, tag=f"email_kyc_{status}")
            await store.update_thread(t["id"], **{"pending.kyc_state_mailed": status})
