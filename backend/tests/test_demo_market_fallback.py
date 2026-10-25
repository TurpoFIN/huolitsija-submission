"""A first round without any partner rate prices the offer from Huolitsija's market rates (production's designed
fallback). That offer goes through exactly the gate of any other: the native quote_threshold card, decided by the quote
agent's audited decision on native approval reasons. The demo adds no bypass and no hold of its own; it labels the
offer "Priced from market rates · no partner rate". Test DB, no mail: native quoting on a lane without rate cards."""
from datetime import timedelta

import test_s2_prepayment as native
from core.db import db
from core.settings import get_settings, update_settings
from core.util import now
from routes.quotes import approval_reasons
from services import geo
from services.demo_autoquote import clock, driver


def setup_module(m):
    native.setup_module(m)


def teardown_module(m):
    native.teardown_module(m)


def test_market_rate_offer_is_released_only_by_the_native_audited_decision(monkeypatch):
    async def customer_context(run_id):
        return {}, native.user(), await db.raw.companies.find_one({"id": native.CID}, {"_id": 0})
    monkeypatch.setattr(driver, "_ensure_thread", customer_context)
    monkeypatch.setattr(driver.conf, "approval_wait_s", lambda: 0)

    async def exercise():
        await update_settings({"partner_network_enabled": False, "autopilot_enabled": True, "autopilot_halted": False})
        await db.raw.companies.update_one({"id": native.CID}, {"$set": {"credit_limit_cents": 500000}})
        await db.raw.demo_runs.insert_one({"id": "market-fallback", "phase": "quote", "status": "running", "event_seq": 0})
        await clock.reset_now("market-fallback")
        req = {"origin": geo.lookup_code("FIOUL"), "destination": geo.lookup_code("SELLA"), "mode": "road",
               "ready_date": (now() + timedelta(days=7)).date().isoformat(), "direction": "export", "incoterm": "DAP",
               "incoterm_place": "Luleå", "customs_clearance": False,
               "cargo": {"load_type": "LTL", "packages": 2, "package_type": "pallet_eur", "weight_kg": 400, "volume_m3": 1.92,
                         "description": "Controlled steel parts", "dangerous_goods": False}}
        priced = {"basis": "market_reference"}
        q, _ = await driver._store_quote("market-fallback", req, "road", priced, None, "market-rate fallback")
        option = q["options"][0]
        assert option["price_basis"] == "market_reference" and option["source"] == {"kind": "market_reference"}
        assert not option.get("rate_card_id")  # no partner rate behind it
        # The card carries the native reasons for this very quote, nothing the demo added or removed.
        assert q["approval"]["reasons"] == approval_reasons(q, await get_settings())
        released = await driver._await_release("market-fallback", q)
        decisions = await db.raw.agent_decisions.find({"agent": "quote_agent", "action": "send_quote", "subject_id": q["id"]},
                                                      {"_id": 0}).sort("at", -1).to_list(10)
        assert decisions, "no audited quote-agent decision"
        card = await db.raw.approvals.find_one({"kind": "quote_threshold", "ref_id": q["id"]}, {"_id": 0})
        if released:  # the agent's audited auto decision released it
            assert released["status"] == "sent" and card["status"] == "approved"
            assert released["send_decision"]["decided_by_id"] == "agent:quote_agent"
            assert released["send_decision"]["decision_outcome"] == "auto" == decisions[0]["outcome"]
        else:  # the agent may not decide: the card waits for a person and nothing is e-mailed
            assert card["status"] == "pending" and decisions[0]["outcome"] != "auto"
            assert (await db.raw.demo_runs.find_one({"id": "market-fallback"}))["status"] == "awaiting_approval"
        assert not await db.raw.email_outbox.find_one({"tag": "email_quote"})  # the driver mails only after release
    native.run(exercise())
