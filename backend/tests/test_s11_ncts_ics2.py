"""Official XSDs, real Mongo/decision/event lifecycle; explicit provider boundary doubles.

Run alone (own DB). Samples are synthetic sandbox goods, never production evidence.
"""
import os,sys,uuid,asyncio,hashlib
from pathlib import Path
os.environ['DB_NAME']='huo_s11_ncts_ics2_'+uuid.uuid4().hex[:10]
os.environ.update(TULLI_TRANSIT_MODE='sandbox',ICS2_MODE='sandbox',TULLI_DECLARANT_ID='FI39900099',
    TULLI_HOLDER_NAME='Sandbox Logistics Oy',TULLI_GUARANTEE_GRN='26FI0000000000000',TULLI_GUARANTEE_ACCESS_CODE='TEST',ICS2_EORI='FI39900099',EMAIL_PROVIDER='log')
sys.path.insert(0,str(Path(__file__).parent/'fixtures'/'ncts_ics2'))
LOOP=asyncio.new_event_loop();asyncio.set_event_loop(LOOP)
import pytest
from copy import deepcopy
from lxml import etree as E
from samples import transit,ens,arrival,unloading,national
from core import config
if config.DB_NAME!=os.environ['DB_NAME']:pytest.skip('Run this module alone',allow_module_level=True)
from core.db import db,client,ensure_indexes
from core.settings import update_settings
from core.events import on,emit
from core.jobs import enqueue,work_once
from services.customs_messages import ncts,ics2,ncts_fi
from services.customs_messages.schema_codec import validate,DATA,parse_xml
from services import customs_transit as tr,customs_ens as en,customs_flow as flow,ics2_sandbox,ics2_gateway
from services.tulli_sandbox import transit_response,transit_sandbox_mrn
def run(c):return LOOP.run_until_complete(c)
EVENTS=[]
for event in flow.EVENTS.values():
    def capture(name):
        async def fn(payload):EVENTS.append((name,payload['declaration']['id']))
        return fn
    on(event)(capture(event))


def setup_module():
    async def setup():
        await ensure_indexes()
        await db.raw.companies.insert_one({'id':'company','name':'Sandbox Company','credit_limit_cents':100000})
    run(setup())

def teardown_module():
    async def finish():
        if os.getenv('S11_EVIDENCE_OUTPUT'):
            import json
            out=Path(os.environ['S11_EVIDENCE_OUTPUT']);out.mkdir(parents=True,exist_ok=True)
            snapshot={'sandbox':True,'external_screening':'controlled test double',
                      'database':db.raw.name,'events':EVENTS}
            for name in ['transit_declarations','ens_filings','agent_decisions','jobs']:
                snapshot[name]=await db.raw[name].find({}, {'_id':0}).to_list(1000)
            (out/'lifecycle-evidence.json').write_text(json.dumps(snapshot,indent=2,default=str)+'\n')
        await client.drop_database(db.raw.name)
    run(finish());LOOP.close()


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # External sanctions-provider boundary only. The real screen_parties walker
    # and decide(), legal delegation, kill switch, Mongo audit writes all execute.
    from services import screening
    async def lists(**kw):
        return [{'list': key, 'version': 'test-provider-v1', 'loaded': True, 'status': 'ok',
                 'fetched_at': __import__('core.util', fromlist=['now_iso']).now_iso()} for key in ('eu', 'un')]
    async def screen(name,**kw):
        if not name:raise ValueError('No name supplied')
        return {'status':'clear','complete':True,'hits':[]}
    monkeypatch.setattr(screening,'refresh_lists',lists)
    monkeypatch.setattr(screening,'lists_status',lists)
    monkeypatch.setattr(screening,'screen_name',screen)
    run(update_settings({'autopilot_enabled':True,'autopilot_halted':False,'customs_filing_delegated':True}))


def test_official_hashes():
    for directory in [DATA/'ncts'/'60.4.16',ics2.BASE,ncts_fi.DIRECTORY]:
        for line in (directory/'SHA256SUMS').read_text().splitlines():
            digest,name=line.split('  ',1)
            assert hashlib.sha256((directory/name).read_bytes()).hexdigest()==digest


