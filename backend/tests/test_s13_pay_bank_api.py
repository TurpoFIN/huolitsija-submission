"""BANK_PAYMENT_MODE=api against the real Nordea Web Services endpoint with Nordea's published shared demo certificate
(customer 11111111, target 11111111A1; https://www.nordea.fi/en/business/our-services/mobile-online-services/testing.html).

What this proves: our SOAP WS-Security signature and the signed ApplicationRequest are accepted by Nordea's production
file-transfer endpoint (a file list and a file come back with code 00), and an api-mode payment run uploads its
pain.001 there and records the bank's real answer. What it does not prove: with the demo certificate Nordea answers
UploadFile with code 23 ("Content processing error. Download feedback.") and refuses pain.002 / camt.054 downloads
(code 35 "Authorization failed."), so the upload -> pain.002 round trip needs a Web Services agreement certificate.

The demo certificate expires 2026-11-11 (Nordea publishes a new one on the testing page); after that these tests skip.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta

import pytest

from test_s13_support import db, iso, now, ops

loop = asyncio.new_event_loop()   # Motor binds to the loop at import: create it before importing backend modules
asyncio.set_event_loop(loop)
os.environ.update(BANK_API_PROVIDER="nordea", BANK_API_DEMO="true")
from services import bank_api  # noqa: E402

NORDEA_TEST_IBAN, NORDEA_TEST_IBAN_2, NORDEA_BIC = "FI4819503000000010", "FI7429501800000014", "NDEAFIHH"


def run(coro):
    return loop.run_until_complete(coro)


def bank(command: str, **kw) -> dict:
    try:
        return run(bank_api.call(command, **kw))
    except bank_api.BankApiError as err:
        if err.code == "bank_api_cert":
            pytest.skip(f"Nordea demo certificate: {err.en}")
        raise


def test_nordea_accepts_our_signatures_and_returns_a_file():
    info = bank("GetUserInfo")
    assert info["http_status"] == 200 and info["fault"] is None
    assert info["code"] == "24"  # Nordea's documented demo answer: connection and signature OK, no service list
    lst = bank("DownloadFileList", file_type="VKEUR", status="ALL")
    assert lst["code"] == "00", (lst["code"], lst["text"])
    assert lst["file_descriptors"] and lst["file_descriptors"][0]["FileType"] == "VKEUR"
    ref = lst["file_descriptors"][0]["FileReference"]
    got = bank("DownloadFile", file_type="VKEUR", file_refs=[ref], status="ALL")
    assert got["code"] == "00" and got["content"] and got["content"].startswith(b"VK01")  # Nordea exchange-rate file


@pytest.mark.skipif(os.environ.get("HUOLITSIJA_ALLOW_BANK_UPLOAD") != "1",
                    reason="Bank upload requires explicit HUOLITSIJA_ALLOW_BANK_UPLOAD=1")
def test_api_mode_payment_run_uploads_and_records_the_bank_answer():
    os.environ["BANK_PAYMENT_MODE"] = "api"
    os.environ["BANK_SERVICE_CODE"] = "111111111"
    from services import payables  # noqa: E402

    before = ops().get("/settings").json()
    r = ops().patch("/settings", json={"seller_iban": NORDEA_TEST_IBAN, "seller_bic": NORDEA_BIC})
    assert r.status_code == 200, r.text
    try:
        ci = {"id": str(uuid.uuid4()), "supplier": {"carrier_id": str(uuid.uuid4()), "name": "Nordea Demo Creditor Oy",
                                                    "country": "FI", "iban": NORDEA_TEST_IBAN_2, "bic": NORDEA_BIC},
              "number": f"CI-{uuid.uuid4().hex[:6]}", "invoice_date": date.today().isoformat(),
              "due_date": (date.today() + timedelta(days=1)).isoformat(), "currency": "EUR", "net_cents": 1234,
              "vat_cents": 0, "total_cents": 1234, "eur": {"net_cents": 1234, "vat_cents": 0, "total_cents": 1234},
              "payment": {"iban": NORDEA_TEST_IBAN_2, "bic": NORDEA_BIC, "reference": "12344"}, "status": "approved",
              "test": True, "created_at": iso(now())}
        db.carrier_invoices.insert_one(dict(ci))
        p = run(payables.create_payable(ci))
        pr = run(payables.create_payment_run(None, None, payable_ids=[p["id"]]))
        assert pr["status"] in ("pending_approval", "approved")
        result = run(payables.approve_run(pr["id"], {"id": "test", "name": "pytest"}))
        doc = db.payment_runs.find_one({"id": pr["id"]})
        assert doc["bank"]["mode"] == "api"
        if doc["status"] == "sent":  # the bank accepted the upload (agreement certificate)
            assert doc["bank"]["response"]["code"] == "00"
            assert db.payables.find_one({"id": p["id"]})["status"] == "sent"
        else:  # demo certificate today: the bank's refusal is recorded and a person gets a card
            assert result["status"] == "approved"
            err = doc["bank"]["error"]
            assert err["code"] == "bank_api_upload" and "23" in err["message_en"], err
            card = db.approvals.find_one({"kind": "payment_run", "ref_id": pr["id"], "status": "pending",
                                          "data.purpose": "upload_failed"})
            assert card and "23" in card["summary"]
            db.approvals.update_one({"id": card["id"]}, {"$set": {"status": "cancelled"}})
        db.payables.update_one({"id": p["id"]}, {"$set": {"status": "cancelled"}})
    finally:
        os.environ["BANK_PAYMENT_MODE"] = "file"
        ops().patch("/settings", json={"seller_iban": before.get("seller_iban") or "", "seller_bic": before.get("seller_bic") or ""})


def test_carrier_invoice_approved_event_creates_one_payable():
    """In-process (same event bus as the server code): agent A's `carrier_invoice.approved` -> one payable, idempotent."""
    from core.events import emit  # noqa: E402
    from services import payables  # noqa: E402,F401  (registers the handler)

    ci = {"id": str(uuid.uuid4()), "supplier": {"carrier_id": str(uuid.uuid4()), "name": "Tapahtuma Rahti Oy",
                                                "country": "FI", "iban": NORDEA_TEST_IBAN_2, "bic": NORDEA_BIC},
          "number": "CI-EVT-1", "invoice_date": date.today().isoformat(),
          "due_date": (date.today() + timedelta(days=14)).isoformat(), "currency": "SEK", "net_cents": 1_000_000,
          "vat_cents": 0, "total_cents": 1_000_000, "eur": {"net_cents": 87_000, "vat_cents": 0, "total_cents": 87_000},
          "payment": {"iban": NORDEA_TEST_IBAN_2, "bic": NORDEA_BIC, "reference": "RF18 5390 0754 7034"},
          "status": "approved", "test": True, "created_at": iso(now())}
    db.carrier_invoices.insert_one(dict(ci))
    run(emit("carrier_invoice.approved", {"carrier_invoice": ci}))
    run(emit("carrier_invoice.approved", {"carrier_invoice": ci}))
    rows = list(db.payables.find({"carrier_invoice_id": ci["id"]}, {"_id": 0}))
    assert len(rows) == 1
    p = rows[0]
    assert (p["currency"], p["amount_cents"], p["eur_cents"], p["status"]) == ("SEK", 1_000_000, 87_000, "approved")
    assert p["reference_type"] == "rf" and p["due_date"] == ci["due_date"] and p["test"] is True
    assert db.carrier_invoices.find_one({"id": ci["id"]})["payable_id"] == p["id"]
    assert not db.event_failures.find_one({"event": "carrier_invoice.approved", "error": {"$regex": ci["id"]}})
    db.payables.update_one({"id": p["id"]}, {"$set": {"status": "cancelled"}})
