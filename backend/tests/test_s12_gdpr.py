"""s12 GDPR export/erasure, legal holds and the retention sweep, against the REAL local API.

Run a backend first (own DB), e.g.
    CI_DEMO_PASSWORD=s12-local-Demo-2026 DB_NAME=huo_s12_gdpr PORT=8123 bash scripts/run-ci-backend.sh
    DB_NAME=huo_s12_gdpr S12_API=http://localhost:8123/api python -m pytest -q backend/tests/test_s12_gdpr.py

Everything a user or ops person does goes through HTTP: sign-up, KYC decision, quote, booking, upload, message,
invoice, privacy request, e-mailed code (read from email_outbox: EMAIL_PROVIDER=log is the real send path), verify,
download, approval, legal hold, retention run. Only setup data that needs a date in the past (old quotes, invoices,
customs records, tokens for the retention sweep) is written straight into MongoDB and marked test: true, and the
s2 kill-switch flag (autopilot_halted) is set in the settings doc because s2's API for it is not merged.
"""
import asyncio
import io
import json
import os
import random
import re
import sys
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from pymongo import MongoClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.environ.setdefault("DB_NAME", "huo_s12_gdpr")

API = os.environ.get("S12_API", "http://localhost:8123/api")
DB_NAME = os.environ["DB_NAME"]
PASSWORD = os.environ.get("S12_DEMO_PASSWORD", "s12-local-Demo-2026")
OPS = "ops.demo@huolitsija.fi"
CUST_PW = "Testi-salasana-2026"
ERASED_NAME = "Poistettu käyttäjä / Erased user"
ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}
PALLETS = {"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
           "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600,
                     "volume_m3": 2.0, "value_cents": 800000}}
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)  # core.db binds Motor to the loop that is current at import
PACKING = b"package;type;weight_kg\n1;pallet_eur;300\n2;pallet_eur;300\n"


def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


def fake_ip() -> str:
    return f"198.51.100.{random.randint(1, 254)}"  # TEST-NET-2: one client address per test, so per-IP limits are per test


def client(email: str | None = None, password: str = PASSWORD, ip: str | None = None) -> httpx.Client:
    c = httpx.Client(base_url=API, timeout=180, headers={"X-Include-Test-Tenants": "true", "X-Forwarded-For": ip or fake_ip()})
    try:
        c.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"API not reachable at {API}: {exc}")
    if email:
        r = c.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return c


def ops():
    return client(OPS)


def ready(days: int = 4) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def set_autopilot(on: bool):
    r = ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})
    assert r.status_code == 200, r.text


def customer(tag: str) -> dict:
    """Real sign-up + profile phone + ops KYC decision (approved, sanctions clear) through the API."""
    email = f"e2e+gdpr-{tag}-{uuid.uuid4().hex[:8]}@example.com"
    c = client()
    r = c.post("/auth/signup", json={"email": email, "password": CUST_PW, "name": f"Maija Testinen {tag}",
                                     "company_name": f"GDPR Testi {tag} Oy {uuid.uuid4().hex[:4]}", "locale": "fi",
                                     "accept_terms": True})
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    phone = f"+358 40 {random.randint(1000000, 9999999)}"
    assert c.patch("/auth/profile", json={"phone": phone}).status_code == 200
    r = ops().post(f"/ops/customers/{user['company_id']}/kyc-decision",
                   json={"decision": "approved", "sanctions_decision": "clear", "note": "s12 GDPR test customer, checked"})
    assert r.status_code == 200, r.text
    return {"client": c, "email": email, "user": user, "company_id": user["company_id"], "phone": phone}


def book(cu: dict) -> dict:
    c = cu["client"]
    r = c.post("/quotes", json={**PALLETS, "ready_date": ready(3), "notes": f"Soita minulle {cu['phone']}"})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    assert q["status"] == "sent", q["status"]
    body = {"quote_id": q["id"], "option_id": q["options"][0]["id"],
            "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL,
                        "contact": {"name": cu["user"]["name"], "email": cu["email"], "phone": cu["phone"]}},
            "consignee": {"name": "Hatanpään Varasto Oy", "address": ADDR_TRE,
                          "contact": {"name": "Kalle Vastaanottaja", "email": "kalle.vastaanottaja@example.com",
                                      "phone": "+358 50 1112223"}},
            "pickup": {"address": ADDR_TLL, "date": ready(4)}, "delivery": {"address": ADDR_TRE},
            "customer_reference": "PO-GDPR-1", "accept_terms": True}
    r = c.post("/bookings", json=body)
    assert r.status_code == 200, r.text
    return {"quote_id": q["id"], "shipment_id": r.json()["shipment_id"]}


