"""Pickup/delivery appointments: actual partner responses, never inferred confirmations."""
import json
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from core import config
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now_iso, now, parse_iso
from services.execution import mail
from services.execution.common import shipment, window, allowed
from services.execution.runtime import AGENT_ACTOR, job, schedule, enqueue, JobDeferred, sign_link, read_link, raise_alert

register_index('slot_bookings', [('shipment_id', 1), ('leg_seq', 1), ('kind', 1)], unique=True)
register_index('slot_bookings', 'id', unique=True)

async def request_slot(shipment_id, leg_seq, kind, window, actor=None):
    from services.execution.common import window as validate_window
    if kind not in ('pickup', 'delivery'):
        raise HTTPException(422, 'Slot kind must be pickup or delivery')
    win = validate_window(window)
    s = await shipment(shipment_id)
    lg = next((l for l in s.get('legs', []) if l['seq'] == leg_seq), None)
    if not lg or not lg.get('carrier_id'):
        raise HTTPException(409, 'Assign a haulier to this leg first')
    key = {'shipment_id': shipment_id, 'leg_seq': leg_seq, 'kind': kind}
    row = await db.slot_bookings.find_one(key, NO_ID)
    if row:
        return row
    carrier = await db.raw.carriers.find_one({'id': lg['carrier_id']}, NO_ID) or {}
    from core.test_counterparty import check
    check(carrier, s, 'haulier')
    email = carrier.get('contact_email') or carrier.get('booking_desk_email') or carrier.get('booking_email') or carrier.get('email')
    row = {**key, 'id': new_id(), 'reference': s['reference'], 'company_id': s.get('company_id'),
           'carrier_id': lg['carrier_id'], 'carrier_name': carrier.get('name') or lg.get('carrier'),
           'email': email, 'window': win, 'status': 'draft', 'revision': 0,
           'from': lg.get('from'), 'to': lg.get('to'), 'created_at': now_iso(), 'updated_at': now_iso(), 'history': []}
    try:
        await db.slot_bookings.insert_one(dict(row))
    except DuplicateKeyError:
        return await db.slot_bookings.find_one(key, NO_ID)
    return await dispatch(row['id'], actor)

async def dispatch(slot_id, actor=None):
    row = await db.slot_bookings.find_one({'id': slot_id}, NO_ID)
    if not row: raise HTTPException(404, 'Slot not found')
    await shipment(row['shipment_id'])
    if row['status'] not in ('draft', 'awaiting_approval', 'send_failed'): return row
    if not await allowed('slot_request', row, actor):
        await db.slot_bookings.update_one({'id': slot_id}, {'$set': {'status': 'awaiting_approval'}})
    else:
        claimed = await db.slot_bookings.find_one_and_update({'id': slot_id, 'status': row['status']},
                      {'$set': {'status': 'sending'}}, return_document=ReturnDocument.AFTER)
        if not claimed: return await db.slot_bookings.find_one({'id': slot_id}, NO_ID)
        # Portal is a real authenticated delivery surface even when SMTP is not configured.
        token = sign_link('slot', {'id': slot_id, 'carrier_id': row['carrier_id'], 'revision': row['revision']})
        url = f'{config.APP_BASE_URL}/slot-reply?t={token}'
        email_status = 'not_configured'
        if row.get('email'):
            m = await mail.send(row['email'], f"{row['kind']} appointment {row['reference']}",
                 f"Requested: {row['window']['start']} to {row['window']['end']}\nConfirm, propose another window or reject: {url}\n"
                 'A request is not a confirmed appointment.', kind='slot', ref_type='slot_booking', ref_id=slot_id,
                 reference=row['reference'], company_id=row.get('company_id'))
            email_status = m['status']
        await db.slot_bookings.update_one({'id': slot_id}, {'$set': {'status': 'requested', 'email_status': email_status,
             'requested_at': now_iso(), 'updated_at': now_iso()}})
        await audit(actor or AGENT_ACTOR, 'execution.slot_requested', 'shipment', row['shipment_id'], {'slot_id': slot_id})
        await emit('slot.requested', {'slot': row})
    return await db.slot_bookings.find_one({'id': slot_id}, NO_ID)

