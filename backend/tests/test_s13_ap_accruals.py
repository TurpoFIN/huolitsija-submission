"""s13 cost accruals: booked-rate costs per shipment + supplier, EUR at the ECB rate of the booking date.

Front-door: fixtures in the V2/V1 shapes, then POST /api/finance/accruals/rebuild and GET /api/finance/accruals.
Expected numbers are computed here independently with Decimal from the quote lines and fx_rates."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from test_s13_ap_support import COST_LINES, ap_shipment, db, expected_cost_cents, make_carrier, ops


def test_rebuild_accrues_booked_rate_exactly_once():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    r = ops().post("/finance/accruals/rebuild", json={})
    assert r.status_code == 200, r.text
    assert r.json()["processed"] >= 1
    rows = ops().get("/finance/accruals", params={"shipment_id": s["id"]}).json()["items"]
    assert len(rows) == 1, rows
    row = rows[0]
    want = expected_cost_cents(COST_LINES)
    assert want == 2 * 187_654 + 2 * 11_728 + 30_150  # duty (pass-through) and the third-party line are not our cost
    assert row["expected_cents"] == want and row["expected_eur_cents"] == want and row["currency"] == "EUR"
    assert row["status"] == "open" and row["basis"] == "booked_rate"
    assert row["supplier"] == {"carrier_id": carrier["id"], "name": carrier["name"], "kind": "carrier"}
    assert row["lane"]["key"] == "sea:CN>FI" and row["shipment_ref"] == s["reference"]
    assert row["period"] == s["milestones"][0]["at"][:7]
    assert sorted(ln["code"] for ln in row["lines"]) == ["DELIVERY", "OCEAN_FREIGHT", "THC_DEST"]
    # idempotent: rebuilding this shipment again updates the same row, never duplicates
    r2 = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]})
    assert r2.status_code == 200, r2.text
    assert db.cost_accruals.count_documents({"shipment_id": s["id"]}) == 1
    assert db.cost_accruals.find_one({"shipment_id": s["id"]})["id"] == row["id"]


def test_foreign_currency_line_converted_at_booking_date_ecb_rate():
    carrier = make_carrier("FI")
    lines = [dict(COST_LINES[0]), {**COST_LINES[2], "currency": "USD", "unit_cost_cents": 41_999}]
    s = ap_shipment(carrier, lines)
    rows = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]}).json()["items"]
    assert len(rows) == 1
    row = rows[0]
    booked = s["milestones"][0]["at"][:10]
    fx = db.fx_rates.find_one({"date": {"$lte": booked}, "rates.USD": {"$exists": True}}, sort=[("date", -1)])
    assert fx, "ECB rates for the booking date were not stored"
    usd_eur = (Decimal(41_999) / Decimal(str(fx["rates"]["USD"]))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    assert row["expected_eur_cents"] == 2 * 187_654 + int(usd_eur)
    assert row["currency"] == "EUR" and row["fx"]["rate_date"] == fx["date"]


def test_shipment_without_any_rate_is_flagged_not_invented():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier, [{**COST_LINES[3]}])  # only a zero-cost fee line on the booked option
    rows = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]}).json()["items"]
    assert len(rows) == 1
    assert rows[0]["status"] == "no_booked_rate" and rows[0]["expected_eur_cents"] is None and rows[0]["lines"] == []
    listing = ops().get("/finance/accruals", params={"status": "no_booked_rate", "q": s["reference"]}).json()
    assert listing["total"] == 1 and listing["counts"]["no_booked_rate"] >= 1


def test_manual_booking_without_quote_is_repriced_from_rate_cards_and_released_on_cancel():
    """shipment.created -> finance.accrue_shipment job -> V2 rate cards; cancel -> rows released."""
    import time

    from test_s13_ap_support import make_company

    company = make_company()
    req = {"mode": "road", "origin": {"unlocode": "DEHAM"}, "destination": {"unlocode": "FIHEL"},
           "cargo": {"weight_kg": 1800, "packages": 4, "package_type": "pallets", "volume_m3": 6}}
    r = ops().post("/public/quote", json=req)
    assert r.status_code == 200, r.text
    q = db.quotes.find_one({"id": r.json()["quote"]["id"]})
    by_carrier: dict = {}
    for o in q["options"]:
        by_carrier.setdefault(o["carrier_id"], []).append(o)
    carrier_id, opts = next((k, v) for k, v in by_carrier.items() if len(v) == 1)
    opt = opts[0]
    want = sum(Decimal(ln["unit_cost_cents"]) * Decimal(str(ln["qty"])) for ln in opt["lines"]
               if not ln.get("is_pass_through") and (ln.get("payer") or "").lower() != "third_party")
    body = {"company_id": company["id"], "mode": "road", "origin": "DEHAM", "destination": "FIHEL",
            "carrier_id": carrier_id, "carrier": opt["carrier_name"], "cargo": req["cargo"]}
    r = ops().post("/shipments", json=body)
    assert r.status_code == 200, r.text
    s = r.json()
    rows = []
    for _ in range(60):
        rows = list(db.cost_accruals.find({"shipment_id": s["id"]}, {"_id": 0}))
        if rows:
            break
        time.sleep(1)
    assert rows, "shipment.created did not accrue the shipment"
    assert {x["basis"] for x in rows} == {"repriced_rate_card"} and all(x["status"] == "open" for x in rows)
    assert sum(x["expected_eur_cents"] for x in rows) == int(want.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    assert {x["supplier"]["carrier_id"] for x in rows} <= {lg["carrier_id"] for lg in opt["legs"]}
    cards = {c for x in rows for c in x["rate_card_ids"]}
    assert cards and db.rate_cards.count_documents({"id": {"$in": sorted(cards)}}) == len(cards)
    r = ops().post(f"/shipments/{s['id']}/cancel", json={"reason": "customer cancelled"})
    assert r.status_code == 200, r.text
    assert {x["status"] for x in db.cost_accruals.find({"shipment_id": s["id"]})} == {"released"}
