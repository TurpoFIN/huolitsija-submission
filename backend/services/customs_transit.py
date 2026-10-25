"""NCTS lifecycle. Physical arrival/unloading require actual notifications."""
from core import config
from core.db import db
from core.jobs import job, schedule, JobDeferred
from core.util import NO_ID,new_id,now_iso
from services import customs_flow as flow
from services.agents.base import entity_lock
from services.customs_messages import ncts,ncts_fi
from services.tulli_sandbox import transit_response

PATCHABLE={'declaration_type','office_of_departure','office_of_destination','offices_of_transit','seals',
           'transport','items','guarantee_type','guarantee_amount','guarantee_currency','ncts_data','national_data',
           'parties','representation_type','arrival_data','unloading_data','office_evidence'}
ALLOWED={
 'accepted':{'submitted'},'released':{'submitted','accepted','under_control'},
 'rejected':{'submitted','accepted','ready'},'under_control':{'submitted','accepted','released'},
 'arrived':{'released','unloading','arrived'},'unloading':{'released','arrived'},
 'closed':{'released','arrived','unloading'},'invalidated':{'submitted','accepted','under_control','released'},
 'submitted':{'submitted'},
}


def mode():return config.env('TULLI_TRANSIT_MODE',config.env('TULLI_MODE','disabled'))


def gateway_status():
    m=mode()
    shared={}
    if m in {'test','production'}:
        from services.tulli_transit_gateway import status
        shared=status()
    return {**shared,'mode':m,'phase':'P6','eu_xsd_version':'60.4.16','national_mig_version':'2.2.0',
        'ready':m=='sandbox','channel':'sandbox' if m=='sandbox' else 'message_exchange',
        'description_fi':'Suomen kansallinen FI-sanomaliikenne; ENS annetaan erikseen ICS2:een',
        'description_en':'Finnish national FI messages; ENS is filed separately in ICS2'}


async def create(shipment_id,body):
    s=await db.raw.shipments.find_one({'id':shipment_id},NO_ID)
    if not s:raise ValueError('Lähetystä ei löydy / Shipment not found')
    ident=new_id();d={'id':ident,'lrn':'T'+ident.replace('-','')[:21],
        'shipment_id':shipment_id,'company_id':s['company_id'],'status':'draft','created_at':now_iso(),
        'declaration_type':body.get('declaration_type','T1'),
        'office_of_departure':config.env('TULLI_TRANSIT_OFFICE_OF_DEPARTURE'),
        **{k:v for k,v in body.items() if k in PATCHABLE}}
    await flow.collection('transit').insert_one(d)
    return await flow.get('transit',ident)


async def ready(ident):
    async with entity_lock('transit:'+ident):
        return await _ready_locked(ident)


async def _ready_locked(ident):
    d=await flow.get('transit',ident)
    if d['status'] not in {'draft','rejected','ready'}:raise ValueError('Invalid state for ready')
    ncts.build_cc015c(d)
    if mode()!='sandbox':
        wire = ncts_fi.build_for(d,'CC015C')
        # EU office list evidence must be current; no fallback to a guessed office.
        await verify_offices(d)
        from services.customs_offices import verify_transit_xml
        await verify_transit_xml(wire)
    return await flow.transition('transit',d,'ready')


async def verify_offices(d):
    from services.customs_offices import enforce
    await enforce(d, 'transit')


async def _send(d,message,xml):
    m=mode()
    if m=='disabled':raise ValueError('Passituksen lähetys ei ole käytössä / Transit gateway disabled')
    if m not in {'sandbox','test','production'}:raise ValueError('Invalid TULLI_TRANSIT_MODE')
    if m=='sandbox':wire=xml
    else:
        from services.tulli_transit_gateway import require_ready
        require_ready()
        wire=ncts_fi.build_for(d,message)
        await verify_offices(d)
        from services.customs_offices import verify_transit_xml
        await verify_transit_xml(wire)
    record=await flow.save_message('transit',d,wire,'out','sandbox' if m=='sandbox' else 'message_exchange')
    # Persist intent BEFORE network I/O; an uncertain POST must not be retried blindly.
    await flow.collection('transit').update_one({'id':d['id']},{'$set':{'delivery_pending':True,'pending_message_id':record['id']}})
    if m=='sandbox':receipt={'channel':'sandbox','sandbox':True}
    else:
        from services.tulli_transit_gateway import submit
        receipt=await submit(wire,ncts_fi.OUTBOUND[message],d,record['id'])
    await flow.collection('transit').update_one({'id':d['id']},{'$set':{
        'delivery_pending':False,'submission':{**receipt,'sent_at':now_iso()},'last_outbound':message}})
    return record


