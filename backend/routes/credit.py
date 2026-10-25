"""s7 credit routes (shared/interfaces.md, section s7).

GET /api/credit (all customers' positions), GET /api/credit/{company_id} (position, checks, pending card),
POST /api/credit/{company_id}/check (credit check now), PATCH /api/credit/{company_id} (limit / hold)  [customers.write]
GET /api/credit/providers (which sources are live)                                                     [customers.write]
POST /api/credit/preview {business_id} (free public signals for any Business ID, nothing stored)       [customers.write]
GET /api/portal/credit (own position)                                                                    [company.manage]
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adapters import credit_bureau as cb
from core.db import db
from core.deps import require_perm
from core.util import NO_ID
from services import credit

router = APIRouter(prefix="/api", tags=["credit"])


@router.get("/credit")
async def list_positions(status: str | None = None, user: dict = Depends(require_perm("customers.write"))):
    out = []
    async for c in db.companies.find({"status": {"$ne": "deleted"}}, NO_ID).sort("name", 1):
        pos = await credit.credit_position(c["id"], company=c)
        if not status or pos["status"] == status:
            out.append(pos)
    counts: dict[str, int] = {}
    for p in out:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return {"items": out, "counts": counts, "total_exposure_cents": sum(p["exposure_cents"] for p in out)}


@router.get("/credit/providers")
async def providers(user: dict = Depends(require_perm("customers.write"))):
    name = cb.bureau_name()
    bureau = None
    if name == "creditsafe":
        c = cb.Creditsafe()
        bureau = {"name": name, "configured": c.configured(), "base_url": c.base,
                  "mode": "sandbox" if "sandbox" in c.base else "live"}
    elif name == "asiakastieto":
        a = cb.Asiakastieto()
        bureau = {"name": name, "configured": a.configured(), "base_url": a.base or None}
    return {"free": [{"name": "ytj", "url": cb.YTJ_URL, "label": cb.YTJ_SOURCE},
                     {"name": "tax_debt", "url": getattr(cb, "TAX_DEBT_URL", None), "label": getattr(cb, "TAX_DEBT_SOURCE", None)},
                     {"name": "financials", "url": getattr(cb, "FINANCIALS_URL", None),
                      "label": getattr(cb, "FINANCIALS_SOURCE", None)}],
            "bureau": bureau, "credit_bureau": name}


class PreviewIn(BaseModel):
    business_id: str = Field(min_length=1, max_length=20)


@router.post("/credit/preview")
async def preview(body: PreviewIn, user: dict = Depends(require_perm("customers.write"))):
    """The free credit signals (YTJ, tax-debt register, PRH financials) for a Business ID before it is a customer.
    Read-only: no company, check row, limit or approval is created."""
    from core.settings import get_settings

    chk = await cb.free_check(body.business_id, await get_settings())
    for s in (chk.get("signals") or {}).values():
        if isinstance(s, dict):
            s.pop("raw", None)
    return {"check": chk, "stored": False}


@router.get("/credit/{company_id}")
async def get_position(company_id: str, user: dict = Depends(require_perm("customers.write"))):
    pos = await credit.credit_position(company_id)
    checks = await db.credit_checks.find({"company_id": company_id}, NO_ID).sort("at", -1).limit(10).to_list(10)
    for ch in checks:
        for s in (ch.get("signals") or {}).values():
            if isinstance(s, dict):
                s.pop("raw", None)
    card = await db.approvals.find_one({"kind": "credit_limit", "ref_id": company_id, "status": "pending"}, NO_ID)
    return {"position": pos, "checks": checks, "pending_approval": card}


@router.post("/credit/{company_id}/check")
async def check_now(company_id: str, user: dict = Depends(require_perm("customers.write"))):
    chk = await credit.run_credit_check(company_id, actor=user, reason="manual")
    return {"check": chk, "position": await credit.credit_position(company_id)}


class CreditPatch(BaseModel):
    credit_limit_cents: int | None = Field(default=None, ge=0, le=10_000_000_000)
    hold: bool | None = None
    reason: str | None = Field(default=None, max_length=300)


@router.patch("/credit/{company_id}")
async def patch_credit(company_id: str, body: CreditPatch, user: dict = Depends(require_perm("customers.write"))):
    if not await db.companies.find_one({"id": company_id}, {"_id": 0, "id": 1}):
        raise HTTPException(404, "Company not found")
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "Give credit_limit_cents or hold")
    if "credit_limit_cents" in data and data["credit_limit_cents"] is not None:
        await credit.set_limit_manual(company_id, data["credit_limit_cents"], user, data.get("reason"))
    if data.get("hold") is not None:
        if data["hold"] and not (data.get("reason") or "").strip():
            raise HTTPException(400, "Write a short reason for the credit hold")
        await credit.set_hold(company_id, data["hold"], (data.get("reason") or "").strip() or
                              ("Released by " + (user.get("name") or user.get("email") or "ops")), user, auto=False)
    return {"position": await credit.credit_position(company_id)}


portal_router = APIRouter(prefix="/api/portal", tags=["credit"])


@portal_router.get("/credit")
async def portal_credit(user: dict = Depends(require_perm("company.manage"))):
    if not user.get("company_id"):
        raise HTTPException(404, "Your account is not linked to a company")
    return {"position": credit.customer_view(await credit.credit_position(user["company_id"]))}


extra_routers = [portal_router]
