"""Notification channels (s14): SMS preference, SMS/e-mail outboxes, provider delivery callbacks.

    PATCH /api/channels/me                     {phone, notify_sms, notify_email}   any signed-in user
    GET   /api/channels/status                 providers in use and the SMS switch    internal
    GET   /api/ops/sms-outbox                  last SMS with provider result          internal
    POST  /api/ops/channels/test               {channel: sms|email, to}               settings.write
    POST  /api/webhooks/twilio/sms-status      Twilio StatusCallback (X-Twilio-Signature checked)
    POST  /api/webhooks/resend                 Resend webhook (Svix signature checked) → email_outbox.delivery
    POST  /api/webhooks/postmark               Postmark delivery/bounce webhook (HTTP basic auth: POSTMARK_WEBHOOK_TOKEN)
"""
import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core import config
from core.audit import audit
from core.db import db
from core.deps import current_user, require_internal, require_perm
from core.email import render, send_email
from core.settings import get_settings
from core.sms import normalize_phone, provider as sms_provider, record_status_callback, send_sms
from services.notify_channels import POSTMARK_EVENTS, RESEND_EVENTS, record_email_event

router = APIRouter(prefix="/api", tags=["channels"])


class ChannelPrefsIn(BaseModel):
    phone: str | None = None
    notify_sms: bool | None = None
    notify_email: bool | None = None


@router.patch("/channels/me")
async def update_my_channels(body: ChannelPrefsIn, user: dict = Depends(current_user)):
    changes = {}
    if body.phone is not None:
        if body.phone.strip() == "":
            changes["phone"] = None
        else:
            num = normalize_phone(body.phone)
            if not num:
                raise HTTPException(400, "Puhelinnumero ei kelpaa / Phone number is not valid (use +358… or 040…)")
            changes["phone"] = num
    if body.notify_sms is not None:
        changes["notify_sms"] = bool(body.notify_sms)
    if body.notify_email is not None:
        changes["notify_email"] = bool(body.notify_email)
    if changes.get("notify_sms") and not (changes.get("phone") or user.get("phone")):
        raise HTTPException(400, "Lisää puhelinnumero ensin / Add a phone number first")
    if changes:
        await db.users.update_one({"id": user["id"]}, {"$set": changes})
        await audit(user, "channels.preferences", "user", user["id"], changes, company_id=user.get("company_id"))
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "phone": 1, "notify_sms": 1, "notify_email": 1})
    return {"phone": u.get("phone"), "notify_sms": bool(u.get("notify_sms")), "notify_email": u.get("notify_email", True)}


@router.get("/channels/me")
async def my_channels(user: dict = Depends(current_user)):
    s = await get_settings()
    return {"phone": user.get("phone"), "notify_sms": bool(user.get("notify_sms")),
            "notify_email": user.get("notify_email", True), "sms_available": bool(s.get("sms_notifications_enabled"))}


@router.get("/channels/status")
async def channels_status(user: dict = Depends(require_internal)):
    s = await get_settings()
    return {"email_provider": config.EMAIL_PROVIDER, "email_from": config.EMAIL_FROM,
            "sms_provider": sms_provider(), "sms_provider_configured": config.SMS_PROVIDER,
            "sms_test_credentials": config.TWILIO_FROM == "+15005550006",
            "sms_notifications_enabled": bool(s.get("sms_notifications_enabled")),
            "email_sent_24h": await db.email_outbox.count_documents({"status": "sent"}),
            "sms_total": await db.sms_outbox.count_documents({})}


@router.get("/ops/sms-outbox")
async def sms_outbox(limit: int = 50, user: dict = Depends(require_internal)):
    rows = await db.sms_outbox.find({}, {"_id": 0}).sort("created_at", -1).to_list(min(max(limit, 1), 200))
    return {"items": rows}


class ChannelTestIn(BaseModel):
    channel: str
    to: str


