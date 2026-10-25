"""core.bounded: creates answer within the budget and never abandon their work."""
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DB_NAME", "huo_test_bounded_" + uuid.uuid4().hex[:8])

import pytest

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import bounded as b
from core.db import client, db


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def database():
    yield
    run(client.drop_database(os.environ["DB_NAME"]))


async def _wait_finished(rid):
    for _ in range(100):
        row = await db.raw.slow_requests.find_one({"id": rid}, {"_id": 0})
        if row and row["status"] != "processing":
            return row
        await asyncio.sleep(0.02)
    raise AssertionError("never finished")


def test_fast_create_returns_its_normal_response():
    async def work():
        return {"shipment": {"id": "s1"}}
    assert run(b.bounded("t", work(), user={"id": "u"}, budget_s=1)) == {"shipment": {"id": "s1"}}


def test_slow_create_answers_202_and_finishes_with_the_same_response():
    writes = []

    async def work():
        await asyncio.sleep(0.3)
        writes.append("inserted")
        return {"quote": {"id": "q1", "number": "Q-1"}}

    resp = run(b.bounded("quote.create", work(), user={"id": "u"}, budget_s=0.05))
    assert isinstance(resp, JSONResponse) and resp.status_code == 202
    body = json.loads(resp.body)
    assert body["status"] == "processing" and body["poll"] == f"/requests/{body['request_id']}"
    assert writes == []  # not finished yet, but not cancelled either
    row = run(_wait_finished(body["request_id"]))
    assert writes == ["inserted"] and row["status"] == "done" and row["http_status"] == 200
    assert row["response"] == {"quote": {"id": "q1", "number": "Q-1"}} and row["duration_s"] >= 0.25
    assert run(b.outcome(body["request_id"], {"id": "u"}))["status"] == "done"
    with pytest.raises(HTTPException):
        run(b.outcome(body["request_id"], {"id": "someone-else"}))


def test_slow_failing_create_keeps_its_status_and_detail():
    async def work():
        await asyncio.sleep(0.2)
        raise HTTPException(409, "Booking needs an approved company check (KYC).")

    resp = run(b.bounded("shipment.create", work(), user={"id": "u"}, budget_s=0.05))
    row = run(_wait_finished(json.loads(resp.body)["request_id"]))
    assert row["status"] == "failed" and row["http_status"] == 409 and "KYC" in row["response"]["detail"]


def test_fast_failure_raises_to_the_caller():
    async def work():
        raise HTTPException(422, "bad")
    with pytest.raises(HTTPException):
        run(b.bounded("t", work(), budget_s=1))


def test_shipment_location_resolution_waits_for_the_threaded_warm_up(monkeypatch):
    from services import gazetteer, shipments
    calls = []

    async def ready():
        calls.append("ready")

    async def find_location(x):
        calls.append("find")
        return {"code": x, "unlocode": x, "name": x, "lat": 60.0, "lon": 25.0, "country": "FI"}

    monkeypatch.setattr(gazetteer, "ready", ready)
    monkeypatch.setattr(shipments.geo, "find_location", find_location)
    run(shipments._resolve("FIKTK"))
    assert calls[:2] == ["ready", "find"]
