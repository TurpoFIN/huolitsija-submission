"""Dangerous goods (s12) on the real built dataset (backend/data/dg, from 49 CFR 172.101/176.83 and ADR 2025 Table A).

Part 1 calls services.dangerous_goods directly. The expected values were read from the downloaded source rows (see the
evidence file s12-compliance/dg/source_rows.txt); with DG_SOURCE_CACHE pointing to the build cache the raw-source tests
also re-read the eCFR XML and the ADR PDF text independently of the build parser and compare.
Part 2 talks HTTP to a real local backend (S12_API, default http://localhost:8122/api; DB S12_DB, default huo_s12_dg):
public lookup, quote refusing air for a UN number forbidden by air, booking with a declared-class mismatch -> 409,
booking that needs review -> shipment dg_review_required + approval card dg_review.
"""
from __future__ import annotations

import html
import os
import re
import sys
import uuid
from datetime import date, timedelta

import httpx
import pytest
from pymongo import MongoClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from services import dangerous_goods as dg  # noqa: E402

API = os.environ.get("S12_API", "http://localhost:8122/api")
DB_NAME = os.environ.get("S12_DB", "huo_s12_dg")
PW = os.environ.get("S12_DEMO_PASSWORD", "s12-local-Demo-2026")
CACHE = os.environ.get("DG_SOURCE_CACHE")


def variant(rec, pg, name=None):
    return next(v for v in rec["variants"] if v["pg"] == pg and (name is None or v["name"] == name) and not v["domestic_only"])


def codes(res):
    return {f["code"]: f["severity"] for f in res["findings"]}


# ------------------------------------------------------------------------------------------------ dataset
def test_dataset_counts_and_sources():
    info = dg.dataset_info()
    c = info["counts"]
    assert c["un_ids"] > 2300 and c["records"] > 2900 and c["records_with_both"] > 2700, c
    keys = {s["key"] for s in info["sources"]}
    assert keys == {"49cfr_172_101", "49cfr_176_83", "49cfr_176_84", "adr_2025"}
    for s in info["sources"]:
        assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) and s["url"].startswith("https://") and s["licence"]
    assert info["air_source"] == "49 CFR 172.101 columns 9A/9B"  # IATA_DGR_DATA_PATH not set


def test_un1263_paint_class3_three_packing_groups():
    r = dg.lookup("UN1263")
    assert r["class"] == "3" and r["subsidiary"] == [] and r["packing_groups"] == ["I", "II", "III"]
    assert "Paint" in r["names"]
    lim = {pg: (variant(r, pg, "Paint")["air"]["passenger_limit"], variant(r, pg, "Paint")["air"]["cargo_limit"]) for pg in ("I", "II", "III")}
    assert lim == {"I": ("1 L", "30 L"), "II": ("5 L", "60 L"), "III": ("60 L", "220 L")}
    assert {v["vessel"]["stowage"] for v in r["variants"] if v["pg"] == "II"} == {"B"}
    assert r["adr"]["classification_code"] == "F1" and r["adr"]["tunnel_code"] == "D/E"
    assert {a["tunnel_code"] for a in variant(r, "III", "Paint")["adr"]} == {"D/E", "E"}  # III: main row + viscous rows
    assert dg.lookup("1263")["un"] == "UN1263" and dg.lookup("UN 1263")["un"] == "UN1263"


def test_un1203_gasoline_tunnel_and_air_limits():
    r = dg.lookup("UN1203")
    v = variant(r, "II")
    assert r["class"] == "3" and r["packing_groups"] == ["II"]
    assert (v["air"]["passenger_limit"], v["air"]["cargo_limit"]) == ("5 L", "60 L")
    assert r["adr"]["tunnel_code"] == "D/E" and r["adr"]["transport_category"] == "2" and r["adr"]["hazard_id"] == "33"
    road = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "II"}}, "road")
    assert road["status"] == "ok" and road["tunnel_code"] == "D/E"
    f = next(x for x in road["findings"] if x["code"] == "dg.adr_tunnel")
    assert f["data"]["forbidden_tunnel_categories"] == ["E"] and "category E" in f["en"]  # packages: only E (ADR 8.6.4)
    tank = dg.check_cargo({"dangerous_goods": True, "tank": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "II"}}, "road")
    assert next(x for x in tank["findings"] if x["code"] == "dg.adr_tunnel")["data"]["forbidden_tunnel_categories"] == ["D", "E"]
    small = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "II",
                                                           "net_quantity": 4, "unit": "L"}}, "air")
    assert small["status"] == "ok" and small["aircraft"] == "any"
    big = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "II",
                                                         "net_quantity": 20, "unit": "L"}}, "air")
    assert big["status"] == "review" and big["aircraft"] == "cargo_only" and "dg.air_over_passenger_limit" in codes(big)
    over = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "II",
                                                          "net_quantity": 61000, "unit": "ml"}}, "air")
    assert over["status"] == "forbidden" and codes(over)["dg.air_over_cargo_limit"] == "block"


