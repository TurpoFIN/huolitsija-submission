"""Ordered terminal scans; missing scans and discrepancies remain visible."""
from fastapi import HTTPException
from pymongo import ReturnDocument
from core.audit import audit
from core.db import db, register_index
from core.events import emit
from core.util import NO_ID, new_id, now_iso
from services.execution.common import shipment, positive
STEPS = ['arrive', 'unload', 'sort', 'reload', 'depart']
register_index('crossdock_tasks', 'id', unique=True)

async def create(sid, hub, packages, actor):
    s = await shipment(sid)
    if not hub: raise HTTPException(422, 'Hub is required')
    row = {'id': new_id(), 'shipment_id': sid, 'reference': s['reference'], 'hub': hub,
           'expected_packages': positive(packages, 'package count'), 'status': 'planned', 'steps': [], 'created_at': now_iso(), 'revision': 0}
    await db.crossdock_tasks.insert_one(dict(row))
    await audit(actor, 'execution.crossdock_created', 'shipment', sid, {'task_id': row['id'], 'hub': hub})
    return row

async def step(tid, action, packages, evidence, actor):
    row = await db.crossdock_tasks.find_one({'id': tid}, NO_ID)
    if not row: raise HTTPException(404, 'Cross-dock task not found')
    await shipment(row['shipment_id'])
    if row['status'] == 'discrepancy': raise HTTPException(409, 'Reconcile the package discrepancy before continuing')
    i = len(row['steps'])
    if i >= len(STEPS) or action != STEPS[i]: raise HTTPException(409, 'Cross-dock scans must follow arrive, unload, sort, reload, depart')
    if not str(evidence or '').strip(): raise HTTPException(422, 'An actual scan or terminal reference is required')
    qty = positive(packages, 'observed package count')
    status = 'discrepancy' if qty != row['expected_packages'] else ('complete' if action == 'depart' else action)
    event = {'action': action, 'packages': qty, 'evidence': evidence, 'at': now_iso()}
    result = await db.crossdock_tasks.find_one_and_update({'id': tid, 'revision': row['revision']},
        {'$set': {'status': status, 'updated_at': now_iso()}, '$push': {'steps': event}, '$inc': {'revision': 1}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Task changed')
    await audit(actor, 'execution.crossdock_' + action, 'shipment', row['shipment_id'], event)
    await emit('crossdock.' + status, {'task': result})
    return result

async def reconcile(tid, packages, evidence, actor):
    row = await db.crossdock_tasks.find_one({'id': tid}, NO_ID)
    if not row or row['status'] != 'discrepancy': raise HTTPException(409, 'No cross-dock discrepancy to reconcile')
    await shipment(row['shipment_id'])
    qty = positive(packages, 'recounted package count')
    if qty != row['expected_packages']: raise HTTPException(409, 'Recount still differs from the expected package count')
    if not str(evidence or '').strip(): raise HTTPException(422, 'Recount receipt or scan evidence is required')
    last = row['steps'][-1]['action']
    status = 'complete' if last == 'depart' else last
    correction = {'at': now_iso(), 'packages': qty, 'evidence': evidence, 'after_step': last}
    result = await db.crossdock_tasks.find_one_and_update({'id': tid, 'revision': row['revision'], 'status': 'discrepancy'},
        {'$set': {'status': status, 'updated_at': now_iso()}, '$inc': {'revision': 1}, '$push': {'reconciliations': correction}},
        projection=NO_ID, return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409, 'Task changed; refresh')
    await audit(actor, 'execution.crossdock_reconciled', 'shipment', row['shipment_id'], correction)
    await emit('crossdock.reconciled', {'task': result})
    return result
