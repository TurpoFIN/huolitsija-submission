"""The customer behind an e-mail address. A sender we do not know gets a company and a customer-admin user at their
first priceable request, exactly like a portal sign-up (KYC not started, sanctions not screened, booking blocked
until verification), but without a password: they act through their mailbox and one-click links."""
from __future__ import annotations

import re

from core import config
from core.audit import audit
from core.db import db
from core.events import emit
from core.permissions import permissions_for
from core.util import NO_ID, new_id, now_iso

TEST_SENDER = re.compile(r"^(e2e[.+]|test[.+]|kuvatesti[.+])")
# Explicitly reserved for the owner's email acceptance test. A '+' alone is
# not a test marker: ordinary plus-addressed customers remain production users.
TEST_SENDER_ALIASES = frozenset({"marc+huolitsija-email@heliosone.fi"})
FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "live.com", "luukku.com",
             "suomi24.fi", "kolumbus.fi", "elisanet.fi", "protonmail.com", "gmx.com"}


def with_permissions(user: dict) -> dict:
    u = {k: v for k, v in user.items() if k != "password_hash"}
    u["permissions"] = sorted(permissions_for(u))
    return u


async def user_for_email(addr: str) -> dict | None:
    return await db.users.find_one({"email": (addr or "").lower(), "disabled": {"$ne": True}}, {"_id": 0, "password_hash": 0})


def _company_name(addr: str, parse: dict) -> str:
    name = ((parse or {}).get("sender") or {}).get("company_name")
    if name:
        return name[:200]
    domain = addr.split("@")[-1].lower()
    if domain in FREE_MAIL:
        person = ((parse or {}).get("sender") or {}).get("person_name")
        return (person or addr.split("@")[0])[:200]
    return domain.split(".")[0].replace("-", " ").title()


async def ensure_customer(addr: str, name: str | None, parse: dict, language: str) -> tuple[dict, dict, bool]:
    """-> (user with permissions, company, created_now)."""
    addr = addr.strip().lower()
    user = await user_for_email(addr)
    if user and user.get("company_id"):
        company = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
        if company:
            return with_permissions(user), company, False
    sender = (parse or {}).get("sender") or {}
    addr_d = {k: v for k, v in (sender.get("address") or {}).items() if v}
    company = {
        "id": new_id(), "name": _company_name(addr, parse), "country": addr_d.get("country") or "FI",
        "business_id": None, "vat_id": None, "eori": None, "address": addr_d,
        "kyc_status": "not_started", "sanctions_status": "not_screened",
        "test_tenant": addr in TEST_SENDER_ALIASES or bool(TEST_SENDER.match(addr)), "credit_limit_cents": 0,
        "payment_terms_days": config.DEFAULT_PAYMENT_TERMS_DAYS, "billing_email": addr,
        "created_at": now_iso(), "created_by": None, "status": "active", "channel": "email",
        "email_intake": {"business_id_stated": sender.get("business_id"), "vat_id_stated": sender.get("vat_id"),
                         "phone": sender.get("phone")},
    }
    user = {
        "id": new_id(), "email": addr, "name": (name or sender.get("person_name") or addr.split("@")[0])[:120],
        "password_hash": None, "role": "customer_admin", "org_type": "customer", "company_id": company["id"],
        "locale": "en" if language == "en" else "fi", "email_verified": True, "email_verified_via": "inbound_email",
        "session_version": 0, "created_at": now_iso(), "notify_email": False, "channel": "email",
    }
    company["created_by"] = user["id"]
    await db.companies.insert_one(dict(company))
    await db.users.insert_one(dict(user))
    await audit(user, "signup_by_email", "company", company["id"], {"email": addr}, company_id=company["id"])
    await emit("company.created", {"company": company, "actor": user})
    return with_permissions(user), company, True


async def channel_user_for_company(company_id: str) -> dict | None:
    return await db.users.find_one({"company_id": company_id, "channel": "email", "disabled": {"$ne": True}},
                                   {"_id": 0, "password_hash": 0})
