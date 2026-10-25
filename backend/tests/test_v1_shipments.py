"""Shipments API on the real backend: list/detail scope, booking guards, the 13.2 state machine, holds, cancel,
customer references. Test shipments are booked through POST /api/shipments (real OSRM routing) and removed afterwards."""
import uuid
from datetime import datetime

import pytest

from test_v1_support import (cleanup_test_shipments, create_test_shipment, customer, customer_company_id, db, ops, partner)
from services.geo import haversine_km

REF_RE = r"^HUO-\d{4}-\d{6}$"


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    cleanup_test_shipments()


def test_list_shape_and_counts():
    r = ops().get("/shipments")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "counts", "generated_at"}
    c = body["counts"]
    statuses = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception"]
    assert c["all"] == sum(c[s] for s in statuses)  # "all" hides cancelled shipments; they keep their own count
    assert c["active"] == c["all"] - c["delivered"]  # cancelled rows are already excluded from all
    item = body["items"][0]
    for k in ("id", "reference", "status", "mode", "origin", "destination", "eta", "carrier", "needs_action", "delay_days", "customer_name"):
        assert k in item, k
    assert len({i["customer_name"] for i in body["items"]}) >= 8  # ops sees every demo company


def test_filters_status_mode_query():
    s_all = ops().get("/shipments").json()
    intr = ops().get("/shipments", params={"status": "in_transit"}).json()
    assert intr["total"] == s_all["counts"]["in_transit"] and all(i["status"] == "in_transit" for i in intr["items"])
    sea = ops().get("/shipments", params={"mode": "sea"}).json()
    assert sea["total"] > 0
    ref = s_all["items"][0]["reference"]
    q = ops().get("/shipments", params={"q": ref}).json()
    assert [i["reference"] for i in q["items"]] == [ref]
    na = ops().get("/shipments", params={"needs_action": "true"}).json()
    assert na["total"] == s_all["counts"]["needs_action"] and all(i["needs_action"] for i in na["items"])


def test_customer_sees_only_own_company():
    cid = customer_company_id()
    mine = customer().get("/shipments").json()
    assert mine["total"] > 0
    assert {db.shipments.find_one({"id": i["id"]})["company_id"] for i in mine["items"]} == {cid}
    other = db.shipments.find_one({"company_id": {"$ne": cid}, "demo": True})
    assert customer().get(f"/shipments/{other['id']}").status_code == 404
    # company_id filter is ignored for customers
    forced = customer().get("/shipments", params={"company_id": other["company_id"]}).json()
    assert forced["total"] == mine["total"]
    assert partner().get("/shipments").status_code == 403


def test_detail_has_events_and_internal_fields_only_for_ops():
    sid = customer().get("/shipments").json()["items"][0]["id"]
    d = customer().get(f"/shipments/{sid}").json()
    assert d["tracking_events"] and d["milestones"] and d["legs"]
    assert "company" not in d and "tracking_adapters" not in d
    o = ops().get(f"/shipments/{sid}").json()
    assert o["company"]["id"] == customer_company_id() and len(o["tracking_adapters"]) == len(o["legs"])


def test_booking_guards_kyc_and_sanctions():
    cid = f"test-v1-{uuid.uuid4()}"
    db.companies.insert_one({"id": cid, "name": "TEST V1 Pending Oy", "kyc_status": "pending", "sanctions_status": "clear", "test": True})
    r = ops().post("/shipments", json={"company_id": cid, "mode": "road", "origin": "FIHEL", "destination": "FITMP"})
    assert r.status_code == 409 and "KYC" in r.json()["detail"] and r.json()["detail"].endswith(".")
    db.companies.update_one({"id": cid}, {"$set": {"kyc_status": "approved", "sanctions_status": "potential_match"}})
    r = ops().post("/shipments", json={"company_id": cid, "mode": "road", "origin": "FIHEL", "destination": "FITMP"})
    assert r.status_code == 409 and "sanctions" in r.json()["detail"]


