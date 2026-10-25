"""Real Mongo, native policy/audit/cards and callers; only pricing and delivery transports are controlled."""
import copy
from unittest.mock import AsyncMock, patch
import pytest
import test_s2_prepayment as n
from core.db import db
from core.settings import get_settings, update_settings
from routes import quotes as rq
from services.agents import quote_agent
from services.email_channel import flow, hooks, outbound
from services import global_quote

run = n.run
OPS = {"id": "quote-review-owner", "type": "user", "org_type": "internal", "role": "ops_admin", "name": "Owner", "email": "owner@example.invalid",
       "permissions": ["quotes.write", "quotes.approve", "shipments.write"]}
BOT = {"id": "quote_agent", "type": "agent", "org_type": "internal"}


def setup_module(m):
    n.setup_module(m)
    run(db.raw.users.insert_one(dict(OPS)))
    run(db.raw.companies.update_one({"id": n.CID}, {"$set": {"test_tenant": False, "test": False}}))


def teardown_module(m):
    n.teardown_module(m)


@pytest.fixture(autouse=True)
def policy():
    run(update_settings({"autopilot_enabled": False, "autopilot_halted": False,
                         "quote_approval_threshold_eur": 10000, "autopilot_quote_max_cents": 1000000}))


def quote(tag, *, company=True):
    q = n.quote(tag)
    q.update(status="sent", source="email", approval={}, revision=1)
    if not company:
        q.update(company_id=None, notify_email="requester@example.invalid", source="public")
    for o in q["options"]:
        o.update(margin_cents=10500, margin_floor_cents=0)
    run(db.raw.quotes.replace_one({"id": q["id"]}, copy.deepcopy(q)))
    return q


def assertions(q, *, auto=False):
    decisions = run(db.raw.agent_decisions.find({"subject_id": q["id"]}, {"_id": 0}).to_list(None))
    assert decisions and all(d["outcome"] == ("auto" if auto else "owner_queue") for d in decisions)
    cards = run(db.raw.approvals.find({"ref_id": q["id"], "status": "pending"}, {"_id": 0}).to_list(None))
    assert len(cards) == (0 if auto else 1)
    if not auto:
        assert cards[0]["reason"] == "Autopilot is off"
        assert cards[0]["reason_fi"] == "Autopilotti on pois päältä"
        assert cards[0]["data"]["notify_owner_email"] is False
        assert run(db.raw.quotes.find_one({"id": q["id"]}))["status"] == "pending_approval"
    return decisions, cards


@pytest.mark.parametrize("lang", ["en", "fi"])
def test_reviewer_exact_policy_flow_and_sourcing_hook_hold_once_without_email(lang):
    q = quote("review-" + lang)
    q.update(status="draft", email_thread_id="review-" + lang)
    q = run(rq.apply_approval_policy(q, run(get_settings()), BOT, send=True))
    run(db.raw.quotes.replace_one({"id": q["id"]}, copy.deepcopy(q)))
    thread = {"id": q["email_thread_id"], "state": "quote_review", "language": lang}
    run(db.raw.email_threads.insert_one(dict(thread)))
    send = AsyncMock()
    with patch.object(outbound, "send", send):
        run(flow.send_quote_status(thread, q))
        run(hooks.sourcing_updated({"quote": q}))
    assert send.call_count == 0
    assert len(assertions(q)[0]) == 3


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("company", [False, True])
def test_sourcing_notify_company_and_anonymous_use_the_audited_gate(on, company):
    q = quote(f"notify-{company}-{on}", company=company)
    q["source"] = "portal" if company else "public"
    q["sourcing"] = {"status": "complete", "message_en": "ready", "message_fi": "valmis"}
    run(update_settings({"autopilot_enabled": on}))
    send, notify = AsyncMock(), AsyncMock()
    with patch("core.email.send_email", send), patch("core.notify.notify_company", notify):
        run(global_quote._notify(q))
    assert send.call_count + notify.call_count == int(on)
    assertions(q, auto=on)


