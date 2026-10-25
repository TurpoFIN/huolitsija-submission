"""EMAIL_PROVIDER=smtp2go: core.email.send_email and CRM prospecting deliver through the SMTP2GO HTTP API.

The response bodies below were captured from the live SMTP2GO API on 2026-10-25 (POST /v3/email/send with a
send-only key; the accepted mail was then received at the Postmark inbound address). Request assertions follow
the payload that live call accepted: sender "Name <addr>", `to` as a list, custom_headers as {header, value}.
"""
import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from core import config
from core import email as core_email
from core.db import db

LIVE_OK = {"request_id": "bc88aa88-c244-483c-909d-2e1f1493361b",
           "data": {"succeeded": 1, "failed": 0, "failures": [], "email_id": "1xKoro-FnQW0hPpoDj-HeNt"}}
LIVE_BAD_PAYLOAD = {"request_id": "3a71d0a9-51b9-4149-9e5d-ef418d04f320",
                    "data": {"error": "An error occurred processing the json data you sent with the request.",
                             "error_code": "E_ApiResponseCodes.NON_VALIDATING_IN_PAYLOAD"}}


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def motor_loop():
    from core.db import client
    client._io_loop = asyncio.get_running_loop()
    yield


@pytest.fixture
def smtp2go(monkeypatch):
    calls = []
    reply = {"status": 200, "body": LIVE_OK}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(reply["status"], json=reply["body"])

    class MockedClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "AsyncClient", MockedClient)
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "smtp2go")
    monkeypatch.setattr(config, "SMTP2GO_API_KEY", "api-" + "A" * 32, raising=False)
    monkeypatch.setattr(config, "EMAIL_FROM", "Huolitsija <ops@huolitsija.example>")
    monkeypatch.setattr(core_email, "OUTBOUND_HOOKS", [])
    return calls, reply


@pytest.mark.asyncio(loop_scope="module")
async def test_send_email_uses_smtp2go_api(smtp2go):
    calls, _ = smtp2go
    assert core_email.email_configured()
    rec = await core_email.send_email("buyer@example.com", "Quote Q-1", "<p>Hi</p>", "Hi", tag="quote",
                                      reply_to="ops@huolitsija.example", headers={"Message-ID": "<q1@huolitsija.example>"},
                                      attachments=[("q.txt", "text/plain", b"x")])
    assert rec["status"] == "sent", rec
    assert rec["provider"] == "smtp2go"
    assert rec["provider_id"] == "1xKoro-FnQW0hPpoDj-HeNt"
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == "https://api.smtp2go.com/v3/email/send"
    assert req.headers["X-Smtp2go-Api-Key"] == "api-" + "A" * 32
    body = json.loads(req.content)
    assert body["sender"] == "Huolitsija <ops@huolitsija.example>"
    assert body["to"] == ["buyer@example.com"]
    assert body["subject"] == "Quote Q-1" and body["text_body"] == "Hi" and body["html_body"] == "<p>Hi</p>"
    sent_headers = {h["header"]: h["value"] for h in body["custom_headers"]}
    assert sent_headers == {"Reply-To": "ops@huolitsija.example", "Message-ID": "<q1@huolitsija.example>"}
    assert body["attachments"] == [{"filename": "q.txt", "fileblob": "eA==", "mimetype": "text/plain"}]
    stored = await db.email_outbox.find_one({"id": rec["id"]}, {"_id": 0})
    assert stored and stored["status"] == "sent"


@pytest.mark.asyncio(loop_scope="module")
async def test_send_email_smtp2go_rejection_is_failed(smtp2go):
    _, reply = smtp2go
    reply.update(status=400, body=LIVE_BAD_PAYLOAD)
    rec = await core_email.send_email("buyer@example.com", "S", "<p>x</p>", "x")
    assert rec["status"] == "failed"
    assert "json data" in rec["error"]


@pytest.mark.asyncio(loop_scope="module")
async def test_prospecting_deliver_routes_smtp2go_through_core(smtp2go, monkeypatch):
    from services import prospecting
    calls, _ = smtp2go
    monkeypatch.setenv("CRM_EMAIL_PROVIDER", "smtp2go")
    monkeypatch.delenv("CRM_SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    rec = await prospecting.deliver("lead@example.com", "Hello", "<p>Hello</p>", "Hello",
                                    {"List-Unsubscribe": "<https://huolitsija.example/stop>"}, "<crm.x@huolitsija.example>",
                                    {"type": "crm_lead", "id": "t"})
    assert rec["status"] == "sent", rec
    assert len(calls) == 1
    sent_headers = {h["header"]: h["value"] for h in json.loads(calls[0].content)["custom_headers"]}
    assert sent_headers["List-Unsubscribe"] == "<https://huolitsija.example/stop>"
    assert rec["provider"] == "smtp2go"
