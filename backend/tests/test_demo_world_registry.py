"""Registry protocol boundary, evaluated by Huolitsija's unchanged KYC parsers."""
import asyncio
import os
import uuid

os.environ["DB_NAME"] = "huo_world_registry_test_" + uuid.uuid4().hex[:10]

from core.db import db, client
from services import kyc
from services.demo_world import boundary
from services.demo_world.identity import _business_id


def test_only_registered_persona_gets_protocol_answers(monkeypatch):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    bid = _business_id(7)
    identity = {"company": "Demo Fictional World Registry Oy", "business_id": bid, "vat_id": "FI" + bid.replace("-", ""),
                "eori": "FI" + bid, "address": {"street": "Fictional Lane 9", "postal_code": "00100", "city": "Helsinki", "country": "FI"}}

    async def exercise():
        await db.raw.world_registry.insert_one({"run_id": "scope-test", "business_id": bid,
            "vat_id": identity["vat_id"], "eori": identity["eori"], "identity": identity, "created_at": "2026-10-25T00:00:00Z"})
        ytj = await kyc.ytj_lookup(bid)
        assert ytj["found"] and ytj["business_id_status"] == "valid" and ytj["name"] == identity["company"]
        assert ytj["vat_registered"] and ytj["trade_register_status"] == "registered"
        assert (await kyc.vies_check(identity["vat_id"]))["status"] == "pass"
        assert (await kyc.eori_check(identity["eori"]))["status"] == "pass"
        assert await boundary.registry_http("GET", "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=0000000-0") is None
        assert await boundary.registry_http("GET", "https://unrelated.test/?businessId=" + bid) is None
        monkeypatch.setenv("DEMO_WORLD_SANDBOX", "false")
        assert await boundary.registry_http("GET", "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=" + bid) is None
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(exercise())
    finally:
        loop.run_until_complete(client.drop_database(db.raw.name))