def test_un3480_lithium_ion_cargo_aircraft_only():
    r = dg.lookup("UN3480")
    v = r["variants"][0]
    assert r["class"] == "9" and r["packing_groups"] == []
    assert v["air"]["passenger_forbidden"] is True and v["air"]["cargo_limit"] == "35 kg" and not v["air"]["cargo_forbidden"]
    assert "A100" in v["special_provisions"]
    assert r["adr"]["tunnel_code"] == "E" and r["adr"]["classification_code"] == "M4"
    res = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN3480", "class": "9", "net_quantity": 30, "unit": "kg"}}, "air")
    assert res["aircraft"] == "cargo_only" and res["status"] == "review" and codes(res)["dg.air_cargo_only"] == "warn"
    heavy = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN3480", "class": "9", "net_quantity": 40, "unit": "kg"}}, "air")
    assert heavy["status"] == "forbidden" and "dg.air_over_cargo_limit" in codes(heavy)
    legacy_pg = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN3480", "class": "9", "packing_group": "II"}}, "sea")
    assert codes(legacy_pg)["dg.pg_not_applicable"] == "warn"  # older DGR editions gave PG II; flagged, not blocked


def test_un1005_ammonia_forbidden_by_air():
    r = dg.lookup("UN1005")
    assert r["class"] == "2.3" and r["subsidiary"] == ["8"]
    intl = [v for v in r["variants"] if not v["domestic_only"]]
    assert all(v["air"]["passenger_forbidden"] and v["air"]["cargo_forbidden"] for v in intl)
    assert r["adr"]["tunnel_code"] == "C/D" and r["adr"]["classification_code"] == "2TC" and r["adr"]["hazard_id"] == "268"
    res = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1005", "class": "2.3"}}, "air")
    assert res["status"] == "forbidden" and res["aircraft"] == "forbidden" and codes(res)["dg.air_forbidden"] == "block"
    sea = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1005", "class": "2.3"}}, "sea")
    assert sea["status"] == "review" and "dg.vessel_passenger_restricted" in codes(sea)  # stowage D: not on ro-pax


def test_un0004_explosive_class1_review():
    r = dg.lookup("UN0004")
    assert r["class"] == "1.1D" and r["adr"]["tunnel_code"] == "B1000C"
    res = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN0004", "class": "1.1D"}}, "road")
    assert res["status"] == "review" and codes(res)["dg.class1_review"] == "warn" and res["tunnel_code"] == "B1000C"
    assert dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN0004", "class": "1.1D"}}, "air")["status"] == "forbidden"
    rad = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN2910", "class": "7"}}, "road")
    assert codes(rad)["dg.class7_review"] == "warn" and rad["status"] == "review"


def test_segregation_table_pairs():
    seg = lambda a, b: dg.segregation_between({"division": a, "subsidiary": []}, {"division": b, "subsidiary": []})["code"]  # noqa: E731
    assert seg("3", "5.1") == "2" and seg("5.1", "3") == "2"  # separated from
    assert seg("8", "4.3") == "1" and seg("4.3", "8") == "1"  # away from
    assert seg("2.1", "1.1D") == "4" and seg("9", "3") == "X"
    one = dg.check_cargo({"dg_items": [{"un_number": "UN1263", "class": "3", "packing_group": "II"},
                                       {"un_number": "UN1479", "class": "5.1", "packing_group": "II"}]}, "sea")
    assert one["status"] == "forbidden" and one["segregation"][0]["code"] == "2" and one["segregation"][0]["severity"] == "block"
    two = dg.check_cargo({"containers": [{"type": "20GP", "count": 2}],
                          "dg_items": [{"un_number": "UN1263", "class": "3", "packing_group": "II"},
                                       {"un_number": "UN1479", "class": "5.1", "packing_group": "II"}]}, "sea")
    assert two["status"] == "review" and two["segregation"][0]["severity"] == "warn"
    away = dg.check_cargo({"dg_items": [{"un_number": "UN1789", "class": "8", "packing_group": "II"},
                                        {"un_number": "UN1396", "class": "4.3", "packing_group": "II"}]}, "sea")
    assert away["segregation"][0]["code"] == "1" and away["status"] == "review"
    # subsidiary hazard counts (176.83(a)(6)): ammonia 2.3 (8) vs a 4.3 is at least "away from"
    sub = dg.check_cargo({"dg_items": [{"un_number": "UN1005", "class": "2.3"}, {"un_number": "UN1396", "class": "4.3", "packing_group": "II"}]}, "sea")
    assert sub["segregation"][0]["code"] in ("1", "2")


