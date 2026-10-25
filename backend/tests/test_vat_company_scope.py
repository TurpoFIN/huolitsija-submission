"""One regression across report and CSV using an isolated real Mongo database."""
import asyncio
import csv
import io
import os
import uuid

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from core.tenant_visibility import VisibleDatabase
from services import vat_report as vat


def test_vat_company_scope_covers_purchases_imports_and_csv(monkeypatch):
    async def scenario():
        client = AsyncIOMotorClient(os.environ.get("VAT_SCOPE_TEST_MONGO_URL", "mongodb://127.0.0.1:27017"),
                                   serverSelectionTimeoutMS=3000)
        name = "test_vat_scope_" + uuid.uuid4().hex
        raw = client[name]
        monkeypatch.setattr(vat, "db", VisibleDatabase(raw))

        async def settings():
            return {"seller_business_id": "1234567-1"}
        monkeypatch.setattr(vat, "get_settings", settings)
        try:
            await raw.shipments.insert_many([{"id": "sa", "company_id": "a"}, {"id": "sb", "company_id": "b"}])
            for company, multiplier in (("a", 1), ("b", 10)):
                await raw.invoices.insert_one({"id": "sale-" + company, "number": "SALE-" + company,
                    "company_id": company, "kind": "invoice", "status": "open", "currency": "EUR",
                    "supply_date": "2033-02-15", "vat_breakdown": [{"vat_code": "FI_25_5", "kind": "standard",
                    "rate_pct": 25.5, "base_cents": 10000 * multiplier, "vat_cents": 2550 * multiplier}]})
                await raw.carrier_invoices.insert_one({"id": "ap-" + company, "number": "AP-" + company,
                    "status": "approved", "invoice_date": "2033-02-15", "currency": "EUR",
                    "vat_treatment": "domestic", "net_cents": 4000 * multiplier, "vat_cents": 1020 * multiplier,
                    "total_cents": 5020 * multiplier, "matches": [{"shipment_id": "s" + company}]})
                source = {"document_id": "assessment-" + company, "basis": "test authority decision",
                          "recorded_by": "test", "recorded_at": "2033-02-15T00:00:00Z"}
                await raw.customs_declarations.insert_one({"id": "import-" + company, "company_id": company,
                    "vat_route": "tilitys", "customs_decision": {"decision_date": "2033-02-15",
                    "document_id": "decision-" + company, "importer_business_id": "1234567-1", "currency": "EUR",
                    "vat_base_cents": 20000 * multiplier, "vat_cents": 5100 * multiplier,
                    "deductible_vat_cents": 5100 * multiplier, "vat_base_source": source,
                    "deductible_vat_source": source}})
            # Unattributed AP remains in the global report, never a scoped view.
            await raw.carrier_invoices.insert_one({"id": "unassigned", "number": "UNASSIGNED",
                "status": "approved", "invoice_date": "2033-02-15", "currency": "EUR",
                "vat_treatment": "domestic", "net_cents": 1000, "vat_cents": 255, "total_cents": 1255})
            for company, multiplier in (("a", 1), ("b", 10)):
                report = await vat.vat_report("2033-02", company_id=company)
                assert {k: report["amounts"][k] for k in ("301", "304", "307", "308", "310")} == {
                    "301": 2550 * multiplier, "304": 5100 * multiplier, "307": 6120 * multiplier,
                    "308": 1530 * multiplier, "310": 20000 * multiplier}
                assert {r["declaration_id"] for r in report["import_tilitys"]} == {"import-" + company}
                assert {r["id"] for rows in report["reconciliation"].values() for r in rows} == {
                    "sale-" + company, "ap-" + company, "import-" + company}
                data, _, _ = await vat.export_vat("2033-02", "csv", company_id=company)
                rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
                assert next(r for r in rows if r and r[0] == "308")[3] == vat.money(1530 * multiplier)
                assert "AP-" + ("b" if company == "a" else "a") not in data.decode("utf-8-sig")
            global_report = await vat.vat_report("2033-02")
            assert global_report["amounts"]["307"] == 6120 * 11 + 255
            assert len(global_report["import_tilitys"]) == 2
            # Adding a second customer's match must not attribute the full invoice to both.
            await raw.carrier_invoices.update_one({"id": "ap-a"}, {"$push": {"matches": {"shipment_id": "sb"}}})
            with pytest.raises(vat.VatPeriodError, match="kohdistus"):
                await vat.vat_report("2033-02", company_id="a")
            assert (await vat.vat_report("2033-02"))["amounts"] == global_report["amounts"]
        finally:
            await client.drop_database(name)
            client.close()
    asyncio.run(scenario())
