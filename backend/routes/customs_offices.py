"""Internal, official customs-office reference lookup (before /customs/{id})."""
from fastapi import APIRouter, Depends, HTTPException, Query
from core.deps import require_internal, require_perm
from core.jobs import enqueue
from core.util import now_iso
from services import customs_offices as col

router = APIRouter(prefix="/api/customs/offices", tags=["customs-reference"],
                   dependencies=[Depends(require_internal)])


@router.get("")
async def offices(country: str | None = Query(None, pattern=r"^[A-Za-z]{2}$"),
                  role: str | None = Query(None, pattern=r"^[A-Za-z]{3}$"),
                  q: str = Query("", max_length=200), unlocode: str | None = None,
                  mode: str | None = None, limit: int = Query(200, ge=1, le=5000),
                  offset: int = Query(0, ge=0)):
    rows = await col.offices_for(country, role, unlocode=unlocode, mode=mode)
    if q:
        term = q.casefold()
        rows = [r for r in rows if term in " ".join(str(r.get(k) or "") for k in
                ("code", "name", "unlocode", "address", "translations")).casefold()]
    snapshot = await col.db.raw.customs_offices.find_one({}, {"_id": 0})
    return {"items": [{**r, "fresh": col.fresh(r)} for r in rows[offset:offset+limit]],
            "total": len(rows), "offset": offset, "limit": limit,
            "status": "unavailable" if snapshot is None else "current" if col.fresh(snapshot) else "stale",
            "source_url": (snapshot or {}).get("source_url"),
            "fetched_at": (snapshot or {}).get("fetched_at")}


@router.post("/refresh")
async def offices_refresh(user: dict = Depends(require_perm("settings.write"))):
    """Run the official COL refresh now (the daily schedule does the same)."""
    j = await enqueue("customs.offices_refresh", {}, key=f"col:manual:{now_iso()[:16]}", source="ops")
    return {"job_id": j["id"], "status": j["status"]}


@router.get("/{code}")
async def office(code: str):
    row = await col.office(code)
    if not row:
        raise HTTPException(404, {"fi": "Toimipaikkaa ei löydy ladatusta COL-luettelosta",
                                  "en": "Office not found in the loaded COL snapshot"})
    return {**row, "fresh": col.fresh(row), "valid_today": col.eligible(row)}
