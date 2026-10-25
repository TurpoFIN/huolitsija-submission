"""Finance (s13) reports: landing page, P&L per shipment / lane, margin, VAT return + EU sales list, ledger + exports.

All routes are internal; reads need an internal user, writes (`POST ledger/export`) need `billing.write`.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response

from core.deps import require_internal, require_perm
from services import finance_reports as fr
from services import ledger as _ledger  # noqa: F401 - registers the finance.ledger_export job
from services import vat_report as _vat  # noqa: F401 - registers the finance.vat_period_close job

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _range(date_from: str | None, date_to: str | None, default_days: int = 90) -> tuple[date, date]:
    today = date.today()
    try:
        dt = date.fromisoformat(date_to[:10]) if date_to else today
        df = date.fromisoformat(date_from[:10]) if date_from else dt - timedelta(days=default_days)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if df > dt:
        raise HTTPException(400, "The start date must be on or before the end date")
    if (dt - df).days > 3700:
        raise HTTPException(400, "Choose a range of at most ten years")
    return df, dt


# ------------------------------------------------------------------------------------------- overview + P&L
@router.get("/overview")
async def overview(user: dict = Depends(require_internal)):
    return await fr.overview()


@router.get("/reports/pnl/shipments")
async def pnl_shipments(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                        lane: str | None = None, company_id: str | None = None, basis: str | None = None,
                        limit: int = Query(500, ge=1, le=5000), user: dict = Depends(require_internal)):
    if basis and basis not in ("actual", "partly_actual", "estimated"):
        raise HTTPException(400, "basis must be actual, partly_actual or estimated")
    df, dt = _range(date_from, date_to)
    return await fr.shipments_pnl(df, dt, lane=lane, company_id=company_id, basis=basis, limit=limit)


@router.get("/reports/pnl/shipments/{shipment_id}")
async def pnl_shipment(shipment_id: str, user: dict = Depends(require_internal)):
    r = await fr.shipment_pnl(shipment_id)
    if not r:
        raise HTTPException(404, "Shipment not found")
    return r


@router.get("/reports/pnl/lanes")
async def pnl_lanes(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                    company_id: str | None = None, user: dict = Depends(require_internal)):
    df, dt = _range(date_from, date_to)
    return await fr.lane_pnl(df, dt, company_id=company_id)


@router.get("/reports/margin")
async def margin(group: str = "customer", date_from: str | None = Query(None, alias="from"),
                 date_to: str | None = Query(None, alias="to"), company_id: str | None = None,
                 user: dict = Depends(require_internal)):
    if group not in fr.MARGIN_GROUPS:
        raise HTTPException(400, f"group must be one of: {', '.join(fr.MARGIN_GROUPS)}")
    df, dt = _range(date_from, date_to)
    return await fr.margin_report(df, dt, group, company_id=company_id)


# ------------------------------------------------------------------------------------------- VAT
def _vat_err(exc: Exception):
    raise HTTPException(400, str(exc))


@router.get("/reports/vat")
async def vat(period: str | None = None, company_id: str | None = None, user: dict = Depends(require_internal)):
    from services import vat_report
    try:
        p = period or await vat_report.current_period()
        return await vat_report.vat_report(p, company_id=company_id)
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)


@router.get("/reports/vat/stored")
async def vat_stored(user: dict = Depends(require_internal)):
    from services import vat_report
    return await vat_report.stored_reports()


@router.get("/reports/vat/export")
async def vat_export(period: str, format: str = "json", company_id: str | None = None,
                     user: dict = Depends(require_internal)):
    from services import vat_report
    if format not in ("json", "csv", "vero"):
        raise HTTPException(400, "format must be json, csv or vero")
    try:
        data, filename, ctype = await vat_report.export_vat(period, format, company_id=company_id)
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/reports/ec-sales")
async def ec_sales(period: str | None = None, format: str = "json", company_id: str | None = None,
                   user: dict = Depends(require_internal)):
    from services import vat_report
    if format not in ("json", "csv", "vero"):
        raise HTTPException(400, "format must be json, csv or vero")
    try:
        p = period or await vat_report.current_period()
        if format == "json":
            return await vat_report.ec_sales_list(p, company_id=company_id)
        data, filename, ctype = await vat_report.export_ec_sales(p, format, company_id=company_id)
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/reports/vat/file")
async def vat_file(body: dict = Body(...), user: dict = Depends(require_perm("billing.write"))):
    """Store (and in VAT_FILING_MODE=api, submit) the VAT return for a period."""
    from services import vat_report
    try:
        return await vat_report.close_period(body.get("period"), actor=user, submit=bool(body.get("submit")))
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)


# ------------------------------------------------------------------------------------------- ledger
@router.get("/ledger")
async def ledger(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                 source: str | None = None, account: str | None = None, shipment_ref: str | None = None,
                 exported: bool | None = None, company_id: str | None = None, limit: int = Query(200, ge=1, le=5000),
                 skip: int = Query(0, ge=0), user: dict = Depends(require_internal)):
    from services import ledger as lg
    return await lg.list_entries(date_from, date_to, source=source, account=account, shipment_ref=shipment_ref,
                                 exported=exported, company_id=company_id, limit=limit, skip=skip)


@router.post("/ledger/build")
async def ledger_build(body: dict = Body(...), user: dict = Depends(require_perm("billing.write"))):
    from services import ledger as lg, vat_report
    df, dt = _range(body.get("from"), body.get("to"), default_days=31)
    try:
        return await lg.build_entries(df, dt)
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)


@router.post("/ledger/export")
async def ledger_export(body: dict = Body(...), user: dict = Depends(require_perm("billing.write"))):
    from services import ledger as lg, vat_report
    fmt = body.get("format") or lg.default_format()
    if fmt not in lg.FORMATS:
        raise HTTPException(400, f"format must be one of: {', '.join(lg.FORMATS)}")
    df, dt = _range(body.get("from"), body.get("to"), default_days=31)
    try:
        return await lg.export(fmt, df, dt, actor=user, only_new=bool(body.get("only_new")))
    except vat_report.VatPeriodError as exc:
        _vat_err(exc)


@router.get("/ledger/exports")
async def ledger_exports(user: dict = Depends(require_internal)):
    from services import ledger as lg
    return await lg.list_exports()


@router.get("/ledger/exports/{export_id}/file")
async def ledger_export_file(export_id: str, user: dict = Depends(require_internal)):
    from services import ledger as lg
    got = await lg.export_file(export_id)
    if not got:
        raise HTTPException(404, "Export not found")
    data, filename, ctype = got
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
