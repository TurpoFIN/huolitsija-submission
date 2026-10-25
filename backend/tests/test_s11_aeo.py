"""AEO controls through the real ASGI API, MongoDB, approval events and s2 SandboxGateway.

Every company/EORI is an explicitly labelled s2 sandbox test tenant. No registry,
permission, control, recomputation, approval handler or gateway is mocked in the
end-to-end tests. Registry error unit tests inject only the network boundary.
Run separately from other modules that choose their own DB_NAME/event loop.
"""
import asyncio
import csv
import gzip
import hashlib
import json
import time
import io
import os
import uuid
from datetime import timedelta
from pathlib import Path

os.environ['DB_NAME'] = 'huo_s11_aeo_' + uuid.uuid4().hex[:10]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['JWT_SECRET'] = 'aeo-disposable-test-secret-not-a-production-key'
os.environ['TULLI_MODE'] = 'sandbox'
os.environ['JOBS_WORKER_ENABLED'] = 'false'
os.environ.pop('OWNER_ALERT_EMAIL', None)

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import httpx
import pytest
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('run test_s11_aeo.py separately', allow_module_level=True)
from core.db import db, client
from core.util import now_iso, now, iso, NO_ID
from core.settings import update_settings, DEFAULTS
from core.security import create_session_token
from core.sandbox import sandbox_eori
from core.agent_runtime import agent_bot
from services import customs, customs_aeo as aeo
from services.agents.customs_agent import gate_filing
from server import app

EVIDENCE = Path(os.environ.get('AEO_EVIDENCE_DIR', '/tmp/aeo/evidence'))
OWNER = {'id': 'owner', 'name': 'Owner', 'email': 'owner@aeo.example', 'role': 'ops_admin', 'org_type': 'internal'}
BROKER = {'id': 'broker', 'name': 'Colleague', 'email': 'broker@aeo.example', 'role': 'customs_broker', 'org_type': 'internal'}
OPS = {'id': 'ops', 'name': 'Operator', 'email': 'ops@aeo.example', 'role': 'ops_agent', 'org_type': 'internal'}
BOT = agent_bot('customs_agent')


def run(coro):
    return LOOP.run_until_complete(coro)


def setup_module():
    async def go():
        assert db.raw.name.startswith('huo_s11_aeo_')
        # Replay official captured feed bytes through the real parsers/store. Live UK
        # downloads intermittently return 405; AEO behaviour must not depend on that.
        from services import screening
        from services.customs_screening_lists import parse_csl, parse_uk
        fix = Path(__file__).parent / 'fixtures/controls'
        manifest = json.loads((fix / 'manifest.json').read_text())
        def raw(key):
            data = gzip.decompress((fix / manifest[key]['file']).read_bytes())
            assert hashlib.sha256(data).hexdigest() == manifest[key]['sha256']
            return data
        parsed = {'eu': screening.parse_eu(raw('eu')), 'un': screening.parse_un(raw('un')),
                  'ofac': screening.parse_ofac(raw('ofac'), raw('ofac_alt'), raw('ofac_add')),
                  'uk': parse_uk(raw('uk')), 'csl': parse_csl(raw('csl'))}
        for key, (entries, generated) in parsed.items():
            await screening._store(key, entries, generated, manifest[key]['sha256'], time.monotonic())
        for u in (OWNER, BROKER, OPS):
            await db.raw.users.insert_one({**u, 'session_version': 0, 'email_verified': True})
    run(go())
    EVIDENCE.mkdir(parents=True, exist_ok=True)


def teardown_module():
    run(client.drop_database(config.DB_NAME))
    LOOP.close()


@pytest.fixture(autouse=True)
def settings():
    run(update_settings(DEFAULTS))
    yield


