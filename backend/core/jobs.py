"""Durable jobs in MongoDB: every recurring or event-driven operation runs here, so nothing depends on a person
remembering it and nothing is lost when a server restarts.

    from core.jobs import job, schedule, enqueue, JobDeferred, JobFailed

    @job("kyc.decide", agent=True)
    async def kyc_decide(j):                      # j = the job document, arguments in j["payload"]
        ...
        raise JobDeferred(300, "registers busy")   # run again later; not a failed attempt
        raise JobFailed("company deleted")         # never retry: dead letter + owner alert

    await enqueue("kyc.decide", {"company_id": cid}, key=f"kyc:{cid}:{run_at}")   # same key = same job

    @schedule("dunning.daily", job="dunning.run", daily_at="08:00")              # Europe/Helsinki
    @schedule("tracking.poll", job="tracking.poll", every_seconds=300)

Collections: jobs, job_schedules, dead_letters, worker_heartbeats.
Job status: queued -> running -> done | retry (back to running at next_run_at) | dead (copied to dead_letters).
Ops can also end a job as cancelled (POST /api/autopilot/jobs/{id}/cancel); a job whose kind belongs only to
disabled schedules is not started but ended as skipped.
A job is claimed with one atomic find_one_and_update that sets a lease (lease_owner, lease_until), so two server
instances never run the same job; a lease left by a crashed process expires and the job is claimed again.
Agent jobs (agent=True) act for the company and wait while the Autopilot kill switch is on; monitoring jobs always run.
Timestamps are the app's ISO strings (core.util.iso), which sort lexicographically.
"""
import asyncio
import logging
import os
import random
import socket

import time
import traceback
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core import config
from core import procinfo  # noqa: F401 - pins the process start time at import
from core.db import db, register_index
from core.lifecycle import background_loop, on_startup
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso, real_now, span

log = logging.getLogger("jobs")

DEFAULT_BACKOFF = (30, 120, 600, 1800, 3600, 7200)
MAX_DEFERS = 2000          # a job that waits this many times without progress goes to dead letters
HEARTBEAT_SECONDS = 15
POLL_SECONDS = float(config.env("JOBS_POLL_SECONDS", "2"))
CONCURRENCY = int(config.env("JOBS_CONCURRENCY", "4"))
WORKER_ENABLED = config.env_bool("JOBS_WORKER_ENABLED", True)
HELSINKI = "Europe/Helsinki"

register_index("jobs", "id", unique=True)
register_index("jobs", "key", unique=True, partialFilterExpression={"key": {"$type": "string"}})
register_index("jobs", [("status", 1), ("next_run_at", 1), ("priority", -1)])
register_index("jobs", [("kind", 1), ("created_at", -1)])
register_index("jobs", [("subject.id", 1)])
register_index("job_schedules", "name", unique=True)
register_index("dead_letters", "id", unique=True)
register_index("dead_letters", [("status", 1), ("at", -1)])

_JOBS: dict[str, dict] = {}
_SCHEDULES: dict[str, dict] = {}
_wake: asyncio.Event | None = None


class JobDeferred(Exception):
    """Not ready yet (waiting for a reply, a document, the customer). Runs again after `seconds`."""

    def __init__(self, seconds: float, reason: str = ""):
        super().__init__(reason)
        self.seconds = max(1.0, float(seconds))
        self.reason = reason


class JobFailed(Exception):
    """Permanent failure: retrying cannot help. The job goes straight to dead letters and the owner is alerted."""


def job(kind: str, *, max_attempts: int = 6, backoff: tuple = DEFAULT_BACKOFF, timeout: float = 300,
        agent: bool = False, label_fi: str | None = None, label_en: str | None = None):
    def deco(fn):
        _JOBS[kind] = {"fn": fn, "max_attempts": max_attempts, "backoff": tuple(backoff), "timeout": timeout,
                       "agent": agent, "label_fi": label_fi or kind, "label_en": label_en or kind}
        return fn
    return deco


def schedule(name: str, *, job: str, every_seconds: float | None = None, daily_at: str | None = None,
             tz: str = HELSINKI, payload: dict | None = None, label_fi: str | None = None, label_en: str | None = None):
    if bool(every_seconds) == bool(daily_at):
        raise ValueError("schedule needs exactly one of every_seconds / daily_at")

    def deco(fn=None):
        _SCHEDULES[name] = {"name": name, "job": job, "every_seconds": every_seconds, "daily_at": daily_at, "tz": tz,
                            "payload": payload or {}, "label_fi": label_fi or name, "label_en": label_en or name}
        return fn
    return deco