async def respond(slot_id, action, *, carrier_id=None, token=None, proposed=None, note='', actor=None):
    row = await db.slot_bookings.find_one({'id': slot_id}, NO_ID)
    if not row: raise HTTPException(404, 'Slot not found')
    if token:
        claims = read_link('slot', token)
        if not claims or claims != {'id': slot_id, 'carrier_id': row['carrier_id'], 'revision': row['revision']}:
            raise HTTPException(403, 'Expired or replaced appointment link')
    elif carrier_id != row['carrier_id']:
        raise HTTPException(404, 'No appointment assigned to this carrier')
    await shipment(row['shipment_id'])
    if row['status'] not in ('requested', 'proposed'):
        raise HTTPException(409, 'Appointment is no longer awaiting a response')
    if action == 'confirm' and row['status'] == 'proposed':
        raise HTTPException(409, 'The proposed window must be accepted by the forwarder')
    if action not in ('confirm', 'propose', 'reject'):
        raise HTTPException(422, 'Unknown response')
    sets = {'status': {'confirm': 'confirmed', 'propose': 'proposed', 'reject': 'rejected'}[action], 'updated_at': now_iso()}
    if action == 'propose': sets['proposed_window'] = window(proposed)
    if action == 'confirm': sets['confirmed_at'] = now_iso()
    event = {'at': now_iso(), 'action': action, 'note': note[:1000], 'source': 'signed_link' if token else 'partner_portal'}
    result = await db.slot_bookings.find_one_and_update({'id': slot_id, 'revision': row['revision']},
             {'$set': sets, '$inc': {'revision': 1}, '$push': {'history': event}}, projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Appointment changed; refresh it')
    await audit(actor or {'type': 'partner', 'id': row['carrier_id']}, 'execution.slot_' + action, 'shipment', row['shipment_id'], event)
    await emit('slot.' + sets['status'], {'slot': result})
    if action == 'propose':
        await enqueue('execution.slot_accept', {'id': slot_id}, key=f"slot-accept:{slot_id}:{result['revision']}")
    return result

async def accept_proposal(slot_id, actor=None):
    row = await db.slot_bookings.find_one({'id': slot_id}, NO_ID)
    if not row or row['status'] != 'proposed': raise HTTPException(409, 'No proposed window')
    await shipment(row['shipment_id'])
    proposed = row['proposed_window']
    # Automated changes must stay inside the original customer-authorised window.
    inside = proposed['start'] >= row['window']['start'] and proposed['end'] <= row['window']['end']
    from core.settings import get_settings
    if not actor and (not inside or not (await get_settings()).get('execution_slot_auto_accept')):
        await raise_alert('slot_proposal', f"slot-proposal:{slot_id}:{row['revision']}", 'Kuljetusaika muuttui', 'Appointment proposal needs review',
                          subject=('shipment', row['shipment_id']), link='/ops/execution')
        return row
    if not await allowed('slot_accept', row, actor): return row
    result = await db.slot_bookings.find_one_and_update({'id': slot_id, 'status': 'proposed', 'revision': row['revision']},
        {'$set': {'status': 'confirmed', 'window': proposed, 'confirmed_at': now_iso(), 'updated_at': now_iso()}, '$inc': {'revision': 1}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Appointment changed')
    await audit(actor or AGENT_ACTOR, 'execution.slot_confirmed', 'shipment', row['shipment_id'], {'slot_id': slot_id, 'window': proposed})
    await emit('slot.confirmed', {'slot': result})
    return result

@job('execution.slot_accept', agent=True)
async def accept_job(j): return await accept_proposal(j['payload']['id'])

@job('execution.slot_start', agent=True)
async def start_job(j):
    s = await shipment(j['payload']['shipment_id'])
    n = 0
    for lg in s.get('legs', []):
        for kind in ('pickup', 'delivery'):
            win = lg.get(kind + '_window')
            if win and lg.get('carrier_id'):
                await request_slot(s['id'], lg['seq'], kind, win); n += 1
    return {'requested': n, 'note': None if n else 'No explicit appointment windows supplied'}

@on('carrier.booked')
async def booking_event(p):
    sid = (p.get('shipment') or {}).get('id') or p.get('shipment_id')
    if sid: await enqueue('execution.slot_start', {'shipment_id': sid}, key=f'slots:{sid}')

@on('carrier.booking_cancelled')
async def cancel_event(p):
    sid = (p.get('shipment') or {}).get('id') or p.get('shipment_id')
    if sid:
        await db.slot_bookings.update_many({'shipment_id': sid, 'status': {'$nin': ['rejected', 'cancelled']}},
            {'$set': {'status': 'cancelled', 'updated_at': now_iso()}, '$inc': {'revision': 1}})

@job('execution.slot_chase', agent=True)
async def chase_job(j):
    n = 0
    async for row in db.slot_bookings.find({'status': 'requested'}, NO_ID):
        if (now() - parse_iso(row['requested_at'])).total_seconds() >= 86400:
            await raise_alert('slot_unanswered', 'slot-unanswered:' + row['id'], 'Kuljetusaika vahvistamatta', 'Appointment not confirmed',
                              subject=('shipment', row['shipment_id']), link='/ops/execution'); n += 1
    return {'unanswered': n}
schedule('execution.slot_chase', job='execution.slot_chase', every_seconds=900)()

@on('approval.decided')
async def approval_event(p):
    a = p.get('approval') or {}; data = a.get('data') or {}
    if a.get('kind') == 'execution_review' and a.get('status') == 'approved':
        if data.get('action') == 'slot_request': await dispatch(data['execution_id'], p.get('actor'))
        if data.get('action') == 'slot_accept': await accept_proposal(data['execution_id'], p.get('actor'))