def test_create_plans_real_legs_liability_and_milestones():
    import re

    s = create_test_shipment()
    assert re.match(REF_RE, s["reference"])
    assert s["status"] == "booked" and s["company_id"] == customer_company_id()
    lg = s["legs"][0]
    assert lg["mode"] == "road" and lg["path_source"] == "osrm" and 150 < lg["distance_km"] < 240
    assert lg["liability_regime"] == "NSAB"  # RULE X-3: domestic road -> NSAB 2015 (CMR only for international road)
    assert s["liability"]["sdr_per_kg"] == 8.33 and s["forwarder_role"]
    intl = create_test_shipment(origin="SESTO", destination="FITMP")
    assert [lg["mode"] for lg in intl["legs"]] == ["road", "sea", "road"]  # the Baltic is crossed by ferry, not by truck
    assert intl["legs"][1]["carrier"] and intl["legs"][1]["liability_regime"] == "HAGUE_VISBY"
    assert intl["legs"][0]["liability_regime"] == "NSAB" or intl["legs"][0]["from"]["country"] != intl["legs"][0]["to"]["country"]
    codes = [m["code"] for m in s["milestones"]]
    assert codes[0] == "booked" and "delivered" in codes
    assert all(m.get("planned") for m in s["milestones"])


def test_state_machine_transitions_and_guards():
    s = create_test_shipment()
    sid = s["id"]
    r = ops().post(f"/shipments/{sid}/status", json={"status": "delivered"})
    assert r.status_code == 409, r.text  # booked -> delivered skips the journey
    r = ops().post(f"/shipments/{sid}/status", json={"status": "exception"})
    assert r.status_code == 422  # an exception needs a description
    r = ops().post(f"/shipments/{sid}/milestones", json={"code": "picked_up", "note": "Collected at Vuosaari"})
    assert r.status_code == 200 and r.json()["status"] == "picked_up"
    r = ops().post(f"/shipments/{sid}/status", json={"status": "in_transit", "milestone": "departed"})
    assert r.status_code == 200 and r.json()["status"] == "in_transit"
    assert r.json()["legs"][0]["atd"]
    assert customer().post(f"/shipments/{sid}/status", json={"status": "at_hub"}).status_code == 403
    r = ops().post(f"/shipments/{sid}/status", json={"status": "exception", "note": "Truck breakdown near Hämeenlinna"})
    assert r.status_code == 200 and r.json()["status"] == "exception"
    r = ops().post(f"/shipments/{sid}/status", json={"status": "in_transit", "note": "Replacement truck on its way"})
    assert r.status_code == 200 and r.json()["status"] == "in_transit"
    events = ops().get(f"/shipments/{sid}/events").json()["items"]
    assert [e["code"] for e in events][:1] == ["booked"] and any(e["code"] == "exception" for e in events)
    assert all(e["source"] in ("manual", "schedule", "partner", "gps", "ais", "carrier_api", "port_call") for e in events)


def _map_item(sid):
    r = ops().get("/map/shipments", params={"scope": "ops"}, headers={"X-Include-Test-Tenants": "true"})
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["shipments"] if x["id"] == sid)


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def test_map_progress_starts_when_the_sea_leg_departs():
    """s4 journey: collected -> received at the terminal -> departed put a seven-week sea leg at 100 % on the map.
    Collection and terminal receipt happen before the vessel sails; then progress follows elapsed time from the ATD."""
    s = create_test_shipment(mode="sea", origin="CNYTN", destination="FIHEL", carrier="MSC", service="LCL")
    sid, first = s["id"], s["legs"][0]
    assert first["mode"] == "sea" and first["role"] != "pickup"  # port to port: no pre-carriage leg
    for code in ("picked_up", "received"):
        assert ops().post(f"/shipments/{sid}/milestones", json={"code": code}).status_code == 200
        m = _map_item(sid)
        assert m["progress"] == 0 and (m["legs"][0]["atd"], m["legs"][0]["ata"]) == (None, None), (code, m["legs"][0])
    r = ops().post(f"/shipments/{sid}/milestones", json={"code": "departed"})
    assert r.status_code == 200 and r.json()["status"] == "in_transit"
    m = _map_item(sid)
    leg = m["legs"][0]
    assert leg["atd"] and leg["ata"] is None and (_ts(leg["eta"]) - _ts(leg["atd"])).days >= 7
    assert m["progress"] < 0.01, m["progress"]  # seconds into a multi-week leg
    assert haversine_km([m["position"]["lon"], m["position"]["lat"]], [first["from"]["lon"], first["from"]["lat"]]) < 50


