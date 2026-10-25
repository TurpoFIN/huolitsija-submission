"""Sourcing Autopilot (Hankinta-autopilotti): limits, opt-out, B2B, kill switch, negotiation, rate loading.

Runs against MongoDB (DB_NAME from the CI runner) and the real services. E-mail uses the configured provider
(log in CI). The model is switched off here so the deterministic parser is exercised; the real-model reading is
proven separately (scripts/parse_proof.py, evidence). Discovery parsing uses responses captured from the real YTJ and
Wikidata APIs (tests/fixtures/*), served through httpx.MockTransport.
"""
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def motor_loop():
    from core.db import client, ensure_indexes
    client._io_loop = asyncio.get_running_loop()
    await ensure_indexes()  # the unique/partial indexes are part of what is under test
    from core.db import db as _db
    before = await _db.settings.find_one({"key": "app"}, {"_id": 0}) or {}
    yield
    # leave the shared CI database as found: later suites assert both switches are off
    await _db.settings.update_one({"key": "app"}, {"$set": {**{k: v for k, v in before.items() if k != "key"},
                                  "partner_network_enabled": bool(before.get("partner_network_enabled")),
                                  "autopilot_enabled": bool(before.get("autopilot_enabled"))}}, upsert=True)


from core import config
from core.autonomy import switch, validate_settings
from core.db import db
from core.settings import get_settings, update_settings
from core.util import new_id
from services import partner_discovery as disc
from services import partner_network as pn
from services import partner_parser as pp

BOT = {"id": "test-sourcing-operator", "name": "Sourcing test operator", "org_type": "internal"}
FIX = Path(__file__).parent / "fixtures"
VALID = (date.today() + timedelta(days=40)).isoformat()


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setenv("PARTNER_OUTREACH_LIVE", "true")
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "log")
    for k in ("OPENAI_API_KEY",):
        monkeypatch.setattr(config, k, "")
    monkeypatch.delenv("PARTNER_SANDBOX_DOMAIN", raising=False)
    monkeypatch.delenv("PARTNER_NETWORK_KILL", raising=False)
    monkeypatch.delenv("PARTNER_EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("PARTNER_SMTP_HOST", raising=False)


async def _policy(**extra):
    await update_settings({"partner_approved_lanes": ["road:FI>SE"], "partner_max_price_cents": 180000,
                           "partner_sell_price_cents": 220000, "partner_target_margin_pct": 18.0,
                           "partner_daily_send_limit": 20, "partner_per_partner_daily_limit": 4, "partner_max_rounds": 3, "partner_rfqs_per_lane": 3,
                           "partner_counter_anchor_pct": 5.0, **extra})
    await db.partner_send_counters.delete_many({})


async def _candidate(domain=None, email=None):
    domain = domain or f"carrier-{new_id()[:8]}.fi"
    cand = {"id": new_id(), "name": f"Testikuljetus {domain} Oy", "business_id": "0217115-3", "country": "FI",
            "domain": domain, "website": f"https://{domain}/", "roles": ["haulier"], "modes": ["road"],
            "contact_email": email or f"myynti@{domain}", "contact_source_url": f"https://{domain}/yhteystiedot",
            "status": "contactable", "lanes": ["road:FI>SE"], "score": 90, "sources": [{"source": "ytj", "url": "https://tietopalvelu.ytj.fi/yritys/0217115-3"}]}
    await db.partner_candidates.insert_one(dict(cand))
    return cand


async def _on():
    await switch("partner_network_enabled", True, "enable", BOT)


async def _rfq(cand):
    row = await pn.open_rfq(cand, "road:FI>SE", await get_settings())
    return await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})


# ---------------------------------------------------------------- policy units

def test_ceiling_and_concession_ladder():
    s = {"partner_approved_lanes": ["road:FI>SE"], "partner_target_margin_pct": 18.0, "partner_max_price_cents": 180000}
    assert pn.cost_limit(s, "road:FI>SE", 220000) == 180000            # max price binds
    assert pn.cost_limit(s, "road:FI>SE", 200000) == 164000            # margin binds: 2 000 x 0,82
    with pytest.raises(HTTPException):
        pn.cost_limit(s, "road:SE>FI", 220000)                         # lane outside policy
    ladder = [pn.counter_amount(180000, r, 3, 5.0) for r in (1, 2, 3)]
    assert ladder == [171000, 175500, 180000] and all(x <= 180000 for x in ladder)
    assert pn.counter_amount(180050, 1, 1, 5.0) == 180000               # whole euros, never above the ceiling


def test_settings_validation_for_new_limits():
    validate_settings({"partner_daily_send_limit": 0, "partner_max_rounds": 5, "partner_rfqs_per_lane": 1})
    for bad in ({"partner_daily_send_limit": 501}, {"partner_max_rounds": 6}, {"partner_rfqs_per_lane": 0},
                {"partner_counter_anchor_pct": 31}, {"partner_daily_send_limit": "20"}, {"partner_network_enabled": True}):
        with pytest.raises(HTTPException):
            validate_settings(dict(bad))


def test_b2b_rules():
    assert disc.b2b_check({"domain": "gmail.com"})[0] is False
    assert disc.b2b_check({"business_id": None, "domain": None})[0] is False
    assert disc.b2b_check({"business_id": "0217115-3", "legal_form_ok": False, "legal_form": "Private trader"})[0] is False
    assert disc.b2b_check({"business_id": "0217115-3", "domain": "kuljetus.fi", "legal_form_ok": True}) == (True, "business ID")
    assert disc.email_is_b2b("matti.virtanen@gmail.com", {"domain": "kuljetus.fi"})[0] is False
    assert disc.email_is_b2b("sales@other.fi", {"domain": "kuljetus.fi"})[0] is False
    assert disc.email_is_b2b("myynti@www.kuljetus.fi", {"domain": "kuljetus.fi"})[0] is True


def test_optout_token_roundtrip_and_tamper():
    t = pn.optout_token("Myynti@Kuljetus.fi")
    assert pn.optout_email_from_token(t) == "myynti@kuljetus.fi"
    assert pn.optout_email_from_token(t[:-1] + ("0" if t[-1] != "0" else "1")) is None
    assert pn.optout_email_from_token("garbage") is None


