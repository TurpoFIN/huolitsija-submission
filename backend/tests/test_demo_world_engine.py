"""World scheduling and completion gates against real Mongo and real input parsers."""
import asyncio
import os
import uuid
import pytest

os.environ["DB_NAME"] = "huo_world_engine_test_" + uuid.uuid4().hex[:10]

from core.db import db, client, ensure_indexes
from core.events import emit as domain_emit
from core.util import now_iso
from services.demo_world import boundary, engine, injectors, observer
from services.reconciliation import parse_camt054


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_offer_handoff_and_default_deny_mail_write_actual_events(monkeypatch):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    monkeypatch.setenv("DEMO_WORLD_SPEED", "86400")
    monkeypatch.setenv("DEMO_REQUESTER_ADDRESS", "customer@demo.invalid")

    async def exercise():
        await ensure_indexes()
        record = {"id": "world-engine-run", "phase": "quote", "status": "offer_sent", "seed": 11, "quote_id": "world-quote",
                  "quote_number": "Q-2026-123456", "event_seq": 0, "created_at": now_iso(), "created_real": now_iso(), "profile": {},
                  "offer": {"message_id": "<offer@test.invalid>", "partner": {"email": "carrier@external.invalid", "name": "Fictional carrier"}}}
        await db.raw.demo_runs.insert_one(dict(record))
        await domain_emit("demo.offer_sent", {"run": record, "quote": {"id": "world-quote"}, "offer": record["offer"]})
        stored = await db.raw.demo_runs.find_one({"id": record["id"]})
        assert stored["status"] == "lifecycle" and stored["phase"] == "lifecycle"
        assert await db.raw.world_actors.count_documents({"run_id": record["id"]}) == 9
        assert await db.raw.jobs.find_one({"kind": "world.react_mail", "payload.run_id": record["id"]})
        captured = await boundary.capture_mail(to="carrier@external.invalid", subject="Booking [CB:abc]", text="Please book", tag="carrier_booking")
        assert captured["provider"] == "world"
        await asyncio.sleep(0.1)
        event = await db.raw.demo_run_events.find_one({"run_id": record["id"], "kind": "email_out"})
        assert event["edge"] == {"from": "booking", "to": "w:customer"} or event["edge"] == {"from": "booking", "to": "w:carrier"}
        assert await db.raw.world_inputs.find_one({"message_id": captured["message_id"]})
        assert not await db.raw.event_failures.find_one({"event": "demo.offer_sent"})
        before = await db.raw.world_mail.count_documents({})
        monkeypatch.setenv("DEMO_WORLD_SANDBOX", "false")
        assert await boundary.capture_mail(to="external@another.invalid", subject="Off", text="Original path") is None
        assert await db.raw.world_mail.count_documents({}) == before
    run(exercise())


def test_seeded_randomness_and_exact_bank_file_fields():
    a = engine.rng({"seed": 1}, "w:carrier", "input-1").random()
    assert a == engine.rng({"seed": 1}, "w:carrier", "input-1").random()
    assert a != engine.rng({"seed": 2}, "w:carrier", "input-1").random()
    invoice = {"total_cents": 123456, "paid_cents": 456, "reference": "12344", "currency": "EUR",
               'seller':{'iban':'FI2112345600000785'}}
    parsed = parse_camt054(injectors.camt054(invoice, "world-credit-1"))
    entry = parsed["entries"][0]
    assert entry["amount_cents"] == 123000 and entry["currency"] == "EUR"
    assert entry["credit_debit"] == "credit" and entry["status"] == "BOOK" and entry["reference"] == "12344"
    assert b'FI2112345600000785' in injectors.camt054(invoice, 'world-credit-1')


