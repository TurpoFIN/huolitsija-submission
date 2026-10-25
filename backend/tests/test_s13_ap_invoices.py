"""s13 carrier invoices: Finvoice 3.0 / PDF ingestion, audit, matching to accruals, Autopilot decisions, disputes.

Front-door against the running backend (S13_API). Finvoice files come from scripts/s13_make_carrier_invoices.py and are
validated here with lxml against Finanssiala's official Finvoice 3.0 XSD. Amounts are checked against Decimal sums,
FX against the ECB rate stored in fx_rates, dispute e-mail by reading the carrier's greenmail inbox over IMAP."""
from __future__ import annotations

import os
import random
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

from test_s13_ap_support import (accrue, ap_shipment, autopilot, db, finvoice_for, fi_iban, gen, imap_messages,
                                 make_carrier, ops, pending_cards, seller_of, upload)

XSD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "finvoice", "Finvoice3.0.xsd"))
FREIGHT = 2 * 187_654 + 2 * 11_728  # OCEAN_FREIGHT + THC_DEST cost (leg 1)
DELIVERY = 30_150
FINAL = ("approved", "needs_review", "disputed", "duplicate", "rejected")


def settled(ci_id: str, timeout: float = 60) -> dict:
    """Wait until the processing job has made its decision (status final and the card/decision recorded)."""
    end, doc = time.time() + timeout, None
    while time.time() < end:
        doc = ops().get(f"/finance/carrier-invoices/{ci_id}").json()
        st = doc["status"]
        if st == "approved" or st == "rejected":
            return doc
        if st == "disputed" and (doc.get("dispute") or {}).get("approval_id"):
            return doc
        if st in ("needs_review", "duplicate") and doc.get("decision"):
            return doc
        if st == "matched" and (doc.get("decision") or {}).get("outcome") == "owner_queue":
            return doc
        time.sleep(0.7)
    raise AssertionError(f"invoice {ci_id} not settled: {doc and doc['status']}")


def xsd_valid(xml: bytes) -> bool:
    return etree.XMLSchema(etree.parse(XSD)).validate(etree.fromstring(xml))


def test_generated_finvoice_is_valid_against_official_xsd():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    xml, _ = finvoice_for(s, carrier, fi_iban(), freight_net=FREIGHT, delivery_net=DELIVERY)
    assert xsd_valid(xml)
    broken = xml.replace(b"<InvoiceNumber>", b"<InvoiceNo>").replace(b"</InvoiceNumber>", b"</InvoiceNo>")
    assert not xsd_valid(broken)