def activity(cu: dict) -> dict:
    """Quote + booking + message + two uploads + an issued invoice, all through the API."""
    out = book(cu)
    c = cu["client"]
    r = c.post("/messages/threads", json={"shipment_id": out["shipment_id"], "subject": "Noutoaika",
                                          "body": f"Hei, noutaisitteko aamulla? Tavoitatte minut {cu['email']}."})
    assert r.status_code == 200, r.text
    out["thread_id"], out["message_id"] = r.json()["thread"]["id"], r.json()["message"]["id"]
    files = {"file": ("pakkauslista.csv", PACKING, "text/csv")}
    r = c.post("/documents", data={"type": "other", "shipment_id": out["shipment_id"]}, files=files)
    assert r.status_code == 200, r.text
    out["doc_shipment"] = r.json()["id"]
    r = c.post("/documents", data={"type": "other"},
               files={"file": ("oma-muistio.csv", b"muistio;arvo\nsoita;aamulla\n", "text/csv")})
    assert r.status_code == 200, r.text
    out["doc_loose"] = r.json()["id"]
    r = ops().post("/invoices", json={"company_id": cu["company_id"], "shipment_id": out["shipment_id"], "issue": True,
                                      "lines": [{"description": "Kuljetus Tallinna - Tampere", "qty": 1,
                                                 "unit_price_cents": 45000}]})
    assert r.status_code == 200, r.text
    out["invoice_id"] = r.json()["id"]
    return out


def outbox_code(request_id: str) -> str:
    m = mongo().email_outbox.find_one({"ref.type": "privacy_request", "ref.id": request_id, "tag": {"$regex": "^privacy_code"}},
                                      sort=[("created_at", -1)])
    assert m, "no privacy code e-mail in email_outbox"
    code = re.search(r"\b(\d{6})\b", m["text"])
    assert code, m["text"]
    return code.group(1)


def public_request(kind: str, email: str, c: httpx.Client | None = None) -> dict:
    r = (c or client()).post("/privacy/requests", json={"type": kind, "email": email, "locale": "fi"})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ export
def test_public_export_bundle_and_zip_contain_the_subjects_data():
    cu = customer("exp")
    act = activity(cu)
    req = public_request("export", cu["email"])
    row = mongo().privacy_requests.find_one({"id": req["id"]})
    code = outbox_code(req["id"])
    assert row["code_hash"] and code not in json.dumps(row, default=str)  # the code is stored only as an HMAC
    r = client().post(f"/privacy/requests/{req['id']}/verify", json={"code": code})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["status"] == "ready" and v["token"] and v["expires_at"]
    # the code works once
    assert client().post(f"/privacy/requests/{req['id']}/verify", json={"code": code}).status_code == 409
    assert client().get(f"/privacy/requests/{req['id']}/download", params={"token": v["token"] + "x"}).status_code == 403
    z = client().get(v["download_url"].replace("/api", "", 1))
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = zf.namelist()
    assert {"export.json", "README.txt", "manifest.json"} <= set(names)
    bundle = json.loads(zf.read("export.json"))
    cols = bundle["collections"]
    assert bundle["format"] == "huolitsija.gdpr-export/1" and bundle["subject"]["email"] == cu["email"]
    assert cols["users"]["items"][0]["email"] == cu["email"] and "password_hash" not in cols["users"]["items"][0]
    assert cols["users"]["fields"]["phone"]
    assert act["quote_id"] in {q["id"] for q in cols["quotes"]["items"]}
    assert act["shipment_id"] in {s["id"] for s in cols["shipments"]["items"]}
    assert act["invoice_id"] in {i["id"] for i in cols["invoices"]["items"]}
    assert act["message_id"] in {m["id"] for m in cols["messages"]["items"]}
    assert {act["doc_shipment"], act["doc_loose"]} <= {d["id"] for d in cols["documents"]["items"]}
    assert all("claim_token" not in q for q in cols["quotes"]["items"])
    assert all("kyc" not in c for c in cols.get("companies", {}).get("items", []))
    # the one-time code never leaves in an export
    assert code not in zf.read("export.json").decode()
    files = [n for n in names if n.startswith("files/")]
    assert any(zf.read(n) == PACKING for n in files)
    manifest = json.loads(zf.read("manifest.json"))
    import hashlib
    assert all(hashlib.sha256(zf.read(n)).hexdigest() == h for n, h in manifest.items())
    assert bundle["information"]["retention"] and bundle["information"]["controller"]["name"]
    j = client().get(f"/privacy/requests/{req['id']}/download", params={"token": v["token"], "format": "json"})
    assert j.status_code == 200 and j.json()["collections"]["users"]["count"] == 1
    row = mongo().privacy_requests.find_one({"id": req["id"]})
    assert row["status"] == "completed" and row["downloads"] == 2 and row["due_at"] > row["created_at"]
    actions = {a["action"] for a in mongo().audit_log.find({"entity": "privacy_request", "entity_id": req["id"]})}
    assert {"privacy_request_created", "privacy_code_sent", "privacy_verified", "privacy_export_ready",
            "privacy_export_downloaded"} <= actions


