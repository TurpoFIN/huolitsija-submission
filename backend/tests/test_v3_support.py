"""Shared helpers for the V3 tests (no tests here). The V3 tests run against the REAL local API (default
http://localhost:8103, override with V3_API) and the real external services (YTJ, VIES, EU EOS, sanctions lists,
ECB, the AI provider configured on the server). Nothing is mocked.

Setup data that has no public API yet (an import shipment with an accepted quote: V2's booking API is not
merged) is written straight into the server's MongoDB (V3_DB, default huo_v3) and marked test: true.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pymongo import MongoClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

API = os.environ.get("V3_API", "http://localhost:8103/api")
DB_NAME = os.environ.get("V3_DB", "huo_v3")
PASSWORD = os.environ.get("V3_DEMO_PASSWORD", "v3-local-Demo-2026")
OPS = "ops.demo@huolitsija.fi"
CUSTOMER = "customer.demo@huolitsija.fi"


def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def client(email: str | None = None, password: str = PASSWORD) -> httpx.Client:
    c = httpx.Client(base_url=API, timeout=180, headers={"X-Include-Test-Tenants": "true"})
    try:
        c.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"V3 API not reachable at {API}: {exc}")
    if email:
        r = c.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return c


def ai_available(ops: httpx.Client) -> bool:
    r = ops.get("/integrations/status")
    return r.status_code == 200 and bool(r.json().get("ai", {}).get("available"))


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_import_shipment(company_name: str = "Pohjolan Kaluste Oy", *, incoterm="FOB", place="Cat Lai, Ho Chi Minh City",
                         weight=1460.0, mode="sea", origin=("Cat Lai, Ho Chi Minh City", "VNCLI", "VN"),
                         customs_handling: str | None = None) -> dict:
    """Test import shipment + accepted quote (lines carry V2's stage field). Marked test: True.
    customs_handling="broker": a person clears it, so the Autopilot customs agent leaves its declaration alone."""
    db = mongo()
    comp = db.companies.find_one({"name": company_name})
    assert comp, f"demo company {company_name} missing (SEED_DEMO must be on)"
    now = datetime.now(timezone.utc)
    qid, oid, sid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    lines = [
        {"code": "OFR", "description": "Ocean freight 40' HC", "stage": "main", "leg_seq": 1, "amount_cents": 185000, "is_pass_through": False},
        {"code": "BAF", "description": "Bunker adjustment", "stage": "main", "leg_seq": 1, "amount_cents": 42000, "is_pass_through": False},
        {"code": "FEEDER", "description": "Feeder Hamburg - Helsinki", "stage": "main", "leg_seq": 2, "amount_cents": 31000, "is_pass_through": False},
        {"code": "INS", "description": "Cargo insurance", "stage": "insurance", "amount_cents": 4500, "is_pass_through": False},
        {"code": "DTHC", "description": "Destination terminal handling", "stage": "destination", "leg_seq": 3, "amount_cents": 26000, "is_pass_through": False},
        {"code": "DELIVERY", "description": "Delivery to Lahti", "stage": "destination", "leg_seq": 3, "amount_cents": 38000, "is_pass_through": False},
        {"code": "CUSTOMS", "description": "Import customs clearance", "stage": "fees", "amount_cents": 6500, "is_pass_through": False},
        {"code": "DUTY", "description": "Customs duty (estimate)", "stage": "pass_through", "amount_cents": 0, "is_pass_through": True},
    ]
    db.quotes.insert_one({"id": qid, "number": f"Q-TEST-{sid[:6]}", "company_id": comp["id"], "status": "booked", "test": True,
                          "options": [{"id": oid, "mode": mode, "legs": [{"seq": 1, "kind": "main"}, {"seq": 2, "kind": "connection"},
                                                                         {"seq": 3, "kind": "on"}],
                                       "lines": lines, "total_cents": sum(x["amount_cents"] for x in lines)}],
                          "accepted_option_id": oid, "created_at": iso(now)})
    ship = {"id": sid, "reference": f"HUO-TEST-{sid[:6].upper()}", "company_id": comp["id"], "customer_name": comp["name"],
            "quote_id": qid, "option_id": oid, "mode": mode, "direction": "import", "incoterm": incoterm, "incoterm_place": place,
            "status": "customs_hold", "milestones": [], "origin": {"name": origin[0], "unlocode": origin[1], "country": origin[2]},
            "destination": {"name": "Helsinki Vuosaari", "unlocode": "FIVSS", "country": "FI"},
            "legs": [{"seq": 1, "mode": mode, "from": {"name": origin[0]}, "to": {"name": "Hamburg"}, "vessel": {"name": "TEST VESSEL"},
                      "voyage": "001W", "transport_doc_no": f"MAEU{sid[:8].upper().replace('-', '0')}"}],
            "cargo": {"description": "Oak dining furniture", "packages": 68, "weight_kg": weight, "volume_m3": 22.4,
                      "containers": [{"type": "40HC", "count": 1}]},
            "holds": [{"type": "customs", "reason": "Import declaration pending", "since": iso(now)}],
            "eta": iso(now + timedelta(days=3)), "test": True, "created_at": iso(now), "parties": {}, "references": {}}
    if customs_handling:
        ship["customs_handling"] = customs_handling
    db.shipments.insert_one(dict(ship))
    return {"shipment": ship, "company": comp, "quote_lines": lines}


def cleanup_import_shipment(created):
    """Remove only this fixture's minimal shipment/quote after its assertions."""
    db = mongo()
    ship = created["shipment"]
    db.customs_declarations.delete_many({"shipment_id": ship["id"]})
    db.shipments.delete_one({"id": ship["id"], "test": True})
    db.quotes.delete_one({"id": ship["quote_id"], "test": True})


INVOICE_LINES = [
    {"description": "Solid oak dining chair with upholstered fabric seat, model AURA", "hs": "9401.61.00", "origin": "VN", "qty": 48,
     "unit": "pcs", "unit_price": 38.50, "amount": 1848.00, "net": 345.6, "gross": 388.8},
    {"description": "Extendable dining table, solid oak, 180-240 cm, model AURA", "hs": "9403.60.10", "origin": "VN", "qty": 12,
     "unit": "pcs", "unit_price": 214.00, "amount": 2568.00, "net": 576.0, "gross": 660.0},
    {"description": "Oak sideboard with three drawers, model LINEA", "hs": "9403.60.10", "origin": "VN", "qty": 8, "unit": "pcs",
     "unit_price": 176.00, "amount": 1408.00, "net": 328.0, "gross": 380.0},
]


def invoice_pdfs(number: str, buyer: dict) -> tuple[bytes, bytes]:
    from services.seed_customs import render_invoice_pdf, render_packing_list_pdf

    seller = {"name": "Mekong Timber Furniture Co., Ltd.", "address": "Lot B2, Song Than 3 Industrial Park, Thu Dau Mot, Binh Duong",
              "country": "VN", "country_name": "Viet Nam", "vat": "3701987654"}
    inv = {"number": number, "date": "2026-10-14", "po": "PO-24117", "seller": seller, "buyer": buyer, "incoterm": "FOB",
           "place": "Cat Lai, Ho Chi Minh City", "currency": "USD", "lines": INVOICE_LINES, "packages": 68, "package_type": "cartons",
           "gross": 1428.8, "net": 1249.6, "payment_terms": "30% deposit, 70% against copy B/L"}
    pl = {"number": f"PL-{number}", "date": "2026-10-14", "invoice_number": number, "shipper": seller, "consignee": buyer,
          "lines": [{**ln, "packages": p, "package_type": "cartons"} for ln, p in zip(INVOICE_LINES, [48, 12, 8])],
          "packages": 68, "package_type": "cartons", "gross": 1428.8, "net": 1249.6, "volume": 22.4,
          "containers": ["MSKU7316254"], "seals": ["SL448812"], "marks": f"POHJOLAN / {number} / 1-68"}
    return render_invoice_pdf(inv, sample=False), render_packing_list_pdf(pl, sample=False)


def buyer_of(comp: dict) -> dict:
    a = comp.get("address") or {}
    return {"name": comp["name"], "address": f"{a.get('street')}, {a.get('postal_code')} {a.get('city')}", "country": "FI",
            "vat": comp.get("vat_id"), "eori": comp.get("eori")}


def wait(fn, timeout=240, every=2.0):
    import time

    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    return last


TEST_CUSTOMER = "customs.test@huolitsija.fi"
TEST_COMPANY = "Testi Huonekalu Oy (V3 test)"


def ensure_test_customer() -> dict:
    """A test-only importer whose KYC is approved by the test fixture (not by a register check: its identifiers are
    fictional, so the live registers would rightly refuse them). Lets the filing test run independently of whatever
    the demo companies' live KYC currently says. Idempotent; marked test: True."""
    import bcrypt

    from core.refs import ytunnus_valid

    db = mongo()
    comp = db.companies.find_one({"name": TEST_COMPANY})
    today = datetime.now(timezone.utc).date()
    if not comp:
        body = next(f"{n:07d}" for n in range(3990101, 3999999) if any(ytunnus_valid(f"{n:07d}-{d}") for d in range(10)))
        bid = next(f"{body}-{d}" for d in range(10) if ytunnus_valid(f"{body}-{d}"))
        comp = {"id": str(uuid.uuid4()), "name": TEST_COMPANY, "business_id": bid, "vat_id": "FI" + bid.replace("-", ""),
                "eori": f"FI{bid}", "country": "FI", "status": "active", "test": True,
                "address": {"street": "Testikatu 1", "postal_code": "15100", "city": "Lahti", "country": "FI"},
                "created_at": iso(datetime.now(timezone.utc))}
        db.companies.insert_one(dict(comp))
    db.companies.update_one({"id": comp["id"]}, {"$set": {
        "kyc_status": "approved", "sanctions_status": "clear", "vat_registered": True, "tilitys_eligible": True,
        "representation_default": "direct", "credit_limit_cents": 5000000, "payment_terms_days": 14,
        "kyc": {"decision": "approved", "decided_by": {"type": "system", "name": "V3 test fixture"}, "note": "test data"},
        "poa": {"type": "direct", "scope": ["import", "export"], "valid_from": (today - timedelta(days=1)).isoformat(),
                "valid_to": (today + timedelta(days=364)).isoformat(), "signed_at": iso(datetime.now(timezone.utc)),
                "revoked_at": None, "test": True, "signature": {"typed_name": "Test Signer", "user_agent": "pytest"}}}})
    if not db.users.find_one({"email": TEST_CUSTOMER}):
        db.users.insert_one({"id": str(uuid.uuid4()), "email": TEST_CUSTOMER, "name": "Customs Test",
                             "password_hash": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode(),
                             "role": "customer_admin", "org_type": "customer", "company_id": comp["id"], "locale": "en",
                             "email_verified": True, "session_version": 0, "test": True, "notify_email": False,
                             "created_at": iso(datetime.now(timezone.utc))})
    return db.companies.find_one({"id": comp["id"]}, {"_id": 0})


def mark_test_company(email: str):
    """Signups made by tests are test data: hide them from the ops customer list (status/test flags)."""
    db = mongo()
    u = db.users.find_one({"email": email}, {"company_id": 1})
    if u:
        db.companies.update_one({"id": u["company_id"]}, {"$set": {"test": True}})
        db.users.update_one({"email": email}, {"$set": {"test": True}})
