"""s12 sanctions and denied parties on REAL list data (no mocks): UK Sanctions List (FCDO) and the US Consolidated
Screening List (data.trade.gov) next to EU / UN / OFAC SDN, party screening of real bookings, the compliance hold and
its approval card, false-positive clearing, a confirmed match, carrier screening and re-screening on a list change.

Two halves share one database (S12_DB, default huo_s12_sanctions):
- direct import (lists, scorer): this module imports core with DB_NAME=S12_DB and downloads the lists when they are
  older than 24 h (about 30 s for all five);
- HTTP against a local API on the same database (S12_API, default http://127.0.0.1:8121/api), started with
  CI_DEMO_PASSWORD=<pw> DB_NAME=huo_s12_sanctions PORT=8121 bash scripts/run-ci-backend.sh (S12_PASSWORD = that pw).
  Bookings go through POST /api/bookings, decisions through POST /api/approvals/{id}/decide.

The list-change test stores a new UN list version through the real path (refresh_lists(force=True) downloads the UN
list again and _store writes a new version), which emits sanctions.lists_changed; the re-screen runs in this process.

Real names used: designated subjects are picked from the downloaded files (UK: designations by the UK alone; CSL: an
open Denied Persons List order) plus well-known listings (Huawei Technologies Co., Ltd. on the BIS Entity List;
Transsphere Oy and Russian Cargo Oy, Finnish companies on the BIS Entity List; MM Cargo Oy on the BIS Unverified List;
Sichuan Anxun Information Technology Co., Ltd. (i-Soon) on the UK list). "Lator Oy" is on the Denied Persons List with
an order that ended on 2000-05-02, so it must stay clear. Real Finnish names: the demo companies, eight names captured
from the YTJ open-data API (GET https://avoindata.prh.fi/opendata-ytj-api/v3/companies?mainBusinessLine=49410&location=
Vantaa, 2026-10-24, tests/fixtures/ytj_49410_vantaa.json on feat/partner-network) and listed Finnish companies.
"""
import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
S12_DB = os.environ.get("S12_DB", "huo_s12_sanctions")
if "core.db" not in sys.modules:
    os.environ["DB_NAME"] = S12_DB

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import httpx  # noqa: E402
import pytest  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from core import compliance_jobs  # noqa: E402
from core.db import db  # noqa: E402
from services import party_screening, screening  # noqa: E402

API = os.environ.get("S12_API", "http://127.0.0.1:8121/api")  # backend started with COOKIE_SECURE=false
PW = os.environ.get("S12_PASSWORD", "s12-local-Demo-2026")
NEUTRAL = party_screening.HOLD_REASON


def run(coro):
    return LOOP.run_until_complete(coro)


DEMO_COMPANIES = ["Pohjolan Kaluste Oy", "Nordic Bike Parts Oy", "Saimaan Kahvipaahtimo Oy", "Arktinen Elektroniikka Oy",
                  "Kymen Teräskauppa Oy", "Lapin Retkivaruste Oy", "Turun Venetarvike Oy", "Helsinki Design House Oy"]
YTJ_NAMES = ["Kuljetusliike V. Mönkkönen Oy", "Kuljetus Oy Laakso", "Kuljetus J.J. Oksanen Oy", "JJ Huoltomestari",
             "Kuljetus ja maanrakennus Katupojat Oy", "Nordig Finland Oy", "Kuljetusliike A Björklund & Co", "Kuljetus Oy T-J"]
LISTED_FI = ["Kesko Oyj", "Valio Oy", "Wärtsilä Oyj Abp", "Finnair Oyj", "Posti Group Oyj", "Konecranes Oyj", "Steveco Oy",
             "Finnlines Oyj", "Nurminen Logistics Oyj", "Kemira Oyj", "Neste Oyj", "UPM-Kymmene Oyj", "Stora Enso Oyj",
             "Cargotec Oyj", "Containerships Oy", "ESL Shipping Oy", "Kuehne + Nagel Oy", "DSV Finland Oy", "Euro Cargo Oy",
             "Baltic Logistics Oy", "Nordic Cargo Oy", "Huolintakeskus Oy"]


@pytest.fixture(scope="module", autouse=True)
def lists_loaded():
    status = run(screening.refresh_lists())
    return {s["list"]: s for s in status}


