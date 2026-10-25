"""E-mail channel HTTP: inbound webhook, one-click links in our mails, the mailed payment link, and the ops inbox."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html as html_lib
import json
import re
from urllib.parse import quote as urlquote
from email.utils import parseaddr

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from core import config
from core.audit import audit
from core.db import db
from core.deps import require_perm
from core.security import COOKIE_NAME, create_session_token
from core.settings import get_settings, update_settings
from core.storage import get_file
from core.util import NO_ID, now, now_iso, parse_iso
from services import email_channel  # noqa: F401 - registers hooks and loops
from services.email_channel import conf, flow, inbound, intake, outbound, store, worker

router = APIRouter(prefix="/api/email-channel", tags=["email-channel"])
OPS = require_perm("messages.ops")
HIDDEN = {"_id": 0, "text": 0}


# ------------------------------------------------------------------------------------------------ inbound webhook

def _signature_ok(raw: bytes, given: str | None) -> bool:
    secret = conf.webhook_secret()
    if not secret or not given:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given.strip().lower())


def _basic_auth_ok(given: str | None, user: str, token: str | None) -> bool:
    if not token or not given or not given.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(given.split(None, 1)[1], validate=True)
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(decoded, f"{user}:{token}".encode())


def _postmark_auth_ok(given: str | None) -> bool:
    return _basic_auth_ok(given, "postmark", conf.postmark_token())


def _cloudmailin_auth_ok(given: str | None) -> bool:
    return _basic_auth_ok(given, "cloudmailin", conf.cloudmailin_token())


SPF_RESULTS = {"pass", "fail", "softfail", "neutral", "none", "temperror", "permerror"}


def _cloudmailin_to_raw(payload: dict, *, authenticated: bool) -> bytes:
    """CloudMailin JSON (Raw): {"envelope": {...}, "message": "<RFC 822>"} -> RFC 822 bytes. Only for a post that passed
    CloudMailin's basic auth, the SPF result CloudMailin checked on the SMTP envelope is recorded in an
    Authentication-Results header of our own (mime.auth_passed reads those); a header the sender wrote is never trusted more."""
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("CloudMailin payload without message")
    raw = message.encode("utf-8")
    env = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    spf = env.get("spf") if isinstance(env.get("spf"), dict) else {}
    result = str(spf.get("result") or "none").strip().lower()
    if authenticated and result in SPF_RESULTS:
        mailfrom = re.sub(r"[^A-Za-z0-9.@+_=-]", "", str(env.get("from") or ""))[:254] or "unknown"
        nl = b"\r\n" if b"\r\n" in raw[:4000] else b"\n"
        raw = f"Authentication-Results: cloudmailin.net; spf={result} smtp.mailfrom={mailfrom}".encode() + nl + raw
    return raw


@router.post("/inbound")
async def inbound_webhook(request: Request):
    """Raw RFC 822 (Content-Type message/rfc822), Postmark inbound JSON or CloudMailin JSON (Raw) ({"envelope", "message"}).
    Authenticated by EMAIL_CHANNEL_WEBHOOK_SECRET (header X-Huolitsija-Signature: hex HMAC-SHA256 of the body), Postmark
    basic auth (postmark:EMAIL_CHANNEL_POSTMARK_TOKEN) or CloudMailin basic auth (cloudmailin:EMAIL_CHANNEL_CLOUDMAILIN_TOKEN,
    set in the CloudMailin target URL). Processing is queued; the answer is immediate."""
    raw = await request.body()
    if not (conf.webhook_secret() or conf.postmark_token() or conf.cloudmailin_token()):
        raise HTTPException(503, "Inbound e-mail webhook is not configured (EMAIL_CHANNEL_WEBHOOK_SECRET).")
    authorization = request.headers.get("authorization")
    postmark_authenticated = _postmark_auth_ok(authorization)
    cloudmailin_authenticated = _cloudmailin_auth_ok(authorization)
    if not (_signature_ok(raw, request.headers.get("x-huolitsija-signature")) or postmark_authenticated
            or cloudmailin_authenticated):
        raise HTTPException(401, "Bad signature")
    ctype = (request.headers.get("content-type") or "").lower()
    provider_id = None
    payload = None
    if "json" in ctype:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if "envelope" in payload and "message" in payload:  # CloudMailin JSON (Raw)
                raw = _cloudmailin_to_raw(payload, authenticated=cloudmailin_authenticated)
                payload = None  # not a Postmark payload (AP routing reads Postmark fields)
            else:
                provider_id = payload.get("MessageID")
                raw = inbound.postmark_to_raw(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(400, f"Not a Postmark or CloudMailin inbound payload: {exc}") from None
    from services.email_channel.dispatch import dispatch_raw
    try:
        return await dispatch_raw(raw, provider_id=provider_id, payload=payload,
                                  provider_authenticated=postmark_authenticated or cloudmailin_authenticated)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# ------------------------------------------------------------------------------------------------ links in our mails

def _page(title: str, lines: list[str], status: int = 200) -> HTMLResponse:
    body = "".join(f"<p>{html_lib.escape(x)}</p>" for x in lines)
    return HTMLResponse(f"<!doctype html><html lang='fi'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                        f"<title>{html_lib.escape(title)}</title><body style='font-family:system-ui,sans-serif;max-width:520px;"
                        f"margin:48px auto;padding:0 20px;color:#111'><p style='color:#666;font-size:13px'>{html_lib.escape(config.COMPANY_NAME)}</p>"
                        f"<h1 style='font-size:20px;font-weight:500'>{html_lib.escape(title)}</h1>{body}</body></html>", status_code=status)


async def _token(token: str, kind: str) -> dict:
    t = await db.tokens.find_one({"token": token, "kind": kind}, NO_ID)
    if not t or parse_iso(t["expires_at"]) < now():
        raise HTTPException(404, "link")
    return t


@router.get("/go/{token}")
async def go(token: str):
    """One-click sign-in from a mail we sent to an e-mail-only customer; lands on the page the mail pointed to."""
    try:
        t = await _token(token, "email_link")
    except HTTPException:
        return _page("Linkki on vanhentunut · Link expired",
                     ["Vastaa viimeisimpään sähköpostiimme, niin lähetämme uuden linkin.",
                      "Reply to our latest e-mail and we will send you a new link."], 410)
    user = await db.users.find_one({"id": t["user_id"], "disabled": {"$ne": True}}, {"_id": 0, "password_hash": 0})
    if not user:
        return _page("Linkki ei ole voimassa · Link not valid", [], 410)
    nxt = (t.get("data") or {}).get("next") or "/portal"
    if not nxt.startswith("/portal"):
        nxt = "/portal"
    claimed = await db.tokens.update_one({"token": token, "kind": "email_link", "used": {"$ne": True}},
                                         {"$inc": {"uses": 1}, "$set": {"used": True, "last_used_at": now_iso()}})
    if not claimed.modified_count:
        return _page("Linkki on käytetty · Link already used",
                     ["Vastaa sähköpostiimme, niin lähetämme uuden linkin.", "Reply to our email for a new link."], 410)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE_NAME, create_session_token(user["id"], int(user.get("session_version", 0))), httponly=True,
                    secure=config.COOKIE_SECURE, samesite="lax", max_age=config.SESSION_DAYS * 86400, path="/")
    return resp


@router.get("/pay/{token}")
async def pay(token: str):
    """Stable payment link in the invoice mail: opens (or reuses) a Stripe Checkout session for the amount due."""
    from services import billing, payments

    try:
        t = await _token(token, "email_pay")
    except HTTPException:
        return _page("Maksulinkki on vanhentunut · Payment link expired",
                     ["Vastaa laskusähköpostiin, niin lähetämme uuden linkin.", "Reply to the invoice e-mail for a new link."], 410)
    inv = await db.invoices.find_one({"id": (t.get("data") or {}).get("invoice_id")}, NO_ID)
    if not inv:
        return _page("Laskua ei löydy · Invoice not found", [], 404)
    if inv["status"] == "paid":
        return _page(f"Lasku {inv.get('number')} on maksettu · Invoice {inv.get('number')} is paid", ["Kiitos! · Thank you!"])
    user = await db.users.find_one({"id": t["user_id"]}, {"_id": 0, "password_hash": 0}) or {}
    base = await outbound.base_url()
    try:
        s = await payments.create_checkout(inv, user=user, base_url=base,
                                           success_url=f"{base}/api/email-channel/paid?session_id={{CHECKOUT_SESSION_ID}}",
                                           cancel_url=f"{base}/api/email-channel/paid?cancelled=1&invoice={inv['id']}")
    except payments.PaymentsUnavailable as exc:
        return _page("Korttimaksu ei ole käytössä · Card payment unavailable", [str(exc)], 503)
    except billing.BillingError as exc:
        return _page("Maksua ei voitu aloittaa · Payment could not start", [str(exc)], 409)
    await db.payment_transactions.update_one({"session_id": s["session_id"]}, {"$set": {"email_channel": True}})
    await db.tokens.update_one({"token": token}, {"$inc": {"uses": 1}, "$set": {"last_used_at": now_iso()}})
    return RedirectResponse(s["url"], status_code=303)


@router.get("/paid")
async def paid(session_id: str | None = None, cancelled: str | None = None, invoice: str | None = None):
    """Return page after Stripe Checkout. The session is re-read from Stripe; the URL itself proves nothing."""
    from services import billing, payments

    if cancelled or not session_id:
        return _page("Maksu peruttiin · Payment cancelled",
                     ["Voit maksaa myöhemmin samasta linkistä tai pankkisiirtona viitenumerolla.",
                      "You can pay later from the same link or by bank transfer with the reference number."])
    try:
        r = await payments.sync_session(session_id)
    except (billing.BillingError, payments.PaymentsUnavailable) as exc:
        return _page("Maksun tila tarkistetaan · Checking the payment", [str(exc)], 202)
    inv = await db.invoices.find_one({"id": r.get("invoice_id")}, {"number": 1, "status": 1, "_id": 0}) or {}
    if inv.get("status") == "paid":
        return _page(f"Kiitos, lasku {inv.get('number')} on maksettu · Thank you, invoice {inv.get('number')} is paid",
                     ["Kuitti lähetetään sähköpostiisi.", "A receipt is on its way to your e-mail."])
    return _page("Maksu käsittelyssä · Payment processing",
                 ["Vahvistamme maksun heti, kun pankki on käsitellyt sen.", "We confirm the payment as soon as the bank has processed it."])


# ------------------------------------------------------------------------------------------------ ops inbox

async def _owner_card(row: dict) -> dict | None:
    if not row.get("approval_id"):
        return None
    return await db.approvals.find_one({"id": row["approval_id"]}, {"_id": 0, "id": 1, "status": 1, "reason": 1, "created_at": 1})


async def _thread_view(t: dict, *, full: bool) -> dict:
    out = dict(t)
    if full:
        rows = await db.email_messages.find({"thread_id": t["id"]}, {"_id": 0}).sort("created_at", 1).to_list(500)
        outbox = {}
        ids = [r["id"] for r in rows if r["direction"] == "out"]
        async for o in db.email_outbox.find({"ref.email_message_id": {"$in": ids}}, {"_id": 0, "status": 1, "ref": 1, "error": 1, "delivery": 1, "delivery_at": 1, "from": 1}).sort("created_at", 1):
            outbox[o["ref"]["email_message_id"]] = o
        for r in rows:
            r.pop("lease_owner", None)
            r.pop("retry_payload", None)
            r["owner_card"] = await _owner_card(r)
            if r["direction"] == "out" and r["id"] in outbox:
                if outbox[r["id"]].get("from"):
                    r["from_email"] = parseaddr(outbox[r["id"]]["from"])[1]
                r["delivery"] = {"status": outbox[r["id"]].get("delivery") or outbox[r["id"]].get("status"),
                                 "at": outbox[r["id"]].get("delivery_at"), "error": outbox[r["id"]].get("error")}
        out["messages"] = rows
    if t.get("quote_id"):
        q = await db.quotes.find_one({"id": t["quote_id"]}, {"_id": 0, "number": 1, "status": 1, "valid_until": 1, "id": 1,
                                                            "request.origin.name": 1, "request.destination.name": 1})
        out["quote"] = q
    if t.get("shipment_id"):
        out["shipment"] = await db.shipments.find_one({"id": t["shipment_id"]}, {"_id": 0, "id": 1, "reference": 1, "status": 1,
                                                                                  "eta": 1})
        inv = await db.invoices.find_one({"shipment_id": t["shipment_id"], "kind": "invoice"},
                                         {"_id": 0, "id": 1, "number": 1, "status": 1, "total_cents": 1}, sort=[("created_at", -1)])
        out["invoice"] = inv
    if t.get("company_id"):
        out["company"] = await db.companies.find_one({"id": t["company_id"]}, {"_id": 0, "id": 1, "name": 1, "kyc_status": 1,
                                                                                "sanctions_status": 1, "test_tenant": 1})
    return out


@router.get("/threads")
async def list_threads(state: str | None = None, q: str | None = None, owner: bool = False, limit: int = 100,
                       user: dict = Depends(OPS)):
    filt: dict = {}
    if state:
        filt["state"] = state
    if owner:
        filt["needs_owner"] = True
    if q:
        rx = {"$regex": re.escape(q.strip()[:60]), "$options": "i"}
        filt["$or"] = [{"customer_email": rx}, {"subject": rx}, {"ref": rx}, {"customer_name": rx}]
    items = await db.email_threads.find(filt, NO_ID).sort("last_message_at", -1).to_list(max(1, min(limit, 300)))
    counts = {}
    async for c in db.email_threads.aggregate([{"$group": {"_id": "$state", "n": {"$sum": 1}}}]):
        counts[c["_id"] or "new"] = c["n"]
    owner_n = await db.email_messages.count_documents({"status": "needs_owner"})
    for t in items:
        last = await db.email_messages.find_one({"thread_id": t["id"]}, {"_id": 0, "direction": 1, "new_text": 1, "text": 1,
                                                                         "subject": 1, "created_at": 1}, sort=[("created_at", -1)])
        if last:
            t["last"] = {"direction": last["direction"], "at": last["created_at"],
                         "preview": ((last.get("new_text") or last.get("text") or "")[:160])}
    return {"items": items, "total": len(items), "counts": counts, "needs_owner": owner_n}


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, user: dict = Depends(OPS)):
    t = await store.thread_by_id(thread_id)
    if not t:
        raise HTTPException(404, "E-mail thread not found")
    return await _thread_view(t, full=True)


@router.get("/for")
async def threads_for(shipment_id: str | None = None, quote_id: str | None = None, user: dict = Depends(OPS)):
    """E-mail threads of a shipment or quote, with every message (ops shipment and quote pages)."""
    if not shipment_id and not quote_id:
        raise HTTPException(400, "Give shipment_id or quote_id")
    ts = await store.threads_for(shipment_id=shipment_id, quote_id=quote_id)
    return {"items": [await _thread_view(t, full=True) for t in ts]}


class ReplyIn(BaseModel):
    text: str


@router.post("/threads/{thread_id}/reply")
async def reply(thread_id: str, body: ReplyIn, user: dict = Depends(OPS)):
    t = await store.thread_by_id(thread_id)
    if not t:
        raise HTTPException(404, "E-mail thread not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Write the reply first")
    lang = t.get("language") or "fi"
    row = await outbound.send(t, t.get("subject") or ("Vastaus" if lang != "en" else "Reply"),
                              [x for x in text.split("\n") if x.strip()], tag="email_ops_reply", kind="ops")
    await db.email_messages.update_one({"id": row["id"]}, {"$set": {"author": {"id": user["id"], "name": user.get("name")}}})
    await audit(user, "email_reply", "email_thread", thread_id, {"message_id": row["message_id"]}, company_id=t.get("company_id"))
    return row


@router.post("/threads/{thread_id}/resend-quote")
async def resend_quote(thread_id: str, user: dict = Depends(OPS)):
    """Send the thread's quote mail again, exactly as the channel first sent it (flow.send_quote_status: text, PDF,
    thread headers), e.g. when the first mail never reached the customer. Only a sent quote; a reply to it can book."""
    t = await store.thread_by_id(thread_id)
    if not t:
        raise HTTPException(404, "E-mail thread not found")
    q = await db.quotes.find_one({"id": t.get("quote_id")}, {"_id": 0}) if t.get("quote_id") else None
    if not q or q.get("status") != "sent":
        raise HTTPException(409, "The thread has no sent quote to send again")
    await flow.send_quote_status(t, q, human_actor=user)
    last = await db.email_messages.find_one({"thread_id": thread_id, "direction": "out"}, {"_id": 0}, sort=[("created_at", -1)])
    await audit(user, "email_quote_resent", "email_thread", thread_id, {"quote": q.get("number"),
                "message_id": (last or {}).get("message_id")}, company_id=t.get("company_id"))
    return last


class ReprocessIn(BaseModel):
    intent: str | None = None


@router.get("/messages")
async def list_messages(status: str | None = None, unthreaded: bool = False, message_id: str | None = None, limit: int = 100,
                        user: dict = Depends(OPS)):
    """Inbound mail by status, including mail kept outside any thread (auto-replies, bounces, own loops)."""
    filt: dict = {"direction": "in"}
    if status:
        filt["status"] = status
    if unthreaded:
        filt["thread_id"] = None
    if message_id:
        filt["message_id"] = message_id
    items = await db.email_messages.find(filt, HIDDEN).sort("created_at", -1).to_list(max(1, min(limit, 300)))
    return {"items": items, "total": len(items)}


@router.post("/messages/{message_id}/reprocess")
async def reprocess(message_id: str, body: ReprocessIn, user: dict = Depends(OPS)):
    """Owner decision: treat the mail as <intent> and let the channel act on it (or re-read it with the model)."""
    row = await db.email_messages.find_one({"id": message_id, "direction": "in"}, {"_id": 0})
    if not row:
        raise HTTPException(404, "Message not found")
    if body.intent and body.intent not in intake.INTENTS:
        raise HTTPException(400, f"Unknown intent; use one of {', '.join(intake.INTENTS)}")
    if row.get("approval_id"):
        await db.approvals.update_one({"id": row["approval_id"], "status": "pending"}, {"$set": {
            "status": "approved", "decided_at": now_iso(), "decided_by": {"id": user["id"], "name": user.get("name")},
            "decision_note": f"intent={body.intent or 're-read'}"}})
    await audit(user, "email_reprocess", "email_message", message_id, {"intent": body.intent}, company_id=None)
    out = await flow.process({**row, "status": "processing"}, forced_intent=body.intent, actor=user)
    if row.get("thread_id") and not await db.email_messages.count_documents({"thread_id": row["thread_id"], "status": "needs_owner"}):
        await store.update_thread(row["thread_id"], needs_owner=False)
    return out


@router.post("/messages/{message_id}/dismiss")
async def dismiss(message_id: str, user: dict = Depends(OPS)):
    row = await db.email_messages.find_one({"id": message_id}, {"_id": 0})
    if not row:
        raise HTTPException(404, "Message not found")
    await db.email_messages.update_one({"id": message_id}, {"$set": {"status": "dismissed", "dismissed_by": user["id"],
                                                                     "dismissed_at": now_iso()}})
    if row.get("approval_id"):
        await db.approvals.update_one({"id": row["approval_id"], "status": "pending"}, {"$set": {
            "status": "rejected", "decided_at": now_iso(), "decided_by": {"id": user["id"], "name": user.get("name")},
            "decision_note": "dismissed in the e-mail inbox"}})
    if row.get("thread_id") and not await db.email_messages.count_documents({"thread_id": row["thread_id"], "status": "needs_owner"}):
        await store.update_thread(row["thread_id"], needs_owner=False)
    await audit(user, "email_dismiss", "email_message", message_id, {}, company_id=None)
    return {"ok": True}


@router.get("/messages/{message_id}/attachments/{index}")
async def attachment(message_id: str, index: int, user: dict = Depends(OPS)):
    row = await db.email_messages.find_one({"id": message_id}, {"_id": 0, "attachments": 1})
    atts = (row or {}).get("attachments") or []
    if not (0 <= index < len(atts)) or not atts[index].get("file_id"):
        raise HTTPException(404, "Attachment not found")
    data, name, ctype = await get_file(atts[index]["file_id"])
    return Response(data, media_type=ctype, headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urlquote(name, safe="")})


@router.get("/messages/{message_id}/raw")
async def raw(message_id: str, user: dict = Depends(OPS)):
    row = await db.email_messages.find_one({"id": message_id}, {"_id": 0, "raw_file_id": 1})
    if not (row or {}).get("raw_file_id"):
        raise HTTPException(404, "Raw message not stored")
    data, _n, _c = await get_file(row["raw_file_id"])
    return Response(data, media_type="message/rfc822")


@router.get("/status")
async def status(user: dict = Depends(OPS)):
    s = await get_settings()
    imap = await db.system.find_one({"key": "email_channel:imap"}, {"_id": 0}) or {}
    c = conf.imap()
    return {"enabled": s.get("email_channel_enabled", True), "address": conf.address(), "sender": conf.sender(),
            "provider": conf.provider(), "smtp_configured": bool(conf.smtp()), "imap_configured": bool(c),
            "imap": {"host": c["host"], "user": c["user"], "folder": c["folder"]} if c else None,
            "webhook_configured": bool(conf.webhook_secret() or conf.postmark_token()), "last_poll_at": imap.get("last_poll_at"),
            "last_error": imap.get("last_error"), "last_error_at": imap.get("last_error_at"),
            "queue": {k: await db.email_messages.count_documents({"direction": "in", "status": k})
                      for k in ("received", "retry", "processing", "needs_owner", "failed")}}


@router.post("/mailtm/poll")
async def mailtm_poll(user: dict = Depends(require_perm("messages.ops"))):
    """Fetch the mail.tm inbound mailbox now (the durable job does this every MAILTM_POLL_SECONDS)."""
    from services.email_channel import mailtm
    if not mailtm.configured():
        raise HTTPException(409, "mail.tm inbound is not configured (MAILTM_ADDRESS, MAILTM_PASSWORD)")
    return await mailtm.poll_once()


@router.post("/poll")
async def poll_now(user: dict = Depends(require_perm("messages.ops"))):
    return await worker.run_once()


class EnabledIn(BaseModel):
    enabled: bool


@router.post("/enabled")
async def set_enabled(body: EnabledIn, user: dict = Depends(require_perm("settings.write"))):
    await update_settings({"email_channel_enabled": body.enabled})
    await audit(user, "email_channel_switch", "settings", "email_channel_enabled", {"enabled": body.enabled})
    return {"enabled": body.enabled}
