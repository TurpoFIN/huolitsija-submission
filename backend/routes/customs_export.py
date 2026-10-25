"""Export routes are separate from /customs/{decl_id}; internal customs permissions."""
from fastapi import APIRouter, Depends, Request, Response
from core.deps import require_perm, company_scope
from core.db import db
from core.util import NO_ID
from services import customs_export as svc, tulli
from services.customs_messages.tulli_export import build_message

router = APIRouter(prefix='/api/customs-export',tags=['customs-export'])

async def scoped(ident,user):
    d = await svc.get(ident)
    if not await db.export_declarations.find_one({'id':ident,**company_scope(user)}):
        svc.error(404,'Ilmoitusta ei löydy.','Declaration not found.')
    return d

@router.get('')
async def listing(status: str | None=None, shipment_id: str | None=None,user=Depends(require_perm('customs.review'))):
    query=company_scope(user)
    if status: query['status']={'$in':status.split(',')}
    if shipment_id: query['shipment_id']=shipment_id
    ids=await db.export_declarations.find(query,{'id':1}).sort('updated_at',-1).to_list(300)
    return {'items':[await svc.get(d['id']) for d in ids], 'gateway':tulli.get_gateway('export').status()}

@router.get('/gateway')
async def gateway(user=Depends(require_perm('customs.review'))):
    return tulli.get_gateway('export').status()

@router.post('')
async def draft(body:dict,user=Depends(require_perm('customs.review'))):
    if not await db.shipments.find_one({'id':body.get('shipment_id'),**company_scope(user)}):
        svc.error(404,'Lähetystä ei löydy.','Shipment not found.')
    return await svc.draft_declaration_for_shipment(body['shipment_id'],user)

@router.get('/{ident}')
async def detail(ident:str,user=Depends(require_perm('customs.review'))):
    return await scoped(ident,user)

@router.patch('/{ident}')
async def patch(ident:str,body:dict,user=Depends(require_perm('customs.review'))):
    return await svc.apply_patch(await scoped(ident,user),body,user)

@router.post('/{ident}/ready')
async def ready(ident:str,user=Depends(require_perm('customs.review'))):
    return await svc.ready(await scoped(ident,user),user)

@router.post('/{ident}/file')
async def file(ident:str,user=Depends(require_perm('customs.file'))):
    return await svc.file_declaration(await scoped(ident,user),user)

@router.post('/{ident}/poll')
async def poll(ident:str,user=Depends(require_perm('customs.file'))):
    return await svc.poll_declaration(await scoped(ident,user))

@router.get('/{ident}/xml')
async def xml(ident:str,user=Depends(require_perm('customs.review'))):
    return Response(build_message(await scoped(ident,user)),media_type='application/xml')

@router.post('/{ident}/messages')
async def receive(ident:str,request:Request,user=Depends(require_perm('customs.file'))):
    d=await scoped(ident,user)
    raw=await request.body()
    if len(raw)>2000000: svc.error(413,'Sanoma on liian suuri.','Message too large.')
    try: return await svc.receive_message(d,raw)
    except ValueError as exc: svc.error(422,'Tullin sanoma ei ole kelvollinen.',str(exc))

@router.get('/{ident}/messages/{message_id}/xml')
async def message_xml(ident:str,message_id:str,user=Depends(require_perm('customs.review'))):
    await scoped(ident,user)
    msg=await db.customs_messages.find_one({'id':message_id,'declaration_id':ident},NO_ID)
    if not msg: svc.error(404,'Sanomaa ei löydy.','Message not found.')
    return Response(msg['xml'],media_type='application/xml')
