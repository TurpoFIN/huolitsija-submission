"""A stale pickup date is corrected by the outside customer through native mail intake."""
from datetime import timedelta
import json
import os
from pathlib import Path
import re

import pytest
import test_s2_prepayment as native

from core.db import db
from core.settings import update_settings
from services.demo_autoquote import clock
from services.demo_world import boundary, engine, injectors
from services.email_channel import flow, store


@pytest.fixture(autouse=True)
def customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


def test_customer_corrects_past_pickup_date_through_normal_email_booking(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    q = native.quote('clock-correction')
    wall = boundary._wall()
    monkeypatch.setattr(boundary,'_wall',lambda:wall)
    if os.environ.get('DEMO_DATE_LIVE_MODEL') != 'true':
        async def customer_reply(actor, system, prompt, **kw):
            data = json.loads(prompt)
            current = boundary._parse(data['world_time'])
            assert current.date().isoformat() > q['request']['ready_date']
            future = current.date() + timedelta(days=2)
            while future.weekday() > 4:
                future += timedelta(days=1)
            return {'reply':True,'text':f'Please book option 1 with pickup on {future.isoformat()}. The agreed price and parties remain as accepted.',
                    'delay_hours':0.1,'memory':{}}
        monkeypatch.setattr(engine.llm,'ask_json',customer_reply)

    async def exercise():
        nonlocal wall
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        await db.raw.companies.update_one({'id':native.CID},{'$set':{'credit_limit_cents':500000}})
        record = {'id':'clock-correction','phase':'quote','status':'running','seed':67,'quote_id':q['id'],
            'event_seq':0,'created_at':wall.isoformat(),'profile':{'ready_date':q['request']['ready_date']}}
        await db.raw.demo_runs.insert_one(dict(record))
        await clock.reset_now(record['id'])
        await db.raw.demo_runs.update_one({'id':record['id']},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock(record['id'],speed=1)
        boundary.install()
        wall += timedelta(days=8)
        party = lambda name,city,country:{'name':name,'street':'Testikatu 1','postal_code':'00100','city':city,'country':country}
        thread = await store.create_thread({'from_email':native.CUSTOMER['email'],'from_name':native.CUSTOMER['name'],
            'subject':'Pickup correction'},language='en')
        thread = await store.update_thread(thread['id'],company_id=native.CID,user_id=native.CUSTOMER['id'],
            quote_id=q['id'],quote_number=q['number'],demo_run_id=record['id'],pending={'booking':{
                'option_number':1,'pickup_date':q['request']['ready_date'],'incoterm_place':'Stockholm',
                'terms_accepted':{'message_id':'<accepted@test.invalid>'},
                'shipper':party('Ennakko Testi Oy','Helsinki','FI'),
                'consignee':party('Mottagare AB','Stockholm','SE')}})
        assert await flow.do_booking(thread) is None
        failed = await db.raw.email_outbox.find_one({'tag':'email_booking_failed','ref.id':thread['id']},{'_id':0})
        assert 'past' in failed['text']
        run = await db.raw.demo_runs.find_one({'id':record['id']},{'_id':0})
        actor = {'actor':'w:customer','address':native.CUSTOMER['email'],'name':native.CUSTOMER['name'],
                 'persona':engine.GOALS['w:customer'],'state':{}}
        outgoing = {**failed,'actor':'w:customer'}
        # Resume an older build's undelivered decision without replaying history.
        await db.raw.world_actions.insert_one({'key':'mail:' + failed['id'],'run_id':record['id'],'actor':'w:customer',
            'status':'pending','decision':{'reply':True,'text':'Please use the next working day.'},'due_at':wall.isoformat()})
        action = await engine._decision(run,actor,outgoing)
        assert action['world_time'] and len(action['superseded_decisions']) == 1
        reply = action['decision']['text']
        dates = [boundary._parse(d).date() for d in re.findall(r'\b\d{4}-\d{2}-\d{2}\b',reply)]
        assert any(d > clock.sim_now(run).date() and d.weekday() < 5 for d in dates)
        delivered = await injectors.mail(run,actor,outgoing,reply,key=action['key'])
        row = await db.raw.email_messages.find_one({'message_id':delivered['message_id']},{'_id':0})
        await flow.process(row)
        booked = await db.raw.email_threads.find_one({'id':thread['id']})
        assert booked.get('shipment_id'), row.get('decision')
        shipment = await db.raw.shipments.find_one({'id':booked['shipment_id']})
        chosen = booked['pending']['booking']['pickup_date']
        assert boundary._parse(chosen).date() > clock.sim_now(run).date()
        accepted_quote = await db.raw.quotes.find_one({'id':q['id']})
        assert accepted_quote['booking']['pickup']['date'] == chosen and shipment['etd'].startswith(chosen)
        if os.environ.get('DEMO_DATE_PROOF_DIR'):
            output = Path(os.environ['DEMO_DATE_PROOF_DIR'])
            output.mkdir(parents=True,exist_ok=True)
            (output/'result.json').write_text(json.dumps({'original_pickup':q['request']['ready_date'],
                'world_time':clock.sim_now(run).isoformat(),'actor_reply':reply,'accepted_pickup':chosen,
                'shipment_reference':shipment['reference'],'normal_email_intake':True,'normal_booking':True,
                'native_intent':row.get('intent'),'native_decision':row.get('decision')},indent=2))
    native.run(exercise())
