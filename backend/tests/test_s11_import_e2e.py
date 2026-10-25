"""Extraction-shaped documents -> real Mongo jobs/agent approval -> sandbox MRN/release.

External TARIC and screening inputs are official captured bytes with hashes. No
customs, control, decision, approval, serializer, gateway or job is mocked.
Sandbox MRNs/releases are simulations, not submissions to Finnish Customs.
"""
import os, uuid
os.environ['DB_NAME'] = 'huo_s11_import_e2e_' + uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['TULLI_MODE'] = 'sandbox'
os.environ['COMPANY_EORI'] = 'FI3990002-2'
os.environ['COMPANY_BUSINESS_ID'] = '3990002-2'
os.environ['TULLI_SANDBOX_RELEASE_SECONDS'] = '0'
os.environ['JOBS_WORKER_ENABLED'] = 'false'
# The filing path below runs on the sandbox registers (399xxxx sandbox EORIs). Since 8acdef8 test tenants read the real
# public registers by default, where these numbers are not registered; core/sandbox.real_registers documents
# TEST_TENANT_REAL_REGISTERS=false as the CI switch. test_real_registers_hold_a_sandbox_eori_filing covers the default.
os.environ['TEST_TENANT_REAL_REGISTERS'] = 'false'
os.environ.pop('OWNER_ALERT_EMAIL',None)
import asyncio, copy, gzip, hashlib, json, time
from pathlib import Path
from datetime import timedelta
import pytest
from lxml import etree
LOOP=asyncio.new_event_loop();asyncio.set_event_loop(LOOP)
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run import e2e in its own pytest process',allow_module_level=True)
from core.db import db, client, ensure_indexes
from core.util import now_iso, now, iso, NO_ID
from core.settings import update_settings, DEFAULTS
from core.sandbox import sandbox_eori
from core.agent_runtime import agent_bot
from core.jobs import enqueue, work_once
from services import customs, taric, screening, extraction
from services.customs_screening_lists import parse_csl, parse_uk
from services.customs_import_data import validations, source_location
from services.customs_messages.tulli_common import validate, schema, NAMESPACES
from services.tulli_codelists import entry
from server import app

EVIDENCE=Path(os.environ.get('IMPORT_E2E_EVIDENCE_DIR','/tmp/import-e2e/evidence'))
FIX=Path(__file__).parent/'fixtures'
BOT=agent_bot('customs_agent')

def run(c): return LOOP.run_until_complete(c)

@pytest.fixture(autouse=True)
def captured_assessment_clock(monkeypatch):
    # Draft, review and filing must query the date present in the captured TARIC URLs.
    # Do not rewrite those URLs or bypass the production tariff/filing checks.
    monkeypatch.setattr(customs,'now_iso',lambda: '2026-10-24T12:00:00Z')

def setup_module():
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    async def go():
        await ensure_indexes()
        await update_settings({**DEFAULTS,'autopilot_enabled':True,'autopilot_halted':False,
                               'customs_filing_delegated':True,'customs_confirm_timeout_hours':0,
                               'guard_confidence_floor':0.9})
        eori=sandbox_eori('3990002-2')
        await db.raw.aeo_eori_checks.insert_one(eori)
        mf=json.loads((FIX/'controls/manifest.json').read_text())
        def raw(k):
            data=gzip.decompress((FIX/'controls'/mf[k]['file']).read_bytes())
            assert hashlib.sha256(data).hexdigest()==mf[k]['sha256']
            return data
        parsed={'eu':screening.parse_eu(raw('eu')), 'un':screening.parse_un(raw('un')),
                'ofac':screening.parse_ofac(raw('ofac'),raw('ofac_alt'),raw('ofac_add')),
                'uk':parse_uk(raw('uk')),'csl':parse_csl(raw('csl'))}
        for k,(entries,generated) in parsed.items():
            await screening._store(k,entries,generated,mf[k]['sha256'],time.monotonic())
    run(go())
    pages={}
    for directory in ('taric','import_e2e'):
        m=json.loads((FIX/directory/'manifest.json').read_text())
        for url,meta in m['pages'].items():
            data=(FIX/directory/meta['file']).read_bytes()
            assert hashlib.sha256(data).hexdigest()==meta['sha256']
            pages[url]=data.decode()
    taric._REPLAY=pages