async def api(method, path, user=OWNER, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://aeo.test') as c:
        return await c.request(method, '/api' + path, headers={
            'Authorization': 'Bearer ' + create_session_token(user['id']), 'X-Include-Test-Tenants': 'true'}, **kw)


async def declaration(*, first=False, euros=1000):
    cid, did, sid = [uuid.uuid4().hex for _ in range(3)]
    eori = sandbox_eori('3990001-4')
    rep = sandbox_eori('3990002-2')
    await db.raw.aeo_eori_checks.update_one({'eori': rep['eori']}, {'$set': rep}, upsert=True)
    c = {'id': cid, 'name': 'AEO Sandbox Oy', 'business_id': '3990001-4', 'test': True,
         'eori': eori['eori'], 'kyc_status': 'approved', 'sanctions_status': 'clear',
         'kyc': {'eori': eori, 'sanctions': {'status': 'pass', 'checked_at': now_iso()}},
         'poa': {'type': 'direct', 'scope': ['import', 'export', 'transit', 'ens'], 'signed_at': now_iso(),
                 'valid_to': iso(now() + timedelta(days=365))[:10]}, 'credit_limit_cents': 100000000}
    await db.raw.companies.insert_one(c)
    await db.raw.shipments.insert_one({'id': sid, 'company_id': cid, 'reference': 'AEO-SANDBOX',
                                       'status': 'in_transit', 'origin': {'country': 'CN'}, 'test': True})
    d = {'id': did, 'lrn': 'AEO-' + did[:12], 'company_id': cid, 'shipment_id': sid,
         'company_name': c['name'], 'type': 'import', 'status': 'draft', 'test': True,
         'submitter': {'name': 'Sandbox broker', 'email': 'broker@aeo.example'}, 'transaction_nature': '11',
         'representation_type': 'direct', 'importer': {'name': c['name'], 'eori': eori['eori']},
         'declarant': {'name': c['name'], 'eori': eori['eori']},
         'exporter': {'name': 'AEO Sandbox Exporter', 'address': {'street': 'Test Street 1',
                      'city': 'Shanghai', 'postal_code': '200000', 'country': 'CN'}},
         'representative': {'name': 'Huolitsija Oy', 'eori': rep['eori'], 'country': 'FI'}, 'office': {'code': 'FI002000'}, 'location_of_goods': 'Helsinki terminal',
         'delivery_terms': {'incoterm': 'FCA', 'place': 'Shanghai'},
         'documents': [{'type': 'commercial_invoice', 'code': 'N380', 'number': 'INV-' + did[:8]}],
         'invoice': {'document_id': 'sandbox-invoice-' + did, 'confirmed': True, 'currency': 'EUR', 'total': euros},
         'transport': {'transport_document': {'type': 'cmr', 'number': 'SANDBOX-CMR-' + did},
                       'nationality': 'FI', 'dispatch_country': 'CN', 'destination_country': 'FI', 'mode_at_border': '3', 'inland_mode': '3'},
         'goods_location': {'type': 'D', 'address': {'street': 'Satamakatu 1', 'city': 'Helsinki', 'postal_code': '00100',
                                                     'country': 'FI'}},
         'valuation': {'invoice_currency': 'EUR', 'invoice_total': euros, 'freight_known': True,
                       'exchange_rate': {'rate': 1, 'source': 'EUR', 'rate_date': now_iso()[:10]}},
         'items': [{'seq': 1, 'description': 'Wooden office desks', 'hs_code': '940330', 'cn_code': '94033011',
                    'marks': 'AEO SANDBOX 1', 'cn_confirmed': True, 'cn_confirmed_by': OWNER, 'invoice_amount': euros,
                    'origin_country': 'CN', 'gross_kg': 100, 'net_kg': 90, 'packages': 1, 'package_type': 'PX',
                    'duty_rate_pct': 0, 'preference': '100',
                    # TARIC (R0966/23) asks for CITES certificate C400 or Y900 "not concerned by CITES" on 9403 30 11
                    'documents': [{'code': 'Y900', 'reference': 'Pine (Pinus sylvestris), not listed in Reg. (EC) 338/97',
                                   'source': 'declared'}]}], 'history': [], 'created_at': now_iso()}
    # Third-country goods (CN) and a real every-party screening against official captured lists (s11 controls, rule K-1),
    # so the AEO checks below are what stops or allows filing, not missing customs data.
    from services.customs_screening import screen_parties
    d['screening'] = await screen_parties(d, kind='import')
    assert d['screening']['status'] == 'clear', [(p['role'], p['name'], p['status']) for p in d['screening']['parties']]
    await db.raw.customs_declarations.insert_one(dict(d))
    if not first:
        await db.raw.customs_declarations.insert_one({'id': uuid.uuid4().hex, 'company_id': cid,
                                                      'status': 'released', 'mrn': 'SANDBOX-PRIOR', 'test': True})
    return await customs.save_and_recompute(d, OWNER, 'recomputed')


def codes(ctl):
    return {r['code'] for r in ctl['reasons']}


@pytest.mark.parametrize('kind', ['import', 'export', 'transit', 'ens'])
def test_signature_and_authorised_filers(kind):
    async def go():
        d = await declaration()
        first_ctl = await aeo.filing_controls(d, OWNER, kind=kind)
        assert first_ctl['may_file'], [c for c in first_ctl['checks'] if not c.get('ok')] or first_ctl['reasons']
        await update_settings({'customs_authorised_filers': ['broker']})
        ctl = await aeo.filing_controls(d, OWNER, kind=kind)
        assert not ctl['may_file'] and 'aeo_filer_authorised' in codes(ctl)
        assert (await aeo.filing_controls(d, BROKER, kind=kind))['may_file']
        assert not (await aeo.filing_controls(d, OPS, kind=kind))['may_file']
        assert not (await aeo.filing_controls(d, {**OWNER, 'id': 'deleted-user'}, kind=kind))['may_file']
    run(go())


def test_unauthorised_request_and_direct_file_return_bilingual_403():
    async def go():
        d = await declaration()
        await update_settings({'customs_authorised_filers': ['broker']})
        r = await api('POST', f"/customs/{d['id']}/approve")
        assert r.status_code == 403, r.text
        assert r.json()['code'] == 'customs_not_authorised' and r.json()['fi'] and r.json()['en']
        with pytest.raises(aeo.ControlRefusal) as err:
            await customs.file_declaration(d, OWNER)
        assert err.value.code == 'customs_not_authorised'
        assert not (await db.raw.customs_declarations.find_one({'id': d['id']})).get('submission')
    run(go())


@pytest.mark.parametrize('euros,own_status', [(1000, 200), (20000, 409)])
def test_person_threshold_and_colleague_filing(euros, own_status):
    async def go():
        d = await declaration(euros=euros)
        requested = await customs.request_filing(d, OWNER)
        aid = requested['approval_id']
        r = await api('POST', f'/approvals/{aid}/decide', json={'decision': 'approved'})
        assert r.status_code == own_status, r.text
        if own_status == 409:
            assert r.json()['code'] == 'four_eyes_required'
            r = await api('POST', f'/approvals/{aid}/decide', user=BROKER, json={'decision': 'approved'})
            assert r.status_code == 200, r.text
        filed = await db.raw.customs_declarations.find_one({'id': d['id']})
        assert filed['status'] == 'accepted', filed
        assert filed['submission']['sandbox'] and filed['mrn'][4:7] == 'SBX'
        assert not await db.raw.event_failures.find_one({'event': 'approval.decided'})
    run(go())


@pytest.mark.parametrize('autopilot', [False, True])
def test_classifier_cannot_be_sole_high_risk_approver(autopilot):
    async def go():
        await update_settings({'autopilot_enabled': autopilot})
        d = await declaration(euros=20000)
        # Broker prepares; owner classified. Neither may independently approve.
        d = await customs.request_filing(d, BROKER)
        third = {**BROKER, 'id': uuid.uuid4().hex, 'name': 'Independent reviewer'}
        await db.raw.users.insert_one(third)
        try:
            r = await api('POST', f"/approvals/{d['approval_id']}/decide", json={'decision': 'approved'})
            assert r.status_code == 409 and r.json()['code'] == 'four_eyes_required', r.text
            r = await api('POST', f"/approvals/{d['approval_id']}/decide", user=third, json={'decision': 'approved'})
            assert r.status_code == 200, r.text
            assert (await db.raw.customs_declarations.find_one({'id': d['id']}))['status'] == 'accepted'
        finally:
            await db.raw.users.delete_one({'id': third['id']})
    run(go())


@pytest.mark.parametrize('euros,expected', [(1000, 'accepted'), (20000, 'ready_for_review')])
def test_delegated_autopilot_files_or_queues(euros, expected):
    async def go():
        await update_settings({'autopilot_enabled': True, 'customs_filing_delegated': True,
                               'guard_spend_per_day_cents': 100000000})
        d = await declaration(euros=euros)
        out = await customs.request_filing(d, BOT)
        assert out['status'] == expected
        a = await db.raw.approvals.find_one({'id': out['approval_id']})
        if expected == 'accepted':
            assert out['submission']['channel'] == 'sandbox' and out['submission']['sandbox']
            assert a['decided_by']['id'] == BOT['id']
        else:
            assert a['status'] == 'pending'
            assert 'aeo_four_eyes_duty_vat' in codes(a)
        assert await db.raw.agent_decisions.find_one({'subject_id': d['id'], 'agent': 'customs_agent'})
    run(go())


def test_kill_switch_and_delegation_hold_actual_gateway():
    async def go():
        d = await declaration()
        await update_settings({'autopilot_enabled': True, 'customs_filing_delegated': True, 'autopilot_halted': True})
        g = await gate_filing(d)
        assert not g.auto and 'halted' in codes(g.row)
        with pytest.raises(aeo.ControlRefusal):
            await customs.file_declaration(d, BOT)
        await update_settings({'autopilot_halted': False, 'customs_filing_delegated': False})
        g = await gate_filing(d)
        assert g.row['legal_point'] == 'customs_filing_not_delegated' and not g.auto
        with pytest.raises(aeo.ControlRefusal):
            await customs.file_declaration(d, BOT)
        assert not (await db.raw.customs_declarations.find_one({'id': d['id']})).get('submission')
    run(go())


@pytest.mark.parametrize('field,value,reason', [
    ('poa.signed_at', None, 'aeo_poa'), ('poa.valid_to', '2000-01-01', 'aeo_poa'),
    ('poa.type', 'indirect', 'aeo_poa'), ('poa.scope', ['export'], 'aeo_poa'),
    ('kyc_status', 'pending', 'aeo_kyc'), ('sanctions_status', 'potential_match', 'aeo_sanctions')])
def test_customer_controls_block(field, value, reason):
    async def go():
        d = await declaration()
        await db.raw.companies.update_one({'id': d['company_id']}, {'$set': {field: value}})
        ctl = await aeo.filing_controls(d, OWNER)
        assert not ctl['may_file'] and reason in codes(ctl)
    run(go())


def test_threshold_equality_first_customer_refiling_and_risk_flags():
    async def go():
        d = await declaration(first=True)
        # A draft without MRN is not a previous filing.
        await db.raw.customs_declarations.insert_one({'id': uuid.uuid4().hex, 'company_id': d['company_id'], 'status': 'draft'})
        d['totals'] = {'duty_cents': 100000, 'vat_cents': 400000, 'customs_value_cents': 10000000}
        d['history'] = [{'to': 'rejected', 'at': now_iso()}]
        d['items'][0].update(preference_code='300', preference='100', flags=[{'kind': k} for k in
                            ('dual_use', 'licence', 'sanctions', 'trade_defence')])
        fe = await aeo.four_eyes_requirement(d, 'import', await aeo.get_settings())
        assert {r['code'] for r in fe['reasons']} == {'aeo_four_eyes_' + x for x in (
            'duty_vat', 'value', 'first_declaration', 'refiling', 'dual_use', 'licence', 'sanctions', 'trade_defence', 'preference')}
    run(go())


def test_stale_eori_rechecks_sandbox_and_registry_outage_fails_closed(monkeypatch):
    async def go():
        d = await declaration()
        await db.raw.companies.update_one({'id': d['company_id']}, {'$set': {'kyc.eori.checked_at': '2000-01-01T00:00:00Z'}})
        ctl = await aeo.filing_controls(d, OWNER)
        assert next(c for c in ctl['checks'] if c['code'] == 'declarant_eori')['data']['rechecked']
        calls = []
        async def unavailable(eori):
            calls.append(eori)
            return {'status': 'error', 'summary': 'Injected network outage', 'checked_at': now_iso()}
        from services import kyc
        monkeypatch.setattr(kyc, 'eori_check', unavailable)
        e = d['representative']['eori']
        await db.raw.aeo_eori_checks.update_one({'eori': e}, {'$set': {'checked_at': '2000-01-01T00:00:00Z'}})
        ctl = await aeo.filing_controls(d, OWNER)
        assert calls == [e] and not ctl['may_file'] and 'aeo_representative_eori' in codes(ctl)
        await db.raw.aeo_eori_checks.update_one({'eori': e}, {'$set': sandbox_eori('3990002-2')})
    run(go())


def test_settings_permissions_csv_all_kinds_kpis_and_retention():
    async def go():
        assert (await api('GET', '/customs/controls/settings', user=BROKER)).status_code == 403
        r = await api('PATCH', '/customs/controls/settings', json={'customs_authorised_filers': ['broker']})
        assert r.status_code == 200 and r.json()['customs_authorised_filers'] == ['broker']
        assert (await api('PATCH', '/customs/controls/settings', json={'customs_four_eyes_duty_eur': -1})).status_code == 400
        await update_settings({'customs_authorised_filers': []})
        from core.audit import audit
        for kind, meta in aeo.KINDS.items():
            d = await declaration()
            await audit(OWNER, 'customs_' + kind + '_test', meta['entity'], d['id'])
        r = await api('GET', '/customs/controls/audit.csv')
        assert r.status_code == 200, r.text
        rows = list(csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))))
        for kind in aeo.KINDS:
            assert any(x['action'] == 'customs_' + kind + '_test' and x['kind'] == kind for x in rows)
        assert any(x['action'] == 'customs_aeo_controls' for x in rows)
        (EVIDENCE / 'audit.csv').write_bytes(r.content)
        d = await declaration()
        date = now_iso()[:10]
        await db.raw.customs_declarations.update_one({'id': d['id']}, {'$set': {'status': 'released', 'history': [
            {'to': 'submitted', 'at': date + 'T00:00:00Z'}, {'to': 'rejected', 'at': date + 'T00:10:00Z'},
            {'from': 'rejected', 'to': 'draft', 'at': date + 'T00:20:00Z'}, {'to': 'released', 'at': date + 'T02:00:00Z'}]}})
        r = await api('GET', '/customs/controls/kpis')
        assert r.status_code == 200, r.text
        k = r.json()
        assert k['totals']['rejected'] >= 1 and k['totals']['amendments'] >= 1
        assert k['totals']['time_to_release_hours']['max'] >= 2
        (EVIDENCE / 'kpis.json').write_text(r.text)
        old = uuid.uuid4().hex
        await db.raw.ens_filings.insert_one({'id': old, 'status': 'closed', 'released_at': '2000-01-01T00:00:00Z'})
        from core.jobs import enqueue, work_once
        j = await enqueue('customs.retention_review', {})
        assert await work_once('aeo-test', kinds=['customs.retention_review']) == 1
        done = await db.raw.jobs.find_one({'id': j['id']})
        assert done['status'] == 'done' and done['result']['deleted'] == 0, done
        assert (await db.raw.ens_filings.find_one({'id': old}))['retention']['status'] == 'review_for_disposal'
        assert (await db.raw.customs_declarations.find_one({'id': d['id']}))['retention']['status'] == 'retained'
        r = await api('GET', f"/customs/{d['id']}/controls")
        assert r.status_code == 200 and r.json()['you']['may_file'], r.text
    run(go())


