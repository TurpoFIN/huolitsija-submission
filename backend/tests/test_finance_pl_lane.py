"""Lane aggregation preserves currency units, weighting, missingness and drilldown scope."""
from urllib.parse import urlparse, parse_qs
from test_s13_support import db, make_company, make_shipment, ops
from test_s13_reports_pnl import accrual, issue_invoice, window


def test_lane_weights_margin_and_preserves_company_in_drilldown():
    c=make_company()
    s1=make_shipment(c)
    s2=make_shipment(c)
    issue_invoice(s1);issue_invoice(s2)
    accrual(s1,status="matched",expected=220000,actual=230000)
    accrual(s2,status="open",expected=210000)
    result=ops().get("/finance/reports/pnl/lanes",params={**window(),"company_id":c["id"]})
    assert result.status_code==200,result.text
    row,=result.json()["items"]
    assert row["shipments"]==2 and row["revenue_cents"]==567000
    assert row["cost_cents"]==440000 and row["gross_profit_cents"]==127000
    assert row["margin_pct"]==22.4 and row["gp_per_shipment_cents"]==63500
    assert row["basis_counts"]=={"actual":1,"partly_actual":1,"estimated":0}
    assert row["cost_actual_share_pct"]==52.3
    link=urlparse(row["path"])
    assert link.path=="/ops/finance/pnl"
    assert parse_qs(link.query)["company_id"]==[c["id"]]
    detail=ops().get("/finance/reports/pnl/shipments",params={k:v[0] for k,v in parse_qs(link.query).items()}).json()
    assert detail["total"]==2 and detail["totals"]["gross_profit_cents"]==row["gross_profit_cents"]
    db.cost_accruals.update_one({"shipment_id":s2["id"]},{"$unset":{"expected_eur_cents":""}})
    row=ops().get("/finance/reports/pnl/lanes",params={**window(),"company_id":c["id"]}).json()["items"][0]
    assert row["incomplete_shipments"]==1 and row["gross_profit_cents"] is None
    assert row["cost_cents"] is None and row["margin_pct"] is None
