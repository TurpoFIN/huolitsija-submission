"""EU Binding Tariff Information (EBTI) precedents: lookup by code, one ruling, index status and refresh."""
from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import require_internal, require_perm
from core.jobs import enqueue
from core.util import now_iso
from services import ebti

router = APIRouter(prefix="/api/customs/bti", tags=["customs"])


@router.get("")
async def bti_by_code(code: str = Query(..., min_length=4, max_length=12), limit: int = Query(20, ge=1, le=200),
                      user: dict = Depends(require_internal)):
    return await ebti.by_code(code, limit=limit)


@router.get("/status")
async def bti_status(user: dict = Depends(require_internal)):
    return await ebti.status()


@router.post("/refresh")
async def bti_refresh(full: bool = False, user: dict = Depends(require_perm("settings.write"))):
    j = await enqueue("ebti.refresh", {"full": full}, key=f"ebti:manual:{now_iso()[:16]}:{full}", source="ops")
    return {"job_id": j["id"], "status": j["status"]}


@router.get("/{ref}")
async def bti_one(ref: str, user: dict = Depends(require_internal)):
    d = await ebti.get(ref)
    if not d:
        raise HTTPException(404, "BTI not found among current rulings")
    return d