def teardown_module():
    taric._REPLAY=None
    assert config.DB_NAME.startswith('huo_s11_import_e2e_')
    run(client.drop_database(config.DB_NAME)); LOOP.close()

async def shipment(country='CN',mode='sea',missing=()):
    cid,sid=uuid.uuid4().hex,uuid.uuid4().hex
    c={'id':cid,'name':'Import E2E Sandbox Oy','business_id':'3990001-4','eori':'FI3990001-4',
       'test':True,'country':'FI','kyc_status':'approved','sanctions_status':'clear','credit_limit_cents':100000000,
       'kyc':{'eori':sandbox_eori('3990001-4'),'sanctions':{'status':'pass','checked_at':now_iso()}},
       'poa':{'type':'direct','scope':['import'],'signed_at':now_iso(),'valid_to':iso(now()+timedelta(days=365))[:10]}}
    await db.raw.companies.insert_one(c)
    # An established sandbox customer: first filing correctly requires a separate reviewer under AEO.
    await db.raw.customs_declarations.insert_one({'id':uuid.uuid4().hex,'company_id':cid,'status':'released',
                                                 'test':True,'mrn':'SANDBOX-PRIOR'})
    s={'id':sid,'reference':'IMPORT-'+sid[:8],'company_id':cid,'test':True,'mode':mode,'status':'in_transit',
       'origin':{'country':country},'destination':{'country':'FI','unlocode':'FIHEL'},
       'transaction_nature':'11','office_of_presentation':{'code':'FI002000'},'incoterm':'CIF' if mode=='sea' else 'DAP',
       'incoterm_place':'Helsinki','legs':[{'role':'main','mode':mode,'nationality':country,'from':{'country':country},'to':{'country':'FI'}}]}
    if mode=='sea':
        s['temporary_storage']={'eori':'FI3990002-2','warehouse_code':'001','location_code':'01'}
    else:
        s['inspection_location']={'type':'D','address':{'street':'Sandbox Terminal, Testikatu 1','city':'Helsinki',
                                                       'postal_code':'00100','country':'FI'}}
    if 'location' in missing:
        s.pop('temporary_storage',None);s.pop('inspection_location',None)
    await db.raw.shipments.insert_one(s)
    inc=s['incoterm']
    fields={'invoice_number':'INV-'+sid[:8],'invoice_date':'2026-10-24','seller_name':'Cotton Towels Sandbox Exporter',
            'seller_address':'Test Street 12','seller_city':'Shanghai' if country=='CN' else 'Oslo',
            'seller_postal_code':'200000' if country=='CN' else '0150','seller_country':country,
            'buyer_name':c['name'],'incoterm':inc,'incoterm_place':'Helsinki','currency':'EUR',
            'total_amount':1000,'country_of_origin':country}
    for key in missing:fields.pop(key,None)
    cases=[('commercial_invoice',fields,[{'description':'Cotton terry towels toilet linen','hs_code':'6302600090',
             'quantity':100,'unit':'pcs','amount':1000,'country_of_origin':country}]),
           ('packing_list',{'packing_list_number':'PL-'+sid[:8],'consignee_name':c['name'],
                            'marks_and_numbers':'CTN 1-10 / TOWELS','total_packages':10,'package_type':'cartons','total_gross_weight_kg':120,'total_net_weight_kg':100},[]),
           ('bill_of_lading' if mode=='sea' else 'cmr',
            {'bl_number' if mode=='sea' else 'cmr_number':'TRANSPORT-'+sid[:8],'total_gross_weight_kg':120},[])]
    # Produce exactly extraction.normalise's value envelopes and plain line items; _save writes number.
    # Store before draft (without emitting extraction events that would draft prematurely).
    for kind,raw_fields,lines in cases:
        fs,ls=extraction.normalise(kind,{'fields':{k:{'value':v,'confidence':1} for k,v in raw_fields.items()},'line_items':lines})
        number=next((fs[k]['value'] for k in ('invoice_number','packing_list_number','bl_number','cmr_number') if k in fs),None)
        await db.raw.documents.insert_one({'id':uuid.uuid4().hex,'company_id':cid,'shipment_id':sid,'type':kind,
            'number':number,'filename':kind+'.pdf','uploaded_at':now_iso(),
            'extraction':{'status':'done','fields':fs,'line_items':ls,'confidence':1,'source':'structured','completed_at':now_iso()}})
    d=await customs.draft_declaration_for_shipment(sid,BOT,classify=False)
    return d,s

