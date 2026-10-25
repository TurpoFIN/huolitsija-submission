"""Shared secured HTTP surface for the independent transit and ENS lifecycles."""
from functools import wraps
from fastapi import APIRouter,Depends,HTTPException,Request,Body
from fastapi.responses import Response
from lxml import etree
from core.jobs import JobDeferred
from core.db import db
from core.deps import require_perm,company_scope
from core.util import NO_ID
from services import customs_flow as flow


def checked(fn):
    @wraps(fn)
    async def inner(*a,**kw):
        try:return await fn(*a,**kw)
        except JobDeferred as exc:
            raise HTTPException(409, detail={'fi':'Ilmoitusta käsitellään jo. Päivitä tila ennen uutta yritystä.',
                'en':'This filing is already being processed. Refresh its status before retrying.'}) from exc
        except (ValueError,KeyError,etree.DocumentInvalid,etree.XMLSyntaxError) as exc:
            raise HTTPException(422,detail={'fi':'Ilmoituksen tiedot tai tila eivät kelpaa', 'en':str(exc)}) from exc
    return inner


def router_for(kind,svc):
    router=APIRouter(prefix='/api/customs-'+kind,tags=['customs-'+kind])
    read=require_perm('customs.review');file=require_perm('customs.file')
    async def scoped(ident,user):
        row=await flow.collection(kind).find_one({'id':ident,**company_scope(user)},NO_ID)
        if not row:raise HTTPException(404,detail={'fi':'Ilmoitusta ei löydy','en':'Declaration not found'})
        return row

    @router.get('')
    async def listing(status:str|None=None,shipment_id:str|None=None,user=Depends(read)):
        query=company_scope(user)
        if status:query['status']=status
        if shipment_id:query['shipment_id']=shipment_id
        rows=await flow.collection(kind).find(query,NO_ID).sort('created_at',-1).to_list(200)
        return [await flow.get(kind,r['id']) for r in rows]

    @router.get('/gateway')
    async def gateway(user=Depends(read)):return svc.gateway_status()

    if kind=='ens':
        @router.get('/deadline')
        @checked
        async def deadline(mode:str,arrival:str,loading:str|None=None,departure:str|None=None,
            duration_hours:float|None=None,sea_category:str|None=None,air_minimum:bool=False,user=Depends(read)):
            from services.customs_messages.ics2 import filing_deadlines
            return filing_deadlines(mode,arrival=arrival,loading=loading,departure=departure,
                duration_hours=duration_hours,sea_category=sea_category,air_minimum=air_minimum)

    @router.post('')
    @checked
    async def create(body:dict=Body(...),user=Depends(read)):
        sid=body['shipment_id']
        s=await db.raw.shipments.find_one({'id':sid,**company_scope(user)},NO_ID)
        if not s:raise HTTPException(404,detail={'fi':'Lähetystä ei löydy','en':'Shipment not found'})
        return await svc.create(sid,body)

    @router.get('/{ident}')
    async def detail(ident:str,user=Depends(read)):
        await scoped(ident,user);return await flow.get(kind,ident)

    @router.patch('/{ident}')
    @checked
    async def patch(ident:str,body:dict=Body(...),user=Depends(read)):
        await scoped(ident,user)
        row=await flow.patch(kind,ident,body,svc.PATCHABLE)
        if kind=='ens':await svc.schedule_filing(row)
        return await flow.get(kind,ident)

    @router.post('/{ident}/file')
    @checked
    async def send(ident:str,user=Depends(file)):
        await scoped(ident,user)
        return await (svc.file_declaration(ident) if kind=='transit' else svc.file_filing(ident))

    if kind=='transit':
        @router.post('/{ident}/ready')
        @checked
        async def ready(ident:str,user=Depends(read)):
            await scoped(ident,user);return await svc.ready(ident)
    else:
        @router.get('/{ident}/sti-export')
        @checked
        async def sti(ident:str,user=Depends(file)):
            await scoped(ident,user)
            return Response(await svc.sti_export(ident),media_type='application/xml',
                            headers={'Content-Disposition':f'attachment; filename="ens-{ident}.xml"'})

    # Register fixed actions explicitly so /messages is never swallowed.
    def make_action(action):
        @checked
        async def handler(ident:str,body:dict=Body(default={}),user=Depends(file)):
            await scoped(ident,user);return await svc.action(ident,action,body)
        return handler
    for action in (['arrival','unloading-remarks','invalidate'] if kind=='transit' else ['invalidate','rfi-response']):
        router.add_api_route('/{ident}/'+action,make_action(action),methods=['POST'])

    @router.post('/{ident}/messages')
    @checked
    async def receive(ident:str,request:Request,user=Depends(file)):
        await scoped(ident,user)
        return await svc.record_response(ident,(await request.body()).decode('utf-8'))

    @router.get('/{ident}/messages/{message_id}/xml')
    async def xml(ident:str,message_id:str,user=Depends(read)):
        await scoped(ident,user)
        row=await db.raw.customs_messages.find_one({'kind':kind,'declaration_id':ident,'id':message_id},NO_ID)
        if not row:raise HTTPException(404,detail={'fi':'Sanomaa ei löydy','en':'Message not found'})
        return Response(row['xml'],media_type='application/xml')
    return router
