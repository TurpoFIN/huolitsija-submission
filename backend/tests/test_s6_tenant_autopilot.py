"""s6 r94: Autopilot for one test tenant only (core/autonomy.autopilot_scope, PATCH /api/autopilot/test-tenants/{id}).

The global switch stays off. A grant on a test tenant makes core.agent_runtime.decide() treat that tenant's decisions
as Autopilot-on while every guard (kill switch, legal points, confidence floor, spend, credit) still applies; another
tenant stays off; an expired, malformed or real-company grant fails closed; the kill switch ends every grant.

In process against a throw-away MongoDB database (own database, dropped at the end)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s6_tenant_ap_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s6_tenant_autopilot.py in its own pytest process", allow_module_level=True)

TID = str(uuid.uuid4())     # test tenant with the grant
TID2 = str(uuid.uuid4())    # another test tenant, never granted
REAL = str(uuid.uuid4())    # a real customer
OWNER = {"id": "owner-fi", "email": "omistaja@huolitsija.example", "name": "Omistaja", "role": "ops_admin",
         "org_type": "internal", "locale": "fi", "session_version": 0}
FIHEL = {"unlocode": "FIHEL", "code": "FIHEL", "name": "Helsinki", "country": "FI", "lat": 60.21, "lon": 25.19}
DEHAM = {"unlocode": "DEHAM", "code": "DEHAM", "name": "Hamburg", "country": "DE", "lat": 53.53, "lon": 9.93}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        await ensure_indexes()
        for cid, name, flag in ((TID, "E2E Testi Oy", True), (TID2, "Toinen Testi Oy", True), (REAL, "Oikea Asiakas Oy", None)):
            doc = {"id": cid, "name": name, "business_id": "3994713-8" if flag else "3596001-2", "country": "FI",
                   "kyc_status": "approved", "status": "active", "credit_limit_cents": 100000,
                   "created_at": "2026-01-01T00:00:00Z"}
            if flag:
                doc["test_tenant"] = True
            await db.raw.companies.insert_one(doc)
        await db.raw.users.insert_one(dict(OWNER))
    run(go())


def teardown_module(_m):
    from core.db import client
    run(client.drop_database(os.environ["DB_NAME"]))


def grant(cid=TID, enabled=True, minutes=30, note="S6 r94 test"):
    from routes.autopilot import TestTenantAutopilotIn, test_tenant_autopilot
    return run(test_tenant_autopilot(cid, TestTenantAutopilotIn(enabled=enabled, minutes=minutes, note=note), OWNER))


def scope(cid):
    from core.autonomy import autopilot_scope
    return run(autopilot_scope(cid))


def decide(cid, **kw):
    from core.agent_runtime import decide as d
    return run(d("kyc_agent", "approve_customer", subject=("company", cid), company_id=cid, create_approval=False, **kw))


def codes(reasons):
    return [r["code"] for r in reasons]


def set_company(cid, **fields):
    from core.db import db
    run(db.raw.companies.update_one({"id": cid}, {"$set": fields}))


def set_settings(**fields):
    from core.settings import update_settings
    run(update_settings(fields))


@pytest.fixture(autouse=True)
def _reset():
    set_settings(autopilot_enabled=False, autopilot_halted=False)
    for cid in (TID, TID2, REAL):
        set_company(cid, test_autopilot=None)
    yield


def test_grant_makes_only_that_test_tenant_auto_and_keeps_global_off():
    from core.db import db
    from core.settings import get_settings
    assert scope(TID) == "off" and codes(decide(TID).reasons) == ["autopilot_off"]
    out = grant(minutes=30)
    assert out["effective_scope"] == "test_tenant" and out["global_enabled"] is False
    exp = datetime.fromisoformat(out["test_autopilot"]["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=29) < exp - datetime.now(timezone.utc) <= timedelta(minutes=30)
    assert run(get_settings())["autopilot_enabled"] is False

    d = decide(TID)
    assert d.auto and d.reasons == []
    row = run(db.raw.agent_decisions.find_one({"id": d.id}, {"_id": 0}))
    assert row["outcome"] == "auto" and row["autopilot_scope"] == "test_tenant" and row["autopilot_enabled"] is False

    assert codes(decide(TID2).reasons) == ["autopilot_off"]  # another test tenant
    assert codes(decide(REAL).reasons) == ["autopilot_off"]  # a real customer
    from core.agent_runtime import decide as raw_decide
    no_company = run(raw_decide("kyc_agent", "approve_customer", subject=("company", TID), create_approval=False))
    assert codes(no_company.reasons) == ["autopilot_off"]  # no company on the decision: fails closed

    audits = run(db.raw.audit_log.find({"action": "test_tenant_autopilot_changed"}, {"_id": 0}).to_list(None))
    assert len(audits) == 1 and audits[0]["company_id"] == TID


def test_every_guard_still_blocks_inside_a_grant():
    grant()
    assert codes(decide(TID, confidence=0.1).reasons) == ["low_confidence"]
    assert codes(decide(TID, spend_cents=999999999).reasons) == ["spend_limit"]
    assert codes(decide(TID, exposure_cents=999999999).reasons) == ["credit_limit"]
    assert codes(decide(TID, legal_point="dangerous_goods").reasons) == ["legal"]
    assert codes(decide(TID, legal_point="not_a_legal_point").reasons) == ["legal"]
    set_settings(autopilot_halted=True)
    assert codes(decide(TID).reasons) == ["halted", "autopilot_off"]


def test_grant_fails_closed():
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases = [
        {"enabled": True, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"enabled": True, "expires_at": future[:-1]},                                    # naive timestamp
        {"enabled": True, "expires_at": "tomorrow"},
        {"enabled": True, "expires_at": None},
        {"enabled": "true", "expires_at": future},                                        # not a real boolean
        {"enabled": True, "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]
    for g in cases:
        set_company(TID, test_autopilot=g)
        assert scope(TID) == "off", g
    set_company(TID, test_autopilot={"enabled": True, "expires_at": future})
    assert scope(TID) == "test_tenant"
    set_company(TID, test_tenant=False)  # the company is no longer a test tenant
    try:
        assert scope(TID) == "off" and codes(decide(TID).reasons) == ["autopilot_off"]
    finally:
        set_company(TID, test_tenant=True)


def test_only_a_test_tenant_and_only_with_global_off():
    for cid in (REAL, "no-such-company"):
        with pytest.raises(HTTPException) as e:
            grant(cid)
        assert e.value.status_code == 404
    set_settings(autopilot_halted=True)
    with pytest.raises(HTTPException) as e:
        grant()
    assert e.value.status_code == 409
    set_settings(autopilot_halted=False, autopilot_enabled=True)
    with pytest.raises(HTTPException) as e:
        grant()
    assert e.value.status_code == 409
    assert scope(TID) == "global"


def test_body_is_strict():
    from routes.autopilot import TestTenantAutopilotIn
    for bad in ({"enabled": "yes", "note": "x"}, {"enabled": 1, "note": "x"}, {"enabled": True, "minutes": 0, "note": "x"},
                {"enabled": True, "minutes": 61, "note": "x"}, {"enabled": True, "minutes": "30", "note": "x"},
                {"enabled": True, "note": "   "}, {"enabled": True}, {"enabled": True, "note": "x", "global": True}):
        with pytest.raises(ValidationError):
            TestTenantAutopilotIn(**bad)
    assert TestTenantAutopilotIn(enabled=True, note=" x ").minutes == 30


def test_disable_and_kill_switch_end_the_grant():
    from core.db import db
    from routes.autopilot import KillIn, kill, status
    grant()
    assert [g["company_id"] for g in run(status(OWNER))["test_tenant_autopilot"]] == [TID]
    off = grant(enabled=False)
    assert off["effective_scope"] == "off" and off["test_autopilot"]["expires_at"] is None
    grant()
    st = run(kill(KillIn(note="test"), OWNER))
    assert st["halted"] is True and st["test_tenant_autopilot"] == []
    c = run(db.raw.companies.find_one({"id": TID}, {"_id": 0}))
    assert c["test_autopilot"]["enabled"] is False and c["test_autopilot"]["revoked"]["why"] == "kill_switch"
    set_settings(autopilot_halted=False)  # resume does not bring the grant back
    assert scope(TID) == "off"


def test_quote_send_inside_policy_is_autonomous_only_for_the_granted_tenant():
    from core.db import db
    from core.settings import get_settings
    from routes import quotes as rq

    def quote(tag, cid, total, cost):
        q = {"id": f"q-{tag}", "number": f"Q-{tag}", "company_id": cid, "customer_name": "Testi", "status": "draft",
             "source": "ops",
             "request": {"mode": "sea", "origin": dict(FIHEL), "destination": dict(DEHAM), "cargo_summary": "1 × 40' HC",
                         "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 9000}},
             "options": [{"id": f"opt-{tag}", "mode": "sea", "carrier_name": "Alpha Lines", "total_cents": total,
                          "cost_cents": cost, "lines": [{"code": "OCEAN_FREIGHT", "amount_cents": total - 3500},
                                                        {"code": "DOC_FEE", "amount_cents": 3500}]}]}
        run(db.raw.quotes.insert_one(dict(q)))
        return run(rq.apply_approval_policy(q, run(get_settings()), OWNER, send=True))

    set_settings(quote_approval_threshold_eur=5000.0)
    grant()
    inside = quote("in", TID, 600000, 480000)          # 6 000 EUR > approval threshold, inside the Autopilot policy
    assert inside["status"] == "sent" and inside["approval"]["decided_by"]["id"] == "autopilot"
    other = quote("other", TID2, 600000, 480000)       # same quote, another test tenant: the owner reviews it
    assert other["status"] == "pending_approval"
    above = quote("above", TID, 1200000, 900000)       # 12 000 EUR > autopilot_quote_max_cents: the owner reviews it
    assert above["status"] == "pending_approval"
    rows = run(db.raw.agent_decisions.find({"subject_id": "q-in"}, {"_id": 0}).to_list(None))
    assert [(r["outcome"], r["autopilot_scope"]) for r in rows] == [("auto", "test_tenant")]


def test_ai_customer_reply_goes_out_only_for_the_granted_tenant():
    from core.db import db
    from services import messaging
    for tid, cid in (("th-1", TID), ("th-2", TID2)):
        run(db.raw.threads.insert_one({"id": tid, "company_id": cid, "company_name": "Testi", "subject": "Missä lähetys?",
                                       "status": "open", "created_at": "2026-10-25T00:00:00Z"}))
    ai = {"type": "ai", "id": "comms_agent", "name": "Viestiagentti"}
    grant()
    sent = run(messaging.add_message(run(db.raw.threads.find_one({"id": "th-1"}, {"_id": 0})), body="Lähetys on matkalla.",
                                     author=ai, notify=False))
    held = run(messaging.add_message(run(db.raw.threads.find_one({"id": "th-2"}, {"_id": 0})), body="Lähetys on matkalla.",
                                     author=ai, notify=False))
    assert sent["status"] == "sent" and held["status"] == "pending_approval"
    set_settings(autopilot_halted=True)
    halted = run(messaging.add_message(run(db.raw.threads.find_one({"id": "th-1"}, {"_id": 0})), body="Toinen vastaus.",
                                       author=ai, notify=False))
    assert halted["status"] == "pending_approval"


def test_a_grant_can_name_its_agents():
    from routes.autopilot import TestTenantAutopilotIn, test_tenant_autopilot
    out = run(test_tenant_autopilot(TID, TestTenantAutopilotIn(enabled=True, minutes=10, note="journey",
                                                               agents=["email_channel", "comms_agent"]), OWNER))
    assert out["test_autopilot"]["agents"] == ["comms_agent", "email_channel"] and out["effective_scope"] == "test_tenant"
    assert codes(decide(TID).reasons) == ["autopilot_off"]  # kyc_agent is not in the grant
    from core.agent_runtime import decide as d
    ok = run(d("comms_agent", "send_reply", subject=("thread", "t1"), company_id=TID, create_approval=False))
    assert ok.auto
    from core.autonomy import autopilot_scope
    assert run(autopilot_scope(TID, agent="email_channel")) == "test_tenant"
    assert run(autopilot_scope(TID, agent="quote_agent")) == "off"
    assert run(autopilot_scope(TID)) == "off"  # a caller that names no agent never gets an agent-scoped grant
    from core.autonomy import active_test_grants
    listed = run(active_test_grants())  # status, the health digest and the sweep key still see the grant
    assert [(g["company_id"], g["agents"]) for g in listed] == [(TID, ["comms_agent", "email_channel"])]
    with pytest.raises(HTTPException) as e:
        run(test_tenant_autopilot(TID, TestTenantAutopilotIn(enabled=True, note="x", agents=["no_such_agent"]), OWNER))
    assert e.value.status_code == 422


def test_self_monitoring_health_names_an_active_grant_until_it_ends():
    from core.db import db
    from services.monitoring import deep_health
    grant(enabled=False)
    assert run(deep_health(store=False))["checks"]["autopilot"]["test_tenant_grants"] == []
    g = grant(minutes=5)["test_autopilot"]
    a = run(deep_health(store=False))["checks"]["autopilot"]
    assert a["status"] == "ok" and a["test_tenant_grants"] == [
        {"company_id": TID, "name": "E2E Testi Oy", "expires_at": g["expires_at"], "agents": None}]
    assert "test-tenant grant active" in a["en"] and g["expires_at"] in a["en"] and "testiasiakkaan lupa" in a["fi"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run(db.raw.companies.update_one({"id": TID}, {"$set": {"test_autopilot.expires_at": past}}))  # the grant runs out
    a = run(deep_health(store=False))["checks"]["autopilot"]
    assert a["test_tenant_grants"] == [] and a["en"] == "Autopilot off"
    grant(enabled=False)