def test_sandbox_rewrite(monkeypatch):
    monkeypatch.setenv("PARTNER_SANDBOX_DOMAIN", "partners.sandbox.test")
    assert pn.sandbox_address("info@Kuljetus-Oy.fi") == "info.kuljetus-oy-fi@partners.sandbox.test"


# ---------------------------------------------------------------- parser (deterministic reader)

@pytest.mark.parametrize("text,expect", [
    ("Rahti: 1 650,00 EUR\nPolttoainelisä 12 %\nLauttamaksu 320 €\nKuljetusaika 2 vrk. Voimassa 31.12.2026 asti.",
     {"cost_cents": 216800, "base_cents": 165000, "all_in": True, "transit_days": 2, "valid_to": "2026-12-31"}),
    ("We can do EUR 1,890.00 all-in. Transit time 2-3 days. Valid until 30 Nov 2026.",
     {"cost_cents": 189000, "all_in": True, "transit_days": 3, "valid_to": "2026-11-30"}),
    ("Price EUR 1500 plus fuel surcharge according to index. Transit 2 days.", {"cost_cents": 150000, "all_in": False}),
    ("Hinta 1720 euroa, ja siihen sisältyy kaikki pakolliset lisät eli lautta ja polttoaine.", {"cost_cents": 172000, "all_in": True}),
    ("Valitettavasti emme pysty tarjoamaan tätä reittiä.", {"intent": "decline", "cost_cents": None}),
    ("Poistakaa meidät listaltanne.", {"intent": "opt_out"}),
])
def test_rule_parser(text, expect):
    r = pp.rule_parse(text, today=date(2026, 10, 24))
    for k, v in expect.items():
        assert r[k] == v, (k, r)


def test_quoted_rfq_is_never_read_as_the_offer():
    text = "Kiitos, palaamme asiaan.\n\nOn 24.10.2026 Huolitsija wrote:\n> Hinta 999 EUR all-in"
    assert pp.rule_parse(text)["cost_cents"] is None


def test_accept_counter_uses_our_counter_amount():
    r = pp.rule_parse("OK, we accept your counteroffer.", last_counter_cents=171000)
    assert r["intent"] == "accept_counter" and r["cost_cents"] == 171000


@pytest.mark.asyncio(loop_scope="module")
async def test_model_price_not_in_email_is_rejected(monkeypatch):
    async def invented(text, context, ref=None):
        return {"intent": "offer", "currency": "EUR", "cost_cents": 100, "base_cents": 100, "all_in": True,
                "transit_days": 2, "valid_to": VALID}
    monkeypatch.setattr(pp, "llm_available", lambda: True)
    monkeypatch.setattr(pp, "ai_parse", invented)
    r = await pp.parse_reply(f"Rahti 1 900 EUR, lisät sisältyvät, 2 päivää, voimassa {VALID}.", expected={"basis": "PER_SHIPMENT"})
    assert r["ai_rejected"] and r["cost_cents"] == 190000 and r["method"] == "rules"


# ---------------------------------------------------------------- discovery from captured real responses

@pytest.mark.asyncio(loop_scope="module")
async def test_ytj_and_wikidata_candidates_from_captured_responses():
    ytj = json.loads((FIX / "ytj_49410_vantaa.json").read_text())
    wd = json.loads((FIX / "wikidata_se_logistics.json").read_text())

    def handler(req: httpx.Request):
        if req.url.host == "avoindata.prh.fi":
            return httpx.Response(200, json=ytj if req.url.params.get("mainBusinessLine") == "49410" else {"companies": []})
        if req.url.host == "query.wikidata.org":
            return httpx.Response(200, json=wd)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fi, notes = await disc.ytj_candidates("road", client=client, hubs=["Vantaa"])
        se, _ = await disc.wikidata_candidates("SE", "road", client=client)
    bids = {c["business_id"] for c in fi}
    assert "0217115-3" in bids                      # active, has website
    assert "0115178-3" not in bids                  # company situation (e.g. liquidation) excluded
    monk = next(c for c in fi if c["business_id"] == "0217115-3")
    assert monk["website"] == "https://www.kuljetusliikemonkkonen.fi/" and monk["domain"] == "kuljetusliikemonkkonen.fi"
    assert monk["sources"][0]["url"] == "https://tietopalvelu.ytj.fi/yritys/0217115-3" and monk["legal_form_ok"]
    assert any(c["name"] == "PostNord" and c["sources"][0]["url"].startswith("https://www.wikidata.org/") for c in se)
    merged = disc.merge(fi + se + se)
    assert len([c for c in merged if c["name"] == "PostNord"]) == 1    # dedupe by domain / name
    score, why = disc.score({**monk, "contact_email": "info@kuljetusliikemonkkonen.fi"}, "road:FI>SE", date(2026, 10, 24))
    assert score >= 85 and any("role address" in w for w in why)


# ---------------------------------------------------------------- compliance gate (DB, real services)

@pytest.mark.asyncio(loop_scope="module")
async def test_switch_off_env_kill_and_halt_block_every_send(monkeypatch):
    await _policy()
    cand = await _candidate()
    await switch("partner_network_enabled", False, "", BOT)
    assert (await pn.run_cycle())["status"] == "off"
    await _on()
    monkeypatch.setenv("PARTNER_NETWORK_KILL", "true")
    row = await pn.open_rfq(cand, "road:FI>SE", await get_settings())
    assert row["send"] == {"status": "blocked", "reason": "Sourcing Autopilot is off"}
    monkeypatch.delenv("PARTNER_NETWORK_KILL")
    await db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": True}})
    try:
        assert not await pn.is_on()
    finally:
        await db.settings.update_one({"key": "app"}, {"$unset": {"autopilot_halted": ""}})
    assert await pn.is_on()