def registered_jobs() -> dict[str, dict]:
    return {k: {kk: vv for kk, vv in v.items() if kk != "fn"} for k, v in _JOBS.items()}


def registered_schedules() -> dict[str, dict]:
    return dict(_SCHEDULES)


def _wake_worker():
    if _wake is not None:
        try:
            _wake.set()
        except RuntimeError:
            pass


async def enqueue(kind: str, payload: dict | None = None, *, key: str | None = None, run_at=None,
                  company_id: str | None = None, subject: tuple | None = None, priority: int = 0,
                  max_attempts: int | None = None, source: str = "event") -> dict:
    """Queue a job. With `key`, a second enqueue of the same key returns the first job (whatever its status)."""
    if kind not in _JOBS:
        raise ValueError(f"Unknown job kind {kind}")
    spec = _JOBS[kind]
    when = run_at if isinstance(run_at, str) else iso(run_at) if run_at else now_iso()
    doc = {
        "id": new_id(), "kind": kind, "key": key, "payload": payload or {}, "status": "queued",
        "attempts": 0, "max_attempts": max_attempts or spec["max_attempts"], "priority": priority,
        "agent": spec["agent"], "company_id": company_id,
        "subject": {"type": subject[0], "id": subject[1]} if subject else None,
        "next_run_at": when, "created_at": now_iso(), "source": source,
        "lease_owner": None, "lease_until": None, "started_at": None, "finished_at": None,
        "last_error": None, "result": None, "defer_count": 0, "history": [],
    }
    if key is None:
        doc.pop("key")
    try:
        await db.raw.jobs.insert_one(dict(doc))
    except DuplicateKeyError:
        existing = await db.raw.jobs.find_one({"key": key}, NO_ID)
        if existing:
            return existing
        raise
    _wake_worker()
    return doc


async def claim(owner: str, *, halted: bool | None = None, kinds: list[str] | None = None) -> dict | None:
    """Atomically take the next due job. Expired leases (crashed worker) are taken over."""
    t, w = now_iso(), iso(real_now())
    if halted is None:
        halted = await autopilot_halted()
    # wall_due / lease_wall: operational timers (retries, deferrals, leases) on wall time; next_run_at is the business
    # time (core.util.now). They are the same clock unless a demo world runs a faster one.
    due = {"$or": [{"status": {"$in": ["queued", "retry"]}, "next_run_at": {"$lte": t}},
                   {"status": {"$in": ["queued", "retry"]}, "wall_due": {"$lte": w}},
                   {"status": "running", "lease_wall": {"$lt": w}},
                   {"status": "running", "lease_wall": None, "lease_until": {"$lt": t}}]}
    q: dict = {"$and": [due, {"kind": {"$in": kinds or list(_JOBS)}}]}
    if halted:
        q["$and"].append({"agent": {"$ne": True}})
    jd = await db.raw.jobs.find_one_and_update(
        q, {"$set": {"status": "running", "lease_owner": owner, "started_at": t,
                     "lease_until": iso(now() + span(60)), "lease_wall": iso(real_now() + timedelta(seconds=60))},
            "$inc": {"attempts": 1}},
        sort=[("priority", -1), ("next_run_at", 1)], projection=NO_ID, return_document=ReturnDocument.AFTER)
    if jd:
        spec = _JOBS.get(jd["kind"]) or {}
        secs = float(spec.get("timeout", 300)) + 60
        lease = iso(now() + span(secs))
        await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {"$set": {
            "lease_until": lease, "lease_wall": iso(real_now() + timedelta(seconds=secs))}})
        jd["lease_until"] = lease
    return jd


def _backoff_seconds(spec: dict, attempts: int) -> float:
    steps = spec.get("backoff") or DEFAULT_BACKOFF
    base = steps[min(max(attempts - 1, 0), len(steps) - 1)]
    return base * (0.9 + 0.2 * random.random())


