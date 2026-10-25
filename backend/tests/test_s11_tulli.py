"""Official XSDs, captured real control feeds, Mongo and authenticated in-process export API.
No mocked success results for controls or gateways. Only clocks/delays are accelerated.
"""
import os, uuid
os.environ['DB_NAME']='huo_s11_tulli_'+uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER']='log'
os.environ['JWT_SECRET']='tulli-test-secret-sufficiently-long'
import asyncio, copy, gzip, hashlib, json, time
from pathlib import Path
import httpx
import pytest
from lxml import etree
LOOP=asyncio.new_event_loop();asyncio.set_event_loop(LOOP)
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run Tulli tests in their own pytest process',allow_module_level=True)
from core.db import db,client,ensure_indexes
from core.events import on
from core.settings import update_settings
from services import tulli, customs_export as exports, tulli_sandbox, taric, screening
from services.customs_messages.tulli_import import build_message as import_xml
from services.customs_messages.tulli_export import build_message as export_xml
from services.customs_messages.tulli_common import ROOT,VERSIONS,assert_valid,parse,NAMESPACES
from services.customs_messages.tulli_responses import parse_response
FIX=Path(__file__).parent/'fixtures'
SAMPLE=json.loads((FIX/'tulli/export-declaration.json').read_text())
EVIDENCE=Path(os.environ.get('S11_EVIDENCE_DIR', '/tmp/huolitsija-s11-tulli'))
EVENTS=[]
for event in ('customs.export.released','customs.export.exited'):
    async def capture(payload,event=event): EVENTS.append((event,payload['declaration']['id']))
    on(event)(capture)

def run(coro): return LOOP.run_until_complete(coro)

@pytest.fixture(autouse=True)
def captured_assessment_clock(monkeypatch):
    # TARIC URLs in this replay were captured for this assessment date.
    # Keep the real controls and missing-capture failures; pin only their clock.
    monkeypatch.setattr(exports,'now_iso',lambda: '2026-10-24T12:00:00Z')

@pytest.fixture(scope='module',autouse=True)
def setup():
    from services.customs_screening_lists import parse_uk,parse_csl
    manifest=json.loads((FIX/'controls/manifest.json').read_text())
    def raw(key):
        blob=gzip.decompress((FIX/'controls'/manifest[key]['file']).read_bytes())
        assert hashlib.sha256(blob).hexdigest()==manifest[key]['sha256']
        return blob
    parsed={'eu':screening.parse_eu(raw('eu')),'un':screening.parse_un(raw('un')),
            'ofac':screening.parse_ofac(raw('ofac'),raw('ofac_alt'),raw('ofac_add')),
            'uk':parse_uk(raw('uk')),'csl':parse_csl(raw('csl'))}
    async def init():
        await ensure_indexes()
        for key,(entries,generated) in parsed.items():
            await screening._store(key,entries,generated,manifest[key]['sha256'],time.monotonic())
        await db.companies.insert_one({'id':'sandbox-export','business_id':'3990009-9','test_tenant':True,
                                      'name':'Sandbox Export Oy','eori':'FI3990009-9','credit_limit_cents':1000000})
        await db.users.insert_one({'id':'tulli-reviewer','org_type':'internal','role':'ops_admin','session_version':0})
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False,'customs_filing_delegated':True})
    run(init())
    pages={}
    for directory in (FIX/'taric',FIX/'tulli/taric'):
        for url,m in json.loads((directory/'manifest.json').read_text())['pages'].items():
            blob=(directory/m['file']).read_bytes()
            assert hashlib.sha256(blob).hexdigest()==m['sha256']
            pages[url]=blob.decode()
    taric._REPLAY=pages
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    yield
    taric._REPLAY=None
    run(client.drop_database(config.DB_NAME));LOOP.close()