@pytest.mark.asyncio(loop_scope="module")
async def test_daily_limit_holds_rfqs_and_keeps_room_for_replies():
    await _policy(partner_daily_send_limit=5)
    await _on()
    rows = [await _rfq(await _candidate()) for _ in range(5)]
    sent = [r for r in rows if r["status"] == "awaiting_reply"]
    held = [r for r in rows if r["status"] == "queued"]
    assert len(sent) == 4 and len(held) == 1                         # RFQs may use 80 % of the limit
    assert held[0]["pending"] == {"kind": "rfq", "amount_cents": None}
    counter = await pn.send_partner_mail(sent[0], "counteroffer", 171000)
    assert counter["status"] in ("sent", "logged")                    # replies use the reserved 20 %
    over = await pn.send_partner_mail(sent[1], "counteroffer", 171000)
    assert over["status"] == "held_limit"
    assert await pn.sends_today() == 5
    out = await db.partner_messages.count_documents({"outreach_id": {"$in": [r["id"] for r in rows]}, "direction": "out"})
    assert out == 5                                                   # nothing beyond the limit left the building


@pytest.mark.asyncio(loop_scope="module")
async def test_opt_out_is_honoured_for_address_and_domain():
    await _policy()
    await _on()
    cand = await _candidate()
    row = await _rfq(cand)
    assert row["status"] == "awaiting_reply"
    res = await pn.add_optout(cand["contact_email"], source="test link")
    assert res["closed_rfqs"] == 1
    assert (await db.partner_outreach.find_one({"id": row["id"]}))["status"] == "opted_out"
    again = await pn.send_partner_mail(row, "reminder")
    assert again == {"status": "blocked", "reason": "recipient opted out"}
    colleague = await _candidate(domain=cand["domain"], email=f"ajojarjestely@{cand['domain']}")
    r2 = await pn.open_rfq(colleague, "road:SE>FI" if False else "road:FI>SE", await get_settings())
    assert r2 is None or r2["send"]["status"] in ("sent", "logged")   # address-level opt-out only blocks that address
    await pn.add_optout(f"toimisto@{cand['domain']}", source="test", scope="domain")
    third = await _candidate(domain=cand["domain"], email=f"myynti2@{cand['domain']}")
    third_row = await pn.open_rfq(third, "road:FI>SE", await get_settings())
    assert third_row is None or third_row["send"] == {"status": "blocked", "reason": "recipient opted out"}


@pytest.mark.asyncio(loop_scope="module")
async def test_non_b2b_recipient_is_never_mailed():
    await _policy()
    await _on()
    cand = await _candidate(email="kuljettaja.matti@gmail.com")
    row = await pn.open_rfq(cand, "road:FI>SE", await get_settings())
    assert row["send"]["status"] == "blocked" and "not B2B" in row["send"]["reason"]


@pytest.mark.asyncio(loop_scope="module")
async def test_rfq_carries_identity_optout_and_is_logged():
    await _policy()
    await _on()
    cand = await _candidate()
    row = await _rfq(cand)
    msg = await db.partner_messages.find_one({"outreach_id": row["id"], "direction": "out"}, {"_id": 0})
    assert msg["kind"] == "rfq" and row["ref"] in msg["subject"] and msg["to"] == cand["contact_email"]
    assert "Y-tunnus" in msg["text"] and "/api/partner-network/opt-out?t=" in msg["text"]
    assert cand["contact_source_url"] in msg["text"]                   # why they receive it
    assert "EUR 1" not in msg["text"].split("—")[0]                     # our ceiling is never disclosed in an RFQ


# ---------------------------------------------------------------- inbound -> negotiation -> rate card

def _reply(row, text, sender=None, mid=None):
    return {"message_id": mid or f"<{new_id()}@partner.test>", "from": sender or row["contact_email"],
            "to": "hankinta@huolitsija.test", "subject": f"Re: [{row['ref']}] Tarjouspyyntö", "text": text,
            "source": "imap", "in_reply_to": row.get("last_message_id")}


@pytest.mark.asyncio(loop_scope="module")
async def test_full_negotiation_to_accepted_rate_card(monkeypatch):
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy()
    await _on()
    await switch("autopilot_enabled", False, "", BOT)                   # independent of the main Autopilot
    cand = await _candidate()
    row = await _rfq(cand)
    r1 = await pn.ingest_message(_reply(row, f"Rahti: 1 650,00 EUR\nPolttoainelisä 12 %\nLauttamaksu 320 €\n"
                                             f"Kuljetusaika 2 vrk. Voimassa {VALID} asti."))
    assert r1["status"] == "counteroffered" and r1["counter_cents"] == 171000 and r1["parsed"]["cost_cents"] == 216800
    row = await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})
    assert row["rounds"] == 1 and row["last_counter_cents"] == 171000
    spoof = await pn.ingest_message(_reply(row, "hyväksymme 1 710 EUR", sender="attacker@evil.example"))
    assert spoof["status"] == "sender_mismatch"
    mid = f"<{new_id()}@partner.test>"
    r2 = await pn.ingest_message(_reply(row, "Hei, hyväksymme vastatarjouksenne 1 710,00 EUR. Kuljetusaika 2 vrk.", mid=mid))
    assert r2["status"] == "accepted", r2
    assert (await pn.ingest_message(_reply(row, "duplicate", mid=mid)))["status"] == "duplicate"
    row = await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})
    rate = await db.rate_cards.find_one({"id": row["rate_card_id"]}, {"_id": 0})
    assert rate["status"] == "accepted" and rate["source"] == "partner_network" and rate["all_in_cost_cents"] == 171000
    assert rate["valid_to"] == VALID and rate["transit_days"] == 2 and rate["origin"] == {"country": "FI"}
    assert rate["source_detail"]["ref"] == row["ref"] and rate["source_detail"]["business_id"] == "0217115-3"
    carrier = await db.carriers.find_one({"id": rate["carrier_id"]})
    assert carrier and carrier["active"] and carrier["source"] == "partner_network"
    kinds = [m["kind"] async for m in db.partner_messages.find({"outreach_id": row["id"], "direction": "out"}).sort("at", 1)]
    assert kinds == ["rfq", "counteroffer", "accept"]
    from services.pricing import price_card
    lines = price_card(rate, {"containers": [], "weight_kg": 18000, "volume_m3": 70, "ldm": 13.6, "is_fcl": False},
                       km=500, kind="main", leg_seq=1, freight_code="ROAD_FREIGHT")
    assert round(sum(ln["unit_cost_cents"] * ln["qty"] for ln in lines)) == 171000


