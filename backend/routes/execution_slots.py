from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from core.db import db
from core.deps import require_perm
from core.util import NO_ID
from routes.partner import require_partner_user
from services.execution import slots
from services.execution.runtime import read_link
router = APIRouter(prefix='/api/execution', tags=['execution'])

class SlotIn(BaseModel):
    leg_seq: int
    kind: str
    window: dict
class ReplyIn(BaseModel):
    action: str
    window: dict | None = None
    note: str = Field(default='', max_length=1000)

@router.get('/slots')
async def list_slots(user=Depends(require_perm('shipments.read_all'))):
    return {'items': await db.slot_bookings.find({}, NO_ID).sort('created_at', -1).to_list(300)}
@router.post('/shipments/{sid}/slots')
async def request(sid: str, body: SlotIn, user=Depends(require_perm('shipments.write'))):
    return await slots.request_slot(sid, body.leg_seq, body.kind, body.window, user)
@router.post('/slots/{sid}/accept')
async def accept(sid: str, user=Depends(require_perm('shipments.write'))):
    return await slots.accept_proposal(sid, user)
@router.get('/partner/slots')
async def partner_slots(user=Depends(require_partner_user)):
    return {'items': await db.slot_bookings.find({'carrier_id': user['carrier_id'], 'status': {'$nin': ['draft', 'awaiting_approval']}},
            {'_id': 0, 'company_id': 0, 'email': 0}).sort('created_at', -1).to_list(300)}
@router.post('/partner/slots/{sid}/reply')
async def partner_reply(sid: str, body: ReplyIn, user=Depends(require_partner_user)):
    return await slots.respond(sid, body.action, carrier_id=user['carrier_id'], proposed=body.window, note=body.note, actor=user)
@router.get('/slot-reply')
async def public_get(t: str):
    claims = read_link('slot', t)
    if not claims: raise HTTPException(403, 'Invalid or expired link')
    row = await db.slot_bookings.find_one({'id': claims['id'], 'revision': claims['revision']}, NO_ID)
    if not row: raise HTTPException(410, 'Appointment link replaced')
    return {k: row.get(k) for k in ('id', 'reference', 'kind', 'window', 'status', 'from', 'to', 'carrier_name')}
@router.post('/slot-reply')
async def public_reply(t: str, body: ReplyIn):
    claims = read_link('slot', t)
    if not claims: raise HTTPException(403, 'Invalid or expired link')
    result = await slots.respond(claims['id'], body.action, token=t, proposed=body.window, note=body.note)
    return {'status': result['status']}
