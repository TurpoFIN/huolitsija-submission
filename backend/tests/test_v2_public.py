"""Public instant quote on the real server: anonymous quote with claim token, per-IP rate limit stored in MongoDB
(right-most X-Forwarded-For hop, so a client cannot reset it by prepending addresses), claim after real sign-up,
and the full visitor path public quote -> sign-up -> claim -> accept -> booking guard."""
import os
import random

import httpx
import pytest

from test_v2_support import v2_cleanup  # noqa: F401  (session teardown of test data)
from test_v2_support import CREATED, API, PALLETS_TLL_TRE, api_up, booking_body, mongo, ops, ready, signup

LIMIT = int(os.environ.get("PUBLIC_QUOTE_RATE_LIMIT_PER_HOUR", "20"))
CHEAP = {"origin": "FIHEL", "destination": "FITMP",
         "cargo": {"load_type": "loose", "packages": 1, "package_type": "pallet_eur", "weight_kg": 200, "volume_m3": 1}}


def _ip():
    return f"198.51.{random.randint(0, 255)}.{random.randint(1, 254)}"  # TEST-NET-2, unique per test run


def public_quote(body, ip=None):
    api_up()
    r = httpx.post(f"{API}/public/quote", json={"ready_date": ready(), **body}, headers={"X-Forwarded-For": ip or _ip()}, timeout=60)
    if r.status_code == 200:
        CREATED["public_quotes"].add(r.json()["quote"]["id"])
    return r


def test_public_quote_is_anonymous_and_hides_costs():
    r = public_quote(PALLETS_TLL_TRE)
    assert r.status_code == 200, r.text
    body = r.json()
    q = body["quote"]
    assert body["claim_token"] and len(body["claim_token"]) >= 24
    assert q["number"] is None and q["company_id"] is None and q["source"] == "public"
    assert q["options"] and all("cost_cents" not in o and "margin_pct" not in o for o in q["options"])
    assert "claim_token" not in q and "ip_hash" not in q
    stored = mongo().quotes.find_one({"id": q["id"]})
    assert stored["ip_hash"] and "198.51" not in str(stored)  # the IP itself is never stored
    g = httpx.get(f"{API}/public/quote/{q['id']}", params={"token": body["claim_token"]})
    assert g.status_code == 200 and g.json()["quote"]["claimed"] is False
    assert httpx.get(f"{API}/public/quote/{q['id']}", params={"token": "wrong"}).status_code == 404
    pdf = httpx.get(f"{API}/public/quote/{q['id']}/pdf", params={"token": body["claim_token"], "lang": "en"})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_public_quote_validation_is_plain():
    r = public_quote({**CHEAP, "origin": "Nowhereville Xyz"})
    assert r.status_code == 400 and "origin" in r.json()["detail"]
    r = public_quote({**CHEAP, "origin": "Moscow"})
    assert r.status_code == 400 and "Russia" in r.json()["detail"]


def test_rate_limit_per_ip_and_not_spoofable():
    ip = _ip()
    codes = [public_quote(CHEAP, ip).status_code for _ in range(LIMIT)]
    assert codes == [200] * LIMIT
    over = public_quote(CHEAP, ip)
    assert over.status_code == 429
    assert "minutes" in over.json()["detail"] and "sign in" in over.json()["detail"]
    # prepending a fake client address does not help: the right-most hop (added by our ingress) counts
    assert public_quote(CHEAP, f"203.0.113.9, {ip}").status_code == 429
    # another network is unaffected
    assert public_quote(CHEAP, _ip()).status_code == 200
    assert mongo().rate_limits.index_information().get("expires_at_1", {}).get("expireAfterSeconds") == 0


def test_locations_search():
    api_up()
    r = httpx.get(f"{API}/public/locations", params={"q": "Tampere"})
    assert r.status_code == 200
    assert any(i["unlocode"] == "FITMP" for i in r.json()["items"])
    r = httpx.get(f"{API}/public/locations", params={"q": "CNSHA"})
    assert r.json()["items"][0]["unlocode"] == "CNSHA"