async def run_job(jd: dict, owner: str) -> str:
    """Run one claimed job and record the outcome. Returns the new status."""
    spec = _JOBS.get(jd["kind"])
    started = time.monotonic()
    if not spec:
        return await _dead(jd, owner, f"No handler registered for {jd['kind']}", "")
    paused = await _paused_schedule(jd["kind"])
    if paused:
        res = await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {"$set": {
            "status": "skipped", "finished_at": now_iso(), "lease_owner": None, "lease_until": None,
            "waiting_for": None, "skipped_reason": f"Schedule {paused} is disabled"}, "$inc": {"attempts": -1}})
        log.warning("job %s %s skipped: schedule %s is disabled", jd["kind"], jd["id"], paused)
        return "skipped" if res.modified_count else "lost_lease"
    # claim() counts every take-over of an expired lease as an attempt. A job whose worker died mid-run (OOM kill,
    # crash) never reaches the except below, so without this check it was re-run past max_attempts forever.
    if jd["attempts"] > jd.get("max_attempts", spec["max_attempts"]):
        return await _dead(jd, owner, (f"Exceeded max attempts ({jd['attempts'] - 1} of {jd.get('max_attempts', spec['max_attempts'])}): "
                                       f"the worker stopped mid-run and the lease expired. Last error: {jd.get('last_error') or 'none'}")[:2000], "")
    try:
        result = await asyncio.wait_for(spec["fn"](jd), timeout=spec["timeout"])
    except JobDeferred as d:
        if int(jd.get("defer_count") or 0) + 1 >= MAX_DEFERS:  # still holding the lease: _dead() needs it
            return await _dead(jd, owner, f"Still waiting after {MAX_DEFERS} checks: {d.reason}", "")
        await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {
            "$set": {"status": "queued", "next_run_at": iso(now() + span(d.seconds)),
                     "wall_due": iso(real_now() + timedelta(seconds=d.seconds)),
                     "lease_owner": None, "lease_until": None, "lease_wall": None, "waiting_for": d.reason[:500]},
            "$inc": {"attempts": -1, "defer_count": 1}})
        return "queued"
    except JobFailed as exc:
        return await _dead(jd, owner, str(exc) or "Failed", traceback.format_exc()[-4000:])
    except asyncio.CancelledError:
        # Server shutting down: give the job back so another instance (or the restart) takes it.
        await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {
            "$set": {"status": "retry", "next_run_at": now_iso(), "wall_due": None, "lease_owner": None, "lease_until": None,
                     "lease_wall": None},
            "$inc": {"attempts": -1}})
        raise
    except Exception as exc:  # noqa: BLE001
        err = (f"Timed out after {spec['timeout']:.0f} s" if isinstance(exc, asyncio.TimeoutError)
               else f"{type(exc).__name__}: {exc}")[:2000]
        trace = traceback.format_exc()[-4000:]
        if jd["attempts"] >= jd.get("max_attempts", spec["max_attempts"]):
            return await _dead(jd, owner, err, trace)
        wait = _backoff_seconds(spec, jd["attempts"])
        await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {
            "$set": {"status": "retry", "next_run_at": iso(now() + span(wait)), "last_error": err,
                     "wall_due": iso(real_now() + timedelta(seconds=wait)),
                     "lease_owner": None, "lease_until": None, "lease_wall": None},
            "$push": {"history": {"$each": [{"at": now_iso(), "attempt": jd["attempts"], "error": err}], "$slice": -20}}})
        log.warning("job %s %s attempt %s failed, retry in %.0f s: %s", jd["kind"], jd["id"], jd["attempts"], wait, err)
        return "retry"
    await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {"$set": {
        "status": "done", "finished_at": now_iso(), "result": _trim(result), "lease_owner": None, "lease_until": None,
        "duration_ms": int((time.monotonic() - started) * 1000), "waiting_for": None}})
    return "done"


async def _paused_schedule(kind: str) -> str | None:
    """Name of the disabled schedule that owns this job kind, when every schedule of the kind is disabled.

    Ops pause a schedule because its job hurts the server (2026-10-25: customs.offices_refresh crash-looped the
    backend); queued, retried and manually started jobs of that kind must stay stopped too, not only new runs."""
    names = {s["name"]: s.get("enabled", True) async for s in
             db.raw.job_schedules.find({"job": kind}, {"_id": 0, "name": 1, "enabled": 1})}
    if names and not any(v is not False for v in names.values()):
        return sorted(names)[0]
    return None


