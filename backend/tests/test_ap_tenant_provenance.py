"""Local Mongo/HTTP contract: scoped AP cannot escape its company or lose test provenance.

These are isolated local records. Manual fields exercise the matching boundary;
the separate live acceptance must prove actual PDF/model extraction.
"""
import asyncio
import io
import uuid

import httpx
from fastapi import FastAPI
from reportlab.pdfgen import canvas


def test_scoped_upload_matching_and_payable_keep_company_and_test_provenance():
    async def scenario():
        from core import config
        from core.db import db
        from core.tenant_visibility import hidden_test_companies
        from routes.finance_ap import router, WRITE
        from services import carrier_invoices as ci, payables

        assert config.DB_NAME.startswith("huo_s5_ap_scope_"), "Use a dedicated local test DB"
        tag = uuid.uuid4().hex
        company = "test-company-" + tag
        other = "live-company-" + tag
        carrier = {"id": "carrier-" + tag, "name": "LOCAL TEST Carrier " + tag, "country": "FI"}
        await db.companies.raw.insert_many([
            {"id": company, "test_tenant": True}, {"id": other, "test_tenant": False}])
        await db.carriers.insert_one(dict(carrier))
        shipments = []
        for index, company_id in enumerate([company, other]):
            shipment = {"id": f"shipment-{index}-{tag}", "reference": f"HUO-2026-{index + 880000:06}",
                        "company_id": company_id}
            shipments.append(shipment)
            await db.shipments.raw.insert_one(dict(shipment))
            # Legacy records may lack test:true even when their company is a test tenant.
            await db.cost_accruals.insert_one({"id": f"accrual-{index}-{tag}", "shipment_id": shipment["id"],
                "company_id": company_id, "supplier": carrier, "status": "open", "expected_eur_cents": 82900,
                "actual_eur_cents": None, "variance_eur_cents": None, "carrier_invoice_ids": []})

        app = FastAPI()
        app.include_router(router)
        actor = {"id": "local-test-ops", "name": "Local test operator", "role": "admin"}
        app.dependency_overrides[WRITE] = lambda: actor
        def pdf(label):
            buffer = io.BytesIO()
            c = canvas.Canvas(buffer)
            c.drawString(40, 800, "TEST ONLY - DO NOT PAY - " + label)
            c.save()
            return buffer.getvalue()
        async def upload(client, label, scope):
            data = {"company_id": scope} if scope else {}
            result = await client.post("/api/finance/carrier-invoices", data=data,
                files={"file": (label + ".pdf", pdf(label), "application/pdf")})
            assert result.status_code == 200, result.text
            return result.json()
        def fields(number, refs):
            return {"supplier": {"name": carrier["name"], "country": "FI"}, "number": number,
                "invoice_date": "2026-10-25", "due_date": "2026-11-25", "currency": "EUR",
                "net_cents": 82900, "vat_cents": 0, "total_cents": 82900,
                "lines": [{"description": "TEST transport", "amount_cents": 82900,
                           "vat_rate_pct": 0, "vat_cents": 0, "shipment_ref": refs[0]}],
                "references": {"shipment_refs": refs}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            invalid = await client.post("/api/finance/carrier-invoices", data={"company_id": "absent"},
                files={"file": ("invalid.pdf", pdf("invalid"), "application/pdf")})
            assert invalid.status_code == 400
            doc = await upload(client, "TEST-scoped-" + tag, company)
            assert doc["company_id"] == company and doc["test"] is True
            same_file, _, _ = await ci.get_file(doc["file_id"])
            duplicate = await client.post("/api/finance/carrier-invoices", data={"company_id": other},
                files={"file": ("duplicate.pdf", same_file, "application/pdf")})
            assert duplicate.status_code == 400
            matched = await ci.process(doc["id"], manual_fields=fields("TEST-SCOPE-" + tag,
                [shipments[0]["reference"]]), actor=actor)
            assert matched["company_id"] == company and matched["test"] is True
            assert [m["shipment_id"] for m in matched["matches"]] == [shipments[0]["id"]]
            assert matched["matches"][0]["invoiced_eur_cents"] == 82900
            approved = await ci.approve(doc["id"], actor=actor, note="LOCAL TEST ONLY; no payment")
            payable = await db.payables.find_one({"carrier_invoice_id": doc["id"]})
            assert approved["status"] == "approved"
            assert payable["company_id"] == company and payable["test"] is True
            assert payable["amount_cents"] == 82900 and payable["payment_run_id"] is None
            token = hidden_test_companies.set([company])
            try:
                assert await db.carrier_invoices.find_one({"id": doc["id"]}) is None
                assert await db.payables.find_one({"id": payable["id"]}) is None
            finally:
                hidden_test_companies.reset(token)

            for label, refs in [("cross", [shipments[1]["reference"]]),
                                ("mixed", [s["reference"] for s in shipments])]:
                bad = await upload(client, "TEST-" + label + "-" + tag, company)
                result = await ci.process(bad["id"], manual_fields=fields("TEST-" + label + tag, refs), actor=actor)
                assert result["matches"] == [] and result["status"] == "needs_review"
                assert result["audit"]["match_info"]["problems"]
                untouched = await db.cost_accruals.find_one({"id": "accrual-1-" + tag})
                assert untouched["actual_eur_cents"] is None and untouched["carrier_invoice_ids"] == []
            legacy = await upload(client, "TEST-legacy-" + tag, None)
            inherited = await ci.process(legacy["id"], manual_fields=fields("TEST-LEGACY-" + tag,
                [shipments[0]["reference"]]), actor=actor)
            assert inherited["company_id"] == company and inherited["test"] is True
        print("Scoped HTTP intake, test-company inheritance, payable visibility, duplicate scope and cross-tenant rejection passed")
    asyncio.run(scenario())
