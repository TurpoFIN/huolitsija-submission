"""Native road/ferry purchase contract, against private Mongo; no external sends or quote requests.

Official location fixtures are read from the project's gazetteer. Geometry I/O and the carrier channel are
controlled; production pricing, shipment splitting, ranking, decision guard, persistence and confirmation run.
"""
import asyncio
import copy
import os
import uuid
from unittest.mock import AsyncMock

os.environ['DB_NAME'] = 'huo_ci_ferry_' + uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['DIGITRAFFIC_ENABLED'] = 'false'

import pytest
from fastapi import HTTPException

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

from core.db import client, db, ensure_indexes
from services import carrier_booking as cb, geo, pricing, shipments, positions
from services.agents import carrier_agent

ACTOR = {'id': 'test-ferry-ops', 'name': 'TEST Ferry Ops', 'org_type': 'internal', 'role': 'ops_admin'}
CID = 'test-ferry-company'
ROAD = 'test-ferry-road-carrier'
CARGO = {'load_type': 'loose', 'packages': 2, 'package_type': 'pallet_eur', 'weight_kg': 600, 'volume_m3': 2.0}


def run(awaitable):
    return LOOP.run_until_complete(awaitable)


def loc(code):
    return geo.location_ref(geo.lookup_code(code))


async def setup():
    await ensure_indexes()
    await db.raw.companies.insert_one({'id': CID, 'name': 'TEST Ferry Customer', 'kyc_status': 'approved',
                                      'sanctions_status': 'clear', 'country': 'FI'})
    await db.raw.carriers.insert_one({'id': ROAD, 'code': 'TESTFERRYROAD', 'name': 'TEST Road Partner', 'active': True,
                                     'modes': ['road'], 'on_time_pct': 95, 'contact_email': 'road@example.test'})


@pytest.fixture(scope='module', autouse=True)
def private_database():
    run(setup())
    yield
    run(client.drop_database(os.environ['DB_NAME']))
    LOOP.close()


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    async def geometry(mode, origin, destination, **kwargs):
        km = geo.haversine_km([origin['lon'], origin['lat']], [destination['lon'], destination['lat']])
        return {'path': [[origin['lon'], origin['lat']], [destination['lon'], destination['lat']]],
                'source': 'test_geometry', 'note': 'Controlled geometry I/O; native route split is exercised', 'distance_km': km}
    monkeypatch.setattr(geo, 'route_info_async', geometry)
    monkeypatch.setattr(positions, 'refresh_shipment', AsyncMock())
    # Exercise the real 0.85 guard, only inside this private DB and without enabling a background worker.
    from core import agent_runtime
    from core.settings import DEFAULTS
    monkeypatch.setattr(agent_runtime, 'get_settings', AsyncMock(return_value={**DEFAULTS, 'autopilot_enabled': True,
        'autopilot_halted': False, 'guard_confidence_floor': .85, 'guard_spend_per_day_cents': 100000000}))


