"""Combined s6+finance HTTP acceptance. Run alone after applying the ingress patch.
Uses real auth, routes, Mongo, GridFS, owner approval and invoice parsing; no sends.
"""
import asyncio
import base64
import json
import os

import httpx
import pytest
from test_s13_ap_postmark import clean, run, db, delivery, document, ap_postmark  # noqa: F401

email_routes = pytest.importorskip('routes.email_channel', reason='Requires the s6 shared ingress branch')
from fastapi import FastAPI
from core.security import load_secret, create_session_token
from core.jobs import work_once
from core.settings import update_settings
from routes import finance_ap, admin
from services import ap_mailbox, carrier_invoices

app = FastAPI()
app.include_router(email_routes.router)
app.include_router(finance_ap.router)
app.include_router(admin.router)
URL = '/api/email-channel/inbound'
AUTH = 'Basic ' + base64.b64encode(b'postmark:local-ap-webhook-test').decode()


@pytest.fixture(autouse=True)
def webhook_config(monkeypatch):
    monkeypatch.setenv('EMAIL_CHANNEL_POSTMARK_TOKEN', 'local-ap-webhook-test')
    monkeypatch.setenv('EMAIL_CHANNEL_WEBHOOK_SECRET', 'local-hmac-test')


async def request(method, path, *, payload=None, auth=AUTH):
    headers = {'Authorization': auth} if auth else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as c:
        return await c.request(method, path, json=payload, headers=headers)


def payload(*, raw=False):
    msg, p = delivery()
    p.update(Subject='Contract test invoice', TextBody='No real debt',
             Headers=[{'Name': 'Message-ID', 'Value': '<original@carrier.example>'},
                      {'Name': 'Authentication-Results', 'Value': 'forged; dkim=pass'}])
    if raw: p['RawEmail'] = msg.decode()
    return p


@pytest.mark.parametrize('raw_email', [False, True])
def test_http_receipt_approval_and_finance_readback(raw_email):
    async def check():
        p = payload(raw=raw_email)
        assert (await request('POST', URL, payload=p, auth=None)).status_code == 401
        a, b = await asyncio.gather(*(request('POST', URL, payload=p) for _ in range(2)))
        assert a.status_code == b.status_code == 200
        assert a.json()['channel'] == b.json()['channel'] == 'accounts_payable'
        assert a.json()['job_id'] == b.json()['job_id']
        await work_once('ap-http-test', 1, kinds=['finance.ap_postmark'])
        assert await db.email_messages.count_documents({}) == 0
        assert await db.users.count_documents({}) == 0
        await db.users.insert_one({'id': 'ap-reviewer', 'name': 'AP test reviewer', 'org_type': 'internal', 'role': 'finance'})
        await load_secret(db)
        auth = 'Bearer ' + create_session_token('ap-reviewer')
        inbox = await request('GET', '/api/finance/ap-mailbox/messages', auth=auth)
        assert inbox.status_code == 200, inbox.text
        item = inbox.json()['items'][0]
        assert item['status'] == 'quarantined'
        assert item['attachments'][0]['filename'] == 'invoice.xml'
        assert inbox.json()['postmark_configured'] is True
        approval = await db.approvals.find_one({'ref_id': item['id']})
        decision = await request('POST', f"/api/approvals/{approval['id']}/decide",
                                 payload={'decision': 'approved', 'note': 'Contract test sender checked'}, auth=auth)
        assert decision.status_code == 200, decision.text
        row = await db.ap_messages.find_one({'id': item['id']})
        assert row['status'] == 'released'
        await carrier_invoices.process(row['carrier_invoice_ids'][0])
        invoices = await request('GET', '/api/finance/carrier-invoices?source=email', auth=auth)
        assert invoices.status_code == 200, invoices.text
        ci = invoices.json()['items'][0]
        assert [ci[k] for k in ('number', 'net_cents', 'vat_cents', 'total_cents', 'currency')] == [
            'AP-WEBHOOK-17', 10000, 2550, 12550, 'EUR']
        assert ci['status'] == 'needs_review'
        assert await db.payables.count_documents({}) == 0
        assert await db.email_outbox.count_documents({}) == 0
        if os.environ.get('S13_AP_EVIDENCE_DIR'):
            from pathlib import Path
            dest = Path(os.environ['S13_AP_EVIDENCE_DIR'])
            dest.mkdir(parents=True, exist_ok=True)
            observation = {'transport': 'local ASGI + real Mongo/GridFS', 'postmark_format': 'RawEmail' if raw_email else 'parsed JSON',
                'receipt_channel': a.json()['channel'], 'duplicate_job_same': a.json()['job_id'] == b.json()['job_id'],
                'mailbox_status_before': item['status'], 'mailbox_status_after': row['status'],
                'approval_http_status': decision.status_code,
                'invoice': {k: ci[k] for k in ('number', 'net_cents', 'vat_cents', 'total_cents', 'currency', 'status', 'source')},
                'payable_count': await db.payables.count_documents({}), 'outgoing_mail_count': await db.email_outbox.count_documents({})}
            (dest / ('raw-roundtrip.json' if raw_email else 'parsed-roundtrip.json')).write_text(json.dumps(observation, indent=2) + '\n')
    run(check())