@pytest.mark.parametrize("on", [False, True])
def test_email_delivery_with_legacy_sent_status_is_audited(on):
    q = quote(f"legacy-{on}")
    thread = {"id": f"legacy-{on}", "language": "en"}
    run(db.raw.email_threads.insert_one(dict(thread)))
    run(update_settings({"autopilot_enabled": on}))
    send = AsyncMock()
    with patch.object(outbound, "send", send), patch.object(outbound, "base_url", AsyncMock(return_value="https://example.invalid")), \
            patch("services.quote_pdf.render_quote_pdf", AsyncMock(return_value=b"controlled-pdf")):
        run(flow.send_quote_status(thread, q))
    assert send.call_count == int(on)
    assertions(q, auto=on)


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("company", [False, True])
def test_native_requote_job_gates_company_and_anonymous_prices(on, company):
    q = quote(f"requote-{on}-{company}", company=company)
    q["source"] = "portal" if company else "public"
    run(db.raw.quotes.replace_one({"id": q["id"]}, dict(q)))
    priced = {"request": q["request"], "options": q["options"], "not_quoted": [], "warnings": [],
              "valid_until": q["valid_until"], "sourcing_needs": []}
    run(update_settings({"autopilot_enabled": on}))
    with patch.object(rq, "_price", AsyncMock(return_value=priced)), patch.object(global_quote, "open_sourcing", AsyncMock()):
        out = run(global_quote.requote_job({"payload": {"quote_id": q["id"], "lane": "road:FI>SE"}}))
    assert out["status"] == ("sent" if on else "pending_approval")
    assertions(q, auto=on)


def test_human_send_stays_sent_and_authorizes_only_that_revision():
    q = quote("human")
    q["source"] = "ops"
    q = run(rq.apply_approval_policy(q, run(get_settings()), OPS, send=True))
    assert q["status"] == "sent" and quote_agent.human_authorized(q)
    assert run(quote_agent.outbound_allowed(q))
    assert run(db.raw.approvals.count_documents({"ref_id": q["id"]})) == 0
    q["options"][0]["total_cents"] += 1
    assert not run(quote_agent.outbound_allowed(q))
    rows = run(db.raw.agent_decisions.find({"subject_id": q["id"]}, {"_id": 0}).to_list(None))
    assert [d["outcome"] for d in rows] == ["auto", "owner_queue"]
    assert run(db.raw.approvals.count_documents({"ref_id": q["id"], "status": "pending"})) == 1


def test_approval_decision_releases_held_email_with_autopilot_still_off():
    q = quote("person-approved")
    assert not run(quote_agent.outbound_allowed(q))
    a = run(db.raw.approvals.find_one({"ref_id": q["id"]}, {"_id": 0}))
    a.update(status="approved", decided_by=OPS)
    with patch.object(rq, "notify_company", AsyncMock()):
        run(rq._quote_approval_decided({"approval": a, "actor": OPS}))
    q = run(db.raw.quotes.find_one({"id": q["id"]}, {"_id": 0}))
    assert q["status"] == "sent" and run(quote_agent.outbound_allowed(q))


@pytest.mark.parametrize("channel", ["edi", "email"])
def test_automatic_booking_cannot_use_legacy_sent_quote_when_off(channel):
    q = quote("book-" + channel)
    create = AsyncMock(side_effect=AssertionError("booking must not be called"))
    with patch.object(rq, "_create_booking", create):
        if channel == "edi":
            from services.edi import service
            out = run(service._book(q, {}, {"id": "partner", "company_id": n.CID}))
            assert out["status"] == "on_hold"
        else:
            thread = {"id": "book-email", "company_id": n.CID, "quote_id": q["id"], "language": "fi",
                      "pending": {"booking": {"option_number": 1}}}
            run(db.raw.email_threads.insert_one(dict(thread)))
            assert run(flow.do_booking(thread)) is None
    assert create.call_count == 0
    assertions(q)


