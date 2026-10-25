"""s2 durable jobs (core/jobs.py) and the Autopilot jobs API (routes/autopilot.py), in process against a throw-away
MongoDB database (own database, dropped at the end).

Test-only job kinds (test.*) are registered below with the real @job / schedule decorators; every assertion reads the
jobs, dead_letters, job_schedules, alerts, notifications, email_outbox and audit_log collections back. The API tests go
through the real FastAPI router, the real session-token auth (core.deps) and the real permission matrix over an
in-process ASGI transport, so the route guards are exercised, not stubbed."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_jobs_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
os.environ.pop("OWNER_ALERT_EMAIL", None)
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)  # core.db (imported below for the @job decorators) binds Motor to the current loop


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_jobs.py in its own pytest process", allow_module_level=True)

from core.jobs import MAX_DEFERS, JobDeferred, JobFailed, job, schedule  # noqa: E402
from core.util import iso, now, parse_iso  # noqa: E402

UTC = timezone.utc
HELSINKI = ZoneInfo("Europe/Helsinki")
S: dict = {}
CALLS: dict[str, list] = {}      # job id -> lease_owner seen by the handler, one entry per handler call
RUNNING_VIEW: dict[str, dict] = {}  # job id -> the job document as stored in MongoDB while the handler ran

CID = str(uuid.uuid4())
OWNER = {"id": "owner-jobs", "email": "omistaja-jobs@huolitsija.example", "name": "Omistaja", "role": "ops_admin",
         "org_type": "internal", "locale": "fi", "notify_email": True, "session_version": 0, "email_verified": True}
OPS_AGENT = {"id": "opsagent-jobs", "email": "operaattori-jobs@huolitsija.example", "name": "Operaattori",
             "role": "ops_agent", "org_type": "internal", "locale": "fi", "session_version": 0, "email_verified": True}
CUSTOMER = {"id": "customer-jobs", "email": "asiakas-jobs@testikuljetus.example", "name": "Asiakas", "role": "customer_admin",
            "org_type": "customer", "company_id": CID, "locale": "fi", "session_version": 0, "email_verified": True}


def _record(j):
    CALLS.setdefault(j["id"], []).append(j.get("lease_owner"))


# --------------------------------------------------------------------------------------------- test job kinds
@job("test.noop", label_fi="Testityö", label_en="Test job")
async def _noop(j):
    _record(j)
    return {"ok": True, "n": (j.get("payload") or {}).get("n")}


@job("test.race", timeout=30)
async def _race(j):
    _record(j)
    await asyncio.sleep(0.01)  # give the other worker a chance to claim while this one runs
    return {"n": j["payload"]["n"]}


@job("test.flaky", max_attempts=3, backoff=(100, 400, 1600))
async def _flaky(j):
    _record(j)
    n = len(CALLS[j["id"]])
    if n <= 3:
        raise RuntimeError(f"boom {n}")
    return {"recovered_on_call": n}


@job("test.fatal", label_fi="Kuolettava testityö", label_en="Fatal test job")
async def _fatal(j):
    _record(j)
    raise JobFailed("company deleted")


@job("test.wait")
async def _wait(j):
    _record(j)
    raise JobDeferred(60, "waiting for the customer's reply")


@job("test.slow", max_attempts=3, timeout=1)
async def _slow(j):
    _record(j)
    await asyncio.sleep(5)
    return {"never": True}


@job("test.lease", timeout=30)
async def _lease(j):
    from core.db import db
    _record(j)
    RUNNING_VIEW[j["id"]] = await db.raw.jobs.find_one({"id": j["id"]}, {"_id": 0})
    return {"ok": True}


@job("test.agentwork", agent=True)
async def _agentwork(j):
    _record(j)
    return {"ok": True}


@job("test.monitorwork")
async def _monitorwork(j):
    _record(j)
    return {"ok": True}


schedule("test.every", job="test.noop", every_seconds=60, label_fi="Testi minuutin välein", label_en="Test every minute")()
schedule("test.daily", job="test.noop", daily_at="08:00", label_fi="Testi klo 8", label_en="Test at 08:00")()


@job("test.heavy")
async def _heavy(j):
    _record(j)
    return {"ok": True}


schedule("test.heavy_daily", job="test.heavy", every_seconds=86400, label_fi="Raskas testi", label_en="Heavy test")()


# --------------------------------------------------------------------------------------------- fixtures
def setup_module(_m):
    async def go():
        from fastapi import FastAPI

        from core.db import db, ensure_indexes
        from core.security import create_session_token, load_secret
        import routes.autopilot  # noqa: F401  (registers every agent job, schedule and the job.dead alert handler)
        await ensure_indexes()
        await load_secret(db)
        await db.raw.companies.insert_one({"id": CID, "name": "Testi Kuljetus Oy", "business_id": "3990009-9",
                                           "country": "FI", "kyc_status": "approved", "status": "active",
                                           "created_at": "2026-01-01T00:00:00Z"})
        for u in (OWNER, OPS_AGENT, CUSTOMER):
            await db.raw.users.insert_one(dict(u))
        S["tokens"] = {u["id"]: create_session_token(u["id"], 0) for u in (OWNER, OPS_AGENT, CUSTOMER)}
        app = FastAPI()
        app.include_router(routes.autopilot.router)
        S["app"] = app
    run(go())


def teardown_module(_m):
    from core.db import client, db
    run(client.drop_database(db.raw.name))  # the database this module really used (os.environ may be another module's)
    _LOOP.close()


async def _job(jid: str) -> dict:
    from core.db import db
    return await db.raw.jobs.find_one({"id": jid}, {"_id": 0})


async def _make_due(jid: str):
    from core.db import db
    await db.raw.jobs.update_one({"id": jid}, {"$set": {"next_run_at": iso(now() - timedelta(seconds=1))}})


def _ahead(ts: str, since: datetime) -> float:
    return (parse_iso(ts) - since).total_seconds()


class _both_read_first:
    """Worst-case interleaving of `parties` fire_due_schedules() calls: each one's job_schedules.find() result is held
    until every caller has read, so all see the same due schedule before any of them writes. MongoDB is the real one
    underneath; only the timing is forced (a plain asyncio.gather does not reliably interleave the read and the write)."""

    def __init__(self, parties: int):
        self.barrier = asyncio.Barrier(parties)

    def __enter__(self):
        import core.jobs
        self.mod, self.real_db = core.jobs, core.jobs.db
        barrier, real_db = self.barrier, self.real_db

        class Schedules:
            def __init__(self, coll):
                self._coll = coll

            def find(self, *a, **kw):
                cursor = self._coll.find(*a, **kw)

                async def held():
                    docs = await cursor.to_list(None)
                    await barrier.wait()
                    for d in docs:
                        yield d
                return held()

            def __getattr__(self, name):
                return getattr(self._coll, name)

        class Raw:
            def __getattr__(self, name):
                coll = getattr(real_db.raw, name)
                return Schedules(coll) if name == "job_schedules" else coll

        class Db:
            raw = Raw()

        core.jobs.db = Db()
        return self

    def __exit__(self, *exc):
        self.mod.db = self.real_db
        return False


async def _http(method: str, path: str, user: dict | None = None, **kw):
    import httpx
    headers = {"authorization": f"Bearer {S['tokens'][user['id']]}"} if user else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=S["app"]), base_url="http://test") as c:
        return await c.request(method, path, headers=headers, **kw)


# --------------------------------------------------------------------------------------------- enqueue / claim
def test_enqueue_same_key_returns_the_same_job():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        key = f"test:key:{uuid.uuid4().hex}"
        a = await enqueue("test.noop", {"n": 1}, key=key, company_id=CID, subject=("shipment", "s-1"))
        b = await enqueue("test.noop", {"n": 2}, key=key, company_id=CID)
        assert b["id"] == a["id"]
        assert b["payload"] == {"n": 1}  # the first job stands; the second payload is not merged in
        assert await db.raw.jobs.count_documents({"key": key}) == 1
        doc = await _job(a["id"])
        assert (doc["status"], doc["attempts"], doc["defer_count"]) == ("queued", 0, 0)
        assert doc["subject"] == {"type": "shipment", "id": "s-1"} and doc["company_id"] == CID
        assert doc["max_attempts"] == 6 and doc["agent"] is False

        assert await work_once("w-key", kinds=["test.noop"]) == 1
        c = await enqueue("test.noop", {"n": 3}, key=key)  # whatever its status: still the same job, not run again
        assert c["id"] == a["id"] and c["status"] == "done"
        assert await work_once("w-key", kinds=["test.noop"]) == 0
        assert CALLS[a["id"]] == ["w-key"]
        assert (await _job(a["id"]))["result"] == {"ok": True, "n": 1}

        x = await enqueue("test.noop", {"n": 4})
        y = await enqueue("test.noop", {"n": 4})
        assert x["id"] != y["id"]  # no key: two jobs
        assert await work_once("w-key", kinds=["test.noop"]) == 2
        with pytest.raises(ValueError):
            await enqueue("test.not_registered", {})
    run(go())


def test_higher_priority_job_is_claimed_first():
    async def go():
        from core.jobs import enqueue, work_once
        low = await enqueue("test.noop", {"n": "low"})
        high = await enqueue("test.noop", {"n": "high"}, priority=5)
        assert await work_once("w-prio", max_jobs=1, kinds=["test.noop"]) == 1
        assert high["id"] in CALLS and low["id"] not in CALLS
        assert (await _job(low["id"]))["status"] == "queued"
        assert await work_once("w-prio", kinds=["test.noop"]) == 1
        assert (await _job(low["id"]))["status"] == "done"
    run(go())


def test_two_racing_workers_run_every_job_exactly_once():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        ids = [(await enqueue("test.race", {"n": i}))["id"] for i in range(20)]
        n1, n2 = await asyncio.gather(work_once("w1", kinds=["test.race"]), work_once("w2", kinds=["test.race"]))
        assert n1 + n2 == 20
        assert n1 > 0 and n2 > 0, (n1, n2)  # the two workers really interleaved, so the race was real
        for i, jid in enumerate(ids):
            assert len(CALLS[jid]) == 1, (jid, CALLS[jid])
            doc = await _job(jid)
            assert doc["status"] == "done" and doc["attempts"] == 1 and doc["result"] == {"n": i}
            assert doc["lease_owner"] is None and doc["lease_until"] is None and doc["finished_at"]
        assert {CALLS[jid][0] for jid in ids} == {"w1", "w2"}
        assert await db.raw.jobs.count_documents({"kind": "test.race", "status": "done"}) == 20
        assert await db.raw.jobs.count_documents({"kind": "test.race", "status": {"$ne": "done"}}) == 0
    run(go())


# --------------------------------------------------------------------------------------------- failures
def test_exception_backs_off_then_dead_letter_alert_and_retry():
    async def go():
        from core.db import db
        from core.jobs import enqueue, retry_dead_letter, work_once
        jd = await enqueue("test.flaky", {"x": 1}, company_id=CID, subject=("invoice", "inv-flaky"))
        jid = jd["id"]

        t0 = now()
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 1
        doc = await _job(jid)
        assert (doc["status"], doc["attempts"]) == ("retry", 1)
        assert doc["last_error"] == "RuntimeError: boom 1"
        assert 88 <= _ahead(doc["next_run_at"], t0) <= 112, doc["next_run_at"]  # backoff[0] = 100 s +-10 %
        assert [(h["attempt"], h["error"]) for h in doc["history"]] == [(1, "RuntimeError: boom 1")]
        assert doc["lease_owner"] is None and doc["lease_until"] is None
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 0  # not due before its backoff

        await _make_due(jid)
        t1 = now()
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 1
        doc = await _job(jid)
        assert (doc["status"], doc["attempts"]) == ("retry", 2)
        assert 358 <= _ahead(doc["next_run_at"], t1) <= 442, doc["next_run_at"]  # backoff[1] = 400 s +-10 %
        assert [h["attempt"] for h in doc["history"]] == [1, 2]
        assert await db.raw.dead_letters.count_documents({"job_id": jid}) == 0

        await _make_due(jid)
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 1  # attempt 3 = max_attempts
        doc = await _job(jid)
        assert (doc["status"], doc["attempts"]) == ("dead", 3)
        assert doc["last_error"] == "RuntimeError: boom 3" and doc["finished_at"]
        dls = await db.raw.dead_letters.find({"job_id": jid}, {"_id": 0}).to_list(None)
        assert len(dls) == 1
        dl = dls[0]
        assert dl["status"] == "open" and dl["kind"] == "test.flaky" and dl["attempts"] == 3
        assert dl["error"] == "RuntimeError: boom 3" and "RuntimeError" in dl["trace"]
        assert dl["payload"] == {"x": 1} and dl["company_id"] == CID and dl["subject"] == {"type": "invoice", "id": "inv-flaky"}

        # job.dead -> services/monitoring.py raises one owner alert per job kind
        alert = await db.raw.alerts.find_one({"key": "dead:test.flaky", "status": "open"}, {"_id": 0})
        assert alert, "job.dead must open the dead:<kind> owner alert"
        assert alert["severity"] == "warning"
        assert alert["title_fi"] == "Työ epäonnistui: test.flaky" and alert["title"] == "Job failed: test.flaky"
        assert alert["body"] == "After 3 attempts: RuntimeError: boom 3"
        assert alert["data"] == {"job_id": jid, "dead_letter_id": dl["id"]}
        bell = await db.raw.notifications.find_one({"user_id": OWNER["id"], "kind": "alert",
                                                  "title_i18n.fi": "Hälytys: Työ epäonnistui: test.flaky"}, {"_id": 0})
        assert bell and bell["title_i18n"]["fi"] == "Hälytys: Työ epäonnistui: test.flaky"
        mail = await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": "Hälytys: Työ epäonnistui: test.flaky"})
        assert mail and mail["provider"] == "log" and mail["status"] == "logged"
        assert await db.raw.notifications.count_documents({"user_id": {"$in": [OPS_AGENT["id"], CUSTOMER["id"]]}}) == 0

        await _make_due(jid)
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 0  # a dead job is never claimed again

        out = await retry_dead_letter(dl["id"], OWNER)
        assert out["status"] == "retried" and out["retried_by"] == {"id": OWNER["id"], "name": OWNER["name"]}
        doc = await _job(jid)
        assert (doc["status"], doc["attempts"], doc["defer_count"]) == ("queued", 0, 0)
        assert doc["last_error"] is None and doc["retried_from_dead_letter"] == dl["id"]
        assert await db.raw.audit_log.find_one({"action": "dead_letter_retried", "entity_id": jid})
        assert await work_once("w-flaky", kinds=["test.flaky"]) == 1
        doc = await _job(jid)
        assert (doc["status"], doc["attempts"]) == ("done", 1)
        assert doc["result"] == {"recovered_on_call": 4} and len(CALLS[jid]) == 4

        with pytest.raises(HTTPException) as e:
            await retry_dead_letter(dl["id"], OWNER)
        assert e.value.status_code == 409
        assert (await _job(jid))["status"] == "done"  # the refused retry did not requeue the finished job
        with pytest.raises(HTTPException) as e:
            await retry_dead_letter("no-such-dead-letter", OWNER)
        assert e.value.status_code == 404
    run(go())


def test_timeout_counts_as_a_failed_attempt():
    async def go():
        from core.jobs import enqueue, work_once
        jd = await enqueue("test.slow", {})
        assert await work_once("w-slow", kinds=["test.slow"]) == 1
        doc = await _job(jd["id"])
        assert (doc["status"], doc["attempts"]) == ("retry", 1)
        assert doc["last_error"] == "Timed out after 1 s"
    run(go())


def test_job_failed_goes_dead_at_once():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        jd = await enqueue("test.fatal", {"company_id": "gone"})
        assert await work_once("w-fatal", kinds=["test.fatal"]) == 1
        doc = await _job(jd["id"])
        assert (doc["status"], doc["attempts"], doc["last_error"]) == ("dead", 1, "company deleted")
        assert len(CALLS[jd["id"]]) == 1
        dl = await db.raw.dead_letters.find_one({"job_id": jd["id"]}, {"_id": 0})
        assert dl and dl["status"] == "open" and dl["attempts"] == 1 and dl["error"] == "company deleted"
        assert "JobFailed" in dl["trace"]
        alert = await db.raw.alerts.find_one({"key": "dead:test.fatal", "status": "open"}, {"_id": 0})
        assert alert and alert["data"]["dead_letter_id"] == dl["id"]
        assert await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": "Hälytys: Työ epäonnistui: test.fatal"})
    run(go())


def test_dead_letter_alert_closes_once_no_dead_letter_of_its_kind_is_open():
    async def go():
        from core.db import db
        from core.jobs import dismiss_dead_letter, enqueue, work_once
        from services.monitoring import close_settled_dead_alerts
        jd = await enqueue("test.fatal", {"company_id": "gone-again"})
        assert await work_once("w-fatal-2", kinds=["test.fatal"]) == 1
        open_dls = await db.raw.dead_letters.find({"kind": "test.fatal", "status": "open"}, {"_id": 0}).to_list(None)
        assert jd["id"] in [d["job_id"] for d in open_dls]
        for dl in open_dls:  # while one is still open, the alert stays
            assert "dead:test.fatal" not in await close_settled_dead_alerts()
            assert await db.raw.alerts.find_one({"key": "dead:test.fatal", "status": "open"}, {"_id": 1})
            await dismiss_dead_letter(dl["id"], OWNER, "test: the company was deleted")
        assert "dead:test.fatal" in await close_settled_dead_alerts()
        assert not await db.raw.alerts.find_one({"key": "dead:test.fatal", "status": "open"}, {"_id": 1})
        alert = await db.raw.alerts.find_one({"key": "dead:test.fatal"}, {"_id": 0}, sort=[("resolved_at", -1)])
        assert alert["status"] == "resolved" and alert["resolution_note"] == "No open dead letter of this kind"
    run(go())


def test_job_deferred_waits_without_using_an_attempt():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        jd = await enqueue("test.wait", {})
        t0 = now()
        assert await work_once("w-wait", kinds=["test.wait"]) == 1
        doc = await _job(jd["id"])
        assert (doc["status"], doc["attempts"], doc["defer_count"]) == ("queued", 0, 1)
        assert doc["waiting_for"] == "waiting for the customer's reply"
        assert 58 <= _ahead(doc["next_run_at"], t0) <= 61, doc["next_run_at"]
        assert doc["lease_owner"] is None and doc["last_error"] is None
        assert await work_once("w-wait", kinds=["test.wait"]) == 0  # not before the 60 s
        await _make_due(jd["id"])
        assert await work_once("w-wait", kinds=["test.wait"]) == 1
        doc = await _job(jd["id"])
        assert (doc["status"], doc["attempts"], doc["defer_count"]) == ("queued", 0, 2)
        assert await db.raw.dead_letters.count_documents({"job_id": jd["id"]}) == 0
    run(go())


def test_job_deferred_too_many_times_goes_to_dead_letters():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        jd = await enqueue("test.wait", {})
        await db.raw.jobs.update_one({"id": jd["id"]}, {"$set": {"defer_count": MAX_DEFERS - 1}})
        assert await work_once("w-wait-max", kinds=["test.wait"]) == 1
        doc = await _job(jd["id"])
        assert doc["status"] == "dead", doc["status"]
        dl = await db.raw.dead_letters.find_one({"job_id": jd["id"]}, {"_id": 0})
        assert dl and dl["error"].startswith(f"Still waiting after {MAX_DEFERS} checks")
    run(go())


def test_expired_lease_is_taken_over_live_lease_is_not():
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        crashed = await enqueue("test.lease", {"n": "crashed"})
        alive = await enqueue("test.lease", {"n": "alive"})
        await db.raw.jobs.update_one({"id": crashed["id"]}, {"$set": {
            "status": "running", "lease_owner": "crashed-worker", "attempts": 1,
            "started_at": iso(now() - timedelta(minutes=10)), "lease_until": iso(now() - timedelta(seconds=5))}})
        await db.raw.jobs.update_one({"id": alive["id"]}, {"$set": {
            "status": "running", "lease_owner": "alive-worker", "attempts": 1,
            "started_at": iso(now()), "lease_until": iso(now() + timedelta(minutes=5))}})
        t0 = now()
        assert await work_once("w-rescue", kinds=["test.lease"]) == 1
        assert CALLS[crashed["id"]] == ["w-rescue"] and alive["id"] not in CALLS
        seen = RUNNING_VIEW[crashed["id"]]  # the stored document while the new owner ran it
        assert seen["status"] == "running" and seen["lease_owner"] == "w-rescue"
        assert 85 <= _ahead(seen["lease_until"], t0) <= 92  # timeout 30 s + 60 s grace
        doc = await _job(crashed["id"])
        assert (doc["status"], doc["attempts"], doc["lease_owner"]) == ("done", 2, None)
        other = await _job(alive["id"])
        assert (other["status"], other["lease_owner"], other["attempts"]) == ("running", "alive-worker", 1)
    run(go())


def test_job_whose_worker_died_at_max_attempts_goes_to_dead_letters_instead_of_running_again():
    """Live 2026-10-25: customs.offices_refresh OOM-killed its worker on every attempt; each lease take-over counted an
    attempt but nothing checked the cap, so the job ran on at 5 of 4 and kept crash-looping the backend."""
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        killed = await enqueue("test.lease", {"n": "oom"}, max_attempts=4)
        await db.raw.jobs.update_one({"id": killed["id"]}, {"$set": {
            "status": "running", "lease_owner": "oom-killed-worker", "attempts": 4, "last_error": "earlier",
            "started_at": iso(now() - timedelta(minutes=10)), "lease_until": iso(now() - timedelta(seconds=5))}})
        assert await work_once("w-after-oom", kinds=["test.lease"]) == 1
        assert killed["id"] not in CALLS  # the handler is not run a 5th time
        doc = await _job(killed["id"])
        assert (doc["status"], doc["lease_owner"]) == ("dead", None)
        dl = await db.raw.dead_letters.find_one({"job_id": killed["id"]}, {"_id": 0})
        assert dl["status"] == "open" and dl["attempts"] == 5 and "Exceeded max attempts (4 of 4)" in dl["error"]
        assert await work_once("w-after-oom", kinds=["test.lease"]) == 0  # and it is never claimed again
        # one attempt left: a crashed run at attempt 3 of 4 is still taken over and run
        spare = await enqueue("test.lease", {"n": "spare"}, max_attempts=4)
        await db.raw.jobs.update_one({"id": spare["id"]}, {"$set": {
            "status": "running", "lease_owner": "crashed", "attempts": 3,
            "started_at": iso(now() - timedelta(minutes=10)), "lease_until": iso(now() - timedelta(seconds=5))}})
        assert await work_once("w-after-oom", kinds=["test.lease"]) == 1
        assert CALLS[spare["id"]] == ["w-after-oom"] and (await _job(spare["id"]))["status"] == "done"
    run(go())


# --------------------------------------------------------------------------------------------- kill switch
def test_kill_switch_holds_agent_jobs_and_runs_the_rest():
    async def go():
        from core.db import db
        from core.jobs import claim, counts, enqueue, work_once
        from core.settings import update_settings
        ag = await enqueue("test.agentwork", {"company_id": CID}, company_id=CID)
        mon = await enqueue("test.monitorwork", {})
        assert ag["agent"] is True and mon["agent"] is False
        await update_settings({"autopilot_halted": True})
        try:
            assert await work_once("w-halt", kinds=["test.agentwork", "test.monitorwork"]) == 1
            assert (await _job(mon["id"]))["status"] == "done"
            held = await _job(ag["id"])
            assert (held["status"], held["attempts"], held["lease_owner"]) == ("queued", 0, None)
            assert ag["id"] not in CALLS
            assert await claim("w-halt-2", kinds=["test.agentwork"]) is None
            c = await counts()
            assert c["held"] >= 1
            assert c["held"] == await db.raw.jobs.count_documents({"agent": True, "status": {"$in": ["queued", "retry"]}})
            assert c["oldest_due_lag_seconds"] is None  # a halted queue is not reported as late
        finally:
            await update_settings({"autopilot_halted": False})
        assert (await counts())["held"] == 0
        assert await work_once("w-resumed", kinds=["test.agentwork", "test.monitorwork"]) == 1
        doc = await _job(ag["id"])
        assert doc["status"] == "done" and CALLS[ag["id"]] == ["w-resumed"]
    run(go())


# --------------------------------------------------------------------------------------------- schedules
def test_daily_next_run_is_0800_helsinki_across_dst():
    from core.jobs import next_run
    spec = {"daily_at": "08:00", "tz": "Europe/Helsinki"}
    # summer (EEST, UTC+3): 08:00 local = 05:00 UTC
    assert next_run(spec, datetime(2026, 7, 1, 4, 0, tzinfo=UTC)) == datetime(2026, 7, 1, 5, 0, tzinfo=UTC)
    assert next_run(spec, datetime(2026, 7, 1, 5, 0, tzinfo=UTC)) == datetime(2026, 7, 2, 5, 0, tzinfo=UTC)  # at 08:00 -> tomorrow
    # winter (EET, UTC+2): 08:00 local = 06:00 UTC
    assert next_run(spec, datetime(2026, 1, 15, 5, 59, tzinfo=UTC)) == datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
    assert next_run(spec, datetime(2026, 1, 15, 10, 0, tzinfo=UTC)) == datetime(2026, 1, 16, 6, 0, tzinfo=UTC)
    # DST ends Sun 25.10.2026 04:00 EEST -> 03:00 EET: Saturday afternoon -> Sunday 08:00 EET = 06:00 UTC
    assert next_run(spec, datetime(2026, 10, 24, 12, 0, tzinfo=UTC)) == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
    # DST starts Sun 29.3.2026 03:00 EET -> 04:00 EEST: Saturday afternoon -> Sunday 08:00 EEST = 05:00 UTC
    assert next_run(spec, datetime(2026, 3, 28, 12, 0, tzinfo=UTC)) == datetime(2026, 3, 29, 5, 0, tzinfo=UTC)
    # no tz in the spec = Helsinki; an "after" given in another zone is the same instant
    assert next_run({"daily_at": "08:00"}, datetime(2026, 1, 15, 7, 0, tzinfo=HELSINKI)) == datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
    t = datetime(2026, 5, 5, 12, 0, 30, tzinfo=UTC)
    assert next_run({"every_seconds": 60}, t) == t + timedelta(seconds=60)
    with pytest.raises(ValueError):
        schedule("test.bad", job="test.noop")
    with pytest.raises(ValueError):
        schedule("test.bad", job="test.noop", every_seconds=60, daily_at="08:00")


def test_schedules_sync_and_fire_exactly_once_under_a_race():
    async def go():
        from core.db import db
        from core.jobs import fire_due_schedules, sync_schedules, work_once
        await sync_schedules()
        s = await db.raw.job_schedules.find_one({"name": "test.every"}, {"_id": 0})
        assert s and s["job"] == "test.noop" and s["every_seconds"] == 60 and s["daily_at"] is None
        assert s["enabled"] is True and s["runs"] == 0 and s["last_job_id"] is None
        assert s["label_fi"] == "Testi minuutin välein" and s["tz"] == "Europe/Helsinki"
        assert 0 <= _ahead(s["next_run_at"], now()) <= 6  # an every_seconds schedule first runs ~5 s after start-up
        d = await db.raw.job_schedules.find_one({"name": "test.daily"}, {"_id": 0})
        local = parse_iso(d["next_run_at"]).astimezone(HELSINKI)
        assert (local.hour, local.minute, local.second) == (8, 0, 0)
        assert 0 < _ahead(d["next_run_at"], now()) <= 24 * 3600
        # a second sync keeps the stored next_run_at of an unchanged schedule
        past = iso(now() - timedelta(minutes=3))
        await db.raw.job_schedules.update_one({"name": "test.every"}, {"$set": {"next_run_at": past}})
        await sync_schedules()
        assert (await db.raw.job_schedules.find_one({"name": "test.every"}))["next_run_at"] == past
        # a schedule stored before it had names gets them on the next start-up, and keeps its next run
        await db.raw.job_schedules.update_one({"name": "test.every"}, {"$set": {"label_fi": "test.every", "label_en": "test.every"}})
        await sync_schedules()
        s = await db.raw.job_schedules.find_one({"name": "test.every"}, {"_id": 0})
        assert (s["label_fi"], s["label_en"], s["next_run_at"]) == ("Testi minuutin välein", "Test every minute", past)

        others = [x["name"] async for x in db.raw.job_schedules.find({"name": {"$ne": "test.every"}, "enabled": True})]
        await db.raw.job_schedules.update_many({"name": {"$in": others}}, {"$set": {"enabled": False}})
        try:
            # two server instances: both read the due schedule before either writes (forced, see _both_read_first)
            with _both_read_first(2):
                fired = await asyncio.gather(fire_due_schedules(), fire_due_schedules())
            assert sorted(fired) == [0, 1]
            made = await db.raw.jobs.find({"kind": "test.noop", "source": "schedule"}, {"_id": 0}).to_list(None)
            assert len(made) == 1
            assert made[0]["key"] == f"schedule:test.every:{past}"
            s = await db.raw.job_schedules.find_one({"name": "test.every"}, {"_id": 0})
            assert s["runs"] == 1 and s["last_job_id"] == made[0]["id"] and s["last_run_at"]
            assert 55 <= _ahead(s["next_run_at"], now()) <= 61  # moved one interval forward
            assert await fire_due_schedules() == 0  # not due again
            # a disabled schedule does not fire even when due
            await db.raw.job_schedules.update_one({"name": "test.every"}, {"$set": {"enabled": False, "next_run_at": past}})
            assert await fire_due_schedules() == 0
            await db.raw.job_schedules.update_one({"name": "test.every"}, {"$set": {"enabled": True,
                                                                                   "next_run_at": s["next_run_at"]}})
        finally:
            await db.raw.job_schedules.update_many({"name": {"$in": others}}, {"$set": {"enabled": True}})
        assert await work_once("w-sched", kinds=["test.noop"]) == 1
        assert (await _job(made[0]["id"]))["status"] == "done"
    run(go())


# --------------------------------------------------------------------------------------------- Autopilot API
def test_autopilot_api_jobs_dead_letters_schedules_and_guards():
    async def go():
        from core.db import db
        from core.jobs import enqueue, sync_schedules, work_once
        await sync_schedules()
        jd = await enqueue("test.fatal", {"via": "api"}, company_id=CID, subject=("company", CID))
        assert await work_once("w-api", kinds=["test.fatal"]) == 1
        dl = await db.raw.dead_letters.find_one({"job_id": jd["id"]}, {"_id": 0})
        assert dl and dl["status"] == "open"

        # who may look
        assert (await _http("GET", "/api/autopilot/jobs")).status_code == 401
        assert (await _http("GET", "/api/autopilot/jobs", CUSTOMER)).status_code == 403
        assert (await _http("GET", "/api/autopilot/dead-letters", CUSTOMER)).status_code == 403
        assert (await _http("POST", f"/api/autopilot/dead-letters/{dl['id']}/retry", CUSTOMER)).status_code == 403

        r = await _http("GET", "/api/autopilot/jobs", OWNER, params={"kind": "test.fatal", "status": "dead"})
        assert r.status_code == 200
        body = r.json()
        item = next(i for i in body["items"] if i["id"] == jd["id"])
        assert item["status"] == "dead" and item["label_fi"] == "Kuolettava testityö" and "history" not in item
        assert body["counts"]["dead"] == await db.raw.jobs.count_documents({"status": "dead"})
        assert "test.fatal" in body["kinds"] and "fn" not in body["kinds"]["test.fatal"]
        r = await _http("GET", f"/api/autopilot/jobs/{jd['id']}", OWNER)
        assert r.status_code == 200 and r.json()["last_error"] == "company deleted"
        assert (await _http("GET", "/api/autopilot/jobs/no-such-job", OWNER)).status_code == 404

        r = await _http("GET", "/api/autopilot/dead-letters", OPS_AGENT)  # internal staff may look
        assert r.status_code == 200
        body = r.json()
        listed = next(i for i in body["items"] if i["id"] == dl["id"])
        assert "trace" not in listed and listed["label_en"] == "Fatal test job" and listed["error"] == "company deleted"
        assert body["open"] == await db.raw.dead_letters.count_documents({"status": "open"})

        # ...but only settings.write may retry
        r = await _http("POST", f"/api/autopilot/dead-letters/{dl['id']}/retry", OPS_AGENT)
        assert r.status_code == 403
        assert (await db.raw.dead_letters.find_one({"id": dl["id"]}))["status"] == "open"
        assert (await _job(jd["id"]))["status"] == "dead"

        r = await _http("POST", f"/api/autopilot/dead-letters/{dl['id']}/retry", OWNER)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "retried" and r.json()["retried_by"]["id"] == OWNER["id"]
        doc = await _job(jd["id"])
        assert (doc["status"], doc["attempts"]) == ("queued", 0)
        assert await db.raw.audit_log.find_one({"action": "dead_letter_retried", "entity_id": jd["id"],
                                                "actor.id": OWNER["id"]})
        r = await _http("POST", f"/api/autopilot/dead-letters/{dl['id']}/retry", OWNER)
        assert r.status_code == 409

        # it fails again (JobFailed) -> a second dead letter, which the owner dismisses with a reason
        assert await work_once("w-api", kinds=["test.fatal"]) == 1
        dl2 = await db.raw.dead_letters.find_one({"job_id": jd["id"], "status": "open"}, {"_id": 0})
        assert dl2 and dl2["id"] != dl["id"]
        r = await _http("POST", f"/api/autopilot/dead-letters/{dl2['id']}/dismiss", OWNER, json={"note": "x"})
        assert r.status_code == 400
        r = await _http("POST", f"/api/autopilot/dead-letters/{dl2['id']}/dismiss", OWNER,
                        json={"note": "Asiakasyritys poistettu, ei uusita"})
        assert r.status_code == 200 and r.json()["status"] == "dismissed"
        assert (await _http("POST", f"/api/autopilot/dead-letters/{dl2['id']}/retry", OWNER)).status_code == 409
        r = await _http("GET", "/api/autopilot/dead-letters", OWNER, params={"status": "all"})
        statuses = {i["id"]: i["status"] for i in r.json()["items"]}
        assert statuses[dl["id"]] == "retried" and statuses[dl2["id"]] == "dismissed"

        # schedules
        assert (await _http("POST", "/api/autopilot/schedules/test.every/run", OPS_AGENT)).status_code == 403
        r = await _http("POST", "/api/autopilot/schedules/test.every/run", OWNER)
        assert r.status_code == 200
        manual = r.json()
        assert manual["kind"] == "test.noop" and manual["source"] == "manual"
        assert (await _job(manual["id"]))["status"] == "queued"
        assert (await _http("POST", "/api/autopilot/schedules/no.such.schedule/run", OWNER)).status_code == 404
        r = await _http("GET", "/api/autopilot/schedules", OWNER)
        assert r.status_code == 200
        sched = {s["name"]: s for s in r.json()["items"]}
        assert {"test.every", "test.daily", "monitoring.health_every_minute", "owner_queue.escalate_every_5_min"} <= set(sched)
        assert sched["test.every"]["last_job_id"] == manual["id"] and sched["test.every"]["last_job"]["status"] == "queued"
        assert (await _http("GET", "/api/autopilot/schedules", CUSTOMER)).status_code == 403
        assert await work_once("w-api", kinds=["test.noop"]) >= 1

        # kill switch through the API
        assert (await _http("POST", "/api/autopilot/kill", OPS_AGENT, json={"note": "testi"})).status_code == 403
        r = await _http("POST", "/api/autopilot/kill", OWNER, json={"note": "testi"})
        assert r.status_code == 200 and r.json()["halted"] is True and r.json()["autopilot_enabled"] is False
        st = await db.raw.settings.find_one({"key": "app"}, {"_id": 0})
        assert st["autopilot_halted"] is True and st["autopilot_halted_changed"]["by"]["id"] == OWNER["id"]
        try:
            ag = await enqueue("test.agentwork", {"via": "api"})
            assert await work_once("w-api", kinds=["test.agentwork"]) == 0
            r = await _http("GET", "/api/autopilot/status", OWNER)
            assert r.status_code == 200 and r.json()["jobs"]["held"] >= 1
            r = await _http("POST", "/api/autopilot/resume", OWNER, json={"confirmation": "yes"})
            assert r.status_code == 400
            assert (await db.raw.settings.find_one({"key": "app"}))["autopilot_halted"] is True
            r = await _http("POST", "/api/autopilot/resume", OWNER, json={"confirmation": "enable"})
            assert r.status_code == 200 and r.json()["halted"] is False
        finally:
            from core.settings import update_settings
            await update_settings({"autopilot_halted": False})
        assert await work_once("w-api", kinds=["test.agentwork"]) == 1
        assert (await _job(ag["id"]))["status"] == "done"
    run(go())


def test_schedule_can_be_paused_and_resumed_through_the_api_with_audit():
    """Live 2026-10-25: a crash-looping customs.offices_refresh could not be paused without halting all of Autopilot."""
    async def go():
        from core.db import db
        from core.jobs import fire_due_schedules, sync_schedules
        await sync_schedules()
        url = "/api/autopilot/schedules/test.every"
        assert (await _http("PATCH", url, json={"enabled": False, "note": "crash loop"})).status_code == 401
        assert (await _http("PATCH", url, CUSTOMER, json={"enabled": False, "note": "crash loop"})).status_code == 403
        assert (await _http("PATCH", url, OPS_AGENT, json={"enabled": False, "note": "crash loop"})).status_code == 403
        assert (await _http("PATCH", url, OWNER, json={"enabled": False, "note": "x"})).status_code == 422
        assert (await _http("PATCH", url, OWNER, json={"enabled": "no", "note": "crash loop"})).status_code == 422
        assert (await _http("PATCH", "/api/autopilot/schedules/no.such", OWNER,
                            json={"enabled": False, "note": "crash loop"})).status_code == 404
        r = await _http("PATCH", url, OWNER, json={"enabled": False, "note": "  crash loop on live  "})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False and r.json()["enabled_changed"]["note"] == "crash loop on live"
        assert r.json()["enabled_changed"]["by"]["id"] == OWNER["id"]
        try:
            past = iso(now() - timedelta(minutes=5))
            await db.raw.job_schedules.update_one({"name": "test.every"}, {"$set": {"next_run_at": past}})
            runs = (await db.raw.job_schedules.find_one({"name": "test.every"}))["runs"]
            await fire_due_schedules()
            await sync_schedules()  # a restart must not re-enable it
            s = await db.raw.job_schedules.find_one({"name": "test.every"}, {"_id": 0})
            assert s["enabled"] is False and s["runs"] == runs and s["next_run_at"] == past
            a = await db.audit_log.find_one({"entity": "job_schedule", "entity_id": "test.every",
                                             "action": "schedule_disabled"}, {"_id": 0})
            assert a and a["actor"]["id"] == OWNER["id"] and a["data"]["before"] == {"enabled": True}
            assert a["data"]["note"] == "crash loop on live"
        finally:
            r = await _http("PATCH", url, OWNER, json={"enabled": True, "note": "fix deployed"})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert await db.audit_log.find_one({"entity_id": "test.every", "action": "schedule_enabled"})
        await fire_due_schedules()
        assert (await db.raw.job_schedules.find_one({"name": "test.every"}))["runs"] == runs + 1
    run(go())


def test_ops_cancel_stops_a_queued_and_a_running_job_with_audit():
    """Live 2026-10-25: a customs.offices_refresh job that crashed the backend could not be stopped (agent=false)."""
    async def go():
        from core.db import db
        from core.jobs import enqueue, work_once
        queued = await enqueue("test.lease", {"n": "cancel-queued"})
        running = await enqueue("test.lease", {"n": "cancel-running"})
        await db.raw.jobs.update_one({"id": running["id"]}, {"$set": {
            "status": "running", "lease_owner": "w-crashing", "attempts": 1,
            "started_at": iso(now()), "lease_until": iso(now() + timedelta(minutes=5))}})
        url = f"/api/autopilot/jobs/{queued['id']}/cancel"
        assert (await _http("POST", url, json={"note": "stop it now"})).status_code == 401
        assert (await _http("POST", url, CUSTOMER, json={"note": "stop it now"})).status_code == 403
        assert (await _http("POST", url, OPS_AGENT, json={"note": "stop it now"})).status_code == 403
        assert (await _http("POST", url, OWNER, json={"note": "x"})).status_code == 422
        assert (await _http("POST", "/api/autopilot/jobs/no-such-job/cancel", OWNER, json={"note": "stop it now"})).status_code == 404
        r = await _http("POST", url, OWNER, json={"note": "crash-looping the backend"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled" and r.json()["cancelled"]["by"] == OWNER["id"]
        r = await _http("POST", f"/api/autopilot/jobs/{running['id']}/cancel", OWNER, json={"note": "crash-looping the backend"})
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        doc = await _job(running["id"])
        assert doc["lease_owner"] is None and doc["cancelled"]["lease_owner_before"] == "w-crashing"
        # the crashing worker's own result write is filtered on its lease: it cannot overwrite the cancel
        res = await db.raw.jobs.update_one({"id": running["id"], "lease_owner": "w-crashing"}, {"$set": {"status": "done"}})
        assert res.modified_count == 0
        assert await work_once("w-after-cancel", kinds=["test.lease"]) == 0
        assert queued["id"] not in CALLS and running["id"] not in CALLS
        assert (await _http("POST", url, OWNER, json={"note": "again please"})).status_code == 409
        a = await db.audit_log.find_one({"action": "job_cancelled", "entity_id": queued["id"]}, {"_id": 0})
        assert a["actor"]["id"] == OWNER["id"] and a["data"]["note"] == "crash-looping the backend"
        assert a["data"]["status_before"] == "queued" and a["data"]["kind"] == "test.lease"
        counts = (await _http("GET", "/api/autopilot/jobs?status=cancelled", OWNER)).json()
        assert {queued["id"], running["id"]} <= {j["id"] for j in counts["items"]} and counts["counts"]["cancelled"] >= 2
    run(go())


def test_worker_skips_jobs_of_a_disabled_schedule_including_manual_and_expired_lease_ones():
    async def go():
        from core.db import db
        from core.jobs import enqueue, sync_schedules, work_once
        await sync_schedules()
        await db.raw.job_schedules.update_one({"name": "test.heavy_daily"}, {"$set": {"enabled": False}})
        try:
            manual = await enqueue("test.heavy", {"n": "manual"}, source="ops")
            orphan = await enqueue("test.heavy", {"n": "orphan"})
            await db.raw.jobs.update_one({"id": orphan["id"]}, {"$set": {
                "status": "running", "lease_owner": "oom-killed", "attempts": 1,
                "started_at": iso(now() - timedelta(minutes=10)), "lease_until": iso(now() - timedelta(seconds=5))}})
            assert await work_once("w-skip", kinds=["test.heavy"]) == 2
            assert manual["id"] not in CALLS and orphan["id"] not in CALLS
            for jid in (manual["id"], orphan["id"]):
                doc = await _job(jid)
                assert (doc["status"], doc["lease_owner"]) == ("skipped", None)
                assert doc["skipped_reason"] == "Schedule test.heavy_daily is disabled"
            assert (await _job(orphan["id"]))["attempts"] == 1  # the skip does not use an attempt
            assert await db.raw.dead_letters.count_documents({"job_id": {"$in": [manual["id"], orphan["id"]]}}) == 0
            # a kind with no schedule, and a kind whose schedule is enabled, still run
            other = await enqueue("test.monitorwork", {"n": "unscheduled"})
            assert await work_once("w-skip", kinds=["test.monitorwork"]) == 1 and CALLS[other["id"]]
        finally:
            await db.raw.job_schedules.update_one({"name": "test.heavy_daily"}, {"$set": {"enabled": True}})
        again = await enqueue("test.heavy", {"n": "after resume"})
        assert await work_once("w-skip", kinds=["test.heavy"]) == 1 and CALLS[again["id"]] == ["w-skip"]
        assert (await _job(again["id"]))["status"] == "done"
    run(go())