async def make_route(monkeypatch, coverage='all_in', *, quoted_segments=False, origin='FIJYV', destination='NOBGO'):
    tag = uuid.uuid4().hex[:10]
    card = {'id': 'test-card-' + tag, 'carrier_id': ROAD, 'mode': 'road', 'service': 'ROAD_LTL', 'basis': 'PER_SHIPMENT',
            'origin': {'country': 'FI'}, 'destination': {'country': loc(destination)['country']},
            'unit_cost_cents': 73000, 'transit_days': 3, 'surcharges': [], 'via': [], 'source': 'partner_network',
            'status': 'accepted', 'all_in_cost_cents': 73000, 'all_in': True, 'outreach_id': 'test-outreach-' + tag,
            'valid_from': '2020-01-01', 'valid_to': '2099-01-01'}
    if coverage == 'excluded':
        card['ferry_included'] = False
    elif coverage == 'unknown':
        card.update(source='contract', all_in_cost_cents=None, all_in=None)
    elif coverage == 'line':
        card.update(source='contract', all_in_cost_cents=None, all_in=None, unit_cost_cents=60000,
                    surcharges=[{'code': 'FERRY', 'label': 'Ferry crossing', 'basis': 'PER_SHIPMENT', 'unit_cost_cents': 13000}])
    await db.raw.rate_cards.insert_one(copy.deepcopy(card))
    req = {'mode': 'road', 'origin': loc(origin), 'destination': loc(destination), 'cargo': CARGO,
           'direction': 'export', 'incoterm': 'FCA', 'ready_date': '2030-01-15', 'customs_clearance': False}
    async def finish(request, option, settings, company):
        # Retain native purchase lines; avoid unrelated customs/insurance/sell-price calls.
        option.update(id='test-option-' + tag, etd='2030-01-15', eta='2030-01-18', total_cents=90000)
        option['lines'] = option['_lines']
        for line in option['lines']:
            line['cost_cents'] = round(line['unit_cost_cents'] * line['qty'])
            line['unit_price_cents'] = round(line['unit_cost_cents'] * 1.2)
            line['amount_cents'] = round(line['unit_price_cents'] * line['qty'])
            line['vat_code'] = 'ZERO_EXPORT'
        option['total_cents'] = sum(line['amount_cents'] for line in option['lines'])
        return option
    monkeypatch.setattr(pricing, '_finish_option', finish)
    metrics = pricing.cargo_metrics(pricing.normalize_cargo(CARGO))
    option = await pricing._price_path(req, metrics, card, ('direct', 0), None, ('direct', 0),
                                       {ROAD: await db.raw.carriers.find_one({'id': ROAD})}, None, {}, {})
    if quoted_segments:
        option['legs'][0]['segments'] = [
            {'mode': 'road', 'from': origin, 'to': 'FIHEL'},
            {'mode': 'sea', 'from': 'FIHEL', 'to': 'SESTO'},
            {'mode': 'road', 'from': 'SESTO', 'to': destination}]
    quote = {'id': 'test-quote-' + tag, 'company_id': CID, 'status': 'booked', 'request': req,
             'options': [option], 'accepted_option_id': option['id']}
    await db.raw.quotes.insert_one(copy.deepcopy(quote))
    shipment = await shipments.create_shipment(company_id=CID, quote=quote, option=option, booking={}, actor=ACTOR)
    return shipment, option, card


class ControlledChannel:
    name = 'controlled_test_channel'
    mode = 'log'
    def __init__(self):
        self.requests = []
    async def book(self, request):
        self.requests.append(request)
        return {'status': 'requested', 'request': {'test_only': True}, 'response': {'test_only': True}}
    async def cancel(self, booking):
        return {'status': 'cancelled', 'response': {'test_only': True}}


