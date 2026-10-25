"""Outside customer-broker actor: signed feed input, never a Tulli declaration."""
import hashlib
import hmac
import json
from io import BytesIO

import httpx

from core import config
from core.db import db
from core.jobs import JobDeferred
from core.util import NO_ID, now_iso
from services.demo_autoquote.events import emit
from . import boundary


def customer_arranged(shipment):
    return shipment.get('customs_handling') in ('broker', 'customer_broker') or \
        shipment.get('customs_filing_status') == 'customer_arranged'


def release_code(shipment):
    return 'export_cleared' if shipment.get('direction') == 'export' else 'customs_released'


async def released(shipment):
    """Require both applied verified ingress and its achieved native milestone."""
    code = release_code(shipment)
    for milestone in shipment.get('milestones') or []:
        if milestone.get('code') != code or not milestone.get('at'):
            continue
        feed = await db.raw.feed_events.find_one({'shipment_id': shipment['id'], 'status': 'applied',
            'verified': True, 'event.shipment_ref': shipment['reference'], 'event.code': code,
            'event.at': milestone['at']}, NO_ID)
        if feed:
            return True
    return False


def report(shipment):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas
    out = BytesIO()
    page = canvas.Canvas(out, pagesize=A4)
    lines = ['SIMULATED - Customer broker release report', 'Shipment: ' + shipment['reference'],
        'Reference: WB-' + shipment['id'], 'Direction: ' + shipment['direction'],
        'Reported at: ' + now_iso(), 'Actor: world-simulated customer broker',
        'The customer-appointed broker reports clearance for this fictional shipment.',
        'This is a simulated broker input, not a Tulli decision or an authority document.',
        'No real broker or customs authority was contacted. No MRN is asserted.']
    y = 790
    for line in lines:
        for text in simpleSplit(line, 'Helvetica', 11, 480):
            page.setFont('Helvetica', 11); page.drawString(50, y, text); y -= 22
    page.save()
    return out.getvalue()


async def signed_feed(event):
    """Use the exact authenticated external route, including its authentication check."""
    from server import app
    secret = config.env('TRACKING_WEBHOOK_SECRET')
    if not secret:
        raise JobDeferred(30, 'Waiting for the signed tracking feed configuration')
    body = json.dumps(event, separators=(',', ':')).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://demo.local', timeout=30) as client:
        response = await client.post('/api/webhooks/tracking/world-broker', content=body,
            headers={'Content-Type': 'application/json', 'X-Huolitsija-Signature': signature})
    if response.status_code != 202:
        raise ValueError('Broker tracking ingress returned ' + str(response.status_code))
    return 'world-broker:' + event['event_id']


async def provide_release(run, shipment):
    if not boundary.on() or not customer_arranged(shipment):
        return {'skipped': True}
    if run.get('phase') != 'lifecycle' or run.get('status') in ('done', 'stopped', 'failed') \
            or shipment.get('quote_id') != run.get('quote_id') or shipment.get('status') in ('cancelled', 'delivered'):
        return {'stopped': True}
    key = 'broker-release:' + shipment['id']
    action = await db.raw.world_actions.find_one({'key': key}, NO_ID)
    if action and action.get('status') == 'delivered':
        return {'duplicate': True}
    if not config.env('TRACKING_WEBHOOK_SECRET'):
        raise JobDeferred(30, 'Waiting for the signed tracking feed configuration')
    from services.documents import create_document
    doc = await db.raw.documents.find_one({'shipment_id': shipment['id'], 'world_broker_key': key}, NO_ID)
    if not doc:
        doc = await create_document(company_id=shipment['company_id'], shipment_id=shipment['id'], doc_type='other',
            filename='SIMULATED-broker-release-' + shipment['reference'] + '.pdf', data=report(shipment),
            actor={'id': 'world-broker', 'type': 'system', 'name': 'World-simulated customer broker'},
            source='world-broker', extract=False,
            extra={'world_broker_key': key, 'simulated': True, 'actor_source': 'world'})
    if not action:
        event = {'event_id': key, 'shipment_ref': shipment['reference'], 'code': release_code(shipment),
            'at': now_iso(), 'note': 'World-simulated customer broker release; document ' + doc['id'],
            'document_id': doc['id'], 'file_id': doc['file_id'], 'simulated': True, 'actor_source': 'world'}
        await db.raw.world_actions.update_one({'key': key}, {'$setOnInsert': {
            'key': key, 'run_id': run['id'], 'actor': 'w:broker', 'status': 'pending', 'event': event}}, upsert=True)
        action = await db.raw.world_actions.find_one({'key': key}, NO_ID)
    feed_key = await signed_feed(action['event'])
    feed = await db.raw.feed_events.find_one({'key': feed_key}, NO_ID)
    if not feed or feed.get('status') != 'applied':
        if feed and feed.get('status') in ('rejected', 'unmatched'):
            raise ValueError('Native tracking rejected the broker release')
        raise JobDeferred(2, 'Waiting for native tracking to apply the signed broker release')
    current = await db.raw.shipments.find_one({'id': shipment['id']}, NO_ID)
    if not current or not await released(current):
        raise JobDeferred(2, 'Waiting for the achieved native broker release milestone')
    await emit(run['id'], 'customs', 'Customer broker release recorded', kind='world', state='done',
        edge=('w:broker', 'customs'), simulated=True, data={
            'facts': {'direction': shipment['direction'], 'real_tulli_contacted': False},
            'documents': [{'id': doc['id'], 'file_id': doc['file_id'], 'filename': doc['filename'],
                'title': 'Simulated customer broker release', 'kind': 'broker_release',
                'content_type': 'application/pdf', 'simulated': True}], 'feed_key': feed_key})
    await db.raw.world_actions.update_one({'key': key}, {'$set': {'status': 'delivered', 'feed_key': feed_key}})
    return {'released': True, 'feed_key': feed_key}
