"""s2 r30/r33: a test counterparty (haulier, warehouse) bound to one test tenant.

Created with `test_company_id` (ops only); it then carries company_id + test_tenant, so the ops views hide it like any
other test-tenant record (core.tenant_visibility), and check() refuses it for any other company's shipment. A test
carrier is always inactive, so pricing, quotes, ranking and Autopilot booking never pick it."""
from fastapi import HTTPException

from core.db import db
from core.sandbox import is_test_company


async def bind(test_company_id: str) -> dict:
    company = await db.raw.companies.find_one({"id": test_company_id}, {"_id": 0, "id": 1, "test_tenant": 1, "test": 1})
    if not is_test_company(company):
        raise HTTPException(422, "A test counterparty can only belong to a test tenant.")
    return {"company_id": company["id"], "test_tenant": True}


def check(counterparty: dict | None, shipment: dict | None, what: str):
    if (counterparty or {}).get("test_tenant") and counterparty.get("company_id") != (shipment or {}).get("company_id"):
        raise HTTPException(409, f"This test {what} works only on its own test tenant's shipments.")
