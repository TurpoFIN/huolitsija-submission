"""Public demo boundaries against private Mongo; no real mail, LLM or production DB."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import AsyncMock

os.environ["DB_NAME"] = "huo_pubdemo_test_" + uuid.uuid4().hex[:10]
os.environ["EMAIL_PROVIDER"] = "log"

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from core import config, email as core_email
from core.db import client, db, ensure_indexes
from core.util import now_iso
from routes.demo_public import router
from services.demo_autoquote import driver, mail, requester
from services.demo_autoquote import public_requester as admission
from services.demo_autoquote.payment import payment_state, simulate_payment
from services.demo_world import boundary, engine, observer
from services.email_channel import inbound, store, flow


def run(coro):
    return LOOP.run_until_complete(coro)


def test_explicit_proof_mailbox_domain_allowlist(monkeypatch):
    monkeypatch.setenv("DEMO_REQUESTER_ALLOW_DOMAINS", "maxxspace.com")
    assert admission.email_address("proof@maxxspace.com") == "proof@maxxspace.com"
    with pytest.raises(HTTPException):
        admission.email_address("proof@sub.maxxspace.com")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    assert db.raw.name.startswith("huo_pubdemo_test_")
    assert config.MONGO_URL.startswith("mongodb://127.0.0.1:27027")
    for name, value in {"DEMO_AUTOQUOTE_ENABLED": "true", "DEMO_REAL_REQUESTER": "1", "DEMO_WORLD_SANDBOX": "true",
        "DEMO_INBOUND_ADDRESS": "inbound@demo.invalid", "MAILTM_ADDRESS": "inbound@demo.invalid",
        "MAILTM_PASSWORD": "test-only", "DEMO_REQUESTER_ADDRESS": "old-requester@demo.invalid",
        "DEMO_EMAIL_FROM": "Verified sender <sender@demo.invalid>"}.items():
        monkeypatch.setenv(name, value)
    run(ensure_indexes())
    boundary._state.clear()
    yield
    for task in list(driver._tasks.values()):
        if not task.done():
            task.cancel()
    run(asyncio.sleep(0))
    driver._tasks.clear()
    run(client.drop_database(db.raw.name))


async def record(ident="run", **kw):
    row = {"id": ident, "ref": "DQ-" + ident, "real_requester": True, "requester": {"address": "owner@customer.invalid"},
        "status": "running", "phase": "quote", "event_seq": 0, "seed": 1, "created_at": now_iso(),
        "created_real": datetime.now(timezone.utc).isoformat(), "request_text": "Helsinki to Stockholm, steel",
        "origin_text": "Helsinki", "destination_text": "Stockholm", "profile": {}, "recipients": [], **kw}
    await db.raw.demo_runs.insert_one(dict(row))
    return row


def test_daily_atomic_quota_and_third_run_captures_rfq(monkeypatch):
    async def exercise():
        runs = [await record(str(i)) for i in range(12)]
        modes = await asyncio.gather(*(admission.rfq_mode(row) for row in runs))
        assert sum(m["rfq_mode"] == "real" for m in modes) == 2
        for row, mode in zip(runs, modes):
            await db.raw.demo_runs.update_one({"id": row["id"]}, {"$set": {**mode, "recipients": ["partner@external.invalid"]}})
            captured = await boundary.capture_mail(to="partner@external.invalid", subject="Rate request", text="Cargo",
                tag="demo_rfq", ref={"type": "demo_run", "id": row["id"], "kind": "rfq"})
            assert (captured is None) == (mode["rfq_mode"] == "real")
        third = await db.raw.demo_runs.find_one({"rfq_mode": "captured_quota"})
        assert third and await db.raw.world_mail.find_one({"to": "partner@external.invalid"})
        monkeypatch.setenv("DEMO_CAPTURE_RFQS", "1")
        assert (await admission.rfq_mode(await record("quote-only")))["rfq_capture_reason"] == "quote_only_gate"
    run(exercise())








def test_each_customer_mail_uses_smtp2go_and_dedicated_reply_to(monkeypatch):
    requests = []
    def provider(request):
        import json
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={"data": {"succeeded": 1, "failed": 0, "email_id": f"provider-{len(requests)}"}})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw))
    monkeypatch.setattr(config, "SMTP2GO_API_KEY", "private-test-only")
    async def exercise():
        await record()
        for tag in ("email_need_info", "email_quote", "booking_confirmation", "email_update", "invoice_issued"):
            sent = await core_email.send_email("owner@customer.invalid", "Your shipment", "<p>Shipment</p>", "Shipment",
                provider="log", tag=tag, ref={"type": "demo_run", "id": "run"}, headers={"Message-ID": f"<{tag}@demo.invalid>"})
            assert sent["status"] == "sent" and sent["provider"] == "smtp2go"
            row = await db.raw.email_outbox.find_one({"id": sent["id"]})
            assert row["message_ids"][-1].endswith("@message-id.smtpcorp.com>")
        assert len(requests) == 5
        for payload in requests:
            assert payload["to"] == ["owner@customer.invalid"]
            assert {h["header"]: h["value"] for h in payload["custom_headers"]}["Reply-To"] == "inbound@demo.invalid"
        world = await core_email.send_email("carrier@external.invalid", "Booking", "", "Book", provider="smtp2go",
            tag="carrier_booking", ref={"type": "demo_run", "id": "run"})
        assert world["provider"] == "world" and len(requests) == 5
    run(exercise())


def test_resend_demo_sender_headers_recipient_limit_and_independent_inbound(monkeypatch):
    import base64
    import hashlib
    import hmac
    import json
    import time
    from routes import channels
    from services.demo_autoquote import resend as receipts
    from services.demo_autoquote import conf
    from services.email_channel import mailtm, mailslurp
    calls = []
    def provider(request):
        import json
        assert str(request.url) == "https://api.resend.com/emails"
        calls.append((request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(201, json={"id": f"resend-{len(calls)}"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw))
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "preferred-test-key")
    monkeypatch.setenv("HUOLITSIJA_DEMO_RESEND_API_KEY", "fallback-test-key")
    monkeypatch.setenv("DEMO_RESEND_ALLOWED_TO", "owner@customer.invalid")
    monkeypatch.delenv("DEMO_RESEND_FROM", raising=False)
    monkeypatch.setenv("DEMO_MAILSLURP_INBOX_ID", "demo-inbox")
    monkeypatch.setenv("HUOLITSIJA_DEMO_MAILSLURP_API_KEY", "inbound-test-key")
    assert mailslurp.configured() and not mailtm.configured()
    monkeypatch.setenv("DEMO_INBOUND_PROVIDER", "mailtm")
    assert conf.real_requester_ready() and mailtm.configured() and not mailslurp.configured()
    monkeypatch.delenv("DEMO_INBOUND_PROVIDER")
    signing_key = b"private-test-webhook-signing-key"
    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(signing_key).decode())
    callback_app = FastAPI()
    callback_app.include_router(channels.router)
    headers = {"Message-ID": "<resend@demo.invalid>", "In-Reply-To": "<request@customer.invalid>",
               "References": "<root@customer.invalid> <request@customer.invalid>"}
    async def exercise():
        await record(thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid"})
        await db.raw.email_messages.insert_one({"id": "outgoing", "direction": "out", "thread_id": "thread"})
        for index, key in enumerate(("preferred-test-key", "fallback-test-key")):
            if index:
                monkeypatch.delenv("RESEND_API_KEY")
                monkeypatch.setenv("DEMO_RESEND_FROM", "Demo <verified@demo.invalid>")
            sent = await core_email.send_email("owner@customer.invalid", "Quote", "<p>Quote</p>", "Quote", provider="log",
                tag="email_quote", headers=headers, ref={"type": "demo_run", "id": "run", "email_message_id": "outgoing"})
            assert sent["status"] == "accepted" and sent["provider"] == "resend" and sent["provider_id"] == f"resend-{index + 1}"
            token, payload = calls[-1]
            assert token == f"Bearer {key}"
            assert payload["from"] == ("Demo <verified@demo.invalid>" if index else "onboarding@resend.dev")
            assert payload["reply_to"] == "inbound@demo.invalid" and payload["to"] == ["owner@customer.invalid"]
            assert payload["headers"] == headers
            stored = await db.raw.email_outbox.find_one({"id": sent["id"]})
            assert stored["provider_id"] == sent["provider_id"] and not stored.get("provider_sent_at")
            body = json.dumps({"type": "email.sent", "data": {"email_id": sent["provider_id"],
                              "message_id": f"<ses-{index}@provider.invalid>"}}).encode()
            timestamp, event_id = str(int(time.time())), f"signed-event-{index}"
            signature = base64.b64encode(hmac.new(signing_key, f"{event_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
            callback_headers = {"svix-id": event_id, "svix-timestamp": timestamp, "svix-signature": "v1," + signature,
                                "Content-Type": "application/json"}
            async with original_client(transport=httpx.ASGITransport(app=callback_app), base_url="http://demo") as callback:
                bad = await callback.post("/api/webhooks/resend", content=body,
                    headers={**callback_headers, "svix-signature": "v1,invalid"})
                assert bad.status_code == 401
                unsigned = await callback.post("/api/webhooks/resend", content=body)
                assert unsigned.status_code == 401
                stale = await callback.post("/api/webhooks/resend", content=body,
                    headers={**callback_headers, "svix-timestamp": str(int(time.time()) - 3600)})
                assert stale.status_code == 401
                assert (await db.raw.email_outbox.find_one({"id": sent["id"]}))["status"] == "accepted"
                valid = await callback.post("/api/webhooks/resend", content=body, headers=callback_headers)
                assert valid.status_code == 200 and valid.json()["matched"]
            stored = await db.raw.email_outbox.find_one({"id": sent["id"]})
            assert stored["status"] == "sent" and f"<ses-{index}@provider.invalid>" in stored["message_ids"]
        thread, how = await store.find_thread_for_inbound({"in_reply_to": "<ses-1@provider.invalid>"})
        assert how == "header" and thread["id"] == "thread"
        await db.raw.email_messages.insert_one({"id": "provider-reply", "direction": "in", "from_email": "owner@customer.invalid",
            "in_reply_to": "<ses-1@provider.invalid>", "references": [], "automatic": False})
        reply = await driver._requester_reply(await db.raw.demo_runs.find_one({"id": "run"}), ["<resend@demo.invalid>"])
        assert reply["id"] == "provider-reply"
        world = await core_email.send_email("carrier@external.invalid", "Booking", "", "Book", provider="resend",
            tag="carrier_booking", ref={"type": "demo_run", "id": "run"})
        assert world["provider"] == "world" and len(calls) == 2
        monkeypatch.setenv("DEMO_RESEND_ALLOWED_TO", "somebody-else@customer.invalid")
        blocked = await core_email.send_email("owner@customer.invalid", "Quote", "", "Quote", tag="email_quote",
            headers=headers, ref={"type": "demo_run", "id": "run"})
        assert blocked["status"] == "failed" and "DEMO_RESEND_ALLOWED_TO" in blocked["error"] and len(calls) == 2
        row = await db.raw.demo_runs.find_one({"id": "run"})
        assert row["internal_transport_events"][-1]["reason"] == "recipient_allowlist"
        monkeypatch.delenv("DEMO_RESEND_ALLOWED_TO")
        assert not await receipts.record_sent({"type": "email.sent", "data": {
            "email_id": "resend-3", "message_id": "<early@provider.invalid>"}})
        allowed = await core_email.send_email("owner@customer.invalid", "Quote", "", "Quote", tag="email_quote",
            headers=headers, ref={"type": "demo_run", "id": "run"})
        assert allowed["status"] == "sent" and len(calls) == 3 and "<early@provider.invalid>" in allowed["message_ids"]
    run(exercise())


def test_mailslurp_send_keeps_actual_message_id_and_requester_boundary(monkeypatch):
    from services.demo_autoquote import conf
    from services.email_channel import mailtm
    calls = []
    def provider(request):
        import json
        assert request.headers["x-api-key"] == "demo-test-only"
        calls.append(request)
        if request.url.path == "/inboxes/demo-inbox" and request.method == "GET":
            return httpx.Response(200, json={"emailAddress": "inbound@demo.invalid"})
        if request.url.path == "/attachments":
            assert json.loads(request.content)["filename"] == "quote.pdf"
            return httpx.Response(201, json=["attachment-id"])
        assert request.url.path == "/inboxes/demo-inbox/confirm"
        payload = json.loads(request.content)
        assert payload["from"] == payload["replyTo"] == "inbound@demo.invalid"
        assert payload["to"] == ["owner@customer.invalid"]
        assert payload["customHeaders"] == {"Message-ID": "<requested@demo.invalid>",
            "In-Reply-To": "<request@customer.invalid>", "References": "<root@customer.invalid>"}
        assert payload["attachments"] == ["attachment-id"]
        return httpx.Response(201, json={"id": "sent-entity-id", "messageId": "actual@provider.invalid"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw))
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "mailslurp")
    monkeypatch.setenv("DEMO_MAILSLURP_INBOX_ID", "demo-inbox")
    monkeypatch.setenv("HUOLITSIJA_DEMO_MAILSLURP_API_KEY", "demo-test-only")
    monkeypatch.setenv("MAILTM_ADDRESS", "unrelated@old.invalid")
    monkeypatch.delenv("MAILTM_PASSWORD")
    assert conf.real_requester_ready() and not mailtm.configured()
    async def exercise():
        await record(thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid"})
        await db.raw.email_messages.insert_one({"id": "outgoing", "direction": "out", "thread_id": "thread"})
        sent = await core_email.send_email("owner@customer.invalid", "Quote", "<p>Quote</p>", "Quote",
            tag="email_quote", ref={"type": "demo_run", "id": "run", "email_message_id": "outgoing"},
            headers={"Message-ID": "<requested@demo.invalid>", "In-Reply-To": "<request@customer.invalid>",
                     "References": "<root@customer.invalid>"}, attachments=[("quote.pdf", "application/pdf", b"pdf")])
        assert sent["status"] == "sent" and sent["provider"] == "mailslurp"
        assert sent["provider_id"] == "sent-entity-id" and sent["message_id"] == "<actual@provider.invalid>"
        stored = await db.raw.email_outbox.find_one({"id": sent["id"]})
        assert stored["message_ids"] == ["<requested@demo.invalid>", "<actual@provider.invalid>"]
        thread, how = await store.find_thread_for_inbound({"in_reply_to": "<actual@provider.invalid>"})
        assert how == "header" and thread["id"] == "thread"
        world = await core_email.send_email("carrier@external.invalid", "Booking", "", "Book", provider="mailslurp",
            tag="carrier_booking", ref={"type": "demo_run", "id": "run"})
        assert world["provider"] == "world" and len(calls) == 3
    run(exercise())


def test_mailslurp_poll_uses_raw_inbound_and_existing_sender_header_guards(monkeypatch):
    from services.email_channel import mailslurp
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "mailslurp")
    monkeypatch.setenv("DEMO_MAILSLURP_INBOX_ID", "demo-inbox")
    monkeypatch.setenv("HUOLITSIJA_DEMO_MAILSLURP_API_KEY", "demo-test-only")
    raw = {}
    for ident, sender, reference in (("owner", "owner@customer.invalid", True),
                                    ("other", "other@customer.invalid", True),
                                    ("subject", "owner@customer.invalid", False)):
        message = EmailMessage()
        message["From"], message["To"] = sender, "inbound@demo.invalid"
        message["Message-ID"], message["Subject"] = f"<{ident}@customer.invalid>", "Re: Quote [Q-2026-000123]"
        if reference:
            message["In-Reply-To"] = "<actual@provider.invalid>"
            message["References"] = "<requested@demo.invalid> <actual@provider.invalid>"
        message.set_content("Please answer my question.")
        raw[ident] = message.as_bytes()
    marked = []
    def provider(request):
        assert request.headers["x-api-key"] == "demo-test-only"
        if request.url.path == "/inboxes/demo-inbox":
            return httpx.Response(200, json={"emailAddress": "inbound@demo.invalid"})
        if request.url.path.endswith("/emails"):
            assert request.url.params["unreadOnly"] == "true"
            return httpx.Response(200, json=[{"id": ident} for ident in raw])
        ident = request.url.path.split("/")[2]
        if request.url.path.endswith("/raw"):
            return httpx.Response(200, content=raw[ident])
        assert request.method == "PATCH" and request.url.params["read"] == "true"
        marked.append(ident)
        return httpx.Response(200, json={})
    async def exercise():
        await record(thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "demo_run_id": "run", "ref": "Q-2026-000123",
                                              "customer_email": "owner@customer.invalid"})
        await db.raw.email_messages.insert_one({"id": "outgoing", "direction": "out", "thread_id": "thread",
            "message_id": "<requested@demo.invalid>", "provider_message_ids": ["<actual@provider.invalid>"]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            result = await mailslurp.poll_once(transport)
            assert result["status"] == "ok" and result["fetched"] == 3 and not result["errors"]
            for ident in raw:
                row = await db.raw.email_messages.find_one({"message_id": f"<{ident}@customer.invalid>"})
                assert row["source"] == "mailslurp" and await admission.handle_reply(row)
            owner = await db.raw.email_messages.find_one({"message_id": "<owner@customer.invalid>"})
            other = await db.raw.email_messages.find_one({"message_id": "<other@customer.invalid>"})
            subject = await db.raw.email_messages.find_one({"message_id": "<subject@customer.invalid>"})
            assert owner["status"] == "demo_waiting"
            assert other["decision"] == "demo_requester_sender_mismatch"
            assert subject["decision"] == "demo_requester_thread_mismatch"
            await mailslurp.poll_once(transport)
            assert await db.raw.email_messages.count_documents({"direction": "in"}) == 3
            monkeypatch.delenv("DEMO_REAL_REQUESTER")
            assert await mailslurp.poll_once(transport) == {"status": "off"}
        assert marked == ["owner", "other", "subject"] * 2
    run(exercise())








def test_late_reply_resumes_after_72_hours_and_other_sender_is_ignored(monkeypatch):
    main = AsyncMock()
    monkeypatch.setattr(driver, "_main", main)
    async def exercise():
        row = await record(status="awaiting_requester", pending_requester_ids=["<question@demo.invalid>"],
                           created_real=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(), thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid", "demo_run_id": "run",
                                              "ref": "Q-2026-000123"})
        await db.raw.email_messages.insert_one({"id": "question", "direction": "out", "message_id": "<question@demo.invalid>",
            "provider_message_ids": ["<smtp-provider@message-id.smtpcorp.com>"], "thread_id": "thread", "created_at": now_iso()})
        subject_only = {"id": "subject-only", "direction": "in", "from_email": "owner@customer.invalid",
                        "subject": "Re: [Q-2026-000123] BOOK 1", "references": [], "in_reply_to": None}
        await db.raw.email_messages.insert_one(dict(subject_only))
        assert await admission.handle_reply(subject_only)
        ignored = await db.raw.email_messages.find_one({"id": "subject-only"})
        assert ignored["decision"] == "demo_requester_thread_mismatch" and ignored["status"] == "ignored"
        assert not main.called
        monkeypatch.setenv("DEMO_REAL_REQUESTER", "0")
        assert await admission.handle_reply(subject_only) is False
        assert await admission.resume_waiting("run") == 0
        await record("deferred", status="deferred")
        assert (await admission.resume_job({}))["resumed"] == 0
        assert not main.called
        monkeypatch.setenv("DEMO_REAL_REQUESTER", "1")
        for sender, mid in (("intruder@external.invalid", "intruder"), ("owner@customer.invalid", "owner")):
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, "inbound@demo.invalid", "Re: Your quote"
            msg["Message-ID"] = f"<{mid}@test.invalid>"
            msg["In-Reply-To"] = "<smtp-provider@message-id.smtpcorp.com>"
            msg["References"] = "<question@demo.invalid> <smtp-provider@message-id.smtpcorp.com>"
            msg.set_content("The gross weight is 400kg.")
            incoming, _ = await inbound.ingest(msg.as_bytes(), source="pubdemo-test-mailbox")
            await flow.process(incoming)
            if mid == "intruder":
                ignored = await db.raw.email_messages.find_one({"id": incoming["id"]})
                assert ignored["status"] == "ignored" and ignored["decision"] == "demo_requester_sender_mismatch"
                assert (await db.raw.demo_runs.find_one({"id": "run"}))["status"] == "awaiting_requester"
                assert not main.called
        await asyncio.sleep(0)
        assert main.call_count == 1
        reply = await driver._await_requester("run", row["pending_requester_ids"])
        assert reply["from_email"] == "owner@customer.invalid"
        assert (await db.raw.email_messages.find_one({"id": reply["id"]}))["demo_consumed"]
        assert await admission.resume_waiting("run") == 0
    run(exercise())


def test_public_api_existing_notify_entry_capability_identity_and_locale(monkeypatch):
    from routes import public_quote as public
    from routes import demo_public
    monkeypatch.setattr(driver, "_main", AsyncMock())
    monkeypatch.setattr(demo_public, "acknowledge", AsyncMock())
    monkeypatch.setattr(driver, "_world_identity", AsyncMock(side_effect=AssertionError("No invented real requester identity")))
    req = {"origin": {"name": "Helsinki", "country": "FI"}, "destination": {"name": "Stockholm", "country": "SE"},
           "cargo": {"packages": 2, "weight_kg": 400}}
    monkeypatch.setattr(public, "_price", AsyncMock(return_value={"request": req, "options": [], "not_quoted": [],
        "warnings": [], "valid_until": "2026-11-30"}))
    app = FastAPI()
    app.include_router(router)
    app.include_router(public.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post("/api/public/quote", json=req)
            assert response.status_code == 200, response.text
            quote = response.json()
            assert quote["quote"]["sourcing"]["status"] == "requested"
            assert await db.raw.demo_runs.count_documents({}) == 0
            notify = f"/api/public/quote/{quote['quote']['id']}/notify"
            response = await api.post(notify, json={"token": quote["claim_token"], "email": "Owner@GMAIL.COM", "locale": "fi"})
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt["track_url"].endswith(f"/track/{receipt['run_id']}?t={receipt['access_token']}")
            assert receipt["status"] == "running"
            row = await db.raw.demo_runs.find_one({"id": receipt["run_id"]})
            assert row["real_requester"] and row["locale"] == "fi" and row["requester"]["address"] == "owner@gmail.com"
            assert "requester_registry" not in row and receipt["access_token"] not in str(row)
            again = await api.post(notify, json={"token": quote["claim_token"], "email": "owner@gmail.com", "locale": "fi"})
            assert again.json()["run_id"] == receipt["run_id"] and await db.raw.demo_runs.count_documents({}) == 1
            route = f"/api/demo/public/runs/{receipt['run_id']}"
            assert (await api.get(route)).status_code == 404
            assert (await api.get(route + "?access_token=wrong")).status_code == 404
            valid = await api.get(route + "?access_token=" + receipt["access_token"])
            assert valid.status_code == 200 and set(valid.json()) == {"run", "events", "sim_now"}
            assert valid.json()["run"]["payment"] == {"available": False, "status": "none", "invoice_id": None, "invoice_number": None}
            assert "access_token_hash" not in valid.text and "rfq_mode" not in valid.text
            assert (await api.post("/api/demo/public/runs", json={})).status_code in (404, 405)
    run(exercise())


@pytest.mark.parametrize("legacy_wait", [False, True])
def test_reply_resumes_clarification_without_replaying_intake(monkeypatch, legacy_wait):
    import copy
    from services.demo_autoquote import clarification_ack
    class ReachedPricing(Exception):
        pass
    initial = {"request": {"origin": {"name": "Helsinki", "country": "FI"},
        "destination": {"name": "Stockholm", "country": "SE"}, "mode": "road", "incoterm": "DAP",
        "ready_date": "2026-11-04", "cargo": {"description": "Steel parts", "packages": 2, "volume_m3": 2}}, "missing": []}
    final = copy.deepcopy(initial)
    final["request"]["cargo"]["weight_kg"] = 400
    extras = {"pickup_address": "Pickup street 1", "delivery_address": "Delivery street 2", "dangerous_goods_stated": True}
    parsed = AsyncMock(side_effect=[(initial, extras, {}), (final, extras, {})])
    clarify = AsyncMock(return_value=("Question", ["<question@demo.invalid>"]))
    research = AsyncMock(return_value=[])
    ack = AsyncMock()
    monkeypatch.setattr(driver, "_parse", parsed)
    monkeypatch.setattr(driver, "_clarify", clarify)
    monkeypatch.setattr(driver, "_research", research)
    monkeypatch.setattr(clarification_ack, "complete", ack)
    monkeypatch.setattr(driver, "_reference", AsyncMock(side_effect=ReachedPricing))
    async def resumed_main(run_id):
        with pytest.raises(ReachedPricing):
            await driver.quote_phase(run_id)
    monkeypatch.setattr(driver, "_main", resumed_main)
    async def exercise():
        await record(thread_id="thread")
        await driver.quote_phase("run")
        waiting = await db.raw.demo_runs.find_one({"id": "run"})
        assert waiting["status"] == "awaiting_requester" and waiting["clarification_checkpoint"]["missing"] == ["weight_kg"]
        assert parsed.call_count == clarify.call_count == 1 and not research.called
        if legacy_wait:
            await db.raw.demo_runs.update_one({"id": "run"}, {"$unset": {"clarification_checkpoint": ""}})
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid", "demo_run_id": "run"})
        await db.raw.email_messages.insert_one({"id": "question", "thread_id": "thread", "direction": "out",
            "message_id": "<question@demo.invalid>", "provider_message_ids": ["<SES-question@provider.invalid>"]})
        await db.raw.email_outbox.insert_one({"id": "sent-question", "demo_run_id": "run",
            "message_ids": ["<question@demo.invalid>", "<SES-question@provider.invalid>"]})
        reply = {"id": "answer", "direction": "in", "from_email": "owner@customer.invalid", "automatic": False,
            "message_id": "<answer@customer.invalid>", "in_reply_to": "<SES-question@provider.invalid>",
            "references": [], "text": "The gross weight is 400 kg."}
        await db.raw.email_messages.insert_one(dict(reply))
        assert await admission.handle_reply(reply)
        await driver._tasks["run"]
        await asyncio.sleep(0)
        titles = [event["title"] async for event in db.raw.demo_run_events.find({"run_id": "run"}).sort("seq", 1)]
        assert titles.count("Quote request typed") == titles.count("Reading the request") == 1
        assert sum(title.startswith("Request understood") for title in titles) == 1
        assert titles.count("Writing questions to the requester") == 1
        assert titles.index("Reply received · reading it") < titles.index("Details complete")
        assert parsed.call_count == 2 and "400 kg" in parsed.call_args.args[1]
        assert clarify.call_count == 1 and research.call_count == 1
        ack.assert_awaited_once_with("run", 1)
        assert (await db.raw.email_messages.find_one({"id": "answer"}))["demo_consumed"]
    run(exercise())


def test_captured_quota_rfq_cannot_escape_via_partner_sandbox(monkeypatch):
    from services.demo_autoquote import simulator
    from services import partner_network as pn
    monkeypatch.setenv("DEMO_PARTNER_SANDBOX_ADDRESS", "partners@demo.invalid")
    monkeypatch.setenv("DEMO_PARTNER_SANDBOX_PASSWORD", "test-only")
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("EMAIL_CHANNEL_PROVIDER", "resend")
    monkeypatch.setattr(config, "RESEND_API_KEY", "configured-private-test-key")
    def forbidden_http(request):
        raise AssertionError("Captured quota RFQ must not contact a provider")
    original_client = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(forbidden_http), **kw))
    monkeypatch.setattr(driver, "_native_partner_outreach", AsyncMock(side_effect=lambda req, mode, row: row))
    monkeypatch.setattr(simulator, "schedule_first_reply", lambda *args, **kwargs: None)
    monkeypatch.setattr(simulator, "llm_text", AsyncMock(return_value="EUR 730 all-in, transit four days, valid until 2026-12-01."))
    monkeypatch.setattr(pn, "parse_reply", AsyncMock(return_value={"intent": "offer", "status": "ok",
        "cost_cents": 73000, "transit_days": 4, "valid_to": "2026-12-01", "method": "controlled-model"}))
    req = {"origin": {"name": "Helsinki", "country": "FI"}, "destination": {"name": "Stockholm", "country": "SE"},
        "cargo": {"description": "Steel parts", "packages": 2, "weight_kg": 400, "volume_m3": 2},
        "incoterm": "DAP", "ready_date": "2026-11-04"}
    async def exercise():
        await record(rfq_mode="captured_quota")
        row = await driver._send_rfq("run", {"name": "Controlled Carrier", "contact_email": "carrier@world.invalid",
            "domain": "world.invalid", "country": "FI", "role": "carrier"}, req, {}, "road", {"cost_cents": 73000})
        sent = await db.raw.partner_messages.find_one({"outreach_id": row["id"], "direction": "out"})
        assert sent["provider"] == "world" and sent["status"] == "sent"
        assert await db.raw.world_mail.count_documents({"run_id": "run", "to": "partners@demo.invalid"}) == 1
        delivered = await simulator.deliver(await db.raw.demo_runs.find_one({"id": "run"}), row,
            turn=0, behaviour="quote", n={"total_cents": 73000, "transit_days": 4, "valid_to": "2026-12-01"},
            follow_up=None, subject=row["rfq_subject"], in_reply_to=sent["message_id"], references=sent["message_ids"], delay=0)
        assert delivered and delivered["dispatch"]["channel"] == "partner_network"
        queued = await db.raw.jobs.find_one({"id": delivered["dispatch"]["job_id"]})
        await pn.process_inbound_job(queued)
        incoming = await db.raw.partner_messages.find_one({"outreach_id": row["id"], "direction": "in"})
        assert incoming["provider_id"].startswith("demo-sim:") and incoming["parsed"]["cost_cents"] == 73000
    run(exercise())


def test_failed_rfq_is_failed_in_source_events_and_collector(monkeypatch):
    from services.demo_autoquote import simulator
    monkeypatch.setenv("DEMO_PARTNER_SANDBOX_ADDRESS", "partners@demo.invalid")
    monkeypatch.setenv("DEMO_PARTNER_SANDBOX_PASSWORD", "test-only")
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(config, "RESEND_API_KEY", "private-test-only")
    original = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"message": "rejected test recipient"})), **kw))
    monkeypatch.setattr(driver, "_native_partner_outreach", AsyncMock(side_effect=lambda req, mode, row: row))
    scheduled = []
    monkeypatch.setattr(simulator, "schedule_first_reply", lambda *args, **kw: scheduled.append(args))
    req = {"origin": {"name": "Helsinki", "country": "FI"}, "destination": {"name": "Stockholm", "country": "SE"},
        "cargo": {"description": "Steel", "packages": 2, "weight_kg": 400, "volume_m3": 2}, "incoterm": "DAP", "ready_date": "2026-11-04"}
    async def exercise():
        await record(rfq_mode="real")
        row = await driver._send_rfq("run", {"name": "Controlled Carrier", "contact_email": "carrier@world.invalid",
            "domain": "world.invalid", "country": "FI", "role": "carrier"}, req, {}, "road", {})
        assert row["status"] == "email_failed" and not scheduled
        assert (await db.raw.partner_outreach.find_one({"id": row["id"]}))["status"] == "email_failed"
        message = await db.raw.partner_messages.find_one({"outreach_id": row["id"], "direction": "out"})
        assert message["status"] == "failed"
        event = await db.raw.demo_run_events.find_one({"node": "p:" + row["id"], "kind": "email_out"})
        assert event["title"] == "Rate request failed" and event["state"] == "failed"
        assert event["data"]["emails"][0]["status"] == "failed"
        state = {}
        import time
        await asyncio.wait_for(driver._collect("run", [row], req, {}, {}, time.monotonic() + 60, {}, state), timeout=1)
        assert state["final"][row["id"]] == "send_failed"
    run(exercise())


@pytest.mark.parametrize("locale,transport", [("en", "resend"), ("fi", "resend"), ("en", "gmail"), ("fi", "gmail")])
def test_clarification_complete_ack_is_threaded_durable_once_and_localized(monkeypatch, locale, transport):
    from services.demo_autoquote import clarification_ack, resend as receipts
    calls = []
    def provider(request):
        import json
        if transport == "gmail":
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "private-test-access"})
            if request.url.path.endswith("/settings/sendAs"):
                return httpx.Response(200, json={"sendAs": [{"sendAsEmail": "huolitsija@heliosone.fi", "verificationStatus": "accepted"}]})
            if request.method == "GET":
                return httpx.Response(200, json={"payload": {"headers": [{"name": "Message-ID", "value": "<Gmail-ack@provider.invalid>"},
                    {"name": "From", "value": "Huolitsija <huolitsija@heliosone.fi>"}]}})
            import base64
            from email import policy
            from email.parser import BytesParser
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(json.loads(request.content)["raw"]))
            payload = {"to": [str(message["To"])], "reply_to": str(message["Reply-To"]), "subject": str(message["Subject"]),
                "text": message.get_body(preferencelist=("plain",)).get_content(),
                "headers": {"In-Reply-To": str(message["In-Reply-To"]), "References": str(message["References"])}}
        else:
            assert request.headers["Idempotency-Key"] == "clarification-complete:run:1"
            payload = json.loads(request.content)
        calls.append(payload)
        expected_reply_to = "huolitsija@heliosone.fi" if transport == "gmail" else "inbound@demo.invalid"
        assert payload["to"] == ["owner@customer.invalid"] and payload["reply_to"] == expected_reply_to
        assert payload["headers"]["In-Reply-To"] == "<customer-answer@customer.invalid>"
        assert "<SES-question@provider.invalid>" in payload["headers"]["References"]
        assert payload["subject"] == "Re: Clarification details [Q-2026-000123]"
        assert "Kiitos, nyt minulla" in payload["text"] if locale == "fi" else "that's everything I need" in payload["text"]
        return httpx.Response(201, json={"id": "ack-provider-id"})
    original = httpx.AsyncClient
    monkeypatch.setattr(core_email.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw))
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", transport)
    monkeypatch.setenv("RESEND_API_KEY", "private-test-only")
    if transport == "gmail":
        monkeypatch.delenv("RESEND_API_KEY")
        monkeypatch.setattr(config, "RESEND_API_KEY", "")
        monkeypatch.setenv("DEMO_INBOUND_PROVIDER", "gmail")
        monkeypatch.setenv("DEMO_INBOUND_ADDRESS", "huolitsija@heliosone.fi")
        for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
            monkeypatch.setenv("HUOLITSIJA_GMAIL_" + name, "private-test-only")
    async def exercise():
        await record(thread_id="thread", locale=locale, clarification_round=1,
            clarification_checkpoint={"missing": ["weight_kg"], "reply": {"id": "answer"}})
        await db.raw.email_threads.insert_one({"id": "thread", "demo_run_id": "run",
            "customer_email": "owner@customer.invalid", "language": locale, "subject": "Quote request", "counts": {}})
        await db.raw.email_messages.insert_many([
            {"id": "question", "thread_id": "thread", "direction": "out", "message_id": "<synthetic@demo.invalid>",
             "provider_message_ids": ["<synthetic@demo.invalid>", "<SES-question@provider.invalid>"], "created_at": "2026-01-01"},
            {"id": "answer", "thread_id": "thread", "direction": "in", "from_email": "owner@customer.invalid",
             "subject": "Re: Re: Clarification details [Q-2026-000123]",
             "message_id": "<customer-answer@customer.invalid>", "created_at": "2026-01-02"}])
        assert (await clarification_ack.complete("run", 1))["status"] == "incomplete" and not calls
        await db.raw.demo_runs.update_one({"id": "run"}, {"$set": {"clarification_checkpoint.missing": [],
            "clarification_checkpoint.all_missing": ["incoterm"]}})
        assert (await clarification_ack.complete("run", 1))["status"] == "incomplete" and not calls
        await db.raw.demo_runs.update_one({"id": "run"}, {"$set": {"clarification_checkpoint.all_missing": []}})
        await asyncio.gather(clarification_ack.complete("run", 1), clarification_ack.complete("run", 1))
        assert len(calls) == 1 and await db.raw.demo_clarification_acks.count_documents({}) == 1
        ack = await db.raw.demo_clarification_acks.find_one({"run_id": "run"})
        assert ack["status"] == ("sent" if transport == "gmail" else "accepted")
        if transport == "resend":
            assert await receipts.record_sent({"type": "email.sent", "data": {"email_id": "ack-provider-id",
                "message_id": "<SES-ack@provider.invalid>"}})
        again = await clarification_ack.complete("run", 1)
        assert again["status"] == "sent" and len(calls) == 1
        message = await db.raw.email_messages.find_one({"id": ack["email_message_id"]})
        actual_id = "<Gmail-ack@provider.invalid>" if transport == "gmail" else "<SES-ack@provider.invalid>"
        assert actual_id in message["provider_message_ids"] and message["thread_id"] == "thread"
        await db.raw.demo_runs.update_one({"id": "run"}, {"$set": {"clarification_round": 2, "clarification_checkpoint.missing": ["pickup_address"]}})
        assert (await clarification_ack.complete("run", 2))["status"] == "incomplete" and len(calls) == 1
    run(exercise())


def test_invoice_must_be_sent_then_payment_is_idempotent_world_bank():
    async def exercise():
        from core.refs import fi_reference, rf_reference
        from core.settings import update_settings
        await update_settings({"autopilot_enabled": True, "autopilot_halted": False})
        row = await record(phase="lifecycle", status="lifecycle", quote_id="quote")
        invoice = {"id": "invoice", "quote_id": "quote", "number": "INV-1", "status": "open", "total_cents": 10000,
                   "paid_cents": 0, "currency": "EUR", "reference": fi_reference("2026000123"),
                   "rf_reference": rf_reference(fi_reference("2026000123")), "created_at": now_iso(),
                   "seller": {"iban": "FI2112345600000785"}, "kind": "invoice", "company_id": "payment-company", "locale": "en"}
        await db.raw.invoices.insert_one(dict(invoice))
        with pytest.raises(HTTPException) as exc:
            await simulate_payment(row)
        assert exc.value.status_code == 409
        for provider, status, expected in (("world", "sent", False), ("smtp2go", "failed", False),
            ("resend", "accepted", False), ("resend", "sent", True), ("mailslurp", "sent", True), ("smtp2go", "sent", True)):
            await db.raw.email_outbox.delete_many({})
            await db.raw.email_outbox.insert_one({"id": "mail", "to": "owner@customer.invalid", "tag": "invoice_issued",
                "provider": provider, "status": status, "ref": {"type": "invoice", "id": "invoice"}})
            assert (await payment_state(row))["available"] is expected
        first, again = await asyncio.gather(simulate_payment(row), simulate_payment(row))
        assert first["status"] == again["status"] == "queued"
        assert await db.raw.jobs.count_documents({"kind": "world.pay"}) == 1
        job = await db.raw.jobs.find_one({"kind": "world.pay"})
        assert job["source"] == "world" and job["payload"]["explicit_request"]
        result = await engine.pay(job)
        assert result["statement_id"]
        assert (await engine.pay(job))["duplicate"]
        assert await db.raw.bank_statements.count_documents({}) == 1
        event = await db.raw.demo_run_events.find_one({"node": "payment", "kind": "world"})
        assert event["data"]["actor"]["source"] == "world"
        from services import reconciliation
        await db.raw.companies.insert_one({"id": "payment-company", "name": "Controlled customer"})
        await reconciliation.camt054_import_job({"payload": {"statement_id": result["statement_id"]}})
        paid = await db.raw.invoices.find_one({"id": "invoice"})
        assert paid["status"] == "paid" and paid["paid_cents"] == 10000
        assert (await payment_state(row))["status"] == "paid"
        assert (await simulate_payment(row))["status"] == "paid"
        await reconciliation.camt054_import_job({"payload": {"statement_id": result["statement_id"]}})
        assert (await db.raw.invoices.find_one({"id": "invoice"}))["paid_cents"] == 10000
        foreign = await record("other", quote_id="other-quote")
        with pytest.raises(HTTPException):
            await simulate_payment(foreign, "invoice")
    run(exercise())


def test_llm_down_never_treats_negated_booking_as_acceptance(monkeypatch):
    from core.llm import LLMUnavailable
    from core.settings import update_settings
    from services.email_channel import intake
    book = AsyncMock(side_effect=AssertionError("No semantic decision without the LLM"))
    monkeypatch.setattr(flow, "handle_book", book)
    monkeypatch.setattr(intake, "read_mail", AsyncMock(side_effect=LLMUnavailable("Controlled unavailable model")))
    async def exercise():
        await update_settings({"autopilot_enabled": True, "autopilot_halted": False})
        await record(phase="lifecycle", status="lifecycle", quote_id="quote", thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid",
            "quote_id": "quote", "state": "quoted", "language": "en", "pending": {"booking": {"option_number": 1}}})
        await db.raw.email_messages.insert_one({"id": "offer", "direction": "out", "message_id": "<offer@demo.invalid>",
                                               "thread_id": "thread", "created_at": now_iso()})
        msg = EmailMessage()
        msg["From"], msg["To"] = "owner@customer.invalid", "inbound@demo.invalid"
        msg["Subject"], msg["Message-ID"] = "Re: Your quote", "<negated-booking@test.invalid>"
        msg["In-Reply-To"] = "<offer@demo.invalid>"
        msg.set_content("Please book nothing until I approve the terms.")
        incoming, _ = await inbound.ingest(msg.as_bytes(), source="pubdemo-test-mailbox")
        assert await inbound.process_pending(limit=1) == 1
        row = await db.raw.email_messages.find_one({"id": incoming["id"]})
        assert row["status"] == "needs_owner" and row["intent"] is None and row["parse"]["model"] == "unavailable"
        assert not book.called and not await db.raw.shipments.find_one({})
        assert await db.raw.demo_run_events.find_one({"node": "booking", "state": "waiting"})
        assert await db.raw.approvals.find_one({"kind": "email_triage", "ref_id": incoming["id"], "status": "pending"})
        assert not book.called and not await db.raw.shipments.find_one({})
    run(exercise())


def test_conditional_booking_and_human_subject_need_model_decision(monkeypatch):
    from core.settings import update_settings
    from services.email_channel import intake, mime
    book = AsyncMock(side_effect=AssertionError("A conditional acceptance needs a person"))
    monkeypatch.setattr(flow, "handle_book", book)
    reading = intake.clean({"intent": "book", "language": "en", "confidence": 0.99,
        "option_number": 1, "accepts_terms": False, "booking_conditions": "Only if pickup is Tuesday"})
    assert reading["booking_conditions"] == "Only if pickup is Tuesday" and "auto_reply" in intake.INTENTS
    assert mime.is_automatic({"from_email": "no-reply@customer.invalid", "subject": "Poissa varastosta: nouto toisesta osoitteesta"}) is None
    assert mime.is_automatic({"auto_submitted": "auto-replied"}) == "auto_submitted"
    monkeypatch.setattr(intake, "read_mail", AsyncMock(return_value=reading))
    async def exercise():
        await update_settings({"autopilot_enabled": True, "autopilot_halted": False})
        await record(phase="lifecycle", status="lifecycle", quote_id="quote", thread_id="thread")
        await db.raw.email_threads.insert_one({"id": "thread", "customer_email": "owner@customer.invalid",
            "quote_id": "quote", "state": "quoted", "language": "en", "pending": {"booking": {"option_number": 1}}})
        await db.raw.email_messages.insert_one({"id": "offer", "direction": "out", "message_id": "<offer@demo.invalid>",
                                               "thread_id": "thread", "created_at": now_iso()})
        msg = EmailMessage()
        msg["From"], msg["To"] = "owner@customer.invalid", "inbound@demo.invalid"
        msg["Subject"], msg["Message-ID"] = "Re: Your quote", "<conditional@test.invalid>"
        msg["In-Reply-To"] = "<offer@demo.invalid>"
        msg.set_content("OK to book if pickup is Tuesday")
        incoming, _ = await inbound.ingest(msg.as_bytes(), source="pubdemo-test-mailbox")
        await inbound.process_pending(limit=1)
        row = await db.raw.email_messages.find_one({"id": incoming["id"]})
        assert row["status"] == "needs_owner"
        assert await db.raw.approvals.find_one({"kind": "email_triage", "ref_id": incoming["id"], "status": "pending"})
        assert not book.called and not await db.raw.shipments.find_one({})
    run(exercise())
