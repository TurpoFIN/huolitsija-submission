"""Shared durable lifecycle machinery for transit and ENS, separate from import."""
import hashlib
import re
from core import agent_runtime
from core.db import db, register_index
from core.events import emit
from core.settings import get_settings
from core.util import NO_ID, new_id, now_iso
from services import customs_screening

COLLECTIONS={'transit':'transit_declarations','ens':'ens_filings'}
LABELS={'draft':('Luonnos','Draft'),'ready':('Valmis','Ready'),'submitted':('Lähetetty','Submitted'),
 'accepted':('Hyväksytty','Accepted'),'registered':('Rekisteröity','Registered'),
 'released':('Luovutettu passitukseen','Released for transit'),'arrived':('Saapunut','Arrived'),
 'unloading':('Purkulupa','Unloading permitted'),'closed':('Päätetty','Closed'),
 'rejected':('Hylätty','Rejected'),'under_control':('Tarkastuksessa','Under control'),
 'invalidated':('Mitätöity','Invalidated'),'assessment_complete':('Riskianalyysi valmis','Assessment complete'),
 'referral':('Lisäselvityspyyntö','Referral'),'do_not_load':('Lastauskielto','Do not load')}
EVENTS={('transit','released'):'customs.transit.released',('transit','closed'):'customs.transit.closed',
 ('ens','registered'):'customs.ens.accepted',('ens','do_not_load'):'customs.ens.do_not_load',
 ('ens','referral'):'customs.ens.referral'}
for coll in COLLECTIONS.values():
    register_index(coll,'id',unique=True)
    register_index(coll,'lrn',unique=True)
register_index('ens_filings',[('shipment_id',1),('filing_type',1)],unique=True)
register_index('customs_messages',[('kind',1),('declaration_id',1),('digest',1),('direction',1)],unique=True)


def collection(kind):return db.raw[COLLECTIONS[kind]]


async def get(kind,ident):
    row=await collection(kind).find_one({'id':ident},NO_ID)
    if not row:raise ValueError('Ilmoitusta ei löydy / Declaration not found')
    fi,en=LABELS[row['status']];row.update(status_label_fi=fi,status_label_en=en)
    row['messages']=await db.raw.customs_messages.find({'kind':kind,'declaration_id':ident},
        {'_id':0,'xml':0}).sort('at',1).to_list(200)
    return row


async def save_message(kind,decl,xml,direction,channel):
    from services.customs_messages.schema_codec import parse_xml
    from lxml import etree as E
    digest=hashlib.sha256(xml.encode()).hexdigest()
    query={'kind':kind,'declaration_id':decl['id'],'digest':digest,'direction':direction}
    row={**query,'id':new_id(),'xml':xml,'at':now_iso(),'channel':channel,
         'type':E.QName(parse_xml(xml)).localname,'sandbox':channel=='sandbox',
         'xsd_valid':True,'company_id':decl.get('company_id')}
    await db.raw.customs_messages.update_one(query,{'$setOnInsert':row},upsert=True)
    return await db.raw.customs_messages.find_one(query,NO_ID)


async def transition(kind,decl,state,*,message=None,extra=None):
    if not state:return await get(kind,decl['id'])
    updates={'status':state,'updated_at':now_iso(),**(extra or {})}
    changed=await collection(kind).update_one({'id':decl['id'],'status':decl['status']},
        {'$set':updates,'$push':{'history':{'from':decl['status'],'to':state,'at':now_iso(),'message_id':message}}})
    if not changed.matched_count:raise ValueError('Concurrent declaration change')
    row=await get(kind,decl['id'])
    event=EVENTS.get((kind,state))
    if event and state!=decl['status']:await emit(event,{'declaration':row,'filing':row,'shipment_id':row.get('shipment_id')})
    return row