@pytest.mark.parametrize('mode',['sea','air','road','rail'])
def test_ens_filings_validate_and_broken_fails(mode):
    d=ens(mode);xml=ics2.build_ens(d);r=validate(xml,ics2.path(d['filing_type']))
    assert E.QName(r).namespace.endswith(':3' if mode in {'road','rail'} else ':2')
    r.remove(next(e for e in r if E.QName(e).localname=='declarant'))
    with pytest.raises(E.DocumentInvalid):validate(E.tostring(r),ics2.path(d['filing_type']))
    assert 'New bicycle frames' in xml


def test_ncts_builders_and_national():
    d=transit();xml=ncts.build_cc015c(d);validate(xml,ncts.DIRECTORY/'CC015C.xsd')
    assert 'SBX-SEAL-1' in xml and '871491' in xml and '1000' in xml
    mrn=transit_sandbox_mrn()
    for message,data in [('CC007C',arrival(mrn)),('CC044C',unloading(mrn)),('CC014C',{
        'TransitOperation':{'MRN':mrn},'Invalidation':{'initiatedByCustoms':'0','justification':'Duplicate filing'},
        'CustomsOfficeOfDeparture':{'referenceNumber':'FI015300'},'HolderOfTheTransitProcedure':{'identificationNumber':'FI39900099'}})]:
        validate(ncts.build(message,data),ncts.DIRECTORY/(message+'.xsd'))
    validate(ncts_fi.build('FI015A',national()),ncts_fi.DIRECTORY/'FI015A.xsd')
    bad=transit();bad['items'][0]['gross_mass']='bad'
    with pytest.raises(Exception):ncts.build_cc015c(bad)
    bad=national();bad['Security']='99'
    with pytest.raises(E.DocumentInvalid):ncts_fi.build('FI015A',bad)


@pytest.mark.parametrize('message',list(ncts.STATES))
def test_ncts_responses(message):
    p=ncts.parse_response(transit_response(transit(),message))
    assert p['type']==message and p['status']==ncts.STATES[message]
    assert len(p['mrn'])==18 and p['mrn'][16]=='J'


@pytest.mark.parametrize('message',['IE3R01','IE3N03','IE3Q01','IE3Q02','IE3R07'])
def test_ics2_responses(message):
    p=ics2.parse_response(ics2_sandbox.response(ens('sea'),message))
    assert p['status']==ics2.STATES[message]


def test_deadlines_and_boundaries():
    d={'arrival':'2026-10-25T12:00:00Z','loading':'2026-10-24T10:00:00Z','departure':'2026-10-25T08:00:00Z'}
    assert ics2.filing_deadlines('sea',**d,sea_category='containerised')['filing']=='2026-10-23T10:00:00+00:00'
    for category,hour in [('bulk',8),('short_sea',10),('overseas_under_24h',10)]:
        assert f'T{hour:02}:00' in ics2.filing_deadlines('sea',**d,sea_category=category)['filing']
    assert 'T11:00' in ics2.filing_deadlines('road',**d)['filing']
    for duration,hour in [(1.99,11),(2,10),(3,10)]:
        assert f'T{hour:02}:00' in ics2.filing_deadlines('rail',**d,duration_hours=duration)['filing']
    assert 'T10:00' in ics2.filing_deadlines('inland_waterway',**d)['filing']
    assert ics2.filing_deadlines('air',**d,duration_hours=3.99)['pre_arrival']=='2026-10-25T08:00:00+00:00'
    assert ics2.filing_deadlines('air',**{**d,'departure':'2026-10-25T06:00:00Z'},duration_hours=4)['pre_arrival']=='2026-10-25T08:00:00+00:00'
    with pytest.raises(ValueError):ics2.filing_deadlines('rail',**d)
    with pytest.raises(ValueError):ics2.filing_deadlines('sea',**d)
    with pytest.raises(ValueError):ics2.filing_deadlines('road',arrival='2026-10-25T12:00:00')


