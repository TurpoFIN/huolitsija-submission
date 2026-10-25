"""Fresh HTTP acceptance for booking cost and AP actuals. No bank actions."""
from test_s13_ap_support import ap_shipment, make_carrier, ops, db, COST_LINES
from test_s13_ap_invoices import test_finvoice_within_tolerance_autopilot_off_goes_to_owner_then_approval_approves


def test_missing_fx_remains_visible_and_recovers_without_duplicate():
    carrier = make_carrier()
    s = ap_shipment(carrier, [{**COST_LINES[0], "currency": "ZZZ"}])
    response = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]})
    assert response.status_code == 200, response.text
    row, = response.json()["items"]
    assert row["status"] == "no_booked_rate"
    assert row["expected_eur_cents"] is None
    assert "Valuuttakurssi puuttuu" in row["reason"]
    assert db.shipments.find_one({"id": s["id"]})["finance_cost_incomplete"]
    listed = ops().get("/finance/accruals", params={"shipment_id": s["id"]}).json()
    assert listed["items"][0]["id"] == row["id"]
    pnl = ops().get(f"/finance/reports/pnl/shipments/{s['id']}").json()
    assert pnl["cost_cents"] is None and pnl["gross_profit_cents"] is None
    db.quotes.update_one({"id": s["quote_id"]}, {"$set": {"options.0.lines.0.currency": "EUR"}})
    recovered = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]}).json()["items"]
    assert len(recovered) == 1 and recovered[0]["id"] == row["id"]
    assert recovered[0]["status"] == "open" and recovered[0]["expected_eur_cents"] == 375308
    assert not db.shipments.find_one({"id": s["id"]})["finance_cost_incomplete"]
