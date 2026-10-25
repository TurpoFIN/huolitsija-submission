"""Gmail demo contracts: mocked Google HTTP, real private Mongo, no live mail."""
import asyncio
import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import AsyncMock

os.environ["MONGO_URL"] = "mongodb://127.0.0.1:27027"
os.environ["DB_NAME"] = "huo_gmail_demo_test_" + uuid.uuid4().hex[:10]
os.environ["EMAIL_PROVIDER"] = "log"

import httpx
import pytest
from core.db import client, db, ensure_indexes
from services.email_channel import dispatch, gmail_demo as gmail

LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    assert db.raw.name.startswith("huo_gmail_demo_test_")
    client._io_loop = LOOP
    for name, value in {"DEMO_AUTOQUOTE_ENABLED": "true", "DEMO_REAL_REQUESTER": "true",
        "DEMO_WORLD_SANDBOX": "true", "DEMO_EMAIL_PROVIDER": "gmail", "DEMO_INBOUND_PROVIDER": "gmail",
        "DEMO_INBOUND_ADDRESS": gmail.ALIAS, "DEMO_GMAIL_FROM": "Huolitsija <huolitsija@heliosone.fi>",
        "HUOLITSIJA_GMAIL_CLIENT_ID": "test-client-id", "HUOLITSIJA_GMAIL_CLIENT_SECRET": "test-client-secret",
        "HUOLITSIJA_GMAIL_REFRESH_TOKEN": "test-refresh-token"}.items():
        monkeypatch.setenv(name, value)
    for name in ("DEMO_GMAIL_ALLOWED_TO", "DEMO_GMAIL_ALLOWED_RECIPIENTS", "DEMO_CAPTURE_RFQS",
                 "DEMO_PARTNER_SANDBOX_ADDRESS", "DEMO_PARTNER_SANDBOX_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    run(ensure_indexes())
    yield
    run(client.drop_database(db.raw.name))


class Google:
    def __init__(self):
        self.requests = []
        self.raw_sent = []
        self.aliases = [{"sendAsEmail": gmail.ALIAS, "verificationStatus": "accepted"}]
        self.alias_status = 200
        self.actual_from = "Huolitsija <huolitsija@heliosone.fi>"
        self.messages = {}
        self.list_ids = []
        self.list_query = None

    def __call__(self, req):
        self.requests.append((req.method, req.url.path))
        path = req.url.path
        if req.method == "POST" and req.url.host == "oauth2.googleapis.com":
            form = dict(httpx.QueryParams(req.content.decode()))
            assert form == {"grant_type": "refresh_token", "client_id": "test-client-id",
                "client_secret": "test-client-secret", "refresh_token": "test-refresh-token"}
            return httpx.Response(200, json={"access_token": "test-access", "scope":
                "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"})
        assert req.headers["authorization"] == "Bearer test-access"
        if req.method == "GET" and path.endswith("/settings/sendAs"):
            return httpx.Response(self.alias_status, json={"sendAs": self.aliases})
        if req.method == "POST" and path.endswith("/messages/send"):
            data = json.loads(req.content)
            self.raw_sent.append(base64.urlsafe_b64decode(data["raw"]))
            return httpx.Response(200, json={"id": "gmail-sent", "threadId": "gmail-thread"})
        if req.method == "GET" and path.endswith("/messages/gmail-sent"):
            assert req.url.params["format"] == "metadata"
            assert set(req.url.params.get_list("metadataHeaders")) == {"Message-ID", "From"}
            return httpx.Response(200, json={"id": "gmail-sent", "payload": {"headers": [
                {"name": "Message-ID", "value": "<gmail-actual@heliosone.fi>"},
                {"name": "From", "value": self.actual_from}]}})
        if req.method == "GET" and path.endswith("/messages"):
            self.list_query = req.url.params["q"]
            return httpx.Response(200, json={"messages": [{"id": mid} for mid in self.list_ids]})
        if req.method == "GET" and "/messages/" in path:
            ident = path.rsplit("/", 1)[1]
            assert req.url.params["format"] == "raw"
            return httpx.Response(200, json=self.messages[ident])
        raise AssertionError(f"Unexpected Google request: {req.method} {path}")

    def add(self, ident, *, reference="<actual-own@heliosone.fi>", sender="owner@example.invalid",
            recipient=gmail.ALIAS, age_seconds=0):
        msg = EmailMessage()
        msg["From"], msg["To"] = sender, recipient
        msg["Delivered-To"] = recipient
        msg["Message-ID"] = f"<{ident}@example.invalid>"
        msg["Subject"] = "Re: freight quote"
        if reference:
            msg["In-Reply-To"] = reference
            msg["References"] = reference
        msg.set_content("The pickup address is provided here.")
        self.messages[ident] = {"id": ident, "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
            "internalDate": str(int((datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).timestamp() * 1000))}
        self.list_ids.append(ident)


async def record(route=None):
    row = {"id": "own-run", "real_requester": True, "requester": {"address": "owner@example.invalid"},
        "gmail_reply_to": route or gmail.reply_to(), "status": "awaiting_requester", "phase": "quote"}
    await db.raw.demo_runs.insert_one(dict(row))
    return row


async def sent_record(route=None):
    row = await record(route)
    await db.raw.demo_gmail_sent.insert_one({"_id": "sent-own", "route": route or gmail.reply_to(),
        "run_id": row["id"], "recipient": row["requester"]["address"],
        "message_id": "<actual-own@heliosone.fi>", "created_wall": datetime.now(timezone.utc) - timedelta(seconds=30)})
    return row


def test_only_fresh_demo_oauth_and_bound_alias_configuration(monkeypatch):
    assert gmail.configured()
    monkeypatch.setenv("DEMO_INBOUND_ADDRESS", "huolitsija+instance-one@heliosone.fi")
    assert gmail.configured()
    monkeypatch.setenv("DEMO_INBOUND_ADDRESS", "unrelated@heliosone.fi")
    assert not gmail.configured()
    monkeypatch.setenv("DEMO_INBOUND_ADDRESS", "huolitsija+tag:subject@heliosone.fi")
    assert not gmail.configured()
    monkeypatch.setenv("DEMO_INBOUND_ADDRESS", gmail.ALIAS)
    monkeypatch.setenv("MAIL_AGENT_REFRESH_TOKEN", "unrelated-must-not-be-read")
    monkeypatch.delenv("HUOLITSIJA_GMAIL_REFRESH_TOKEN")
    assert not gmail.configured()
    monkeypatch.setenv("HUOLITSIJA_GMAIL_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "false")
    assert not gmail.configured()
    assert run(gmail.poll_once()) == {"status": "off"}


def test_send_raw_mime_verified_alias_actual_message_id_and_attachment():
    google = Google()
    async def exercise():
        row = await record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            result = await gmail.send(http, run=row, to="owner@example.invalid", subject="Your freight quote",
                html="<p>Details</p>", text="Details", headers={"Message-ID": "<requested@heliosone.fi>",
                    "In-Reply-To": "<customer@example.invalid>", "References": "<customer@example.invalid>"},
                attachments=[("quote.pdf", "application/pdf", b"%PDF-test")])
        assert result["status"] == "sent" and result["provider_id"] == "gmail-sent"
        assert result["message_id"] == "<gmail-actual@heliosone.fi>"
        assert result["message_ids"] == ["<requested@heliosone.fi>", "<gmail-actual@heliosone.fi>"]
        assert result["reply_to"] == gmail.ALIAS
        saved = await db.raw.demo_gmail_sent.find_one({"message_id": result["message_id"]})
        assert saved["run_id"] == row["id"] and saved["recipient"] == "owner@example.invalid"
    run(exercise())
    raw = BytesParser(policy=policy.default).parsebytes(google.raw_sent[0])
    assert raw["From"] == "Huolitsija <huolitsija@heliosone.fi>"
    assert raw["Reply-To"] == gmail.ALIAS and raw["To"] == "owner@example.invalid"
    assert raw["In-Reply-To"] == "<customer@example.invalid>"
    attachment = next(raw.iter_attachments())
    assert attachment.get_filename() == "quote.pdf" and attachment.get_payload(decode=True) == b"%PDF-test"


@pytest.mark.parametrize("failure", ["forbidden", "missing", "pending", "rewrite"])
def test_alias_or_actual_from_verification_fails_closed(failure):
    google = Google()
    if failure == "forbidden":
        google.alias_status = 403
    elif failure == "missing":
        google.aliases = [{"sendAsEmail": "mail-agent@heliosone.fi", "verificationStatus": "accepted"}]
    elif failure == "pending":
        google.aliases[0]["verificationStatus"] = "pending"
    else:
        google.actual_from = "mail-agent@heliosone.fi"
    async def exercise():
        row = await record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            with pytest.raises(gmail.GmailDemoError):
                await gmail.send(http, run=row, to="owner@example.invalid", subject="Quote", text="Details", html="")
        assert await db.raw.demo_gmail_sent.count_documents({}) == 0
    run(exercise())
    assert len(google.raw_sent) == (1 if failure == "rewrite" else 0)


@pytest.mark.parametrize("failure", ["recipient", "multiple", "allowlist", "bcc"])
def test_requester_lineage_and_recipient_guards_before_any_google_call(monkeypatch, failure):
    google = Google()
    to, headers = "owner@example.invalid", {}
    if failure == "recipient":
        to = "world-carrier@example.invalid"
    elif failure == "multiple":
        to = "owner@example.invalid, world-carrier@example.invalid"
    elif failure == "allowlist":
        monkeypatch.setenv("DEMO_GMAIL_ALLOWED_RECIPIENTS", "other-owner@example.invalid")
    else:
        headers["Bcc"] = "world-carrier@example.invalid"
    async def exercise():
        row = await record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            with pytest.raises(gmail.GmailDemoError):
                await gmail.send(http, run=row, to=to, subject="Quote", text="Details", html="", headers=headers)
    run(exercise())
    assert google.requests == []


def test_shared_alias_only_owned_actual_headers_and_requester_reach_dispatch(monkeypatch):
    google = Google()
    google.add("own")
    google.add("other-instance", reference="<other-demo-actual@heliosone.fi>")
    google.add("forged-requested-only", reference="<client-requested-not-actual@heliosone.fi>")
    google.add("wrong-sender", sender="world-party@example.invalid")
    google.add("wrong-route", recipient="huolitsija+other-instance@heliosone.fi")
    google.add("older-than-send", age_seconds=3600)
    dispatcher = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(dispatch, "dispatch_raw", dispatcher)
    async def exercise():
        await sent_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            result = await gmail.poll_once(http)
            assert result["fetched"] == 1 and result["unmatched"] == 3 and result["ignored"] == 2
            assert result["errors"] == []
            again = await gmail.poll_once(http)
            assert again["fetched"] == 0 and again["duplicates"] == 3
        assert await db.raw.demo_gmail_receipts.count_documents({"status": "done"}) == 1
        assert await db.raw.demo_gmail_receipts.count_documents({"status": "ignored"}) == 2
        assert await db.raw.demo_gmail_receipts.count_documents({}) == 3
        assert await db.raw.demo_gmail_cursors.count_documents({}) == 1
    run(exercise())
    dispatcher.assert_awaited_once()
    assert dispatcher.await_args.kwargs == {"provider_id": "gmail:own", "provider_authenticated": True, "source": "gmail_demo"}
    assert google.list_query.startswith("deliveredto:huolitsija@heliosone.fi after:")
    assert all(method == "GET" or path == "/token" for method, path in google.requests)


def test_failed_dispatch_is_recovered_durably_without_mailbox_mutation(monkeypatch):
    google = Google()
    google.add("reply")
    dispatcher = AsyncMock(side_effect=[RuntimeError("process restart"), {"ok": True}])
    monkeypatch.setattr(dispatch, "dispatch_raw", dispatcher)
    async def exercise():
        await sent_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            first = await gmail.poll_once(http)
            assert len(first["errors"]) == 1 and first["fetched"] == 0
            assert await db.raw.demo_gmail_receipts.count_documents({"status": "retry"}) == 1
            google.list_ids = []  # retry does not depend on unread state or list cursor
            second = await gmail.poll_once(http)
            assert second["fetched"] == 1 and second["errors"] == []
            assert await db.raw.demo_gmail_receipts.count_documents({"status": "done"}) == 1
    run(exercise())
    assert dispatcher.await_count == 2
    assert all(method == "GET" or path == "/token" for method, path in google.requests)


def test_parallel_pollers_dispatch_owned_reply_once(monkeypatch):
    google = Google()
    google.add("reply")
    async def delayed(*args, **kwargs):
        await asyncio.sleep(.03)
        return {"ok": True}
    dispatcher = AsyncMock(side_effect=delayed)
    monkeypatch.setattr(dispatch, "dispatch_raw", dispatcher)
    async def exercise():
        await sent_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            results = await asyncio.gather(gmail.poll_once(http), gmail.poll_once(http))
        assert sum(result["fetched"] for result in results) == 1
        assert await db.raw.demo_gmail_receipts.count_documents({"status": "done"}) == 1
    run(exercise())
    dispatcher.assert_awaited_once()


def test_paginated_cursor_keeps_first_scan_floor_across_delayed_restart(monkeypatch):
    google = Google()
    first_wall = datetime.now(timezone.utc)
    calls = []
    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/messages"):
            calls.append(req.url.params)
            return httpx.Response(200, json={"messages": [], **({"nextPageToken": "next-page"} if len(calls) == 1 else {})})
        return google(req)
    async def exercise():
        await sent_record()
        await db.raw.demo_gmail_sent.update_one({"_id": "sent-own"}, {"$set": {"created_wall": first_wall - timedelta(hours=1)}})
        monkeypatch.setattr(gmail, "_wall", lambda: first_wall)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert (await gmail.poll_once(http))["errors"] == []
            monkeypatch.setattr(gmail, "_wall", lambda: first_wall + timedelta(minutes=10))
            assert (await gmail.poll_once(http))["errors"] == []
        cursor = await db.raw.demo_gmail_cursors.find_one({})
        assert calls[1]["pageToken"] == "next-page"
        assert cursor["after"] == int(first_wall.timestamp()) - 120
        assert cursor["page_token"] is None
    run(exercise())


async def rfq_record():
    from services.demo_autoquote.public_requester import wall_now
    row = await record()
    day = wall_now().date().isoformat()
    active = {"status": "running", "phase": "quote", "rfq_mode": "real", "rfq_day": day,
              "recipients": ["rates@partner.invalid"]}
    await db.raw.demo_runs.update_one({"id": row["id"]}, {"$set": active})
    await db.raw.demo_public_limits.insert_one({"_id": f"rfq:{day}:0", "owner": row["id"]})
    await db.raw.partner_candidates.insert_one({"id": "candidate", "contact_email": "rates@partner.invalid"})
    await db.raw.carriers.insert_one({"id": "carrier", "contact_email": "rates@partner.invalid"})
    await db.raw.partner_outreach.insert_one({"id": "outreach", "demo_run_id": row["id"],
        "candidate_id": "candidate", "carrier_id": "carrier", "contact_email": "rates@partner.invalid"})
    return {**row, **active}, {"type": "demo_run", "id": row["id"], "kind": "rfq", "outreach_id": "outreach"}


def test_capped_native_partner_rfq_and_reply_reach_existing_dispatch(monkeypatch):
    google = Google()
    dispatcher = AsyncMock(return_value={"ok": True, "channel": "partner_network"})
    monkeypatch.setattr(dispatch, "dispatch_raw", dispatcher)
    async def exercise():
        row, ref = await rfq_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            result = await gmail.send(http, run=row, ref=ref, tag="demo_rfq", to="rates@partner.invalid",
                subject="Rate request", html="", text="Please quote this shipment.")
            assert result["status"] == "sent"
            saved = await db.raw.demo_gmail_sent.find_one({"message_id": result["message_id"]})
            assert saved["role"] == "partner" and saved["outreach_id"] == "outreach"
            google.add("partner-reply", reference=result["message_id"], sender="rates@partner.invalid")
            google.add("wrong-partner", reference=result["message_id"], sender="other@partner.invalid")
            polled = await gmail.poll_once(http)
            assert polled["fetched"] == 1 and polled["ignored"] == 1 and polled["errors"] == []
    run(exercise())
    dispatcher.assert_awaited_once()
    assert dispatcher.await_args.kwargs["provider_id"] == "gmail:partner-reply"


def test_partner_quota_cap_day_purpose_native_candidate_and_capture_guards(monkeypatch):
    google = Google()
    async def exercise():
        row, ref = await rfq_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            async def denied(**kw):
                with pytest.raises(gmail.GmailDemoError):
                    await gmail.send(http, run=row, ref=kw.pop("ref", ref), tag=kw.pop("tag", "demo_rfq"),
                        to=kw.pop("to", "rates@partner.invalid"), subject="Rate request", html="", text="Shipment", **kw)
            for changes in ({"rfq_mode": "captured_quota"}, {"rfq_day": "2000-01-01"},
                            {"phase": "lifecycle"}, {"status": "stopped"}, {"recipients": []},
                            {"recipients": ["rates@partner.invalid", *[f"other{i}@partner.invalid" for i in range(8)]]}):
                await db.raw.demo_runs.update_one({"id": row["id"]}, {"$set": changes})
                await denied()
                await db.raw.demo_runs.update_one({"id": row["id"]}, {"$set": {k: row[k] for k in changes}})
            await db.raw.demo_public_limits.update_many({}, {"$set": {"owner": "other-run"}})
            await denied()
            await db.raw.demo_public_limits.update_many({}, {"$set": {"owner": row["id"]}})
            await denied(tag="carrier_booking")
            await denied(ref={**ref, "outreach_id": "other-outreach"})
            for collection in ("partner_candidates", "carriers"):
                await db.raw[collection].update_many({}, {"$set": {"contact_email": "other@partner.invalid"}})
                await denied()
                await db.raw[collection].update_many({}, {"$set": {"contact_email": "rates@partner.invalid"}})
            monkeypatch.setenv("DEMO_CAPTURE_RFQS", "true")
            await denied()
            monkeypatch.delenv("DEMO_CAPTURE_RFQS")
            # A requester address on an RFQ tag cannot bypass partner quotas.
            await denied(to="owner@example.invalid")
        assert google.requests == []
    run(exercise())


def test_partner_sandbox_requires_exact_original_contact_independently_of_requester_allowlist(monkeypatch):
    google = Google()
    monkeypatch.setenv("DEMO_PARTNER_SANDBOX_ADDRESS", "rehearsal@example.invalid")
    monkeypatch.setenv("DEMO_GMAIL_ALLOWED_TO", "owner@example.invalid")
    async def exercise():
        row, ref = await rfq_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
            with pytest.raises(gmail.GmailDemoError):
                await gmail.send(http, run=row, ref=ref, tag="demo_rfq", to="rehearsal@example.invalid",
                    subject="Rate request", html="", text="Shipment", headers={"X-Huolitsija-Original-To": "other@partner.invalid"})
            result = await gmail.send(http, run=row, ref=ref, tag="demo_rfq", to="rehearsal@example.invalid",
                subject="Rate request", html="", text="Shipment", headers={"X-Huolitsija-Original-To": "rates@partner.invalid"})
            assert result["status"] == "sent"
        saved = await db.raw.demo_gmail_sent.find_one({})
        assert saved["recipient"] == "rehearsal@example.invalid" and saved["contact"] == "rates@partner.invalid"
    run(exercise())


def test_partner_gate_is_rechecked_after_oauth_before_send():
    google = Google()
    async def handler(request):
        if request.url.path.endswith("/settings/sendAs"):
            await db.raw.demo_runs.update_one({"id": "own-run"}, {"$set": {"status": "stopped"}})
        return google(request)
    async def exercise():
        row, ref = await rfq_record()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(gmail.GmailDemoError):
                await gmail.send(http, run=row, ref=ref, tag="demo_rfq", to="rates@partner.invalid",
                    subject="Rate request", html="", text="Shipment")
        assert google.raw_sent == []
    run(exercise())