def test_received_at_terminal_completes_the_pickup_leg():
    s = create_test_shipment(mode="sea", origin="CNYTN", destination="FIHEL", carrier="MSC", service="LCL",
                             pickup_address={"street": "No. 18 Huanguan South Road", "city": "Shenzhen", "country": "CN"})
    sid = s["id"]
    assert s["legs"][0]["role"] == "pickup" and s["legs"][1]["mode"] == "sea"
    assert ops().post(f"/shipments/{sid}/milestones", json={"code": "picked_up"}).status_code == 200
    assert ops().post(f"/shipments/{sid}/milestones", json={"code": "received"}).status_code == 200
    legs = _map_item(sid)["legs"]
    assert legs[0]["atd"] and legs[0]["ata"] and legs[1]["atd"] is None  # truck at the terminal, vessel not sailed
    m = _map_item(sid)
    assert m["progress"] < 0.01, m["progress"]


def test_hold_blocks_and_release_restores():
    s = create_test_shipment()
    sid = s["id"]
    ops().post(f"/shipments/{sid}/milestones", json={"code": "picked_up"})
    r = ops().post(f"/shipments/{sid}/holds", json={"type": "customs", "reason": "Customs documentary check"})
    assert r.status_code == 200 and r.json()["status"] == "customs_hold"
    assert any(h["type"] == "customs" for h in r.json()["holds"])
    r = ops().delete(f"/shipments/{sid}/holds/customs")
    assert r.status_code == 200 and r.json()["status"] == "picked_up" and not r.json()["holds"]
    r = ops().post(f"/shipments/{sid}/holds", json={"type": "lien", "reason": "Unpaid freight (NSAB 2015 lien)"})
    assert r.status_code == 200
    r = ops().post(f"/shipments/{sid}/status", json={"status": "delivered"})
    assert r.status_code == 409  # RULE N-4: a lien blocks delivery
    assert customer().post(f"/shipments/{sid}/holds", json={"type": "other", "reason": "x" * 5}).status_code == 403