# ------------------------------------------------------------------------------------------------ lists
def test_uk_and_csl_load_with_plausible_counts(lists_loaded):
    for key, minimum in (("uk", 5000), ("csl", 4000)):
        s = lists_loaded[key]
        assert s["loaded"] and s["status"] == "ok", (key, s["error"])
        assert s["entries"] > minimum, (key, s["entries"])
        assert s["generated_at"] and s["fetched_at"] and len(s["sha256"]) == 64 and len(s["file_sha256"]) == 64
        assert s["source_url"].startswith("https://") and s["authority"]
        assert run(db.sanctions_entries.count_documents({"list": key, "version": s["version"]})) == s["entries"]
    assert lists_loaded["uk"]["source_url"] == "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"
    assert "data.trade.gov" in lists_loaded["csl"]["source_url"]
    assert [s["list"] for s in lists_loaded.values()] == ["eu", "un", "ofac", "uk", "csl"]
    assert all(s["loaded"] for s in lists_loaded.values())


def test_csl_keeps_sub_lists_and_skips_sdn_and_expired_denials(lists_loaded):
    v = lists_loaded["csl"]["version"]
    subs = set(run(db.sanctions_entries.distinct("sub_list", {"list": "csl", "version": v})))
    assert {"BIS Entity List", "BIS Denied Persons List", "BIS Unverified List", "BIS Military End User List",
            "State Dept ITAR Debarred List"} <= subs, subs
    assert "OFAC SDN List" not in subs  # the ofac list holds the SDN rows
    today = date.today().isoformat()
    assert run(db.sanctions_entries.count_documents({"list": "csl", "version": v, "end_date": {"$lt": today}})) == 0
    uk = run(db.sanctions_entries.count_documents({"list": "uk", "version": lists_loaded["uk"]["version"], "type": "vessel"}))
    assert uk > 100  # the UK list designates ships too


@pytest.mark.parametrize("name,kind,lst,sub", [
    ("Huawei Technologies Co., Ltd.", "entity", "csl", "BIS Entity List"),
    ("Transsphere Oy", "entity", "csl", "BIS Entity List"),
    ("Russian Cargo Oy", "entity", "csl", "BIS Entity List"),
    ("MM Cargo Oy", "entity", "csl", "BIS Unverified List"),
    ("Semiconductor Manufacturing International Corporation", "entity", "csl", None),
    ("Sichuan Anxun Information Technology Co., Ltd.", "entity", "uk", None),
    ("The State Research Institute of Military Medicine", "entity", "uk", None),
])
def test_known_listed_names_hit_the_right_list(name, kind, lst, sub):
    r = run(screening.screen_name(name, kind=kind))
    assert r["status"] == "potential_match", r
    mine = [h for h in r["hits"] if h["list"] == lst and (sub is None or h["sub_list"] == sub)]
    assert mine, [(h["list"], h["sub_list"], h["matched_name"]) for h in r["hits"]]
    h = mine[0]
    assert h["score"] >= screening.MATCH_THRESHOLD and h["ref"] in h["explanation"]
    assert h["list_label"] in h["explanation"]
    if sub:
        assert sub in h["list_label"]


def test_uk_only_designation_from_the_file_hits_uk(lists_loaded):
    """A UK-only designation (DesignationSource UK) from the downloaded file, designated in 2026."""
    e = run(db.sanctions_entries.find_one({"list": "uk", "version": lists_loaded["uk"]["version"], "type": "entity",
                                           "designation_source": "UK", "listed_on": {"$gte": "2026-01-01"}},
                                          {"_id": 0}, sort=[("ref", 1)]))
    assert e, "no 2026 UK-only entity designation in the file"
    r = run(screening.screen_name(e["primary_name"], kind="entity"))
    assert any(h["list"] == "uk" and h["ref"] == e["ref"] for h in r["hits"]), (e["primary_name"], r["hits"])


def test_open_denied_persons_order_hits_with_sub_list_label(lists_loaded):
    """A Denied Persons List order from the file that has no end date (or ends more than a year from now)."""
    later = (date.today() + timedelta(days=365)).isoformat()
    e = run(db.sanctions_entries.find_one({"list": "csl", "version": lists_loaded["csl"]["version"],
                                           "sub_list": "BIS Denied Persons List",
                                           "$or": [{"end_date": None}, {"end_date": {"$gt": later}}]},
                                          {"_id": 0}, sort=[("ref", 1)]))
    assert e, "no open Denied Persons List order"
    r = run(screening.screen_name(e["primary_name"]))
    hit = next((h for h in r["hits"] if h["ref"] == e["ref"]), None)
    assert hit, (e["primary_name"], r["hits"])
    assert hit["sub_list"] == "BIS Denied Persons List"
    assert hit["list_label"] == "US Consolidated Screening List: BIS Denied Persons List"
    assert "Denied Persons List" in hit["explanation"]