@pytest.mark.parametrize('quoted_segments', [False, True])
@pytest.mark.parametrize('coverage', ['all_in', 'line'])
def test_fijyv_bergen_one_full_route_booking(monkeypatch, quoted_segments, coverage):
    async def scenario():
        shipment, option, card = await make_route(monkeypatch, coverage, quoted_segments=quoted_segments)
        legs = shipment['legs']
        assert [lg['mode'] for lg in legs] == ['road', 'sea', 'road']
        assert [(lg['from']['unlocode'], lg['to']['unlocode']) for lg in legs] == [
            ('FIJYV', 'FIHEL'), ('FIHEL', 'SESTO'), ('SESTO', 'NOBGO')]
        assert legs[1]['carrier'] == 'Tallink Grupp' and legs[1]['carrier_id'] is None
        # Neither FI->FI nor SE->NO is a new road purchase: even with no current lane card, the accepted route remains.
        await db.raw.rate_cards.delete_one({'id': card['id']})
        ranking = await cb.rank_carriers(shipment, legs[0])
        assert ranking['choice']['carrier_id'] == ROAD
        assert ranking['confidence'] >= .85 and ranking['choice']['cost_cents'] == 73000
        channel = ControlledChannel()
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(return_value=channel))
        result = await carrier_agent.book_one_leg(shipment, legs[0], reason='test accepted full route')
        assert result['status'] == 'requested'
        assert len(channel.requests) == 1
        request = channel.requests[0]
        assert request['leg']['from']['unlocode'] == 'FIJYV' and request['leg']['to']['unlocode'] == 'NOBGO'
        assert request['leg']['mode'] == 'road' and request['cost_cents'] == 73000
        assert request['leg']['covered_legs'][1]['operator'] == 'Tallink Grupp'
        assert 'Ferry crossing included in the road carrier booking' in cb._request_mail(request)[2]
        b = await cb.confirm_booking(result['booking_id'], carrier_ref='TEST-ROAD-BOOKING', actor=ACTOR,
                                     details={'eta': '2030-01-19T12:00:00Z'})
        for child in legs[1:]:
            again = await carrier_agent.book_one_leg(shipment, child, reason='test child')
            assert again['booking_id'] == b['id'] and again['status'] == 'confirmed'
        fresh = await db.raw.shipments.find_one({'id': shipment['id']})
        assert all(lg['booking']['id'] == b['id'] and lg['booking']['ref'] == 'TEST-ROAD-BOOKING' for lg in fresh['legs'])
        assert fresh['legs'][0]['eta'] == legs[0]['eta']  # Full-route ETA must not overwrite arrival at Helsinki.
        assert await db.raw.carrier_bookings.count_documents({'shipment_id': shipment['id']}) == 1
        assert sum([row['cost_cents'] async for row in db.raw.carrier_bookings.find({'shipment_id': shipment['id']})]) == 73000
        decision = await db.raw.agent_decisions.find_one({'subject_id': shipment['id'], 'action': 'book_leg'})
        assert decision['outcome'] == 'auto' and decision['guardrails']['confidence_floor'] == .85
        assert decision['spend_cents'] == 73000
        monkeypatch.setattr(cb, '_adapter_for_booking', AsyncMock(return_value=channel))
        await cb.cancel_booking(b['id'], ACTOR, 'test cancellation')
        fresh = await db.raw.shipments.find_one({'id': shipment['id']})
        assert all(lg['booking']['status'] == 'cancelled' for lg in fresh['legs'])
        assert len(channel.requests) == 1
    run(scenario())


@pytest.mark.parametrize('coverage', ['excluded', 'unknown'])
@pytest.mark.parametrize('quoted_segments', [False, True])
def test_crossing_without_coverage_goes_to_person(monkeypatch, coverage, quoted_segments):
    async def scenario():
        shipment, option, card = await make_route(monkeypatch, coverage, quoted_segments=quoted_segments)
        ferry = shipment['legs'][1]
        rank = AsyncMock(side_effect=AssertionError('Uncovered ferry must not be ranked automatically'))
        monkeypatch.setattr(cb, 'rank_carriers', rank)
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(side_effect=AssertionError('No provider booking')))
        booking = AsyncMock(side_effect=AssertionError('No provider booking request'))
        email = AsyncMock(side_effect=AssertionError('No provider email'))
        notification = AsyncMock(side_effect=AssertionError('No partner notification'))
        monkeypatch.setattr(cb, 'book_leg', booking)
        monkeypatch.setattr(cb, 'send_email', email)
        monkeypatch.setattr(cb, 'notify_user', notification)
        result = await carrier_agent.book_one_leg(shipment, ferry, reason='test coverage missing')
        assert result['status'] == 'owner_queue'
        assert result['reason'] == 'ferry crossing needs a separate booking with Tallink Grupp'
        approval = await db.raw.approvals.find_one({'id': result['approval_id']})
        assert approval['data']['reason'] == result['reason'] and approval['data']['leg_seq'] == 2
        assert approval['status'] == 'pending' and approval['data']['suggested_operator'] == 'Tallink Grupp'
        assert await db.raw.approvals.count_documents({'ref_id': shipment['id'], 'kind': 'carrier_booking', 'status': 'pending'}) == 1
        assert result['reason'] in approval['reason']
        assert 'Lauttamatka tarvitsee erillisen varauksen: Tallink Grupp' in approval['reason_fi']
        assert await db.raw.carrier_bookings.count_documents({'shipment_id': shipment['id']}) == 0
        assert not rank.called and not booking.called and not email.called and not notification.called
    run(scenario())


