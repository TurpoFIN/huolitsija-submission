"""Postmark AP receipt/worker boundaries against real Mongo and GridFS.
Run this module alone (Motor's client owns a single event loop). No provider sends.
Payload contract: https://postmarkapp.com/developer/webhooks/inbound-webhook
"""
import asyncio
import base64
import os
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest

os.environ['DB_NAME'] = 'huo_s13_postmark_' + uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER'] = 'log'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run this file in its own process', allow_module_level=True)
from core.db import db, client, ensure_indexes
from core.jobs import work_once
from core.settings import update_settings
from core.storage import get_file
from services import ap_postmark, ap_mailbox, carrier_invoices
from s13_make_carrier_invoices import invoice_dict, make_finvoice



def run(coro):
    return loop.run_until_complete(coro)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv('AP_INBOUND_ADDRESSES', 'server+ap@inbound.postmarkapp.com')
    monkeypatch.setenv('AP_INBOUND_REQUIRE_AUTH', 'true')
    async def reset():
        for name in await db.list_collection_names():
            await db[name].delete_many({})
        await ensure_indexes()
        await update_settings({'autopilot_enabled': False, 'autopilot_halted': False})
    run(reset())
    yield


def document():
    inv = invoice_dict(number='AP-WEBHOOK-17', seller={
        'name': 'Contract Test Carrier Oy', 'business_id': '1234567-1', 'vat_id': 'FI12345671',
        'iban': 'FI2112345600000785', 'bic': 'NDEAFIHH', 'country': 'FI',
        'street': 'Testikatu 1', 'postal_code': '00100', 'city': 'Helsinki'},
        rows=[{'name': 'Test freight', 'amount_cents': 10000, 'vat_rate_pct': 25.5}])
    return make_finvoice(inv)


def delivery(*, attachment=True, provider_id='provider-ap-17', message_id='<original@carrier.example>'):
    xml = document()
    m = EmailMessage()
    m['From'] = 'billing@carrier.example'
    m['To'] = 'not-ap@example.com'  # Envelope routing handles BCC and forwarding.
    m['Subject'] = 'Carrier invoice AP-WEBHOOK-17'
    m['Message-ID'] = message_id
    m['Authentication-Results'] = 'attacker.example; dkim=pass; dmarc=pass'
    m.set_content('Contract test invoice; no real debt.')
    if attachment:
        m.add_attachment(xml, maintype='application', subtype='xml', filename='invoice.xml')
    p = {'MessageID': provider_id, 'OriginalRecipient': 'server+ap@inbound.postmarkapp.com',
         'To': 'not-ap@example.com', 'From': 'billing@carrier.example',
         'Attachments': [{'Name': 'invoice.xml', 'ContentType': 'application/xml',
                          'Content': base64.b64encode(xml).decode(), 'ContentLength': len(xml)}] if attachment else []}
    return m.as_bytes(), p


async def ingest(raw, p, auth=True):
    return await ap_postmark.try_ingest_postmark(raw, payload=p, authenticated_provider=auth)


async def receive(raw, p):
    result = await ingest(raw, p)
    await work_once('ap-test', 1, kinds=['finance.ap_postmark'])
    receipt = await db.ap_webhook_receipts.find_one({'id': result['id']})
    row = await db.ap_messages.find_one({'id': receipt.get('ap_message_id')})
    assert row, await db.jobs.find_one({'id': result['job_id']})
    return result, receipt, row


def test_postmark_quarantine_release_extract_and_replay():
    async def check():
        raw, p = delivery()
        result, receipt, row = await receive(raw, p)
        assert row['status'] == 'quarantined'
        assert row['authentication']['provider_authenticated'] is True
        assert row['authentication']['pass'] is False  # Forged copied auth headers do not bypass review.
        assert row['original_message_id'] == '<original@carrier.example>'
        assert await db.carrier_invoices.count_documents({}) == 0
        stored, _, _ = await get_file(receipt['raw_file_id'])
        assert stored == raw
        approval = await db.approvals.find_one({'ref_id': row['id']})
        assert approval['status'] == 'pending'
        await ap_mailbox._on_decided({'approval': {**approval, 'status': 'approved'}, 'actor': {'id': 'reviewer'}})
        released = await db.ap_messages.find_one({'id': row['id']})
        assert released['status'] == 'released' and len(released['carrier_invoice_ids']) == 1
        ci_id = released['carrier_invoice_ids'][0]
        ci = await carrier_invoices.process(ci_id)
        assert (ci['number'], ci['net_cents'], ci['vat_cents'], ci['total_cents'], ci['currency']) == (
            'AP-WEBHOOK-17', 10000, 2550, 12550, 'EUR')
        assert ci['source'] == 'email' and ci['sender'] == 'billing@carrier.example'
        assert ci['status'] == 'needs_review'  # No shipment/supplier match invented.
        assert (await ingest(raw, p)) == {**result, 'duplicate': True}
        assert await db.carrier_invoices.count_documents({}) == 1
        assert await db.payables.count_documents({}) == 0
        assert await db.email_outbox.count_documents({}) == 0
    run(check())


def test_concurrent_provider_retries_have_one_receipt_file_and_job():
    async def check():
        raw, p = delivery()
        results = await asyncio.gather(*(ingest(raw, p) for _ in range(10)))
        assert len({r['id'] for r in results}) == len({r['job_id'] for r in results}) == 1
        assert sum(not r['duplicate'] for r in results) == 1
        assert await db.ap_webhook_receipts.count_documents({}) == 1
        assert await db.jobs.count_documents({'kind': 'finance.ap_postmark'}) == 1
        assert await db['files.files'].count_documents({}) == 1
    run(check())


