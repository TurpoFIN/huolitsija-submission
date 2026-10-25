"""Customs: AI-drafted import declaration from an uploaded invoice, totals math checked independently (ECB rate fetched
by the test itself, quote stages, IA art. 137 / ALVL 91 §), and approval-gated filing through the real API down to a
simulated MRN and release that lifts the test shipment's customs hold.
AI extraction, TARIC, and ECB use real services; the reserved test tenant uses explicit sandbox register and Tulli data.

Needs the server's AI key for extraction + HS classification (skips otherwise).
Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_customs.py
"""
import io
import copy
import uuid
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import httpx
import pytest

import test_v3_support as S


@pytest.fixture(scope="module")
def ops():
    return S.client(S.OPS)


@pytest.fixture(scope="module")
def company():
    return S.ensure_test_customer()


@pytest.fixture(scope="module")
def cust(company):
    return S.client(S.TEST_CUSTOMER)


@pytest.fixture(scope="module")
def drafted(ops, cust, company, request):
    if not S.ai_available(ops):
        pytest.skip("No AI key on the server: the AI draft cannot run")
    # the broker's own path (fields, four-eyes approval, filing): the Autopilot customs agent stays out of this shipment
    ship = S.make_import_shipment(company["name"], customs_handling="broker")
    request.addfinalizer(lambda: S.cleanup_import_shipment(ship))
    sid = ship["shipment"]["id"]
    # Explicit test-consignment inputs: strict FI415B does not invent these from a port name.
    S.mongo().shipments.update_one({"id": sid}, {"$set": {
        "transaction_nature": "11", "legs.0.nationality": "FI", "legs.0.transport_doc_type": "bill_of_lading",
        "parties.shipper.address": {"street": "Lot B2, Song Than 3 Industrial Park", "city": "Thu Dau Mot",
                                    "postal_code": "75000", "country": "VN"}}})
    number = f"MTF-{uuid.uuid4().hex[:6].upper()}"
    inv_pdf, pl_pdf = S.invoice_pdfs(number, S.buyer_of(ship["company"]))
    for name, data in ((f"{number}.pdf", inv_pdf), (f"PL-{number}.pdf", pl_pdf)):
        r = cust.post("/documents", files={"file": (name, io.BytesIO(data), "application/pdf")}, data={"shipment_id": sid})
        assert r.status_code == 200, r.text

    def ready():
        items = cust.get("/customs", params={"shipment_id": sid}).json()["items"]
        if not items:
            return None
        d = ops.get(f"/customs/{items[0]['id']}").json()
        # The invoice draft can precede packing-list extraction and party screening.
        # Wait for their actual projection, otherwise broker edits race the automatic redraft.
        packing_ready = bool(d.get("transport", {}).get("container_numbers")) and all(
            it.get("marks") for it in d["items"])
        screening_ready = (d.get("screening") or {}).get("complete")
        return d if packing_ready and screening_ready and len(d["items"]) == 3 and all(
            it.get("hs_code") for it in d["items"]) else None

    decl = S.wait(ready, timeout=420, every=4)
    assert decl, "no complete invoice/packing/screening draft within 7 minutes of the upload"
    # The reserved test tenant needs a fictional representative too. No production company/config is changed.
    from core.sandbox import is_sandbox_company, sandbox_eori
    assert is_sandbox_company(company)
    representative = {"name": "Local test customs representative", "eori": "FI3990002-2"}
    S.mongo().aeo_eori_checks.replace_one({"eori": representative["eori"]}, sandbox_eori("3990002-2"), upsert=True)
    S.mongo().customs_declarations.update_one({"id": decl["id"], "company_id": company["id"]},
                                             {"$set": {"representative": representative}})
    r = ops.patch(f"/customs/{decl['id']}", json={"submitter": {"name": "Local test broker", "email": "broker@local-test.example"}})
    assert r.status_code == 200, r.text
    decl = r.json()
    r = ops.post(f"/customs/{decl['id']}/screen")
    assert r.status_code == 200, r.text
    decl = ops.get(f"/customs/{decl['id']}").json()
    return {"decl": decl, "ship": ship, "number": number}


