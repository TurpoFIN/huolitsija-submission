"""s14 EDI against the running backend (real HTTP, real Mongo, real pricing and booking code, real AS2 crypto).

Env: S14_API (default http://localhost:8032/api), S14_DB (huo_s14_edi), MONGO_URL, CI_DEMO_PASSWORD / DEMO_OPS_PASSWORD.
The AS2 tests need the server started with EDI_AS2_ENABLED=true, EDI_AS2_KEY_PEM and EDI_AS2_CERT_PEM; they act as a
second AS2 station (own key pair, own HTTP receiver) and check both directions: partner → us with our signed MDN, and
our IFTMCS → partner, decrypted and signature-verified by pyas2lib on the partner side.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

from services.edi.edifact import parse

API = os.environ.get("S14_API", "http://localhost:8032/api")
DB = os.environ.get("S14_DB", "huo_s14_edi")
OPS_PW = os.environ.get("DEMO_OPS_PASSWORD") or os.environ.get("CI_DEMO_PASSWORD", "ci-pass-123456")
FIX = Path(__file__).parent / "fixtures" / "edi"


@lru_cache
def mongo():
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB]


@lru_cache
def ops() -> httpx.Client:
    try:
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"API not reachable at {API}: {exc}")
    r = httpx.post(f"{API}/auth/login", json={"email": "ops.demo@huolitsija.fi", "password": OPS_PW}, timeout=30)
    assert r.status_code == 200, r.text
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {r.json()['token']}", "X-Include-Test-Tenants": "true"},
                        timeout=180)


def business_day(n=4) -> date:
    d = date.today() + timedelta(days=n)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def company(verified=True) -> str:
    email = f"s14edi-{uuid.uuid4().hex[:10]}@example.fi"
    r = httpx.post(f"{API}/auth/signup", timeout=60, json={"email": email, "password": "Testi-salasana-2026", "name": "EDI Testaaja",
                                                           "company_name": f"S14 EDI Oy {uuid.uuid4().hex[:5]}", "locale": "en",
                                                           "accept_terms": True})
    assert r.status_code == 200, r.text
    cid = r.json()["user"]["company_id"]
    fields = {"test": True}
    if verified:
        fields.update(kyc_status="approved", sanctions_status="clear", vat_registered=True)
    mongo().companies.update_one({"id": cid}, {"$set": fields})  # KYC is owned by the KYC slice; tests set its outcome
    return cid


def partner(cid: str, **extra) -> tuple[dict, str]:
    body = {"name": "S14 test partner", "company_id": cid, "unb_id": f"S14{uuid.uuid4().hex[:8].upper()}", "transport": "none",
            "accept_terms_for_company": True, **extra}
    r = ops().post("/edi/partners", json=body)
    assert r.status_code == 200, r.text
    return r.json()["partner"], r.json()["hmac_secret"]


def iftmin(p: dict, doc_no: str | None = None, extra_gid: str = "", count_extra: int = 0) -> bytes:
    raw = (FIX / "iftmin_road_tll_tre.edi").read_text().replace("{READY}", business_day().strftime("%Y%m%d")) \
        .replace("KALUSTE1", p["unb_id"]).replace("KA-BOOK-2026-0417", doc_no or f"DOC-{uuid.uuid4().hex[:8]}")
    if extra_gid:
        raw = raw.replace("DIM+1+CMT:120:80:125'\n", "DIM+1+CMT:120:80:125'\n" + extra_gid)
        raw = raw.replace("UNT+28+1", f"UNT+{28 + count_extra}+1")
    return raw.encode()


def post_signed(path: str, p: dict, secret: str, body: bytes, **kw) -> httpx.Response:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return httpx.post(f"{API}{path}", content=body, timeout=180,
                      headers={"X-Huolitsija-Partner": p["id"], "X-Huolitsija-Signature": sig, **kw.pop("headers", {})}, **kw)


def answers(p: dict, mtype: str = "IFTMCS") -> list[dict]:
    return list(mongo().edi_messages.find({"partner_id": p["id"], "direction": "out", "type": mtype}, {"_id": 0}).sort([("created_at", 1), ("_id", 1)]))


# --------------------------------------------------------------------------------------------- HTTPS + HMAC

def test_bad_signature_is_refused():
    p, _ = partner(company())
    r = post_signed("/edi/inbound", p, "wrong-secret", iftmin(p))
    assert r.status_code == 401
    assert mongo().edi_messages.count_documents({"partner_id": p["id"]}) == 0


def test_iftmin_books_through_the_portal_path_and_answers_iftmcs():
    cid = company()
    p, secret = partner(cid)
    doc_no = f"DOC-{uuid.uuid4().hex[:8]}"
    r = post_signed("/edi/inbound", p, secret, iftmin(p, doc_no))
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["status"] == "booked", res
    s = mongo().shipments.find_one({"id": res["shipment_id"]}, {"_id": 0})
    q = mongo().quotes.find_one({"id": res["quote_id"]}, {"_id": 0})
    assert s["company_id"] == cid and s["status"] == "booked" and q["status"] == "booked" and q["source"] == "edi"
    assert s["references"]["customer_ref"] == "KA-PO-778812" and s["references"]["po_numbers"] == ["PO-4410", "PO-4411"]
    assert q["booking"]["shipper"]["name"] == "Tallinna Mööbel OÜ" and q["booking"]["pickup"]["date"] == business_day().isoformat()
    a = answers(p)
    assert len(a) == 1 and a[0]["status"] == "stored"
    m = parse(a[0]["raw"]).messages[0]
    assert m.type == "IFTMCS" and m.first("BGM").el(3) == "29" and m.first("BGM").el(2) == s["reference"]
    assert {(x.el(1, 1), x.el(1, 2)) for x in m.all("RFF")} >= {("ACW", doc_no), ("BN", s["reference"])}
    assert mongo().audit_log.find_one({"action": "edi.instruction", "data.shipment_reference": s["reference"]})
    # the same bytes again: stored once, nothing booked twice
    again = post_signed("/edi/inbound", p, secret, iftmin(p, doc_no)).json()
    assert again.get("duplicate") is True
    assert mongo().quotes.count_documents({"company_id": cid}) == 1


def test_unverified_company_gets_not_accepted_with_reason():
    p, secret = partner(company(verified=False))
    res = post_signed("/edi/inbound", p, secret, iftmin(p)).json()["results"][0]
    assert res["status"] == "rejected" and "verified" in " ".join(res["reasons"])
    m = parse(answers(p)[0]["raw"]).messages[0]
    assert m.first("BGM").el(3) == "27" and "verified" in m.first("FTX").el(4, 1)


def test_wrong_recipient_and_unknown_sender_are_rejected():
    p, secret = partner(company())
    raw = iftmin(p).replace(b"+HUOLITSIJA:ZZZ+", b"+SOMEONEELSE:ZZZ+")
    r = post_signed("/edi/inbound", p, secret, raw).json()
    assert r["status"] == "rejected" and "recipient" in r["error"]
    raw = iftmin(p).replace(p["unb_id"].encode(), b"NOTPARTNER", 1)
    r = post_signed("/edi/inbound", p, secret, raw).json()
    assert r["status"] == "rejected" and "sender" in r["error"]


def test_cancel_instruction_cancels_and_reports():
    p, secret = partner(company())
    doc_no = f"DOC-{uuid.uuid4().hex[:8]}"
    res = post_signed("/edi/inbound", p, secret, iftmin(p, doc_no)).json()["results"][0]
    assert res["status"] == "booked"
    cancel = iftmin(p, doc_no).replace(f"BGM+610+{doc_no}+9'".encode(), f"BGM+610+{doc_no}+1'".encode())
    res2 = post_signed("/edi/inbound", p, secret, cancel).json()["results"][0]
    assert res2["status"] == "cancelled" and res2["shipment_id"] == res["shipment_id"]
    assert mongo().shipments.find_one({"id": res["shipment_id"]})["status"] == "cancelled"
    assert parse(answers(p)[-1]["raw"]).messages[0].first("BGM").el(3) == "1"
    deadline = time.time() + 20
    while time.time() < deadline and not answers(p, "IFTSTA"):
        time.sleep(1)
    sta = answers(p, "IFTSTA")
    assert sta and parse(sta[-1]["raw"]).messages[0].first("STS").el(2, 1) == "64"


DG_GID = ("DGS+ADR+3+1263++3'\nFTX+AAD+++PAINT'\nCTA+HG+:Kadri Tamm'\nCOM+?+372 5550 9999:TE'\n")


def test_dangerous_goods_wait_for_approval_then_complete_by_themselves():
    p, secret = partner(company())
    doc_no = f"DOC-DG-{uuid.uuid4().hex[:6]}"
    res = post_signed("/edi/inbound", p, secret, iftmin(p, doc_no, DG_GID, 4)).json()["results"][0]
    assert res["status"] == "on_hold", res
    first = parse(answers(p)[0]["raw"]).messages[0]
    assert first.first("BGM").el(3) == "23" and "dangerous goods" in first.first("FTX").el(4, 1)
    appr = mongo().approvals.find_one({"ref_id": res["quote_id"], "status": "pending"})
    r = ops().post(f"/approvals/{appr['id']}/decide", json={"decision": "approved", "note": "ADR class 3 PG III, carrier accepts"})
    assert r.status_code == 200, r.text
    deadline = time.time() + 60
    while time.time() < deadline and len(answers(p)) < 2:
        time.sleep(1)
    second = parse(answers(p)[-1]["raw"]).messages[0]
    assert second.first("BGM").el(3) == "29", answers(p)[-1]["raw"]
    rec = mongo().edi_instructions.find_one({"partner_id": p["id"], "doc_number": doc_no})
    s = mongo().shipments.find_one({"id": rec["result"]["shipment_id"]})
    assert s["cargo"].get("dangerous_goods") or (mongo().quotes.find_one({"id": res["quote_id"]})["booking"]["cargo"]["dg"]["un_number"] == "UN1263")


def test_dcsa_booking_json():
    p, secret = partner(company())
    ref = f"BKR-{uuid.uuid4().hex[:6]}"
    body = {"carrierBookingRequestReference": ref, "modeOfTransport": "TRUCK", "expectedDepartureDate": business_day().isoformat(),
            "commodities": [{"commodityType": "Oak tables", "cargoGrossWeight": 600, "cargoGrossWeightUnit": "KGM"}],
            "shipmentLocations": [{"shipmentLocationTypeCode": "PRE", "location": {"UNLocationCode": "EETLL"}},
                                  {"shipmentLocationTypeCode": "PDE", "location": {"UNLocationCode": "FITMP"}}],
            "documentParties": [{"partyFunction": "OS", "party": {"partyName": "Tallinna Mööbel OÜ", "address": {
                "street": "Pärnu mnt", "streetNumber": "139", "city": "Tallinn", "postCode": "11317", "countryCode": "EE"}}},
                {"partyFunction": "CN", "party": {"partyName": "Testi Oy", "address": {
                    "street": "Hatanpään valtatie", "streetNumber": "24", "city": "Tampere", "postCode": "33100", "countryCode": "FI"}}}]}
    import json

    raw = json.dumps(body).encode()
    r = post_signed("/edi/dcsa/bookings", p, secret, raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["bookingStatus"] == "CONFIRMED" and out["carrierBookingRequestReference"] == ref
    s = mongo().shipments.find_one({"reference": out["carrierBookingReference"]})
    assert s and s["status"] == "booked"
    # T&T for that booking over HTTPS with the partner's HMAC (GET signs path + query)
    path = f"/api/edi/dcsa/events?carrierBookingReference={out['carrierBookingReference']}"
    sig = hmac.new(secret.encode(), path.encode(), hashlib.sha256).hexdigest()
    ev = httpx.get(API.rsplit("/api", 1)[0] + path, headers={"X-Huolitsija-Partner": p["id"], "X-Huolitsija-Signature": sig}).json()
    assert ev and ev[0]["eventType"] == "SHIPMENT" and ev[0]["shipmentEventTypeCode"] == "CONF"


# --------------------------------------------------------------------------------------------- AS2 loopback

def _keypair(cn: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30)).sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()),
            cert.public_bytes(serialization.Encoding.PEM))


@lru_cache
def as2_on() -> bool:
    return bool(ops().get("/edi/status").json().get("as2_ready"))


def _station(as2_id: str):
    """A partner AS2 station: org with its own key, our server as its partner, and an HTTP receiver thread."""
    from pyas2lib import as2

    key, cert = _keypair(as2_id)
    our_cert = httpx.get(f"{API}/edi/as2/certificate").content
    our_id = httpx.get(f"{API}/edi/as2/certificate").headers["x-as2-id"]
    org = as2.Organization(as2_name=as2_id, sign_key=key + cert, decrypt_key=key + cert)
    us = as2.Partner(as2_name=our_id, verify_cert=our_cert, encrypt_cert=our_cert, validate_certs=False, sign=True, encrypt=True,
                     enc_alg="aes_256_cbc", digest_alg="sha256", mdn_mode="SYNC", mdn_digest_alg="sha256")
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            raw = b"".join(f"{k}: {v}\r\n".encode() for k, v in self.headers.items()) + b"\r\n" + body
            msg = as2.Message()
            status, (exc, _), mdn = msg.parse(raw, find_org_cb=lambda x: org if x == as2_id else None,
                                              find_partner_cb=lambda x: us if x == our_id else None)
            received.append({"status": status, "error": str(exc) if exc else None, "payload": msg.content,
                             "signed": msg.signed, "encrypted": msg.encrypted})
            self.send_response(200)
            for k, v in (mdn.headers.items() if mdn else []):
                self.send_header(k, v)
            self.end_headers()
            if mdn:
                self.wfile.write(mdn.content)

        def log_message(self, *a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return org, us, cert, f"http://127.0.0.1:{port}/as2", received, server


def _as2_post(org, us, data: bytes, filename: str) -> tuple[httpx.Response, object]:
    from pyas2lib import as2

    msg = as2.Message(org, us)
    msg.build(data, filename=filename, content_type="application/EDIFACT", disposition_notification_to="edi@partner.example")
    r = httpx.post(f"{API}/edi/as2/inbound", content=msg.content, headers=dict(msg.headers), timeout=180)
    return r, msg


def test_as2_both_directions_signed_encrypted_with_mdn():
    if not as2_on():
        pytest.skip("server started without EDI_AS2_ENABLED / keys")
    from pyas2lib import as2

    as2_id = f"S14AS2{uuid.uuid4().hex[:6].upper()}"
    org, us, cert, url, received, server = _station(as2_id)
    try:
        p, _ = partner(company(), transport="as2", as2={"as2_id": as2_id, "url": url, "cert_pem": cert.decode()})
        doc_no = f"DOC-AS2-{uuid.uuid4().hex[:6]}"
        r, sent = _as2_post(org, us, iftmin(p, doc_no), f"IFTMIN_{doc_no}.edi")
        assert r.status_code == 200, r.text
        mdn = as2.Mdn()
        status, detail = mdn.parse(b"".join(f"{k}: {v}\r\n".encode() for k, v in r.headers.items()) + b"\r\n" + r.content,
                                   find_message_cb=lambda mid, rcpt: sent)
        assert status == "processed", (status, detail)  # our MDN, signed with our key, verified by the partner
        inbound = mongo().edi_messages.find_one({"partner_id": p["id"], "direction": "in"})
        assert inbound["transport"] == "as2" and inbound["status"] == "processed"
        out = answers(p)
        assert out and out[0]["status"] == "sent" and out[0]["delivery"]["transport"] == "as2"
        assert out[0]["delivery"]["mdn_status"] == "processed"  # the partner's signed MDN, verified by us
        got = [x for x in received if x["status"] == "processed"]
        assert got and got[0]["signed"] and got[0]["encrypted"]
        m = parse(got[0]["payload"]).messages[0]
        assert m.type == "IFTMCS" and m.first("BGM").el(3) == "29"
    finally:
        server.shutdown()


def test_as2_message_signed_with_the_wrong_key_is_not_processed():
    if not as2_on():
        pytest.skip("server started without EDI_AS2_ENABLED / keys")
    from pyas2lib import as2

    as2_id = f"S14AS2{uuid.uuid4().hex[:6].upper()}"
    org, us, cert, url, received, server = _station(as2_id)
    try:
        _, other_cert = _keypair("someone-else")
        p, _ = partner(company(), transport="as2", as2={"as2_id": as2_id, "url": url, "cert_pem": other_cert.decode()})
        r, sent = _as2_post(org, us, iftmin(p), "IFTMIN_bad.edi")
        status, detail = as2.Mdn().parse(b"".join(f"{k}: {v}\r\n".encode() for k, v in r.headers.items()) + b"\r\n" + r.content,
                                         find_message_cb=lambda mid, rcpt: sent)
        assert status != "processed", (status, detail)
        assert mongo().quotes.count_documents({"company_id": p["company_id"]}) == 0
    finally:
        server.shutdown()