async def review(d):
    j=await enqueue('customs.review_and_file',{'declaration_id':d['id']},key='import-e2e:'+d['id'],company_id=d['company_id'])
    assert await work_once('import-e2e',max_jobs=1,kinds=['customs.review_and_file'])==1
    return await db.raw.customs_declarations.find_one({'id':d['id']},NO_ID), await db.raw.jobs.find_one({'id':j['id']},NO_ID)

@pytest.mark.parametrize('country,mode',[('CN','sea'),('NO','road')])
def test_draft_job_delegated_approval_sandbox_release(country,mode):
    async def go():
        d,s=await shipment(country,mode)
        assert not customs.blocking(d,owner='customer'),customs.blocking(d)
        d,j=await review(d)
        assert j['status']=='done',j
        assert d['status']=='accepted',json.dumps({'job':j,'blocking':customs.blocking(d),'submission':d.get('submission')},default=str)
        assert d['mrn'][4:7]=='SBX' and d['submission']['sandbox']
        a=await db.raw.approvals.find_one({'id':d['approval_id']},NO_ID)
        assert a['status']=='approved',a
        assert d['items'][0]['cn_confirmed_by_agent']
        assert d['items'][0]['duty_source']=='taric'
        assert d['items'][0]['duty_rate_pct']==12 and d['items'][0]['duty_cents']==12000
        assert d['items'][0]['preference']=='100'  # NO origin alone is not evidence for a preference claim.
        msg=await db.raw.customs_messages.find_one({'declaration_id':d['id'],'direction':'out'},NO_ID)
        assert msg['validation']['valid']
        result=validate(msg['xml'],'import');assert result['valid']
        root=etree.fromstring(msg['xml'].encode());ns={'t':NAMESPACES['import']}
        def texts(path):return root.xpath(path,namespaces=ns)
        assert texts('//t:DutyRegime/text()')==['100']
        assert texts('//t:GovernmentProcedure/t:AdditionalProcedure/t:Procedure/text()')==['999']
        assert sorted(texts('//t:AdditionalDocument[@function="SupportingDocument"]/t:Type/text()'))==['N271','N380']
        assert texts('//t:GoodsLocation/t:Type/text()')==(['B'] if mode=='sea' else ['D'])
        if mode=='sea':assert texts('//t:GoodsLocation/t:Identification[@function="EO"]/text()')==['FI3990002-200101']
        else:assert texts('//t:GoodsLocation/t:Address/t:Line/text()')==['Sandbox Terminal, Testikatu 1']
        assert texts('//t:TransportContractDocument/t:Type/text()')==(['N705'] if mode=='sea' else ['N730'])
        assert texts('//t:Invoice/@CurrencyUnit')==['EUR']
        from services.tulli_sandbox import advance
        assert await advance(d)=='released'
        released=await db.raw.customs_declarations.find_one({'id':d['id']},NO_ID)
        assert released['status']=='released'
        from services.tulli_sandbox import sandbox_decision_items
        decision=sandbox_decision_items(released)
        assert decision[0]['duty_cents']==12000
        (EVIDENCE/f'{country}-{mode}-FI415B.xml').write_text(msg['xml'])
        (EVIDENCE/f'{country}-{mode}-validation.json').write_text(json.dumps(result,indent=2))
        (EVIDENCE/f'{country}-{mode}-lifecycle.json').write_text(json.dumps({'declaration':released,'job':j,'approval':a,'sandbox_decision_items':decision},indent=2,default=str))
        assert not await db.raw.event_failures.find_one({'event':'approval.decided'})
    run(go())