def test_claim_rules():
    r = public_quote(PALLETS_TLL_TRE).json()
    token, qid = r["claim_token"], r["quote"]["id"]
    assert ops().post("/quotes/claim", json={"token": token}).status_code == 403
    c, user = signup()
    bad = c.post("/quotes/claim", json={"token": "nope"})
    assert bad.status_code == 404
    ok = c.post("/quotes/claim", json={"token": token})
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == qid
    q = ok.json()["quote"]
    assert q["number"].startswith("Q-") and q["company_id"] == user["company_id"] and q["status"] == "sent"
    assert c.post("/quotes/claim", json={"token": token}).json()["id"] == qid  # idempotent for the same company
    other, _ = signup()
    assert other.post("/quotes/claim", json={"token": token}).status_code == 409
    assert httpx.get(f"{API}/public/quote/{qid}", params={"token": token}).json()["quote"]["claimed"] is True
    assert any(i["id"] == qid for i in c.get("/quotes").json()["items"])


def test_visitor_path_public_quote_signup_claim_accept_kyc_guard():
    pub = public_quote(PALLETS_TLL_TRE).json()
    c, user = signup()
    claimed = c.post("/quotes/claim", json={"token": pub["claim_token"]}).json()
    q = claimed["quote"]
    acc = c.post(f"/quotes/{claimed['id']}/accept", json={"option_id": q["options"][0]["id"]})
    assert acc.status_code == 200 and acc.json()["quote"]["status"] == "accepted"
    assert [b["code"] for b in acc.json()["booking_blockers"]] == ["kyc", "sanctions"]
    r = c.post("/bookings", json=booking_body(q))
    assert r.status_code == 409 and "/portal/company" in r.json()["detail"]
    assert mongo().companies.find_one({"id": user["company_id"]})["kyc_status"] == "not_started"
    assert not mongo().shipments.find_one({"quote_id": claimed["id"]})


BASE = {"origin": "Tallinn", "destination": "Tampere",
        "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2}}


def _with_cargo(**kw):
    return {**BASE, "cargo": {**BASE["cargo"], **kw}}


@pytest.mark.parametrize("body,needle", [
    (_with_cargo(weight_kg=-5), "weight"),
    (_with_cargo(weight_kg="lots"), "number"),
    (_with_cargo(weight_kg=1e9), "too large"),
    (_with_cargo(volume_m3="Infinity"), "too large"),
    (_with_cargo(value_cents=-100), "value"),
    ({"origin": "CNSHA", "destination": "FIHEL", "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 60}]}}, "50"),
    ({"origin": "CNSHA", "destination": "FIHEL", "cargo": {"load_type": "FCL", "containers": [{"type": "53HC", "count": 1}]}}, "53HC"),
    ({**BASE, "destination": "Tallinn"}, "same place"),
    ({**BASE, "ready_date": "2030-01-01"}, "6 months"),
    ({**BASE, "ready_date": "tomorrow"}, "YYYY-MM-DD"),
    ({**BASE, "origin": 12345}, "origin"),
    ({**BASE, "origin": "x" * 5000}, "origin"),
    ({**BASE, "origin": {"lat": 999, "lon": 5, "name": "x", "country": "EE"}}, "origin"),
    ({**BASE, "origin": {"lat": 59.9, "lon": 30.3, "name": "Piter", "country": "RU"}}, "Russia"),
    ({**BASE, "mode": "teleport"}, "Mode"),
])
def test_hostile_inputs_get_plain_400(body, needle):
    r = public_quote(body)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_soft_problems_become_warnings():
    past = public_quote({**BASE, "ready_date": "2020-01-01"}).json()["quote"]
    assert any(w["code"] == "ready_date.moved" for w in past["warnings"])
    odd = public_quote(_with_cargo(packages=1000)).json()["quote"]
    assert any(w["code"] == "cargo.check_weight" for w in odd["warnings"])
    assert all(o["service"] != "ROAD_LTL" for o in odd["options"])  # 400 ldm is not groupage
