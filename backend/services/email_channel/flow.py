"""What the e-mail channel does with one inbound customer mail.

    received -> (thread found or created) -> intake read (model, or a person when the model is unavailable)
             -> quote request  : customer account (first time) -> quote -> quote mail (or "being reviewed" / "need info")
             -> book           : option + parties collected -> KYC link (first booking) -> booking -> confirmation
             -> documents      : attachments filed on the shipment (or kept for the booking)
             -> question, ...  : posted to the shipment/company message thread (comms agent answers, relayed by mail)
             -> unknown, ambiguous, injection, different sender, failed checks : owner queue (approvals kind email_triage)

Every decision is written on the message row (intent, parse, decision) so ops sees why the channel did what it did.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from fastapi import HTTPException

from core.approvals import request_approval
from core.audit import audit
from core.db import db
from core.llm import LLMUnavailable
from core.notify import bi, notify_role, same
from core.settings import get_settings
from core.storage import get_file
from core.util import NO_ID, now_iso

from . import conf, customers, intake, mime, outbound, store, texts

log = logging.getLogger("email_channel")

OWNER_INTENTS = ("change_request", "cancel", "complaint", "payment", "other")
CONFIDENCE_FLOOR = 0.55
SYSTEM_ACTOR = {"id": "email-channel", "name": "E-mail channel", "role": "system", "type": "system"}


class Stop(Exception):
    """The message is handled (or handed to the owner); nothing more to do."""


# ------------------------------------------------------------------------------------------------ helpers

async def _attachments(row: dict) -> list[dict]:
    out = []
    for a in row.get("attachments") or []:
        if not a.get("file_id"):
            continue
        try:
            data, _name, _ct = await get_file(a["file_id"])
        except Exception:  # noqa: BLE001
            continue
        out.append({**a, "data": data})
    return out


async def _set_row(row: dict, **fields):
    fields["updated_at"] = now_iso()
    await db.email_messages.update_one({"id": row["id"]}, {"$set": fields})
    row.update(fields)


async def _quote(thread: dict) -> dict | None:
    return await db.quotes.find_one({"id": thread["quote_id"]}, NO_ID) if thread.get("quote_id") else None


async def context_line(thread: dict | None) -> str:
    if not thread or not thread.get("quote_id"):
        return "New conversation; no quote has been sent to this sender in this thread."
    q = await _quote(thread) or {}
    opts = "; ".join(f"option {i}: {o.get('mode')} {o.get('carrier_name') or ''} {o.get('total_cents', 0) / 100:.2f} EUR"
                     for i, o in enumerate(q.get("options") or [], 1))
    line = f"We sent quote {q.get('number')} ({opts or 'no options'}) and asked the customer to reply BOOK <option number>."
    if thread.get("shipment_reference"):
        s = await db.shipments.find_one({"id": thread["shipment_id"]}, {"status": 1, "_id": 0}) or {}
        line += f" It is booked as shipment {thread['shipment_reference']} (status {s.get('status')})."
    elif thread.get("state") in ("booking_details", "booking_pending_kyc"):
        line += f" A booking is in progress (state {thread['state']}); we asked for shipper/consignee details or verification."
    return line


def _fake_request(base: str):
    from starlette.requests import Request

    u = urlparse(base or "http://localhost")
    return Request({"type": "http", "method": "POST", "path": "/api/bookings", "query_string": b"", "scheme": u.scheme or "http",
                    "server": (u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)),
                    "headers": [(b"origin", base.encode())] if base else []})


async def to_owner(thread: dict | None, row: dict, reason: str, *, ack: bool = True, summary: str | None = None) -> dict:
    """Owner queue card for a mail the channel must not act on by itself."""
    a = await request_approval(
        "email_triage", "email_message", row["id"],
        f"Sähköposti: {row.get('subject') or '(ei aihetta)'}"[:160],
        summary=(summary or row.get("new_text") or "")[:600], reason=reason, company_id=(thread or {}).get("company_id"),
        requested_by=None,
        data={"thread_id": (thread or {}).get("id"), "from": row.get("from_email"), "intent": row.get("intent"),
              "reason": reason, "subject": row.get("subject")},
        link=f"/ops/email?thread={(thread or {}).get('id') or ''}&message={row['id']}")
    await db.approvals.update_one({"id": a["id"]}, {"$set": {
        "title_fi": f"Sähköposti: {row.get('subject') or '(ei aihetta)'}"[:160],
        "title_en": f"E-mail: {row.get('subject') or '(no subject)'}"[:160]}})
    await _set_row(row, status="needs_owner", decision=reason, approval_id=a["id"])
    if thread:
        await store.update_thread(thread["id"], needs_owner=True)
    await notify_role(["ops_agent"], bi("Sähköposti odottaa päätöstä", "E-mail waiting for a decision"),
                      same(f"{row.get('from_email')}: {row.get('subject') or ''}"[:200]),
                      link=f"/ops/email?thread={(thread or {}).get('id') or ''}", kind="message")
    if ack and thread and not (thread.get("pending") or {}).get("owner_ack_sent"):
        title, lines = texts.received_for_owner(thread.get("language") or "fi")
        await outbound.send(thread, title, lines, tag="email_ack")
        await store.update_thread(thread["id"], **{"pending.owner_ack_sent": now_iso()})
    raise Stop(reason)


# ------------------------------------------------------------------------------------------------ entry point

async def process(row: dict, *, forced_intent: str | None = None, actor: dict | None = None) -> dict:
    """Handle one inbound row (status received/retry or a reprocess by ops). Returns the updated row."""
    try:
        await _process(row, forced_intent=forced_intent, actor=actor)
        if row.get("status") in ("received", "processing", "retry") or (forced_intent and row.get("status") == "needs_owner"):
            await _set_row(row, status="processed")
    except Stop:
        pass
    return await db.email_messages.find_one({"id": row["id"]}, {"_id": 0})


async def _process(row: dict, *, forced_intent: str | None, actor: dict | None):
    from services.demo_autoquote.public_requester import handle_reply
    if await handle_reply(row):
        row["status"] = "demo_waiting"
        raise Stop("demo requester reply handled")
    settings = await get_settings()
    lang_guess = intake.guess_language(row.get("new_text") or row.get("text") or "")
    # Re-evaluate header facts, including for rows ingested before subject/sender heuristics were removed.
    auto = mime.is_automatic(row)
    if auto != row.get("automatic"):
        await _set_row(row, automatic=auto)
    if (row.get("from_email") or "") == conf.address() or (row.get("from_email") or "").endswith("@" + conf.domain()) and \
            (row.get("from_email") or "").startswith("em-"):
        await _set_row(row, status="ignored", decision="own_mail")
        raise Stop("own mail")
    if auto and not forced_intent:
        await _set_row(row, status="ignored", decision=f"automatic:{auto}")
        raise Stop("automatic mail")

    thread, how = (await store.thread_by_id(row["thread_id"]), "known") if row.get("thread_id") else await store.find_thread_for_inbound(row)
    if thread and thread.get("customer_email") != row.get("from_email"):
        same_company = False
        if thread.get("company_id"):
            u = await customers.user_for_email(row.get("from_email"))
            same_company = bool(u and u.get("company_id") == thread["company_id"])
        if not same_company and not forced_intent:
            await _set_row(row, thread_id=thread["id"], match=how)
            await to_owner(thread, row, "sender_differs", ack=False,
                           summary=f"{row.get('from_email')} wrote in the thread of {thread.get('customer_email')}")
    if not thread:
        thread = await store.create_thread(row, language=lang_guess)
    await _set_row(row, thread_id=thread["id"], match=how)
    await db.email_threads.update_one({"id": thread["id"]}, {"$inc": {"counts.in": 1}, "$set": {"last_message_at": now_iso()}})

    if not settings.get("email_channel_enabled", True) and not forced_intent:
        await to_owner(thread, row, "channel_off", ack=False)
    from core.autonomy import autopilot_scope  # s6 r94: a test tenant's Autopilot grant counts here
    # a new thread has no company yet (handle_quote_request resolves it): a known customer's first mail uses its company
    gate_company = thread.get("company_id") or ((await customers.user_for_email(row.get("from_email") or "")) or {}).get("company_id")
    granted = await autopilot_scope(gate_company, settings, agent="email_channel") == "test_tenant"
    if not (settings.get("autopilot_enabled", False) or granted) and not forced_intent:
        await to_owner(thread, row, "autopilot_off", ack=False)
    if settings.get("autopilot_halted", False) and not forced_intent:
        await to_owner(thread, row, "autopilot_halted", ack=False)
    if conf.require_auth() and mime.auth_passed(row) is not True and not forced_intent:
        await to_owner(thread, row, "sender_authentication_failed", ack=False)

    markers = intake.injection_markers(row.get("subject") or "", row.get("new_text") or row.get("text") or "")
    atts = await _attachments(row)
    parse = None
    if forced_intent:
        parse = {**(row.get("parse") or intake.clean({})), "intent": forced_intent, "confidence": 1.0}
    else:
        try:
            parse = await intake_or_person(row, context=await context_line(thread), attachments=atts,
                                           company_id=thread.get("company_id"))
        except Exception as exc:  # noqa: BLE001 - a model or network failure: retry later, then the owner
            raise RetryLater(f"intake failed: {exc}") from exc
        if parse == MODEL_UNAVAILABLE:
            await _set_row(row, intent=None, parse={"model": "unavailable"})
            run = await db.demo_runs.find_one({"thread_id": thread["id"]}, {"_id": 0, "id": 1})
            if run:
                from services.demo_autoquote.events import emit
                await emit(run["id"], "booking" if thread.get("quote_id") else "clarify",
                    "Waiting for email interpretation", state="waiting", kind="tool",
                    data={"reasoning": "LLM unavailable; a person must review this email."})
            await to_owner(thread, row, "model_unavailable", ack=False)
    lang = parse.get("language") or thread.get("language") or lang_guess
    if lang != thread.get("language"):
        thread = await store.update_thread(thread["id"], language=lang)
    await _set_row(row, intent=parse["intent"], parse=parse, injection_markers=markers)
    if (markers or parse.get("injection_suspected")) and not forced_intent:
        await to_owner(thread, row, "possible_prompt_injection", ack=False,
                       summary=f"Markers: {', '.join(markers) or 'model flag'}. {parse.get('summary') or ''}")
    if parse["confidence"] < CONFIDENCE_FLOOR and not forced_intent:
        await to_owner(thread, row, "ambiguous", ack=False, summary=parse.get("summary"))

    intent = parse["intent"]
    if intent == "book" and parse.get("booking_conditions") and not forced_intent:
        # "Varaamme vaihtoehdon 2, jos nouto onnistuu maanantaina": a conditional booking is a person's decision.
        await to_owner(thread, row, "conditional_booking", summary=f"{parse['booking_conditions']} | {parse.get('summary') or ''}")
    if intent in ("book", "booking_details") and how == "subject" and not forced_intent and not thread.get("shipment_id"):
        # A booking changes money and liability: only a reply that carries our Message-ID may book on its own.
        await to_owner(thread, row, "booking_without_thread_headers", summary=parse.get("summary"))
    if intent == "quote_request" or (thread.get("state") == "awaiting_info" and intent in ("other", "question", "booking_details")):
        if thread.get("quote_id") and intent == "quote_request" and thread.get("state") not in ("awaiting_info",):
            thread = await store.create_thread(row, language=lang)  # a new request in an old thread: its own thread
            await _set_row(row, thread_id=thread["id"])
        await handle_quote_request(thread, row, parse, atts)
    elif intent in ("book", "booking_details"):
        await handle_book(thread, row, parse, atts)
    elif intent == "documents" or (atts and thread.get("shipment_id") and intent in ("other", "question", "thanks")):
        await handle_documents(thread, row, atts)
    elif intent in ("thanks", "unsubscribe", "auto_reply"):
        if intent == "unsubscribe":
            await store.update_thread(thread["id"], marketing_opt_out=True)
        await _set_row(row, decision=f"no_reply:{intent}")
    elif intent == "question" and thread.get("company_id"):
        await post_to_messages(thread, row, settings)
    else:
        await to_owner(thread, row, f"intent:{intent}", summary=parse.get("summary"))


class RetryLater(Exception):
    pass


MODEL_UNAVAILABLE = "model_unavailable"


async def intake_or_person(row: dict, *, context: str, attachments: list[dict], company_id: str | None, read=None):
    """The model's reading of the mail, or MODEL_UNAVAILABLE. No rule reads a mail in the model's place (no "BOOK 2"
    pattern): without the model the mail waits for a person, so nothing is booked and no terms are accepted by itself.
    `read` replaces intake.read_mail (same signature): the ops dry run passes one whose model is unavailable."""
    try:
        result = await (read or intake.read_mail)(row, context=context, attachments=attachments, company_id=company_id)
        if not isinstance(result, dict) or result.get("intent") not in intake.INTENTS:
            return MODEL_UNAVAILABLE
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            return MODEL_UNAVAILABLE
        return result
    except Exception as exc:  # A provider error, unreadable JSON or invalid form always waits for a person.
        log.warning("Intake reading unavailable (%s)", type(exc).__name__)
        return MODEL_UNAVAILABLE


# ------------------------------------------------------------------------------------------------ quote

async def _price(request: dict, *, user: dict, company: dict, ai: dict | None, lang: str) -> dict:
    try:
        from services.global_quote import quote_any  # s5 any-city quote, when merged
    except ImportError:
        quote_any = None
    if quote_any:
        return await quote_any(request, company=company, user=user, source="email", send=True, locale=lang, ai=ai)
    from routes.quotes import create_quote

    return await create_quote(request, user=user, company=company, source="email", send=True, ai=ai)


def _request_body(req: dict, parsed: dict) -> dict:
    body = {k: req.get(k) for k in ("mode", "origin", "destination", "ready_date", "incoterm", "incoterm_place", "cargo",
                                    "insurance", "customs_clearance") if req.get(k) is not None}
    # s5 geocodes free text: a place the local lookup could not settle goes on as the customer's words.
    for u in parsed.get("unresolved") or []:
        if not body.get(u["field"]) and u.get("query"):
            body[u["field"]] = u["query"]
    body["notes"] = "E-mail request"
    return body


async def handle_quote_request(thread: dict, row: dict, parse: dict, atts: list[dict]):
    from services import pricing, quoting_agent

    lang = thread.get("language") or "fi"
    user, company, created = await customers.ensure_customer(row["from_email"], row.get("from_name"), parse, lang)
    thread = await store.update_thread(thread["id"], company_id=company["id"], user_id=user["id"],
                                       customer_name=company.get("name"))
    pending = thread.get("pending") or {}
    text = parse.get("quote_request_text") or row.get("new_text") or row.get("text") or ""
    if thread.get("state") == "awaiting_info" and pending.get("request_text"):
        text = pending["request_text"] + "\n" + (row.get("new_text") or text)
    if atts:
        await store.update_thread(thread["id"], **{"pending.attachment_rows": list({*pending.get("attachment_rows", []), row["id"]})})
    try:
        parsed = await quoting_agent.parse_request(text, locale=lang, user=user)
    except LLMUnavailable:
        await to_owner(thread, row, "model_unavailable")
    except quoting_agent.ParseFailed as exc:
        raise RetryLater(str(exc)) from exc
    await _set_row(row, quote_parse={k: parsed.get(k) for k in ("missing", "assumptions", "unresolved", "summary", "confidence")})
    req = parsed.get("request") or {}
    body = _request_body(req, parsed)
    blocking = [k for k in texts.BLOCKING if k in (parsed.get("missing") or []) and not body.get(k)]
    if "weight_kg" in blocking and ((req.get("cargo") or {}).get("containers")):
        blocking.remove("weight_kg")
    if blocking:
        title, lines = texts.need_info(blocking, lang, (parsed.get("summary_i18n") or {}).get(lang) or parsed.get("summary"))
        await store.update_thread(thread["id"], state="awaiting_info", **{"pending.request_text": text[:6000]})
        await outbound.send(thread, title, lines, tag="email_need_info")
        await _set_row(row, decision="asked_for_missing:" + ",".join(blocking))
        return
    try:
        q = await _price(body, user=user, company=company, ai=parsed.get("ai"), lang=lang)
    except (HTTPException, pricing.QuoteInputError) as exc:
        reason = getattr(exc, "detail", None) or str(exc)
        await _set_row(row, quote_error=reason)
        title, lines = texts.cannot_quote(reason, lang)
        await outbound.send(thread, title, lines, tag="email_cannot_quote")
        await store.update_thread(thread["id"], **{"pending.owner_ack_sent": now_iso()})
        await to_owner(thread, row, "quote_failed", ack=False, summary=reason)
    thread = await store.update_thread(thread["id"], quote_id=q["id"], quote_number=q.get("number"), ref=q.get("number"),
                                       **{"pending.request_text": None})
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"email_thread_id": thread["id"]}})
    await audit(user, "quote_requested_by_email", "quote", q["id"], {"message_id": row.get("message_id")},
                company_id=company["id"])
    await _set_row(row, decision=f"quote:{q.get('number')}:{q.get('status')}")
    await send_quote_status(thread, q)


async def send_quote_status(thread: dict, q: dict, *, human_actor=None):
    from services.agents import quote_agent
    if human_actor is not None:
        quote_agent.authorize_human(q, human_actor)
        if quote_agent.human_authorized(q):
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"send_authorization": q["send_authorization"]}})
    if not await quote_agent.outbound_allowed(q):
        await store.update_thread(thread["id"], state="quote_review")
        return
    lang = thread.get("language") or "fi"
    if q.get("status") == "sent" and q.get("options"):
        base = await outbound.base_url()
        title, lines = texts.quote_mail(q, lang, base)
        atts = []
        try:
            from services.quote_pdf import render_quote_pdf

            company = await db.companies.find_one({"id": q.get("company_id")}, NO_ID)
            atts.append((f"{q.get('number')}.pdf", "application/pdf", await render_quote_pdf(q, company, lang=lang)))
        except Exception:  # noqa: BLE001 - the mail text carries the full offer; the PDF is a convenience
            log.exception("quote pdf for %s failed", q.get("number"))
        await store.update_thread(thread["id"], state="quoted")
        await outbound.send(thread, title, lines, tag="email_quote", subject=title, attachments=atts)
    elif q.get("status") == "pending_approval":
        title, lines = texts.quote_review(q, lang)
        await store.update_thread(thread["id"], state="quote_review")
        await outbound.send(thread, title, lines, tag="email_quote_review", subject=title)
    else:
        reason = "; ".join((q.get("warnings") or [])[:2]) or f"quote status {q.get('status')}"
        title, lines = texts.cannot_quote(reason, lang)
        await store.update_thread(thread["id"], state="owner")
        await outbound.send(thread, title, lines, tag="email_cannot_quote")


# ------------------------------------------------------------------------------------------------ booking

def _merge_party(old: dict | None, new: dict | None) -> dict:
    out = dict(old or {})
    for k, v in (new or {}).items():
        if v:
            out[k] = v
    return out


def _booking_missing(pending: dict, q: dict) -> list[str]:
    miss = []
    if not pending.get("option_number"):
        miss.append("option")
    for role in ("shipper", "consignee"):
        p = pending.get(role) or {}
        if not (p.get("name") and p.get("street") and p.get("city") and p.get("country")):
            miss.append(role)
    if (q.get("request") or {}).get("incoterm") and not ((q.get("request") or {}).get("incoterm_place") or pending.get("incoterm_place")):
        miss.append("incoterm_place")
    return miss


async def handle_book(thread: dict, row: dict, parse: dict, atts: list[dict]):
    lang = thread.get("language") or "fi"
    q = await _quote(thread)
    if not q:
        await to_owner(thread, row, "booking_without_quote", summary=parse.get("summary"))
    if q.get("status") == "booked" or thread.get("shipment_id"):
        if atts:
            return await handle_documents(thread, row, atts)
        await _set_row(row, decision="already_booked")
        return
    pending = dict((thread.get("pending") or {}).get("booking") or {})
    opts = q.get("options") or []
    n = parse.get("option_number") or pending.get("option_number") or (1 if len(opts) == 1 else None)
    if n and not (1 <= n <= len(opts)):
        n = None
    if n:
        opt = opts[n - 1]
        if opt.get("firmness") and opt["firmness"] != "firm" or opt.get("bookable") is False:
            title, lines = texts.booking_failed(
                "Valitsemasi vaihtoehto on hinta-arvio; sitova hinta tulee tähän ketjuun." if lang != "en"
                else "The option you chose is an estimate; the firm price will follow in this thread.", lang)
            await outbound.send(thread, title, lines, tag="email_booking_estimate")
            await _set_row(row, decision="estimate_not_bookable")
            return
        pending["option_number"] = n
    parties = parse.get("parties") or {}
    for role in ("shipper", "consignee"):
        pending[role] = _merge_party(pending.get(role), parties.get(role))
    for k in ("pickup_date", "incoterm_place", "customer_reference"):
        if parse.get(k):
            pending[k] = parse[k]
    if parse.get("accepts_terms"):  # the model's reading that the sender explicitly accepts, not the intent alone
        pending["terms_accepted"] = {"message_id": row.get("message_id"), "at": now_iso(), "row_id": row["id"]}
    if atts:
        pending["attachment_rows"] = list({*((thread.get("pending") or {}).get("attachment_rows") or []), row["id"]})
    await store.update_thread(thread["id"], **{"pending.booking": pending},
                              **({"pending.attachment_rows": pending["attachment_rows"]} if atts else {}))
    missing = _booking_missing(pending, q)
    if not pending.get("terms_accepted") and "option" not in missing:
        missing.append("option")
    if missing:
        title, lines = texts.booking_need(missing, lang)
        await store.update_thread(thread["id"], state="booking_details")
        await outbound.send(thread, title, lines, tag="email_booking_details")
        await _set_row(row, decision="asked_for_booking_details:" + ",".join(missing))
        return
    company = await db.companies.find_one({"id": thread["company_id"]}, NO_ID)
    await _prefill_company(company, row.get("parse") or {})
    company = await db.companies.find_one({"id": thread["company_id"]}, NO_ID)
    if company.get("kyc_status") == "approved" and company.get("sanctions_status") == "clear":
        await do_booking(await store.thread_by_id(thread["id"]), row)
        return
    await store.update_thread(thread["id"], state="booking_pending_kyc")
    await send_kyc_link(await store.thread_by_id(thread["id"]))
    await _set_row(row, decision="kyc_link_sent")


async def _prefill_company(company: dict, parse: dict):
    """Business ID / VAT ID / address the customer wrote in a mail pre-fill the KYC form (still checked by KYC)."""
    from core.refs import ytunnus_valid

    s = parse.get("sender") or {}
    upd = {}
    bid = (s.get("business_id") or (company.get("email_intake") or {}).get("business_id_stated") or "").strip()
    if bid and not company.get("business_id") and ytunnus_valid(bid):
        upd["business_id"] = bid
    if s.get("company_name") and company.get("channel") == "email" and company.get("kyc_status") == "not_started":
        upd["name"] = s["company_name"]
    addr = {k: v for k, v in (s.get("address") or {}).items() if v}
    if addr.get("street") and not (company.get("address") or {}).get("street"):
        upd["address"] = addr
    if upd:
        await db.companies.update_one({"id": company["id"]}, {"$set": upd})


async def send_kyc_link(thread: dict, *, again: bool = False):
    pending = thread.get("pending") or {}
    if pending.get("kyc_link_sent_at") and not again:
        return
    lang = thread.get("language") or "fi"
    url = await outbound.link_url(thread["user_id"], "/portal/company?from=email")
    title, lines, label = texts.kyc_link(url, lang, conf.link_days())
    await outbound.send(thread, title, lines, tag="email_kyc_link", action=(url, label))
    await store.update_thread(thread["id"], **{"pending.kyc_link_sent_at": now_iso()})


def _party_in(p: dict) -> dict:
    return {"name": p.get("name"), "eori": p.get("eori") or None,
            "address": {"street": p.get("street"), "postal_code": p.get("postal_code"), "city": p.get("city"),
                        "country": p.get("country")},
            "contact": {k: v for k, v in {"name": p.get("contact_name"), "phone": p.get("contact_phone")}.items() if v} or None}


async def do_booking(thread: dict, row: dict | None = None) -> dict | None:
    """Book the pending option of the thread's quote as the customer (their acceptance is the reply that said BOOK)."""
    from routes.quotes import BookingIn, _create_booking

    settings = await get_settings()
    lang = thread.get("language") or "fi"
    pending = (thread.get("pending") or {}).get("booking") or {}
    q = await _quote(thread)
    if not q or not pending.get("option_number") or thread.get("shipment_id"):
        return None
    from services.agents.quote_agent import outbound_allowed
    if not await outbound_allowed(q, operation="book_quote") or not settings.get("email_channel_enabled", True):
        return None
    opt = (q.get("options") or [])[pending["option_number"] - 1]
    user = await db.users.find_one({"id": thread["user_id"]}, {"_id": 0, "password_hash": 0})
    user = customers.with_permissions(user)
    body = BookingIn(quote_id=q["id"], option_id=opt["id"], shipper=_party_in(pending["shipper"]),
                     consignee=_party_in(pending["consignee"]),
                     pickup={"date": pending.get("pickup_date")} if pending.get("pickup_date") else {},
                     incoterm_place=pending.get("incoterm_place"), customer_reference=pending.get("customer_reference"),
                     accept_terms=bool(pending.get("terms_accepted")),
                     special_instructions=None)
    try:
        res = await _create_booking(body, _fake_request(await outbound.base_url()), user)
    except HTTPException as exc:
        title, lines = texts.booking_failed(str(exc.detail), lang)
        await store.update_thread(thread["id"], state="booking_details", **{"pending.booking_error": str(exc.detail)})
        await outbound.send(thread, title, lines, tag="email_booking_failed")
        if row:
            await _set_row(row, decision=f"booking_failed:{exc.detail}")
        return None
    if res.get("prepayment"):  # s2: the prepayment invoice (with its Stripe link) goes to the customer; booked when paid
        await store.update_thread(thread["id"], state="awaiting_prepayment", needs_owner=False,
                                  **{"pending.prepayment_invoice_id": res["prepayment"]["invoice_id"]})
        if row:
            await _set_row(row, decision=f"prepayment_invoice:{res['prepayment']['invoice_number']}")
        return res
    thread = await store.update_thread(thread["id"], shipment_id=res["shipment_id"], shipment_reference=res["shipment_reference"],
                                       ref=res["shipment_reference"], state="booked", needs_owner=False)
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"booking.terms_accepted_by_email": pending.get("terms_accepted")}})
    await db.shipments.update_one({"id": res["shipment_id"]}, {"$set": {"email_thread_id": thread["id"], "channel": "email"}})
    if row:
        await _set_row(row, decision=f"booked:{res['shipment_reference']}")
    # documents that came before the booking go onto the shipment now
    rows = (thread.get("pending") or {}).get("attachment_rows") or []
    for r in await db.email_messages.find({"id": {"$in": rows}}, {"_id": 0}).to_list(20):
        await file_documents(thread, r, await _attachments(r), notify=False)
    return res


