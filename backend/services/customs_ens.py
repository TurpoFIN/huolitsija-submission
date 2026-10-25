"""ICS2 ENS lifecycle, deadline scheduling and separate AP/business acknowledgements."""
from datetime import timedelta
from pymongo.errors import DuplicateKeyError
from core import config
from core.db import db
from core.events import on
from core.jobs import job,schedule,enqueue,JobDeferred
from core.util import NO_ID,new_id,now_iso,now,parse_iso
from services import customs_flow as flow,ics2_gateway,ics2_sandbox
from services.agents.base import entity_lock
from services.customs_messages import ics2

PATCHABLE={'mode','filing_type','ens_data','deadline_inputs','parties','representation_type'}
EU=set('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
ALLOWED={'registered':{'submitted'},'assessment_complete':{'registered','referral'},
         'referral':{'submitted','registered','assessment_complete','referral'},
         'do_not_load':{'submitted','registered','assessment_complete','referral'},
         'arrived':{'registered','assessment_complete','referral'},
         'rejected':{'submitted','registered'},'invalidated':{'submitted','registered','assessment_complete','referral','do_not_load'}}


def gateway_status():return ics2_gateway.status()


async def create(shipment_id,body):
    s=await db.raw.shipments.find_one({'id':shipment_id},NO_ID)
    if not s:raise ValueError('Lähetystä ei löydy / Shipment not found')
    mode=body.get('mode') or s.get('mode')
    ft=body.get('filing_type') or ics2.DEFAULT.get(mode)
    if mode not in ics2.FILINGS or ft not in ics2.FILINGS[mode]:raise ValueError('Unsupported ENS mode/type')
    existing=await flow.collection('ens').find_one({'shipment_id':shipment_id,'filing_type':ft},NO_ID)
    if existing:
        changes={k:v for k,v in body.items() if k in PATCHABLE}
        if existing['status']=='draft' and changes:
            row=await flow.patch('ens',existing['id'],changes,PATCHABLE)
            await schedule_filing(row)
        return await flow.get('ens',existing['id'])
    ident=new_id();d={'id':ident,'lrn':'E'+ident.replace('-','')[:21],'shipment_id':shipment_id,
        'company_id':s['company_id'],'status':'draft','created_at':now_iso(),'mode':mode,'filing_type':ft,
        'eori':config.env('ICS2_EORI'),**{k:v for k,v in body.items() if k in PATCHABLE}}
    try:
        await flow.collection('ens').insert_one(d)
    except DuplicateKeyError:
        existing=await flow.collection('ens').find_one({'shipment_id':shipment_id,'filing_type':ft},NO_ID)
        return await flow.get('ens',existing['id'])
    await schedule_filing(d)
    return await flow.get('ens',ident)


async def schedule_filing(d):
    if not d.get('deadline_inputs'):
        await flow.collection('ens').update_one({'id':d['id']},{'$set':{'deadline_missing':True}})
        return
    deadlines=ics2.deadlines_for(d)
    due=parse_iso(deadlines['filing'])
    await flow.collection('ens').update_one({'id':d['id']},{'$set':{'deadlines':deadlines,'deadline_missing':False}})
    await enqueue('customs.ens_file',{'filing_id':d['id']},key='ens:'+d['id']+':'+deadlines['filing'],
                  run_at=min(now(),due-timedelta(minutes=30)),company_id=d.get('company_id'))


async def file_filing(ident):
    async with entity_lock('ens:'+ident):
        d=await flow.get('ens',ident)
        if d.get('delivery_pending'):raise ValueError('Delivery uncertain; reconcile stored message with the access point')
        if d['status'] not in {'draft'}:return d
        if not d.get('deadline_inputs'):raise ValueError('ENS deadline inputs missing')
        deadlines=ics2.deadlines_for(d)
        xml=ics2.build_ens(d)
        if ics2_gateway.mode() != 'sandbox':
            from services.customs_offices import enforce
            await enforce(d, 'ens')
        decision=await flow.gate('ens',d,'file_ens',xml)
        if not decision.auto:return await flow.get('ens',ident)
        m=ics2_gateway.mode()
        if m not in {'sandbox','conformance','production'}:
            raise ValueError('ENS gateway disabled or STI manual upload required')
        record=await flow.save_message('ens',d,xml,'out','sandbox' if m=='sandbox' else 'domibus')
        await flow.collection('ens').update_one({'id':ident},{'$set':{'delivery_pending':True,'pending_message_id':record['id']}})
        receipt={'channel':'sandbox','sandbox':True} if m=='sandbox' else await ics2_gateway.submit(xml,record['id']+'@huolitsija')
        await flow.collection('ens').update_one({'id':ident},{'$set':{'delivery_pending':False,
            'submission':{**receipt,'sent_at':now_iso()},'late_filing':now()>parse_iso(deadlines['filing'])}})
        return await flow.transition('ens',d,'submitted')


async def sti_export(ident):
    d=await flow.get('ens',ident)
    if d['status']!='draft':raise ValueError('Only a draft can be exported for STI submission')
    xml=ics2.build_ens(d)
    from services.customs_offices import enforce
    await enforce(d, 'ens')
    decision=await flow.gate('ens',d,'export_sti_file',xml)
    if not decision.auto:raise ValueError('STI export held by decision gate')
    xml=ics2.build_ens(d)
    await flow.save_message('ens',d,xml,'out','sti_export')
    # Download is not proof of submission; the status remains draft until a correlated response arrives.
    await flow.collection('ens').update_one({'id':ident},{'$set':{'sti_exported_at':now_iso()}})
    return xml


async def action(ident,action,data):
    async with entity_lock('ens:'+ident):
        d=await flow.get('ens',ident)
        if not d.get('mrn'):raise ValueError('Registered MRN required')
        if d.get('delivery_pending'):raise ValueError('Delivery uncertain')
        if action=='rfi-response' and d['status']!='referral':raise ValueError('No open referral')
        if d['status'] in {'arrived','invalidated','rejected'}:raise ValueError('Filing is final')
        message='IE3Q04' if action=='invalidate' else 'IE3R02'
        payload={**data,'MRN':d['mrn'],'documentIssueDate':{'DateTime':ics2.timestamp()},
                 'declarant':{'identificationNumber':config.env('ICS2_EORI')}}
        xml=ics2.build(message,payload)
        decision=await flow.gate('ens',d,'ens_'+action,xml)
        if not decision.auto:return await flow.get('ens',ident)
        m=ics2_gateway.mode()
        if m not in {'sandbox','conformance','production'}:raise ValueError('ENS gateway unavailable')
        record=await flow.save_message('ens',d,xml,'out','sandbox' if m=='sandbox' else 'domibus')
        await flow.collection('ens').update_one({'id':ident},{'$set':{'delivery_pending':True}})
        if m!='sandbox':await ics2_gateway.submit(xml,record['id']+'@huolitsija')
        await flow.collection('ens').update_one({'id':ident},{'$set':{'delivery_pending':False,'last_outbound':message}})
        return await flow.get('ens',ident)


async def record_response(ident,xml,channel='manual'):
    async with entity_lock('ens:'+ident):
        d=await flow.get('ens',ident);parsed=ics2.parse_response(xml)
        if parsed.get('lrn') and parsed['type']!='IE3N06' and parsed['lrn']!=d['lrn']:raise ValueError('LRN mismatch')
        if d.get('mrn') and parsed.get('mrn') and parsed['mrn']!=d['mrn']:raise ValueError('MRN mismatch')
        if parsed['type']=='IE3N06' and d.get('mrn') not in parsed['related_mrns']:
            raise ValueError('Arrival does not reference this ENS MRN')
        correlated=(parsed.get('lrn')==d['lrn'] or
            (d.get('mrn') and (parsed.get('mrn')==d['mrn'] or d['mrn'] in parsed.get('related_mrns',[]))) or
            (bool(parsed.get('functional_reference')) and parsed['functional_reference'] in {d['lrn'],(d.get('submission') or {}).get('transport_id')}))
        if not correlated or (parsed.get('functional_reference') is None and not parsed.get('lrn') and not parsed.get('mrn') and not parsed.get('related_mrns')):
            raise ValueError('Uncorrelated ICS2 response')
        record=await flow.save_message('ens',d,xml,'in',channel)
        if record.get('applied'):return d
        state=parsed['status']
        permitted=d['status'] in ALLOWED.get(state,set()) or (state=='registered' and d.get('sti_exported_at') and d['status']=='draft')
        if state!=d['status'] and not permitted:raise ValueError(f'Out-of-order response: {d["status"]} -> {state}')
        extra={'last_response':parsed,**({'mrn':parsed['mrn']} if parsed.get('mrn') else {})}
        row=await flow.transition('ens',d,state,message=record['id'],extra=extra)
        await db.raw.customs_messages.update_one({'id':record['id']},{'$set':{'applied':True}})
        return row


@job('customs.ens_file',agent=True,label_fi='ENS-ilmoituksen lähetys',label_en='File ENS')
async def ens_file(j):
    row=await file_filing(j['payload']['filing_id'])
    if row['status']=='draft':raise JobDeferred(300,'ENS is held at filing gate')
    return row


@job('customs.poll_ics2',agent=True,label_fi='ICS2-vastaukset',label_en='ICS2 responses')
async def poll_ics2(j):
    count=0
    if ics2_gateway.mode()=='sandbox':
        async for d in flow.collection('ens').find({'submission.channel':'sandbox'},NO_ID):
            msg={'submitted':'IE3R01','registered':'IE3N03'}.get(d['status'])
            if d.get('last_outbound')=='IE3Q04' and d['status']!='invalidated':msg='IE3R07'
            elif d.get('last_outbound')=='IE3R02' and d['status']=='referral':msg='IE3N03'
            if msg:await record_response(d['id'],ics2_sandbox.response(d,msg),'sandbox');count+=1
    elif ics2_gateway.mode() in {'conformance','production'}:
        await ics2_gateway.receive()
        async for incoming in db.raw.ics2_inbox.find({'processed':False}).limit(50):
            try:
                parsed=ics2.parse_response(incoming['xml'])
                query={'mrn':parsed['mrn']} if parsed.get('mrn') else {'lrn':parsed.get('lrn')}
                d=await flow.collection('ens').find_one(query,NO_ID)
                if not d and parsed.get('lrn'):d=await flow.collection('ens').find_one({'lrn':parsed['lrn']},NO_ID)
                targets=[d] if d else []
                if parsed.get('related_mrns'):
                    targets=await flow.collection('ens').find({'mrn':{'$in':parsed['related_mrns']}},NO_ID).to_list(1000)
                if not targets and parsed.get('functional_reference'):
                    ref=parsed['functional_reference']
                    found=await flow.collection('ens').find_one({'$or':[{'lrn':ref},{'submission.transport_id':ref}]},NO_ID)
                    if found:targets=[found]
                if not targets:raise ValueError('No matching ENS filing')
                for target in targets:await record_response(target['id'],incoming['xml'],'domibus')
                await db.raw.ics2_inbox.update_one({'_id':incoming['_id']},{'$set':{'processed':True}});count+=1
            except ValueError as exc:
                await db.raw.ics2_inbox.update_one({'_id':incoming['_id']},{'$set':{'error':str(exc)}})
    return {'processed':count}


@on('shipment.created')
@on('carrier.booked')
async def auto_create(payload):
    s=payload.get('shipment') or {}
    origin=(s.get('origin') or {}).get('country');dest=(s.get('destination') or {}).get('country')
    if dest not in EU or origin in EU or not origin:return
    body={'mode':s.get('mode'),**s.get('ens',{})}
    if body.get('mode') not in ics2.FILINGS:return
    await create(s['id'],body)

schedule('customs.poll_ics2',job='customs.poll_ics2',every_seconds=30)()
