"""Real Mongo/service tests; all business records here are isolated test fixtures."""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.db import client, db
from core.util import new_id, now, now_iso
from core.tenant_visibility import hidden_test_companies
from services import allocations, scorecards, shipments

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def database():
    client._io_loop = asyncio.get_running_loop()
    assert "test" in db.raw.name or "ci" in db.raw.name
    yield


async def carrier():
    row = {"id": new_id(), "name": "Isolated provenance fixture", "active": True}
    await db.carriers.insert_one(dict(row))
    return row["id"]


async def shipment(cid, company_id=None, **flags):
    row = {"id": new_id(), "company_id": company_id, "reference": "PROVENANCE-TEST-" + new_id(), "status": "delivered",
           "mode": "sea", "origin": {"country": "CN"}, "destination": {"country": "FI"},
           "teu": 2, "created_at": now_iso(), "etd": now_iso(),
           "legs": [{"carrier_id": cid, "eta": now_iso(), "ata": now_iso()}], **flags}
    await db.raw.shipments.insert_one(dict(row))
    return row


async def test_legacy_test_tenants_never_become_measured_shipments_or_cached_scores():
    cid = await carrier()
    company_id = new_id()
    await db.raw.companies.insert_one({"id": company_id, "test_tenant": True})
    for _ in range(6):
        await shipment(cid, company_id)
    for flag in ("demo", "test", "test_tenant"):
        await shipment(cid, **{flag: True})
    real = await shipment(cid)
    # A prior cached score must be invalidated immediately, also in the two
    # quoting/booking entry points that previously read the cache directly.
    await db.carrier_scorecards.insert_one({"carrier_id": cid, "score": 100, "grade": "A", "metrics": [], "computed_at": now_iso()})
    for display_filter in (None, [company_id]):
        token = hidden_test_companies.set(display_filter)
        try:
            sc = await scorecards.get(cid)
            metric = next(m for m in sc["metrics"] if m["key"] == "on_time")
            assert metric["sample"] == 1 and metric["value"] == 100
            assert sc["score"] is None and sc["provenance_version"] == scorecards.PROVENANCE_VERSION
        finally:
            hidden_test_companies.reset(token)
    options = [{"carrier_id": cid}]
    await db.carrier_scorecards.update_one({"carrier_id": cid}, {"$unset": {"provenance_version": ""}, "$set": {"score": 100}})
    await scorecards.annotate_options(options)
    assert options[0]["scorecard"]["score"] is None and "on_time_source" not in options[0]
    await db.carrier_scorecards.update_one({"carrier_id": cid}, {"$unset": {"provenance_version": ""}, "$set": {"score": 100}})
    selection = await scorecards.selection_inputs([cid], "sea", {"country": "CN"}, {"country": "FI"})
    assert selection[cid]["scorecard"]["score"] is None and selection[cid]["adjust"] == 0


async def test_existing_allocation_usage_ignores_test_company_and_cancelled_shipments():
    cid = await carrier()
    co = new_id()
    await db.raw.companies.insert_one({"id": co, "test_tenant": True})
    real, legacy = await shipment(cid), await shipment(cid, co)
    today = now().date()
    alloc = {"id": new_id(), "carrier_id": cid, "lane": "sea:CN>FI", "period": "month", "unit": "TEU",
             "committed_qty": 10, "max_qty": 20, "status": "active", "valid_from": today.isoformat(),
             "valid_to": (today + timedelta(days=30)).isoformat()}
    await db.carrier_allocations.insert_one(dict(alloc))
    await allocations.record_shipment(real)
    assert await allocations.record_shipment(legacy) == []
    flagged = await shipment(cid, test=True)
    assert await allocations.record_shipment(flagged) == []
    await db.allocation_usage.insert_one({"allocation_id": alloc["id"], "shipment_id": legacy["id"], "qty": 18,
                                           "period_key": allocations.period_bounds("month", today)[0]})
    assert (await allocations.status(alloc))["used"] == 2
    await db.raw.shipments.update_one({"id": real["id"]}, {"$set": {"status": "cancelled"}})
    assert (await allocations.status(alloc))["used"] == 0


async def test_verification_responses_demo_rates_and_test_invoices_are_not_evidence():
    cid, other = await carrier(), await carrier()
    service = "PROVENANCE-TEST-" + new_id()
    for i in range(3):
        await db.partner_outreach.insert_one({"id": new_id(), "campaign_key": new_id(), "carrier_id": cid,
            "verification": True, "created_at": now_iso(), "log": [{"kind": "rfq", "at": now_iso()}, {"kind": "reply", "at": now_iso()}]})
        await db.raw.carrier_invoices.insert_one({"id": new_id(), "supplier": {"carrier_id": cid}, "test_tenant": True,
            "created_at": now_iso(), "status": "audited", "audit": {"within_tolerance": True}})
    for owner, extra in ((cid, {}), (other, {"demo": True})):
        await db.rate_cards.insert_one({"id": new_id(), "carrier_id": owner, "mode": "sea", "service": service,
            "origin": {"country": "ZZ"}, "destination": {"country": "YY"}, "unit_cost_cents": 10000,
            "source": "partner_network", "status": "accepted", **extra})
    sc = await scorecards.compute(cid)
    metrics = {m["key"]: m for m in sc["metrics"]}
    for key in ("response", "invoice_accuracy", "cost"):
        assert metrics[key]["sample"] == 0 and metrics[key]["value"] is None


async def test_shipment_assembly_preserves_company_provenance():
    for flags in ({"demo": True}, {"test": True}, {"test_tenant": True}, {}):
        row = await shipments._assemble(company={"id": new_id(), "name": "Isolated fixture", **flags},
            mode="road", direction="export", incoterm="FCA", incoterm_place="Helsinki",
            origin={"lat": 60, "lon": 24}, destination={"lat": 59, "lon": 18},
            legs=[{"seq": 1, "mode": "road", "role": "main", "distance_km": 100,
                   "etd": now_iso(), "eta": now_iso(), "from": {"name": "Fixture origin"},
                   "to": {"name": "Fixture destination"}}],
            etd=now(), eta=now(), cargo={}, parties={}, references={}, customs=False,
            insurance={}, price={}, liability_regime=None, forwarder_role=None, quote_id=None,
            option_id=None, source="portal", actor={"id": "fixture"})
        for flag in ("demo", "test", "test_tenant"):
            assert row[flag] is bool(flags.get(flag))