def test_expired_denial_order_stays_clear():
    r = run(screening.screen_name("Lator Oy", kind="entity", country="FI"))  # DPL order 1990-05-02 .. 2000-05-02
    assert not any(h["list"] == "csl" for h in r["hits"]), r["hits"]


def test_sdn_hits_are_not_counted_twice():
    r = run(screening.screen_name("Banco Nacional de Cuba", kind="entity"))
    assert any(h["list"] == "ofac" for h in r["hits"])
    assert not any(h["list"] == "csl" and h["sub_list"] == "OFAC SDN List" for h in r["hits"])


@pytest.mark.parametrize("name", DEMO_COMPANIES + YTJ_NAMES + LISTED_FI)
def test_real_finnish_company_names_stay_clear(name):
    r = run(screening.screen_name(name, kind="entity", country="FI"))
    assert r["status"] == "clear", [(h["list"], h["matched_name"], h["score"]) for h in r["hits"]]
    assert r["complete"] is True


def test_screening_stays_fast_with_five_lists():
    run(screening.screen_name("warm up the index"))
    stats = run(screening.index_stats())
    assert stats["subjects"] > 35000 and stats["aliases"] > 100000, stats
    names = DEMO_COMPANIES + YTJ_NAMES + ["Mohammed Ali Hassan", "The State Research Institute of Military Medicine",
                                          "Islamic Revolutionary Guard Corps", "Huawei Technologies Co., Ltd.",
                                          "Global Trading International Ltd", "Sergey Shoygu", "Oy Holding Company Ab"]
    times = []
    for n in names:
        t = time.perf_counter()
        run(screening.screen_name(n))
        times.append((time.perf_counter() - t) * 1000)
    assert statistics.median(times) < 100, sorted(times)
    assert max(times) < 400, sorted(times)


# ------------------------------------------------------------------------------------------------ HTTP: bookings
@pytest.fixture(scope="module")
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[S12_DB]


CREATED: dict = {"companies": [], "carriers": []}


def _client(token=None):
    h = {"X-Include-Test-Tenants": "true"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=h, timeout=180)


@pytest.fixture(scope="module")
def ops():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"S12 API not reachable at {API}: {exc}")
    r = httpx.post(f"{API}/auth/login", json={"email": "ops.demo@huolitsija.fi", "password": PW}, timeout=30)
    assert r.status_code == 200, r.text
    return _client(r.json()["token"])


@pytest.fixture(scope="module")
def customer(mongo, ops):
    email = f"s12test-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={
        "email": email, "password": "Testi-salasana-2026", "name": "S12 Testaaja", "company_name": "S12 Seulontatesti Oy",
        "locale": "fi", "accept_terms": True})
    assert r.status_code == 200, r.text
    body = r.json()
    cid = body["user"]["company_id"]
    if not mongo.companies.find_one({"id": cid}):
        pytest.fail(f"The API at {API} does not write to {S12_DB}: point S12_API at a backend started with DB_NAME={S12_DB}")
    CREATED["companies"].append(cid)
    mongo.users.update_one({"email": email}, {"$set": {"test": True}})
    # KYC is owned by services.kyc (YTJ, VIES); the test sets its outcome directly, as the V2 booking tests do
    mongo.companies.update_one({"id": cid}, {"$set": {"test": True, "kyc_status": "approved", "sanctions_status": "clear",
                                                      "vat_registered": True}})
    yield _client(body["token"])
    if os.environ.get("S12_KEEP_TEST_DATA"):
        return
    sids = [s["id"] for s in mongo.shipments.find({"company_id": cid}, {"id": 1})]
    qids = [q["id"] for q in mongo.quotes.find({"company_id": cid}, {"id": 1})]
    mongo.approvals.delete_many({"ref_id": {"$in": sids + qids + [cid] + CREATED["carriers"]}})
    rows = [r["id"] for r in mongo.party_screenings.find({"subject_id": {"$in": sids + qids + [cid] + CREATED["carriers"]}}, {"id": 1})]
    mongo.sanctions_clearances.delete_many({"screening_id": {"$in": rows}})  # this run's false-positive decisions
    mongo.party_screenings.delete_many({"id": {"$in": rows}})
    mongo.notifications.delete_many({"company_id": cid})
    mongo.tracking_events.delete_many({"shipment_id": {"$in": sids}})
    mongo.shipments.delete_many({"id": {"$in": sids}})
    mongo.quotes.delete_many({"id": {"$in": qids}})
    mongo.carriers.delete_many({"id": {"$in": CREATED["carriers"]}, "test": True})
    mongo.users.delete_many({"company_id": cid, "test": True})
    mongo.companies.delete_many({"id": cid, "test": True})


