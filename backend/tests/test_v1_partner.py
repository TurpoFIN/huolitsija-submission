"""Partner (haulier) portal: only legs assigned to the partner's carrier, no prices, milestone updates, GPS position
reports (map source "gps") and proof of delivery stored through core.storage as a `documents` record of type "pod"."""
import pytest

from test_v1_support import cleanup_test_shipments, create_test_shipment, customer, db, ops, partner, partner_carrier_id

# A real one-page PDF (what a scanner app produces is larger, but the structure is the same).
POD_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
           b"4 0 obj<</Length 58>>stream\nBT /F1 18 Tf 72 760 Td (Proof of delivery - signed M. Laine) Tj ET\nendstream endobj\n"
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
           b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \n0000000222 00000 n \n"
           b"0000000330 00000 n \ntrailer<</Size 6/Root 1 0 R>>\nstartxref\n392\n%%EOF\n")


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    cleanup_test_shipments()


def keys_deep(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from keys_deep(v)
    elif isinstance(o, list):
        for v in o:
            yield from keys_deep(v)


@pytest.fixture(scope="module")
def job():
    cid = partner_carrier_id()
    s = create_test_shipment(carrier_id=cid, carrier=None, parties={
        "shipper": {"name": "TEST V1 Vuosaari Terminal", "address": {"street": "Satamakaari 24", "postal_code": "00980", "city": "Helsinki", "country": "FI"}},
        "consignee": {"name": "TEST V1 Tampere Oy", "address": {"street": "Hatanpään valtatie 34", "postal_code": "33100", "city": "Tampere", "country": "FI"}}})
    assert s["legs"][0]["carrier_id"] == cid
    return s


def test_partner_sees_only_assigned_legs(job):
    r = partner().get("/partner/legs")
    assert r.status_code == 200
    body = r.json()
    assert body["carrier"]["id"] == partner_carrier_id()
    assert job["id"] in [i["shipment_id"] for i in body["items"]]
    for i in body["items"]:
        doc = db.shipments.find_one({"id": i["shipment_id"]})
        leg = next(lg for lg in doc["legs"] if lg["seq"] == i["leg"]["seq"])
        assert leg["carrier_id"] == partner_carrier_id()
    other = db.shipments.find_one({"legs.carrier_id": {"$ne": partner_carrier_id()}, "demo": True})
    assert partner().get(f"/partner/shipments/{other['id']}").status_code == 404
    assert customer().get("/partner/legs").status_code == 403


def test_partner_detail_has_no_prices(job):
    d = partner().get(f"/partner/shipments/{job['id']}").json()
    ks = set(keys_deep(d))
    assert not ks & {"price", "total_cents", "amount_cents", "rate", "margin", "invoice_ids"}, ks & {"price", "total_cents"}
    assert d["legs"] and d["cargo"] and d["parties"]["consignee"]


def test_accept_leg_and_partner_map_scope(job):
    sid = job["id"]
    r = partner().post(f"/partner/shipments/{sid}/legs/1/accept", json={"truck_plate": "zxv-512"})
    assert r.status_code == 200, r.text
    leg = r.json()["legs"][0]
    assert leg["accepted_at"] and leg["truck_plate"] == "ZXV-512"
    item = next(i for i in partner().get("/partner/legs").json()["items"] if i["shipment_id"] == sid)
    assert item["leg"]["accepted_at"]
    m = partner().get("/map/shipments", params={"scope": "partner"})
    assert m.status_code == 200
    ids = {x["id"] for x in m.json()["shipments"]}
    assert sid in ids
    for x in m.json()["shipments"]:
        assert any(lg.get("carrier_id") == partner_carrier_id() for lg in db.shipments.find_one({"id": x["id"]})["legs"])
    assert customer().get("/map/shipments", params={"scope": "partner"}).status_code == 403


def test_milestones_position_report_and_pod(job):
    sid = job["id"]
    r = partner().post(f"/partner/shipments/{sid}/legs/1/milestones", json={"action": "picked_up", "note": "Loaded 4 pallets"})
    assert r.status_code == 200, r.text
    # a door-to-door truck: collecting the goods is the departure ("Collected at ...")
    s0 = db.shipments.find_one({"id": sid})
    assert s0["status"] == "in_transit" and s0["legs"][0]["atd"]
    assert any(m["code"] == "departed" and m.get("source") == "partner" for m in s0["milestones"])
    lon, lat = job["legs"][0]["path"][len(job["legs"][0]["path"]) // 3]
    r = partner().post(f"/partner/shipments/{sid}/legs/1/position", json={"lat": lat, "lon": lon, "accuracy_m": 12})
    assert r.status_code == 200, r.text
    assert r.json()["position"]["source"] == "gps" and r.json()["off_lane_km"] < 1
    s = db.shipments.find_one({"id": sid})
    assert s["status"] == "in_transit" and s["legs"][0]["atd"]
    m = ops().get("/map/shipments", params={"scope": "ops"}).json()["shipments"]
    on_map = next(x for x in m if x["id"] == sid)
    assert on_map["position"]["source"] == "gps" and abs(on_map["position"]["lat"] - lat) < 0.01
    # delivery needs the signed POD first
    r = partner().post(f"/partner/shipments/{sid}/legs/1/milestones", json={"action": "delivered"})
    assert r.status_code == 409 and "POD" in r.json()["detail"]
    r = partner().post(f"/partner/shipments/{sid}/legs/1/pod", files={"file": ("POD-test.pdf", POD_PDF, "application/pdf")},
                       data={"signed_by": "M. Laine"})
    assert r.status_code == 200, r.text
    assert r.json()["document"]["type"] == "pod" and r.json()["shipment_status"] == "delivered"
    doc = db.documents.find_one({"shipment_id": sid, "type": "pod"})
    assert doc and doc["file_id"] and doc["signed_by"] == "M. Laine" and doc["size"] == len(POD_PDF)
    ev = [e["code"] for e in ops().get(f"/shipments/{sid}/events").json()["items"]]
    assert "delivered" in ev
    assert partner().post(f"/partner/shipments/{sid}/legs/1/position", json={"lat": lat, "lon": lon}).status_code == 409


def test_pod_rejects_other_file_types(job):
    r = partner().post(f"/partner/shipments/{job['id']}/legs/1/pod", files={"file": ("pod.txt", b"hello", "text/plain")})
    assert r.status_code in (409, 415)


def test_at_hub_is_arrival_at_the_leg_end():
    cid = partner_carrier_id()
    s = create_test_shipment(carrier_id=cid, carrier=None)
    sid = s["id"]
    assert partner().post(f"/partner/shipments/{sid}/legs/1/milestones", json={"action": "picked_up"}).status_code == 200
    r = partner().post(f"/partner/shipments/{sid}/legs/1/milestones", json={"action": "at_hub", "note": "At the gate"})
    assert r.status_code == 200, r.text
    doc = db.shipments.find_one({"id": sid})
    assert doc["legs"][0]["ata"] and any(m["code"] == "arrived" and m.get("source") == "partner" for m in doc["milestones"])
    assert partner().post(f"/partner/shipments/{sid}/legs/1/milestones", json={"action": "teleported"}).status_code == 422