@pytest.mark.parametrize("on", [False, True])
def test_edi_native_instruction_is_held_or_audited_before_automatic_booking(on):
    from services.edi import service
    q = quote(f"edi-instruction-{on}")
    priced = {"request": q["request"], "options": q["options"], "not_quoted": [], "warnings": [], "valid_until": q["valid_until"]}
    partner = {"id": f"edi-{on}", "company_id": n.CID, "terms_accepted": True}
    instr = {"doc_number": f"DOC-{on}", "mode": "road", "cargo": q["request"]["cargo"],
             "parties": {"shipper": n.body(q).shipper.model_dump(), "consignee": n.body(q).consignee.model_dump()}}
    run(update_settings({"autopilot_enabled": on}))
    create = AsyncMock(return_value={"shipment_id": "controlled-booking-boundary", "shipment_reference": "CONTROLLED"})
    with patch.object(rq, "_price", AsyncMock(return_value=priced)), \
            patch.object(global_quote, "open_sourcing", AsyncMock()), patch.object(rq, "_create_booking", create):
        out = run(service.handle_instruction(instr, partner, inbound_id="test-inbound", standard="dcsa"))
    assert out["status"] == ("booked" if on else "on_hold")
    assert create.call_count == int(on)
    created = run(db.raw.quotes.find_one({"id": out["quote_id"]}, {"_id": 0}))
    assertions(created, auto=on)


@pytest.mark.parametrize("channel", ["portal", "api"])
@pytest.mark.parametrize("lang", ["en", "fi"])
def test_customer_booking_request_is_recorded_but_carrier_purchase_holds(channel, lang):
    from types import SimpleNamespace
    from routes import api_v1
    from services import carrier_booking as cb, positions, geo, shipments, party_screening
    from services.agents import booking_agent, carrier_agent
    q = quote(f"customer-{channel}-{lang}")
    q["source"] = channel
    q["request"].update(origin={**n.FIHEL, "lat": 60.17, "lon": 24.94},
                        destination={**n.SESTO, "lat": 59.33, "lon": 18.07})
    run(db.raw.quotes.replace_one({"id": q["id"]}, dict(q)))
    run(db.raw.companies.update_one({"id": n.CID}, {"$set": {"credit_limit_cents": 10000000}}))
    user = {**n.user(), "locale": lang}
    priced = {"request": q["request"], "options": q["options"], "not_quoted": [], "warnings": [], "valid_until": q["valid_until"]}
    company = run(db.raw.companies.find_one({"id": n.CID}, {"_id": 0}))
    with patch.object(rq, "_price", AsyncMock(return_value=priced)), patch.object(global_quote, "open_sourcing", AsyncMock()):
        q = run(rq.create_quote(q["request"], user=user, company=company, source=channel, send=True))
    assert q["status"] == "sent"  # the customer's requested price is visible in either channel
    adapter = AsyncMock(side_effect=AssertionError("no carrier adapter before approval"))
    async def geometry(mode, a, b, **kwargs):
        return {"path": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]], "distance_km": 400,
                "source": "controlled-geometry", "note": "Geometry I/O only"}
    async def door(loc, address):
        return {**loc, "address": address}
    with patch.object(cb, "adapter_for", adapter), patch.object(positions, "refresh_shipment", AsyncMock()), \
            patch.object(geo, "route_info_async", geometry), patch.object(shipments, "_at_door", door), \
            patch.object(party_screening, "_screen", AsyncMock(return_value=("clear", [], {}))):
        if channel == "portal":
            out = run(rq.book(n.body(q), n.request(), user))
        else:
            ctx = SimpleNamespace(user=user, company={"id": n.CID}, key={"id": "customer-owned-api-key"})
            body = api_v1.BookRequest(**n.body(q).model_dump(exclude={"quote_id"}))
            with patch.object(api_v1, "_api_audit", AsyncMock()):
                out = run(api_v1.book_quote(q["id"], body, n.request(), ctx))
        sid = out["shipment_id"]
        stored_q = run(db.raw.quotes.find_one({"id": q["id"]}, {"_id": 0}))
        assert stored_q["booking"]["accept_terms"] and stored_q["shipment_id"] == sid
        assert run(db.raw.audit_log.count_documents({"action": "quote_booked", "entity_id": q["id"]})) == 1
        assert run(booking_agent.confirm_booking({"payload": {"shipment_id": sid}})) == {"owner_queue": True}
        shipment = run(db.raw.shipments.find_one({"id": sid}, {"_id": 0}))
        assert shipment["booking_confirmation"]["status"] == "pending_owner"
        result = run(carrier_agent.book_one_leg(shipment, shipment["legs"][0], reason="test booking request"))
        assert result["status"] == "owner_queue"
    assert adapter.call_count == 0
    assert run(db.raw.carrier_bookings.count_documents({"shipment_id": sid})) == 0
    cards = run(db.raw.approvals.find({"ref_id": sid, "status": "pending"}, {"_id": 0}).to_list(None))
    assert len(cards) == 1 and cards[0]["kind"] == "booking_review"
    assert cards[0]["reason"] == "Autopilot is off" and cards[0]["reason_fi"] == "Autopilotti on pois päältä"
    receipts = run(db.raw.email_outbox.find({"tag": "booking_confirmation", "ref.id": q["id"]}, {"_id": 0}).to_list(None))
    assert receipts
    expected = "Varauspyyntö vastaanotettu" if lang == "fi" else "Booking request received"
    assert all(expected in m["subject"] for m in receipts)
    assert all("Carrier confirmation is pending" in m["text"] if lang == "en" else "Kuljetusliikkeen vahvistusta odotetaan" in m["text"] for m in receipts)


