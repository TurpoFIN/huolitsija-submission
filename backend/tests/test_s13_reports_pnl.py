"""P&L per shipment and per lane against the real local API (front door).

Fixtures: shipments + quotes through test_s13_support, customer invoices issued through POST /api/invoices/from-shipment
and credit notes through POST /api/invoices/{id}/credit (the real billing engine). Cost rows of the other s13 parts
(cost_accruals, carrier_invoices, payables) are inserted directly in the contract shape (shared/interfaces.md s13),
test=True. Every expected number is computed here independently with Decimal from the fixture values.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from test_s13_support import db, make_company, make_shipment, now, iso, ops


def r(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pct(part: int, whole: int) -> float:
    return float((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def window() -> dict:
    return {"from": (date.today() - timedelta(days=30)).isoformat(), "to": (date.today() + timedelta(days=1)).isoformat()}


def issue_invoice(shipment: dict) -> dict:
    res = ops().post(f"/invoices/from-shipment/{shipment['id']}")
    assert res.status_code == 200, res.text
    inv = res.json()
    assert inv["status"] in ("open", "overdue"), (inv["status"], inv.get("warnings"))
    return inv


def accrual(shipment: dict, *, status: str, expected: int, actual: int = 0, name: str = "Test Line",
            kind: str = "carrier", pass_through: bool = False) -> dict:
    a = {"id": str(uuid.uuid4()), "shipment_id": shipment["id"], "shipment_ref": shipment["reference"],
         "company_id": shipment["company_id"], "quote_id": shipment["quote_id"], "option_id": shipment["option_id"],
         "lane": {"key": f"{shipment['mode']}:{shipment['origin']['country']}>{shipment['destination']['country']}"},
         "supplier": {"carrier_id": None, "name": name, "kind": kind}, "supplier_key": f"name:{name.lower()}",
         "lines": [{"code": "X", "description": name, "qty": 1, "unit_cost_cents": expected, "amount_cents": expected,
                    "currency": "EUR", "is_pass_through": pass_through}],
         "currency": "EUR", "expected_cents": expected, "expected_eur_cents": expected, "fx": {}, "status": status,
         "actual_eur_cents": actual, "variance_eur_cents": actual - expected if actual else 0, "carrier_invoice_ids": [],
         "period": date.today().isoformat()[:7], "created_at": iso(now()), "updated_at": iso(now()), "test": True}
    db.cost_accruals.insert_one(dict(a))
    return a


def carrier_invoice(matches: list[dict], *, status: str = "approved", total: int = 0) -> dict:
    ci = {"id": str(uuid.uuid4()), "supplier": {"name": "Test Line", "country": "FI", "vat_id": "FI12345678"},
          "number": f"CI-{uuid.uuid4().hex[:6]}", "invoice_date": date.today().isoformat(), "currency": "EUR",
          "net_cents": total, "vat_cents": 0, "total_cents": total, "eur": {"net_cents": total, "vat_cents": 0, "total_cents": total},
          "matches": matches, "vat_treatment": "zero_rated", "status": status, "created_at": iso(now()), "test": True}
    db.carrier_invoices.insert_one(dict(ci))
    return ci


def payable(ci: dict, realised_fx: int) -> dict:
    p = {"id": str(uuid.uuid4()), "carrier_invoice_id": ci["id"], "supplier": ci["supplier"], "currency": "USD",
         "amount_cents": ci["total_cents"], "eur_cents": ci["eur"]["total_cents"], "due_date": date.today().isoformat(),
         "status": "paid", "paid_at": iso(now()), "realised_fx_cents": realised_fx, "test": True}
    db.payables.insert_one(dict(p))
    return p


def get_row(shipment: dict) -> dict:
    res = ops().get("/finance/reports/pnl/shipments", params={**window(), "company_id": shipment["company_id"]})
    assert res.status_code == 200, res.text
    rows = [x for x in res.json()["items"] if x["shipment"]["id"] == shipment["id"]]
    assert len(rows) == 1, res.json()
    one = ops().get(f"/finance/reports/pnl/shipments/{shipment['id']}")
    assert one.status_code == 200, one.text
    for k in ("revenue_cents", "cost_cents", "gross_profit_cents", "basis"):
        assert one.json()[k] == rows[0][k]
    return rows[0]


def test_matched_vs_accrued_cost_credit_note_and_fx():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=2)  # quote: 245000 sell / 190000 cost + 38500 sell / 30000 cost
    inv = issue_invoice(s)
    assert inv["services_net_cents"] == 245000 + 38500

    a_matched = accrual(s, status="matched", expected=190000, actual=193500)
    accrual(s, status="open", expected=30000, name="Kuljetus Oy")
    accrual(s, status="released", expected=5000, name="Released Oy")
    a_part = accrual(s, status="partially_matched", expected=40000, actual=25000, name="Agent GmbH", kind="agent")
    accrual(s, status="open", expected=9999, name="Tulli", kind="customs", pass_through=True)
    accrual(s, status="disputed", expected=7000, name="Riita Oy")
    other = make_shipment(company, delivered_days_ago=40)
    ci1 = carrier_invoice([{"accrual_id": a_matched["id"], "shipment_id": s["id"], "shipment_ref": s["reference"],
                            "accrued_eur_cents": 190000, "invoiced_eur_cents": 193500, "variance_eur_cents": 3500}],
                          total=193500)
    ci2 = carrier_invoice([{"accrual_id": a_part["id"], "shipment_id": s["id"], "shipment_ref": s["reference"],
                            "accrued_eur_cents": 24000, "invoiced_eur_cents": 25000, "variance_eur_cents": 1000},
                           {"accrual_id": None, "shipment_id": other["id"], "shipment_ref": other["reference"],
                            "accrued_eur_cents": 75000, "invoiced_eur_cents": 75000, "variance_eur_cents": 0}],
                          total=100000)
    payable(ci1, -1200)
    payable(ci2, 400)

    # credit the domestic delivery line
    line = next(ln for ln in inv["lines"] if ln["code"] == "DELIVERY")
    cn = ops().post(f"/invoices/{inv['id']}/credit", json={"reason": "Jakelu peruttu", "line_ids": [line["id"]]})
    assert cn.status_code == 200, cn.text
    assert cn.json()["services_net_cents"] == -38500

    row = get_row(s)
    revenue = 245000 + 38500 - 38500
    actual = 193500 + 25000
    accrued = 30000 + (40000 - 24000) + 7000           # open + remaining of partial + disputed; released and pass-through 0
    cost = actual + accrued
    gp = revenue - cost
    fx = -1200 + r(Decimal(400) * Decimal(25000) / Decimal(100000))
    assert row["revenue_cents"] == revenue
    assert row["cost_actual_cents"] == actual
    assert row["cost_accrued_cents"] == accrued
    assert row["cost_cents"] == cost
    assert row["gross_profit_cents"] == gp
    assert row["margin_pct"] == pct(gp, revenue)
    assert row["fx_result_cents"] == fx
    assert row["net_result_cents"] == gp + fx
    assert row["basis"] == "partly_actual"
    q_rev, q_cost = 245000 + 38500, 190000 + 30000
    assert row["quoted"]["revenue_cents"] == q_rev and row["quoted"]["cost_cents"] == q_cost
    assert row["quoted"]["margin_cents"] == q_rev - q_cost
    assert row["variance"]["margin_cents"] == gp - (q_rev - q_cost)
    assert row["variance"]["cost_cents"] == cost - q_cost
    # drill-down shows what fed the numbers
    assert {i["id"] for i in row["lines"]["invoices"]} == {inv["id"], cn.json()["id"]}
    assert len(row["lines"]["accruals"]) == 6
    assert {c["id"] for c in row["lines"]["carrier_invoices"]} == {ci1["id"], ci2["id"]}


def test_all_matched_is_actual_and_foreign_invoice_uses_eur_breakdown():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=3)
    inv = issue_invoice(s)
    # The invoice was billed in USD: billing (agent D's hook) writes the EUR breakdown at the ECB rate. Use the real
    # stored ECB USD rate when one is present, else a fixed rate; the report must take eur.services_net_cents.
    fx_doc = db.fx_rates.find_one({"rates.USD": {"$exists": True}}, sort=[("date", -1)])
    rate = Decimal(str(fx_doc["rates"]["USD"])) if fx_doc else Decimal("1.0812")
    usd_net = 310000
    eur_net = r(Decimal(usd_net) / rate)
    db.invoices.update_one({"id": inv["id"]}, {"$set": {
        "currency": "USD", "services_net_cents": usd_net, "fx_realised_cents": 2500,
        "eur": {"services_net_cents": eur_net, "pass_through_cents": 0, "vat_cents": 0, "total_cents": eur_net},
        "fx": {"rate": float(rate), "rate_date": (fx_doc or {}).get("date"), "source": "ECB"}}})
    accrual(s, status="matched", expected=190000, actual=188000)
    accrual(s, status="matched", expected=30000, actual=30500, name="Kuljetus Oy")
    accrual(s, status="written_off", expected=1234, name="Old Oy")
    row = get_row(s)
    assert row["revenue_cents"] == eur_net
    assert row["cost_cents"] == 188000 + 30500 and row["cost_accrued_cents"] == 0
    assert row["basis"] == "actual"
    assert row["fx_result_cents"] == 2500
    assert row["gross_profit_cents"] == eur_net - 218500

    # Without an eur breakdown the stored ECB rate on the invoice converts (half-up)
    db.invoices.update_one({"id": inv["id"]}, {"$unset": {"eur": ""}})
    row = get_row(s)
    assert row["revenue_cents"] == eur_net  # same formula: cents / rate, half-up


def test_estimated_from_quote_and_lanes_and_margin_groups():
    company = make_company()
    s_sea = make_shipment(company, delivered_days_ago=1)
    issue_invoice(s_sea)
    accrual(s_sea, status="matched", expected=190000, actual=195000)
    accrual(s_sea, status="open", expected=30000, name="Kuljetus Oy")
    # in transit, no invoice, no accruals: everything from the quote
    s_road = make_shipment(company, mode="road", origin=("DEHAM", "Hamburg", "DE", 53.55, 9.99), status="in_transit",
                           lines=[{"code": "ROAD_FREIGHT", "description": "Maantierahti", "qty": 1, "unit_cost_cents": 80000,
                                   "unit_price_cents": 99000, "amount_cents": 99000, "vat_code": "FI_25_5",
                                   "is_pass_through": False},
                                  {"code": "DUTY", "description": "Tulli (arvio)", "qty": 1, "unit_cost_cents": 5000,
                                   "unit_price_cents": 5000, "amount_cents": 5000, "vat_code": "OUT_OF_SCOPE_DISBURSEMENT",
                                   "is_pass_through": True}])
    est = get_row(s_road)
    assert est["basis"] == "estimated"
    assert est["revenue_cents"] == 99000 and est["cost_cents"] == 80000 and est["cost_source"] == "quote"
    assert est["gross_profit_cents"] == 19000

    res = ops().get("/finance/reports/pnl/lanes", params={**window(), "company_id": company["id"]})
    assert res.status_code == 200, res.text
    lanes = {x["lane"]: x for x in res.json()["items"]}
    assert set(lanes) == {"sea:CN>FI", "road:DE>FI"}
    sea = lanes["sea:CN>FI"]
    sea_rev, sea_cost = 245000 + 38500, 195000 + 30000
    assert sea["shipments"] == 1 and sea["revenue_cents"] == sea_rev and sea["cost_cents"] == sea_cost
    assert sea["gross_profit_cents"] == sea_rev - sea_cost and sea["gp_per_shipment_cents"] == sea_rev - sea_cost
    assert sea["margin_pct"] == pct(sea_rev - sea_cost, sea_rev)
    assert sea["cost_actual_share_pct"] == pct(195000, sea_cost)
    assert sea["margin_variance_cents"] == (sea_rev - sea_cost) - (283500 - 220000)
    road = lanes["road:DE>FI"]
    assert road["revenue_cents"] == 99000 and road["gross_profit_cents"] == 19000 and road["cost_actual_share_pct"] == 0.0
    tot = res.json()["totals"]
    assert tot["revenue_cents"] == sea_rev + 99000 and tot["shipments"] == 2

    m = ops().get("/finance/reports/margin", params={**window(), "company_id": company["id"], "group": "mode"})
    assert m.status_code == 200, m.text
    by_mode = {x["key"]: x for x in m.json()["items"]}
    assert by_mode["sea"]["gross_profit_cents"] == sea_rev - sea_cost and by_mode["road"]["gross_profit_cents"] == 19000
    only_est = ops().get("/finance/reports/pnl/shipments", params={**window(), "company_id": company["id"], "basis": "estimated"})
    assert [x["shipment"]["id"] for x in only_est.json()["items"]] == [s_road["id"]]
    assert ops().get("/finance/reports/margin", params={"group": "nope"}).status_code == 400


def test_overview_tiles_have_links():
    res = ops().get("/finance/overview")
    assert res.status_code == 200, res.text
    tiles = res.json()["tiles"]
    for key in ("open_accruals", "carrier_invoices_attention", "payables_due_7d", "ar_overdue", "collections_open",
                "vat_current", "gross_profit_mtd", "fx", "ledger_last_export", "autopilot"):
        assert key in tiles and tiles[key]["path"].startswith("/ops/"), key
    # the open-accrual tile counts what is in the DB (independent count of open/partially/disputed rows), without
    # test-tenant records: company-wide finance figures never include them (s2, core.tenant_visibility.exclude_test)
    test_ids = [c["id"] for c in db.companies.find({"$or": [{"test_tenant": True}, {"test": True}]}, {"id": 1})]
    n = db.cost_accruals.count_documents({"status": {"$in": ["open", "partially_matched", "disputed"]},
                                          "company_id": {"$nin": test_ids}, "test": {"$ne": True},
                                          "test_tenant": {"$ne": True}, "demo": {"$ne": True}})
    assert tiles["open_accruals"]["count"] == n