def screening_payload(decl):
    # XML uses CamelCase roles; controls lane consumes lower snake_case roles.
    roles={'HolderOfTheTransitProcedure':'holder','Principal':'principal',
           'TraderAtDestination':'consignee','DestinationTrader':'consignee',
           'additionalSupplyChainActor':'additional_supply_chain_actors'}
    def walk(v):
        if isinstance(v,list):return [walk(x) for x in v]
        if not isinstance(v,dict):return v
        out={}
        for k,x in v.items():
            key=roles.get(k,re.sub(r'(?<!^)(?=[A-Z])','_',k).lower())
            if key=='identification_number':key='eori'
            out[key]=walk(x)
        return out
    result = walk(decl)
    # Some official ENS carrier types carry only an EORI. Use an explicitly
    # supplied party name with the same identity; never infer one from the role.
    identities = {}
    def names(v):
        if isinstance(v, list):
            for child in v: names(child)
        elif isinstance(v, dict):
            if v.get('eori') and v.get('name'):
                identities.setdefault(v['eori'], set()).add(v['name'])
            for child in v.values(): names(child)
    names(result.get('parties', {}))
    def enrich(v):
        if isinstance(v, list):
            for child in v: enrich(child)
        elif isinstance(v, dict):
            candidates = identities.get(v.get('eori'), set())
            if not v.get('name') and len(candidates) == 1:
                v['name'] = next(iter(candidates))
            for child in v.values(): enrich(child)
    enrich(result)
    return result


async def gate(kind,decl,action,xml=None):
    payload=screening_payload(decl)
    if xml:
        from services.customs_messages.schema_codec import as_dict,parse_xml
        payload['outbound']=screening_payload(as_dict(parse_xml(xml)))
        # House filings can legally identify a carrier by EORI alone. Resolve its
        # name from the declaration's explicit party directory; never fabricate it.
        directory={}
        def collect(v):
            if isinstance(v,dict):
                if v.get('eori') and v.get('name'):directory.setdefault(v['eori'],set()).add(v['name'])
                for x in v.values():collect(x)
            elif isinstance(v,list):
                for x in v:collect(x)
        collect(payload)
        def enrich(v):
            if isinstance(v,dict):
                if len(directory.get(v.get('eori'),set()))==1 and not v.get('name'):v['name']=next(iter(directory[v['eori']]))
                for x in v.values():enrich(x)
            elif isinstance(v,list):
                for x in v:enrich(x)
        enrich(payload)
    try:
        screen=await customs_screening.screen_parties(payload,kind=kind)
    except Exception as exc:
        screen={'status':'error','complete':False,'error':str(exc)[:500]}

    settings=await get_settings()
    reasons=[]
    if screen.get('status')!='clear' or not screen.get('complete'):
        reasons.append(agent_runtime.policy_reason('screening','Osapuolten tarkistus ei ole hyväksytty','Party screening has not cleared'))
    shipment=await db.raw.shipments.find_one({'id':decl.get('shipment_id')},NO_ID) or {}
    if shipment.get('compliance_hold'):
        reasons.append(agent_runtime.policy_reason('compliance_hold','Lähetys on tarkastuksessa','Shipment is on compliance hold'))
    legal=None
    if not settings.get('customs_filing_delegated'):legal='customs_filing_not_delegated'
    elif decl.get('representation_type')=='indirect' and settings.get('customs_delegation_scope')!='direct_and_indirect':
        legal='customs_indirect_representation'
    d=await agent_runtime.decide('customs_agent',action,subject=(COLLECTIONS[kind],decl['id']),
        company_id=decl.get('company_id'),subject_label=decl['lrn'],confidence=None,
        inputs={'lrn':decl['lrn'],'kind':kind,'screening':screen},decision={'file':True},
        legal_point=legal,extra_reasons=reasons,approval_kind='customs_filing',
        title=f'File {kind} {decl["lrn"]}',title_fi=f'Tulli-ilmoituksen jättäminen {decl["lrn"]}',
        link=f'/ops/customs/{kind}/{decl["id"]}')
    await collection(kind).update_one({'id':decl['id']},{'$set':{'screening':screen,'decision_id':d.id}})
    return d


async def patch(kind,ident,body,allowed):
    from services.agents.base import entity_lock
    async with entity_lock(kind+':'+ident):
        return await _patch_locked(kind,ident,body,allowed)


async def _patch_locked(kind,ident,body,allowed):
    d=await get(kind,ident)
    if d['status'] not in {'draft','ready','rejected'}:raise ValueError('Ilmoitus on jo lähetetty / Declaration already sent')
    bad=set(body)-set(allowed)
    if bad:raise ValueError('Fields cannot be changed: '+', '.join(sorted(bad)))
    await collection(kind).update_one({'id':ident},{'$set':{**body,'status':'draft','updated_at':now_iso()}})
    return await get(kind,ident)