def ecb_rate(currency: str, on: date) -> tuple[float, str]:
    """Independent check of IA 2015/2447 art. 146: ECB reference rate of the penultimate Wednesday of the previous month."""
    first = on.replace(day=1)
    last_prev = first - timedelta(days=1)
    weds = [last_prev - timedelta(days=i) for i in range(31) if (last_prev - timedelta(days=i)).weekday() == 2
            and (last_prev - timedelta(days=i)).month == last_prev.month]
    target = sorted(weds)[-2]
    r = httpx.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml", timeout=30)
    r.raise_for_status()
    by_day = {}
    for day in ET.fromstring(r.content).iter():
        if day.get("time"):
            for c in day:
                if c.get("currency") == currency:
                    by_day[day.get("time")] = c.get("rate")
    d = target
    while d.isoformat() not in by_day or by_day[d.isoformat()] in ("", "N/A"):  # ECB holiday: last earlier rate
        d -= timedelta(days=1)
    return float(by_day[d.isoformat()]), d.isoformat()


def test_draft_shape_and_provenance(drafted):
    d = drafted["decl"]
    assert d["status"] in ("draft", "needs_info") and d["lrn"].startswith("LRN-")
    assert d["ai"]["label"] == "AI drafted · broker approves before filing"
    assert d["representation_type"] == "direct" and d["vat_route"] == "tilitys"  # VAT-registered importer (V-3)
    for it in d["items"]:
        s = it["hs_suggestion"]
        assert s["source"] in ("ai", "invoice") and len(it["hs_code"]) == 6 and it["taric_url"].startswith("https://")
        assert it["duty_source"] in ("ai_estimate", "seed", "broker", "invoice", "taric")
    assert [it["hs_code"] for it in d["items"]] == ["940161", "940360", "940360"]


def test_valuation_inputs(drafted):
    d = drafted["decl"]
    v = d["valuation"]
    assert v["incoterm"] == "FOB" and v["incoterm_group"] == "F" and v["invoice_currency"] == "USD"
    assert v["invoice_total"] == pytest.approx(5824.00)
    # quote stages: main leg 1 (OFR+BAF) is carriage to the EU border; the feeder after the first EU port,
    # destination charges and the clearance fee go to the VAT base; the duty pass-through line is excluded
    assert v["freight_to_border_cents"] == 185000 + 42000
    assert v["insurance_cents"] == 4500
    assert v["post_border_cents"] == 31000 + 26000 + 38000 + 6500
    rate, rate_day = ecb_rate("USD", date.fromisoformat(d["created_at"][:10]))
    assert v["exchange_rate"]["rate"] == pytest.approx(rate) and v["exchange_rate"]["rate_date"] == rate_day


def test_totals_math(drafted):
    d = drafted["decl"]
    v, items, t = d["valuation"], d["items"], d["totals"]
    rate = v["exchange_rate"]["rate"]
    assert sum(it["value_cents"] for it in items) == round(5824.00 / rate * 100)
    assert sum(it["freight_cents"] for it in items) == v["freight_to_border_cents"]           # to the cent
    assert sum(it["insurance_cents"] for it in items) == v["insurance_cents"]
    assert sum(it["post_border_cents"] for it in items) == v["post_border_cents"]
    gross = [it["gross_kg"] for it in items]
    for it, g in zip(items, gross):  # freight by gross weight (largest remainder): within 1 cent of the exact share
        assert abs(it["freight_cents"] - v["freight_to_border_cents"] * g / sum(gross)) <= 1
        assert it["customs_value_cents"] == it["value_cents"] + it["freight_cents"] + it["insurance_cents"]
        assert it["duty_cents"] == round(it["customs_value_cents"] * it["duty_rate_pct"] / 100)
        assert it["vat_rate_pct"] == 25.5                                                    # furniture: standard rate
        assert it["vat_base_cents"] == it["customs_value_cents"] + it["duty_cents"] + it["post_border_cents"]
        assert it["vat_cents"] == round(it["vat_base_cents"] * 25.5 / 100)
    for k in ("customs_value_cents", "duty_cents", "vat_base_cents", "vat_cents"):
        assert t[k] == sum(it[k] for it in items), k
    assert t["invoice_value_cents"] == sum(it["value_cents"] for it in items)
    assert t["payable_to_customs_cents"] == t["duty_cents"]  # tilitys: VAT goes to the VAT return, not to Customs
    # s11 deliberately identifies complete official TARIC rates separately from AI rate estimates.
    official = all(it.get("duty_source") == "taric" for it in items) and t["duty_complete"]
    assert t["estimate"] is (not official)
    assert ("TARIC" if official else "estimate") in t["estimate_label"]
    assert not d.get("customs_decision")  # a calculated amount is still not an authority decision
    assert t["gross_kg"] == pytest.approx(1428.8) and t["packages"] == 68


