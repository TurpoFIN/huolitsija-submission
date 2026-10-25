"""Procurement slice 2: allocations (MQC/cap, usage from bookings, pricing preference), overseas agents (agreements,
agent rate cards, selection, agent lines in prices) and partner scorecards (measured metrics only, carrier ranking).

Runs against the seeded CI database and the real services; the pricing checks use the seeded CN>FI sea cards.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import pytest_asyncio
from fastapi import HTTPException


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def motor_loop():
    from core.db import client, ensure_indexes
    client._io_loop = asyncio.get_running_loop()
    await ensure_indexes()
    yield
    from core.db import db as _db
    # leave nothing that changes prices for later suites
    await _db.carrier_allocations.update_many({"notes": TAG}, {"$set": {"status": "ended"}})
    await _db.agent_agreements.update_many({"terms.liability": TAG}, {"$set": {"status": "terminated"}})
    await _db.carriers.update_many({"services_note": TAG}, {"$set": {"active": False}})
    # These minimal service fixtures are not complete API shipment records.
    # Remove this run's exact IDs, including demo=True rows, before API suites run.
    await _db.raw.shipments.delete_many({"id": {"$in": SHIPMENT_IDS}})


from core.db import db
from core.util import new_id, now_iso
from services import agent_network as agents, allocations, pricing, scorecards

TAG = "test_core_procurement"
SHIPMENT_IDS = []
OPS = {"id": "test-procurement", "name": "Procurement test operator"}
TODAY = date.today()
READY = (TODAY + timedelta(days=5)).isoformat()
# These assertions exercise sea allocations and a sea-only origin agent. An
# unrestricted FCL search also returns rail offers, which cannot use that agent.
FCL_REQ = {"mode": "sea", "origin": "CNSHA", "destination": "FIHEL", "incoterm": "FOB", "incoterm_place": "Shanghai", "ready_date": READY,
           "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 12000}}


def _iso(d: date) -> str:
    return d.isoformat()


async def _carrier(name="Testivarustamo", mode="sea", country="FI") -> dict:
    row = {"id": new_id(), "name": f"{name} {new_id()[:6]}", "code": None, "modes": [mode], "country": country, "active": True,
           "on_time_pct": 90.0, "services_note": TAG, "created_at": now_iso()}
    await db.carriers.insert_one(dict(row))
    return row


async def _shipment(carrier_id: str, *, mode="sea", o=("CNSHA", "CN"), d=("FIHEL", "FI"), teu=2, demo=False, late=False, delivered=True):
    eta = f"{_iso(TODAY - timedelta(days=3))}T10:00:00Z"
    ata = f"{_iso(TODAY - timedelta(days=1 if late else 3))}T{'18' if late else '09'}:00:00Z" if delivered else None
    s = {"id": new_id(), "reference": f"HUO-T{new_id()[:5]}", "mode": mode, "status": "delivered" if delivered else "booked",
         "origin": {"unlocode": o[0], "country": o[1]}, "destination": {"unlocode": d[0], "country": d[1]},
         "etd": f"{_iso(TODAY)}T08:00:00Z", "teu": teu, "weight_kg": 9000, "cargo": {"teu": teu},
         "legs": [{"seq": 1, "kind": "main", "carrier_id": carrier_id, "eta": eta, "ata": ata}],
         "demo": demo, "created_at": now_iso()}
    await db.shipments.insert_one(dict(s))
    SHIPMENT_IDS.append(s["id"])
    s.pop("_id", None)
    return s


# ------------------------------------------------------------------------------------------------- allocations
def test_lane_and_period_rules():
    assert allocations.parse_lane("sea:cn>fi") == ("sea", "CN", "FI")
    assert allocations.parse_lane("sea:CNSHA>FIHEL") == ("sea", "CNSHA", "FIHEL")
    for bad in ("boat:CN>FI", "sea:CHN>FI", "seaCN>FI", ""):
        with pytest.raises(ValueError):
            allocations.parse_lane(bad)
    assert allocations.period_bounds("month", date(2026, 2, 10))[1:] == (date(2026, 2, 1), date(2026, 2, 28))
    assert allocations.period_bounds("quarter", date(2026, 11, 3))[0] == "2026-Q4"
    assert allocations.period_bounds("week", date(2026, 10, 24))[0] == "2026-W43"
    s = {"teu": 4, "weight_kg": 20000}
    assert allocations.shipment_qty("TEU", s) == 4 and allocations.shipment_qty("FEU", s) == 2
    assert allocations.shipment_qty("shipments", s) == 1 and allocations.shipment_qty("kg", s) == 20000
    assert allocations.shipment_qty("TEU", s, [{"iso_type": "45G1", "count": 2}, {"iso_type": "22G1", "count": 1}]) == 5.5


@pytest.mark.asyncio(loop_scope="module")
async def test_allocation_usage_comes_from_bookings_once_and_cancel_releases():
    car = await _carrier()
    before = await _shipment(car["id"], teu=2)              # booked before the allocation exists -> backfilled
    alloc = await allocations.create({"carrier_id": car["id"], "lane": "sea:CN>FI", "unit": "TEU", "committed_qty": 20,
                                      "max_qty": 24, "period": "month", "valid_from": _iso(TODAY - timedelta(days=40)),
                                      "valid_to": _iso(TODAY + timedelta(days=200)), "shortfall_cents_per_unit": 15000,
                                      "notes": TAG}, OPS)
    assert alloc["status_now"]["used"] == 2 and alloc["usage"][0]["shipment_id"] == before["id"]
    s = await _shipment(car["id"], teu=4)
    from core.events import emit
    await emit("shipment.created", {"shipment": s, "actor": OPS})   # the real booking event
    await emit("shipment.created", {"shipment": s, "actor": OPS})   # twice: counted once
    st = await allocations.status(alloc["id"])
    assert st["used"] == 6 and st["remaining"] == 14 and st["capacity_left"] == 18
    demo = await _shipment(car["id"], teu=10, demo=True)
    await allocations.record_shipment(demo)
    other_lane = await _shipment(car["id"], teu=10, o=("DEHAM", "DE"))
    await allocations.record_shipment(other_lane)
    assert (await allocations.status(alloc["id"]))["used"] == 6     # demo data and other lanes never count
    await emit("shipment.status_changed", {"shipment": s, "old_status": "booked", "new_status": "cancelled", "actor": OPS})
    assert (await allocations.status(alloc["id"]))["used"] == 2
    with pytest.raises(HTTPException):
        await allocations.create({"carrier_id": car["id"], "lane": "sea:CN>FI", "unit": "TEU", "committed_qty": 10, "max_qty": 5,
                                  "period": "month", "valid_from": READY, "valid_to": READY}, OPS)


@pytest.mark.asyncio(loop_scope="module")
async def test_pricing_prefers_carrier_behind_mqc_and_skips_full_one():
    base = await pricing.quote_pricing(dict(FCL_REQ))
    opts = base["options"]
    assert len({o["carrier_id"] for o in opts}) >= 2, "seeded CN>FI sea lane has several carriers"
    best = next(o for o in opts if o["recommended"])
    other = next(o for o in opts if o["carrier_id"] != best["carrier_id"] and o["service"] == best["service"])
    gap = other["total_cents"] - best["total_cents"]
    # the other carrier is far behind a 100 TEU MQC with a dead-freight penalty bigger than the price gap
    a = await allocations.create({"carrier_id": other["carrier_id"], "lane": "sea:CN>FI", "unit": "TEU", "committed_qty": 100,
                                  "period": "quarter", "valid_from": _iso(TODAY - timedelta(days=60)),
                                  "valid_to": _iso(TODAY + timedelta(days=200)),
                                  "shortfall_cents_per_unit": max(gap, 0) // 2 + 100000, "notes": TAG}, OPS)
    q = await pricing.quote_pricing(dict(FCL_REQ))
    pick = next(o for o in q["options"] if o["recommended"])
    assert pick["carrier_id"] == other["carrier_id"] and pick["allocation"]["allocation_id"] == a["id"]
    assert pick["allocation"]["ranking_credit_cents"] > 0 and pick["total_cents"] == other["total_cents"]  # price unchanged
    # the same carrier, now with a hard cap that is used up -> tagged and no longer recommended
    await allocations.update(a["id"], {"status": "ended"}, OPS)
    full = await allocations.create({"carrier_id": other["carrier_id"], "lane": "sea:CN>FI", "unit": "TEU", "committed_qty": 0,
                                     "max_qty": 0, "period": "month", "valid_from": _iso(TODAY - timedelta(days=1)),
                                     "valid_to": _iso(TODAY + timedelta(days=200)), "notes": TAG}, OPS)
    q2 = await pricing.quote_pricing(dict(FCL_REQ))
    capped = [o for o in q2["options"] if o["carrier_id"] == other["carrier_id"]]
    assert capped and all("allocation_full" in o["tags"] and not o["recommended"] for o in capped)
    await allocations.update(full["id"], {"status": "ended"}, OPS)


# ------------------------------------------------------------------------------------------------- agents
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_needs_signed_agreement_and_rate_card_then_prices_include_it():
    agent = await agents.create_agent({"name": f"Shanghai Origin Agent {new_id()[:5]} Co., Ltd.", "country": "CN",
                                       "countries": ["CN"], "roles": ["origin"], "services": ["pickup", "export_customs"],
                                       "modes": ["sea"], "contact_email": "ops@sh-agent.example.cn", "services_note": TAG}, OPS)
    assert agent["org_type"] == "agent"
    ag = await agents.create_agreement(agent["id"], {"valid_from": _iso(TODAY - timedelta(days=1)),
                                                     "valid_to": _iso(TODAY + timedelta(days=365)),
                                                     "terms": {"payment_days": 45, "profit_share_pct": 50, "liability": TAG}}, OPS)
    assert await agents.select_agent("CN", "origin", "sea") is None          # draft agreement, no card
    card = await agents.create_rate_card(agent["id"], {"role": "origin", "country": "CN", "mode": "sea",
                                                       "valid_from": _iso(TODAY - timedelta(days=1)), "valid_to": _iso(TODAY + timedelta(days=180)),
                                                       "charges": [{"code": "EXPCLR", "label": "Export clearance", "label_fi": "Vienti-ilmoitus",
                                                                    "basis": "PER_SHIPMENT", "amount_cents": 6500},
                                                                   {"code": "HANDLING", "label": "Origin handling", "label_fi": "Lähtöpään käsittely",
                                                                    "basis": "PER_CONTAINER", "amount_cents": 9000}]}, OPS)
    assert await agents.select_agent("CN", "origin", "sea") is None          # still a draft
    with pytest.raises(HTTPException):
        await agents.set_agreement_status(ag["id"], "signed", {"signed_by": ""}, OPS)   # who signed is required
    await agents.set_agreement_status(ag["id"], "sent", {}, OPS)
    signed = await agents.set_agreement_status(ag["id"], "signed", {"signed_by": "Li Wei, Managing Director"}, OPS)
    assert signed["live"] and signed["countersigned_by"]["name"] == OPS["name"]
    sel = await agents.select_agent("CN", "origin", "sea")
    assert sel and sel["agent"]["id"] == agent["id"] and sel["rate_card"]["id"] == card["id"]
    assert await agents.select_agent("CN", "destination", "sea") is None and await agents.select_agent("CN", "origin", "air") is None
    fob = await pricing.quote_pricing(dict(FCL_REQ))                     # FOB: the seller pays origin handling
    assert all({"AGENT_EXPCLR", "AGENT_HANDLING"} <= {n["code"] for n in o["not_included"]} for o in fob["options"])
    q = await pricing.quote_pricing({**FCL_REQ, "incoterm": "EXW"})      # EXW: the buyer pays it, through our agent
    lines = [ln for o in q["options"] for ln in o["lines"] if ln["code"].startswith("AGENT_")]
    assert q["options"] and all(any(a["agent_id"] == agent["id"] for a in o.get("agents", [])) for o in q["options"])
    assert {ln["code"] for ln in lines} == {"AGENT_EXPCLR", "AGENT_HANDLING"} and all(ln["stage"] == "origin" for ln in lines)
    assert all(ln["description_fi"] in ("Vienti-ilmoitus", "Lähtöpään käsittely") for ln in lines)
    with pytest.raises(HTTPException):
        await agents.set_agreement_status(ag["id"], "draft", {}, OPS)        # signed -> draft is not allowed
    await agents.set_agreement_status(ag["id"], "terminated", {}, OPS)
    q2 = await pricing.quote_pricing({**FCL_REQ, "incoterm": "EXW"})
    assert not any(ln["code"].startswith("AGENT_") for o in q2["options"] for ln in o["lines"])


@pytest.mark.asyncio(loop_scope="module")
async def test_candidate_promoted_to_agent_keeps_its_public_source():
    cand = {"id": new_id(), "name": f"Baltic Forwarding {new_id()[:5]} SIA", "country": "LV", "website": "https://balticfwd.example.lv/",
            "contact_email": "info@balticfwd.example.lv", "status": "contactable", "lanes": ["road:FI>LV"],
            "sources": [{"source": "wikidata", "url": "https://www.wikidata.org/wiki/Q1"}]}
    await db.partner_candidates.insert_one(dict(cand))
    agent = await agents.agent_from_candidate(cand["id"], {"roles": ["destination"], "modes": ["road"]}, OPS)
    await db.carriers.update_one({"id": agent["id"]}, {"$set": {"services_note": TAG}})
    assert agent["org_type"] == "agent" and agent["agent"]["countries"] == ["LV"] and agent["sources"][0]["source"] == "wikidata"
    assert (await db.partner_candidates.find_one({"id": cand["id"]}))["carrier_id"] == agent["id"]


# ------------------------------------------------------------------------------------------------- scorecards
@pytest.mark.asyncio(loop_scope="module")
async def test_scorecard_uses_measured_data_only_and_feeds_ranking():
    car = await _carrier("Mittausvarustamo", mode="road")
    empty = await scorecards.compute(car["id"])
    assert empty["score"] is None and not any(m["enough"] for m in empty["metrics"])
    for i in range(6):
        await _shipment(car["id"], mode="road", o=("FIHEL", "FI"), d=("SESTO", "SE"), late=(i == 0))
    for _ in range(20):                                                         # demo rows are never measured
        await _shipment(car["id"], mode="road", o=("FIHEL", "FI"), d=("SESTO", "SE"), late=True, demo=True)
    ships = await db.shipments.find({"legs.carrier_id": car["id"], "demo": False}, {"id": 1}).to_list(10)
    await db.claims.insert_one({"id": new_id(), "shipment_id": ships[0]["id"], "type": "damage", "created_at": now_iso()})
    # its negotiated partner card on FI>SE vs the other cards on that lane
    await db.rate_cards.insert_one({"id": new_id(), "carrier_id": car["id"], "mode": "road", "service": "ROAD_FTL", "basis": "PER_SHIPMENT",
                                    "origin": {"country": "FI"}, "destination": {"country": "SE"}, "unit_cost_cents": 150000,
                                    "all_in_cost_cents": 150000, "valid_from": _iso(TODAY - timedelta(days=1)),
                                    "valid_to": _iso(TODAY + timedelta(days=90)), "status": "accepted", "source": "partner_network",
                                    "archived": False, "created_at": now_iso()})
    other = await _carrier("Vertailukuljetus", mode="road")
    await db.rate_cards.insert_one({"id": new_id(), "carrier_id": other["id"], "mode": "road", "service": "ROAD_FTL", "basis": "PER_SHIPMENT",
                                    "origin": {"country": "FI"}, "destination": {"country": "SE"}, "unit_cost_cents": 250000,
                                    "all_in_cost_cents": 250000, "valid_from": _iso(TODAY - timedelta(days=1)),
                                    "valid_to": _iso(TODAY + timedelta(days=90)), "status": "accepted", "source": "manual",
                                    "archived": False, "created_at": now_iso()})
    cand_id = new_id()
    await db.partner_candidates.insert_one({"id": cand_id, "name": car["name"], "carrier_id": car["id"], "status": "partner"})
    for h in (2, 5, 30):
        t0 = f"{_iso(TODAY - timedelta(days=10))}T08:00:00Z"
        t1 = f"{_iso(TODAY - timedelta(days=10))}T{8 + h:02d}:00:00Z" if h < 16 else f"{_iso(TODAY - timedelta(days=9))}T14:00:00Z"
        await db.partner_outreach.insert_one({"id": new_id(), "campaign_key": new_id(), "candidate_id": cand_id, "created_at": now_iso(),
                                              "log": [{"kind": "rfq", "at": t0}, {"kind": "reply", "at": t1}]})
    sc = await scorecards.compute(car["id"])
    m = {x["key"]: x for x in sc["metrics"]}
    assert m["on_time"]["enough"] and m["on_time"]["value"] == 83.3 and m["on_time"]["sample"] == 6
    assert m["claims"]["enough"] and m["claims"]["value"] == round(100 / 6, 1)
    assert m["cost"]["enough"] and m["cost"]["value"] < 100            # cheaper than the lane median
    assert m["response"]["enough"] and m["response"]["value"] == 5.0
    has_invoices = "carrier_invoices" in await db.list_collection_names()
    assert m["invoice_accuracy"]["enough"] is False and (has_invoices or "not present" in m["invoice_accuracy"]["source"])
    assert sc["score"] is not None and sc["grade"] in "ABCD" and "invoice_accuracy" not in sc["metrics_used"]
    ranked = await scorecards.rank_carriers("road", "FI", "SE", service="ROAD_FTL")
    ids = [r["carrier_id"] for r in ranked]
    assert car["id"] in ids and ids.index(car["id"]) < ids.index(other["id"])
    mine = next(r for r in ranked if r["carrier_id"] == car["id"])
    assert mine["score"] == sc["score"] and any(r["code"] == "scorecard" for r in mine["reasons"])
    # a full allocation sends it to the back of the ranking
    full = await allocations.create({"carrier_id": car["id"], "lane": "road:FI>SE", "unit": "loads", "committed_qty": 0, "max_qty": 0,
                                     "period": "month", "valid_from": _iso(TODAY - timedelta(days=1)),
                                     "valid_to": _iso(TODAY + timedelta(days=30)), "notes": TAG}, OPS)
    ranked2 = await scorecards.rank_carriers("road", "FI", "SE", service="ROAD_FTL")
    assert ranked2[-1]["carrier_id"] == car["id"] and any(r["code"] == "allocation_full" for r in ranked2[-1]["reasons"])
    await allocations.update(full["id"], {"status": "ended"}, OPS)
    await db.rate_cards.update_many({"carrier_id": {"$in": [car["id"], other["id"]]}}, {"$set": {"archived": True}})


@pytest.mark.asyncio(loop_scope="module")
async def test_selection_inputs_for_s2_carrier_booking():
    """s2's carrier_booking.rank_carriers adds these: scorecard +-0.06, allocation behind +0.05, cap reached -0.25."""
    good, capped, plain = await _carrier("Hyväkuljetus", mode="road"), await _carrier("Täysikuljetus", mode="road"), await _carrier("Uusikuljetus", mode="road")
    for _ in range(6):
        await _shipment(good["id"], mode="road")
    assert (await scorecards.compute(good["id"]))["score"] == 100.0
    await allocations.create({"carrier_id": good["id"], "lane": "road:FI>SE", "unit": "loads", "committed_qty": 50, "period": "month",
                              "valid_from": _iso(TODAY - timedelta(days=20)), "valid_to": _iso(TODAY + timedelta(days=60)),
                              "shortfall_cents_per_unit": 20000, "notes": TAG}, OPS)
    await allocations.create({"carrier_id": capped["id"], "lane": "road:FI>SE", "unit": "loads", "committed_qty": 0, "max_qty": 0,
                              "period": "month", "valid_from": _iso(TODAY - timedelta(days=1)), "valid_to": _iso(TODAY + timedelta(days=60)),
                              "notes": TAG}, OPS)
    sel = await scorecards.selection_inputs([good["id"], capped["id"], plain["id"]], "road", {"country": "FI", "unlocode": "FIHEL"},
                                            {"country": "SE", "unlocode": "SESTO"}, TODAY, unit_qty={"loads": 1})
    assert sel[good["id"]]["adjust"] == 0.11 and {r["code"] for r in sel[good["id"]]["reasons"]} == {"scorecard", "allocation_behind"}
    assert sel[capped["id"]]["adjust"] == -0.25 and sel[capped["id"]]["allocation"]["full"]
    assert sel[plain["id"]] == {"scorecard": {"score": None, "grade": None}, "allocation": None, "adjust": 0.0, "reasons": []}
    await db.carrier_allocations.update_many({"carrier_id": {"$in": [good["id"], capped["id"]]}}, {"$set": {"status": "ended"}})
