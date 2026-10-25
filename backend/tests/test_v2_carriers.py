"""Carriers and rate cards on the real server: seeded network, integration status from env, ops-only access,
rate CRUD with validation and audit, expiring filter, and carrier ranking for a real booked shipment leg."""
from datetime import date, timedelta

import pytest

from test_v2_support import v2_cleanup  # noqa: F401  (session teardown of test data)
from test_v2_support import PALLETS_TLL_TRE, booking_body, cust, mongo, ops, ready

EXPECTED = {"MAERSK", "MSC", "HAPAG", "CMACGM", "COSCO", "ONE", "UNIFEEDER", "CONTAINERSHIPS", "FINNLINES", "TALLINK", "ECKERO",
            "VIKING", "DFDS", "DSV", "SCHENKER", "KAUKOKIITO", "POSTI", "POSTNORD", "GIRTEKA", "FINNAIR", "LUFTHANSA", "DHLGF",
            "TURKISH", "QATAR", "VRTP", "RCG", "GREENCARGO"}


def test_carrier_network_is_seeded_with_integration_status():
    data = ops().get("/carriers").json()
    codes = {c["code"] for c in data["items"]}
    assert EXPECTED <= codes
    for c in data["items"]:
        if c["code"] in EXPECTED:
            integ = c["integration"]
            assert integ["type"] in ("dcsa", "maersk", "dhl", "partner_portal", "manual")
            assert integ["status"] == "not_configured", c["code"]  # no carrier keys in this environment
            assert all(e.startswith(f"CARRIER_{c['code']}_") for e in integ["env"])
            assert c["stats"]["rate_cards"] >= 1 or c["code"] in ("ECKERO",)
    maersk = next(c for c in data["items"] if c["code"] == "MAERSK")
    assert "CARRIER_MAERSK_API_KEY" in maersk["integration"]["env"]
    assert data["counts"]["sea"] >= 12 and data["counts"]["air"] >= 5


def test_carriers_and_rates_are_ops_only():
    assert cust().get("/carriers").status_code == 403
    assert cust().get("/rates").status_code == 403
    assert cust().post("/rates", json={}).status_code == 403


def test_rate_filters():
    air = ops().get("/rates", params={"mode": "air"}).json()
    assert air["total"] > 0 and all(r["mode"] == "air" for r in air["items"])
    expiring = ops().get("/rates", params={"state": "expiring"}).json()
    assert expiring["total"] >= 1
    for r in expiring["items"]:
        assert r["state"] == "expiring" and 0 <= r["days_left"] <= 21
    expired = ops().get("/rates", params={"state": "expired"}).json()
    assert all(r["valid_to"] < date.today().isoformat() for r in expired["items"])
    lane = ops().get("/rates", params={"q": "CNSHA"}).json()
    assert lane["total"] > 0 and all("CNSHA" in (r["lane"] + " ".join(r.get("via") or [])) or "CNSHA" in r.get("service_name", "")
                                     for r in lane["items"])
    sum_counts = sum(ops().get("/rates").json()["counts"].values())
    assert sum_counts == ops().get("/rates").json()["total"]


def test_rate_crud_validation_and_audit():
    finnlines = next(c for c in ops().get("/carriers").json()["items"] if c["code"] == "FINNLINES")
    today = date.today()
    body = {"carrier_id": finnlines["id"], "mode": "sea", "service": "RORO", "basis": "PER_SHIPMENT",
            "origin": {"unlocode": "FIHKO"}, "destination": {"unlocode": "SEKPS"}, "unit_cost_cents": 95000,
            "valid_from": today.isoformat(), "valid_to": (today + timedelta(days=60)).isoformat(), "transit_days": 1,
            "surcharges": [{"code": "BAF", "basis": "PERCENT", "pct": 12}], "source": "contract",
            "service_name": "TEST Hanko–Kapellskär trailer"}
    bad = ops().post("/rates", json={**body, "valid_to": (today - timedelta(days=1)).isoformat()})
    assert bad.status_code == 400 and "Valid to" in bad.json()["detail"]
    ru = ops().post("/rates", json={**body, "destination": {"unlocode": "RULED"}})
    assert ru.status_code == 400 and "Russia" in ru.json()["detail"]
    nosur = ops().post("/rates", json={**body, "surcharges": [{"basis": "PERCENT", "pct": 5}]})
    assert nosur.status_code == 400
    r = ops().post("/rates", json=body)
    assert r.status_code == 200, r.text
    rate = r.json()["rate"]
    try:
        assert rate["state"] == "valid" and rate["carrier_code"] == "FINNLINES" and rate["lane"]
        p = ops().patch(f"/rates/{rate['id']}", json={"unit_cost_cents": 99000})
        assert p.status_code == 200 and p.json()["rate"]["unit_cost_cents"] == 99000
        log = mongo().audit_log.find_one({"action": "rate_updated", "entity_id": rate["id"]})
        assert log and log["data"]["changes"]["unit_cost_cents"] == {"from": 95000, "to": 99000}
    finally:
        d = ops().delete(f"/rates/{rate['id']}")
    assert d.status_code == 200
    assert all(x["id"] != rate["id"] for x in ops().get("/rates").json()["items"])
    assert mongo().audit_log.find_one({"action": "rate_archived", "entity_id": rate["id"]})["data"]["snapshot"]["unit_cost_cents"] == 99000
    assert ops().patch(f"/rates/{rate['id']}", json={"unit_cost_cents": 1}).status_code == 404


def test_duplicate_carrier_code_rejected():
    r = ops().post("/carriers", json={"name": "Maersk again", "code": "MAERSK", "modes": ["sea"], "integration_type": "manual"})
    assert r.status_code == 409


def test_rank_carriers_for_a_booked_leg():
    r = cust().post("/quotes", json={**PALLETS_TLL_TRE, "ready_date": ready()})
    q = r.json()["quote"]
    b = cust().post("/bookings", json=booking_body(q))
    assert b.status_code == 200, b.text
    sid = b.json()["shipment_id"]
    shp = mongo().shipments.find_one({"id": sid})
    # V1 may split a ferry groupage leg into city -> harbour, crossing, harbour -> city; local hops are not ranked
    data = None
    for lg in shp["legs"]:
        if lg["role"] != "main":
            continue
        rank = ops().get("/carriers/rank", params={"shipment_id": sid, "leg_seq": lg["seq"]})
        assert rank.status_code == 200, rank.text
        if rank.json()["items"]:
            data = rank.json()
            break
        assert [w["code"] for w in rank.json()["warnings"]] == ["leg.local"], rank.json()
    assert data and data["items"], shp["legs"]
    assert not any(w["code"].startswith("incoterm.") for w in data["warnings"])
    assert sum(1 for i in data["items"] if i["recommended"]) == 1
    assert data["items"][0]["recommended"]  # sorted by score
    for i in data["items"]:
        assert i["cost_cents"] > 0 and i["transit_days"] >= 1 and i["co2e_kg"] > 0
    assert ops().get("/carriers/rank", params={"shipment_id": sid, "leg_seq": 99}).status_code == 404
    assert cust().get("/carriers/rank", params={"shipment_id": sid, "leg_seq": 1}).status_code == 403


@pytest.mark.parametrize("path", ["/carriers/doesnotexist", "/rates/doesnotexist"])
def test_unknown_ids_404(path):
    assert ops().get(path).status_code == 404