def test_validations_name_rules_and_owners(drafted):
    vals = drafted["decl"]["validations"]
    assert all({"rule", "ok", "severity", "owner", "message"} <= set(v) for v in vals)
    rules = {v["rule"] for v in vals}
    assert {"E-1", "R-2", "K-1", "I-5", "DOC-2", "H-1", "V-3"} <= rules
    open_broker = [v for v in vals if not v["ok"] and v["severity"] == "blocking"]
    assert open_broker and all(v["owner"] in {"broker", "customer", "compliance"} for v in open_broker), open_broker
    assert any(v["rule"] == "C-2" and not v["ok"] for v in vals)  # office of presentation is the broker's to set


def test_customer_cannot_touch_broker_fields_or_file(drafted, cust):
    did = drafted["decl"]["id"]
    assert cust.patch(f"/customs/{did}", json={"items": [{"seq": 1, "duty_rate_pct": 0}]}).status_code in (400, 403)
    assert cust.patch(f"/customs/{did}", json={"office": {"code": "FI002000"}}).status_code in (400, 403)
    assert cust.post(f"/customs/{did}/approve").status_code == 403
    assert cust.post(f"/customs/{did}/record-response", data={"event": "released", "mrn": "26FI000000000001R1"}).status_code == 403


def test_incomplete_xml_returns_validation_error(drafted, ops):
    # Missing package type is an actual XSD violation, independent of optional MIG fields.
    incomplete = copy.deepcopy(drafted["decl"])
    incomplete.update(id=str(uuid.uuid4()), lrn="LOCAL-TEST-INVALID-XML")
    incomplete.pop("_id", None)
    incomplete["items"][0]["package_type"] = None
    S.mongo().customs_declarations.insert_one(incomplete)
    try:
        r = ops.get(f"/customs/{incomplete['id']}/xml")
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "customs_xml_invalid"
        assert "Packaging" in r.json()["detail"]["message"]
    finally:
        S.mongo().customs_declarations.delete_one({"id": incomplete["id"]})


