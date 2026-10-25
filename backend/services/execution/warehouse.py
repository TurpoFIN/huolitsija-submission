"""Warehouse orders over SMTP or Odoo. Receipt/dispatch needs actual warehouse evidence."""
import json
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from core import config
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now_iso
from services.execution import mail, wms_odoo
from services.execution.common import shipment, positive, allowed
from services.execution.runtime import AGENT_ACTOR, enqueue, job, schedule, sign_link, read_link
register_index('warehouse_orders', [('shipment_id', 1), ('kind', 1), ('warehouse_id', 1)], unique=True)
register_index('warehouses', 'id', unique=True)

async def create_order(shipment_id, kind, warehouse_id, actor=None):
    if kind not in ('inbound', 'outbound'): raise HTTPException(422, 'Unknown warehouse order kind')
    s = await shipment(shipment_id)
    wh = await db.warehouses.find_one({'id': warehouse_id}, NO_ID)
    if not wh: raise HTTPException(404, 'Warehouse not found')
    from core.test_counterparty import check
    check(wh, s, 'warehouse')
    key = {'shipment_id': shipment_id, 'kind': kind, 'warehouse_id': warehouse_id}
    old = await db.warehouse_orders.find_one(key, NO_ID)
    if old: return old
    packages = positive((s.get('cargo') or {}).get('packages'), 'package count')
    row = {**key, 'id': new_id(), 'reference': s['reference'], 'company_id': s.get('company_id'), 'packages': packages,
           'warehouse_name': wh['name'], 'status': 'draft', 'provider': wh.get('provider') or config.env('WMS_PROVIDER', 'none'),
           'created_at': now_iso(), 'updated_at': now_iso(), 'history': []}
    try: await db.warehouse_orders.insert_one(dict(row))
    except DuplicateKeyError: return await db.warehouse_orders.find_one(key, NO_ID)
    return await dispatch(row['id'], actor)

async def dispatch(oid, actor=None):
    row = await db.warehouse_orders.find_one({'id': oid}, NO_ID)
    if not row: raise HTTPException(404, 'Warehouse order not found')
    await shipment(row['shipment_id'])
    if row['status'] not in ('draft', 'failed', 'awaiting_approval'): return row
    if not await allowed('warehouse_send', row, actor):
        await db.warehouse_orders.update_one({'id': oid}, {'$set': {'status': 'awaiting_approval'}})
        return await db.warehouse_orders.find_one({'id': oid}, NO_ID)
    wh = await db.warehouses.find_one({'id': row['warehouse_id']}, NO_ID)
    claimed = await db.warehouse_orders.update_one({'id': oid, 'status': row['status']}, {'$set': {'status': 'sending'}})
    if not claimed.modified_count: return await db.warehouse_orders.find_one({'id': oid}, NO_ID)
    sets = {'status': 'failed', 'updated_at': now_iso()}
    try:
        if row['provider'] == 'email':
            if not wh.get('email'): raise ValueError('Warehouse email is missing')
            token = sign_link('warehouse', {'id': oid, 'warehouse_id': row['warehouse_id']})
            url = f'{config.APP_BASE_URL}/warehouse-reply?t={token}'
            text = f"{row['kind']} order {row['reference']}\nPackages: {row['packages']}\nReport actual receipt/dispatch: {url}"
            m = await mail.send(wh['email'], f"Warehouse {row['kind']} {row['reference']}", text,
                kind='warehouse', ref_type='warehouse_order', ref_id=oid, reference=row['reference'], company_id=row.get('company_id'),
                attachments=[('warehouse-order.json', 'application/json', json.dumps(row).encode())])
            sets.update(email_status=m['status'], message_id=m['message_id'])
            if m['status'] != 'sent': raise ValueError('Warehouse order was not sent; configure SMTP')
            sets['status'] = 'submitted'
        elif row['provider'] == 'odoo':
            result = await wms_odoo.send(row, wh)
            sets.update(status='submitted', external_id=result['id'], external_reference=result['name'], external_state=result['state'])
        else: raise ValueError('WMS_PROVIDER is not configured (none)')
    except Exception as exc:
        sets['error'] = str(exc)[:500]
    await db.warehouse_orders.update_one({'id': oid}, {'$set': sets})
    await audit(actor or AGENT_ACTOR, 'execution.warehouse_' + sets['status'], 'shipment', row['shipment_id'],
                {'order_id': oid, 'provider': row['provider'], 'external_reference': sets.get('external_reference')})
    return await db.warehouse_orders.find_one({'id': oid}, NO_ID)

async def milestone(oid, packages, evidence, actor=None, token=None, source='ops'):
    row = await db.warehouse_orders.find_one({'id': oid}, NO_ID)
    if not row: raise HTTPException(404, 'Warehouse order not found')
    if token:
        if read_link('warehouse', token) != {'id': oid, 'warehouse_id': row['warehouse_id']}:
            raise HTTPException(403, 'Invalid warehouse receipt link')
        source = 'warehouse_signed_link'
    elif not actor: raise HTTPException(403, 'Warehouse identity required')
    await shipment(row['shipment_id'])
    if row['status'] not in ('submitted', 'discrepancy'): raise HTTPException(409, 'Order is not awaiting a warehouse result')
    if not str(evidence or '').strip(): raise HTTPException(422, 'Actual warehouse receipt/dispatch reference is required')
    qty = positive(packages, 'actual package count')
    target = ('received' if row['kind'] == 'inbound' else 'dispatched') if qty == row['packages'] else 'discrepancy'
    event = {'at': now_iso(), 'status': target, 'packages': qty, 'evidence': evidence, 'source': source}
    result = await db.warehouse_orders.find_one_and_update({'id': oid, 'status': row['status'], 'updated_at': row['updated_at']},
        {'$set': {'status': target, 'actual_packages': qty, 'updated_at': now_iso(), target + '_at': now_iso()}, '$push': {'history': event}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Warehouse result changed; refresh')
    await audit(actor or {'type': 'warehouse', 'id': row['warehouse_id']}, 'execution.warehouse_' + target, 'shipment', row['shipment_id'], event)
    await emit('warehouse.' + target, {'order': result})
    return result

@job('execution.warehouse_send', agent=True)
async def send_job(j): return await dispatch(j['payload']['id'])

@job('execution.wms_sync', agent=True)
async def sync_job(j):
    n = 0
    async for row in db.warehouse_orders.find({'provider': 'odoo', 'status': 'submitted', 'external_id': {'$exists': True}}, NO_ID):
        result = await wms_odoo.status(row['external_id'])
        await db.warehouse_orders.update_one({'id': row['id']}, {'$set': {'external_state': result['state']}})
        if result['state'] == 'done':
            await milestone(row['id'], result['packages'], result['name'] + ' / ' + str(result['date_done']), AGENT_ACTOR, source='odoo'); n += 1
    return {'milestones': n}
schedule('execution.wms_sync', job='execution.wms_sync', every_seconds=300)()

@on('carrier.booked')
async def booking(p):
    s = p.get('shipment') or {}
    for item in s.get('warehouse_plan') or []:
        await create_order(s['id'], item['kind'], item['warehouse_id'])

@on('approval.decided')
async def approval(p):
    a = p.get('approval') or {}; data = a.get('data') or {}
    if a.get('kind') == 'execution_review' and a.get('status') == 'approved' and data.get('action') == 'warehouse_send':
        await dispatch(data['execution_id'], p.get('actor'))
