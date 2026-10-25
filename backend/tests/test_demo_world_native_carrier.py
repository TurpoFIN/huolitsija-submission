"""Native procurement, quote, customer booking and carrier guard on a single road leg."""
import os
from datetime import timedelta
import pytest
import test_s2_prepayment as native
from core.db import db
from core.settings import update_settings
from core.util import now, now_iso
from services import geo, carrier_booking, partner_network as pn
from services.agents import booking_agent, carrier_agent
from services.demo_autoquote import driver, clock
from services.demo_world import boundary

@pytest.fixture(autouse=True)
def customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


@pytest.mark.parametrize('simulated',[False,True])
def test_real_procurement_quote_and_booking_keep_supplier_contract(monkeypatch,simulated):
    from routes.quotes import book
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    # Exercise the permissive provider configuration: the LAST world boundary,
    # rather than an earlier disabled transport, must contain the acceptance.
    from core import email
    from email.utils import getaddresses
    provider_calls = []
    monkeypatch.setattr(email,'_smtp_send',lambda msg,smtp:provider_calls.extend(
        address for _,address in getaddresses(msg.get_all('To',[]))))
    monkeypatch.setattr(email.config,'EMAIL_PROVIDER','smtp')
    monkeypatch.setenv('SMTP_HOST','intercepted.invalid')
    monkeypatch.setenv('PARTNER_EMAIL_PROVIDER','smtp')
    monkeypatch.setenv('PARTNER_SANDBOX_DOMAIN','')
    monkeypatch.setenv('PARTNER_REPLY_TO','owned@demo.invalid')
    monkeypatch.setenv('PARTNER_OUTREACH_LIVE','true')
    monkeypatch.setenv('PARTNER_SINGLE_RFQ_ONLY','false')
    async def extracted_reply(text,**kw):
        return {'intent':'offer','status':'ok','cost_cents':73000,'transit_days':4,
            'valid_to':(now()+timedelta(days=30)).date().isoformat(),'method':'controlled-parser-facts'}
    monkeypatch.setattr(pn,'parse_reply',extracted_reply)
    monkeypatch.setattr(boundary,'_PROCUREMENT_DATABASE',db.raw.name)
    async def customer_context(run_id):
        return {},native.user(),await db.raw.companies.find_one({'id':native.CID},{'_id':0})
    monkeypatch.setattr(driver,'_ensure_thread',customer_context)
    async def exercise():
        await update_settings({'partner_network_enabled':False,'autopilot_enabled':True,'autopilot_halted':False,
            'partner_approved_lanes':['road:FI>SE'],'partner_max_price_cents':180000})
        await db.raw.companies.update_one({'id':native.CID},{'$set':{'credit_limit_cents':500000,'test_tenant':False,'test':False}})
        await db.raw.demo_runs.insert_one({'id':'native-carrier','phase':'quote','status':'running','event_seq':0})
        await clock.reset_now('native-carrier')
        origin,destination = geo.lookup_code('FIOUL'),geo.lookup_code('SELLA')
        req = {'origin':origin,'destination':destination,'mode':'road',
            'ready_date':(now()+timedelta(days=7)).date().isoformat(),'direction':'export',
            'incoterm':'DAP','incoterm_place':'Luleå','customs_clearance':False,
            'cargo':{'load_type':'LTL','packages':2,'package_type':'pallet_eur','weight_kg':400,
                'volume_m3':1.92,'description':'Controlled steel parts','dangerous_goods':False}}
        row = {'id':'researched-rfq','ref':'RFQ-TEST01','lane':'road:FI>SE','mode':'road',
            'carrier_name':'Controlled Transport Oy','domain':'controlled.invalid','country':'FI',
            'contact_email':'dispatch@controlled.invalid','contact_source_url':'https://controlled.invalid/contact',
            'sources':[],'service':'DOOR_TO_DOOR_ROAD','basis':'PER_SHIPMENT','sell_cents':610000,
            'origin':'FIOUL','destination':'SELLA','rounds':0,'log':[],'status':'demo_reply_received',
            'single_rfq_only':False,'verification_only':False,'test_mailbox':False,'demo_run_id':'native-carrier'}
        await db.raw.partner_outreach.insert_one(dict(row))
        pick = {'outreach_id':row['id'],'partner':row['carrier_name'],'cost_cents':73000,
            'transit_days':4,'valid_to':(now()+timedelta(days=30)).date().isoformat(),
            'message_id':'<controlled-offer@controlled.invalid>','simulated':simulated}
        if not simulated:
            received = await pn.ingest_message({'message_id':pick['message_id'],'from':row['contact_email'],
                'to':'owned@demo.invalid','subject':'Re: RFQ-TEST01','source':'postmark',
                'text':'EUR730 all in, four days, valid through ' + pick['valid_to']})
            assert received['status'] == 'reply_received'
            assert await db.raw.rate_cards.count_documents({'outreach_id':row['id']}) == 0
        priced = {'basis':'partner_offer'}
        assert not await pn.is_on()
        assert not await pn.is_on(outreach=row)
        q, _ = await driver._store_quote('native-carrier',req,'road',priced,pick,'Controlled native supplier proof')
        option = q['options'][0]
        assert not await pn.is_on(outreach=row)
        card = await db.raw.rate_cards.find_one({'id':option['rate_card_id']})
        assert card['status'] == 'accepted' and card['carrier_id'] == option['carrier_id']
        assert card['all_in_cost_cents'] == pick['cost_cents'] and not card['archived']
        acceptance = await db.raw.email_outbox.find_one({'tag':'partner_outreach','ref.id':row['id'],'to':row['contact_email']})
        assert acceptance and acceptance['provider'] == 'world'
        assert row['contact_email'] not in provider_calls
        assert option['bookable'] and option['firmness'] == 'firm' and option['white_label']
        assert option['legs'][0]['carrier_id'] == card['carrier_id']
        assert option['legs'][0]['rate_card_id'] == card['id']
        freight = [ln for ln in option['lines'] if ln.get('stage') in carrier_booking.FREIGHT_STAGES]
        assert freight and all(ln.get('leg_seq') is not None for ln in freight)
        assert sum(ln['cost_cents'] for ln in freight) == 73000
        assert priced['lines'] == option['lines'] and priced['sell_cents'] == option['total_cents']
        # The offer is released only by production's agent path: its quote_threshold card, decided by the quote agent
        # (one audited auto decision) on the native option itself, before any offer e-mail.
        from core.settings import get_settings
        from routes.quotes import FREIGHT_LINE_CODES, approval_reasons, charges_freight
        assert charges_freight(option) and approval_reasons(q, await get_settings()) == []
        released = await driver._await_release('native-carrier', q)
        info = released['send_decision']
        assert released['status'] == 'sent' and info['card_status'] == 'approved'
        assert info['decided_by_id'] == 'agent:quote_agent' and info['decision_outcome'] == 'auto'
        # Native card decision and delivery re-gate each leave one audit row.
        decisions = await db.raw.agent_decisions.find({'agent':'quote_agent','action':'send_quote',
            'subject_id':q['id']}).sort('at', -1).to_list(10)
        assert len(decisions) == 2 and all(d['outcome'] == 'auto' for d in decisions)
        assert decisions[0]['id'] == info['decision_id']
        approval = await db.raw.approvals.find_one({'id':info['approval_id']})
        assert approval['status'] == 'approved' and approval['decided_by']['id'] == 'agent:quote_agent'
        assert not await db.raw.email_outbox.find_one({'tag':'email_quote'})  # the driver mails only after release
        monkeypatch.setattr(driver.conf,'approval_wait_s',lambda:0)

        async def held_quote(qid,lines,total):
            held = {**q,'id':qid,'number':qid,'status':'draft','approval':{},
                    'options':[{**option,'lines':lines,'total_cents':total,'subtotal_cents':total}]}
            await db.raw.quotes.insert_one(dict(held))
            await db.raw.demo_runs.update_one({'id':'native-carrier'},{'$set':{'status':'running'}})
            return await driver._send_gate('native-carrier',held)

        # The reviewer's probe: a valid native ROAD_FREIGHT + DOC_FEE option of EUR 325 with Autopilot off must wait for a
        # person on its card, and nothing is e-mailed.
        await update_settings({'autopilot_enabled':False})
        lines = [{**ln,'code':'ROAD_FREIGHT'} for ln in option['lines'] if ln.get('code') in FREIGHT_LINE_CODES][:1] + \
            [{'code':'DOC_FEE','description':'Documentation','qty':1,'unit_price_cents':3500,'amount_cents':3500,
              'unit_cost_cents':0,'cost_cents':0,'stage':'origin'}]
        lines[0] = {**lines[0],'amount_cents':29000,'unit_price_cents':29000}
        off = await held_quote('Q-PROBE-OFF',lines,32500)
        assert off['status'] == 'pending_approval'
        assert await db.raw.approvals.find_one({'kind':'quote_threshold','ref_id':off['id'],'status':'pending'})
        assert not await db.raw.agent_decisions.find_one({'agent':'quote_agent','subject_id':off['id'],'outcome':'auto'})
        assert await driver._await_release('native-carrier',off) is None
        assert (await db.raw.demo_runs.find_one({'id':'native-carrier'}))['status'] == 'awaiting_approval'
        assert not await db.raw.email_outbox.find_one({'tag':'email_quote'})
        await update_settings({'autopilot_enabled':True})
        # A3's and B's hand-built option (no native freight code) must wait for a person, Autopilot on or not.
        legacy = await held_quote('Q-LEGACY',[{**ln,'code':'FREIGHT'} if ln.get('code') in FREIGHT_LINE_CODES else ln
                                              for ln in option['lines']],option['total_cents'])
        assert legacy['status'] == 'pending_approval'
        assert not await db.raw.agent_decisions.find_one({'agent':'quote_agent','subject_id':legacy['id'],'outcome':'auto'})
        assert await driver._await_release('native-carrier',legacy) is None
        await db.raw.demo_runs.update_one({'id':'native-carrier'},{'$set':{
            'phase':'lifecycle','status':'lifecycle','quote_id':q['id']}})
        booking = native.body(q)
        booking.consignee.address['city'] = 'Luleå'
        booking.consignee.address['country'] = 'SE'
        result = await book(booking,native.request(),native.user())
        sid = result['shipment_id']
        await booking_agent.confirm_booking({'payload':{'shipment_id':sid}})
        shipment = await db.raw.shipments.find_one({'id':sid})
        assert len(shipment['legs']) == 1 and shipment['legs'][0]['mode'] == 'road'
        ranking = await carrier_booking.rank_carriers(shipment,shipment['legs'][0])
        assert ranking['choice']['carrier_id'] == card['carrier_id']
        assert ranking['choice']['lane_ok'] and ranking['choice']['eligible']
        assert ranking['choice']['cost_cents'] == 73000 and ranking['confidence'] == .95
        result = await carrier_agent.book_one_leg(shipment,shipment['legs'][0],reason='controlled native RFQ proof')
        assert result['status'] in ('requested','confirmed'), result
        cb = await db.raw.carrier_bookings.find_one({'id':result['booking_id']})
        assert cb['carrier_id'] == card['carrier_id'] and cb['cost_cents'] == 73000
        assert cb['adapter'] == 'log' and not cb['sandbox']
        booking_mail = await db.raw.email_outbox.find_one({'tag':'carrier_booking','to':row['contact_email']})
        assert booking_mail and booking_mail['provider'] == 'world'
        assert row['contact_email'] not in provider_calls
        import json
        from pathlib import Path
        if os.environ.get('DEMO_CARRIER_PROOF_DIR'):
            directory = Path(os.environ['DEMO_CARRIER_PROOF_DIR'])
            directory.mkdir(parents=True,exist_ok=True)
            (directory/('simulated.json' if simulated else 'real-reply.json')).write_text(json.dumps({
                'reply_simulated':simulated,'background_autopilot':await pn.is_on(),
                'native_card_status':card['status'],'native_firmness':option['firmness'],
                'supplier_link_preserved':True,'purchase_cost_cents':cb['cost_cents'],
                'leg_count':len(shipment['legs']),'lane_ok':ranking['choice']['lane_ok'],
                'confidence':ranking['confidence'],'native_booking_status':cb['status'],
                'acceptance_provider':acceptance['provider'],'booking_provider':booking_mail['provider'],
                'real_partner_provider_send_count':provider_calls.count(row['contact_email'])},indent=2))
        assert not await db.raw.approvals.find_one({'ref_id':sid,'kind':'carrier_booking','status':'pending'})
        # A retry reuses the accepted card, not a second supplier or fabricated quote.
        again = await driver._promote_partner_rate(req,'road',pick)
        assert again['id'] == card['id']
        assert await db.raw.rate_cards.count_documents({'outreach_id':row['id']}) == 1
    native.run(exercise())


