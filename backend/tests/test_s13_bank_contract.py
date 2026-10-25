"""Bank debit -> payable settlement against a disposable real Mongo database, no bank submission."""
import os,uuid,asyncio
os.environ['DB_NAME']='huo_s13_bank_'+uuid.uuid4().hex[:8]
import pytest
from core import config
if config.DB_NAME!=os.environ['DB_NAME']:pytest.skip('Run finance bank contract in its own process',allow_module_level=True)
from core.db import db
from services import payables,reconciliation
from core.util import new_id

def test_real_mongo_bank_debit_exact_match_and_replay():
    async def exercise():
        pid=new_id();rid=new_id();cid=new_id();eid='AP'+uuid.uuid4().hex[:30]
        await db.payables.insert_one({'id':pid,'carrier_invoice_id':cid,'invoice_number':'TEST-AP-SETTLEMENT','status':'accepted',
            'payment_run_id':rid,'end_to_end_id':eid,'amount_cents':276100,'eur_cents':276100,'currency':'EUR'})
        await db.payment_runs.insert_one({'id':rid,'status':'accepted'})
        await db.carrier_invoices.insert_one({'id':cid,'status':'approved'})
        assert await payables.match_bank_debit(eid,276099,'EUR','2026-10-24') is None
        assert await payables.match_bank_debit(eid,276100,'USD','2026-10-24') is None
        tx={'id':new_id(),'end_to_end_id':eid,'amount_cents':276100,'currency':'EUR','booking_date':'2026-10-24','entry_status':'BOOK'}
        await db.bank_transactions.insert_one(dict(tx))
        assert (await reconciliation._match_debit(tx))['status']=='matched'
        got=await db.payables.find_one({'id':pid})
        assert got['status']=='paid' and got['settled_eur_cents']==276100 and got['realised_fx_cents'] in (0,None)
        assert (await db.carrier_invoices.find_one({'id':cid}))['status']=='paid'
        assert (await reconciliation._match_debit(tx))['status']=='matched'
        assert await db.payables.count_documents({'id':pid})==1
    from core.db import client
    client.get_io_loop().run_until_complete(exercise())


def test_unverified_upload_response_holds_payables_and_blocks_resend(monkeypatch):
    from services import bank_api
    from core.db import client
    from unittest.mock import AsyncMock
    monkeypatch.setenv('BANK_PAYMENT_MODE', 'api')
    rejected = AsyncMock(side_effect=bank_api.BankApiError('Signature invalid', code='bank_api_response_signature'))
    monkeypatch.setattr(bank_api, 'submit', rejected)
    monkeypatch.setattr(payables, 'get_file', AsyncMock(return_value=(b'<Document/>', 'test.xml', 'application/xml')))
    monkeypatch.setattr(payables, 'request_approval', AsyncMock(return_value={'id': 'test-card'}))
    async def exercise():
        rid, pid = new_id(), new_id()
        run = {'id':rid,'number':'TEST-HOLD','status':'approved','file_id':'local-test','bank':{},'eur_total_cents':100}
        await db.payment_runs.insert_one(dict(run))
        await db.payables.insert_one({'id':pid,'status':'scheduled','payment_run_id':rid})
        with pytest.raises(bank_api.BankApiError):await payables.send(run)
        held = await db.payment_runs.find_one({'id':rid})
        assert held['bank']['reconciliation_required'] is True and held['status']=='approved'
        with pytest.raises(payables.PaymentError) as err:await payables.send(run)  # deliberately stale input
        assert err.value.code=='bank_reconciliation_required'
        assert rejected.await_count==1
        await payables.reject_run(rid,None,allow_approved=True)
        assert (await db.payment_runs.find_one({'id':rid}))['status']=='approved'
        assert (await db.payables.find_one({'id':pid}))['status']=='scheduled'
        assert payables.request_approval.call_args.kwargs['data']['purpose']=='bank_response_unverified'
    client.get_io_loop().run_until_complete(exercise())
