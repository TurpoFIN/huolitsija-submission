"""s7 pricing: margin rules and customer contract tariffs, through the real local API (POST /api/quotes) and MongoDB.

Nothing is mocked: every price comes from the seeded rate cards through services.pricing, the rules and tariffs are
created through their HTTP API, and expectations are computed from the cost lines the engine returned. Margin rules are
global, so every rule a test makes is deleted again (fixture `rules`); companies are test=True fixtures (KYC is not
what is under test here).

Env: V7_API (http://localhost:8130/api), V7_DB, MONGO_URL, DEMO_OPS_PASSWORD.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, timedelta
from functools import lru_cache

import httpx
import pytest
from pymongo import MongoClient

API = os.environ.get("V7_API", "http://localhost:8130/api")
DB_NAME = os.environ.get("V7_DB", "huo_s7_pricing_1")
os.environ.setdefault("DB_NAME", DB_NAME)
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

TODAY = date.today()
LOOP = asyncio.new_event_loop()  # one loop for the in-process calls (Motor binds to the first loop it runs on)
asyncio.set_event_loop(LOOP)


def run(coro):
    return LOOP.run_until_complete(coro)

FCL2 = {"origin": "CNSHA", "destination": "FIHEL", "mode": "sea",
        "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 2}], "weight_kg": 18000}}
FCL1 = {"origin": "CNSHA", "destination": "FIHEL", "mode": "sea",
        "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 9000}}
ROAD = {"origin": "DEBER", "destination": "FILHI",
        "cargo": {"load_type": "loose", "packages": 3, "package_type": "pallet_eur", "weight_kg": 900, "volume_m3": 3.0}}


@lru_cache
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


@lru_cache
def ops() -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": "ops.demo@huolitsija.fi", "password": os.environ["DEMO_OPS_PASSWORD"]}, timeout=20)
    assert r.status_code == 200, r.text
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {r.json()['token']}", "X-Include-Test-Tenants": "true"},
                        timeout=120)


CREATED = {"companies": [], "tariffs": []}


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"backend not reachable at {API}: {exc}")
    assert mongo().margin_rules.count_documents({}) == 0, "tests need a DB without margin rules (they are global)"
    yield
    db = mongo()
    db.margin_rules.delete_many({"note": {"$regex": "^s7-test"}})
    db.margin_rules.delete_many({"default_key": {"$exists": True}})
    db.customer_tariffs.delete_many({"id": {"$in": CREATED["tariffs"]}})
    qids = [q["id"] for q in db.quotes.find({"company_id": {"$in": CREATED["companies"]}}, {"id": 1})]
    db.approvals.delete_many({"ref_id": {"$in": qids}})
    db.quotes.delete_many({"id": {"$in": qids}})
    db.users.delete_many({"company_id": {"$in": CREATED["companies"]}, "test": True})
    db.companies.delete_many({"id": {"$in": CREATED["companies"]}, "test": True})


def company(name: str | None = None) -> dict:
    c = {"id": str(uuid.uuid4()), "name": name or f"S7 Hinnoittelu Oy {uuid.uuid4().hex[:5]}", "business_id": "3990009-9",
         "country": "FI", "kyc_status": "approved", "sanctions_status": "clear", "status": "active", "vat_registered": True,
         "vat_id": "FI39900099", "payment_terms_days": 14, "test": True}
    mongo().companies.insert_one(dict(c))
    CREATED["companies"].append(c["id"])
    return c


@pytest.fixture
def rules():
    made = []

    def make(**body):
        body.setdefault("name", "s7 test rule")
        body["note"] = "s7-test " + (body.get("note") or "")
        r = ops().post("/margin-rules", json=body)
        assert r.status_code == 200, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for rid in made:
        ops().delete(f"/margin-rules/{rid}")


def quote(body: dict, comp: dict) -> dict:
    r = ops().post("/quotes", json={**body, "company_id": comp["id"], "send": False})
    assert r.status_code == 200, r.text
    q = r.json()
    q = q.get("quote", q)
    assert q["options"], q.get("warnings")
    return q


def own_lines(o):
    return [ln for ln in o["lines"] if not ln.get("is_pass_through")]


def assert_markup(o: dict, pct: float):
    """Every cost-priced line is cost x (1 + pct/100) (the engine's own rounding), and the totals add up."""
    assert o["markup_pct"] == pytest.approx(pct), (o["carrier_name"], o["markup_pct"])
    priced = [ln for ln in own_lines(o) if ln["unit_cost_cents"] > 0]
    assert priced
    for ln in priced:
        assert ln["unit_price_cents"] == round(ln["unit_cost_cents"] * (1 + pct / 100)), ln
    assert o["total_cents"] == sum(ln["amount_cents"] for ln in o["lines"])
    assert o["margin_cents"] == sum(ln["amount_cents"] - ln["cost_cents"] for ln in own_lines(o))


def by_carrier(q):
    return {(o["carrier_name"], o["service"]): o for o in q["options"] if o.get("price_basis") != "contract"}


# ------------------------------------------------------------------------------------------------
# Margin rules
# ------------------------------------------------------------------------------------------------
def test_no_rule_falls_back_to_default_margin():
    c = company()
    q = quote(FCL1, c)
    default = ops().get("/margin-rules").json()["fallback"]["default_margin_pct"]
    for o in q["options"]:
        assert o["margin_source"] == "default" and o["margin_rule_id"] is None and o["margin_floor_cents"] == 0
        assert_markup(o, default)


def test_mode_rule_changes_the_quote_price(rules):
    c = company()
    before = by_carrier(quote(FCL1, c))
    road_before = quote(ROAD, c)
    r = rules(name="Sea 27 %", scope={"mode": "sea"}, margin_pct=27.0)
    after = by_carrier(quote(FCL1, c))
    assert after.keys() == before.keys()
    for k, o in after.items():
        assert o["margin_rule_id"] == r["id"] and o["margin_source"] == "rule"
        assert_markup(o, 27.0)
        assert o["cost_cents"] == before[k]["cost_cents"]  # same buy price ...
        assert o["total_cents"] > before[k]["total_cents"]  # ... sold higher
    road_after = quote(ROAD, c)  # a road quote is not touched by a sea rule
    for o in road_after["options"]:
        if o["mode"] == "road":
            assert o["margin_rule_id"] is None
    assert sorted(o["total_cents"] for o in road_after["options"] if o["mode"] == "road") == \
        sorted(o["total_cents"] for o in road_before["options"] if o["mode"] == "road")


def test_lane_rule_country_and_locode(rules):
    c = company()
    r = rules(name="CN>FI 31 %", scope={"lane": "CN>FI"}, margin_pct=31.0)
    for o in quote(FCL1, c)["options"]:
        assert o["margin_rule_id"] == r["id"]
        assert_markup(o, 31.0)
    for o in quote(ROAD, c)["options"]:  # DE>FI: no match
        assert o["margin_rule_id"] is None
    ops().delete(f"/margin-rules/{r['id']}")
    r2 = rules(name="CNSHA>FIHEL 29 %", scope={"lane": "cnsha>fihel"}, margin_pct=29.0)
    assert r2["scope"]["lane"] == "CNSHA>FIHEL"
    for o in quote(FCL1, c)["options"]:
        assert o["margin_rule_id"] == r2["id"]
        assert_markup(o, 29.0)


def test_customer_rule_only_for_that_customer(rules):
    a, b = company(), company()
    r = rules(name="Customer A 9 %", scope={"company_id": a["id"]}, margin_pct=9.0)
    for o in quote(FCL1, a)["options"]:
        assert o["margin_rule_id"] == r["id"]
        assert_markup(o, 9.0)
    for o in quote(FCL1, b)["options"]:
        assert o["margin_rule_id"] is None


def test_size_band_rule_by_teu(rules):
    c = company()
    r = rules(name="3+ TEU 11 %", scope={"mode": "sea", "size": {"metric": "teu", "min": 3}}, margin_pct=11.0)
    for o in quote(FCL2, c)["options"]:  # 2 x 40HC = 4 TEU
        assert o["margin_rule_id"] == r["id"]
        assert_markup(o, 11.0)
    for o in quote(FCL1, c)["options"]:  # 1 x 40HC = 2 TEU
        assert o["margin_rule_id"] is None


def test_precedence_company_beats_lane_beats_mode_then_priority(rules):
    c = company()
    mode = rules(name="mode", scope={"mode": "sea"}, margin_pct=20.0)
    lane = rules(name="lane", scope={"lane": "CN>FI"}, margin_pct=22.0)
    comp = rules(name="company", scope={"company_id": c["id"]}, margin_pct=14.0)
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] == comp["id"]
    assert_markup(o, 14.0)
    ops().delete(f"/margin-rules/{comp['id']}")
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] == lane["id"]
    assert_markup(o, 22.0)
    ops().delete(f"/margin-rules/{lane['id']}")
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] == mode["id"]
    assert_markup(o, 20.0)
    # more matched dimensions beat a single company dimension; equal specificity -> higher priority
    hi = rules(name="mode hi", scope={"mode": "sea"}, margin_pct=24.0, priority=5)
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] == hi["id"]
    two = rules(name="mode+service", scope={"mode": "sea", "service": "FCL"}, margin_pct=16.0)
    rules(name="company again", scope={"company_id": c["id"]}, margin_pct=13.0)
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] == two["id"]
    assert_markup(o, 16.0)
    # an inactive or out-of-validity rule never applies
    ops().patch(f"/margin-rules/{two['id']}", json={"active": False})
    future = rules(name="future", scope={"mode": "sea", "service": "FCL", "lane": "CN>FI"}, margin_pct=40.0,
                   valid_from=(TODAY + timedelta(days=3)).isoformat())
    o = quote(FCL1, c)["options"][0]
    assert o["margin_rule_id"] not in (two["id"], future["id"])


