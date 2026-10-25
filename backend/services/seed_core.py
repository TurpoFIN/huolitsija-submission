"""Demo companies and logins (startup order 10). Every record carries demo=True.

Demo logins exist only when their password env var is set (DEMO_OPS_PASSWORD, DEMO_CUSTOMER_PASSWORD,
DEMO_PARTNER_PASSWORD); passwords are never stored anywhere but as bcrypt hashes. The env password is used when the user
is created; after that a password changed in the app is kept across restarts and deploys, unless DEMO_RESET_PASSWORDS=true
(default false) asks for the env passwords to be written again."""
import os

from core import config
from core.db import db
from core.lifecycle import on_startup
from core.security import hash_password
from core.util import days_from_now, new_id, now_iso

# Fictional Finnish importers/exporters. Business IDs pass the check digit and were confirmed unissued in YTJ on 2026-10-24.
DEMO_COMPANIES = [
    {"key": "kaluste", "name": "Pohjolan Kaluste Oy", "city": "Lahti", "street": "Askonkatu 9", "postal_code": "15100",
     "business_id": "3990001-4", "credit_limit_cents": 5_000_000},
    {"key": "bike", "name": "Nordic Bike Parts Oy", "city": "Tampere", "street": "Hatanpään valtatie 34", "postal_code": "33100",
     "business_id": "3990002-2", "credit_limit_cents": 2_500_000},
    {"key": "coffee", "name": "Saimaan Kahvipaahtimo Oy", "city": "Lappeenranta", "street": "Valtakatu 41", "postal_code": "53100",
     "business_id": "3990003-0", "credit_limit_cents": 1_500_000},
    {"key": "electro", "name": "Arktinen Elektroniikka Oy", "city": "Oulu", "street": "Elektroniikkatie 8", "postal_code": "90590",
     "business_id": "3990004-9", "credit_limit_cents": 8_000_000},
    {"key": "steel", "name": "Kymen Teräskauppa Oy", "city": "Kotka", "street": "Hietasenkatu 12", "postal_code": "48100",
     "business_id": "3990005-7", "credit_limit_cents": 12_000_000},
    {"key": "outdoor", "name": "Lapin Retkivaruste Oy", "city": "Rovaniemi", "street": "Teollisuustie 15", "postal_code": "96320",
     "business_id": "3990006-5", "credit_limit_cents": 2_000_000},
    {"key": "marine", "name": "Turun Venetarvike Oy", "city": "Turku", "street": "Linnankatu 54", "postal_code": "20100",
     "business_id": "3990007-3", "credit_limit_cents": 3_000_000},
    {"key": "fashion", "name": "Helsinki Design House Oy", "city": "Helsinki", "street": "Fredrikinkatu 61", "postal_code": "00100",
     "business_id": "3990008-1", "credit_limit_cents": 4_000_000},
]


def demo_company_id(key: str) -> str:
    """Stable id per demo company so other slices can reference them."""
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"huolitsija-demo-company-{key}"))


async def _ensure_user(email: str, password_env: str, **fields):
    pw = os.environ.get(password_env, "").strip()
    existing = await db.users.find_one({"email": email})
    if not pw:
        return existing
    doc = {"email": email, "password_hash": hash_password(pw), "email_verified": True, "session_version": 0,
           "demo": True, "notify_email": False, **fields}
    if existing:
        reset = {"password_hash": doc["password_hash"]} if config.env_bool("DEMO_RESET_PASSWORDS") else {}
        await db.users.update_one({"email": email}, {"$set": {**reset, **fields}})
        return await db.users.find_one({"email": email}, {"_id": 0})
    doc.update(id=new_id(), created_at=now_iso())
    await db.users.insert_one(dict(doc))
    return doc


@on_startup(order=10)
async def seed_core():
    if config.SEED_DEMO and not await db.system.find_one({"key": "seed:core:v1"}):
        from core.refs import ytunnus_to_eori, ytunnus_to_vat

        for c in DEMO_COMPANIES:
            cid = demo_company_id(c["key"])
            if await db.companies.find_one({"id": cid}):
                continue
            await db.companies.insert_one({
                "id": cid, "name": c["name"], "business_id": c["business_id"], "vat_id": ytunnus_to_vat(c["business_id"]),
                "eori": ytunnus_to_eori(c["business_id"]), "country": "FI",
                "address": {"street": c["street"], "postal_code": c["postal_code"], "city": c["city"], "country": "FI"},
                "kyc_status": "not_started", "kyc": {},
                "sanctions_status": "not_screened", "credit_limit_cents": c["credit_limit_cents"], "payment_terms_days": 14,
                "billing_email": f"laskut@{c['key']}.example.fi", "vat_registered": True, "tilitys_eligible": True,
                "representation_default": "direct",
                "poa": {"type": "direct", "scope": ["import", "export"], "valid_from": now_iso()[:10],
                        "valid_to": days_from_now(365)[:10], "document_id": None},
                "status": "active", "demo": True, "demo_key": c["key"], "created_at": now_iso(),
            })
        await db.system.update_one({"key": "seed:core:v1"}, {"$set": {"at": now_iso()}}, upsert=True)

    # Demo logins (only when their password env var is set).
    # Demo users carry the names the UI shows, so notes written with their name match (s4 D10).
    await _ensure_user(config.DEMO_OPS_EMAIL, "DEMO_OPS_PASSWORD", name="Esimerkkikäyttäjä", role="ops_admin",
                       org_type="internal", company_id=None, locale="fi")
    if config.SEED_DEMO:
        await _ensure_user("customer.demo@huolitsija.fi", "DEMO_CUSTOMER_PASSWORD", name="Asiakkaan edustaja",
                           role="customer_admin", org_type="customer", company_id=demo_company_id("kaluste"), locale="fi")
