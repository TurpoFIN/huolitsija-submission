"""Text messages (SMS). Provider is chosen by SMS_PROVIDER: twilio | log.

Every message is stored in `sms_outbox` with its delivery result, like core.email's `email_outbox`, so ops can see what a
customer was sent even when the provider is `log` (nothing leaves the app).

    rec = await send_sms("+358401234567", "HUO-2026-000057 on toimitettu.", tag="shipment", company_id=..., ref={...})

Twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and a sender: TWILIO_FROM_NUMBER (a number on the account) or
TWILIO_MESSAGING_SERVICE_SID. On a trial account Twilio only delivers to verified recipients; upgrading the account removes
that limit with no code change. scripts/s14_twilio_setup.py verifies a recipient, claims the number and sends the proof.
"""
import logging
import re

import httpx

from core import config
from core.db import db
from core.util import new_id, now_iso

log = logging.getLogger("sms")

TWILIO_API = "https://api.twilio.com/2010-04-01"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_phone(raw: str | None, default_cc: str = "358") -> str | None:
    """E.164 or None. Finnish numbers written locally (040 123 4567) get +358."""
    if not raw:
        return None
    s = re.sub(r"[\s\-().]", "", str(raw))
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):
        s = f"+{default_cc}{s[1:]}"
    elif not s.startswith("+"):
        s = "+" + s
    return s if E164.match(s) else None


def provider() -> str:
    if config.SMS_PROVIDER == "twilio" and config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and \
            (config.TWILIO_FROM or config.TWILIO_MESSAGING_SERVICE_SID):
        return "twilio"
    return "log"


def segments(text: str) -> int:
    """Billable SMS parts: GSM-7 160/153, otherwise UCS-2 70/67 (å, ä, ö are in GSM-7; € counts double)."""
    gsm = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    ext = "^{}\\[~]|€"
    if all(c in gsm or c in ext for c in text):
        n = sum(2 if c in ext else 1 for c in text)
        return 1 if n <= 160 else -(-n // 153)
    return 1 if len(text) <= 70 else -(-len(text) // 67)


async def send_sms(to: str, text: str, *, tag: str = "general", company_id: str | None = None,
                   ref: dict | None = None, user_id: str | None = None) -> dict:
    number = normalize_phone(to)
    rec = {"id": new_id(), "to": number or to, "text": text[:1600], "tag": tag, "provider": provider(),
           "company_id": company_id, "user_id": user_id, "ref": ref or {}, "status": "queued",
           "segments": segments(text), "created_at": now_iso()}
    if not number:
        rec.update(status="failed", error="invalid phone number")
    elif rec["provider"] == "twilio":
        form = {"To": number, "Body": text[:1600]}
        if config.TWILIO_MESSAGING_SERVICE_SID:
            form["MessagingServiceSid"] = config.TWILIO_MESSAGING_SERVICE_SID
        else:
            form["From"] = config.TWILIO_FROM
        if config.TWILIO_STATUS_CALLBACK_URL:
            form["StatusCallback"] = config.TWILIO_STATUS_CALLBACK_URL
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{TWILIO_API}/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json", data=form,
                                      auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN))
            data = r.json() if r.content else {}
            ok = r.status_code in (200, 201) and not data.get("error_code")
            rec.update(status=(data.get("status") or "queued") if ok else "failed", provider_id=data.get("sid"),
                       provider_response={k: data.get(k) for k in ("sid", "status", "error_code", "error_message", "code",
                                                                     "message", "num_segments", "price", "more_info")},
                       http_status=r.status_code)
            if not ok:
                rec["error"] = data.get("message") or data.get("error_message") or f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            log.warning("sms to %s failed: %s", number, exc)
            rec.update(status="failed", error=str(exc))
    else:
        rec.update(status="logged")
    await db.sms_outbox.insert_one(dict(rec))
    rec.pop("_id", None)
    return rec


async def record_status_callback(form: dict) -> dict | None:
    """Twilio StatusCallback (queued → sent → delivered | undelivered | failed) onto the outbox row."""
    sid = form.get("MessageSid") or form.get("SmsSid")
    if not sid:
        return None
    upd = {"status": form.get("MessageStatus") or form.get("SmsStatus"), "status_at": now_iso()}
    if form.get("ErrorCode"):
        upd["error"] = f"Twilio {form.get('ErrorCode')}"
    await db.sms_outbox.update_one({"provider_id": sid}, {"$set": upd, "$push": {"events": {**upd, "raw": dict(form)}}})
    return await db.sms_outbox.find_one({"provider_id": sid}, {"_id": 0})
