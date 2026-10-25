"""POST /api/credit/preview: free public credit signals for a Business ID that is not a customer, nothing stored.

PRH/YTJ and PRH XBRL answer from the recorded real responses in tests/fixtures/registers (strict: an unrecorded request
fails). Real MongoDB in its own database (dropped at the end) proves nothing is written.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

os.environ["DB_NAME"] = f"huo_test_s1_credit_{uuid.uuid4().hex[:8]}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.db import client, db  # noqa: E402
from routes.credit import PreviewIn, preview  # noqa: E402

REGISTERS = os.path.join(os.path.dirname(__file__), "fixtures", "registers")
KESKO, BANKRUPT = "0109862-8", "0502119-3"
OPS = {"id": "test-s1-credit", "name": "s1 credit test", "permissions": ["customers.write"]}


@pytest.fixture(autouse=True)
def recorded(monkeypatch):
    monkeypatch.setenv("REGISTER_RECORDINGS", REGISTERS)
    monkeypatch.setenv("REGISTER_RECORDINGS_STRICT", "1")
    monkeypatch.delenv("REGISTER_RECORD", raising=False)


def test_preview_reads_real_registers_and_stores_nothing():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        ok = await preview(PreviewIn(business_id=KESKO), OPS)
        bad = await preview(PreviewIn(business_id=BANKRUPT), OPS)
        counts = (await db.credit_checks.count_documents({}), await db.companies.count_documents({}),
                  await db.approvals.count_documents({}))
        await client.drop_database(os.environ["DB_NAME"])
        return ok, bad, counts
    ok, bad, counts = asyncio.run(go())
    assert ok["stored"] is False and ok["check"]["provider"] == "free" and ok["check"]["status"] == "ok"
    ytj = next(s for s in ok["check"]["sources"] if s["name"] == "ytj")
    assert ytj["status"] == "ok" and ytj["url"].startswith("https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=0109862-8")
    assert "raw" not in ok["check"]["signals"]["ytj"]
    assert bad["check"]["rating"] != ok["check"]["rating"] and bad["check"]["recommended_limit_cents"] < ok["check"]["recommended_limit_cents"]
    assert counts == (0, 0, 0)
