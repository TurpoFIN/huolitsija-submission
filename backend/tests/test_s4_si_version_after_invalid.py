"""Live HUO-2026-000080 (TEST): after an SI refused by the DCSA check, the next SI was numbered version 1 again, so the
execution view kept showing the refused SI and the alert key / carrier file name repeated. Needs a local MongoDB
(MONGO_URL, default mongodb://localhost:27017); uses its own throwaway database."""
import asyncio
import os
import sys
import uuid

import pytest

os.environ.setdefault("DB_NAME", f"s4_si_version_{uuid.uuid4().hex[:8]}")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.db import client, db  # noqa: E402
from services.execution import si_vgm  # noqa: E402

OPS = {"id": "ops-test", "name": "Ops tester", "type": "user", "org_type": "internal"}


async def _seed():
    sid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.shipments.insert_one({
        "id": sid, "reference": "HUO-TEST-S4", "company_id": "c-test", "mode": "sea", "direction": "export", "incoterm": "FCA",
        "customer_name": "TEST Shipper Oy", "destination": {"country": "DE"}, "references": {"customer_ref": "TEST-S4-SIVER"},
        "cargo": {"description": "TEST paper rolls - acceptance test, no cargo", "hs_hint": "480519", "package_type": "roll"},
        "parties": {"shipper": {"name": "TEST Shipper Oy", "address": {"street": "Testikatu 1", "postal_code": "00100",
                                                                     "city": "Helsinki", "country": "FI"}},
                    "consignee": {"name": "TEST Consignee GmbH", "address": {"street": "Teststrasse 1", "postal_code": "20095",
                                                                            "city": "Hamburg", "country": "DE"}}}})
    await db.execution_records.insert_one({
        "id": rid, "shipment_id": sid, "reference": "HUO-TEST-S4", "company_id": "c-test", "mode": "sea",
        "carrier_id": None, "carrier_code": "TESTSYN", "carrier_name": "TEST carrier", "carrier_booking_reference": "TESTBKG-1",
        "si_required": True, "vgm_required": True, "state": "started", "created_at": "2026-10-25T00:00:00Z", "history": [],
        "containers": [{"index": 0, "equipment_reference": "TSTU1234573", "seal_number": "TESTSEAL1", "iso_type": "45G1",
                        "cargo_gross_kg": 18000, "packages": 20}]})
    await db.shipping_instructions.insert_one({"id": str(uuid.uuid4()), "record_id": rid, "shipment_id": sid, "version": 1,
                                               "status": "invalid", "validation_errors": ["earlier refusal"], "history": []})
    return sid, rid


def test_si_after_a_refused_one_gets_the_next_version():
    async def run():
        try:
            await client.admin.command("ping")
        except Exception:  # noqa: BLE001
            pytest.skip("no local MongoDB")
        try:
            sid, rid = await _seed()
            result = await si_vgm.submit_si(sid, OPS)
            assert result.get("status") == "failed", result  # no booking desk e-mail for the synthetic carrier: nothing is sent
            latest = await db.shipping_instructions.find_one({"record_id": rid}, {"_id": 0}, sort=[("version", -1)])
            assert latest["version"] == 2 and latest["status"] == "failed"
            assert latest["error"] == "no booking desk e-mail for this carrier"
        finally:
            await client.drop_database(os.environ["DB_NAME"])
    asyncio.get_event_loop().run_until_complete(run())  # core.db binds its Motor client to this loop at import
