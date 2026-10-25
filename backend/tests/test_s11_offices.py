"""Real full DG TAXUD COL capture, real Mongo, actual authenticated API.

Run in its own pytest process. Mutated validity/status records are explicit negative
cases derived from the official capture, not fabricated positive office fixtures.
"""
import os
import uuid
os.environ['DB_NAME'] = 'huo_s11_col_' + uuid.uuid4().hex[:12]
os.environ['JWT_SECRET'] = 'col-test-secret-for-local-tests-only'
import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run this module alone', allow_module_level=True)
from core.db import db, client, ensure_indexes
from services import customs_offices as col

FIX = Path(__file__).parent / 'fixtures/offices'
RAW = (FIX / 'COL-Generic.zip').read_bytes()
SOURCE = json.loads((FIX / 'SOURCE.json').read_text())
ROWS = {r['code']: r for r in col.parse_col(RAW)}


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(scope='module', autouse=True)
def database():
    async def setup():
        await ensure_indexes()
        await col.install_snapshot(RAW, SOURCE)
        await db.raw.users.insert_one({'id':'col-reviewer','email':'reviewer@col.test','org_type':'internal','role':'ops_admin','session_version':0})
        await db.raw.users.insert_one({'id':'col-customer','email':'customer@col.test','org_type':'customer','role':'customer_admin','session_version':0})
    try:
        run(setup())
        yield
    finally:
        run(client.drop_database(config.DB_NAME))
        LOOP.close()


@pytest.fixture(autouse=True)
def capture_clock(monkeypatch, request):
    if request.node.name == 'test_live_official_refresh_job':
        return
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(SOURCE['fetched_at']) + timedelta(seconds=1)
    monkeypatch.setattr(col, 'datetime', Clock)


def test_capture_hash_and_complete_office_population():
    assert hashlib.sha256(RAW).hexdigest() == SOURCE['sha256']
    assert SOURCE['source_url'].startswith(col.BASE)
    assert len(ROWS) == SOURCE['office_count'] == 4275
    assert len({r['country'] for r in ROWS.values()}) == 41
    assert run(db.raw.customs_offices.count_documents({})) == 4275
    assert all(r['name'] and r['valid_from'] for r in ROWS.values())


@pytest.mark.parametrize('code,name,loc,role', [
    ('FI009801','Meritulli Vuosaari satama V6','FIHEL','ENT'),
    ('FI015300','Lentotulli Helsinki-Vantaa Rahti','FIVAT','ENT'),
    ('FI002000','Tulliselvityskeskus','FITOR','RFC'),
    ('DE004851','Hamburg','DEHAM','DES'),
])
def test_named_offices_from_real_capture(code, name, loc, role):
    row = run(col.office(code))
    assert row['name'] == name and row['unlocode'] == loc
    assert role in row['roles']
    assert row['address']['street'] and row['address']['postal_code']
    assert row['source_url'] == SOURCE['source_url'] and row['sha256'] == SOURCE['sha256']
    assert run(col.valid(code, role, SOURCE['source_date']))
    assert run(col.verify(code, role))['code'] == code


def test_roles_are_not_interchangeable_and_traffic_is_bound_to_role():
    assert not run(col.valid('FI002000','ENT',SOURCE['source_date']))
    assert not run(col.valid('XX999999','DES',SOURCE['source_date']))
    assert col.eligible(ROWS['FI015300'], 'ENT', SOURCE['source_date'], 'air')
    assert not col.eligible(ROWS['FI015300'], 'DEP', SOURCE['source_date'], 'air')
    assert col.eligible(ROWS['FI015300'], 'DEP', SOURCE['source_date'], 'road')
    assert 'FI015300' in [r['code'] for r in run(col.offices_for('FI','ENT','FIVAT','air'))]
    assert not run(col.offices_for('FI','ENT','FIHEL','air'))
    assert 'DE004851' in [r['code'] for r in run(col.offices_for('DE','DES','DEHAM'))]


