"""Shared helpers for the s9 (execution) tests. No tests here.

Tests talk to the real local API over HTTP (S9_API, default http://localhost:8109/api, DB S9_DB / huo_s9) and through it
to the real systems execution uses: the sandbox SMTP/IMAP server (GreenMail), the DCSA reference carrier (DCSA
Conformance Gateway, run locally) and Odoo as the WMS. Nothing is mocked. Records carry test=True / TEST- references.

Env: S9_API, S9_DB, MONGO_URL, DEMO_OPS_PASSWORD, DEMO_PARTNER_PASSWORD, DEMO_CUSTOMER_PASSWORD.
"""
from __future__ import annotations

import os

os.environ.setdefault("V1_API", os.environ.get("S9_API", "http://localhost:8109/api"))
os.environ.setdefault("V1_DB", os.environ.get("S9_DB", "huo_s9"))

from test_v1_support import (  # noqa: E402,F401
    API, cleanup_test_shipments, create_test_shipment, customer, customer_company_id, db, ops, partner, partner_carrier_id)


def set_autopilot(enabled: bool, halted: bool = False):
    """Flip the switches straight in the DB (the typed-confirmation API is s2's and is tested there)."""
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_enabled": enabled, "autopilot_halted": halted}}, upsert=True)


def work(kinds: list[str] | None = None, rounds: int = 3, fire: bool = False) -> dict:
    """Run due execution jobs inline through the API (ops only, test helper endpoint). fire=True first enqueues a fresh
    run of each listed kind (for scheduled jobs such as execution.mail_poll)."""
    r = ops().post("/execution/test/work", json={"kinds": kinds, "rounds": rounds, "fire": fire})
    assert r.status_code == 200, r.text
    return r.json()