async def shipment():
    sid=uuid.uuid4().hex
    await db.raw.shipments.insert_one({'id':sid,'company_id':'company','mode':'road',
        'origin':{'country':'CN'},'destination':{'country':'FI'}})
    return sid

async def work(kind,payload):
    j=await enqueue(kind,payload)
    assert await work_once('s11-test',kinds=[kind],max_jobs=1)==1
    saved=await db.raw.jobs.find_one({'id':j['id']})
    assert saved['status']=='done',saved.get('last_error') or saved
    return saved


def test_transit_lifecycle_jobs_events_audit():
    async def go():
        d=await tr.create(await shipment(),transit());ident=d['id']
        d=await tr.ready(ident);assert d['status']=='ready'
        await work('customs.transit_file',{'declaration_id':ident})
        assert (await flow.get('transit',ident))['status']=='submitted'
        await work('customs.poll_transit',{});assert (await flow.get('transit',ident))['status']=='accepted'
        await work('customs.poll_transit',{});d=await flow.get('transit',ident);assert d['status']=='released'
        # No invented physical arrival: another poll leaves released.
        await tr.poll_transit({});assert (await flow.get('transit',ident))['status']=='released'
        with pytest.raises(ValueError):await tr.action(ident,'unloading-remarks',unloading(d['mrn']))
        await tr.action(ident,'arrival',arrival(d['mrn']))
        await work('customs.poll_transit',{});assert (await flow.get('transit',ident))['status']=='unloading'
        await tr.action(ident,'unloading-remarks',unloading(d['mrn']))
        await work('customs.poll_transit',{});assert (await flow.get('transit',ident))['status']=='arrived'
        await work('customs.poll_transit',{});d=await flow.get('transit',ident);assert d['status']=='closed'
        assert ('customs.transit.released',ident) in EVENTS and ('customs.transit.closed',ident) in EVENTS
        assert await db.raw.agent_decisions.count_documents({'subject_id':ident,'outcome':'auto'})==3
        assert len(d['messages'])==8
        assert all(m['xsd_valid'] and m['sandbox'] for m in d['messages'])
    run(go())


def test_ens_lifecycle_and_dnl_cannot_be_cleared_by_late_response():
    async def go():
        d=await en.create(await shipment(),ens('road'));ident=d['id']
        # creation has already scheduled the filing job
        assert await work_once('s11-test',kinds=['customs.ens_file'],max_jobs=1)==1
        assert (await flow.get('ens',ident))['status']=='submitted'
        await work('customs.poll_ics2',{});d=await flow.get('ens',ident);assert d['status']=='registered'
        await work('customs.poll_ics2',{});assert (await flow.get('ens',ident))['status']=='assessment_complete'
        assert ('customs.ens.accepted',ident) in EVENTS
        d=await en.record_response(ident,ics2_sandbox.response(d,'IE3Q02'),'sandbox');assert d['status']=='referral'
        assert ('customs.ens.referral',ident) in EVENTS
        xml=ics2_sandbox.response(d,'IE3Q01')
        d=await en.record_response(ident,xml,'sandbox');assert d['status']=='do_not_load'
        before=len(EVENTS);await en.record_response(ident,xml,'sandbox');assert len(EVENTS)==before
        await en.record_response(ident,ics2_sandbox.response(d,'IE3N03'),'sandbox')  # exact duplicate is ignored
        assert (await flow.get('ens',ident))['status']=='do_not_load'
        late=ics2_sandbox.response(d,'IE3N03').replace('completionDate>', 'completionDate>')
        late=late.replace(ics2.timestamp(),'2026-12-31T12:00:00Z')
        with pytest.raises(ValueError):await en.record_response(ident,late,'sandbox')
        assert ('customs.ens.do_not_load',ident) in EVENTS
        assert await db.raw.agent_decisions.count_documents({'subject_id':ident,'action':'file_ens'})==1
    run(go())