def test_office_validity_boundaries_and_open_ended_version():
    row = ROWS['FI009801']
    assert row['valid_to'] is None  # official absence means open-ended, no invented expiry
    assert not col.eligible(row, 'ENT', '2026-03-26')
    assert col.eligible(row, 'ENT', '2026-03-27')
    closed = {**row, 'valid_to':'2026-10-24'}  # negative boundary mutation
    assert col.eligible(closed, 'ENT', '2026-10-24')
    assert not col.eligible(closed, 'ENT', '2026-10-25')
    assert not col.eligible({**row, 'state':'invalid'}, 'ENT', '2026-10-24')
    seasonal = copy.deepcopy(row)
    for c in seasonal['competence']:
        c['valid_to'] = '2026-10-23'
    assert not col.eligible(seasonal, 'ENT', '2026-10-24')


def test_stale_and_untrusted_sources_do_not_verify():
    async def go():
        before = await col.office('FI009801')
        for changes in ({'fetched_at':'2020-01-01T00:00:00+00:00'},
                        {'source_date':'2020-01-01'}, {'source_url':'https://example.com/COL'},
                        {'sha256':None}):
            try:
                await db.raw.customs_offices.update_one({'code':'FI009801'},{'$set':changes})
                with pytest.raises(col.OfficeDataUnavailable):
                    await col.verify('FI009801','ENT')
            finally:
                await db.raw.customs_offices.replace_one({'code':'FI009801'},before)
    run(go())


def test_coordinates_unavailable_is_explicit_not_an_invented_nearest():
    assert not any('lat' in r or 'lon' in r for r in ROWS.values())
    assert ROWS['FI009801']['source_fields']['GeoInfoCode'] == 'CB'
    with pytest.raises(col.OfficeDataUnavailable, match='coordinates'):
        run(col.nearest('ENT',60.2,25.1,'FI'))
    with pytest.raises(ValueError):
        run(col.nearest('ENT',91,25))


def test_failed_refresh_preserves_last_complete_snapshot():
    async def go():
        before = await col.office('FI009801')
        with pytest.raises(ValueError):
            await col.install_snapshot(b'<html>error</html>', SOURCE)
        assert await col.office('FI009801') == before
        assert await db.raw.customs_offices.count_documents({}) == 4275
    run(go())


def test_downloader_retries_502_with_backoff_then_parses_official_bytes(monkeypatch):
    attempts, delays = [], []
    async def handle(request):
        attempts.append(str(request.url))
        return httpx.Response(502, text='Service unavailable') if len(attempts) < 3 else httpx.Response(200, content=RAW)
    async def sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr(col.asyncio, 'sleep', sleep)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            raw, rows, source = await col.download(SOURCE['source_date'], client=c)
        assert raw == RAW and len(rows) == 4275
        assert source['sha256'] == SOURCE['sha256']
    run(go())
    assert len(attempts) == 3 and delays == [1,2]


def test_unpublished_day_falls_back_to_newest_earlier_official_file(monkeypatch):
    # Observed 2026-10-25 01:43 UTC in production: COL-Generic-<today>.zip answered 200 with 0 bytes until the
    # Commission published it, and every refresh failed with "Document is empty".
    asked = []
    day = SOURCE['source_date']
    nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    async def handle(request):
        asked.append(request.url.path.rsplit('-', 1)[-1])
        return httpx.Response(200, content=RAW if request.url.path.endswith(day.replace('-', '') + '.zip') else b'')
    async def sleep(seconds):
        raise AssertionError('an unpublished day is not retried')
    monkeypatch.setattr(col.asyncio, 'sleep', sleep)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            raw, rows, source = await col.download(nxt, client=c)
            assert raw == RAW and len(rows) == 4275 and source['source_date'] == day
            assert source['sha256'] == SOURCE['sha256']
            with pytest.raises(col.OfficeDataUnavailable, match='not published'):
                await col.download((datetime.fromisoformat(day) + timedelta(days=5)).date().isoformat(), client=c)
    run(go())
    assert asked[:2] == [nxt.replace('-', '') + '.zip', day.replace('-', '') + '.zip'] and len(asked) == 5


