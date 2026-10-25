"""A5 (DQ-JU9SRY) regression: a rerun of the same shipment took over rate requests whose winning rate A4's procurement
had already accepted as a native card; the driver countered that partner again, the new simulated answer no longer
matched the card, and native procurement rightly refused it. Now an accepted card that native pricing quotes for this
very request is used at its own terms and never countered again; one native pricing does not quote is left out. The
accepted card is never written. In memory: no real HTTP, mail, LLM or MongoDB; global_quote.preview is replaced by the
response shape it returned live for A5's stored request (options[].rate_card_id and options[].legs[].rate_card_id)."""
import socket
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from services import global_quote
from services.demo_autoquote import driver as d

READY = "2026-11-04"
REQ = {"origin": {"country": "FI", "name": "Oulu"}, "destination": {"country": "NO", "name": "Bergen"}, "mode": "road",
       "cargo": {"description": "parts", "weight_kg": 2100, "packages": 5, "volume_m3": 5.28}, "ready_date": READY,
       "incoterm": "DAP"}
CARD = {"id": "card-a4", "status": "accepted", "archived": False, "all_in_cost_cents": 76000, "transit_days": 3,
        "valid_to": "2026-11-15", "created_at": "2026-10-25T10:21:52Z",
        "source_detail": {"message_id": "<a-accept@katupojat.invalid>", "ref": "RFQ-A4"}}
ROWS = [{"id": "a", "carrier_name": "Partner a", "contact_email": "a@partner.invalid", "rate_card_id": "card-a4"},
        {"id": "b", "carrier_name": "Partner b", "contact_email": "b@partner.invalid"},
        {"id": "c", "carrier_name": "Partner c", "contact_email": "c@partner.invalid"}]


def offer(pid, cents, mid, valid_to="2026-11-15"):
    return {"outreach_id": pid, "partner": f"Partner {pid}", "cost_cents": cents, "transit_days": 3, "valid_to": valid_to,
            "simulated": True, "message_id": mid}


def preview(*card_ids):  # the live shape: one option per priced rate, with the card on the option and on its legs
    return {"options": [{"service": "ROAD_LTL", "rate_card_id": c, "legs": [{"seq": 1, "rate_card_id": c}]} for c in card_ids]
            + [{"service": "ROAD_LTL", "rate_card_id": None, "legs": [{"seq": 1, "rate_card_id": None}]}]}


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


def harness(monkeypatch, first, priced, after_counter=None, card=CARD):
    """quote_phase on a reused shipment up to pricing. The DB fake can read the accepted card and its source message
    but has no way to write either, so any rewrite of the card fails the test."""
    events, follow = [], AsyncMock()
    msgs = NS(find_one=AsyncMock(return_value={"provider_id": "demo-sim:katupojat"}))
    monkeypatch.setattr(d, "db", NS(demo_runs=NS(update_one=AsyncMock()), rate_cards=NS(find_one=AsyncMock(return_value=dict(card))),
                                    partner_messages=msgs))
    monkeypatch.setattr(d, "emit", AsyncMock(side_effect=lambda *a, **k: events.append((a, k))))
    monkeypatch.setattr(d, "_run", AsyncMock(return_value={"status": "running", "request_text": "in-memory", "reuse_from": "old",
                                                         "origin_text": "Oulu", "destination_text": "Bergen"}))
    monkeypatch.setattr(d, "_parse", AsyncMock(return_value=({"request": dict(REQ), "missing": []}, {}, {})))
    monkeypatch.setattr(d, "_missing", lambda *a: [])
    monkeypatch.setattr(d, "_essential_missing", lambda *a: [])
    monkeypatch.setattr(d, "_complete", lambda req: req)
    monkeypatch.setattr(d, "_reference", AsyncMock(return_value={}))
    monkeypatch.setattr(d, "_reuse_outreach", AsyncMock(return_value=ROWS))
    monkeypatch.setattr(global_quote, "preview", AsyncMock(return_value=preview(*priced)))
    calls = {"n": 0}

    async def collect(run_id, rows, req, extras, reference, until, offers, state):
        calls["n"] += 1
        batch = first if calls["n"] == 1 else (after_counter or {})
        if callable(batch):  # a round with real replies: the transitions production _collect makes
            return batch(offers, state)
        offers.update(batch)
        state.setdefault("final", {}).update({k: "offer" for k in batch})
    monkeypatch.setattr(d, "_collect", collect)
    monkeypatch.setattr(d, "_follow_up", follow)
    price = AsyncMock(side_effect=Stop)
    monkeypatch.setattr(d, "_price", price)
    return events, follow, price