def test_import_official_schema_and_source_mapping():
    d=copy.deepcopy(SAMPLE);d['type']='import';d['importer']=d['declarant'];d['transport']['destination_country']='FI';d['transport']['dispatch_country']='CN'
    d['items'][0]['taric_code']='8712003010'
    xml=import_xml(d)
    result=assert_valid(xml,'import')
    assert result['schema_version']=='3.0.37'
    root=parse(xml);ns={'t':NAMESPACES['import']}
    assert root.tag=='{'+ns['t']+'}FI415B'
    assert root.xpath('string(.//t:Importer/t:Identification)',namespaces=ns)=='FI3990009-9'
    assert root.xpath('string(.//t:Classification[t:IdentificationType="TAR"]/t:Identification)',namespaces=ns)=='10'
    assert root.xpath('string(.//t:NetNetMass)',namespaces=ns)=='12'
    assert tulli.build_message(d,'SANDBOX')==xml
    (EVIDENCE/'import-FI415B.xml').write_text(xml)
    (EVIDENCE/'import-validation.json').write_text(json.dumps(result,indent=2))


def test_export_official_schema_and_broken_message():
    xml=export_xml(SAMPLE);result=assert_valid(xml,'export')
    assert result['schema_version']=='1.0.90'
    root=parse(xml);ns={'t':NAMESPACES['export']}
    assert root.xpath('string(.//t:Classification[t:IdentificationType="CN"]/t:Identification)',namespaces=ns)=='30'
    assert root.xpath('string(.//t:AdditionalDocument/t:Identification)',namespaces=ns)=='Ordinary pedal bicycle without fluorinated gases'
    (EVIDENCE/'export-FI515A.xml').write_text(xml)
    (EVIDENCE/'export-validation.json').write_text(json.dumps(result,indent=2))
    root.remove(root.find('{'+ns['t']+'}ExitOffice'))
    with pytest.raises(ValueError,match='Tulli XSD'): assert_valid(etree.tostring(root),'export')
    (EVIDENCE/'broken-export-rejected.txt').write_text('Removing mandatory ExitOffice fails official FI515A XSD validation.\n')
    d=copy.deepcopy(SAMPLE);d['items'][0]['net_kg']=-1
    with pytest.raises(ValueError): export_xml(d)


def test_unmodified_authority_export_example_validates():
    example=ROOT/'export'/VERSIONS['export']/'examples/FI515A.xml'
    assert assert_valid(example.read_bytes(),'export')['valid']


@pytest.mark.parametrize('name,event,mrn',[('FI528A','accepted',True),('FI529A','released',True),('FI599A','exited',True),('FI516A','rejected',False),('FI544A','under_control',True),('FI510A','invalidated',True)])
def test_official_responses(name,event,mrn):
    xml=(ROOT/'export'/VERSIONS['export']/'examples'/(name+'.xml')).read_bytes()
    response=parse_response(xml,'export')
    assert response['type']==event
    assert bool(response['mrn'])==mrn
    if name=='FI516A': assert response['errors'][0]['reason']=='NR5507'


@pytest.mark.parametrize('base,override,expected',[('disabled',None,'disabled'),('production','sandbox','sandbox'),('sandbox','test','test'),('test','production','production')])
def test_gateway_modes_and_sandbox_isolation(monkeypatch,base,override,expected):
    monkeypatch.setattr(config,'TULLI_MODE',base);monkeypatch.setattr(config,'TULLI_EXPORT_MODE',override)
    monkeypatch.setattr(config,'TULLI_IMPORT_MODE','disabled')
    assert tulli.get_gateway('export').mode==expected
    assert tulli.get_gateway().mode=='disabled'
    assert run(tulli.gateway_for(SAMPLE,'export')).mode=='sandbox'
    assert run(tulli.gateway_for(SAMPLE)).mode=='sandbox'