def test_full_capture_parse_stays_within_container_memory():
    """Live 2026-10-25: a whole-tree parse of the 60 MB COL XML took ~620 MB and the refresh crash-looped the backend."""
    import subprocess, sys
    # VmHWM, not ru_maxrss: a child's ru_maxrss starts at the (large) pytest parent's high-water mark.
    probe = ("import re,sys;from services import customs_offices as col;raw=open(sys.argv[1],'rb').read();"
             "hwm=lambda:int(re.search(r'VmHWM:\\s+(\\d+)',open('/proc/self/status').read()).group(1));"
             "b=hwm();n=len(col.parse_col(raw));print(n,(hwm()-b)//1024,hwm()//1024)")
    out = subprocess.run([sys.executable, '-c', probe, str(FIX / 'COL-Generic.zip')], capture_output=True, text=True,
                         cwd=Path(__file__).parents[1], timeout=300, check=True).stdout.split()
    assert int(out[0]) == 4275
    assert int(out[1]) < 200, f'COL parse grew RSS by {out[1]} MB'
    # whole-process peak (interpreter + lxml + the 4275 rows): the old whole-tree parse peaked at ~670 MB
    assert int(out[2]) < 150, f'COL parse peaked at {out[2]} MB RSS'


def test_streaming_parse_still_rejects_doctype_and_non_col_xml():
    xml = (b'<?xml version="1.0"?><!DOCTYPE RDEntityList [<!ENTITY x "y">]>'
           b'<RDEntityList><RDEntity name="CustomsOffices"/></RDEntityList>')
    with pytest.raises(ValueError, match='Expected CS/RD2 COL RDEntityList'):
        col.parse_col(xml)
    with pytest.raises(ValueError, match='Expected CS/RD2 COL RDEntityList'):
        col.parse_col(b'<html><body>error</body></html>')
    with pytest.raises(ValueError, match='no CustomsOffices'):
        col.parse_col(b'<RDEntityList><RDEntity name="CountryCodesFullList"/></RDEntityList>')


def test_broker_uses_unique_col_and_exposes_ambiguity_and_offline_fallback():
    from services.agents.customs_agent import broker_patch
    async def go():
        # A unique official ENT/rail location, selected from the actual full capture.
        by_location = {}
        for r in ROWS.values():
            if r['unlocode'] and col.eligible(r, 'ENT', SOURCE['source_date'], 'rail'):
                by_location.setdefault(r['unlocode'],[]).append(r)
        loc, candidates = next((k,v) for k,v in by_location.items() if len(v)==1)
        patch, notes = await broker_patch({}, {'mode':'rail','destination':{'unlocode':loc}}, .9)
        assert patch['office']['code'] == candidates[0]['code']
        assert patch['office']['source'] == 'EU COL' and patch['office']['sha256'] == SOURCE['sha256']
        patch, notes = await broker_patch({}, {'mode':'sea','destination':{'unlocode':'FIHEL'}}, .9)
        assert 'office' not in patch
        assert 'FI009801' in notes[0]['candidates'] and len(notes[0]['candidates']) > 1
        patch, notes = await broker_patch({}, {'mode':'air','destination':{'unlocode':'FIHEL'}}, .9)
        assert 'office' not in patch and not notes[0]['candidates']  # no FIVAT -> FIHEL alias invented
        current = await db.raw.customs_offices.find_one({})
        try:
            await db.raw.customs_offices.update_one({'_id':current['_id']},{'$set':{'fetched_at':'2000-01-01T00:00:00+00:00'}})
            patch, notes = await broker_patch({}, {'mode':'sea','destination':{'unlocode':'FIHEL'}}, .9)
            assert patch['office']['code']=='FI009801' and patch['office']['source']=='offline_fallback'
            assert patch['office']['verified'] is False
        finally:
            await db.raw.customs_offices.replace_one({'_id':current['_id']},current)
    run(go())


