"""A rerun takes over an earlier run's rate requests and replies on purpose (A5 DQ-45QQB2: every reply predates the
run). Each partner reply event now says when it arrived and in which run (data.reply.received_at, source_run_ref): the
run that owned the rate request at that moment, i.e. the request's own run, then each takeover in the row's log.
In memory: no MongoDB, mail, model or HTTP."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from services.demo_autoquote import driver as d

RUN = {"id": "run-now", "ref": "DQ-NOW", "created_at": "2026-10-25T13:56:56Z", "status": "running"}
REFS = {"run-g": "DQ-G6P53M", "run-p": "DQ-P7A5WT", "run-j": "DQ-JU9SRY", "run-now": "DQ-NOW"}
ROWS = [{"id": "a", "carrier_name": "Partner a", "contact_email": "a@partner.invalid",
         "log": [{"kind": "rfq", "at": "2026-10-25T10:10:06Z"},
                 {"kind": "reused_by_run", "at": "2026-10-25T10:21:28Z", "run_id": "run-p"},
                 {"kind": "reused_by_run", "at": "2026-10-25T12:31:42Z", "run_id": "run-j"}]},
        {"id": "b", "carrier_name": "Partner b", "contact_email": "b@partner.invalid", "log": []}]


def reply(mid, oid, at, cents):
    return {"id": mid, "message_id": f"<{mid}@partner.invalid>", "outreach_id": oid, "at": at, "direction": "in",
            "provider_id": f"demo-sim:{oid}", "status": "processed", "text": "rate",
            "parsed": {"status": "ok", "intent": "offer", "cost_cents": cents, "transit_days": 3, "valid_to": "2026-11-15"}}


MSGS = [reply("a1", "a", "2026-10-25T10:10:54Z", 81000),   # in the request's own run
        reply("a2", "a", "2026-10-25T12:32:13Z", 74000),   # after the second takeover
        reply("b1", "b", "2026-10-25T13:58:02Z", 87000)]   # in this run


@pytest.mark.asyncio
async def test_each_reply_event_carries_its_receive_time_and_source_run(monkeypatch):
    batches = [MSGS, []]
    find = lambda *a, **k: NS(sort=lambda *a, **k: NS(to_list=AsyncMock(return_value=batches.pop(0) if batches else [])))
    async def run_ref(f, *a, **k):
        return {"ref": REFS.get(f["id"])}
    monkeypatch.setattr(d, "db", NS(
        partner_messages=NS(find=find, update_one=AsyncMock(),
                            find_one=AsyncMock(return_value={"demo_run_id": "run-g"})),  # a's rate request
        demo_runs=NS(find_one=run_ref)))
    monkeypatch.setattr(d, "_run", AsyncMock(return_value=dict(RUN)))
    monkeypatch.setattr(d.simulator, "real_reply_exists", AsyncMock(return_value=False))
    events = []
    monkeypatch.setattr(d, "emit", AsyncMock(side_effect=lambda *a, **k: events.append((a, k))))
    offers, state = {}, {}
    await d._collect("run-now", ROWS, {"origin": {}, "destination": {}}, {}, {}, d.time.monotonic() + 30, offers, state)
    got = [(k["data"]["emails"][0]["message_id"], k["data"]["reply"]) for a, k in events if k.get("kind") == "email_in"]
    assert got == [("<a1@partner.invalid>", {"source": "world", "received_at": "2026-10-25T10:10:54Z", "source_run_ref": "DQ-G6P53M"}),
                   ("<a2@partner.invalid>", {"source": "world", "received_at": "2026-10-25T12:32:13Z", "source_run_ref": "DQ-JU9SRY"}),
                   ("<b1@partner.invalid>", {"source": "world", "received_at": "2026-10-25T13:58:02Z", "source_run_ref": "DQ-NOW"})]
    assert offers["a"]["cost_cents"] == 74000  # the provenance is a label; what counts is unchanged