def test_changed_destination_is_outside_accepted_coverage(monkeypatch):
    async def scenario():
        shipment, _, _ = await make_route(monkeypatch)
        shipment['legs'][-1]['to'] = loc('DEHAM')
        await db.raw.shipments.update_one({'id': shipment['id']}, {'$set': {'legs': shipment['legs']}})
        ranking = await cb.rank_carriers(shipment, shipment['legs'][0])
        assert ranking['choice'] is None
        result = await carrier_agent.book_one_leg(shipment, shipment['legs'][0], reason='route amendment')
        assert result['status'] == 'owner_queue' and 'outside the accepted quote' in result['reason']
        with pytest.raises(HTTPException) as exc:
            await cb.book_leg(shipment['id'], 1, ROAD, ACTOR)
        assert exc.value.status_code == 409
    run(scenario())


def test_accepted_quote_does_not_bypass_dg_or_inactive_carrier(monkeypatch):
    async def scenario():
        shipment, _, _ = await make_route(monkeypatch)
        shipment['cargo'] = {**CARGO, 'dangerous_goods': True, 'dg': {'class': '1', 'un_number': 'UN0004'}}
        ranking = await cb.rank_carriers(shipment, shipment['legs'][0])
        assert ranking['choice'] is None
        await db.raw.carriers.update_one({'id': ROAD}, {'$set': {'active': False}})
        try:
            shipment['cargo'] = CARGO
            assert (await cb.rank_carriers(shipment, shipment['legs'][0]))['choice'] is None
        finally:
            await db.raw.carriers.update_one({'id': ROAD}, {'$set': {'active': True}})
    run(scenario())


def test_native_job_does_not_rank_or_book_ferry_separately(monkeypatch):
    async def scenario():
        from core.jobs import JobDeferred
        shipment, _, _ = await make_route(monkeypatch)
        await db.raw.shipments.update_one({'id': shipment['id']}, {'$set': {'booking_confirmation.status': 'confirmed'}})
        channel = ControlledChannel()
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(return_value=channel))
        rank = AsyncMock(wraps=cb.rank_carriers)
        monkeypatch.setattr(cb, 'rank_carriers', rank)
        with pytest.raises(JobDeferred):
            await carrier_agent.select_and_book({'payload': {'shipment_id': shipment['id']}})
        assert len(channel.requests) == 1 and rank.await_count == 1
        b = await db.raw.carrier_bookings.find_one({'shipment_id': shipment['id']})
        await cb.confirm_booking(b['id'], carrier_ref='TEST-NATIVE-JOB', actor=ACTOR)
        assert await carrier_agent.select_and_book({'payload': {'shipment_id': shipment['id']}}) == {
            '1': 'confirmed', '2': 'confirmed', '3': 'confirmed'}
        assert len(channel.requests) == 1 and rank.await_count == 1
        # An amendment outside the purchase cannot retain a confirmed status merely because it shares the old booking.
        s = await db.raw.shipments.find_one({'id': shipment['id']})
        s['legs'][-1]['to'] = loc('DEHAM')
        await db.raw.shipments.update_one({'id': shipment['id']}, {'$set': {'legs': s['legs']}})
        result = await carrier_agent.select_and_book({'payload': {'shipment_id': shipment['id']}})
        assert result == {'1': 'owner_queue', '2': 'owner_queue', '3': 'owner_queue'}
    run(scenario())


def test_free_text_and_route_names_do_not_prove_ferry_coverage():
    assert pricing.ferry_coverage({'service_name': 'All-in ferry; lauttamatka sisältyy', 'notes': 'including ferry'}, [])['included'] is None
    assert pricing.ferry_coverage({'source': 'partner_network', 'status': 'pending', 'all_in_cost_cents': 73000}, [])['included'] is None
    assert pricing.ferry_coverage({'source': 'partner_network', 'status': 'accepted', 'all_in_cost_cents': 73000,
                                  'all_in': False}, [{'code': 'FERRY', 'unit_cost_cents': 100}])['included'] is False