async def file_declaration(ident):
    async with entity_lock('transit:'+ident):
        d=await flow.get('transit',ident)
        if d.get('delivery_pending'):raise ValueError('Delivery uncertain; reconcile the stored message with Tulli before retrying')
        if d['status'] not in {'ready'}:return d
        xml=ncts.build_cc015c(d)
        wire=ncts_fi.build_for(d,'CC015C') if mode() in {'test','production'} else xml
        decision=await flow.gate('transit',d,'file_transit',wire)
        if not decision.auto:return await flow.get('transit',ident)
        await _send(d,'CC015C',xml)
        return await flow.transition('transit',d,'submitted')


async def action(ident,action,data):
    async with entity_lock('transit:'+ident):
        d=await flow.get('transit',ident)
        if d.get('delivery_pending'):raise ValueError('Delivery uncertain; reconcile first')
        message={'arrival':'CC007C','unloading-remarks':'CC044C','invalidate':'CC014C'}[action]
        allowed={'arrival':{'released'},'unloading-remarks':{'unloading'},
                 'invalidate':{'submitted','accepted','under_control','released'}}[action]
        if d['status'] not in allowed:raise ValueError('Invalid state for '+action)
        payload=data.get('ncts_data') or data
        if message=='CC014C' and 'reason' in data:
            payload={'TransitOperation':{'MRN':d['mrn']},'Invalidation':{'initiatedByCustoms':'0',
                'requestDateAndTime':ncts.timestamp(),'justification':data['reason']},
                'CustomsOfficeOfDeparture':{'referenceNumber':d['office_of_departure']},
                'HolderOfTheTransitProcedure':ncts.declaration_data(d)['HolderOfTheTransitProcedure']}
        if payload.get('TransitOperation',{}).get('MRN') != d.get('mrn'):raise ValueError('MRN mismatch')
        xml=ncts.build(message,payload)
        if data.get('national_data'):d['national_data']={**d.get('national_data',{}),**data['national_data']}
        wire=ncts_fi.build_for(d,message) if mode() in {'test','production'} else xml
        decision=await flow.gate('transit',d,'transit_'+action,wire)
        if not decision.auto:return await flow.get('transit',ident)
        await _send(d,message,xml)
        if action=='arrival':return await flow.transition('transit',d,'arrived')
        return await flow.get('transit',ident)


async def record_response(ident,xml,channel='manual'):
    async with entity_lock('transit:'+ident):
        d=await flow.get('transit',ident)
        from services.customs_messages.schema_codec import parse_xml
        from lxml import etree as E
        parsed=(ncts_fi if E.QName(parse_xml(xml)).localname.startswith('FI') else ncts).parse_response(xml)
        if parsed.get('lrn') and parsed['lrn']!=d['lrn']:raise ValueError('LRN mismatch')
        if d.get('mrn') and parsed.get('mrn') and parsed['mrn']!=d['mrn']:raise ValueError('MRN mismatch')
        if not (parsed.get('lrn')==d['lrn'] or (d.get('mrn') and parsed.get('mrn')==d['mrn'])):
            raise ValueError('Uncorrelated transit response')
        record=await flow.save_message('transit',d,xml,'in',channel)
        if record.get('applied'):return d
        state=parsed['status']
        if state and state!=d['status'] and d['status'] not in ALLOWED.get(state,set()):
            raise ValueError(f'Out-of-order response: {d["status"]} -> {state}')
        extra={'last_response':parsed,**({'mrn':parsed['mrn']} if parsed.get('mrn') else {})}
        row=await flow.transition('transit',d,state,message=record['id'],extra=extra)
        await db.raw.customs_messages.update_one({'id':record['id']},{'$set':{'applied':True}})
        return row


@job('customs.transit_file',agent=True,label_fi='Passituksen lähetys',label_en='File transit')
async def transit_file(j):
    row=await file_declaration(j['payload']['declaration_id'])
    if row['status']=='ready':raise JobDeferred(300,'Transit is held at filing gate')
    return row


@job('customs.poll_transit',agent=True,label_fi='Passituksen vastaukset',label_en='Transit responses')
async def poll_transit(j):
    count=0
    async for d in flow.collection('transit').find({'submission.channel':'sandbox'},NO_ID):
        msg={'submitted':'CC028C','accepted':'CC029C'}.get(d['status'])
        if d.get('last_outbound')=='CC014C' and d['status']!='invalidated':msg='CC009C'
        elif d['status']=='arrived' and d.get('last_outbound')=='CC007C':msg='CC043C'
        elif d['status']=='unloading' and d.get('last_outbound')=='CC044C':msg='CC025C'
        elif d['status']=='arrived' and d.get('last_outbound')=='CC044C':msg='CC045C'
        if msg:
            await record_response(d['id'],transit_response(d,msg),'sandbox');count+=1
    if mode() in {'test','production'}:
        from services.tulli_transit_gateway import poll
        count+=await poll()
    return {'processed':count}

schedule('customs.poll_transit',job='customs.poll_transit',every_seconds=30)()