def test_invalid_mode_and_missing_transport_configuration_are_explicit(monkeypatch):
    monkeypatch.setattr(config,'TULLI_EXPORT_MODE','typo')
    with pytest.raises(ValueError):tulli.get_gateway('export')
    gateway=tulli.MessageExchangeGateway('test','export')
    assert not gateway.status()['ready'] and gateway.status()['transport_implemented']
    assert run(gateway.submit(SAMPLE))['state']=='error'


async def make_draft(suffix):
    s={'id':'shipment-'+suffix,'reference':'OUT-'+suffix,'direction':'export','company_id':'sandbox-export',
       'origin':{'country':'FI'},'destination':{'country':'US'},'customs_items':copy.deepcopy(SAMPLE['items'])}
    for k in exports.FIELDS-{'items','transport'}:
        if k in SAMPLE:s[k]=copy.deepcopy(SAMPLE[k])
    await db.shipments.insert_one(s)
    return await exports.draft_declaration_for_shipment(s['id'])


def test_authenticated_sandbox_export_end_to_end(monkeypatch):
    import server
    from core.security import create_session_token
    from services.customs import mrn_check_digit
    monkeypatch.setattr(tulli_sandbox,'RELEASE_SECONDS',0);monkeypatch.setenv('TULLI_SANDBOX_EXIT_SECONDS','0')
    async def go():
        d=await make_draft('e2e')
        token=create_session_token('tulli-reviewer')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://tulli') as h:
            assert (await h.get('/api/customs-export/'+d['id'])).status_code==401
            h.headers['Authorization']='Bearer '+token
            r=await h.post('/api/customs-export/'+d['id']+'/ready');assert r.status_code==200,r.text
            r=await h.post('/api/customs-export/'+d['id']+'/file');assert r.status_code==200,r.text
            current=r.json(); assert current['status']=='accepted',json.dumps(current.get('controls'))
            mrn=current['mrn'];assert len(mrn)==18 and mrn[-2]=='E' and mrn[-1]==mrn_check_digit(mrn[:-1])
            assert current['submission']['sandbox']
            r=await h.post('/api/customs-export/'+d['id']+'/poll');assert r.status_code==200,r.text
            assert r.json()['status']=='released'
            r=await h.post('/api/customs-export/'+d['id']+'/poll');assert r.status_code==200,r.text
            current=r.json();assert current['status']=='exited'
            assert [x['to'] for x in current['history']]==['ready','approved','submitted','accepted','released','exited']
            assert EVENTS[-2:]==[('customs.export.released',d['id']),('customs.export.exited',d['id'])]
            audit=await db.agent_decisions.find_one({'subject_id':d['id'],'action':'export_file'},{'_id':0})
            assert audit['agent']=='customs_agent' and audit['outcome']=='auto'
            messages=await db.customs_messages.find({'declaration_id':d['id']},{'_id':0}).to_list(20)
            assert messages[0]['validation']['valid'] and messages[0]['sandbox']
            assert parse(messages[0]['xml']).tag.endswith('FI515A')
            before=len(EVENTS);await exports.poll_declaration(current);assert len(EVENTS)==before
            (EVIDENCE/'sandbox-export-e2e.json').write_text(json.dumps({'declaration':current,'decision':audit,'events':EVENTS,'messages':messages},indent=2,default=str))
    run(go())


def test_controls_failure_stops_filing_and_audits():
    async def go():
        d=await make_draft('blocked');d['items'][0].pop('dual_use_classification')
        d=await exports.apply_patch(d,{'items':d['items']})
        d=await exports.ready(d);d=await exports.file_declaration(d)
        assert d['status']=='ready'
        assert any(r['code']=='dual_use_export' for r in d['controls']['reasons'])
        assert not await db.customs_messages.find_one({'declaration_id':d['id']})
        assert await db.agent_decisions.find_one({'subject_id':d['id'],'legal_point':'dual_use_export'})
    run(go())


def test_missing_required_values_cannot_be_ready():
    async def go():
        d=await make_draft('missing');d=await exports.apply_patch(d,{'exit_office':{}})
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:await exports.ready(d)
        assert e.value.status_code==422
        assert (await exports.get(d['id']))['status']=='draft'
    run(go())


