"""Export lifecycle with durable agent decisions and official FI515A business XML."""
from copy import deepcopy
import hashlib
import json
from fastapi import HTTPException
from core import agent_runtime
from core.db import db, register_index
from core.events import emit, on
from core.jobs import job, enqueue, schedule
from core.settings import get_settings
from core.util import NO_ID, new_id, now_iso
from services import tulli
from services.agents.base import entity_lock
from services.customs_messages.tulli_export import build_message, validate as validate_xml

register_index('export_declarations', 'id', unique=True)
register_index('export_declarations', 'shipment_id', unique=True)
STATES = {'draft':('Luonnos','Draft'), 'ready':('Valmis tarkistettavaksi','Ready'),
          'approved':('Hyväksytty jätettäväksi','Approved'), 'submitted':('Lähetetty','Submitted'),
          'accepted':('Tullin hyväksymä','Accepted'), 'released':('Luovutettu vientiin','Released'),
          'exited':('Poistuminen vahvistettu','Exited'), 'rejected':('Hylätty','Rejected'),
          'under_control':('Tarkastuksessa','Under control'), 'invalidated':('Mitätöity','Invalidated')}
ALLOWED = {'draft':{'ready'}, 'ready':{'draft','approved'}, 'approved':{'submitted','draft'},
           'submitted':{'accepted','rejected','under_control'}, 'accepted':{'released','under_control','invalidated','rejected'},
           'under_control':{'released','rejected','invalidated'}, 'released':{'exited','invalidated'},
           'rejected':{'draft'}, 'exited':set(), 'invalidated':set()}
EDITABLE = {'draft','ready','rejected'}
FIELDS = {'items','exporter','declarant','representative','representation_type','consignee','buyer',
          'submitter','transport','invoice','delivery_terms','goods_location','office','exit_office',
          'procedure_code','additional_procedure','declaration_type','additional_declaration_type','security',
          'transaction_nature','documents','language'}


def fingerprint(d):
    values = deepcopy({k:d.get(k) for k in sorted(FIELDS)})
    for item in values.get('items') or []:
        for field in ('licence_requirements','licence_check'):
            item.pop(field,None)
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()


def error(status, fi, en):
    raise HTTPException(status, {'fi':fi,'en':en})


async def get(decl_id):
    d = await db.export_declarations.find_one({'id':decl_id}, NO_ID)
    if not d: error(404,'Vienti-ilmoitusta ei löydy.','Export declaration not found.')
    d['status_label_fi'], d['status_label_en'] = STATES[d['status']]
    d['messages'] = await db.customs_messages.find({'declaration_id':decl_id}, {**NO_ID,'xml':0}).sort('at',1).to_list(200)
    return d


async def transition(d, status, actor=None, **fields):
    if status not in ALLOWED[d['status']]:
        error(409,'Ilmoituksen tilasiirtymä ei ole sallittu.',f'Invalid export transition {d["status"]} → {status}.')
    at = now_iso()
    fields.update(status=status, updated_at=at)
    if status in ('released','exited'): fields[status+'_at']=at
    result = await db.export_declarations.update_one({'id':d['id'],'status':d['status']},
        {'$set':fields, '$push':{'history':{'from':d['status'],'to':status,'at':at,'actor':(actor or {}).get('id')}}})
    if not result.modified_count: error(409,'Ilmoitus muuttui. Lataa uudelleen.','Declaration changed; reload.')
    current = await get(d['id'])
    if status in ('released','exited'):
        await emit('customs.export.'+status, {'declaration':current,'actor':actor})
    return current


