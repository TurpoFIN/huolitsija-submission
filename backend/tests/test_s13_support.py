"""Shared helpers for the s13 finance tests (no tests here).

Same pattern as the V4 tests: the tests talk to the real local API over HTTP and write fixture records straight to
MongoDB in the owning slices' shapes (every fixture carries test=True). Env: S13_API (default V4_API), S13_DB / V4_DB.
"""
from __future__ import annotations

import os

os.environ.setdefault("V4_API", os.environ.get("S13_API", "http://localhost:8130/api"))
os.environ.setdefault("V4_DB", os.environ.get("S13_DB", os.environ.get("DB_NAME", "huo_s13")))

from test_v4_support import (API, customer, customer_company_id, db, iso, make_company, make_declaration,  # noqa: E402,F401
                             make_shipment, make_user, now, ops, wait_for)