def real(pid, cents=None, mid=None):
    """Production _collect on a partner's real reply: the partner's thread is real from now on, its simulated rate is
    gone, and a rate (cents) is its offer while a decline (no cents) leaves it out."""
    def apply(offers, state):
        state.setdefault("real", set()).add(pid)
        if (offers.get(pid) or {}).get("simulated"):
            offers.pop(pid)
        if cents:
            offers[pid] = {**offer(pid, cents, mid), "simulated": False}
        state.setdefault("final", {})[pid] = "offer" if cents else "declined"
    return apply


def rounds(*steps):
    """One collection round of several partners' replies; a dict is simulated offers, a callable a real reply."""
    def apply(offers, state):
        for step in steps:
            if callable(step):
                step(offers, state)
            else:
                offers.update(step)
                state.setdefault("final", {}).update({k: "offer" for k in step})
    return apply


def titles(events):
    return [a[2] for a, _ in events]


@pytest.mark.asyncio
async def test_accepted_card_native_quotes_is_used_at_its_terms_without_a_counter(monkeypatch):
    # what A5 saw: the re-read thread's latest answer (74000, a new message) differs from the accepted card (76000)
    first = {"a": offer("a", 74000, "<a-new@katupojat.invalid>"), "b": offer("b", 81000, "b1"), "c": offer("c", 87000, "c1")}
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"])
    with pytest.raises(Stop):
        await d.quote_phase("r")
    follow.assert_not_awaited()  # no counteroffer to the agreed partner (nor to anyone: it is the best valid rate)
    pick = price.await_args.args[3]
    assert {k: pick[k] for k in ("outreach_id", "cost_cents", "transit_days", "valid_to", "message_id", "agreed_card")} == \
        {"outreach_id": "a", "cost_cents": 76000, "transit_days": 3, "valid_to": "2026-11-15",
         "message_id": "<a-accept@katupojat.invalid>", "agreed_card": "card-a4"}
    assert "Rate already agreed: EUR 760.00 · 3 days" in titles(events)
    assert "Comparing 3 rates · best rate already agreed" in titles(events)
    agreed = next(k["data"] for a, k in events if a[2].startswith("Rate already agreed"))
    assert agreed["title_key"] == "partner.rate_agreed" and agreed["reply"] == {"source": "world"}
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner a")
    assert best["ranking"][0]["agreed"] is True and "agreed" not in best["ranking"][1]
    # native procurement's guard now accepts the pick as the very card, without writing anything
    monkeypatch.setattr(d, "db", NS(partner_outreach=NS(find_one=AsyncMock(return_value=dict(ROWS[0]))),
                                    rate_cards=NS(find_one=AsyncMock(return_value=dict(CARD)))))
    assert (await d._promote_partner_rate(REQ, "road", pick))["id"] == "card-a4"


@pytest.mark.asyncio
async def test_card_native_does_not_quote_for_this_request_is_left_out(monkeypatch):
    first = {"a": offer("a", 74000, "<a-new@katupojat.invalid>"), "b": offer("b", 81000, "b1"), "c": offer("c", 87000, "c1")}
    accepted = {"b": offer("b", 77000, "b2")}
    events, follow, price = harness(monkeypatch, first, priced=[], after_counter=accepted)
    with pytest.raises(Stop):
        await d.quote_phase("r")
    assert "Agreed rate does not fit this request" in titles(events)
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]  # the counter goes to the best rate left
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"]) == ("b", 77000) and "agreed_card" not in pick
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner b")
    assert [o["partner"] for o in best["ranking"]] == ["Partner b", "Partner c"]


@pytest.mark.asyncio
async def test_agreed_card_expired_on_the_ready_date_is_left_out(monkeypatch):
    first = {"a": offer("a", 74000, "<a-new@katupojat.invalid>"), "b": offer("b", 81000, "b1"), "c": offer("c", 87000, "c1")}
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter={"b": offer("b", 77000, "b2")},
                                    card={**CARD, "valid_to": "2026-11-01"})
    with pytest.raises(Stop):
        await d.quote_phase("r")
    skip = next(k["data"] for a, k in events if a[2] == "Agreed rate does not fit this request")
    assert "valid until 2026-11-01" in skip["reasoning"]
    assert price.await_args.args[3]["outreach_id"] == "b"