def test_finvoice_within_tolerance_autopilot_on_approves_and_matches():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    acc = accrue(s)
    assert acc["expected_eur_cents"] == FREIGHT + DELIVERY
    over = 1_000  # within max(25.00 EUR, 2 %)
    iban = fi_iban()
    xml, inv = finvoice_for(s, carrier, iban, freight_net=FREIGHT + over, delivery_net=DELIVERY)
    with autopilot(True):
        doc = settled(upload(xml, f"finvoice-{inv['number']}.xml", "application/xml")["id"])
    assert doc["status"] == "approved", (doc["status"], doc.get("decision"), [c for c in doc["audit"]["checks"] if not c["ok"]])
    net = FREIGHT + over + DELIVERY
    vat = int((Decimal(DELIVERY) * Decimal("25.5") / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    assert (doc["net_cents"], doc["vat_cents"], doc["total_cents"]) == (net, vat, net + vat)
    assert doc["eur"] == {"net_cents": net, "vat_cents": vat, "total_cents": net + vat}
    assert doc["supplier"]["carrier_id"] == carrier["id"] and doc["supplier"]["iban"] == iban
    assert doc["source"] == "finvoice" and doc["vat_treatment"] == "domestic"
    assert doc["extraction"]["xsd"]["valid"] is True
    assert all(c["ok"] for c in doc["audit"]["checks"]), doc["audit"]["checks"]
    assert [(m["shipment_ref"], m["accrued_eur_cents"], m["invoiced_eur_cents"], m["variance_eur_cents"]) for m in doc["matches"]] \
        == [(s["reference"], FREIGHT + DELIVERY, net, over)]
    row = db.cost_accruals.find_one({"id": acc["id"]})
    assert row["status"] == "matched" and row["actual_eur_cents"] == net and row["variance_eur_cents"] == over
    assert doc["id"] in row["carrier_invoice_ids"]
    d = db.agent_decisions.find_one({"subject_id": doc["id"], "action": "approve_carrier_invoice"})
    assert d and d["outcome"] == "auto"


def test_finvoice_within_tolerance_autopilot_off_goes_to_owner_then_approval_approves():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    accrue(s)
    xml, inv = finvoice_for(s, carrier, fi_iban(), freight_net=FREIGHT - 4_000, delivery_net=DELIVERY)
    with autopilot(False):
        doc = settled(upload(xml, "lasku.xml", "text/xml")["id"])
    assert doc["status"] == "matched"
    cards = pending_cards(doc["id"], "carrier_invoice_review")
    assert len(cards) == 1 and any(r["code"] == "autopilot_off" for r in cards[0]["reasons"])
    assert cards[0]["data"]["matches"][0]["variance_eur_cents"] == -4_000
    r = ops().post(f"/approvals/{cards[0]['id']}/decide", json={"decision": "approved", "note": "ok"})
    assert r.status_code == 200, r.text
    doc = ops().get(f"/finance/carrier-invoices/{doc['id']}").json()
    assert doc["status"] == "approved" and doc["amount_due_cents"] == doc["total_cents"]


def test_over_tolerance_disputed_mail_sent_and_credit_note_nets_it():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    acc = accrue(s)
    over = 50_000  # 500 EUR > max(25 EUR, 2 % of 4 289.14 EUR)
    iban = fi_iban()
    xml, inv = finvoice_for(s, carrier, iban, freight_net=FREIGHT + over, delivery_net=DELIVERY)
    with autopilot(True):
        doc = settled(upload(xml, "inv.xml", "application/xml")["id"])
        assert doc["status"] == "disputed"
        cards = pending_cards(doc["id"], "carrier_invoice_dispute")
        assert len(cards) == 1
        m = cards[0]["data"]["matches"][0]
        assert (m["accrued_eur_cents"], m["invoiced_eur_cents"], m["variance_eur_cents"]) == \
            (FREIGHT + DELIVERY, FREIGHT + DELIVERY + over, over)
        assert doc["dispute"]["sent_at"] and doc["dispute"]["to"] == carrier["contact_email"]
        assert db.cost_accruals.find_one({"id": acc["id"]})["status"] == "disputed"
        mails = imap_messages(carrier["contact_email"], inv["number"])
        assert len(mails) == 1, "credit-note request not delivered to the carrier's mailbox"
        body = mails[0].get_body(preferencelist=("plain",)).get_content()
        assert "Hyvityspyyntö" in str(mails[0]["Subject"]) and "Credit note request" in str(mails[0]["Subject"])
        assert "500.00 EUR" in body and "hyvityslaskua" in body and "credit note" in body
        assert str(mails[0]["Message-ID"]) == doc["dispute"]["message_id"]
        # The carrier sends a credit note for the difference -> the dispute nets within tolerance -> approved.
        cxml, cinv = finvoice_for(s, carrier, iban, freight_net=-over, delivery_net=0, type_code="INV02",
                                  original=inv["number"])
        cn = settled(upload(cxml, "credit.xml", "application/xml")["id"])
    orig = ops().get(f"/finance/carrier-invoices/{doc['id']}").json()
    assert cn["document_type"] == "credit_note" and cn["credit_for"] == doc["id"] and cn["total_cents"] == -over
    assert orig["status"] == "approved", orig.get("credit_netting")
    assert orig["amount_due_cents"] == orig["total_cents"] - over
    assert ops().get(f"/finance/carrier-invoices/{cn['id']}").json()["status"] == "approved"
    assert not pending_cards(doc["id"])
    row = db.cost_accruals.find_one({"id": acc["id"]})
    assert row["actual_eur_cents"] == FREIGHT + DELIVERY and row["status"] == "matched"


def test_usd_finvoice_converted_at_ecb_rate_of_invoice_date():
    carrier = make_carrier("US", name=f"Test Ocean Lines {random.randint(1000, 9999)} Inc", vat_id="")
    s = ap_shipment(carrier)
    accrue(s)
    inv_date = date.today() - timedelta(days=9)
    while inv_date.weekday() >= 5:
        inv_date -= timedelta(days=1)
    fx = db.fx_rates.find_one({"date": {"$lte": inv_date.isoformat()}, "rates.USD": {"$exists": True}}, sort=[("date", -1)])
    if not fx or (inv_date - date.fromisoformat(fx["date"])).days > 4:
        assert ops().post("/finance/fx/refresh", json={"kind": "90d"}).status_code == 200
        fx = db.fx_rates.find_one({"date": {"$lte": inv_date.isoformat()}, "rates.USD": {"$exists": True}}, sort=[("date", -1)])
    rate = Decimal(str(fx["rates"]["USD"]))
    usd_net = int((Decimal(FREIGHT + DELIVERY) * rate).quantize(Decimal("1")))  # same cost billed in USD
    xml, inv = finvoice_for(s, carrier, fi_iban(), freight_net=usd_net, delivery_net=0, currency="USD",
                            invoice_date=inv_date.isoformat(), vat=False)
    with autopilot(False):
        doc = settled(upload(xml, "usd.xml", "application/xml")["id"])
    want = int((Decimal(usd_net) / rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    assert doc["currency"] == "USD" and doc["net_cents"] == usd_net
    assert doc["eur"]["net_cents"] == want and doc["eur"]["total_cents"] == want
    assert doc["fx"]["rate"] == fx["rates"]["USD"] and doc["fx"]["rate_date"] == fx["date"]
    assert doc["vat_treatment"] == "non_eu_reverse_charge"
    assert doc["matches"][0]["invoiced_eur_cents"] == want
    assert abs(doc["matches"][0]["variance_eur_cents"]) <= 1 and doc["status"] == "matched"


def test_pdf_without_model_needs_review_and_invents_nothing():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    accrue(s)
    inv = gen.invoice_dict(number=f"PDF{random.randint(10**5, 10**6)}", seller=seller_of(carrier, fi_iban()),
                           rows=[{"name": "Ocean freight", "amount_cents": FREIGHT, "vat_rate_pct": 0,
                                  "free_text": [f"{s['reference']} {s['_container']}"]},
                                 {"name": "Delivery", "amount_cents": DELIVERY, "vat_rate_pct": 25.5}])
    pdf = gen.make_pdf(inv)
    with autopilot(True):
        doc = settled(upload(pdf, f"invoice-{inv['number']}.pdf", "application/pdf")["id"])
    if doc["extraction"].get("reason") != "llm_unavailable":
        # A model is configured (integration): it must read what is printed, nothing else.
        assert doc["number"] == inv["number"]
        assert doc["total_cents"] == sum(r["amount_cents"] + r["vat_cents"] for r in inv["rows"])
        return
    assert doc["status"] == "needs_review"
    assert doc["number"] is None and doc["total_cents"] is None and doc["net_cents"] is None
    assert doc["supplier"]["name"] is None and doc["supplier"]["iban"] is None and doc["lines"] == []
    assert doc["matches"] == [] and doc["extraction"]["fields"] == {}
    cards = pending_cards(doc["id"], "carrier_invoice_review")
    assert len(cards) == 1 and cards[0]["data"]["reasons"][0]["code"] == "extraction_failed"
    r = ops().post(f"/finance/carrier-invoices/{doc['id']}/approve", json={"note": "x"})
    assert r.status_code == 409
    # A person types the fields in -> the same audit and matching runs.
    fields = {"supplier": {"name": carrier["name"], "vat_id": carrier["vat_id"], "iban": inv["seller"]["iban"],
                           "bic": "NDEAFIHH"},
              "number": inv["number"], "invoice_date": inv["invoice_date"], "due_date": inv["due_date"], "currency": "EUR",
              "net_cents": FREIGHT + DELIVERY, "vat_cents": inv["rows"][1]["vat_cents"],
              "total_cents": FREIGHT + DELIVERY + inv["rows"][1]["vat_cents"],
              "lines": [{"description": "Ocean freight", "amount_cents": FREIGHT, "vat_rate_pct": 0, "vat_cents": 0,
                         "shipment_ref": s["reference"]},
                        {"description": "Delivery", "amount_cents": DELIVERY, "vat_rate_pct": 25.5,
                         "vat_cents": inv["rows"][1]["vat_cents"], "shipment_ref": s["reference"]}]}
    with autopilot(False):
        r = ops().post(f"/finance/carrier-invoices/{doc['id']}/rematch", json={"fields": fields})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["extraction"]["method"] == "manual" and doc["status"] == "matched"
    assert doc["matches"][0]["variance_eur_cents"] == 0


def test_iban_change_goes_to_owner_even_with_autopilot():
    carrier = make_carrier("FI")
    s1, s2 = ap_shipment(carrier), ap_shipment(carrier)
    accrue(s1)
    accrue(s2)
    iban_a, iban_b = fi_iban(), fi_iban()
    x1, _ = finvoice_for(s1, carrier, iban_a, freight_net=FREIGHT, delivery_net=DELIVERY)
    x2, _ = finvoice_for(s2, carrier, iban_b, freight_net=FREIGHT, delivery_net=DELIVERY)
    with autopilot(True):
        d1 = settled(upload(x1, "a.xml", "application/xml")["id"])
        assert d1["status"] == "approved"
        d2 = settled(upload(x2, "b.xml", "application/xml")["id"])
    assert d2["status"] == "matched"
    chk = next(c for c in d2["audit"]["checks"] if c["rule"] == "iban_change")
    assert chk["ok"] is False and chk["known"] == [iban_a] and iban_b in chk["message_en"]
    cards = pending_cards(d2["id"], "carrier_invoice_review")
    assert len(cards) == 1 and [r["code"] for r in cards[0]["data"]["reasons"]] == ["check_iban_change"]


def test_duplicate_file_and_duplicate_number():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    accrue(s)
    iban = fi_iban()
    xml, inv = finvoice_for(s, carrier, iban, freight_net=FREIGHT, delivery_net=DELIVERY)
    with autopilot(False):
        first = upload(xml, "same.xml", "application/xml")
        again = upload(xml, "same-again.xml", "application/xml")
        assert again["id"] == first["id"] and again["duplicate_of_existing"] is True
        assert db.carrier_invoices.count_documents({"sha256": first["sha256"]}) == 1
        settled(first["id"])
        # Same supplier and invoice number in a different file -> duplicate, owner decides.
        xml2, _ = finvoice_for(s, carrier, iban, freight_net=FREIGHT + 10, delivery_net=DELIVERY, number=inv["number"])
        dup = settled(upload(xml2, "resend.xml", "application/xml")["id"])
    assert dup["id"] != first["id"] and dup["status"] == "duplicate" and dup["duplicate_of"] == first["id"]
    assert len(db.carrier_invoices.find_one({"id": first["id"]})["duplicate_receipts"]) == 1
    assert pending_cards(dup["id"], "carrier_invoice_review")