def test_approved_carrier_handoff_is_scoped_to_the_unchanged_shipment():
    s = {"id": "manual-carrier-handoff", "company_id": n.CID, "reference": "TEST-HANDOFF", "legs": []}
    run(db.raw.shipments.insert_one(dict(s)))
    d = run(quote_agent.autosend(shipment=s))
    assert not d.auto
    from routes.admin import decide, DecisionIn
    run(decide(d.approval["id"], DecisionIn(decision="approved", note="Reviewed carrier handoff"), user=OPS))
    saved = run(db.raw.approvals.find_one({"id": d.approval["id"]}, {"_id": 0}))
    assert set(saved["decided_by"]) == {"id", "name"}
    assert run(quote_agent.carrier_human_authorized(s))
    assert run(quote_agent.autosend(shipment=s)).auto
    assert run(quote_agent.autosend(shipment=s)).auto
    assert run(db.raw.approvals.count_documents({"ref_id": s["id"], "status": "pending"})) == 0
    s["destination"] = n.SESTO
    assert not run(quote_agent.autosend(shipment=s)).auto


@pytest.mark.parametrize("reason", ["cap", "margin", "freight", "dangerous_goods", "halted"])
def test_autopilot_on_still_holds_policy_failures_with_an_audit(reason):
    q = quote("on-policy-" + reason)
    run(update_settings({"autopilot_enabled": True, "autopilot_halted": reason == "halted"}))
    if reason == "cap":
        q["options"][0]["total_cents"] = 2000000
    elif reason == "margin":
        q["options"][0]["cost_cents"] = q["options"][0]["total_cents"]
    elif reason == "freight":
        q["options"][0]["lines"] = [{"code": "DOC_FEE", "amount_cents": 3500}]
    elif reason == "dangerous_goods":
        q["request"]["cargo"]["dangerous_goods"] = True
    assert not run(quote_agent.outbound_allowed(q))
    rows = run(db.raw.agent_decisions.find({"subject_id": q["id"]}, {"_id": 0}).to_list(None))
    assert len(rows) == 1 and rows[0]["outcome"] == "owner_queue" and rows[0]["reasons"]
    assert run(db.raw.approvals.count_documents({"ref_id": q["id"], "status": "pending"})) == 1


