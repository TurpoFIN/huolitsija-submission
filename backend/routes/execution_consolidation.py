from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from core.db import db
from core.deps import require_perm
from core.util import NO_ID
from services.execution import consolidation as c, crossdock as x
router = APIRouter(prefix='/api/execution', tags=['execution'])
read = require_perm('shipments.read_all'); write = require_perm('shipments.write')
class MasterIn(BaseModel):
    origin_hub: str
    destination_hub: str
    mode: str = 'sea'
    capacity_kg: float = Field(gt=0, allow_inf_nan=False)
    capacity_m3: float = Field(gt=0, allow_inf_nan=False)
    cutoff: str
    master_shipment_id: str | None = None
class HouseIn(BaseModel): shipment_id: str
class TransitionIn(BaseModel): evidence: str | None = None
class CrossIn(BaseModel):
    shipment_id: str
    hub: str
    packages: int = Field(gt=0)
class StepIn(BaseModel):
    action: str
    packages: int = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=1000)
@router.get('/consolidations')
async def masters(user=Depends(read)): return {'items': await db.consolidations.find({}, NO_ID).sort('created_at', -1).to_list(300)}
@router.post('/consolidations')
async def create(body: MasterIn, user=Depends(write)): return await c.create(**body.model_dump(), actor=user)
@router.post('/consolidations/{cid}/houses')
async def assign(cid: str, body: HouseIn, user=Depends(write)): return await c.assign(cid, body.shipment_id, user)
@router.post('/consolidations/{cid}/actions/{action}')
async def change(cid: str, action: str, body: TransitionIn, user=Depends(write)): return await c.transition(cid, action, user, body.evidence)
@router.get('/consolidations/{cid}/load-plan.csv')
async def export(cid: str, user=Depends(read)): return Response(await c.load_plan(cid), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="load-plan.csv"'})
@router.get('/crossdock')
async def cross_list(user=Depends(read)): return {'items': await db.crossdock_tasks.find({}, NO_ID).sort('created_at', -1).to_list(300)}
@router.post('/crossdock')
async def cross_create(body: CrossIn, user=Depends(write)): return await x.create(body.shipment_id, body.hub, body.packages, user)
@router.post('/crossdock/{tid}/steps')
async def cross_step(tid: str, body: StepIn, user=Depends(write)): return await x.step(tid, body.action, body.packages, body.evidence, user)

class RecountIn(BaseModel):
    packages: int = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=1000)
@router.post('/crossdock/{tid}/reconcile')
async def cross_reconcile(tid: str, body: RecountIn, user=Depends(write)):
    return await x.reconcile(tid, body.packages, body.evidence, user)