def _ready(days=4):
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}


def _book(customer, consignee_name: str, consignee_addr: dict) -> dict:
    q = customer.post("/quotes", json={"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
                                       "ready_date": _ready(3), "cargo": {"load_type": "loose", "packages": 2,
                                                                          "package_type": "pallet_eur", "weight_kg": 600,
                                                                          "volume_m3": 2.0, "value_cents": 800000}})
    assert q.status_code == 200, q.text
    quote = q.json()["quote"]
    r = customer.post("/bookings", json={
        "quote_id": quote["id"], "option_id": quote["options"][0]["id"],
        "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL},
        "consignee": {"name": consignee_name, "address": consignee_addr},
        "pickup": {"address": ADDR_TLL, "date": _ready(4)}, "delivery": {"address": consignee_addr},
        "customer_reference": "S12-T-1", "accept_terms": True})
    assert r.status_code == 200, r.text
    return r.json()


ADDR_KOTKA = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}
ADDR_HAMINA = {"street": "Hämeenkatu 10", "city": "Tampere", "postal_code": "33100", "country": "FI"}


@pytest.fixture(scope="module")
def listed_booking(customer, mongo):
    """Real booking whose consignee is Transsphere Oy (Finnish company on the BIS Entity List)."""
    return _book(customer, "Transsphere Oy", ADDR_KOTKA)


def test_booking_with_listed_consignee_sets_hold_and_card(listed_booking, customer, ops, mongo):
    b = listed_booking
    assert NEUTRAL in b["booking"]["warnings"]
    s = mongo.shipments.find_one({"id": b["shipment_id"]}, {"_id": 0})
    assert s["compliance_hold"] is True and s["compliance_hold_reason"] == NEUTRAL
    assert s["compliance_hold_code"] == "sanctions_potential_match" and s["compliance_status"] == "pending_review"
    assert any(h["type"] == "sanctions" and h["reason"] == NEUTRAL for h in s["holds"])
    row = mongo.party_screenings.find_one({"id": s["compliance_screening_id"]}, {"_id": 0})
    assert row["subject_type"] == "shipment" and row["trigger"] == "booking" and row["status"] == "potential_match"
    assert set(row["list_versions"]) == {"eu", "un", "ofac", "uk", "csl"}
    cons = next(p for p in row["parties"] if p["role"] == "consignee")
    assert cons["status"] == "potential_match"
    assert any(h["sub_list"] == "BIS Entity List" and h["list"] == "csl" for h in cons["hits"])
    roles = {p["role"]: p["status"] for p in row["parties"]}
    assert roles["shipper"] == "clear" and roles["customer"] == "clear" and "carrier" in roles
    pre = mongo.party_screenings.find_one({"id": row["booking_screening_id"]}, {"_id": 0})
    assert pre["subject_type"] == "booking" and pre["status"] == "potential_match"
    card = mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": s["id"], "status": "pending"}, {"_id": 0})
    assert card and card["data"]["screening_id"] == row["id"] and card["legal_point"] == "sanctions_potential_match"
    assert "BIS Entity List" in card["summary"]
    lst = ops.get("/approvals", params={"kind": "sanctions_review"}).json()["items"]
    assert any(a["id"] == card["id"] and a["can_decide"] for a in lst)
    # the shipment cannot move while held
    r = ops.post(f"/shipments/{s['id']}/status", json={"status": "picked_up"})
    assert r.status_code == 409 and "sanctions hold" in r.json()["detail"].lower()
    # the customer sees the neutral text only, never the list or the matched entry
    cv = customer.get(f"/shipments/{s['id']}")
    assert cv.status_code == 200
    text = cv.text
    assert NEUTRAL in text
    for secret in ("Entity List", "CSL-", "potential_match", "compliance_screening_id", "pending_review"):
        assert secret not in text, secret
    holds = ops.get("/compliance/holds").json()["items"]
    mine = next(x for x in holds if x["shipment_id"] == s["id"])
    assert mine["approval_id"] == card["id"] and mine["screening_id"] == row["id"] and mine["since"]
    assert mine["reference"] == s["reference"] and "BIS Entity List" in mine["reason"]


