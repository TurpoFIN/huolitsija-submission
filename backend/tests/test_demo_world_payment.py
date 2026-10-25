"""Native prepayment booking funded by an imported world-bank file, never a paid flag."""
import json
import os
from pathlib import Path

import pytest

import test_s2_prepayment as native

from core.db import db
from core.settings import update_settings
from core.storage import get_file
from services import prepayment, reconciliation
from services.demo_autoquote import clock
from services.demo_world import boundary, engine, observer


@pytest.fixture(autouse=True)
def setup_native_customer():
    native.setup_module(None)
    yield
    native.teardown_module(None)


def test_world_bank_file_pays_native_invoice_and_releases_prepayment_booking(monkeypatch):
    from routes.quotes import book
    from services import billing
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    # The controlled input is the bank's timing, not Huolitsija's reconciliation.
    async def bank_timing(*a, **kw):
        return {'delay_hours':1,'late_payment':False,'reason':'Controlled bank processing input'}
    if os.environ.get('DEMO_PAYMENT_LIVE_MODEL') != 'true':
        monkeypatch.setattr(engine.llm,'ask_json',bank_timing)
    q = native.quote('world-bank')

    async def exercise():
        await db.raw.demo_runs.insert_one({'id':'native-bank-run','phase':'quote','status':'running','seed':29,
            'quote_id':q['id'],'event_seq':0,'created_at':boundary._wall().isoformat()})
        await clock.reset_now('native-bank-run')
        await db.raw.demo_runs.update_one({'id':'native-bank-run'},{'$set':{'phase':'lifecycle','status':'lifecycle'}})
        await clock.set_clock('native-bank-run',speed=86400)
        await update_settings({'autopilot_enabled':True,'autopilot_halted':False})
        response = await book(native.body(q),native.request(),native.user())
        assert response['shipment_id'] is None
        invoice = await db.raw.invoices.find_one({'id':response['prepayment']['invoice_id']},{'_id':0})
        assert invoice['status'] == 'open' and invoice['pdf_file_id'] and invoice['origin'] == 'prepayment'
        pdf, filename, mime = await get_file(invoice['pdf_file_id'])
        assert pdf.startswith(b'%PDF')
        queued = await db.raw.jobs.find_one({'kind':'world.pay','payload.invoice_id':invoice['id']})
        assert queued
        paid_input = await engine.pay(queued)
        assert (await db.raw.invoices.find_one({'id':invoice['id']}))['status'] == 'open'
        await reconciliation.camt054_import_job({'payload':{'statement_id':paid_input['statement_id']}})
        paid = await db.raw.invoices.find_one({'id':invoice['id']})
        transaction = await db.raw.bank_transactions.find_one({'invoice_id':invoice['id']})
        assert paid['status'] == 'paid' and transaction['status'] == 'matched'
        assert transaction['amount_cents'] == invoice['total_cents']
        assert transaction['rf'] == invoice['rf_reference']
        booking_job = await db.raw.jobs.find_one({'kind':'prepayment.book','payload.invoice_id':invoice['id']})
        assert booking_job
        booked = await prepayment.book_after_payment(booking_job)
        shipment = await db.raw.shipments.find_one({'id':booked['shipment_id']})
        assert shipment['prepaid_invoice_id'] == invoice['id']
        assert (await billing.create_invoice_for_shipment(shipment['id'],None))['id'] == invoice['id']
        # Re-importing the same statement neither pays twice nor books twice.
        await reconciliation.camt054_import_job({'payload':{'statement_id':paid_input['statement_id']}})
        assert (await db.raw.invoices.find_one({'id':invoice['id']}))['paid_cents'] == invoice['total_cents']
        assert await db.raw.shipments.count_documents({'quote_id':q['id']}) == 1
        if os.environ.get('DEMO_PAYMENT_PROOF_DIR'):
            directory = Path(os.environ['DEMO_PAYMENT_PROOF_DIR'])
            directory.mkdir(parents=True,exist_ok=True)
            statement = await db.raw.bank_statements.find_one({'id':paid_input['statement_id']})
            xml, _, _ = await get_file(statement['file_id'])
            (directory/'prepayment-invoice.pdf').write_bytes(pdf)
            (directory/'bank-credit.xml').write_bytes(xml)
            call = await db.raw.world_llm_calls.find_one({'run_id':'native-bank-run','actor':'w:bank'},{'_id':0,'model':1,'output':1})
            (directory/'result.json').write_text(json.dumps({'invoice_number':invoice['number'],'amount_cents':invoice['total_cents'],
                'reference':invoice['reference'],'invoice_status':paid['status'],'bank_transaction_status':transaction['status'],
                'shipment_reference':shipment['reference'],'one_shipment':True,'one_invoice':True,'bank_persona':call,
                'method':'Native book → issued PDF → world bank camt.054 → native reconciliation → prepayment.book'},indent=2))
    native.run(exercise())