def test_partner_acceptance_on_child_confirms_the_one_booking(monkeypatch):
    async def scenario():
        from routes.partner import partner_accept, AcceptIn
        shipment, _, _ = await make_route(monkeypatch)
        channel = ControlledChannel()
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(return_value=channel))
        # A direct booking call on a covered child must resolve to the same road purchase as the native agent.
        b = await cb.book_leg(shipment['id'], 2, ROAD, ACTOR)
        again = await cb.book_leg(shipment['id'], 3, ROAD, ACTOR)
        assert b['id'] == again['id'] and len(channel.requests) == 1
        user = {'id': 'test-road-partner-user', 'name': 'TEST Road Partner User', 'email': 'road@example.test',
                'carrier_id': ROAD, 'org_type': 'partner', 'role': 'partner'}
        await partner_accept(shipment['id'], 3, AcceptIn(truck_plate='TEST-123'), user)
        fresh = await db.raw.shipments.find_one({'id': shipment['id']})
        assert all(lg['booking']['status'] == 'confirmed' for lg in fresh['legs'])
        assert await db.raw.carrier_bookings.count_documents({'shipment_id': shipment['id']}) == 1
    run(scenario())


def test_legacy_option_without_legs_keeps_full_accepted_scope(monkeypatch):
    async def scenario():
        shipment, option, card = await make_route(monkeypatch)
        quote = await db.raw.quotes.find_one({'id': shipment['quote_id']})
        option.pop('legs')
        option['id'] += '-legacy'
        quote.update(id=quote['id']+'-legacy', options=[option], accepted_option_id=option['id'])
        quote.pop('_id', None)
        await db.raw.quotes.insert_one(copy.deepcopy(quote))
        legacy = await shipments.create_shipment(company_id=CID, quote=quote, option=option, booking={}, actor=ACTOR)
        ranking = await cb.rank_carriers(legacy, legacy['legs'][0])
        assert ranking['choice']['carrier_id'] == ROAD and ranking['choice']['cost_cents'] == 73000
        assert ranking['confidence'] >= .85
        assert legacy['legs'][1]['booking_parent_seq'] == 1
    run(scenario())


@pytest.mark.parametrize('operation', ['rebook', 'cancel', 'amend', 'reprice_quote', 'reprice_booking',
                                       'cancel_shipment', 'cancel_status', 'edit_quote', 'requote'])