def test_export_transit_and_ens_role_rules():
    async def go():
        assert not await col.validation_reasons({'office':{'code':'FI002000'},'exit_office':{'code':'FI009801'}},'export')
        assert not await col.validation_reasons({'office_of_departure':'FI009801',
            'office_of_destination':'DE004851','offices_of_transit':['FI015300']},'transit')
        assert not await col.validation_reasons({'mode':'air','ens_data':{'customsOfficeOfFirstEntry':{'referenceNumber':'FI015300'}}},'ens')
        assert not await col.validation_reasons({'mode':'sea','ens_data':{}},'ens')  # house F15 has no office
        reasons = await col.validation_reasons({'mode':'road','ens_data':{'customsOfficeOfFirstEntry':{'referenceNumber':'FI002000'}}},'ens')
        assert [r['code'] for r in reasons] == ['COL-ENT']
        assert await col.validation_reasons({'mode':'road','ens_data':{}},'ens')
        assert await col.validation_reasons({'office':{'code':'DE004851'},'exit_office':{'code':'FI009801'}},'export')  # no EXP in Hamburg record
        with pytest.raises(ValueError):
            await col.enforce({'office_of_departure':'FI009801','office_of_destination':'XX999999'},'transit')
    run(go())


def test_actual_export_and_ens_filing_boundaries(monkeypatch):
    from services import customs_export, customs_ens, customs_transit
    monkeypatch.setenv('ICS2_MODE','production')
    monkeypatch.setenv('ICS2_EORI','FI39900099')
    async def go():
        sample = json.loads((Path(__file__).parent/'fixtures/tulli/export-declaration.json').read_text())
        sample['office']={'code':'DE004851'}
        check = await customs_export.validation_with_offices(sample)
        assert not check['valid'] and any(r['code']=='COL-EXP' for r in check['reasons'])
        with pytest.raises(ValueError):
            await customs_transit.verify_offices({'office_of_departure':'FI009801','office_of_destination':'XX999999'})
        import importlib.util
        path = Path(__file__).parent/'fixtures/ncts_ics2/samples.py'
        spec = importlib.util.spec_from_file_location('col_ens_samples',path)
        samples = importlib.util.module_from_spec(spec);spec.loader.exec_module(samples)
        d = samples.ens('road')
        d.update(id='col-invalid-ens',status='draft',company_id='col-company')
        d['ens_data']['customsOfficeOfFirstEntry']['referenceNumber']='FI002000'
        await db.raw.ens_filings.insert_one(d)
        with pytest.raises(ValueError,match='ENT'):
            await customs_ens.file_filing(d['id'])
        assert not await db.raw.customs_messages.find_one({'declaration_id':d['id']})
        assert (await db.raw.ens_filings.find_one({'id':d['id']}))['status']=='draft'
    run(go())


def test_authenticated_real_api_routes_and_persisted_data():
    from server import app
    from core.security import create_session_token
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://col.test') as c:
            assert (await c.get('/api/customs/offices')).status_code == 401
            c.headers['Authorization']='Bearer '+create_session_token('col-customer')
            assert (await c.get('/api/customs/offices')).status_code == 403
            assert (await c.post('/api/customs/offices/refresh')).status_code == 403
            c.headers['Authorization']='Bearer '+create_session_token('col-reviewer')
            r=await c.get('/api/customs/offices',params={'country':'FI','role':'ENT','q':'Vuosaari'})
            assert r.status_code==200,r.text
            response=r.json()
            assert response['status']=='current'
            assert [o['code'] for o in response['items']]==['FI009801']
            r=await c.get('/api/customs/offices/FI015300')
            assert r.status_code==200 and r.json()['unlocode']=='FIVAT'
            assert r.json()['address']['street']=='Rahtitie 1 A'
            assert (await c.get('/api/customs/offices/XX999999')).status_code==404
            r=await c.post('/api/customs/offices/refresh')  # ops_admin holds settings.write
            assert r.status_code==200 and r.json()['status']=='queued',r.text
            queued=await db.raw.jobs.find_one({'id':r.json()['job_id']},{'_id':0})
            assert queued['kind']=='customs.offices_refresh' and queued['source']=='ops'
            await db.raw.jobs.delete_one({'id':queued['id']})
            evidence=os.getenv('S11_COL_EVIDENCE')
            if evidence:
                Path(evidence,'authenticated-api.json').write_text(json.dumps({'list':response,'detail':r.json()},indent=2,ensure_ascii=False))
    run(go())


