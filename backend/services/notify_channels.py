"""Shipment milestones by SMS, and the delivery status of e-mail and SMS.

SMS goes to every active user of the shipment's company who saved a phone number and switched `notify_sms` on, in the
user's language, when ops have turned on the company-wide switch `sms_notifications_enabled` (Settings). E-mail
notifications keep going through core.notify / the comms agent; this module only records what the provider later says
about them (delivered, bounced) so "sent" is never confused with "delivered".
"""
import logging

import httpx

from core import config
from core.audit import audit
from core.db import db
from core.events import on
from core.lifecycle import background_loop
from core.settings import get_settings
from core.sms import normalize_phone, send_sms
from core.util import now_iso

log = logging.getLogger("notify_channels")

SYSTEM = {"id": "system", "name": "Huolitsija", "role": "system", "org_type": "internal"}

# status -> (fi, en). {ref}, {dest}, {origin} are filled in. Kept short: one GSM-7 segment where possible.
SMS_TEXTS = {
    "picked_up": ("Huolitsija: {ref} on noudettu ({origin}).", "Huolitsija: {ref} was picked up ({origin})."),
    "in_transit": ("Huolitsija: {ref} on matkalla kohteeseen {dest}.", "Huolitsija: {ref} is in transit to {dest}."),
    "customs_hold": ("Huolitsija: {ref} on pysäytetty tullissa. Katso lisätiedot portaalista.",
                     "Huolitsija: {ref} is held in customs. See the portal for details."),
    "exception": ("Huolitsija: lähetyksessä {ref} on poikkeama. Hoidamme asian ja ilmoitamme uudesta arviosta.",
                  "Huolitsija: shipment {ref} has an exception. We are handling it and will send a new estimate."),
    "out_for_delivery": ("Huolitsija: {ref} on jakelussa tänään kohteeseen {dest}.",
                         "Huolitsija: {ref} is out for delivery to {dest} today."),
    "delivered": ("Huolitsija: {ref} on toimitettu kohteeseen {dest}.", "Huolitsija: {ref} was delivered to {dest}."),
}


def sms_text(status: str, shipment: dict, lang: str) -> str | None:
    pair = SMS_TEXTS.get(status)
    if not pair:
        return None
    args = {"ref": shipment.get("reference") or shipment.get("id"),
            "dest": (shipment.get("destination") or {}).get("name") or "",
            "origin": (shipment.get("origin") or {}).get("name") or ""}
    return (pair[1] if lang == "en" else pair[0]).format(**args)


async def sms_recipients(company_id: str) -> list[dict]:
    out = []
    async for u in db.users.find({"company_id": company_id, "disabled": {"$ne": True}, "notify_sms": True},
                                 {"_id": 0, "password_hash": 0}):
        if normalize_phone(u.get("phone")):
            out.append(u)
    return out


async def notify_shipment_sms(shipment: dict, status: str, *, dedupe_key: str | None = None) -> list[dict]:
    """Send the milestone SMS for one status change. Idempotent per shipment + status + user, or per dedupe_key + user
    (the exception notice uses services.agents.exception_agent.notice_key)."""
    if not shipment.get("company_id") or status not in SMS_TEXTS:
        return []
    if not (await get_settings()).get("sms_notifications_enabled"):
        return []
    sent = []
    for u in await sms_recipients(shipment["company_id"]):
        key = f"{dedupe_key}:{u['id']}" if dedupe_key else f"shipment:{shipment['id']}:{status}:{u['id']}"
        if await db.sms_outbox.find_one({"dedupe_key": key}, {"_id": 1}):
            continue
        lang = "en" if (u.get("locale") or "fi") == "en" else "fi"
        rec = await send_sms(u["phone"], sms_text(status, shipment, lang), tag="shipment", company_id=shipment["company_id"],
                             user_id=u["id"], ref={"shipment_id": shipment["id"], "status": status})
        await db.sms_outbox.update_one({"id": rec["id"]}, {"$set": {"dedupe_key": key}})
        await audit(SYSTEM, "sms.sent", "shipment", shipment["id"],
                    {"to_user": u["id"], "status": status, "provider": rec["provider"], "result": rec["status"],
                     "sms_id": rec["id"]}, company_id=shipment["company_id"])
        sent.append(rec)
    return sent


