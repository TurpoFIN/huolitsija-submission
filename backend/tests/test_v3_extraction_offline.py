"""Extraction when the AI provider is not configured (a real deployment state, not a mock): the document must end in
needs_review with a reason a person can act on, and no field may be invented. Also a scanned (image-only) PDF and an
unreadable spreadsheet. Runs in-process on its own database (huo_v3_test_offline) with real GridFS storage.

Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_extraction_offline.py
"""
import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if "core.db" not in sys.modules:
    os.environ["DB_NAME"] = "huo_v3_test_offline"
for k in ("OPENAI_API_KEY",):
    os.environ.pop(k, None)
os.environ["LLM_PROVIDER"] = "none"

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import pytest  # noqa: E402

from core import config  # noqa: E402

if config.OPENAI_API_KEY:  # core was imported earlier in this process with a key
    pytest.skip("run this module in its own process (AI key already loaded)", allow_module_level=True)

from core.db import db  # noqa: E402
from services import documents, extraction  # noqa: E402
from services.seed_customs import render_invoice_pdf  # noqa: E402


def run(coro):
    return LOOP.run_until_complete(coro)


ACTOR = {"type": "system", "id": None, "name": "pytest"}


def _invoice() -> bytes:
    lines = [{"description": "Oak chair", "hs": "9401.61.00", "origin": "VN", "qty": 2, "unit": "pcs", "unit_price": 10.0,
              "amount": 20.0, "net": 10, "gross": 12}]
    return render_invoice_pdf({"number": "OFF-1", "date": "2026-10-01", "po": "-", "seller": {"name": "S", "address": "A",
                               "country": "VN", "country_name": "Viet Nam", "vat": "1"}, "buyer": {"name": "B", "address": "C",
                               "country": "FI", "vat": "FI1", "eori": "FI1"}, "incoterm": "FOB", "place": "X", "currency": "USD",
                               "lines": lines, "packages": 1, "package_type": "cartons", "gross": 12, "net": 10}, sample=False)


def _scan() -> bytes:
    """An image-only PDF (what a scanner produces): no text layer at all."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.text((100, 100), "COMMERCIAL INVOICE  No. SCAN-77  Total USD 1,234.00", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


async def _make(data: bytes, name: str, doc_type=None):
    doc = await documents.create_document(company_id="test-offline", shipment_id=None, doc_type=doc_type, filename=name,
                                          data=data, actor=ACTOR, source="pytest", extract=False)
    await db.documents.update_one({"id": doc["id"]}, {"$set": {"extraction.status": "pending"}})
    await extraction.extract_document(doc["id"])
    return await db.documents.find_one({"id": doc["id"]}, {"_id": 0})


def test_text_pdf_without_ai_is_needs_review_with_reason():
    d = run(_make(_invoice(), "invoice.pdf"))
    ex = d["extraction"]
    assert ex["status"] == "needs_review"
    assert "AI is not configured" in ex["reason"]
    assert ex["fields"] == {} and ex["line_items"] == []      # nothing invented
    assert ex.get("method") == "text"                           # the text layer was found; only the model is missing


def test_scanned_pdf_without_ai_routes_to_vision_and_still_needs_review():
    d = run(_make(_scan(), "scan.pdf"))
    ex = d["extraction"]
    assert ex["status"] == "needs_review" and ex["fields"] == {}
    assert ex.get("method") == "vision"                         # no text layer: page images would go to the model


def test_input_builder_renders_scans_as_images():
    inp = extraction.build_input(_scan(), "application/pdf")
    assert inp["method"] == "vision" and inp["files"] and inp["files"][0][0] == "image/png"
    inp = extraction.build_input(_invoice(), "application/pdf")
    assert inp["method"] == "text" and "OFF-1" in inp["text"]


def test_post_check_catches_inconsistent_values_without_a_model():
    """The deterministic checks that run after every extraction (and after every user edit)."""
    w = extraction.post_check("commercial_invoice", {
        "invoice_number": {"value": "X1"}, "total_amount": {"value": 95.0}, "currency": {"value": "XYZ"},
        "incoterm": {"value": "FOB"}, "subtotal_amount": {"value": 100.0}, "buyer_eori": {"value": "FI1234567-8"},
        "seller_country": {"value": "ZZ"}, "total_gross_weight_kg": {"value": 10}, "total_net_weight_kg": {"value": 12}},
        [{"description": "a", "quantity": 2, "unit_price": 10, "amount": 25, "country_of_origin": "CN"},
         {"description": "b", "quantity": 1, "unit_price": 70, "amount": 70, "country_of_origin": "CN"}])
    by = {x["code"]: x for x in w}
    assert "XYZ" in by["currency"]["message"]                                  # not ISO 4217
    assert "check digit" in by["eori_check"]["message"]                        # FI1234567-8: Y-tunnus check digit is 1
    assert "ZZ" in by["country_code"]["message"]                               # not ISO 3166
    assert "95.00" in by["line_sum"]["message"] and by["line_sum"]["severity"] == "error"
    assert "2 × 10 = 20.00" in by["line_math"]["message"]
    assert by["weights"]["severity"] == "error"                                 # net > gross
    ok = extraction.post_check("commercial_invoice", {"currency": {"value": "USD"}, "seller_country": {"value": "VN"},
                                                      "buyer_eori": {"value": "FI3596623-5"}}, [])
    assert not {"currency", "country_code", "eori_check"} & {x["code"] for x in ok}


def teardown_module(_m):
    run(db.client.drop_database("huo_v3_test_offline")) if hasattr(db, "client") else None
