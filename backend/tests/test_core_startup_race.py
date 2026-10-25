"""Two server processes starting together (765ad42 deploy, 2026-10-24): startup hooks run one process at a time
(core/lifecycle.py), and services/data_repair_r3b.py removes what the double run left. Runs the real functions against
a throwaway Mongo database."""
import asyncio
import time
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

from core import config, lifecycle
from services import data_repair_r3b as r3b


def _run(monkeypatch, body):
    async def run():
        client = AsyncIOMotorClient(config.MONGO_URL, tz_aware=True)
        db = client[f"huo_test_startup_race_{uuid.uuid4().hex[:10]}"]
        monkeypatch.setattr(lifecycle, "db", db)
        monkeypatch.setattr(r3b, "db", db)
        try:
            await body(db)
        finally:
            await client.drop_database(db.name)
            client.close()

    asyncio.run(run())


def test_two_processes_run_the_startup_hooks_one_at_a_time(monkeypatch):
    running, seen, work = [0], [0], []

    async def once_marker_hook():  # the check-then-insert shape every one-time repair uses
        running[0] += 1
        seen[0] = max(seen[0], running[0])
        if not await lifecycle.db.system.find_one({"key": "test-once:1"}):
            await asyncio.sleep(0.2)
            work.append("ran")
            await lifecycle.db.system.insert_one({"key": "test-once:1"})
        running[0] -= 1

    monkeypatch.setattr(lifecycle, "_startup", [(1, once_marker_hook)])

    async def body(db):
        await asyncio.gather(lifecycle.run_startup(), lifecycle.run_startup())
        assert seen[0] == 1 and work == ["ran"]
        assert await db.system.find_one({"_id": lifecycle.STARTUP_LOCK}) is None  # released

    _run(monkeypatch, body)


def test_a_lock_left_by_a_crashed_process_expires(monkeypatch):
    async def body(db):
        await db.system.insert_one({"_id": lifecycle.STARTUP_LOCK, "owner": "gone", "expires": time.time() - 1})
        assert await asyncio.wait_for(lifecycle.acquire_startup_lock("next", poll=0.05), 2)
        held = await db.system.find_one({"_id": lifecycle.STARTUP_LOCK})
        assert held["owner"] == "next"
        assert not await _acquired_within(lifecycle.acquire_startup_lock("third", poll=0.05), 0.3)  # held, not expired

    _run(monkeypatch, body)


async def _acquired_within(coro, seconds):
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        return False


def _approval(id_, kind, ref_id, created_at, title="t"):
    return {"id": id_, "kind": kind, "ref_id": ref_id, "status": "pending", "created_at": created_at, "title": title}


def test_the_approval_the_quote_points_to_stays_and_its_copy_goes(monkeypatch):
    async def body(db):
        await db.quotes.insert_one({"id": "q1", "approval": {"approval_id": "b", "status": "pending"}})
        await db.approvals.insert_many([
            _approval("a", "quote_threshold", "q1", "2026-10-24T17:21:04Z"),
            _approval("b", "quote_threshold", "q1", "2026-10-24T17:21:04Z"),
            _approval("c", "claim", "cl1", "2026-10-24T09:00:00Z"),
            _approval("d", "claim", "cl1", "2026-10-24T09:00:01Z"),
            _approval("e", "quote_threshold", "q2", "2026-10-24T17:21:02Z"),
            {**_approval("f", "quote_threshold", "q1", "2026-10-24T10:00:00Z"), "status": "approved"},
        ])
        removed = await r3b.dedupe_pending_approvals()
        left = sorted([a["id"] async for a in db.approvals.find({}, {"_id": 0, "id": 1})])
        assert left == ["b", "c", "e", "f"] and len(removed) == 2

    _run(monkeypatch, body)


def test_a_notification_repeated_within_seconds_is_a_copy(monkeypatch):
    def n(id_, at, user="u1", title="Quote to review: Q-1"):
        return {"id": id_, "user_id": user, "title": title, "body": "b", "link": "/ops/quotes/1", "kind": "approval", "created_at": at}

    async def body(db):
        await db.notifications.insert_many([
            n("1", "2026-10-24T17:21:04Z"), n("2", "2026-10-24T17:21:05Z"),  # race copy
            n("3", "2026-10-24T17:21:05Z", user="u2"),  # another person
            n("4", "2026-10-25T08:00:00Z"),  # the same text a day later is a new event
        ])
        assert await r3b.dedupe_notifications() == 1
        assert sorted([x["id"] async for x in db.notifications.find({}, {"_id": 0, "id": 1})]) == ["1", "3", "4"]

    _run(monkeypatch, body)


def test_the_second_copy_of_the_demo_replacement_quote_goes(monkeypatch):
    opts = [{"carrier_name": "Finnlines", "service": "LCL", "total_cents": 120000}]

    def q(id_, number, at, **kw):
        return {"id": id_, "number": number, "demo": True, "status": "sent", "shipment_id": None, "company_id": "k",
                "options": opts, "created_at": at, **kw}

    async def body(db):
        await db.quotes.insert_many([
            q("2", "Q-2026-000064", "2026-10-24T17:21:05Z"), q("1", "Q-2026-000063", "2026-10-24T17:21:05Z"),
            q("3", "Q-2026-000010", "2026-10-20T08:00:00Z"),  # an older demo quote with the same price stays
            q("4", "Q-2026-000065", "2026-10-24T17:21:05Z", company_id="other"),
            q("5", "Q-2026-000066", "2026-10-24T17:21:05Z", demo=False),
        ])
        assert await r3b.dedupe_demo_replacement_quotes() == ["Q-2026-000064"]
        assert await db.quotes.count_documents({}) == 4

    _run(monkeypatch, body)


def test_document_use_follows_the_declaration_status(monkeypatch):
    async def body(db):
        await db.customs_declarations.insert_many([{"id": "k6", "status": "draft"}, {"id": "other", "status": "released"}])
        await db.documents.insert_many([
            {"id": "d1", "shipment_id": "s6", "used_by": [{"declaration_id": "k6", "status": "released"}]},
            {"id": "d2", "shipment_id": "s6", "used_by": [{"declaration_id": "gone", "status": "draft"},
                                                         {"declaration_id": "other", "status": "released"}]},
            {"id": "d3", "shipment_id": "s7", "used_by": []},
        ])
        assert await r3b.resync_document_use({"shipment_id": "s6"}) == 2
        d1 = await db.documents.find_one({"id": "d1"})
        d2 = await db.documents.find_one({"id": "d2"})
        assert d1["used_by"] == [{"declaration_id": "k6", "status": "draft"}]
        assert d2["used_by"] == [{"declaration_id": "other", "status": "released"}]
        assert await r3b.resync_document_use({"shipment_id": "s6"}) == 0

    _run(monkeypatch, body)