# ------------------------------------------------------------------------------------------------ documents

async def file_documents(thread: dict, row: dict, atts: list[dict], *, notify: bool = True) -> list[str]:
    from services.documents import create_document

    user = await db.users.find_one({"id": thread.get("user_id")}, {"_id": 0, "password_hash": 0}) if thread.get("user_id") else None
    names, stored = [], list(row.get("attachments") or [])
    for i, a in enumerate(atts):
        if a.get("document_id"):
            continue
        try:
            doc = await create_document(company_id=thread["company_id"], shipment_id=thread["shipment_id"], doc_type=None,
                                        filename=a.get("filename") or "attachment", data=a["data"], actor=user, source="email",
                                        extra={"email_message_id": row["id"]})
        except HTTPException as exc:
            log.info("attachment %s not filed: %s", a.get("filename"), exc.detail)
            continue
        names.append(a.get("filename") or doc.get("filename"))
        for s in stored:
            if s.get("file_id") == a.get("file_id"):
                s["document_id"] = doc["id"]
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"attachments": stored}})
    if notify and names:
        title, lines = texts.docs_received(names, thread.get("shipment_reference"), thread.get("language") or "fi")
        await outbound.send(thread, title, lines, tag="email_docs_received")
    return names


async def handle_documents(thread: dict, row: dict, atts: list[dict]):
    if not atts:
        await to_owner(thread, row, "documents_without_attachments")
    if thread.get("shipment_id"):
        names = await file_documents(thread, row, atts)
        await _set_row(row, decision=f"documents_filed:{len(names)}")
        if not names:
            await to_owner(thread, row, "attachments_not_accepted")
        return
    if thread.get("quote_id"):
        rows = list({*((thread.get("pending") or {}).get("attachment_rows") or []), row["id"]})
        await store.update_thread(thread["id"], **{"pending.attachment_rows": rows})
        title, lines = texts.docs_received([a.get("filename") for a in atts], None, thread.get("language") or "fi", later=True)
        await outbound.send(thread, title, lines, tag="email_docs_received")
        await _set_row(row, decision="documents_kept_for_booking")
        return
    await to_owner(thread, row, "documents_without_shipment")


