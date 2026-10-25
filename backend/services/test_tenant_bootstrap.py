"""One-off owner setup for the labelled test tenant, driven by deploy env so no direct database access is needed.

TEST_BOOTSTRAP_COMPANY_ID       the test tenant (must exist with test_tenant=true, otherwise nothing happens)
TEST_BOOTSTRAP_ADMIN_EMAIL      existing user of that tenant whose password is set to TEST_BOOTSTRAP_ADMIN_PASSWORD
TEST_BOOTSTRAP_EMAIL_ALIAS      inbound e-mail sender mapped to that tenant (a password-less e-mail channel user)

Idempotent; never touches a user or address that belongs to another company."""
from __future__ import annotations

import logging

from core import config
from core.audit import audit
from core.db import db
from core.lifecycle import on_startup
from core.security import hash_password, verify_password
from core.util import NO_ID, new_id, now_iso

log = logging.getLogger("test_tenant_bootstrap")


@on_startup(order=900)
async def bootstrap_test_tenant():
    company_id = config.env("TEST_BOOTSTRAP_COMPANY_ID")
    if not company_id:
        return
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company or company.get("test_tenant") is not True:
        log.warning("Test bootstrap skipped: %s is not a test tenant", company_id)
        return
    email, password = (config.env("TEST_BOOTSTRAP_ADMIN_EMAIL") or "").lower(), config.env("TEST_BOOTSTRAP_ADMIN_PASSWORD")
    if email and password:
        user = await db.users.find_one({"email": email}, NO_ID)
        if not user or user.get("company_id") != company_id:
            log.warning("Test bootstrap: admin %s is not a user of the test tenant", email)
        elif not verify_password(password, user.get("password_hash")):
            await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(password)},
                                                           "$inc": {"session_version": 1}})
            await audit(None, "test_bootstrap_password_set", "user", user["id"], {"email": email}, company_id=company_id)
            log.info("Test bootstrap: password set for %s", email)
    alias = (config.env("TEST_BOOTSTRAP_EMAIL_ALIAS") or "").strip().lower()
    if alias:
        existing = await db.users.find_one({"email": alias}, NO_ID)
        if existing and existing.get("company_id") != company_id:
            log.warning("Test bootstrap: %s already belongs to another company; not remapped", alias)
        elif not existing:
            user = {"id": new_id(), "email": alias, "name": alias.split("@")[0], "password_hash": None,
                    "role": "customer_admin", "org_type": "customer", "company_id": company_id, "locale": "fi",
                    "email_verified": True, "email_verified_via": "test_bootstrap", "session_version": 0,
                    "created_at": now_iso(), "notify_email": False, "channel": "email"}
            await db.users.insert_one(dict(user))
            await audit(None, "test_bootstrap_alias_mapped", "user", user["id"], {"email": alias}, company_id=company_id)
            log.info("Test bootstrap: %s mapped to test tenant %s", alias, company_id)
