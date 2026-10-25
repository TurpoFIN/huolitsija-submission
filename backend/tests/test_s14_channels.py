"""s14 notification channels: SMS adapter, milestone SMS, provider callbacks, SMTP delivery.

Boundaries are checked against independent implementations, not against doubles written here:
- Twilio request signatures with the official `twilio` library's RequestValidator;
- Resend (Svix) webhook signatures with the official `svix` library;
- SMTP delivery against a real SMTP server (GreenMail sandbox, s3's setup) and read back over IMAP;
- Twilio's REST API itself when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are set (real account: an invalid recipient is
  rejected by Twilio; with TWILIO_FROM_NUMBER and S14_SMS_PROOF_TO a real SMS is sent and must be accepted).
Database tests use a throwaway MongoDB database that is dropped at the end.
"""
import asyncio
import imaplib
import json
import os
import socket
import time
import uuid
from email import message_from_bytes

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from core import config, sms
from routes import channels
from services import notify_channels

MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")


def _with_db(fn):
    async def run():
        client = AsyncIOMotorClient(MONGO)
        name = f"huo_test_s14_{uuid.uuid4().hex[:8]}"
        try:
            return await fn(client[name])
        finally:
            await client.drop_database(name)
            client.close()
    return asyncio.run(run())


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


# ---- phone numbers and segments --------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("040 123 4567", "+358401234567"), ("+358 40 123 4567", "+358401234567"), ("00358401234567", "+358401234567"),
    ("+46 70 123 45 67", "+46701234567"), ("12", None), ("", None), (None, None), ("abc", None),
])
def test_normalize_phone(raw, want):
    assert sms.normalize_phone(raw) == want


def test_segments_gsm_and_unicode():
    assert sms.segments("a" * 160) == 1
    assert sms.segments("a" * 161) == 2
    assert sms.segments("Lähetys on toimitettu kohteeseen Hämeenlinna.") == 1  # ä/ö are GSM-7
    assert sms.segments("€" * 80) == 1 and sms.segments("€" * 81) == 2  # € counts twice
    assert sms.segments("Доставлено" * 8) == 2  # Cyrillic = UCS-2, 67 per part


# ---- milestone SMS ----------------------------------------------------------------------------------------------------
def test_milestone_sms_respects_switch_optin_language_and_dedupe(monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "log")

    async def scenario(db):
        for mod in (notify_channels, sms):
            monkeypatch.setattr(mod, "db", db)
        import core.audit as audit_mod
        import core.settings as settings_mod
        monkeypatch.setattr(audit_mod, "db", db)
        monkeypatch.setattr(settings_mod, "db", db)
        await db.users.insert_many([
            {"id": "u-fi", "company_id": "c1", "phone": "040 123 4567", "notify_sms": True, "locale": "fi"},
            {"id": "u-en", "company_id": "c1", "phone": "+44 7700 900123", "notify_sms": True, "locale": "en"},
            {"id": "u-off", "company_id": "c1", "phone": "+358401111111", "notify_sms": False},
            {"id": "u-nophone", "company_id": "c1", "notify_sms": True},
            {"id": "u-other", "company_id": "c2", "phone": "+358402222222", "notify_sms": True},
        ])
        s = {"id": "s1", "company_id": "c1", "reference": "HUO-2026-000057", "destination": {"name": "Tampere"},
             "origin": {"name": "Hamburg"}}
        off = await notify_channels.notify_shipment_sms(s, "delivered")  # switch off by default
        await db.settings.update_one({"key": "app"}, {"$set": {"sms_notifications_enabled": True}}, upsert=True)
        sent = await notify_channels.notify_shipment_sms(s, "delivered")
        again = await notify_channels.notify_shipment_sms(s, "delivered")
        none_for_unknown = await notify_channels.notify_shipment_sms(s, "booked")
        rows = await db.sms_outbox.find({}, {"_id": 0}).to_list(None)
        audits = await db.audit_log.count_documents({"action": "sms.sent"})
        return off, sent, again, none_for_unknown, rows, audits

    off, sent, again, none_for_unknown, rows, audits = _with_db(scenario)
    assert off == [] and again == [] and none_for_unknown == []
    by_user = {r["user_id"]: r for r in rows}
    assert set(by_user) == {"u-fi", "u-en"} and audits == 2
    assert by_user["u-fi"]["to"] == "+358401234567"
    assert by_user["u-fi"]["text"] == "Huolitsija: HUO-2026-000057 on toimitettu kohteeseen Tampere."
    assert by_user["u-en"]["text"] == "Huolitsija: HUO-2026-000057 was delivered to Tampere."
    assert all(r["status"] == "logged" and r["provider"] == "log" for r in rows)


