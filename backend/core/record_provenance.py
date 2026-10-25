"""Exclude demonstration and test traffic from operational measurements.

Unlike the ops display filter, this policy also applies in background jobs and
when an operator chooses to display test tenants. Legacy child records may not
carry their company's flags, so measurement queries check that owner as well.
"""
from core.db import db

FLAGS = ("demo", "test", "test_tenant", "verification")


def non_operational(record: dict | None) -> bool:
    return bool(record and (any(record.get(k) for k in FLAGS)
                            or record.get("source") in ("seed", "sandbox", "simulation")))


async def operational_query() -> dict:
    excluded = [c["id"] async for c in db.raw.companies.find(
        {"$or": [{k: True} for k in FLAGS]}, {"id": 1})]
    return {**{k: {"$ne": True} for k in FLAGS},
            "source": {"$nin": ["seed", "sandbox", "simulation"]},
            "company_id": {"$nin": excluded}}


async def operational_record(record: dict) -> bool:
    if non_operational(record):
        return False
    if record.get("company_id"):
        company = await db.raw.companies.find_one({"id": record["company_id"]},
                                                  {k: 1 for k in FLAGS})
        if non_operational(company):
            return False
    return True
