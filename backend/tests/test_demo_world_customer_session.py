"""Reused fictional customers keep only sessions proven by native authentication."""
import httpx
import pytest
import test_s2_prepayment as native

from core.db import db
from core.security import load_secret
from services.demo_world import engine, injectors


@pytest.fixture(autouse=True)
def customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


def test_same_persona_reuses_authenticated_session_and_other_or_revoked_identity_does_not():
    from server import app
    from services.email_channel.outbound import issue_link
    async def exercise():
        await load_secret(db.raw)
        for ident,bid in (('old-session','3994713-8'),('new-session','3994713-8'),('different-persona','3999999-9'),('revoked-session','3994713-8')):
            await db.raw.demo_runs.insert_one({'id':ident,'company_id':native.CID,'requester_registry':{'business_id':bid}})
            await engine._register_actor({'id':ident},'w:customer',native.CUSTOMER['email'],native.CUSTOMER['name'])
        token = await issue_link(native.CUSTOMER['id'],'/portal')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://demo.local') as client:
            response = await client.get('/api/email-channel/go/' + token)
            assert response.status_code == 303
            await db.raw.world_actors.update_one({'run_id':'old-session'},{'$set':{'session_cookies':dict(client.cookies)}})
        async def resume(ident):
            run = await db.raw.demo_runs.find_one({'id':ident})
            actor = await engine.actor_for(ident,'w:customer')
            return await injectors.customer_session(run,actor)
        assert await resume('new-session')
        assert (await engine.actor_for('new-session','w:customer')).get('session_cookies')
        assert not await resume('different-persona')
        assert not (await engine.actor_for('different-persona','w:customer')).get('session_cookies')
        await db.raw.users.update_one({'id':native.CUSTOMER['id']},{'$inc':{'session_version':1}})
        assert not await resume('revoked-session')
        assert not (await engine.actor_for('revoked-session','w:customer')).get('session_cookies')
    native.run(exercise())
