"""Approval -> message.posted -> email relay with real Mongo/event bus.
Local log-provider evidence; no claim of external mailbox delivery.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

os.environ['DB_NAME'] = 'huo_s6_relay_' + uuid.uuid4().hex[:8]
os.environ['EMAIL_PROVIDER'] = 'log'
os.environ['EMAIL_CHANNEL_PROVIDER'] = 'log'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from core import config
if config.DB_NAME != os.environ['DB_NAME']:
    pytest.skip('Run this file in its own process', allow_module_level=True)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
from core.db import db
from core.settings import update_settings
from services import messaging
from services.email_channel import hooks, inbound, store
from routes.admin import decide, DecisionIn
from email.message import EmailMessage


def test_approved_ai_answer_is_emailed_with_thread_subject_and_approved_body():
    async def check():
        await update_settings({'autopilot_enabled': False, 'autopilot_halted': False})
        await db.companies.insert_one({'id': 'relay-company', 'name': 'Relay Test'})
        await db.users.insert_one({'id': 'relay-user', 'company_id': 'relay-company',
                                   'email': 'customer@example.com', 'channel': 'email', 'locale': 'fi'})
        await db.shipments.insert_one({'id': 'relay-shipment', 'company_id': 'relay-company',
                                       'reference': 'HUO-2026-000044'})
        raw = EmailMessage()
        raw['From'] = 'customer@example.com'
        raw['To'] = 'ops@example.com'
        raw['Message-ID'] = '<relay-question@example.com>'
        raw['Subject'] = 'Toimituskuittaus [HUO-2026-000044]'
        raw.set_content('Saisinko toimituskuittauksen?')
        received, _ = await inbound.ingest(raw.as_bytes(), source='test')
        et = await store.create_thread(received, language='fi')
        et = await store.update_thread(et['id'], company_id='relay-company', shipment_id='relay-shipment',
                                       ref='HUO-2026-000044')
        await db.email_messages.update_one({'id': received['id']}, {'$set': {'thread_id': et['id']}})
        thread = await messaging.get_or_create_thread(company_id='relay-company', shipment_id='relay-shipment',
                                                      subject='Toimituskuittaus', created_by=None)
        msg = await messaging.add_message(thread, body='Luonnos', author={'type':'ai', 'name':'Comms'}, hold_for_owner=True)
        assert msg['visibility'] == 'customer' and msg['status'] == 'pending_approval'
        assert await db.email_messages.count_documents({'tag': 'email_answer'}) == 0
        approved_body = 'Toimituskuittaus on pyydetty kuljetusliikkeeltä.'
        await decide(msg['approval_id'], DecisionIn(decision='approved', note='Checked', data={'body': approved_body}),
                     user={'id':'operator', 'name':'Operator', 'permissions':['messages.ops']})
        saved = await db.messages.find_one({'id': msg['id']})
        assert saved['status'] == 'sent' and saved['email_relayed_at']
        answer = await db.email_messages.find_one({'tag':'email_answer'})
        assert answer['status'] == 'logged' and approved_body in answer['text']
        assert answer['thread_id'] == et['id']
        assert answer['subject'] == 'Re: Toimituskuittaus [HUO-2026-000044]'
        assert answer['in_reply_to'] == '<relay-question@example.com>' or answer['in_reply_to'].startswith('<em-')
        assert await db.event_failures.count_documents({'handler':'relay'}) == 0
        await hooks.relay({'thread':thread, 'message':saved})
        assert await db.email_messages.count_documents({'tag':'email_answer'}) == 1
        await hooks.relay({'thread':thread, 'message':{**saved, 'email_relayed_at':None, 'visibility':'internal'}})
        assert await db.email_messages.count_documents({'tag':'email_answer'}) == 1
    loop.run_until_complete(check())
