"""Demo logins keep a password changed in the app across restarts (services/seed_core.py _ensure_user).

The env password (DEMO_*_PASSWORD) is used when the user is created. On later startups only the other fields are
refreshed, unless DEMO_RESET_PASSWORDS=true. Runs the real function against a throwaway Mongo database."""
import asyncio
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

from core import config
from core.security import hash_password, verify_password
from services import seed_core

EMAIL = "ops.demo@huolitsija.fi"


def _run(monkeypatch, body):
    async def run():
        client = AsyncIOMotorClient(config.MONGO_URL, tz_aware=True)
        db = client[f"huo_test_demo_pw_{uuid.uuid4().hex[:10]}"]
        monkeypatch.setattr(seed_core, "db", db)
        try:
            await body(db)
        finally:
            await client.drop_database(db.name)
            client.close()

    asyncio.run(run())


def test_new_user_gets_the_env_password(monkeypatch):
    monkeypatch.setenv("DEMO_TEST_PASSWORD", "first-env-password")
    monkeypatch.delenv("DEMO_RESET_PASSWORDS", raising=False)

    async def body(db):
        await seed_core._ensure_user(EMAIL, "DEMO_TEST_PASSWORD", name="Esimerkkikäyttäjä", role="ops_admin")
        u = await db.users.find_one({"email": EMAIL})
        assert verify_password("first-env-password", u["password_hash"])
        assert (u["name"], u["role"], u["demo"]) == ("Esimerkkikäyttäjä", "ops_admin", True)

    _run(monkeypatch, body)


def test_password_changed_in_the_app_survives_a_restart(monkeypatch):
    monkeypatch.setenv("DEMO_TEST_PASSWORD", "first-env-password")
    monkeypatch.delenv("DEMO_RESET_PASSWORDS", raising=False)

    async def body(db):
        await seed_core._ensure_user(EMAIL, "DEMO_TEST_PASSWORD", name="Old name", role="ops_admin")
        changed = hash_password("rotated-in-the-app")
        await db.users.update_one({"email": EMAIL}, {"$set": {"password_hash": changed}})
        await seed_core._ensure_user(EMAIL, "DEMO_TEST_PASSWORD", name="Esimerkkikäyttäjä", role="ops_admin")  # next startup
        u = await db.users.find_one({"email": EMAIL})
        assert u["password_hash"] == changed
        assert verify_password("rotated-in-the-app", u["password_hash"])
        assert u["name"] == "Esimerkkikäyttäjä"  # other fields are still refreshed

    _run(monkeypatch, body)


def test_reset_flag_writes_the_env_password_again(monkeypatch):
    monkeypatch.setenv("DEMO_TEST_PASSWORD", "first-env-password")
    monkeypatch.delenv("DEMO_RESET_PASSWORDS", raising=False)

    async def body(db):
        await seed_core._ensure_user(EMAIL, "DEMO_TEST_PASSWORD", name="Esimerkkikäyttäjä", role="ops_admin")
        await db.users.update_one({"email": EMAIL}, {"$set": {"password_hash": hash_password("rotated-in-the-app")}})
        monkeypatch.setenv("DEMO_RESET_PASSWORDS", "true")
        monkeypatch.setenv("DEMO_TEST_PASSWORD", "second-env-password")
        await seed_core._ensure_user(EMAIL, "DEMO_TEST_PASSWORD", name="Esimerkkikäyttäjä", role="ops_admin")
        u = await db.users.find_one({"email": EMAIL})
        assert verify_password("second-env-password", u["password_hash"])
        assert not verify_password("rotated-in-the-app", u["password_hash"])

    _run(monkeypatch, body)


def test_partner_login_follows_the_same_rule(monkeypatch):
    from services import seed_shipments

    monkeypatch.setenv("DEMO_PARTNER_PASSWORD", "first-env-password")
    monkeypatch.delenv("DEMO_RESET_PASSWORDS", raising=False)

    async def body(db):
        partner = {"id": "carrier-1", "name": "Esimerkkikuljetus"}
        await seed_shipments.ensure_partner_user(partner)
        changed = hash_password("rotated-in-the-app")
        await db.users.update_one({"email": seed_shipments.PARTNER_EMAIL}, {"$set": {"password_hash": changed}})
        await seed_shipments.ensure_partner_user(partner)  # next startup
        u = await db.users.find_one({"email": seed_shipments.PARTNER_EMAIL})
        assert u["password_hash"] == changed
        assert (u["role"], u["org_type"], u["carrier_id"], u["demo"]) == ("partner", "partner", "carrier-1", True)

    _run(monkeypatch, body)
