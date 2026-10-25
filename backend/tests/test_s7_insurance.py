"""s7 cargo insurance: insurer selection, exclusions, the no-contract state and the autopilot on booking.

There is no stand-in insurer. Redkik is the only adapter; until Redkik issues our API token set (insurer contract),
every quote and bind answers 503 `insurer_not_configured` with state `live_ready` and the named blocker, the shipment's
insurance_auto says so, and the owner queue gets a card. These tests assert exactly that state.

- API tests: the real local backend over HTTP (V7_API, V7_DB), started with no Redkik credentials (standard CI).
- Offline tests: captured real Redkik responses and its published OpenAPI (tests/fixtures/s7_ins_*.json).
- One opt-in live check (S7_LIVE=1): Redkik production's public endpoints (REDKIK_BASE_URL=https://sandbox.app.redkik.com:
  Redkik's test host). With REDKIK_CLIENT_ID/SECRET set it also quotes, binds, fetches the certificate and cancels for real.

Env: V7_API (http://localhost:8150/api), V7_DB, DEMO_OPS_PASSWORD, MONGO_URL, S7_LIVE.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

API = os.environ.get("V7_API", "http://localhost:8150/api")
DB_NAME = os.environ.get("V7_DB", "huo_s7_ins_1")
OPS_PW = os.environ.get("DEMO_OPS_PASSWORD", "ci-pass-7")
BACKEND = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adapters import insurer as ins  # noqa: E402


# ------------------------------------------------------------------------------------------------- helpers

@lru_cache
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def _client(token: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}", "X-Include-Test-Tenants": "true"}, timeout=120)


def api_up():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"API not reachable at {API}: {exc}")


@lru_cache
def ops() -> httpx.Client:
    api_up()
    r = httpx.post(f"{API}/auth/login", json={"email": "ops.demo@huolitsija.fi", "password": OPS_PW}, timeout=30)
    assert r.status_code == 200, r.text
    return _client(r.json()["token"])


def signup() -> tuple[httpx.Client, dict]:
    api_up()
    email = f"s7ins-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={
        "email": email, "password": "Testi-salasana-2026", "name": "S7 Vakuutus Testaaja",
        "company_name": f"S7 Vakuutus Testi Oy {uuid.uuid4().hex[:5]}", "locale": "fi", "accept_terms": True})
    assert r.status_code == 200, r.text
    body = r.json()
    mongo().users.update_one({"email": email}, {"$set": {"test": True}})
    mongo().companies.update_one({"id": body["user"]["company_id"]},
                                 {"$set": {"test": True, "kyc_status": "approved", "sanctions_status": "clear", "vat_registered": True}})
    # s7 credit enforcement: a new customer starts at the provisional default limit; these tests book many shipments.
    r = ops().patch(f"/credit/{body['user']['company_id']}", json={"credit_limit_cents": 10_000_000, "reason": "insurance tests"})
    assert r.status_code == 200, r.text
    return _client(body["token"]), body["user"]


@lru_cache
def customer() -> tuple[httpx.Client, dict]:
    return signup()


def ready(days: int = 4) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}
PALLETS = {"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
           "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2.0,
                     "value_cents": 800000, "description": "Upholstered wooden chairs", "hs_hint": "940161"}}


def customer_quote(c: httpx.Client, cover: str = "ICC_A", **cargo) -> dict:
    body = json.loads(json.dumps(PALLETS))
    body["cargo"].update(cargo)
    r = c.post("/quotes", json={**body, "ready_date": ready(), "insurance": {"cover": cover}})
    assert r.status_code == 200, r.text
    return r.json()["quote"]


def book(c: httpx.Client, q: dict) -> dict:
    r = c.post("/bookings", json={"quote_id": q["id"], "option_id": q["options"][0]["id"],
                                  "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL},
                                  "consignee": {"name": "Testi Oy", "address": ADDR_TRE},
                                  "pickup": {"address": ADDR_TLL, "date": ready(5)}, "delivery": {"address": ADDR_TRE},
                                  "customer_reference": "PO-INS-1", "accept_terms": True})
    assert r.status_code == 200, r.text
    return r.json()


def set_autopilot(on: bool):
    r = ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})
    assert r.status_code == 200, r.text
    mongo().settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": False}})


def wait_for(pred, seconds: float = 45):
    end = time.time() + seconds
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    return None


def assert_insurer_not_configured(r: httpx.Response) -> dict:
    assert r.status_code == 503, r.text
    d = r.json()["detail"]
    assert d["code"] == "insurer_not_configured" and d["state"] == "live_ready", d
    assert "Redkik" in d["blocked_on"] and "REDKIK_CLIENT_ID" in d["blocked_on"] and "Vakuutussopimus" in d["blocked_on_fi"]
    assert d["message_fi"].startswith("Vakuutusyhtiösopimusta ei vielä ole")
    return d


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------- LIVE (opt-in)

@pytest.mark.skipif(os.environ.get("S7_LIVE") != "1", reason="opt-in live check against Redkik: set S7_LIVE=1")
def test_live_redkik_production():
    # Production by default; REDKIK_BASE_URL=https://sandbox.app.redkik.com runs the same check on Redkik's test host.
    rk = ins.Redkik()
    if os.environ.get("REDKIK_BASE_URL"):
        assert rk.base in (ins.REDKIK_PRODUCTION, ins.REDKIK_SANDBOX), rk.base
    else:
        assert rk.base == ins.REDKIK_PRODUCTION and rk.mode == "live"
    st = run(rk.status())
    assert st["http"] == 200 and st["body"] == {"statusMsg": "running"}
    rows = run(rk.search_commodities("940161", 25))
    assert rows and any(r["name"].startswith("HS/HTS 940161") for r in rows)
    uuid.UUID(rows[0]["id"])
    if not rk.configured():
        probe = ins.Redkik()
        probe.client_id, probe.client_secret = "huolitsija-probe-" + uuid.uuid4().hex[:6], "not-a-secret"
        with pytest.raises(ins.InsurerError) as e:
            run(probe.token())
        assert e.value.code == "insurer_auth_failed" and e.value.detail["body"]["error"] == "invalid_client"
        with pytest.raises(ins.InsurerError) as e:
            run(rk.token())
        assert e.value.code == "insurer_not_configured" and e.value.detail["state"] == "live_ready"
        return
    start, end = ins.cover_dates(ready(3) + "T08:00:00Z", None, 4)
    req = {"subject": {"shipment_id": "live-test"}, "cover": "ICC_A", "insured_value_cents": 880000, "currency": "EUR", "mode": "road",
           "origin": {"unlocode": "EETLL", "country": "EE"}, "destination": {"unlocode": "FITMP", "country": "FI"},
           "start": start, "end": end, "cargo": PALLETS["cargo"], "description": "Upholstered wooden chairs, 2 pallets",
           "reference": "LIVE-" + uuid.uuid4().hex[:6], "carrier": "Tallink", "customer": {"name": "Huolitsija test", "country": "FI"}}
    offer = run(rk.quote(req))
    assert offer["provider_quote_id"] and isinstance(offer["premium_cents"], int) and offer["premium_cents"] > 0
    if not offer["accepted"]:
        pytest.fail(f"Redkik referred the offer: {offer['amendments']}")
    bound = run(rk.bind(offer["provider_quote_id"]))
    assert bound["provider_policy_id"] and bound["provider_status"] in ("ACTIVE", "PENDING", "AWAITING_APPROVAL", "AWAITING_PAYMENT")
    pdf = run(rk.certificate(bound["provider_policy_id"]))
    if pdf:
        assert pdf[0][:5] == b"%PDF-"
    assert run(rk.cancel(bound["provider_policy_id"]))["cancelled"] is True


def test_offline_adapter_without_credentials_is_live_ready_not_a_stand_in(monkeypatch):
    for k in ("INSURANCE_PROVIDER", "REDKIK_CLIENT_ID", "REDKIK_CLIENT_SECRET", "REDKIK_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    p = ins.get_provider("")
    assert isinstance(p, ins.Redkik) and p.base == "https://sales.app.redkik.com" and p.mode == "live"
    info = p.info()
    assert info["state"] == "live_ready" and info["configured"] is False and info["blocked_on"] == ins.BLOCKED_ON
    assert set(ins.PROVIDERS) == {"redkik"} and not hasattr(ins, "StubNamed")
    with pytest.raises(ins.InsurerError) as e:
        run(p.token())
    assert e.value.code == "insurer_not_configured" and e.value.status == 503 and e.value.detail["state"] == "live_ready"
    monkeypatch.setenv("INSURANCE_PROVIDER", "stub_named")
    with pytest.raises(ins.InsurerError) as e:
        ins.get_provider("")
    assert e.value.code == "insurer_unknown"
    monkeypatch.setenv("INSURANCE_PROVIDER", "redkik")
    monkeypatch.setenv("REDKIK_CLIENT_ID", "id")
    monkeypatch.setenv("REDKIK_CLIENT_SECRET", "secret")
    assert ins.get_provider("").info()["state"] == "live"


# ------------------------------------------------------------------------------------------------- offline (captured)

def test_offline_quote_body_matches_redkik_published_schema():
    spec = fixture("s7_ins_redkik_openapi.json")
    qr = spec["schemas"]["QuoteRequest"]
    rk = ins.Redkik()
    start, end = ins.cover_dates("2026-11-02T08:00:00Z", "2026-11-30T08:00:00Z")
    body = rk.quote_body({"subject": {"shipment_id": "s1"}, "cover": "ICC_B", "insured_value_cents": 1234567, "currency": "EUR",
                          "mode": "sea", "origin": {"unlocode": "CNNGB", "country": "CN"}, "destination": {"unlocode": "FIHEL", "country": "FI"},
                          "start": start, "end": end, "description": "Sofas", "reference": "HUO-1", "loading_port": "CNNGB",
                          "discharge_port": "DEHAM", "vessel_name": "MSC Test", "voyage": "123W", "carrier": "MSC",
                          "customer": {"name": "Pohjolan Kaluste Oy", "business_id": "3990001-4", "country": "FI", "address": "Askonkatu 9, Lahti"}},
                         {"id": "01475cfc-daaa-46eb-acb8-e7500dc4a1fb"})
    assert set(qr["required"]) <= set(body)
    unknown = set(body) - set(qr["properties"])
    assert not unknown, f"fields not in Redkik's QuoteRequest: {unknown}"
    for k in ("conveyanceType", "originType", "destinationType", "loadingSpaceType", "dischargeSpaceType"):
        if k in body:
            assert body[k] in qr["properties"][k]["enum"], k
    com = spec["schemas"]["QuoteRequestCommodity"]
    assert set(com["required"]) <= set(body["commodities"][0])
    assert body["commodities"][0]["insuredValue"] == 12345.67  # currency units; premiums come back in cents
    assert set(spec["schemas"]["QuoteRequestCustomer"]["required"]) <= set(body["customer"])
    assert "/api/v2/quote/quotes/quote" in spec["paths"] and "post" in spec["paths"]["/api/v2/quote/quotes/quote"]
    purchase = spec["paths"]["/api/v2/quote/bookings/purchase"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in purchase and purchase["multipart/form-data"]["schema"]["required"] == ["offerId"]


def test_offline_offer_normalisation_reads_redkik_offer_fields():
    """The offer parser only reads fields the published OfferResponse marks required (cents, accepted, clauses)."""
    offer = fixture("s7_ins_redkik_openapi.json")["schemas"]["OfferResponse"]
    for f in ("id", "insurerPremium", "totalCost", "accepted", "amendments", "currencySymbol", "clauses"):
        assert f in offer["required"]
    assert "cents" in offer["properties"]["totalCost"]["description"].lower()
    booking = fixture("s7_ins_redkik_openapi.json")["schemas"]["BookingDto"]
    assert set(booking["properties"]["status"]["enum"]) >= {"ACTIVE", "CANCELLED", "AWAITING_APPROVAL"}


def test_offline_commodity_pick_from_captured_search(monkeypatch):
    cap = fixture("s7_ins_redkik_sandbox_public.json")
    by_q = {c["url"].split("query=")[1].split("&")[0]: c["body"] for c in cap["calls"] if "commodities/search" in c["url"]}
    rk = ins.Redkik()

    async def fake_search(q, take=10):
        return by_q.get(q, [])
    monkeypatch.setattr(rk, "search_commodities", fake_search)
    assert run(rk.commodity_for({"hs_hint": "940161"}))["name"].startswith("HS/HTS 940161")
    four = run(rk.commodity_for({"hs_hint": "9401", "description": "Seats convertible into beds of wood"}))
    assert four["name"].startswith("HS/HTS 9401") and "beds" in four["name"]
    with pytest.raises(ins.InsurerError):
        run(rk.commodity_for({"description": "zz"}))


def test_offline_reprice_option_line_swaps_indicative_premium():
    asyncio.set_event_loop(asyncio.new_event_loop())  # services.insurance imports core.db (Motor wants a loop at import)
    from services.insurance import reprice_option_line
    opt = {"lines": [{"code": "INSURANCE", "amount_cents": 3500, "cost_cents": 3500, "vat_rate_pct": 0.0}],
           "total_cents": 103500, "pass_through_cents": 3500, "cost_cents": 80000, "vat_cents": 1000, "total_incl_vat_cents": 104500}
    iq = {"id": "iq1", "status": "quoted", "premium_cents": 5210, "provider": "redkik", "provider_label": "Redkik", "cover_label": "ICC (A)",
          "insured_value_cents": 900000, "currency": "EUR"}
    reprice_option_line(opt, iq)
    assert opt["lines"][0]["amount_cents"] == 5210 and opt["total_cents"] == 105210 and opt["total_incl_vat_cents"] == 106210
    assert opt["lines"][0]["insurance_quote_id"] == "iq1" and opt["lines"][0]["estimated"] is False
    assert reprice_option_line({"lines": []}, {"status": "refused"}) == {"lines": []}


# ------------------------------------------------------------------------------------------------- API (no insurer contract)

def test_providers_endpoint_reports_live_ready_blocked_on_insurer_contract():
    r = ops().get("/insurance/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] == "redkik" and body["state"] == "live_ready" and "Redkik" in body["blocked_on"]
    assert [p["provider"] for p in body["providers"]] == ["redkik"]
    rk = body["providers"][0]
    assert rk["configured"] is False and rk["mode"] == "live" and rk["base_url"] == "https://sales.app.redkik.com"
    assert rk["missing"] == ["REDKIK_CLIENT_ID", "REDKIK_CLIENT_SECRET"] and rk["state"] == "live_ready"


def test_quote_without_insurer_contract_is_explicit_and_binds_nothing():
    c, user = customer()
    q = customer_quote(c, "ICC_A")
    d = assert_insurer_not_configured(c.post("/insurance/quote", json={"quote_id": q["id"], "cover": "ICC_A"}))
    iq = mongo().insurance_quotes.find_one({"id": d["insurance_quote_id"]})
    opt = q["options"][0]
    freight = sum(ln["amount_cents"] for ln in opt["lines"] if ln["stage"] in ("origin", "main"))
    assert iq["status"] == "insurer_not_configured" and iq["provider"] == "redkik" and iq["premium_cents"] is None
    assert iq["insured_value_cents"] == round((800000 + freight) * 1.10)  # RULE X-1 still computed: 110 % CIF
    assert iq["error"]["detail"]["state"] == "live_ready" and "stub" not in iq
    assert not mongo().quotes.find_one({"id": q["id"]}).get("insurance_quote_id")
    assert mongo().audit_log.find_one({"action": "insurance_quote_blocked", "entity_id": iq["id"]})
    b = c.post(f"/insurance/{iq['id']}/bind")
    assert b.status_code == 409 and b.json()["detail"]["code"] == "quote_insurer_not_configured"
    assert c.get("/insurance/policies", params={"quote_id": q["id"]}).json()["items"] == []


def test_customer_cannot_insure_another_companys_shipment():
    c, _ = customer()
    other = mongo().shipments.find_one({"company_id": {"$ne": customer()[1]["company_id"]}}, {"id": 1})
    r = c.post("/insurance/quote", json={"shipment_id": other["id"], "cover": "ICC_A", "insured_value_cents": 100000})
    assert r.status_code == 403


def test_value_missing_and_bad_cover_are_refused():
    c, _ = customer()
    q = customer_quote(c, "NONE", value_cents=None)
    r = c.post("/insurance/quote", json={"quote_id": q["id"], "cover": "ICC_A"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "value_missing"
    assert c.post("/insurance/quote", json={"quote_id": q["id"], "cover": "ALL_RISKS", "insured_value_cents": 1000}).status_code == 400


def _shipment_fixture(company_id: str, legs_countries: list[tuple[str, str]], **extra) -> dict:
    sid = str(uuid.uuid4())
    loc = lambda cc, code: {"name": code, "code": code, "unlocode": code, "country": cc, "lat": 60.0, "lon": 25.0}  # noqa: E731
    legs = [{"seq": i + 1, "mode": "rail", "from": loc(a, a + "AAA"), "to": loc(b, b + "BBB"), "carrier": "Test Rail"}
            for i, (a, b) in enumerate(legs_countries)]
    doc = {"id": sid, "reference": "HUO-TEST-" + sid[:6], "company_id": company_id, "mode": "rail", "legs": legs,
           "origin": legs[0]["from"], "destination": legs[-1]["to"], "etd": None, "eta": None,
           "cargo": {"description": "Machine parts", "value_cents": 2000000, "hs_hint": "843139"}, "insurance": {"cover": "NONE"},
           "price": {"total_cents": 300000, "currency": "EUR"}, "status": "booked", "test": True, **extra}
    mongo().shipments.insert_one(dict(doc))
    return doc


def test_exclusions_refuse_sanctioned_route_dg_and_party_under_review():
    c, user = customer()
    via_ru = _shipment_fixture(user["company_id"], [("CN", "KZ"), ("KZ", "RU"), ("RU", "FI")])
    r = c.post("/insurance/quote", json={"shipment_id": via_ru["id"], "cover": "ICC_A"})
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["code"] == "sanctioned_country" and d["reasons"][0]["countries"] == ["RU"] and "Pakotemaa" in d["message_fi"]
    assert mongo().insurance_quotes.find_one({"id": d["insurance_quote_id"]})["status"] == "refused"
    dg = _shipment_fixture(user["company_id"], [("CN", "KZ"), ("KZ", "FI")],
                           cargo={"description": "Lithium batteries", "value_cents": 500000, "dangerous_goods": True,
                                  "dg": {"un_number": "3480", "class": "9"}})
    r = c.post("/insurance/quote", json={"shipment_id": dg["id"], "cover": "ICC_A"})
    # Redkik decides dangerous goods per commodity: never refused by us, quoted but never auto-bound (legal point
    # dangerous_goods). Without the insurer contract the DG quote stops at the insurer step like any other.
    assert ins.Redkik.dangerous_goods == "refer"
    assert_insurer_not_configured(r)
    clean = _shipment_fixture(user["company_id"], [("CN", "KZ"), ("KZ", "FI")])
    mongo().companies.update_one({"id": user["company_id"]}, {"$set": {"sanctions_status": "potential_match"}})
    try:
        r = c.post("/insurance/quote", json={"shipment_id": clean["id"], "cover": "ICC_C"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "party_sanctions"
    finally:
        mongo().companies.update_one({"id": user["company_id"]}, {"$set": {"sanctions_status": "clear"}})
    # a clean route passes our exclusions and reaches the insurer step, which is blocked on the contract
    d = assert_insurer_not_configured(c.post("/insurance/quote", json={"shipment_id": clean["id"], "cover": "ICC_C"}))
    assert mongo().insurance_quotes.find_one({"id": d["insurance_quote_id"]})["insured_value_cents"] == round((2000000 + 300000) * 1.10)


def _booked_state(cover: str) -> tuple[str, dict]:
    c, _ = customer()
    sid = book(c, customer_quote(c, cover))["shipment_id"]  # booking itself never waits on the insurer
    s = wait_for(lambda: mongo().shipments.find_one({"id": sid, "insurance_auto.state": {"$in": ["insurer_not_configured", "bound",
                                                                                                  "owner_queue", "refused"]}}))
    assert s, f"insurance agent did not record a state: {mongo().shipments.find_one({'id': sid}, {'insurance_auto': 1})}"
    return sid, s


@pytest.mark.parametrize("autopilot", [True, False])
def test_booking_with_cover_records_insurer_not_configured_and_asks_owner(autopilot):
    set_autopilot(autopilot)
    try:
        sid, s = _booked_state("ICC_A" if autopilot else "ICC_B")
        auto = s["insurance_auto"]
        assert auto["state"] == "insurer_not_configured" and auto["state_detail"] == "live_ready", auto
        assert auto["blocked_on"] == ins.BLOCKED_ON and auto["blocked_on_fi"] == ins.BLOCKED_ON_FI
        assert not s.get("insurance_policy_id") and not mongo().insurance_policies.find_one({"shipment_id": sid})
        card = mongo().approvals.find_one({"kind": "insurance_bind", "ref_id": sid, "status": "pending"})
        assert card and card["id"] == auto["approval_id"]
        assert [r["code"] for r in card["data"]["reasons"]] == ["insurer_not_configured"]
        assert "Vakuutusta ei sidottu" in card["data"]["reason_fi"]
        assert mongo().audit_log.find_one({"action": "insurance_bind_blocked", "entity_id": sid})
    finally:
        set_autopilot(False)


def test_kill_switch_stops_auto_bind():
    set_autopilot(True)
    mongo().settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": True}})
    try:
        c, _ = customer()
        sid = book(c, customer_quote(c, "ICC_A"))["shipment_id"]
        # s2's core.jobs does not claim agent jobs while the kill switch is set: the bind job stays held
        held = wait_for(lambda: mongo().jobs.find_one({"kind": "insurance.bind_on_booking", "payload.shipment_id": sid,
                                                       "status": {"$in": ["queued", "retry"]}}))
        assert held, "bind job not held by the kill switch"
        time.sleep(3)
        s = mongo().shipments.find_one({"id": sid})
        assert not s.get("insurance_policy_id") and (s.get("insurance_auto") or {}).get("state") == "queued"
    finally:
        set_autopilot(False)