def test_declared_mismatch_blocks_and_unknown_un():
    wrong_class = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1263", "class": "8", "packing_group": "II"}}, "road")
    assert wrong_class["status"] == "forbidden" and codes(wrong_class)["dg.class_mismatch"] == "block"
    assert "class 3" in wrong_class["findings"][0]["en"] and wrong_class["findings"][0]["fi"]
    wrong_pg = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1203", "class": "3", "packing_group": "III"}}, "road")
    assert codes(wrong_pg)["dg.pg_mismatch"] == "block"
    unknown = dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN9999", "class": "3"}}, "road")
    assert codes(unknown)["dg.un_unknown"] == "block"
    assert dg.check_cargo({"dangerous_goods": True, "dg": {"un_number": "UN1263", "class": "3.0"}}, "road")["status"] == "forbidden"


def test_search():
    assert dg.search("UN1203")[0]["un"] == "UN1203"
    assert any(x["un"] == "UN3480" for x in dg.search("lithium ion"))
    assert all(x["un"].startswith("UN126") for x in dg.search("126"))


# ------------------------------------------------------------------------------------------------ raw sources
@pytest.mark.skipif(not CACHE, reason="DG_SOURCE_CACHE not set (build cache with the downloaded source files)")
def test_values_match_raw_source_text():
    s = open(os.path.join(CACHE, "172.101.xml"), encoding="utf-8").read()

    def row(un, contains):
        for tr in re.findall(r"<TR>(.*?)</TR>", s, re.S):
            cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)]
            if len(cells) == 14 and cells[3] == un and contains in cells[1]:
                return cells
        raise AssertionError(un)
    assert row("UN1203", "Gasoline")[2:6] == ["3", "UN1203", "II", "3"] and row("UN1203", "Gasoline")[10:12] == ["5 L", "60 L"]
    assert row("UN3480", "Lithium ion")[10:12] == ["Forbidden", "35 kg"]
    am = row("UN1005", "Ammonia, anhydrous")
    assert am[0] == "I" and am[2] == "2.3" and am[5] == "2.3, 8" and am[10:12] == ["Forbidden", "Forbidden"]
    import pdfplumber
    pdf = pdfplumber.open(os.path.join(CACHE, "ECE_TRANS_352-EN.pdf"))
    p302 = pdf.pages[301].extract_text()
    assert re.search(r"0004 AMMONIUM PICRATE.*1 1\.1D 1 0 E0", p302) and "(B1000C)" in p302
    p334 = pdf.pages[333].extract_text()
    assert re.search(r"1005 AMMONIA, ANHYDROUS 2 2TC 2\.3", p334) and "(C/D)" in p334


# ------------------------------------------------------------------------------------------------ HTTP
def _up():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"S12 API not reachable at {API}: {exc}")


def _login(email, password=PW):
    _up()
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {r.json()['token']}", "X-Include-Test-Tenants": "true"},
                        timeout=180)


def _mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def _ready(days=3):
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


@pytest.fixture(scope="module")
def customer():
    """A real signed-up customer, KYC approved and sanctions clear (those states are owned elsewhere; set directly)."""
    _up()
    email = f"s12dg-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={"email": email, "password": "Testi-salasana-2026", "name": "S12 DG Testaaja",
                                                           "company_name": f"S12 DG Testi Oy {uuid.uuid4().hex[:5]}", "locale": "fi",
                                                           "accept_terms": True})
    assert r.status_code == 200, r.text
    cid = r.json()["user"]["company_id"]
    _mongo().companies.update_one({"id": cid}, {"$set": {"kyc_status": "approved", "sanctions_status": "clear", "vat_registered": True,
                                                         "test": True}})
    return _login(email, "Testi-salasana-2026"), cid


ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}
DG_PAINT = {"un_number": "UN1263", "class": "3", "packing_group": "II", "proper_shipping_name": "Paint",
            "emergency_phone": "+358 9 471 977"}


def _approved_quote(cust, body):
    c, _ = cust
    r = c.post("/quotes", json={**body, "ready_date": _ready()})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    if q["status"] == "pending_approval":
        a = _mongo().approvals.find_one({"ref_id": q["id"], "kind": "quote_threshold", "status": "pending"}, {"_id": 0})
        ok = _login("ops.demo@huolitsija.fi").post(f"/approvals/{a['id']}/decide", json={"decision": "approved", "note": "DG checked"})
        assert ok.status_code == 200, ok.text
        q = c.get(f"/quotes/{q['id']}").json()["quote"]
    return q


def _booking(q, cargo=None):
    return {"quote_id": q["id"], "option_id": q["options"][0]["id"], "shipper": {"name": "Tallinna Värv OÜ", "address": ADDR_TLL},
            "consignee": {"name": "Testi Oy", "address": ADDR_TRE}, "pickup": {"address": ADDR_TLL, "date": _ready(4)},
            "delivery": {"address": ADDR_TRE}, "accept_terms": True, **({"cargo": cargo} if cargo else {})}


