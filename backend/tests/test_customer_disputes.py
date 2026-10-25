"""Real Mongo + ASGI contract for invoice disputes; no provider or settlement double."""
import asyncio
import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from core import audit as audit_mod, notify
from core.deps import current_user
from core.tenant_visibility import VisibleDatabase
from routes import invoices
from services import billing, collections, dunning, invoice_disputes as disputes

CUSTOMER = {"id": "demo-customer", "name": "Demo customer", "org_type": "customer", "company_id": "demo-c1",
            "permissions": ["company.book"], "demo": True}
FINANCE = {"id": "demo-finance", "name": "Demo finance", "org_type": "internal", "permissions": ["billing.write"], "demo": True}
REASON = "Demo acceptance: duplicate freight charge on line two."


def run(monkeypatch, fn):
    async def scenario():
        client = AsyncIOMotorClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=3000)
        name = "customer_disputes_test_" + uuid.uuid4().hex
        raw = client[name]
        db = VisibleDatabase(raw)
        try:
            for module in (disputes, invoices, billing, collections, dunning, audit_mod, notify):
                monkeypatch.setattr(module, "db", db)
            await raw.invoices.insert_one({"id": "inv1", "company_id": "demo-c1", "number": "DEMO-1",
                "kind": "invoice", "status": "overdue", "total_cents": 10000, "paid_cents": 0, "credited_cents": 0,
                "currency": "EUR", "due_date": "2026-01-01", "demo": True, "lines": [],
                "emails": [{"kind": "reminder"}]})
            await raw.users.insert_many([{**CUSTOMER, "role": "customer_admin"}, {**FINANCE, "role": "finance"}])
            return await fn(db)
        finally:
            await client.drop_database(name)
            client.close()
    return asyncio.run(scenario())


def test_customer_finance_thread_withdrawal_and_preserved_history(monkeypatch):
    async def scenario(db):
        d = await disputes.submit("inv1", CUSTOMER, reason=REASON, amount_cents=2500)
        assert d["status"] == "submitted" and d["amount_cents"] == 2500
        assert await db.notifications.count_documents({"user_id": FINANCE["id"]}) == 1
        d = await disputes.respond("inv1", d["id"], FINANCE, text="Demo: we are checking the original quote.")
        assert d["status"] == "in_review" and len(d["messages"]) == 2
        assert await db.notifications.count_documents({"user_id": CUSTOMER["id"]}) == 1
        d = await disputes.resolve("inv1", d["id"], CUSTOMER, outcome="withdrawn", note="Demo: the original quote explains this charge.")
        assert d["status"] == "withdrawn"
        inv = await db.invoices.find_one({"id": "inv1"})
        assert not disputes.is_open(inv) and billing._amount_due(inv) == 10000
        await disputes.submit("inv1", CUSTOMER, reason=REASON, amount_cents=1000)
        inv = await db.invoices.find_one({"id": "inv1"})
        assert inv["dispute_history"][0]["id"] == d["id"]
        assert await db.email_outbox.count_documents({}) == 0  # demo cannot send external mail
    run(monkeypatch, scenario)


def test_open_dispute_blocks_stale_and_approved_recovery_paths(monkeypatch):
    async def scenario(db):
        stale = await db.invoices.find_one({"id": "inv1"})
        await disputes.submit("inv1", CUSTOMER, reason=REASON, amount_cents=10000)
        inv = await db.invoices.find_one({"id": "inv1"})
        assert await dunning._blocked(stale) == "invoice_disputed"
        assert not await dunning.claim_stage("inv1", "reminder_1", None)
        result = await dunning.execute_stage("inv1", "reminder_1", expected_prev=None, actor=FINANCE, approval_id="prior-card")
        assert result == {"skipped": "invoice_disputed"}
        assert collections.entry_reason(inv, {}, date(2026, 10, 24)) is None
        await db.collections_cases.insert_one({"id": "case1", "invoice_id": "inv1", "stage": "final_demand"})
        assert (await collections.send_demand("case1", actor=FINANCE))["skipped"] == "invoice_disputed"
        assert (await collections.hand_over("case1", actor=FINANCE))["skipped"] == "invoice_disputed"
        assert await billing.send_invoice_email(stale, kind="reminder") == []
        assert await db.email_outbox.count_documents({}) == 0
        assert await db.invoices.count_documents({"origin": "dunning"}) == 0
    run(monkeypatch, scenario)


