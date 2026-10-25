"""Compliance: sanctions / denied-party lists, party screenings and compliance holds (s12).

GET  /api/compliance/lists                    status of every list (eu, un, ofac, uk, csl)        shipments.read_all
POST /api/compliance/lists/refresh            download now (force)                                settings.write
GET  /api/compliance/screenings?status=&subject_type=&subject_id=&limit=                           shipments.read_all
GET  /api/compliance/screenings/{id}                                                              shipments.read_all
POST /api/compliance/shipments/{id}/screen    screen every party now (can only add a hold)        shipments.read_all
POST /api/compliance/rescreen-all             open shipments + KYC-approved customers + carriers  settings.write
GET  /api/compliance/holds                    shipments on a compliance hold                      shipments.read_all

Potential matches are decided in the approvals queue (kind sanctions_review, perm kyc.approve).
"""
from fastapi import APIRouter, Depends, HTTPException

from core.audit import actor_ref, audit
from core.compliance_jobs import enqueue_or_run
from core.db import db
from core.deps import require_perm
from core.util import NO_ID, now_iso
from services import party_screening, screening

router = APIRouter(prefix="/api", tags=["compliance"])

SUBJECT_TYPES = ("shipment", "company", "booking", "carrier")
STATUSES = ("clear", "potential_match", "error", "cleared", "blocked_sanctions")


@router.get("/compliance/lists")
async def compliance_lists(user: dict = Depends(require_perm("shipments.read_all"))):
    items = await screening.lists_status()
    return {"items": items, "loaded": sum(1 for s in items if s["loaded"]), "total": len(items),
            "ofsi_consolidated_list": {"url": screening.OFSI_CONLIST_URL, "loaded": False,
                                       "why": "Not updated since the UK Sanctions List became the single UK list on 28 Jan 2026; "
                                              "the UK list (uk) carries OFSI's designations."},
            "csl_live_api": {"configured": bool(screening.csl_api_key()), "env": "TRADE_GOV_API_KEY"}}


@router.post("/compliance/lists/refresh")
async def compliance_lists_refresh(user: dict = Depends(require_perm("settings.write"))):
    items = await screening.refresh_lists(force=True)
    await audit(user, "sanctions_lists_refreshed", "compliance", None,
                {"lists": {s["list"]: {"status": s["status"], "entries": s["entries"], "version": s["version"]} for s in items}})
    return {"items": items}


@router.get("/compliance/screenings")
async def list_screenings(status: str | None = None, subject_type: str | None = None, subject_id: str | None = None,
                          limit: int = 100, user: dict = Depends(require_perm("shipments.read_all"))):
    q: dict = {}
    if status:
        if status not in STATUSES:
            raise HTTPException(400, f"Unknown status. Use one of: {', '.join(STATUSES)}.")
        q["status"] = status
    if subject_type:
        if subject_type not in SUBJECT_TYPES:
            raise HTTPException(400, f"Unknown subject type. Use one of: {', '.join(SUBJECT_TYPES)}.")
        q["subject_type"] = subject_type
    if subject_id:
        q["subject_id"] = subject_id
    items = await db.party_screenings.find(q, NO_ID).sort("created_at", -1).to_list(max(1, min(limit, 500)))
    counts = {r["_id"]: r["n"] async for r in db.party_screenings.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    return {"items": items, "counts": counts}


@router.get("/compliance/screenings/{screening_id}")
async def get_screening(screening_id: str, user: dict = Depends(require_perm("shipments.read_all"))):
    row = await db.party_screenings.find_one({"id": screening_id}, NO_ID)
    if not row:
        raise HTTPException(404, "Screening not found")
    if row.get("approval_id"):
        row["approval"] = await db.approvals.find_one({"id": row["approval_id"]}, NO_ID)
    return row


@router.post("/compliance/shipments/{shipment_id}/screen")
async def screen_shipment_now(shipment_id: str, user: dict = Depends(require_perm("shipments.read_all"))):
    row = await party_screening.screen_shipment(shipment_id, "manual", actor=user)
    await audit(user, "compliance.screen", "shipment", shipment_id, {"screening_id": row["id"], "status": row["status"]},
                company_id=row.get("company_id"))
    return row


@router.post("/compliance/rescreen-all")
async def rescreen_all(user: dict = Depends(require_perm("settings.write"))):
    res = await enqueue_or_run("compliance.rescreen_all", {"trigger": "manual", "manual": True, "actor": actor_ref(user)},
                               key=f"rescreen:manual:{now_iso()}")
    await audit(user, "compliance.rescreen_all", "compliance", None, {"queued": True})
    return {"status": "queued", "job": {k: res.get(k) for k in ("id", "kind", "status", "key") if isinstance(res, dict)}}


@router.get("/compliance/holds")
async def compliance_holds(user: dict = Depends(require_perm("shipments.read_all"))):
    """Shipments on a compliance hold: {shipment_id, reference, reason, screening_id, since} (+ status, approval_id)."""
    proj = {"_id": 0, "id": 1, "reference": 1, "customer_name": 1, "compliance_hold_code": 1, "compliance_hold_since": 1,
            "compliance_status": 1, "compliance_screening_id": 1, "compliance_hold_reason": 1}
    items = []
    async for s in db.shipments.find({"compliance_hold": True}, proj).sort("compliance_hold_since", -1).limit(500):
        row = await db.party_screenings.find_one({"id": s.get("compliance_screening_id")}, {"_id": 0, "parties": 1}) or {}
        card = await db.approvals.find_one({"kind": "sanctions_review", "ref_id": s["id"], "status": "pending"}, {"_id": 0, "id": 1})
        reason = party_screening.hold_reason_internal(s.get("compliance_hold_code"), row.get("parties") or [])
        items.append({"shipment_id": s["id"], "reference": s.get("reference"), "reason": reason,
                      "screening_id": s.get("compliance_screening_id"), "since": s.get("compliance_hold_since"),
                      "customer_name": s.get("customer_name"), "status": s.get("compliance_status"),
                      "approval_id": (card or {}).get("id")})
    return {"items": items}