def test_http_lookup_search_check_and_public():
    ops = _login("ops.demo@huolitsija.fi")
    r = ops.get("/compliance/dg/un/UN1203")
    assert r.status_code == 200 and r.json()["adr"]["tunnel_code"] == "D/E"
    assert ops.get("/compliance/dg/un/UN9999").status_code == 404
    assert ops.get("/compliance/dg/search", params={"q": "ammonia, anhydrous"}).json()["items"][0]["un"] == "UN1005"
    chk = ops.post("/compliance/dg/check", json={"cargo": {"dangerous_goods": True, "dg": {"un_number": "UN1005", "class": "2.3"}}, "mode": "air"})
    assert chk.status_code == 200 and chk.json()["status"] == "forbidden"
    assert httpx.get(f"{API}/compliance/dg/un/UN1203", timeout=30).status_code == 401
    pub = httpx.get(f"{API}/public/dg/un/3480", timeout=30)
    assert pub.status_code == 200 and pub.json()["variants"][0]["air"]["passenger_forbidden"] is True


def test_quote_refuses_air_for_un_forbidden_by_air(customer):
    c, _ = customer
    cargo = {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 400, "volume_m3": 2.0,
             "dangerous_goods": True, "dg": {"un_number": "UN1005", "class": "2.3"}}
    only_air = c.post("/quotes", json={"mode": "air", "origin": "CNPVG", "destination": "FIHEL", "ready_date": _ready(), "cargo": cargo})
    assert only_air.status_code == 400 and "forbidden by air" in only_air.json()["detail"]
    # carbon disulfide (class 3, PG I, 6.1): air carriers accept class 3, but 49 CFR 9A/9B forbid it on all aircraft
    cargo = {**cargo, "dg": {"un_number": "UN1131", "class": "3", "packing_group": "I"}}
    assert dg.check_cargo(cargo, "air")["status"] == "forbidden"
    anym = c.post("/quotes", json={"origin": "CNPVG", "destination": "FIHEL", "ready_date": _ready(), "cargo": cargo})
    assert anym.status_code == 200, anym.text
    q = anym.json()["quote"]
    assert not [o for o in q["options"] if o["mode"] == "air"]
    assert any(n.get("code") == "dg_forbidden" and n["mode"] == "air" for n in q["not_quoted"]), q["not_quoted"]
    assert q["request"]["dg_check"]["by_mode"]["air"]["status"] == "forbidden"
    assert any(w["code"] == "dg.air_forbidden" for w in q["warnings"])


def test_booking_with_declared_class_mismatch_is_409(customer):
    q = _approved_quote(customer, {"origin": "EETLL", "destination": "FITMP", "cargo": {
        "load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2.0,
        "dangerous_goods": True, "dg": DG_PAINT}})
    assert q["status"] == "sent" and q["options"], q.get("not_quoted")
    assert q["request"]["dg_check"]["status"] == "ok"
    c, _ = customer
    r = c.post("/bookings", json=_booking(q, {"dangerous_goods": True, "dg": {**DG_PAINT, "class": "8"}}))
    assert r.status_code == 409 and "UN1263 is class 3" in r.json()["detail"], r.text
    assert not _mongo().shipments.find_one({"quote_id": q["id"]})
    audit = _mongo().audit_log.find_one({"action": "dg_check_booking_refused", "entity_id": q["id"]})
    assert audit and audit["actor"]["id"] == "compliance" and audit["data"]["status"] == "forbidden"


def test_booking_needing_review_flags_shipment_and_opens_card(customer):
    items = [{"un_number": "UN1263", "class": "3", "packing_group": "II"}, {"un_number": "UN1479", "class": "5.1", "packing_group": "II"}]
    q = _approved_quote(customer, {"origin": "EETLL", "destination": "FITMP", "cargo": {
        "load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2.0,
        "dangerous_goods": True, "dg": DG_PAINT, "dg_items": items}})
    assert q["options"], q.get("not_quoted")
    c, _ = customer
    r = c.post("/bookings", json=_booking(q))
    assert r.status_code == 200, r.text
    sid = r.json()["shipment_id"]
    shp = _mongo().shipments.find_one({"id": sid}, {"_id": 0})
    assert shp["dg_review_required"] is True and shp["dg_check"]["status"] == "review"
    card = _mongo().approvals.find_one({"kind": "dg_review", "ref_id": sid}, {"_id": 0})
    assert card and card["status"] == "pending" and card["data"]["legal_point"] == "dangerous_goods"
    assert any(x["code"] == "dg.segregation" for x in card["data"]["reasons"])
