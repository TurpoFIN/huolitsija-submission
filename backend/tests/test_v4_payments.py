"""Stripe Checkout (test mode, real Stripe API) and webhook verification against the local API."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest
import stripe

from test_v4_support import API, customer, customer_company_id, db, ops

KEY_FILE = Path("/home/user/work/secrets/stripe_api_key")
STRIPE_TEST_KEY = os.environ.get("STRIPE_API_KEY", "").strip() or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")


def card_enabled() -> bool:
    return bool(ops().get("/payments/config").json().get("card_enabled"))


def open_invoice_for_customer(amount_cents: int = 15800) -> dict:
    r = ops().post("/invoices", json={"company_id": customer_company_id(), "issue": True, "lines": [
        {"description": "Huolintapalkkio (testi)", "qty": 1, "unit_price_cents": amount_cents, "vat_code": "FI_25_5"}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_checkout_without_key_says_bank_transfer_or_creates_session():
    inv = open_invoice_for_customer()
    r = customer().post(f"/invoices/{inv['id']}/checkout")
    if not card_enabled():
        assert r.status_code == 503 and "bank transfer" in r.json()["detail"].lower()
        return
    assert r.status_code == 200, r.text


@pytest.mark.skipif(not STRIPE_TEST_KEY,
                    reason="Stripe test key not provisioned")
def test_stripe_checkout_session_in_test_mode():
    if not card_enabled():
        pytest.skip("Backend was started without STRIPE_API_KEY")
    inv = open_invoice_for_customer(23456)
    r = customer().post(f"/invoices/{inv['id']}/checkout")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["url"].startswith("https://checkout.stripe.com/") and out["session_id"].startswith("cs_test_")
    # Independent check with Stripe itself (not through our API).
    client = stripe.StripeClient(STRIPE_TEST_KEY)
    s = client.v1.checkout.sessions.retrieve(out["session_id"])
    assert s["amount_total"] == inv["total_cents"] and s["currency"] == "eur" and s["mode"] == "payment"
    assert s["metadata"]["invoice_id"] == inv["id"] and s["metadata"]["company_id"] == inv["company_id"]
    assert s["success_url"].endswith(f"/portal/invoices/{inv['id']}?paid=1&session_id={{CHECKOUT_SESSION_ID}}")
    assert s["status"] == "open" and s["payment_status"] == "unpaid"
    # Second click reuses the open session instead of creating another one.
    again = customer().post(f"/invoices/{inv['id']}/checkout").json()
    assert again["session_id"] == out["session_id"] and again["reused"] is True
    # Polling an unpaid session changes nothing.
    st = customer().get(f"/payments/status/{out['session_id']}").json()
    assert st["payment_status"] == "unpaid" and st["invoice_status"] == "open" and st["applied_now"] is False
    st2 = customer().get(f"/invoices/{inv['id']}/payment-status").json()
    assert st2["invoice_status"] == "open"
    tx = db.payment_transactions.find_one({"session_id": out["session_id"]})
    assert tx and tx["amount_cents"] == inv["total_cents"] and tx["livemode"] is False


def test_other_company_cannot_poll_session():
    tx = db.payment_transactions.find_one({"company_id": {"$ne": customer_company_id()}})
    sid = tx["session_id"] if tx else "cs_test_doesnotexist"
    assert customer().get(f"/payments/status/{sid}").status_code == 404


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(etype: str, obj: dict) -> bytes:
    return json.dumps({"id": f"evt_test_{int(time.time() * 1000)}", "object": "event", "type": etype,
                       "data": {"object": obj}}).encode()


def test_webhook_rejects_bad_signature():
    import httpx

    payload = _event("checkout.session.completed", {"id": "cs_test_forged", "object": "checkout.session",
                                                    "payment_status": "paid", "status": "complete",
                                                    "metadata": {"invoice_id": "x"}})
    r = httpx.post(f"{API}/webhooks/stripe", content=payload,
                   headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"})
    # With a secret: signature check fails. Without one: the forged session cannot be confirmed with Stripe.
    assert r.status_code == 400, r.text


@pytest.mark.skipif(not os.environ.get("STRIPE_WEBHOOK_SECRET"), reason="STRIPE_WEBHOOK_SECRET not set for this run")
def test_webhook_signature_paths_with_secret():
    import httpx

    secret = os.environ["STRIPE_WEBHOOK_SECRET"]
    unknown = _event("customer.created", {"id": "cus_test"})
    ok = httpx.post(f"{API}/webhooks/stripe", content=unknown, headers={"Stripe-Signature": _sign(unknown, secret)})
    assert ok.status_code == 200 and ok.json().get("ignored") == "customer.created"
    stale = httpx.post(f"{API}/webhooks/stripe", content=unknown,
                       headers={"Stripe-Signature": _sign(unknown, secret, ts=int(time.time()) - 3600)})
    assert stale.status_code == 400  # replay outside the 5-minute tolerance
    tampered = unknown.replace(b"cus_test", b"cus_evil")
    bad = httpx.post(f"{API}/webhooks/stripe", content=tampered, headers={"Stripe-Signature": _sign(unknown, secret)})
    assert bad.status_code == 400
    # A correctly signed completion for a session that is not ours does not touch any invoice.
    forged = _event("checkout.session.completed", {"id": "cs_test_nottracked", "object": "checkout.session",
                                                   "payment_status": "paid", "status": "complete", "metadata": {},
                                                   "amount_total": 100, "currency": "eur"})
    r = httpx.post(f"{API}/webhooks/stripe", content=forged, headers={"Stripe-Signature": _sign(forged, secret)})
    assert r.status_code == 200 and r.json().get("ok") is False
