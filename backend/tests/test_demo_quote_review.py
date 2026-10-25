"""Regressions from the reviewer's 235357a in-memory probe. No real HTTP, mail, LLM or MongoDB."""
import copy
import socket
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from services import beneficial_owners as bo, pep
from services.demo_autoquote import driver as d, simulator as s
from services.demo_world import boundary, registry


def matches(row, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(row, q) for q in expected):
                return False
            continue
        value = row.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$gte":
                    if value is None or value < operand:
                        return False
                elif op == "$ne":
                    if value == operand:
                        return False
                elif op == "$nin":
                    if value in operand:
                        return False
                elif op == "$in":
                    if value not in operand:
                        return False
                else:
                    raise AssertionError(f"Unsupported query operator {op}")
        elif value != expected:
            return False
    return True


class Rows:
    def __init__(self, rows=()):
        self.rows = list(copy.deepcopy(rows))

    async def find_one(self, query, *args, **kwargs):
        return next((copy.deepcopy(r) for r in reversed(self.rows) if matches(r, query)), None)

    async def insert_one(self, row):
        self.rows.append(copy.deepcopy(row))


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args):
        return self

    async def to_list(self, *args):
        return self.rows


@pytest.fixture(autouse=True)
def intercept_side_effects(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("DEMO_EMAIL_PROVIDER", "log")
    monkeypatch.delenv("SMTP2GO_API_KEY", raising=False)
    for key in ("PRH_BO_API_URL", "PRH_BO_CLIENT_ID", "PRH_BO_USER_ID", "OPENSANCTIONS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("Unexpected HTTP")))
    from core import email, llm
    monkeypatch.setattr(email, "send_email", AsyncMock(side_effect=AssertionError("Unexpected mail")))
    monkeypatch.setattr(llm, "llm_text", AsyncMock(side_effect=AssertionError("Unexpected model call")))


@pytest.mark.asyncio
async def test_real_decline_removes_simulated_offer_from_ranking(monkeypatch):
    real = {"id": "real-decline", "outreach_id": "p", "seq": 2, "provider_id": "smtp-real",
            "status": "declined", "parsed": {"intent": "decline", "status": "ok"}}
    monkeypatch.setattr(d, "emit", AsyncMock())
    monkeypatch.setattr(d, "_run", AsyncMock(return_value={"status": "running"}))
    monkeypatch.setattr(d, "db", NS(partner_messages=NS(find=lambda *a: Cursor([real]), update_one=AsyncMock())))
    offers = {"p": {"outreach_id": "p", "cost_cents": 10000, "simulated": True, "message_id": "sim-old"}}
    state = {}
    await d._collect("r", [{"id": "p", "carrier_name": "Fixture", "contact_email": "p@example.invalid"}],
                     {}, {}, {}, time.monotonic() + 1, offers, state)
    assert state["final"] == {"p": "declined"}
    assert d._rank(offers) == []


@pytest.mark.asyncio
async def test_missing_cargo_size_and_requester_timeout_holds_before_pricing(monkeypatch):
    req = {"origin": {"country": "FI", "name": "Helsinki"}, "destination": {"country": "DE", "name": "Hamburg"},
           "mode": "road", "cargo": {"description": "bolts", "weight_kg": 1000, "packages": None, "volume_m3": None},
           "ready_date": "2026-11-01", "incoterm": "DAP"}
    monkeypatch.setattr(d, "_run", AsyncMock(return_value={"status": "running", "request_text": "in-memory",
                                                         "origin_text": "Helsinki", "destination_text": "Hamburg"}))
    monkeypatch.setattr(d, "_parse", AsyncMock(return_value=({"request": req, "missing": []},
        {"pickup_address": "stated", "delivery_address": "stated", "dangerous_goods_stated": True}, {})))
    writes = AsyncMock()
    monkeypatch.setattr(d, "db", NS(demo_runs=NS(update_one=writes)))
    monkeypatch.setattr(d, "emit", AsyncMock())
    monkeypatch.setattr(d, "_research", AsyncMock(return_value=[]))
    monkeypatch.setattr(d, "_clarify", AsyncMock(return_value=("question", ["q"])))
    monkeypatch.setattr(d, "_await_requester", AsyncMock(return_value=None))
    reference = AsyncMock(side_effect=AssertionError("Pricing before essential cargo facts"))
    monkeypatch.setattr(d, "_reference", reference)
    await d.quote_phase("r")
    reference.assert_not_awaited()
    assert any(c.args[1].get("$set", {}).get("status") == "awaiting_requester" for c in writes.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("rfq,owned,allowed", [
    (None, True, False),
    ({"status": "failed", "demo_run_id": "r"}, True, False),
    ({"status": "queued", "demo_run_id": "r"}, True, False),
    ({"status": "logged", "demo_run_id": "r"}, True, False),
    ({"status": None, "demo_run_id": "r"}, True, False),
    ({"status": "sent", "demo_run_id": "other"}, True, False),
    ({"status": "sent", "demo_run_id": "r"}, False, False),
    ({"status": "sent", "demo_run_id": "r"}, True, True),
    ({"status": "sent", "demo_run_id": "halted", "reused_by_run": "r"}, True, True),  # taken over from a halted run
    ({"status": "sent", "demo_run_id": "halted", "reused_by_run": "other"}, True, False),
])
async def test_simulator_requires_sent_rfq_and_partner_ownership(monkeypatch, rfq, owned, allowed):
    monkeypatch.setattr(s, "db", NS(
        demo_runs=Rows([{"id": "r", "status": "running"}]),
        partner_messages=Rows([{**rfq, "outreach_id": "p", "direction": "out", "kind": "rfq", "text": "rate request"}] if rfq else []),
        partner_outreach=NS(find_one=Rows([{"id": "p", "demo_run_id": "r"}] if owned else []).find_one,
                            update_one=AsyncMock())))
    monkeypatch.setattr(s, "real_reply_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(s, "llm_text", AsyncMock(return_value="EUR 100 all-in"))
    monkeypatch.setattr(s, "emit", AsyncMock())
    monkeypatch.setattr(s.conf, "sim_model", lambda: None)
    from services.email_channel import dispatch
    inbound = AsyncMock(return_value={"channel": "partner"})
    monkeypatch.setattr(dispatch, "dispatch_raw", inbound)
    await s.deliver({"id": "r"}, {"id": "p", "carrier_name": "Fixture", "contact_email": "p@example.invalid"},
        turn=0, behaviour="quote", n={"total_cents": 10000, "transit_days": 2, "valid_to": "2026-12-31"},
        follow_up=None, subject="RFQ", in_reply_to="out", references=["out"], delay=0)
    assert inbound.await_count == int(allowed)


@pytest.fixture
def persona(monkeypatch):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    identity = {"company": "Fictional Fixture Oy", "business_id": "1234567-1",
                "address": {"street": "Fixture 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"},
                "signatory": {"name": "Private Signatory", "title": "Director"},
                "ubos": [{"name": "Private Owner", "country": "FI", "ownership_pct": 100, "pep": False}]}
    store = Rows([{"run_id": "first-run", "business_id": identity["business_id"], "identity": identity,
                   "created_at": "2026-10-25T00:00:00Z"}])
    monkeypatch.setattr(registry, "db", NS(raw=NS(world_registry=store)))
    trace = AsyncMock()
    monkeypatch.setattr(registry, "_trace", trace)
    checks = Rows()
    monkeypatch.setattr(pep, "db", NS(pep_checks=checks))
    return NS(identity=identity, store=store, checks=checks, trace=trace,
              company={"id": "persona", "name": identity["company"], "business_id": identity["business_id"],
                       "country": "FI", "demo_run_id": "later-run", "signatory": identity["signatory"], "ubos": identity["ubos"]})


@pytest.mark.asyncio
async def test_persona_gleif_never_reaches_http(persona):
    assert await bo.gleif_lookup("1234567-1") == {"found": False}
    httpx.AsyncClient.send.assert_not_awaited()
    persona.trace.assert_awaited_once()


@pytest.mark.asyncio
async def test_persona_gleif_ownership_and_pep_use_registry_protocols(persona):
    owners = await bo.owners_for_company(persona.company, ytj={"found": True})
    assert owners["lei"] is None and owners["parents"] == []
    assert owners["owners"][0]["name"] == "Private Owner"
    assert owners["owners"][0]["share_pct"] == 100
    assert owners["owners"][0]["source"] == "PRH"
    assert bo.compare_declared(owners) == {"compared": True, "missing_from_declaration": [], "not_in_register": []}
    assert {s["source"]: s["status"] for s in owners["sources_checked"]} == {
        "YTJ": "not_available", "GLEIF": "ok", "PRH": "ok"}
    result = await pep.kyc_check(persona.company, owners)
    assert result["status"] == "error"  # signatory has no PEP facts in the stored actor identity
    assert {p["name"]: p["status"] for p in result["persons"]} == {"Private Owner": "clear", "Private Signatory": "error"}
    assert {call.args[1] for call in persona.trace.await_args_list} == {"GLEIF", "PRH", "PEP"}
    assert all(row["key"].startswith("world:1234567-1:") for row in persona.checks.rows)


@pytest.mark.asyncio
async def test_company_refresh_preserves_persona_context(persona, monkeypatch):
    from routes import compliance_owners
    writes = AsyncMock()
    monkeypatch.setattr(compliance_owners, "db", NS(companies=NS(update_one=writes)))
    monkeypatch.setattr(compliance_owners, "audit", AsyncMock())
    result = await compliance_owners.refresh_company(persona.company, None, refresh_pep=True)
    assert {p["name"]: p["status"] for p in result["persons"]} == {"Private Owner": "clear", "Private Signatory": "error"}
    assert result["owners"][0]["share_pct"] == 100
    writes.assert_awaited_once()


@pytest.mark.asyncio
async def test_persona_opensanctions_protocol(persona, monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test-not-a-credential")
    result = await pep.check_person("Private Owner", country="FI", persona_business_id="1234567-1")
    assert result["status"] == "clear" and result["source"] == "OpenSanctions"
    assert result["sources"][0]["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Unregistered Person", "Private Owner"])
async def test_missing_persona_pep_facts_are_error_not_clear(persona, name):
    persona.store.rows[0]["identity"]["ubos"][0].pop("pep")
    result = await pep.check_person(name, persona_business_id="1234567-1")
    assert result["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Wikidata", "OpenSanctions"])
@pytest.mark.parametrize("fact,expected", [(None, "error"), (True, "pep"), (False, "clear")])
@pytest.mark.parametrize("also_owner", [False, True])
async def test_signatory_preserves_missing_positive_and_negative_facts(persona, monkeypatch, provider, fact, expected, also_owner):
    if provider == "OpenSanctions":
        monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test-not-a-credential")
    identity = persona.store.rows[0]["identity"]
    if fact is not None:
        identity["signatory"]["pep"] = fact
    if also_owner:
        identity["ubos"].append({"name": " private  SIGNATORY ", "country": "FI", "ownership_pct": 10, "pep": False})
    before = copy.deepcopy(identity)
    result = await pep.check_person("Private Signatory", country="FI", persona_business_id="1234567-1")
    assert result["status"] == ("clear" if fact is None and also_owner else expected)
    assert result["source"] == ("World registry" if fact is True else provider)
    assert identity == before
    httpx.AsyncClient.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fact", [None, True])
async def test_persona_cache_cannot_clear_changed_or_prepatch_signatory_facts(persona, fact):
    identity = persona.store.rows[0]["identity"]
    identity["signatory"]["pep"] = False
    first = await pep.check_person("Private Signatory", country="FI", persona_business_id="1234567-1")
    assert first["status"] == "clear"
    if fact is None:
        identity["signatory"].pop("pep")
    else:
        identity["signatory"]["pep"] = fact
    # Same key as the old release's falsely clear result; it must be re-evaluated from current actor facts.
    again = await pep.check_person("Private Signatory", country="FI", persona_business_id="1234567-1")
    assert again["status"] == ("pep" if fact is True else "error") and not again["cached"]
    httpx.AsyncClient.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_positive_signatory_enters_normal_human_pep_review(persona):
    persona.store.rows[0]["identity"]["signatory"]["pep"] = True
    result = await pep.kyc_check(persona.company)
    assert result["status"] == "review" and result["legal_point"] == "pep_relationship"
    assert result["hit_keys"]
    signatory = next(p for p in result["persons"] if p["name"] == "Private Signatory")
    assert signatory["status"] == "pep" and signatory["matches"][0]["pep"] is True
    assert "World registry" in result["summary"]
    httpx.AsyncClient.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "signatory", "owner"])
async def test_persona_seed_requires_and_persists_explicit_pep_facts(monkeypatch, missing):
    from services.demo_world import identity
    seed = {"company": "Kuutamola Oy", "contact_name": "Private Contact",
            "address": {"street": "Test Lane 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"},
            "signatory": {"name": "Private Contact", "title": "Director", "pep": False},
            "ubos": [{"name": "Private Owner", "country": "FI", "ownership_pct": 100, "pep": False}]}
    if missing:
        (seed["signatory"] if missing == "signatory" else seed["ubos"][0]).pop("pep")
    rows = Rows()
    monkeypatch.setattr(identity, "db", NS(raw=NS(world_registry=rows)))
    monkeypatch.setattr(registry, "db", NS(raw=NS(world_registry=rows)))
    monkeypatch.setattr(registry, "_trace", AsyncMock())
    monkeypatch.setattr(identity.llm, "ask_json", AsyncMock(return_value=seed))
    monkeypatch.setenv("DEMO_REQUESTER_ADDRESS", "")

    async def collision_transport(client, request, *args, **kwargs):
        assert request.url.host == "avoindata.prh.fi"
        return httpx.Response(200, request=request, json={"companies": []})

    monkeypatch.setattr(httpx.AsyncClient, "send", collision_transport)
    run = {"id": "seed-test", "seed": 7, "origin_text": "Helsinki", "destination_text": "Hamburg"}
    if missing:
        with pytest.raises(ValueError, match="explicit PEP fact"):
            await identity.requester_identity(run)
        assert not rows.rows
        return
    seeded = await identity.requester_identity(run)
    assert seeded["signatory"]["pep"] is False and all(u["pep"] is False for u in seeded["ubos"])
    assert rows.rows[0]["identity"]["signatory"]["pep"] is False
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    monkeypatch.setattr(pep, "db", NS(pep_checks=Rows()))
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("Unexpected PEP HTTP")))
    result = await pep.kyc_check({"business_id": seeded["business_id"], "signatory": seeded["signatory"], "ubos": seeded["ubos"]})
    assert result["status"] == "pass" and all(p["status"] == "clear" for p in result["persons"])


