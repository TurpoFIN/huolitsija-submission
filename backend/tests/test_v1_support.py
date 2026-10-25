"""Shared helpers for the V1 tests (no tests here).

The tests talk to the real local API (default http://localhost:8101/api, DB huo_v1) over HTTP, and through it to the
real external services the V1 slice uses (Digitraffic AIS/port calls/rail, OSRM, Nominatim, searoute data). Nothing
is mocked. Records the tests create carry test=True or a TEST- reference so they can be told apart from demo data.

Env: V1_API, V1_DB, MONGO_URL, DEMO_OPS_PASSWORD, DEMO_CUSTOMER_PASSWORD, DEMO_PARTNER_PASSWORD.
"""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

import httpx
import pymongo

API = os.environ.get("V1_API", "http://localhost:8101/api")
os.environ.setdefault("DB_NAME", os.environ.get("V1_DB", "huo_v1"))  # before any backend module reads core.config
db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[os.environ.get("V1_DB", "huo_v1")]
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def client_for(email: str, password: str) -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return httpx.Client(base_url=API, headers={"X-Include-Test-Tenants": "true", "Authorization": f"Bearer {r.json()['token']}"}, timeout=90)


@lru_cache
def ops() -> httpx.Client:
    return client_for("ops.demo@huolitsija.fi", os.environ["DEMO_OPS_PASSWORD"])


@lru_cache
def customer() -> httpx.Client:
    return client_for("customer.demo@huolitsija.fi", os.environ["DEMO_CUSTOMER_PASSWORD"])


@lru_cache
def partner() -> httpx.Client:
    return client_for("partner.demo@huolitsija.fi", os.environ["DEMO_PARTNER_PASSWORD"])


def customer_company_id() -> str:
    return db.users.find_one({"email": "customer.demo@huolitsija.fi"})["company_id"]


# ---- closed airspace: Natural Earth 1:110m rings of Russia, Belarus and Ukraine, plain ray casting

@lru_cache
def closed_airspace() -> dict[str, list[list[list[float]]]]:
    return json.loads((FIXTURES / "v1_closed_airspace_ne110m.json").read_text())["countries"]


def _in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def country_at(lon: float, lat: float) -> str | None:
    lon = ((lon + 180) % 360) - 180
    for code, rings in closed_airspace().items():
        if any(_in_ring(lon, lat, r) for r in rings):
            return code
    return None


def sample_path(path: list[list[float]], step_km: float = 10.0):
    """Every vertex plus points every step_km along each segment (linear in lon/lat; segments are <= 40 km)."""
    from services.geo import haversine_km

    for a, b in zip(path, path[1:]):
        n = max(1, int(haversine_km(a, b) / step_km))
        for k in range(n):
            f = k / n
            yield [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
    if path:
        yield path[-1]


# ---- shipments created by tests (marked test=True, removed by cleanup_test_shipments)

def demo_company_id(key: str) -> str:
    from services.seed_core import demo_company_id as dcid

    return dcid(key)


def partner_carrier_id() -> str:
    return db.users.find_one({"email": "partner.demo@huolitsija.fi"})["carrier_id"]


def create_test_shipment(**over) -> dict:
    from datetime import datetime, timedelta, timezone

    etd = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"company_id": customer_company_id(), "mode": "road", "origin": "FIHEL", "destination": "FITMP", "etd": etd,
            "carrier": "Kaukokiito", "service": "FTL", "customs_clearance": False,
            "cargo": {"description": "TEST V1 pallets", "packages": 4, "package_type": "pallets", "weight_kg": 1800},
            "references": {"customer_ref": "TEST-V1"}}
    body.update(over)
    # Explicit test fixture authorization, scoped to this booking; seed data remains unverified.
    company = db.companies.find_one({"id": body["company_id"]})
    original = {k: company.get(k) for k in ("kyc_status", "sanctions_status")}
    db.companies.update_one({"id": body["company_id"]}, {"$set": {"kyc_status": "approved", "sanctions_status": "clear"}})
    try:
        r = ops().post("/shipments", json=body)
    finally:
        db.companies.update_one({"id": body["company_id"]}, {"$set": original})
    assert r.status_code == 200, f"create test shipment: {r.status_code} {r.text}"
    s = r.json()
    db.shipments.update_one({"id": s["id"]}, {"$set": {"test": True}})
    return s


def cleanup_test_shipments():
    ids = [d["id"] for d in db.shipments.find({"test": True}, {"id": 1})]
    if ids:
        db.tracking_events.delete_many({"shipment_id": {"$in": ids}})
        db.documents.delete_many({"shipment_id": {"$in": ids}, "type": "pod"})
        db.position_reports.delete_many({"shipment_id": {"$in": ids}})
        db.shipment_notes.delete_many({"shipment_id": {"$in": ids}})
        threads = [t["id"] for t in db.threads.find({"shipment_id": {"$in": ids}}, {"id": 1})]
        db.messages.delete_many({"thread_id": {"$in": threads}})
        db.threads.delete_many({"id": {"$in": threads}})
        db.shipments.delete_many({"id": {"$in": ids}})
    db.companies.delete_many({"test": True, "id": {"$regex": "^test-v1-"}})
