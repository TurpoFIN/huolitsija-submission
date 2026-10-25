"""Damage claims keep their real review card while the physical cargo continues."""
import json
import os
from pathlib import Path

import httpx
import pytest
import test_s2_prepayment as native

from core.db import db
from core.security import load_secret
from core.settings import get_settings, update_settings
from core.storage import get_file
from core.util import now_iso
from services import carrier_booking
from services.agents import claims_agent, delivery_agent, exception_agent, invoice_agent, tracking_agent
from services.demo_autoquote import clock
from services.demo_world import boundary, engine, injectors, observer


@pytest.fixture(autouse=True)
def native_customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


def test_damage_claim_review_does_not_stop_delivery_pod_or_invoice(monkeypatch):
    from routes.quotes import book
    from server import app
    from services.email_channel.outbound import issue_link
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')

    async def external_facts(actor, system, prompt, **kw):
        data = json.loads(prompt)
        if 'exception' in data:
            return {'description':'One carton corner was crushed; the customer inspected bent steel parts. Remaining cargo can move.',
                'amount_claimed_cents':8000,'gross_kg_affected':10,'packages_affected':1}
        return {'code':data['allowed'][0],'delay_hours':1,'note':'Controlled carrier event input'}
    async def no_delay(*a,**kw):
        pass
    monkeypatch.setattr(engine.llm,'ask_json',external_facts)
    monkeypatch.setattr(engine.clock,'sleep_sim',no_delay)
    q = native.quote('damage-world')
    native.run(db.raw.quotes.update_one({'id':q['id']},{'$set':{'request.customs_clearance':False}}))

    async def exercise():
        await load_secret(db.raw)
        await db.raw.companies.update_one({'id':native.CID},{'$set':{'credit_limit_cents':500000}})
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        assert (await get_settings())['insurance_claim_limit_cents'] == 0
        await db.raw.demo_runs.insert_one({'id':'damage-world','phase':'quote','status':'running','seed':43,
            'quote_id':q['id'],'event_seq':0,'created_at':now_iso(),'profile':{'value_eur':1000}})
        await clock.reset_now('damage-world')
        await db.raw.demo_runs.update_one({'id':'damage-world'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock('damage-world',speed=86400)
        result = await book(native.body(q),native.request(),native.user())
        sid = result['shipment_id']
        shipment = await db.raw.shipments.find_one({'id':sid})
        await db.raw.carriers.update_one({'id':'c-road'},{'$set':{'modes':['road','sea']}})
        for leg in shipment['legs']:
            ident = 'damage-booking-' + str(leg['seq'])
            await db.raw.carrier_bookings.insert_one({'id':ident,'shipment_id':sid,'leg_seq':leg['seq'],
                'status':'requested','carrier_id':'c-road','carrier_name':'Testi Kuljetus Oy','history':[]})
            await carrier_booking.confirm_booking(ident,carrier_ref='DAMAGE-INPUT-' + str(leg['seq']),actor={'id':'feed-test','type':'system'})
        await engine._register_actor({'id':'damage-world'},'w:customer',native.CUSTOMER['email'],native.CUSTOMER['name'])
        token = await issue_link(native.CUSTOMER['id'],'/portal/claims')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://demo.local') as client:
            response = await client.get('/api/email-channel/go/' + token)
            assert response.status_code == 303
            await db.raw.world_actors.update_one({'run_id':'damage-world','actor':'w:customer'},
                {'$set':{'session_cookies':dict(client.cookies)}})
        async def feed(code, **extra):
            event = {'event_id':'damage-input-' + code,'shipment_ref':shipment['reference'],
                'leg_seq':1,'code':code,'at':now_iso(),**extra}
            record, _ = await tracking_agent.store_feed_event('TESTIK',event,verified=True)
            await tracking_agent.ingest({'payload':{'key':record['key']}})
        await feed('picked_up')
        await feed('exception',exception={'type':'damage','title':'One carton corner crushed',
            'description':'One carton inspected; remaining goods can move.','stops_goods':False})
        exception = await db.raw.exceptions.find_one({'shipment_id':sid,'type':'damage'})
        await exception_agent.handle({'payload':{'exception_id':exception['id']}})
        request = await db.raw.jobs.find_one({'kind':'world.file_claim','payload.exception_id':exception['id']})
        assert request
        submitted = await engine.file_claim(request)
        claim = await db.raw.claims.find_one({'id':submitted['id']})
        assert claim['status'] == 'submitted' and claim['document_ids']
        report = await db.raw.documents.find_one({'id':claim['document_ids'][0]})
        content,_,_ = await get_file(report['file_id'])
        assert content.startswith(b'%PDF') and report['source'] == 'portal'
        await claims_agent.decide_claim({'payload':{'claim_id':claim['id']}})
        claim = await db.raw.claims.find_one({'id':claim['id']})
        approval = await db.raw.approvals.find_one({'id':claim['approval_id']})
        assert claim['status'] == 'in_review' and approval['status'] == 'pending'
        assert approval.get('legal_point') == 'claim_above_insurance_limit'
        review_event = await db.raw.demo_run_events.find_one({'run_id':'damage-world','title':'Claim awaiting settlement review'})
        assert review_event['state'] == 'waiting' and review_event['data']['facts']['approval_id'] == approval['id']
        assert await engine.physical_blocker(sid) is None
        # A true physical hold still blocks, even when it is damage-related.
        await db.raw.exceptions.insert_one({'id':'physical-hold','shipment_id':sid,'type':'damage','status':'open',
            'evidence':{'event':{'exception':{'stops_goods':True}}}})
        assert (await engine.physical_blocker(sid))['id'] == 'physical-hold'
        await db.raw.exceptions.delete_one({'id':'physical-hold'})
        for turn in range(20):
            await engine.move({'payload':{'run_id':'damage-world','shipment_id':sid,'key':'claim-move-' + str(turn)}})
            pending = await db.raw.feed_events.find_one({'event.shipment_ref':shipment['reference'],'status':'queued'})
            assert pending
            await tracking_agent.ingest({'payload':{'key':pending['key']}})
            shipment = await db.raw.shipments.find_one({'id':sid})
            if shipment['status'] == 'delivered':
                break
        assert shipment['status'] == 'delivered'
        pod = await db.raw.documents.find_one({'shipment_id':sid,'type':'pod'})
        pod_bytes,_,_ = await get_file(pod['file_id'])
        assert pod_bytes.startswith(b'%PDF')
        assert (await delivery_agent.confirm({'payload':{'shipment_id':sid}}))['confirmed']
        issued = await invoice_agent.issue_on_delivery({'payload':{'shipment_id':sid}})
        invoice = await db.raw.invoices.find_one({'shipment_id':sid})
        assert invoice['status'] == 'open' and invoice['pdf_file_id']
        assert (await db.raw.claims.find_one({'id':claim['id']}))['status'] == 'in_review'
        assert (await db.raw.exceptions.find_one({'id':exception['id']}))['status'] == 'in_progress'
        assert (await get_settings())['insurance_claim_limit_cents'] == 0
        assert (await engine.file_claim(request))['id'] == claim['id']
        assert await db.raw.claims.count_documents({'shipment_id':sid}) == 1
        # A real imported freight payment finishes the autonomous work with a
        # review handoff, never a false claim settlement.
        from services import reconciliation
        statement = await injectors.bank(invoice,'claim-freight-credit')
        # Another damage report has its own claim work. The first review card
        # cannot end the run before that second customer's claim is filed.
        await db.raw.exceptions.insert_one({'id':'second-unfiled-damage','shipment_id':sid,
            'type':'damage','status':'in_progress'})
        await reconciliation.camt054_import_job({'payload':{'statement_id':statement['id']}})
        assert (await db.raw.demo_runs.find_one({'id':'damage-world'}))['phase'] == 'lifecycle'
        assert await db.raw.world_actions.find_one({'key':'claim:second-unfiled-damage'}) is None
        await db.raw.exceptions.delete_one({'id':'second-unfiled-damage'})
        await observer.finish_if_responsibility_ended(await db.raw.demo_runs.find_one({'id':'damage-world'}))
        finished = await db.raw.demo_runs.find_one({'id':'damage-world'})
        assert finished['status'] == 'done' and finished['completion_reason'] == 'claim_review_handoff'
        assert finished['claim_review_cards'][0]['approval_id'] == approval['id']
        final = await db.raw.demo_run_events.find_one({'run_id':'damage-world','kind':'phase','node':'payment'})
        assert final['title'] == 'Delivered and paid · damage claim with a person for review'
        assert final['data']['end_reason'] == 'claim_review_handoff'
        assert (await db.raw.claims.find_one({'id':claim['id']}))['status'] == 'in_review'
        assert (await db.raw.exceptions.find_one({'id':exception['id']}))['status'] == 'in_progress'
        invoice = await db.raw.invoices.find_one({'id':invoice['id']})
        assert invoice['status'] == 'paid'
        if os.environ.get('DEMO_CLAIM_PROOF_DIR'):
            directory = Path(os.environ['DEMO_CLAIM_PROOF_DIR'])
            directory.mkdir(parents=True,exist_ok=True)
            (directory/'inspection-report.pdf').write_bytes(content)
            (directory/'proof-of-delivery.pdf').write_bytes(pod_bytes)
            invoice_bytes,_,_ = await get_file(invoice['pdf_file_id'])
            (directory/'freight-invoice.pdf').write_bytes(invoice_bytes)
            (directory/'result.json').write_text(json.dumps({'shipment':shipment['reference'],'shipment_status':'delivered',
                'claim':claim['number'],'claim_status':'in_review','review_status':approval['status'],
                'legal_point':approval.get('legal_point'),'insurer_limit_cents':0,
                'exception_status':'in_progress','pod_file_present':True,'invoice_status':invoice['status'],
                'customer_api':True,'one_claim_after_retry':True,'freight_paid':True,
                'run_completion':finished['end_reason'],'run_event_title':final['title'],
                'event_end_reason':final['data']['end_reason']},indent=2))
    native.run(exercise())
