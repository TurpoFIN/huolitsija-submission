"""Env-gated test-tenant bootstrap: sets the TEST admin password and maps the e-mail alias, never other tenants.
Run alone: core.db binds one event loop."""
import os,sys,uuid,asyncio
from pathlib import Path
os.environ['DB_NAME']='huo_boot_test_'+uuid.uuid4().hex[:8]
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
import pytest
from core import config
if config.DB_NAME!=os.environ['DB_NAME']:
    pytest.skip('Run this file in its own process',allow_module_level=True)
from core.db import db,client
from core.security import verify_password
from services.test_tenant_bootstrap import bootstrap_test_tenant

TEST, OTHER = 'test-co', 'real-co'


def run(coro): return loop.run_until_complete(coro)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run(client.drop_database(config.DB_NAME))
    run(db.companies.insert_many([{'id':TEST,'test_tenant':True},{'id':OTHER,'test_tenant':False}]))
    run(db.users.insert_many([{'id':'e2e','email':'e2e.x@huolitsija.fi','company_id':TEST,'password_hash':None,'session_version':0},
                              {'id':'real','email':'real@example.com','company_id':OTHER,'password_hash':None}]))
    for k,v in {'TEST_BOOTSTRAP_COMPANY_ID':TEST,'TEST_BOOTSTRAP_ADMIN_EMAIL':'E2E.x@huolitsija.fi',
                'TEST_BOOTSTRAP_ADMIN_PASSWORD':'pw-for-test','TEST_BOOTSTRAP_EMAIL_ALIAS':'alias+t@example.com'}.items():
        monkeypatch.setenv(k,v)
    yield
    run(client.drop_database(config.DB_NAME))


def test_sets_password_and_maps_alias_idempotently():
    run(bootstrap_test_tenant()); run(bootstrap_test_tenant())
    u=run(db.users.find_one({'id':'e2e'}))
    assert verify_password('pw-for-test',u['password_hash']) and u['session_version']==1
    alias=run(db.users.find({'email':'alias+t@example.com'}).to_list(5))
    assert len(alias)==1 and alias[0]['company_id']==TEST and alias[0]['password_hash'] is None


def test_never_touches_a_non_test_tenant(monkeypatch):
    monkeypatch.setenv('TEST_BOOTSTRAP_COMPANY_ID',OTHER); monkeypatch.setenv('TEST_BOOTSTRAP_ADMIN_EMAIL','real@example.com')
    run(bootstrap_test_tenant())
    assert run(db.users.find_one({'id':'real'}))['password_hash'] is None
    assert run(db.users.count_documents({'email':'alias+t@example.com'}))==0


def test_alias_owned_by_another_company_is_not_remapped(monkeypatch):
    monkeypatch.setenv('TEST_BOOTSTRAP_EMAIL_ALIAS','real@example.com')
    run(bootstrap_test_tenant())
    assert run(db.users.find_one({'email':'real@example.com'}))['company_id']==OTHER