def test_sms_text_covers_every_customer_facing_status():
    s = {"reference": "R1", "destination": {"name": "Oulu"}, "origin": {"name": "Turku"}}
    for status in ("picked_up", "in_transit", "customs_hold", "exception", "out_for_delivery", "delivered"):
        fi, en = notify_channels.sms_text(status, s, "fi"), notify_channels.sms_text(status, s, "en")
        assert fi and en and fi != en and "R1" in fi and "R1" in en
        assert sms.segments(fi) <= 2 and sms.segments(en) <= 2
    assert notify_channels.sms_text("booked", s, "fi") is None


# ---- provider callbacks: signatures checked by the providers' own libraries ------------------------------------------
def test_twilio_signature_matches_official_validator():
    RequestValidator = pytest.importorskip("twilio.request_validator").RequestValidator

    token = "test_auth_token_" + uuid.uuid4().hex
    url = "https://huolitsija-app.emergent.host/api/webhooks/twilio/sms-status"
    params = {"MessageSid": "SM" + "a" * 32, "MessageStatus": "delivered", "To": "+358401234567", "AccountSid": "AC" + "b" * 32}
    ours = channels.twilio_signature(url, params, token)
    assert RequestValidator(token).validate(url, params, ours)
    assert not RequestValidator(token).validate(url, {**params, "MessageStatus": "failed"}, ours)


def test_resend_svix_signature_verified_against_official_library():
    Webhook = pytest.importorskip("svix.webhooks").Webhook

    secret = "whsec_" + __import__("base64").b64encode(os.urandom(24)).decode()
    body = json.dumps({"type": "email.delivered", "created_at": "2026-10-24T19:00:00Z",
                       "data": {"email_id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"}}).encode()
    msg_id, ts = "msg_" + uuid.uuid4().hex, str(int(time.time()))
    from datetime import datetime, timezone
    sig = Webhook(secret).sign(msg_id, datetime.fromtimestamp(int(ts), tz=timezone.utc), body.decode())
    assert channels.svix_verify(secret, msg_id, ts, body, sig)
    assert not channels.svix_verify(secret, msg_id, ts, body + b" ", sig)
    assert not channels.svix_verify(secret, msg_id, str(int(ts) - 3600), body, sig)  # replay window


def test_resend_event_recorded_on_outbox(monkeypatch):
    async def scenario(db):
        monkeypatch.setattr(notify_channels, "db", db)
        await db.email_outbox.insert_one({"id": "e1", "provider": "resend", "provider_id": "re_1", "status": "sent"})
        ok = await notify_channels.record_email_event("re_1", "delivered", "2026-10-24T19:00:00Z", raw={"type": "email.delivered"})
        miss = await notify_channels.record_email_event("re_unknown", "delivered")
        return ok, miss, await db.email_outbox.find_one({"id": "e1"}, {"_id": 0})

    ok, miss, row = _with_db(scenario)
    assert ok and not miss and row["delivery"] == "delivered" and row["delivery_events"][0]["event"] == "delivered"


