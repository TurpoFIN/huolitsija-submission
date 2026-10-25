"""Opt-in regression against real local Mongo and the actual pricing pipeline.

Run with DB_NAME=huo_public_surcharges_local MONGO_URL=mongodb://127.0.0.1:27017
SURCHARGE_MONGO_TEST=1 PYTHONPATH=backend python -m pytest -s this_file.
Only existing demo/indicative AIR cards are temporarily annotated; restored in finally.
No quotes, offers, customers, RFQs or messages are created.
"""
import copy
import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv('SURCHARGE_MONGO_TEST') != '1', reason='explicit local Mongo opt-in required')


@pytest.mark.asyncio
async def test_real_mongo_invalid_tariffs_preserve_independent_options(monkeypatch):
    assert os.environ['MONGO_URL'] in ('mongodb://127.0.0.1:27017', 'mongodb://localhost:27017')
    assert os.environ['DB_NAME'].startswith('huo_') and os.environ['DB_NAME'].endswith('_local')
    from core.db import db
    from core.settings import get_settings
    from services import pricing, global_quote as g
    from services import public_surcharges as s
    from datetime import date

    request = {'origin':'CNPVG', 'destination':'FIHEL', 'mode':'air', 'ready_date':'2026-10-25',
               'cargo':{'weight_kg':120, 'volume_m3':1.2, 'packages':1, 'package_type':'pallet_eur'}}
    cards = await db.rate_cards.find({'service':'AIR'}).to_list(1000)
    assert cards and all(c.get('demo') and c.get('source') == 'indicative' for c in cards)
    counts = {n:await db[n].count_documents({}) for n in ('quotes','lane_rate_requests','email_outbox','shipments')}
    evidence = {}
    def summary(q):
        return {'options':[{k:o.get(k) for k in ('mode','service','rate_card_id','carrier_name','total_cents','firmness','bookable','price_basis')} for o in q['options']],
                'not_quoted':q['not_quoted']}
    def signature(q):
        return sorted((o['mode'],o['service'],o['total_cents']) for o in q['options'])
    async def restore():
        for c in cards:
            await db.rate_cards.replace_one({'id':c['id']},copy.deepcopy(c))
    async def annotate(ids, refs, **extra):
        await db.rate_cards.update_many({'id':{'$in':ids}}, {'$set':{'public_surcharge_refs':refs,**extra}})
    try:
        baseline = await pricing.quote_pricing(copy.deepcopy(request))
        assert baseline['options'] and all(o['mode']=='air' for o in baseline['options'])
        evidence['baseline'] = summary(baseline)
        # Empty refs must preserve mode, service and exact prices.
        ids=[c['id'] for c in cards]
        await annotate(ids,[])
        assert signature(await pricing.quote_pricing(copy.deepcopy(request))) == signature(baseline)
        # Explicit inclusion avoids any lookup (same-leg itemization is covered in focused hook tests).
        async def no_lookup(*args, **kwargs):
            raise AssertionError('included/itemized tariff must not be fetched')
        with monkeypatch.context() as patch:
            patch.setattr(s,'calculate',no_lookup)
            await annotate(ids,[{'tariff_id':'finnlines-baf-finnlink'}],included_charge_codes=['BAF'])
            assert signature(await pricing.quote_pricing(copy.deepcopy(request))) == signature(baseline)
        await restore()
        # Cheapest invalid card cannot evict valid siblings in the rate-table engine.
        cheapest=min(baseline['options'],key=lambda o:o['total_cents'])['rate_card_id']
        await annotate([cheapest],[{'tariff_id':'finnlines-baf-finnlink'}])
        siblings=await pricing.quote_pricing(copy.deepcopy(request))
        assert siblings['options'] and all(o['rate_card_id'] != cheapest for o in siblings['options'])
        assert any(n.get('rate_card_id')==cheapest for n in siblings['not_quoted'])
        evidence['valid_siblings'] = summary(siblings)
        # Exercise the global selector directly too: it picks a cheapest card before dedupe.
        req=await pricing.normalize_request(copy.deepcopy(request));m=pricing.cargo_metrics(req['cargo'])
        settings=await get_settings();tables=await g.Tables.load(date(2026,10,25))
        routing=next(r for r in await g.plan(req,m) if r['mode']=='air' and r['main'][0]['unlocode']=='CNPVG')
        saved=copy.deepcopy(tables.cards);rejections=[]
        option=await g.price_routing(req,m,routing,tables,settings,None,rejections=rejections)
        assert option and option['rate_card_id'] and option['rate_card_id'] != cheapest
        assert rejections and tables.cards == saved
        evidence['global_valid_sibling']={'selected':option['rate_card_id'],'rejections':rejections}
        # Each unavailable state uses actual resolver/evaluator and actual routing.
        capture=Path(__file__).parent/'fixtures/public_surcharges/swissport.pdf'
        async def captured(url):return capture.read_bytes()
        async def unavailable(url):raise httpx.ConnectError('local regression: source unavailable')
        async def expired(*args, **kwargs):
            # Inject the resolver refusal contract; dated Finnlines expiry is tested from its capture separately.
            return {'state':'unavailable','amount':None,'reason':'tariff_period_expired'}
        for state in ('provider_mismatch','missing_source','expired','out_of_scope'):
            await restore();s._CACHE.clear();s._LOCKS.clear()
            refs=[{'tariff_id':'finnlines-baf-finnlink'}]
            extra={}
            if state != 'provider_mismatch':
                refs=[{'tariff_id':'swissport-hel-import-thc','on_date':'2026-10-25'}]
                extra={'destination_terminal_operator':'swissport'}
                if state=='out_of_scope':
                    refs=[{'tariff_id':'swissport-hel-export-thc'}]
                    extra={'origin_terminal_operator':'swissport'}
            await annotate(ids,refs,**extra)
            with monkeypatch.context() as patch:
                patch.setattr(s,'_download',unavailable if state=='missing_source' else captured)
                if state=='expired':patch.setattr(s,'calculate',expired)
                result=await pricing.quote_pricing(copy.deepcopy(request))
            assert result['options'], state
            assert all(o['mode']=='air' and o['total_cents']>0 for o in result['options'])
            assert all(o['firmness']=='estimate' and o['bookable'] is False and o['price_basis']=='benchmark_estimate' for o in result['options']), state
            assert result['not_quoted'] and all(n.get('details') for n in result['not_quoted'] if n.get('code')=='public_tariff.unavailable')
            assert not any(o.get('_public_surcharge_errors') for o in result['options'])
            evidence[state]=summary(result)
        # Exhausted candidates and no independent benchmark: return explicit refusal, never a partial price.
        s._CACHE.clear();s._LOCKS.clear()
        await annotate(ids,[{'tariff_id':'finnlines-baf-finnlink'}])
        tables=await g.Tables.load(date(2026,10,25));saved=copy.deepcopy(tables.cards)
        async def no_benchmark(*args,**kwargs):return [],{},0
        rejections=[]
        with monkeypatch.context() as patch:
            patch.setattr(g,'air_leg_estimate',no_benchmark)
            option=await g.price_routing(req,m,routing,tables,settings,None,rejections=rejections)
        assert option is None and rejections and tables.cards==saved
        assert len(rejections)<=len(tables.cards)
        evidence['no_benchmark']={'option':option,'rejections':rejections}
    finally:
        await restore();s._CACHE.clear();s._LOCKS.clear()
    restored=await pricing.quote_pricing(copy.deepcopy(request))
    assert signature(restored)==signature(baseline)
    assert counts == {n:await db[n].count_documents({}) for n in counts}
    evidence['unchanged_counts']=counts
    if os.getenv('SURCHARGE_EVIDENCE'):
        Path(os.environ['SURCHARGE_EVIDENCE']).write_text(json.dumps(evidence,indent=2))
    for name,row in evidence.items():
        print(name, json.dumps(row if name=='unchanged_counts' else {'options':row.get('options',[]),'refusals':len(row.get('not_quoted',row.get('rejections',[])))}))
