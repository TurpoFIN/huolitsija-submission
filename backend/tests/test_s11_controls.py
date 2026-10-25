"""Official captured feeds + Mongo + real ASGI routes. Live probe opt-in CONTROLS_LIVE=1."""
import os
import uuid
os.environ['DB_NAME'] = 'huo_s11_controls_' + uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['JWT_SECRET'] = 'controls-isolated-test-secret-long-enough'
import asyncio
import gzip
import hashlib
import json
import time
from pathlib import Path
import httpx
import pytest
from lxml import etree
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run controls tests in their own pytest process', allow_module_level=True)
from core.db import db, client
from services import dual_use as du, customs_screening as cs, screening, taric
from services.customs_screening_lists import parse_csl, parse_uk
FIX = Path(__file__).parent / 'fixtures/controls'
MANIFEST = json.loads((FIX / 'manifest.json').read_text())
EVIDENCE = Path(os.environ.get('S11_EVIDENCE_DIR', '/tmp/huolitsija-s11-controls'))

def run(coro):
    return LOOP.run_until_complete(coro)

def raw(key):
    b = gzip.decompress((FIX / MANIFEST[key]['file']).read_bytes())
    assert hashlib.sha256(b).hexdigest() == MANIFEST[key]['sha256']
    return b

@pytest.fixture(scope='module', autouse=True)
def lists():
    parsed = {'eu': screening.parse_eu(raw('eu')), 'un': screening.parse_un(raw('un')),
              'ofac': screening.parse_ofac(raw('ofac'), raw('ofac_alt'), raw('ofac_add')),
              'uk': parse_uk(raw('uk')), 'csl': parse_csl(raw('csl'))}
    async def load():
        for k, (entries, generated) in parsed.items():
            await screening._store(k, entries, generated, MANIFEST[k]['sha256'], time.monotonic())
    run(load())
    yield parsed
    run(client.drop_database(config.DB_NAME))
    LOOP.close()

@pytest.fixture
def replay():
    p = Path(__file__).parent / 'fixtures/taric'
    m = json.loads((p / 'manifest.json').read_text())
    taric._REPLAY = {url: (p / meta['file']).read_bytes().decode() for url, meta in m['pages'].items()}
    yield
    taric._REPLAY = None

@pytest.mark.parametrize('code,phrase', [('5A002','Information security'),('3A001','Electronic items'),
                                        ('9A012','Unmanned aerial vehicles'),('1C010','Fibrous or filamentary materials')])
def test_entries_against_official_xhtml(code, phrase):
    b = gzip.decompress((du.DATA / 'source/02021R0821-20260921.en.xhtml.gz').read_bytes())
    assert hashlib.sha256(b).hexdigest() == du.data('annex1')['source']['sha256']
    root = etree.fromstring(b)
    text = ' '.join(' '.join(root.itertext()).split())
    e = du.entry(code)
    assert phrase in e['text'] and code in text and phrase in text
    assert e['category'] == code[0] and e['group'] == code[1] and e['parent'] is None
    assert e['descendants'] and e['regimes']
    # An entry's exact text is present in the publisher's document, apart from NBSP normalization.
    assert ' '.join(e['text'].split()) in text

@pytest.mark.parametrize('cn', ['8471','8517','8525','9013','8456'])
def test_cn_candidates(cn):
    candidates = du.candidates_for_cn(cn)
    expected = {'8471': '4A003.b', '8517': '5A002', '8525': '9A012', '9013': '6A005', '8456': '2B001.e'}
    assert expected[cn] in {c['entry'] for c in candidates}
    assert candidates and all(du.entry(c['entry']) and c['footnote'].startswith('DU') for c in candidates)
    assert all(du.entry(c['entry'])['category'] in '0123456789' for c in candidates)

def test_cn_table_reparsed_from_official_xlsx():
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_dualuse_data', Path(__file__).resolve().parents[2] / 'scripts/build_dualuse_data.py')
    # repo root is parents[2]
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    b = (du.DATA / 'source/2026 - Dual use correlation table 1 January 2026.xlsx').read_bytes()
    parsed = mod.parse_correlation(b, set(du._entries()))
    assert parsed['cn'] == du.data('cn_correlation')['cn']