def test_selected_scope_rejects_background_other_database_other_run_and_halt(monkeypatch):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    async def exercise():
        await update_settings({'partner_network_enabled':True,'autopilot_halted':False})
        await db.raw.demo_runs.insert_one({'id':'scoped','phase':'quote','status':'running'})
        row = {'id':'chosen','demo_run_id':'scoped'}
        assert not await pn.is_on(outreach=row)
        async with boundary.selected_partner_offer('scoped','chosen'):
            # Actual test DB is not the approved demo DB.
            assert not await pn.is_on(outreach=row)
            monkeypatch.setattr(boundary,'_PROCUREMENT_DATABASE',db.raw.name)
            assert not await pn.is_on()
            assert not await pn.is_on(outreach={**row,'id':'not-chosen'})
            assert not await pn.is_on(outreach={**row,'demo_run_id':'other-run'})
            assert await pn.is_on(outreach=row)
            await update_settings({'autopilot_halted':True})
            assert not await pn.is_on(outreach=row)
            await update_settings({'autopilot_halted':False})
            await db.raw.demo_runs.update_one({'id':'scoped'},{'$set':{'status':'stopped'}})
            assert not await pn.is_on(outreach=row)
        monkeypatch.setenv('DEMO_WORLD_SANDBOX','false')
        assert await pn.is_on()
    native.run(exercise())