def test_cancel_is_final():
    s = create_test_shipment()
    sid = s["id"]
    assert ops().post(f"/shipments/{sid}/cancel", json={"reason": ""}).status_code == 422
    r = ops().post(f"/shipments/{sid}/cancel", json={"reason": "Customer cancelled the order"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert ops().post(f"/shipments/{sid}/status", json={"status": "in_transit"}).status_code == 409
    assert sid not in [i["id"] for i in ops().get("/shipments").json()["items"]]
    assert sid in [i["id"] for i in ops().get("/shipments", params={"status": "cancelled"}).json()["items"]]
    assert ops().get("/shipments").json()["counts"]["cancelled"] >= 1


def test_customer_references_and_leg_edit():
    s = create_test_shipment()
    sid = s["id"]
    r = customer().patch(f"/shipments/{sid}/references", json={"customer_ref": "PK-TEST-1", "po_numbers": ["PO-1", "PO-1", " PO-2 "]})
    assert r.status_code == 200 and r.json()["references"]["po_numbers"] == ["PO-1", "PO-2"]
    assert customer().patch(f"/shipments/{sid}/references", json={"po_numbers": ["X" * 41]}).status_code == 422
    other = db.shipments.find_one({"company_id": {"$ne": customer_company_id()}, "demo": True})
    assert customer().patch(f"/shipments/{other['id']}/references", json={"customer_ref": "nope"}).status_code == 404
    r = ops().patch(f"/shipments/{sid}/legs/1", json={"carrier": "Posti Freight", "truck_plate": "ABC-123"})
    assert r.status_code == 200, r.text
    assert r.json()["legs"][0]["carrier"] == "Posti Freight" and r.json()["legs"][0]["truck_plate"] == "ABC-123"


def test_v2_quote_booking_becomes_a_v1_shipment_with_the_ferry_as_its_own_leg():
    """Front door: the customer prices Tallinn -> Tampere in the portal (V2) and books it; V1 plans the real legs."""
    from datetime import date, timedelta

    d = date.today() + timedelta(days=4)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    addr_tll = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
    addr_tre = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}
    r = customer().post("/quotes", json={"origin": "EETLL", "destination": "FITMP", "mode": "road", "incoterm": "FCA",
                                         "incoterm_place": "Tallinn", "ready_date": d.isoformat(),
                                         "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                                                   "volume_m3": 2.0, "value_cents": 800000}})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    body = {"quote_id": q["id"], "option_id": q["options"][0]["id"], "shipper": {"name": "Tallinna Mööbel OÜ", "address": addr_tll},
            "consignee": {"name": "TEST V1 Tampere Oy", "address": addr_tre}, "pickup": {"address": addr_tll, "date": d.isoformat()},
            "delivery": {"address": addr_tre}, "customer_reference": "TEST-V1-BOOK", "accept_terms": True}
    cid = customer_company_id()
    company = db.companies.find_one({"id": cid})
    original = {k: company.get(k) for k in ("kyc_status", "sanctions_status")}
    db.companies.update_one({"id": cid}, {"$set": {"kyc_status": "approved", "sanctions_status": "clear"}})
    try:
        r = customer().post("/bookings", json=body)
    finally:
        db.companies.update_one({"id": cid}, {"$set": original})
    assert r.status_code == 200, r.text
    sid = r.json()["shipment_id"]
    db.shipments.update_one({"id": sid}, {"$set": {"test": True}})
    s = customer().get(f"/shipments/{sid}").json()
    assert s["quote_id"] == q["id"] and s["price"]["total_cents"] == q["options"][0]["total_cents"]
    assert [lg["mode"] for lg in s["legs"]] == ["road", "sea", "road"]
    assert s["legs"][0]["from"]["address"]["city"] == "Tallinn" and s["legs"][-1]["to"]["address"]["city"] == "Tampere"
    assert s["legs"][1]["from"]["country"] == "EE" and s["legs"][1]["to"]["country"] == "FI"


def test_door_delivery_at_the_ferry_port_keeps_its_truck_leg():
    """Tallinn -> Helsinki by road: the ferry lands at FIHEL itself, the consignee's door still needs the truck."""
    from datetime import date, timedelta

    d = date.today() + timedelta(days=4)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    addr_tll = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
    addr_hel = {"street": "Mannerheimintie 12", "city": "Helsinki", "postal_code": "00100", "country": "FI"}
    r = customer().post("/quotes", json={"origin": "EETLL", "destination": "FIHEL", "mode": "road", "incoterm": "FCA",
                                         "incoterm_place": "Tallinn", "ready_date": d.isoformat(),
                                         "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                                                   "volume_m3": 2.0, "value_cents": 800000}})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    body = {"quote_id": q["id"], "option_id": q["options"][0]["id"], "shipper": {"name": "Tallinna Mööbel OÜ", "address": addr_tll},
            "consignee": {"name": "TEST V1 Helsinki Oy", "address": addr_hel}, "pickup": {"address": addr_tll, "date": d.isoformat()},
            "delivery": {"address": addr_hel}, "customer_reference": "TEST-V1-BOOK-HEL", "accept_terms": True}
    cid = customer_company_id()
    company = db.companies.find_one({"id": cid})
    original = {k: company.get(k) for k in ("kyc_status", "sanctions_status")}
    db.companies.update_one({"id": cid}, {"$set": {"kyc_status": "approved", "sanctions_status": "clear"}})
    try:
        r = customer().post("/bookings", json=body)
    finally:
        db.companies.update_one({"id": cid}, {"$set": original})
    assert r.status_code == 200, r.text
    sid = r.json()["shipment_id"]
    db.shipments.update_one({"id": sid}, {"$set": {"test": True}})
    legs = customer().get(f"/shipments/{sid}").json()["legs"]
    assert [lg["mode"] for lg in legs] == ["road", "sea", "road"], [(lg["mode"], lg["from"]["name"], lg["to"]["name"]) for lg in legs]
    assert legs[0]["from"]["address"]["city"] == "Tallinn" and legs[-1]["to"]["address"]["city"] == "Helsinki"


def test_meta_allowed_transitions_and_internal_notes():
    meta = customer().get("/shipments/meta").json()
    assert meta["transitions"]["booked"] and "delivered" not in meta["transitions"]["booked"]
    s = create_test_shipment()
    sid = s["id"]
    d = ops().get(f"/shipments/{sid}").json()
    assert d["allowed_transitions"] == sorted(meta["transitions"]["booked"])
    r = ops().post(f"/shipments/{sid}/notes", json={"text": "Customer asked for a tail-lift truck"})
    assert r.status_code == 200 and r.json()["by"]["name"]
    assert ops().get(f"/shipments/{sid}").json()["internal_notes"][0]["text"] == "Customer asked for a tail-lift truck"
    c = customer().get(f"/shipments/{sid}").json()
    assert "internal_notes" not in c and "allowed_transitions" not in c
    assert customer().post(f"/shipments/{sid}/notes", json={"text": "x"}).status_code == 403
    assert ops().post(f"/shipments/{sid}/notes", json={"text": ""}).status_code == 422


def test_container_codes_are_v2_iso_codes():
    bad = list(db.shipments.find({"cargo.containers.type": {"$in": ["20DV", "40DV", "40HQ", "45HQ"]}}, {"reference": 1}))
    assert not bad, [b["reference"] for b in bad]
    assert db.shipments.count_documents({"cargo.containers.type": "20GP"}) >= 1


def test_air_liability_is_montreal_26_sdr_per_kg():
    air = db.shipments.find_one({"liability_regime": "MONTREAL"}, {"id": 1})
    assert air, "no air shipment with the Montreal regime in the demo set"
    terms = ops().get(f"/shipments/{air['id']}").json()["liability"]
    assert terms["sdr_per_kg"] == 26 and terms["limit"] == "26 SDR/kg"
    assert db.shipments.count_documents({"liability_regime": "MONTREAL", "liability.sdr_per_kg": {"$ne": 26}}) == 0


def test_delivered_notice_is_left_to_v4_routine_update():
    s = create_test_shipment()
    sid, ref = s["id"], s["reference"]
    for code in ("picked_up", "departed", "arrived", "out_for_delivery", "delivered"):
        r = ops().post(f"/shipments/{sid}/milestones", json={"code": code})
        assert r.status_code == 200, (code, r.text)
    assert db.shipments.find_one({"id": sid})["status"] == "delivered"
    # one notice per customer user: V4's routine update (thread message + its e-mail), none from V1 itself
    assert db.notifications.count_documents({"title": {"$in": [f"Delivered: {ref}", f"Out for delivery: {ref}"]}}) == 0
    assert db.messages.find_one({"routine_key": f"{sid}:delivered"})