@pytest.mark.asyncio
async def test_a_cheaper_rate_is_countered_never_the_agreed_card(monkeypatch):
    first = {"a": offer("a", 74000, "<a-new@katupojat.invalid>"), "b": offer("b", 70000, "b1"), "c": offer("c", 87000, "c1")}
    late = {"a": offer("a", 50000, "<a-late@katupojat.invalid>"), "b": offer("b", 66000, "b2")}  # a late answer on a's thread
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter=late)
    with pytest.raises(Stop):
        await d.quote_phase("r")
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"]) == ("b", 66000)
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner b")
    assert best["ranking"][1] == {**best["ranking"][1], "partner": "Partner a", "cost_cents": 76000, "agreed": True}


@pytest.mark.asyncio
async def test_reviewer_probe_a_real_decline_during_the_counter_never_brings_the_agreed_card_back(monkeypatch):
    # the reviewer's late_real_probe on 7c58c61d: the agreed card resurrected after the partner's real decline
    first = {"a": offer("a", 74000, "sim-first"), "b": offer("b", 70000, "sim-b")}
    late = rounds(real("a"), {"b": offer("b", 90000, "b-counter")})
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter=late)
    with pytest.raises(Stop):
        await d.quote_phase("r")
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"], pick["message_id"]) == ("b", 90000, "b-counter")
    assert "agreed_card" not in pick
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner b")
    assert [o["partner"] for o in best["ranking"]] == ["Partner b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["first round", "during the counter"])
async def test_a_real_new_price_beats_the_simulated_agreed_card_and_a_person_decides(monkeypatch, when):
    # a's real 79000 replaces the agreed 76000 card although it is dearer: real evidence wins; it is not countered
    # (only the rate request reaches a real partner) and procurement takes no second offer on the thread by itself
    requested = AsyncMock(return_value={"id": "card-review"})
    from core import approvals
    monkeypatch.setattr(approvals, "request_approval", requested)
    if when == "first round":
        first = rounds(real("a", 79000, "<a-real@partner.invalid>"), {"b": offer("b", 81000, "b1"), "c": offer("c", 87000, "c1")})
        late = None
    else:
        first = {"a": offer("a", 74000, "sim-first"), "b": offer("b", 70000, "b1"), "c": offer("c", 87000, "c1")}
        late = rounds(real("a", 69000, "<a-real@partner.invalid>"), {"b": offer("b", 70000, "b2")})  # b answers: 70000 stands
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter=late)
    await d.quote_phase("r")
    price.assert_not_awaited()
    assert [c.args[1]["id"] for c in follow.await_args_list] == ([] if when == "first round" else ["b"])
    review = next(k["data"] for a, k in events if a[2] == "A real reply changes an accepted rate · a person decides")
    top = review["ranking"][0]
    assert (top["partner"], top["cost_cents"], top["outreach_id"]) == ("Partner a", 79000 if when == "first round" else 69000, "a")
    assert not any(o.get("agreed") for o in review["ranking"])
    assert requested.await_args.kwargs["reason"] == "real_reply_after_acceptance"
    assert requested.await_args.kwargs["data"]["outreach_id"] == "a"
    if when == "first round":
        assert "Rate already agreed: EUR 760.00 · 3 days" not in titles(events)
        skip = next(k["data"] for a, k in events if a[2] == "Agreed rate does not fit this request")
        assert "real reply" in skip["reasoning"]


@pytest.mark.asyncio
async def test_a_real_decline_excludes_the_partner_and_its_agreed_card(monkeypatch):
    first = rounds(real("a"), {"b": offer("b", 81000, "b1"), "c": offer("c", 87000, "c1")})
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter={"b": offer("b", 77000, "b2")})
    with pytest.raises(Stop):
        await d.quote_phase("r")
    assert "Rate already agreed: EUR 760.00 · 3 days" not in titles(events)
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"]) == ("b", 77000) and "agreed_card" not in pick
    best = next(k["data"] for a, k in events if a[2] == "Best rate: Partner b")
    assert [o["partner"] for o in best["ranking"]] == ["Partner b", "Partner c"]


@pytest.mark.asyncio
async def test_a_real_best_rate_is_never_countered(monkeypatch):
    first = rounds(real("b", 70000, "<b-real@partner.invalid>"), {"a": offer("a", 74000, "sim-first"), "c": offer("c", 87000, "c1")})
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"])
    with pytest.raises(Stop):
        await d.quote_phase("r")
    follow.assert_not_awaited()
    pick = price.await_args.args[3]
    assert (pick["outreach_id"], pick["cost_cents"], pick["simulated"]) == ("b", 70000, False)


