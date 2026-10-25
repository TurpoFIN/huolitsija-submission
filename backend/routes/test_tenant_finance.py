"""Tenant-scoped finance triggers for the labelled test tenant: dunning, collections, camt.054 import, FX revaluation.

Internal users with billing.write only; services.test_tenant_finance refuses every company except the configured
test tenant. The global schedules are not touched.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from core.deps import require_internal, require_perm
from core.tenant_visibility import hidden_test_companies
from services import test_tenant_finance as ttf

router = APIRouter(prefix="/api/finance/test-tenant", tags=["finance"])
WRITE = require_perm("billing.write")
MAX_BYTES = 5 * 1024 * 1024


def _see_tenant():
    # The scoped run must see its own test tenant even when the ops view hides test tenants.
    hidden_test_companies.set(None)


async def _call(coro):
    try:
        return await coro
    except ttf.ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - domain errors carry their own status
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            raise HTTPException(status, str(exc)) from None
        raise


@router.get("/{company_id}")
async def scope_status(company_id: str, user: dict = Depends(require_internal)):
    _see_tenant()
    g = await _call(ttf.guard(company_id))
    return {"company_id": company_id, "name": g["company"].get("name"), "test_tenant": True, "alias": g["alias"]}


@router.post("/{company_id}/dunning/run")
async def dunning_run(company_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    _see_tenant()
    body = body or {}
    as_of = await _call(_as_of(body.get("as_of")))
    return await _call(ttf.run_dunning(company_id, actor=user, as_of=as_of))


@router.post("/{company_id}/collections/run")
async def collections_run(company_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    _see_tenant()
    body = body or {}
    as_of = await _call(_as_of(body.get("as_of")))
    return await _call(ttf.run_collections(company_id, actor=user, as_of=as_of, send_demand=bool(body.get("send_demand")),
                                           handover=bool(body.get("handover"))))


@router.post("/{company_id}/camt054")
async def camt054(company_id: str, file: UploadFile = File(...), book: bool = True, user: dict = Depends(WRITE)):
    _see_tenant()
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "The file is larger than 5 MB")
    return await _call(ttf.import_camt054(company_id, data, file.filename or "", actor=user, book=book))


@router.post("/{company_id}/fx/revalue")
async def fx_revalue(company_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    _see_tenant()
    raw = (body or {}).get("as_of")
    try:
        as_of = date.fromisoformat(str(raw)[:10]) if raw else ttf.billing.real_today_fi()
    except ValueError:
        raise HTTPException(400, "as_of must be YYYY-MM-DD") from None
    return await _call(ttf.revalue(company_id, actor=user, as_of=as_of))


async def _as_of(value):
    return ttf.parse_as_of(value)