async def draft_declaration_for_shipment(shipment_id, actor=None):
    existing = await db.export_declarations.find_one({'shipment_id':shipment_id}, NO_ID)
    if existing: return await get(existing['id'])
    s = await db.shipments.find_one({'id':shipment_id}, NO_ID)
    if not s: error(404,'Lähetystä ei löydy.','Shipment not found.')
    if s.get('direction') != 'export': error(400,'Lähetyksen on oltava vienti.','Shipment must be outbound/export.')
    c = await db.companies.find_one({'id':s.get('company_id')}, NO_ID) or {}
    ident = new_id()
    exporter = deepcopy(s.get('exporter') or {'name':c.get('name'),'eori':c.get('eori'), 'address':c.get('address')})
    d = {'id':ident,'shipment_id':shipment_id,'shipment_ref':s.get('reference'),'company_id':s.get('company_id'),
         'type':'export','flow':'export','status':'draft','lrn':'EX'+ident.replace('-','')[:12],
         'message_id':'EX'+ident.replace('-','')[:12], 'created_at':now_iso(),'updated_at':now_iso(),
         'draft_source': 'agent' if (actor or {}).get('type') == 'agent' else 'ops',
         'history':[], 'items':deepcopy(s.get('customs_items') or []), 'exporter':exporter,
         'declarant':deepcopy(s.get('declarant') or exporter), 'consignee':deepcopy(s.get('consignee') or {}),
         'transport':{'dispatch_country':(s.get('origin') or {}).get('country'),
                      'destination_country':(s.get('destination') or {}).get('country')},
         'representation_type':'direct','procedure_code':'1000', 'declaration_type':'EX',
         'additional_declaration_type':'A','test':bool(s.get('test'))}
    # Actual structured declaration fields may already exist on the outbound shipment.
    for k in FIELDS - {'items','transport'}:
        if k in s: d[k]=deepcopy(s[k])
    await db.export_declarations.insert_one(deepcopy(d))
    return await get(ident)


async def apply_patch(d, body, actor=None):
    if d['status'] not in EDITABLE: error(409,'Lähetettyä ilmoitusta ei voi muokata.','Filed declaration cannot be edited.')
    unknown = set(body) - FIELDS
    if unknown: error(400,'Tuntematon tai suojattu kenttä.',f'Unknown or protected fields: {sorted(unknown)}')
    if d['status'] != 'draft': d = await transition(d,'draft',actor)
    provenance = {'manual_edit_at': now_iso()} if actor and actor.get('type') != 'agent' else {}
    await db.export_declarations.update_one({'id':d['id'],'status':'draft'},
        {'$set':{**body,**provenance,'updated_at':now_iso()}, '$unset':{'validation':'','controls':'','approval_id':''}})
    return await get(d['id'])


def validation(d):
    reasons = []
    def required(ok, code, fi, en):
        if not ok: reasons.append(agent_runtime.policy_reason(code,fi,en))
    for role in ('exporter','declarant','consignee'):
        required(bool((d.get(role) or {}).get('name')),role,'Osapuolen nimi puuttuu.',f'{role} name is missing.')
    required(bool(d.get('items')), 'items','Tavaraerät puuttuvat.','Goods items are missing.')
    required(bool((d.get('transport') or {}).get('destination_country')), 'destination','Kohdemaa puuttuu.','Destination country missing.')
    required(bool((d.get('invoice') or {}).get('currency')) and (d.get('invoice') or {}).get('total') is not None,
             'invoice','Kauppalaskun valuutta tai summa puuttuu.','Invoice currency or total missing.')
    for it in d.get('items') or []:
        required(len(str(it.get('cn_code') or ''))==8, 'cn','Kahdeksannumeroinen CN-koodi vaaditaan.','An eight-digit CN code is required.')
        required(it.get('statistical_value_cents') is not None,'statistical_value','Tilastoarvo puuttuu.','Statistical value missing.')
    try:
        xml = build_message(d)
        result = validate_xml(xml)
    except (ValueError, TypeError, KeyError) as exc:
        result = {'valid':False,'errors':[str(exc)]}
        reasons.append(agent_runtime.policy_reason('xsd','Ilmoitus ei vastaa Tullin skeemaa.','Declaration fails official Tulli schema validation.'))
    return {'valid':not reasons, 'reasons':reasons,'xsd':result}


async def ready(d, actor=None):
    check = await validation_with_offices(d)
    await db.export_declarations.update_one({'id':d['id']},{'$set':{'validation':check}})
    if not check['valid']: raise HTTPException(422,check)
    return await transition(d,'ready',actor)


async def validation_with_offices(d):
    check = validation(d)
    gateway = await tulli.gateway_for(d, 'export')
    check['offices'] = {'status': 'sandbox_unverified' if gateway.channel == 'sandbox' else 'checked'}
    if gateway.channel != 'sandbox':
        from services.customs_offices import validation_reasons
        check['reasons'].extend(await validation_reasons(d, 'export'))
        check['valid'] = not check['reasons']
    return check


