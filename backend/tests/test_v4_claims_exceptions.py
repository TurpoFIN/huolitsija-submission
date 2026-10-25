"""Liability caps with the live IMF SDR rate, claim decision via approval, exception rules + dedupe + auto-clear."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

import httpx
import pytest

from test_v4_support import customer, customer_company_id, db, iso, make_company, make_shipment, now, ops, wait_for


def _today() -> date:
    """The server validates claim dates against the Helsinki date; between 00:00 and 03:00 that is ahead of UTC."""
    return datetime.now(ZoneInfo("Europe/Helsinki")).date()


@pytest.fixture(scope="module")
def sdr():
    r = ops().get("/claims/sdr-rate")
    if r.status_code == 503 and "could not be fetched from the IMF" in r.text:
        pytest.skip("IMF SDR source unreachable and no stored rate in this fresh database")
    assert r.status_code == 200, r.text
    return r.json()


def test_sdr_rate_matches_imf_source(sdr):
    r = httpx.get("https://www.imf.org/external/np/fin/data/rms_five.aspx?tsvflag=Y", timeout=30)
    if r.status_code != 200 or "\nCurrency units per SDR" not in r.text:
        pytest.skip(f"IMF source unavailable (HTTP {r.status_code}); cannot compare")
    tsv = r.text
    section = tsv[tsv.index("\nCurrency units per SDR"):]  # the title line also contains the phrase
    euro = next(ln for ln in section.splitlines() if ln.startswith("Euro\t"))
    values = [v for v in euro.split("\t")[1:] if v.strip()]
    assert abs(float(values[0]) - sdr["eur_per_sdr"]) < 1e-9 or sdr.get("stale")
    assert 1.0 < sdr["eur_per_sdr"] < 1.5 and date.fromisoformat(sdr["date"]) <= _today()


def cap_eur(cap_sdr: float, rate: float) -> int:
    return int(Decimal(str(cap_sdr)) * Decimal(str(rate)) * 100)


@pytest.mark.parametrize("regime, mode, role, kg, pkgs, ctype, expect_sdr", [
    ("NSAB", "road", "principal", 1200, 10, "damage", 8.33 * 1200),
    ("CMR", "road", "principal", 830.5, 4, "loss", 8.33 * 830.5),
    ("MONTREAL", "air", "principal", 150, 3, "damage", 26 * 150),          # ICAO 2024 revision, from 28 Dec 2024
    ("HAGUE_VISBY", "sea", "principal", 2000, 12, "damage", max(667 * 12, 2 * 2000)),   # package limit wins
    ("HAGUE_VISBY", "sea", "principal", 9000, 1, "damage", max(667 * 1, 2 * 9000)),     # weight limit wins
    ("CIM", "rail", "principal", 400, 2, "damage", 17 * 400),
    ("CMR", "road", "intermediary", 5000, 10, "damage", 50000),
])
def test_liability_cap_math(sdr, regime, mode, role, kg, pkgs, ctype, expect_sdr):
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode=mode, regime=regime, role=role,
                      weight_kg=max(kg, 10000), delivered_days_ago=3)
    r = customer().get("/claims/estimate", params={"shipment_id": s["id"], "type": ctype, "gross_kg": kg, "packages": pkgs})
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["regime"] == regime
    assert est["cap_sdr"] == pytest.approx(round(expect_sdr, 2), abs=0.01)
    assert est["cap_eur_cents"] == cap_eur(est["cap_sdr"], est["sdr_rate"]["eur_per_sdr"])
    delivered = date.fromisoformat(est["delivery_date"])
    assert est["time_bar_date"] == delivered.replace(year=delivered.year + 1).isoformat()
    if regime == "MONTREAL":
        assert est["regime_time_bar_date"] == delivered.replace(year=delivered.year + 2).isoformat()


def test_delay_cap_is_freight_for_nsab():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode="road", regime="NSAB",
                      delivered_days_ago=2, origin=("FITKU", "Turku", "FI", 60.45, 22.26))
    est = customer().get("/claims/estimate", params={"shipment_id": s["id"], "type": "delay"}).json()
    assert est["cap_sdr"] is None and est["cap_eur_cents"] == s["price"]["total_cents"]


def test_claim_flow_needs_approval_with_claims_permission():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode="road", regime="CMR",
                      delivered_days_ago=2, origin=("DEHAM", "Hamburg", "DE", 53.55, 9.99))
    bad = customer().post("/claims", json={"shipment_id": s["id"], "type": "damage", "description": "Rikki meni kaikki.",
                                           "amount_claimed_cents": 100000, "gross_kg_affected": 999999})
    assert bad.status_code == 400 and "gross weight" in bad.json()["detail"]
    r = customer().post("/claims", json={"shipment_id": s["id"], "type": "damage", "submit": True,
                                         "description": "Kaksi lavaa kaatui, 14 laatikkoa rikki. Merkitty CMR-rahtikirjaan.",
                                         "amount_claimed_cents": 250000, "gross_kg_affected": 640, "packages_affected": 14})
    assert r.status_code == 200, r.text
    cl = r.json()
    assert cl["status"] == "submitted" and cl["number"].startswith("HUO-CLM-") and cl["notice_late"] is False
    assert cl["cap_sdr"] == pytest.approx(8.33 * 640, abs=0.01)
    assert ops().post(f"/claims/{cl['id']}/review").json()["status"] == "in_review"
    over = ops().post(f"/claims/{cl['id']}/propose", json={"decision": "approve", "amount_cents": 300000, "note": "x yli"})
    assert over.status_code == 400
    p = ops().post(f"/claims/{cl['id']}/propose", json={"decision": "approve", "amount_cents": 180000,
                                                       "note": "Vaurio todettu CMR-merkinnöistä"}).json()
    assert p["status"] == "in_review" and p["approval_id"]
    appr = db.approvals.find_one({"id": p["approval_id"]})
    assert appr["kind"] == "claim" and appr["status"] == "pending"
    # Customer still sees in_review; no decision leaked.
    assert customer().get(f"/claims/{cl['id']}").json()["status"] == "in_review"
    # the proposer is the only user with claims.approve: four-eyes needs an audited override
    d = ops().post(f"/approvals/{p['approval_id']}/decide", json={"decision": "approved", "note": "OK",
                                                                 "data": {"amount_cents": 175000}})
    assert d.status_code == 409 and d.json()["code"] == "four_eyes_single_user", d.text
    d = ops().post(f"/approvals/{p['approval_id']}/decide", json={"decision": "approved", "override": True,
                                                                 "note": "Ainoa käyttäjä, jolla on oikeus (testi)",
                                                                 "data": {"amount_cents": 175000}})
    assert d.status_code == 200, d.text
    got = customer().get(f"/claims/{cl['id']}").json()
    assert got["status"] == "approved" and got["decision"]["amount_cents"] == 175000
    assert "proposal" not in got


def test_late_notice_is_flagged():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode="road", regime="NSAB",
                      delivered_days_ago=12, origin=("FIOUL", "Oulu", "FI", 65.0, 25.47))
    cl = customer().post("/claims", json={"shipment_id": s["id"], "type": "damage", "submit": True,
                                          "description": "Piilevä vaurio löytyi purettaessa.", "amount_claimed_cents": 50000,
                                          "gross_kg_affected": 50}).json()
    assert cl["notice_late"] is True and cl["notice_source"] == "PSYM 2015 § 27"


def test_customs_hold_rule_dedupes_and_clears():
    company = make_company()
    since = iso(now() - timedelta(hours=60))
    s = make_shipment(company, status="customs_hold", extra={
        "holds": [{"type": "customs", "reason": "Tulli pyysi alkuperäselvityksen", "since": since}]})
    assert ops().post("/exceptions/detect").status_code == 200
    ops().post("/exceptions/detect")
    exc = list(db.exceptions.find({"shipment_id": s["id"], "type": "customs_hold"}))
    assert len(exc) == 1 and exc[0]["severity"] == "high" and exc[0]["detected_by"] == "rule"
    assert db.shipments.find_one({"id": s["id"]})["has_exception"] is True
    listed = ops().get("/exceptions", params={"shipment_id": s["id"]}).json()
    assert listed["items"][0]["id"] == exc[0]["id"]
    db.shipments.update_one({"id": s["id"]}, {"$set": {"status": "at_hub", "holds": []}})
    # Off: facts are detected, but autonomous resolution needs the explicit switch.
    ops().post("/exceptions/detect")
    assert db.exceptions.find_one({"id": exc[0]["id"]})["status"] != "resolved"
    assert ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": True, "confirmation": "enable"}).status_code == 200
    try:
        ops().post("/exceptions/detect")
    finally:
        ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": False})
    got = db.exceptions.find_one({"id": exc[0]["id"]})
    assert got["status"] == "resolved" and got["resolution"].startswith("Cleared automatically")
    still_open = db.exceptions.count_documents({"shipment_id": s["id"], "status": {"$in": ["open", "in_progress"]}})
    assert db.shipments.find_one({"id": s["id"]})["has_exception"] is (still_open > 0)
    assert not db.exceptions.find_one({"shipment_id": s["id"], "type": "customs_hold", "status": {"$ne": "resolved"}})


def test_eta_slip_and_missing_docs_rules():
    company = make_company()
    planned = now() + timedelta(hours=6)
    s = make_shipment(company, status="in_transit", extra={"eta": iso(planned + timedelta(hours=50)), "eta_planned": iso(planned)})
    ops().post("/exceptions/detect")
    types = {e["type"]: e for e in db.exceptions.find({"shipment_id": s["id"]})}
    assert "delay" in types and types["delay"]["evidence"]["slip_hours"] == pytest.approx(50, abs=0.2)
    assert "missing_docs" in types and "commercial_invoice" in " ".join(types["missing_docs"]["evidence"]["missing"])


def test_manual_exception_resolve_with_customer_message():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, status="in_transit")
    r = ops().post("/exceptions", json={"shipment_id": s["id"], "type": "address_issue", "severity": "medium",
                                        "title": "Toimitusosoite puuttuu", "description": "Vastaanottajan osoite epäselvä"})
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["owner"] and ex["sla_due"]
    seen = customer().get("/exceptions").json()["items"]
    mine = next(x for x in seen if x["id"] == ex["id"])
    assert "owner" not in mine and "ai_assessment" not in mine
    done = ops().post(f"/exceptions/{ex['id']}/resolve", json={"resolution": "Osoite vahvistettu puhelimitse",
                                                              "customer_message": "Osoite on nyt vahvistettu, kiitos!"}).json()
    assert done["status"] == "resolved" and done["customer_message"]["status"] == "sent"
    assert customer().post("/exceptions", json={"shipment_id": s["id"], "type": "other", "title": "abc"}).status_code == 403


def test_claim_dates_and_numbers_are_validated():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, status="delivered", delivered_days_ago=2)
    base = {"shipment_id": s["id"], "type": "damage", "description": "Kaksi kollia rikki vastaanotossa, kuvat liitteenä.",
            "amount_claimed_cents": 50000, "gross_kg_affected": 40, "packages_affected": 2}
    future = customer().post("/claims", json={**base, "date_of_loss": (datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=3)).isoformat()})
    assert future.status_code == 400 and "future" in future.json()["detail"]
    before = customer().post("/claims", json={**base, "date_of_loss": (datetime.now(ZoneInfo("Europe/Helsinki")).date() - timedelta(days=400)).isoformat()})
    assert before.status_code == 400 and "booked" in before.json()["detail"]
    garbled = customer().post("/claims", json={**base, "date_of_loss": "eilen"})
    assert garbled.status_code == 400
    heavy = customer().post("/claims", json={**base, "gross_kg_affected": 10**7})
    assert heavy.status_code == 400 and "gross weight" in heavy.json()["detail"]
    ok = customer().post("/claims", json={**base, "date_of_loss": (datetime.now(ZoneInfo("Europe/Helsinki")).date() - timedelta(days=2)).isoformat()})
    assert ok.status_code == 200, ok.text
    edit = customer().patch(f"/claims/{ok.json()['id']}", json={"date_of_loss": (datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=1)).isoformat()})
    assert edit.status_code == 400


def test_notice_on_delivery_receipt_is_not_late():
    """Damage remarked on the delivery receipt at delivery is timely notice even if the claim is filed 12 days later."""
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode="road", regime="NSAB",
                      delivered_days_ago=12, origin=("FIOUL", "Oulu", "FI", 65.0, 25.47))
    delivered = (datetime.now(ZoneInfo("Europe/Helsinki")).date() - timedelta(days=12)).isoformat()
    body = {"shipment_id": s["id"], "type": "damage", "submit": True, "description": "Kolhiintunut lava, merkintä rahtikirjaan.",
            "amount_claimed_cents": 30000, "gross_kg_affected": 40}
    timely = customer().post("/claims", json={**body, "notice_given_on": delivered})
    assert timely.status_code == 200, timely.text
    assert timely.json()["notice_late"] is False and timely.json()["notified_at"].startswith(delivered)
    future = customer().post("/claims", json={**body, "notice_given_on": (datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=1)).isoformat()})
    assert future.status_code == 400
