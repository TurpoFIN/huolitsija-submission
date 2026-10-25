"""Documents: upload validation (adversarial files), AI extraction of a generated commercial invoice and packing list,
DOC-3 tie-out, user corrections, scoping. Real API, real storage, real AI provider (extraction tests skip only when
the server has no AI key). No mocks.

Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_documents.py
"""
import io
import uuid

import pytest

import test_v3_support as S


@pytest.fixture(scope="module")
def ops():
    return S.client(S.OPS)


@pytest.fixture(scope="module")
def cust():
    return S.client(S.CUSTOMER)


@pytest.fixture(scope="module")
def ship(request):
    created = S.make_import_shipment()
    request.addfinalizer(lambda: S.cleanup_import_shipment(created))
    return created


def upload(c, name, data, **form):
    return c.post("/documents", files={"file": (name, io.BytesIO(data), "application/octet-stream")},
                  data={k: v for k, v in form.items() if v is not None})


# ---- adversarial uploads ------------------------------------------------------------------------
def _encrypted_pdf() -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(595, 842)
    w.encrypt(user_password="secret", owner_password="owner")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize("name,data,status,needle", [
    ("empty.pdf", b"", 400, "empty"),
    ("invoice.pdf", b"MZ\x90\x00\x03" + b"\x00" * 200, 415, "Unsupported"),          # Windows executable named .pdf
    ("invoice.pdf", b"<html><script>alert(1)</script></html>", 415, "Unsupported"),
    ("broken.pdf", b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\n%%EOF", 400, "PDF"),        # truncated PDF
    ("data.xlsx", b"PK\x03\x04" + b"\x00" * 100, 400, None),                              # zip that is not a workbook
])
def test_rejects_bad_files_with_a_reason(cust, ship, name, data, status, needle):
    r = upload(cust, name, data, shipment_id=ship["shipment"]["id"])
    if name == "data.xlsx" and r.status_code == 200:
        # accepted as a spreadsheet by magic bytes; extraction must then fail honestly, never invent data
        doc = S.wait(lambda: (lambda d: d if d["extraction"]["status"] not in ("pending", "processing") else None)(
            cust.get(f"/documents/{r.json()['id']}").json()), timeout=120)
        assert doc["extraction"]["status"] in ("failed", "needs_review") and not doc["extraction"]["fields"]
        cust.delete(f"/documents/{doc['id']}")
        return
    assert r.status_code == status, r.text
    if needle:
        assert needle.lower() in r.json()["detail"].lower()


def test_password_protected_pdf(cust, ship):
    r = upload(cust, "locked.pdf", _encrypted_pdf(), shipment_id=ship["shipment"]["id"])
    assert r.status_code == 400 and "password-protected" in r.json()["detail"]


def test_oversize_file(cust, ship):
    r = upload(cust, "big.pdf", b"%PDF-1.4\n" + b"0" * (20 * 1024 * 1024 + 10), shipment_id=ship["shipment"]["id"])
    assert r.status_code == 413 and "20 MB" in r.json()["detail"]


def test_hostile_filename_is_neutralised(cust, ship):
    from services.seed_customs import render_packing_list_pdf  # any valid PDF

    pdf = render_packing_list_pdf({"number": "X", "date": "2026-10-01", "invoice_number": "X", "shipper": {"name": "A", "address": "B", "country_name": "C"},
                                   "consignee": {"name": "D", "address": "E"}, "lines": [], "packages": 0, "package_type": "cartons",
                                   "gross": 0, "net": 0, "volume": 0})
    r = upload(cust, "../../etc/passwd\x00\x07 Ääkköset 📦.pdf", pdf, type="other", shipment_id=ship["shipment"]["id"])
    assert r.status_code == 200, r.text
    doc = r.json()
    assert "/" not in doc["filename"] and "\x00" not in doc["filename"] and "Ääkköset" in doc["filename"]
    assert doc["extraction"]["status"] == "not_applicable"  # type "other" is stored, not extracted
    f = cust.get(f"/documents/{doc['id']}/file", params={"download": 1})
    assert f.status_code == 200 and f.content[:4] == b"%PDF"
    assert cust.delete(f"/documents/{doc['id']}").status_code == 200
    assert cust.get(f"/documents/{doc['id']}").status_code == 404


def test_unknown_type_and_foreign_shipment(cust, ship):
    pdf = S.invoice_pdfs("REJ-1", S.buyer_of(ship["company"]))[0]
    assert upload(cust, "a.pdf", pdf, type="love_letter", shipment_id=ship["shipment"]["id"]).status_code == 400
    other = S.mongo().shipments.find_one({"company_id": {"$ne": ship["company"]["id"]}}, {"id": 1})
    assert upload(cust, "a.pdf", pdf, shipment_id=other["id"]).status_code == 404  # not visible, not "forbidden"


# ---- AI extraction --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def extracted(ops, cust, ship):
    if not S.ai_available(ops):
        pytest.skip("No AI key configured on the server: extraction would end in needs_review (tested separately)")
    number = f"MTF-{uuid.uuid4().hex[:6].upper()}"
    inv_pdf, pl_pdf = S.invoice_pdfs(number, S.buyer_of(ship["company"]))
    sid = ship["shipment"]["id"]
    inv = upload(cust, f"{number}.pdf", inv_pdf, shipment_id=sid).json()      # no type: the agent detects it
    pl = upload(cust, "scan_0042.pdf", pl_pdf, shipment_id=sid).json()
    assert inv["extraction"]["status"] == "pending"

    def done():
        a, b = cust.get(f"/documents/{inv['id']}").json(), cust.get(f"/documents/{pl['id']}").json()
        busy = {"pending", "processing"}
        return (a, b) if a["extraction"]["status"] not in busy and b["extraction"]["status"] not in busy else None

    res = S.wait(done, timeout=300, every=3)
    assert res, "extraction did not finish in 5 minutes"
    return {"number": number, "invoice": res[0], "packing": res[1], "shipment_id": sid}


def test_invoice_type_detected_and_fields_read(extracted):
    d = extracted["invoice"]
    ex = d["extraction"]
    assert d["type"] == "commercial_invoice" and d["type_source"] != "user"
    assert ex["status"] == "done", ex.get("warnings")
    assert ex["source"] == "ai" and ex["model"] and ex.get("ai_run_id")
    f = {k: v["value"] for k, v in ex["fields"].items()}
    assert f["invoice_number"] == extracted["number"]
    assert f["invoice_date"] == "2026-10-14"
    assert f["currency"] == "USD" and f["incoterm"] == "FOB"
    assert float(f["total_amount"]) == pytest.approx(5824.00)
    assert f["seller_country"] == "VN" and "Mekong Timber" in f["seller_name"]
    assert f["buyer_eori"] == "FI3990001-4"
    assert float(f["total_gross_weight_kg"]) == pytest.approx(1428.8) and int(float(f["total_packages"])) == 68
    for k, v in ex["fields"].items():
        assert v["confidence"] is None or 0 <= v["confidence"] <= 1, k
    lines = ex["line_items"]
    assert len(lines) == 3
    assert sum(float(x["amount"]) for x in lines) == pytest.approx(5824.00)
    assert [str(x["hs_code"]).replace(".", "")[:8] for x in lines] == ["94016100", "94036010", "94036010"]
    assert d["number"] == extracted["number"]


def test_packing_list_detected_and_tied_out(extracted, cust):
    p = extracted["packing"]
    assert p["type"] == "packing_list" and p["extraction"]["status"] == "done"
    f = {k: v["value"] for k, v in p["extraction"]["fields"].items()}
    assert f["container_numbers"] == ["MSKU7316254"]
    t = cust.get("/documents/tieout", params={"shipment_id": extracted["shipment_id"]}).json()
    checks = {c["key"]: c for c in t["items"]}
    assert checks["gross_weight"]["ok"] is True and checks["packages"]["ok"] is True
    assert checks["consignee"]["ok"] is True
    assert all(c["rule"] == "DOC-3" for c in checks.values())


def test_required_documents_checklist(extracted, cust):
    r = cust.get("/documents/required", params={"shipment_id": extracted["shipment_id"]}).json()
    rows = {x["type"]: x for x in r["items"]}
    assert rows["commercial_invoice"]["state"] in ("uploaded", "verified")
    assert rows["packing_list"]["state"] in ("uploaded", "verified")
    assert rows["bill_of_lading"]["state"] == "missing" and rows["bill_of_lading"]["required"]


def test_tieout_flags_a_weight_mismatch(extracted, cust):
    """Correct the packing list's gross weight by 5 % (more than the 2 % tolerance): DOC-3 must fail and say why."""
    pid = extracted["packing"]["id"]
    r = cust.patch(f"/documents/{pid}", json={"fields": {"total_gross_weight_kg": 1500.2}})
    assert r.status_code == 200, r.text
    fld = r.json()["extraction"]["fields"]["total_gross_weight_kg"]
    assert fld["source"] == "user" and float(fld["value"]) == 1500.2 and float(fld["previous"]) == pytest.approx(1428.8)
    t = cust.get("/documents/tieout", params={"shipment_id": extracted["shipment_id"]}).json()
    gw = next(c for c in t["items"] if c["key"] == "gross_weight")
    assert gw["ok"] is False and "kg" in gw["message"]
    # put it back and confirm the document
    r = cust.patch(f"/documents/{pid}", json={"fields": {"total_gross_weight_kg": 1428.8}, "confirm": True})
    assert r.status_code == 200 and r.json()["extraction"].get("confirmed_at")


def test_preview_renders_pages(extracted, cust):
    d = extracted["invoice"]
    r = cust.get(f"/documents/{d['id']}/preview", params={"page": 1, "width": 600})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert cust.get(f"/documents/{d['id']}/preview", params={"page": d["pages"] + 1}).status_code == 404


def test_other_customers_cannot_see_it(extracted):
    stranger = S.client()
    r = stranger.post("/auth/signup", json={"email": f"doc.test+{uuid.uuid4().hex[:8]}@huolitsija.fi", "password": "Doc-test-2026-pw",
                                            "name": "Stranger", "company_name": "Stranger Oy", "accept_terms": True, "locale": "en"})
    assert r.status_code == 200
    S.mark_test_company(r.json()["user"]["email"])
    did = extracted["invoice"]["id"]
    assert stranger.get(f"/documents/{did}").status_code == 404
    assert stranger.get(f"/documents/{did}/file").status_code == 404
    assert stranger.get(f"/documents/{did}/preview").status_code == 404
    assert stranger.patch(f"/documents/{did}", json={"fields": {"currency": "EUR"}}).status_code == 404
    assert stranger.delete(f"/documents/{did}").status_code == 404
    assert all(x["id"] != did for x in stranger.get("/documents").json()["items"])
