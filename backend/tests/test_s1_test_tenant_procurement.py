"""TEST procurement records on a test tenant: allocations, overseas agents, agent agreements and agent rate cards keep
company_id / test_tenant, and only that company's quotes and bookings see them. Production selection never does.

Runs in process against a real MongoDB in its own database (dropped at the end). No outside system is called.
"""
import asyncio
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

os.environ["DB_NAME"] = f"huo_s1_scope_{uuid.uuid4().hex[:8]}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.db import db  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from services import agent_network as agents, allocations  # noqa: E402

OPS = {"id": "test-s1-scope", "name": "s1 scope test operator"}
TODAY = date.today()
TEST_CO = {"id": "co-test-s1", "name": "TEST Sandbox Oy", "business_id": "3990009-1", "test_tenant": True}
OTHER_TEST_CO = {"id": "co-test-other", "name": "TEST Toinen Oy", "business_id": "3990010-4", "test_tenant": True}
REAL_CO = {"id": "co-real", "name": "Oikea Asiakas Oy", "business_id": "3596001-2"}
CN, FI = {"country": "CN", "unlocode": "CNSHA"}, {"country": "FI", "unlocode": "FIHEL"}


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def motor_loop():
    from core.db import client, ensure_indexes
    client._io_loop = asyncio.get_running_loop()
    await ensure_indexes()
    await db.raw.companies.insert_many([dict(TEST_CO), dict(OTHER_TEST_CO), dict(REAL_CO)])
    yield
    await client.drop_database(os.environ["DB_NAME"])


async def _carrier() -> dict:
    row = {"id": new_id(), "name": f"Testivarustamo {new_id()[:6]}", "modes": ["sea"], "country": "FI", "active": True,
           "created_at": now_iso()}
    await db.carriers.insert_one(dict(row))
    return row


def _alloc_body(carrier_id: str, **extra) -> dict:
    return {"carrier_id": carrier_id, "lane": "sea:CN>FI", "unit": "TEU", "committed_qty": 10, "period": "month",
            "shortfall_cents_per_unit": 5000, "valid_from": (TODAY - timedelta(days=1)).isoformat(),
            "valid_to": (TODAY + timedelta(days=60)).isoformat(), "notes": "TEST", **extra}


def _shipment(carrier_id: str, company_id: str) -> dict:
    return {"id": new_id(), "reference": f"TEST-{new_id()[:5]}", "company_id": company_id, "mode": "sea", "status": "booked",
            "origin": CN, "destination": FI, "etd": f"{TODAY.isoformat()}T08:00:00Z", "teu": 2,
            "legs": [{"seq": 1, "kind": "main", "carrier_id": carrier_id}], "created_at": now_iso()}


@pytest.mark.asyncio(loop_scope="module")
async def test_allocation_keeps_test_company_and_only_that_company_sees_it():
    carrier = await _carrier()
    row = await allocations.create(_alloc_body(carrier["id"], company_id=TEST_CO["id"]), OPS)
    stored = await db.carrier_allocations.find_one({"id": row["id"]})
    assert stored["company_id"] == TEST_CO["id"] and stored["test_tenant"] is True

    assert await allocations.for_option(carrier["id"], "sea", CN, FI, TODAY) is None
    assert await allocations.for_option(carrier["id"], "sea", CN, FI, TODAY, company_id=OTHER_TEST_CO["id"]) is None
    seen = await allocations.for_option(carrier["id"], "sea", CN, FI, TODAY, company_id=TEST_CO["id"])
    assert seen and seen["allocation_id"] == row["id"]

    # PATCH cannot move it to production or to another company
    await allocations.update(row["id"], {"company_id": None, "test_tenant": False, "committed_qty": 12}, OPS)
    after = await db.carrier_allocations.find_one({"id": row["id"]})
    assert after["company_id"] == TEST_CO["id"] and after["test_tenant"] is True and after["committed_qty"] == 12


@pytest.mark.asyncio(loop_scope="module")
async def test_test_bookings_and_production_allocations_never_mix():
    carrier = await _carrier()
    prod = await allocations.create(_alloc_body(carrier["id"]), OPS)
    test = await allocations.create(_alloc_body(carrier["id"], company_id=TEST_CO["id"]), OPS)
    assert "company_id" not in await db.carrier_allocations.find_one({"id": prod["id"]})

    hits = await allocations.record_shipment(_shipment(carrier["id"], TEST_CO["id"]))
    assert [h["allocation_id"] for h in hits] == [test["id"]]
    assert await allocations.record_shipment(_shipment(carrier["id"], OTHER_TEST_CO["id"])) == []
    hits = await allocations.record_shipment(_shipment(carrier["id"], REAL_CO["id"]))
    assert [h["allocation_id"] for h in hits] == [prod["id"]]


@pytest.mark.asyncio(loop_scope="module")
async def test_company_id_is_only_for_test_tenants():
    carrier = await _carrier()
    with pytest.raises(HTTPException) as e:
        await allocations.create(_alloc_body(carrier["id"], company_id=REAL_CO["id"]), OPS)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await agents.create_agent({"name": "TEST Agent", "country": "CN", "company_id": "no-such-company"}, OPS)
    assert e.value.status_code == 404


@pytest.mark.asyncio(loop_scope="module")
async def test_test_agent_agreement_and_card_inherit_the_company_and_stay_out_of_production():
    agent = await agents.create_agent({"name": f"TEST Ouagadougou Agent {new_id()[:5]}", "country": "BF", "countries": ["BF"],
                                       "roles": ["origin"], "modes": ["air"], "company_id": TEST_CO["id"]}, OPS)
    assert agent["company_id"] == TEST_CO["id"] and agent["test_tenant"] is True
    ag = await agents.create_agreement(agent["id"], {"valid_from": (TODAY - timedelta(days=1)).isoformat(),
                                                     "valid_to": (TODAY + timedelta(days=365)).isoformat()}, OPS)
    card = await agents.create_rate_card(agent["id"], {"role": "origin", "country": "BF", "mode": "air",
                                                       "valid_from": (TODAY - timedelta(days=1)).isoformat(),
                                                       "valid_to": (TODAY + timedelta(days=90)).isoformat(),
                                                       "charges": [{"code": "PICKUP", "basis": "PER_SHIPMENT", "amount_cents": 4500}]}, OPS)
    for coll, rid in (("agent_agreements", ag["id"]), ("rate_cards", card["id"])):
        stored = await db[coll].find_one({"id": rid})
        assert stored["company_id"] == TEST_CO["id"] and stored["test_tenant"] is True, coll
    with pytest.raises(HTTPException):
        await agents.create_agreement(agent["id"], {"valid_from": TODAY.isoformat(), "valid_to": TODAY.isoformat(),
                                                    "company_id": OTHER_TEST_CO["id"]}, OPS)
    await agents.set_agreement_status(ag["id"], "signed", {"signed_by": "TEST signer, Director"}, OPS)

    assert await agents.select_agent("BF", "origin", "air") is None
    assert await agents.select_agent("BF", "origin", "air", company_id=OTHER_TEST_CO["id"]) is None
    sel = await agents.select_agent("BF", "origin", "air", company_id=TEST_CO["id"])
    assert sel and sel["agent"]["id"] == agent["id"] and sel["rate_card"]["id"] == card["id"]
    m = {"weight_kg": 300, "volume_m3": 1.2, "containers": []}
    assert await agents.agent_charges("BF", "origin", "air", m) is None
    ac = await agents.agent_charges("BF", "origin", "air", m, company_id=TEST_CO["id"])
    assert ac and [ln["code"] for ln in ac["lines"]] == ["AGENT_PICKUP"]
