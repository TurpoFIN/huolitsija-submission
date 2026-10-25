"""Real official HTTP responses replayed through production parsers. Declaration inputs below are
explicit test cases, not purported real certificates or manufacturing records. Live tests opt-in.
"""
import os,uuid,asyncio,json,hashlib,gzip
from pathlib import Path
os.environ['DB_NAME']='huo_s11_origin_'+uuid.uuid4().hex[:8]
LOOP=asyncio.new_event_loop();asyncio.set_event_loop(LOOP)
import pytest,httpx
from services import customs_origin as co,taric
from core.db import db,client
FIX=Path(__file__).parent/'fixtures/origin'
MAN=json.loads((FIX/'manifest.json').read_text())
DATA=co.DATA_DIR
SOURCES=json.loads((DATA/'manifest.json').read_text())
def run(c):return LOOP.run_until_complete(c)
def content(meta,base=FIX):
 b=(base/meta['file']).read_bytes();assert hashlib.sha256(b).hexdigest()==meta['sha256'];return b.decode()
@pytest.fixture(scope='module',autouse=True)
def cleanup():
 yield
 run(client.drop_database(os.environ['DB_NAME']));client.close();LOOP.close()
@pytest.fixture
def replay(monkeypatch):
 run(db.origin_cache.delete_many({}));run(db.taric_cache.delete_many({}))
 taric._REPLAY={u:content(m) for u,m in MAN['pages'].items()}
 pages={u:content(m) for u,m in MAN['http'].items()}
 for name,meta in SOURCES.items():
  if name.startswith('variants_') or name.startswith(('rex_CH','rex_INREX')):
   pages[meta['url']]=gzip.decompress((DATA/meta['file']).read_bytes()).decode()
 async def get(url,params=None):
  req=httpx.Request('GET',url,params=params)
  assert str(req.url) in pages,f'Uncaptured official request: {req.url}'
  return httpx.Response(200,text=pages[str(req.url)],request=req)
 monkeypatch.setattr(co,'_http_get',get)
 yield
 taric._REPLAY=None

def item(code='8708999790',value=100000):
 return {'seq':1,'taric_code':code,'cn_code':code[:8],'hs_code':code[:6],'value_cents':value,'customs_value_cents':value,'net_kg':100}
def certificate(country,typ='eur1'):
 return {'type':typ,'ref':'TEST-DECLARATION-RECORD','date':'2026-10-01','origin_country':country,'verified':True}
def statement(country,arr):
 return {'type':'statement_on_origin','ref':'TEST-INVOICE','date':'2026-10-01','origin_country':country,'verified':True,
         'text':co.origin_statement_text(arr,'',country,'en')['text']}
CASES=[('JP','8708999790','JP',3.5,0),('VN','6403999890','VN',7,.8),('KR','8708999790','KR',3.5,0),
       ('CA','8708999790','CA',3.5,0),('GB','8708999790','GB',3.5,0),('CH','8708999790','PEM2021',3.5,0),
       ('NO','8708999790','EEA2022',3.5,0),('TR','8708999790','CU_TR',3.5,0),
       ('IN','6403919800','GSPB',5,1.5),('BD','6203423100','GSPA',12,0)]
@pytest.mark.parametrize('country,code,arr,mfn,pref',CASES)
def test_real_tariffs_without_proof_retain_100(replay,country,code,arr,mfn,pref):
 r=run(co.assess(item(code),origin=country,on='2026-10-24'))
 assert r['arrangement']['code']==arr
 assert r['third_country_duty']['pct']==mfn
 assert r['preferential_duty']['pct']==pref
 assert r['preference_code']=='100' and r['proof']['valid'] is False
 assert any(x['code']=='proof_missing' for x in r['reasons'])
 assert all(x['fi'] and x['en'] for x in r['reasons'])