def test_approval_gated_filing_to_release(drafted, ops, cust):
    d = drafted["decl"]
    did, sid = d["id"], drafted["ship"]["shipment"]["id"]
    # 1. the broker cannot ask for approval while blocking checks are open
    r = ops.post(f"/customs/{did}/approve")
    assert r.status_code == 400 and "Resolve blocking checks" in r.json()["detail"]
    # 2. broker completes the declaration
    # Fixture-only FLEGT licence and supplier assertions. Nothing here proves an authority filing.
    r = ops.post("/customs/licences", json={"company_id": d["company_id"], "certificate_code": "C690",
        "number": "LOCAL-TEST-FLEGT-" + did, "issuer": "Local test fixture (not an authority)",
        "holder": d["importer"]["name"], "goods": ["9403"], "valid_from": date.today().isoformat(),
        "valid_to": (date.today() + timedelta(days=1)).isoformat(), "note": "Fictional licence for local test tenant"})
    assert r.status_code == 201, r.text
    body = {"office": {"code": "FI002000"}, "location_of_goods": "Testikatu 1, Helsinki (test)",
            "goods_location": {"type": "D", "address": {"street": "Testikatu 1", "city": "Helsinki", "postal_code": "00100", "country": "FI"}},
            "transport": {"transport_document_type": "bill_of_lading", "nationality": "FI"},
            "items": [{"seq": it["seq"], "cn_confirmed": True, "documents": [
                {"code": "Y904", "reference": "LOCAL TEST: ordinary furniture, not torture equipment", "source": "declared"},
                {"code": "Y900", "reference": "LOCAL TEST: non-CITES timber species", "source": "declared"},
                {"code": "Y922", "reference": "LOCAL TEST: textile upholstery, no cat/dog fur", "source": "declared"}
            ]} for it in d["items"]]}
    r = ops.patch(f"/customs/{did}", json=body)
    assert r.status_code == 200, r.text
    left = [v for v in r.json()["validations"] if not v["ok"] and v["severity"] == "blocking"]
    assert not left, left
    # 3. the customer confirms the goods data
    r = cust.post(f"/customs/{did}/submit-for-review")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready_for_review"
    # 4. approval requested: nothing is filed yet
    r = ops.post(f"/customs/{did}/approve")
    assert r.status_code == 200, r.text
    d1 = r.json()
    assert d1["status"] == "ready_for_review" and d1["approval"]["status"] == "pending" and not d1.get("submission")
    assert ops.post(f"/customs/{did}/file").status_code == 409
    assert ops.post(f"/customs/{did}/record-response", data={"event": "accepted", "mrn": "26FI000000000001R1"}).status_code in (400, 409)
    first = d1["approval"]["id"]
    # 5. a change after the request voids the approval (the approver would have seen other numbers)
    r = ops.patch(f"/customs/{did}", json={"valuation": {"insurance_cents": 5200}})
    assert r.status_code == 200
    assert S.mongo().approvals.find_one({"id": first})["status"] == "cancelled"
    assert ops.post(f"/approvals/{first}/decide", json={"decision": "approved", "note": "late"}).status_code == 409
    assert r.json()["status"] == "ready_for_review" and not r.json().get("approval_id")
    # 6. request again, a customer cannot decide, the broker approves -> filed through the configured gateway
    d2 = ops.post(f"/customs/{did}/approve").json()
    aid = d2["approval"]["id"]
    assert aid != first
    assert cust.post(f"/approvals/{aid}/decide", json={"decision": "approved"}).status_code == 403
    # the broker asked for this approval and is the only user with customs.file: four-eyes needs an audited override
    r = ops.post(f"/approvals/{aid}/decide", json={"decision": "approved", "note": "Checked against TARIC (test)"})
    assert r.status_code == 409 and r.json()["code"] == "four_eyes_single_user", r.text
    r = ops.post(f"/approvals/{aid}/decide", json={"decision": "approved", "note": "Checked against TARIC (test)", "override": True})
    assert r.status_code == 200, r.text
    d3 = ops.get(f"/customs/{did}").json()
    # the gateway this declaration files through: a sandbox test company (reserved 399xxxx Business ID) always uses
    # sandbox Tulli, whatever TULLI_MODE says
    gw = ops.get("/customs/gateway", params={"declaration_id": did}).json()
    if gw["mode"] == "disabled":
        assert d3["status"] == "approved" and d3["submission"]["channel"] == "manual"
    else:
        assert d3["status"] in ("submitted", "accepted", "rejected", "released") and d3["submission"]["channel"] == gw["channel"]
    # 7. MRN input is validated; a typo is accepted with a hint, a foreign MRN is refused
    from services.customs import mrn_check_digit

    assert ops.post(f"/customs/{did}/record-response", data={"event": "accepted", "mrn": "NOT-AN-MRN"}).status_code == 400
    base = f"{d3['created_at'][2:4]}FI{uuid.uuid4().hex[:12].upper()}R"
    de = "26DE" + base[4:]
    assert ops.post(f"/customs/{did}/record-response", data={"event": "accepted", "mrn": de + mrn_check_digit(de)}).status_code == 400
    if gw["mode"] == "sandbox":  # the sandbox answered with its own MRN (SBX serial, valid check digit, never a real one)
        mrn = d3["mrn"]
        assert mrn and mrn[2:7] == "FISBX" and mrn[-1] == mrn_check_digit(mrn[:-1]), mrn
    else:
        mrn = base + mrn_check_digit(base)
        r = ops.post(f"/customs/{did}/record-response", data={"event": "accepted", "mrn": mrn.lower()})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted" and r.json()["mrn"] == mrn
    # 8. release with Tulli's decision attached: shipment customs hold lifted, customer sees it
    from services.seed_customs import render_packing_list_pdf

    pdf = render_packing_list_pdf({"number": "decision", "date": "2026-10-24", "invoice_number": "-", "shipper": {"name": "-", "address": "-", "country_name": "-"},
                                   "consignee": {"name": "-", "address": "-"}, "lines": [], "packages": 0, "package_type": "-",
                                   "gross": 0, "net": 0, "volume": 0})
    d4 = ops.get(f"/customs/{did}").json()
    uploaded = d4["status"] != "released"  # sandbox Tulli releases by itself after TULLI_SANDBOX_RELEASE_SECONDS
    if uploaded:
        r = ops.post(f"/customs/{did}/record-response", data={"event": "released", "note": "Released (test)"},
                     files={"file": ("release-decision.pdf", io.BytesIO(pdf), "application/pdf")})
        assert r.status_code == 200, r.text
        d4 = r.json()
    assert d4["status"] == "released"
    hist = [h.get("to") or h.get("status") for h in d4["history"] if h.get("to") or h.get("status")]
    assert hist[-3:] == ["approved", "accepted", "released"] or hist[-2:] == ["accepted", "released"], hist
    shp = S.mongo().shipments.find_one({"id": sid}, {"_id": 0, "holds": 1, "status": 1})
    assert not [h for h in shp.get("holds") or [] if h.get("type") == "customs" and not h.get("released_at")], shp
    if uploaded:
        assert any(x["type"] == "customs_decision" for x in cust.get("/documents", params={"shipment_id": sid}).json()["items"])
    c = cust.get(f"/customs/{did}").json()
    assert c["status"] == "released" and c["mrn"] == mrn
    # 9. a document used by a filed declaration cannot be deleted
    inv = next(x for x in cust.get("/documents", params={"shipment_id": sid}).json()["items"] if x["type"] == "commercial_invoice")
    assert cust.delete(f"/documents/{inv['id']}").status_code == 409


