"""Signed Stripe webhooks (review R3): the secret is read from STRIPE_WEBHOOK_SECRET or its Emergent-safe alias,
a valid signature is accepted and a forged one is rejected. Offline: fake event store, no Stripe calls."""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import config
from services import payments

SECRET = "whsec_test_" + "a" * 24


class _Events:
    async def update_one(self, *_a, **_k):
        return type("R", (), {"upserted_id": "new"})()


def _signed(payload: bytes, secret: str = SECRET) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "STRIPE_API_KEY", "")
    monkeypatch.setattr(payments, "db", type("D", (), {"stripe_events": _Events()})())


def test_valid_signature_is_accepted(webhook):
    payload = json.dumps({"id": "evt_test_1", "type": "customer.created", "data": {"object": {}}}).encode()
    result = asyncio.run(payments.handle_webhook(payload, _signed(payload)))
    assert result["ok"] is True


def test_forged_signature_is_rejected(webhook):
    payload = json.dumps({"id": "evt_test_2", "type": "checkout.session.completed", "data": {"object": {}}}).encode()
    with pytest.raises(payments.WebhookRejected):
        asyncio.run(payments.handle_webhook(payload, _signed(payload, "whsec_wrong_" + "b" * 24)))
    with pytest.raises(payments.WebhookRejected):
        asyncio.run(payments.handle_webhook(payload, None))


def test_alias_env_name_sets_the_secret():
    env = {k: v for k, v in os.environ.items() if k != "STRIPE_WEBHOOK_SECRET"}
    env["PAYMENT_WEBHOOK_SIGNING_SECRET"] = SECRET
    out = subprocess.run([sys.executable, "-c", "from core import config; print(config.STRIPE_WEBHOOK_SECRET == %r)" % SECRET],
                         cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().endswith("True"), out.stderr
