"""s12 x s2: a compliance hold or an open dangerous-goods review stops every Autopilot agent on that shipment.

core.agent_runtime.decide() is the one gate every s2 agent passes (carrier booking, customs filing, invoicing). With
Autopilot on and nothing else in the way, an action on a shipment that party screening put on hold (or whose DG review
is open) must not run automatically: it goes to a person with the legal point (sanctions_potential_match /
dangerous_goods). A person approving the DG card lets the agents continue. In process, own throw-away database.
"""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s12_guard_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"

import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s12_autopilot_guard.py in its own pytest process", allow_module_level=True)

from core.agent_runtime import decide  # noqa: E402
from core.db import client, db  # noqa: E402
from core.events import emit  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from services import dangerous_goods  # noqa: E402,F401  (registers the dg_review decision handler)

SID, DECL, INV = new_id(), new_id(), new_id()


@pytest.fixture(scope="module", autouse=True)
def world():
    async def setup():
        await db.settings.update_one({"key": "app"}, {"$set": {"autopilot_enabled": True, "autopilot_halted": False}},
                                     upsert=True)
        await db.shipments.insert_one({"id": SID, "reference": "HUO-TEST-S12", "status": "booked", "test": True,
                                       "compliance_hold": True, "compliance_status": "pending_review", "created_at": now_iso()})
        await db.customs_declarations.insert_one({"id": DECL, "shipment_id": SID, "lrn": "S12LRN", "test": True})
        await db.invoices.insert_one({"id": INV, "shipment_id": SID, "number": "S12-1", "test": True})
    run(setup())
    yield
    run(client.drop_database(os.environ["DB_NAME"]))


def _legal(d):
    return {r.get("legal_point") for r in d.reasons if r.get("code") == "legal"}


@pytest.mark.parametrize("agent,action,subject", [
    ("carrier_agent", "book_leg", ("shipment", SID)),
    ("customs_agent", "file_declaration", ("customs_declaration", DECL)),
    ("invoice_agent", "issue_invoice", ("invoice", INV)),
])
def test_compliance_hold_stops_autopilot_on_every_record_of_the_shipment(agent, action, subject):
    d = run(decide(agent, action, subject=subject, create_approval=False))
    assert d.auto is False and d.outcome == "blocked"
    assert "sanctions_potential_match" in _legal(d), d.reasons
    row = run(db.agent_decisions.find_one({"id": d.id}, {"_id": 0}))
    assert row["outcome"] == "blocked" and row["autopilot_enabled"] is True  # audit row says Autopilot was on


def test_open_dg_review_stops_carrier_booking_until_a_person_approves():
    run(db.shipments.update_one({"id": SID}, {"$set": {"compliance_hold": False, "dg_review_required": True}}))
    d = run(decide("carrier_agent", "book_leg", subject=("shipment", SID), create_approval=False))
    assert d.auto is False and _legal(d) == {"dangerous_goods"}
    card = {"id": new_id(), "kind": "dg_review", "ref_id": SID, "status": "approved", "company_id": None,
            "decision_note": "DG declaration checked by a trained person"}
    run(emit("approval.decided", {"approval": card, "actor": {"type": "user", "id": "ops-1", "name": "Ops"}}))
    s = run(db.shipments.find_one({"id": SID}, {"_id": 0}))
    assert s["dg_review_required"] is False and s["dg_review_status"] == "approved"
    d = run(decide("carrier_agent", "book_leg", subject=("shipment", SID), create_approval=False))
    assert d.auto is True, d.reasons


def test_clear_shipment_is_not_stopped():
    d = run(decide("carrier_agent", "book_leg", subject=("shipment", new_id()), create_approval=False))
    assert d.auto is True, d.reasons
