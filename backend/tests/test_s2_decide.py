"""s2 decision gate: core/agent_runtime.decide() with the Autopilot switch, kill switch, legal human points and every
guardrail; settings guardrails (core/settings.py, core/autonomy.py); the owner queue (core/approvals.py: SLA fields,
silent agent_decides cards); owner notifications (bell + e-mail via EMAIL_PROVIDER=log); and the monitoring jobs in
services/monitoring.py (owner_queue.escalate, monitoring.health, monitoring.digest_daily) run through core.jobs.work_once.

In process against a throw-away MongoDB database (own database, dropped at the end). Assertions read approvals,
agent_decisions, notifications, email_outbox, alerts, system and owner_reports back from MongoDB, and compare guardrail
texts with literal Finnish strings, not with the code's own formatter."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_decide_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
os.environ.pop("OWNER_ALERT_EMAIL", None)
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)  # core.events -> nothing async at import, but keep Motor on this loop whatever imports first


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_decide.py in its own pytest process", allow_module_level=True)

from core.events import on  # noqa: E402
from core.util import iso, now, parse_iso  # noqa: E402

HELSINKI = ZoneInfo("Europe/Helsinki")
DECIDED: list[dict] = []


@on("approval.decided")
async def _capture_decided(p):
    DECIDED.append(p)


CID = str(uuid.uuid4())       # customer with its own credit limit
CID_NEW = str(uuid.uuid4())   # new customer: credit_limit_cents 0 -> the default limit applies
CID_OTHER = str(uuid.uuid4())
OWNER = {"id": "owner-fi", "email": "omistaja@huolitsija.example", "name": "Omistaja", "role": "ops_admin",
         "org_type": "internal", "locale": "fi", "notify_email": True, "session_version": 0, "email_verified": True}
OWNER_EN = {"id": "owner-en", "email": "owner-en@huolitsija.example", "name": "Owner EN", "role": "ops_admin",
            "org_type": "internal", "locale": "en", "notify_email": True, "session_version": 0, "email_verified": True}
DISABLED_ADMIN = {"id": "admin-disabled", "email": "entinen@huolitsija.example", "name": "Entinen", "role": "ops_admin",
                  "org_type": "internal", "locale": "fi", "disabled": True, "session_version": 0}
OPS_AGENT = {"id": "ops-agent", "email": "operaattori@huolitsija.example", "name": "Operaattori", "role": "ops_agent",
             "org_type": "internal", "locale": "fi", "session_version": 0}
CUSTOMER = {"id": "customer-admin", "email": "asiakas@testikuljetus.example", "name": "Asiakas", "role": "customer_admin",
            "org_type": "customer", "company_id": CID, "locale": "fi", "session_version": 0}
NOT_OWNERS = [DISABLED_ADMIN["id"], OPS_AGENT["id"], CUSTOMER["id"]]
S: dict = {}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        import services.monitoring  # noqa: F401  (escalate / health / digest jobs and their event handlers)
        await ensure_indexes()
        for cid, name, limit in ((CID, "Luotto Testi Oy", 200000), (CID_NEW, "Uusi Asiakas Oy", 0),
                                 (CID_OTHER, "Muu Yritys Oy", 100)):
            await db.raw.companies.insert_one({"id": cid, "name": name, "business_id": "3990009-9", "country": "FI",
                                               "kyc_status": "approved", "status": "active", "credit_limit_cents": limit,
                                               "created_at": "2026-01-01T00:00:00Z"})
        for u in (OWNER, OWNER_EN, DISABLED_ADMIN, OPS_AGENT, CUSTOMER):
            await db.raw.users.insert_one(dict(u))
    run(go())


def teardown_module(_m):
    from core.db import client, db
    run(client.drop_database(db.raw.name))  # the database this module really used (os.environ may be another module's)
    _LOOP.close()


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _codes(reasons: list[dict]) -> list[str]:
    return [r["code"] for r in reasons]


async def _card(ref_id: str) -> dict | None:
    from core.db import db
    return await db.raw.approvals.find_one({"ref_id": ref_id}, {"_id": 0})


async def _row(decision_id: str) -> dict:
    from core.db import db
    return await db.raw.agent_decisions.find_one({"id": decision_id}, {"_id": 0})


async def _autopilot_on():
    from core.autonomy import switch
    from core.settings import get_settings
    await switch("autopilot_enabled", True, "enable", OWNER)
    s = await get_settings()
    assert s["autopilot_enabled"] is True and s["autopilot_halted"] is False


async def _work(kind: str) -> dict:
    from core.jobs import enqueue, work_once
    jd = await enqueue(kind, {})
    assert await work_once("w-decide", kinds=[kind]) == 1
    from core.db import db
    return await db.raw.jobs.find_one({"id": jd["id"]}, {"_id": 0})


async def _decide(**kw):
    from core.agent_runtime import decide
    agent = kw.pop("agent", "exception_agent")
    action = kw.pop("action", "test_action")
    return await decide(agent, action, **kw)


# --------------------------------------------------------------------------------------------- Autopilot off
def test_autopilot_off_queues_card_with_sla_reasons_notifications_and_decision_log():
    async def go():
        from core.db import db
        from core.settings import get_settings
        assert (await get_settings())["autopilot_enabled"] is False  # the default
        sid = _sid("exc")
        d = await _decide(agent="exception_agent", action="reroute_shipment", subject=("exception", sid),
                          inputs={"exception_id": sid, "eta_delay_h": 30}, decision={"plan": "reroute", "carrier": "DSV"},
                          confidence=0.93, model="gpt-6.1-sol", cost_cents=4, company_id=CID,
                          approval_kind="exception_action", title="Reroute delayed shipment",
                          title_fi="Viivästyneen lähetyksen uudelleenreititys", summary="ETA slipped 30 h",
                          summary_fi="Saapumisaika siirtyi 30 h", link=f"/ops/exceptions/{sid}", amount_cents=12000,
                          subject_label="HUO-123")
        assert d.auto is False and d.outcome == "owner_queue"
        assert _codes(d.reasons) == ["autopilot_off"]

        card = await _card(sid)
        assert card and card["id"] == d.approval["id"]
        assert (card["kind"], card["status"], card["ref_type"]) == ("exception_action", "pending", "exception")
        assert card["title"] == "Reroute delayed shipment" and card["title_fi"] == "Viivästyneen lähetyksen uudelleenreititys"
        assert card["summary_fi"] == "Saapumisaika siirtyi 30 h" and card["amount_cents"] == 12000
        assert card["reason"] == "Autopilot is off" and card["reason_fi"] == "Autopilotti on pois päältä"
        assert card["reasons"] == [{"code": "autopilot_off", "fi": "Autopilotti on pois päältä", "en": "Autopilot is off"}]
        assert card["agent"] == "exception_agent" and card["agent_decision_id"] == d.id
        assert card["data"]["agent_decision_id"] == d.id and card["data"]["action"] == "reroute_shipment"
        assert card["data"]["decision"] == {"plan": "reroute", "carrier": "DSV"} and card["data"]["confidence"] == 0.93
        assert card["requested_by"]["id"] == "agent:exception_agent" and card["requested_by"]["type"] == "agent"
        assert card["company_id"] == CID and card["link"] == f"/ops/exceptions/{sid}"
        # SLA: exception_action has its own 4 h
        assert card["sla_hours"] == 4 and card["escalation_level"] == 0 and card["escalated_at"] is None
        sla = (parse_iso(card["sla_due_at"]) - parse_iso(card["created_at"])).total_seconds()
        assert abs(sla - 4 * 3600) <= 2, sla

        row = await _row(d.id)
        assert row["agent"] == "exception_agent" and row["agent_label_fi"] == "Poikkeamat"
        assert row["action"] == "reroute_shipment" and (row["subject_type"], row["subject_id"]) == ("exception", sid)
        assert row["subject_label"] == "HUO-123" and row["company_id"] == CID
        assert row["inputs"] == {"exception_id": sid, "eta_delay_h": 30}
        assert row["decision"] == {"plan": "reroute", "carrier": "DSV"}
        assert row["confidence"] == 0.93 and row["model"] == "gpt-6.1-sol"
        assert row["cost_cents"] == 4 and row["spend_cents"] == 0
        assert row["outcome"] == "owner_queue" and row["autopilot_enabled"] is False
        assert row["reasons"] == card["reasons"] and row["approval_id"] == card["id"]
        g = row["guardrails"]
        assert g["confidence_floor"] == 0.85 and g["credit"] == {} and g["claim"] == {}
        assert g["spend"]["this_cents"] == 4 and g["spend"]["limit_cents"] == 500000  # AI cost is committed money too
        assert row["title_fi"] == "Viivästyneen lähetyksen uudelleenreititys"

        # owner notification: one bell + one e-mail per active internal ops admin, nobody else
        link = f"/ops/approvals?focus={card['id']}"
        fi_bell = await db.raw.notifications.find({"user_id": OWNER["id"], "link": link}, {"_id": 0}).to_list(None)
        assert len(fi_bell) == 1
        assert fi_bell[0]["kind"] == "approval" and fi_bell[0]["read"] is False
        assert fi_bell[0]["title_i18n"] == {"fi": "Hyväksyttävää: Viivästyneen lähetyksen uudelleenreititys",
                                            "en": "To approve: Reroute delayed shipment"}
        assert fi_bell[0]["body_i18n"]["fi"] == "Saapumisaika siirtyi 30 h Vastaa 4 tunnin kuluessa."
        assert fi_bell[0]["body_i18n"]["en"] == "ETA slipped 30 h Respond within 4 hours."
        assert await db.raw.notifications.count_documents({"user_id": OWNER_EN["id"], "link": link}) == 1
        assert await db.raw.notifications.count_documents({"link": link}) == 2
        assert await db.raw.notifications.count_documents({"user_id": {"$in": NOT_OWNERS}}) == 0
        mails = await db.raw.email_outbox.find({"to": OWNER["email"]}, {"_id": 0}).to_list(None)
        assert len(mails) == 1
        assert mails[0]["subject"] == "Hyväksyttävää: Viivästyneen lähetyksen uudelleenreititys"
        assert mails[0]["provider"] == "log" and mails[0]["status"] == "logged" and mails[0]["tag"] == "approval"
        assert "Vastaa 4 tunnin kuluessa." in mails[0]["text"]
        en_mail = await db.raw.email_outbox.find_one({"to": OWNER_EN["email"]}, {"_id": 0})
        assert en_mail["subject"] == "To approve: Reroute delayed shipment"
        assert await db.raw.email_outbox.count_documents({"to": {"$in": [DISABLED_ADMIN["email"], OPS_AGENT["email"],
                                                                         CUSTOMER["email"]]}}) == 0
    run(go())


def test_kind_without_own_sla_uses_the_owner_queue_sla_setting():
    async def go():
        from core.db import db
        from core.settings import update_settings
        await update_settings({"owner_queue_sla_hours": 10})
        try:
            sid = _sid("credit")
            d = await _decide(agent="kyc_agent", action="raise_credit_limit", subject=("company", sid), confidence=0.99,
                              approval_kind="credit_limit", title="Raise credit limit", title_fi="Luottorajan nosto",
                              summary="To 10 000 EUR", summary_fi="10 000 euroon")
            card = await _card(sid)
            assert d.outcome == "owner_queue" and card["sla_hours"] == 10
            sla = (parse_iso(card["sla_due_at"]) - parse_iso(card["created_at"])).total_seconds()
            assert abs(sla - 10 * 3600) <= 2
            bell = await db.raw.notifications.find_one({"user_id": OWNER["id"], "link": f"/ops/approvals?focus={card['id']}"})
            assert bell["body_i18n"]["fi"] == "10 000 euroon Vastaa 10 tunnin kuluessa."
        finally:
            await update_settings({"owner_queue_sla_hours": 24})
    run(go())


def test_blocked_when_no_card_is_wanted():
    async def go():
        sid = _sid("nocard")
        d = await _decide(subject=("exception", sid), confidence=0.99)  # no approval_kind
        assert (d.auto, d.outcome, d.approval) == (False, "blocked", None)
        d2 = await _decide(subject=("exception", sid), confidence=0.99, approval_kind="exception_action",
                           create_approval=False)
        assert (d2.outcome, d2.approval) == ("blocked", None)
        assert await _card(sid) is None
        assert (await _row(d.id))["outcome"] == "blocked" and (await _row(d.id))["approval_id"] is None
    run(go())


def test_agent_decides_card_is_silent():
    async def go():
        from core.approvals import request_approval
        from core.db import db
        silent_ref, loud_ref = _sid("pay"), _sid("pay")
        silent = await request_approval("payment_match", "payment", silent_ref, "Match payment",
                                        extra={"agent_decides": True, "title_fi": "Maksun kohdistus"})
        loud = await request_approval("payment_match", "payment", loud_ref, "Match payment",
                                      extra={"title_fi": "Maksun kohdistus"})
        for c in (silent, loud):
            stored = await _card(c["ref_id"])
            assert stored["status"] == "pending" and stored["sla_hours"] == 48 and stored["sla_due_at"]
        assert (await _card(silent_ref))["agent_decides"] is True
        assert await db.raw.notifications.count_documents({"link": f"/ops/approvals?focus={silent['id']}"}) == 0
        assert await db.raw.notifications.count_documents({"link": f"/ops/approvals?focus={loud['id']}"}) == 2
        # the same pending work asked twice is one card
        again = await request_approval("payment_match", "payment", loud_ref, "Match payment")
        assert again["id"] == loud["id"]
        assert await db.raw.approvals.count_documents({"ref_id": loud_ref}) == 1
    run(go())


# --------------------------------------------------------------------------------------------- settings guardrails
def test_switch_needs_the_typed_word_and_turns_autopilot_on():
    async def go():
        from core.autonomy import switch
        from core.db import db
        from core.settings import get_settings, update_settings
        for bad in ("", "yes", "Enable", "enable ", "ENABLE"):
            with pytest.raises(HTTPException) as e:
                await switch("autopilot_enabled", True, bad, OWNER)
            assert e.value.status_code == 400 and e.value.detail == "Type enable to enable autonomous actions"
        assert (await get_settings())["autopilot_enabled"] is False
        for name, value in (("autopilot_halted", False), ("guard_spend_per_day_cents", True), ("autopilot_enabled", "true")):
            with pytest.raises(HTTPException) as e:
                await switch(name, value, "enable", OWNER)
            assert e.value.status_code == 400 and e.value.detail == "Unknown autonomy switch"

        sid = _sid("exc")
        d0 = await _decide(subject=("exception", sid), confidence=0.99, approval_kind="exception_action", title="Act")
        assert d0.outcome == "owner_queue" and _codes(d0.reasons) == ["autopilot_off"]  # control: off -> queued

        await update_settings({"autopilot_halted": True})  # a kill switch left on ...
        res = await switch("autopilot_enabled", True, "enable", OWNER)
        assert res["autopilot_enabled"] is True and res["autopilot_halted"] is False  # ... is lifted by the typed word
        raw = await db.raw.settings.find_one({"key": "app"}, {"_id": 0})
        assert raw["autopilot_enabled_changed"]["by"] == {"id": OWNER["id"], "name": OWNER["name"]}
        assert raw["autopilot_enabled_changed"]["enabled"] is True
        audit = await db.raw.audit_log.find_one({"action": "autonomy_switch_changed", "data.after": True}, {"_id": 0})
        assert audit and audit["data"] == {"setting": "autopilot_enabled", "before": False, "after": True}

        sid2 = _sid("exc")
        d = await _decide(subject=("exception", sid2), confidence=0.99, approval_kind="exception_action", title="Act",
                          decision={"plan": "notify_customer"})
        assert (d.auto, d.outcome, d.reasons, d.approval) == (True, "auto", [], None)
        assert await _card(sid2) is None
        row = await _row(d.id)
        assert row["outcome"] == "auto" and row["approval_id"] is None and row["autopilot_enabled"] is True

        # switching off needs no typed word
        res = await switch("autopilot_enabled", False, "", OWNER)
        assert res["autopilot_enabled"] is False
        await _autopilot_on()
    run(go())


def test_validate_settings_refuses_switches_and_bad_limits():
    from core.autonomy import validate_settings
    refused = [
        ({"autopilot_enabled": True}, "Use the typed confirmation to change autonomy switches"),
        ({"autopilot_halted": False}, "Use the typed confirmation to change autonomy switches"),
        ({"partner_network_enabled": True}, "Use the typed confirmation to change autonomy switches"),
        ({"customs_filing_delegated": True}, "Use the typed confirmation to change autonomy switches"),
        ({"customs_delegation_scope": "direct_and_indirect"}, "Use the typed confirmation to change autonomy switches"),
        ({"guard_spend_per_day_cents": -1}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"guard_spend_per_day_cents": 100_000_001}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"guard_spend_per_day_cents": 1500.5}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"guard_claim_limit_cents": "100000"}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"insurance_claim_limit_cents": True}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"guard_default_credit_limit_cents": None}, "Limit must be a whole number of cents from 0 to 1 000 000 EUR"),
        ({"guard_confidence_floor": 0.49}, "Confidence floor must be between 0.5 and 1"),
        ({"guard_confidence_floor": 1.01}, "Confidence floor must be between 0.5 and 1"),
        ({"guard_confidence_floor": True}, "Confidence floor must be between 0.5 and 1"),
        ({"guard_confidence_floor": "0.9"}, "Confidence floor must be between 0.5 and 1"),
        ({"owner_queue_sla_hours": 366}, "Response time and reminder interval must be whole hours from 1 to 365"),
        ({"owner_queue_sla_hours": 0}, "Response time and reminder interval must be whole hours from 1 to 365"),
        ({"owner_escalation_hours": 2.5}, "Response time and reminder interval must be whole hours from 1 to 365"),
        ({"owner_escalation_hours": 0}, "Response time and reminder interval must be whole hours from 1 to 365"),
        ({"customs_confirm_timeout_hours": 366}, "Hours and days must be whole numbers from 0 to 365"),
        ({"dunning_reminder1_days": 7, "dunning_reminder2_days": 21, "dunning_final_days": 35, "dunning_collection_days": 30},
         "Dunning steps must come in order: reminder, second reminder, final notice, collection"),
        ({"owner_alert_email": "omistaja at huolitsija"}, "Enter a valid e-mail address"),
    ]
    for body, detail in refused:
        with pytest.raises(HTTPException) as e:
            validate_settings(dict(body))
        assert e.value.status_code == 400 and e.value.detail == detail, body
    for body in ({"guard_spend_per_day_cents": 0}, {"guard_spend_per_day_cents": 100_000_000},
                 {"guard_confidence_floor": 0.5}, {"guard_confidence_floor": 1}, {"owner_queue_sla_hours": 1},
                 {"owner_escalation_hours": 365}, {"customs_confirm_timeout_hours": 0},
                 {"owner_alert_email": ""}, {"owner_alert_email": "omistaja@huolitsija.fi"},
                 {"dunning_reminder1_days": 7, "dunning_reminder2_days": 21, "dunning_final_days": 35,
                  "dunning_collection_days": 45}):
        validate_settings(dict(body))  # accepted: no exception


def test_update_settings_keeps_only_known_keys():
    async def go():
        from core.db import db
        from core.settings import DEFAULTS, get_settings, update_settings
        out = await update_settings({"not_a_setting": 1, "guard_claim_limit_cents": 123})
        assert "not_a_setting" not in out and out["guard_claim_limit_cents"] == 123
        assert "not_a_setting" not in (await db.raw.settings.find_one({"key": "app"}))
        await update_settings({"guard_claim_limit_cents": DEFAULTS["guard_claim_limit_cents"]})
        assert (await get_settings())["guard_claim_limit_cents"] == 100000
    run(go())


# --------------------------------------------------------------------------------------------- guardrails in decide()
def test_kill_switch_blocks_auto_even_with_autopilot_on():
    async def go():
        from core.settings import update_settings
        await _autopilot_on()
        await update_settings({"autopilot_halted": True})
        try:
            sid = _sid("exc")
            d = await _decide(subject=("exception", sid), confidence=0.99, approval_kind="exception_action", title="Act")
            assert d.auto is False and d.outcome == "owner_queue"
            assert _codes(d.reasons) == ["halted"]
            assert (await _card(sid))["reason_fi"] == "Hätäpysäytys on päällä"
        finally:
            await update_settings({"autopilot_halted": False})
    run(go())


def test_dangerous_goods_is_never_auto_and_the_card_carries_the_legal_basis():
    async def go():
        await _autopilot_on()
        sid = _sid("shp")
        d = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", sid), confidence=0.99,
                          legal_point="dangerous_goods", approval_kind="booking_review", title="Accept DG booking",
                          title_fi="Vaarallisten aineiden varaus")
        assert d.auto is False and d.outcome == "owner_queue"
        assert _codes(d.reasons) == ["legal"] and d.reasons[0]["legal_point"] == "dangerous_goods"
        assert d.reasons[0]["fi"] == ("Vaarallisten aineiden kuljetus: Vaarallisten aineiden tiedot ja hyväksyntä "
                                      "tarkistaa koulutettu henkilö.")
        card = await _card(sid)
        assert card["legal_point"] == "dangerous_goods"
        assert card["legal_basis"] == "IMDG Code 1.3 and 5.4.1; ADR 1.3 and 5.4.1; IATA DGR 1.5 (trained personnel)"
        assert card["legal_reason_fi"] == "Vaarallisten aineiden tiedot ja hyväksyntä tarkistaa koulutettu henkilö."
        assert card["legal_title_fi"] == "Vaarallisten aineiden kuljetus"
        assert card["sla_hours"] == 4
        assert (await _row(d.id))["legal_point"] == "dangerous_goods"
    run(go())


def test_unknown_legal_point_code_is_not_auto():
    async def go():
        await _autopilot_on()
        sid = _sid("shp")
        d = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", sid), confidence=0.99,
                          legal_point="dangerous_good", approval_kind="booking_review", title="Accept DG booking")
        assert d.auto is False
    run(go())


def test_low_confidence_reason_uses_a_decimal_comma_in_finnish():
    async def go():
        from core.settings import update_settings
        await _autopilot_on()
        sid = _sid("doc")
        d = await _decide(agent="document_agent", action="accept_document", subject=("document", sid), confidence=0.72,
                          approval_kind="document_review", title="Check document", title_fi="Tarkista asiakirja")
        assert d.auto is False and _codes(d.reasons) == ["low_confidence"]
        assert d.reasons[0]["fi"] == "Varmuus 0,72 alittaa rajan 0,85"
        assert d.reasons[0]["en"] == "Confidence 0.72 is below the floor 0.85"
        card = await _card(sid)
        assert card["reason_fi"] == "Varmuus 0,72 alittaa rajan 0,85" and card["sla_hours"] == 8
        # exactly at the floor is enough; no confidence (no model involved) counts as certain
        assert (await _decide(subject=("document", _sid("doc")), confidence=0.85, approval_kind="document_review")).auto
        assert (await _decide(subject=("document", _sid("doc")), confidence=None, approval_kind="document_review")).auto
        await update_settings({"guard_confidence_floor": 0.9})
        try:
            d2 = await _decide(subject=("document", _sid("doc")), confidence=0.88, approval_kind="document_review")
            assert _codes(d2.reasons) == ["low_confidence"] and d2.reasons[0]["fi"] == "Varmuus 0,88 alittaa rajan 0,90"
            assert (await _row(d2.id))["guardrails"]["confidence_floor"] == 0.9
        finally:
            await update_settings({"guard_confidence_floor": 0.85})
    run(go())


def test_spend_limit_counts_earlier_auto_decisions_of_the_helsinki_day():
    async def go():
        from core.db import db
        from core.settings import update_settings
        await _autopilot_on()
        # this test owns "today": earlier auto decisions of this module are moved to 2000
        await db.raw.agent_decisions.update_many({"outcome": "auto"}, {"$set": {"at": "2000-01-01T00:00:00Z"}})
        local_midnight = datetime.now(HELSINKI).replace(hour=0, minute=0, second=0, microsecond=0)
        await db.raw.agent_decisions.insert_one({"id": _sid("yday"), "at": iso(local_midnight - timedelta(minutes=1)),
                                                 "agent": "carrier_agent", "outcome": "auto", "spend_cents": 9000,
                                                 "cost_cents": 0})  # yesterday in Helsinki: does not count today
        await db.raw.agent_decisions.insert_one({"id": _sid("early"), "at": iso(local_midnight + timedelta(minutes=1)),
                                                 "agent": "carrier_agent", "outcome": "auto", "spend_cents": 600,
                                                 "cost_cents": 400})  # 00:01 Helsinki (still "yesterday" in UTC): counts
        await update_settings({"guard_spend_per_day_cents": 10000})
        try:
            q = await _decide(agent="carrier_agent", action="book_carrier", subject=("shipment", _sid("shp")),
                              spend_cents=9000, confidence=0.5, approval_kind="carrier_booking", title="Book")
            assert _codes(q.reasons) == ["low_confidence"]  # 1000 + 9000 fits, but it is queued: queued money is not spent

            a = await _decide(agent="carrier_agent", action="book_carrier", subject=("shipment", _sid("shp")),
                              spend_cents=5000, approval_kind="carrier_booking", title="Book")
            assert a.auto, a.reasons
            assert (await _row(a.id))["guardrails"]["spend"] == {"today_cents": 1000, "limit_cents": 10000, "this_cents": 5000}

            b = await _decide(agent="carrier_agent", action="book_carrier", subject=("shipment", _sid("shp")),
                              spend_cents=3500, cost_cents=500, approval_kind="carrier_booking", title="Book")
            assert b.auto, b.reasons  # 6000 + 4000 = the limit exactly
            assert (await _row(b.id))["guardrails"]["spend"] == {"today_cents": 6000, "limit_cents": 10000, "this_cents": 4000}

            sid = _sid("shp")
            c = await _decide(agent="carrier_agent", action="book_carrier", subject=("shipment", sid), cost_cents=1,
                              approval_kind="carrier_booking", title="Book", title_fi="Varaa kuljetus")
            assert c.auto is False and _codes(c.reasons) == ["spend_limit"]
            assert c.reasons[0]["fi"] == "Päivän kulutusraja 100,00 € ylittyisi (100,01 €)"
            assert c.reasons[0]["en"] == "Today's spend limit 100,00 € would be exceeded (100,01 €)"
            assert (await _row(c.id))["guardrails"]["spend"] == {"today_cents": 10000, "limit_cents": 10000, "this_cents": 1}
            assert (await _card(sid))["reason_fi"] == "Päivän kulutusraja 100,00 € ylittyisi (100,01 €)"
        finally:
            await update_settings({"guard_spend_per_day_cents": 500000})
    run(go())


def test_credit_limit_uses_open_invoices_and_uninvoiced_booked_work():
    async def go():
        from core.agent_runtime import credit_position
        from core.db import db
        await _autopilot_on()
        s_ref, s_booked = _sid("shp"), _sid("shp")
        await db.raw.invoices.insert_many([
            {"id": _sid("inv"), "company_id": CID, "kind": "invoice", "status": "open", "total_cents": 120000,
             "paid_cents": 20000, "shipment_id": s_ref},                                        # 100 000 open
            {"id": _sid("inv"), "company_id": CID, "kind": "invoice", "status": "overdue", "total_cents": 30000,
             "paid_cents": 0, "credited_cents": 10000},                                         # 20 000 open
            {"id": _sid("inv"), "company_id": CID, "kind": "invoice", "status": "paid", "total_cents": 500000,
             "paid_cents": 500000},                                                             # paid: no
            {"id": _sid("inv"), "company_id": CID, "kind": "credit_note", "status": "open", "total_cents": 70000},  # no
            {"id": _sid("inv"), "company_id": CID, "kind": "invoice", "status": "draft", "total_cents": 400000},    # no
            {"id": _sid("inv"), "company_id": CID_OTHER, "kind": "invoice", "status": "open", "total_cents": 999999},
        ])
        await db.raw.shipments.insert_many([
            {"id": s_booked, "company_id": CID, "status": "booked", "price": {"total_cents": 30000}, "invoice_ids": []},
            {"id": _sid("shp"), "company_id": CID, "status": "cancelled", "price": {"total_cents": 800000}},
            {"id": _sid("shp"), "company_id": CID, "status": "delivered", "price": {"total_cents": 600000},
             "invoice_ids": ["inv-x"]},
            {"id": s_ref, "company_id": CID, "status": "delivered", "price": {"total_cents": 700000}},  # invoiced above
        ])
        pos = await credit_position(CID)
        assert pos == {"limit_cents": 200000, "limit_source": "company", "open_cents": 120000, "booked_cents": 30000,
                       "used_cents": 150000, "hold": False}

        ok = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", _sid("shp")),
                           company_id=CID, exposure_cents=50000, approval_kind="booking_review", title="Book")
        assert ok.auto, ok.reasons  # 150 000 + 50 000 = the limit exactly
        sid = _sid("shp")
        over = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", sid), company_id=CID,
                             exposure_cents=50001, approval_kind="booking_review", title="Book")
        assert over.auto is False and _codes(over.reasons) == ["credit_limit"]
        assert over.reasons[0]["fi"] == "Asiakkaan luottoraja 2 000,00 € ylittyisi (2 000,01 €)"
        g = (await _row(over.id))["guardrails"]["credit"]
        assert (g["used_cents"], g["limit_cents"], g["limit_source"], g["this_cents"]) == (150000, 200000, "company", 50001)
        assert (await _card(sid))["reason_fi"] == "Asiakkaan luottoraja 2 000,00 € ylittyisi (2 000,01 €)"
        # re-pricing the booked shipment itself does not count it twice
        again = await _decide(agent="booking_agent", action="reprice", subject=("shipment", s_booked), company_id=CID,
                              exposure_cents=80000, exclude_shipment_id=s_booked, approval_kind="booking_review")
        assert again.auto, again.reasons
        # a new customer (credit_limit_cents 0) gets guard_default_credit_limit_cents
        pos_new = await credit_position(CID_NEW)
        assert (pos_new["limit_cents"], pos_new["limit_source"], pos_new["used_cents"]) == (500000, "default", 0)
        new = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", _sid("shp")),
                            company_id=CID_NEW, exposure_cents=500001, approval_kind="booking_review")
        assert _codes(new.reasons) == ["credit_limit"]
        assert new.reasons[0]["fi"] == "Asiakkaan luottoraja 5 000,00 € ylittyisi (5 000,01 €)"
        # no company -> no credit check
        assert (await _decide(subject=("shipment", _sid("shp")), exposure_cents=10**9, approval_kind="booking_review")).auto
        # credit control / collections put the customer on hold: no auto work for it even inside the limit
        await db.raw.companies.update_one({"id": CID_NEW}, {"$set": {"credit_hold": True}})
        held = await _decide(agent="booking_agent", action="accept_booking", subject=("shipment", _sid("shp")),
                             company_id=CID_NEW, exposure_cents=100, approval_kind="booking_review")
        assert held.auto is False and _codes(held.reasons) == ["credit_hold"]
        assert held.reasons[0]["fi"] == "Asiakkaan luotto on pysäytetty"
        await db.raw.companies.update_one({"id": CID_NEW}, {"$unset": {"credit_hold": ""}})
    run(go())


def test_claim_limit_and_claim_above_insurance_limit():
    async def go():
        from core.settings import update_settings
        await _autopilot_on()
        # no insurance policy recorded (insurance_claim_limit_cents 0): every claim is the owner's legal decision
        sid0 = _sid("clm")
        d0 = await _decide(agent="claims_agent", action="settle_claim", subject=("claim", sid0), claim_cents=100,
                           approval_kind="claim", title="Settle claim")
        assert _codes(d0.reasons) == ["legal"] and d0.reasons[0]["legal_point"] == "claim_above_insurance_limit"
        await update_settings({"guard_claim_limit_cents": 100000, "insurance_claim_limit_cents": 500000})
        try:
            ok = await _decide(agent="claims_agent", action="settle_claim", subject=("claim", _sid("clm")),
                               claim_cents=100000, approval_kind="claim", title="Settle claim")
            assert ok.auto, ok.reasons  # at the limit
            sid = _sid("clm")
            over = await _decide(agent="claims_agent", action="settle_claim", subject=("claim", sid), claim_cents=100001,
                                 approval_kind="claim", title="Settle claim", title_fi="Korvauksen maksu")
            assert over.auto is False and _codes(over.reasons) == ["claim_limit"]
            assert over.reasons[0]["fi"] == "Korvausraja 1 000,00 € ylittyy (1 000,01 €)"
            assert (await _row(over.id))["guardrails"]["claim"] == {"claim_cents": 100001, "limit_cents": 100000,
                                                                     "insurance_limit_cents": 500000}
            card = await _card(sid)
            assert card["sla_hours"] == 72 and "legal_point" not in card
            sid2 = _sid("clm")
            ins = await _decide(agent="claims_agent", action="settle_claim", subject=("claim", sid2), claim_cents=500001,
                                approval_kind="claim", title="Settle claim")
            assert _codes(ins.reasons) == ["legal"] and ins.reasons[0]["legal_point"] == "claim_above_insurance_limit"
            card2 = await _card(sid2)
            assert card2["legal_point"] == "claim_above_insurance_limit"
            assert card2["legal_basis"].startswith("Vakuutussopimuslaki 543/1994")
            assert card2["legal_title_fi"] == "Korvausvaatimus yli vakuutuksen ratkaisuvaltuuden"
            assert (await _row(ins.id))["legal_point"] == "claim_above_insurance_limit"
        finally:
            await update_settings({"guard_claim_limit_cents": 100000, "insurance_claim_limit_cents": 0})
    run(go())


# --------------------------------------------------------------------------------------------- one card per work item
async def _two_decisions_same_card():
    from core.db import db
    await _autopilot_on()
    sid = _sid("doc")
    d1 = await _decide(agent="document_agent", action="accept_document", subject=("document", sid), confidence=0.6,
                       decision={"accept": True, "hs": "8471"}, approval_kind="document_review",
                       title="Check commercial invoice", title_fi="Tarkista kauppalasku")
    link = f"/ops/approvals?focus={d1.approval['id']}"
    bells = await db.raw.notifications.count_documents({"link": link})
    d2 = await _decide(agent="document_agent", action="accept_document", subject=("document", sid), confidence=0.99,
                       decision={"accept": True, "hs": "8473"}, legal_point="dangerous_goods",
                       approval_kind="document_review", title="Check commercial invoice", title_fi="Tarkista kauppalasku")
    return sid, d1, d2, link, bells


def test_second_decide_reuses_the_pending_card_and_refreshes_its_reasons():
    async def go():
        from core.db import db
        sid, d1, d2, link, bells = await _two_decisions_same_card()
        assert _codes(d1.reasons) == ["low_confidence"] and _codes(d2.reasons) == ["legal"]
        assert d2.approval["id"] == d1.approval["id"]
        assert await db.raw.approvals.count_documents({"ref_id": sid}) == 1
        card = await _card(sid)
        assert card["status"] == "pending"
        assert _codes(card["reasons"]) == ["legal"]
        assert card["reason_fi"] == ("Vaarallisten aineiden kuljetus: Vaarallisten aineiden tiedot ja hyväksyntä "
                                     "tarkistaa koulutettu henkilö.")
        assert card["agent_decision_id"] == d2.id and card["legal_point"] == "dangerous_goods"
        assert (await _row(d2.id))["approval_id"] == card["id"] and (await _row(d1.id))["approval_id"] == card["id"]
        assert await db.raw.notifications.count_documents({"link": link}) == bells == 2  # no second bell for the same card
    run(go())


def test_refreshed_card_english_reason_is_refreshed_too():
    async def go():
        sid, d1, d2, link, bells = await _two_decisions_same_card()
        card = await _card(sid)
        assert card["reason"] == ("Dangerous goods transport: Dangerous goods data and acceptance are checked by a "
                                  "trained person."), card["reason"]
    run(go())


def test_refreshed_card_data_points_to_the_latest_decision():
    async def go():
        sid, d1, d2, link, bells = await _two_decisions_same_card()
        card = await _card(sid)
        assert card["data"]["agent_decision_id"] == d2.id
        assert card["data"]["decision"] == {"accept": True, "hs": "8473"}
        assert card["data"]["confidence"] == 0.99
    run(go())


# --------------------------------------------------------------------------------------------- approve_as_agent
def test_approve_as_agent_emits_approval_decided():
    async def go():
        from core.agent_runtime import approve_as_agent
        sid = _sid("pay")
        d = await _decide(agent="payments_agent", action="match_payment", subject=("payment", sid), confidence=0.5,
                          approval_kind="payment_match", title="Match payment")
        DECIDED.clear()
        a = await approve_as_agent(d.approval, "payments_agent")
        assert a["status"] == "approved" and a["decided_by"] == {"id": "agent:payments_agent", "name": "Autopilot · Payments"}
        assert a["decision_note"] == "Autopilot" and a["decided_at"]
        assert (await _card(sid))["status"] == "approved"
        assert len(DECIDED) == 1
        assert DECIDED[0]["approval"]["id"] == d.approval["id"] and DECIDED[0]["approval"]["status"] == "approved"
        assert DECIDED[0]["actor"]["type"] == "agent" and DECIDED[0]["actor"]["id"] == "agent:payments_agent"
        assert DECIDED[0]["actor"]["org_type"] == "internal"
        assert await approve_as_agent(d.approval, "payments_agent") is None  # already decided: nothing, no event
        assert len(DECIDED) == 1
    run(go())


# --------------------------------------------------------------------------------------------- monitoring jobs
def test_overdue_card_is_escalated_by_the_owner_queue_job():
    async def go():
        from core.agent_runtime import approve_as_agent
        from core.db import db
        await _autopilot_on()
        sid = _sid("inv")
        d = await _decide(agent="invoice_agent", action="issue_invoice", subject=("invoice", sid), confidence=0.5,
                          approval_kind="invoice_issue", title="Issue invoice", title_fi="Laskun lähetys")
        card = d.approval
        link = f"/ops/approvals?focus={card['id']}"
        assert card["sla_hours"] == 24
        # the card was created 29 h ago with its 24 h SLA: 5 h overdue, its first bell rang 29 h ago
        await db.raw.approvals.update_one({"id": card["id"]}, {"$set": {"created_at": iso(now() - timedelta(hours=29)),
                                                                        "sla_due_at": iso(now() - timedelta(hours=5))}})
        await db.raw.notifications.update_many({"link": link}, {"$set": {"created_at": iso(now() - timedelta(hours=29))}})
        overdue = await db.raw.approvals.count_documents({"status": "pending", "sla_due_at": {"$lt": iso(now())}})
        assert overdue == 1  # only this card is late, so the job result is about it

        j = await _work("owner_queue.escalate")
        assert j["status"] == "done" and j["result"] == {"escalated": 1}
        c = await _card(sid)
        assert c["escalation_level"] == 1 and abs((parse_iso(c["escalated_at"]) - now()).total_seconds()) < 30
        bell = await db.raw.notifications.find_one({"user_id": OWNER["id"], "link": link,
                                                    "title_i18n.fi": "Myöhässä (1. muistutus): Laskun lähetys"}, {"_id": 0})
        assert bell, "the reminder must ring the owner's bell"
        assert bell["body_i18n"]["fi"] == "Vastausaika ylittyi 5 tuntia sitten. Varmuus 0,50 alittaa rajan 0,85"
        assert await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": "Myöhässä (1. muistutus): Laskun lähetys"})
        assert await db.raw.email_outbox.find_one({"to": OWNER_EN["email"], "subject": "Overdue (reminder 1): Issue invoice"})
        assert await db.raw.alerts.count_documents({"key": f"approval_overdue:{card['id']}"}) == 0  # level 1: no alert yet

        j = await _work("owner_queue.escalate")  # at once again: owner_escalation_hours (4 h) not passed
        assert j["result"] == {"escalated": 0} and (await _card(sid))["escalation_level"] == 1

        await db.raw.approvals.update_one({"id": card["id"]}, {"$set": {"escalated_at": iso(now() - timedelta(hours=5))}})
        j = await _work("owner_queue.escalate")
        assert j["result"] == {"escalated": 1} and (await _card(sid))["escalation_level"] == 2
        assert await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": "Myöhässä (2. muistutus): Laskun lähetys"})
        alert = await db.raw.alerts.find_one({"key": f"approval_overdue:{card['id']}", "status": "open"}, {"_id": 0})
        assert alert and alert["severity"] == "warning" and alert["link"] == link
        assert alert["title_fi"] == "Hyväksyntä myöhässä: Laskun lähetys" and alert["body_fi"] == "2. muistutus lähetetty"
        assert await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": "Hälytys: Hyväksyntä myöhässä: Laskun lähetys"})

        await approve_as_agent(card, "invoice_agent")  # deciding the card closes its overdue alert
        alert = await db.raw.alerts.find_one({"key": f"approval_overdue:{card['id']}"}, {"_id": 0})
        assert alert["status"] == "resolved" and alert["resolution_note"] == "decided"
        j = await _work("owner_queue.escalate")
        assert j["result"] == {"escalated": 0}
    run(go())


def test_health_job_stores_checks_and_opens_and_resolves_alerts():
    async def go():
        from core.agent_runtime import approve_as_agent
        from core.db import db
        from core.jobs import heartbeat
        sid = _sid("rate")
        d = await _decide(agent="rates_agent", action="update_rate", subject=("rate", sid), confidence=0.5,
                          approval_kind="rate_update", title="Update rate", title_fi="Hinnan päivitys")
        await db.raw.approvals.update_one({"id": d.approval["id"]}, {"$set": {"sla_due_at": iso(now() - timedelta(hours=1))}})
        assert await db.raw.worker_heartbeats.count_documents({}) == 0  # no worker has reported in this database

        j = await _work("monitoring.health")
        assert j["status"] == "done" and j["result"] == {"status": "fail"}
        h = await db.raw.system.find_one({"_id": "health"})
        assert h["status"] == "fail" and abs((parse_iso(h["checked_at"]) - now()).total_seconds()) < 30
        assert {"database", "worker", "queue", "schedules", "dead_letters", "owner_queue", "autopilot", "events", "ai",
                "email", "payments", "reconciliation", "sanctions_lists"} <= set(h["checks"])
        assert h["checks"]["database"]["status"] == "ok"
        assert h["checks"]["worker"]["status"] == "fail"
        assert h["checks"]["email"] == {"status": "warn", "fi": "Sähköpostit tallennetaan lokiin, niitä ei lähetetä",
                                        "en": "E-mails are logged, not delivered"}
        overdue = await db.raw.approvals.count_documents({"status": "pending", "sla_due_at": {"$lt": iso(now())}})
        assert overdue >= 1
        assert h["checks"]["owner_queue"]["status"] == "warn" and h["checks"]["owner_queue"]["overdue"] == overdue
        assert h["checks"]["dead_letters"]["status"] == "ok"

        worker = await db.raw.alerts.find_one({"key": "health:worker", "status": "open"}, {"_id": 0})
        assert worker and worker["severity"] == "critical"
        assert worker["title_fi"] == "Työjonon käsittelijä ei ole kuitannut toimintaansa"
        oq = await db.raw.alerts.find_one({"key": "health:owner_queue", "status": "open"}, {"_id": 0})
        assert oq and oq["severity"] == "warning" and oq["title_fi"] == f"{overdue} hyväksyntää myöhässä"
        rec = await db.raw.alerts.find_one({"key": "health:reconciliation", "status": "open"}, {"_id": 0})
        assert rec and rec["title_fi"] == "Täsmäytystä ei ole vielä ajettu" and rec["count"] == 1
        assert await db.raw.alerts.count_documents({"key": "health:dead_letters"}) == 0
        assert await db.raw.email_outbox.find_one({"to": OWNER["email"],
                                                   "subject": "Hälytys: Työjonon käsittelijä ei ole kuitannut toimintaansa"})

        # the worker reports in and the late cards are decided: the next run closes those alerts
        await heartbeat()
        async for a in db.raw.approvals.find({"status": "pending", "sla_due_at": {"$lt": iso(now())}}, {"_id": 0}):
            await approve_as_agent(a, "rates_agent")
        j = await _work("monitoring.health")
        assert j["result"]["status"] == "warn"  # logged e-mail, no reconciliation yet, no Stripe key
        h = await db.raw.system.find_one({"_id": "health"})
        assert h["checks"]["worker"]["status"] == "ok" and h["checks"]["owner_queue"]["status"] == "ok"
        for key in ("health:worker", "health:owner_queue"):
            assert await db.raw.alerts.count_documents({"key": key, "status": "open"}) == 0, key
            assert (await db.raw.alerts.find_one({"key": key}))["status"] == "resolved"
        assert await db.raw.alerts.count_documents({"key": "health:reconciliation", "status": "open"}) == 1
        assert (await db.raw.alerts.find_one({"key": "health:reconciliation", "status": "open"}))["count"] == 2
    run(go())


def test_daily_digest_job_mails_the_owner_in_finnish():
    async def go():
        from core.approvals import request_approval
        from core.db import db
        from core.settings import update_settings
        await _autopilot_on()
        # the owner queue of this digest: exactly three cards, one of them late, one a legal point
        await db.raw.approvals.update_many({"status": "pending"}, {"$set": {"status": "cancelled"}})
        late_card = await request_approval("invoice_issue", "invoice", _sid("inv"), "Issue invoice",
                                           extra={"agent_decides": True, "title_fi": "Laskun lähetys"})
        await db.raw.approvals.update_one({"id": late_card["id"]}, {"$set": {"sla_due_at": iso(now() - timedelta(hours=2))}})
        await request_approval("booking_review", "shipment", _sid("shp"), "Accept DG booking",
                               extra={"agent_decides": True, "title_fi": "Vaarallisten aineiden varaus",
                                      "legal_point": "dangerous_goods"})
        await request_approval("rate_update", "rate", _sid("rate"), "Update rate",
                               extra={"agent_decides": True, "title_fi": "Hinnan päivitys"})
        await update_settings({"owner_alert_email": "omistaja.posti@huolitsija.example"})
        try:
            j = await _work("monitoring.digest_daily")
        finally:
            await update_settings({"owner_alert_email": ""})
        assert j["status"] == "done" and j["result"]["sent"] == 3, j  # fi owner, en owner, owner mailbox
        rid = j["result"]["report_id"]
        # the report's own day and 24 h window, not the test's clock: independent of midnight and of decisions written
        # after the job ran
        rep = await db.raw.owner_reports.find_one({"id": rid}, {"_id": 0})
        day, window = rep["day"], {"$gte": rep["data"]["from"], "$lte": rep["data"]["to"]}
        assert day in (datetime.now(HELSINKI).date().isoformat(), (datetime.now(HELSINKI) - timedelta(days=1)).date().isoformat())
        n = {o: await db.raw.agent_decisions.count_documents({"at": window, "outcome": o})
             for o in ("auto", "owner_queue", "blocked")}
        assert n == {o: rep["data"]["decisions"].get(o, 0) for o in n}, (n, rep["data"]["decisions"])
        assert n["auto"] > 0 and n["owner_queue"] > 0 and n["blocked"] > 0  # earlier tests made each kind

        fi = await db.raw.email_outbox.find_one({"to": OWNER["email"], "tag": "digest"}, {"_id": 0})
        assert fi["subject"] == f"Huolitsija: päivän kooste {day}"
        assert fi["provider"] == "log" and fi["status"] == "logged" and fi["ref"] == {"type": "owner_report", "id": rid}
        assert "Autopilotti päällä" in fi["text"]
        assert (f"Agenttien päätökset 24 h: {n['auto']} tehty itse, {n['owner_queue']} omistajalle, "
                f"{n['blocked']} estetty.") in fi["text"]
        assert "Hyväksyttävää: 3, joista myöhässä 1." in fi["text"]
        assert "  – Laskun lähetys\n" in fi["text"] and "  – Vaarallisten aineiden varaus (lakisääteinen)" in fi["text"]
        assert "/ raja 5 000,00 €." in fi["text"]
        en = await db.raw.email_outbox.find_one({"to": OWNER_EN["email"], "tag": "digest"}, {"_id": 0})
        assert en["subject"] == f"Huolitsija: daily digest {day}"
        assert f"Agent decisions in 24 h: {n['auto']} done, {n['owner_queue']} to the owner" in en["text"]
        assert "To approve: 3, 1 overdue." in en["text"] and "  – Accept DG booking (legally required)" in en["text"]
        box = await db.raw.email_outbox.find_one({"to": "omistaja.posti@huolitsija.example", "tag": "digest"}, {"_id": 0})
        assert box["subject"] == f"Huolitsija: päivän kooste {day}"
        assert "Agenttien päätökset" in box["text"] and "Agent decisions" in box["text"]
        assert await db.raw.email_outbox.count_documents({"tag": "digest"}) == 3
        assert await db.raw.email_outbox.count_documents({"tag": "digest", "to": {"$in": [DISABLED_ADMIN["email"],
                                                                                           OPS_AGENT["email"]]}}) == 0
        report = await db.raw.owner_reports.find_one({"id": rid}, {"_id": 0})
        assert report["kind"] == "digest" and report["day"] == day
        assert sorted(s["to"] for s in report["sent_to"]) == sorted([OWNER["email"], OWNER_EN["email"],
                                                                     "omistaja.posti@huolitsija.example"])
        assert {s["status"] for s in report["sent_to"]} == {"logged"}
        assert report["data"]["decisions"]["auto"] == n["auto"]
    run(go())


def test_daily_digest_day_after_helsinki_midnight(monkeypatch):
    from datetime import timezone
    from unittest.mock import AsyncMock
    from services import monitoring
    from core.db import db

    # Helsinki is already on October 25 while UTC is still October 24.
    monkeypatch.setattr(monitoring, "now", lambda: datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(monitoring, "build_digest", AsyncMock(return_value={}))
    monkeypatch.setattr(monitoring, "owner_users", AsyncMock(return_value=[]))
    monkeypatch.setattr(monitoring, "owner_extra_email", AsyncMock(return_value=None))

    async def go():
        result = await monitoring.digest_job({})
        report = await db.raw.owner_reports.find_one({"id": result["report_id"]})
        assert report["day"] == "2026-10-25"
    run(go())


def test_daily_digest_counts_every_pending_card():
    async def go():
        from core.approvals import request_approval
        from core.db import db
        for i in range(25):
            await request_approval("payment_match", "payment", _sid("bulk"), f"Match payment {i}",
                                   extra={"agent_decides": True})
        pending = await db.raw.approvals.count_documents({"status": "pending"})
        assert pending > 20
        j = await _work("monitoring.digest_daily")
        mail = await db.raw.email_outbox.find_one({"to": OWNER["email"], "tag": "digest", "ref.id": j["result"]["report_id"]})
        assert f"Hyväksyttävää: {pending}," in mail["text"], mail["text"]
    run(go())


def test_daily_digest_overdue_total_subtracts_credit_notes():
    async def go():
        from core.db import db
        await db.raw.invoices.update_many({"status": "overdue"}, {"$set": {"status": "void"}})  # this test owns "overdue"
        await db.raw.invoices.insert_one({"id": _sid("inv"), "company_id": CID, "kind": "invoice", "status": "overdue",
                                          "total_cents": 50000, "paid_cents": 10000, "credited_cents": 15000})
        j = await _work("monitoring.digest_daily")
        mail = await db.raw.email_outbox.find_one({"to": OWNER["email"], "tag": "digest", "ref.id": j["result"]["report_id"]})
        assert "erääntynyttä 250,00 €." in mail["text"], mail["text"]
    run(go())


def test_owner_mail_uses_the_finnish_title_kept_in_card_data():
    """Cards made by credit and insurance keep their Finnish title in data.title_fi; the owner mail uses it."""
    async def go():
        from core.approvals import request_approval
        from core.db import db
        await db.raw.email_outbox.delete_many({"to": OWNER["email"]})
        card = await request_approval("credit_limit", "company", CID, "Set credit limit for Testi Oy", summary="Limit 5 000 EUR",
                                      company_id=CID, data={"title_fi": "Aseta luottoraja: Testi Oy"})
        await asyncio.sleep(0.3)  # the approval.requested handler runs in the background
        assert card["status"] == "pending"
        mail = await db.raw.email_outbox.find_one({"to": OWNER["email"], "subject": {"$regex": "Testi Oy"}}, {"_id": 0})
        assert mail and mail["subject"] == "Hyväksyttävää: Aseta luottoraja: Testi Oy"
        en = await db.raw.email_outbox.find_one({"to": OWNER_EN["email"], "subject": {"$regex": "Testi Oy"}}, {"_id": 0})
        assert en and en["subject"] == "To approve: Set credit limit for Testi Oy"
    run(go())