@pytest.mark.parametrize('missing,rule',[('currency','IMP-INVOICE-CURRENCY'),('total_amount','IMP-INVOICE-TOTAL'),
                                       ('seller_city','IMP-PARTY'),('location','IMP-LOCATION-IDENTITY')])
def test_missing_input_blocks_before_approval(missing,rule):
    async def go():
        d,s=await shipment(missing=(missing,))
        rules=[v for v in customs.blocking(d) if v['rule']==rule]
        assert rules and all(v['field'] and v['owner'] and v['message_i18n']['fi'] for v in rules)
        d,j=await review(d)
        assert not d.get('submission') and not d.get('mrn')
        assert not await db.raw.approvals.find_one({'ref_id':d['id'],'kind':'customs_filing'})
        assert not await db.raw.customs_messages.find_one({'declaration_id':d['id']})
        (EVIDENCE/f'blocked-{missing}.json').write_text(json.dumps({'rules':rules,'job':j},indent=2,default=str))
    run(go())

@pytest.mark.parametrize('list_id,code',[('CL754','N705'),('CL754','N730'),('CL017','CT'),('CL603','100'),
                                        ('CL213','N380'),('NCL232','4000999'),('CL347','B'),('CL521','D')])
def test_official_codes(list_id,code):assert entry(list_id,code,on='2026-10-24')

def test_invalid_expired_codes_and_no_location_invention():
    assert not entry('NCL232','4000000')
    assert not entry('CL213','Y900',on='2026-10-24') and entry('CL380','Y900',on='2026-10-24')
    assert not entry('CL754','FAKE')
    assert source_location({'destination':{'unlocode':'FIHEL'},'transaction_nature':'11','office_of_presentation':{'code':'FI002000'}}) is None
    from services.tulli import package_code
    assert package_code('unreadable packaging') is None

def test_rejection_texts_from_official_cl401():
    from services.customs_messages.tulli_responses import parse_response
    path=Path(__file__).resolve().parents[1]/'data/xsd/tulli/import/2.1.7/examples/FI416B.xml'
    xml=path.read_text()
    r=parse_response(xml,'import')
    assert r['errors'][0]['reason']=='NR1305'
    assert r['errors'][0]['message_en']=='Unknown Tulli error code: NR1305'
    # Publisher's envelope with a current official location rule (controlled input mutation).
    r=parse_response(xml.replace('NR1305','NR0234'),'import')
    assert r['errors'][0]['message_fi']=='Tavaroiden sijaintipaikkana täytyy ilmoittaa joko talouden toimija, lupanumero, tullitoimipaikka tai osoite.'
    assert r['errors'][0]['message_en'].startswith('One of the following must be provided as the location of goods:')
    (EVIDENCE/'rejection-NR0234.json').write_text(json.dumps(r,indent=2,ensure_ascii=False))

@pytest.mark.skipif(os.environ.get('IMPORT_E2E_LIVE')!='1',reason='live official reference probe opt-in')
def test_live_official_codelist_contract():
    import httpx
    r=httpx.get('https://raw.githubusercontent.com/finnishcustoms-suomentulli/codelists/master/codelists.json',timeout=60)
    r.raise_for_status();lists={x['Identification']:x for x in r.json()['CodeLists']}
    assert any(x['Code']=='4000999' for x in lists['NCL232']['CodeItems'])
    (EVIDENCE/'live-codelists.json').write_text(json.dumps({'status':r.status_code,'sha256':hashlib.sha256(r.content).hexdigest(),
                                                         'url':str(r.url),'at':now_iso()}))

@pytest.fixture(scope='module')
def draft_for_validation():
    return run(shipment())[0]

