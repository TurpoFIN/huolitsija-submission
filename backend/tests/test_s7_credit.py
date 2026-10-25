"""s7 credit against the real local API: position math from real invoices, booking blocked over the limit, hold on
issue and release on payment, the free credit check (PRH/YTJ, PRH XBRL) on real Business IDs, Autopilot on/off.

Register answers: the CI backend replays recorded real PRH/YTJ responses (tests/fixtures/registers, core/recorded_http.py,
set by scripts/run-ci-backend.sh), so these tests do not depend on PRH being reachable. Exactly one test calls the live
registers: test_live_registers_match_the_recordings, opt-in with S7_LIVE=1.

Env: V7_API, V7_DB, DEMO_OPS_PASSWORD (see test_s7_credit_support.py), S7_LIVE.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_s7_credit_support import (booking_body, db, manual_invoice, ops, pay, position, quote,  # noqa: E402
                                    set_limit, signup, wait_for, autopilot)

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
KESKO, BANKRUPT = "0109862-8", "0502119-3"  # Kesko Oyj (listed); Kuljetusliike Velj. Aaltonen Oy (bankruptcy 2025-12-16)


def vat_total(net: int) -> int:
    from decimal import ROUND_HALF_UP, Decimal
    return net + int((Decimal(net) * Decimal("0.255")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------------------------------------ position
def test_position_math_from_real_invoices_partial_payment_and_overdue():
    _, user = signup()
    cid = user["company_id"]
    set_limit(cid, 2_000_000)
    p0 = position(cid)
    assert p0["limit_cents"] == 2_000_000 and p0["limit_basis"] == "set"
    assert p0["open_ar_cents"] == 0 and p0["booked_uninvoiced_cents"] == 0 and p0["available_cents"] == 2_000_000

    a = manual_invoice(cid, 100_000)
    b = manual_invoice(cid, 40_000)
    assert a["total_cents"] == vat_total(100_000) and b["status"] == "open"
    draft = manual_invoice(cid, 999_00, issue=False)  # a draft is not a receivable
    p1 = position(cid)
    assert p1["open_ar_cents"] == a["total_cents"] + b["total_cents"]
    assert p1["available_cents"] == 2_000_000 - p1["open_ar_cents"]
    assert p1["overdue_cents"] == 0 and p1["open_invoices"] == 2 and draft["status"] == "draft"

    pay(a["id"], 50_000)  # partial
    p2 = position(cid)
    assert p2["open_ar_cents"] == a["total_cents"] - 50_000 + b["total_cents"]

    past = (date.today() - timedelta(days=12)).isoformat()
    db.invoices.update_one({"id": b["id"]}, {"$set": {"due_date": past, "status": "overdue"}})
    p3 = position(cid)
    assert p3["overdue_cents"] == b["total_cents"]
    assert 11 <= p3["max_days_overdue"] <= 13
    assert p3["status"] == "ok"  # 12 days < credit_hold_overdue_days (30), well inside the limit

    pay(b["id"])
    pay(a["id"])
    p4 = position(cid)
    assert p4["open_ar_cents"] == 0 and p4["overdue_cents"] == 0


def test_seeded_shipment_moves_from_booked_uninvoiced_to_open_ar_when_invoiced():
    """A seeded shipment is exposure while uninvoiced; delivering and invoicing it through the real APIs moves it to AR."""
    shp = None
    for s in db.shipments.find({"status": {"$in": ["out_for_delivery", "at_hub", "in_transit"]}, "quote_id": {"$ne": None},
                                "price.total_cents": {"$gt": 0}}, {"_id": 0}):
        if not db.invoices.find_one({"shipment_id": s["id"]}):
            shp = s
            break
    if not shp:
        pytest.skip("no uninvoiced seeded shipment left in this database")
    cid = shp["company_id"]
    before = position(cid)
    r = ops().post(f"/shipments/{shp['id']}/status", json={"status": "delivered", "note": "s7 credit test"})
    assert r.status_code == 200, r.text
    assert position(cid)["booked_uninvoiced_cents"] == before["booked_uninvoiced_cents"]  # a draft is not yet invoiced
    r = ops().post(f"/invoices/from-shipment/{shp['id']}")
    assert r.status_code == 200, r.text
    inv = r.json()
    if inv["status"] == "draft":
        r = ops().post(f"/invoices/{inv['id']}/issue", json={"send": False})
        assert r.status_code == 200, r.text
        inv = r.json()
    after = position(cid)
    assert after["booked_uninvoiced_cents"] == before["booked_uninvoiced_cents"] - shp["price"]["total_cents"]
    assert after["open_ar_cents"] == before["open_ar_cents"] + inv["total_cents"]
    assert after["exposure_cents"] == after["open_ar_cents"] + after["booked_uninvoiced_cents"]


# ------------------------------------------------------------------------------------------------ booking
def test_booking_blocked_over_limit_then_allowed_after_payment():
    c, user = signup()
    cid = user["company_id"]
    q = quote(c)
    total = q["options"][0]["total_cents"]
    assert total > 20_000
    set_limit(cid, total + 20_000)
    inv = manual_invoice(cid, 20_000)  # 251 EUR open AR > 200 EUR headroom, below the limit
    assert position(cid)["available_cents"] < total

    got = c.get(f"/quotes/{q['id']}").json()["quote"]["booking_blockers"]
    blk = next(b for b in got if b["code"] == "credit")
    assert "€" in blk["message"] and "€" in blk["message_fi"] and "luotto" in blk["message_fi"].lower()
    assert blk["available_cents"] == total + 20_000 - inv["total_cents"]
    r = c.post("/bookings", json=booking_body(q))
    assert r.status_code == 409 and "available credit" in r.json()["detail"]
    assert not db.shipments.find_one({"quote_id": q["id"]})

    pay(inv["id"])
    r = c.post("/bookings", json=booking_body(q))
    assert r.status_code == 200, r.text
    shp = db.shipments.find_one({"id": r.json()["shipment_id"]}, {"_id": 0})
    p = position(cid)
    assert p["booked_uninvoiced_cents"] == shp["price"]["total_cents"] == total
    assert p["available_cents"] == 20_000

    # a second quote of the same size no longer fits in the remaining 200 EUR
    q2 = quote(c)
    r = c.post("/bookings", json=booking_body(q2))
    assert r.status_code == 409 and "available credit" in r.json()["detail"]


def test_issue_invoice_over_limit_sets_hold_and_payment_releases_it():
    c, user = signup()
    cid = user["company_id"]
    set_limit(cid, 100_000)
    q = quote(c)
    inv = manual_invoice(cid, 120_000)  # invoicing itself is never blocked
    assert inv["status"] == "open" and inv["number"]
    comp = db.companies.find_one({"id": cid})
    assert comp["credit_hold"] is True and comp["credit_status"] == "hold" and comp["credit_hold_auto"] is True
    assert "exceed the credit limit" in comp["credit_hold_reason"]
    assert db.audit_log.find_one({"entity_id": cid, "action": "credit_hold_set"})
    note = db.notifications.find_one({"link": f"/ops/customers/{cid}", "title_i18n.fi": {"$regex": "^Luottoesto"}})
    assert note and "€" in note["body_i18n"]["fi"]
    assert db.notifications.find_one({"company_id": cid, "title_i18n.en": "New bookings paused"})
    blk = next(b for b in c.get(f"/quotes/{q['id']}").json()["quote"]["booking_blockers"] if b["code"] == "credit")
    assert "credit hold" in blk["message"]
    assert c.post("/bookings", json=booking_body(q)).status_code == 409

    pay(inv["id"])  # invoice.paid -> automatic hold released
    comp = db.companies.find_one({"id": cid})
    assert comp["credit_hold"] is False and comp["credit_status"] != "hold"
    assert db.audit_log.find_one({"entity_id": cid, "action": "credit_hold_released"})


def test_overdue_beyond_policy_days_sets_hold_on_next_issue_and_manual_hold_is_not_auto_released():
    _, user = signup()
    cid = user["company_id"]
    set_limit(cid, 5_000_000)
    old = manual_invoice(cid, 10_000)
    db.invoices.update_one({"id": old["id"]}, {"$set": {"due_date": (date.today() - timedelta(days=45)).isoformat(),
                                                        "status": "overdue"}})
    manual_invoice(cid, 10_000)
    comp = db.companies.find_one({"id": cid})
    assert comp["credit_hold"] is True and "days overdue" in comp["credit_hold_reason"]
    pay(old["id"])
    assert db.companies.find_one({"id": cid})["credit_hold"] is False

    r = ops().patch(f"/credit/{cid}", json={"hold": True, "reason": "Asiakkaan pyyntö / customer request"})
    assert r.status_code == 200 and r.json()["position"]["status"] == "hold"
    inv = manual_invoice(cid, 5_000)
    pay(inv["id"])
    assert db.companies.find_one({"id": cid})["credit_hold"] is True  # a person's hold stays until a person releases it
    r = ops().patch(f"/credit/{cid}", json={"hold": False})
    assert r.status_code == 200 and r.json()["position"]["hold"] is False


# ------------------------------------------------------------------------------------------------ free credit check
def _check(cid: str) -> dict:
    r = ops().post(f"/credit/{cid}/check")
    assert r.status_code == 200, r.text
    return r.json()["check"]


def test_free_check_listed_company_vs_company_in_bankruptcy():
    """PRH/YTJ open data v3 answers for Kesko Oyj and a company in bankruptcy; different ratings, traceable reasons."""
    _, u1 = signup("Kesko Oyj", business_id=KESKO)
    _, u2 = signup("Kuljetusliike Velj. Aaltonen Oy", business_id=BANKRUPT)
    good, bad = _check(u1["company_id"]), _check(u2["company_id"])
    assert good["status"] == "ok" and bad["status"] == "ok", (good.get("sources"), bad.get("sources"))
    assert good["name"] == "Kesko Oyj" and good["rating"] in ("A", "B") and good["recommended_limit_cents"] > 0
    assert bad["rating"] == "decline" and bad["recommended_limit_cents"] == 0
    bk = next(r for r in bad["reasons"] if r["code"] == "situation_konk")
    assert "konkurssi" in bk["fi"] and "bankruptcy" in bk["en"]
    assert bk["url"] == f"https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId={BANKRUPT}"
    for chk in (good, bad):
        ytj = next(s for s in chk["sources"] if s["name"] == "ytj")
        assert ytj["status"] == "ok" and ytj["url"].startswith("https://avoindata.prh.fi/")
        assert all(r["fi"] and r["en"] for r in chk["reasons"])
    stored = db.credit_checks.find_one({"id": bad["id"]})
    assert stored and stored["company_id"] == u2["company_id"]
    assert db.companies.find_one({"id": u2["company_id"]})["credit_last_check"]["rating"] == "decline"


def test_scoring_on_captured_ytj_responses():
    """Offline: the scoring rules on real YTJ responses captured into fixtures/s7_credit_ytj.json."""
    from adapters import credit_bureau as cb
    fx = json.load(open(os.path.join(FIXTURES, "s7_credit_ytj.json"), encoding="utf-8"))
    out = {}
    for k, case in fx["cases"].items():
        y = cb.parse_ytj(case["response"], case["business_id"], case["url"])
        reasons, hard = cb.ytj_reasons(y, today=date(2026, 10, 24))
        score = max(0, min(100, 100 + sum(r["points"] for r in reasons)))
        out[k] = (cb.rating_for(score, hard), {r["code"] for r in reasons})
    assert out["kesko"][0] == "A" and "public_company" in out["kesko"][1]
    assert out["bankruptcy"][0] == "decline" and "situation_konk" in out["bankruptcy"][1]
    assert out["liquidation"][0] == "decline" and "situation_seltila" in out["liquidation"][1]
    assert out["restructuring"][0] == "D" and "situation_sane" in out["restructuring"][1]


# ------------------------------------------------------------------------------------------------ autonomy
def test_autopilot_off_creates_approval_card_on_sets_limit_with_audit():
    _, user = signup("Kesko Oyj", business_id=KESKO)
    cid = user["company_id"]
    autopilot(False)
    chk = _check(cid)
    assert chk["outcome"] == "approval_requested" and chk["approval_id"]
    card = db.approvals.find_one({"id": chk["approval_id"]}, {"_id": 0})
    assert card["kind"] == "credit_limit" and card["status"] == "pending"
    assert card["amount_cents"] == chk["recommended_limit_cents"] > 0 and card["title_fi"].startswith("Luottoraja")
    assert db.companies.find_one({"id": cid}).get("credit_limit_cents") == 0
    # no limit decided yet -> a booking is blocked with the "awaiting approval" reason
    r = ops().post(f"/approvals/{card['id']}/decide", json={"decision": "approved", "note": "ok",
                                                            "data": {"limit_cents": 1_500_000}})
    assert r.status_code == 200, r.text
    comp = db.companies.find_one({"id": cid})
    assert comp["credit_limit_cents"] == 1_500_000 and comp["credit_limit_source"]["via"] == "approval"

    _, user2 = signup("Kesko Oyj", business_id=KESKO)
    cid2 = user2["company_id"]
    autopilot(True)
    try:
        chk2 = _check(cid2)
    finally:
        autopilot(False)
    assert chk2["outcome"] == "limit_set", chk2
    comp2 = db.companies.find_one({"id": cid2})
    assert comp2["credit_limit_cents"] == chk2["recommended_limit_cents"] > 0
    assert comp2["credit_limit_source"]["via"] == "autopilot" and comp2["credit_limit_source"]["by"]["id"] == "credit"
    assert db.audit_log.find_one({"entity_id": cid2, "action": "credit_limit_set", "actor.id": "credit"})
    trail = db.autonomy_actions.find_one({"entity_id": cid2, "kind": "credit_limit_set"}) or \
        db.agent_decisions.find_one({"subject_id": cid2, "action": "set_credit_limit"})
    assert trail
    assert not db.approvals.find_one({"kind": "credit_limit", "ref_id": cid2, "status": "pending"})


def test_autopilot_halted_still_asks_a_person():
    _, user = signup("Kesko Oyj", business_id=KESKO)
    cid = user["company_id"]
    autopilot(True)
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": True}})
    try:
        chk = _check(cid)
    finally:
        db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": False}})
        autopilot(False)
    assert chk["outcome"] == "approval_requested"
    assert db.companies.find_one({"id": cid}).get("credit_limit_cents") == 0


def test_unassessed_customer_first_booking_runs_check_and_waits_for_approval():
    c, user = signup("Kesko Oyj", business_id=KESKO)
    cid = user["company_id"]
    autopilot(False)
    q = quote(c)
    blk = next(b for b in c.get(f"/quotes/{q['id']}").json()["quote"]["booking_blockers"] if b["code"] == "credit")
    assert "waiting for approval" in blk["message"] and "hyväksyntää" in blk["message_fi"]
    assert db.credit_checks.find_one({"company_id": cid, "reason": "booking"})
    assert db.approvals.find_one({"kind": "credit_limit", "ref_id": cid, "status": "pending"})


def test_kyc_approval_runs_a_credit_check():
    _, user = signup("Kesko Oyj", business_id=KESKO, kyc_status="in_review")
    cid = user["company_id"]
    r = ops().post(f"/ops/customers/{cid}/kyc-decision", json={"decision": "approved", "note": "s7 credit test"})
    assert r.status_code == 200, r.text
    chk = wait_for(lambda: db.credit_checks.find_one({"company_id": cid, "reason": "kyc_approved"}), timeout=60)
    assert chk and chk["status"] == "ok" and chk["rating"] in ("A", "B")


# ------------------------------------------------------------------------------------------------ API scope
def test_portal_credit_own_position_and_ops_only_routes():
    c, user = signup()
    cid = user["company_id"]
    set_limit(cid, 300_000)
    r = c.get("/portal/credit")
    assert r.status_code == 200, r.text
    pos = r.json()["position"]
    assert pos["limit_cents"] == 300_000 and pos["available_cents"] == 300_000 and "last_check" not in pos
    assert c.get(f"/credit/{cid}").status_code == 403
    assert c.post(f"/credit/{cid}/check").status_code == 403
    allp = ops().get("/credit")
    assert allp.status_code == 200 and any(p["company_id"] == cid for p in allp.json()["items"])
    prov = ops().get("/credit/providers").json()
    assert prov["credit_bureau"] in ("none", "creditsafe", "asiakastieto") and prov["free"][0]["name"] == "ytj"


# ---------------------------------------------------------------------------------------------------------------
# PRH digital financial statements (XBRL open data v3)
# ---------------------------------------------------------------------------------------------------------------
def test_xbrl_parse_captured_filing():
    from pathlib import Path
    from adapters import credit_bureau as cb
    xml = (Path(__file__).parent / "fixtures" / "s7_credit_xbrl_0291577-3.xml").read_text()
    f = cb.parse_xbrl(xml, "2025-07-31")
    # values as filed (SBR codes x740 net profit, x689 operating profit, x360 assets, x399 cash), and the prior year
    assert f["net_profit"] == 6417730 and f["net_profit_prev"] == 6075242
    assert f["operating_profit"] == 7912290 and f["assets"] == 35627433 and f["cash"] == 20945925
    reasons, hard = cb.financial_reasons(f, "u")
    assert {r["code"] for r in reasons} == {"profit", "cash_strong"} and not hard
    loss = cb.financial_reasons({"financial_date": "2025-12-31", "net_profit": -100_00, "net_profit_prev": -50_00,
                                 "equity": -1_000_00}, "u")[0]
    assert {r["code"]: r["points"] for r in loss} == {"loss": -15, "negative_equity": -25}


REGISTERS = os.path.join(FIXTURES, "registers")


def test_xbrl_financials_from_recorded_prh_answers(monkeypatch):
    import asyncio
    from adapters import credit_bureau as cb
    monkeypatch.setenv("REGISTER_RECORDINGS", REGISTERS)
    monkeypatch.setenv("REGISTER_RECORDINGS_STRICT", "1")  # an unrecorded request fails instead of going live
    monkeypatch.delenv("REGISTER_RECORD", raising=False)
    res = asyncio.run(cb.financials("0291577-3"))
    assert res["status"] == "ok" and res["financial_date"] >= "2025-07-31", res
    assert res["url"].startswith("https://avoindata.prh.fi/opendata-xbrl-api/v3/financial?businessId=0291577-3")
    assert any(r["code"] in ("profit", "loss") for r in res["reasons"])
    assert asyncio.run(cb.financials("0109862-8"))["status"] == "none_filed"
    kesko, bad = asyncio.run(cb.free_check(KESKO, {})), asyncio.run(cb.free_check(BANKRUPT, {}))
    assert kesko["status"] == "ok" and kesko["rating"] in ("A", "B") and bad["rating"] == "decline"


@pytest.mark.skipif(os.environ.get("S7_LIVE") != "1", reason="opt-in live check against PRH/YTJ: set S7_LIVE=1")
def test_live_registers_match_the_recordings(monkeypatch):
    """The one live check: the same three Business IDs against the live PRH registers give the recorded outcome."""
    import asyncio
    from adapters import credit_bureau as cb
    monkeypatch.delenv("REGISTER_RECORDINGS", raising=False)
    kesko, bad = asyncio.run(cb.free_check(KESKO, {})), asyncio.run(cb.free_check(BANKRUPT, {}))
    assert kesko["status"] == "ok" and kesko["name"] == "Kesko Oyj" and kesko["rating"] in ("A", "B")
    assert bad["rating"] == "decline" and any(r["code"] == "situation_konk" for r in bad["reasons"])
    res = asyncio.run(cb.financials("0291577-3"))
    assert res["status"] == "ok" and res["financial_date"] >= "2025-07-31"
