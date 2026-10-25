"""s2 r8: the exception agent is the single sender of the customer's notice about an exception, in process against a
throw-away MongoDB database (own database, dropped at the end).

What is proven: s14's milestone SMS no longer sends its own notice when a shipment turns "exception" (other milestones
still go out); the agent's notice goes out once (thread message + s14's exception SMS to opted-in users) under the one
key exception-notice:<exception id>, however often it runs; a pending AI draft about the exception is withdrawn when
the notice goes; the AI assessment path sees the key. Model off, EMAIL_PROVIDER=log, SMS_PROVIDER=log."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_exception_notice_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["SMS_PROVIDER"] = "log"
os.environ["LLM_PROVIDER"] = "none"
import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)  # core.db binds Motor to the current loop


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_exception_notice.py in its own pytest process", allow_module_level=True)

from core.db import db  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from services import messaging, notify_channels  # noqa: E402
from services.agents import exception_agent as xa  # noqa: E402

COMPANY = "co-exc"
AI = {"type": "ai", "id": "exception_agent", "name": "Huolitsija AI"}


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.settings.update_one({"key": "app"}, {"$set": {"key": "app", "sms_notifications_enabled": True}}, upsert=True))
    run(db.raw.companies.insert_one({"id": COMPANY, "name": "Poikkeama Oy", "business_id": "3990013-0", "test_tenant": True}))
    run(db.raw.users.insert_many([
        {"id": "u-sms", "company_id": COMPANY, "email": "sms@poikkeama.test", "phone": "040 765 4321", "notify_sms": True,
         "locale": "fi", "role": "customer_admin", "org_type": "customer"},
        {"id": "u-mail", "company_id": COMPANY, "email": "mail@poikkeama.test", "notify_sms": False, "locale": "fi",
         "role": "customer_user", "org_type": "customer"}]))
    yield
    run(db.raw.client.drop_database(os.environ["DB_NAME"]))


def _shipment_and_exception() -> tuple[dict, dict]:
    sid, eid = new_id(), new_id()
    s = {"id": sid, "reference": f"HUO-T-{sid[:6].upper()}", "company_id": COMPANY, "status": "exception", "test": True,
         "destination": {"name": "Tampere"}, "origin": {"name": "Hamburg"}, "eta": now_iso()}
    ex = {"id": eid, "shipment_id": sid, "company_id": COMPANY, "type": "delay", "severity": "medium", "status": "open",
          "title": "Vessel delayed 2 days", "title_i18n": {"fi": "Alus myöhässä 2 päivää"}, "customer_visible": True,
          "detected_by": "carrier", "created_at": now_iso(), "history": []}
    run(db.raw.shipments.insert_one(dict(s)))
    run(db.raw.exceptions.insert_one(dict(ex)))
    return s, ex


def _sms(sid: str) -> list[dict]:
    return run(db.raw.sms_outbox.find({"ref.shipment_id": sid}, {"_id": 0}).to_list(None))


def _customer_messages(sid: str) -> list[dict]:
    thread = run(db.raw.threads.find_one({"shipment_id": sid}, {"_id": 0, "id": 1})) or {}
    return run(db.raw.messages.find({"thread_id": thread.get("id")}, {"_id": 0}).to_list(None))


def test_s14_status_sms_leaves_the_exception_notice_to_the_agent():
    s, _ex = _shipment_and_exception()
    run(notify_channels._on_status({"shipment": s, "old_status": "in_transit", "new_status": "exception"}))
    assert _sms(s["id"]) == []
    run(notify_channels._on_status({"shipment": s, "old_status": "exception", "new_status": "delivered"}))
    assert [r["ref"]["status"] for r in _sms(s["id"])] == ["delivered"]  # other milestones still go out


def test_the_agent_sends_one_notice_under_one_key_and_withdraws_the_ai_draft():
    s, ex = _shipment_and_exception()
    thread = run(messaging.get_or_create_thread(company_id=COMPANY, shipment_id=s["id"], subject=s["reference"],
                                                created_by=AI))
    draft = run(messaging.add_message(thread, body="Hei, lähetyksenne on myöhässä.", author=AI, needs_approval=True,
                                      ai={"agent": "exception_agent", "exception_id": ex["id"]},
                                      approval_reason="Customer message about exception"))
    ex["ai_message_id"] = draft["id"]
    for _ in range(2):  # a retried job, or the owner approving the same card twice
        run(xa.send_exception_notice(ex, s, "Lähetyksessä on poikkeama.", "There is an exception on the shipment."))
    key = xa.notice_key(ex["id"])
    assert key == f"exception-notice:{ex['id']}" and run(xa.notice_sent(ex["id"]))
    sent = [m for m in _customer_messages(s["id"]) if (m.get("author") or {}).get("type") == "system"]
    assert len(sent) == 1 and sent[0]["body"] == "Lähetyksessä on poikkeama."
    sms = _sms(s["id"])
    assert len(sms) == 1 and sms[0]["user_id"] == "u-sms" and sms[0]["dedupe_key"] == f"{key}:u-sms"
    assert sms[0]["text"] == f"Huolitsija: lähetyksessä {s['reference']} on poikkeama. Hoidamme asian ja ilmoitamme uudesta arviosta."
    assert run(db.raw.messages.find_one({"id": draft["id"]}, {"_id": 0}))["status"] == "rejected"


def test_execute_notify_customer_goes_through_the_same_key():
    from services.exceptions_agent import resolve_exception
    s, ex = _shipment_and_exception()
    plan = {"actions": ["notify_customer"], "new_eta": now_iso()}
    out = run(xa.execute(ex, s, plan, None, resolve_exception))
    assert "customer_notified" in out["handled"], out
    run(xa.send_exception_notice(ex, s, "toinen", "second"))  # anything later with the same exception is a no-op
    sent = [m for m in _customer_messages(s["id"]) if (m.get("author") or {}).get("type") == "system"]
    assert len(sent) == 1 and sent[0]["body"].startswith(f"Lähetyksessä {s['reference']} on poikkeama: Alus myöhässä 2 päivää.")
    assert [r["dedupe_key"] for r in _sms(s["id"])] == [f"{xa.notice_key(ex['id'])}:u-sms"]