@pytest.mark.parametrize('path,value,rule',[
    ('transport.transport_document.type','not_a_document','IMP-TRANSPORT-CODE'),
    ('goods_location.type','A','IMP-LOCATION-TYPE'),
    ('goods_location.identification','FI3990002-2','IMP-LOCATION-IDENTITY'),
    ('items.0.package_type','unreadable','IMP-PACKAGE-CODE'),
    ('items.0.preference','999','IMP-PREFERENCE'),
    ('additional_procedure','000','IMP-PROCEDURE'),
    ('documents.0.code','FAKE','IMP-DOCUMENT-CODE'),
    ('documents.0.number',None,'IMP-DOCUMENT-REFERENCE'),
    ('items.0.net_kg',None,'IMP-NET-MASS'),
    ('transport.nationality',None,'IMP-TRANSPORT-NATIONALITY'),
    ('submitter.email',None,'IMP-SUBMITTER'),
    ('items.0.marks',None,'IMP-SHIPPING-MARKS'),
    ('lrn','X'*23,'IMP-XSD'),
])
def test_precise_preapproval_rules(draft_for_validation,path,value,rule):
    d=copy.deepcopy(draft_for_validation);node=d
    if path.startswith('documents.0.'):
        d['documents'] = [x for x in d['documents'] if x['type']=='commercial_invoice']
    parts=path.split('.')
    for part in parts[:-1]:node=node[int(part)] if isinstance(node,list) else node[part]
    node[parts[-1]]=value
    bad=[v for v in validations(d) if not v['ok'] and v['rule']==rule]
    assert bad and all(v['field'] and v['owner'] and v['message_i18n']['fi'] for v in bad)


def test_location_and_xml_fields_invalidate_approval(draft_for_validation):
    from services.customs import filing_fingerprint
    d=copy.deepcopy(draft_for_validation);before=filing_fingerprint(d)
    d['goods_location']['identification']+='1'
    assert before != filing_fingerprint(d)
    d=copy.deepcopy(draft_for_validation);d['items'][0]['package_type']='BX'
    assert before != filing_fingerprint(d)


def test_broker_uses_recorded_location_only(draft_for_validation):
    from services.agents.customs_agent import broker_patch
    d=copy.deepcopy(draft_for_validation);d['goods_location']=None
    source={'temporary_storage':{'eori':'FI3990002-2','warehouse_code':'001','location_code':'01'}}
    body,_=run(broker_patch(d,source,0.9))
    assert body['goods_location']['identification']=='FI3990002-200101'
    body,_=run(broker_patch(d,{'destination':{'country':'FI','unlocode':'FIHEL'}},0.9))
    assert 'goods_location' not in body and 'location_of_goods' not in body


def test_non_declarable_cn_suffix_is_not_assumed(draft_for_validation):
    d=copy.deepcopy(draft_for_validation)
    it=d['items'][0];it['taric_code']='6302600000'
    run(taric.apply_to_item(it,on='2026-10-24'))
    assert it['taric']['status']=='not_declarable'
    assert any(v['rule']=='TARIC-2' and not v['ok'] for v in taric.rules(d))


def test_incomplete_warehouse_components_are_not_filled():
    assert source_location({'temporary_storage':{'eori':'FI3990002-2'}}) is None
    assert source_location({'temporary_storage':{'eori':'FI3990002-2','warehouse_code':'1','location_code':'01'}}) is None


def test_real_registers_hold_a_sandbox_eori_filing(monkeypatch):
    """Production default (real EU EOS for test tenants too): a sandbox EORI is not registered, so the agent may not
    file; the declaration waits in the owner queue with the EORI reasons instead of reaching the gateway."""
    monkeypatch.setenv('TEST_TENANT_REAL_REGISTERS', 'true')
    async def go():
        saved = await db.raw.aeo_eori_checks.find({}, NO_ID).to_list(100)
        try:
            d, s = await shipment('CN', 'sea')
            d, j = await review(d)
            assert j['status'] == 'done', j
            assert d['status'] == 'ready_for_review' and not d.get('submission'), d.get('status')
            dec = await db.raw.agent_decisions.find_one({'action': 'file_declaration', 'subject_id': d['id']}, NO_ID,
                                                        sort=[('at', -1)])
            codes = {r['code'] for r in dec['reasons']}
            assert dec['outcome'] == 'owner_queue' and {'aeo_declarant_eori', 'aeo_representative_eori'} <= codes, dec
        finally:  # the live EOS answers written back must not leak into the sandbox-register tests
            await db.raw.aeo_eori_checks.delete_many({})
            if saved:
                await db.raw.aeo_eori_checks.insert_many(saved)
    run(go())
