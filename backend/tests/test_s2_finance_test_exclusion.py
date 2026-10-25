"""s2: company-wide finance figures and outside exports never include test tenants.

A real customer and a test tenant (test_tenant: true) each have a shipment and an issued, overdue invoice; the test
tenant also has a credit note with a refund (like HUO-INV-2026-000029 on live) and a journal entry that was booked
before test tenants were left out. Every company-wide figure (VAT return, P&L / lane / margin, finance overview AR tile,
insights revenue and receivables, reconciliation receivables, owner digest) and the ledger journal + export carry only
the real company, both with the ops toggle on / no request (hidden_test_companies None) and with it off (the test ids).
An explicit company_id=<test tenant> still shows that tenant's own figures.

In process against a throw-away MongoDB database (own database, dropped at the end)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_fin_test_excl_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s2_finance_test_exclusion.py in its own pytest process", allow_module_level=True)

REAL = str(uuid.uuid4())
TEST = "e19d517b-7637-49e9-8403-97fe63db7728"  # the live test tenant id s5 proves against (throw-away DB here)
S_REAL, S_TEST = str(uuid.uuid4()), str(uuid.uuid4())
INV_REAL, INV_TEST, CN_TEST = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
OLD_TEST_ENTRY = str(uuid.uuid4())
CI_TEST = str(uuid.uuid4())  # like live a039f440: the test tenant's own TEST carrier invoice, EUR 880.01 zero rated
PERIOD = "2026-09"
DF, DT = date(2026, 9, 1), date(2026, 9, 30)
NOW = datetime.now(timezone.utc).isoformat()


def _inv(iid, cid, sid, number, base, vat, *, kind="invoice", status="overdue", paid=0, **extra):
    return {"id": iid, "company_id": cid, "company_name": "Oikea Asiakas Oy" if cid == REAL else "E2E Testi Oy",
            "shipment_id": sid, "number": number, "kind": kind, "status": status, "currency": "EUR",
            "issue_date": "2026-09-10", "supply_date": "2026-09-10", "due_date": "2026-09-20", "issued_at": NOW,
            "vat_breakdown": [{"vat_code": "FI_25_5", "rate_pct": 25.5, "base_cents": base, "vat_cents": vat}],
            "subtotal_cents": base, "services_net_cents": base, "pass_through_cents": 0, "vat_cents": vat,
            "total_cents": base + vat, "paid_cents": paid, "credited_cents": 0,
            "buyer": {"name": "x", "country": "FI"}, "created_at": "2026-09-10T08:00:00+00:00", **extra}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        await ensure_indexes()
        await db.raw.companies.insert_many([
            {"id": REAL, "name": "Oikea Asiakas Oy", "business_id": "3596001-2", "country": "FI", "status": "active"},
            {"id": TEST, "name": "E2E Testi Oy", "business_id": "3994713-8", "country": "FI", "status": "active",
             "test_tenant": True}])
        for sid, cid, ref in ((S_REAL, REAL, "HUO-S2-REAL"), (S_TEST, TEST, "HUO-S2-TEST")):
            await db.raw.shipments.insert_one({
                "id": sid, "reference": ref, "company_id": cid, "customer": cid, "status": "delivered", "mode": "road",
                "origin": {"country": "FI"}, "destination": {"country": "DE"}, "carrier": "Kuljetus Oy",
                "created_at": "2026-09-05T08:00:00+00:00", "delivered_at": "2026-09-09T08:00:00+00:00",
                "price": {"total_cents": 10000}})
        await db.raw.invoices.insert_many([
            _inv(INV_REAL, REAL, S_REAL, "HUO-INV-2026-S2R1", 10000, 2550, paid=5000,
                 payments=[{"key": "p1", "amount_cents": 5000, "paid_at": "2026-09-15T09:00:00+00:00", "method": "bank_transfer"}]),
            _inv(INV_TEST, TEST, S_TEST, "HUO-INV-2026-S2T1", 50000, 12750, paid=1000,
                 payments=[{"key": "p1", "amount_cents": 1000, "paid_at": "2026-09-15T09:00:00+00:00", "method": "bank_transfer"}]),
            _inv(CN_TEST, TEST, S_TEST, "HUO-INV-2026-S2T2", -564, -144, kind="credit_note", status="paid",  # stored negative like billing does
                 refunds=[{"key": "r1", "amount_cents": 708, "at": "2026-09-12T09:00:00+00:00"}]),
        ])
        await db.raw.carrier_invoices.insert_one({
            "id": CI_TEST, "number": "S2T-CI-1", "status": "approved", "invoice_date": "2026-09-14", "currency": "EUR",
            "vat_treatment": "zero_rated", "net_cents": 88001, "vat_cents": 0, "total_cents": 88001, "test": True,
            "supplier": {"name": "Testi Rahti Oy", "country": "FI"},
            "matches": [{"shipment_id": S_TEST, "invoiced_eur_cents": 88001}]})
        # booked before test tenants were left out of the journal (the live case): must never be exported
        await db.raw.ledger_entries.insert_one({
            "id": OLD_TEST_ENTRY, "date": "2026-09-12", "period": "2026-09", "source": "payment", "source_id": CN_TEST,
            "kind": "refund:old", "company_id": TEST, "description": "Palautus HUO-INV-2026-S2T2", "total_cents": 708,
            "lines": [{"account": "1700", "account_key": "receivables", "debit_cents": 708, "credit_cents": 0},
                      {"account": "1910", "account_key": "bank", "debit_cents": 0, "credit_cents": 708}],
            "export_batch_id": None, "exported_at": None, "created_at": NOW})
    run(go())


def teardown_module(_m):
    from core.db import client
    run(client.drop_database(os.environ["DB_NAME"]))


@pytest.fixture(params=["toggle_on_or_no_request", "toggle_off"])
def visibility(request):
    """hidden_test_companies as the request left it: None (ops toggle ticked, or a scheduled job / export with no
    request) and the list of test ids (toggle not ticked)."""
    from core.tenant_visibility import hidden_test_companies
    token = hidden_test_companies.set(None if request.param == "toggle_on_or_no_request" else [TEST])
    yield request.param
    hidden_test_companies.reset(token)


def _vat_invoice_ids(rep):
    return {s["id"] for srcs in rep["reconciliation"].values() for s in srcs if s.get("type") == "invoice"}


def _vat_purchase_ids(rep):
    return ({s["id"] for srcs in rep["reconciliation"].values() for s in srcs if s.get("type") == "carrier_invoice"}
            | {x["id"] for x in rep["purchases_not_reported"]})


def test_vat_return_excludes_test_tenant(visibility):
    from services import vat_report
    rep = run(vat_report.vat_report(PERIOD))
    assert _vat_invoice_ids(rep) == {INV_REAL}
    assert _vat_purchase_ids(rep) == set()  # the test tenant's carrier invoice is not a company purchase
    assert rep["tax_payable_cents"] == 2550
    assert rep["amounts"][vat_report.tax_field_for_rate(25.5)] == 2550
    data, _name, _ctype = run(vat_report.export_vat(PERIOD, "csv"))  # the file handed to the tax office
    assert b"S2R1" in data and b"S2T" not in data


def test_vat_explicit_test_tenant_scope_shows_its_own(visibility):
    from services import vat_report
    rep = run(vat_report.vat_report(PERIOD, company_id=TEST))
    assert _vat_invoice_ids(rep) == {INV_TEST, CN_TEST}
    assert _vat_purchase_ids(rep) == {CI_TEST}  # its own TEST purchase stays in its own view
    assert rep["tax_payable_cents"] == 12750 - 144


def test_pnl_lane_margin_exclude_test_tenant(visibility):
    from services import finance_reports as fr
    pnl = run(fr.shipments_pnl(DF, DT))
    assert {r["shipment"]["id"] for r in pnl["items"]} == {S_REAL}
    assert pnl["totals"]["revenue_cents"] == 10000
    lanes = run(fr.lane_pnl(DF, DT))
    assert lanes["totals"]["revenue_cents"] == 10000 and sum(i["revenue_cents"] for i in lanes["items"]) == 10000
    margin = run(fr.margin_report(DF, DT, "customer"))
    assert {i["key"] for i in margin["items"]} == {REAL}


def test_pnl_explicit_test_tenant_scope_shows_its_own(visibility):
    from services import finance_reports as fr
    pnl = run(fr.shipments_pnl(DF, DT, company_id=TEST))
    assert {r["shipment"]["id"] for r in pnl["items"]} == {S_TEST}
    assert pnl["totals"]["revenue_cents"] == 50000 - 564
    margin = run(fr.margin_report(DF, DT, "customer", company_id=TEST))
    assert {i["key"] for i in margin["items"]} == {TEST}


def test_ar_figures_exclude_test_tenant(visibility):
    from services import finance_reports as fr, insights, reconciliation
    from services.monitoring import build_digest
    ov = run(fr.overview())
    assert ov["tiles"]["ar_overdue"]["count"] == 1 and ov["tiles"]["ar_overdue"]["eur_cents"] == 12550 - 5000
    k = insights.summarize(run(insights.load(DF, DT)), DF, DT)
    assert k["revenue_cents"] == 10000
    assert k["receivables_cents"] == 12550 - 5000 and k["open_invoices"] == 1
    t = run(reconciliation.totals(date(2026, 9, 15)))
    assert t["open_cents"] == 12550 - 5000 and t["open_count"] == 1
    assert t["received_yesterday_cents"] == 5000
    d = run(build_digest())
    assert d["invoices"]["issued"] == 1 and d["invoices"]["issued_cents"] == 12550
    assert d["invoices"]["overdue_cents"] == 12550 - 5000


def test_ar_explicit_test_tenant_scope_shows_its_own(visibility):
    from services import insights
    k = insights.summarize(run(insights.load(DF, DT, TEST)), DF, DT)
    assert k["revenue_cents"] == 50000 - 564
    assert k["receivables_cents"] == 62750 - 1000


def test_ledger_journal_and_export_exclude_test_tenant(visibility):
    from core.db import db
    from services import ledger
    built = run(ledger.build_entries(DF, DT))
    assert not built["rejected"]
    booked = run(db.raw.ledger_entries.find({}, {"_id": 0}).to_list(None))
    assert {e["company_id"] for e in booked if e["id"] != OLD_TEST_ENTRY} == {REAL}
    assert {e["source_id"] for e in booked if e["id"] != OLD_TEST_ENTRY} == {INV_REAL}
    listed = run(ledger.list_entries(DF, DT))
    assert {e["company_id"] for e in listed["items"]} == {REAL}
    assert listed["trial_balance"]["debit_cents"] == 12550 + 5000
    row = run(ledger.export("csv", DF, DT))
    assert row["status"] == "ready" and row["count"] == len(listed["items"])
    exported = run(db.raw.ledger_entries.find({"exported_at": {"$nin": [None, ""]}}, {"_id": 0}).to_list(None))
    assert exported and {e["company_id"] for e in exported} == {REAL}
    old = run(db.raw.ledger_entries.find_one({"id": OLD_TEST_ENTRY}, {"_id": 0}))
    assert old["exported_at"] is None and old["export_batch_id"] is None
    data, _filename, _ctype = run(ledger.export_file(row["id"]))  # the file sent to the accounting system
    assert b"S2R1" in data and b"S2T" not in data


def test_ledger_explicit_test_tenant_scope_lists_its_own(visibility):
    from services import ledger
    listed = run(ledger.list_entries(DF, DT, company_id=TEST))
    assert {e["id"] for e in listed["items"]} == {OLD_TEST_ENTRY}


# --------------------------------------------------------------------------------------------- front door (API)
OWNER = {"id": "owner-fi", "email": "omistaja@huolitsija.example", "name": "Omistaja", "role": "ops_admin",
         "org_type": "internal", "locale": "fi", "session_version": 0, "permissions": ["billing.write"]}


@pytest.fixture(params=["toggle_on", "toggle_off"])
def api(request):
    """The real routes and the real core.deps.current_user (which sets the ops toggle from the request); only the
    signed-in user lookup is replaced. toggle_on = the "Näytä testiyritykset" header."""
    import httpx
    from fastapi import FastAPI

    from core.deps import get_optional_user
    from routes import finance_reports
    app = FastAPI()
    app.include_router(finance_reports.router)

    async def signed_in():
        return dict(OWNER)
    app.dependency_overrides[get_optional_user] = signed_in
    headers = {"x-include-test-tenants": "true"} if request.param == "toggle_on" else {}

    def get(path, **params):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers) as c:
                return await c.get(path, params=params)
        r = run(go())
        assert r.status_code == 200, r.text
        return r
    return get


def test_api_vat_report_explicit_test_tenant_and_company_wide(api):
    own = api("/api/finance/reports/vat", period=PERIOD, company_id=TEST).json()
    assert _vat_invoice_ids(own) == {INV_TEST, CN_TEST} and own["tax_payable_cents"] == 12750 - 144
    assert _vat_purchase_ids(own) == {CI_TEST}
    wide = api("/api/finance/reports/vat", period=PERIOD).json()
    assert _vat_invoice_ids(wide) == {INV_REAL} and wide["tax_payable_cents"] == 2550
    assert _vat_purchase_ids(wide) == set()


def test_api_vat_export_explicit_test_tenant_and_company_wide(api):
    own = api("/api/finance/reports/vat/export", period=PERIOD, format="csv", company_id=TEST).content
    assert b"S2T1" in own and b"S2T2" in own and b"S2R1" not in own
    own_json = api("/api/finance/reports/vat/export", period=PERIOD, format="json", company_id=TEST).content
    assert CI_TEST.encode() in own_json and b"S2T-CI-1" in own_json
    wide = api("/api/finance/reports/vat/export", period=PERIOD, format="csv").content
    assert b"S2R1" in wide and b"S2T" not in wide
    wide_json = api("/api/finance/reports/vat/export", period=PERIOD, format="json").content
    assert CI_TEST.encode() not in wide_json


def test_api_pnl_explicit_test_tenant_and_company_wide(api):
    rng = {"from": DF.isoformat(), "to": DT.isoformat()}
    own = api("/api/finance/reports/pnl/shipments", company_id=TEST, **rng).json()
    assert {r["shipment"]["id"] for r in own["items"]} == {S_TEST} and own["totals"]["revenue_cents"] == 50000 - 564
    wide = api("/api/finance/reports/pnl/shipments", **rng).json()
    assert {r["shipment"]["id"] for r in wide["items"]} == {S_REAL} and wide["totals"]["revenue_cents"] == 10000


def test_api_margin_explicit_test_tenant_and_company_wide(api):
    rng = {"from": DF.isoformat(), "to": DT.isoformat()}
    own = api("/api/finance/reports/margin", group="customer", company_id=TEST, **rng).json()
    assert {i["key"] for i in own["items"]} == {TEST} and own["totals"]["revenue_cents"] == 50000 - 564
    wide = api("/api/finance/reports/margin", group="customer", **rng).json()
    assert {i["key"] for i in wide["items"]} == {REAL} and wide["totals"]["revenue_cents"] == 10000