def test_kill_delegation_and_screening_fail_closed(monkeypatch):
    async def go():
        d=await tr.create(await shipment(),transit());await tr.ready(d['id'])
        await update_settings({'autopilot_halted':True})
        assert (await tr.file_declaration(d['id']))['status']=='ready'
        assert await db.raw.customs_messages.count_documents({'declaration_id':d['id'],'direction':'out'})==0
        await update_settings({'autopilot_halted':False,'customs_filing_delegated':False})
        await tr.file_declaration(d['id'])
        row=await db.raw.agent_decisions.find_one({'subject_id':d['id'],'legal_point':'customs_filing_not_delegated'})
        assert row and row['outcome']!='auto'
        await update_settings({'customs_filing_delegated':True})
        from services import screening
        async def hit(name,**kw):return {'status':'potential_match','complete':True,'hits':[{'name':name,'score':1}]}
        monkeypatch.setattr(screening,'screen_name',hit)
        assert (await tr.file_declaration(d['id']))['status']=='ready'
        assert await db.raw.customs_messages.count_documents({'declaration_id':d['id'],'direction':'out'})==0
    run(go())


def test_mismatched_response_and_xml_entities_rejected():
    async def go():
        d=await tr.create(await shipment(),transit());await tr.ready(d['id']);await tr.file_declaration(d['id'])
        wrong={**d,'lrn':'WRONG-LRN'}
        with pytest.raises(ValueError):await tr.record_response(d['id'],transit_response(wrong,'CC028C'))
        assert (await flow.get('transit',d['id']))['status']=='submitted'
    run(go())
    with pytest.raises(ValueError):parse_xml(b'<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><foo>&x;</foo>')


def test_domibus_envelope_is_standard_and_does_not_claim_registration(monkeypatch):
    for key,value in {'ICS2_PARTY_ID':'sender','ICS2_RECEIVER_PARTY_ID':'receiver','ICS2_SERVICE':'service',
                      'ICS2_ACTION':'action','ICS2_FINAL_RECIPIENT':'ICS2'}.items():monkeypatch.setenv(key,value)
    xml=ics2.build_ens(ens('road'));root=parse_xml(ics2_gateway.envelope('submitRequest',xml,'sbx@local'))
    import base64
    assert base64.b64decode(root.xpath('//*[local-name()="payload"]/value/text()')[0]).decode()==xml
    assert root.xpath('//*[local-name()="Action"]/text()')==['action']
    assert root.xpath('//*[local-name()="MessageId"]/text()')==['sbx@local']


@pytest.mark.skipif(os.getenv('S11_LIVE')!='1',reason='set S11_LIVE=1 for official source probe')
def test_live_tulli_current_phase():
    import requests
    r=requests.get('https://finnishcustoms-suomentulli.github.io/MIGs/Transit/common/intro.json',timeout=30)
    r.raise_for_status();j=r.json()
    assert j['Versions']['2.0.0']['info']['remark']['en']=='NCTS phase 6'
    assert j['Versions']['2.2.0']['info']['releaseDate'].startswith('2026-10-01')


@pytest.mark.parametrize('message',['IE3Q03','IE3N01','IE3N99','IE3N06'])
def test_other_ics2_responses(message):
    mrn='26FISBX000000001R0';stamp={'DateTime':'2026-10-25T12:00:00Z'}
    data={'MRN':mrn,'documentIssueDate':stamp,'responsibleMemberState':{'country':'FI'},
          'declarant':{'identificationNumber':'FI39900099'}}
    if message=='IE3Q03':data['referralRequestDetails']={'referralRequestReference':'SBX-RFS',
        'requestType':'RFS','transportDocumentHouse':{'documentNumber':'SBX-HOUSE-001','type':'N714'}}
    elif message=='IE3N01':data={'LRN':'SBX-ENS','notificationDate':stamp,'declarant':{'identificationNumber':'FI39900099'},
        'error':{'description':'Missing commodity code','validationCode':'100','pointer':{'messageElementPath':'/IE3F50/consignmentMasterLevel'}}}
    elif message=='IE3N99':data={'functionalReference':'SBX-ENS','notificationDate':stamp,
        'error':{'description':'Invalid XML','validationCode':'100'}}
    else:data={'LRN':'SBX-ARRIVAL','documentIssueDate':stamp,
        'activeBorderTransportMeans':{'ModeOfTransport':'1','actualDateAndTimeOfArrival':stamp},
        'relatedMRN':[{'identification':mrn}],'actualCustomsOfficeOfFirstEntry':{'referenceNumber':'FI015300'}}
    p=ics2.parse_response(ics2.build(message,data))
    assert p['status']==ics2.STATES[message]
    if message=='IE3N06':assert p['related_mrns']==[mrn]


