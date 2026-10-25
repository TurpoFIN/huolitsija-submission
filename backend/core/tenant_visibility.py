"""Request-scoped operational tenant visibility, including aggregate counts.

Customer and partner authorization stays in company_scope. This is an ops view filter,
never a substitute for authorization. Background work does not inherit a global preference.
"""
from contextvars import ContextVar

hidden_test_companies = ContextVar("hidden_test_companies", default=None)
TENANT_COLLECTIONS = {"trade_documents", "shipments", "quotes", "documents", "customs_declarations", "invoices", "threads", "messages",
                      "exceptions", "claims", "approvals", "ai_runs", "audit_log", "email_outbox", "tracking_events", "autonomy_actions", "partner_outreach",
                      "party_screenings", "cost_accruals", "carrier_invoices", "payables", "collections_cases",
                      "carriers", "warehouses"}  # s2: test counterparties (core/test_counterparty.py)


class VisibleCollection:
    def __init__(self, collection, name):
        self.raw, self.name = collection, name

    def query(self, query=None):
        ids = hidden_test_companies.get()
        if ids is None:
            return query or {}
        field = "id" if self.name == "companies" else "company_id"
        return {"$and": [query or {}, {field: {"$nin": ids}, "test": {"$ne": True}, "test_tenant": {"$ne": True}}]}

    def find(self, filter=None, *a, **kw):
        return self.raw.find(self.query(filter), *a, **kw)

    async def find_one(self, filter=None, *a, **kw):
        return await self.raw.find_one(self.query(filter), *a, **kw)

    async def count_documents(self, filter=None, *a, **kw):
        return await self.raw.count_documents(self.query(filter), *a, **kw)

    def aggregate(self, pipeline, *a, **kw):
        return self.raw.aggregate(([{"$match": self.query()}] if hidden_test_companies.get() is not None else [])
                                  + pipeline, *a, **kw)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class VisibleDatabase:
    def __init__(self, raw):
        self.raw = raw

    def __getitem__(self, name):
        c = self.raw[name]
        return VisibleCollection(c, name) if name in TENANT_COLLECTIONS or name == "companies" else c

    def __getattr__(self, name):
        return self[name] if not hasattr(type(self.raw), name) else getattr(self.raw, name)


# ------------------------------------------------------------------------------------------- finance figures
# Company-wide finance figures (VAT return, P&L, margin, AR, KPIs, owner reports) and every export to an outside system
# (ledger to Netvisor/Procountor, the Vero API) never include test tenants, whatever the ops toggle above says and
# whether or not a request is involved. The only exception is a figure explicitly scoped to one company_id: that
# company's own view works even when it is a test tenant.
TEST_FLAGS = ("test", "test_tenant", "demo")


def _raw_db(database=None):
    """The unfiltered database: `database` (a VisibleDatabase or a plain Motor database), default core.db.db."""
    if database is None:
        from core.db import db as database
    return database.raw if isinstance(database, VisibleDatabase) else database


async def test_company_ids(database=None) -> list[str]:
    """Ids of every test tenant (companies with test_tenant or test true). Independent of the request toggle; call it
    once per report and pass the list down."""
    rows = await _raw_db(database).companies.find({"$or": [{"test_tenant": True}, {"test": True}]}, {"_id": 0, "id": 1}).to_list(None)
    return [r["id"] for r in rows if r.get("id")]


async def test_shipment_ids(ids: list[str] | None = None, database=None) -> set[str]:
    """Ids of shipments that belong to a test tenant (or carry a test flag themselves)."""
    ids = await test_company_ids(database) if ids is None else ids
    q = {"$or": [{"company_id": {"$in": list(ids)}}, *({f: True} for f in ("test", "test_tenant"))]}
    return {s["id"] async for s in _raw_db(database).shipments.find(q, {"_id": 0, "id": 1}) if s.get("id")}


def exclude_test(query: dict | None = None, field: str = "company_id", ids=(), flags=TEST_FLAGS) -> dict:
    """`query` narrowed to records that are not test-tenant records: `field` not one of `ids` and none of the record's
    own provenance `flags` set."""
    cond: dict = {field: {"$nin": list(ids)}} if ids else {}
    for f in flags:
        cond[f] = {"$ne": True}
    return {"$and": [query, cond]} if query else cond


async def finance_scope(company_id: str | None = None, field: str = "company_id", flags=TEST_FLAGS,
                        ids: list[str] | None = None, database=None) -> dict:
    """Filter for a finance figure: the one explicitly requested company (test tenant or not), else every company
    except the test tenants."""
    if company_id:
        return {field: company_id}
    return exclude_test(None, field, await test_company_ids(database) if ids is None else ids, flags)


def is_test_record(doc: dict, ids, shipment_ids=()) -> bool:
    """A row read without a filter (carrier invoice, payable, accrual, invoice) that is test-tenant data: own flag,
    test company, or every shipment it is matched to belongs to a test tenant."""
    if any(doc.get(f) is True for f in TEST_FLAGS):
        return True
    if doc.get("company_id") and doc["company_id"] in set(ids):
        return True
    sids = {m.get("shipment_id") for m in doc.get("matches") or [] if m.get("shipment_id")}
    if doc.get("shipment_id"):
        sids.add(doc["shipment_id"])
    return bool(sids) and sids <= set(shipment_ids)
