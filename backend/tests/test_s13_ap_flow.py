"""Regression: incomplete costs must remain missing through the real finance API."""
from test_s13_support import db, make_company, make_shipment, ops, customer

def test_missing_purchase_price_does_not_become_zero_profit_cost():
    c = make_company()
    s = make_shipment(c, delivered_days_ago=1)
    q = db.quotes.find_one({"id": s["quote_id"]})
    for option in q["options"]:
        for line in option.get("lines", []):
            line.pop("cost_cents", None)
            line.pop("unit_cost_cents", None)
    db.quotes.update_one({"id": q["id"]}, {"$set": {"options": q["options"]}})
    result = ops().post("/finance/accruals/rebuild", json={"shipment_id": s["id"]})
    assert result.status_code == 200, result.text
    row = ops().get("/finance/reports/pnl/shipments/" + s["id"]).json()
    assert row["cost_complete"] is False
    assert row["cost_cents"] is None and row["gross_profit_cents"] is None and row["margin_pct"] is None
    assert "Purchase cost missing: margin unavailable" in row["warnings"]
    total = ops().get("/finance/reports/pnl/shipments", params={"company_id": c["id"]}).json()["totals"]
    assert total["cost_complete"] is False and total["gross_profit_cents"] is None
    assert customer().get("/finance/payables").status_code == 403