# ------------------------------------------------------------------------------------------------ questions

async def post_to_messages(thread: dict, row: dict, settings: dict):
    """A customer question goes into the shipment's (or company's) message thread, where the comms agent drafts the
    answer (sent by a person, or by Autopilot); the answer comes back to the customer by mail (hooks.relay)."""
    from services import messaging

    user = await db.users.find_one({"id": thread["user_id"]}, {"_id": 0, "password_hash": 0}) if thread.get("user_id") else None
    if not user:
        await to_owner(thread, row, "question_without_customer")
    mt = await messaging.get_or_create_thread(company_id=thread["company_id"], shipment_id=thread.get("shipment_id"),
                                              subject=thread.get("shipment_reference") or thread.get("quote_number") or thread.get("subject"),
                                              created_by=messaging.author_from_user(user))
    await db.threads.update_one({"id": mt["id"]}, {"$set": {"email_thread_id": thread["id"]}})
    msg = await messaging.add_message(mt, body=(row.get("new_text") or row.get("text") or "")[:8000],
                                      author={**messaging.author_from_user(user), "via": "email"})
    await db.messages.update_one({"id": msg["id"]}, {"$set": {"email_message_id": row["id"]}})
    await _set_row(row, decision=f"posted_to_messages:{mt['id']}", portal_thread_id=mt["id"])
    from core.autonomy import autopilot_scope
    if await autopilot_scope(thread.get("company_id"), settings, agent="email_channel") == "off":
        lang = thread.get("language") or "fi"
        title, lines = texts.received_for_owner(lang)
        await outbound.send(thread, title, lines, tag="email_ack")
