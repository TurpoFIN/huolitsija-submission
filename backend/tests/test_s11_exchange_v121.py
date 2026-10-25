"""Real Mongo durability with official Tulli response shapes at the HTTP boundary."""
import os
import uuid
os.environ['DB_NAME'] = 'tulli_transport_test_' + uuid.uuid4().hex[:10]

import asyncio
from pathlib import Path
import pytest

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

from test_s11_transport_v121 import transport, FIX
from core import config
from core.db import db, client
from services import tulli_exchange as ex, tulli_transport as t

def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def database():
    assert config.DB_NAME == os.environ['DB_NAME']
    yield
    run(client.drop_database(config.DB_NAME))


def test_durable_receipt_and_same_payload_never_uploads_twice(transport, monkeypatch):
    calls = []
    async def call(operation, **kwargs):
        calls.append(kwargs)
        node = t.Transport.response((FIX / 'Example07_UploadResponse.xml').read_bytes(), 'Upload')
        info = node.find('{' + t.CT + '}ApplicationRequestMessageInformation')
        info.find('{' + t.CT + '}Application').text = 'IMP'
        info.find('{' + t.CT + '}ControlReference').text = kwargs['reference']
        return node
    monkeypatch.setattr(transport, 'call', call)
    async def scenario():
        d = {'id': 'declaration-1', 'company_id': 'company-1'}
        r1 = await ex.next_reference(transport, 'import')
        r2 = await ex.next_reference(transport, 'import')
        assert (r1, r2) == ('LOCAL000000001', 'LOCAL000000002')
        p1 = '<FI415B xmlns="urn:test"><Identification>' + r1 + '</Identification><value>1</value></FI415B>'
        a = await ex.upload(transport, 'import', d, p1, r1)
        b = await ex.upload(transport, 'import', d, p1.replace(r1, r2), r2)
        assert a == b and len(calls) == 1
        assert a['state'] == 'sent' and 'mrn' not in a
    run(scenario())


def test_uncertain_upload_is_persisted_and_not_retried(transport, monkeypatch):
    calls = []
    async def call(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('test delivery timeout')
    monkeypatch.setattr(transport, 'call', call)
    async def scenario():
        d = {'id': 'declaration-2'}
        with pytest.raises(TimeoutError):
            await ex.upload(transport, 'export', d, '<x/>', 'LOCAL000000001')
        row = await db.raw.tulli_transmissions.find_one({'declaration_id': d['id']})
        assert row['state'] == 'reconciliation_required'
        with pytest.raises(t.TransportError, match='reconciliation'):
            await ex.upload(transport, 'export', d, '<x/>', 'LOCAL000000002')
        assert len(calls) == 1
    run(scenario())


def test_download_list_is_account_wide_and_rate_limited(transport, monkeypatch):
    calls = []
    async def call(*args, **kwargs):
        calls.append(1)
        return t.Transport.response((FIX / 'Example13_DownloadListResponse.xml').read_bytes(), 'DownloadList')
    monkeypatch.setattr(transport, 'call', call)
    async def scenario():
        await asyncio.gather(*(ex.refresh_replies(transport) for _ in range(8)))
        assert len(calls) == 1
        cursor = await db.raw.tulli_poll_cursors.find_one({})
        assert cursor['through'] and cursor['next_at'] > cursor['through']
    run(scenario())


def test_mismatched_acknowledgement_cannot_be_sent(transport, monkeypatch):
    async def call(*args, **kwargs):
        return t.Transport.response((FIX / 'Example07_UploadResponse.xml').read_bytes(), 'Upload')
    monkeypatch.setattr(transport, 'call', call)
    with pytest.raises(t.TransportError, match='identity mismatch'):
        run(ex.upload(transport, 'import', {'id':'d3'}, '<x/>', 'LOCAL000000001'))


def test_export_reply_worker_applies_official_acceptance_once(transport, monkeypatch):
    from services import customs_export, tulli
    from services.customs_messages.tulli_common import ROOT, VERSIONS
    from services.customs_messages.tulli_responses import parse_response
    payload = (ROOT / 'export' / VERSIONS['export'] / 'examples/FI528A.xml').read_bytes()
    parsed = parse_response(payload, 'export')
    reference, storage = 'LOCAL000000001', 'EXP-LOCAL-REPLY'
    app = t.parse((FIX / 'Example16_ApplicationResponse.xml').read_bytes())
    for key,value in [('Application','EXP'),('DeclarantBusinessId','FI9991026-5'),
                      ('MessageStorageId',storage),('ControlReference',reference)]:
        app.find('{' + t.APP + '}' + key).text = value
    app.find('{' + t.APP + '}ApplicationResponseContent/{' + t.MSG + '}Content').text = t.encode(payload)
    response = t.Transport.response((FIX / 'Example15_DownloadResponse.xml').read_bytes(), 'Download')
    response.find('{' + t.CT + '}ApplicationResponseMessage').text = t.encode(t.E.tostring(app))
    async def call(operation, **kwargs):
        assert operation == 'Download'
        return response
    monkeypatch.setattr(transport, 'call', call)
    monkeypatch.setattr(ex, 'transport', lambda mode: transport)
    monkeypatch.setattr(config, 'TULLI_EXPORT_MODE', 'test')
    async def scenario():
        from datetime import datetime, timedelta, timezone
        d = {'id':'export-worker','status':'submitted','lrn':parsed.get('lrn'),
             'submission':{'channel':'message_exchange'}}
        await db.raw.export_declarations.insert_one(dict(d))
        await db.raw.tulli_transmissions.insert_one({'system':'export','mode':'test','declarant_id':'FI9991026-5',
                                                     'declaration_id':d['id'],'reference':reference})
        await db.raw.tulli_poll_cursors.insert_one({'_id':'test:FI9991026-5',
                                                    'next_at':datetime.now(timezone.utc)+timedelta(minutes=5)})
        await db.raw.tulli_replies.insert_one({'_id':'worker-reply','account':'test:FI9991026-5','applied':False,
            'info':{'Application':'EXP','DeclarantBusinessId':'FI9991026-5',
                    'ControlReference':reference,'MessageStorageId':storage,'MessageStoredTimestamp':'2026-10-24T22:00:00Z'}})
        got = await customs_export.poll_declaration(d)
        assert got['status'] == 'accepted' and got['mrn'] == parsed['mrn']
        assert (await db.raw.tulli_replies.find_one({'_id':'worker-reply'}))['applied'] is True
        again = await customs_export.poll_declaration(got)
        assert again['history'] == got['history']
    run(scenario())