def test_general_authorisation_us_vs_cn():
    us = run(du.assess({'dual_use_code': '5A002'}, flow='export', destination='US', on='2026-10-24'))
    cn = run(du.assess({'dual_use_code': '5A002'}, flow='export', destination='CN', on='2026-10-24'))
    assert us['controlled'] and us['licence_required'] and us['authorisation']['type'] == 'EU001'
    assert us['authorisation']['status'] == 'conditions_require_review'
    assert cn['authorisation']['type'] == 'individual' and cn['licence_required']
    assert us['legal_point'] == cn['legal_point'] == 'dual_use_export'
    assert set(du.data('annex2_eugea')['eugea']) == {'EU00' + str(i) for i in range(1,9)}

def test_annex_iv_transfer_and_catch_all():
    r = run(du.assess({'dual_use_code':'1C001'},flow='transfer',destination='SE',on='2026-10-24'))
    assert r['annex_iv'] and r['licence_required']
    r = run(du.assess({'dual_use_classification':{'controlled':False,'basis':'Technical review','reviewed_by':'broker'},
                       'military_end_use':True},flow='export',destination='RU',on='2026-10-24'))
    assert r['controlled'] is False and r['catch_all'][0]['code'] == 'military_end_use' and r['licence_required']

def test_cn_is_not_positive_or_negative_classification(replay):
    r = run(du.assess({'cn_code':'8471300000'},flow='export',destination='RU',on='2026-10-24'))
    assert r['controlled'] is None and r['candidates'] and r['requires_review']
    assert any(c['certificate'] == 'Y901' for m in r['taric_measures'] for c in m['conditions'])
    assert run(du.assess({'dual_use_code':'9Z999'},flow='export',destination='CN'))['controlled'] is None