def test_xml_and_pdf_outputs(drafted, ops):
    did = drafted["decl"]["id"]
    r = ops.get(f"/customs/{did}/xml")
    assert r.status_code == 200 and "xml" in r.headers["content-type"]
    root = ET.fromstring(r.content)
    text = ET.tostring(root, encoding="unicode")
    assert drafted["decl"]["lrn"] in text
    from services.customs_messages.tulli_import import validate
    validate(r.text)  # s11 emits the official FI415B schema instead of the retired private H1 template
    assert root.tag.endswith("FI415B")
    assert text.count("GoodsItem") >= 3
    r = ops.get(f"/customs/{did}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_other_customer_cannot_see_declaration(drafted):
    stranger = S.client()
    r = stranger.post("/auth/signup", json={"email": f"cus.test+{uuid.uuid4().hex[:8]}@huolitsija.fi", "password": "Cus-test-2026-pw",
                                            "name": "Stranger", "company_name": "Stranger Two Oy", "accept_terms": True, "locale": "en"})
    assert r.status_code == 200
    S.mark_test_company(r.json()["user"]["email"])
    did = drafted["decl"]["id"]
    assert stranger.get(f"/customs/{did}").status_code == 404
    assert stranger.get(f"/customs/{did}/xml").status_code in (403, 404)
    assert all(x["id"] != did for x in stranger.get("/customs").json()["items"])
