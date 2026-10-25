"""First deployment catches up once through the real queue, retaining the daily schedule."""
import asyncio
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient

from core import config, jobs
from core.tenant_visibility import VisibleDatabase
from services import reconciliation


def test_first_report_catchup_is_durable_and_respects_disable(monkeypatch):
    async def run():
        client = AsyncIOMotorClient(config.MONGO_URL)
        raw = client['huo_reconcile_startup_' + uuid4().hex]
        monkeypatch.setattr(jobs, 'db', VisibleDatabase(raw))
        monkeypatch.setattr(reconciliation, 'db', VisibleDatabase(raw))
        try:
            await raw.jobs.create_index('key', unique=True, sparse=True)
            await raw.job_schedules.insert_one({'name': 'payments.reconcile_0630', 'enabled': False,
                                                'next_run_at': '2026-10-25T04:30:00Z'})
            await reconciliation.ensure_initial_reconciliation()
            assert await raw.jobs.count_documents({}) == 0
            await raw.job_schedules.update_one({}, {'$set': {'enabled': True}})
            await reconciliation.ensure_initial_reconciliation()
            await reconciliation.ensure_initial_reconciliation()
            assert await raw.jobs.count_documents({}) == 1
            j = await raw.jobs.find_one({})
            assert j['kind'] == 'payments.reconcile_daily' and j['status'] == 'queued'
            assert (await raw.job_schedules.find_one({}))['next_run_at'] == '2026-10-25T04:30:00Z'
            await raw.jobs.delete_many({})
            await raw.owner_reports.insert_one({'kind': 'reconciliation', 'status': 'attention'})
            await reconciliation.ensure_initial_reconciliation()
            assert await raw.jobs.count_documents({}) == 0
        finally:
            await client.drop_database(raw.name)
            client.close()
    asyncio.run(run())