def test_long_payment_term_and_paused_clock_defer_without_sleeping(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from core.jobs import JobDeferred
    wall = datetime.now(timezone.utc)
    monkeypatch.setattr(engine.clock,'sim_now',lambda _:wall)
    async def no_sleep(*a, **kw):
        raise AssertionError('Long terms and pauses must release the worker')
    monkeypatch.setattr(engine.clock,'sleep_sim',no_sleep)
    monkeypatch.setattr(boundary,'clock_state',lambda:{'speed':1440,'paused':False})
    with pytest.raises(JobDeferred):
        run(engine._await_due({'id':'term-test'},(wall+timedelta(days=14)).isoformat()))
    monkeypatch.setattr(boundary,'clock_state',lambda:{'speed':1440,'paused':True})
    with pytest.raises(JobDeferred):
        run(engine._await_due({'id':'term-test'},wall.isoformat()))


def test_prepayment_or_blank_pod_does_not_complete_run():
    async def exercise():
        r = await db.raw.demo_runs.find_one({"id": "world-engine-run"}, {"_id": 0})
        await db.raw.invoices.insert_one({"id": "paid-prepayment", "quote_id": r["quote_id"], "status": "paid", "kind": "invoice"})
        await observer.finish_if_responsibility_ended(r)
        assert (await db.raw.demo_runs.find_one({"id": r["id"]}))["status"] == "lifecycle"
        await db.raw.shipments.insert_one({"id": "delivered-no-pod", "quote_id": r["quote_id"], "status": "delivered", "reference": "HUO-WORLD-1"})
        await db.raw.documents.insert_one({"id": "blank-pod", "shipment_id": "delivered-no-pod", "type": "pod", "file_id": ""})
        await observer.finish_if_responsibility_ended(r)
        assert (await db.raw.demo_runs.find_one({"id": r["id"]}))["status"] == "lifecycle"
    run(exercise())


def test_two_leg_world_inputs_reach_native_delivery_and_person_signed_pod(monkeypatch):
    """A connection starts with receipt/departure, never a second global pickup."""
    from datetime import timedelta
    from core.jobs import work_once
    from core.storage import get_file
    from core.util import now, iso
    from services import shipments
    from services.agents import tracking_agent
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')

    async def physical_input(actor, system, prompt, **kw):
        import json
        facts = json.loads(prompt)
        return {'code':facts['allowed'][0], 'delay_hours':1, 'note':'Controlled physical-event test input'}

    async def no_delay(*a, **kw):
        pass
    monkeypatch.setattr(engine.llm,'ask_json',physical_input)
    monkeypatch.setattr(engine.clock,'sleep_sim',no_delay)

    async def exercise():
        t = now()
        a = {'name':'Helsinki','country':'FI','unlocode':'FIHEL','lat':60.17,'lon':24.94}
        b = {'name':'Turku','country':'FI','unlocode':'FITKU','lat':60.45,'lon':22.27}
        c = {'name':'Stockholm','country':'SE','unlocode':'SESTO','lat':59.33,'lon':18.07}
        company = {'id':'world-multi-company','name':'Controlled test shipper'}
        await db.raw.companies.insert_one(dict(company))
        legs = [{'seq':seq,'mode':'road','role':'main','from':start,'to':end,'etd':iso(t),'eta':iso(t+timedelta(days=1)),
                 'atd':None,'ata':None,'distance_km':200,'carrier_id':'world-multi-carrier','carrier':'Controlled test carrier'}
                for seq,start,end in ((1,a,b),(2,b,c))]
        shipment = await shipments._assemble(company=company,mode='road',direction='export',incoterm='FCA',incoterm_place='Helsinki',
            origin=a,destination=c,legs=legs,etd=t,eta=t+timedelta(days=2),cargo={'description':'Controlled test goods','packages':1,'weight_kg':10},
            parties={'consignee':{'name':'Controlled test consignee','contact':{'name':'Sindre Vardli'}}},references={},customs=False,insurance={},
            price={'total_cents':10000,'currency':'EUR'},liability_regime='CMR',forwarder_role='principal',quote_id='world-multi-quote',
            option_id='world-multi-option',source='portal',actor={'id':'world-multi-user','type':'customer','name':'Controlled test customer'})
        await db.raw.shipments.insert_one(dict(shipment))
        await db.raw.demo_runs.insert_one({'id':'world-multi-run','phase':'lifecycle','status':'lifecycle','seed':31,'quote_id':'world-multi-quote','event_seq':0})
        await db.raw.carriers.insert_one({'id':'world-multi-carrier','code':'WMT','name':'Controlled test carrier'})
        for seq in (1,2):
            await db.raw.carrier_bookings.insert_one({'id':'world-multi-booking-'+str(seq),'shipment_id':shipment['id'],
                'leg_seq':seq,'status':'confirmed','carrier_id':'world-multi-carrier'})
        for turn in range(12):
            result = await engine.move({'payload':{'run_id':'world-multi-run','shipment_id':shipment['id'],'key':'multileg-'+str(turn)}})
            await work_once('world-multi-test', max_jobs=1, kinds=['tracking.ingest'])
            current = await db.raw.shipments.find_one({'id':shipment['id']})
            if current['status'] == 'delivered':
                break
        assert current['status'] == 'delivered'
        assert all(leg.get('ata') for leg in current['legs'])
        assert not await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference'],'status':'rejected'})
        second_codes = [r['event']['code'] async for r in db.raw.feed_events.find({'event.shipment_ref':shipment['reference'],'event.leg_seq':2})]
        assert 'picked_up' not in second_codes and 'departed' in second_codes
        pod = await db.raw.documents.find_one({'shipment_id':shipment['id'],'type':'pod'})
        raw, _, _ = await get_file(pod['file_id'])
        from pypdf import PdfReader
        from io import BytesIO
        assert 'Sindre Vardli' in PdfReader(BytesIO(raw)).pages[0].extract_text()
    run(exercise())


def teardown_module():
    run(client.drop_database(db.raw.name))
