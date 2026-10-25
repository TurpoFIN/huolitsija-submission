"""Structured customs boundary and actual Mongo-backed quote/approval/shipment integration."""
import asyncio

from core.db import client, db
from services import pricing


def place(country, name, lat, lon, **fields):
    return {"country": country, "name": name, "lat": lat, "lon": lon, **fields}


FI = place("FI", "Helsinki", 60.17, 24.94, unlocode="FIHEL")
NO = place("NO", "Oslo", 59.91, 10.75, unlocode="NOOSL")
SE = place("SE", "Stockholm", 59.33, 18.07, unlocode="SESTO")
AX = place("AX", "Mariehamn", 60.10, 19.95, unlocode="AXMHQ")


def request(origin=FI, destination=NO, **fields):
    return {"mode": "road", "origin": origin, "destination": destination,
            "cargo": {"packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2}, **fields}


def test_unstated_border_requirement_real_mongo():
    async def run():
        assert (await db.command("ping"))["ok"] == 1
        req = await pricing.normalize_request(request(customs_clearance=None))
        assert req.get("customs_required") is True
        assert req["customs_handling"] == "unknown"
    client.get_io_loop().run_until_complete(run())

import copy
import uuid
from unittest.mock import AsyncMock
import pytest
from core import llm
from core.settings import get_settings
from core.util import now_iso
from services import customs_border as border, quoting_agent


@pytest.mark.parametrize('country,fields,required,tax', [
    ('SE', {}, False, False), ('NO', {}, True, False), ('CH', {}, True, False),
    ('GB', {}, None, None), ('GB', {'subdivision':'NIR'}, False, False),
    ('GB', {'subdivision':'BFS'}, False, False), ('XI', {}, False, False),
    ('AX', {}, True, True), ('FI', {'subdivision':'01'}, True, True),
    ('FI', {'unlocode':'FIMHQ'}, True, True), ('ES', {'subdivision':'GC'}, True, True),
    ('ES', {'subdivision':'TF'}, True, True), ('ES', {'territory_code':'IC'}, True, True),
    ('ES', {'subdivision':'CE'}, True, False), ('ES', {'subdivision':'ML'}, True, False),
    ('ES', {'unlocode':'ESCEU'}, True, False), ('FO', {}, True, False),
    ('GL', {}, True, False), ('GI', {}, True, False), ('ZZ', {}, None, None),
])
def test_structured_territories(country, fields, required, tax):
    result = border.assess(FI, place(country, 'Ignored free text: no customs', 50, 5, **fields))
    assert result['customs_required'] is required
    assert result['vat_excise_formalities'] is tax
    assert result['source'].startswith('https://taxation-customs.ec.europa.eu/')


def test_normalization_policy_and_shipment_real_mongo(monkeypatch):
    from routes import quotes
    from services import shipments, positions, geo
    from services.agents import quote_agent
    from core.autonomy import quote_within_policy
    monkeypatch.setattr(shipments, 'emit', AsyncMock())  # no transport/provider side effects
    monkeypatch.setattr(positions, 'refresh_shipment', AsyncMock())
    monkeypatch.setattr(pricing, '_schedule', AsyncMock())
    monkeypatch.setattr(pricing.agents, 'agent_charges', AsyncMock(return_value=None))
    async def run():
        assert (await db.command('ping'))['ok'] == 1
        settings = await get_settings()
        settings = {**settings, 'autopilot_enabled':False, 'quote_approval_threshold_eur':1000000}
        actor = {'id':'customs-test-person', 'org_type':'internal', 'type':'user', 'name':'TEST customs reviewer'}
        tag = 'customs-test-'+uuid.uuid4().hex
        await db.companies.insert_one({'id':tag, 'name':'TEST customs boundary', 'test':True,
                                      'kyc_status':'approved','sanctions_status':'clear'})
        await db.users.replace_one({'id':actor['id']},actor,upsert=True)
        try:
            for i,(o,d,handling,required,tax,direction) in enumerate([
                (FI,NO,'unknown',True,False,'export'), (FI,SE,'unknown',False,False,'none'),
                (FI,AX,'unknown',True,True,'tax_border'), (NO,FI,'unknown',True,False,'import'),
                (FI,NO,'customer_broker',True,False,'export'), (FI,NO,'us',True,False,'export'),
                (FI,place('FI','Mariehamn',60.1222,19.8982,unlocode='FIMHQ',subdivision='01'),'unknown',True,True,'tax_border'),
                (FI,geo.public_location(geo.lookup_code('ESLPA')),'unknown',True,True,'tax_border'),
                (FI,geo.public_location(geo.lookup_code('GBBEL')),'unknown',False,False,'none'),
                (FI,geo.public_location(geo.lookup_code('DEHGL')),'unknown',True,False,'export')]):
                req = await pricing.normalize_request(request(o,d,customs_handling=handling,customs_required=False))
                assert req['customs_required'] is required and req['vat_excise_formalities'] is tax
                assert req['customs_direction'] == direction
                leg={'seq':1,'kind':'main','mode':'air','from':o,'to':d,'distance_km':1000}
                # Isolated option fixture exercises the real fee completion; no carrier offer is represented.
                opt=await pricing._finish_option(req, {'mode':'air','service':'AIR','legs':[copy.deepcopy(leg)],
                    '_lines':[pricing._cost_line('AIR_FREIGHT','main','PER_SHIPMENT',1,10000,leg_seq=1)],
                    '_cargo_metrics':pricing.cargo_metrics(req['cargo']), '_carrier':{}, '_card':{},
                    '_partial':[], '_dedupe':tag+str(i)}, settings, None)
                rows=[n for n in opt['not_included'] if n.get('reason_code','').startswith('customs.')]
                assert bool(rows) is required
                assert not any(n['code'] in ('CUSTOMS','CUSTOMS_ITEMS','EXPORT_CUSTOMS','ICS2') for n in opt['lines'])
                if required:
                    assert rows[0]['amount_cents'] is None and rows[0]['reason_fi']
                    assert rows[0]['status'] == ('customer_arranged' if handling=='customer_broker' else 'postponed')
                q={'id':tag+str(i),'request':req,'options':[opt],'status':'draft','company_id':tag,'number':tag+str(i)}
                await quotes.apply_approval_policy(q, settings, actor, send=True)
                await db.quotes.insert_one(copy.deepcopy(q))
                hold=required and handling!='customer_broker'
                assert (q['status']=='pending_approval') is hold
                assert ('customs_required' in q['approval']['reasons']) is hold
                assert q['customs_required'] is required
                if hold:
                    card=await db.approvals.find_one({'ref_id':q['id'],'status':'pending'})
                    assert border.POSTPONED_FI in card['reason_fi'] and border.POSTPONED_EN in card['reason']
                    assert card['agent_decision_id'] and card['data']['quote_fingerprint']==quote_agent.fingerprint(q)
                    decision=await db.agent_decisions.find_one({'id':card['agent_decision_id']})
                    assert decision['inputs']['customs_border']['customs_required'] is True
                    await quotes.apply_approval_policy(q, settings, actor, send=True)
                    assert await db.approvals.count_documents({'ref_id':q['id'],'status':'pending'})==1
                    assert 'customs_required' in (await quote_agent.autosend({'payload':{'approval_id':card['id']}}))['reasons']
                    blockers=await quotes.booking_blockers(q,None)
                    assert any(b['code']=='customs_required' for b in blockers)
                    agent={'id':'quote_agent','type':'agent','org_type':'internal'}
                    await quotes._quote_approval_decided({'approval':{**card,'status':'approved'},'actor':agent})
                    assert (await db.quotes.find_one({'id':q['id']}))['status']=='pending_approval'
                    await db.approvals.update_one({'id':card['id']},{'$set':{'status':'approved','decided_by':actor}})
                    await quotes._quote_approval_decided({'approval':{**card,'status':'approved','decided_by':actor},'actor':actor})
                    stored=await db.quotes.find_one({'id':q['id']})
                    assert stored['status']=='sent' and border.human_approved(stored)
                    changed=copy.deepcopy(stored);changed['request']['destination']['country']='CH'
                    assert not border.human_approved(changed)
                    if i in (0, 3, 5):
                        before_cards=await db.approvals.count_documents({'ref_id':q['id']})
                        revised=copy.deepcopy(stored);revised['revision']=stored.get('revision',1)+1
                        await quote_agent.carry_customs_review(stored,revised)
                        await quotes.apply_approval_policy(revised,settings,actor,send=True)
                        assert revised['status']=='sent' and quote_agent.human_authorized(revised)
                        assert await db.approvals.count_documents({'ref_id':q['id']})==before_cards
                        assert await db.approvals.count_documents({'ref_id':q['id'],'status':'pending'})==0
                        altered=copy.deepcopy(revised);altered['revision']+=1
                        if i==0:
                            altered['request']['destination']=place('CH','Zurich',47.38,8.54,unlocode='CHZRH')
                            altered['options'][0]['legs'][-1]['to']=altered['request']['destination']
                        elif i==5:
                            altered['request']['customs_handling']='customer_broker'
                        else:
                            altered['options'][0]['total_cents']+=100
                        # Simulate a source edit before repricing: the native authority keeps the approved snapshot.
                        edited_previous=copy.deepcopy(revised)
                        edited_previous['request']=copy.deepcopy(altered['request'])
                        await quote_agent.carry_customs_review(edited_previous,altered)
                        await quotes.apply_approval_policy(altered,settings,actor,send=True)
                        assert altered['status']=='pending_approval' and 'customs_required' in altered['approval']['reasons']
                        assert await db.approvals.count_documents({'ref_id':q['id'],'status':'pending'})==1
                elif required:
                    assert q['customs_notice']=={'en':border.CUSTOMER_EN,'fi':border.CUSTOMER_FI}
                shipment=await shipments.create_shipment(company_id=tag,quote=q,option=opt,booking={'origin':'FIHEL','destination':d['code']} if i>=7 else {},actor=actor)
                saved=await db.shipments.find_one({'id':shipment['id']})
                assert saved['customs_required'] is required and saved['customs_clearance'] is required
                assert saved['customs'] is required and saved['vat_excise_formalities'] is tax
                assert bool([m for m in saved['milestones'] if m['code'] in ('export_cleared','customs_released')]) is required
                if i>=7:
                    assert saved['destination']['subdivision']==d['subdivision']
                assert saved['customs_handling']==handling
                assert saved['customs_filing_status']==('customer_arranged' if handling=='customer_broker' else 'postponed' if required else 'not_required')
        finally:
            await db.quotes.delete_many({'company_id':tag})
            await db.shipments.delete_many({'company_id':tag})
            await db.approvals.delete_many({'company_id':tag})
            await db.companies.delete_one({'id':tag})
            await db.users.delete_one({'id':actor['id']})
            await db.agent_decisions.delete_many({'company_id':tag})
    client.get_io_loop().run_until_complete(run())


@pytest.mark.parametrize('text,handling',[
    ('Kuljetus Helsingistä Osloon, 2 lavaa, 600 kg.','unknown'),
    ('Frakt Helsinki til Oslo, 2 paller, 600 kg. Kunden ordner eksport og import med egen tollmegler.','customer_broker'),
    ('Transport Helsinki nach Oslo, 2 Paletten, 600 kg. Bitte übernehmen Sie die Zollabfertigung.','us'),
])
def test_handling_semantic_source_boundary(monkeypatch,text,handling):
    evidence=text.split('. ')[-1] if handling!='unknown' else None
    answer={'origin':FI,'destination':NO,'cargo':{'packages':2,'weight_kg':600},'summary':'Transport',
            'summary_i18n':{'fi':'Kuljetus','en':'Transport'},'customs_handling':handling,'customs_handling_evidence':evidence}
    model=AsyncMock(return_value=answer)
    monkeypatch.setattr(quoting_agent,'llm_json',model)
    monkeypatch.setattr(quoting_agent,'_resolve',AsyncMock(side_effect=[(FI,None),(NO,None)]))
    result=client.get_io_loop().run_until_complete(quoting_agent.parse_request(text))
    assert result['request']['customs_handling']==handling
    assert text in model.call_args.args[2]


def test_handling_ungrounded_or_model_down(monkeypatch):
    monkeypatch.setattr(quoting_agent,'_resolve',AsyncMock(side_effect=[(FI,None),(NO,None)]))
    monkeypatch.setattr(quoting_agent,'llm_json',AsyncMock(return_value={'summary':'x','summary_i18n':{'fi':'x','en':'x'},
        'customs_handling':'customer_broker','customs_handling_evidence':'invented'}))
    result=client.get_io_loop().run_until_complete(quoting_agent.parse_request('Kuljetus Helsinki Oslo, 2 lavaa 600 kg'))
    assert result['request']['customs_handling']=='unknown'
    monkeypatch.setattr(quoting_agent,'llm_json',AsyncMock(side_effect=llm.LLMUnavailable('offline')))
    with pytest.raises(llm.LLMUnavailable):
        client.get_io_loop().run_until_complete(quoting_agent.parse_request('Frakt fra Helsinki til Oslo'))


def test_unknown_territory_holds_real_mongo_even_customer_broker(monkeypatch):
    from routes import quotes
    from services import shipments
    from services.agents import quote_agent
    from fastapi import HTTPException
    assemble=AsyncMock()
    monkeypatch.setattr(shipments,'_assemble',assemble)
    async def run():
        assert (await db.command('ping'))['ok']==1
        tag='customs-unknown-'+uuid.uuid4().hex
        actor={'id':tag+'-person','type':'user','org_type':'internal','name':'TEST reviewer'}
        company={'id':tag,'name':'TEST unknown territory','kyc_status':'approved','sanctions_status':'clear','test':True}
        await db.companies.insert_one(dict(company));await db.users.insert_one(dict(actor))
        try:
            for i,handling in enumerate(('unknown','customer_broker')):
                req=border.apply(request(FI,place('DE','No regional evidence',52,13),customs_handling=handling))
                assert req['customs_required'] is None and req['customs_territory_verified'] is False
                opt={'id':tag+'-opt'+str(i),'mode':'air','total_cents':10000,'cost_cents':5000,'margin_cents':5000,
                     'lines':[{'code':'AIR_FREIGHT','amount_cents':10000}], 'legs':[]}
                q={'id':tag+str(i),'number':tag+str(i),'company_id':tag,'request':req,'options':[opt],'status':'draft'}
                await quotes.apply_approval_policy(q,await get_settings(),actor,send=True)
                assert q['status']=='pending_approval' and q['customs_required'] is None
                assert q['customs_territory_verified'] is False and 'territory_unverified' in q['approval']['reasons']
                await db.quotes.insert_one(copy.deepcopy(q))
                await quotes.apply_approval_policy(q,await get_settings(),actor,send=True)
                assert await db.approvals.count_documents({'ref_id':q['id'],'status':'pending'})==1
                card=await db.approvals.find_one({'ref_id':q['id'],'status':'pending'})
                assert border.UNVERIFIED_EN in card['reason'] and border.UNVERIFIED_FI in card['reason_fi']
                assert card['agent_decision_id']
                gate=await quote_agent.autosend(quote=q)
                assert not gate.auto and any(r['code']=='territory_unverified' for r in gate.reasons)
                assert any(b['code']=='territory_unverified' for b in await quotes.booking_blockers(q,company))
                await quotes._quote_approval_decided({'approval':{**card,'status':'approved','decided_by':actor},'actor':actor})
                assert (await db.quotes.find_one({'id':q['id']}))['status']=='pending_approval'
                with pytest.raises(HTTPException) as exc:
                    await shipments.create_shipment(company_id=tag,quote=q,option=opt,booking={},actor=actor)
                assert exc.value.status_code==409 and exc.value.detail['code']=='territory_unverified'
                assert await db.shipments.count_documents({'quote_id':q['id']})==0
            assert assemble.await_count==0
        finally:
            for col in (db.quotes,db.approvals,db.agent_decisions):await col.delete_many({'company_id':tag})
            await db.users.delete_one({'id':actor['id']});await db.companies.delete_one({'id':tag})
    client.get_io_loop().run_until_complete(run())