def test_ens_arrival_and_manual_file_does_not_claim_submitted():
    async def go():
        d=await en.create(await shipment(),ens('sea'));ident=d['id']
        xml=await en.sti_export(ident)
        assert 'IE3F15' in xml and (await flow.get('ens',ident))['status']=='draft'
        d=await en.record_response(ident,ics2_sandbox.response(d,'IE3R01'))
        d=await en.record_response(ident,ics2_sandbox.response(d,'IE3N03'))
        stamp={'DateTime':'2026-10-25T12:00:00Z'}
        data={'LRN':'SBX-ARRIVAL','documentIssueDate':stamp,
            'activeBorderTransportMeans':{'ModeOfTransport':'1','actualDateAndTimeOfArrival':stamp},
            'relatedMRN':[{'identification':d['mrn']}],'actualCustomsOfficeOfFirstEntry':{'referenceNumber':'FI015300'}}
        d=await en.record_response(ident,ics2.build('IE3N06',data));assert d['status']=='arrived'
    run(go())


def test_ens_auto_create_and_deadline():
    async def go():
        sid=await shipment();s=await db.raw.shipments.find_one({'id':sid},{'_id':0})
        s['ens']=ens('road')
        await emit('shipment.created',{'shipment':s})
        await emit('shipment.created',{'shipment':s})
        rows=await flow.collection('ens').find({'shipment_id':sid},{'_id':0}).to_list(None)
        assert len(rows)==1 and rows[0]['status']=='draft'
        assert rows[0]['deadlines']['filing']=='2026-10-25T11:00:00+00:00'
        j=await db.raw.jobs.find_one({'payload.filing_id':rows[0]['id']})
        assert j and j['kind']=='customs.ens_file'
    run(go())


