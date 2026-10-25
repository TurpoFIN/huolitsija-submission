"""s3 r2: a 202 create whose process died (restart, out-of-memory kill) does not stay "processing": once its beat stops,
the sweep marks it failed, retryable, with a note to look the record up first. Live requests keep beating and are
never touched. Own throw-away MongoDB database."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s3_bounded_{uuid.uuid4().hex[:8]}"
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s3_bounded_orphans.py in its own pytest process", allow_module_level=True)

from core import bounded  # noqa: E402
from core.db import db  # noqa: E402
from core.util import iso, now  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def database():
    yield
    run(db.raw.client.drop_database(os.environ["DB_NAME"]))


def test_a_slow_create_beats_while_it_runs_and_finishes_normally():
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return {"id": "s1", "reference": "HUO-T-1"}

    async def scenario():
        resp = await bounded.bounded("shipment.create", slow(), user={"id": "u1"}, budget_s=0.05)
        rid = __import__("json").loads(resp.body)["request_id"]
        row = await db.raw.slow_requests.find_one({"id": rid}, {"_id": 0})
        assert resp.status_code == 202 and row["status"] == "processing" and row["beat_at"]
        await db.raw.slow_requests.update_one({"id": rid}, {"$set": {"beat_at": iso(now() - timedelta(minutes=5))}})
        await bounded.beat()  # this process still runs it: the beat is fresh again and the sweep leaves it alone
        assert await bounded.sweep_orphans() == 0
        release.set()
        for _ in range(50):
            await asyncio.sleep(0.02)
            row = await db.raw.slow_requests.find_one({"id": rid}, {"_id": 0})
            if row["status"] != "processing":
                break
        return row

    row = run(scenario())
    assert row["status"] == "done" and row["response"]["reference"] == "HUO-T-1"


def test_a_request_whose_process_died_is_marked_failed_and_retryable():
    t = now()
    rows = [  # the shape bounded() writes; the process that ran them is gone (no task here)
        {"id": "dead-beat", "label": "shipment.create", "status": "processing", "started_at": iso(t - timedelta(minutes=3)),
         "beat_at": iso(t - timedelta(minutes=2)), "user_id": "u1", "expires_at": t + timedelta(days=2)},
        {"id": "legacy", "label": "shipment.create", "status": "processing", "started_at": iso(t - timedelta(minutes=12)),
         "user_id": "u1", "expires_at": t + timedelta(days=2)},  # written before beats existed (live 2a64d703)
        {"id": "fresh", "label": "quote.create", "status": "processing", "started_at": iso(t), "beat_at": iso(t),
         "user_id": "u1", "expires_at": t + timedelta(days=2)},
        {"id": "legacy-young", "label": "quote.create", "status": "processing", "started_at": iso(t - timedelta(minutes=2)),
         "user_id": "u1", "expires_at": t + timedelta(days=2)}]
    run(db.raw.slow_requests.insert_many([dict(r) for r in rows]))
    assert run(bounded.sweep_orphans()) == 2
    got = {r["id"]: r for r in run(db.raw.slow_requests.find({}, {"_id": 0}).to_list(None))}
    for rid in ("dead-beat", "legacy"):
        r = got[rid]
        assert (r["status"], r["http_status"], r["retryable"]) == ("failed", 503, True)
        assert "look it up before trying again" in r["response"]["detail"]
    assert got["fresh"]["status"] == "processing" and got["legacy-young"]["status"] == "processing"
    out = run(bounded.outcome("legacy", {"id": "u1"}))  # what GET /api/requests/{id} returns to the web client
    assert out["status"] == "failed" and out["http_status"] == 503


def test_every_create_records_the_process_memory_and_heartbeats_carry_it():
    from core import jobs
    from core.procinfo import proc_mem

    async def quick():
        return {"id": "s2"}

    async def scenario():
        assert await bounded.bounded("shipment.create", quick(), user={"id": "u1"}) == {"id": "s2"}
        for _ in range(50):
            await asyncio.sleep(0.02)
            row = await db.raw.process_samples.find_one({"label": "shipment.create"}, {"_id": 0})
            if row:
                break
        await jobs.heartbeat()
        return row, await db.raw.worker_heartbeats.find_one({"_id": jobs.WORKER_ID}, {"_id": 0})

    row, beat = run(scenario())
    mem = proc_mem()
    assert row["ok"] is True and row["pid"] == mem["pid"] and row["worker_id"] == jobs.WORKER_ID
    assert 10 < row["rss_mb"] <= row["peak_rss_mb"] and mem["process_started_at"] == row["process_started_at"]
    assert beat["rss_mb"] > 10 and beat["peak_rss_mb"] >= beat["rss_mb"] and beat["process_started_at"]
