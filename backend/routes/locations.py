"""Locations reference data (UN/LOCODE + airports + curated terminals). Public: the instant-quote page uses it."""
from fastapi import APIRouter, HTTPException, Query

from services import geo

router = APIRouter(prefix="/api", tags=["locations"])
KINDS = {"port", "airport", "rail", "road"}


@router.get("/locations")
async def list_locations(q: str = "", kind: str | None = None, country: str | None = None, limit: int = Query(20, ge=1, le=100)):
    if kind and kind not in KINDS:
        raise HTTPException(422, "kind must be one of port, airport, rail, road")
    items = geo.search(q, kind=kind, limit=limit, country=country)
    return {"items": items, "total": len(items)}


@router.get("/locations/{code}")
async def get_location(code: str):
    loc = geo.lookup_code(code)
    if not loc:
        raise HTTPException(404, f"No location with code {code.upper()}. Search by name instead.")
    return geo.public_location(loc)