def test_kill_switch_holds_export_and_jobs():
    from core.jobs import registered_jobs
    async def go():
        d=await make_draft('halt');d=await exports.ready(d)
        await update_settings({'autopilot_halted':True})
        try:
            d=await exports.file_declaration(d);assert d['status']=='ready'
            assert not await db.customs_messages.find_one({'declaration_id':d['id']})
        finally:await update_settings({'autopilot_halted':False})
    run(go())
    assert registered_jobs()['customs.export_file']['agent']
    assert registered_jobs()['customs.poll_export']['agent']


@pytest.mark.skipif(os.getenv('TULLI_LIVE')!='1',reason='Set TULLI_LIVE=1 to verify official schema download')
def test_live_official_schema_download():
    import requests
    manifest=json.loads((ROOT/'export/1.1.1/manifest.json').read_text())
    archive=manifest[0]
    r=requests.get(archive['url'],timeout=30);r.raise_for_status()
    assert hashlib.sha256(r.content).hexdigest()==archive['sha256']
    (EVIDENCE/'live-schema-probe.txt').write_text(f"GET {archive['url']}\nHTTP {r.status_code}\nSHA256 {archive['sha256']}\n")


def test_invalid_runtime_xml_is_stored_before_refusal():
    async def go():
        d=copy.deepcopy(SAMPLE);d['id']='invalid-runtime';d['items'][0]['net_kg']=-1
        with pytest.raises(ValueError):await tulli_sandbox.ExportSandboxGateway().submit(d)
        row=await db.customs_messages.find_one({'declaration_id':d['id']},{'_id':0})
        assert row['xml'] and row['validation']['valid'] is False and row['validation']['errors']
    run(go())


def test_unmet_official_licence_requirement_blocks_export():
    async def go():
        d=await make_draft('licences');d['items'][0]['documents']=[]
        d=await exports.apply_patch(d,{'items':d['items']});d=await exports.ready(d)
        d=await exports.file_declaration(d)
        assert d['status']=='ready'
        assert d['controls']['licences']['items'][0]['unmet']==['Y935','E012','Y123']
    run(go())


def test_stale_approval_cannot_file_edited_declaration():
    async def go():
        from core.util import NO_ID
        await update_settings({'autopilot_enabled':False})
        try:
            d=await exports.ready(await make_draft('approval'))
            d=await exports.file_declaration(d)
            a=await db.approvals.find_one({'id':d['approval_id']},NO_ID)
            assert a['data']['fingerprint']
            old_fingerprint=a['data']['fingerprint']
            d=await exports.apply_patch(d,{'invoice':{'currency':'EUR','total':1500}})
            d=await exports.ready(d)
            a['status']='approved'
            d=await exports.file_declaration(d,approval=a)
            assert exports.fingerprint(d)!=old_fingerprint and d['status']=='ready'
            assert not await db.customs_messages.find_one({'declaration_id':d['id']})
        finally:await update_settings({'autopilot_enabled':True})
    run(go())


def test_unchanged_human_approval_files_when_autopilot_is_off():
    async def go():
        from core.util import NO_ID
        await update_settings({'autopilot_enabled':False,'customs_filing_delegated':False})
        try:
            d=await exports.ready(await make_draft('human-approved'))
            d=await exports.file_declaration(d)
            a=await db.approvals.find_one({'id':d['approval_id']},NO_ID)
            assert a['data']['fingerprint']==exports.fingerprint(d)
            a['status']='approved'
            await db.approvals.update_one({'id':a['id']},{'$set':{'status':'approved'}})
            await exports.on_approval({'approval':a,'actor':{'id':'tulli-reviewer'}})
            d=await exports.get(d['id'])
            assert d['status']=='accepted',d.get('controls')
        finally:await update_settings({'autopilot_enabled':True,'customs_filing_delegated':True})
    run(go())