def test_floor_raises_a_thin_quote(rules):
    c = company()
    r = rules(name="thin + floor", scope={"company_id": c["id"]}, margin_pct=1.0, floor_cents=50_000)
    for o in quote(FCL1, c)["options"]:
        assert o["margin_rule_id"] == r["id"] and o["margin_floor_cents"] == 50_000
        assert o["markup_pct"] == pytest.approx(1.0)
        thin = sum(round(ln["unit_cost_cents"] * 1.01 * ln["qty"]) - ln["cost_cents"] for ln in own_lines(o)
                   if ln["unit_cost_cents"] > 0)
        assert thin < 50_000  # 1 % alone would not reach the floor ...
        assert 50_000 <= o["margin_cents"] <= 50_000 + 10  # ... so the lines were raised to it (cent rounding)
        assert sum(ln.get("floor_raised_cents") or 0 for ln in o["lines"]) > 0
        assert o["total_cents"] == sum(ln["amount_cents"] for ln in o["lines"])
        assert o["margin_cents"] == sum(ln["amount_cents"] - ln["cost_cents"] for ln in own_lines(o))


def test_floor_per_container_and_cap(rules):
    c = company()
    rules(name="per container", scope={"company_id": c["id"]}, margin_pct=0.5, floor_cents=30_000, floor_basis="PER_CONTAINER")
    for o in quote(FCL2, c)["options"]:
        assert o["margin_floor_cents"] == 60_000  # 2 containers
        assert o["margin_cents"] >= 60_000
    mongo().margin_rules.delete_many({"scope.company_id": c["id"]})
    rules(name="capped", scope={"company_id": c["id"]}, margin_pct=0.5, floor_cents=900_000, cap_pct=6.0)
    for o in quote(FCL1, c)["options"]:
        own_cost = sum(ln["cost_cents"] for ln in own_lines(o))
        assert o["margin_floor_cents"] == int(own_cost * 0.06) < 900_000
        assert o["margin_floor_cents"] <= o["margin_cents"] <= o["margin_floor_cents"] + 10