def test_single_user_override_is_audited_and_counted():
    async def go():
        await db.raw.users.update_one({'id': BROKER['id']}, {'$set': {'disabled': True}})
        try:
            d = await customs.request_filing(await declaration(euros=20000), OWNER)
            path = f"/approvals/{d['approval_id']}/decide"
            r = await api('POST', path, json={'decision': 'approved'})
            assert r.status_code == 409 and r.json()['code'] == 'four_eyes_single_user'
            r = await api('POST', path, json={'decision': 'approved', 'override': True, 'note': 'Checked as the only authorised filer'})
            assert r.status_code == 200, r.text
            assert (await db.raw.customs_declarations.find_one({'id': d['id']}))['status'] == 'accepted'
            assert await db.raw.audit_log.find_one({'entity_id': d['id'], 'action': 'approval_four_eyes_override'})
            k = await aeo.kpis()
            assert k['four_eyes']['overrides'] >= 1
            (EVIDENCE / 'kpis-with-override.json').write_text(__import__('json').dumps(k, ensure_ascii=False, indent=2))
        finally:
            await db.raw.users.update_one({'id': BROKER['id']}, {'$set': {'disabled': False}})
    run(go())


def test_direct_filing_requires_independent_approval_and_fresh_actor():
    async def go():
        d = await declaration(euros=20000)
        with pytest.raises(aeo.ControlRefusal) as err:
            await customs.file_declaration(d, OWNER)
        assert err.value.status_code == 409 and err.value.code == 'four_eyes_required'
        d = await customs.request_filing(d, OWNER)
        a = await db.raw.approvals.find_one({'id': d['approval_id']}, NO_ID)
        await update_settings({'customs_authorised_filers': [OWNER['id']]})
        await customs._on_approval({'approval': {**a, 'status': 'approved'}, 'actor': BROKER})
        stored = await db.raw.customs_declarations.find_one({'id': d['id']})
        assert stored['approval_voided']['code'] == 'customs_not_authorised' and not stored.get('submission')
    run(go())


