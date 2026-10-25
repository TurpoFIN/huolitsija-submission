"""System notices to an e-mail-channel customer never join an unrelated inbound conversation.
Live 25 Oct: TEST DG-declaration notice ec982ea0 (tradedoc_dg_declaration) became the only reply in the customer's
open thread 0d6ba1fd, which was about something else. A notice joins a thread only when it names that thread or the
thread is the one about the same shipment / quote; otherwise it goes out on its own. Real Mongo, log provider."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

os.environ['DB_NAME'] = 'huo_s4_notice_' + uuid.uuid4().hex[:8]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['EMAIL_CHANNEL_PROVIDER'] = 'log'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run this file in its own process', allow_module_level=True)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
from email.message import EmailMessage
from core.db import db
from core.email import send_email
from services import messaging
from services.email_channel import hooks, inbound, store

MAIL = 'notice-customer@example.com'


async def _setup():
    await db.companies.insert_one({'id': 'nt-co', 'name': 'Notice Test'})
    await db.users.insert_one({'id': 'nt-user', 'company_id': 'nt-co', 'email': MAIL, 'channel': 'email', 'locale': 'fi'})
    await db.shipments.insert_one({'id': 'nt-ship-a', 'company_id': 'nt-co', 'reference': 'HUO-2026-000901'})
    await db.shipments.insert_one({'id': 'nt-ship-b', 'company_id': 'nt-co', 'reference': 'HUO-2026-000902'})
    raw = EmailMessage()
    raw['From'], raw['To'], raw['Message-ID'] = MAIL, 'ops@example.com', '<nt-question@example.com>'
    raw['Subject'] = 'Kysymys toimituksesta [HUO-2026-000901]'
    raw.set_content('Milloin lähetys saapuu?')
    received, _ = await inbound.ingest(raw.as_bytes(), source='test')
    et = await store.create_thread(received, language='fi')
    et = await store.update_thread(et['id'], company_id='nt-co', shipment_id='nt-ship-a', ref='HUO-2026-000901')
    await db.email_messages.update_one({'id': received['id']}, {'$set': {'thread_id': et['id']}})
    return et


async def _rows(tag):
    return await db.email_messages.find({'tag': tag}, {'_id': 0}).to_list(10)


def test_notices_stay_out_of_unrelated_threads_but_join_their_own():
    async def check():
        et = await _setup()
        # DG notice for another shipment: not in the open thread, sent on its own.
        rec = await send_email(MAIL, 'DG-ilmoitus HUO-2026-000902', '<div>DG</div>', 'DG', tag='tradedoc_dg_declaration',
                               company_id='nt-co', ref={'type': 'trade_document', 'id': 'doc-dg', 'shipment_id': 'nt-ship-b'})
        assert not any(r.get('thread_id') == et['id'] for r in await _rows('tradedoc_dg_declaration'))
        assert 'email_thread_id' not in (rec.get('ref') or {})
        # Invoice and tracking-style notice without any shipment link: likewise unthreaded.
        await send_email(MAIL, 'Lasku', '<div>L</div>', 'L', tag='invoice_issued', company_id='nt-co',
                         ref={'type': 'invoice', 'id': 'no-such-invoice'})
        await send_email(MAIL, 'Seuranta', '<div>S</div>', 'S', tag='tracking_update', company_id='nt-co', ref={})
        for t in ('invoice_issued', 'tracking_update'):
            assert not any(r.get('thread_id') == et['id'] for r in await _rows(t)), t
        assert await db.email_messages.count_documents({'thread_id': et['id'], 'direction': 'out'}) == 0
        # The same notice for the thread's own shipment, and a mail naming the thread, do join it.
        await send_email(MAIL, 'DG-ilmoitus HUO-2026-000901', '<div>DG</div>', 'DG', tag='tradedoc_dg_declaration',
                         company_id='nt-co', ref={'type': 'trade_document', 'id': 'doc-dg-a', 'shipment_id': 'nt-ship-a'})
        await send_email(MAIL, 'Vastaus', '<div>V</div>', 'V', tag='general', company_id='nt-co',
                         ref={'type': 'email_thread', 'id': et['id']})
        assert await db.email_messages.count_documents({'thread_id': et['id'], 'direction': 'out'}) == 2
    loop.run_until_complete(check())


def test_status_update_for_unlinked_shipment_is_not_relayed_into_newest_thread():
    async def check():
        et = await db.email_threads.find_one({'customer_email': MAIL}, {'_id': 0})
        before = await db.email_messages.count_documents({'thread_id': et['id']})
        thread = await messaging.get_or_create_thread(company_id='nt-co', shipment_id='nt-ship-b', subject='Seuranta',
                                                      created_by=None)
        msg = {'id': 'nt-upd', 'visibility': 'customer', 'status': 'sent', 'kind': 'update', 'author': {'type': 'system'},
               'body': 'HUO-2026-000902 on lastattu.'}
        await hooks.relay({'thread': thread, 'message': msg})
        assert await db.email_messages.count_documents({'thread_id': et['id']}) == before
        assert await db.email_messages.count_documents({'tag': 'email_update'}) == 0
    loop.run_until_complete(check())


def test_prepayment_invoice_and_receipt_go_to_the_quote_thread_with_pay_link(monkeypatch):
    """Prepayment invoice has quote_id but no shipment until paid: it (and its receipt) joins the quote's own thread
    and carries the Stripe pay link, while the unrelated open thread stays untouched."""
    from core import config as cfg
    monkeypatch.setattr(cfg, 'STRIPE_API_KEY', 'test-key-not-used')
    async def check():
        other = await db.email_threads.find_one({'customer_email': MAIL}, {'_id': 0})
        raw = EmailMessage()
        raw['From'], raw['To'], raw['Message-ID'] = MAIL, 'ops@example.com', '<nt-quote@example.com>'
        raw['Subject'] = 'Tarjouspyyntö'
        raw.set_content('Hinta Tukholmaan?')
        received, _ = await inbound.ingest(raw.as_bytes(), source='test')
        qt = await store.create_thread(received, language='fi')
        qt = await store.update_thread(qt['id'], company_id='nt-co', quote_id='nt-quote', ref='Q-2026-000901')
        await db.email_threads.update_one({'id': other['id']}, {'$set': {'updated_at': '2099-01-01T00:00:00Z'}})
        await db.invoices.insert_one({'id': 'nt-inv', 'number': 'INV-NT-1', 'company_id': 'nt-co', 'quote_id': 'nt-quote',
                                      'status': 'open', 'kind': 'prepayment'})
        before = await db.email_messages.count_documents({'thread_id': other['id']})
        await send_email(MAIL, 'Ennakkolasku INV-NT-1', '<div>Lasku</div>', 'Lasku', tag='invoice_issued', company_id='nt-co',
                         ref={'type': 'invoice', 'id': 'nt-inv'})
        row = await db.email_messages.find_one({'tag': 'invoice_issued', 'thread_id': qt['id']}, {'_id': 0})
        assert row and '/api/email-channel/pay/' in row['text']
        await send_email(MAIL, 'Kuitti INV-NT-1', '<div>Kuitti</div>', 'Kuitti', tag='invoice_paid', company_id='nt-co',
                         ref={'type': 'invoice', 'id': 'nt-inv'})
        assert await db.email_messages.count_documents({'tag': 'invoice_paid', 'thread_id': qt['id']}) == 1
        assert await db.email_messages.count_documents({'thread_id': other['id']}) == before
    loop.run_until_complete(check())


def test_quote_only_thread_gets_no_shipment_notices_and_no_quote_tag():
    """Live 25 Oct 04:05-04:15, thread 2ba8e10d (quote Q-2026-000113, no shipment): HAWB, House B/L, MBL draft and two
    status updates for another shipment landed in it, subjects tagged [Q-2026-000113]. The quote thread from the
    prepayment test above has the same shape (quote_id, ref Q-2026-000901, no shipment)."""
    async def check():
        qt = await db.email_threads.find_one({'quote_id': 'nt-quote'}, {'_id': 0})
        assert qt and not qt.get('shipment_id')
        await db.email_threads.update_one({'id': qt['id']}, {'$set': {'updated_at': '2099-12-31T00:00:00Z'}})  # newest
        before = await db.email_messages.count_documents({'thread_id': qt['id']})
        for kind, no in (('hawb', 'HUOA26000902'), ('house_bl', 'HUOB26000902'), ('master_bl', 'MBLD-2026-000902')):
            await send_email(MAIL, f'[TEST] {kind} {no} · HUO-2026-000902', '<div>doc</div>', 'doc', tag=f'tradedoc_{kind}',
                             company_id='nt-co', ref={'type': 'trade_document', 'id': f'doc-{kind}', 'doc_no': no, 'version': 1,
                                                           'shipment_id': 'nt-ship-b'})
        await send_email(MAIL, '[TEST] HUO-2026-000902: tilannepäivitys', '<div>upd</div>', 'upd', tag='message', company_id='nt-co')
        thread = await messaging.get_or_create_thread(company_id='nt-co', shipment_id='nt-ship-b', subject='Seuranta', created_by=None)
        await hooks.relay({'thread': thread, 'message': {'id': 'nt-upd-2', 'visibility': 'customer', 'status': 'sent',
                                                         'kind': 'update', 'author': {'type': 'system'}, 'body': 'Lähtenyt.'}})
        assert await db.email_messages.count_documents({'thread_id': qt['id']}) == before
        assert await db.email_messages.count_documents({'subject': {'$regex': r'\[Q-2026-000901\]'},
                                                        'tag': {'$regex': '^(tradedoc_|message$|email_update$)'}}) == 0
    loop.run_until_complete(check())
