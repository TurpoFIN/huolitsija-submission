"""Customs POA PDF: the representative's Business ID and EORI read "pending registration" until the company has them."""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pypdf import PdfReader  # noqa: E402

from core import config  # noqa: E402
from services.poa_pdf import render_poa_pdf  # noqa: E402

PRINCIPAL = {"name": "E2E Testi 084712 Oy", "business_id": "3994713-8", "eori": "FI3994713-8", "vat_id": "FI39947138",
             "address": {"street": "Mannerheimintie 12", "postal_code": "00100", "city": "Helsinki", "country": "FI"}}
POA = {"id": "poa-1", "type": "direct", "scope": ["import"], "valid_from": "2026-10-24", "valid_to": "2027-10-24"}


def text(pdf: bytes) -> str:
    return " ".join(" ".join(p.extract_text().split()) for p in PdfReader(io.BytesIO(pdf)).pages)


def test_representative_ids_pending_before_incorporation(monkeypatch):
    monkeypatch.setattr(config, "COMPANY_BUSINESS_ID", "", raising=False)
    monkeypatch.setattr(config, "COMPANY_EORI", "", raising=False)
    t = text(render_poa_pdf(PRINCIPAL, POA))
    rep = t[t.index("Representative / Edustaja"):]
    assert rep.count("pending registration") == 2 and "Business ID —" not in rep and "EORI —" not in rep
    assert "Business ID 3994713-8" in t and "EORI FI3994713-8" in t  # the principal's own IDs are untouched


def test_representative_ids_printed_once_configured(monkeypatch):
    monkeypatch.setattr(config, "COMPANY_BUSINESS_ID", "1234567-8", raising=False)
    monkeypatch.setattr(config, "COMPANY_EORI", "FI1234567-8", raising=False)
    rep = text(render_poa_pdf(PRINCIPAL, POA))
    rep = rep[rep.index("Representative / Edustaja"):]
    assert "Business ID 1234567-8" in rep and "EORI FI1234567-8" in rep and "pending registration" not in rep