# ---- SMTP delivery to a real mail server, read back over IMAP --------------------------------------------------------
SMTP_HOST = os.environ.get("S14_SMTP_HOST", "127.0.0.1")


@pytest.mark.skipif(not _port_open(SMTP_HOST, 3025), reason="GreenMail sandbox not running (shared/interfaces.md s3 → s6)")
def test_smtp_email_is_delivered_to_mailbox(monkeypatch):
    from core import email as email_mod

    to = f"customer.{uuid.uuid4().hex[:6]}@customers.sandbox.test"
    subject = f"Toimitettu: HUO-2026-{uuid.uuid4().hex[:6]}"
    smtp = {"host": SMTP_HOST, "port": "3025", "user": "", "password": "", "security": "none"}

    async def scenario(db):
        monkeypatch.setattr(email_mod, "db", db)
        html, text = email_mod.render(subject, ["Lähetys on toimitettu kohteeseen Tampere."])
        return await email_mod.send_email(to, subject, html, text, tag="shipment", provider="smtp", smtp=smtp,
                                          from_addr="Huolitsija <asiakaspalvelu@huolitsija.test>")

    rec = _with_db(scenario)
    assert rec["status"] == "sent" and rec["provider"] == "smtp" and rec["message_id"]
    m = imaplib.IMAP4(SMTP_HOST, 3143)
    m.login(to, "x")
    m.select("INBOX")
    ids = m.search(None, "ALL")[1][0].split()
    assert ids, "no message in the recipient's mailbox"
    raw = m.fetch(ids[-1], "(RFC822)")[1][0][1]
    m.logout()
    got = message_from_bytes(raw)
    assert got["Message-ID"] == rec["message_id"]
    assert subject in str(__import__("email.header", fromlist=["x"]).make_header(
        __import__("email.header", fromlist=["x"]).decode_header(got["Subject"])))


# ---- Twilio REST API, real account (skipped without credentials) ------------------------------------------------------
TW_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TW_FROM = os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_FROM")
PROOF_TO = os.environ.get("S14_SMS_PROOF_TO")  # a verified recipient while the account is on trial


def _twilio(monkeypatch, sender):
    monkeypatch.setattr(config, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", TW_SID)
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TW_TOKEN)
    monkeypatch.setattr(config, "TWILIO_FROM", sender)
    monkeypatch.setattr(config, "TWILIO_MESSAGING_SERVICE_SID", "")
    monkeypatch.setattr(config, "TWILIO_STATUS_CALLBACK_URL", "")


@pytest.mark.skipif(not (TW_SID and TW_TOKEN), reason="TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not provided")
def test_twilio_real_api_rejects_unusable_request(monkeypatch):
    """Authenticated call to Twilio's Messages API: Twilio itself answers with an error code, nothing is delivered."""
    _twilio(monkeypatch, TW_FROM or "+358401234567")

    async def scenario(db):
        monkeypatch.setattr(sms, "db", db)
        return await sms.send_sms("+15005550001", "Huolitsija: invalid recipient check.", tag="test")

    bad = _with_db(scenario)
    assert bad["status"] == "failed" and 400 <= bad["http_status"] < 500 and bad["provider_response"]["code"], bad


@pytest.mark.skipif(not (TW_SID and TW_TOKEN and TW_FROM and PROOF_TO),
                    reason="TWILIO_FROM_NUMBER / S14_SMS_PROOF_TO not set (trial account needs a verified recipient)")
def test_twilio_real_sms_accepted(monkeypatch):
    _twilio(monkeypatch, TW_FROM)

    async def scenario(db):
        monkeypatch.setattr(sms, "db", db)
        return await sms.send_sms(PROOF_TO, "Huolitsija: SMS-ilmoitukset toimivat.", tag="test")

    ok = _with_db(scenario)
    assert ok["status"] in ("queued", "accepted", "sent") and ok["provider_id"].startswith("SM"), ok