@pytest.mark.parametrize('country,code,arr,mfn,pref',CASES)
def test_claim_requires_reviewed_proof(replay,country,code,arr,mfn,pref):
 docs=[certificate(country,'atr' if country=='TR' else 'eur1')] if country in ('VN','CH','NO','TR') else [statement(country,arr)]
 if country=='GB':docs[0]['exporter_ref']='GB123456789000'
 r=run(co.assess(item(code),origin=country,documents=docs,on='2026-10-24'))
 assert r['preference_code']==('400' if country=='TR' else '200' if country in ('IN','BD') else '300'),r
 assert r['saving_cents']==round((mfn-pref)*1000)
 assert r['proof']['document_code']
 assert r['psr']['met'] is True and r['psr']['basis'] in ('proof_of_origin','customs_union')
 if country!='TR':assert r['psr']['rule_text'] and r['psr']['structure']

def test_china_has_no_arrangement(replay):
 r=run(co.assess(item(),origin='CN',on='2026-10-24'))
 assert r['preference_code']=='100' and r['arrangement'] is None
 assert r['third_country_duty']['pct']==3.5

def test_rex_public_valid_and_revoked(replay):
 # Both published by Swiss customs, cited in data/origin/manifest.json: rex_public_list.
 assert run(co.rex_check('CHREX000018'))['valid'] is True
 assert run(co.rex_check('CHREX000007'))['valid'] is False
 assert co.parse_rex('<html>HTTP 200</html>','CHREX000018')['valid'] is None

def test_gsp_threshold_and_real_document_codes(replay):
 p=statement('IN','GSPB')
 for value,expected in [(600000,'200'),(600001,'100')]:
  r=run(co.assess(item('6403919800',value),origin='IN',documents=[p],on='2026-10-24'))
  assert r['preference_code']==expected
  assert r['proof']['document_code']=='U166'
 p['exporter_ref']='CHREX000018'
 r=run(co.assess(item('6403919800',600001),origin='IN',documents=[p],on='2026-10-24'))
 assert r['preference_code']=='100' and r['proof']['document_code']=='U165'
 assert any(x['code']=='rex_wrong_country' for x in r['reasons'])

@pytest.mark.parametrize('change',[{'verified':False},{'date':'2025-01-01'},{'date':'2026-11-01'},{'origin_country':'CN'},{'ref':''},{'text':''}])
def test_invalid_statements_never_claim(replay,change):
 p=statement('JP','JP');p.update(change)
 r=run(co.assess(item(),origin='JP',documents=[p],on='2026-10-24'))
 assert r['preference_code']=='100' and r['proof']['valid'] is False

def test_japan_psr_bom_and_unknown_inputs(replay):
 p=run(co.product_specific_rule('870899','JP'))
 assert p['found'] and 'MaxNOM' in str(p['blocks'])
 assert co.evaluate_psr(p,'870899',{'materials':[]})['met'] is None
 good={'complete':True,'ex_works_cents':100000,'fob_cents':100000,'materials':[{'hs':'730890','value_cents':20000,'originating':False}]}
 assert co.evaluate_psr(p,'870899',good)['met'] is True
 bad={**good,'materials':[{'hs':'870899','value_cents':90000,'originating':False}]}
 assert co.evaluate_psr(p,'870899',bad)['met'] is False
 pdoc=statement('JP','JP')
 r=run(co.assess({**item(),'bom':bad},origin='JP',documents=[pdoc],on='2026-10-24'))
 assert r['preference_code']=='100' and r['psr']['met'] is False

def test_importers_knowledge_needs_actual_manufacturing_evidence(replay):
 r=run(co.assess(item(),origin='JP',documents=[{'type':'importers_knowledge','ref':'test'}],on='2026-10-24'))
 assert r['preference_code']=='100' and r['psr']['met'] is None

def test_real_quota(replay):
 r=run(co.assess(item('1604142800'),origin='VN',documents=[certificate('VN')],on='2026-10-24'))
 assert r['quota']['order_number']=='098204'
 assert r['quota']['status'] in ('open','critical','exhausted','blocked','suspended')
 assert r['preference_code']==('320' if r['quota']['status'] in ('open','critical') else '100'),r

