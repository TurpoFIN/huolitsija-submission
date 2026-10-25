"""Regress the reviewer's actual SMTP escape; intercept transport, never send."""
import asyncio
from email.utils import getaddresses
import os
import uuid

import pytest
import httpx

os.environ['DB_NAME'] = 'huo_world_envelope_test_' + uuid.uuid4().hex[:10]

from core import email
from core.db import db, client
from services.demo_world import boundary


@pytest.mark.parametrize('headers,to', [
    ({'Bcc': 'outsider@external.invalid'}, 'owned@demo.invalid'),
    ({'Cc': 'outsider@external.invalid'}, 'owned@demo.invalid'),
    ({'bCc': 'outsider@external.invalid'}, 'owned@demo.invalid'),
    ({'Resent-To': 'outsider@external.invalid', 'Resent-Date': 'Sun, 25 Oct 2026 00:00:00 +0000'}, 'owned@demo.invalid'),
    ({'Resent-Cc': 'outsider@external.invalid'}, 'owned@demo.invalid'),
    ({'Resent-Bcc': 'outsider@external.invalid'}, 'owned@demo.invalid'),
    ({}, 'owned@demo.invalid, outsider@external.invalid'),
])
def test_all_external_envelope_targets_captured(monkeypatch, headers, to):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX', 'true')
    monkeypatch.setenv('DEMO_INBOUND_ADDRESS', 'owned@demo.invalid')
    calls = []
    monkeypatch.setattr(email, '_smtp_send', lambda *args: calls.append(args))
    async def exercise():
        result = await email.send_email(to, 'Envelope regression', '<p>Test</p>', 'Test', headers=headers,
                                       provider='smtp', smtp={'host': 'intercepted.invalid'})
        assert result['provider'] == 'world' and result['status'] == 'sent'
        assert not calls
        capture = await db.raw.world_mail.find_one({'id': result['provider_id']})
        assert 'outsider@external.invalid' in capture['envelope_recipients']
    asyncio.get_event_loop().run_until_complete(exercise())