@pytest.mark.asyncio(loop_scope="module")
async def test_itemised_offer_inside_limit_keeps_surcharges_and_prices_exactly(monkeypatch):
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy()
    await _on()
    row = await _rfq(await _candidate())
    res = await pn.ingest_message(_reply(row, f"Rahti 1 400,00 EUR\nPolttoainelisä 10 %\nLauttamaksu 150 €\n"
                                              f"Kuljetusaika 3 päivää. Voimassa {VALID} asti."))
    assert res["status"] == "accepted" and res["all_in_cost_cents"] == 169000      # 1 400 + 140 + 150
    rate = await db.rate_cards.find_one({"id": res["id"]}, {"_id": 0})
    assert rate["unit_cost_cents"] == 140000 and {s["code"] for s in rate["surcharges"]} == {"FUEL", "FERRY"}
    from services.pricing import price_card
    lines = price_card(rate, {"containers": [], "weight_kg": 18000, "volume_m3": 70, "ldm": 13.6, "is_fcl": False},
                       km=500, kind="main", leg_seq=1, freight_code="ROAD_FREIGHT")
    assert round(sum(ln["unit_cost_cents"] * ln["qty"] for ln in lines)) == 169000


@pytest.mark.asyncio(loop_scope="module")
async def test_rounds_exhausted_then_declined_and_far_above_declined_at_once(monkeypatch):
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy(partner_max_rounds=2)
    await _on()
    row = await _rfq(await _candidate())
    for i, price in enumerate(("2 100", "2 050")):
        r = await pn.ingest_message(_reply(row, f"Hinta {price} EUR all-in, kuljetusaika 2 päivää, voimassa {VALID} asti."))
        assert r["status"] == "counteroffered" and r["round"] == i + 1
        row = await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})
    r = await pn.ingest_message(_reply(row, f"Hinta 2 000 EUR all-in, kuljetusaika 2 päivää, voimassa {VALID} asti."))
    assert r["status"] == "declined"
    assert not await db.rate_cards.find_one({"outreach_id": row["id"]})
    row2 = await _rfq(await _candidate())
    r = await pn.ingest_message(_reply(row2, f"Hinta 9 000 EUR all-in, 2 päivää, voimassa {VALID} asti."))
    assert r["status"] == "declined"


@pytest.mark.asyncio(loop_scope="module")
async def test_reply_opt_out_and_unclear_reply_get_one_clarification(monkeypatch):
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy()
    await _on()
    row = await _rfq(await _candidate())
    r = await pn.ingest_message(_reply(row, "Price EUR 1500 plus fuel surcharge per index."))
    assert r["status"] == "clarification_sent"
    r = await pn.ingest_message(_reply(row, "As said, plus fuel."))
    assert r["status"] == "needs_review"
    row2 = await _rfq(await _candidate())
    r = await pn.ingest_message(_reply(row2, "Poistakaa meidät listalta, kiitos."))
    assert r["status"] == "opted_out"
    assert await db.partner_optouts.find_one({"email": row2["contact_email"]})


@pytest.mark.asyncio(loop_scope="module")
async def test_unauthenticated_mail_is_not_acted_on(monkeypatch):
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "true")
    await _policy()
    await _on()
    row = await _rfq(await _candidate())
    r = await pn.ingest_message({**_reply(row, f"Hinta 1 500 EUR all-in, 2 päivää, voimassa {VALID}."), "auth_results": "mx; dmarc=fail"})
    assert r["status"] == "unauthenticated"
    ok = await pn.ingest_message({**_reply(row, f"Hinta 1 500 EUR all-in, 2 päivää, voimassa {VALID}."),
                                  "auth_results": "mx.huolitsija.fi; dkim=pass header.d=x; dmarc=pass"})
    assert ok["status"] == "accepted"


@pytest.mark.asyncio(loop_scope="module")
async def test_kill_switch_cancels_queue_and_needs_enable_again():
    await _policy(partner_daily_send_limit=1)
    await _on()
    held = await _rfq(await _candidate())                               # limit 1 -> RFQ share 0 -> queued
    assert held["status"] == "queued"
    res = await pn.stop(BOT)
    assert res["enabled"] is False and res["cancelled"] >= 1
    assert (await db.partner_outreach.find_one({"id": held["id"]}))["status"] == "cancelled"
    assert await db.audit_log.find_one({"action": "partner_network_killed", "actor.id": BOT["id"]})
    with pytest.raises(HTTPException):
        await switch("partner_network_enabled", True, "Enable", BOT)
    await _policy()


@pytest.mark.asyncio(loop_scope="module")
async def test_retention_drops_old_records_but_keeps_optouts_and_partners():
    old = "2020-01-01T00:00:00Z"
    await db.system.delete_one({"key": "partner_retention"})
    mid = new_id()
    await db.partner_messages.insert_one({"id": mid, "direction": "out", "at": old})
    stale, partner = new_id(), new_id()
    await db.partner_candidates.insert_many([{"id": stale, "status": "contactable", "updated_at": old},
                                             {"id": partner, "status": "partner", "updated_at": old}])
    await db.partner_optouts.update_one({"email": "old@kept.fi"}, {"$set": {"email": "old@kept.fi", "at": old}}, upsert=True)
    res = await pn.retention()
    assert res["status"] == "ran" and res["messages"] >= 1
    assert not await db.partner_messages.find_one({"id": mid}) and not await db.partner_candidates.find_one({"id": stale})
    assert await db.partner_candidates.find_one({"id": partner}) and await db.partner_optouts.find_one({"email": "old@kept.fi"})
    assert (await pn.retention())["status"] == "done_today"