def test_actual_serialized_transit_offices_cannot_bypass_col(monkeypatch):
    import importlib.util
    from services.customs_messages import ncts, ncts_fi
    path = Path(__file__).parent/'fixtures/ncts_ics2/samples.py'
    spec = importlib.util.spec_from_file_location('col_transit_samples',path)
    samples = importlib.util.module_from_spec(spec);spec.loader.exec_module(samples)
    for key, value in {'TULLI_DECLARANT_ID':'FI39900099','TULLI_HOLDER_NAME':'Sandbox Logistics Oy',
                       'TULLI_GUARANTEE_GRN':'26FI0000000000000','TULLI_GUARANTEE_ACCESS_CODE':'TEST'}.items():
        monkeypatch.setenv(key, value)
    async def go():
        payload = ncts.declaration_data(samples.transit())
        wire = ncts.build('CC015C', payload)
        await col.verify_transit_xml(wire)
        payload['CustomsOfficeOfTransitDeclared'] = [{'sequenceNumber':1,'referenceNumber':'FI002000'}]
        wire = ncts.build('CC015C', payload)  # XSD-valid, but this office has no TRA role
        with pytest.raises(ValueError, match='TRA'):
            await col.verify_transit_xml(wire)
        national = samples.national()
        await col.verify_transit_xml(ncts_fi.build('FI015A',national))
        national['DepartureOffice']['Identification'] = 'XX999999'
        with pytest.raises(ValueError):
            await col.verify_transit_xml(ncts_fi.build('FI015A',national))
    run(go())


@pytest.mark.skipif(os.getenv('S11_COL_LIVE')!='1',reason='Set S11_COL_LIVE=1 for official refresh job probe')
def test_live_official_refresh_job():
    from core.jobs import enqueue, work_once, registered_schedules
    async def go():
        j=await enqueue('customs.offices_refresh',{})
        assert await work_once('col-live',max_jobs=1,kinds=['customs.offices_refresh'])==1
        saved=await db.raw.jobs.find_one({'id':j['id']},{'_id':0})
        assert saved['status']=='done',saved
        assert saved['result']['office_count']>4000
        assert (await col.verify('FI009801','ENT'))['unlocode']=='FIHEL'
        assert (await col.verify('FI015300','ENT'))['unlocode']=='FIVAT'
        assert (await col.verify('FI002000','RFC'))['unlocode']=='FITOR'
        assert (await col.verify('DE004851','DES'))['country']=='DE'
        assert registered_schedules()['customs.offices_refresh']['every_seconds']==86400
        evidence=os.getenv('S11_COL_EVIDENCE')
        if evidence:
            Path(evidence,'live-job.json').write_text(json.dumps(saved,indent=2,default=str))
    run(go())


REFRESH_PROBE = r"""
import asyncio, os, re, sys, time, uuid
os.environ["DB_NAME"] = "huo_col_probe_" + uuid.uuid4().hex[:8]
sys.path.insert(0, os.getcwd())
hwm = lambda: int(re.search(r"VmHWM:\s+(\d+)", open("/proc/self/status").read()).group(1)) // 1024
RAW = open(sys.argv[1], "rb").read()
LOOP = asyncio.new_event_loop(); asyncio.set_event_loop(LOOP)
import httpx
from core.db import client
from services import customs_offices as col
import services.agents.base  # noqa: F401  (the running server has these loaded already)
real = httpx.AsyncClient
async def handle(request):
    return httpx.Response(200, content=RAW)
httpx.AsyncClient = lambda *a, **k: real(transport=httpx.MockTransport(handle), follow_redirects=True)
async def main():
    await col.db.raw.agent_locks.find_one({})
    async with real(transport=httpx.MockTransport(handle)) as c:
        await c.get("https://ec.europa.eu/warm-up")
    base = hwm()
    try:
        out = await col.refresh()
        print(out["office_count"], await col.db.raw.customs_offices.count_documents({}), hwm() - base, hwm())
    finally:
        await client.drop_database(os.environ["DB_NAME"])
LOOP.run_until_complete(main())
"""


