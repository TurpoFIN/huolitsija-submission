"""Helpers for test_s7_credit.py (no tests here).

The tests talk HTTP to the real local API (V7_API, default http://localhost:8140/api) backed by MongoDB V7_DB. Customers
are created through the real sign-up; KYC / sanctions outcomes (owned by the KYC slice) and backdated due dates are
written straight to MongoDB and marked test=True, like the V2 / V4 suites do.

Env: V7_API, V7_DB, MONGO_URL, DEMO_OPS_PASSWORD.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import date, timedelta
from functools import lru_cache

import httpx
import pymongo

API = os.environ.get("V7_API", "http://localhost:8140/api")
DB_NAME = os.environ.get("V7_DB", "huo_s7_credit_1")
db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def client(token: str | None = None) -> httpx.Client:
    h = {"X-Include-Test-Tenants": "true", "Origin": "http://localhost:3000"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=h, timeout=120)


def login(email: str, password: str) -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return client(r.json()["token"])


@lru_cache
def ops() -> httpx.Client:
    return login("ops.demo@huolitsija.fi", os.environ.get("DEMO_OPS_PASSWORD", "ci-pass-7"))


def signup(name: str | None = None, **company_fields) -> tuple[httpx.Client, dict]:
    """Real sign-up; then the KYC slice's outcome (approved, sanctions clear) and any company fields are set directly."""
    email = f"s7credit-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={
        "email": email, "password": "Testi-salasana-2026", "name": "S7 Luottotestaaja",
        "company_name": name or f"S7 Luotto Oy {uuid.uuid4().hex[:5]}", "locale": "fi", "accept_terms": True})
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    db.users.update_one({"email": email}, {"$set": {"test": True}})
    db.companies.update_one({"id": user["company_id"]}, {"$set": {
        "test": True, "kyc_status": "approved", "sanctions_status": "clear", "vat_registered": True,
        "address": {"street": "Testikatu 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"}, **company_fields}})
    return client(r.json()["token"]), user


def ready(days: int = 4) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


PALLETS_TLL_TRE = {"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
                   "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                             "volume_m3": 2.0, "value_cents": 800000}}
ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}


def quote(c: httpx.Client) -> dict:
    r = c.post("/quotes", json={**PALLETS_TLL_TRE, "ready_date": ready()})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    assert q["status"] == "sent", q["status"]
    return q


def booking_body(q: dict) -> dict:
    return {"quote_id": q["id"], "option_id": q["options"][0]["id"],
            "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL}, "consignee": {"name": "Testi Oy", "address": ADDR_TRE},
            "pickup": {"address": ADDR_TLL, "date": ready(5)}, "delivery": {"address": ADDR_TRE},
            "customer_reference": "PO-S7-CREDIT", "accept_terms": True}


def manual_invoice(company_id: str, net_cents: int, issue: bool = True) -> dict:
    """Real invoice through POST /api/invoices (+ issue): domestic freight line, VAT 25.5 %."""
    r = ops().post("/invoices", json={"company_id": company_id, "issue": issue, "send": False, "lines": [
        {"description": "Kotimaan kuljetus Helsinki–Tampere", "qty": 1, "unit_price_cents": net_cents, "vat_code": "FI_25_5"}]})
    assert r.status_code == 200, r.text
    inv = r.json()
    if issue and inv["status"] == "draft":
        r = ops().post(f"/invoices/{inv['id']}/issue", json={"send": False})
        assert r.status_code == 200, r.text
        inv = r.json()
    return inv


def pay(invoice_id: str, amount_cents: int | None = None) -> dict:
    body = {"paid_at": date.today().isoformat(), "reference": f"S7-{uuid.uuid4().hex[:10]}"}
    if amount_cents is not None:
        body["amount_cents"] = amount_cents
    r = ops().post(f"/invoices/{invoice_id}/mark-paid", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def position(company_id: str) -> dict:
    r = ops().get(f"/credit/{company_id}")
    assert r.status_code == 200, r.text
    return r.json()["position"]


def set_limit(company_id: str, cents: int):
    r = ops().patch(f"/credit/{company_id}", json={"credit_limit_cents": cents, "reason": "s7 test"})
    assert r.status_code == 200, r.text
    return r.json()["position"]


def autopilot(on: bool):
    r = ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})
    assert r.status_code == 200, r.text


def wait_for(fn, timeout: float = 60, step: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()
