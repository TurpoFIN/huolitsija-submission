"""s5 any-city quoting, against the REAL local API (V2_API, default http://localhost:8105/api) and its MongoDB (V2_DB).

Nothing is mocked: places come from the GeoNames/UN-LOCODE data and (for hamlets) the live Nominatim geocoder,
hubs from Mongo `hubs`, prices from the seeded rate cards and the dated benchmark levels the server loaded.
Run: V2_API=http://127.0.0.1:8105/api V2_DB=huo_s5 DEMO_OPS_PASSWORD=... ~/work/venv/bin/python -m pytest -q backend/tests/test_core_global_quote.py
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import httpx
import pytest
from pymongo import MongoClient

API = os.environ.get("V2_API", "http://localhost:8105/api")
DB = os.environ.get("V2_DB", "huo_s5")
OPS_PW = os.environ.get("DEMO_OPS_PASSWORD", "S5-local-demo-2026")
CUSTOMER_PW = os.environ.get("DEMO_CUSTOMER_PASSWORD", OPS_PW)
CREATED: list[str] = []


def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB]


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    if CREATED and not os.environ.get("S5_KEEP_TEST_DATA"):
        mongo().quotes.delete_many({"id": {"$in": CREATED}})


def client(token: str | None = None, ip: str | None = None) -> httpx.Client:
    h = {"X-Include-Test-Tenants": "true", "X-Forwarded-For": ip or f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.7"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=h, timeout=180)


def login(email: str, pw: str) -> httpx.Client:
    r = client().post("/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return client(r.json()["token"])


def public_quote(origin, destination, cargo, **extra) -> dict:
    r = client().post("/public/quote", json={"origin": origin, "destination": destination, "cargo": cargo, **extra})
    assert r.status_code == 200, f"{origin} -> {destination}: {r.status_code} {r.text[:300]}"
    body = r.json()
    CREATED.append(body["quote"]["id"])
    return body


PALLETS = {"weight_kg": 1800, "volume_m3": 6, "packages": 4, "package_type": "pallet_eur"}
FCL40 = {"load_type": "FCL", "containers": [{"type": "40HC", "count": 1}], "weight_kg": 18000}
PARCEL = {"weight_kg": 12, "volume_m3": 0.05, "packages": 1, "package_type": "carton"}

# 12 pairs, every inhabited continent, none of them a seeded lane (the seed covers Asia->FI, FI->US and EU road)
PAIRS = [
    ("Ouagadougou", "Kuopio", PALLETS, "BF", "FI"),          # Africa (landlocked) -> Europe
    ("Lagos, Nigeria", "Lima", FCL40, "NG", "PE"),           # Africa -> South America
    ("Belo Horizonte", "Tampere", PALLETS, "BR", "FI"),      # South America -> Europe
    ("Nairobi", "Dubai", PALLETS, "KE", "AE"),               # Africa -> Middle East
    ("Denver", "Monterrey", PALLETS, "US", "MX"),            # North America (road-connected)
    ("Hobart", "Christchurch", PALLETS, "AU", "NZ"),         # Oceania islands
    ("Ulaanbaatar", "Almaty", FCL40, "MN", "KZ"),            # Central Asia, landlocked both ends
    ("Kuopio", "Cochabamba", PARCEL, "FI", "BO"),            # small parcel to landlocked South America
    ("Winnipeg", "Oulu", PALLETS, "CA", "FI"),               # North American interior -> Nordic
    ("Tukholma", "Lontoo", PALLETS, "SE", "GB"),             # Finnish exonyms, Europe road
    ("Chengdu", "Hamburg", {"load_type": "FCL", "containers": [{"type": "40HC", "count": 2}], "weight_kg": 30000}, "CN", "DE"),
    ("Mumbai", "Nairobi", FCL40, "IN", "KE"),                # Indian Ocean
]


def _check_option(o: dict):
    assert o["firmness"] in ("firm", "partial", "estimate")
    assert o["bookable"] is (o["firmness"] == "firm")
    assert type(o["total_cents"]) is int and o["total_cents"] > 0
    assert o["legs"] and any(lg["kind"] == "main" for lg in o["legs"])
    for lg in o["legs"]:
        src = lg.get("pricing") or {}
        assert src.get("kind") in ("rate_card", "partner_rate", "benchmark", "benchmark_derived", "model"), lg
        if src["kind"].startswith("benchmark"):
            assert src.get("as_of"), f"benchmark leg without a date: {src}"
            date.fromisoformat(src["as_of"][:10])
            assert src.get("url", "").startswith("http"), src
    for ln in o["lines"]:
        assert "cost_cents" not in ln and "unit_cost_cents" not in ln  # public view hides buy prices
        if ln.get("estimated"):
            assert (ln.get("source") or {}).get("kind") in ("benchmark", "benchmark_derived", "model")


@pytest.mark.parametrize("origin,destination,cargo,o_cc,d_cc", PAIRS, ids=[f"{a}>{b}" for a, b, *_ in PAIRS])
def test_any_city_pair_gets_priced_options(origin, destination, cargo, o_cc, d_cc):
    body = public_quote(origin, destination, cargo)
    q = body["quote"]
    assert q["request"]["origin"]["country"] == o_cc
    assert q["request"]["destination"]["country"] == d_cc
    assert q["options"], f"no options for {origin} -> {destination}: {q.get('warnings')}"
    for o in q["options"]:
        _check_option(o)
    assert q["sourcing"]["status"] in ("none", "off", "requested", "partial", "unavailable")
    assert q["sourcing"].get("message_fi") or q["sourcing"]["status"] == "none"
    assert "needs" not in q["sourcing"]  # carries our estimated buy cost
    if not any(o["firmness"] == "firm" for o in q["options"]):
        assert any(w["code"] == "lane.estimate_only" for w in q["warnings"])


def test_seeded_lane_by_city_name_stays_firm():
    q = public_quote("Shanghai", "Helsinki", FCL40)["quote"]
    assert q["request"]["origin"]["unlocode"] == "CNSHA" and q["request"]["destination"]["unlocode"] == "FIHEL"
    rec = next(o for o in q["options"] if o["recommended"])
    assert rec["firmness"] == "firm" and rec["bookable"]
    assert all((lg.get("pricing") or {}).get("kind") in ("rate_card", "partner_rate") for lg in rec["legs"])


def test_unlocode_requests_unchanged():
    q = public_quote({"unlocode": "CNSHA"}, {"unlocode": "FIHEL"}, FCL40)["quote"]
    assert any(o["firmness"] == "firm" for o in q["options"])
    assert all(o["service"] in ("FCL", "RAIL_FCL") for o in q["options"])
    typed = public_quote("CNSHA", "fihel", FCL40)["quote"]["request"]  # a typed code is still a code
    assert typed["origin"]["unlocode"] == "CNSHA" and typed["destination"]["unlocode"] == "FIHEL"


@pytest.mark.parametrize("text,cc,name", [
    ("Lagos", "NG", "Lagos"), ("Lagos, Portugal", "PT", "Lagos"), ("Portland, Maine", "US", "Portland"),
    ("Tukholma", "SE", "Stockholm"), ("Kööpenhamina", "DK", "Copenhagen"), ("Belo Horizonte BR", "BR", "Belo Horizonte"),
    ("Paris", "FR", "Paris"), ("Dubai", "AE", "Dubai"), ("Tokyo", "JP", "Tokyo"),  # five letters, but not UN/LOCODEs
])
def test_place_resolution(text, cc, name):
    q = public_quote(text, "Tampere", PARCEL)["quote"]
    o = q["request"]["origin"]
    assert o["country"] == cc and name.lower() in (o.get("city") or o["name"]).lower(), o
    assert "city" in (o.get("kinds") or []), f"{text} resolved to a hub record, not the city: {o}"
    if text == "Portland, Maine":
        assert (o.get("admin1") or "").lower().startswith("maine")


def test_finnish_small_town_keeps_its_unlocode():
    q = public_quote("Utsjoki", "Helsinki", PARCEL)["quote"]  # ~1 200 inhabitants: the town, carrying its UN/LOCODE
    o = q["request"]["origin"]
    assert o["country"] == "FI" and o["lat"] > 69 and o["geocode"]["source"] == "geonames", o
    assert o["unlocode"] == "FIUTS" and o["geocode"]["hub"] == "FIUTS", o


def test_small_town_abroad_from_towns_tier():
    """Towns below the 15 000-inhabitant city tier resolve offline from GeoNames cities1000 (no public geocoder needed)."""
    for text, cc, lat, lon in [("Mazamitla, Mexico", "MX", 19.92, -103.02), ("Kalbarri", "AU", -27.71, 114.17)]:
        o = public_quote(text, "Helsinki", PARCEL)["quote"]["request"]["origin"]
        assert o["country"] == cc and abs(o["lat"] - lat) < 0.1 and abs(o["lon"] - lon) < 0.1, o
        assert o["geocode"]["source"] == "geonames" and o["geocode"].get("tier") == "town", o


def test_hamlet_goes_to_live_geocoder_or_says_busy():
    """Below 1 000 inhabitants only OpenStreetMap knows the place. Nominatim answers (then the cache does), or, when
    Nominatim and Photon both refuse this IP, the customer is told to retry: never "no such place"."""
    text = "Ushguli, Georgia"  # ~200 inhabitants, Svaneti
    r = client().post("/public/quote", json={"origin": text, "destination": "Helsinki", "cargo": PARCEL})
    if r.status_code == 200:
        CREATED.append(r.json()["quote"]["id"])
        o = r.json()["quote"]["request"]["origin"]
        assert o["country"] == "GE" and 42.9 < o["lat"] < 43.1, o
        assert o["geocode"]["source"] in ("nominatim", "photon"), o
        assert mongo().geocode_cache.find_one({"key": {"$regex": "^place1:ushguli"}})
    else:
        assert r.status_code == 400, r.text
        assert "map service is busy" in r.json()["detail"], r.text  # refusal is not cached as "not found"
        assert not mongo().geocode_cache.find_one({"key": {"$regex": "^place1:ushguli"}, "result": None})


@pytest.mark.parametrize("body,needle", [
    ({"origin": "Xqzvbnmwq Plkjh", "destination": "Tampere"}, "origin"),
    ({"origin": "Moskova", "destination": "Tampere"}, "Russia"),
    ({"origin": "Minsk, Belarus", "destination": "Tampere"}, "Russia"),
    ({"origin": "Novosibirsk", "destination": "Helsinki"}, "Russia"),
    ({"origin": "12345", "destination": "Tampere"}, "origin"),
    ({"origin": "Lima" * 60, "destination": "Tampere"}, "origin"),
])
def test_refusals_are_plain(body, needle):
    r = client().post("/public/quote", json={**body, "cargo": PARCEL})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_places_autocomplete_has_cities_ports_airports():
    r = client().get("/public/places", params={"q": "ouag"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["name"].lower().startswith("ouagadougou") and items[0]["country"] == "BF"
    lagos = client().get("/public/places", params={"q": "Lagos"}).json()["items"]
    assert lagos[0]["country"] == "NG"
    kinds = {i["kind"] for i in client().get("/public/places", params={"q": "Rotterdam", "limit": 10}).json()["items"]}
    assert "port" in kinds


def test_estimate_option_cannot_be_booked():
    ops = login("ops.demo@huolitsija.fi", OPS_PW)
    cos = ops.get("/quotes/companies").json()["items"]
    company = next(c for c in cos if c.get("kyc_status") == "approved") if any(c.get("kyc_status") == "approved" for c in cos) else cos[0]
    r = ops.post("/quotes", json={"origin": "Ouagadougou", "destination": "Kuopio", "cargo": PALLETS, "company_id": company["id"],
                                  "send": True})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    CREATED.append(q["id"])
    est = next(o for o in q["options"] if not o["bookable"])
    if q["status"] in ("sent", "accepted"):
        r = ops.post(f"/quotes/{q['id']}/accept", json={"option_id": est["id"]})
        assert r.status_code == 409 and "estimate" in r.json()["detail"]
    else:
        assert q["status"] == "pending_approval" and "estimate_only" in q["approval"]["reasons"]


def test_sourcing_state_with_switch():
    """Partner sourcing off -> 'off'; on -> s3 is asked (or, without s3 deployed, an honest 'unavailable')."""
    ops = login("ops.demo@huolitsija.fi", OPS_PW)
    before = ops.get("/settings").json() if ops.get("/settings").status_code == 200 else {}
    was_on = bool((before.get("settings") or before).get("partner_network_enabled"))
    try:
        ops.post("/settings/autonomy/partner_network_enabled", json={"enabled": False, "confirmation": ""})
        q = public_quote("Lagos, Nigeria", "Lima", FCL40)["quote"]
        assert q["sourcing"]["status"] == "off"
        r = ops.post("/settings/autonomy/partner_network_enabled", json={"enabled": True, "confirmation": "enable"})
        assert r.status_code == 200, r.text
        q = public_quote("Lagos, Nigeria", "Lima", FCL40)["quote"]
        s = q["sourcing"]
        stored = mongo().quotes.find_one({"id": q["id"]}, {"_id": 0, "sourcing": 1})["sourcing"]
        if stored.get("reason") == "procurement_not_deployed":
            assert s["status"] == "unavailable" and not s.get("request_ids")
        else:
            assert s["status"] in ("requested", "partial", "unavailable"), s
            assert s["request_ids"], s
            assert any(ln["lane"] == "sea:NG>PE" for ln in s["lanes"]), s["lanes"]
            if s["status"] != "unavailable":
                assert datetime.fromisoformat(s["eta_at"]) > datetime.now(timezone.utc)
                assert s.get("eta_basis")
            req = mongo().lane_rate_requests.find_one({"id": {"$in": s["request_ids"]}, "lane": "sea:NG>PE"}, {"_id": 0})
            assert req and q["id"] in req["quote_ids"]
    finally:
        ops.post("/settings/autonomy/partner_network_enabled",
                 json={"enabled": was_on, "confirmation": "enable" if was_on else ""})


def test_notify_subscription():
    body = public_quote("Hobart", "Christchurch", PALLETS)
    q, token = body["quote"], body["claim_token"]
    r = client().post(f"/public/quote/{q['id']}/notify", json={"token": token, "email": "visitor.s5@example.com", "locale": "fi"})
    assert r.status_code == 200, r.text
    assert mongo().quotes.find_one({"id": q["id"]})["notify_email"] == "visitor.s5@example.com"
    assert client().get(f"/public/quote/{q['id']}", params={"token": token}).json()["quote"].get("notify_email") is None
    assert client().post(f"/public/quote/{q['id']}/notify", json={"token": token, "email": "not-an-address"}).status_code == 400
    assert client().post(f"/public/quote/{q['id']}/notify", json={"token": "wrong", "email": "a@b.fi"}).status_code == 404


def test_benchmarks_are_dated_and_sourced():
    ops = login("ops.demo@huolitsija.fi", OPS_PW)
    r = ops.get("/global/benchmarks")
    assert r.status_code == 200
    items = {i["lane_code"]: i for i in r.json()["items"]}
    for code in ("WCI-SHA-RTM", "FBX24", "FAX-CN-EU", "ROAD-NORTH_AMERICA", "RAIL-MIDCOR-WEST", "COURIER-DHL-FI-Z8"):
        assert code in items, code
        assert items[code]["as_of"] and items[code]["source_url"].startswith("http")
    assert r.json()["fx"]["rates"]["USD"] > 0
    assert client().get("/global/benchmarks").status_code == 401
    bad = ops.post("/global/benchmarks", json={"index": "WCI", "lane_code": "WCI-SHA-RTM", "label": "x", "mode": "sea",
                                               "unit": "USD/FEU", "value": -5, "as_of": "2026-10-29"})
    assert bad.status_code == 400


def test_estimate_lines_name_their_index_and_date():
    q = public_quote("Lagos, Nigeria", "Lima", FCL40)["quote"]
    o = next(o for o in q["options"] if o["mode"] == "sea")
    freight = next(ln for ln in o["lines"] if ln["code"] == "OCEAN_FREIGHT")
    src = freight["source"]
    assert freight["estimated"] and src["kind"] in ("benchmark", "benchmark_derived")
    assert src["index"] in ("WCI", "FBX") and src["as_of"] >= "2026-10-01"
    assert src.get("fx", {}).get("EURUSD")
    codes = {ln["code"] for ln in o["lines"]}
    assert {"OTHC", "DTHC", "DOC", "DO"} <= codes  # surcharges the index excludes are itemised
