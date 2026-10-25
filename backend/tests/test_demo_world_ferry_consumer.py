"""World consumer of reviewed native eb0ac7e through-road booking contract."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import test_core_ferry_booking as native
from core.db import db, client
from core.settings import update_settings
from core.storage import get_file
from services import carrier_booking as cb, geo, positions
from services.agents import carrier_agent, tracking_agent
from services.demo_world import boundary, engine
from services.demo_autoquote import clock


@pytest.fixture(autouse=True)
def private_database(monkeypatch):
    from core import util, jobs
    monkeypatch.setattr(util,'_clock',util._clock)
    monkeypatch.setattr(util,'_rate',util._rate)
    monkeypatch.setattr(jobs,'DEMO_CALENDAR',jobs.DEMO_CALENDAR)
    boundary._state.clear()
    native.run(native.setup())
    async def geometry(mode,origin,destination,**kw):
        return {'path':[[origin['lon'],origin['lat']],[destination['lon'],destination['lat']]],
            'source':'controlled_geometry','note':'Controlled geometry input','distance_km':geo.haversine_km(
                [origin['lon'],origin['lat']],[destination['lon'],destination['lat']])}
    monkeypatch.setattr(geo,'route_info_async',geometry)
    monkeypatch.setattr(positions,'refresh_shipment',AsyncMock())
    yield
    native.run(client.drop_database(db.raw.name))
    boundary._state.clear()


def test_one_native_purchase_moves_all_physical_legs_and_creates_pod(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    boundary.install()
    wall = datetime(2030,1,14,tzinfo=timezone.utc)
    monkeypatch.setattr(boundary,'_wall',lambda:wall)
    async def choice(actor,system,prompt,**kw):
        facts = json.loads(prompt)
        return {'code':facts['allowed'][0],'delay_hours':1,'note':'Controlled physical input'}
    async def no_delay(run_id,seconds,**kw):
        nonlocal wall
        wall += timedelta(seconds=seconds / boundary.clock_state()['speed'])
    monkeypatch.setattr(engine.llm,'ask_json',choice)
    monkeypatch.setattr(engine.clock,'sleep_sim',no_delay)
    channel = native.ControlledChannel()
    monkeypatch.setattr(cb,'adapter_for',AsyncMock(return_value=channel))
    async def scenario():
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        shipment,option,card = await native.make_route(monkeypatch, destination='SEGOT')
        sid = shipment['id']
        assert [leg['mode'] for leg in shipment['legs']] == ['road','sea','road']
        assert shipment['legs'][1]['booking_parent_seq'] == 1
        assert shipment['legs'][1]['carrier_id'] is None
        assert shipment['legs'][1]['ferry_crossing']['operator'] == 'Tallink Grupp'
        await db.raw.shipments.update_one({'id':sid},{'$set':{
            'parties.consignee':{'name':'Controlled consignee','contact':{'name':'Sindre Vardli'}}}})
        await db.raw.demo_runs.insert_one({'id':'through-world','phase':'quote','status':'running','seed':47,
            'quote_id':shipment['quote_id'],'event_seq':0})
        await clock.reset_now('through-world')
        await db.raw.demo_runs.update_one({'id':'through-world'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock('through-world',speed=86400)
        result = await carrier_agent.book_one_leg(shipment,shipment['legs'][0],reason='controlled native purchase')
        assert result['status'] == 'requested', result
        booking = await cb.confirm_booking(result['booking_id'],carrier_ref='CONTROL-THROUGH-001',actor=native.ACTOR)
        assert booking['covered_leg_seqs'] == [1,2,3]
        assert len(channel.requests) == 1
        reported = []
        physical_times = []
        for turn in range(20):
            result = await engine.move({'payload':{'run_id':'through-world','shipment_id':sid,'key':'through-'+str(turn)}})
            event = await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference'],'status':'queued'})
            assert event
            from core.util import parse_iso
            physical_times.append(parse_iso(event['event']['at']))
            if turn == 0:
                assert physical_times[0] == parse_iso(booking.get('etd') or shipment['legs'][0]['etd'])
            reported.append((event['event']['leg_seq'],result['event']))
            await tracking_agent.ingest({'payload':{'key':event['key']}})
            current = await db.raw.shipments.find_one({'id':sid})
            if current['status'] == 'delivered':
                break
        assert current['status'] == 'delivered', reported
        assert physical_times == sorted(physical_times)
        assert all(leg.get('ata') for leg in current['legs'])
        assert current['legs'][1]['carrier_id'] is None
        assert all(leg['booking']['id'] == booking['id'] for leg in current['legs'])
        assert await db.raw.carrier_bookings.count_documents({'shipment_id':sid}) == 1
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference'],'status':'rejected'})
        pod = await db.raw.documents.find_one({'shipment_id':sid,'type':'pod'})
        blob,_,_ = await get_file(pod['file_id'])
        from io import BytesIO
        from pypdf import PdfReader
        assert 'Sindre Vardli' in PdfReader(BytesIO(blob)).pages[0].extract_text()
        if os.environ.get('DEMO_FERRY_PROOF_DIR'):
            folder = Path(os.environ['DEMO_FERRY_PROOF_DIR']);folder.mkdir(parents=True,exist_ok=True)
            (folder/'proof-of-delivery.pdf').write_bytes(blob)
            (folder/'result.json').write_text(json.dumps({'native_contract':'eb0ac7ec7507c72c51625d66aa13293dc4581bc3',
                'events':reported,'native_booking_count':1,'covered_leg_seqs':booking['covered_leg_seqs'],
                'shipment_status':current['status'],'pod_signed_by':'Sindre Vardli',
                'ferry_operator':current['legs'][1]['ferry_crossing']['operator'],
                'ferry_carrier_id':current['legs'][1]['carrier_id'],'controlled_actor_facts':True},indent=2))
    native.run(scenario())


def test_shared_booking_hints_require_confirmed_same_shipment_coverage():
    async def scenario():
        shipment = {'id':'lookup-group'}
        booking = {'id':'old-group','shipment_id':shipment['id'],'leg_seq':1,'covered_leg_seqs':[1,2,3],
            'status':'confirmed','carrier_id':native.ROAD}
        await db.raw.carrier_bookings.insert_one(dict(booking))
        leg = {'seq':2,'booking_parent_seq':1,'booking':{'id':booking['id'],'carrier_id':native.ROAD}}
        assert (await engine.confirmed_booking_for_leg(shipment,leg))['id'] == booking['id']
        assert (await engine.confirmed_booking_for_leg(shipment,{'seq':3,'booking_parent_seq':1}))['id'] == booking['id']
        assert await engine.confirmed_booking_for_leg({'id':'foreign'},leg) is None
        assert await engine.confirmed_booking_for_leg(shipment,{**leg,'seq':4}) is None
        assert await engine.confirmed_booking_for_leg(shipment,{**leg,'booking':{'id':booking['id'],'carrier_id':'other'}}) is None
        await db.raw.carrier_bookings.update_one({'id':booking['id']},{'$set':{'status':'cancelled'}})
        await db.raw.carrier_bookings.insert_one({**booking,'id':'new-group'})
        assert await engine.confirmed_booking_for_leg(shipment,leg) is None
    native.run(scenario())


def test_cancelled_purchase_during_actor_wait_emits_no_physical_feed(monkeypatch):
    from core.jobs import JobDeferred
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    boundary.install()
    monkeypatch.setattr(boundary,'_wall',lambda:datetime(2030,1,14,tzinfo=timezone.utc))
    channel = native.ControlledChannel()
    monkeypatch.setattr(cb,'adapter_for',AsyncMock(return_value=channel))
    monkeypatch.setattr(cb,'_adapter_for_booking',AsyncMock(return_value=channel))
    async def choice(actor,system,prompt,**kw):
        return {'code':'picked_up','delay_hours':1,'note':'Controlled pickup input'}
    monkeypatch.setattr(engine.llm,'ask_json',choice)
    async def scenario():
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        shipment,_,_ = await native.make_route(monkeypatch, destination='SEGOT')
        await db.raw.demo_runs.insert_one({'id':'cancel-race','phase':'quote','status':'running','seed':47,
            'quote_id':shipment['quote_id'],'event_seq':0})
        await clock.reset_now('cancel-race')
        await db.raw.demo_runs.update_one({'id':'cancel-race'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock('cancel-race',speed=86400)
        requested = await carrier_agent.book_one_leg(shipment,shipment['legs'][0],reason='controlled cancellation race')
        booking = await cb.confirm_booking(requested['booking_id'],carrier_ref='CANCEL-RACE',actor=native.ACTOR)
        async def cancel_during_wait(*args,**kw):
            await cb.cancel_booking(booking['id'],native.ACTOR,'Controlled cancellation before collection')
        monkeypatch.setattr(engine.clock,'sleep_sim',cancel_during_wait)
        with pytest.raises(JobDeferred,match='current carrier-confirmed'):
            await engine.move({'payload':{'run_id':'cancel-race','shipment_id':shipment['id'],'key':'cancel-race-move'}})
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference']})
        assert (await db.raw.carrier_bookings.find_one({'id':booking['id']}))['status'] == 'cancelled'
        assert (await db.raw.shipments.find_one({'id':shipment['id']}))['status'] == 'booked'
    native.run(scenario())