def test_paid_prepayment_records_payment_and_request_then_holds_carrier_execution():
    from services import billing, prepayment, carrier_booking as cb, positions, geo, shipments, party_screening
    from services.agents import booking_agent
    q = quote("prepaid-gate")
    run(db.raw.companies.update_one({"id": n.CID}, {"$set": {"credit_limit_cents": 0}}))
    adapter = AsyncMock(side_effect=AssertionError("prepayment must not contact carrier before release"))
    async def geometry(mode, a, b, **kwargs):
        return {"path": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]], "distance_km": 400,
                "source": "controlled-geometry", "note": "Geometry I/O only"}
    async def door(loc, address):
        return {**loc, "address": address}
    with patch.object(cb, "adapter_for", adapter), patch.object(positions, "refresh_shipment", AsyncMock()), \
            patch.object(geo, "route_info_async", geometry), patch.object(shipments, "_at_door", door), \
            patch.object(party_screening, "_screen", AsyncMock(return_value=("clear", [], {}))):
        request = run(rq.book(n.body(q), n.request(), n.user()))
        invoice = run(db.raw.invoices.find_one({"id": request["prepayment"]["invoice_id"]}, {"_id": 0}))
        paid, applied = run(billing.record_payment(invoice["id"], amount_cents=invoice["total_cents"], method="card",
                                                  ref="controlled-payment-reference", actor=None))
        assert applied and paid["status"] == "paid"
        result = run(prepayment.book_paid(invoice["id"]))
        sid = result["shipment_id"]
        assert run(db.raw.invoices.find_one({"id": invoice["id"]}))["shipment_id"] == sid
        assert run(booking_agent.confirm_booking({"payload": {"shipment_id": sid}})) == {"owner_queue": True}
    assert adapter.call_count == 0 and run(db.raw.carrier_bookings.count_documents({"shipment_id": sid})) == 0
    cards = run(db.raw.approvals.find({"ref_id": sid, "status": "pending"}, {"_id": 0}).to_list(None))
    assert len(cards) == 1 and cards[0]["reason"] == "Autopilot is off" and cards[0]["reason_fi"] == "Autopilotti on pois päältä"


@pytest.mark.parametrize("on", [False, True])
def test_native_deadline_scheduler_notification_passes_the_same_gate(on):
    import asyncio
    q = quote("scheduler-" + str(on), company=False)
    q["sourcing"] = {"status": "requested", "deadline_at": "2020-01-01T00:00:00Z", "request_ids": []}
    run(db.raw.quotes.replace_one({"id": q["id"]}, dict(q)))
    run(update_settings({"autopilot_enabled": on}))
    send = AsyncMock()
    async def sweep():
        await global_quote.sourcing_sweep()
        await asyncio.sleep(0)
    with patch("core.email.send_email", send):
        run(sweep())
    assert send.call_count == int(on)
    assertions(q, auto=on)


@pytest.mark.parametrize("state", ["disabled", "deleted", "foreign", "agent"])
def test_carrier_release_rejects_inactive_noninternal_and_agent_approvers(state):
    from routes.admin import decide, DecisionIn
    owner = {**OPS, "id": "invalid-approver-" + state, "email": state + "@example.invalid"}
    run(db.raw.users.insert_one(dict(owner)))
    s = {"id": "invalid-handoff-" + state, "company_id": n.CID, "reference": "TEST-INVALID", "legs": []}
    run(db.raw.shipments.insert_one(dict(s)))
    d = run(quote_agent.autosend(shipment=s))
    run(decide(d.approval["id"], DecisionIn(decision="approved", note="Review"), user=owner))
    if state == "deleted":
        run(db.raw.users.delete_one({"id": owner["id"]}))
    else:
        change = {"disabled": True} if state == "disabled" else {"org_type": "customer"} if state == "foreign" else {"type": "agent"}
        run(db.raw.users.update_one({"id": owner["id"]}, {"$set": change}))
    assert not run(quote_agent.carrier_human_authorized(s))
    assert not run(quote_agent.autosend(shipment=s)).auto


