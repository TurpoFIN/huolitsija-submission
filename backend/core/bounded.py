"""Bounded create requests: answer within BUDGET_S, never abandon the work.

A create (shipment, quote, booking) runs as a task. If it finishes inside the budget the caller gets
the normal response. Otherwise the caller gets 202 {status: processing, request_id, poll}; the task
keeps running to completion (no half-done record) and its exact response is stored for
GET /api/requests/{id}, which the web client follows automatically. Slow runs are recorded with
their duration so the slow leg can be found afterwards.

A running request beats (beat_at) every BEAT_S from the process that runs it. If that process dies (a restart, an
out-of-memory kill) the beat stops, and any process marks the request failed (503, retryable, with a note to check
whether the record was already created) instead of leaving it "processing" until it expires.
"""
import asyncio
import logging
import time

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from core.db import db, register_index
from core.lifecycle import background_loop, on_startup
from core.util import iso, new_id, now, now_iso
from datetime import timedelta

log = logging.getLogger("bounded")
BUDGET_S = 25.0
register_index("slow_requests", "id", unique=True)
register_index("slow_requests", "expires_at", expireAfterSeconds=0)
register_index("process_samples", "expires_at", expireAfterSeconds=0)
register_index("process_samples", [("at", -1)])
_running: set = set()
_live: dict = {}  # request id -> task, while this process runs it
BEAT_S = 20
ORPHAN_S = 90          # no beat for this long: the process that ran it is gone
LEGACY_ORPHAN_S = 600  # rows written before beats existed
ORPHAN_DETAIL = ("The server restarted before this request finished, so its result was not stored. The record may already "
                 "exist: look it up before trying again.")


async def _record(rid: str, task: asyncio.Task, started: float):
    took = round(time.monotonic() - started, 2)
    upd = {"finished_at": now_iso(), "duration_s": took}
    try:
        upd.update(status="done", http_status=200, response=jsonable_encoder(task.result()))
    except HTTPException as exc:
        upd.update(status="failed", http_status=exc.status_code, response={"detail": exc.detail})
    except Exception as exc:  # noqa: BLE001
        log.exception("bounded request %s failed", rid)
        upd.update(status="failed", http_status=500, response={"detail": type(exc).__name__})
    await db.raw.slow_requests.update_one({"id": rid}, {"$set": upd})
    log.warning("slow request %s finished in %.1fs", rid, took)


async def _sample(label: str, task: asyncio.Task, started: float):
    """RSS of this process right after a create, kept 2 days (ops: GET /api/autopilot/process)."""
    from core.jobs import WORKER_ID
    from core.procinfo import proc_mem
    try:
        mem = proc_mem()
        ok = not task.cancelled() and task.exception() is None
        await db.raw.process_samples.insert_one({"at": now_iso(), "label": label, "ok": ok, "worker_id": WORKER_ID,
                                                 "duration_s": round(time.monotonic() - started, 2), **mem,
                                                 "expires_at": now() + timedelta(days=2)})
        log.info("%s finished in %.1fs: pid %s rss %s MB peak %s MB", label, time.monotonic() - started, mem["pid"],
                 mem["rss_mb"], mem["peak_rss_mb"])
    except Exception:  # noqa: BLE001 - observability never breaks a create
        log.exception("process sample after %s failed", label)
    finally:
        _running.discard(asyncio.current_task())


async def bounded(label: str, coro, *, user: dict | None = None, budget_s: float | None = None):
    started = time.monotonic()
    task = asyncio.ensure_future(coro)
    task.add_done_callback(lambda t: _running.add(asyncio.ensure_future(_sample(label, t, started))))
    done, _ = await asyncio.wait({task}, timeout=BUDGET_S if budget_s is None else budget_s)
    if done:
        return task.result()
    rid = new_id()
    await db.raw.slow_requests.insert_one({"id": rid, "label": label, "status": "processing", "started_at": now_iso(),
                                           "beat_at": now_iso(), "user_id": (user or {}).get("id"),
                                           "expires_at": now() + timedelta(days=2)})
    _running.add(task)
    _live[rid] = task

    def finished(t):
        _running.discard(t)
        _live.pop(rid, None)
        _running.add(asyncio.ensure_future(_record(rid, t, started)))

    task.add_done_callback(finished)
    return JSONResponse(status_code=202, content={"status": "processing", "request_id": rid, "poll": f"/requests/{rid}"})


async def outcome(rid: str, user: dict) -> dict:
    row = await db.raw.slow_requests.find_one({"id": rid}, {"_id": 0})
    if not row or (row.get("user_id") and row["user_id"] != user.get("id")):
        raise HTTPException(404, "Request not found")
    return row


@background_loop(seconds=BEAT_S)
async def beat():
    if _live:
        await db.raw.slow_requests.update_many({"id": {"$in": list(_live)}, "status": "processing"}, {"$set": {"beat_at": now_iso()}})


@on_startup(order=40)
async def _startup_sweep():
    await sweep_orphans()


@background_loop(seconds=60)
async def sweep_orphans() -> int:
    """Mark requests whose process stopped beating as failed and retryable."""
    t = now()
    q = {"status": "processing", "id": {"$nin": list(_live)}, "$or": [
        {"beat_at": {"$lt": iso(t - timedelta(seconds=ORPHAN_S))}},
        {"beat_at": {"$exists": False}, "started_at": {"$lt": iso(t - timedelta(seconds=LEGACY_ORPHAN_S))}}]}
    r = await db.raw.slow_requests.update_many(q, {"$set": {
        "status": "failed", "http_status": 503, "retryable": True, "finished_at": now_iso(), "orphaned": True,
        "response": {"detail": ORPHAN_DETAIL, "retryable": True}}})
    if r.modified_count:
        log.warning("marked %d orphaned request(s) failed", r.modified_count)
    return r.modified_count