@pytest.mark.parametrize('bad_auth', ['Basic !!!', 'Basic ' + base64.b64encode(b'postmark:wrong').decode()])
def test_auth_rejection_before_any_ap_storage(bad_auth):
    r = run(request('POST', URL, payload=payload(), auth=bad_auth))
    assert r.status_code == 401
    assert run(db.ap_webhook_receipts.count_documents({})) == 0


@pytest.mark.parametrize('kind', ['unknown', 'wrong_sender'])
def test_rfq_triage_wins_even_when_sent_to_ap_address(kind):
    async def check():
        await update_settings({'autopilot_enabled': True})
        await db.partner_outreach.insert_one({'id': 'outreach-1', 'ref': 'RFQ-ABC123',
            'contact_email': 'carrier@example.com', 'send_to': 'carrier@example.com',
            'status': 'sent', 'candidate_id': 'candidate-1'})
        p = payload()
        p['Subject'] = 'Re: RFQ-ZZZ999' if kind == 'unknown' else 'Re: RFQ-ABC123'
        p['From'] = 'outsider@example.com'
        r = await request('POST', URL, payload=p)
        assert r.status_code == 200 and r.json()['channel'] == 'partner_network'
        await work_once('ap-rfq-test', 1, kinds=['partner.inbound'])
        row = await db.partner_messages.find_one({'direction': 'in'})
        assert row['status'] == ('unmatched' if kind == 'unknown' else 'sender_mismatch')
        assert await db.ap_webhook_receipts.count_documents({}) == 0
        assert await db.email_messages.count_documents({}) == 0
    run(check())


def test_customer_commercial_invoice_stays_customer_mail():
    p = payload()
    p['OriginalRecipient'] = 'server@inbound.postmarkapp.com'
    r = run(request('POST', URL, payload=p))
    assert r.status_code == 200 and 'channel' not in r.json()
    assert run(db.email_messages.count_documents({})) == 1
    assert run(db.ap_webhook_receipts.count_documents({})) == 0


def test_queue_failure_is_500_and_provider_retry_repairs_receipt(monkeypatch):
    async def fail(*args, **kwargs): raise RuntimeError('queue unavailable')
    async def check():
        p = payload()
        with monkeypatch.context() as patch:
            patch.setattr(ap_postmark, 'enqueue', fail)
            r = await request('POST', URL, payload=p)
            assert r.status_code == 500
        r = await request('POST', URL, payload=p)
        assert r.status_code == 200 and r.json()['duplicate'] is True
        assert await db.jobs.count_documents({'kind': 'finance.ap_postmark'}) == 1
        assert await db.email_messages.count_documents({}) == 0
    run(check())


def test_bad_ap_attachment_is_400_without_falling_through():
    p = payload()
    p['Attachments'][0]['ContentLength'] += 1
    r = run(request('POST', URL, payload=p))
    assert r.status_code == 400
    assert run(db.ap_webhook_receipts.count_documents({})) == 0
    assert run(db.email_messages.count_documents({})) == 0


def test_ap_hmac_without_native_provider_auth_does_not_fall_through():
    async def check():
        import hashlib, hmac
        body = json.dumps(payload()).encode()
        headers = {'Content-Type': 'application/json', 'X-Huolitsija-Signature': hmac.new(
            b'local-hmac-test', body, hashlib.sha256).hexdigest()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            r = await c.post(URL, content=body, headers=headers)
        assert r.status_code == 400
        assert await db.email_messages.count_documents({}) == 0
        assert await db.ap_webhook_receipts.count_documents({}) == 0
    run(check())