def test_real_person_release_allows_one_carrier_commitment_on_retries():
    from routes.admin import decide, DecisionIn
    from services import carrier_booking as cb
    from core.agent_runtime import agent_bot
    s = {"id": "once-carrier-release", "company_id": n.CID, "reference": "TEST-ONCE", "status": "booked", "mode": "road",
         "legs": [{"seq": 1, "mode": "road", "from": dict(n.FIHEL), "to": dict(n.SESTO), "carrier_id": "c-road"}]}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    d = run(quote_agent.autosend(shipment=s))
    run(decide(d.approval["id"], DecisionIn(decision="approved", note="Reviewed carrier booking"), user=OPS))
    channel = type("ControlledCarrier", (), {"name": "controlled", "mode": "log", "book": AsyncMock(return_value={"status": "requested"})})()
    with patch.object(cb, "adapter_for", AsyncMock(return_value=channel)):
        first = run(cb.book_leg(s["id"], 1, "c-road", agent_bot("carrier_agent"), cost_cents=22000))
        second = run(cb.book_leg(s["id"], 1, "c-road", agent_bot("carrier_agent"), cost_cents=22000))
    assert first["id"] == second["id"] and channel.book.call_count == 1
    assert run(db.raw.carrier_bookings.count_documents({"shipment_id": s["id"]})) == 1
    assert run(db.raw.approvals.count_documents({"ref_id": s["id"], "status": "pending"})) == 0
    assert run(db.raw.shipments.find_one({"id": s["id"]}))["carrier_release"]["status"] == "approved"


@pytest.mark.parametrize("operation", ["rebook", "cancel"])
def test_agent_group_mutation_off_preserves_confirmed_booking_and_aliases(operation):
    from services import carrier_booking as cb
    from core.agent_runtime import agent_bot
    from fastapi import HTTPException
    sid, bid = "mutation-" + operation, "booking-" + operation
    s = {"id": sid, "company_id": n.CID, "reference": "TEST-MUTATION", "status": "booked", "mode": "road", "legs": [
        {"seq": 1, "mode": "road", "from": dict(n.FIHEL), "to": dict(n.SESTO), "carrier_id": "c-road",
         "booking_group": {"carrier_id": "c-road", "leg_seqs": [1, 2, 3]}, "booking": {"id": bid, "status": "confirmed"}},
        {"seq": 2, "mode": "sea", "carrier_id": None, "booking_parent_seq": 1, "booking": {"id": bid, "status": "confirmed"}},
        {"seq": 3, "mode": "road", "carrier_id": "c-road", "booking_parent_seq": 1, "booking": {"id": bid, "status": "confirmed"}}]}
    b = {"id": bid, "shipment_id": sid, "leg_seq": 1, "carrier_id": "c-road", "status": "confirmed"}
    run(db.raw.shipments.insert_one(copy.deepcopy(s)))
    run(db.raw.carrier_bookings.insert_one(dict(b)))
    adapter = AsyncMock(side_effect=AssertionError("no provider call before authority"))
    with patch.object(cb, "_adapter_for_booking", adapter), patch.object(cb, "adapter_for", adapter):
        with pytest.raises(HTTPException) as exc:
            if operation == "rebook":
                run(cb.rebook_leg(sid, 2, agent_bot("carrier_agent"), reason="reschedule"))
            else:
                run(cb.cancel_booking(bid, agent_bot("carrier_agent"), "cancel"))
    assert exc.value.status_code == 409 and adapter.call_count == 0
    assert run(db.raw.shipments.find_one({"id": sid}, {"_id": 0})) == s
    assert run(db.raw.carrier_bookings.find_one({"id": bid}, {"_id": 0})) == b
    cards = run(db.raw.approvals.find({"ref_id": sid, "status": "pending"}, {"_id": 0}).to_list(None))
    assert len(cards) == 1 and cards[0]["reason"] == "Autopilot is off" and cards[0]["reason_fi"] == "Autopilotti on pois päältä"