def test_final_switch_check_does_not_charge_agent_spend_twice():
    async def go():
        d = await declaration()
        from core.agent_runtime import spend_today
        before = await spend_today()
        amount = d['totals']['duty_cents'] + d['totals']['vat_cents']
        await update_settings({'autopilot_enabled': True, 'customs_filing_delegated': True,
                               'guard_spend_per_day_cents': before + amount})
        out = await customs.request_filing(d, BOT)
        assert out['status'] == 'accepted', out
        assert await spend_today() == before + amount
    run(go())


def test_read_only_stale_eori_is_not_reported_valid_and_missing_representative_blocks():
    async def go():
        d = await declaration()
        await db.raw.companies.update_one({'id': d['company_id']}, {'$set': {'kyc.eori.checked_at': '2000-01-01T00:00:00Z'}})
        ctl = await aeo.filing_controls(d, OWNER, refresh=False)
        assert not ctl['may_file'] and 'aeo_declarant_eori' in codes(ctl)
        d['representative'] = {}
        from unittest.mock import patch
        with patch.object(config, 'COMPANY_EORI', ''):
            ctl = await aeo.filing_controls(d, OWNER)
        assert not ctl['may_file'] and 'aeo_representative_eori' in codes(ctl)
    run(go())


@pytest.mark.skipif(os.environ.get('AEO_LIVE_EORI') != '1', reason='live EU EOS probe; set AEO_LIVE_EORI=1')
def test_live_eu_eori_negative_answer():
    """Deliberately invalid number, with the actual official response captured by the companion script."""
    async def go():
        from services.kyc import eori_check
        result = await eori_check('FI0000000-0')
        assert result['status'] == 'fail' and result['valid'] is False, result
        assert result['data']['results'] and all(r['status'] == '1' for r in result['data']['results'])
        (EVIDENCE / 'live-eori-test.json').write_text(__import__('json').dumps(result, indent=2))
    run(go())