@pytest.mark.asyncio
async def test_a_follow_up_on_a_real_thread_sends_nothing_without_the_rehearsal_sandbox(monkeypatch):
    from services.demo_autoquote import conf, mail
    events = []
    monkeypatch.setattr(conf, "partner_sandbox", lambda: None)
    monkeypatch.setattr(mail, "send", AsyncMock(side_effect=AssertionError("real partner mail")))
    monkeypatch.setattr(d, "emit", AsyncMock(side_effect=lambda *a, **k: events.append((a, k))))
    monkeypatch.setattr(d, "db", NS())  # no reads or writes either
    for kind in ("counteroffer", "clarify"):
        await d._follow_up("r", ROWS[1], kind, "", {}, simulated_thread=False, counter_cents=66000)
    assert titles(events) == ["Real reply · the demo does not answer it"] * 2
    assert events[0][1]["data"]["title_key"] == "partner.real_reply_held"


def person_card(monkeypatch):
    requested = AsyncMock(return_value={"id": "card-person"})
    from core import approvals
    monkeypatch.setattr(approvals, "request_approval", requested)
    return requested


@pytest.mark.asyncio
async def test_reviewer_probe_every_partner_declines_during_the_counter_and_a_person_decides(monkeypatch):
    # the reviewer's all_decline_probe on cccc0a7c: an empty pool crashed _no_valid_rate (IndexError)
    requested = person_card(monkeypatch)
    first = {"a": offer("a", 74000, "sim-first"), "b": offer("b", 70000, "sim-b")}
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter=rounds(real("a"), real("b")))
    await d.quote_phase("r")
    price.assert_not_awaited()
    assert [c.args[1]["id"] for c in follow.await_args_list] == ["b"]  # the one simulated counter, before the declines
    kw = requested.await_args.kwargs
    assert requested.await_args.args[3] == "No usable partner rate" and kw["reason"] == "no_partner_rate"
    assert [(r["outreach_id"], r["reply"], r["source"]) for r in kw["data"]["replies"]] == \
        [("a", "declined", "real"), ("b", "declined", "real"), ("c", "no reply", "world")]
    assert kw["summary"] == "2 of 3 partners declined; the other 1 have no usable rate for this request. A person decides."
    ev = next(k["data"] for a, k in events if a[2] == "No usable partner rate · a person decides")
    assert ev["title_key"] == "replies.no_rate" and ev["approval_id"] == "card-person" and ev["ranking"] == []
    status = d.db.demo_runs.update_one.await_args.args[1]["$set"]
    assert (status["status"], status["awaiting"]) == ("awaiting_approval", ["a partner rate"])


@pytest.mark.asyncio
async def test_some_partners_decline_and_the_rest_expire_during_the_counter_is_the_validity_card(monkeypatch):
    requested = person_card(monkeypatch)
    first = {"a": offer("a", 74000, "sim-first"), "b": offer("b", 70000, "sim-b")}
    late = rounds(real("a"), {"b": offer("b", 66000, "b2", valid_to="2026-11-01")})  # b's answer expires before 2026-11-04
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"], after_counter=late)
    await d.quote_phase("r")
    price.assert_not_awaited()
    kw = requested.await_args.kwargs
    assert requested.await_args.args[3] == "No valid rate for the ready date" and kw["reason"] == "rate_validity"
    assert [(o["outreach_id"], o["cost_cents"]) for o in kw["data"]["comparison"]] == [("b", 66000)]
    assert [(r["outreach_id"], r["reply"]) for r in kw["data"]["replies"]] == [("a", "declined"), ("b", "offer"), ("c", "no reply")]
    assert "No valid rate for the ready date · a person decides" in titles(events)


@pytest.mark.asyncio
async def test_a_first_round_without_any_partner_rate_is_labelled_market_rates(monkeypatch):
    first = rounds(real("a"), real("b"))  # every reply a decline, c silent: production's market-rate fallback, labelled
    events, follow, price = harness(monkeypatch, first, priced=["card-a4"])
    with pytest.raises(Stop):
        await d.quote_phase("r")
    follow.assert_not_awaited()
    assert price.await_args.args[3] is None
    ev = next(k["data"] for a, k in events if a[2] == "Priced from market rates · no partner rate")
    assert ev["title_key"] == "replies.market_rates" and ev["price_basis"] == "market_reference"
