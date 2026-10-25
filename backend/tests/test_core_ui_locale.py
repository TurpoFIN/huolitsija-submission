"""Offline model boundary + real Mongo locale migration, and Helsinki calendar boundaries."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from core import config, llm
from routes.portal import helsinki_day_bounds
from services import locale_backfill as locale


@pytest.fixture(autouse=True)
def isolate_provider(monkeypatch):
    # Provider configuration from the host must never route these offline
    # translation/storage contract tests to a real service.
    monkeypatch.setattr(config, 'LLM_PROVIDER', 'openai')
    monkeypatch.setattr(config, 'OPENAI_API_KEY', '')


@pytest.mark.parametrize('instant,day,start,end',[
    ('2026-10-24T20:59:59+00:00','2026-10-24','2026-10-23T21:00:00Z','2026-10-24T21:00:00Z'),
    ('2026-10-24T21:00:00+00:00','2026-10-25','2026-10-24T21:00:00Z','2026-10-25T22:00:00Z'),
    ('2026-03-29T09:00:00+00:00','2026-03-29','2026-03-28T22:00:00Z','2026-03-29T21:00:00Z'),
    ('2026-10-25T09:00:00+00:00','2026-10-25','2026-10-24T21:00:00Z','2026-10-25T22:00:00Z'),
])
def test_helsinki_day_exact_utc_bounds(instant,day,start,end):
    assert helsinki_day_bounds(datetime.fromisoformat(instant)) == (start,end,day)


def test_backfill_idempotent_real_storage_and_model_contract(monkeypatch):
    async def run():
        client=AsyncIOMotorClient(config.MONGO_URL)
        db=client['huo_locale_test_'+uuid4().hex]
        monkeypatch.setattr(locale,'db',db)
        monkeypatch.setattr(llm,'db',db)
        monkeypatch.setattr(config,'OPENAI_API_KEY','offline-test-key')
        calls=[]
        async def provider(system,prompt,files,model,max_tokens):
            calls.append(prompt)
            strings=json.loads(prompt)['strings']
            return json.dumps({'items':[{'fi':'Kaksi lavaa vaurioitui.','en':s} for s in strings]}),'offline-test-provider'
        monkeypatch.setattr(llm,'_call_openai',provider)
        try:
            source={'id':'legacy','demo':True,'title':'Two damaged pallets','description':'Source untouched'}
            await db.exceptions.insert_one(dict(source))
            assert await locale.backfill_record(db.exceptions,source,['title']) is True
            stored=await db.exceptions.find_one({'id':'legacy'})
            assert stored['title']=='Two damaged pallets'
            assert stored['description']=='Source untouched'
            assert stored['title_i18n']=={'fi':'Kaksi lavaa vaurioitui.','en':'Two damaged pallets'}
            assert await locale.backfill_record(db.exceptions,stored,['title']) is False
            assert len(calls)==1
            assert await db.ai_runs.count_documents({'status':'ok','agent':'locale_backfill'})==1
            assert await locale.backfill_record(db.exceptions,stored,['title']) is False
        finally:
            await client.drop_database(db.name)
            client.close()
    asyncio.run(run())


def test_missing_provider_skips_and_preserves_source(monkeypatch):
    monkeypatch.setattr(config,'OPENAI_API_KEY','')
    async def must_not_call(*a,**k):
        raise AssertionError('provider called without configuration')
    monkeypatch.setattr(llm,'_call_openai',must_not_call)
    assert asyncio.run(locale.translate_strings(['Source'])) is None


def test_invalid_model_translation_is_rejected(monkeypatch):
    monkeypatch.setattr(config,'OPENAI_API_KEY','offline-test-key')
    async def malformed(*a,**k):return {'items':[{'en':'Only English'}]}
    monkeypatch.setattr(llm,'llm_json',malformed)
    with pytest.raises(ValueError,match='Incomplete bilingual'):
        asyncio.run(locale.translate_strings(['Source']))


def test_deterministic_legacy_migration_is_additive_and_idempotent():
    source={'title':'Customs hold','cargo':{'description':'Goods'},'demo':True}
    result=locale.legacy_variants(source,{'Customs hold':'Tullipysäytys','Goods':'Tavarat'})
    assert result['title']==source['title']
    assert result['cargo']['description']==source['cargo']['description']
    assert result['cargo']['description_i18n']=={'fi':'Tavarat','en':'Goods'}
    assert locale.legacy_variants(result,{'Customs hold':'Tullipysäytys','Goods':'Tavarat'})==result
    assert 'title_i18n' not in source


def test_startup_budget_and_existing_ai_variants(monkeypatch):
    async def run():
        client=AsyncIOMotorClient(config.MONGO_URL)
        db=client['huo_locale_budget_'+uuid4().hex]
        monkeypatch.setattr(locale,'db',db)
        monkeypatch.setattr(llm,'db',db)
        monkeypatch.setattr(config,'OPENAI_API_KEY','offline-test-key')
        calls=[]
        async def provider(system,prompt,files,model,max_tokens):
            calls.append(prompt)
            return json.dumps({'items':[{'fi':'Suomenkielinen käännös','en':s} for s in json.loads(prompt)['strings']]}),'offline-test-provider'
        monkeypatch.setattr(llm,'_call_openai',provider)
        try:
            for i in range(2):
                await db.exceptions.insert_one({'id':str(i),'ai_assessment':{'status':'ok','summary':'Known source','impact':'Delivery delayed','recommended_actions':['Ask carrier']}})
            await db.exceptions.insert_one({'id':'already','ai_assessment':{'status':'ok','summary':'Known source','summary_i18n':{'fi':'Tunnettu lähde','en':'Known source'}}})
            assert await locale.run_backfill(max_records=1)==1
            assert len(calls)==1
            assert await locale.run_backfill(max_records=1)==1
            assert len(calls)==2
            assert await locale.run_backfill(max_records=1)==0
            assert len(calls)==2
            saved=await db.exceptions.find_one({'id':'0'})
            assert saved['ai_assessment']['recommended_actions_i18n']=={'fi':['Suomenkielinen käännös'],'en':['Ask carrier']}
            assert saved['ai_assessment']['summary']=='Known source'
            await db.messages.insert_one({'id':'partial','thread_id':'thread','author':{'type':'ai'},'ai':{'reason':'Known reason','reason_i18n':{'fi':'Tunnettu perustelu','en':'Known reason'},'facts_used':['Source fact']}})
            assert await locale.run_backfill(max_records=1)==1
            assert await locale.run_backfill(max_records=1)==0
            message=await db.messages.find_one({'id':'partial'})
            assert message['ai']['reason_i18n']['fi']=='Tunnettu perustelu'
            assert message['ai']['facts_used_i18n']=={'fi':['Suomenkielinen käännös'],'en':['Source fact']}
            assert len(calls)==3
        finally:
            await client.drop_database(db.name)
            client.close()
    asyncio.run(run())


def test_nested_customs_ai_translation_preserves_codes(monkeypatch):
    async def run():
        client=AsyncIOMotorClient(config.MONGO_URL)
        db=client['huo_locale_nested_'+uuid4().hex]
        monkeypatch.setattr(locale,'db',db)
        monkeypatch.setattr(llm,'db',db)
        monkeypatch.setattr(config,'OPENAI_API_KEY','offline-test-key')
        calls=[]
        async def provider(system,prompt,files,model,max_tokens):
            calls.append(prompt)
            return json.dumps({'items':[{'fi':'Luokittelun perustelu','en':s} for s in json.loads(prompt)['strings']]}),'offline-test-provider'
        monkeypatch.setattr(llm,'_call_openai',provider)
        try:
            await db.customs_declarations.insert_one({'id':'decl','status':'draft','items':[{'seq':1,'cn_code':'94016100','hs_suggestion':{'source':'ai','rationale':'Classification rationale'}}]})
            assert await locale.run_backfill(max_records=1)==1
            assert await locale.run_backfill(max_records=1)==0
            record=await db.customs_declarations.find_one({'id':'decl'})
            assert record['items'][0]['cn_code']=='94016100'
            assert record['items'][0]['hs_suggestion']['rationale_i18n']=={'fi':'Luokittelun perustelu','en':'Classification rationale'}
            assert len(calls)==1
        finally:
            await client.drop_database(db.name)
            client.close()
    asyncio.run(run())
