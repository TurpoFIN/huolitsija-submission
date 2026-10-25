"""Local Mongo/HTTP contract for the tenant-scoped finance triggers (rows 87-90).

Real routes, services and Mongo in a dedicated local DB; e-mail provider `log` (the outbox row records what would go
out). Proves: only the configured test tenant is accepted, every mail goes to its alias with [TEST], other companies
are untouched, dunning -> collections -> camt.054 payment closes the case, and FX revaluation is company-scoped.
"""
import asyncio
import os
import uuid
from datetime import timedelta

import httpx
from fastapi import FastAPI

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.02"><BkToCstmrDbtCdtNtfctn>
<GrpHdr><MsgId>TEST-{tag}</MsgId><CreDtTm>2026-10-25T03:00:00</CreDtTm></GrpHdr>
<Ntfctn><Id>TEST-N-{tag}</Id><CreDtTm>2026-10-25T03:00:00</CreDtTm>
<Acct><Id><IBAN>FI2112345600000785</IBAN></Id><Ccy>EUR</Ccy></Acct>
<Ntry><NtryRef>1</NtryRef><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>BOOK</Sts>
<BookgDt><Dt>2026-10-25</Dt></BookgDt><ValDt><Dt>2026-10-25</Dt></ValDt><AcctSvcrRef>TEST-ARCH-{tag}</AcctSvcrRef>
<BkTxCd/><NtryDtls><TxDtls><Refs><EndToEndId>NOTPROVIDED</EndToEndId></Refs>
<AmtDtls><TxAmt><Amt Ccy="EUR">{amount}</Amt></TxAmt></AmtDtls>
<RltdPties><Dbtr><Nm>TEST Payer</Nm></Dbtr></RltdPties>
<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Cd>SCOR</Cd></CdOrPrtry></Tp><Ref>{reference}</Ref></CdtrRefInf></Strd></RmtInf>
</TxDtls></NtryDtls></Ntry></Ntfctn></BkToCstmrDbtCdtNtfctn></Document>"""


def test_scoped_dunning_collections_camt_and_fx_stay_in_the_test_tenant():
    async def scenario():
        from core import config
        from core.db import db
        from routes import test_tenant_finance as route
        from services import billing

        assert config.DB_NAME.startswith("huo_s5_ttf_"), "Use a dedicated local test DB"
        tag = uuid.uuid4().hex[:10]
        company, other = "ttf-test-" + tag, "ttf-live-" + tag
        alias = f"marc+ttf-{tag}@example.test"
        os.environ["TEST_BOOTSTRAP_COMPANY_ID"] = company
        os.environ["TEST_BOOTSTRAP_EMAIL_ALIAS"] = alias
        await db.companies.raw.insert_many([
            {"id": company, "name": "TEST tenant " + tag, "test_tenant": True, "billing_email": f"billing-{tag}@example.test"},
            {"id": other, "name": "Live company " + tag, "test_tenant": False, "billing_email": f"live-{tag}@example.test"}])
        await db.users.insert_one({"id": "u-" + tag, "email": alias, "company_id": company, "role": "customer_admin"})
        today = billing.real_today_fi()
        ref = billing_reference = None
        invs = {}
        for cid, num in ((company, 1), (other, 2)):
            inv = {"id": f"inv-{num}-{tag}", "kind": "invoice", "number": f"TEST-{tag}-{num}", "status": "overdue",
                   "company_id": cid, "company_name": cid, "currency": "EUR", "total_cents": 12400, "paid_cents": 0,
                   "issue_date": today.isoformat(), "due_date": today.isoformat(), "reference": None, "lines": []}
            inv["reference"] = billing_reference = _fi_reference(1000 + num + int(tag[:4], 16))
            await db.raw.invoices.insert_one(dict(inv))
            invs[cid] = inv
        ref = invs[company]["reference"]

        app = FastAPI()
        app.include_router(route.router)
        from routes import finance as finance_routes
        app.include_router(finance_routes.router)
        app.dependency_overrides[finance_routes.require_internal] = lambda: actor
        actor = {"id": "local-ops", "name": "Local test operator", "role": "admin", "org_type": "internal",
                 "permissions": ["billing.write"]}
        app.dependency_overrides[route.WRITE] = lambda: actor
        app.dependency_overrides[route.require_internal] = lambda: actor
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            # Only the configured test tenant.
            assert (await c.post(f"/api/finance/test-tenant/{other}/dunning/run", json={})).status_code == 403
            assert (await c.post(f"/api/finance/test-tenant/{company}/dunning/run",
                                 json={"as_of": (today + timedelta(days=90)).isoformat()})).status_code == 400

            # Dunning: reminder 1 at +8 days, then the collection stage at +46 days.
            r = (await c.post(f"/api/finance/test-tenant/{company}/dunning/run",
                              json={"as_of": (today + timedelta(days=8)).isoformat()})).json()
            assert [d["stage"] for d in r["done"]] == ["reminder_1"] and r["simulated"] is True
            r = (await c.post(f"/api/finance/test-tenant/{company}/dunning/run",
                              json={"as_of": (today + timedelta(days=46)).isoformat()})).json()
            assert [d["stage"] for d in r["done"]] == ["collection"]
            assert billing.today_fi() == today  # the override never leaks out of the run

            # Collections: case + final demand + credit hold.
            r = (await c.post(f"/api/finance/test-tenant/{company}/collections/run",
                              json={"as_of": (today + timedelta(days=46)).isoformat(), "send_demand": True})).json()
            assert len(r["opened"]) == 1 and r["opened"][0]["reason"] == "dunning_collection"
            assert r["demands"][0]["sent"] and r["demands"][0]["credit_hold"]
            case_id = r["opened"][0]["case_id"]

            # Agency hand-over: owner card with the package (claim CSV, statement, FI + EN letters), then the
            # owner's approval hands the case to the agency in file mode (no agency contract).
            r = (await c.post(f"/api/finance/test-tenant/{company}/collections/run",
                              json={"as_of": (today + timedelta(days=46)).isoformat(), "handover": True})).json()
            assert r["handover"] and r["handover"][0]["case_id"] == case_id and r["handover"][0]["auto"] is False
            approval = await db.approvals.raw.find_one({"id": r["handover"][0]["approval_id"]}, {"_id": 0})
            assert approval and (approval["data"].get("finance_action") or approval["data"]["action"]) in ("agency", "handover_to_agency"), approval["data"]
            assert {f["kind"] for f in approval["data"]["package"]} == {"claim", "statement", "letter_fi", "letter_en"}
            for kind, magic in (("claim", b"\xef\xbb\xbfcase_id"), ("statement", b"%PDF"), ("letter_fi", b"%PDF"), ("letter_en", b"%PDF")):
                f = await c.get(f"/api/finance/collections/{case_id}/package/{kind}")
                assert f.status_code == 200 and f.content.startswith(magic), (kind, f.status_code)
            from services import collections
            done = await collections.hand_over(case_id, actor=actor, approval_id=approval["id"])
            assert done["stage"] == "agency" and done["handover"]["mode"] == "file"

            # camt.054 for another company's reference is refused; the tenant's own is imported and booked.
            bad = CAMT.format(tag="x" + tag, amount="124.00", reference=invs[other]["reference"]).encode()
            assert (await c.post(f"/api/finance/test-tenant/{company}/camt054",
                                 files={"file": ("bad.xml", bad, "application/xml")})).status_code == 400
            good = CAMT.format(tag=tag, amount="124.00", reference=ref).encode()
            r = await c.post(f"/api/finance/test-tenant/{company}/camt054", files={"file": ("t.xml", good, "application/xml")})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["booked"] and body["booked"][0]["status"] == "matched"

            # FX: company-scoped row, the all-company row untouched.
            await db.payables.insert_many([
                {"id": "p1-" + tag, "company_id": company, "currency": "USD", "amount_cents": 98500, "eur_cents": 88001,
                 "status": "approved", "supplier": {"name": "TEST"}},
                {"id": "p2-" + tag, "company_id": other, "currency": "USD", "amount_cents": 50000, "eur_cents": 40000,
                 "status": "approved", "supplier": {"name": "LIVE"}}])
            from services import fx
            async def fake_rate(cur, d):
                return {"rate": 1.1, "rate_date": "2026-10-23", "source": fx.SOURCE}
            orig, fx.eur_rate = fx.eur_rate, fake_rate
            try:
                r = (await c.post(f"/api/finance/test-tenant/{company}/fx/revalue", json={"as_of": "2026-10-31"})).json()
            finally:
                fx.eur_rate = orig
            assert r["scope"] == "company" and r["company_id"] == company
            assert [i["id"] for i in r["items"]] == ["p1-" + tag]
            assert r["period"] == f"2026-10/{company}" and r["period_month"] == "2026-10"

        inv = await db.raw.invoices.find_one({"id": invs[company]["id"]}, {"_id": 0})
        assert inv["status"] == "paid" and inv["dunning"]["simulated_as_of"]
        case = await db.collections_cases.raw.find_one({"id": case_id}, {"_id": 0})
        assert case["stage"] == "paid"
        assert (await db.companies.raw.find_one({"id": company}, {"_id": 0})).get("credit_hold") is not True
        untouched = await db.raw.invoices.find_one({"id": invs[other]["id"]}, {"_id": 0})
        assert not untouched.get("dunning") and untouched["status"] == "overdue"
        mails = await db.email_outbox.find({"company_id": {"$in": [company, other]}}, {"_id": 0}).to_list(50)
        assert mails, "the run must send mail"
        assert {m["to"] for m in mails} == {alias}
        assert all(m["subject"].startswith("[TEST]") for m in mails)
        assert {m["tag"] for m in mails} >= {"dunning_reminder_1", "collections_final_demand"}

    asyncio.run(scenario())


def _fi_reference(base: int) -> str:
    digits = str(base)
    weights = [7, 3, 1]
    s = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(digits)))
    return digits + str((10 - s % 10) % 10)
