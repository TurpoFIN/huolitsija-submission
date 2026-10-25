"""Shipper REST API v1 end to end on the real server: API keys from the portal, Bearer and X-API-Key auth, scopes,
revocation, per-key rate limits, quote -> booking -> shipment -> tracking through the API, signed webhooks delivered
to a real local HTTP receiver (signature checked here with an independent HMAC), audit rows, and the published
OpenAPI document and docs page.

Nothing is mocked: the API prices with the real rate engine and books real shipments. Like the V2 tests, the only
direct DB writes are KYC/sanctions outcomes (owned by V3) and one key's rate limit (an ops setting).

Env: S14_API (default http://localhost:8031/api), S14_DB (default huo_s14_api), MONGO_URL.
The server must run with WEBHOOKS_ALLOW_PRIVATE=true so it may call the local receiver.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from pymongo import MongoClient

API = os.environ.get("S14_API") or os.environ.get("V2_API") or "http://localhost:8031/api"
DB_NAME = os.environ.get("S14_DB") or os.environ.get("V2_DB") or "huo_s14_api"
ALL_SCOPES = ["quotes:read", "quotes:write", "bookings:write", "shipments:read", "tracking:read", "documents:write",
              "invoices:read", "webhooks:manage"]
CREATED: set[str] = set()

PALLETS = {"origin": "EETLL", "destination": "FITMP", "incoterm": "FCA", "incoterm_place": "Tallinn",
           "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2.0,
                     "value_cents": 800000, "description": "Office chairs"}}
ADDR_TLL = {"street": "Peterburi tee 46", "city": "Tallinn", "postal_code": "11415", "country": "EE"}
ADDR_TRE = {"street": "Hatanpään valtatie 24", "city": "Tampere", "postal_code": "33100", "country": "FI"}


@lru_cache
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB_NAME]


@pytest.fixture(scope="session", autouse=True)
def cleanup():
    yield
    if os.environ.get("S14_KEEP_TEST_DATA") or not CREATED:
        return
    db, cids = mongo(), list(CREATED)
    sids = [s["id"] for s in db.shipments.find({"company_id": {"$in": cids}}, {"id": 1})]
    db.tracking_events.delete_many({"shipment_id": {"$in": sids}})
    for coll in ("shipments", "quotes", "approvals", "notifications", "email_outbox", "api_keys", "webhook_endpoints",
                 "webhook_deliveries", "audit_log"):
        db[coll].delete_many({"company_id": {"$in": cids}})
    db.users.delete_many({"company_id": {"$in": cids}, "test": True})
    db.companies.delete_many({"id": {"$in": cids}, "test": True})


def ready(days: int = 3) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def api_up():
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"API not reachable at {API}: {exc}")


def signup(name: str) -> tuple[httpx.Client, dict]:
    api_up()
    email = f"s14api-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=30, json={"email": email, "password": "Testi-salasana-2026", "name": "S14 Testaaja",
                                                           "company_name": name, "locale": "fi", "accept_terms": True})
    assert r.status_code == 200, r.text
    body = r.json()
    cid = body["user"]["company_id"]
    mongo().users.update_one({"email": email}, {"$set": {"test": True}})
    mongo().companies.update_one({"id": cid}, {"$set": {"test": True, "kyc_status": "approved", "sanctions_status": "clear",
                                                        "vat_registered": True}})
    CREATED.add(cid)
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {body['token']}"}, timeout=120), body["user"]


@lru_cache
def portal() -> tuple[httpx.Client, dict]:
    return signup(f"S14 API Testi Oy {uuid.uuid4().hex[:5]}")


def new_key(scopes=ALL_SCOPES, client: httpx.Client | None = None) -> tuple[dict, str]:
    c = client or portal()[0]
    r = c.post("/portal/api-keys", json={"name": f"ERP {uuid.uuid4().hex[:4]}", "scopes": scopes})
    assert r.status_code == 200, r.text
    return r.json()["key"], r.json()["secret"]


def keyed(secret: str, header: str = "bearer") -> httpx.Client:
    h = {"Authorization": f"Bearer {secret}"} if header == "bearer" else {"X-API-Key": secret}
    return httpx.Client(base_url=f"{API}/v1", headers=h, timeout=120)


@lru_cache
def full_key() -> tuple[dict, str]:
    return new_key()


# ------------------------------------------------------------------------------------------------ local receiver
class Receiver:
    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw,
                                       "json": json.loads(raw or b"{}")})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, pred, timeout=40.0):
        end = time.time() + timeout
        while time.time() < end:
            hit = [r for r in self.requests if pred(r)]
            if hit:
                return hit[0]
            time.sleep(0.5)
        return None


def check_signature(secret: str, req: dict) -> bool:
    """Independent receiver-side check: v1 = HMAC-SHA256(secret, "<t>.<raw body>")."""
    parts = dict(p.split("=", 1) for p in req["headers"]["huolitsija-signature"].split(","))
    expected = hmac.new(secret.encode(), parts["t"].encode() + b"." + req["raw"], hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"]) and abs(time.time() - int(parts["t"])) < 300


@lru_cache
def receiver() -> Receiver:
    return Receiver()


# ------------------------------------------------------------------------------------------------ keys and auth
def test_key_is_shown_once_and_stored_hashed():
    key, secret = full_key()
    assert secret.startswith("huo_test_") and key["prefix"] in secret  # test company -> test key
    stored = mongo().api_keys.find_one({"id": key["id"]})
    assert stored["hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(stored, default=str)
    listed = portal()[0].get("/portal/api-keys").json()
    row = next(k for k in listed["keys"] if k["id"] == key["id"])
    assert "hash" not in row and "secret" not in row and row["scopes"] == ALL_SCOPES
    assert {s["scope"] for s in listed["scopes"]} == set(ALL_SCOPES)


def test_bearer_and_x_api_key_both_work_with_rate_headers():
    key, secret = full_key()
    for header in ("bearer", "x-api-key"):
        r = keyed(secret, header).get("/me")
        assert r.status_code == 200, r.text
        assert r.json()["key"]["id"] == key["id"]
        assert r.json()["company"]["id"] == portal()[1]["company_id"]
        assert int(r.headers["x-ratelimit-limit"]) >= 1 and "x-ratelimit-remaining" in r.headers and "x-ratelimit-reset" in r.headers


def test_missing_and_wrong_key_401():
    api_up()
    r = httpx.get(f"{API}/v1/me", timeout=30)
    assert r.status_code == 401 and "API key" in r.json()["detail"]
    r = keyed("huo_live_deadbeef_" + "x" * 43).get("/me")
    assert r.status_code == 401


def test_scope_is_enforced():
    _, secret = new_key(["shipments:read"])
    c = keyed(secret)
    assert c.get("/shipments").status_code == 200
    r = c.post("/quotes", json={**PALLETS, "ready_date": ready()})
    assert r.status_code == 403 and "quotes:write" in r.json()["detail"]
    assert c.get("/invoices").status_code == 403


def test_revoked_key_401():
    key, secret = new_key(["shipments:read"])
    assert keyed(secret).get("/me").status_code == 200
    r = portal()[0].delete(f"/portal/api-keys/{key['id']}")
    assert r.status_code == 200 and r.json()["key"]["revoked_at"]
    assert keyed(secret).get("/me").status_code == 401


def test_rate_limit_429_with_retry_after():
    key, secret = new_key(["shipments:read"])
    mongo().api_keys.update_one({"id": key["id"]}, {"$set": {"rate_limit_per_min": 3}})
    c = keyed(secret)
    for _attempt in range(2):  # a 60-s window boundary can fall inside the first burst; the next burst cannot span two
        codes = [c.get("/me") for _ in range(4)]
        if codes[-1].status_code == 429:
            break
    assert [r.status_code for r in codes] == [200, 200, 200, 429], [r.status_code for r in codes]
    r = codes[-1]
    assert r.headers["x-ratelimit-remaining"] == "0" and r.headers["x-ratelimit-limit"] == "3"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert "Rate limit" in r.json()["detail"]
    assert codes[0].headers["x-ratelimit-remaining"] == "2"


def test_other_company_cannot_see_shipments_or_quotes():
    other_client, _ = signup(f"S14 Toinen Oy {uuid.uuid4().hex[:5]}")
    _, other_secret = new_key(["quotes:read", "shipments:read"], client=other_client)
    _, secret = full_key()
    q = keyed(secret).post("/quotes", json={**PALLETS, "ready_date": ready()}).json()["quote"]
    assert keyed(other_secret).get(f"/quotes/{q['id']}").status_code == 404
    assert all(i["id"] != q["id"] for i in keyed(other_secret).get("/quotes").json()["items"])


# ------------------------------------------------------------------------------------------------ webhooks + flow
@lru_cache
def webhook() -> tuple[dict, str]:
    _, secret = full_key()
    r = keyed(secret).post("/webhooks", json={"url": receiver().url, "events": ["*"], "description": "pytest receiver"})
    assert r.status_code == 200, r.text
    return r.json()["endpoint"], r.json()["secret"]


def test_webhook_ping_is_signed_and_delivered():
    ep, wsecret = webhook()
    assert wsecret.startswith("whsec_") and "secret" not in ep
    _, secret = full_key()
    r = keyed(secret).post(f"/webhooks/{ep['id']}/test")
    assert r.status_code == 200, r.text
    d = r.json()["delivery"]
    assert d["status"] == "delivered" and d["response_code"] == 200 and d["attempts"] == 1
    req = receiver().wait_for(lambda x: x["headers"].get("huolitsija-delivery") == d["id"], timeout=5)
    assert req and req["json"]["type"] == "ping" and req["headers"]["huolitsija-event"] == "ping"
    assert check_signature(wsecret, req)
    tampered = {**req, "raw": req["raw"] + b" "}
    assert not check_signature(wsecret, tampered)
    listed = keyed(secret).get("/webhooks").json()
    assert any(e["id"] == ep["id"] for e in listed["items"]) and all("secret" not in e for e in listed["items"])


def test_quote_book_track_through_api_with_webhooks():
    ep, wsecret = webhook()
    key, secret = full_key()
    c = keyed(secret)
    r = c.post("/quotes", json={**PALLETS, "ready_date": ready()})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    assert q["status"] == "sent" and q["source"] == "api" and q["options"]
    for o in q["options"]:  # customer view: no buy prices or margins
        assert not {"cost_cents", "margin_cents", "margin_pct", "markup_pct"} & set(o)
        assert all("unit_cost_cents" not in ln and "cost_cents" not in ln for ln in o["lines"])
    assert c.get(f"/quotes/{q['id']}").json()["quote"]["id"] == q["id"]
    assert any(i["id"] == q["id"] for i in c.get("/quotes").json()["items"])

    r = c.post(f"/quotes/{q['id']}/book", json={
        "accept_terms": True, "customer_reference": "API-PO-1",
        "shipper": {"name": "Tallinna Mööbel OÜ", "address": ADDR_TLL}, "consignee": {"name": "S14 Testi Oy", "address": ADDR_TRE},
        "pickup": {"date": ready(4)}})
    assert r.status_code == 200, r.text
    booked = r.json()
    ref, sid = booked["shipment_reference"], booked["shipment_id"]
    assert ref and sid and booked["quote"]["status"] == "booked"
    # second booking of the same quote is refused
    again = c.post(f"/quotes/{q['id']}/book", json={"accept_terms": True, "shipper": {"name": "x", "address": ADDR_TLL},
                                                    "consignee": {"name": "y", "address": ADDR_TRE}})
    assert again.status_code == 409

    for ident in (ref, sid, "API-PO-1"):
        s = c.get(f"/shipments/{ident}")
        assert s.status_code == 200, s.text
        assert s.json()["id"] == sid and s.json()["status"] == "booked"
    ev = c.get(f"/shipments/{ref}/events")
    assert ev.status_code == 200 and any(e["code"] == "booked" for e in ev.json()["items"])
    assert any(i["id"] == sid for i in c.get("/shipments?status=active").json()["items"])

    # webhooks for the quote, the booking and the new shipment arrive signed
    for event, pick in (("quote.created", lambda d: d["quote"]["id"] == q["id"]),
                        ("quote.accepted", lambda d: d["quote"]["id"] == q["id"]),
                        ("shipment.created", lambda d: d["shipment"]["id"] == sid)):
        req = receiver().wait_for(lambda x, e=event, p=pick: x["json"].get("type") == e and p(x["json"]["data"]))
        assert req, f"no {event} webhook arrived"
        assert check_signature(wsecret, req)
        assert "cost_cents" not in json.dumps(req["json"])
    rows = list(mongo().webhook_deliveries.find({"endpoint_id": ep["id"], "status": "delivered"}))
    assert {"ping", "quote.created", "quote.accepted", "shipment.created"} <= {r["event"] for r in rows}

    # audit: the API calls are on record with the key
    acts = {a["action"] for a in mongo().audit_log.find({"actor.id": f"api_key:{key['id']}"})}
    assert {"api_quote_created", "api_quote_booked", "quote_created", "quote_booked"} <= acts
    used = mongo().api_keys.find_one({"id": key["id"]})
    assert used["last_used_at"] and used["request_count"] >= 5


def test_failed_webhook_is_retried_later_not_lost():
    _, secret = full_key()
    c = keyed(secret)
    ep = c.post("/webhooks", json={"url": "http://127.0.0.1:9/nothing-listens", "events": ["invoice.paid"]}).json()["endpoint"]
    d = c.post(f"/webhooks/{ep['id']}/test").json()["delivery"]
    assert d["status"] == "failed" and d["attempts"] == 1 and d["next_attempt_at"] and d["last_error"]
    assert c.delete(f"/webhooks/{ep['id']}").status_code == 200
    assert mongo().webhook_deliveries.find_one({"id": d["id"]})["status"] == "cancelled"


def test_webhook_rejects_bad_url_and_unknown_event():
    _, secret = full_key()
    c = keyed(secret)
    assert c.post("/webhooks", json={"url": "not a url"}).status_code == 400
    r = c.post("/webhooks", json={"url": receiver().url, "events": ["shipment.teleported"]})
    assert r.status_code == 400 and "Unknown event" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ docs
def test_openapi_document_and_docs_page():
    api_up()
    r = httpx.get(f"{API}/v1/openapi.json", timeout=30)
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.") and spec["info"]["title"] == "Huolitsija Shipper API"
    paths = spec["paths"]
    for p in ("/api/v1/me", "/api/v1/quotes", "/api/v1/quotes/{quote_id}/book", "/api/v1/shipments/{id_or_ref}",
              "/api/v1/shipments/{id_or_ref}/events", "/api/v1/documents", "/api/v1/invoices", "/api/v1/webhooks",
              "/api/v1/webhooks/{endpoint_id}/test"):
        assert p in paths, p
    assert not [p for p in paths if not p.startswith("/api/v1/")]  # only the shipper API, no internal routes
    assert {"ApiKeyBearer", "ApiKeyHeader"} <= set(spec["components"]["securitySchemes"])
    assert "example" in spec["components"]["schemas"]["QuoteRequest"]
    assert all(op.get("summary") for item in paths.values() for op in item.values())
    page = httpx.get(f"{API}/v1/docs", timeout=30)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Huolitsija Shipper API" in page.text and "/api/v1/openapi.json" in page.text