def test_validation_does_not_accept_candidate_code():
 d={'items':[{'seq':1,'preference':'300','origin_assessment':{'preference_code':'100','available_preference_code':'300','proof':{'valid':True},'psr':{'met':True}}}]}
 assert co.validations(d)[0]['ok'] is False

def test_official_statements_and_eur1():
 for arr in ('JP','VN','KR','CA','GB','PEM2021','EEA2022'):
  for lang in ('en','fi'):
   s=co.origin_statement_text(arr,'FI-EXPORTER','EU',lang)
   assert 'FI-EXPORTER' in s['text'] and 'EU' in s['text'] and s['draft']
 with pytest.raises(KeyError):co.origin_statement_text('GSPB','','EU','fi')
 f=co.eur1_field_data(exporter={'name':'Test exporter'},consignee={'name':'Test consignee'},origin_country='FI',destination='VN',items=[{'description':'Test goods','gross_kg':5}],invoice_ref='TEST-INVOICE')
 assert f['fields']['9_gross_kg']==5 and f['issued'] is False

def test_catalogue_and_provenance():
 d=co.list_arrangements();assert len(d['arrangements'])>=26
 assert {x['code'] for x in d['arrangements']} >= {'JP','VN','CU_TR','CU_AD','CU_SM','GSPA','GSPB'}
 assert co.preference_label('320')['fi'] and co.preference_suffix(quota=True)=='20'
 for name,meta in SOURCES.items():
  b=gzip.decompress((DATA/meta['file']).read_bytes());assert hashlib.sha256(b).hexdigest()==meta['sha256'],name

@pytest.mark.skipif(os.environ.get('ORIGIN_LIVE')!='1',reason='set ORIGIN_LIVE=1 to probe official live services')
def test_live_official_services():
 run(db.origin_cache.delete_many({}));run(db.taric_cache.delete_many({}))
 r=run(co.assess(item(),origin='JP',on='2026-10-24'))
 assert r['third_country_duty']['pct']==3.5 and r['preferential_duty']['pct']==0
 assert r['psr']['rule_text'] and r['preference_code']=='100'
 assert run(co.rex_check('CHREX000018'))['valid'] is True
 print(json.dumps({'live_assessment':r,'rex':run(co.rex_check('CHREX000018'))},ensure_ascii=False))

def test_real_indian_rex_over_threshold(replay):
 # Public number: https://thehandicraftscenter.com/page/About-Us . This checks registry logic;
 # it does not assert that this exporter issued the test invoice or manufactured these goods.
 p=statement('IN','GSPB');p['exporter_ref']='INREX2916500928DG005'
 r=run(co.assess(item('6403919800',1000000),origin='IN',documents=[p],on='2026-10-24'))
 assert r['preference_code']=='200' and r['proof']['document_code']=='U165'
 assert r['proof']['exporter_check']['valid'] is True

def test_recompute_uses_new_values_and_serializes_proof(replay):
 from services import customs
 d={'id':'test-recompute','status':'draft','items':[{**item('6403919800'), 'origin_country':'IN','invoice_amount':6000,
     'gross_kg':100,'packages':1,'origin_proofs':[statement('IN','GSPB')]}],
    'valuation':{'exchange_rate':{'rate':1},'invoice_total':6000,'freight_to_border_cents':0}}
 run(customs.recompute(d,company={'id':'test','vat_registered':True},shipment={'id':'test'},settings={'test':True},on='2026-10-24'))
 it=d['items'][0]
 assert it['value_cents']==600000 and it['preference']=='200' and it['duty_cents']==9000,d
 assert it['origin_proof_document']['code']=='U166'
 assert any(d['code']=='U166' and d['reference']=='TEST-INVOICE' for d in it['documents'])
 assert next(v for v in d['validations'] if v['rule']=='ORIGIN-PREF')['ok'] is True
 d['items'][0]['invoice_amount']=6001;d['valuation']['invoice_total']=6001
 run(customs.recompute(d,company={'id':'test','vat_registered':True},shipment={'id':'test'},settings={'test':True},on='2026-10-24'))
 assert it['value_cents']==600100 and it['preference']=='100' and it['duty_cents']==30005
 assert not any(d.get('source')=='origin_assessment' for d in it['documents'])
 run(db.customs_declarations.replace_one({'id':d['id']},d,upsert=True))
 stored=run(db.customs_declarations.find_one({'id':d['id']},{'_id':0}))
 assert stored['items'][0]['preference']=='100' and stored['items'][0]['origin_assessment']['proof']['valid'] is False