def test_customer_view_hides_rule_fields(rules):
    from core.security import hash_password

    c = company()
    rules(name="floor", scope={"company_id": c["id"]}, margin_pct=2.0, floor_cents=40_000)
    q = quote(FCL1, c)
    mongo().quotes.update_one({"id": q["id"]}, {"$set": {"published": True}})
    pw = "S7-test-" + uuid.uuid4().hex[:10]
    u = {"id": str(uuid.uuid4()), "email": f"s7test-{uuid.uuid4().hex[:8]}@example.fi", "name": "S7 Test", "role": "customer_admin",
         "org_type": "customer", "company_id": c["id"], "password_hash": hash_password(pw), "locale": "fi",
         "email_verified": True, "session_version": 0, "notify_email": False, "test": True}
    mongo().users.insert_one(dict(u))
    tok = httpx.post(f"{API}/auth/login", json={"email": u["email"], "password": pw}).json()["token"]
    r = httpx.get(f"{API}/quotes/{q['id']}", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    view = r.json().get("quote", r.json())
    assert view["options"]
    for o in view["options"]:
        assert not {"margin_floor_cents", "margin_rule_id", "margin_source", "margin_cents", "cost_cents"} & o.keys()
        assert all("floor_raised_cents" not in ln and "cost_cents" not in ln for ln in o["lines"])
    assert ops().get("/margin-rules", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    assert ops().get("/tariffs", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


@pytest.mark.parametrize("body,needle", [
    ({"margin_pct": 100}, "Margin"), ({"margin_pct": -1}, "Margin"), ({"margin_pct": 10, "floor_cents": -5}, "Floor"),
    ({"margin_pct": 10, "scope": {"lane": "FINLAND>SE"}}, "Lane"), ({"margin_pct": 10, "scope": {"mode": "ship"}}, "Mode"),
    ({"margin_pct": 10, "scope": {"company_id": "nope"}}, "company"),
    ({"margin_pct": 10, "scope": {"size": {"metric": "teu", "min": 4, "max": 2}}}, "max"),
    ({"margin_pct": 10, "scope": {"size": {"metric": "pallets", "min": 1}}}, "metric"),
    ({"margin_pct": 10, "cap_pct": 5}, "Cap"), ({"margin_pct": 10, "floor_basis": "PER_BANANA"}, "basis"),
    ({"margin_pct": 10, "valid_from": "2026-12-01", "valid_to": "2026-11-01"}, "Valid"),
])
def test_rule_validation(body, needle):
    r = ops().post("/margin-rules", json={"name": "bad", "note": "s7-test bad", **body})
    assert r.status_code == 400 and needle.lower() in r.json()["detail"].lower(), r.text


def test_simulate_shows_rule_and_margin_per_option(rules):
    c = company()
    r = rules(name="sim", scope={"lane": "CN>FI"}, margin_pct=1.0, floor_cents=45_000)
    q = quote(FCL1, c)
    sim = ops().post("/margin-rules/simulate", json={"quote_id": q["id"]}).json()
    assert sim["quote_id"] == q["id"] and sim["company_id"] == c["id"]
    stored = {o["id"]: o for o in q["options"]}
    assert len(sim["options"]) == len(q["options"])
    for row in sim["options"]:
        assert row["rule_id"] == r["id"] and row["rule_name"] == "sim" and row["floor_cents"] == 45_000
        assert row["margin_cents"] >= 45_000 and row["floor_raised_cents"] > 0
        assert row["stored"]["total_cents"] == stored[row["option_id"]]["total_cents"]
    assert sim["floor_ok"] is True
    sim2 = ops().post("/margin-rules/simulate", json={"request": ROAD, "company_id": c["id"]}).json()
    assert all(row["rule_id"] is None and row["source"] == "default" for row in sim2["options"])


def test_margin_floor_ok_blocks_a_below_floor_quote(rules):
    c = company()
    rules(name="floor 600", scope={"company_id": c["id"]}, margin_pct=18.0, floor_cents=60_000)
    q = quote(FCL1, c)
    chk = ops().get("/margin-rules/floor-check", params={"quote_id": q["id"]}).json()
    assert chk["ok"] is True, chk
    target = q["options"][0]
    r = ops().patch(f"/quotes/{q['id']}", json={"options": [{"id": target["id"], "margin_pct": 1.0}]})  # ops cuts the margin
    assert r.status_code == 200, r.text
    chk = ops().get("/margin-rules/floor-check", params={"quote_id": q["id"]}).json()
    assert chk["ok"] is False
    assert [x["code"] for x in chk["reasons"]] == ["margin_below_floor"]
    assert chk["reasons"][0]["fi"] and chk["reasons"][0]["en"]
    # the function s2's quote.autosend calls, on the stored document
    from services.margin_rules import margin_floor_ok

    ok, reasons = run(margin_floor_ok(mongo().quotes.find_one({"id": q["id"]}, {"_id": 0})))
    assert ok is False and reasons[0]["code"] == "margin_below_floor"


def test_defaults_endpoint_is_explicit_and_idempotent():
    assert mongo().margin_rules.count_documents({"default_key": {"$exists": True}}) == 0  # nothing seeded by itself
    r = ops().post("/margin-rules/defaults", json={"active": False}).json()
    try:
        keys = {d["default_key"] for d in r["created"]}
        assert {"courier", "sea_fcl", "air"} <= keys and not r["kept"]
        assert all(d["active"] is False for d in r["created"])
        fcl = next(d for d in r["created"] if d["default_key"] == "sea_fcl")
        assert fcl["floor_basis"] == "PER_CONTAINER" and fcl["scope"] == {"mode": "sea", "service": "FCL"}
        again = ops().post("/margin-rules/defaults", json={}).json()
        assert again["created"] == [] and set(again["kept"]) == keys
    finally:
        mongo().margin_rules.delete_many({"default_key": {"$exists": True}})


# ------------------------------------------------------------------------------------------------
# Contract tariffs
# ------------------------------------------------------------------------------------------------
def tariff(comp: dict, lanes: list[dict], *, valid_from=None, valid_to=None, activate=True, force_active=False, **extra) -> dict:
    body = {"company_id": comp["id"], "name": "Vuosisopimus 2026", "lanes": lanes,
            "valid_from": (valid_from or TODAY - timedelta(days=1)).isoformat(),
            "valid_to": (valid_to or TODAY + timedelta(days=60)).isoformat(), **extra}
    r = ops().post("/tariffs", json=body)
    assert r.status_code == 200, r.text
    t = r.json()
    CREATED["tariffs"].append(t["id"])
    assert t["status"] == "draft" and t["number"].startswith("CT-")
    if force_active:  # an active tariff whose validity has since ended (activation itself refuses an ended one)
        assert ops().post(f"/tariffs/{t['id']}/activate").status_code == 400
        mongo().customer_tariffs.update_one({"id": t["id"]}, {"$set": {"status": "active"}})
        return {**t, "status": "active"}
    if activate:
        r = ops().post(f"/tariffs/{t['id']}/activate")
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["status"] == "active"
    return t


SEA_LANE = {"origin": {"country": "CN", "locode": "CNSHA"}, "destination": {"country": "FI"}, "mode": "sea", "service": "FCL",
            "equipment_type": "40HC", "basis": "PER_CONTAINER", "price_cents": 400_000, "surcharges_included": True}  # above the carriers' ~3 460 EUR buy: a real margin


def contract_and_spot(q):
    contract = [o for o in q["options"] if o["price_basis"] == "contract"]
    spot = [o for o in q["options"] if o["price_basis"] == "spot"]
    return contract, spot


def test_contract_option_recommended_at_contract_price_with_real_margin():
    c = company()
    t = tariff(c, [SEA_LANE])
    q = quote(FCL2, c)
    contract, spot = contract_and_spot(q)
    assert len(contract) == 1 and spot, "spot options stay visible for comparison"
    k = contract[0]
    assert k["margin_cents"] > 0 and k["recommended"] is True and not any(o["recommended"] for o in spot)
    assert k["below_floor"] is False and not any(w["code"] == "contract.below_floor" for w in k["warnings"])
    assert k["contract_tariff_id"] == t["id"] and k["contract_tariff_number"] == t["number"]
    cl = [ln for ln in k["lines"] if ln["code"] == "CONTRACT_FREIGHT"]
    assert len(cl) == 1 and cl[0]["qty"] == 2 and cl[0]["unit_price_cents"] == 400_000 and cl[0]["amount_cents"] == 800_000
    # surcharges included: no spot transport line is left, fees stay
    assert not [ln for ln in k["lines"] if ln["stage"] in ("origin", "main", "destination") and ln["code"] != "CONTRACT_FREIGHT"]
    base = next(o for o in spot if o["id"] == k["spot_option_id"])
    assert base["cost_cents"] == min(o["cost_cents"] for o in spot if o["service"] == "FCL")  # cheapest buy
    # cost = the spot option's real cost lines (moved onto the contract line), so margin is real
    replaced = [ln for ln in base["lines"] if ln["stage"] in ("origin", "main", "destination")]
    assert cl[0]["cost_cents"] == sum(ln["cost_cents"] for ln in replaced) > 0
    assert k["cost_cents"] == base["cost_cents"] and k["cost_basis"] == "rate_card"
    assert k["total_cents"] == sum(ln["amount_cents"] for ln in k["lines"])
    assert k["margin_cents"] == sum(ln["amount_cents"] - ln["cost_cents"] for ln in k["lines"] if not ln["is_pass_through"])
    assert k["margin_cents"] == k["total_cents"] - k["pass_through_cents"] - (k["cost_cents"] - k["pass_through_cents"])
    assert k["valid_until"] <= t["valid_to"]
    assert any(w["code"] == "contract.applied" for w in q["warnings"])


def test_contract_without_surcharges_keeps_spot_surcharges():
    c = company()
    tariff(c, [{**SEA_LANE, "surcharges_included": False, "price_cents": 120_000}])
    q = quote(FCL2, c)
    contract, spot = contract_and_spot(q)
    k = contract[0]
    base = next(o for o in spot if o["id"] == k["spot_option_id"])
    base_surch = sorted((ln["code"], ln["amount_cents"]) for ln in base["lines"]
                        if ln["stage"] in ("origin", "main", "destination") and ln["code"] not in ("OCEAN_FREIGHT", "PICKUP", "DELIVERY"))
    k_surch = sorted((ln["code"], ln["amount_cents"]) for ln in k["lines"]
                     if ln["stage"] in ("origin", "main", "destination") and ln["code"] != "CONTRACT_FREIGHT")
    assert base_surch and k_surch == base_surch  # BAF / THC / ... from the spot option of the same mode
    assert "OCEAN_FREIGHT" in k["replaced_line_codes"] and k["cost_cents"] == base["cost_cents"]


def test_contract_respects_validity_status_and_company():
    c, other = company(), company()
    tariff(c, [SEA_LANE], valid_from=TODAY + timedelta(days=20), valid_to=TODAY + timedelta(days=80))  # not yet valid
    tariff(c, [SEA_LANE], valid_from=TODAY - timedelta(days=60), valid_to=TODAY - timedelta(days=1), force_active=True)  # ended
    tariff(c, [SEA_LANE], activate=False)  # draft
    assert not contract_and_spot(quote(FCL2, c))[0]
    # shipments ready inside the future validity get it
    ready = (TODAY + timedelta(days=25)).isoformat()
    contract, _ = contract_and_spot(quote({**FCL2, "ready_date": ready}, c))
    assert len(contract) == 1
    tariff(other, [SEA_LANE])
    assert not contract_and_spot(quote(FCL2, c))[0]  # another customer's contract never applies
    assert contract_and_spot(quote(FCL2, other))[0]
    # equipment must match: a 20GP shipment does not get the 40HC lane
    q20 = quote({**FCL2, "cargo": {"load_type": "FCL", "containers": [{"type": "20GP", "count": 1}], "weight_kg": 8000}}, other)
    assert not contract_and_spot(q20)[0]


def test_expiry_sweep_and_manual_expire():
    c = company()
    t_old = tariff(c, [SEA_LANE], valid_from=TODAY - timedelta(days=60), valid_to=TODAY - timedelta(days=1), force_active=True)
    t_now = tariff(c, [SEA_LANE])
    r = ops().post("/tariffs/expire-sweep").json()
    assert r["expired"] >= 1
    assert ops().get(f"/tariffs/{t_old['id']}").json()["status"] == "expired"
    assert ops().get(f"/tariffs/{t_now['id']}").json()["status"] == "active"
    assert contract_and_spot(quote(FCL2, c))[0]
    assert ops().post(f"/tariffs/{t_now['id']}/expire").json()["status"] == "expired"
    assert not contract_and_spot(quote(FCL2, c))[0]
    assert ops().post(f"/tariffs/{t_now['id']}/activate").status_code == 400


def test_most_specific_then_newest_tariff_wins_and_supersede():
    c = company()
    general = tariff(c, [{**SEA_LANE, "origin": {"country": "CN"}, "service": None, "equipment_type": None, "price_cents": 170_000}])
    specific = tariff(c, [{**SEA_LANE, "price_cents": 160_000}], valid_from=TODAY - timedelta(days=30))
    k = contract_and_spot(quote(FCL2, c))[0][0]
    assert k["contract_tariff_id"] == specific["id"]  # LOCODE + service + equipment beat country-only, though older
    newer = tariff(c, [{**SEA_LANE, "price_cents": 155_000}], valid_from=TODAY, supersedes_id=None)
    k = contract_and_spot(quote(FCL2, c))[0][0]
    assert k["contract_tariff_id"] == newer["id"]
    repl = ops().post("/tariffs", json={"company_id": c["id"], "lanes": [{**SEA_LANE, "price_cents": 150_500}],
                                         "valid_from": TODAY.isoformat(), "supersedes_id": newer["id"]}).json()
    CREATED["tariffs"].append(repl["id"])
    ops().post(f"/tariffs/{repl['id']}/activate")
    assert ops().get(f"/tariffs/{newer['id']}").json()["status"] == "superseded"
    assert general["id"]


def test_exclusive_tariff_hides_spot_of_that_mode():
    c = company()
    tariff(c, [SEA_LANE], exclusive=True)
    q = quote(FCL2, c)
    assert all(o["price_basis"] == "contract" for o in q["options"] if o["mode"] == "sea")


def test_contract_without_spot_rate_has_unknown_cost_and_is_blocked():
    c = company()
    rail = {"origin": {"country": "CN"}, "destination": {"country": "FI"}, "mode": "rail", "equipment_type": "40HC",
            "basis": "PER_CONTAINER", "price_cents": 210_000, "transit_days": 18, "surcharges_included": True}
    tariff(c, [rail])
    body = {k: v for k, v in FCL2.items() if k != "mode"}
    q = quote(body, c)
    k = next(o for o in q["options"] if o["price_basis"] == "contract")
    assert k["mode"] == "rail" and k["cost_basis"] == "unknown" and k["cost_cents"] is None and k["margin_cents"] is None
    assert k['bookable'] is False and k['firmness'] == 'partial'
    assert k["total_cents"] >= 420_000 and k["door_to_door_days"] >= 18
    chk = ops().get("/margin-rules/floor-check", params={"quote_id": q["id"]}).json()
    assert chk["ok"] is False and "cost_unknown" in [x["code"] for x in chk["reasons"]]
    from core.autonomy import quote_within_policy  # the existing Autopilot policy never approves it either

    assert quote_within_policy(q, {"autopilot_enabled": True, "autopilot_quote_max_cents": 10**9, "autopilot_min_margin_pct": 0}) is False


def test_tariff_validation_and_active_edit_rules():
    c = company()
    bad = [({"lanes": [{**SEA_LANE, "basis": "PER_BOX"}]}, "basis"), ({"lanes": [{**SEA_LANE, "mode": "ship", "service": None}]}, "mode"),
           ({"lanes": [{**SEA_LANE, "price_cents": 0}]}, "price"), ({"lanes": [{**SEA_LANE, "equipment_type": "53FT"}]}, "equipment"),
           ({"lanes": [{**SEA_LANE, "origin": {"country": "FI", "locode": "SEGOT"}}]}, "not in"),
           ({"company_id": "nope"}, "company"), ({"valid_to": "2020-01-01"}, "valid to")]
    for patch, needle in bad:
        body = {"company_id": c["id"], "lanes": [SEA_LANE], "valid_from": TODAY.isoformat(), **patch}
        r = ops().post("/tariffs", json=body)
        assert r.status_code == 400 and needle in r.json()["detail"].lower(), (patch, r.text)
    t = tariff(c, [SEA_LANE])
    r = ops().patch(f"/tariffs/{t['id']}", json={"lanes": [{**SEA_LANE, "price_cents": 1}]})
    assert r.status_code == 400  # active prices are not edited in place
    r = ops().patch(f"/tariffs/{t['id']}", json={"name": "Uusi nimi"})
    assert r.status_code == 200 and r.json()["name"] == "Uusi nimi" and r.json()["lanes"][0]["price_cents"] == 400_000


def test_create_from_tender_tolerates_tender_shapes():
    from services.contract_tariffs import create_from_tender

    c = company()
    tender = {"id": "s7test-" + uuid.uuid4().hex[:8], "number": "TND-TEST", "company_id": c["id"],
              "valid_until": (TODAY + timedelta(days=365)).isoformat()}
    lanes = [{"id": "L1", "origin": "CNSHA", "destination": "FI", "service": "FCL", "equipment": "40HC",
              "unit_price_eur": "1 480,00", "price_basis": "per container"},
             {"id": "L2", "origin": {"country": "DE", "unlocode": "DEBER"}, "destination": "FI", "mode": "road",
              "service": "ROAD_LTL", "unit_price_cents": 21, "basis": "PER_KG", "min_cents": 9000},
             {"id": "L3", "origin": "Xyzzyqwv Qqq", "destination": "FI", "mode": "sea", "price_cents": 1}]
    t = run(create_from_tender(tender, lanes, None))
    CREATED["tariffs"].append(t["id"])
    assert t["status"] == "active" and t["source"] == "tender" and t["tender_id"] == tender["id"]
    assert [ln["tender_lane_id"] for ln in t["lanes"]] == ["L1", "L2"]
    assert t["lanes"][0]["price_cents"] == 148_000 and t["lanes"][0]["basis"] == "PER_CONTAINER" and t["lanes"][0]["mode"] == "sea"
    assert t["import_errors"][0]["lane"] == "L3"
    again = run(create_from_tender(tender, lanes, None))
    assert again["id"] == t["id"]  # idempotent per tender
    k = contract_and_spot(quote(FCL2, c))[0][0]
    assert k["contract_tariff_id"] == t["id"] and k["total_cents"] > 296_000


def test_create_from_tender_with_services_tenders_lane_shape():
    """The shape wt-tenders services/tenders.py award_tender passes (lane.parsed / lane.request / lane.best with the
    priced option's real lines): the contract price is the transport part of the tender price, not the all-in total."""
    from services.contract_tariffs import create_from_tender

    c = company()
    priced = quote({**FCL2, "customs_clearance": True}, c)
    best_opt = min(priced["options"], key=lambda o: o["total_cents"])
    lane = {"id": "TL1", "ref": "7", "status": "priced",
            "parsed": {"origin": {"text": "Shanghai", "city": "Shanghai", "unlocode": "CNSHA", "zip": None, "country": "CN"},
                       "destination": {"text": "Helsinki", "city": "Helsinki", "unlocode": None, "zip": None, "country": "FI"},
                       "mode": "sea", "service": "FCL", "containers": [{"type": "40HC", "count": 2}]},
            "request": {"origin": {"unlocode": "CNSHA"}, "destination": {"query": "Helsinki"}, "mode": "sea"},
            "best": {"mode": "sea", "service": "FCL", "unit": "per_container", "unit_qty": 2.0,
                     "price_per_unit_cents": best_opt["total_cents"] // 2, "total_cents": best_opt["total_cents"],
                     "valid_until": best_opt["valid_until"], "lines": best_opt["lines"]}}
    tender = {"id": "s7test-" + uuid.uuid4().hex[:8], "number": "T-2026-TEST", "company_id": c["id"]}
    t = run(create_from_tender(tender, [lane], None))
    CREATED["tariffs"].append(t["id"])
    ln = t["lanes"][0]
    transport = sum(x["amount_cents"] for x in best_opt["lines"] if x["stage"] in ("origin", "main", "destination"))
    fees = sum(x["amount_cents"] for x in best_opt["lines"] if x["stage"] not in ("origin", "main", "destination"))
    assert fees > 0  # documentation + import customs: not part of the lane price
    assert ln["price_cents"] == round(transport / 2) and ln["basis"] == "PER_CONTAINER" and ln["surcharges_included"] is True
    assert ln["origin"] == {"country": "CN", "locode": "CNSHA", "name": "Shanghai"} and ln["destination"]["country"] == "FI"
    assert ln["equipment_type"] == "40HC" and ln["tender_lane_id"] == "TL1" and t["valid_to"] == best_opt["valid_until"]
    k = contract_and_spot(quote({**FCL2, "customs_clearance": True}, c))[0][0]
    assert k["contract_tariff_id"] == t["id"]
    assert abs(k["total_cents"] - best_opt["total_cents"]) <= 2  # same all-in price as tendered, fees not counted twice


def assert_below_floor_not_recommended(q, k, spot):
    assert k["below_floor"] is True and k["recommended"] is False
    w = next(w for w in k["warnings"] if w["code"] == "contract.below_floor")
    assert w["params"]["margin_cents"] == k["margin_cents"] and w["message_fi"].startswith("Sopimushinta alittaa")
    rec = [o for o in q["options"] if o["recommended"]]
    assert len(rec) == 1 and rec[0]["price_basis"] == "spot" and rec[0] in spot  # a sellable spot option takes it
    assert rec[0]["margin_cents"] >= max(rec[0]["margin_floor_cents"] or 0, 0)


def test_contract_price_below_cost_is_flagged_not_hidden():
    c = company()
    tariff(c, [{**SEA_LANE, "price_cents": 50_000}])  # 500 EUR / container, far below any carrier's cost
    q = quote(FCL2, c)
    contract, spot = contract_and_spot(q)
    k = contract[0]
    assert k["margin_cents"] < 0  # the agreed price stands (never raised), the loss is visible ...
    assert [ln["unit_price_cents"] for ln in k["lines"] if ln["code"] == "CONTRACT_FREIGHT"] == [50_000]
    assert_below_floor_not_recommended(q, k, spot)  # ... and a loss-making option is never the recommendation
    chk = ops().get("/margin-rules/floor-check", params={"quote_id": q["id"]}).json()
    assert chk["ok"] is False and [x["code"] for x in chk["reasons"]] == ["margin_negative"]


def test_contract_price_under_a_positive_floor_is_flagged_and_spot_recommended(rules):
    """Live Q-2026-000075 (recount): a contract option under its margin floor was still recommended. A floor rule now
    flags a positive-but-thin contract margin the same way; with no floor breach the contract option stays recommended."""
    c = company()
    rules(name="floor 50k", scope={"company_id": c["id"]}, margin_pct=10.0, floor_cents=5_000_000)
    tariff(c, [SEA_LANE])  # 4 000 EUR / container: a positive margin, far under a 50 000 EUR floor
    q = quote(FCL2, c)
    contract, spot = contract_and_spot(q)
    k = contract[0]
    assert k["margin_floor_cents"] == 5_000_000 and 0 <= k["margin_cents"] < 5_000_000
    assert_below_floor_not_recommended(q, k, spot)
    chk = ops().get("/margin-rules/floor-check", params={"quote_id": q["id"]}).json()
    assert chk["ok"] is False and "margin_below_floor" in [x["code"] for x in chk["reasons"]]