async def assess_controls(d):
    """Always run the real controls services, including for sandbox declarations."""
    try:
        from services import dual_use, customs_screening, customs_licences
    except ImportError as exc:
        return {'reasons':[agent_runtime.policy_reason('controls_unavailable','Vientivalvonnan palvelu puuttuu.','Export controls dependency unavailable.')], 'error':str(exc)}
    reasons, assessments = [], []
    assessment_date = now_iso()[:10]  # one UTC assessment date for all three control services
    destination = (d.get('transport') or {}).get('destination_country')
    screening = await customs_screening.screen_parties(d, kind='export')
    if screening.get('status') != 'clear' or not screening.get('complete'):
        reasons.append(agent_runtime.policy_reason('screening','Pakoteseulonta vaatii tarkistuksen.','Party screening is incomplete or requires review.'))
    for it in d.get('items') or []:
        result = await dual_use.assess(it, flow='export', destination=destination, on=assessment_date)
        assessments.append(result)
        if result.get('controlled') is not False or result.get('licence_required') or result.get('requires_review'):
            reasons.append(agent_runtime.policy_reason('dual_use_export','Kaksikäyttötuotteiden tarkistus tai lupa vaaditaan.','Dual-use classification review or licence is required.'))
        goods = await customs_screening.goods_sanctions(it, flow='export',country=destination,on=assessment_date)
        if goods.get('requires_review') or goods.get('prohibited'):
            reasons.append(agent_runtime.policy_reason('goods_sanctions','Tavaran pakoterajoitus vaatii tarkistuksen.','Goods sanctions require review.'))
    d['destination_country']=destination
    licences = await customs_licences.attribute(d, flow='export', on=assessment_date)  # calls requirements() for every item
    if licences.get('status') != 'ok' or any(i.get('unmet') for i in licences.get('items',[])):
        reasons.append(agent_runtime.policy_reason('licences','TARIC-lupavaatimuksia puuttuu tai niitä ei voitu tarkistaa.','TARIC licence requirements are unmet or unavailable.'))
    return {'assessment_date':assessment_date,'screening':screening,'dual_use':assessments,'licences':licences,'reasons':reasons}


async def file_declaration(decl, actor=None, approval=None):
    async with entity_lock('export:'+decl['id']):
        d = await get(decl['id'])
        if d['status'] not in ('ready','approved'):
            error(409,'Ilmoitus ei ole valmis jätettäväksi.','Export must be ready or approved before filing.')
        check = await validation_with_offices(d)
        try:
            controls = await assess_controls(d) if check['valid'] else {'reasons':[]}
        except Exception as exc:
            controls = {'reasons':[agent_runtime.policy_reason('controls_unavailable','Vientivalvonta ei ole käytettävissä.','Export controls unavailable.')], 'error':str(exc)[:500]}
        reasons = check['reasons'] + controls['reasons']
        shipment = await db.shipments.find_one({'id':d.get('shipment_id')}, NO_ID) or {}
        if shipment.get('compliance_hold'):
            reasons.append(agent_runtime.policy_reason('compliance_hold','Lähetys on vaatimustenmukaisuuden pidossa.','Shipment is on compliance hold.'))
        settings = await get_settings()
        legal = None
        if not settings.get('customs_filing_delegated'): legal='customs_filing_not_delegated'
        elif d.get('representation_type')=='indirect' and settings.get('customs_delegation_scope')!='direct_and_indirect': legal='customs_indirect_representation'
        if any(r['code']=='dual_use_export' for r in reasons): legal='dual_use_export'
        if (controls.get('screening') or {}).get('status')=='potential_match': legal='sanctions_potential_match'
        human_approved = bool(approval and approval.get('status')=='approved' and approval.get('ref_id')==d['id']
                              and approval.get('ref_type')=='export_declaration'
                              and (approval.get('data') or {}).get('fingerprint')==fingerprint(d))
        decision = await agent_runtime.decide('customs_agent','export_file',subject=('export_declaration',d['id']),
            subject_label=d['lrn'], company_id=d.get('company_id'), confidence=None,
            inputs={'validation':check,'controls':controls,'human_approval_id':approval.get('id') if human_approved else None},
            decision={'file':True}, legal_point=None if human_approved else legal, extra_reasons=reasons,
            approval_kind='customs_filing', title='File export '+d['lrn'], title_fi='Vienti-ilmoituksen jättäminen '+d['lrn'],
            link='/ops/customs/export/'+d['id'], approval_data={'kind':'export','declaration_id':d['id'],'fingerprint':fingerprint(d)},
            create_approval=not human_approved)
        await db.export_declarations.update_one({'id':d['id']},{'$set':{'validation':check,'controls':controls,
            'items':d['items'],'decision_id':decision.id,'approval_id':(decision.approval or {}).get('id') or d.get('approval_id')}})
        # An approval cannot override invalid XML, unresolved sanctions, or the kill switch.
        if reasons or settings.get('autopilot_halted') or (not decision.auto and not human_approved):
            return await get(d['id'])
        if d['status']=='ready': d = await transition(d,'approved',actor)
        gateway = await tulli.gateway_for(d,'export')
        try:
            result = await gateway.submit(d)
        except (ValueError, OSError) as exc:
            result = {'state':'error','channel':gateway.channel,'error':str(exc)[:2000],
                      'error_fi':'Ilmoituksen muodostus tai lähetys epäonnistui.','at':now_iso()}

        await db.export_declarations.update_one({'id':d['id']},{'$set':{'submission':result}})
        if result['state'] != 'sent': return await get(d['id'])
        d = await transition(d,'submitted',actor, submission=result)
        if result.get('mrn'): d = await transition(d,'accepted',actor,mrn=result['mrn'])
        await enqueue('customs.poll_export',{'declaration_id':d['id']},key='export-poll:'+d['id'],company_id=d.get('company_id'))
        return d


