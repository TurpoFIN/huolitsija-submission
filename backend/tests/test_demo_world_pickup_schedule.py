"""Pickup calendar and actor provenance at actual world input/event boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import pytest

os.environ['DB_NAME'] = 'huo_world_pickup_schedule_' + uuid.uuid4().hex[:10]
from core.db import db, client
from core.jobs import JobDeferred
from core.util import iso, parse_iso
from services.demo_autoquote import clock, events
from services.demo_world import boundary, engine, observer
from services import shipments
from services.agents import tracking_agent


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def private_database(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    boundary._state.clear()
    yield
    run(client.drop_database(db.raw.name))
    boundary._state.clear()


async def make_native_state():
    start = datetime(2026,11,4,tzinfo=timezone.utc)
    a = {'name':'Oulu','country':'FI','lat':65.0,'lon':25.47}
    b = {'name':'Bergen','country':'NO','lat':60.39,'lon':5.32}
    company = {'id':'calendar-shipper','name':'Controlled calendar shipper'}
    await db.raw.companies.insert_one(dict(company))
    leg = {'seq':1,'mode':'road','role':'main','from':a,'to':b,'etd':iso(start),
        'eta':iso(start+timedelta(days=3)),'atd':None,'ata':None,'carrier_id':'calendar-carrier','distance_km':1600}
    s = await shipments._assemble(company=company,mode='road',direction='export',incoterm='FCA',incoterm_place='Oulu',
        origin=a,destination=b,legs=[leg],etd=start,eta=start+timedelta(days=3),
        cargo={'description':'Controlled goods','packages':1,'weight_kg':10},parties={},references={},customs=False,
        insurance={},price={'total_cents':10000,'currency':'EUR'},liability_regime='CMR',forwarder_role='principal',
        quote_id='calendar-quote',option_id='calendar-option',source='portal',
        actor={'id':'calendar-user','type':'customer','name':'Controlled customer'})
    await db.raw.shipments.insert_one(dict(s))
    await db.raw.carrier_bookings.insert_one({'id':'calendar-purchase','shipment_id':s['id'],'leg_seq':1,
        'carrier_id':'calendar-carrier','status':'confirmed','confirmed_at':'2026-10-26T00:00:00Z','etd':iso(start)})
    await db.raw.carriers.insert_one({'id':'calendar-carrier','name':'Controlled native carrier'})
    await db.raw.demo_runs.insert_one({'id':'calendar-run','phase':'quote','status':'running','event_seq':0,'seed':12,
        'quote_id':'calendar-quote'})
    await clock.reset_now('calendar-run')
    await db.raw.demo_runs.update_one({'id':'calendar-run'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
    await clock.set_clock('calendar-run',speed=1440)
    return s


@pytest.mark.parametrize('source',['native_etd','booking_etd','pickup_window','slot_booking','legacy_pending'])
def test_world_pickup_uses_native_calendar_and_native_tracking(monkeypatch,source):
    wall = datetime(2026,10,26,tzinfo=timezone.utc)
    monkeypatch.setattr(boundary,'_wall',lambda:wall)
    calls = []
    async def choice(actor,system,prompt,**kw):
        calls.append(json.loads(prompt))
        return {'code':'picked_up','delay_hours':1,'note':'World-simulated pickup on the native appointment'}
    monkeypatch.setattr(engine.llm,'ask_json',choice)
    async def scenario():
        nonlocal wall
        s = await make_native_state()
        target = datetime(2026,11,4,tzinfo=timezone.utc)
        if source == 'native_etd':
            await db.raw.carrier_bookings.update_one({'id':'calendar-purchase'},{'$unset':{'etd':''}})
        if source == 'booking_etd':
            target += timedelta(days=1)
            await db.raw.carrier_bookings.update_one({'id':'calendar-purchase'},{'$set':{'etd':iso(target)}})
        if source == 'pickup_window':
            target += timedelta(hours=9)
            await db.raw.shipments.update_one({'id':s['id']},{'$set':{'legs.0.pickup_window':{
                'start':iso(target),'end':iso(target+timedelta(hours=4))}}})
        if source == 'slot_booking':
            target += timedelta(days=1,hours=10)
            await db.raw.slot_bookings.insert_one({'id':'native-slot','shipment_id':s['id'],'leg_seq':1,
                'kind':'pickup','status':'confirmed','window':{'start':iso(target),'end':iso(target+timedelta(hours=2))}})
        if source == 'legacy_pending':
            await db.raw.world_actions.insert_one({'key':'move:calendar-step','run_id':'calendar-run',
                'actor':'w:trucker','status':'pending','due_at':iso(wall+timedelta(hours=1)),
                'decision':{'code':'picked_up','delay_hours':1,'note':'Legacy pending world actor'}})
        job = {'payload':{'run_id':'calendar-run','shipment_id':s['id'],'key':'calendar-step'}}
        with pytest.raises(JobDeferred):
            await engine.move(job)
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':s['reference']})
        action = await db.raw.world_actions.find_one({'key':'move:calendar-step'})
        assert parse_iso(action['due_at']) == target
        assert action['pickup_schedule']['source'] == ('native_etd' if source in ('booking_etd','legacy_pending') else source)
        # The accelerated run reaches its booked date. A delayed worker reports
        # the physical event at the native appointment, not at its retry time.
        wall += (target-clock.sim_now({'id':'calendar-run'}))/1440 + timedelta(seconds=2)
        assert (await engine.move(job))['event'] == 'picked_up'
        feed = await db.raw.feed_events.find_one({'event.shipment_ref':s['reference']})
        assert parse_iso(feed['event']['at']) == target
        await tracking_agent.ingest({'payload':{'key':feed['key']}})
        current = await db.raw.shipments.find_one({'id':s['id']})
        assert parse_iso(current['legs'][0]['atd']) == target
        assert current['status'] == 'picked_up'
        assert len(calls) == (0 if source == 'legacy_pending' else 1)
    run(scenario())


def test_unconfirmed_or_missed_native_appointment_never_emits_pickup(monkeypatch):
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    async def scenario():
        s = await make_native_state()
        leg = s['legs'][0]
        booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
        await db.raw.slot_bookings.insert_one({'id':'waiting-slot','shipment_id':s['id'],'leg_seq':1,
            'kind':'pickup','status':'requested','window':{'start':leg['etd'],'end':'2026-11-04T04:00:00Z'}})
        with pytest.raises(JobDeferred,match='appointment confirmation'):
            await engine._pickup_schedule(s,leg,booking)
        await db.raw.slot_bookings.update_one({'id':'waiting-slot'},{'$set':{'status':'cancelled'}})
        with pytest.raises(ValueError,match='no valid pickup'):
            await engine._pickup_schedule(s,{**leg,'etd':None},{**booking,'etd':None})
        with pytest.raises(JobDeferred,match='predates carrier confirmation'):
            await engine._pickup_schedule(s,leg,{**booking,'confirmed_at':'2026-11-05T00:00:00Z'})
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':s['reference']})
    run(scenario())


@pytest.mark.parametrize('change',['cancel','reschedule'])
def test_native_change_while_actor_waits_prevents_stale_pickup(monkeypatch,change):
    wall = datetime(2026,11,4,tzinfo=timezone.utc)
    monkeypatch.setattr(boundary,'_wall',lambda:wall)
    async def choice(*a,**kw):
        return {'code':'picked_up','delay_hours':1,'note':'Controlled pickup input'}
    monkeypatch.setattr(engine.llm,'ask_json',choice)
    async def scenario():
        s = await make_native_state()
        async def native_change(*a,**kw):
            await db.raw.carrier_bookings.update_one({'id':'calendar-purchase'},{'$set':
                {'status':'cancelled'} if change == 'cancel' else {'etd':'2026-11-05T00:00:00Z'}})
        monkeypatch.setattr(engine.clock,'sleep_sim',native_change)
        with pytest.raises(JobDeferred,match='current carrier-confirmed|schedule changed'):
            await engine.move({'payload':{'run_id':'calendar-run','shipment_id':s['id'],'key':'changed-pickup'}})
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':s['reference']})
    run(scenario())


def test_every_world_actor_event_is_marked_in_storage_and_stream():
    async def scenario():
        await db.raw.demo_runs.insert_one({'id':'provenance-run','event_seq':0})
        stream = events.subscribe('provenance-run')
        try:
            for actor in ('customer','carrier','trucker','terminal','warehouse','customs','consignee','bank','insurer','unrouted'):
                for node,edge,kind in [(f'w:{actor}',None,'step'),('booking',(f'w:{actor}','booking'),'world'),
                        ('booking',('booking',f'w:{actor}'),'email_out')]:
                    data = {'actor':{'name':'Native company name','source':'real'},'facts':{'delivered_for_real':True}}
                    ev = await events.emit('provenance-run',node,'World actor action',edge=edge,kind=kind,data=data)
                    assert ev['data']['actor']['source'] == 'world'
                    assert ev['data']['actor']['id'] == 'w:'+actor
                    assert ev['data']['actor']['name'] == 'Native company name'
                    assert ev['data']['facts']['delivered_for_real'] is True
                    assert data['actor']['source'] == 'real'
                    assert (await stream.get()) == ev
            partner = await events.emit('provenance-run','p:selected','Simulated partner reply',simulated=True)
            assert partner['data']['actor']['source'] == 'world'
            real = await events.emit('provenance-run','p:real','Real reply',data={'actor':{'source':'real'}})
            assert real['data']['actor']['source'] == 'real'
            rows = await events.history('provenance-run')
            assert len(rows) == 32
            assert all(e['data']['actor']['source'] == 'world' for e in rows[:-1])
        finally:
            events.unsubscribe('provenance-run',stream)
    run(scenario())


@pytest.mark.parametrize('provider_id,source',[
    ('demo-sim:controlled-world-input','world'),('postmark:controlled-real-provider-id','real'),(None,None)])
def test_native_carrier_projections_follow_exact_message_evidence(monkeypatch,provider_id,source):
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    async def scenario():
        s = await make_native_state()
        booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
        message = {'id':'exact-confirmation','booking_id':booking['id'],'status':'confirmed','provider_id':provider_id}
        await db.raw.carrier_booking_messages.insert_one(dict(message))
        booking['confirmation_message_id'] = message['id']
        await db.raw.carrier_bookings.update_one({'id':booking['id']},{'$set':{'confirmation_message_id':message['id']}})
        await observer.carrier_booked({'shipment':s,'booking':booking})
        await observer.carrier_reply({'shipment':s,'booking':booking,'message':message,'parsed':{'carrier_ref':'CONTROL-REF'}})
        rows = await events.history('calendar-run')
        assert len(rows) == 2
        if source:
            assert all(e['data']['actor']['source'] == source for e in rows)
            assert all(e['data']['actor']['id'] == ('w:carrier' if source == 'world' else 'carrier') for e in rows)
        else:
            assert all('actor' not in e['data'] for e in rows)
        assert rows[0]['data']['facts']['carrier'] == 'Controlled native carrier'
        # Native projections stay native steps/decisions; the marker describes
        # the external actor, not the application's real execution of its logic.
        assert [e['kind'] for e in rows] == ['step','decision']
    run(scenario())


@pytest.mark.parametrize('site',['carrier_reply','carrier_booked'])
@pytest.mark.parametrize('mismatch',['foreign_message','foreign_shipment'])
def test_foreign_message_or_purchase_never_tags_current_shipment(monkeypatch,site,mismatch):
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    async def scenario():
        s = await make_native_state()
        booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
        foreign = {**booking,'id':'other-native-booking','shipment_id':'other-native-shipment',
            'confirmation_message_id':'foreign-message'}
        foreign.pop('_id',None)
        await db.raw.carrier_bookings.insert_one(dict(foreign))
        message = {'id':'foreign-message','booking_id':foreign['id'],'status':'confirmed','provider_id':'demo-sim:foreign'}
        await db.raw.carrier_booking_messages.insert_one(dict(message))
        booking['confirmation_message_id'] = message['id']
        await db.raw.carrier_bookings.update_one({'id':booking['id']},{'$set':{'confirmation_message_id':message['id']}})
        supplied = foreign if mismatch == 'foreign_shipment' else booking
        before = await db.raw.carrier_bookings.find_one({'id':booking['id']})
        await getattr(observer,site)({'shipment':s,'booking':supplied,'message':message,
            'parsed':{'carrier_ref':'FOREIGN-REF'}})
        rows = await events.history('calendar-run')
        assert all('actor' not in e['data'] for e in rows)
        if site == 'carrier_reply' or mismatch == 'foreign_shipment':
            assert not rows
        assert await db.raw.carrier_bookings.find_one({'id':booking['id']}) == before
    run(scenario())


@pytest.mark.parametrize('site',['carrier_reply','carrier_booked'])
def test_stored_real_message_wins_over_world_provider_in_payload(monkeypatch,site):
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    async def scenario():
        s = await make_native_state()
        booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
        message = {'id':'stored-real','booking_id':booking['id'],'status':'confirmed','provider_id':'postmark:stored-real'}
        await db.raw.carrier_booking_messages.insert_one(dict(message))
        booking['confirmation_message_id'] = message['id']
        await db.raw.carrier_bookings.update_one({'id':booking['id']},{'$set':{'confirmation_message_id':message['id']}})
        await getattr(observer,site)({'shipment':s,'booking':booking,
            'message':{**message,'provider_id':'demo-sim:payload-only'},'parsed':{'carrier_ref':'REAL-REF'}})
        rows = await events.history('calendar-run')
        assert len(rows) == 1 and rows[0]['data']['actor']['source'] == 'real'
    run(scenario())


def test_old_or_foreign_confirmation_reference_is_unknown_despite_world_request(monkeypatch):
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    async def scenario():
        s = await make_native_state()
        booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
        await db.raw.email_outbox.insert_one({'id':'world-request','provider':'world','status':'sent',
            'ref':{'booking_id':booking['id']}})
        booking.update(email_id='world-request',response={'provider':'world'})
        await db.raw.carrier_booking_messages.insert_one({'id':'latest-unrelated','booking_id':booking['id'],
            'provider_id':'demo-sim:unlinked-reply','status':'confirmed'})
        await observer.carrier_booked({'shipment':s,'booking':booking})
        await db.raw.carrier_booking_messages.insert_one({'id':'foreign-reply','booking_id':'other-purchase',
            'provider_id':'demo-sim:foreign-reply','status':'confirmed'})
        await observer.carrier_booked({'shipment':s,'booking':{**booking,'confirmation_message_id':'foreign-reply'}})
        assert all('actor' not in e['data'] for e in await events.history('calendar-run'))
    run(scenario())


def test_world_outbox_is_exact_evidence_but_does_not_override_real_provider():
    async def scenario():
        await db.raw.email_outbox.insert_one({'id':'world-confirmation','provider_id':'world-receipt',
            'provider':'world','status':'sent'})
        assert (await observer._carrier_actor({'provider_id':'world-receipt'}))['actor']['source'] == 'world'
        assert (await observer._carrier_actor({'id':'world-confirmation'}))['actor']['source'] == 'world'
        assert (await observer._carrier_actor({'id':'world-confirmation','provider_id':'postmark:actual'}))['actor']['source'] == 'real'
        assert await observer._carrier_actor({'id':'missing'}) == {}
    run(scenario())


@pytest.mark.parametrize('provider_id,source',[
    ('demo-sim:atomic-world','world'),('postmark:atomic-real','real')])
def test_native_confirmation_persists_exact_reference_before_emitting(monkeypatch,provider_id,source):
    from services import carrier_booking as cb, positions
    from unittest.mock import AsyncMock
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    monkeypatch.setattr(positions,'refresh_shipment',AsyncMock())
    original_emit = cb._emit_booking_event
    observed = []
    async def capture(name,payload):
        if name == 'carrier.booked':
            booking = await db.raw.carrier_bookings.find_one({'id':'calendar-purchase'})
            message = await db.raw.carrier_booking_messages.find_one({'id':booking['confirmation_message_id']})
            observed.append((booking['status'],booking['confirmation_message_id'],message['status']))
        await original_emit(name,payload)
    monkeypatch.setattr(cb,'_emit_booking_event',capture)
    async def scenario():
        await make_native_state()
        await db.raw.carrier_bookings.update_one({'id':'calendar-purchase'},{'$set':{'status':'requested',
            'leg_mode':'road','mode':'log','adapter':'log','company_id':'calendar-shipper','history':[]}})
        await db.raw.carrier_booking_messages.insert_one({'id':'atomic-source','booking_id':'calendar-purchase',
            'status':'received','provider_id':provider_id})
        booking = await cb.confirm_booking('calendar-purchase',carrier_ref='ATOMIC-REF',
            actor={'id':'controlled-ops','type':'system'},source_message_id='atomic-source')
        assert booking['status'] == 'confirmed'
        assert observed == [('confirmed','atomic-source','received')]
        row = next(e for e in await events.history('calendar-run') if e['title'] == 'Carrier booking confirmed')
        assert row['data']['actor']['source'] == source
    run(scenario())
