"""Shared helpers for the V2 tests (quotes, pricing, bookings, carriers, public quote). No tests here.

The V2 tests talk to the REAL local API over HTTP (default http://localhost:8102/api, override with V2_API) backed by
the real MongoDB (V2_DB, default huo_v2) with the seeded indicative rate cards. Nothing is mocked: prices come from the
rate engine, bookings create real V1 shipments, approvals go through the real approvals queue, the AI parse test calls
the configured model. Accounts made here use real sign-up; the only direct DB writes are the KYC / sanctions states
that V3 owns (there is no V2 API for them) and they are marked test=True.

Env: V2_API, V2_DB, MONGO_URL, DEMO_OPS_PASSWORD, DEMO_CUSTOMER_PASSWORD.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from functools import lru_cache

import httpx
import pytest
from pymongo import MongoClient

API = os.environ.get("V2_API", "http://localhost:8102/api")
DB_NAME = os.environ.get("V2_DB", "huo_v2")
OPS_PW = os.environ.get("DEMO_OPS_PASSWORD", "V2-ops-demo-2026")
CUSTOMER_PW = os.environ.get("DEMO_CUSTOMER_PASSWORD", "V2-customer-demo-2026")


CREATED: dict[str, set] = {"companies": set(), "public_quotes": set()}


@pytest.fixture(scope="session", autouse=True)
def v2_cleanup():
    """Session teardown: delete what this run created, so the shared dev DB stays usable for demos.
    Set V2_KEEP_TEST_DATA=1 to keep it for inspection."""
    yield
    if os.environ.get("V2_KEEP_TEST_DATA") or not (CREATED["companies"] or CREATED["public_quotes"]):
        return
    db = mongo()
    cids = list(CREATED["companies"])
    qids = [q["id"] for q in db.quotes.find({"company_id": {"$in": cids}}, {"id": 1})] + list(CREATED["public_quotes"])
    sids = [s["id"] for s in db.shipments.find({"company_id": {"$in": cids}}, {"id": 1})]
    links = [f"/ops/quotes/{i}" for i in qids] + [f"/portal/quotes/{i}" for i in qids] + [f"/ops/shipments/{i}" for i in sids]
    db.approvals.delete_many({"ref_id": {"$in": qids}})
    db.notifications.delete_many({"$or": [{"company_id": {"$in": cids}}, {"link": {"$in": links}}]})
    db.tracking_events.delete_many({"shipment_id": {"$in": sids}})
    db.shipments.delete_many({"id": {"$in": sids}})
    db.quotes.delete_many({"id": {"$in": qids}})
    db.email_outbox.delete_many({"company_id": {"$in": cids}})
    db.users.delete_many({"company_id": {"$in": cids}, "test": True})
    db.companies.delete_many({"id": {"$in": cids}, "test": True})


@lru_cache
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def _client(token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers["X-Include-Test-Tenants"] = "true"
    return httpx.Client(base_url=API, headers=headers, timeout=120)


def api_up():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"V2 API not reachable at {API}: {exc}")


def login(email: str, password: str) -> httpx.Client:
    api_up()
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return _client(r.json()["token"])


@lru_cache
def ops() -> httpx.Client:
    return login("ops.demo@huolitsija.fi", OPS_PW)


@lru_cache
def demo_customer() -> httpx.Client:
    """The seeded demo customer: used read-only so test runs do not clutter the demo account."""
    return login("customer.demo@huolitsija.fi", CUSTOMER_PW)


@lru_cache
def _test_customer() -> tuple[httpx.Client, dict]:
    """A real signed-up customer for this test run, verified (KYC approved, sanctions clear) and VAT-registered."""
    c, user = signup("V2 Testiasiakas Oy")
    set_company(user["company_id"], kyc_status="approved", sanctions_status="clear", vat_registered=True)
    return c, user


def cust() -> httpx.Client:
    return _test_customer()[0]


def cust_company_id() -> str:
    return _test_customer()[1]["company_id"]


def signup(company_name: str | None = None) -> tuple[httpx.Client, dict]:
    """Real sign-up (POST /api/auth/signup): new company with kyc_status not_started, sanctions not_screened."""
    api_up()
    email = f"v2test-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={
        "email": email, "password": "Testi-salasana-2026", "name": "V2 Testaaja",
        "company_name": company_name or f"V2 Testi Oy {uuid.uuid4().hex[:5]}", "locale": "fi", "accept_terms": True})
    assert r.status_code == 200, r.text
    body = r.json()
    mongo().users.update_one({"email": email}, {"$set": {"test": True}})
    mongo().companies.update_one({"id": body["user"]["company_id"]}, {"$set": {"test": True}})
    CREATED["companies"].add(body["user"]["company_id"])
    return _client(body["token"]), body["user"]


def set_company(company_id: str, **fields):
    """KYC / sanctions / VAT registration are owned by V3; tests set the outcome directly."""
    mongo().companies.update_one({"id": company_id}, {"$set": fields})


def ready(days: int = 3) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def ops_quote(body: dict, company_id: str | None = None, send: bool = False) -> dict:
    """Price through POST /api/quotes as ops (returns cost fields) for this run's test customer."""
    r = ops().post("/quotes", json={**body, "company_id": company_id or cust_company_id(), "send": send})
    assert r.status_code == 200, r.text
    return r.json()["quote"]


def line(opt: dict, code: str) -> dict | None:
    return next((ln for ln in opt["lines"] if ln["code"] == code), None)


def lines(opt: dict, code: str) -> list[dict]:
    return [ln for ln in opt["lines"] if ln["code"] == code]


def by_service(q: dict, service: str) -> list[dict]:
    return [o for o in q["options"] if o["service"] == service]


ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}


def booking_body(quote: dict, option_id: str | None = None, **over) -> dict:
    b = {"quote_id": quote["id"], "option_id": option_id or quote["options"][0]["id"],
         "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL},
         "consignee": {"name": "Testi Oy", "address": ADDR_TRE},
         "pickup": {"address": ADDR_TLL, "date": ready(4)}, "delivery": {"address": ADDR_TRE},
         "customer_reference": "PO-T-1", "accept_terms": True}
    b.update(over)
    return b


PALLETS_TLL_TRE = {"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
                   "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                             "volume_m3": 2.0, "value_cents": 800000}}
