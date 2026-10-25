"""s3 r2: an ETA-slip exception found by the rule is handled end to end by the exception agent under a test-tenant
grant that names only the exception agent: the customer is told the new ETA, the plan moves to it (eta_replanned,
replans[], a "replanned" event, an audit row) and the exception closes. eta_planned stays the booking promise.
A slip that needs a re-booking stops at the carrier step (carrier agent not in the grant) and the plan waits.
Own throw-away MongoDB database, model off, EMAIL_PROVIDER=log, SMS_PROVIDER=log."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s3_replan_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["SMS_PROVIDER"] = "log"
os.environ["LLM_PROVIDER"] = "none"
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
    pytest.skip("run test_s3_exception_replan.py in its own pytest process", allow_module_level=True)

from core.autonomy import set_test_grant  # noqa: E402
from core.db import db  # noqa: E402
from core.util import iso, new_id, now  # noqa: E402
from services import exceptions_agent as det  # noqa: E402
from services.agents import exception_agent as xa  # noqa: E402

COMPANY = "co-s3-replan"
OWNER = {"id": "owner-1", "name": "Omistaja", "role": "ops_admin", "org_type": "internal"}


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.settings.update_one({"key": "app"}, {"$set": {"key": "app", "autopilot_enabled": False, "autopilot_halted": False}},
                                   upsert=True))
    run(db.raw.companies.insert_one({"id": COMPANY, "name": "TEST Replan Oy", "business_id": "3990014-9", "test_tenant": True}))
    run(db.raw.users.insert_one({"id": "u-mail", "company_id": COMPANY, "email": "marc+replan@heliosone.fi", "notify_sms": False,
                                 "locale": "fi", "role": "customer_admin", "org_type": "customer"}))
    yield
    run(db.raw.client.drop_database(os.environ["DB_NAME"]))


def _shipment(slip_h: float, departed: bool) -> dict:
    sid = new_id()
    planned = now() + timedelta(days=3)
    eta = planned + timedelta(hours=slip_h)
    leg = {"seq": 1, "mode": "sea", "from": {"name": "Rauma", "unlocode": "FIRAU", "lat": 61.13, "lon": 21.43},
           "to": {"name": "Bremerhaven", "unlocode": "DEBRV", "lat": 53.55, "lon": 8.58}, "eta": iso(eta),
           "atd": iso(now() - timedelta(hours=6)) if departed else None, "ata": None,
           "status": "in_progress" if departed else "planned"}
    s = {"id": sid, "reference": f"HUO-T-{sid[:6].upper()}", "company_id": COMPANY, "status": "in_transit" if departed else "booked",
         "test": True, "mode": "sea", "origin": {"name": "Rauma"}, "destination": {"name": "Bremerhaven"}, "legs": [leg],
         "eta": iso(eta), "eta_planned": iso(planned), "milestones": [], "created_at": iso(now())}
    run(db.raw.shipments.insert_one(dict(s)))
    return s


def _rule_exception(sid: str) -> dict:
    run(det.run_detection(assess=False))
    ex = run(db.raw.exceptions.find_one({"rule_key": f"eta:{sid}"}, {"_id": 0}))
    assert ex and ex["detected_by"] == "rule" and ex["type"] == "delay", ex
    return ex


def _grant(agents):
    return run(set_test_grant(COMPANY, True, 5, "s3 r2 exception journey", OWNER, agents=agents))


def test_eta_slip_is_replanned_notified_and_closed_by_the_exception_agent_alone():
    s = _shipment(slip_h=30, departed=True)
    ex = _rule_exception(s["id"])
    assert _grant(["exception_agent"])["effective_scope"] == "test_tenant"
    out = run(xa.handle({"payload": {"exception_id": ex["id"]}}))
    assert out["status"] == "resolved" and "customer_notified" in out["handled"], out
    assert not any("rebook" in x for x in out["handled"] if isinstance(x, dict))  # the vessel has left: no re-booking

    got = run(db.raw.shipments.find_one({"id": s["id"]}, {"_id": 0}))
    assert got["eta_replanned"] == s["eta"] and got["eta_planned"] == s["eta_planned"]  # the promise is kept for KPIs
    assert [(r["from"], r["to"], r["exception_id"]) for r in got["replans"]] == [(s["eta_planned"], s["eta"], ex["id"])]
    ev = run(db.raw.tracking_events.find_one({"shipment_id": s["id"], "code": "replanned"}, {"_id": 0}))
    assert ev["source"] == "exception_agent" and ev["raw"]["replan"]["slip_hours"] == 30.0
    assert run(db.raw.audit_log.find_one({"action": "shipment.replanned", "entity_id": s["id"]}, {"_id": 0}))
    done = run(db.raw.exceptions.find_one({"id": ex["id"]}, {"_id": 0}))
    assert done["status"] == "resolved" and "plan moved from" in done["resolution"] and "(Autopilot)" in done["resolution"]
    assert run(xa.notice_sent(ex["id"]))
    decision = run(db.raw.agent_decisions.find_one({"id": done["agent"]["decision_id"]}, {"_id": 0}))
    assert decision["outcome"] == "auto" and decision["autopilot_scope"] == "test_tenant"

    run(det.run_detection(assess=False))  # the condition is gone: nothing reopens
    assert run(db.raw.exceptions.count_documents({"rule_key": f"eta:{s['id']}", "status": {"$in": ["open", "in_progress"]}})) == 0


def test_a_slip_that_needs_a_rebooking_stops_at_the_carrier_step_and_the_plan_waits():
    s = _shipment(slip_h=60, departed=False)
    ex = _rule_exception(s["id"])
    _grant(["exception_agent"])
    out = run(xa.handle({"payload": {"exception_id": ex["id"]}}))
    rebook = next(x["rebook"] for x in out["handled"] if isinstance(x, dict) and "rebook" in x)
    assert rebook["status"] == "owner_queue", out  # carrier_agent is not in the grant: a person decides the carrier
    assert {"replan": {"status": "waits_for_rebook"}} in out["handled"] and out["status"] == "in_progress"
    got = run(db.raw.shipments.find_one({"id": s["id"]}, {"_id": 0}))
    assert "eta_replanned" not in got and run(db.raw.exceptions.find_one({"id": ex["id"]}, {"_id": 0}))["status"] == "in_progress"


def test_without_a_grant_the_plan_goes_to_the_owner_queue_and_nothing_moves():
    run(set_test_grant(COMPANY, False, 5, "off", OWNER))
    s = _shipment(slip_h=30, departed=True)
    ex = _rule_exception(s["id"])
    out = run(xa.handle({"payload": {"exception_id": ex["id"]}}))
    assert out["owner_queue"] and out["plan"]["actions"] == ["notify_customer", "replan", "resolve"]
    assert "eta_replanned" not in run(db.raw.shipments.find_one({"id": s["id"]}, {"_id": 0}))
