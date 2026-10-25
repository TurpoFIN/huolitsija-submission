"""Capacity-limited LCL/groupage load plans with exclusive house assignment.

A volume/weight allocation is not a physical stowage certification. Actual loading and
arrival come from the terminal/warehouse; the planner never fabricates either.
"""
import csv
import io
from fastapi import HTTPException
from pymongo import ReturnDocument
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now_iso, parse_iso, now
from services.execution.common import positive, shipment, allowed
from services.execution.runtime import AGENT_ACTOR, job, schedule
register_index('consolidations', 'id', unique=True)

def cargo_facts(s):
    c = s.get('cargo') or {}
    if c.get('dangerous_goods') or c.get('dg') or s.get('dangerous_goods') or s.get('dg_review_required'):
        raise HTTPException(409, 'Dangerous goods need an approved segregation plan')
    return {'shipment_id': s['id'], 'reference': s['reference'],
            'weight_kg': positive(c.get('weight_kg'), 'cargo weight'), 'volume_m3': positive(c.get('volume_m3'), 'cargo volume'),
            'packages': positive(c.get('packages'), 'package count')}

def location(s, end):
    p = s.get(end) or {}
    return p.get('code') or p.get('unlocode')

async def create(origin_hub, destination_hub, mode, capacity_kg, capacity_m3, cutoff, actor=None, master_shipment_id=None):
    if not origin_hub or not destination_hub or origin_hub == destination_hub:
        raise HTTPException(422, 'Two different hubs are required')
    if mode not in ('sea', 'road', 'rail'): raise HTTPException(422, 'Unsupported groupage mode')
    if not parse_iso(cutoff): raise HTTPException(422, 'A valid consolidation cut-off is required')
    row = {'id': new_id(), 'reference': 'CON-' + new_id()[:8].upper(), 'origin_hub': origin_hub, 'destination_hub': destination_hub,
           'mode': mode, 'capacity_kg': positive(capacity_kg, 'weight capacity'), 'capacity_m3': positive(capacity_m3, 'volume capacity'),
           'cutoff': cutoff, 'master_shipment_id': master_shipment_id, 'houses': [], 'weight_kg': 0, 'volume_m3': 0,
           'status': 'open', 'revision': 0, 'created_at': now_iso(), 'updated_at': now_iso()}
    if master_shipment_id: await shipment(master_shipment_id)
    await db.consolidations.insert_one(dict(row))
    await audit(actor or AGENT_ACTOR, 'execution.consolidation_created', 'consolidation', row['id'], {})
    return row

