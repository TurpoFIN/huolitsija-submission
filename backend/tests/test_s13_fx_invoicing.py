"""s13 multi-currency AR against the real local API: invoice currency, ECB conversion, EUR VAT statement (AVL 73 §),
PDF, realised FX on payment, month-end revaluation. Rates are always read back from fx_rates (live ECB data)."""
from __future__ import annotations

import io
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from test_s13_support import customer, db, make_company, make_declaration, make_shipment, ops, wait_for

LINES = [
    {"code": "OCEAN_FREIGHT", "description": "Merirahti Shanghai–Helsinki 1×40HC", "basis": "PER_CONTAINER", "qty": 1,
     "unit_cost_cents": 190000, "unit_price_cents": 245013, "amount_cents": 245013, "vat_code": "ZERO_INTL_TRANSPORT",
     "is_pass_through": False},
    {"code": "DELIVERY", "description": "Kotimaan jakelu Vuosaari–Lahti", "basis": "PER_SHIPMENT", "qty": 1,
     "unit_cost_cents": 30000, "unit_price_cents": 38577, "amount_cents": 38577, "vat_code": "FI_25_5",
     "is_pass_through": False},
]


def hu(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ecb(currency: str, on: str) -> tuple[Decimal, str]:
    """Independent read of the stored ECB set: latest publication on or before `on`."""
    doc = db.fx_rates.find_one({"date": {"$lte": on}, f"rates.{currency}": {"$exists": True}}, sort=[("date", -1)])
    assert doc, f"no ECB rate for {currency} on or before {on} in fx_rates"
    return Decimal(str(doc["rates"][currency])), doc["date"]


def eur_fi(cents: int) -> str:
    s = f"{Decimal(cents) / 100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    return "".join("".join(p.extract_text().split()) for p in PdfReader(io.BytesIO(content)).pages)


@pytest.fixture(scope="module", autouse=True)
def ecb_rates():
    r = ops().post("/finance/fx/refresh", json={"kind": "90d"})
    assert r.status_code == 200, r.text
    assert r.json()["days"] > 40  # the 90-day ECB feed really was read
    return r.json()


def usd_company() -> dict:
    c = make_company()
    r = ops().put(f"/finance/customers/{c['id']}/currency", json={"currency": "USD"})
    assert r.status_code == 200, r.text
    assert db.companies.find_one({"id": c["id"]})["invoice_currency"] == "USD"
    return c


def issued_usd_invoice(with_customs: bool = True) -> tuple[dict, dict, dict | None]:
    c = usd_company()
    s = make_shipment(c, LINES, delivered_days_ago=1)
    d = make_declaration(s, duty_cents=12345, vat_cents=67891, vat_route="paid_to_customs") if with_customs else None
    r = ops().post(f"/invoices/from-shipment/{s['id']}")
    assert r.status_code == 200, r.text
    inv = r.json()
    if inv["status"] == "draft":
        r = ops().post(f"/invoices/{inv['id']}/issue", json={"send": False})
        assert r.status_code == 200, r.text
        inv = r.json()
    assert inv["status"] == "open", inv
    return inv, c, d


def test_currency_endpoint_validates_against_ecb_set_and_needs_billing_write():
    c = make_company()
    assert ops().put(f"/finance/customers/{c['id']}/currency", json={"currency": "XAU"}).status_code == 400
    assert ops().put(f"/finance/customers/{uuid.uuid4()}/currency", json={"currency": "USD"}).status_code == 404
    assert customer().put(f"/finance/customers/{c['id']}/currency", json={"currency": "USD"}).status_code == 403
    latest = db.fx_rates.find_one({}, sort=[("date", -1)])
    some = sorted(latest["rates"])[0]
    assert ops().put(f"/finance/customers/{c['id']}/currency", json={"currency": some.lower()}).status_code == 200
    assert db.audit_log.find_one({"action": "company_invoice_currency_set", "entity_id": c["id"]})


def test_usd_invoice_lines_vat_eur_statement_and_pdf():
    inv, c, decl = issued_usd_invoice()
    assert inv["currency"] == "USD"
    rate, rate_date = ecb("USD", inv["issue_date"])
    assert Decimal(str(inv["fx"]["rate"])) == rate and inv["fx"]["rate_date"] == rate_date
    assert inv["fx"]["basis"] == "issue_date" and inv["fx"]["source"].startswith("ECB")
    by_code = {ln["code"]: ln for ln in inv["lines"]}
    expected_eur = {"OCEAN_FREIGHT": 245013, "DELIVERY": 38577, "DUTY": 12345, "IMPORT_VAT": 67891}
    assert set(by_code) == set(expected_eur)
    for code, eur in expected_eur.items():
        ln = by_code[code]
        assert ln["eur_amount_cents"] == eur, code  # EUR original kept (Tulli amounts too)
        assert ln["amount_cents"] == hu(Decimal(eur) * rate), (code, ln["amount_cents"], eur, rate)
    # VAT computed in USD per rate group, then stated in EUR at the same ECB rate
    groups: dict[tuple[str, Decimal], int] = {}
    for ln in inv["lines"]:
        groups.setdefault((ln["vat_code"], Decimal(str(ln["vat_rate_pct"]))), 0)
        groups[(ln["vat_code"], Decimal(str(ln["vat_rate_pct"])))] += ln["amount_cents"]
    vat_usd = {k: hu(Decimal(base) * k[1] / 100) for k, base in groups.items()}
    assert ("FI_25_5", Decimal("25.5")) in vat_usd and vat_usd[("FI_25_5", Decimal("25.5"))] > 0
    assert inv["vat_cents"] == sum(vat_usd.values())
    subtotal = sum(ln["amount_cents"] for ln in inv["lines"])
    assert inv["subtotal_cents"] == subtotal and inv["total_cents"] == subtotal + inv["vat_cents"]
    eur_vat = sum(hu(Decimal(v) / rate) for v in vat_usd.values())
    assert inv["eur"]["vat_cents"] == eur_vat
    assert inv["eur"]["subtotal_cents"] == hu(Decimal(subtotal) / rate)
    assert inv["eur"]["total_cents"] == inv["eur"]["subtotal_cents"] + eur_vat
    fi_row = next(b for b in inv["eur"]["vat_breakdown"] if b["vat_code"] == "FI_25_5")
    assert fi_row["vat_cents"] == hu(Decimal(vat_usd[("FI_25_5", Decimal("25.5"))]) / rate)
    # PDF: currency code and the EUR VAT line
    pdf = ops().get(f"/invoices/{inv['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    text = pdf_text(pdf.content)
    assert "USD" in text and "Total USD".replace(" ", "") in text
    assert "".join(f"ALV euroina / VAT in EUR: {eur_fi(eur_vat)}".split()) in text, text[:3000]
    assert "Virtuaaliviivakoodi" not in text  # the Finnish virtual barcode is EUR-only


def test_eur_customer_invoice_unchanged():
    c = make_company()
    s = make_shipment(c, LINES, delivered_days_ago=1)
    r = ops().post(f"/invoices/from-shipment/{s['id']}")
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["currency"] == "EUR" and "fx" not in inv and "eur" not in inv
    assert [ln["amount_cents"] for ln in inv["lines"]] == [245013, 38577]
    assert all("eur_amount_cents" not in ln and "price_currency" not in ln for ln in inv["lines"])
    assert inv["vat_cents"] == hu(Decimal(38577) * Decimal("25.5") / 100)
    pdf = ops().get(f"/invoices/{inv['id']}/pdf")
    text = pdf_text(pdf.content)
    assert "TotalEUR" in text and "VATinEUR" not in text and "USD" not in text


def test_payment_in_usd_realises_fx_against_booked_eur():
    inv, c, _ = issued_usd_invoice(with_customs=False)
    rate_issue, issue_rate_date = ecb("USD", inv["issue_date"])
    # value date of the customer's payment: an ECB day with a different USD rate than the booking rate
    pay_doc = next(d for d in db.fx_rates.find({"date": {"$lt": issue_rate_date}}, sort=[("date", -1)])
                   if Decimal(str(d["rates"]["USD"])) != rate_issue)
    rate_pay = Decimal(str(pay_doc["rates"]["USD"]))
    r = ops().post(f"/invoices/{inv['id']}/mark-paid", json={"paid_at": pay_doc["date"], "reference": f"BANK-{uuid.uuid4().hex[:8]}",
                                                            "amount_cents": inv["total_cents"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paid"
    got = wait_for(lambda: db.invoices.find_one({"id": inv["id"], "fx_realised_cents": {"$exists": True}}), timeout=30)
    assert got, "invoice.paid did not realise FX"
    settled = hu(Decimal(inv["total_cents"]) / rate_pay)
    booked = inv["eur"]["total_cents"]
    assert got["fx_realised"]["settled_eur_cents"] == settled
    assert got["fx_realised"]["booked_eur_cents"] == booked
    assert got["fx_realised_cents"] == settled - booked  # AR: more EUR received = gain
    assert got["fx_realised_cents"] != 0
    assert got["payments"][0]["settled_eur_cents"] == settled
    assert db.audit_log.find_one({"action": "invoice_fx_realised", "entity_id": inv["id"]})


def test_credit_note_of_usd_invoice_keeps_currency_and_rate():
    inv, c, _ = issued_usd_invoice(with_customs=False)
    r = ops().post(f"/invoices/{inv['id']}/credit", json={"reason": "Väärä hinta / wrong price"})
    assert r.status_code == 200, r.text
    cn = r.json()
    assert cn["currency"] == "USD" and cn["fx"]["rate"] == inv["fx"]["rate"] and cn["fx"]["inherited_from"] == inv["number"]
    assert cn["total_cents"] == -inv["total_cents"]
    assert cn["eur"]["vat_cents"] == -inv["eur"]["vat_cents"]


def test_month_end_revaluation_ar_and_ap_idempotent():
    inv, c, _ = issued_usd_invoice(with_customs=False)
    rate_issue, issue_rate_date = ecb("USD", inv["issue_date"])
    as_doc = next(d for d in db.fx_rates.find({"date": {"$lt": issue_rate_date}}, sort=[("date", -1)])
                  if Decimal(str(d["rates"]["USD"])) != rate_issue and "SEK" in d["rates"])
    as_of = as_doc["date"]
    rate_usd, rate_sek = Decimal(str(as_doc["rates"]["USD"])), Decimal(str(as_doc["rates"]["SEK"]))
    # an approved SEK supplier payable (s13 AP shape), booked at an older ECB rate
    old = db.fx_rates.find_one({"date": {"$lt": as_of}}, sort=[("date", 1)])
    pay = {"id": str(uuid.uuid4()), "carrier_invoice_id": "test-ci-" + uuid.uuid4().hex, "supplier": {"name": "Test Åkeri AB"}, "currency": "SEK",
           "amount_cents": 1234567, "eur_cents": hu(Decimal(1234567) / Decimal(str(old["rates"]["SEK"]))),
           "due_date": (date.today() + timedelta(days=10)).isoformat(), "end_to_end_id": "FXTEST"+uuid.uuid4().hex[:28], "status": "approved", "test": True}
    db.payables.insert_one(dict(pay))
    r = ops().post("/finance/fx/revalue", json={"as_of": as_of})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["period"] == as_of[:7] and doc["as_of"] == as_of and doc["rate_date"] == as_of
    ar = next(i for i in doc["items"] if i["id"] == inv["id"])
    assert ar["type"] == "ar" and ar["open_cents"] == inv["total_cents"]
    assert ar["booked_eur_cents"] == inv["eur"]["total_cents"]
    assert ar["revalued_eur_cents"] == hu(Decimal(inv["total_cents"]) / rate_usd)
    assert ar["diff_cents"] == ar["revalued_eur_cents"] - ar["booked_eur_cents"]
    ap = next(i for i in doc["items"] if i["id"] == pay["id"])
    assert ap["type"] == "ap" and ap["booked_eur_cents"] == pay["eur_cents"]
    assert ap["revalued_eur_cents"] == hu(Decimal(1234567) / rate_sek)
    assert ap["diff_cents"] == pay["eur_cents"] - ap["revalued_eur_cents"]  # AP: owing fewer EUR = gain
    assert doc["gain_cents"] == sum(i["diff_cents"] for i in doc["items"] if i["diff_cents"] > 0)
    assert doc["loss_cents"] == -sum(i["diff_cents"] for i in doc["items"] if i["diff_cents"] < 0)
    again = ops().post("/finance/fx/revalue", json={"as_of": as_of}).json()
    assert again["id"] == doc["id"] and again["runs"] == doc["runs"] + 1
    assert db.fx_revaluations.count_documents({"period": as_of[:7]}) == 1
    assert len([i for i in again["items"] if i["id"] in (inv["id"], pay["id"])]) == 2
    listed = ops().get("/finance/fx/revaluations").json()
    assert any(x["id"] == doc["id"] for x in listed["items"])


def test_rates_endpoint_shows_watched_currencies_from_ecb():
    r = ops().get("/finance/fx/rates")
    assert r.status_code == 200, r.text
    body = r.json()
    latest = db.fx_rates.find_one({}, sort=[("date", -1)])
    assert body["date"] == latest["date"]
    for cur in ("USD", "SEK", "NOK", "DKK", "GBP", "CNY", "PLN", "CHF", "JPY"):
        assert body["latest"][cur] == latest["rates"][cur]
        assert len(body["history"][cur]) >= 15  # ~21 ECB days in 30 calendar days
    assert customer().get("/finance/fx/rates").status_code == 403