def test_started_group_mutations_preserve_booking_and_queue_one_person(monkeypatch, operation):
    async def scenario():
        from routes import quotes
        from core import email, notify, agent_runtime
        shipment, option, _ = await make_route(monkeypatch)
        channel = ControlledChannel()
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(return_value=channel))
        b = await cb.book_leg(shipment['id'], 1, ROAD, ACTOR)
        await cb.confirm_booking(b['id'], carrier_ref='TEST-STARTED-GROUP', actor=ACTOR)
        # Exact reviewer scenario: native pickup starts the first road leg; the upcoming ferry has not departed.
        await shipments.record_milestone(shipment['id'], 'picked_up', actor=ACTOR, source='test_private', leg_seq=1)
        before = await db.raw.shipments.find_one({'id': shipment['id']})
        quote_before = await db.raw.quotes.find_one({'id': shipment['quote_id']})
        booking_before = await db.raw.carrier_bookings.find_one({'id': b['id']})
        assert before['legs'][0]['atd'] and not before['legs'][1]['atd']
        # An actual owner user makes a mistaken owner-email notification observable too (queue stays in-app).
        await db.raw.users.update_one({'id': 'test-owner'}, {'$set': {'role': 'ops_admin', 'org_type': 'internal',
            'email': 'owner@example.test', 'name': 'TEST Owner'}}, upsert=True)
        fail = AsyncMock(side_effect=AssertionError('Started group must not reach any adapter/send/price path'))
        monkeypatch.setattr(cb, '_adapter_for_booking', fail)
        monkeypatch.setattr(cb, 'adapter_for', fail)
        monkeypatch.setattr(cb, 'rank_carriers', fail)
        monkeypatch.setattr(cb, 'send_email', fail)
        monkeypatch.setattr(email, 'send_email', fail)
        monkeypatch.setattr(notify, 'send_email', fail)
        monkeypatch.setattr(quotes, '_price', fail)
        async def mutate():
            if operation == 'rebook':
                return await cb.rebook_leg(shipment['id'], 2, ACTOR, reason='reschedule ferry')
            if operation == 'cancel':
                return await cb.cancel_booking(b['id'], ACTOR, 'cancel group')
            if operation == 'amend':
                return await shipments.update_leg(shipment['id'], 3, {'eta': '2030-01-20T12:00:00Z'}, ACTOR)
            if operation == 'reprice_quote':
                return await quotes.reprice_in_place(copy.deepcopy(quote_before), ACTOR)
            if operation == 'reprice_booking':
                return await cb.book_leg(shipment['id'], 1, ROAD, ACTOR, cost_cents=74000)
            if operation == 'cancel_shipment':
                return await shipments.cancel_shipment(shipment['id'], actor=ACTOR, reason='cancel shipment')
            if operation == 'cancel_status':
                return await shipments.set_status(shipment['id'], 'cancelled', actor=ACTOR, note='cancel status')
            if operation == 'edit_quote':
                return await quotes.patch_quote(shipment['quote_id'], quotes.QuotePatch(note='amend purchase'), None, ACTOR)
            return await quotes.requote(shipment['quote_id'], ACTOR)
        for _ in range(2):  # retries reuse one pending person decision rather than send or mutate anything
            with pytest.raises(HTTPException) as exc:
                await mutate()
            assert exc.value.status_code == 409 and 'Group booking already in transit' in exc.value.detail
        assert not fail.called
        assert len(channel.requests) == 1  # initial fixture setup only; no replacement or cancellation
        assert await db.raw.shipments.find_one({'id': shipment['id']}) == before
        assert await db.raw.quotes.find_one({'id': shipment['quote_id']}) == quote_before
        assert await db.raw.carrier_bookings.find_one({'id': b['id']}) == booking_before
        assert all(lg['booking']['status'] == 'confirmed' for lg in before['legs'])
        approvals = await db.raw.approvals.find({'ref_id': shipment['id'], 'status': 'pending'}).to_list(None)
        assert len(approvals) == 1
        approval = approvals[0]
        assert 'Group booking already in transit' in approval['reason']
        assert 'Ryhmävarauksen kuljetus on jo alkanut' in approval['reason_fi']
        assert approval['data']['manual_group_change'] is True
        assert await db.raw.email_outbox.count_documents({'company_id': CID, 'ref.shipment_id': shipment['id']}) == 0
    run(scenario())


@pytest.mark.parametrize('started_seq', [1, 2, 3])
def test_started_guard_checks_every_member_not_only_parent(monkeypatch, started_seq):
    async def scenario():
        shipment, _, _ = await make_route(monkeypatch)
        # Partial/out-of-order tracking data: only the indicated drawing leg says it has started.
        shipment['legs'][started_seq-1]['status'] = 'in_progress'
        with pytest.raises(HTTPException) as exc:
            await cb.guard_started_group(shipment, shipment['legs'][1], operation='rebook')
        assert exc.value.status_code == 409
        row = await db.raw.agent_decisions.find_one({'subject_id': shipment['id'], 'action': 'change_started_group'})
        assert row['inputs']['started_leg_seqs'] == [started_seq]
    run(scenario())


