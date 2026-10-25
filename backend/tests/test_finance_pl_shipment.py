"""Missing data, settlement basis and quote boundaries through real shipment P&L API."""
from test_s13_support import db, make_company, make_shipment, ops
from test_s13_reports_pnl import issue_invoice, accrual, get_row


def test_missing_actual_is_not_zero_and_open_zero_is_not_actual():
    s=make_shipment(make_company())
    issue_invoice(s)
    a=accrual(s,status="matched",expected=220000,actual=0)
    db.cost_accruals.update_one({"id":a["id"]},{"$unset":{"actual_eur_cents":""}})
    r=get_row(s)
    assert not r["cost_complete"] and r["gross_profit_cents"] is None
    assert r["lines"]["accruals"][0]["actual_eur_cents"] is None
    db.cost_accruals.update_one({"id":a["id"]},{"$set":{"status":"open","expected_eur_cents":0}})
    r=get_row(s)
    assert r["basis"]=="partly_actual" and r["cost_cents"]==0


def test_missing_revenue_and_fx_are_not_profit_values():
    s=make_shipment(make_company())
    accrual(s,status="open",expected=220000)
    db.shipments.update_one({"id":s["id"]},{"$unset":{"quote_id":"","option_id":""}})
    r=get_row(s)
    assert r["revenue_cents"] is None and r["gross_profit_cents"] is None
    inv=issue_invoice(make_shipment(make_company()))
    original = db.invoices.find_one({"id": inv["id"]})
    try:
        db.invoices.update_one({"id":inv["id"]},{"$set":{"shipment_id":s["id"],"currency":"USD"},"$unset":{"eur":"","fx":""}})
        r=get_row(s)
        assert r["revenue_complete"] is False and r["net_result_cents"] is None
        # The same invalid source must produce a repairable error at ledger boundaries.
        dates = {"from": original["issue_date"][:10], "to": original["issue_date"][:10]}
        for endpoint in ("build", "export"):
            response = ops().post("/finance/ledger/" + endpoint, json=dates)
            assert response.status_code == 400, response.text
            assert "EUR-muunnos" in response.json()["detail"]
    finally:
        # Restore even on assertion failure so later all-company reports are not poisoned.
        fields = ("shipment_id", "currency", "eur", "fx")
        restore = {"$set": {key: original[key] for key in fields if key in original}}
        absent = {key: "" for key in fields if key not in original}
        if absent:
            restore["$unset"] = absent
        db.invoices.update_one({"id": inv["id"]}, restore)


def test_quote_excludes_third_party_and_preserves_zero_quantity():
    lines=[{"code":"OWN","qty":1,"amount_cents":10000,"unit_cost_cents":6000},
           {"code":"OTHER","qty":1,"amount_cents":4000,"unit_cost_cents":4000,"payer":"third_party"},
           {"code":"ZERO","qty":0,"amount_cents":0,"unit_cost_cents":9000}]
    s=make_shipment(make_company(),lines)
    r=get_row(s)
    assert (r["revenue_cents"],r["cost_cents"],r["gross_profit_cents"])==(10000,6000,4000)