def test_goods_russia_real_taric(replay):
    r = run(cs.goods_sanctions({'cn_code':'8471300000'},flow='export',country='RU',on='2026-10-24'))
    assert r['blocked'] and r['legal_point'] == 'ru_by_trade' and r['taric_verified']
    assert any(m['regulation'] == 'R0833/14' for m in r['taric_measures'])
    assert any(c['action_kind'] == 'not_allowed' for m in r['taric_measures'] for c in m['conditions'])
    assert any(x['annex'] == 'XXIII' for x in r['matches'])
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    (EVIDENCE/'ru-export-assessment.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))

def test_goods_high_priority_and_outage(monkeypatch):
    async def offline(*a,**kw):
        raise taric.TaricUnavailable('Network unavailable')
    monkeypatch.setattr(taric,'lookup',offline)
    for country in ['RU','BY','IR','KP','SY']:
        r=run(cs.goods_sanctions({'cn_code':'8471500000'},flow='export',country=country,on='2026-10-24'))
        assert r['requires_review'] and not r['taric_verified'] and r['common_high_priority']
    r=run(cs.goods_sanctions({'cn_code':'8471500000'},flow='export',country='JP',on='2026-10-24'))
    assert not r['blocked'] and r['common_high_priority'][0]['tier']=='3.A'

def test_official_uk_and_csl_parsers(lists):
    assert len(lists['uk'][0]) > 6000 and len(lists['csl'][0]) > 5000
    assert any(e['ref']=='AFG0001' and 'HAJI KHAIRULLAH' in e['primary_name'] for e in lists['uk'][0])
    assert any('Huawei' in e['primary_name'] and any('Entity List' in p for p in e['programmes']) for e in lists['csl'][0])

def test_every_party_nested_and_empty():
    decl={'seller':{'name':'Seller'},'buyer':{'name':'Buyer'},'carrier':{'name':'Carrier'},
          'house_consignments':[{'consignee':{'name':'Receiver'},'parties':{'notify':{'name':'Notify'}},
                                 'items':[{'manufacturer':{'name':'Maker'}}]}], 'representative':{}}
    rows=cs.parties(decl)
    assert {p['name'] for p in rows} == {'Seller','Buyer','Carrier','Receiver','Notify','Maker',None}
    result=run(cs.screen_parties(decl))
    assert result['status']=='error' and not result['complete'] and len(result['parties'])==7

def test_listed_non_seller_goes_to_legal_gate(lists):
    # Literal listed EU entity, independently present in the downloaded XML.
    name='Almaz-Antey'
    assert any(name.lower() in n.lower() for e in lists['eu'][0] for n in e['names'])
    decl={'id':'non-seller-eu','seller':{'name':'North Finnish Test Supplier'},'consignee':{'name':name}}
    r=run(cs.screen_parties(decl,kind='export'))
    assert r['status']=='potential_match' and any(h['role']=='consignee' and h['list']=='eu' for h in r['hits'])
    decision=run(db.agent_decisions.find_one({'id':r['decision_id']}))
    assert decision['legal_point']=='sanctions_potential_match' and decision['outcome'] != 'auto'
    assert decision['approval_id']
    (EVIDENCE/'non-seller-screening.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))

def test_clearance_invalidated_by_identity_change_and_time():
    from core.util import now_iso
    d={'buyer':{'name':'Test Buyer','address':'A'}}
    d['screening']={'status':'clear','complete':True,'screened_at':now_iso(),'party_fingerprint':cs.party_fingerprint(d)}
    assert cs.screening_current(d)
    d['buyer']['address']='B'
    assert not cs.screening_current(d)
    d['screening']['party_fingerprint']=cs.party_fingerprint(d)
    d['screening']['screened_at']='2020-01-01T00:00:00+00:00'
    assert not cs.screening_current(d)

def test_missing_list_cannot_clear():
    """A required (legally binding) list that is not fresh can never clear; a missing advisory list is named, and a
    screen made without it stops counting once it loads again."""
    from core.settings import update_settings
    from core.util import now_iso
    async def screen(missing):
        for k in missing:
            await db.sanctions_lists.update_one({'list':k},{'$set':{'status':'error'}})
        try:
            return await cs.screen_parties({'buyer':{'name':'North Finnish Test Supplier'}})
        finally:
            for k in missing:
                await db.sanctions_lists.update_one({'list':k},{'$set':{'status':'ok','fetched_at':now_iso()}})
    for required in (['eu'], ['un']):
        r=run(screen(required)); assert r['status']=='error' and not r['complete'], required
    r=run(screen(['uk']))  # the FCDO site answers bots with an AWS WAF challenge (HTTP 405) at times
    assert r['status']=='clear' and r['complete'] and r['missing_lists']==['uk'] and r['required_lists']==['eu','un']
    assert 'UK' in r['note']['fi'] and 'UK' in r['note']['en']
    decl={'buyer':{'name':'North Finnish Test Supplier'},'screening':r}
    assert run(cs.validation_rows(decl))[0]['ok']  # K-1: clear on the binding lists
    # The UK list loads again with a version this screen never saw: the clearance no longer counts.
    run(db.sanctions_lists.update_one({'list':'uk'},{'$set':{'version':'uk-new'}}))
    try:
        assert not run(cs.validation_rows(decl))[0]['ok']
    finally:
        run(db.sanctions_lists.update_one({'list':'uk'},{'$set':{'version':next(m['version'] for m in r['lists'] if m['list']=='uk')}}))
    # The owner can make any list binding for filings.
    run(update_settings({'customs_screening_required_lists':['eu','un','uk']}))
    try:
        r=run(screen(['uk'])); assert r['status']=='error' and not r['complete']
    finally:
        run(update_settings({'customs_screening_required_lists':['eu','un']}))
    import pytest as _p
    from services import customs_aeo
    with _p.raises(customs_aeo.ControlRefusal):
        run(customs_aeo.validate_controls_settings({'customs_screening_required_lists':['un']}))
    assert run(customs_aeo.validate_controls_settings({'customs_screening_required_lists':['uk','eu']})) == \
        {'customs_screening_required_lists':['eu','uk']}

def test_daily_job_all_four_collections():
    from core.jobs import registered_jobs, registered_schedules
    async def go():
        for kind,col in cs.COLLECTIONS.items():
            await db[col].insert_one({'id':'daily-'+kind,'status':'draft','buyer':{'name':'Almaz-Antey'}})
            await db[col].insert_one({'id':'closed-'+kind,'status':'closed','buyer':{'name':'Almaz-Antey'}})
        await db.transit_declarations.insert_one({'id':'released-transit','status':'released','carrier':{'name':'Almaz-Antey'}})
        await db.export_declarations.insert_one({'id':'released-export','status':'released','carrier':{'name':'Almaz-Antey'}})
        result=await cs.rescreen_parties({'payload':{}})
        for kind,col in cs.COLLECTIONS.items():
            assert (await db[col].find_one({'id':'daily-'+kind}))['screening']['status']=='potential_match'
            assert 'screening' not in await db[col].find_one({'id':'closed-'+kind})
        return result
    r=run(go()); assert r['screened']==6 and r['potential_match']==6
    assert registered_jobs()['customs.rescreen_parties']['agent']
    assert registered_schedules()['customs.rescreen_parties.daily']['daily_at']=='04:30'

def test_validation_blocks_unscreened_and_goods(replay):
    from services import customs
    d={'type':'export','destination_country':'RU','items':[{'seq':1,'cn_code':'8471300000'}]}
    rows=run(cs.validation_rows(d))
    assert {r['rule'] for r in rows if not r['ok']}=={'K-1','DU-1','SANC-GOODS'}
    # The actual import validator must block an absent all-party result even without seller screening.
    rows=run(customs.validate({'items':[]},{},{},{}))
    assert any(r['rule']=='K-1' and r.get('field')=='screening' and not r['ok'] for r in rows)

def test_approved_declaration_cannot_bypass_fresh_controls():
    from services import customs
    from fastapi import HTTPException
    async def go():
        await db.customs_declarations.insert_one({'id':'stale-approved','status':'approved',
                                                  'buyer':{'name':'Almaz-Antey'},'items':[]})
        # Simulates a caller holding an older approved object: persisted parties win.
        with pytest.raises(HTTPException) as exc:
            await customs.file_declaration({'id':'stale-approved','status':'approved','items':[]},None)
        assert exc.value.status_code==409
        assert any(c['rule']=='K-1' for c in exc.value.detail['checks'])
        assert 'submission' not in await db.customs_declarations.find_one({'id':'stale-approved'})
    run(go())


def test_authenticated_reference_and_screen_routes():
    import server
    from core.security import create_session_token
    async def go():
        await db.users.insert_one({'id':'controls-reviewer','org_type':'internal','role':'ops_admin','session_version':0})
        await db.customs_declarations.insert_one({'id':'http-screen','status':'accepted','consignee':{'name':'Almaz-Antey'}})
        token=create_session_token('controls-reviewer')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://controls') as h:
            assert (await h.get('/api/customs/dual-use/search?q=5A002')).status_code==401
            h.headers['Authorization']='Bearer '+token
            for path in ['/search?q=5A002','/entry/5A002','/assess?code=5A002&destination=US&flow=export']:
                r=await h.get('/api/customs/dual-use'+path);assert r.status_code==200,r.text
            r=await h.post('/api/customs/http-screen/screen');assert r.status_code==200,r.text
            assert r.json()['screening']['status']=='potential_match'
            assert r.json()['screening']['parties'][0]['role']=='consignee'
            return r.json()['screening']
    r=run(go());(EVIDENCE/'authenticated-api-screening.json').write_text(json.dumps(r,indent=2))

@pytest.mark.skipif(os.getenv('CONTROLS_LIVE')!='1',reason='Enable CONTROLS_LIVE=1 for official live feeds')
def test_live_eu_non_seller_and_taric():
    async def go():
        meta=await screening.refresh_lists(force=True)
        assert all(m['loaded'] and m['status']=='ok' for m in meta),meta
        r=await cs.screen_parties({'id':'live-eu','carrier':{'name':'Almaz-Antey'}},kind='transit')
        assert r['status']=='potential_match' and any(h['list']=='eu' and h['role']=='carrier' for h in r['hits'])
        t=await taric.lookup('8471300000',country='RU',flow='export',on='2026-10-24',use_cache=False)
        assert any(m['regulation']=='R0833/14' for m in t['restrictions'])
        return {'lists':meta,'screening':r,'taric':t}
    r=run(go());(EVIDENCE/'live-probe.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
