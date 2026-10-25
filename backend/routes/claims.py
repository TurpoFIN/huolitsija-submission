"""Cargo claims: customers file, ops review, decisions through the core approval queue (claims.approve)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.db import db
from core.deps import current_user, is_internal, require_internal, require_perm
from core.util import NO_ID
from services import claims as svc
from services import claims_recourse as recourse
from services.sdr import get_rate

router = APIRouter(prefix="/api/claims", tags=["claims"])


def _scope(user: dict) -> dict:
    if is_internal(user):
        return {}
    if user.get("org_type") == "customer":
        return {"company_id": user.get("company_id")}
    raise HTTPException(403, "Claims are not available for this account")


def _public(cl: dict, user: dict) -> dict:
    if is_internal(user):
        return cl
    out = {k: v for k, v in cl.items() if k not in ("proposal", "last_proposal_declined", "reviewer", "recourse")}
    return out


def _err(exc: Exception):
    if isinstance(exc, svc.ClaimError):
        raise HTTPException(exc.status, str(exc))
    raise exc


@router.get("")
async def list_claims(status: str | None = None, shipment_id: str | None = None, limit: int = Query(300, le=1000),
                      user: dict = Depends(current_user)):
    base = _scope(user)
    if shipment_id:
        base["shipment_id"] = shipment_id
    q = dict(base)
    if status == "open":
        q["status"] = {"$in": ["submitted", "in_review"]}
    elif status and status != "all":
        q["status"] = status
    items = await db.claims.find(q, NO_ID).sort("created_at", -1).to_list(limit)
    counts = {s: 0 for s in svc.STATUSES}
    async for row in db.claims.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    open_q = {**base, "status": {"$in": ["submitted", "in_review"]}}
    claimed_open = 0
    async for c in db.claims.find(open_q, {"_id": 0, "amount_claimed_cents": 1}):
        claimed_open += c.get("amount_claimed_cents") or 0
    from services.billing import today_fi

    soon = await db.claims.count_documents({**base, "status": {"$in": ["draft", "submitted", "in_review"]},
                                            "time_bar_alert_date": {"$lte": today_fi().isoformat()}})
    return {"items": [_public(c, user) for c in items], "total": len(items), "counts": counts,
            "open_claimed_cents": claimed_open, "time_bar_alerts": soon}


@router.get("/sdr-rate")
async def sdr_rate(user: dict = Depends(current_user)):
    rate = await get_rate()
    if not rate:
        raise HTTPException(503, "The SDR rate could not be fetched from the IMF and none is stored yet")
    return rate


@router.get("/regimes")
async def regimes(user: dict = Depends(current_user)):
    return {"items": [{"key": k, "label": v["label"], "basis": v["basis"], "per_kg": v["per_kg"],
                       "per_package": v.get("per_package"), "time_bar_years": v["time_bar_years"],
                       "notice_days": v["notice_days"], "notice_source": v["notice_source"]} for k, v in svc.REGIMES.items()]}


@router.get("/estimate")
async def estimate(shipment_id: str, type: str = "damage", gross_kg: float | None = None, packages: int | None = None,
                   user: dict = Depends(current_user)):
    s = await db.shipments.find_one({"id": shipment_id, **_scope(user)}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    try:
        est = await svc.estimate(s, claim_type=type, gross_kg=gross_kg, packages=packages)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    est["shipment"] = {"id": s["id"], "reference": s.get("reference"), "mode": s.get("mode"),
                       "weight_kg": (s.get("cargo") or {}).get("weight_kg"), "packages": (s.get("cargo") or {}).get("packages")}
    return est


@router.get("/{claim_id}")
async def get_claim(claim_id: str, user: dict = Depends(current_user)):
    cl = await db.claims.find_one({"id": claim_id, **_scope(user)}, NO_ID)
    if not cl:
        raise HTTPException(404, "Claim not found")
    if is_internal(user) and cl.get("approval_id"):
        cl["approval"] = await db.approvals.find_one({"id": cl["approval_id"]}, NO_ID)
    if is_internal(user) and cl.get("recourse"):
        cl = await recourse.refresh_letters(cl)
    if is_internal(user) and (cl.get("recourse") or {}).get("approval_id"):
        cl["recourse"]["approval"] = await db.approvals.find_one({"id": cl["recourse"]["approval_id"]}, NO_ID)
    return _public(cl, user)


class ClaimIn(BaseModel):
    shipment_id: str
    type: str
    description: str = Field(min_length=10, max_length=5000)
    amount_claimed_cents: int = Field(gt=0)
    gross_kg_affected: float | None = None
    packages_affected: int | None = None
    date_of_loss: str | None = None
    notice_given_on: str | None = None
    document_ids: list[str] = []
    submit: bool = False


@router.post("")
async def create(body: ClaimIn, user: dict = Depends(current_user)):
    scope = _scope(user)
    if not is_internal(user) and "company.book" not in user.get("permissions", []) and "company.manage" not in user.get("permissions", []):
        raise HTTPException(403, "Your role cannot file claims")
    try:
        cl = await svc.create_claim(body.model_dump(), user, company_id=scope.get("company_id"))
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return _public(cl, user)


class ClaimPatch(BaseModel):
    description: str | None = Field(default=None, max_length=5000)
    amount_claimed_cents: int | None = Field(default=None, gt=0)
    gross_kg_affected: float | None = None
    packages_affected: int | None = None
    date_of_loss: str | None = None
    notice_given_on: str | None = None
    type: str | None = None
    document_ids: list[str] | None = None


@router.patch("/{claim_id}")
async def patch(claim_id: str, body: ClaimPatch, user: dict = Depends(current_user)):
    cl = await db.claims.find_one({"id": claim_id, **_scope(user)}, NO_ID)
    if not cl:
        raise HTTPException(404, "Claim not found")
    try:
        cl = await svc.update_claim(claim_id, body.model_dump(exclude_unset=True), user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return _public(cl, user)


@router.post("/{claim_id}/submit")
async def submit(claim_id: str, user: dict = Depends(current_user)):
    cl = await db.claims.find_one({"id": claim_id, **_scope(user)}, NO_ID)
    if not cl:
        raise HTTPException(404, "Claim not found")
    try:
        return _public(await svc.submit_claim(claim_id, user), user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@router.post("/{claim_id}/review")
async def review(claim_id: str, user: dict = Depends(require_internal)):
    try:
        return await svc.start_review(claim_id, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


class ProposalIn(BaseModel):
    decision: str
    amount_cents: int | None = None
    note: str = Field(min_length=3, max_length=2000)


@router.post("/{claim_id}/propose")
async def propose(claim_id: str, body: ProposalIn, user: dict = Depends(require_internal)):
    try:
        return await svc.propose_decision(claim_id, decision=body.decision, amount_cents=body.amount_cents,
                                          note=body.note, actor=user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


class ClaimPaidIn(BaseModel):
    paid_at: str | None = None
    reference: str | None = Field(default=None, max_length=120)


@router.post("/{claim_id}/mark-paid")
async def mark_paid(claim_id: str, body: ClaimPaidIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        return await svc.mark_paid(claim_id, paid_at=body.paid_at, reference=body.reference, actor=user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


# --------------------------------------------------------------------------------------------- carrier recourse (s2)
@router.get("/{claim_id}/recourse")
async def get_recourse(claim_id: str, user: dict = Depends(require_internal)):
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise HTTPException(404, "Claim not found")
    cl = await recourse.refresh_letters(cl)
    r = cl.get("recourse")
    if r and r.get("approval_id"):
        r["approval"] = await db.approvals.find_one({"id": r["approval_id"]}, NO_ID)
    return {"claim_id": claim_id, "number": cl.get("number"), "status": cl["status"], "recourse": r}


class RecourseIn(BaseModel):
    leg_seq: int | None = None


@router.post("/{claim_id}/recourse")
async def create_recourse(claim_id: str, body: RecourseIn | None = None, user: dict = Depends(require_internal)):
    """Draft the carrier recourse for a paid claim and put it on the owner queue (idempotent)."""
    try:
        cl = await recourse.create_recourse(claim_id, user, leg_seq=(body.leg_seq if body else None))
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return {"claim_id": claim_id, "number": cl.get("number"), "status": cl["status"], "recourse": cl.get("recourse")}


@router.post("/{claim_id}/recourse/send")
async def send_recourse(claim_id: str, user: dict = Depends(require_perm("claims.approve"))):
    """Send an approved recourse letter once the carrier has a claims e-mail and the recourse contract flag."""
    try:
        cl = await recourse.send_letter(claim_id, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return {"claim_id": claim_id, "number": cl.get("number"), "status": cl["status"], "recourse": cl.get("recourse")}


class RecoveryIn(BaseModel):
    amount_cents: int = Field(gt=0)
    received_on: str | None = None
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    close: bool = False


@router.post("/{claim_id}/recourse/recovery")
async def record_recovery(claim_id: str, body: RecoveryIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        cl = await recourse.record_recovery(claim_id, amount_cents=body.amount_cents, received_on=body.received_on,
                                            reference=body.reference, note=body.note, close=body.close, actor=user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return {"claim_id": claim_id, "number": cl.get("number"), "status": cl["status"], "recourse": cl.get("recourse")}
