"""Finance (s13) routes: foreign currency (customer invoice currency, ECB rates, revaluation) and collections.

All internal; write actions need billing.write. Other finance routes live in finance_ap / finance_pay / finance_reports.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.util import NO_ID
from services import collections, fx, fx_revaluation

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _raise(exc: Exception):
    status = getattr(exc, "status", None)
    if status:
        raise HTTPException(status, str(exc))
    raise exc


# ------------------------------------------------------------------------------------------------ currency
class CurrencyIn(BaseModel):
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code with an ECB reference rate, or EUR")


@router.put("/customers/{company_id}/currency")
async def set_currency(company_id: str, body: CurrencyIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        return await fx_revaluation.set_customer_currency(company_id, body.currency, user)
    except fx_revaluation.FxError as exc:
        _raise(exc)


@router.get("/currencies")
async def currencies(user: dict = Depends(require_internal)):
    return {"items": await fx_revaluation.allowed_currencies(), "base": "EUR"}


# ------------------------------------------------------------------------------------------------ FX
@router.get("/fx/rates")
async def fx_rates(user: dict = Depends(require_internal)):
    return await fx_revaluation.rates_view()


class RefreshIn(BaseModel):
    kind: str | None = Field(default="daily", description="daily | 90d | hist")


@router.post("/fx/refresh")
async def fx_refresh(body: RefreshIn | None = None, user: dict = Depends(require_perm("billing.write"))):
    kind = (body.kind if body else None) or "daily"
    if kind not in ("daily", "90d", "hist"):
        raise HTTPException(400, "kind must be daily, 90d or hist")
    try:
        out = await fx.refresh(kind)
    except Exception as exc:  # noqa: BLE001 - the ECB feed could not be read
        raise HTTPException(502, f"The ECB rates could not be fetched: {exc}")
    await audit(user, "fx_rates_refreshed", "fx_rates", out.get("latest"), out)
    return out


class RevalueIn(BaseModel):
    as_of: str = Field(description="Closing date YYYY-MM-DD (usually the last day of the month)")


@router.post("/fx/revalue")
async def fx_revalue(body: RevalueIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        as_of = date.fromisoformat(body.as_of[:10])
    except ValueError:
        raise HTTPException(400, "Give the closing date as YYYY-MM-DD")
    try:
        doc = await fx_revaluation.revalue(as_of)
    except fx.FxUnavailable as exc:
        raise HTTPException(409, str(exc))
    await audit(user, "fx_revaluation_requested", "fx_revaluation", doc["id"], {"as_of": doc["as_of"]})
    return doc


@router.get("/fx/revaluations")
async def fx_revaluations(period: str | None = None, limit: int = Query(24, ge=1, le=120),
                          user: dict = Depends(require_internal)):
    q = {"period": period} if period else {}
    items = await db.fx_revaluations.find(q, NO_ID).sort("period", -1).to_list(limit)
    return {"items": items, "total": len(items)}


# ------------------------------------------------------------------------------------------------ collections
@router.get("/collections")
async def list_collections(stage: str | None = None, company_id: str | None = Query(None, alias="company"),
                           company_id2: str | None = Query(None, alias="company_id"),
                           user: dict = Depends(require_internal)):
    if stage and stage not in collections.STAGES:
        raise HTTPException(400, f"stage must be one of {', '.join(collections.STAGES)}")
    return await collections.list_cases(stage=stage, company_id=company_id or company_id2)


@router.post("/collections/sweep")
async def collections_sweep(user: dict = Depends(require_perm("billing.write"))):
    out = await collections.sweep()
    await audit(user, "collections_sweep_run", "collections", None, {k: (len(v) if isinstance(v, list) else v)
                                                                     for k, v in out.items()})
    return out


@router.get("/collections/{case_id}")
async def get_case(case_id: str, user: dict = Depends(require_internal)):
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise HTTPException(404, "Collections case not found")
    return case


@router.get("/collections/{case_id}/file")
async def case_file(case_id: str, user: dict = Depends(require_internal)):
    from core.storage import get_file

    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    fid = ((case or {}).get("handover") or {}).get("file_id")
    if not fid:
        raise HTTPException(404, "This case has no hand-over file")
    data, name, ctype = await get_file(fid)
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{case["handover"]["filename"]}"'})



@router.get("/collections/{case_id}/package/{kind}")
async def case_package_file(case_id: str, kind: str, user: dict = Depends(require_internal)):
    """One file of the agency hand-over package: claim (CSV), statement, letter_fi, letter_en (PDF)."""
    from core.storage import get_file

    case = await db.collections_cases.raw.find_one({"id": case_id}, NO_ID)
    f = next((x for x in ((case or {}).get("handover_package") or {}).get("files", []) if x["kind"] == kind), None)
    if not f:
        raise HTTPException(404, "This case has no such hand-over file")
    data, _, _ = await get_file(f["file_id"])
    return Response(data, media_type=f["content_type"], headers={"Content-Disposition": f'attachment; filename="{f["filename"]}"'})

@router.post("/collections/{case_id}/{action}")
async def case_action(case_id: str, action: str, body: dict | None = Body(None),
                      user: dict = Depends(require_perm("billing.write"))):
    body = body or {}
    if action not in collections.ACTIONS:
        raise HTTPException(400, f"action must be one of {', '.join(collections.ACTIONS)}")
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise HTTPException(404, "Collections case not found")
    try:
        if action == "send_demand":
            out = await collections.send_demand(case_id, actor=user)
        elif action == "payment_plan":
            out = await collections.payment_plan(case_id, body.get("installments") or [], user)
        elif action == "agency":
            out = await collections.hand_over(case_id, actor=user)
        elif action == "write_off":
            out = await collections.request_write_off(case_id, user, body.get("note"))
        else:  # close
            inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID) or {}
            from services.billing import _amount_due

            if case["stage"] not in collections.OPEN_STAGES:
                raise HTTPException(409, f"The case is already {case['stage']}")
            if _amount_due(inv) > 0 and inv.get("status") not in ("void", "credited"):
                raise HTTPException(409, "The invoice is still unpaid. Record the payment, agree a payment plan or "
                                         "request a write-off instead.")
            out = await collections.close_paid(case["invoice_id"], actor=user, note=body.get("note") or "closed by ops")
    except (collections.CollectionsError, fx_revaluation.FxError) as exc:
        _raise(exc)
    return {"action": action, "result": out, "case": await db.collections_cases.find_one({"id": case_id}, NO_ID)}