@pytest.mark.asyncio
async def test_missing_ownership_facts_are_error_not_empty_register(persona):
    persona.store.rows[0]["identity"].pop("ubos")
    source, owners = await bo.PRHBeneficialOwnersAdapter().fetch("1234567-1")
    assert source["status"] == "error" and owners == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [True, False])
async def test_real_party_uses_original_transports_and_separate_pep_cache(persona, monkeypatch, sandbox):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", str(sandbox))
    bid = "7654321-9" if sandbox else "1234567-1"
    seen = []

    async def transport(client, request, *args, **kwargs):
        seen.append(request)
        if request.url.host == "api.gleif.org":
            return httpx.Response(200, request=request, json={"data": []})
        if request.url.host == "www.wikidata.org":
            return httpx.Response(200, request=request, json={"search": []})
        if request.url.host == "prh.fixture.invalid":
            return httpx.Response(200, request=request, content=b"<body><noBeneficialOwners>true</noBeneficialOwners></body>")
        raise AssertionError(f"Unintercepted URL {request.url.host}")

    monkeypatch.setattr(httpx.AsyncClient, "send", transport)
    monkeypatch.setenv("PRH_BO_API_URL", "https://prh.fixture.invalid/xroad")
    monkeypatch.setenv("PRH_BO_CLIENT_ID", "FI-TEST/COM/0000000-0/fixture")
    monkeypatch.setenv("PRH_BO_USER_ID", "fixture")
    assert await bo.gleif_lookup(bid) == {"found": False}
    source, owners = await bo.PRHBeneficialOwnersAdapter().fetch(bid)
    assert source["status"] == "ok" and owners == []
    result = await pep.check_person("Private Owner", country="FI", persona_business_id=bid)
    assert result["status"] == "clear" and not result["key"].startswith("world:")
    assert {r.url.host for r in seen} == {"api.gleif.org", "prh.fixture.invalid", "www.wikidata.org"}


@pytest.mark.asyncio
async def test_persona_pep_cache_cannot_clear_real_namesake(persona, monkeypatch):
    simulated = await pep.check_person("Private Owner", country="FI", persona_business_id="1234567-1")
    real = AsyncMock(return_value={"status": "pep", "matches": [{"pep": True}],
                                 "sources": [{"source": "Wikidata", "status": "ok"}]})
    monkeypatch.setattr(pep, "wikidata_check", real)
    namesake = await pep.check_person("Private Owner", country="FI")
    assert simulated["status"] == "clear" and namesake["status"] == "pep"
    assert namesake["key"] != simulated["key"] and not namesake["cached"]
    real.assert_awaited_once_with("Private Owner", "FI", None)