def test_false_positive_decision_lifts_the_hold_and_is_remembered(listed_booking, ops, mongo):
    sid = listed_booking["shipment_id"]
    card = mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": sid, "status": "pending"}, {"_id": 0})
    r = ops.post(f"/approvals/{card['id']}/decide", json={"decision": "approved", "note": "Different company: test"})
    assert r.status_code == 200, r.text
    s = mongo.shipments.find_one({"id": sid}, {"_id": 0})
    assert s["compliance_hold"] is False and s["compliance_status"] == "cleared"
    assert not any(h["type"] == "sanctions" for h in s["holds"])
    row = mongo.party_screenings.find_one({"id": card["data"]["screening_id"]}, {"_id": 0})
    assert row["status"] == "cleared" and row["decided"]["outcome"] == "false_positive"
    assert row["decided"]["by"]["type"] == "user"
    assert mongo.sanctions_clearances.find_one({"name": "Transsphere Oy", "list": "csl"})
    assert mongo.audit_log.find_one({"action": "compliance.hold_cleared", "entity_id": sid})
    # a re-screen sees the same hit as cleared earlier and does not hold again
    again = ops.post(f"/compliance/shipments/{sid}/screen").json()
    assert again["status"] == "clear"
    cons = next(p for p in again["parties"] if p["role"] == "consignee")
    assert cons["hits"] and all(h["previously_cleared"] for h in cons["hits"])
    assert mongo.shipments.find_one({"id": sid})["compliance_hold"] is False


def test_confirmed_match_keeps_the_shipment_blocked(customer, ops, mongo):
    b = _book(customer, "MM Cargo Oy", ADDR_HAMINA)  # BIS Unverified List
    sid = b["shipment_id"]
    card = mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": sid, "status": "pending"}, {"_id": 0})
    assert card, "no review card"
    row = mongo.party_screenings.find_one({"id": card["data"]["screening_id"]}, {"_id": 0})
    cons = next(p for p in row["parties"] if p["role"] == "consignee")
    assert any(h["sub_list"] == "BIS Unverified List" for h in cons["hits"])
    r = ops.post(f"/approvals/{card['id']}/decide", json={"decision": "rejected", "note": "Confirmed listed party"})
    assert r.status_code == 200, r.text
    s = mongo.shipments.find_one({"id": sid}, {"_id": 0})
    assert s["compliance_status"] == "blocked_sanctions" and s["compliance_hold"] is True
    assert any(h["type"] == "sanctions" for h in s["holds"])
    assert mongo.party_screenings.find_one({"id": row["id"]})["status"] == "blocked_sanctions"
    # a later re-screen never lifts a confirmed block
    ops.post(f"/compliance/shipments/{sid}/screen")
    assert mongo.shipments.find_one({"id": sid})["compliance_status"] == "blocked_sanctions"


@pytest.fixture(scope="module")
def clean_booking(customer):
    return _book(customer, "Kuljetusliike V. Mönkkönen Oy", ADDR_KOTKA)


def test_clean_booking_is_not_held(clean_booking, mongo):
    s = mongo.shipments.find_one({"id": clean_booking["shipment_id"]}, {"_id": 0})
    assert not s.get("compliance_hold") and s["compliance_status"] == "clear"
    assert NEUTRAL not in clean_booking["booking"]["warnings"]
    row = mongo.party_screenings.find_one({"id": s["compliance_screening_id"]}, {"_id": 0})
    assert row["status"] == "clear" and len(row["parties"]) >= 4
    assert not mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": s["id"]})