def test_authenticated_origin_routes(replay):
 from server import app
 from core.security import load_secret,create_session_token
 async def probe():
  await load_secret(db)
  await db.users.insert_one({'id':'origin-route-broker','role':'customs_broker','org_type':'internal'})
  token=create_session_token('origin-route-broker')
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://origin-test') as c:
   assert (await c.get('/api/customs/origin/arrangements')).status_code==401
   c.headers['Authorization']='Bearer '+token
   rows=(await c.get('/api/customs/origin/arrangements')).json()
   assert len(rows['arrangements'])>=26
   r=await c.get('/api/customs/origin/assess',params={'code':'8708999790','origin':'JP','date':'2026-10-24','value_eur':1000})
   assert r.status_code==200,r.text
   body=r.json();assert body['preference_code']=='100' and body['saving_cents']==3500 and body['psr']['rule_text']
   r=await c.get('/api/customs/origin/rex/CHREX000018');assert r.status_code==200 and r.json()['valid'] is True
   r=await c.get('/api/customs/origin/statement',params={'arrangement':'JP','exporter_id':'TEST','origin_country':'EU','lang':'fi'})
   assert r.status_code==200 and 'TEST' in r.json()['text']
   return body
 body=run(probe());print('Authenticated API observed',json.dumps({'arrangement':body['arrangement'],'preference_code':body['preference_code'],'saving_cents':body['saving_cents']},ensure_ascii=False))

def test_open_quota_selects_320_and_exact_order(replay):
 r=run(co.assess(item('1108140000'),origin='VN',documents=[certificate('VN')],on='2026-10-24'))
 assert r['quota']['order_number']=='098203' and r['quota']['status']=='open',r
 assert r['preference_code']=='320' and r['saving_cents']==1660,r
 it={**item('1108140000'),'preference':'320','origin_country':'VN','origin_assessment':r,'quota_order_number':'098203'}
 duty=run(taric.duty_for_item(it,origin='VN',preference='320',on='2026-10-24'))
 assert duty['order_number']=='098203' and duty['duty_cents']==0

def test_gsp_plus_is_official_geo_2027(replay):
 r=run(co.assess(item('6403919800'),origin='PK',on='2026-10-24'))
 assert r['arrangement']['type']=='gsp_plus' and r['available_preference_code']=='200'
 assert r['preferential_duty']['geo_code']=='2027' and r['preferential_duty']['pct']==0

def test_second_valid_proof_can_be_selected(replay):
 first=statement('JP','JP');first['date']='2024-01-01'
 r=run(co.assess(item(),origin='JP',documents=[first,statement('JP','JP')],on='2026-10-24'))
 assert r['preference_code']=='300'

def test_unknown_quota_cannot_claim(replay,monkeypatch):
 async def unavailable(number,on):return {'order_number':number,'status':'unknown','error':'controlled unavailable-service test'}
 monkeypatch.setattr(co,'quota_status',unavailable)
 r=run(co.assess(item('1108140000'),origin='VN',documents=[certificate('VN')],on='2026-10-24'))
 assert r['preference_code']=='100' and any(x['code']=='quota_unknown' for x in r['reasons'])
