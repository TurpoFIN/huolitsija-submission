"""s13 collections against the real local API: entry after dunning (main fallback and s2's `collection` stage),
Korkolaki interest/fee math (Decimal), final demand e-mail, credit hold, owner queue, hand-over CSV, paid -> closed."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pytest

from test_s13_support import db, make_company, make_shipment, ops

TODAY = lambda: datetime.now(ZoneInfo("Europe/Helsinki")).date()  # noqa: E731


def hu(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def interest(principal: int, pct: float, days: int) -> int:
    return hu(Decimal(principal) * Decimal(str(pct)) * Decimal(days) / Decimal(36500))


def eur_fi(cents: int) -> str:
    return f"{Decimal(cents) / 100:,.2f}".replace(",", " ").replace(".", ",") + " €"


def set_autopilot(on: bool):
    body = {"enabled": on, "confirmation": "enable"} if on else {"enabled": False, "confirmation": ""}
    r = ops().post("/settings/autonomy/autopilot_enabled", json=body)
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module", autouse=True)
def restore_autopilot():
    before = ops().get("/autonomy/status").json()["autopilot_enabled"]
    yield
    set_autopilot(before)


def overdue_invoice(company: dict, days: int, *, reminder: bool = True, unit: int = 100000) -> dict:
    r = ops().post("/invoices", json={"company_id": company["id"], "issue": True, "send": False, "lines": [
        {"description": "Huolintapalkkio ja rahti", "qty": 1, "unit_price_cents": unit, "vat_code": "FI_25_5"}]})
    assert r.status_code == 200, r.text
    inv = r.json()
    # time passes: the due date is `days` ago (Helsinki)
    db.invoices.update_one({"id": inv["id"]}, {"$set": {"due_date": (TODAY() - timedelta(days=days)).isoformat()}})
    if reminder:
        assert ops().post("/invoices/run-overdue").status_code == 200  # billing marks overdue + sends the reminder
        got = db.invoices.find_one({"id": inv["id"]})
        assert got["status"] == "overdue" and any(e["kind"] == "reminder" for e in got.get("emails") or [])
    return db.invoices.find_one({"id": inv["id"]}, {"_id": 0})


def sweep() -> dict:
    r = ops().post("/finance/collections/sweep")
    assert r.status_code == 200, r.text
    return r.json()


def case_of(inv: dict) -> dict:
    c = db.collections_cases.find_one({"invoice_id": inv["id"]}, {"_id": 0})
    assert c, f"no collections case for {inv.get('number')}"
    return c


def test_autopilot_final_demand_email_credit_hold_then_paid_releases():
    set_autopilot(True)
    company = make_company()
    make_shipment(company)  # still on the way: retention right (NSAB 2015 § 14) possible
    inv = overdue_invoice(company, 30)
    out = sweep()
    case = case_of(inv)
    assert case["id"] in out["opened"] and case["id"] in out["demands"]
    principal = inv["total_cents"]
    assert principal == 125500
    assert case["stage"] == "final_demand" and case["amount_due_cents"] == principal and case["days_past_due"] == 30
    assert case["interest_cents"] == interest(principal, inv["late_interest_pct"], 30)
    assert case["fee_cents"] == 4000 and case["lien_possible"] is True and case["entry"] == "overdue_after_reminder"
    mail = db.email_outbox.find_one({"tag": "collections_final_demand", "ref.id": inv["id"]})
    assert mail and mail["to"] == company["billing_email"] and mail["subject"] == f"Maksuvaatimus: lasku {inv['number']}"
    new_due = TODAY() + timedelta(days=14)
    for part in (eur_fi(principal), eur_fi(case["interest_cents"]), "40,00 €", inv["reference"],
                 f"{new_due.day}.{new_due.month}.{new_due.year}", "luottokiellossa", "perintätoimistolle", "4 a §", "10 e §"):
        assert part in mail["text"], (part, mail["text"])
    co = db.companies.find_one({"id": company["id"]})
    assert co["credit_hold"] is True and co["credit_status"] == "hold" and co["credit_hold_source"] == "collections"
    assert db.agent_decisions.find_one({"action": "send_to_collections", "subject_id": inv["id"], "outcome": "auto"})
    # idempotent per invoice
    sweep()
    assert db.collections_cases.count_documents({"invoice_id": inv["id"]}) == 1
    # paid -> case closed, hold released
    r = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": TODAY().isoformat(),
                                                            "reference": f"BANK-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    case = case_of(inv)
    assert case["stage"] == "paid" and case["closed_at"] and case["credit_hold"] is False
    co = db.companies.find_one({"id": company["id"]})
    assert co["credit_hold"] is False and co["credit_status"] == "ok"


def test_autopilot_off_goes_to_owner_queue_nothing_sent_until_approved():
    set_autopilot(False)
    company = make_company()
    inv = overdue_invoice(company, 25)
    out = sweep()
    case = case_of(inv)
    assert case["id"] in out["queued"] and case["pending_action"]["action"] == "send_demand"
    appr = db.approvals.find_one({"kind": "collections_action", "ref_id": inv["id"], "status": "pending"})
    assert appr and appr["data"]["case_id"] == case["id"] and appr["data"]["finance_action"] == "send_demand"
    assert any(r["code"] == "autopilot_off" for r in appr["reasons"])
    assert not db.email_outbox.find_one({"tag": "collections_final_demand", "ref.id": inv["id"]})
    assert not db.companies.find_one({"id": company["id"]}).get("credit_hold")
    r = ops().post(f"/approvals/{appr['id']}/decide", json={"decision": "approved", "note": "Lähetä maksuvaatimus"})
    assert r.status_code == 200, r.text
    assert db.email_outbox.find_one({"tag": "collections_final_demand", "ref.id": inv["id"]})
    assert db.companies.find_one({"id": company["id"]})["credit_hold"] is True
    assert case_of(inv)["demand"]["approval_id"] == appr["id"]


def test_s2_collection_stage_with_compensation_invoiced_charges_no_fee():
    set_autopilot(True)
    company = make_company()
    inv = overdue_invoice(company, 50, reminder=False)
    t = datetime.now(ZoneInfo("UTC")).isoformat()
    db.invoices.update_one({"id": inv["id"]}, {"$set": {
        "status": "overdue",
        "dunning": {"stage": "collection", "compensation_invoice_id": "comp-" + inv["id"][:8], "updated_at": t,
                    "history": [{"stage": "reminder_2", "at": t, "compensation_invoice_id": "comp-" + inv["id"][:8]}]},
        "collection": {"at": t, "by": {"name": "Owner"}, "approval_id": None, "agency": None}}})
    # an invoice s2 is still dunning (no collection stage yet) stays out of collections
    other = overdue_invoice(company, 60, reminder=False)
    db.invoices.update_one({"id": other["id"]}, {"$set": {"status": "overdue", "dunning": {"stage": "reminder_2", "history": []}}})
    sweep()
    case = case_of(inv)
    assert case["entry"] == "dunning_collection" and case["fee_cents"] == 0
    assert case["interest_cents"] == interest(inv["total_cents"], inv["late_interest_pct"], 50)
    assert not db.collections_cases.find_one({"invoice_id": other["id"]})
    mail = db.email_outbox.find_one({"tag": "collections_final_demand", "ref.id": inv["id"]})
    assert mail and "laskutettu erikseen" in mail["text"] and "40,00 €" not in mail["text"]


def test_handover_csv_payment_plan_write_off_and_list():
    set_autopilot(True)
    company = make_company()
    inv = overdue_invoice(company, 40, unit=200000)
    sweep()
    case = case_of(inv)
    assert case["demand"]["sent_at"]
    # agency window passes without payment
    db.collections_cases.update_one({"id": case["id"]}, {"$set": {
        "demand.sent_at": (datetime.now(ZoneInfo("UTC")) - timedelta(days=31)).isoformat()}})
    out = sweep()
    assert any(h["case_id"] == case["id"] and h["auto"] for h in out["handover"])
    case = case_of(inv)
    assert case["stage"] == "agency" and case["handover"]["mode"] == "file"
    f = ops().get(f"/finance/collections/{case['id']}/file")
    assert f.status_code == 200, f.text
    rows = list(csv.DictReader(io.StringIO(f.content.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1
    row = rows[0]
    assert row["debtor_name"] == company["name"] and row["business_id"] == company["business_id"]
    assert row["invoice_number"] == inv["number"] and row["due_date"] == db.invoices.find_one({"id": inv["id"]})["due_date"]
    assert Decimal(row["principal"]) == Decimal(inv["total_cents"]) / 100
    assert Decimal(row["interest"]) == Decimal(interest(inv["total_cents"], inv["late_interest_pct"], 40)) / 100
    assert row["fees"] == "40.00" and row["reference"] == inv["reference"]
    # payment plan must cover what is due
    bad = ops().post(f"/finance/collections/{case['id']}/payment_plan", json={"installments": [
        {"due_date": (TODAY() + timedelta(days=10)).isoformat(), "amount_cents": 1000}]})
    assert bad.status_code == 400 and "less than" in bad.json()["detail"]
    half = inv["total_cents"] // 2
    ok = ops().post(f"/finance/collections/{case['id']}/payment_plan", json={"installments": [
        {"due_date": (TODAY() + timedelta(days=30)).isoformat(), "amount_cents": inv["total_cents"] - half},
        {"due_date": (TODAY() + timedelta(days=10)).isoformat(), "amount_cents": half}]})
    assert ok.status_code == 200, ok.text
    c2 = ok.json()["case"]
    assert c2["stage"] == "payment_plan" and c2["next_action_at"] == (TODAY() + timedelta(days=10)).isoformat()
    # write-off is always the owner's decision, even under Autopilot
    w = ops().post(f"/finance/collections/{case['id']}/write_off", json={"note": "Konkurssi"})
    assert w.status_code == 200, w.text
    assert w.json()["result"]["auto"] is False
    appr = db.approvals.find_one({"kind": "collections_action", "ref_id": case["id"], "status": "pending"})
    assert appr and appr["data"]["action"] == "write_off"
    assert any(r["code"] == "judgment" for r in appr["reasons"])
    assert case_of(inv)["stage"] == "payment_plan"
    r = ops().post(f"/approvals/{appr['id']}/decide", json={"decision": "approved", "note": "Kirjataan luottotappioksi"})
    assert r.status_code == 200, r.text
    assert case_of(inv)["stage"] == "written_off"
    # close refuses an unpaid invoice
    inv2 = overdue_invoice(company, 22)
    sweep()
    c3 = case_of(inv2)
    bad_close = ops().post(f"/finance/collections/{c3['id']}/close", json={})
    assert bad_close.status_code == 409
    lst = ops().get("/finance/collections", params={"company": company["id"]}).json()
    assert lst["total"] == 2 and lst["counts"]["written_off"] == 1 and lst["counts"]["final_demand"] == 1
    assert lst["totals"]["open_count"] == 1
    assert lst["totals"]["open_claim_eur_cents"] == c3["amount_due_cents"] + c3["interest_cents"] + c3["fee_cents"]
    # one open case still holds the customer; the written-off case's hold stays too
    assert db.companies.find_one({"id": company["id"]})["credit_hold"] is True


def test_usd_invoice_case_claims_in_usd_and_fee_in_eur():
    set_autopilot(True)
    company = make_company()
    assert ops().put(f"/finance/customers/{company['id']}/currency", json={"currency": "USD"}).status_code == 200
    inv = overdue_invoice(company, 23, unit=50000)  # manual lines on a USD customer are typed in USD
    assert inv["currency"] == "USD" and inv["total_cents"] == 50000 + hu(Decimal(50000) * Decimal("25.5") / 100)
    sweep()
    case = case_of(inv)
    assert case["currency"] == "USD" and case["fee_cents"] == 4000 and case["fee_currency"] == "EUR"
    assert case["interest_cents"] == interest(inv["total_cents"], inv["late_interest_pct"], 23)
    doc = db.fx_rates.find_one({"date": {"$lte": TODAY().isoformat()}}, sort=[("date", -1)])
    rate = Decimal(str(doc["rates"]["USD"]))
    assert case["eur"]["amount_due_cents"] == hu(Decimal(inv["total_cents"]) / rate)
    assert case["eur"]["total_cents"] == case["eur"]["amount_due_cents"] + hu(Decimal(case["interest_cents"]) / rate) + 4000
    mail = db.email_outbox.find_one({"tag": "collections_final_demand", "ref.id": inv["id"]})
    principal_txt = f"{Decimal(inv['total_cents']) / 100:,.2f}".replace(",", " ").replace(".", ",") + " USD"
    assert principal_txt in mail["text"] and "40,00 €" in mail["text"] and "€ USD" not in mail["text"]