@on("shipment.status_changed")
async def _on_status(p: dict):
    s = p.get("shipment") or {}
    if p.get("new_status") == "exception":
        return  # the exception agent sends this notice, SMS included, once per exception (exception-notice:<id>)
    if s.get("id") and p.get("new_status") != p.get("old_status"):
        await notify_shipment_sms(s, p.get("new_status"))


# ---- e-mail delivery status ---------------------------------------------------------------------------------------
RESEND_EVENTS = {"email.sent": "sent", "email.delivered": "delivered", "email.delivery_delayed": "delayed",
                 "email.bounced": "bounced", "email.complained": "complained", "email.opened": "opened",
                 "email.clicked": "clicked", "email.failed": "failed"}


async def record_email_event(provider_id: str, event: str, at: str | None = None, raw: dict | None = None) -> bool:
    upd = {"delivery": event, "delivery_at": at or now_iso()}
    r = await db.email_outbox.update_one({"provider_id": provider_id},
                                         {"$set": upd, "$push": {"delivery_events": {"event": event, "at": upd["delivery_at"],
                                                                                     "raw": raw or {}}}})
    return r.matched_count > 0


async def refresh_resend_status(limit: int = 50) -> int:
    """Ask Resend for the last event of recent mails that have no final delivery state (works without a webhook)."""
    if not config.RESEND_API_KEY:
        return 0
    n = 0
    q = {"provider": "resend", "status": "sent", "provider_id": {"$ne": None},
         "delivery": {"$nin": ["delivered", "bounced", "complained", "failed"]}}
    rows = await db.email_outbox.find(q, {"_id": 0, "provider_id": 1, "delivery": 1}).sort("created_at", -1).to_list(limit)
    if not rows:
        return 0
    async with httpx.AsyncClient(timeout=15) as client:
        for row in rows:
            try:
                r = await client.get(f"https://api.resend.com/emails/{row['provider_id']}",
                                     headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"})
            except Exception:  # noqa: BLE001
                log.exception("resend status lookup failed")
                continue
            if r.status_code != 200:
                continue
            last = (r.json() or {}).get("last_event")
            if last and last != row.get("delivery"):
                await record_email_event(row["provider_id"], last, raw={"source": "GET /emails/{id}", "last_event": last})
                n += 1
    return n


POSTMARK_EVENTS = {"Delivered": "delivered", "Bounced": "bounced", "SpamComplaint": "complained", "Opened": "opened",
                   "LinkClicked": "clicked", "Transient": "delayed", "SubscriptionChanged": "unsubscribed"}


async def refresh_postmark_status(limit: int = 50) -> int:
    """Postmark's message details list every event (Delivered with the receiving MTA's answer, Bounced, ...)."""
    if not config.POSTMARK_SERVER_TOKEN:
        return 0
    n = 0
    q = {"provider": "postmark", "status": "sent", "provider_id": {"$ne": None},
         "delivery": {"$nin": ["delivered", "bounced", "complained"]}}
    rows = await db.email_outbox.find(q, {"_id": 0, "provider_id": 1, "delivery": 1}).sort("created_at", -1).to_list(limit)
    if not rows:
        return 0
    async with httpx.AsyncClient(timeout=15) as client:
        for row in rows:
            try:
                r = await client.get(f"https://api.postmarkapp.com/messages/outbound/{row['provider_id']}/details",
                                     headers={"X-Postmark-Server-Token": config.POSTMARK_SERVER_TOKEN, "Accept": "application/json"})
            except Exception:  # noqa: BLE001
                log.exception("postmark status lookup failed")
                continue
            if r.status_code != 200:
                continue
            events = (r.json() or {}).get("MessageEvents") or []
            for ev in events:
                mapped = POSTMARK_EVENTS.get(ev.get("Type"))
                if mapped and mapped != row.get("delivery"):
                    await record_email_event(row["provider_id"], mapped, ev.get("ReceivedAt"),
                                             raw={"source": "GET /messages/outbound/{id}/details", "type": ev.get("Type"),
                                                  "details": ev.get("Details") or {}})
                    row["delivery"] = mapped
                    n += 1
    return n


@background_loop(seconds=120)
async def _email_status_loop():
    if config.EMAIL_PROVIDER == "resend":
        await refresh_resend_status()
    elif config.EMAIL_PROVIDER == "postmark":
        await refresh_postmark_status()