def test_storage_before_enqueue_failure_is_repaired(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError('queue unavailable')
    async def check():
        raw, p = delivery()
        with monkeypatch.context() as patch:
            patch.setattr(ap_postmark, 'enqueue', fail)
            with pytest.raises(RuntimeError, match='queue unavailable'):
                await ingest(raw, p)
        result, receipt, row = await receive(raw, p)
        assert result['duplicate'] is True and row['status'] == 'quarantined'
        assert await db.ap_webhook_receipts.count_documents({}) == 1
    run(check())


def test_quarantine_card_failure_is_repaired_without_duplicate_attachments(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError('approval unavailable')
    async def check():
        raw, p = delivery()
        result = await ingest(raw, p)
        j = await db.jobs.find_one({'id': result['job_id']})
        with monkeypatch.context() as patch:
            patch.setattr(ap_mailbox, 'request_approval', fail)
            with pytest.raises(RuntimeError, match='approval unavailable'):
                await ap_postmark.process_job(j)
        count = await db['files.files'].count_documents({})
        await ap_postmark.process_job(j)
        assert await db.approvals.count_documents({'ref_type': 'ap_message'}) == 1
        assert await db.ap_messages.count_documents({}) == 1
        assert await db['files.files'].count_documents({}) == count
    run(check())


@pytest.mark.parametrize('recipient', ['ops@example.com', 'server+ap@inbound.postmarkapp.com.evil.test', '', None])
def test_only_exact_envelope_recipient_selects_ap(recipient):
    raw, p = delivery()
    p['OriginalRecipient'] = recipient
    p['To'] = 'server+ap@inbound.postmarkapp.com'
    assert run(ingest(raw, p)) is None
    assert run(db.ap_webhook_receipts.count_documents({})) == 0


def test_unconfigured_ap_does_not_capture_customer_invoice(monkeypatch):
    monkeypatch.delenv('AP_INBOUND_ADDRESSES')
    assert run(ingest(*delivery())) is None


def test_provider_auth_required_even_with_forged_dkim():
    with pytest.raises(ValueError, match='authenticated provider'):
        run(ingest(*delivery(), auth=False))
    assert run(db.ap_webhook_receipts.count_documents({})) == 0


@pytest.mark.parametrize('kind', ['missing-id', 'invalid-base64', 'length-mismatch', 'too-many', 'too-large'])
def test_invalid_delivery_is_not_acknowledged(kind):
    raw, p = delivery()
    if kind == 'missing-id': p['MessageID'] = ''
    if kind == 'invalid-base64': p['Attachments'][0]['Content'] = '%%%%'
    if kind == 'length-mismatch': p['Attachments'][0]['ContentLength'] += 1
    if kind == 'too-many': p['Attachments'] *= 51
    if kind == 'too-large': raw = b'x' * (ap_postmark.MAX_RAW_BYTES + 1)
    with pytest.raises(ValueError): run(ingest(raw, p))
    assert run(db.ap_webhook_receipts.count_documents({})) == 0


def test_no_attachments_consumed_without_customer_signup_or_empty_invoice():
    async def check():
        _, _, row = await receive(*delivery(attachment=False))
        assert row['status'] == 'no_attachments'
        assert await db.carrier_invoices.count_documents({}) == 0
        assert await db.users.count_documents({}) == 0
    run(check())


def test_separate_provider_ids_do_not_trust_same_mime_id_for_dedupe():
    async def check():
        await receive(*delivery(provider_id='first'))
        await receive(*delivery(provider_id='second'))
        assert await db.ap_messages.count_documents({}) == 2
    run(check())


def test_invoice_enqueue_failure_recovers_on_release_retry(monkeypatch):
    async def fail(*args, **kwargs): raise RuntimeError('queue unavailable')
    async def check():
        _, _, row = await receive(*delivery())
        with monkeypatch.context() as patch:
            patch.setattr(carrier_invoices.fc, 'enqueue', fail)
            with pytest.raises(RuntimeError, match='queue unavailable'):
                await ap_mailbox.release_quarantined(row['id'], {'id': 'reviewer'})
        await ap_mailbox.release_quarantined(row['id'], {'id': 'reviewer'})
        assert await db.carrier_invoices.count_documents({}) == 1
        assert await db.jobs.count_documents({'kind': 'finance.carrier_invoice_process'}) == 1
    run(check())


def test_imap_ingest_and_repeat_contract_is_preserved(monkeypatch):
    monkeypatch.setenv('AP_INBOUND_REQUIRE_AUTH', 'false')
    async def check():
        raw, _ = delivery()
        row = await ap_mailbox._handle(raw, None)
        assert row['status'] == 'processed' and len(row['carrier_invoice_ids']) == 1
        again = await ap_mailbox._handle(raw, None)
        assert again == {'message_id': '<original@carrier.example>', 'status': 'already_processed'}
        assert await db.carrier_invoices.count_documents({}) == 1
    run(check())


def test_quarantine_rejection_creates_no_invoice():
    async def check():
        _, _, row = await receive(*delivery())
        approval = await db.approvals.find_one({'ref_id': row['id']})
        await ap_mailbox._on_decided({'approval': {**approval, 'status': 'rejected'}, 'actor': {'id': 'reviewer'}})
        assert (await db.ap_messages.find_one({'id': row['id']}))['status'] == 'discarded'
        assert await db.carrier_invoices.count_documents({}) == 0
    run(check())
