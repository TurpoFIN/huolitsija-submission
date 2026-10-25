"""Signed broker boundary and native export scheduler ownership, private Mongo."""
from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
import test_s2_prepayment as native
from core.db import db
from core.jobs import JobDeferred
from core.util import now_iso
from services.agents import export_agent
from services.demo_world import broker, engine


@pytest.fixture(autouse=True)
def private_db():
    native.setup_module(None)
    yield
    native.teardown_module(None)


@pytest.mark.parametrize('handling,status', [('customer_broker','customer_arranged'), ('broker',None), (None,'customer_arranged')])
def test_export_schedule_and_already_queued_review_never_draft_customer_broker(monkeypatch,handling,status):
    from services import customs_export
    draft = AsyncMock(side_effect=AssertionError('Customer broker must not create an authority draft'))
    monkeypatch.setattr(customs_export, 'draft_declaration_for_shipment', draft)
    async def scenario():
        s = {'id':'broker-native','company_id':native.CID,'direction':'export','customs_clearance':True,
            'destination':{'country':'NO'},'customs_handling':handling,'customs_filing_status':status,'status':'booked'}
        await db.raw.shipments.insert_one(dict(s))
        await export_agent.schedule(s, 'booking')
        assert not await db.raw.jobs.find_one({'kind':'customs.review_export','payload.shipment_id':s['id']})
        assert (await export_agent.review({'payload':{'shipment_id':s['id']}}))['skipped']
        assert not await db.raw.export_declarations.find_one({'shipment_id':s['id']})
        draft.assert_not_awaited()
    native.run(scenario())


@pytest.mark.parametrize('handling', ['us','unknown'])
def test_other_native_handling_keeps_export_schedule_and_review(monkeypatch,handling):
    from services import customs_export
    draft = AsyncMock(return_value={'draft_source':'manual'})
    monkeypatch.setattr(customs_export,'draft_declaration_for_shipment',draft)
    async def scenario():
        s = {'id':'our-native','company_id':native.CID,'direction':'export','customs_clearance':True,
            'destination':{'country':'NO'},'customs_handling':handling,'status':'booked'}
        await db.raw.shipments.insert_one(dict(s))
        await export_agent.schedule(s,'booking')
        assert await db.raw.jobs.find_one({'kind':'customs.review_export','payload.shipment_id':s['id']})
        await export_agent.review({'payload':{'shipment_id':s['id']}})
        draft.assert_awaited_once()
    native.run(scenario())


def test_world_broker_refuses_flag_off_unknown_and_missing_signature_configuration(monkeypatch):
    run = {'id':'broker-world','quote_id':'q-broker','phase':'lifecycle','status':'lifecycle'}
    s = {'id':'broker-world-shipment','quote_id':'q-broker','reference':'WB-TEST','company_id':native.CID,
        'direction':'export','status':'booked','customs_handling':'customer_broker'}
    async def scenario():
        monkeypatch.setenv('DEMO_WORLD_SANDBOX','false')
        assert (await broker.provide_release(run,s))['skipped']
        monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
        assert (await broker.provide_release(run,{**s,'customs_handling':'unknown'}))['skipped']
        assert (await broker.provide_release(run,{**s,'quote_id':'foreign'}))['stopped']
        monkeypatch.delenv('TRACKING_WEBHOOK_SECRET',raising=False)
        with pytest.raises(JobDeferred):
            await broker.provide_release(run,s)
        assert not await db.raw.feed_events.find_one({})
        assert not await db.raw.documents.find_one({'shipment_id':s['id']})
    native.run(scenario())


def test_release_requires_verified_applied_same_shipment_feed_and_milestone():
    async def scenario():
        at=now_iso()
        s={'id':'release-proof','reference':'WB-PROOF','direction':'export',
            'milestones':[{'code':'export_cleared','at':at}]}
        assert not await broker.released(s)
        row={'key':'world-broker:proof','shipment_id':s['id'],'status':'queued','verified':True,
            'event':{'shipment_ref':s['reference'],'code':'export_cleared','at':at}}
        await db.raw.feed_events.insert_one(dict(row))
        assert not await broker.released(s)
        await db.raw.feed_events.update_one({'key':row['key']},{'$set':{'status':'applied','verified':False}})
        assert not await broker.released(s)
        await db.raw.feed_events.update_one({'key':row['key']},{'$set':{'verified':True,'shipment_id':'foreign'}})
        assert not await broker.released(s)
        await db.raw.feed_events.update_one({'key':row['key']},{'$set':{'shipment_id':s['id']}})
        assert await broker.released(s)
        assert not await broker.released({**s,'milestones':[]})
    native.run(scenario())