async def cancel_job(job_id: str, actor: dict, note: str) -> dict:
    """Ops stop for one job: a queued/retry job never starts, a running one loses its lease (the worker's own
    result write is filtered on lease_owner, so it cannot overwrite the cancel)."""
    from fastapi import HTTPException

    from core.audit import audit
    before = await db.raw.jobs.find_one({"id": job_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Job not found")
    if before["status"] not in ("queued", "retry", "running"):
        raise HTTPException(409, f"Job is already {before['status']}")
    t = now_iso()
    res = await db.raw.jobs.update_one({"id": job_id, "status": before["status"],
                                        "lease_owner": before.get("lease_owner")}, {"$set": {
        "status": "cancelled", "finished_at": t, "lease_owner": None, "lease_until": None, "waiting_for": None,
        "cancelled": {"at": t, "by": actor.get("id"), "note": note, "status_before": before["status"],
                      "lease_owner_before": before.get("lease_owner")}}})
    if not res.modified_count:
        raise HTTPException(409, "Job changed while cancelling; try again")
    await audit(actor, "job_cancelled", "job", job_id, {"kind": before["kind"], "status_before": before["status"],
                                                        "attempts": before.get("attempts"), "note": note},
                company_id=before.get("company_id"))
    return await db.raw.jobs.find_one({"id": job_id}, {"_id": 0, "history": 0})


def _trim(value, limit: int = 8000):
    if value is None:
        return None
    import json
    try:
        s = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {"repr": repr(value)[:limit]}
    return value if len(s) <= limit else {"truncated": s[:limit]}


async def _dead(jd: dict, owner: str, error: str, trace: str) -> str:
    t = now_iso()
    res = await db.raw.jobs.update_one({"id": jd["id"], "lease_owner": owner}, {"$set": {
        "status": "dead", "finished_at": t, "last_error": error, "lease_owner": None, "lease_until": None}})
    if not res.modified_count:
        return "lost_lease"
    dl = {"id": new_id(), "job_id": jd["id"], "kind": jd["kind"], "key": jd.get("key"), "payload": jd.get("payload"),
          "company_id": jd.get("company_id"), "subject": jd.get("subject"), "attempts": jd.get("attempts"),
          "error": error, "trace": trace, "at": t, "status": "open", "retried_at": None, "retried_by": None}
    await db.raw.dead_letters.insert_one(dict(dl))
    log.error("job %s %s dead after %s attempts: %s", jd["kind"], jd["id"], jd.get("attempts"), error)
    try:
        from core.events import emit
        await emit("job.dead", {"job": jd, "dead_letter": dl, "error": error})
    except Exception:  # noqa: BLE001
        log.exception("job.dead handlers failed")
    return "dead"


async def retry_dead_letter(dead_letter_id: str, actor: dict) -> dict:
    from fastapi import HTTPException

    from core.audit import audit
    dl = await db.raw.dead_letters.find_one({"id": dead_letter_id}, NO_ID)
    if not dl:
        raise HTTPException(404, "Dead letter not found")
    if dl["status"] != "open":
        raise HTTPException(409, "This dead letter was already retried or dismissed")
    t = now_iso()
    await db.raw.jobs.update_one({"id": dl["job_id"]}, {"$set": {
        "status": "queued", "next_run_at": t, "attempts": 0, "defer_count": 0, "last_error": None,
        "retried_from_dead_letter": dl["id"], "lease_owner": None, "lease_until": None}})
    await db.raw.dead_letters.update_one({"id": dl["id"]}, {"$set": {
        "status": "retried", "retried_at": t, "retried_by": {"id": actor.get("id"), "name": actor.get("name")}}})
    await audit(actor, "dead_letter_retried", "job", dl["job_id"], {"kind": dl["kind"], "dead_letter_id": dl["id"]},
                company_id=dl.get("company_id"))
    _wake_worker()
    return await db.raw.dead_letters.find_one({"id": dl["id"]}, NO_ID)


async def dismiss_dead_letter(dead_letter_id: str, actor: dict, note: str) -> dict:
    from fastapi import HTTPException

    from core.audit import audit
    res = await db.raw.dead_letters.update_one({"id": dead_letter_id, "status": "open"}, {"$set": {
        "status": "dismissed", "dismissed_at": now_iso(), "dismissed_by": {"id": actor.get("id"), "name": actor.get("name")},
        "note": note}})
    if not res.matched_count:
        raise HTTPException(409, "Only an open dead letter can be dismissed")
    dl = await db.raw.dead_letters.find_one({"id": dead_letter_id}, NO_ID)
    await audit(actor, "dead_letter_dismissed", "job", dl["job_id"], {"note": note}, company_id=dl.get("company_id"))
    return dl


async def autopilot_halted() -> bool:
    from core.settings import get_settings
    try:
        return bool((await get_settings()).get("autopilot_halted"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------------------------
# Set by a demo world: job name -> True when its daily_at follows the (simulated) business calendar of now();
# every other schedule runs on wall time (wall_due), so a faster demo clock never changes how often Huolitsija polls.
DEMO_CALENDAR = None


def on_business_calendar(spec: dict) -> bool:
    return bool(spec.get("daily_at")) and (DEMO_CALENDAR is None or DEMO_CALENDAR(spec.get("job")))


def schedule_times(spec: dict) -> dict:
    """{next_run_at, wall_due} of a schedule's next run: business-calendar schedules have no wall_due."""
    if on_business_calendar(spec):
        return {"next_run_at": iso(next_run(spec, now())), "wall_due": None}
    wall = real_now()
    w = next_run(spec, wall)
    return {"next_run_at": iso(now() + span((w - wall).total_seconds())), "wall_due": iso(w)}


def next_run(spec: dict, after: datetime) -> datetime:
    if spec.get("every_seconds"):
        return after + timedelta(seconds=float(spec["every_seconds"]))
    tz = ZoneInfo(spec.get("tz") or HELSINKI)
    hh, mm = (int(x) for x in spec["daily_at"].split(":"))
    local = after.astimezone(tz)
    cand = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= local:
        cand = (local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cand.astimezone(ZoneInfo("UTC"))


async def sync_schedules():
    """Write the registered schedules to job_schedules; keeps next_run_at of an unchanged schedule."""
    for name, spec in _SCHEDULES.items():
        shape = {k: spec[k] for k in ("job", "every_seconds", "daily_at", "tz", "payload")}
        cur = await db.raw.job_schedules.find_one({"name": name}, NO_ID)
        if cur and all(cur.get(k) == v for k, v in shape.items()):
            if (cur.get("label_fi"), cur.get("label_en")) != (spec["label_fi"], spec["label_en"]):  # new names only
                await db.raw.job_schedules.update_one({"name": name}, {"$set": {
                    "label_fi": spec["label_fi"], "label_en": spec["label_en"], "updated_at": now_iso()}})
            continue
        first = schedule_times(spec) if spec.get("daily_at") else {"next_run_at": iso(now() + span(5)),
                                                                   "wall_due": iso(real_now() + timedelta(seconds=5))}
        await db.raw.job_schedules.update_one({"name": name}, {"$set": {
            **shape, "label_fi": spec["label_fi"], "label_en": spec["label_en"], **first,
            "updated_at": now_iso()}, "$setOnInsert": {"enabled": True, "last_run_at": None, "last_job_id": None,
                                                      "runs": 0}}, upsert=True)


async def fire_due_schedules() -> int:
    fired = 0
    t, w = now_iso(), iso(real_now())
    due = {"enabled": {"$ne": False}, "$or": [{"next_run_at": {"$lte": t}}, {"wall_due": {"$lte": w}}]}
    async for s in db.raw.job_schedules.find(due, NO_ID):
        spec = _SCHEDULES.get(s["name"])
        if not spec or spec["job"] not in _JOBS:
            continue
        nxt = schedule_times(spec)
        won = await db.raw.job_schedules.find_one_and_update(
            {"name": s["name"], "next_run_at": s["next_run_at"]},
            {"$set": {**nxt, "last_run_at": t}, "$inc": {"runs": 1}}, projection=NO_ID)
        if not won:
            continue  # another instance fired it
        jd = await enqueue(spec["job"], dict(spec["payload"]), key=f"schedule:{s['name']}:{s['next_run_at']}",
                           source="schedule")
        await db.raw.job_schedules.update_one({"name": s["name"]}, {"$set": {"last_job_id": jd["id"]}})
        fired += 1
    return fired


async def run_schedule_now(name: str, actor: dict) -> dict:
    from fastapi import HTTPException

    from core.audit import audit
    spec = _SCHEDULES.get(name)
    if not spec:
        raise HTTPException(404, "Schedule not found")
    jd = await enqueue(spec["job"], dict(spec["payload"]), key=f"schedule:{name}:manual:{now_iso()}", source="manual")
    await db.raw.job_schedules.update_one({"name": name}, {"$set": {"last_job_id": jd["id"], "last_run_at": now_iso()}})
    await audit(actor, "schedule_run_now", "job_schedule", name, {"job_id": jd["id"]})
    return jd


# ---------------------------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------------------------
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_stats = {"started_at": None, "processed": 0, "failed": 0, "running": 0}


async def heartbeat():
    from core.procinfo import proc_mem
    mem = proc_mem()
    await db.raw.worker_heartbeats.update_one({"_id": WORKER_ID}, {"$set": {
        "rss_mb": mem["rss_mb"], "peak_rss_mb": mem["peak_rss_mb"], "process_started_at": mem["process_started_at"],
        "worker_id": WORKER_ID, "host": socket.gethostname(), "pid": os.getpid(), "beat_at": now_iso(),
        "started_at": _stats["started_at"], "processed": _stats["processed"], "failed": _stats["failed"],
        "running": _stats["running"], "concurrency": CONCURRENCY}}, upsert=True)


async def work_once(owner: str = WORKER_ID, max_jobs: int = 50, kinds: list[str] | None = None) -> int:
    """Claim and run due jobs one after another (tests and the drain endpoint use this)."""
    n = 0
    halted = await autopilot_halted()
    while n < max_jobs:
        jd = await claim(owner, halted=halted, kinds=kinds)
        if not jd:
            break
        await run_job(jd, owner)
        n += 1
    return n


async def worker_main():
    global _wake
    _wake = asyncio.Event()
    _stats["started_at"] = now_iso()
    sem = asyncio.Semaphore(CONCURRENCY)
    running: set[asyncio.Task] = set()
    last_beat = 0.0
    last_sched = 0.0
    log.info("job worker %s started (concurrency %s)", WORKER_ID, CONCURRENCY)

    async def one(jd):
        _stats["running"] += 1
        try:
            status = await run_job(jd, WORKER_ID)
            _stats["processed"] += 1
            _stats["failed"] += status in ("retry", "dead")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("job runner crashed on %s", jd.get("id"))
        finally:
            _stats["running"] -= 1
            sem.release()

    while True:
        mono = time.monotonic()
        try:
            if mono - last_beat >= HEARTBEAT_SECONDS:
                await heartbeat()
                last_beat = mono
            if mono - last_sched >= 5:
                await fire_due_schedules()
                last_sched = mono
            halted = await autopilot_halted()
            while True:
                await sem.acquire()
                jd = await claim(WORKER_ID, halted=halted)
                if not jd:
                    sem.release()
                    break
                t = asyncio.create_task(one(jd))
                running.add(t)
                t.add_done_callback(running.discard)
        except asyncio.CancelledError:
            for t in running:
                t.cancel()
            raise
        except Exception:  # noqa: BLE001
            log.exception("job worker loop error")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


@on_startup(order=5)
async def _jobs_startup():
    await sync_schedules()


@background_loop(seconds=5)
async def jobs_worker():
    if not WORKER_ENABLED:
        return
    await worker_main()


async def counts() -> dict:
    out = {"queued": 0, "running": 0, "retry": 0, "done": 0, "dead": 0, "cancelled": 0, "skipped": 0}
    async for r in db.raw.jobs.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        out[r["_id"]] = r["n"]
    halted = await autopilot_halted()
    out["held"] = await db.raw.jobs.count_documents({"status": {"$in": ["queued", "retry"]}, "agent": True}) if halted else 0
    first_due = await db.raw.jobs.find_one({"status": {"$in": ["queued", "retry"]}, "next_run_at": {"$lte": now_iso()}},
                                           {"_id": 0, "next_run_at": 1}, sort=[("next_run_at", 1)])
    lag = None
    if first_due and not halted:
        lag = max(0, int((now() - parse_iso(first_due["next_run_at"])).total_seconds()))
    out["oldest_due_lag_seconds"] = lag
    out["dead_letters_open"] = await db.raw.dead_letters.count_documents({"status": "open"})
    return out