async def poll_declaration(d):
    gateway = await tulli.gateway_for(d,'export')
    if gateway.channel != (d.get('submission') or {}).get('channel'): return d
    for ev in await gateway.poll(d):
        if ev['type'] != d['status']:
            d = await transition(d,ev['type'],agent_runtime.agent_bot('customs_agent'),mrn=ev.get('mrn') or d.get('mrn'))
        await emit('customs.response_received', {'declaration': d, 'response': ev, 'system': 'export'})
        from services.tulli_exchange import acknowledge
        await acknowledge(ev)
    return d


async def receive_message(d, xml):
    from services.customs_messages.tulli_responses import parse_response
    from services.customs_messages.tulli_store import record
    event = parse_response(xml,'export')
    if event['lrn'] and event['lrn'] != d['lrn']: error(409,'LRN ei vastaa ilmoitusta.','Response LRN does not match declaration.')
    if d.get('mrn') and event['mrn'] and d['mrn'] != event['mrn']: error(409,'MRN ei vastaa ilmoitusta.','Response MRN does not match declaration.')
    if not event['lrn'] and not (d.get('mrn') and event['mrn']==d['mrn']): error(409,'Vastausta ei voida yhdistää ilmoitukseen.','Response has no matching declaration reference.')
    await record(d,xml,system='export',direction='in',channel='manual',validation=event['validation'])
    if event['type'] and event['type'] != d['status']:
        if d['status']=='approved' and event['type'] in ('accepted','rejected'):
            d = await transition(d,'submitted')
        return await transition(d,event['type'],mrn=event.get('mrn') or d.get('mrn'))
    return await get(d['id'])


@job('customs.export_file', agent=True, max_attempts=3, timeout=180, label_fi='Vienti-ilmoituksen jättäminen',label_en='File export')
async def export_file_job(j):
    d = await file_declaration(await get(j['payload']['declaration_id']),agent_runtime.agent_bot('customs_agent'))
    return {'id':d['id'],'status':d['status']}


@job('customs.poll_export', agent=True, max_attempts=3, timeout=180, label_fi='Viennin vastaukset',label_en='Export replies')
async def poll_export_job(j):
    ids = [j['payload']['declaration_id']] if j.get('payload',{}).get('declaration_id') else [d['id'] async for d in db.raw.export_declarations.find({'status':{'$in':['submitted','accepted','under_control','released']}}, {'id':1})]
    out={}
    for ident in ids:
        d = await poll_declaration(await get(ident));out[ident]=d['status']
    return out


schedule('customs.poll_export_every_15s',job='customs.poll_export',every_seconds=15,
         label_fi='Viennin vastausten haku',label_en='Fetch export replies')()


@on('approval.decided')
async def on_approval(payload):
    a = payload.get('approval') or {}
    if a.get('ref_type')=='export_declaration' and a.get('kind')=='customs_filing' and a.get('status')=='approved':
        await file_declaration(await get(a['ref_id']),payload.get('actor'),approval=a)
