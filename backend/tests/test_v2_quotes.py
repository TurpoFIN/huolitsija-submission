"""Quotes API end to end on the real server: Incoterm validation, approval threshold flow (approve + reject via the
real approvals queue), scoping, ops edits, PDF, KYC / sanctions booking guard, booking into a real V1 shipment,
double-booking protection, and the AI parse endpoint."""
import io
import uuid

import httpx
import pytest

from test_v2_support import v2_cleanup  # noqa: F401  (session teardown of test data)
from test_v2_support import (ADDR_TRE, API, PALLETS_TLL_TRE, booking_body, cust, cust_company_id, line, mongo, ops,
                             ready, set_company, signup)

BIG_FCL = {"origin": "CNSHA", "destination": "FIHEL", "incoterm": "FOB", "incoterm_place": "Shanghai",
           "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 6}], "weight_kg": 60000}}


# This variant alone supplies an explicit broker appointment; shared BIG_FCL remains unchanged.
BIG_FCL_CUSTOMER_BROKER = {}


@pytest.fixture(scope="module", autouse=True)
def _explicit_broker_request_from_recorded_model():
    """Real parser/location path; replay a captured real-model answer only at the provider boundary."""
    import asyncio
    import copy
    import json
    from pathlib import Path
    from unittest.mock import AsyncMock, patch
    # This module's expiry checks share one Motor loop; bind the parser to that same loop.
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    from services import quoting_agent
    capture = json.loads((Path(__file__).parent / "fixtures/customs/customer_broker_model.json").read_text())
    with patch.object(quoting_agent, "llm_json", AsyncMock(return_value=copy.deepcopy(capture["model_response"]))):
        parsed = _LOOP.run_until_complete(quoting_agent.parse_request(capture["source_text"], locale="en"))
    assert parsed["request"]["customs_handling"] == "customer_broker"
    assert parsed["request"]["customs_handling_evidence"] in capture["source_text"]
    BIG_FCL_CUSTOMER_BROKER.update(parsed["request"])


def _quote(client, body):
    r = client.post("/quotes", json={**body, "ready_date": ready()})
    assert r.status_code == 200, r.text
    return r.json()["quote"]


def _approval_for(quote_id):
    a = mongo().approvals.find_one({"ref_id": quote_id, "kind": "quote_threshold", "status": "pending"}, {"_id": 0})
    assert a, "no pending approval in the queue"
    return a


# ---------------------------------------------------------------------------------------------------------- incoterms
@pytest.mark.parametrize("legacy,replacement", [("DAT", "DPU"), ("DDU", "DAP")])
def test_legacy_incoterms_rejected(legacy, replacement):
    r = cust().post("/quotes", json={**PALLETS_TLL_TRE, "incoterm": legacy, "incoterm_place": "Tampere"})
    assert r.status_code == 400
    assert legacy in r.json()["detail"] and replacement in r.json()["detail"]


def test_sea_only_incoterm_with_air_mode_rejected():
    r = cust().post("/quotes", json={"mode": "air", "origin": "CNPVG", "destination": "FIHEL", "incoterm": "FOB",
                                         "incoterm_place": "Shanghai", "cargo": {"load_type": "loose", "weight_kg": 50, "volume_m3": 0.2}})
    assert r.status_code == 400
    assert "sea" in r.json()["detail"] and "FCA" in r.json()["detail"]


def test_incoterm_needs_named_place_when_quoting():
    r = cust().post("/quotes", json={**PALLETS_TLL_TRE, "incoterm": "FCA", "incoterm_place": ""})
    assert r.status_code == 400 and "place" in r.json()["detail"]


def test_unknown_incoterm_rejected():
    r = cust().post("/quotes", json={**PALLETS_TLL_TRE, "incoterm": "XYZ", "incoterm_place": "Tampere"})
    assert r.status_code == 400 and "Incoterms 2020" in r.json()["detail"]


def test_buyer_under_dap_pays_only_own_part():
    q = _quote(cust(), {**PALLETS_TLL_TRE, "incoterm": "DAP", "incoterm_place": "Tampere"})
    o = q["options"][0]
    assert not line(o, "ROAD_FREIGHT") and not line(o, "FREIGHT")
    assert any(n["reason_code"] == "incoterm.seller" for n in o["not_included"])
    assert any(w["code"] == "incoterm.other_party_main" for w in o["warnings"])


def test_cif_without_insurance_warns():
    q = _quote(cust(), {**BIG_FCL, "cargo": {"load_type": "FCL", "containers": [{"type": "20GP", "count": 1}], "weight_kg": 8000},
                            "incoterm": "CIF", "incoterm_place": "Helsinki"})
    assert any(w["code"] == "incoterm.cif_cip_no_insurance" for w in q["warnings"])


# ------------------------------------------------------------------------------------------------- approvals
def test_threshold_quote_needs_approval_then_is_sent():
    q = _quote(cust(), BIG_FCL_CUSTOMER_BROKER)
    assert q["status"] == "pending_approval"
    assert q["approval"]["reasons"] == ["threshold"] and q["approval"]["status"] == "pending"
    # the customer cannot accept or book while it is being reviewed
    acc = cust().post(f"/quotes/{q['id']}/accept", json={"option_id": q["options"][0]["id"]}).json()
    assert acc["booking_blockers"][0]["code"] == "status"
    r = cust().post("/bookings", json=booking_body(q))
    assert r.status_code == 409 and "reviewed" in r.json()["detail"]
    a = _approval_for(q["id"])
    assert a["amount_cents"] > 1_500_000
    r = ops().post(f"/approvals/{a['id']}/decide", json={"decision": "approved", "note": "Rates checked"})
    assert r.status_code == 200, r.text
    after = cust().get(f"/quotes/{q['id']}").json()["quote"]
    assert after["status"] == "sent" and after["approval"]["status"] == "approved"
    # requoting at the same price keeps the approval
    rq = cust().post(f"/quotes/{q['id']}/requote").json()["quote"]
    assert rq["status"] == "sent" and rq["revision"] == 2
    note = mongo().notifications.find_one({"company_id": cust_company_id(), "link": f"/portal/quotes/{q['id']}"})
    assert note, "customer was not notified that the quote is ready"


def test_rejected_approval_rejects_quote():
    q = _quote(cust(), BIG_FCL)
    a = _approval_for(q["id"])
    r = ops().post(f"/approvals/{a['id']}/decide", json={"decision": "rejected", "note": "Space not available this week"})
    assert r.status_code == 200
    after = cust().get(f"/quotes/{q['id']}").json()["quote"]
    assert after["status"] == "rejected"
    assert after["booking_blockers"][0]["code"] == "status"


def test_ops_send_runs_approval_policy():
    r = ops().post("/quotes", json={**BIG_FCL, "company_id": cust_company_id(), "send": False, "ready_date": ready()})
    q = r.json()["quote"]
    assert q["status"] == "draft" and q["approval"]["required"]
    assert not mongo().approvals.find_one({"ref_id": q["id"]})
    # unsent ops quotes are invisible to the customer (detail and list)
    assert cust().get(f"/quotes/{q['id']}").status_code == 404
    assert all(i["id"] != q["id"] for i in cust().get("/quotes", params={"q": q["number"]}).json()["items"])
    r = ops().patch(f"/quotes/{q['id']}", json={"send": True})
    assert r.json()["quote"]["status"] == "pending_approval"
    a = mongo().approvals.find_one({"ref_id": q["id"], "status": "pending"})
    assert a and cust().get(f"/quotes/{q['id']}").status_code == 404
    ops().post(f"/approvals/{a['id']}/decide", json={"decision": "approved", "note": "ok"})
    assert cust().get(f"/quotes/{q['id']}").json()["quote"]["status"] == "sent"


# ------------------------------------------------------------------------------------------------- scoping / views
def test_customer_view_hides_costs_and_ops_sees_them():
    q = _quote(cust(), PALLETS_TLL_TRE)
    cv = cust().get(f"/quotes/{q['id']}").json()["quote"]
    o = cv["options"][0]
    assert not {"cost_cents", "margin_cents", "margin_pct", "markup_pct"} & set(o)
    assert all("unit_cost_cents" not in ln for ln in o["lines"])
    assert "notes" not in cv and "claim_token" not in cv
    ov = ops().get(f"/quotes/{q['id']}").json()["quote"]["options"][0]
    assert ov["margin_cents"] > 0 and ov["markup_pct"] == 18.0


def test_other_company_cannot_see_quote():
    q = _quote(cust(), PALLETS_TLL_TRE)
    other, _ = signup()
    assert other.get(f"/quotes/{q['id']}").status_code == 404
    assert other.get(f"/quotes/{q['id']}/pdf").status_code == 404
    assert other.post(f"/quotes/{q['id']}/requote").status_code == 404
    assert all(i["id"] != q["id"] for i in other.get("/quotes").json()["items"])


def test_customer_cannot_patch_or_list_companies():
    q = _quote(cust(), PALLETS_TLL_TRE)
    assert cust().patch(f"/quotes/{q['id']}", json={"note": "x"}).status_code == 403
    assert cust().get("/quotes/companies").status_code == 403
    assert httpx.get(f"{API}/quotes").status_code == 401


def test_list_counts_and_filters():
    data = cust().get("/quotes").json()
    assert data["counts"]["all"] == sum(v for k, v in data["counts"].items() if k != "all")
    sent = cust().get("/quotes", params={"status": "sent"}).json()
    assert all(i["status"] == "sent" for i in sent["items"])
    item = data["items"][0]
    assert {"number", "status", "origin", "destination", "cargo_summary", "cargo", "valid_until", "best_total_cents"} <= set(item)
    assert "margin_cents" not in (item["recommended_option"] or {})


# ------------------------------------------------------------------------------------------------- ops edits
def test_ops_patch_markup_manual_option_notes():
    q = ops().post("/quotes", json={**PALLETS_TLL_TRE, "company_id": cust_company_id(), "send": False, "ready_date": ready()}).json()["quote"]
    opt = q["options"][0]
    r = ops().patch(f"/quotes/{q['id']}", json={"options": [{"id": opt["id"], "margin_pct": 30}], "note": "internal: tight space",
                                               "customer_message": "Price includes the Tallinn–Helsinki ferry."})
    assert r.status_code == 200, r.text
    o2 = next(o for o in r.json()["quote"]["options"] if o["id"] == opt["id"])
    for ln in o2["lines"]:
        if not ln["is_pass_through"] and ln["unit_cost_cents"] > 0:
            assert ln["unit_price_cents"] == round(ln["unit_cost_cents"] * 1.30)
    assert o2["markup_pct"] == 30 and o2["total_cents"] > opt["total_cents"]
    r = ops().patch(f"/quotes/{q['id']}", json={"add_manual_option": {
        "carrier_name": "Tallink Cargo", "mode": "road", "service_name": "Trailer on Tallinn–Helsinki ferry", "transit_days": 1,
        "margin_pct": 15, "lines": [{"code": "ROAD_FREIGHT", "description": "Road freight", "qty": 1, "unit_cost_cents": 30000,
                                    "vat_code": "ZERO_INTL_TRANSPORT"}]}, "send": True})
    assert r.status_code == 200, r.text
    qq = r.json()["quote"]
    manual = next(o for o in qq["options"] if o["rate_source"] == "manual")
    assert manual["total_cents"] == 34500 and manual["liability_regime"] == "CMR"
    assert qq["status"] == "sent"
    cv = cust().get(f"/quotes/{q['id']}").json()["quote"]
    assert cv["customer_message"].startswith("Price includes") and "notes" not in cv
    # validation
    bad = ops().patch(f"/quotes/{q['id']}", json={"options": [{"id": opt["id"], "lines": [
        {"code": "X", "description": "x", "qty": 1, "unit_cost_cents": 100, "vat_code": "FI_24"}]}]})
    assert bad.status_code == 400 and "VAT code" in bad.json()["detail"]
    past = ops().patch(f"/quotes/{q['id']}", json={"valid_until": "2020-01-01"})
    assert past.status_code == 400


# ------------------------------------------------------------------------------------------------- PDF
def test_quote_pdf_has_terms_validity_and_breakdown():
    pdfium = pytest.importorskip("pypdfium2")
    q = _quote(cust(), PALLETS_TLL_TRE)
    for lang, needle in (("fi", "Voimassa asti"), ("en", "Valid until")):
        r = cust().get(f"/quotes/{q['id']}/pdf", params={"lang": lang})
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        doc = pdfium.PdfDocument(io.BytesIO(r.content))
        text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
        assert "NSAB 2015 (PSYM 2015)" in text
        assert q["number"] in text and needle in text
        assert "ZERO_INTL_TRANSPORT" in text  # VAT treatment section


# ------------------------------------------------------------------------------------------------- bookings
def test_booking_blocked_until_kyc_approved_and_sanctions_clear():
    c, user = signup()
    q = _quote(c, PALLETS_TLL_TRE)
    blockers = c.get(f"/quotes/{q['id']}").json()["quote"]["booking_blockers"]
    assert {b["code"] for b in blockers} >= {"kyc"}
    r = c.post("/bookings", json=booking_body(q))
    assert r.status_code == 409
    assert "/portal/company" in r.json()["detail"] and "verified" in r.json()["detail"]
    set_company(user["company_id"], kyc_status="approved", sanctions_status="potential_match")
    r = c.post("/bookings", json=booking_body(q))
    assert r.status_code == 409 and "compliance review" in r.json()["detail"]
    assert not mongo().shipments.find_one({"quote_id": q["id"]})


def test_booking_does_not_require_email_verification_and_creates_shipment():
    c, user = signup()
    assert mongo().users.find_one({"id": user["id"]})["email_verified"] is False
    set_company(user["company_id"], kyc_status="approved", sanctions_status="clear")
    q = _quote(c, PALLETS_TLL_TRE)
    body = booking_body(q, customer_reference=f"PO-{uuid.uuid4().hex[:6]}")
    r = c.post("/bookings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["shipment_reference"].startswith("HUO-") and out["quote"]["status"] == "booked"
    shp = mongo().shipments.find_one({"id": out["shipment_id"]}, {"_id": 0})
    assert shp["company_id"] == user["company_id"] and shp["quote_id"] == q["id"]
    assert shp["references"]["customer_ref"] == body["customer_reference"]
    assert shp["price"]["total_cents"] == q["options"][0]["total_cents"]
    mail = mongo().email_outbox.find_one({"tag": "booking_confirmation", "ref.shipment_id": out["shipment_id"]})
    assert mail and "NSAB 2015 (PSYM 2015)" in mail["text"]
    # double submit: second booking of the same quote is refused, no second shipment
    again = c.post("/bookings", json=body)
    assert again.status_code == 409
    assert mongo().shipments.count_documents({"quote_id": q["id"]}) == 1


def test_booking_validation_messages():
    q = _quote(cust(), PALLETS_TLL_TRE)
    no_terms = cust().post("/bookings", json=booking_body(q, accept_terms=False))
    assert no_terms.status_code == 400 and "NSAB" in no_terms.json()["detail"]
    no_city = booking_body(q)
    no_city["consignee"] = {"name": "X Oy", "address": {"street": "Katu 1", "country": "FI"}}
    r = cust().post("/bookings", json=no_city)
    assert r.status_code == 400 and "city" in r.json()["detail"]
    past = booking_body(q)
    past["pickup"] = {**past["pickup"], "date": "2025-01-01"}
    assert cust().post("/bookings", json=past).status_code == 400
    ru = booking_body(q)
    ru["shipper"] = {"name": "OOO Test", "address": {"street": "Nevsky 1", "city": "St Petersburg", "country": "RU"}}
    r = cust().post("/bookings", json=ru)
    assert r.status_code == 400 and "Russia" in r.json()["detail"]
    wrong = cust().post("/bookings", json=booking_body(q, option_id="opt_doesnotexist"))
    assert wrong.status_code == 404


# ------------------------------------------------------------------------------------------------- AI parse
def test_parse_rejects_empty_text_without_calling_ai():
    r = cust().post("/quotes/parse", json={"text": "hi"})
    assert r.status_code == 400


def test_parse_free_text_with_real_model():
    r = cust().post("/quotes/parse", json={
        "locale": "en", "text": "2 euro pallets of furniture, 300 kg each, from Tallinn to Tampere next Tuesday, FCA Tallinn."})
    if r.status_code == 503:
        pytest.skip(r.json()["detail"])
    assert r.status_code == 200, r.text
    d = r.json()
    req = d["request"]
    assert req["origin"]["country"] == "EE" and req["destination"]["unlocode"] == "FITMP"
    assert req["cargo"]["weight_kg"] == 600 and req["cargo"]["packages"] == 2 and req["cargo"]["package_type"] == "pallet_eur"
    assert req["incoterm"] == "FCA"
    assert req["mode"] is None  # not stated in the text: every mode is priced
    # the parsed request prices as-is
    priced = cust().post("/quotes", json={**req, "ai": d["ai"]})
    assert priced.status_code == 200 and priced.json()["quote"]["source"] == "ai"


# ------------------------------------------------------------------------------------------------- housekeeping
_LOOP = None


def run_expiry_loop_once():
    """Run the real expiry loop body in-process. One event loop for the whole session: the app's Motor client binds
    to the loop of its first use, so a second asyncio.run() would hit a closed loop."""
    import asyncio
    import os
    import sys

    global _LOOP
    os.environ.setdefault("DB_NAME", mongo().name)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    async def run():
        from routes.quotes import expire_quotes  # imported inside the loop: the app's Motor client binds to it

        await expire_quotes()

    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    _LOOP.run_until_complete(run())


def test_expiry_loop_expires_old_quotes_and_drops_unclaimed_public_ones():
    q = _quote(cust(), PALLETS_TLL_TRE)
    mongo().quotes.update_one({"id": q["id"]}, {"$set": {"valid_until": "2026-01-02"}})
    stale_public = {"id": f"test-pub-{uuid.uuid4().hex[:8]}", "source": "public", "company_id": None, "status": "sent",
                    "claim_token": uuid.uuid4().hex, "claim_expires_at": "2026-01-01T00:00:00Z", "valid_until": "2026-01-02", "test": True}
    mongo().quotes.insert_one(dict(stale_public))

    run_expiry_loop_once()
    assert mongo().quotes.find_one({"id": q["id"]})["status"] == "expired"
    assert not mongo().quotes.find_one({"id": stale_public["id"]})
    blockers = cust().get(f"/quotes/{q['id']}").json()["quote"]["booking_blockers"]
    assert blockers[0]["code"] == "status" and "expired" in blockers[0]["message"]
    # requote brings it back with fresh validity
    rq = cust().post(f"/quotes/{q['id']}/requote").json()["quote"]
    assert rq["status"] == "sent" and rq["valid_until"] >= ready(0)


def test_booking_accepts_hyphenated_eori_and_names_the_party_when_wrong():
    """The portal prefills the company's EORI as Tulli and EU EOS hold it (FI + Business ID with the hyphen)."""
    c, user = signup()
    set_company(user["company_id"], kyc_status="approved", sanctions_status="clear")
    q = _quote(c, PALLETS_TLL_TRE)
    for wrong in ("FI123", "FI12345678-9", "X1"):
        bad = booking_body(q)
        bad["consignee"] = {**bad["consignee"], "eori": wrong}
        r = c.post("/bookings", json=bad)
        assert r.status_code == 400 and r.json()["detail"].startswith("The consignee's EORI number looks wrong"), (wrong, r.text)
    body = booking_body(q)
    body["shipper"] = {**body["shipper"], "eori": "EE100123456"}
    body["consignee"] = {**body["consignee"], "eori": "FI1234567-8"}
    body["notify"] = {"name": "Huolinta Oy", "address": ADDR_TRE, "eori": "fi 12345678"}
    r = c.post("/bookings", json=body)
    assert r.status_code == 200, r.text
    b = r.json()["booking"]
    assert (b["shipper"]["eori"], b["consignee"]["eori"], b["notify"]["eori"]) == ("EE100123456", "FI1234567-8", "FI1234567-8")


def test_sea_leg_to_helsinki_ends_at_the_harbour_not_the_airport():
    """FIHEL names Vuosaari harbour and Helsinki-Vantaa airport; s4 saw the sea leg end at the airport."""
    from services.geo import haversine_km

    q = _quote(cust(), {"mode": "sea", "origin": "CNYTN", "destination": "FIHEL", "incoterm": "FOB", "incoterm_place": "Yantian",
                        "cargo": {"packages": 58, "package_type": "carton", "weight_kg": 820, "volume_m3": 6.4}})
    sea = [lg for o in q["options"] for lg in o["legs"] if lg["mode"] == "sea" and (lg["to"] or {}).get("unlocode") == "FIHEL"]
    assert sea, [[(lg["mode"], lg["to"]) for lg in o["legs"]] for o in q["options"]]
    for lg in sea:
        assert haversine_km([lg["to"]["lon"], lg["to"]["lat"]], [25.19392, 60.21502]) < 3, lg["to"]


def test_booking_defaults_door_addresses_and_pickup_date_from_quote():
    c, user = signup()
    set_company(user["company_id"], kyc_status="approved", sanctions_status="clear")
    q = _quote(c, PALLETS_TLL_TRE)
    body = booking_body(q)
    body["pickup"] = {}
    body["delivery"] = {}
    r = c.post("/bookings", json=body)
    assert r.status_code == 200, r.text
    b = r.json()["booking"]
    assert b["pickup"]["address"]["city"] == "Tallinn" and b["delivery"]["address"]["city"] == "Tampere"
    assert b["pickup"]["date"] == q["request"]["ready_date"]
    assert q["options"][0]["liability"]["label_fi"].startswith("CMR")


def test_ops_line_edit_keeps_finnish_texts():
    q = ops().post("/quotes", json={**PALLETS_TLL_TRE, "company_id": cust_company_id(), "send": False, "ready_date": ready()}).json()["quote"]
    opt = q["options"][0]
    fi_before = {ln["code"]: ln["description_fi"] for ln in opt["lines"]}
    assert fi_before["ROAD_FREIGHT"] != "Road freight"
    # the editor sends the lines back without description_fi (worst case) and one changed price
    lines = [{k: ln[k] for k in ("code", "description", "qty", "unit_cost_cents", "unit_price_cents", "vat_code", "is_pass_through",
                                 "stage", "note")}
             for ln in opt["lines"]]
    lines[0]["unit_cost_cents"] += 100
    r = ops().patch(f"/quotes/{q['id']}", json={"options": [{"id": opt["id"], "lines": lines, "margin_pct": 18}]})
    assert r.status_code == 200, r.text
    after = next(o for o in r.json()["quote"]["options"] if o["id"] == opt["id"])
    assert {ln["code"]: ln["description_fi"] for ln in after["lines"]} == fi_before
    assert r.json()["quote"]["revision"] == 2


def test_pdf_survives_markup_in_free_text():
    pdfium = pytest.importorskip("pypdfium2")
    q = ops().post("/quotes", json={**PALLETS_TLL_TRE, "company_id": cust_company_id(), "send": True, "ready_date": ready()}).json()["quote"]
    hostile = 'Hinta <sis. & lauttamaksun> <b>bold <i>unclosed & "quotes" </font>'
    assert ops().patch(f"/quotes/{q['id']}", json={"customer_message": hostile}).status_code == 200
    for lang in ("fi", "en"):
        r = cust().get(f"/quotes/{q['id']}/pdf", params={"lang": lang})
        assert r.status_code == 200, r.text[:200]
        text = pdfium.PdfDocument(io.BytesIO(r.content))[0].get_textpage().get_text_range()
        assert "<sis. & lauttamaksun>" in text


def test_stale_booking_lock_is_released():
    old = _quote(cust(), PALLETS_TLL_TRE)
    fresh = _quote(cust(), PALLETS_TLL_TRE)
    mongo().quotes.update_one({"id": old["id"]}, {"$set": {"status": "booking", "status_before_booking": "sent",
                                                           "booking_started_at": "2026-01-01T00:00:00Z"}})
    mongo().quotes.update_one({"id": fresh["id"]}, {"$set": {"status": "booking", "status_before_booking": "sent",
                                                             "booking_started_at": "2999-01-01T00:00:00Z"}})

    run_expiry_loop_once()
    assert mongo().quotes.find_one({"id": old["id"]})["status"] == "sent"
    assert mongo().quotes.find_one({"id": fresh["id"]})["status"] == "booking"
    assert cust().post("/bookings", json=booking_body(fresh)).status_code == 409  # still locked
    mongo().quotes.update_one({"id": fresh["id"]}, {"$set": {"status": "sent"}})
    assert cust().post("/bookings", json=booking_body(old)).status_code == 200


# ------------------------------------------------------------------------------------------ r3 N1: no 35 EUR quotes
ROAD_600 = {"mode": "road", "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                                      "volume_m3": 2.4, "stackable": True}}


@pytest.mark.parametrize("origin,destination,incoterm,place", [("DEHAM", "FIHEL", "FCA", "Hamburg"),   # import, buyer
                                                               ("FIHEL", "DEHAM", "DAP", "Hamburg")])  # export, seller
def test_road_into_and_out_of_finland_charges_road_freight(origin, destination, incoterm, place):
    """When the customer's side of the Incoterm includes the main carriage, every road option charges it, both ways."""
    q = _quote(cust(), {**ROAD_600, "origin": origin, "destination": destination, "incoterm": incoterm, "incoterm_place": place})
    assert q["status"] == "sent"
    assert q["options"] and all(line(o, "ROAD_FREIGHT") for o in q["options"])
    assert all(o["total_cents"] > 10000 for o in q["options"])  # far above the 35 EUR document fee


@pytest.mark.parametrize("origin,destination,incoterm,place", [("DEHAM", "FIHEL", "DAP", "Helsinki"),   # importer under DAP
                                                               ("FIHEL", "DEHAM", "FCA", "Helsinki")])  # exporter under FCA
def test_quote_without_freight_goes_to_ops_review_not_to_the_customer(origin, destination, incoterm, place):
    """r3 N1: the other party pays the main carriage, so the options are left with the document fee. Such a quote was
    auto-sent at 35 EUR and could be booked; it now waits for ops in the approval queue and cannot be accepted."""
    q = _quote(cust(), {**ROAD_600, "origin": origin, "destination": destination, "incoterm": incoterm, "incoterm_place": place})
    assert q["status"] == "pending_approval"
    assert all(not line(o, "ROAD_FREIGHT") for o in q["options"])
    assert "no_freight" in mongo().quotes.find_one({"id": q["id"]})["approval"]["reasons"]
    assert "no freight" in _approval_for(q["id"])["reason"]
    r = cust().post(f"/quotes/{q['id']}/accept", json={"option_id": q["options"][0]["id"]})
    assert r.status_code == 200 and r.json()["quote"]["status"] == "pending_approval"


def test_autopilot_does_not_send_a_quote_without_freight():
    sw = lambda on: ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})  # noqa: E731
    assert sw(True).status_code == 200
    try:
        q = _quote(cust(), {**ROAD_600, "origin": "DEHAM", "destination": "FIHEL", "incoterm": "DAP", "incoterm_place": "Helsinki"})
        assert q["status"] == "pending_approval"
        assert not mongo().autonomy_actions.find_one({"entity_id": q["id"], "kind": "quote_approved"})
    finally:
        assert sw(False).status_code == 200


def test_domestic_requote_has_no_customs_hold():
    q = _quote(cust(), {"origin":"FIHEL", "destination":"FIOUL", "mode":"road",
        "cargo":{"packages":2,"package_type":"pallet_eur","weight_kg":600,"volume_m3":2}})
    assert q["request"]["customs_required"] is False
    assert "customs_required" not in q["approval"]["reasons"]
    before = mongo().approvals.count_documents({"ref_id":q["id"],"status":"pending"})
    rq = cust().post(f"/quotes/{q['id']}/requote").json()["quote"]
    assert rq["request"]["customs_required"] is False
    assert "customs_required" not in rq["approval"]["reasons"]
    assert rq["status"] == q["status"]
    assert mongo().approvals.count_documents({"ref_id":q["id"],"status":"pending"}) == before


def test_unchanged_cross_border_requote_carries_customs_approval():
    q = _quote(cust(), BIG_FCL)  # unspecified customs: the original fixture still exercises unknown handling
    assert q["request"]["customs_handling"] == "unknown" and q["request"]["customs_required"] is True
    assert "customs_required" in q["approval"]["reasons"]
    a = _approval_for(q["id"])
    r = ops().post(f"/approvals/{a['id']}/decide", json={"decision":"approved","note":"Broker arranged for these customs formalities"})
    assert r.status_code == 200, r.text
    after = cust().get(f"/quotes/{q['id']}").json()["quote"]
    assert after["status"] == "sent"
    before_cards = mongo().approvals.count_documents({"ref_id":q["id"]})
    rq = cust().post(f"/quotes/{q['id']}/requote").json()["quote"]
    assert rq["request"] == after["request"]
    assert [o["total_cents"] for o in rq["options"]] == [o["total_cents"] for o in after["options"]]
    assert rq["revision"] == 2 and rq["status"] == "sent" and rq["approval"]["status"] == "approved"
    assert mongo().approvals.count_documents({"ref_id":q["id"]}) == before_cards
    assert mongo().approvals.count_documents({"ref_id":q["id"],"status":"pending"}) == 0