def test_actual_authenticated_http_routes():
    async def go():
        import httpx
        import server
        from core.security import create_session_token,load_secret
        await load_secret(db)
        uid=uuid.uuid4().hex
        await db.raw.users.insert_one({'id':uid,'role':'ops_admin','org_type':'internal','session_version':0})
        headers={'Authorization':'Bearer '+create_session_token(uid),'X-Include-Test-Tenants':'true'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as c:
            assert (await c.get('/api/customs-transit')).status_code==401
            sid=await shipment()
            r=await c.post('/api/customs-transit',json={'shipment_id':sid,**transit()},headers=headers)
            assert r.status_code==200,r.text
            ident=r.json()['id']
            r=await c.post('/api/customs-transit/'+ident+'/ready',headers=headers);assert r.status_code==200,r.text
            r=await c.post('/api/customs-transit/'+ident+'/file',headers=headers);assert r.status_code==200,r.text
            assert r.json()['status']=='submitted'
            mid=r.json()['messages'][0]['id']
            r=await c.get(f'/api/customs-transit/{ident}/messages/{mid}/xml',headers=headers)
            assert r.status_code==200 and 'CC015C' in r.text
            r=await c.get('/api/customs-ens/deadline',params={'mode':'road','arrival':'2026-10-25T12:00:00Z'},headers=headers)
            assert r.status_code==200 and 'T11:00' in r.json()['filing']
            r=await c.patch('/api/customs-transit/'+ident,json={'status':'closed'},headers=headers)
            assert r.status_code==422
    run(go())


def test_tulli_transport_is_explicitly_blocked(monkeypatch):
    monkeypatch.setenv('TULLI_TRANSIT_MODE','production')
    from services.tulli_transit_gateway import require_ready
    assert tr.gateway_status()['ready'] is False
    with pytest.raises(ValueError,match='TULLI_CLIENT_CERT'):require_ready()


def test_missing_party_name_blocks_actual_outgoing_xml():
    async def go():
        d=ens('sea');d.pop('parties')
        d=await en.create(await shipment(),d)
        row=await en.file_filing(d['id'])
        assert row['status']=='draft' and row['screening']['status']=='error'
        assert not await db.raw.customs_messages.count_documents({'declaration_id':d['id'],'direction':'out'})
    run(go())


def official_payload(message):
    root=parse_xml((Path(__file__).parent/'fixtures/ncts_ics2/official-fi'/(message+'.xml')).read_bytes())
    def convert(e):
        attrs={'@'+k:v for k,v in e.attrib.items() if not k.startswith('{')}
        if not len(e):return {**attrs,'#text':e.text} if attrs else e.text
        result=attrs
        for c in e:
            k=E.QName(c).localname;v=convert(c)
            if k in result:
                if not isinstance(result[k],list):result[k]=[result[k]]
                result[k].append(v)
            else:result[k]=v
        return result
    return convert(root)


@pytest.mark.parametrize('message',['FI015A','FI007A','FI044A'])
def test_national_builders_against_published_examples(message):
    xml=ncts_fi.build(message,official_payload(message))
    assert E.QName(validate(xml,ncts_fi.DIRECTORY/(message+'.xsd'))).localname==message


def test_fi_invalidation_order_and_date_contract():
    data=official_payload('FI014A')
    # Published example is stale: Z date fails current pattern and Submitter is
    # absent. Keep source untouched; add explicit synthetic submitter data.
    data['Notification']['Notification']='2026-10-24T12:00:00'
    data['Submitter']=national()['Submitter']
    xml=ncts_fi.build('FI014A',data)
    validate(xml,ncts_fi.DIRECTORY/'FI014A.xsd')
    assert xml.index('<m:Submitter>')<xml.index('<m:Applicant>')


def test_stale_authority_example_is_not_silently_accepted():
    raw=(Path(__file__).parent/'fixtures/ncts_ics2/official-fi/FI028A.xml').read_bytes()
    with pytest.raises(E.DocumentInvalid):ncts_fi.parse_response(raw)


def test_fi_response_correlation():
    from services.customs_messages.schema_codec import encode
    data=official_payload('FI028A')
    data['Notification']['Notification']='2026-10-24T12:00:00'
    xml=encode(ncts_fi.DIRECTORY/'FI028A.xsd','FI028A',data)
    result=ncts_fi.parse_response(xml)
    assert result['status']=='accepted' and result['mrn'] and result['lrn']


def test_domibus_http_boundary_and_missing_receipt(monkeypatch):
    import httpx,ssl
    for key,value in {'ICS2_MODE':'conformance','ICS2_AS4_ENDPOINT':'https://ap.example.invalid/backend',
        'ICS2_AS4_CERT':'test-only.pem','ICS2_AS4_KEY':'test-only.key','ICS2_PARTY_ID':'sender',
        'ICS2_RECEIVER_PARTY_ID':'receiver','ICS2_SERVICE':'service','ICS2_ACTION':'action',
        'ICS2_FINAL_RECIPIENT':'ICS2'}.items():monkeypatch.setenv(key,value)
    # No certificate or network available; replace those external boundaries.
    # Actual request encoder, HTTP body and response decoder remain in use.
    monkeypatch.setattr(ssl.SSLContext,'load_cert_chain',lambda *a,**kw:None)
    factory=httpx.AsyncClient;sent=[];response=[b'<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body><ws:submitResponse xmlns:ws="http://org.ecodex.backend/1_1/"><messageID>receipt@ap</messageID></ws:submitResponse></soap:Body></soap:Envelope>']
    def handle(req):
        sent.append(req)
        assert req.url.path=='/backend'
        assert req.headers['content-type'].startswith('application/soap+xml')
        assert parse_xml(req.content).xpath('//*[local-name()="submitRequest"]/payload/value')
        return httpx.Response(200,content=response[0])
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:factory(**kw,transport=httpx.MockTransport(handle)))
    async def go():
        result=await ics2_gateway.submit(ics2.build_ens(ens('road')),'message@local')
        assert result['state']=='submitted' and result['transport_id']=='receipt@ap' and 'mrn' not in result
        response[0]=b'<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body/></soap:Envelope>'
        with pytest.raises(ValueError,match='messageID'):await ics2_gateway.submit('<ignored/>','message@local')
        response[0]=b'<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body><soap:Fault/></soap:Body></soap:Envelope>'
        with pytest.raises(ValueError,match='SOAP Fault'):await ics2_gateway.submit('<ignored/>','message@local')
    run(go());assert len(sent)==3


