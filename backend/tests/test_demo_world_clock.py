"""Successive runs keep their own calendar; no wall waits or external calls."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest

os.environ['DB_NAME'] = 'huo_world_clock_test_' + uuid.uuid4().hex[:10]
from core.db import db, client
from services.demo_world import boundary
from services.demo_autoquote import clock


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_next_run_starts_at_its_wall_time_and_previous_dates_are_frozen(monkeypatch):
    wall = datetime(2026, 10, 25, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(boundary, '_wall', lambda: wall)
    async def exercise():
        nonlocal wall
        await db.raw.demo_runs.insert_one({'id': 'first', 'phase': 'quote', 'status': 'running', 'clock': clock.initial()})
        await clock.reset_now('first')
        await db.raw.demo_runs.update_one({'id': 'first'}, {'$set': {'phase': 'lifecycle', 'status': 'lifecycle'}})
        await clock.set_clock('first', speed=1440)
        wall += timedelta(minutes=10)
        first = await db.raw.demo_runs.find_one({'id': 'first'})
        assert clock.sim_now(first) == datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
        await db.raw.demo_runs.insert_one({'id': 'second', 'phase': 'quote', 'status': 'running', 'clock': clock.initial()})
        second = await db.raw.demo_runs.find_one({'id': 'second'})
        assert clock.sim_now(second) == wall
        with pytest.raises(ValueError, match='Another lifecycle'):
            await clock.reset_now('second')
        await db.raw.demo_runs.update_one({'id': 'first'}, {'$set': {'status': 'stopped'}})
        await clock.reset_now('second')
        first = await db.raw.demo_runs.find_one({'id': 'first'})
        first_date = clock.sim_now(first)
        second = await db.raw.demo_runs.find_one({'id': 'second'})
        assert clock.sim_now(second) == wall and boundary.demo_now() == wall
        assert second['clock']['speed'] == 1 and not second['clock']['paused']
        await db.raw.demo_runs.update_one({'id': 'second'}, {'$set': {'phase': 'lifecycle', 'status': 'lifecycle'}})
        await clock.set_clock('second', speed=1440)
        wall += timedelta(minutes=5)
        assert clock.sim_now(first) == first_date
        assert boundary.demo_now() == datetime(2026, 10, 30, 9, 10, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match='Cannot reset a lifecycle'):
            await clock.reset_now('second')
        # Restart resumes this run without charging accelerated downtime.
        await boundary._save()
        before = boundary.demo_now()
        boundary._state.clear()
        wall += timedelta(hours=1)
        await boundary.load_clock()
        assert boundary.demo_now() == before
        assert clock.sim_now(first) == first_date
        await db.raw.demo_runs.update_one({'id': 'second'}, {'$set': {'status': 'stopped'}})
        boundary._state.clear()
        await boundary.load_clock()
        assert boundary.demo_now() == wall and boundary.clock_state()['speed'] == 1
        second = await db.raw.demo_runs.find_one({'id': 'second'})
        wall += timedelta(days=2)
        assert clock.sim_now(second) == before
    run(exercise())


def teardown_module():
    run(client.drop_database(db.raw.name))


def test_native_owner_queue_holds_next_step_without_changing_native_states(monkeypatch):
    from core.approvals import request_approval, cancel_approvals
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    wall = datetime(2026,10,25,10,tzinfo=timezone.utc)
    monkeypatch.setattr(boundary,'_wall',lambda:wall)
    async def exercise():
        nonlocal wall
        await db.raw.demo_runs.insert_one({'id':'held-run','phase':'quote','status':'running','quote_id':'held-quote'})
        await clock.reset_now('held-run')
        await db.raw.demo_runs.update_one({'id':'held-run'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock('held-run',speed=1440)
        await db.raw.shipments.insert_one({'id':'held-shipment','quote_id':'held-quote','status':'booked',
            'legs':[{'seq':1,'mode':'road','ata':None},{'seq':2,'mode':'road','ata':None}]})
        wall += timedelta(seconds=1)
        before = boundary.demo_now()
        card = await request_approval('carrier_booking','shipment','held-shipment','Find eligible carrier',
            data={'shipment_id':'held-shipment','leg_seq':1})
        assert boundary.clock_state()['hold']['approval_ids'] == [card['id']]
        assert boundary.clock_state()['paused'] and not boundary.clock_state()['user_paused']
        wall += timedelta(days=3)
        assert boundary.demo_now() == before
        assert (await db.raw.approvals.find_one({'id':card['id']}))['status'] == 'pending'
        boundary._state.clear()
        await boundary.load_clock()
        assert boundary.clock_state()['hold']['approval_ids'] == [card['id']]
        assert boundary.demo_now() == before
        await clock.set_clock('held-run',paused=True)
        await cancel_approvals('held-shipment','carrier_booking')
        await boundary.refresh_clock_hold()
        assert boundary.clock_state()['hold'] is None and boundary.clock_state()['user_paused']
        wall += timedelta(hours=1)
        assert boundary.demo_now() == before
        await clock.set_clock('held-run',paused=False)
        wall += timedelta(seconds=1)
        assert boundary.demo_now() == before + timedelta(seconds=1440)
        await request_approval('carrier_booking','shipment','held-shipment','Later leg review',
            data={'shipment_id':'held-shipment','leg_seq':2})
        await request_approval('claim','claim','unrelated-claim','Settlement review')
        await boundary.refresh_clock_hold()
        assert boundary.clock_state()['hold'] is None
        await db.raw.shipments.update_one({'id':'held-shipment'},{'$set':{'legs.0.ata':wall.isoformat()}})
        await boundary.refresh_clock_hold()
        assert boundary.clock_state()['hold']
        assert (await db.raw.shipments.find_one({'id':'held-shipment'}))['status'] == 'booked'
        await db.raw.demo_runs.update_one({'id':'held-run'},{'$set':{'status':'stopped'}})
        await boundary.refresh_clock_hold()
        assert boundary.clock_state()['hold'] is None
    run(exercise())
