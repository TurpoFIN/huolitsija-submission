"""Shared helpers for the V4 tests (no tests here).

The tests talk to the real local API (default http://localhost:8104/api, DB huo_v4) over HTTP. Fixture records
(companies, quotes, shipments, declarations) are written straight to MongoDB in the shape the owning slices use,
because creating them through V1/V2/V3 would make these tests depend on those slices' availability. Every fixture
carries test=True so it can be told apart from demo data.

Env: V4_API, V4_DB, MONGO_URL, DEMO_OPS_PASSWORD, DEMO_CUSTOMER_PASSWORD.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import httpx
import pymongo

API = os.environ.get("V4_API", "http://localhost:8104/api")
os.environ.setdefault("DB_NAME", os.environ.get("V4_DB", "huo_v4"))  # before any backend module reads core.config
db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[os.environ.get("V4_DB", "huo_v4")]
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    return datetime.now(timezone.utc)


def client_for(email: str, password: str) -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return httpx.Client(base_url=API, headers={"X-Include-Test-Tenants": "true", "Authorization": f"Bearer {r.json()['token']}",
                                               "Origin": "http://localhost:3104"}, timeout=60)


@lru_cache
def ops() -> httpx.Client:
    return client_for("ops.demo@huolitsija.fi", os.environ["DEMO_OPS_PASSWORD"])


@lru_cache
def customer() -> httpx.Client:
    return client_for("customer.demo@huolitsija.fi", os.environ["DEMO_CUSTOMER_PASSWORD"])


def customer_company_id() -> str:
    return db.users.find_one({"email": "customer.demo@huolitsija.fi"})["company_id"]


def make_user(company_id: str, role: str = "customer_admin") -> tuple[httpx.Client, dict]:
    from core.security import hash_password

    pw = "Test-" + uuid.uuid4().hex[:12] + "9"
    u = {"id": str(uuid.uuid4()), "email": f"v4test-{uuid.uuid4().hex[:8]}@example.fi", "name": "V4 Test User",
         "password_hash": hash_password(pw), "role": role, "org_type": "customer", "company_id": company_id,
         "locale": "fi", "email_verified": True, "session_version": 0, "notify_email": False, "test": True,
         "created_at": iso(now())}
    db.users.insert_one(dict(u))
    return client_for(u["email"], pw), u


def make_company(country: str = "FI", vat_id: str | None = "FI39900099", name: str | None = None, terms: int = 14) -> dict:
    c = {"id": str(uuid.uuid4()), "name": name or f"V4 Testi Oy {uuid.uuid4().hex[:5]}", "business_id": "3990009-9",
         "vat_id": vat_id, "country": country, "address": {"street": "Testikatu 1", "postal_code": "00100", "city": "Helsinki",
                                                           "country": country},
         "kyc_status": "approved", "sanctions_status": "clear", "payment_terms_days": terms,
         "billing_email": f"laskut-{uuid.uuid4().hex[:6]}@example.fi", "vat_registered": True, "status": "active",
         "test": True, "created_at": iso(now())}
    db.companies.insert_one(dict(c))
    return c


def make_shipment(company: dict, lines: list[dict] | None = None, *, mode: str = "sea", origin=("CNSHA", "Shanghai", "CN", 31.23, 121.47),
                  destination=("FIHEL", "Helsinki", "FI", 60.2, 25.1), status: str = "out_for_delivery",
                  direction: str = "import", regime: str | None = "HAGUE_VISBY", role: str = "principal",
                  weight_kg: float = 12000, packages: int = 24, delivered_days_ago: float | None = None,
                  extra: dict | None = None) -> dict:
    """Quote (V2 shape) + shipment (V1 shape) linked by quote_id/option_id."""
    q_id, opt_id, s_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    lines = lines if lines is not None else [
        {"code": "OCEAN_FREIGHT", "description": "Merirahti 1×40HC", "basis": "PER_CONTAINER", "qty": 1,
         "unit_cost_cents": 190000, "unit_price_cents": 245000, "amount_cents": 245000, "vat_code": "ZERO_INTL_TRANSPORT",
         "is_pass_through": False},
        {"code": "DELIVERY", "description": "Kotimaan jakelu Vuosaari–Lahti", "basis": "PER_SHIPMENT", "qty": 1,
         "unit_cost_cents": 30000, "unit_price_cents": 38500, "amount_cents": 38500, "vat_code": "FI_25_5",
         "is_pass_through": False},
    ]
    total = sum(ln["amount_cents"] for ln in lines)
    eta = now() - timedelta(hours=6)
    o = {"name": origin[1], "unlocode": origin[0], "code": origin[0], "country": origin[2], "lat": origin[3], "lon": origin[4]}
    d = {"name": destination[1], "unlocode": destination[0], "code": destination[0], "country": destination[2],
         "lat": destination[3], "lon": destination[4]}
    quote = {"id": q_id, "number": f"Q-TEST-{uuid.uuid4().hex[:6]}", "company_id": company["id"], "status": "booked",
             "options": [{"id": opt_id, "mode": mode, "carrier_name": "Test Line", "lines": lines, "total_cents": total,
                          "eta": iso(eta), "liability_regime": regime}],
             "accepted_option_id": opt_id, "shipment_id": s_id, "test": True, "created_at": iso(now())}
    legs = [{"seq": 1, "role": "main", "mode": mode, "from": o, "to": d, "carrier": "Test Line", "status": "in_progress",
             "etd": iso(now() - timedelta(days=20)), "eta": iso(eta), "atd": iso(now() - timedelta(days=20)), "ata": None,
             "distance_km": 1000}]
    s = {"id": s_id, "reference": f"HUO-TEST-{uuid.uuid4().hex[:6].upper()}", "company_id": company["id"],
         "customer_name": company["name"], "quote_id": q_id, "option_id": opt_id, "mode": mode, "direction": direction,
         "forwarder_role": role, "status": status, "milestone": status, "origin": o, "destination": d, "legs": legs,
         "milestones": [{"code": "booked", "label": "Booking confirmed", "at": iso(now() - timedelta(days=21)), "planned": None}],
         "etd": iso(now() - timedelta(days=20)), "eta": iso(eta), "eta_planned": iso(eta), "atd": iso(now() - timedelta(days=20)),
         "ata": None, "delivered_at": None, "position": {"lat": d["lat"], "lon": d["lon"], "updated_at": iso(now()), "source": "gps"},
         "cargo": {"description": "Huonekaluja", "packages": packages, "package_type": "CT", "weight_kg": weight_kg},
         "weight_kg": weight_kg, "references": {"customer_ref": "PO-TEST-1", "po_numbers": []}, "has_exception": False,
         "holds": [], "price": {"total_cents": total}, "liability_regime": regime, "customs_declaration_ids": [],
         "invoice_ids": [], "lien_exercised": False, "test": True,
         "created_at": iso(now() - timedelta(days=max(21, (delivered_days_ago or 0) + 14)))}
    if delivered_days_ago is not None:
        at = iso(now() - timedelta(days=delivered_days_ago))
        s.update(status="delivered", milestone="delivered", delivered_at=at)
        s["milestones"].append({"code": "delivered", "label": "Delivered", "at": at, "planned": None})
    if extra:
        s.update(extra)
    db.quotes.insert_one(dict(quote))
    db.shipments.insert_one(dict(s))
    return s


def make_declaration(shipment: dict, *, duty_cents: int, vat_cents: int, vat_route: str, status: str = "released") -> dict:
    d = {"id": str(uuid.uuid4()), "lrn": f"LRN{uuid.uuid4().hex[:10]}", "mrn": f"26FI{uuid.uuid4().hex[:14].upper()}",
         "shipment_id": shipment["id"], "company_id": shipment["company_id"], "type": "import", "status": status,
         "vat_route": vat_route, "totals": {"customs_value_cents": 2_500_000, "duty_cents": duty_cents, "vat_cents": vat_cents},
         "test": True, "created_at": iso(now())}
    if status == "released":
        d["customs_decision"] = {"source": "tulli_decision", "mrn": d["mrn"], "decision_date": date.today().isoformat(),
                                "duty_cents": duty_cents, "vat_cents": vat_cents, "paid_by": "forwarder",
                                "items": [{"seq": 1, "duty_cents": duty_cents, "vat_cents": vat_cents}]}
    db.customs_declarations.insert_one(dict(d))
    db.shipments.update_one({"id": shipment["id"]}, {"$push": {"customs_declaration_ids": d["id"]}})
    return d


def wait_for(fn, timeout: float = 90, every: float = 1.5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    return last


def fi_check_digit(base: str) -> int:
    """Independent 7-3-1 implementation (Finanssiala reference standard)."""
    weights = [7, 3, 1]
    total = sum(int(ch) * weights[i % 3] for i, ch in enumerate(reversed(base)))
    return (10 - total % 10) % 10


def rf_valid(rf: str) -> bool:
    s = rf[4:] + rf[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