def test_self_service_export_needs_the_password():
    cu = customer("self")
    book(cu)
    c = cu["client"]
    assert c.post("/privacy/me/export", json={"password": "wrong-password-1"}).status_code == 403
    r = c.post("/privacy/me/export", params={"format": "json"}, json={"password": CUST_PW})
    assert r.status_code == 200 and r.json()["collections"]["quotes"]["count"] >= 1
    r = c.post("/privacy/me/export", json={"password": CUST_PW})
    assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).read("export.json")


def test_ops_records_a_request_received_by_letter():
    cu = customer("ops")
    r = ops().post("/privacy/ops/requests", json={"type": "export", "email": cu["email"], "received_via": "letter",
                                                  "identity_note": "Kirje 24.10., henkilöllisyys tarkistettu passista"})
    assert r.status_code == 200, r.text
    d = client().get(r.json()["download_url"].replace("/api", "", 1) + "&format=json")
    assert d.status_code == 200 and d.json()["subject"]["email"] == cu["email"]
    lst = ops().get("/privacy/requests", params={"type": "export"}).json()["items"]
    row = next(x for x in lst if x["id"] == r.json()["id"])
    assert row["channel"] == "ops" and row["received_via"] == "letter" and "code_hash" not in row
    assert cu["client"].get("/privacy/requests").status_code == 403  # customers cannot list requests


# ------------------------------------------------------------------------------------------------ verification
def test_unknown_email_gets_the_same_answer():
    cu = customer("enum")
    known = public_request("export", cu["email"])
    unknown = public_request("export", f"nobody-{uuid.uuid4().hex[:8]}@example.com")
    strip = lambda d: {k: v for k, v in d.items() if k not in ("id", "due_at")}  # noqa: E731
    assert strip(known) == strip(unknown)
    assert set(known) == set(unknown)


def test_wrong_code_five_times_locks_the_request():
    email = f"e2e+gdpr-lock-{uuid.uuid4().hex[:8]}@example.com"
    c = client()
    req = public_request("erasure", email, c)
    code = outbox_code(req["id"])
    wrong = f"{(int(code) + 1) % 1000000:06d}"
    for i in range(4):
        r = c.post(f"/privacy/requests/{req['id']}/verify", json={"code": wrong})
        assert r.status_code == 400 and f"{4 - i} attempts left" in r.json()["detail"], r.text
    r = c.post(f"/privacy/requests/{req['id']}/verify", json={"code": wrong})
    assert r.status_code == 429
    r = c.post(f"/privacy/requests/{req['id']}/verify", json={"code": code})
    assert r.status_code == 429  # locked: even the right code no longer works
    row = mongo().privacy_requests.find_one({"id": req["id"]})
    assert row["status"] == "locked" and row["code_hash"] is None and row["attempts"] == 5
    assert mongo().audit_log.find_one({"entity_id": req["id"], "action": "privacy_request_locked"})


