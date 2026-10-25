"""A rerun of a halted run's shipment takes over its rate requests and replies: no partner is asked twice. Real MongoDB
(its own database), no mail, HTTP or model calls."""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ['DB_NAME'] = 'huo_rfq_reuse_test_' + uuid.uuid4().hex[:10]
from core.db import db, client  # noqa: E402
from services.demo_autoquote import driver  # noqa: E402

PROFILE = {'cargo_description': 'parts', 'packages': '5 EUR pallets', 'weight_kg': '2100', 'ready_date': '2026-11-04',
           'pickup_address': '90100 Oulu', 'delivery_address': '5003 Bergen'}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_rerun_takes_over_rate_requests_and_replies_without_a_second_request(monkeypatch):
    events = []
    monkeypatch.setattr(driver, 'emit', AsyncMock(side_effect=lambda *a, **k: events.append((a, k))))
    from core import email
    monkeypatch.setattr(email, 'send_email', AsyncMock(side_effect=AssertionError('no mail on reuse')))

    async def exercise():
        old = {'id': 'halted', 'ref': 'DQ-HALTED', 'status': 'stopped', 'phase': 'quote', 'origin_text': 'Oulu, Finland',
               'destination_text': 'Bergen, Norway', 'profile': {**PROFILE, 'company': 'World Oy'},
               'recipients': ['a@partner.invalid', 'b@partner.invalid']}
        await db.raw.demo_runs.insert_one(dict(old))
        for pid, address in (('p1', 'a@partner.invalid'), ('p2', 'b@partner.invalid')):
            await db.raw.partner_outreach.insert_one({'id': pid, 'demo_run_id': 'halted', 'carrier_name': pid, 'contact_email': address,
                                                      'created_at': pid, 'log': []})
            await db.raw.partner_messages.insert_one({'id': f'{pid}-rfq', 'outreach_id': pid, 'direction': 'out', 'kind': 'rfq',
                                                      'status': 'sent', 'demo_run_id': 'halted', 'subject': 'RFQ', 'at': 't0'})
            await db.raw.partner_messages.insert_one({'id': f'{pid}-reply', 'outreach_id': pid, 'direction': 'in',
                                                      'demo_seen': True, 'demo_run_id': 'halted', 'seq': 1})
        new = {'id': 'rerun', 'origin_text': 'Oulu, Finland', 'destination_text': 'Bergen, Norway', 'profile': dict(PROFILE)}
        assert await driver._check_reuse('halted', new) == 'halted'
        for bad, why in (({**new, 'destination_text': 'Tromsø, Norway'}, 'different shipment'),
                         ({**new, 'profile': {**PROFILE, 'weight_kg': '900'}}, 'different shipment')):
            with pytest.raises(HTTPException, match=why):
                await driver._check_reuse('halted', bad)
        await db.raw.demo_runs.insert_one({'id': 'rerun', 'ref': 'DQ-RERUN', 'status': 'running'})
        rows = await driver._reuse_outreach('rerun', 'halted')
        assert [r['id'] for r in rows] == ['p1', 'p2']
        assert await db.raw.partner_outreach.count_documents({'demo_run_id': 'rerun', 'reused_from_run': 'halted'}) == 2
        # Every earlier reply is read again by the new run; the rate requests themselves stay as they were sent.
        assert await db.raw.partner_messages.count_documents({'direction': 'in', 'demo_seen': False, 'reused_by_run': 'rerun'}) == 2
        assert await db.raw.partner_messages.count_documents({'kind': 'rfq', 'demo_run_id': 'halted', 'reused_by_run': 'rerun'}) == 2
        rerun = await db.raw.demo_runs.find_one({'id': 'rerun'})
        assert rerun['rfq_reuse'] == {'from_run': 'halted', 'from_ref': 'DQ-HALTED', 'outreach_ids': ['p1', 'p2'],
                                      'recipients': 2, 'new_rfqs': 0}
        assert rerun['recipients'] == old['recipients']
        assert any('2 rate requests reused · 0 new' in a[2] for a, _ in events)
        with pytest.raises(HTTPException, match='already reused'):  # a second rerun cannot take them again
            await driver._check_reuse('halted', new)
        with pytest.raises(HTTPException, match='still live'):  # the rerun itself is live: nothing to take over yet
            await driver._check_reuse('rerun', {**new, 'id': 'third'})
        # Once the rerun has ended (even with its offer sent), a third run of the shipment takes them over from it.
        await db.raw.demo_runs.update_one({'id': 'rerun'}, {'$set': {'status': 'done', 'offer': {'quote_number': 'Q-1'},
            'origin_text': new['origin_text'], 'destination_text': new['destination_text'], 'profile': dict(PROFILE)}})
        assert await driver._check_reuse('rerun', {**new, 'id': 'third'}) == 'rerun'
        await db.raw.demo_runs.insert_one({'id': 'third', 'ref': 'DQ-THIRD', 'status': 'running'})
        assert [r['id'] for r in await driver._reuse_outreach('third', 'rerun')] == ['p1', 'p2']
        assert await db.raw.partner_messages.count_documents({'kind': 'rfq', 'reused_by_run': 'third'}) == 2
        await client.drop_database(os.environ['DB_NAME'])
    run(exercise())