async def assign(cid, sid, actor=None):
    row = await db.consolidations.find_one({'id': cid}, NO_ID)
    if not row or row['status'] != 'open': raise HTTPException(409, 'Consolidation is not open')
    s = await shipment(sid)
    if sid == row.get('master_shipment_id'): raise HTTPException(409, 'Master cannot be its own house')
    if s.get('mode') != row['mode'] or location(s, 'origin') != row['origin_hub'] or location(s, 'destination') != row['destination_hub']:
        raise HTTPException(409, 'House must match the consolidation lane and mode')
    facts = cargo_facts(s)
    if any(x['shipment_id'] == sid for x in row['houses']): return row
    if parse_iso(row['cutoff']) <= now(): raise HTTPException(409, 'Consolidation cut-off has passed')
    if not await allowed('consolidation_assign', {**row, 'shipment_id': sid}, actor): return {'status': 'awaiting_approval'}
    claim = await db.shipments.update_one({'id': sid, '$or': [{'consolidation_id': {'$exists': False}}, {'consolidation_id': None}]},
                                         {'$set': {'consolidation_id': cid}})
    if not claim.modified_count: raise HTTPException(409, 'Shipment is already assigned to a consolidation')
    result = await db.consolidations.find_one_and_update({'id': cid, 'status': 'open', 'revision': row['revision'],
         'weight_kg': {'$lte': row['capacity_kg'] - facts['weight_kg']}, 'volume_m3': {'$lte': row['capacity_m3'] - facts['volume_m3']}},
         {'$push': {'houses': facts}, '$inc': {'weight_kg': facts['weight_kg'], 'volume_m3': facts['volume_m3'], 'revision': 1},
          '$set': {'updated_at': now_iso()}}, projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result:
        await db.shipments.update_one({'id': sid, 'consolidation_id': cid}, {'$unset': {'consolidation_id': ''}})
        raise HTTPException(409, 'Capacity exceeded or load plan changed; refresh and retry')
    await audit(actor or AGENT_ACTOR, 'execution.house_assigned', 'consolidation', cid, facts)
    return result

async def plan(origin_hub, destination_hub, mode='sea', cutoff=None):
    """Fill explicitly configured open masters; never invent a container or commercial booking."""
    masters = await db.consolidations.find({'origin_hub': origin_hub, 'destination_hub': destination_hub,
        'mode': mode, 'status': 'open'}, NO_ID).sort('created_at', 1).to_list(100)
    assigned, excluded = [], []
    candidates = await db.shipments.find({'mode': mode, 'status': {'$nin': ['cancelled', 'delivered']},
        '$or': [{'consolidation_id': {'$exists': False}}, {'consolidation_id': None}]}, NO_ID).to_list(1000)
    for s in candidates:
        if location(s, 'origin') != origin_hub or location(s, 'destination') != destination_hub: continue
        if (s.get('cargo') or {}).get('containers'): continue
        for master in masters:
            try:
                r = await assign(master['id'], s['id'])
                if r.get('status') == 'awaiting_approval': break
                assigned.append({'shipment_id': s['id'], 'consolidation_id': master['id']}); break
            except HTTPException as exc:
                excluded.append({'shipment_id': s['id'], 'reason': exc.detail})
    return {'assigned': assigned, 'excluded': excluded}

async def transition(cid, action, actor=None, evidence=None):
    row = await db.consolidations.find_one({'id': cid}, NO_ID)
    if not row: raise HTTPException(404, 'Consolidation not found')
    expected, target = {'close': ('open', 'closed'), 'arrive': ('closed', 'arrived'), 'deconsolidate': ('arrived', 'deconsolidated')}.get(action, (None, None))
    if not expected or row['status'] != expected: raise HTTPException(409, 'Invalid consolidation transition')
    if not row['houses']: raise HTTPException(409, 'An empty consolidation cannot be closed')
    if action != 'close' and not str(evidence or '').strip(): raise HTTPException(422, 'Actual terminal receipt or scan reference is required')
    for h in row['houses']:
        current = cargo_facts(await shipment(h['shipment_id']))
        if current != h: raise HTTPException(409, 'House cargo changed after allocation; rebuild the load plan')
    if not await allowed('consolidation_' + action, row, actor): return {'status': 'awaiting_approval'}
    result = await db.consolidations.find_one_and_update({'id': cid, 'status': expected, 'revision': row['revision']},
        {'$set': {'status': target, target + '_at': now_iso(), 'updated_at': now_iso(), target + '_evidence': evidence}, '$inc': {'revision': 1}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Consolidation changed')
    if action == 'deconsolidate':
        await db.shipments.update_many({'consolidation_id': cid}, {'$set': {'deconsolidated_at': now_iso(), 'consolidation_status': target}})
    await audit(actor or AGENT_ACTOR, 'execution.consolidation_' + target, 'consolidation', cid, {'evidence': evidence})
    await emit('consolidation.' + target, {'consolidation': result})
    return result

async def load_plan(cid):
    r = await db.consolidations.find_one({'id': cid}, NO_ID)
    if not r: raise HTTPException(404, 'Consolidation not found')
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(['Consolidation', 'Shipment', 'Packages', 'Weight kg', 'Volume m3'])
    for h in r['houses']: w.writerow([r['reference'], h['reference'], h['packages'], h['weight_kg'], h['volume_m3']])
    return buf.getvalue()

@job('execution.consolidation_plan', agent=True)
async def planner_job(j):
    n = 0
    async for r in db.consolidations.find({'status': 'open'}, NO_ID):
        out = await plan(r['origin_hub'], r['destination_hub'], r['mode']); n += len(out['assigned'])
        updated = await db.consolidations.find_one({'id': r['id']}, NO_ID)
        from core.settings import get_settings
        target = float((await get_settings()).get('execution_lcl_fill_target_pct') or 85) / 100
        if updated['houses'] and (max(updated['weight_kg']/updated['capacity_kg'], updated['volume_m3']/updated['capacity_m3']) >= target or parse_iso(updated['cutoff']) <= now()):
            await transition(updated['id'], 'close')
    return {'assigned': n}
schedule('execution.consolidation_plan', job='execution.consolidation_plan', every_seconds=900)()