def test_owned_and_quote_reserved_envelopes_still_reach_original_transport(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX', 'true')
    monkeypatch.setenv('DEMO_INBOUND_ADDRESS', 'owned@demo.invalid')
    calls = []
    monkeypatch.setattr(email, '_smtp_send', lambda message, smtp: calls.append([a for _,a in getaddresses(message.get_all('To', []) + message.get_all('Cc', []))]))
    async def exercise():
        await db.raw.demo_runs.insert_one({'id': 'quote-mail', 'phase': 'quote', 'status': 'running', 'recipients': ['rfq@external.invalid']})
        quote_ref = {'type': 'demo_run', 'id': 'quote-mail', 'kind': 'rfq'}
        result = await email.send_email('owned@demo.invalid, rfq@external.invalid', 'Allowed', '<p>Allowed</p>', 'Allowed',
                                       provider='smtp', smtp={'host': 'intercepted.invalid'}, ref=quote_ref, tag='demo_rfq')
        assert result['provider'] == 'smtp' and calls == [['owned@demo.invalid', 'rfq@external.invalid']]
        await db.raw.demo_runs.update_one({'id': 'quote-mail'}, {'$set': {'phase': 'lifecycle'}})
        result = await email.send_email('owned@demo.invalid', 'Lifecycle', '<p>Captured</p>', 'Captured', headers={'Cc': 'rfq@external.invalid'},
                                       provider='smtp', smtp={'host': 'intercepted.invalid'})
        assert result['provider'] == 'world' and len(calls) == 1
    asyncio.get_event_loop().run_until_complete(exercise())


@pytest.mark.parametrize('provider', ['postmark', 'resend', 'smtp2go'])
@pytest.mark.parametrize('recipient_header', ['Cc', 'Bcc', 'Resent-To', 'Resent-Cc', 'Resent-Bcc', 'X-Envelope-To'])
def test_api_providers_refuse_recipient_custom_headers_at_transport(monkeypatch, provider, recipient_header):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX', 'true')
    monkeypatch.setenv('DEMO_INBOUND_ADDRESS', 'owned@demo.invalid')
    for setting in ('POSTMARK_SERVER_TOKEN','RESEND_API_KEY','SMTP2GO_API_KEY'):
        monkeypatch.setattr(email.config, setting, 'test-intercepted-transport')
    calls = []
    original_client = httpx.AsyncClient
    def intercept(request):
        calls.append(request)
        return httpx.Response(200, json={'ErrorCode':0,'MessageID':'test','id':'test','data':{'succeeded':1,'email_id':'test'}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original_client(*a, **{**kw, 'transport': httpx.MockTransport(intercept)}))
    async def exercise():
        result = await email.send_email('owned@demo.invalid','Forbidden','<p>Test</p>','Test',provider=provider,
                                       headers={recipient_header:'outsider@external.invalid'})
        assert result['provider'] == 'world' and not calls
        result = await email.send_email('owned@demo.invalid','Allowed','<p>Test</p>','Test',provider=provider,
                                       headers={'Message-ID':'<allowed@test.invalid>','In-Reply-To':'<previous@test.invalid>',
                                                'References':'<previous@test.invalid>'})
        assert result['provider'] == provider and result['status'] == 'sent' and len(calls) == 1
    asyncio.get_event_loop().run_until_complete(exercise())


def test_threaded_requester_offer_is_delivered_and_lifecycle_mail_is_copied(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    monkeypatch.setenv('DEMO_REQUESTER_ADDRESS','requester@demo.invalid')
    calls = []
    monkeypatch.setattr(email,'_smtp_send',lambda *args:calls.append(args))
    async def exercise():
        await db.raw.demo_runs.insert_one({'id':'threaded-offer','phase':'quote','status':'running','quote_id':'threaded-quote'})
        args = {'to':'requester@demo.invalid','subject':'Offer','html':'<p>Offer</p>','text':'Offer',
            'provider':'smtp','smtp':{'host':'intercepted.invalid'},'headers':{'In-Reply-To':'<previous@test.invalid>','References':'<previous@test.invalid>'},
            'ref':{'type':'quote','id':'threaded-quote'}}
        result = await email.send_email(**args)
        assert result['provider'] == 'smtp' and len(calls) == 1
        await db.raw.demo_runs.update_one({'id':'threaded-offer'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        result = await email.send_email(**{**args,'subject':'Booking update'})
        assert result['provider'] == 'smtp' and len(calls) == 2
        copy = await db.raw.world_mail.find_one({'subject':'Booking update'})
        assert copy['run_id'] == 'threaded-offer' and copy['actor'] == 'w:customer' and copy['delivered_for_real']
        assert copy['provider'] == 'smtp' and copy['outbox_id'] == result['id']
        result = await email.send_email('ops@external.invalid','Daily digest','<p>Digest</p>','Digest',provider='smtp',
                                       smtp={'host':'intercepted.invalid'},tag='daily_digest')
        assert result['provider'] == 'world' and len(calls) == 2
        captured = await db.raw.world_mail.find_one({'subject':'Daily digest'})
        assert captured['run_id'] is None and captured['actor'] == 'w:unrouted'
    asyncio.get_event_loop().run_until_complete(exercise())


def test_owned_recipient_headers_are_also_forbidden_in_custom_headers(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    monkeypatch.setenv('DEMO_INBOUND_ADDRESS','owned@demo.invalid')
    calls = []
    monkeypatch.setattr(email,'_smtp_send',lambda *a: calls.append(a))
    result = asyncio.get_event_loop().run_until_complete(email.send_email('owned@demo.invalid','Custom header','<p>Test</p>','Test',
        headers={'Cc':'owned@demo.invalid'},provider='smtp',smtp={'host':'intercepted.invalid'}))
    assert result['provider'] == 'world' and not calls


def test_shared_requester_mailbox_uses_thread_run_not_old_actor_address(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    monkeypatch.setenv('DEMO_REQUESTER_ADDRESS','shared@demo.invalid')
    calls = []
    monkeypatch.setattr(email,'_smtp_send',lambda *a:calls.append(a))
    async def exercise():
        await db.raw.demo_runs.insert_many([
            {'id':'old-shared','phase':'lifecycle','status':'lifecycle'},
            {'id':'new-shared','phase':'quote','status':'running'}])
        await db.raw.world_actors.insert_one({'run_id':'old-shared','actor':'w:customer',
            'address':'shared@demo.invalid','active':True})
        await db.raw.email_threads.insert_many([
            {'id':'old-shared-thread','demo_run_id':'old-shared'},
            {'id':'new-shared-thread','demo_run_id':'new-shared'}])
        args = dict(to='shared@demo.invalid',html='<p>Test</p>',text='Test',provider='smtp',smtp={'host':'intercepted.invalid'})
        await email.send_email(**args,subject='New run clarification',ref={'type':'email_thread','id':'new-shared-thread'})
        assert not await db.raw.world_mail.find_one({'subject':'New run clarification'})
        await db.raw.demo_runs.update_one({'id':'new-shared'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await email.send_email(**args,subject='New run lifecycle',ref={'type':'email_thread','id':'new-shared-thread'})
        captured = await db.raw.world_mail.find_one({'subject':'New run lifecycle'})
        assert captured['run_id'] == 'new-shared' and captured['actor'] == 'w:customer'
        await db.raw.demo_runs.update_one({'id':'old-shared'},{'$set':{'status':'stopped'}})
        await email.send_email(**args,subject='Stopped run mail',ref={'type':'email_thread','id':'old-shared-thread'})
        await email.send_email(**args,subject='No run lineage')
        assert not await db.raw.world_mail.find_one({'subject':{'$in':['Stopped run mail','No run lineage']}})
        assert len(calls) == 4
        assert await boundary.owner('shared@demo.invalid') == (None,'w:unrouted')
    asyncio.get_event_loop().run_until_complete(exercise())


@pytest.mark.parametrize('provider',['smtp','postmark','resend','smtp2go'])
def test_failed_owned_requester_send_never_becomes_customer_input(monkeypatch, provider):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    monkeypatch.setenv('DEMO_REQUESTER_ADDRESS','delivery@demo.invalid')
    for setting in ('POSTMARK_SERVER_TOKEN','RESEND_API_KEY','SMTP2GO_API_KEY'):
        monkeypatch.setattr(email.config,setting,'test-intercepted-transport')
    def refused(*a):
        raise RuntimeError('Controlled provider rejection')
    monkeypatch.setattr(email,'_smtp_send',refused)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda *a,**kw:original_client(*a,**{**kw,
        'transport':httpx.MockTransport(lambda request:httpx.Response(503,json={'ErrorCode':503,'data':{'failed':1}}))}))
    async def exercise():
        ident = 'delivery-failed-' + provider
        await db.raw.demo_runs.insert_one({'id':ident,'phase':'lifecycle','status':'lifecycle','quote_id':ident+'-quote'})
        result = await email.send_email('delivery@demo.invalid',ident,'<p>Test</p>','Test',provider=provider,
            smtp={'host':'intercepted.invalid'},ref={'type':'quote','id':ident+'-quote'})
        assert result['status'] == 'failed'
        assert not await db.raw.world_mail.find_one({'subject':ident})
    asyncio.get_event_loop().run_until_complete(exercise())


def test_only_explicit_capped_quote_path_can_use_reserved_external_recipient(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    calls = []
    monkeypatch.setattr(email,'_smtp_send',lambda *a: calls.append(a))
    async def exercise():
        await db.raw.demo_runs.insert_one({'id':'explicit-quote','phase':'quote','status':'running','recipients':['reserved@external.invalid']})
        result = await email.send_email('reserved@external.invalid','Operations mail','<p>Test</p>','Test',provider='smtp',smtp={'host':'intercepted.invalid'})
        assert result['provider'] == 'world' and not calls
        result = await email.send_email('reserved@external.invalid','Capped RFQ','<p>Test</p>','Test',provider='smtp',smtp={'host':'intercepted.invalid'},
            ref={'type':'demo_run','id':'explicit-quote','kind':'rfq'},tag='demo_rfq')
        assert result['provider'] == 'smtp' and len(calls) == 1
        for kind in ('partner_counteroffer','partner_clarify','partner_followup'):
            result = await email.send_email('reserved@external.invalid','Follow-up','<p>Test</p>','Test',
                provider='smtp',smtp={'host':'intercepted.invalid'},
                ref={'type':'demo_run','id':'explicit-quote','kind':kind},tag='demo_' + kind)
            assert result['provider'] == 'world' and len(calls) == 1
    asyncio.get_event_loop().run_until_complete(exercise())


def teardown_module():
    asyncio.get_event_loop().run_until_complete(client.drop_database(db.raw.name))
