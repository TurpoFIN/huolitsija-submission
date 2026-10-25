"""Invoices of a test tenant print a clearly marked TEST bank account: no IBAN, no BIC, no virtual barcode, so a TEST
invoice can never be paid to a made-up or real account. Production invoices keep the configured seller account."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pypdf import PdfReader

from services.billing import _seller
from services.invoice_pdf import render_invoice_pdf

SETTINGS = {"seller_iban": "FI2112345600000785", "seller_bic": "NDEAFIHH"}


def _text(inv):
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(render_invoice_pdf(inv))).pages)


def _inv(seller):
    return {"id": "i1", "kind": "invoice", "status": "open", "number": "HUO-INV-2026-000901", "reference": "20260009013",
            "issue_date": "2026-10-25", "due_date": "2026-11-08", "supply_date": "2026-10-25", "currency": "EUR",
            "total_cents": 12400, "subtotal_cents": 10000, "vat_cents": 2400, "lines": [], "seller": seller,
            "buyer": {"name": "E2E Testi Oy"}}


def test_test_tenant_seller_has_no_account_and_production_keeps_it():
    assert _seller(SETTINGS, {"id": "c", "test_tenant": True}) | {} == {**_seller(SETTINGS), "iban": None, "bic": None,
                                                                       "test_bank": True}
    real = _seller(SETTINGS, {"id": "c"})
    assert real["iban"] == "FI2112345600000785" and "test_bank" not in real


def test_test_invoice_pdf_prints_marked_test_account_without_iban():
    t = _text(_inv(_seller(SETTINGS, {"id": "c", "test": True})))
    assert "TESTITILI – ei maksettavissa / TEST ACCOUNT – not payable" in t
    assert "FI21" not in t and "NDEAFIHH" not in t and "Virtuaaliviivakoodi" not in t
    t2 = _text(_inv(_seller(SETTINGS, {"id": "c"})))
    assert "FI21" in t2.replace(" ", "") and "TESTITILI" not in t2