def test_list_version_change_rescreens_open_shipments(clean_booking, mongo):
    sid = clean_booking["shipment_id"]
    before = mongo.sanctions_lists.find_one({"list": "un"})["version"]
    t0 = party_screening.now_iso()
    status = {s["list"]: s for s in run(screening.refresh_lists(force=True, only=["un"]))}
    new = status["un"]["version"]
    assert status["un"]["status"] == "ok" and new != before
    if compliance_jobs.has_durable_jobs():  # s2 core.jobs: the change is a durable job (the API's worker may run it first)
        from core import jobs
        job = mongo.jobs.find_one({"kind": "compliance.rescreen_all", "created_at": {"$gte": t0}}, {"_id": 0})
        assert job, "the list change did not queue a re-screen job"
        run(jobs.work_once("s12-test", 5, kinds=["compliance.rescreen_all"]))
        deadline = time.monotonic() + 180
        while mongo.jobs.find_one({"id": job["id"]}, {"_id": 0, "status": 1})["status"] != "done":
            assert time.monotonic() < deadline, mongo.jobs.find_one({"id": job["id"]}, {"_id": 0})
            time.sleep(1)
    else:
        pending = list(compliance_jobs._bg)
        assert pending, "the list change did not start a re-screen"
        run(asyncio.gather(*pending))
    row = mongo.party_screenings.find_one({"subject_type": "shipment", "subject_id": sid, "trigger": "list_change",
                                           "created_at": {"$gte": t0}}, {"_id": 0})
    assert row, "open shipment was not re-screened"
    assert row["list_versions"]["un"] == new and row["status"] == "clear"
    assert mongo.audit_log.find_one({"action": "compliance.rescreen", "entity_id": sid, "data.trigger": "list_change"})
    assert mongo.audit_log.find_one({"action": "compliance.rescreen", "actor.id": "compliance"})
    company_row = mongo.party_screenings.find_one({"subject_type": "company", "trigger": "list_change", "created_at": {"$gte": t0}})
    assert company_row, "KYC-approved customers were not re-screened"
    done = mongo.compliance_runs.find_one({"name": "compliance.rescreen_all.last"})
    assert done["trigger"] == "list_change" and done["result"]["shipments"] >= 1


def test_carrier_is_screened_when_created(ops, mongo):
    code = f"S12{uuid.uuid4().hex[:5].upper()}"
    r = ops.post("/carriers", json={"name": "Russian Cargo Oy", "code": code, "modes": ["road"], "country": "FI"})
    assert r.status_code == 200, r.text
    cid = r.json()["carrier"]["id"]
    CREATED["carriers"].append(cid)
    mongo.carriers.update_one({"id": cid}, {"$set": {"test": True}})
    car = mongo.carriers.find_one({"id": cid}, {"_id": 0})
    assert car["sanctions_status"] == "potential_match"
    row = mongo.party_screenings.find_one({"id": car["sanctions_screening"]["screening_id"]}, {"_id": 0})
    assert row["subject_type"] == "carrier" and any(h["sub_list"] == "BIS Entity List" for h in row["parties"][0]["hits"])
    assert mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": cid, "status": "pending"})


def test_compliance_routes(ops, customer):
    lists = ops.get("/compliance/lists").json()
    assert lists["loaded"] == lists["total"] == 5
    assert {x["list"] for x in lists["items"]} == {"eu", "un", "ofac", "uk", "csl"}
    assert lists["ofsi_consolidated_list"]["loaded"] is False
    rows = ops.get("/compliance/screenings", params={"subject_type": "shipment", "limit": 5}).json()
    assert rows["items"] and all(r["subject_type"] == "shipment" for r in rows["items"])
    one = ops.get(f"/compliance/screenings/{rows['items'][0]['id']}")
    assert one.status_code == 200 and one.json()["parties"]
    assert ops.get("/compliance/screenings", params={"status": "nope"}).status_code == 400
    assert customer.get("/compliance/lists").status_code == 403
    assert customer.get("/compliance/screenings").status_code == 403
    assert customer.post("/compliance/rescreen-all").status_code == 403


def test_false_positive_is_scoped_to_the_party_identity(customer, ops, mongo):
    """A person cleared "Transsphere Oy" at one address (earlier test). The same name at another address is a different
    party: it must be held again, not waved through by the remembered decision."""
    other = {"street": "Mannerheimintie 1", "city": "Helsinki", "postal_code": "00100", "country": "FI"}
    sid = _book(customer, "Transsphere Oy", other)["shipment_id"]
    s = mongo.shipments.find_one({"id": sid}, {"_id": 0})
    assert s.get("compliance_hold") is True, s
    assert mongo.approvals.find_one({"kind": "sanctions_review", "ref_id": sid, "status": "pending"})
    c = mongo.sanctions_clearances.find_one({"name": "Transsphere Oy"}, {"_id": 0})
    assert c and c["identity"].startswith("addr:") and c["expires_at"] > c["at"], c
