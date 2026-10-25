"""Run 2 (DQ-PW63DT) regression: the cheapest partner rate expired before the cargo's ready date, native pricing
rightly refused the accepted card, and the run failed. The driver now ranks and counters only rates valid on the ready
date (native pricing.card_valid_on), and with none valid it stops at a person card without writing to any partner.
In memory: no real HTTP, mail, LLM or MongoDB."""
import socket
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from services.demo_autoquote import driver as d

READY = "2026-11-04"
REQ = {"origin": {"country": "FI", "name": "Oulu"}, "destination": {"country": "NO", "name": "Tromsø"}, "mode": "road",
       "cargo": {"description": "parts", "weight_kg": 2100, "packages": 5, "volume_m3": 5.28}, "ready_date": READY,
       "incoterm": "DAP"}
ROWS = [{"id": p, "carrier_name": f"Partner {p}", "contact_email": f"{p}@partner.invalid"} for p in ("a", "b", "c")]


def offer(pid, cents, valid_to, mid="m1"):
    return {"outreach_id": pid, "partner": f"Partner {pid}", "cost_cents": cents, "transit_days": 3, "valid_to": valid_to,
            "simulated": True, "message_id": f"{pid}-{mid}"}


class Stop(Exception):
    pass


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "log")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("Unexpected HTTP")))
    from core import email
    monkeypatch.setattr(email, "send_email", AsyncMock(side_effect=AssertionError("Unexpected mail")))
    monkeypatch.setattr(d.pn, "send_partner_mail", AsyncMock(side_effect=AssertionError("Unexpected partner mail")))


def harness(monkeypatch, first, after_counter=None):
    """quote_phase on a reused shipment up to pricing: _collect yields `first`, then `after_counter` once countered."""
    writes, events, follow = AsyncMock(), [], AsyncMock()
    monkeypatch.setattr(d, "db", NS(demo_runs=NS(update_one=writes)))
    monkeypatch.setattr(d, "emit", AsyncMock(side_effect=lambda *a, **k: events.append((a, k))))
    monkeypatch.setattr(d, "_run", AsyncMock(return_value={"status": "running", "request_text": "in-memory", "reuse_from": "old",
                                                         "origin_text": "Oulu", "destination_text": "Tromsø"}))
    monkeypatch.setattr(d, "_parse", AsyncMock(return_value=({"request": dict(REQ), "missing": []}, {}, {})))
    monkeypatch.setattr(d, "_missing", lambda *a: [])
    monkeypatch.setattr(d, "_essential_missing", lambda *a: [])
    monkeypatch.setattr(d, "_complete", lambda req: req)
    monkeypatch.setattr(d, "_reference", AsyncMock(return_value={}))
    monkeypatch.setattr(d, "_reuse_outreach", AsyncMock(return_value=ROWS))
    calls = {"n": 0}

    async def collect(run_id, rows, req, extras, reference, until, offers, state):
        calls["n"] += 1
        batch = first if calls["n"] == 1 else (after_counter or {})
        offers.update(batch)
        state.setdefault("final", {}).update({k: "offer" for k in batch})
    monkeypatch.setattr(d, "_collect", collect)
    monkeypatch.setattr(d, "_follow_up", follow)
    price = AsyncMock(side_effect=Stop)
    monkeypatch.setattr(d, "_price", price)
    return writes, events, follow, price


def test_validity_matches_native_card_valid_on():
    assert d._valid_on({"valid_to": READY}, READY)
    assert not d._valid_on({"valid_to": "2026-11-03"}, READY)
    assert d._valid_on({"valid_to": None}, READY)
    assert not d._valid_on({"valid_to": "next week"}, READY)


@pytest.mark.asyncio
async def test_expired_cheapest_rate_is_skipped_for_the_next_valid_one(monkeypatch):
    first = {"a": offer("a", 40000, "2026-11-01"), "b": offer("b", 45000, "2026-11-15"), "c": offer("c", 51000, "2026-11-20")}
    accepted = {"b": offer("b", 42000, "2026-11-15", mid="m2")}
    writes, events, follow, price = harness(monkeypatch, first, accepted)
    with pytest.raises(Stop):
        await d.quote_phase("r")
    # the counteroffer goes to the cheapest valid partner, never to the expired one
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]
    assert follow.await_args_list[0].args[2] == "counteroffer"
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"]) == ("b", 42000)
    titles = [a[2] for a, _ in events]
    assert "1 rate not valid on 2026-11-04" in titles
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner b")
    assert [o["partner"] for o in best["ranking"]] == ["Partner b", "Partner c"]
    assert [o["partner"] for o in best["expired"]] == ["Partner a"]


@pytest.mark.asyncio
async def test_every_rate_expired_stops_at_a_person_card_with_nothing_sent(monkeypatch):
    first = {"a": offer("a", 40000, "2026-11-01"), "b": offer("b", 45000, "2026-10-30")}
    writes, events, follow, price = harness(monkeypatch, first)
    from core import approvals
    card = AsyncMock(return_value={"id": "card-1"})
    monkeypatch.setattr(approvals, "request_approval", card)
    await d.quote_phase("r")
    follow.assert_not_awaited()  # no counteroffer, follow-up or extension request to any partner
    price.assert_not_awaited()  # no quote drafted from an expired rate
    kind, ref_type, ref_id, title = card.await_args.args
    assert (kind, ref_type, ref_id, title) == ("partner_rate", "demo_run", "r", "No valid rate for the ready date")
    data = card.await_args.kwargs["data"]
    assert data["ready_date"] == READY
    assert [(o["partner"], o["valid_to"]) for o in data["comparison"]] == [("Partner a", "2026-11-01"), ("Partner b", "2026-10-30")]
    assert any(c.args[1].get("$set", {}).get("status") == "awaiting_approval" for c in writes.await_args_list)
    wait = next(k for a, k in events if a[2] == "No valid rate for the ready date · a person decides")
    assert wait["state"] == "waiting" and wait["data"]["approval_id"] == "card-1"


@pytest.mark.asyncio
async def test_counter_answer_that_expires_first_also_ends_at_the_card(monkeypatch):
    first = {"a": offer("a", 40000, "2026-11-10"), "b": offer("b", 45000, "2026-11-12")}
    shorter = {"a": offer("a", 38000, "2026-11-01", mid="m2"), "b": offer("b", 45000, "2026-11-02", mid="m2")}
    writes, events, follow, price = harness(monkeypatch, first, shorter)
    from core import approvals
    card = AsyncMock(return_value={"id": "card-2"})
    monkeypatch.setattr(approvals, "request_approval", card)
    await d.quote_phase("r")
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["a"]  # only the one counteroffer
    price.assert_not_awaited()
    assert card.await_args.args[0] == "partner_rate"