def test_whole_refresh_download_parse_and_install_stays_flat_in_memory():
    """Live 2026-10-25 03:59: the pod restarted 76 s into a refresh even after the streaming parse (2ba607c), which
    still held the download, all 4275 rows and a copy of each insert batch (+87 MB on top of a ~271 MB server).
    Measured end to end in a warm process: download -> temp file -> incremental parse -> 100-row inserts."""
    import subprocess, sys
    out = subprocess.run([sys.executable, '-c', REFRESH_PROBE, str(FIX / 'COL-Generic.zip')], capture_output=True,
                         text=True, cwd=Path(__file__).parents[1], timeout=300, env=os.environ.copy())
    assert out.returncode == 0, out.stderr[-2000:]
    installed, in_db, grew, peak = (int(x) for x in out.stdout.split())
    assert installed == in_db == 4275
    assert grew < 40, f'refresh() grew RSS by {grew} MB'
    assert peak < 150, f'refresh() peaked at {peak} MB RSS'


def test_refresh_streams_the_download_through_a_temp_file_and_parses_once(monkeypatch):
    """refresh() never builds the full row list (parse_col) and never keeps the download in memory or on disk."""
    import tempfile
    parses, files = [], []
    real_iter, real_download = col.iter_col, col.download_to_file
    def counting_iter(source):
        parses.append(source)
        return real_iter(source)
    def no_list(raw):
        raise AssertionError('refresh() must not build the whole row list')
    async def handle(request):
        return httpx.Response(200, content=RAW)
    monkeypatch.setattr(col, 'iter_col', counting_iter)
    monkeypatch.setattr(col, 'parse_col', no_list)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            async def download(*a, **k):
                path, prov = await real_download(*a, client=c, **k)
                files.append(path)
                return path, prov
            monkeypatch.setattr(col, 'download_to_file', download)
            try:
                out = await col.refresh()
                assert out['office_count'] == 4275 and out['sha256'] == SOURCE['sha256']
                assert parses == files and len(files) == 1 and not os.path.exists(files[0])
                assert files[0].startswith(tempfile.gettempdir())
                assert await db.raw.customs_offices.count_documents({'sha256': SOURCE['sha256'], 'office_count': 4275}) == 4275
                assert ROWS['FI009801']['name'] == (await col.office('FI009801'))['name']
            finally:
                await col.install_snapshot(RAW, SOURCE)  # back to the captured provenance
    run(go())


def test_failed_install_keeps_the_old_list_and_removes_the_temp_file(monkeypatch):
    import tempfile
    files = []
    leftovers = lambda: {p for p in os.listdir(tempfile.gettempdir()) if p.startswith('col-') and p.endswith('.download')}
    already = leftovers()
    real_download = col.download_to_file
    broken = RAW[:len(RAW) // 2]  # truncated ZIP: passes nothing past the shape check
    async def handle(request):
        return httpx.Response(200, content=broken)
    async def sleep(seconds):
        return None
    monkeypatch.setattr(col.asyncio, 'sleep', sleep)
    async def go():
        before = await db.raw.customs_offices.count_documents({})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            async def download(*a, **k):
                try:
                    path, prov = await real_download(*a, client=c, **k)
                except Exception:
                    files.extend(leftovers() - already)
                    raise
                files.append(path)
                return path, prov
            monkeypatch.setattr(col, 'download_to_file', download)
            with pytest.raises(col.OfficeDataUnavailable):
                await col.refresh()
        assert files == [] and await db.raw.customs_offices.count_documents({}) == before == 4275
    run(go())