def test_only_one_concurrent_submission_and_resolution(monkeypatch):
    async def scenario(db):
        rows = await asyncio.gather(*(disputes.submit("inv1", CUSTOMER, reason=REASON, amount_cents=500)
                                      for _ in range(6)), return_exceptions=True)
        good = [r for r in rows if isinstance(r, dict)]
        assert len(good) == 1
        assert all(isinstance(r, HTTPException) and r.status_code == 409 for r in rows if not isinstance(r, dict))
        did = good[0]["id"]
        rows = await asyncio.gather(*(disputes.resolve("inv1", did, FINANCE, outcome="rejected", note=REASON)
                                      for _ in range(6)), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in rows) == 1
        assert await db.audit_log.count_documents({"action": "invoice_dispute_resolved"}) == 1
    run(monkeypatch, scenario)


def test_credit_resolution_requires_real_linked_issued_credit_and_amount(monkeypatch):
    async def scenario(db):
        d = await disputes.submit("inv1", CUSTOMER, reason=REASON, amount_cents=2500)
        cn = {"id": "cn1", "company_id": "demo-c1", "credit_note_for": "inv1", "number": "DEMO-CN1",
              "kind": "credit_note", "status": "open", "currency": "EUR", "total_cents": -2500, "demo": True}
        for changed in ({"status": "draft"}, {"status": "void"}, {"company_id": "demo-other"},
                        {"credit_note_for": "other"}, {"total_cents": -2499}, {"currency": "USD"}, {"number": None}):
            await db.invoices.replace_one({"id": "cn1"}, {**cn, **changed}, upsert=True)
            with pytest.raises(HTTPException) as err:
                await disputes.resolve("inv1", d["id"], FINANCE, outcome="credited", note=REASON, credit_note_id="cn1")
            assert err.value.status_code == 409
        await db.invoices.replace_one({"id": "cn1"}, cn)
        r = await disputes.resolve("inv1", d["id"], FINANCE, outcome="credited", note=REASON, credit_note_id="cn1")
        assert r["resolution"]["credit_note_number"] == "DEMO-CN1" and r["resolution"]["amount_cents"] == 2500
    run(monkeypatch, scenario)


def test_http_scope_permissions_validation_and_customer_output(monkeypatch):
    async def scenario(db):
        app = FastAPI(); app.include_router(invoices.router)
        user = dict(CUSTOMER)
        async def auth(): return user
        app.dependency_overrides[current_user] = auth
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as api:
            url = "/api/invoices/inv1/dispute"
            for amount in (0, -1, True, 1.2, "2500"):
                assert (await api.post(url, json={"reason": REASON, "amount_cents": amount})).status_code == 422
            assert (await api.post(url, json={"reason": REASON, "amount_cents": 10001})).status_code == 400
            user["company_id"] = "demo-other"
            assert (await api.post(url, json={"reason": REASON, "amount_cents": 2500})).status_code == 404
            user.update(CUSTOMER); user["permissions"] = []
            assert (await api.post(url, json={"reason": REASON, "amount_cents": 2500})).status_code == 403
            user.update(CUSTOMER)
            r = await api.post(url, json={"reason": REASON, "amount_cents": 2500}); assert r.status_code == 200
            did = r.json()["id"]
            assert (await api.post(url+f"/{did}/resolve", json={"outcome": "rejected", "note": REASON})).status_code == 403
            user.update(FINANCE)
            assert (await api.post(url+f"/{did}/resolve", json={"outcome": "withdrawn", "note": REASON})).status_code == 403
            assert (await api.post(url+f"/{did}/messages", json={"text": "Demo: reviewing the duplicate charge."})).status_code == 200
            inv = await db.invoices.find_one({"id": "inv1"})
            public = billing.public_invoice(inv, internal=False)
            assert public["dispute"]["reason"] == REASON and public["dispute"]["status"] == "in_review"
            assert public["amount_due_cents"] == 10000
    run(monkeypatch, scenario)
