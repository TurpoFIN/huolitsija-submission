"""s2: the Sourcing Autopilot cycle (s3, services/partner_network.py) runs as a durable core.jobs job, in process against
a throw-away MongoDB database (own database, dropped at the end).

What is proven: the cycle is the registered job `sourcing.cycle` on the schedule `sourcing.cycle_every_5_min` (no
background loop left); a failing cycle is retried with backoff and then dead-lettered with an owner alert; the inbox is
still read while the switch is off or Autopilot is halted; a lane request left "working" by a stopped process is
taken over after STALE_WORK_MINUTES, a fresh claim is not. The model and e-mail are off (EMAIL_PROVIDER=log)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_sourcing_jobs_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
os.environ.pop("PARTNER_IMAP_HOST", None)
os.environ.pop("PARTNER_NETWORK_KILL", None)
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)  # core.db binds Motor to the current loop


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_sourcing_jobs.py in its own pytest process", allow_module_level=True)

from core import jobs  # noqa: E402
from core.db import db  # noqa: E402
from core.lifecycle import _loops  # noqa: E402
from core.util import iso, new_id, now, now_iso  # noqa: E402
from services import partner_network as pn  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.settings.update_one({"key": "app"}, {"$set": {"key": "app", "partner_network_enabled": False,
                                                             "autopilot_halted": False}}, upsert=True))
    yield
    run(db.raw.client.drop_database(os.environ["DB_NAME"]))


def _set(**fields):
    run(db.raw.settings.update_one({"key": "app"}, {"$set": fields}))


def test_the_cycle_is_a_scheduled_job_not_a_background_loop():
    spec = jobs._JOBS["sourcing.cycle"]
    assert spec["agent"] is False and spec["max_attempts"] == 3 and spec["timeout"] == 900
    assert spec["label_fi"] == "Hankinta-autopilotin kierros"
    assert not any(getattr(fn, "__module__", "") == "services.partner_network" for _s, fn in _loops)
    run(jobs.sync_schedules())
    s = run(db.raw.job_schedules.find_one({"name": "sourcing.cycle_every_5_min"}, {"_id": 0}))
    assert s and s["job"] == "sourcing.cycle" and s["every_seconds"] == 300 and s["enabled"] is True
    assert s["label_fi"] == "Hankinta-autopilotti 5 minuutin välein"


def test_switch_off_or_autopilot_halted_still_reads_the_inbox_and_sends_nothing():
    _set(partner_network_enabled=False, autopilot_halted=False)
    out = run(pn.cycle_job({}))
    assert out["status"] == "off" and out["inbox"] == "not_configured" and out["retention"] in ("ran", "done_today")
    _set(partner_network_enabled=True, autopilot_halted=True)  # kill switch wins over the sourcing switch
    try:
        assert run(pn.is_on()) is False
        assert run(pn.cycle_job({}))["status"] == "off"
        # not an agent job: the worker runs it while Autopilot is halted (agent jobs wait)
        job = run(jobs.enqueue("sourcing.cycle", {}, key=f"cycle-halted-{new_id()}"))
        assert run(jobs.work_once("w-src", kinds=["sourcing.cycle"])) == 1
        done = run(db.raw.jobs.find_one({"id": job["id"]}, {"_id": 0}))
        assert done["status"] == "done" and done["result"]["status"] == "off"
    finally:
        _set(partner_network_enabled=False, autopilot_halted=False)
    assert run(db.partner_messages.count_documents({"direction": "out"})) == 0


def test_a_failing_cycle_is_retried_then_dead_lettered(monkeypatch):
    calls = []

    async def broken(actor=None, **kw):
        calls.append(1)
        raise RuntimeError("partner mailbox and database both down")
    monkeypatch.setattr(pn, "run_cycle", broken)
    _set(partner_network_enabled=True)
    try:
        job = run(jobs.enqueue("sourcing.cycle", {}, key=f"cycle-broken-{new_id()}"))
        for attempt in (1, 2, 3):
            assert run(jobs.work_once("w-src", kinds=["sourcing.cycle"])) == 1
            j = run(db.raw.jobs.find_one({"id": job["id"]}, {"_id": 0}))
            assert j["attempts"] == attempt and "database both down" in j["last_error"]
            if attempt < 3:
                assert j["status"] == "retry" and j["next_run_at"] > now_iso()  # backoff, not a tight loop
                run(db.raw.jobs.update_one({"id": job["id"]}, {"$set": {"next_run_at": iso(now() - timedelta(seconds=1))}}))
        assert j["status"] == "dead" and len(calls) == 3
        dl = run(db.raw.dead_letters.find_one({"job_id": job["id"]}, {"_id": 0}))
        assert dl and dl["kind"] == "sourcing.cycle"
    finally:
        _set(partner_network_enabled=False)


def test_a_lane_request_stranded_in_working_is_taken_over(monkeypatch):
    worked = []

    async def outreach(lane, settings, *, discover=True, request=None):
        worked.append(request["id"])
        return {"lane": lane, "rfqs": [], "open_before": 0}
    monkeypatch.setattr(pn, "outreach_lane", outreach)
    _set(partner_network_enabled=True)
    try:
        old = iso(now() - timedelta(minutes=pn.STALE_WORK_MINUTES + 5))
        fresh = iso(now() - timedelta(minutes=2))
        ids = {}
        for name, at in (("stranded", old), ("busy", fresh)):
            ids[name] = new_id()
            run(db.lane_rate_requests.insert_one({"id": ids[name], "lane": f"road:FI>{'EE' if name == 'busy' else 'SE'}",
                                                  "status": "working", "updated_at": at, "created_at": at,
                                                  "rate_request_ids": [], "quote_ids": []}))
        assert run(pn._work_request(ids["busy"]))["status"] == "busy_or_done"   # another worker holds a fresh claim
        assert run(pn._work_request(ids["stranded"]))["status"] == "no_candidates"
        assert worked == [ids["stranded"]]
        got = run(db.lane_rate_requests.find_one({"id": ids["stranded"]}, {"_id": 0}))
        assert got["status"] == "no_candidates" and got["updated_at"] > old
        assert run(db.lane_rate_requests.find_one({"id": ids["busy"]}, {"_id": 0}))["status"] == "working"
    finally:
        _set(partner_network_enabled=False)
