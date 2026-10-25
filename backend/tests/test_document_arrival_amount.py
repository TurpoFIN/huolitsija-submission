"""Arrival PDF financial wording; labelled in-memory data, no business records or mail."""
import io
import os
from pathlib import Path

import pytest
from pypdf import PdfReader

from services.tradedocs import pdf


def context():
    location = {"name": "Demo terminal", "country": "FI"}
    return {
        "issuer": {"name": "DEMO issuer — no cargo", "address": {}},
        "shipment": {"reference": "DEMO-NO-CARGO", "mode": "road", "eta": "2026-10-25", "demo": True},
        "shipper": {"name": "Demo shipper"}, "consignee": {"name": "Demo consignee"}, "notify": None,
        "main_leg": {"carrier": "Demo carrier"}, "refs": {}, "port_of_discharge": location,
        "port_of_loading": location, "place_of_delivery": location,
        "goods": [{"seq": 1, "description": "Demo goods — no cargo", "packages": 1, "gross_kg": 10}],
        "containers": [], "customs": {"required": False}, "delivery_carrier": None,
        "freight": {"terms": "collect", "currency": "EUR"},
    }


@pytest.mark.parametrize("lang", ["fi", "en"])
@pytest.mark.parametrize("case,amount", [("missing", None), ("null", None), ("zero", 0), ("positive", 12550)])
@pytest.mark.parametrize("terms", ["collect", "prepaid"])
def test_pdf_preserves_unknown_zero_and_positive_without_claiming_payment(lang, case, amount, terms):
    ctx = context()
    ctx["freight"]["terms"] = terms
    if case != "missing":
        ctx["freight"]["total_cents"] = amount
    blob = pdf.render("arrival_notice", ctx, lang=lang,
                      meta={"doc_no": "AN-DEMO", "version": 1, "status": "draft", "issued_at": "2026-10-25"})
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(blob)).pages)
    assert ("Maksutilaa ei vahvistettu" if lang == "fi" else "Payment status unverified") in text
    assert "Nothing due before release" not in text and "Ei maksettavaa ennen luovutusta" not in text
    expected = ("Rahtimäärä puuttuu" if lang == "fi" else "Freight amount unavailable") if amount is None else pdf.money(amount / 100, "EUR", lang)
    assert expected in text
    assert terms.capitalize() in text
    if os.getenv("DOCUMENT_EVIDENCE_DIR"):
        out = Path(os.environ["DOCUMENT_EVIDENCE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / f"arrival-{terms}-{case}-{lang}.pdf").write_bytes(blob)
