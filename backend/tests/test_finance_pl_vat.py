"""OmaVero figures and downloadable CSV from sales + approved AP, no filing."""
import csv
import io
import uuid
import pytest
from services.vat_report import invoice_vat_groups, VatPeriodError
from test_s13_support import db, ops


def test_foreign_sales_cannot_be_silently_reported_as_euros():
    with pytest.raises(VatPeriodError, match="EUR-muunnos"):
        invoice_vat_groups({"currency": "USD", "number": "FX-MISSING",
                            "vat_breakdown": [{"vat_code": "FI_25_5", "base_cents": 10000, "vat_cents": 2550}]})


def test_month_quarter_credit_and_csv_reconcile_sales_and_purchase_vat():
    # A dedicated historic period avoids unrelated test/demo transactions.
    tag = uuid.uuid4().hex
    ids = []
    def sales(kind, base, vat, code="FI_25_5", rate=25.5, status="open"):
        ident = str(uuid.uuid4()); ids.append(ident)
        db.invoices.insert_one({"id": ident, "number": tag + ident, "kind": kind, "status": status,
             "issue_date": "2026-08-15", "supply_date": "2026-08-15", "currency": "EUR",
             "vat_breakdown": [{"vat_code": code, "kind": "standard" if rate==25.5 else "reduced",
                                "rate_pct": rate, "base_cents": base, "vat_cents": vat}]})
    before = ops().get("/finance/reports/vat", params={"period": "2026-08"}).json()["amounts"]
    before_quarter = ops().get("/finance/reports/vat", params={"period": "2026-Q3"}).json()["amounts"]
    sales("invoice", 10000, 2550)
    sales("credit_note", 2000, 510)
    sales("invoice", 10000, 1350, "FI_13_5", 13.5)
    sales("invoice", 10000, 1000, "FI_10", 10)
    sales("invoice", 10000, 2550, status="draft")
    ci_id = str(uuid.uuid4())
    db.carrier_invoices.insert_one({"id": ci_id, "number": tag, "invoice_date": "2026-08-15", "currency": "EUR",
         "status": "approved", "vat_treatment": "domestic", "net_cents": 4000, "vat_cents": 1020, "total_cents": 5020})
    try:
        rep = ops().get("/finance/reports/vat", params={"period": "2026-08"})
        assert rep.status_code == 200, rep.text
        a = rep.json()["amounts"]
        for code, delta in {"301":2040, "302":1350, "303":1000, "307":1020, "308":3370}.items():
            assert a[code] - before[code] == delta
        quarter = ops().get("/finance/reports/vat", params={"period":"2026-Q3"}).json()
        for code in a:
            assert quarter["amounts"][code] - before_quarter[code] == a[code] - before[code]
        result = ops().get("/finance/reports/vat/export", params={"period":"2026-08", "format":"csv"})
        assert result.status_code == 200 and "text/csv" in result.headers["content-type"]
        rows = list(csv.reader(io.StringIO(result.content.decode("utf-8-sig")), delimiter=";"))
        assert next(r for r in rows if r and r[0]=="308")[3] == f'{a["308"]/100:.2f}'.replace('.',',')
        assert any(r and r[0]=="307" and "carrier_invoice" in r for r in rows)
        assert ops().get("/finance/reports/vat", params={"period":"2026-13"}).status_code == 400
    finally:
        db.invoices.delete_many({"id":{"$in":ids}})
        db.carrier_invoices.delete_one({"id":ci_id})
