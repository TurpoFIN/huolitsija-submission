"""Shared execution guards. Unknown facts never become a successful operational event."""
import math
from fastapi import HTTPException
from core.db import db
from core.util import NO_ID, iso, parse_iso
from services.execution.runtime import decide

async def shipment(sid):
    s = await db.shipments.find_one({'id': sid}, NO_ID)
    if not s:
        raise HTTPException(404, 'Shipment not found')
    if s.get('status') == 'cancelled' or s.get('compliance_hold'):
        raise HTTPException(409, 'Shipment is cancelled or on compliance hold')
    return s

def positive(value, name):
    try:
        v = float(value)
    except (ValueError, TypeError):
        raise HTTPException(422, f'{name} is required')
    if not math.isfinite(v) or v <= 0:
        raise HTTPException(422, f'{name} must be a finite positive number')
    return v

def window(value):
    a, b = parse_iso((value or {}).get('start')), parse_iso((value or {}).get('end'))
    if not a or not b or a >= b:
        raise HTTPException(422, 'A slot needs a valid start before its end')
    return {'start': iso(a), 'end': iso(b)}

async def allowed(action, row, actor=None):
    if actor and actor.get('org_type') == 'internal' and actor.get('type') != 'agent':
        return True
    d = await decide(action, subject=('execution', row['id']), subject_label=row.get('reference', row['id']),
                     inputs={'shipment_id': row.get('shipment_id')}, decision={'action': action},
                     title=action, title_fi='Toteutus: ' + action, company_id=row.get('company_id'),
                     link='/ops/execution', approval_data={'action': action, 'execution_id': row['id']})
    return d.auto
