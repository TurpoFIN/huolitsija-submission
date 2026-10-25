"""Single-road world consumer through native mail, tracking, POD, invoice and bank.

Controlled outside actor/parser facts; native business decisions remain unchanged.
"""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest
import test_s2_prepayment as native

from core.db import db
from core.settings import update_settings
from core.storage import get_file
from core.util import now_iso
from services import geo, carrier_booking, carrier_booking_mail, reconciliation
from services.agents import booking_agent, delivery_agent, invoice_agent, tracking_agent
from services.demo_autoquote import clock
from services.demo_world import boundary, broker, engine, injectors, observer


@pytest.fixture(autouse=True)
def customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


@pytest.mark.parametrize('customer_broker', [False, True])
def test_single_road_consumer_delivers_pod_and_native_invoice_is_paid(monkeypatch, customer_broker):
    from routes.quotes import book
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    boundary._state.clear()
    boundary.install()
    wall = datetime.now(timezone.utc)
    monkeypatch.setattr(boundary, '_wall', lambda: wall)
    monkeypatch.setenv('CARRIER_BOOKING_MODE','log')
    monkeypatch.setenv('TRACKING_WEBHOOK_SECRET','controlled-private-broker-feed-key')
    from core import email
    provider_calls = []
    monkeypatch.setattr(email.config,'EMAIL_PROVIDER','smtp')
    monkeypatch.setenv('SMTP_HOST','intercepted.invalid')
    monkeypatch.setattr(email,'_smtp_send',lambda *args:provider_calls.append(args))
    async def actor_fact(actor,system,prompt,**kw):
        facts = json.loads(prompt)
        return {'code':facts['allowed'][0],'delay_hours':1,'note':'Controlled road carrier position input'} \
            if 'allowed' in facts else {'delay_hours':1,'late_payment':False,'reason':'Controlled bank processing input'}
    async def booking_facts(*args,**kw):
        return {'intent':'confirmed','confidence':1,'carrier_ref':'ROAD-CONTROL-001','cost_cents':None,
            'details':{},'reason':'Explicit carrier confirmation'}
    async def no_delay(run_id, seconds, **kw):
        nonlocal wall
        wall += timedelta(seconds=seconds / boundary.clock_state()['speed'])
    monkeypatch.setattr(engine.llm,'ask_json',actor_fact)
    monkeypatch.setattr(carrier_booking_mail,'llm_json',booking_facts)
    monkeypatch.setattr(engine.clock,'sleep_sim',no_delay)
    q = native.quote('single-road-consumer')

    async def exercise():
        origin,destination = geo.lookup_code('FIOUL'),geo.lookup_code('NOTOS' if customer_broker else 'SELLA')
        place = 'Tromsø' if customer_broker else 'Luleå'
        if customer_broker:
            from services import quoting_agent
            from unittest.mock import AsyncMock
            capture = json.loads((Path(__file__).parent / 'fixtures/customs/world_broker_model.json').read_text())
            monkeypatch.setattr(quoting_agent, 'llm_json', AsyncMock(return_value=capture['model_response']))
            parsed = await quoting_agent.parse_request(capture['source_text'], locale='en')
            assert parsed['request']['customs_handling'] == 'customer_broker'
            assert parsed['request']['customs_handling_evidence'] in capture['source_text']
            # The capture proves broker appointment through the real parser.
            # Keep this native fixture's future pickup calendar, independent of capture age.
            q['request'].update({k:v for k,v in parsed['request'].items() if k != 'ready_date'})
        q['request'].update(origin=origin,destination=destination,incoterm='FCA',incoterm_place=place)
        for option in q['options']:
            option['legs'][0].update({'from':origin,'to':destination})
        await db.raw.quotes.replace_one({'id':q['id']},dict(q))
        await db.raw.companies.update_one({'id':native.CID},{'$set':{
            'credit_limit_cents':500000,'test_tenant':False,'test':False}})
        await db.raw.carriers.update_one({'id':'c-road'},{'$set':{'contact_email':'dispatch@controlled.invalid',
            'integration':{'type':'email','status':'configured'}}})
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        run_id = 'single-road-consumer'
        await db.raw.demo_runs.insert_one({'id':run_id,'phase':'quote','status':'running','seed':71,
            'quote_id':q['id'],'event_seq':0,'created_at':now_iso()})
        await clock.reset_now(run_id)
        await db.raw.demo_runs.update_one({'id':run_id},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock(run_id,speed=86400)
        body = native.body(q)
        body.shipper.address.update(city='Oulu',country='FI')
        body.consignee.address.update(city=place,country='NO' if customer_broker else 'SE')
        result = await book(body,native.request(),native.user())
        sid = result['shipment_id']
        assert (await booking_agent.confirm_booking({'payload':{'shipment_id':sid}}))['confirmed']
        shipment = await db.raw.shipments.find_one({'id':sid})
        assert len(shipment['legs']) == 1 and shipment['legs'][0]['mode'] == 'road'
        if customer_broker:
            assert shipment['customs_handling'] == 'customer_broker'
            assert shipment['customs_clearance'] and shipment['customs_filing_status'] == 'customer_arranged'
            from services.agents import export_agent
            await export_agent.schedule(shipment, 'controlled-broker-fixture')
            assert (await export_agent.review({'payload': {'shipment_id': sid}}))['skipped'] == 'customer broker handles customs'
            assert not await db.raw.jobs.find_one({'kind': 'customs.review_export', 'payload.shipment_id': sid})
            assert not await db.raw.export_declarations.find_one({'shipment_id': sid})
        # This is a consumer replay: supplier selection is covered separately in
        # test_demo_world_native_carrier. Use its already chosen native purchase.
        booking = await carrier_booking.book_leg(sid,1,'c-road',{'id':'controlled-purchase','type':'system'},
            reason='Controlled existing native road purchase',cost_cents=22000)
        assert booking['status'] == 'requested' and booking['adapter'] == 'log'
        outgoing = await db.raw.email_outbox.find_one({'id':booking['email_id']}) \
            if booking.get('email_id') else await db.raw.email_outbox.find_one({'ref.booking_id':booking['id']})
        assert outgoing and outgoing['provider'] == 'world'
        actor = {'address':'dispatch@controlled.invalid'}
        await injectors.mail({'id':run_id},actor,outgoing,'Confirmed booking ROAD-CONTROL-001 at the agreed price.',
            key='single-road-booking-confirmed')
        message = await db.raw.carrier_booking_messages.find_one({'booking_id':booking['id']})
        assert (await carrier_booking_mail.process({'payload':{'message_id':message['id']}}))['status'] == 'confirmed'
        confirmed = await db.raw.carrier_bookings.find_one({'id':booking['id']})
        assert confirmed['status'] == 'confirmed'
        assert confirmed['confirmation_message_id'] == message['id']
        projected = await db.raw.demo_run_events.find_one({'run_id':run_id,'title':'Carrier booking confirmed'})
        assert projected['data']['actor']['source'] == 'world'
        codes = []
        physical_times = []
        for turn in range(12):
            move_job = {'payload':{'run_id':run_id,'shipment_id':sid,'key':'road-step-'+str(turn)}}
            if customer_broker and turn == 2:
                from core.jobs import JobDeferred
                with pytest.raises(JobDeferred, match='verified customer broker release'):
                    await engine.move(move_job)
                await prove_broker_ingress(monkeypatch, run_id, shipment)
            outcome = await engine.move(move_job)
            codes.append(outcome['event'])
            feed = await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference'],'status':'queued'})
            assert feed
            from core.util import parse_iso
            physical_times.append(parse_iso(feed['event']['at']))
            if turn == 0:
                assert physical_times[0] == parse_iso(confirmed.get('etd') or shipment['legs'][0]['etd'])
            await tracking_agent.ingest({'payload':{'key':feed['key']}})
            assert (await db.raw.feed_events.find_one({'key':feed['key']}))['status'] == 'applied'
            if customer_broker:
                from services.demo_world import authority
                async def no_authority(*args, **kwargs):
                    raise AssertionError('Customer broker must not contact Tulli actor')
                monkeypatch.setattr(authority, 'notify_exit', no_authority)
                await engine.after_feed({'payload': {'run_id':run_id,'shipment_id':sid,
                    'feed_key':feed['key'],'code':outcome['event']}})
            shipment = await db.raw.shipments.find_one({'id':sid})
            if shipment['status'] == 'delivered':
                break
        assert shipment['status'] == 'delivered', codes
        assert physical_times == sorted(physical_times)
        assert {'picked_up','departed','delivered'}.issubset(codes)
        pod = await db.raw.documents.find_one({'shipment_id':sid,'type':'pod'})
        pod_bytes,_,_ = await get_file(pod['file_id'])
        assert pod_bytes.startswith(b'%PDF')
        assert (await delivery_agent.confirm({'payload':{'shipment_id':sid}}))['confirmed']
        await invoice_agent.issue_on_delivery({'payload':{'shipment_id':sid}})
        invoice = await db.raw.invoices.find_one({'shipment_id':sid,'origin':{'$ne':'prepayment'}})
        assert invoice['status'] == 'open' and invoice['pdf_file_id']
        invoice_bytes,_,_ = await get_file(invoice['pdf_file_id'])
        assert invoice_bytes.startswith(b'%PDF')
        pay_job = await db.raw.jobs.find_one({'kind':'world.pay','payload.invoice_id':invoice['id']})
        assert pay_job
        bank = await engine.pay(pay_job)
        assert (await db.raw.invoices.find_one({'id':invoice['id']}))['status'] == 'open'
        await reconciliation.camt054_import_job({'payload':{'statement_id':bank['statement_id']}})
        paid = await db.raw.invoices.find_one({'id':invoice['id']})
        assert paid['status'] == 'paid'
        tx = await db.raw.bank_transactions.find_one({'invoice_id':invoice['id']})
        assert tx['status'] == 'matched' and tx['amount_cents'] == invoice['total_cents']
        run = await db.raw.demo_runs.find_one({'id':run_id})
        assert run['status'] == 'done' and run['end_reason'] == 'settled'
        if customer_broker:
            assert not await db.raw.export_declarations.find_one({'shipment_id': sid})
            assert not await db.raw.customs_declarations.find_one({'shipment_id': sid})
            assert await broker.released(shipment)
            assert await db.raw.demo_run_events.find_one({'run_id': run_id, 'title': 'Customer broker release recorded',
                'data.actor.source': 'world', 'data.facts.real_tulli_contacted': False})
        assert not provider_calls
        assert await db.raw.carrier_bookings.count_documents({'shipment_id':sid}) == 1
        if os.environ.get('DEMO_ROAD_PROOF_DIR'):
            folder = Path(os.environ['DEMO_ROAD_PROOF_DIR']); folder.mkdir(parents=True,exist_ok=True)
            (folder/'proof-of-delivery.pdf').write_bytes(pod_bytes)
            (folder/'freight-invoice.pdf').write_bytes(invoice_bytes)
            (folder/'result.json').write_text(json.dumps({'base_revision':'368a54703757b418121c31d80d5f0a9bfc02c700',
                'route':'Oulu → ' + place,'leg_count':1,'mode':'road','carrier_confirmation':'native inbound mail parser and guard',
                'events':codes,'physical_times':[t.isoformat() for t in physical_times],'shipment_status':shipment['status'],'pod_present':True,
                'invoice_status':paid['status'],'bank_transaction_status':tx['status'],
                'run_end_reason':run['end_reason'],'controlled_actor_facts':True,'external_sends':len(provider_calls),
                'customer_broker':customer_broker,'real_tulli_contacted':False,
                'export_declaration_count':await db.raw.export_declarations.count_documents({'shipment_id':sid}),
                'customs_declaration_count':await db.raw.customs_declarations.count_documents({'shipment_id':sid}),
                **({'broker_release_actor_source':'world','unsigned_release_http_status':401,
                    'wrong_signature_http_status':401,'broker_release_ingress':'POST /api/webhooks/tracking/world-broker'} if customer_broker else {})},indent=2))
    native.run(exercise())


async def prove_broker_ingress(monkeypatch, run_id, shipment):
    import httpx
    from server import app
    from core.jobs import JobDeferred
    from pypdf import PdfReader
    from io import BytesIO
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://demo.local') as client:
        forged = await client.post('/api/webhooks/tracking/world-broker', json={
            'event_id': 'forged', 'shipment_ref': shipment['reference'], 'code': 'export_cleared', 'at': now_iso()})
    assert forged.status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://demo.local') as client:
        wrong = await client.post('/api/webhooks/tracking/world-broker', json={
            'event_id':'forged','shipment_ref':shipment['reference'],'code':'export_cleared','at':now_iso()},
            headers={'X-Huolitsija-Signature':'0' * 64})
    assert wrong.status_code == 401
    assert not await db.raw.feed_events.find_one({'event.event_id': 'forged'})
    assert not await broker.released(shipment)
    with pytest.raises(JobDeferred):
        await engine.broker_release({'payload': {'run_id': run_id, 'shipment_id': shipment['id']}})
    feed = await db.raw.feed_events.find_one({'provider': 'world-broker'})
    assert feed and feed['verified'] and feed['status'] == 'queued'
    assert not await broker.released(shipment)
    await tracking_agent.ingest({'payload': {'key': feed['key']}})
    assert (await engine.broker_release({'payload': {'run_id': run_id, 'shipment_id': shipment['id']}}))['released']
    event = await db.raw.demo_run_events.find_one({'run_id': run_id, 'title': 'Customer broker release recorded'})
    assert event['data']['actor'] == {'id': 'w:broker', 'source': 'world'}
    assert event['simulated'] and event['data']['documents'][0]['simulated']
    data, _, _ = await get_file(event['data']['documents'][0]['file_id'])
    assert 'SIMULATED' in PdfReader(BytesIO(data)).pages[0].extract_text()
    assert not await db.raw.export_declarations.find_one({'shipment_id': shipment['id']})
    assert not await db.raw.customs_declarations.find_one({'shipment_id': shipment['id']})
    current = await db.raw.shipments.find_one({'id': shipment['id']})
    assert await broker.released(current)
    if os.environ.get('DEMO_ROAD_PROOF_DIR'):
        (Path(os.environ['DEMO_ROAD_PROOF_DIR']) / 'SIMULATED-broker-release.pdf').write_bytes(data)


def test_unknown_handling_keeps_native_customs_card_and_booking_hold(monkeypatch):
    from fastapi import HTTPException
    from routes.quotes import apply_approval_policy, book
    q = native.quote('unknown-customs-hold')
    async def exercise():
        q['request'].update(origin=geo.lookup_code('FIOUL'), destination=geo.lookup_code('NOTOS'),
            customs_handling='unknown', customs_clearance=False)
        await db.raw.quotes.replace_one({'id': q['id']}, dict(q))
        from core.settings import get_settings
        held = await apply_approval_policy(q, await get_settings(), send=True, actor=native.user())
        assert held['status'] == 'pending_approval'
        assert 'customs_required' in held['approval']['reasons']
        assert await db.raw.approvals.find_one({'kind':'quote_threshold','ref_id':q['id'],'status':'pending'})
        with pytest.raises(HTTPException) as stopped:
            await book(native.body(held), native.request(), native.user())
        assert stopped.value.status_code == 409
        assert not await db.raw.shipments.find_one({'quote_id': q['id']})
    native.run(exercise())