@router.post("/ops/channels/test")
async def channel_test(body: ChannelTestIn, user: dict = Depends(require_perm("settings.write"))):
    if body.channel == "sms":
        rec = await send_sms(body.to, "Huolitsija: testiviesti / test message.", tag="test")
    elif body.channel == "email":
        html, text = render("Huolitsija: testiviesti / test message",
                            ["Tämä on Huolitsijan lähettämä testiviesti.", "This is a test message sent by Huolitsija."])
        rec = await send_email(body.to, "Huolitsija: testiviesti / test message", html, text, tag="test")
    else:
        raise HTTPException(400, "channel must be sms or email")
    await audit(user, f"channels.test_{body.channel}", "channel", rec["id"], {"to": body.to, "status": rec["status"],
                                                                                "provider": rec["provider"]})
    return rec


# ---- provider callbacks ---------------------------------------------------------------------------------------------
def twilio_signature(url: str, params: dict, token: str) -> str:
    """Twilio's documented scheme: HMAC-SHA1 over the full URL + every POST param name+value sorted by name, base64."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@router.post("/webhooks/twilio/sms-status")
async def twilio_status(request: Request):
    form = dict(await request.form())
    if not config.TWILIO_AUTH_TOKEN:
        raise HTTPException(503, "Twilio not configured")
    url = config.TWILIO_STATUS_CALLBACK_URL or str(request.url)
    expected = twilio_signature(url, form, config.TWILIO_AUTH_TOKEN)
    if not hmac.compare_digest(expected, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(403, "bad signature")
    row = await record_status_callback(form)
    return {"ok": True, "matched": bool(row)}


def svix_verify(secret: str, msg_id: str, ts: str, body: bytes, sig_header: str, tolerance: int = 300) -> bool:
    """Resend signs webhooks with Svix: base64 HMAC-SHA256 of "<id>.<timestamp>.<body>" keyed by the whsec_ secret."""
    try:
        if abs(time.time() - int(ts)) > tolerance:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:  # noqa: BLE001
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[-1]) for part in sig_header.split())


@router.post("/webhooks/resend")
async def resend_webhook(request: Request):
    body = await request.body()
    if not config.RESEND_WEBHOOK_SECRET:
        raise HTTPException(503, "Resend webhook not configured")
    h = request.headers
    if not svix_verify(config.RESEND_WEBHOOK_SECRET, h.get("svix-id", ""), h.get("svix-timestamp", "0"), body,
                       h.get("svix-signature", "")):
        from services.demo_autoquote import resend as demo_resend
        raise HTTPException(401 if demo_resend.enabled() else 403, "bad signature")
    evt = await request.json()
    event = RESEND_EVENTS.get(evt.get("type"))
    email_id = (evt.get("data") or {}).get("email_id")
    matched = bool(event and email_id and await record_email_event(email_id, event, evt.get("created_at"), raw=evt))
    from services.demo_autoquote import resend as demo_resend
    alias_matched = await demo_resend.record_sent(evt)
    matched = matched or alias_matched
    return {"ok": True, "matched": matched}


@router.post("/webhooks/postmark")
async def postmark_webhook(request: Request):
    """Postmark posts Delivery / Bounce / SpamComplaint / Open events; its webhooks authenticate with basic auth in the URL
    (https://postmark:<POSTMARK_WEBHOOK_TOKEN>@<host>/api/webhooks/postmark)."""
    if not config.POSTMARK_WEBHOOK_TOKEN:
        raise HTTPException(503, "Postmark webhook not configured")
    auth = request.headers.get("authorization", "")
    try:
        user_pw = base64.b64decode(auth[6:]).decode() if auth.lower().startswith("basic ") else ""
    except Exception:  # noqa: BLE001
        user_pw = ""
    if not hmac.compare_digest(user_pw.split(":", 1)[-1], config.POSTMARK_WEBHOOK_TOKEN):
        raise HTTPException(401, "bad credentials")
    evt = await request.json()
    kind = {"Delivery": "Delivered", "Bounce": "Bounced", "SpamComplaint": "SpamComplaint", "Open": "Opened",
            "Click": "LinkClicked"}.get(evt.get("RecordType"), evt.get("RecordType"))
    event = POSTMARK_EVENTS.get(kind)
    at = evt.get("DeliveredAt") or evt.get("BouncedAt") or evt.get("ReceivedAt")
    matched = bool(event and evt.get("MessageID") and await record_email_event(evt["MessageID"], event, at, raw=evt))
    return {"ok": True, "matched": matched}