def test_uncertain_post_is_not_repeated(monkeypatch):
    monkeypatch.setenv('ICS2_MODE','conformance');calls=[]
    # The real office gate must clear before exercising an uncertain transport.
    # Use captured DG TAXUD bytes, with its clock pinned to the capture instant.
    import json
    from datetime import datetime, timedelta
    from services import customs_offices as col
    fixture = Path(__file__).parent/'fixtures/offices'
    source = json.loads((fixture/'SOURCE.json').read_text())
    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(source['fetched_at']) + timedelta(seconds=1)
    monkeypatch.setattr(col, 'datetime', CaptureClock)
    run(col.install_snapshot((fixture/'COL-Generic.zip').read_bytes(), source))
    async def uncertain(xml,message_id):calls.append(message_id);raise TimeoutError('Transport outcome unknown')
    monkeypatch.setattr(ics2_gateway,'submit',uncertain)
    async def go():
        d=await en.create(await shipment(),ens('road'))
        with pytest.raises(TimeoutError):await en.file_filing(d['id'])
        row=await flow.get('ens',d['id']);assert row['delivery_pending'] is True and row['status']=='draft'
        with pytest.raises(ValueError,match='uncertain'):await en.file_filing(d['id'])
        assert len(calls)==1
    run(go())


def test_referral_response_and_invalidation_are_gated_and_validate():
    async def go():
        d=await en.create(await shipment(),ens('air'));ident=d['id'];await en.file_filing(ident)
        await en.poll_ics2({});d=await flow.get('ens',ident)
        d=await en.record_response(ident,ics2_sandbox.response(d,'IE3Q02'),'sandbox')
        assert d['last_response']['data']['referralRequestDetails']['referralRequestReference']=='SANDBOX-RFI'
        d=await en.action(ident,'rfi-response',{'responsibleMemberState':{'country':'FI'},
            'additionalInformationResponse':{'referralRequestReference':'SANDBOX-RFI'}})
        await en.poll_ics2({});assert (await flow.get('ens',ident))['status']=='assessment_complete'
        await en.action(ident,'invalidate',{})
        await en.poll_ics2({});d=await flow.get('ens',ident);assert d['status']=='invalidated'
        types={m['type'] for m in d['messages']};assert {'IE3R02','IE3Q04','IE3R07'}<=types
        assert await db.raw.agent_decisions.count_documents({'subject_id':ident,'outcome':'auto'})==3
    run(go())


def test_auto_created_draft_receives_later_carrier_data():
    async def go():
        sid=await shipment();d=await en.create(sid,{'mode':'road'})
        assert d.get('deadline_missing') is True and not d.get('ens_data')
        d=await en.create(sid,ens('road'))
        assert d['ens_data']['consignmentMasterLevel']['transportDocumentMasterLevel']['documentNumber']=='SBX-MASTER-001'
        assert d['deadline_missing'] is False
        assert await flow.collection('ens').count_documents({'shipment_id':sid})==1
    run(go())
