"""Procurement records (allocations, overseas agents, agent agreements, agent rate cards) owned by a test tenant.

Ops can create such a record for a test company (company.test_tenant / test) by sending company_id. The record keeps
company_id and test_tenant, and only that company's quotes and bookings ever see it: production pricing, agent
selection and carrier ranking skip it. A record without company_id is a normal production record.
"""
from fastapi import HTTPException

from core.sandbox import is_test_company


async def owner_fields(body: dict, inherited: dict | None = None) -> dict:
    """{company_id, test_tenant} for a new record, or {} for a production record. `inherited` is the parent record
    (the agent of an agreement or rate card): a child of a test record belongs to the same test company."""
    from core.db import db

    parent_cid = (inherited or {}).get("company_id") if (inherited or {}).get("test_tenant") else None
    cid = body.get("company_id") or parent_cid
    if not cid:
        return {}
    if parent_cid and cid != parent_cid:
        raise HTTPException(400, "The record belongs to the same test company as its agent")
    company = await db.raw.companies.find_one({"id": cid}, {"_id": 0, "id": 1, "test_tenant": 1, "test": 1})
    if not company:
        raise HTTPException(404, "Company not found")
    if not is_test_company(company):
        raise HTTPException(400, "company_id is only for test-tenant records; production records have no company")
    return {"company_id": cid, "test_tenant": True}


def scope_query(company_id: str | None = None) -> dict:
    """Mongo filter: production records, plus the given test company's own records."""
    if company_id:
        return {"$or": [{"test_tenant": {"$ne": True}}, {"company_id": company_id}]}
    return {"test_tenant": {"$ne": True}}


def in_scope(row: dict, company_id: str | None = None) -> bool:
    return not row.get("test_tenant") or (company_id is not None and row.get("company_id") == company_id)