@pytest.mark.asyncio(loop_scope="module")
async def test_request_lane_rates_sources_an_unapproved_lane_and_emits_rate_loaded(monkeypatch):
    """s5 contract: request_lane_rates -> RFQs on that lane -> accepted rate -> request rate_loaded + event."""
    from core import events
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy()                                                   # road:FI>EE is NOT an approved lane
    await _on()
    lane = "road:FI>EE"
    cand = await _candidate()
    await db.partner_candidates.update_one({"id": cand["id"]}, {"$set": {"lanes": [lane], "score": 99}})

    async def cached(l, **kw):                                         # no network in this test
        return {"lane": l, "status": "cached"}
    monkeypatch.setattr(disc, "discover_lane", cached)
    got = []

    async def handler(payload):
        got.append(payload)
    events._handlers["partner.rate_loaded"].append(handler)
    with pytest.raises(HTTPException):
        await pn.request_lane_rates("Helsinki", "EETLL", "road")
    await db.lane_rate_requests.delete_many({"lane": lane})
    req = await pn.request_lane_rates("FIHEL", "EETLL", "road", {"load_type": "FTL"}, max_cost_cents=120000, quote_id="q-1")
    assert req["lane"] == lane and req["max_cost_cents"] == 120000 and req["status"] == "open"
    shared = await pn.request_lane_rates("FI", "EE", "road", quote_id="q-2")
    assert shared["id"] == req["id"] and shared["shared"]
    for _ in range(50):                                               # the immediate background task works it
        if (await pn.get_lane_request(req["id"]))["status"] not in ("open", "working"):
            break
        await asyncio.sleep(0.1)
    assert (await pn.get_lane_request(req["id"]))["status"] == "rfqs_sent"
    assert (await pn._work_request(req["id"]))["status"] == "busy_or_done"   # never worked twice
    row = await db.partner_outreach.find_one({"request_id": req["id"]}, {"_id": 0})
    assert row and row["limit_cents"] == 120000
    r = await pn.ingest_message(_reply(row, f"Hinta 1 300 EUR all-in, 1 päivä, voimassa {VALID} asti."))
    assert r["status"] == "counteroffered" and r["counter_cents"] <= 120000
    row = await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})
    r = await pn.ingest_message(_reply(row, f"OK, hyväksymme {row['last_counter_cents'] // 100} EUR."))
    assert r["status"] == "accepted" and r["all_in_cost_cents"] <= 120000
    done = await pn.get_lane_request(req["id"])
    assert done["status"] == "rate_loaded" and done["rate_card_ids"] == [r["id"]] and set(done["quote_ids"]) == {"q-1", "q-2"}
    await asyncio.sleep(0.2)                                           # event handlers run in the background
    assert got and got[-1]["request"]["id"] == req["id"] and got[-1]["rate_card"]["id"] == r["id"]
    events._handlers["partner.rate_loaded"].remove(handler)


@pytest.mark.asyncio(loop_scope="module")
async def test_s5_rate_requests_queue_is_folded_into_one_lane_effort(monkeypatch):
    """s5 writes open rate_requests; the cycle turns them into one lane request, marks them sourcing when an RFQ is out,
    and the accepted card carries source_detail.rate_request_id plus the rate_card.accepted event."""
    from core import events
    monkeypatch.setenv("PARTNER_INBOUND_REQUIRE_AUTH", "false")
    await _policy()
    await _on()
    lane = "road:FI>LV"
    cand = await _candidate()
    await db.partner_candidates.update_one({"id": cand["id"]}, {"$set": {"lanes": [lane], "score": 99}})

    async def cached(l, **kw):
        return {"lane": l, "status": "cached"}
    monkeypatch.setattr(disc, "discover_lane", cached)
    got = []

    async def handler(payload):
        got.append(payload)
    events._handlers["rate_card.accepted"].append(handler)
    await db.lane_rate_requests.delete_many({"lane": lane})
    a, b = new_id(), new_id()
    await db.rate_requests.insert_many([
        {"id": a, "status": "open", "lane": lane, "max_cost_cents": 110000, "quote_ids": ["gq-1"], "cargo": {"weight_kg": 9000}},
        {"id": b, "status": "open", "lane": lane, "max_cost_cents": 110000, "quote_ids": ["gq-2"]},
        {"id": new_id(), "status": "open", "lane": "nonsense"}])
    imported = await pn.import_rate_requests()
    mine = [x for x in imported if x["rate_request_id"] in (a, b)]
    assert len(mine) == 2 and mine[0]["lane_request_id"] == mine[1]["lane_request_id"]
    req_id = mine[0]["lane_request_id"]
    assert await pn.import_rate_requests() == [] or all(x["rate_request_id"] not in (a, b) for x in await pn.import_rate_requests())
    for _ in range(50):
        if (await pn.get_lane_request(req_id))["status"] not in ("open", "working"):
            break
        await asyncio.sleep(0.1)
    req = await pn.get_lane_request(req_id)
    assert req["status"] == "rfqs_sent" and set(req["rate_request_ids"]) == {a, b} and {"gq-1", "gq-2"} <= set(req["quote_ids"])
    assert (await db.rate_requests.find_one({"id": a}))["status"] == "sourcing"
    row = await db.partner_outreach.find_one({"request_id": req_id}, {"_id": 0})
    assert row["limit_cents"] == 110000
    r = await pn.ingest_message(_reply(row, f"Hinta 1 050 EUR all-in, 2 päivää, voimassa {VALID} asti."))
    assert r["status"] == "accepted"
    card = await db.rate_cards.find_one({"id": r["id"]}, {"_id": 0})
    assert card["source_detail"]["rate_request_id"] == a and set(card["source_detail"]["rate_request_ids"]) == {a, b}
    await asyncio.sleep(0.2)
    assert got and got[-1]["rate_card"]["id"] == r["id"]
    events._handlers["rate_card.accepted"].remove(handler)


