"""Invoices against the real local API: VAT math, pass-through, references, overdue, credit notes, scoping."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest

from test_v4_support import (customer, customer_company_id, db, fi_check_digit, make_company, make_declaration,
                             make_shipment, make_user, ops, rf_valid)


def vat(base_cents: int, rate: str) -> int:
    return int((Decimal(base_cents) * Decimal(rate) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def invoice_for(shipment: dict) -> dict:
    r = ops().post(f"/invoices/from-shipment/{shipment['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def test_vat_zero_rated_domestic_and_pass_through_paid_to_customs():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=1, lines=[
        {"code": "OCEAN_FREIGHT", "description": "Merirahti Shanghai–Helsinki 1×40HC", "qty": 1, "unit_price_cents": 245000,
         "amount_cents": 245000, "vat_code": "ZERO_INTL_TRANSPORT", "is_pass_through": False},
        {"code": "THC_DEST", "description": "Terminaalikäsittely Vuosaari", "qty": 1, "unit_price_cents": 21000,
         "amount_cents": 21000, "vat_code": "ZERO_INTL_TRANSPORT", "is_pass_through": False},
        {"code": "DELIVERY", "description": "Jakelu Vuosaari–Lahti", "qty": 1, "unit_price_cents": 38500,
         "amount_cents": 38500, "vat_code": "FI_25_5", "is_pass_through": False},
        {"code": "CUSTOMS_CLEARANCE", "description": "Tulliselvitys", "qty": 1, "unit_price_cents": 6500,
         "amount_cents": 6500, "vat_code": "FI_25_5", "is_pass_through": False},
        # estimated duty on the quote must be replaced by the declaration's actual amount
        {"code": "DUTY", "description": "Tulli (arvio)", "qty": 1, "unit_price_cents": 50000, "amount_cents": 50000,
         "vat_code": "OUT_OF_SCOPE_DISBURSEMENT", "is_pass_through": True},
    ])
    make_declaration(s, duty_cents=41234, vat_cents=123456, vat_route="paid_to_customs")
    inv = invoice_for(s)

    assert inv["status"] == "open", inv.get("warnings")
    codes = [ln["code"] for ln in inv["lines"]]
    assert codes.count("DUTY") == 1 and "IMPORT_VAT" in codes
    duty = next(ln for ln in inv["lines"] if ln["code"] == "DUTY")
    assert duty["amount_cents"] == 41234 and duty["vat_code"] == "OUT_OF_SCOPE_DISBURSEMENT" and duty["vat_cents"] == 0
    std_base = 38500 + 6500
    expected_vat = vat(std_base, "25.5")  # 11475.0 -> 11475
    assert inv["vat_cents"] == expected_vat
    assert inv["services_net_cents"] == 245000 + 21000 + std_base
    assert inv["pass_through_cents"] == 41234 + 123456
    assert inv["total_cents"] == inv["services_net_cents"] + inv["pass_through_cents"] + expected_vat
    by_code = {b["vat_code"]: b for b in inv["vat_breakdown"]}
    assert by_code["FI_25_5"]["rate_pct"] == 25.5 and by_code["FI_25_5"]["base_cents"] == std_base
    assert by_code["ZERO_INTL_TRANSPORT"]["vat_cents"] == 0 and by_code["ZERO_INTL_TRANSPORT"]["base_cents"] == 266000
    lb = by_code["ZERO_INTL_TRANSPORT"]["legal_basis"]
    assert lb and lb["fi"] and lb["en"] and lb["status"] in ("pending_tax_adviser_review", "approved")
    assert sum(ln["vat_cents"] for ln in inv["lines"]) == inv["vat_cents"]
    # shipment milestone INVOICED recorded
    sh = db.shipments.find_one({"id": s["id"]})
    assert inv["id"] in sh["invoice_ids"]
    assert any((m.get("code") or "").lower() == "invoiced" for m in sh["milestones"])
    # idempotent per shipment
    again = invoice_for(s)
    assert again["id"] == inv["id"]


def test_tilitys_route_has_no_import_vat_line_and_states_postponement():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=1)
    make_declaration(s, duty_cents=9900, vat_cents=77777, vat_route="tilitys")
    inv = invoice_for(s)
    codes = [ln["code"] for ln in inv["lines"]]
    assert "IMPORT_VAT" not in codes and "DUTY" in codes
    assert any(i["code"] == "IMPORT_VAT_POSTPONED" for i in inv["info"])


def test_unreleased_declaration_keeps_invoice_as_draft_with_reason():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=1)
    make_declaration(s, duty_cents=1000, vat_cents=1000, vat_route="paid_to_customs", status="submitted")
    inv = invoice_for(s)
    assert inv["status"] == "draft" and inv["number"] is None
    assert any("not released" in w for w in inv["warnings"])


def test_foreign_eu_business_customer_gets_reverse_charge():
    company = make_company(country="SE", vat_id="SE556677889901")
    s = make_shipment(company, delivered_days_ago=1)
    inv = invoice_for(s)
    assert {ln["vat_code"] for ln in inv["lines"]} == {"EU_ICS_RC"}
    assert inv["vat_cents"] == 0
    assert inv["vat_breakdown"][0]["legal_basis"]["fi"].startswith("Käännetty verovelvollisuus")


def test_vat_rounding_allocates_to_lines_exactly():
    company = make_company()
    lines = [{"description": f"Käsittely {i}", "qty": 1, "unit_price_cents": 33, "vat_code": "FI_25_5"} for i in range(7)]
    lines.append({"description": "Varastointi 3 pv", "qty": 3, "unit_price_cents": 1999, "vat_code": "FI_25_5"})
    r = ops().post("/invoices", json={"company_id": company["id"], "lines": lines})
    assert r.status_code == 200, r.text
    inv = r.json()
    base = 7 * 33 + 3 * 1999
    assert inv["subtotal_cents"] == base
    assert inv["vat_cents"] == vat(base, "25.5")
    assert sum(ln["vat_cents"] for ln in inv["lines"]) == inv["vat_cents"]
    assert inv["status"] == "draft" and inv["number"] is None


@pytest.mark.parametrize("bad, msg", [
    ({"description": "x", "qty": 0, "unit_price_cents": 100}, "quantity"),
    ({"description": "x", "qty": 1, "unit_price_cents": 100, "vat_code": "FI_99"}, "unknown VAT code"),
    ({"description": "Tulli", "qty": 1, "unit_price_cents": 100, "vat_code": "FI_25_5", "is_pass_through": True}, "pass-through"),
])
def test_manual_invoice_rejects_bad_lines(bad, msg):
    company = make_company()
    r = ops().post("/invoices", json={"company_id": company["id"], "lines": [bad]})
    assert r.status_code == 400 and msg.lower() in r.json()["detail"].lower()


def test_issue_assigns_number_viitenumero_rf_and_due_date():
    company = make_company(terms=21)
    r = ops().post("/invoices", json={"company_id": company["id"], "lines": [
        {"description": "Huolintapalkkio", "qty": 2, "unit_price_cents": 4750, "vat_code": "FI_25_5"}], "issue": True})
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] == "open" and inv["number"].startswith("HUO-INV-")
    ref = inv["reference"].replace(" ", "")
    assert ref.isdigit() and 4 <= len(ref) <= 20
    assert int(ref[-1]) == fi_check_digit(ref[:-1]), ref
    assert ref[:-1] == inv["number"].split("-", 2)[2].replace("-", "")
    assert rf_valid(inv["rf_reference"]) and inv["rf_reference"].endswith(ref)
    issue, due = date.fromisoformat(inv["issue_date"]), date.fromisoformat(inv["due_date"])
    assert (due - issue).days == 21
    pdf = ops().get(f"/invoices/{inv['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and len(pdf.content) > 3000
    detail = ops().get(f"/invoices/{inv['id']}").json()
    bt = detail["payment_options"]["bank_transfer"]
    assert bt["reference"] == inv["reference"] and bt["amount_cents"] == inv["total_cents"]
    if bt["iban"] and bt["iban"].startswith("FI"):
        assert bt["virtual_barcode"] and len(bt["virtual_barcode"]) == 54


def test_overdue_sweep_marks_and_reminds():
    company = make_company()
    inv = ops().post("/invoices", json={"company_id": company["id"], "issue": True, "lines": [
        {"description": "Varastointi", "qty": 1, "unit_price_cents": 12000, "vat_code": "FI_25_5"}]}).json()
    past = (date.today() - timedelta(days=3)).isoformat()
    db.invoices.update_one({"id": inv["id"]}, {"$set": {"due_date": past}})
    r = ops().post("/invoices/run-overdue")
    assert r.status_code == 200 and r.json()["marked_overdue"] >= 1
    got = ops().get(f"/invoices/{inv['id']}").json()
    assert got["status"] == "overdue" and got["days_overdue"] >= 3
    assert db.email_outbox.find_one({"ref.id": inv["id"], "tag": "invoice_reminder"})
    # running again does not send a second reminder
    ops().post("/invoices/run-overdue")
    assert db.email_outbox.count_documents({"ref.id": inv["id"], "tag": "invoice_reminder"}) == 1


def test_bank_transfer_payment_is_recorded_once_and_adds_paid_milestone():
    company = make_company()
    s = make_shipment(company, delivered_days_ago=2)
    inv = invoice_for(s)
    future = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": (date.today() + timedelta(days=2)).isoformat(),
                                                                  "reference": "X"})
    assert future.status_code == 400
    body = {"paid_at": date.today().isoformat(), "reference": "ARK-20261024-0001"}
    r1 = ops().post(f"/invoices/{inv['id']}/mark-paid", json=body)
    assert r1.status_code == 200 and r1.json()["status"] == "paid"
    r2 = ops().post(f"/invoices/{inv['id']}/mark-paid", json=body)
    assert r2.status_code == 409
    got = db.invoices.find_one({"id": inv["id"]})
    assert got["paid_cents"] == inv["total_cents"] and len(got["payments"]) == 1
    sh = db.shipments.find_one({"id": s["id"]})
    assert any((m.get("code") or "").lower() == "paid" for m in sh["milestones"])


def test_full_credit_note_links_and_negates():
    company = make_company()
    inv = ops().post("/invoices", json={"company_id": company["id"], "issue": True, "lines": [
        {"description": "Rahti", "qty": 1, "unit_price_cents": 100000, "vat_code": "ZERO_INTL_TRANSPORT"},
        {"description": "Jakelu", "qty": 1, "unit_price_cents": 20000, "vat_code": "FI_25_5"}]}).json()
    r = ops().post(f"/invoices/{inv['id']}/credit", json={"reason": "Väärä asiakas"})
    assert r.status_code == 200, r.text
    cn = r.json()
    assert cn["kind"] == "credit_note" and cn["credit_note_for"] == inv["id"]
    assert cn["total_cents"] == -inv["total_cents"] and cn["vat_cents"] == -inv["vat_cents"]
    orig = ops().get(f"/invoices/{inv['id']}").json()
    assert orig["status"] == "credited" and orig["amount_due_cents"] == 0
    assert any(x["id"] == cn["id"] for x in orig["related"])
    again = ops().post(f"/invoices/{inv['id']}/credit", json={"reason": "toinen"})
    assert again.status_code == 409


def test_void_rules():
    company = make_company()
    inv = ops().post("/invoices", json={"company_id": company["id"], "issue": True, "lines": [
        {"description": "Rahti", "qty": 1, "unit_price_cents": 5000, "vat_code": "FI_25_5"}]}).json()
    assert ops().post(f"/invoices/{inv['id']}/void", json={"reason": "x"}).status_code == 422  # reason too short
    r = ops().post(f"/invoices/{inv['id']}/void", json={"reason": "Tuplalasku"})
    assert r.status_code == 200 and r.json()["status"] == "void"
    assert ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": date.today().isoformat(), "reference": "a"}).status_code == 409


def test_customer_scope_drafts_and_other_companies_hidden():
    mine = customer_company_id()
    other = make_company()
    other_inv = ops().post("/invoices", json={"company_id": other["id"], "issue": True, "lines": [
        {"description": "Rahti", "qty": 1, "unit_price_cents": 5000, "vat_code": "FI_25_5"}]}).json()
    my_draft = ops().post("/invoices", json={"company_id": mine, "lines": [
        {"description": "Rahti", "qty": 1, "unit_price_cents": 5000, "vat_code": "FI_25_5"}]}).json()
    assert customer().get(f"/invoices/{other_inv['id']}").status_code == 404
    assert customer().get(f"/invoices/{my_draft['id']}").status_code == 404
    lst = customer().get("/invoices", params={"status": "draft"}).json()
    assert all(i["status"] != "draft" for i in lst["items"])
    assert all(i["company_id"] == mine for i in customer().get("/invoices").json()["items"])
    assert customer().post(f"/invoices/{other_inv['id']}/mark-paid", json={"paid_at": "2026-10-01", "reference": "x"}).status_code == 403
    member, _ = make_user(mine, role="customer_user")
    mine_open = ops().post("/invoices", json={"company_id": mine, "issue": True, "lines": [
        {"description": "Rahti", "qty": 1, "unit_price_cents": 5000, "vat_code": "FI_25_5"}]}).json()
    assert member.post(f"/invoices/{mine_open['id']}/checkout").status_code == 403
    assert httpx.get(f"{ops().base_url}invoices").status_code == 401


def test_hostile_inputs_are_plain_400s_not_500s():
    """Found by adversarial probing: NaN quantity and a dd.mm.yyyy payment date used to raise 500s, and a manual
    payment far above the amount due used to mark the invoice paid."""
    company = make_company()
    bad = ops().post("/invoices", json={"company_id": company["id"], "lines": [
        {"description": "Kuljetus", "qty": "NaN", "unit_price_cents": 1000, "vat_code": "FI_25_5"}]})
    assert bad.status_code == 400 and "quantity" in bad.json()["detail"]
    inf = ops().post("/invoices", json={"company_id": company["id"], "lines": [
        {"description": "Kuljetus", "qty": 1, "unit_price_cents": 10**15, "vat_code": "FI_25_5"}]})
    assert inf.status_code in (400, 422)
    inv = ops().post("/invoices", json={"company_id": company["id"], "issue": True, "send": False, "lines": [
        {"description": "Kotimaan jakelu", "qty": 1, "unit_price_cents": 10000, "vat_code": "FI_25_5"}]}).json()
    assert inv["status"] == "open" and inv["total_cents"] == 12550
    fi_date = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": "24.10.2026", "reference": "ARK-1"})
    assert fi_date.status_code == 400, fi_date.text
    over = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": date.today().isoformat(), "reference": "ARK-2",
                                                               "amount_cents": 10**9})
    assert over.status_code == 400, over.text
    after = ops().get(f"/invoices/{inv['id']}").json()
    assert after["status"] == "open" and after["paid_cents"] == 0
    part = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": date.today().isoformat(), "reference": "ARK-3",
                                                               "amount_cents": 5000})
    assert part.status_code == 200 and part.json()["status"] == "open" and part.json()["amount_due_cents"] == 7550