def test_started_group_forward_webhooks_to_delivery_pod_and_invoice_need_no_person(monkeypatch):
    async def scenario():
        import hashlib
        import hmac
        import json
        import httpx
        from fastapi import FastAPI
        from routes import webhooks_feeds
        from services.agents import tracking_agent, invoice_agent
        from services import billing
        shipment, option, _ = await make_route(monkeypatch)
        await db.raw.companies.update_one({'id': CID}, {'$set': {'credit_limit_cents': 100000000,
            'payment_terms_days': 14, 'vat_registered': True, 'country': 'FI'}})
        channel = ControlledChannel()
        monkeypatch.setattr(cb, 'adapter_for', AsyncMock(return_value=channel))
        b = await cb.book_leg(shipment['id'], 1, ROAD, ACTOR)
        await cb.confirm_booking(b['id'], carrier_ref='TEST-FORWARD-GROUP', actor=ACTOR)
        monkeypatch.setenv('TRACKING_WEBHOOK_SECRET', 'test-ferry-webhook-only')
        app = FastAPI()
        app.include_router(webhooks_feeds.router)
        states = []
        async def feed(code, leg_seq, **extra):
            eid = uuid.uuid4().hex
            event = {'event_id': eid, 'shipment_ref': shipment['reference'], 'code': code, 'leg_seq': leg_seq,
                     'at': '2030-01-16T12:00:00Z', **extra}
            body = json.dumps(event).encode()
            signature = hmac.new(b'test-ferry-webhook-only', body, hashlib.sha256).hexdigest()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.local') as c:
                response = await c.post('/api/webhooks/tracking/test-road', content=body,
                    headers={'Content-Type': 'application/json', 'X-Huolitsija-Signature': signature})
            assert response.status_code == 202
            await tracking_agent.ingest({'payload': {'key': 'test-road:' + eid}})
            record = await db.raw.feed_events.find_one({'key': 'test-road:' + eid})
            assert record['status'] == 'applied'
            fresh = await db.raw.shipments.find_one({'id': shipment['id']})
            states.append(fresh['status'])
            return fresh
        assert (await feed('picked_up', 1))['status'] == 'picked_up'
        assert (await feed('departed', 1))['status'] == 'in_transit'
        fresh = await feed('departed', 2)
        assert fresh['legs'][1]['atd'] and fresh['legs'][0]['status'] == 'completed'
        # A verified carrier's ETA observation updates tracking, not the accepted purchase contract.
        await feed('eta', 2, eta='2030-01-17T12:00:00Z')
        fresh = await feed('arrived', 2)
        assert fresh['status'] == 'at_hub'
        assert fresh['legs'][1]['ata'] and fresh['legs'][1]['status'] == 'completed'
        assert (await feed('out_for_delivery', 3))['status'] == 'out_for_delivery'
        fresh = await feed('delivered', 3, pod={'signed_by': 'TEST Bergen Recipient', 'at': '2030-01-18T12:00:00Z'})
        assert fresh['status'] == 'delivered' and all(leg['status'] == 'completed' for leg in fresh['legs'])
        pod = await db.raw.documents.find_one({'shipment_id': shipment['id'], 'type': 'pod'})
        assert pod and pod['file_id'] and pod['size'] > 0
        # Invoice machinery is real; its outward delivery boundary remains controlled in this regression.
        invoice_send = AsyncMock(return_value=[])
        monkeypatch.setattr(billing, 'send_invoice_email', invoice_send)
        result = await invoice_agent.issue_on_delivery({'payload': {'shipment_id': shipment['id']}})
        inv = await db.raw.invoices.find_one({'shipment_id': shipment['id'], 'kind': 'invoice'})
        assert result['issued'] == inv['number'] and inv['number'] and inv['status'] == 'open'
        assert inv['total_cents'] == option['total_cents'] > 0 and inv['pdf_file_id'] and inv['reference']
        assert not inv['warnings']
        assert invoice_send.await_count == 1
        assert await db.raw.approvals.count_documents({'$or': [{'ref_id': shipment['id']}, {'ref_id': inv['id']}]}) == 0
        assert await db.raw.agent_decisions.count_documents({'subject_id': {'$in': [shipment['id'], inv['id']]},
            'outcome': {'$ne': 'auto'}}) == 0
        assert await db.raw.agent_decisions.count_documents({'subject_id': shipment['id'], 'action': 'change_started_group'}) == 0
        assert len(channel.requests) == 1
    run(scenario())