def test_rate_limit_per_email():
    email = f"e2e+gdpr-rate-{uuid.uuid4().hex[:8]}@example.com"
    codes = [client().post("/privacy/requests", json={"type": "export", "email": email}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_expired_code_is_refused():
    email = f"e2e+gdpr-exp-{uuid.uuid4().hex[:8]}@example.com"
    req = public_request("export", email)
    code = outbox_code(req["id"])
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mongo().privacy_requests.update_one({"id": req["id"]}, {"$set": {"code_expires_at": past}})  # 30 minutes later
    r = client().post(f"/privacy/requests/{req['id']}/verify", json={"code": code})
    assert r.status_code == 410


# ------------------------------------------------------------------------------------------------ erasure
def test_public_erasure_under_autopilot_erases_and_keeps_what_law_requires():
    set_autopilot(True)
    try:
        cu = customer("del")
        act = activity(cu)
        inv_before = mongo().invoices.find_one({"id": act["invoice_id"]})
        assert inv_before["buyer"]["billing_email"] == cu["email"] and inv_before["number"]
        req = public_request("erasure", cu["email"])
        r = client().post(f"/privacy/requests/{req['id']}/verify", json={"code": outbox_code(req["id"])})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "completed", out
        res = out["result"]
        assert res["login_disabled"] and not res["errors"]
    finally:
        set_autopilot(False)
    db = mongo()
    # sign-in is gone
    r = client().post("/auth/login", json={"email": cu["email"], "password": CUST_PW})
    assert r.status_code == 401
    assert cu["client"].get("/auth/me").json()["user"] is None  # the old session is revoked too
    u = db.users.find_one({"id": cu["user"]["id"]})
    assert u["email"].endswith("@erased.invalid") and u["name"] == ERASED_NAME and not u.get("password_hash")
    assert not u.get("phone") and u["disabled"] is True
    # quotes: personal fields gone, the other party's contact stays
    q = db.quotes.find_one({"id": act["quote_id"]})
    assert q["requested_by"]["name"] == ERASED_NAME
    assert q["booking"]["shipper"]["contact"] == {"name": None, "email": None, "phone": None}
    assert q["booking"]["consignee"]["contact"]["email"] == "kalle.vastaanottaja@example.com"
    assert cu["phone"] not in json.dumps(q, default=str) and cu["email"] not in json.dumps(q, default=str).lower()
    s = db.shipments.find_one({"id": act["shipment_id"]})
    assert cu["email"] not in json.dumps(s, default=str).lower() and cu["phone"] not in json.dumps(s, default=str)
    m = db.messages.find_one({"id": act["message_id"]})
    assert m["author"]["name"] == ERASED_NAME and cu["email"] not in m["body"] and m["body"].startswith("[Poistettu")
    assert cu["email"] not in (db.threads.find_one({"id": act["thread_id"]})["last_message_preview"] or "")
    # invoice: kept for bookkeeping, contact anonymised, restricted, erasable date = 31 Dec of issue year + 6
    inv = db.invoices.find_one({"id": act["invoice_id"]})
    assert inv["buyer"]["billing_email"] is None and inv["buyer"]["name"] == inv_before["buyer"]["name"]
    assert inv["total_cents"] == inv_before["total_cents"] and inv["number"] == inv_before["number"]
    assert inv["processing_restricted"] is True and inv["retention"]["basis"] == "bookkeeping_voucher"
    assert inv["retention"]["until"] == f"{int(inv_before['issue_date'][:4]) + 6}-12-31"
    assert "Kirjanpitolaki" in inv["retention"]["basis_fi"]
    retained = {(x["collection"], x["id"]): x for x in res["retained"]}
    assert retained[("invoices", act["invoice_id"])]["until"] == inv["retention"]["until"]
    # documents: the shipment's packing list is a customs record (kept), the loose personal note is deleted with its file
    d = db.documents.find_one({"id": act["doc_shipment"]})
    assert d["retention"]["basis"] == "customs_records" and d["uploaded_by"]["name"] == ERASED_NAME
    assert db.documents.find_one({"id": act["doc_loose"]}) is None
    assert ("documents", act["doc_loose"]) in {(x["collection"], x["id"]) for x in res["erased"]}
    assert not db.email_outbox.find_one({"to": {"$regex": f"^{re.escape(cu['email'])}$", "$options": "i"}})
    assert not db.notifications.find_one({"user_id": cu["user"]["id"]})
    row = db.privacy_requests.find_one({"id": req["id"]})
    assert row["status"] == "completed" and row["email"] is None and row["email_hash"] and row["result"]["counts"]
    assert db.audit_log.find_one({"entity_id": req["id"], "action": "privacy_erasure_executed"})
    assert not db.audit_log.find_one({"data.email": cu["email"]})
    # an export for the address now finds no account
    r = ops().post("/privacy/ops/requests", json={"type": "export", "email": cu["email"], "received_via": "email",
                                                  "identity_note": "follow-up check after erasure"})
    after = client().get(r.json()["download_url"].replace("/api", "", 1) + "&format=json").json()
    assert after["subject"]["accounts"] == 0 and "users" not in after["collections"]


def test_erasure_without_account_still_erases_contact_data():
    set_autopilot(True)
    try:
        cu = customer("contact")
        act = book(cu)
        contact = "kalle.vastaanottaja@example.com"
        req = public_request("erasure", contact)
        r = client().post(f"/privacy/requests/{req['id']}/verify", json={"code": outbox_code(req["id"])})
        assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    finally:
        set_autopilot(False)
    q = mongo().quotes.find_one({"id": act["quote_id"]})
    assert q["booking"]["consignee"]["contact"] == {"name": None, "email": None, "phone": None}
    assert q["booking"]["consignee"]["name"] == "Hatanpään Varasto Oy"  # the company stays
    assert q["booking"]["shipper"]["contact"]["email"] == cu["email"]  # someone else's data is untouched


def test_legal_hold_blocks_erasure_until_released_and_autopilot_off_needs_approval():
    set_autopilot(False)
    cu = customer("hold")
    act = book(cu)
    o = ops()
    r = o.post("/compliance/legal-holds", json={"scope": "company", "ref_id": cu["company_id"],
                                                "reason": "Vahinkoasia, käräjäoikeus 2026 / court case"})
    assert r.status_code == 200, r.text
    hold = r.json()
    assert cu["client"].post("/compliance/legal-holds", json={"scope": "company", "ref_id": "x", "reason": "nope nope"}) \
        .status_code == 403
    r = cu["client"].post("/privacy/me/erasure", json={"password": CUST_PW, "confirm": "maybe"})
    assert r.status_code == 400
    r = cu["client"].post("/privacy/me/erasure", json={"password": CUST_PW, "confirm": "POISTA"})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval", r.text
    req_id, appr_id = r.json()["id"], r.json()["approval_id"]
    a = o.get(f"/approvals/{appr_id}").json()
    assert a["kind"] == "privacy_erasure" and a["status"] == "pending"
    # still nothing erased while the card waits
    assert client().post("/auth/login", json={"email": cu["email"], "password": CUST_PW}).status_code == 200
    r = o.post(f"/approvals/{appr_id}/decide", json={"decision": "approved", "note": "Henkilöllisyys ok"})
    assert r.status_code == 200, r.text
    row = mongo().privacy_requests.find_one({"id": req_id})
    assert row["status"] == "partially_held" and row["result"]["held"]
    # the company's records are frozen as they were, flagged restricted under the hold
    q = mongo().quotes.find_one({"id": act["quote_id"]})
    assert q["requested_by"]["name"] == cu["user"]["name"] and q["booking"]["shipper"]["contact"]["email"] == cu["email"]
    assert q["processing_restricted"] is True and q["retention"]["basis"] == "legal_hold"
    assert hold["id"] in q["retention"]["hold_ids"]
    # ... but the person can no longer sign in
    assert client().post("/auth/login", json={"email": cu["email"], "password": CUST_PW}).status_code == 401
    # releasing the hold runs the held erasure again
    r = o.post(f"/compliance/legal-holds/{hold['id']}/release", json={"note": "Asia ratkaistu"})
    assert r.status_code == 200 and req_id in r.json()["erasures_rerun"], r.text
    q = mongo().quotes.find_one({"id": act["quote_id"]})
    assert q["requested_by"]["name"] == ERASED_NAME and q["booking"]["shipper"]["contact"]["email"] is None
    row = mongo().privacy_requests.find_one({"id": req_id})
    assert row["status"] == "completed" and row["email"] is None
    holds = o.get("/compliance/legal-holds").json()["items"]
    assert next(h for h in holds if h["id"] == hold["id"])["released_at"]


def test_rejected_erasure_card_informs_the_subject():
    set_autopilot(False)
    cu = customer("rej")
    r = cu["client"].post("/privacy/me/erasure", json={"password": CUST_PW, "confirm": "DELETE"})
    assert r.json()["status"] == "awaiting_approval"
    o = ops()
    assert o.post(f"/approvals/{r.json()['approval_id']}/decide",
                  json={"decision": "rejected", "note": "Avoin reklamaatio, tiedot tarvitaan"}).status_code == 200
    row = mongo().privacy_requests.find_one({"id": r.json()["id"]})
    assert row["status"] == "rejected" and row["rejection_reason"]
    assert mongo().email_outbox.find_one({"to": cu["email"], "tag": "privacy_erasure_rejected"})
    assert client().post("/auth/login", json={"email": cu["email"], "password": CUST_PW}).status_code == 200


# ------------------------------------------------------------------------------------------------ retention
def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_retention_sweep_dry_run_then_apply_with_hold(request):
    db = mongo()
    today = datetime.now(timezone.utc)
    y = today.year
    cid, held_cid = f"test-ret-{uuid.uuid4().hex[:8]}", f"test-ret-held-{uuid.uuid4().hex[:8]}"
    def cleanup_retention_fixtures():
        # Surviving minimal retention records are deliberately not full billing
        # contracts. Do not expose them to later API/exception-agent suites.
        for collection in ("invoices", "quotes", "customs_declarations"):
            db[collection].delete_many({"company_id": {"$in": [cid, held_cid]}})
        db.companies.delete_many({"id": {"$in": [cid, held_cid]}})
    request.addfinalizer(cleanup_retention_fixtures)
    db.companies.insert_many([{"id": cid, "name": "Retention Test Oy", "test": True, "created_at": _iso(today)},
                              {"id": held_cid, "name": "Retention Held Oy", "test": True, "created_at": _iso(today)}])
    tag = uuid.uuid4().hex[:6]

    def inv(year, status="paid", company=cid):
        i = {"id": str(uuid.uuid4()), "kind": "invoice", "number": f"T-{tag}-{year}-{status}", "status": status,
             "company_id": company, "issue_date": f"{year}-03-15", "due_date": f"{year}-03-29", "supply_date": f"{year}-03-10",
             "created_at": f"{year}-03-15T10:00:00Z", "updated_at": f"{year}-04-01T10:00:00Z", "total_cents": 1000, "test": True}
        db.invoices.insert_one(dict(i))
        return i["id"]

    within6, beyond6, beyond10 = inv(y - 3), inv(y - 7), inv(y - 11)
    open_old = inv(y - 11, status="overdue")
    held_old = inv(y - 11, company=held_cid)
    q_old, q_new, q_booked = (str(uuid.uuid4()) for _ in range(3))
    db.quotes.insert_many([
        {"id": q_old, "company_id": cid, "status": "expired", "shipment_id": None, "source": "portal", "test": True,
         "created_at": _iso(today - timedelta(days=800)), "updated_at": _iso(today - timedelta(days=780))},
        {"id": q_new, "company_id": cid, "status": "expired", "shipment_id": None, "source": "portal", "test": True,
         "created_at": _iso(today - timedelta(days=300)), "updated_at": _iso(today - timedelta(days=290))},
        {"id": q_booked, "company_id": cid, "status": "booked", "shipment_id": "s-1", "source": "portal", "test": True,
         "created_at": _iso(today - timedelta(days=800)), "updated_at": _iso(today - timedelta(days=780))}])
    cd_new, cd_old = str(uuid.uuid4()), str(uuid.uuid4())
    db.customs_declarations.insert_many([
        {"id": cd_new, "company_id": cid, "status": "released", "created_at": f"{y - 2}-05-01T00:00:00Z",
         "released_at": f"{y - 2}-05-03T00:00:00Z", "test": True},
        {"id": cd_old, "company_id": cid, "status": "released", "created_at": f"{y - 8}-05-01T00:00:00Z",
         "released_at": f"{y - 8}-05-03T00:00:00Z", "test": True}])
    tok = f"test-ret-token-{tag}"
    db.tokens.insert_one({"token": tok, "kind": "reset_password", "user_id": "x", "used": False, "test": True,
                          "expires_at": _iso(today - timedelta(days=30)), "created_at": _iso(today - timedelta(days=30))})
    o = ops()
    hold = o.post("/compliance/legal-holds", json={"scope": "company", "ref_id": held_cid, "reason": "Tax audit 2026"}).json()
    try:
        assert client().post("/compliance/retention/run", json={"dry_run": False}).status_code == 401
        dry = o.post("/compliance/retention/run", json={"dry_run": True})
        assert dry.status_code == 200, dry.text
        d = dry.json()
        assert d["dry_run"] is True and d["per_policy"]["invoices"]["deleted"] >= 2
        assert db.invoices.find_one({"id": beyond10}) and db.quotes.find_one({"id": q_old})  # dry run deletes nothing
        run = o.post("/compliance/retention/run", json={"dry_run": False}).json()
        pp = run["per_policy"]
        assert pp["invoices"]["held"] >= 1 and pp["quotes_unconverted"]["deleted"] >= 1 and pp["tokens"]["deleted"] >= 1
        assert pp["customs_records"]["deleted"] >= 1
    finally:
        o.post(f"/compliance/legal-holds/{hold['id']}/release", json={"note": "test done"})
    assert db.invoices.find_one({"id": within6}), "an invoice inside 6 years must stay"
    assert not db.invoices.find_one({"id": beyond6}) and not db.invoices.find_one({"id": beyond10})
    assert db.invoices.find_one({"id": open_old}), "an open receivable is never deleted"
    assert db.invoices.find_one({"id": held_old}), "a legal hold blocks retention deletion"
    assert not db.quotes.find_one({"id": q_old})
    assert db.quotes.find_one({"id": q_new}) and db.quotes.find_one({"id": q_booked})
    assert db.customs_declarations.find_one({"id": cd_new}) and not db.customs_declarations.find_one({"id": cd_old})
    assert not db.tokens.find_one({"token": tok})
    row = db.retention_runs.find_one({"id": run["id"]})
    assert row["finished_at"] and row["dry_run"] is False and set(row["per_policy"]["invoices"]) >= {
        "scanned", "deleted", "anonymised", "held", "errors"}
    assert db.audit_log.find_one({"entity": "retention_run", "entity_id": run["id"], "action": "retention_sweep"})
    assert db.audit_log.find_one({"entity": "retention_policy", "entity_id": "invoices", "data.run_id": run["id"]})
    runs = o.get("/compliance/retention/runs").json()["items"]
    assert runs[0]["id"] == run["id"]
    pol = {p["key"]: p for p in o.get("/compliance/retention/policy").json()["items"]}
    assert pol["invoices"]["period"]["value"] == 6 and pol["books"]["period"]["value"] == 10
    assert pol["customs_records"]["period"]["value"] >= 3 and pol["quotes_unconverted"]["period"]["value"] == 24
    assert pol["kyc"]["period"]["value"] == 5 and "444/2017" in pol["kyc"]["basis_en"]


def test_scheduled_sweep_respects_the_kill_switch():
    """Direct call of the registered job (what core.lifecycle / core.jobs runs at 03:30)."""
    from services import retention

    db = mongo()
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": True}}, upsert=True)
    try:
        out = LOOP.run_until_complete(retention.retention_sweep({"kind": "compliance.retention_sweep"}))
        assert out["skipped"] == "autopilot_halted" and out["run_id"] is None
    finally:
        db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": False}})
    out = LOOP.run_until_complete(retention.run_sweep(dry_run=True, trigger="schedule"))
    assert out["dry_run"] is True and out["totals"]["errors"] == 0, out


def test_year_end_arithmetic():
    from services.gdpr import basis_years, year_end_plus

    assert year_end_plus("2020-05-01", 6) == "2026-12-31"
    assert year_end_plus("2020-12-31T23:00:00Z", 6) == "2026-12-31"
    s = {"record_retention_years": 2, "retention_financial_statements_years": 10}
    assert basis_years("customs_records", s) == 3  # UCC art. 51 minimum wins over a smaller setting
    assert basis_years("bookkeeping_voucher", s) == 6  # Kirjanpitolaki minimum
    assert basis_years("customs_records", {"record_retention_years": 8}) == 8


def test_subject_email_hold_keeps_that_persons_old_mail_log_through_the_sweep():
    """A legal hold on a person's e-mail address must stop the retention sweep deleting rows about that person."""
    db = mongo()
    y = datetime.now(timezone.utc).year
    tag = uuid.uuid4().hex[:8]
    held_to, free_to = f"e2e+held-{tag}@example.com", f"e2e+free-{tag}@example.com"
    rows = [{"id": str(uuid.uuid4()), "to": to, "subject": "Vanha viesti", "tag": "general", "status": "sent", "test": True,
             "created_at": f"{y - 8}-02-01T10:00:00Z"} for to in (held_to.upper(), free_to)]
    db.email_outbox.insert_many([dict(r) for r in rows])
    o = ops()
    r = o.post("/compliance/legal-holds", json={"scope": "subject_email", "ref_id": held_to,
                                                 "reason": "Tax audit covers this customer's correspondence"})
    assert r.status_code == 200, r.text
    hold = r.json()
    try:
        r = o.post("/compliance/retention/run", json={"dry_run": False})
        assert r.status_code == 200, r.text
        assert r.json()["per_policy"]["email_outbox"]["held"] >= 1
        assert db.email_outbox.find_one({"id": rows[0]["id"]}), "held person's mail log row was deleted"
        assert not db.email_outbox.find_one({"id": rows[1]["id"]}), "unheld old row should be swept"
    finally:
        o.post(f"/compliance/legal-holds/{hold['id']}/release", json={"note": "test done"})
    r = o.post("/compliance/retention/run", json={"dry_run": False})
    assert r.status_code == 200, r.text
    assert not db.email_outbox.find_one({"id": rows[0]["id"]}), "after release the row follows the normal period"


def test_dry_run_look_ahead_shows_what_a_hold_keeps_without_deleting():
    """as_of previews the sweep on a future date (dry run only): the held person's row is counted as held, the unheld
    row as deletable, and nothing is deleted. A real run with as_of is refused."""
    db = mongo()
    tag = uuid.uuid4().hex[:8]
    held_to, free_to = f"e2e+preview-held-{tag}@example.com", f"e2e+preview-free-{tag}@example.com"
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"id": str(uuid.uuid4()), "to": to, "subject": "Vahvistuskoodi", "tag": "privacy_code_export", "status": "sent",
             "test": True, "created_at": fresh} for to in (held_to, free_to)]
    db.email_outbox.insert_many([dict(r) for r in rows])
    o = ops()
    assert o.post("/compliance/retention/run", json={"dry_run": False, "as_of": "2099-01-01"}).status_code == 400
    now_run = o.post("/compliance/retention/run", json={"dry_run": True}).json()
    ahead = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    hold = o.post("/compliance/legal-holds", json={"scope": "subject_email", "ref_id": held_to,
                                                    "reason": "Dispute about this person's request"}).json()
    try:
        r = o.post("/compliance/retention/run", json={"dry_run": True, "as_of": ahead})
        assert r.status_code == 200, r.text
        run = r.json()
        st = run["per_policy"]["privacy_requests"]
        assert run["dry_run"] is True and run["as_of"].startswith(ahead)
        assert st["held"] >= 1 and st["by_collection"].get("email_outbox", 0) >= 1, st
        assert st["held"] > now_run["per_policy"]["privacy_requests"]["held"]
        assert db.email_outbox.count_documents({"id": {"$in": [r["id"] for r in rows]}}) == 2, "a dry run deleted rows"
    finally:
        o.post(f"/compliance/legal-holds/{hold['id']}/release", json={"note": "test done"})


def test_sandbox_registers_follow_the_real_register_switch(monkeypatch):
    from core import sandbox
    c = {"test_tenant": True, "business_id": "3994713-8"}
    monkeypatch.setenv("TEST_TENANT_SANDBOX", "true")
    monkeypatch.setenv("TEST_TENANT_REAL_REGISTERS", "true")
    assert sandbox.is_sandbox_company(c) and not sandbox.sandbox_registers(c)  # lookups real, submissions still guarded
    monkeypatch.setenv("TEST_TENANT_REAL_REGISTERS", "false")
    assert sandbox.sandbox_registers(c)
    assert not sandbox.sandbox_registers({"test_tenant": True, "business_id": "0109862-8"})
