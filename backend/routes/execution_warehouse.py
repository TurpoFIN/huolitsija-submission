from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr
from core.db import db
from core.deps import require_perm
from core.util import NO_ID, new_id, now_iso
from services.execution import warehouse
from services.execution.runtime import read_link
router = APIRouter(prefix='/api/execution', tags=['execution'])
read = require_perm('shipments.read_all'); write = require_perm('shipments.write')
class WarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    email: EmailStr | None = None
    provider: str = 'email'
    odoo_inbound_type_id: int | None = Field(default=None, gt=0)
    odoo_outbound_type_id: int | None = Field(default=None, gt=0)
    odoo_product_id: int | None = Field(default=None, gt=0)
    odoo_uom_id: int | None = Field(default=None, gt=0)
    test_company_id: str | None = None  # s2: a test warehouse bound to one test tenant (core/test_counterparty.py)
class OrderIn(BaseModel):
    shipment_id: str
    kind: str
    warehouse_id: str
class ReceiptIn(BaseModel):
    packages: int = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=1000)
@router.get('/warehouses')
async def warehouses(user=Depends(read)): return {'items': await db.warehouses.find({}, NO_ID).to_list(300)}
@router.post('/warehouses')
async def create(body: WarehouseIn, user=Depends(require_perm('settings.write'))):
    if body.provider not in ('none', 'email', 'odoo'): raise HTTPException(422, 'Unknown WMS provider')
    data = body.model_dump()
    if data.pop('test_company_id'):
        if body.provider == 'odoo': raise HTTPException(422, 'A test warehouse uses e-mail, not the WMS')
        from core.test_counterparty import bind
        data.update(await bind(body.test_company_id))
    row = {'id': new_id(), **data, 'created_at': now_iso()}
    await db.warehouses.insert_one(dict(row)); return row
@router.get('/warehouse-orders')
async def orders(user=Depends(read)): return {'items': await db.warehouse_orders.find({}, NO_ID).sort('created_at', -1).to_list(300)}
@router.post('/warehouse-orders')
async def order(body: OrderIn, user=Depends(write)): return await warehouse.create_order(**body.model_dump(), actor=user)
@router.post('/warehouse-orders/{oid}/send')
async def send(oid: str, user=Depends(write)): return await warehouse.dispatch(oid, user)
@router.post('/warehouse-orders/{oid}/milestone')
async def milestone(oid: str, body: ReceiptIn, user=Depends(write)): return await warehouse.milestone(oid, body.packages, body.evidence, user)
@router.get('/warehouse-reply')
async def reply_get(t: str):
    c = read_link('warehouse', t)
    if not c: raise HTTPException(403, 'Invalid or expired warehouse link')
    row = await db.warehouse_orders.find_one({'id': c['id']}, NO_ID)
    if not row: raise HTTPException(404, 'Warehouse order not found')
    return {k: row.get(k) for k in ('id', 'reference', 'kind', 'packages', 'warehouse_name', 'status')}
@router.post('/warehouse-reply')
async def reply_post(t: str, body: ReceiptIn):
    c = read_link('warehouse', t)
    if not c: raise HTTPException(403, 'Invalid or expired warehouse link')
    row = await warehouse.milestone(c['id'], body.packages, body.evidence, token=t)
    return {'status': row['status']}