@pytest.mark.asyncio(loop_scope="module")
async def test_expected_reply_hours_needs_samples_and_prefers_the_lane():
    lane = "rail:FI>CN"
    await db.partner_outreach.delete_many({"lane": {"$regex": "^rail:"}})
    assert await pn.expected_reply_hours("rail", lane) is None
    base = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"id": new_id(), "campaign_key": new_id(), "lane": lane, "status": "accepted", "updated_at": now,
             "log": [{"kind": "rfq", "at": base.strftime("%Y-%m-%dT%H:%M:%SZ")},
                     {"kind": "rate_accepted", "at": (base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")}]}
            for h in (2, 4, 6, 8, 40)]
    await db.partner_outreach.insert_many(rows)
    got = await pn.expected_reply_hours("rail", lane)
    assert got == {"p50_hours": 6.0, "p90_hours": 40.0, "samples": 5, "scope": "lane"}
    assert (await pn.expected_reply_hours("rail", "rail:FI>DE"))["scope"] == "mode"
    await db.partner_outreach.delete_many({"lane": lane})


@pytest.mark.asyncio(loop_scope="module")
async def test_send_email_attachments_reach_the_smtp_message(monkeypatch):
    """s8/s9 request: send_email(..., attachments=[(name, ctype, bytes)]) attaches files on the smtp path and logs them."""
    from core import email as core_email
    sent = []
    monkeypatch.setattr(core_email, "_smtp_send", lambda msg, smtp: sent.append(msg))
    rec = await core_email.send_email("desk@carrier.example", "SI", "<p>SI</p>", "SI", tag="test", provider="smtp",
                                      smtp={"host": "127.0.0.1", "port": "3025", "security": "none"},
                                      attachments=[("si.json", "application/json", b'{"a":1}'), ("vgm.edi", "text/plain", b"UNB+")])
    assert rec["status"] == "sent" and [a["filename"] for a in rec["attachments"]] == ["si.json", "vgm.edi"]
    parts = {p.get_filename(): p.get_content() for p in sent[0].iter_attachments()}
    assert parts["si.json"] in (b'{"a":1}', '{"a":1}') and "UNB+" in str(parts["vgm.edi"])
    logged = await core_email.send_email("x@example.fi", "S", "<p>s</p>", "s", tag="test", provider="log",
                                         attachments=[("a.pdf", "application/pdf", b"%PDF")])
    assert logged["status"] == "logged" and logged["attachments"][0]["size"] == 4


@pytest.mark.asyncio(loop_scope="module")
async def test_live_outreach_defaults_closed_and_allowlist_is_exact(monkeypatch):
    monkeypatch.delenv("PARTNER_OUTREACH_LIVE")
    monkeypatch.setenv("PARTNER_OUTREACH_ALLOWLIST", "marc+rfq@heliosone.fi")
    await _policy()
    await _on()
    row = await pn.open_rfq(await _candidate(), "road:FI>SE", await get_settings())
    assert row["send"]["status"] == "blocked"
    assert await pn.sends_today() == 0
    assert pn.outreach_allowed("marc+rfq@heliosone.fi")
    assert not pn.outreach_allowed("someone@heliosone.fi")
    assert not pn.outreach_allowed("marc+rfq@heliosone.fi.attacker.com")
    monkeypatch.setenv("PARTNER_OUTREACH_LIVE", "true")
    assert pn.outreach_allowed(row["contact_email"])
    monkeypatch.setenv("PARTNER_NETWORK_KILL", "true")
    assert (await pn.send_partner_mail(row, "rfq"))["status"] == "blocked"


@pytest.mark.asyncio(loop_scope="module")
async def test_daily_and_partner_caps_are_atomic_under_concurrent_sends():
    await _policy(partner_daily_send_limit=100, partner_per_partner_daily_limit=3)
    s = await get_settings()
    results = await asyncio.gather(*(pn._reserve_send("rfq", s, "one-carrier.fi") for _ in range(20)))
    assert sum(results) == 3
    assert await pn.sends_today() == 3
    assert await pn._reserve_send("accept", s, "another-carrier.fi")
    await _policy(partner_daily_send_limit=5, partner_per_partner_daily_limit=20)
    s = await get_settings()
    results = await asyncio.gather(*(pn._reserve_send("rfq", s, f"carrier-{n}.fi") for n in range(20)))
    assert sum(results) == 4  # 80% reserved for RFQs, remaining 20% for negotiation
    assert await pn._reserve_send("accept", s, "one.fi")
    assert not await pn._reserve_send("accept", s, "two.fi")
    await _policy(partner_per_partner_daily_limit=0)
    assert not await pn._reserve_send("rfq", await get_settings(), "one.fi")


@pytest.mark.asyncio(loop_scope="module")
async def test_native_postmark_route_auth_queue_parse_and_duplicate(monkeypatch):
    from fastapi import FastAPI
    from routes.partner_network import router
    monkeypatch.setenv("PARTNER_INBOUND_SECRET", "local-route-test")
    await _policy()
    await _on()
    row = await _rfq(await _candidate())
    body = {"MessageID": new_id(), "FromFull": {"Email": row["contact_email"]},
            "OriginalRecipient": "reply@inbound.postmarkapp.com", "Subject": f"Re: [{row['ref']}] Rate",
            "TextBody": "EUR 1500 all-in per shipment, transit 2 days, valid 30 days", "Headers": []}
    app = FastAPI(); app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/api/partner-network/inbound/postmark", json=body)).status_code == 401
        r = await c.post("/api/partner-network/inbound/postmark", json=body, auth=("postmark", "local-route-test"))
        assert r.status_code == 200, r.text
        ack = r.json()
        duplicate = await c.post("/api/partner-network/inbound/postmark", json=body, auth=("postmark", "local-route-test"))
        assert duplicate.json()["job_id"] == ack["job_id"]
    j = await db.jobs.find_one({"id": ack["job_id"]}, {"_id": 0})
    result = await pn.process_inbound_job(j)
    assert result["status"] == "accepted", result
    inbound = await db.partner_messages.find_one({"direction": "in", "message_id": body["MessageID"]})
    assert inbound["source"] == "postmark" and inbound["provider_id"] == body["MessageID"]
    assert inbound["parsed"]["cost_cents"] == 150000 and inbound["parsed"]["transit_days"] == 2
    assert (await pn.process_inbound_job(j))["status"] == "duplicate"


@pytest.mark.asyncio(loop_scope="module")
async def test_shared_dispatch_never_treats_rfq_sender_mismatch_as_customer(monkeypatch):
    from email.message import EmailMessage
    await _policy(); await _on()
    row = await _rfq(await _candidate())
    msg = EmailMessage()
    msg["Message-ID"] = f"<{new_id()}@attacker.fi>"
    msg["From"] = "attacker@elsewhere.fi"
    msg["To"] = "reply@huolitsija.fi"
    msg["Subject"] = f"Re: [{row['ref']}]"
    msg.set_content("EUR 1850 all-in, transit 32 days, valid 30 days")
    result = await pn.try_ingest_rfq(msg.as_bytes(), provider_id="pm-receipt", authenticated_provider=True)
    assert result and result["job_id"]
    j = await db.jobs.find_one({"id": result["job_id"]}, {"_id": 0})
    assert (await pn.process_inbound_job(j))["status"] == "sender_mismatch"
    msg.replace_header("Subject", "New customer shipment inquiry")
    assert await pn.try_ingest_rfq(msg.as_bytes(), authenticated_provider=True) is None


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_provider_credentials_fails_without_log_success(monkeypatch):
    from core.email import send_email
    monkeypatch.setattr(config, "POSTMARK_SERVER_TOKEN", "")
    sent = await send_email("nobody@example.com", "No delivery", "", "", provider="postmark")
    assert sent["status"] == "failed" and sent["provider"] == "postmark"


@pytest.mark.asyncio(loop_scope="module")
async def test_company_mailbox_card_never_enters_customer_prices_or_sends_confirmation(monkeypatch):
    from services.partner_mailbox import parse_postmark
    await _policy(partner_max_price_cents=500000, partner_sell_price_cents=610000)
    await _on()
    candidate = await _candidate()
    candidate["verification_only"] = True
    row = await pn.open_rfq(candidate, "sea:CN>FI", await get_settings(), {"id": None, "max_cost_cents": 500000})
    result = await pn.ingest_message(parse_postmark({"MessageID": new_id(), "From": candidate["contact_email"],
         "Subject": f"Re: [{row['ref']}]", "TextBody": "EUR 1850 all-in per 40HC, valid 30 days, transit 32 days"}))
    assert result["status"] == "pending" and result["all_in_cost_cents"] == 185000
    assert result["container_costs"] == {"40HC": 185000} and result["transit_days"] == 32
    assert result["archived"] and result["verification_only"]
    assert result["carrier_id"] is None
    assert not await db.carriers.find_one({"id": candidate["id"]})
    assert (await db.partner_candidates.find_one({"id": candidate["id"]}))["status"] == "verification"
    assert await db.partner_messages.count_documents({"outreach_id": row["id"], "direction": "out"}) == 1
    assert (await pn.send_partner_mail(row, "accept", 185000))["status"] == "blocked"


@pytest.mark.asyncio(loop_scope="module")
async def test_on_demand_service_equipment_and_places_survive_to_rate(monkeypatch):
    """Explicit requests must not become 40HC or country-wide rates. Local test records, log-only email."""
    from services import pricing
    await _policy(partner_network_enabled=False)
    a = {"name": "Kuopio", "country": "FI", "lat": 62.89, "lon": 27.68}
    b = {"name": "Lagos", "country": "NG", "lat": 6.45, "lon": 3.39}
    cargo = {"load_type": "FCL", "weight_kg": 12000, "containers": [{"type": "20GP", "count": 1}],
             "equipment_type": "20GP", "origin_location": a, "destination_location": b}
    req = await pn.request_lane_rates('FI', 'NG', 'sea', cargo, service='FCL', max_cost_cents=180000, quote_id='s5-scope-20')
    same = await pn.request_lane_rates('FI', 'NG', 'sea', cargo, service='FCL', max_cost_cents=180000, quote_id='s5-scope-again')
    assert req['service'] == 'FCL' and req['equipment_type'] == '20GP'
    # Off requests are not coalesced with active procurement, and neither sends an email.
    assert req['status'] == same['status'] == 'off'
    different = await pn.request_lane_rates('FI', 'NG', 'sea', {**cargo, 'equipment_type': '40HC'}, service='FCL', max_cost_cents=180000)
    assert req['request_scope'] != different['request_scope']
    cand = await _candidate()
    row = await pn.open_rfq(cand, req['lane'], await get_settings(), req)
    assert row['equipment_type'] == '20GP' and row['service'] == 'FCL'
    _, lines = pn.compose(row, 'rfq', await get_settings())
    body = '\n'.join(lines)
    assert '20GP' in body and 'Kuopio' in body and 'Lagos' in body
    assert '40\' HC' not in body
    offer = {'cost_cents': 100000, 'transit_days': 20, 'valid_to': VALID, 'all_in': True, 'surcharges': [], 'method': 'test'}
    rate = await pn.load_rate(row, offer, 180000, await get_settings(), status='accepted')
    assert rate['container_costs'] == {'20GP': 100000}
    assert rate['origin']['lat'] == a['lat'] and rate['destination']['lon'] == b['lon']
    assert not pricing.is_region(rate['origin'])
    assert pricing.cover(a, rate, 'origin') == ('direct', 0.0)
    assert pricing.cover({'country':'FI','lat':60.17,'lon':24.94}, rate, 'origin')[0] != 'direct'
    m = pricing.cargo_metrics(cargo)
    assert pricing.price_card(rate, m, km=6000, kind='main', leg_seq=1, freight_code='OCEAN_FREIGHT')
    assert pricing.price_card(rate, {**m, 'weight_kg':24000}, km=6000, kind='main', leg_seq=1, freight_code='OCEAN_FREIGHT') == []


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize('mode,service,basis', [('sea','LCL','PER_WM'), ('road','ROAD_LTL','PER_KG'), ('air','COURIER','PER_KG')])
async def test_additive_service_uses_its_actual_pricing_basis(mode, service, basis):
    await _policy(partner_network_enabled=False)
    req = await pn.request_lane_rates('FIHEL','EETLL',mode, {'weight_kg':80,'volume_m3':.4}, service=service)
    row = await pn.open_rfq(await _candidate(),req['lane'],await get_settings(),req)
    assert row['service'] == service and row['basis'] == basis and row['equipment_type'] is None
    _, lines = pn.compose(row,'rfq',await get_settings())
    assert pn.REQUEST_SERVICES[service][3] in '\n'.join(lines)
    # A per-unit response must keep its surcharge basis through ingestion and customer pricing.
    offer = {'base_cents':100,'cost_cents':125,'transit_days':3,'valid_to':VALID,
             'surcharges':[{'code':'FUEL','amount_cents':25}],'method':'test'}
    rate = await pn.load_rate(row,offer,1000,await get_settings(),status='accepted')
    assert rate['surcharges'][0]['basis'] == basis
    from services import pricing
    metrics = pricing.cargo_metrics(req['cargo'])
    lines = pricing.price_card(rate,metrics,km=100,kind='main',leg_seq=1,freight_code='FREIGHT')
    assert round(sum(line['unit_cost_cents'] * line['qty'] for line in lines)) == round(125 * pricing.chargeable(service,metrics)['qty'])


@pytest.mark.asyncio(loop_scope="module")
async def test_single_real_rfq_concurrent_calls_and_reply_never_send_twice(monkeypatch):
    from routes.partner_network import single_rfq
    from services.partner_mailbox import parse_postmark
    await db.partner_outreach.delete_many({"campaign_key": "single-real-rfq-v1"})
    await _policy(partner_network_enabled=False)
    cand = await _candidate()
    req = await pn.request_lane_rates('FIHEL', 'SESTO', 'road', {'weight_kg': 100, 'volume_m3': 1},
                                      service='ROAD_LTL')
    monkeypatch.setenv('PARTNER_OUTREACH_LIVE', 'false')
    monkeypatch.setenv('PARTNER_SINGLE_RFQ_ONLY', 'true')
    monkeypatch.setenv('PARTNER_OUTREACH_ALLOWLIST', cand['contact_email'])
    monkeypatch.setenv('PARTNER_EMAIL_PROVIDER', 'postmark')
    monkeypatch.setenv('PARTNER_REPLY_TO', 'inbound@example.invalid')
    sends = []

    sent_bodies = []

    async def provider(to, subject, *args, **kwargs):
        sends.append({'to': to, 'subject': subject})
        sent_bodies.append([args, kwargs])
        await asyncio.sleep(.02)
        return {'id': new_id(), 'status': 'sent', 'provider': 'postmark', 'provider_id': 'fixture-provider-id'}

    monkeypatch.setattr(pn, 'send_email', provider)
    await _on()
    assert (await pn._work_request(req['id']))['status'] == 'manual'
    body = {'candidate_id': cand['id'], 'request_id': req['id']}
    monkeypatch.setenv('PARTNER_OUTREACH_ALLOWLIST', cand['contact_email'] + ',extra@example.invalid')
    with pytest.raises(HTTPException) as denied:
        await single_rfq(body, BOT)
    assert denied.value.status_code == 409 and not sends
    monkeypatch.setenv('PARTNER_OUTREACH_ALLOWLIST', cand['contact_email'])
    monkeypatch.setenv('PARTNER_OUTREACH_LIVE', 'true')
    with pytest.raises(HTTPException) as denied:
        await single_rfq(body, BOT)
    assert denied.value.status_code == 409 and not sends
    monkeypatch.setenv('PARTNER_OUTREACH_LIVE', 'false')
    assert (await pn.run_cycle(BOT))['status'] == 'manual'
    await db.raw.partner_single_rfq.delete_many({})
    await db.approvals.delete_many({'kind': 'partner_rfq'})
    cards = await asyncio.gather(*(single_rfq(body, BOT) for _ in range(5)))
    assert not sends  # nothing leaves before the owner approves
    assert {c['status'] for c in cards} == {'awaiting_approval'}
    assert await db.approvals.count_documents({'kind': 'partner_rfq'}) == 1
    card = await db.approvals.find_one({'kind': 'partner_rfq'}, {'_id': 0})
    assert card['data']['recipient'] == cand['contact_email'] and 'company in formation' in card['summary']
    from routes.partner_network import send_approved_single_rfq
    results = await asyncio.gather(*(send_approved_single_rfq({**card, 'status': 'approved'}, BOT) for _ in range(5)))
    assert len({r['id'] for r in results}) == 1
    assert 'company in formation' in json.dumps(sent_bodies, default=str)
    row = await db.partner_outreach.find_one({'campaign_key': 'single-real-rfq-v1'}, {'_id': 0})
    assert row['single_send_reserved_at'] and row['single_rfq_only'] and not row['verification_only']
    assert row['request_scope'] == req['request_scope']
    assert len(sends) == 1 and sends[0]['to'] == cand['contact_email']
    assert (await single_rfq(body, BOT))['id'] == row['id']
    assert (await pn.send_partner_mail(row, 'rfq'))['status'] == 'blocked'
    for kind in ('accept', 'decline', 'reminder', 'clarify', 'counteroffer'):
        assert (await pn.send_partner_mail(row, kind, 10000))['status'] == 'blocked'
    # A normal campaign to the same allowlisted address is blocked, too.
    assert (await pn.send_partner_mail({**row, 'single_rfq_only': False}, 'rfq'))['status'] == 'blocked'
    await db.partner_outreach.update_one({'id': row['id']}, {'$set': {'last_outbound_at': '2000-01-01T00:00:00Z'}})
    await pn.follow_ups(await get_settings())
    assert len(sends) == 1
    # Incomplete real replies stay for review without a clarification email.
    bad = await pn.ingest_message(parse_postmark({'MessageID': new_id(), 'From': cand['contact_email'],
                                 'Subject': f"Re: [{row['ref']}]", 'TextBody': 'Please call our office.'}))
    assert bad['status'] == 'needs_review'
    good = await pn.ingest_message(parse_postmark({'MessageID': new_id(), 'From': cand['contact_email'],
        'Subject': f"Re: [{row['ref']}]", 'TextBody': 'EUR 2000 all-in per kg, valid 30 days, transit 3 days'}))
    assert good['status'] == 'pending' and good['all_in_cost_cents'] == 200000
    assert good['carrier_id'] and not good['archived'] and not good['verification_only']
    assert len(sends) == 1
    await db.partner_outreach.delete_one({'id': row['id']})
    await db.raw.partner_single_rfq.delete_many({})


@pytest.mark.asyncio(loop_scope="module")
async def test_single_rfq_timeout_reservation_is_not_retried(monkeypatch):
    await db.partner_outreach.delete_many({'campaign_key': 'single-real-rfq-v1'})
    await _policy()
    await _on()
    cand = await _candidate()
    calls = []

    async def uncertain(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('fixture: provider response lost')

    monkeypatch.setattr(pn, 'send_email', uncertain)
    with pytest.raises(TimeoutError):
        await pn.open_rfq(cand, 'road:FI>SE', await get_settings(), single_rfq_only=True)
    row = await db.partner_outreach.find_one({'campaign_key': 'single-real-rfq-v1'}, {'_id': 0})
    assert row['single_send_reserved_at']
    assert await pn.open_rfq(await _candidate(), 'road:FI>SE', await get_settings(), single_rfq_only=True) is None
    assert (await pn.send_partner_mail(row, 'rfq'))['status'] == 'blocked'
    assert calls == [1]
    await db.partner_outreach.delete_one({'id': row['id']})
