"""Dangerous goods lookup and checks over HTTP (s12). Data and rules: services/dangerous_goods.py.

GET  /api/compliance/dg/un/{un}      merged record (any signed-in user)
GET  /api/compliance/dg/search?q=    UN number prefix or name
POST /api/compliance/dg/check        {cargo, mode, legs?} -> {status, findings, tunnel_code, segregation, ...}
GET  /api/compliance/dg/dataset      counts, sources (URL, sha256, licence), what is not included
GET  /api/public/dg/un/{un}          read-only lookup without sign-in (the public quote form)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from core.deps import current_user
from services import dangerous_goods

router = APIRouter(prefix="/api", tags=["compliance"])


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cargo: dict
    mode: str | None = None
    legs: list[dict] | None = None


def _get(un: str) -> dict:
    if not dangerous_goods.norm_un(un):
        raise HTTPException(400, "Give a UN number with four digits, for example UN1263.")
    rec = dangerous_goods.lookup(un)
    if not rec:
        raise HTTPException(404, f"{dangerous_goods.norm_un(un)} is not in the dangerous goods list (49 CFR 172.101, ADR 2025).")
    return rec


@router.get("/compliance/dg/un/{un}")
async def dg_un(un: str, user: dict = Depends(current_user)):
    return _get(un)


@router.get("/compliance/dg/search")
async def dg_search(q: str = "", limit: int = 20, user: dict = Depends(current_user)):
    return {"items": dangerous_goods.search(q, limit=max(1, min(limit, 50)))}


@router.get("/compliance/dg/dataset")
async def dg_dataset(user: dict = Depends(current_user)):
    return dangerous_goods.dataset_info()


@router.post("/compliance/dg/check")
async def dg_check(body: CheckIn, user: dict = Depends(current_user)):
    mode = (body.mode or "").lower() or None
    if mode and mode not in ("sea", "road", "air", "rail"):
        raise HTTPException(400, "Mode must be sea, road, air or rail.")
    return dangerous_goods.check_cargo(body.cargo, mode, body.legs)


@router.get("/public/dg/un/{un}")
async def public_dg_un(un: str):
    rec = _get(un)
    return {k: rec[k] for k in ("un", "name", "names", "class", "subsidiary", "packing_groups", "sources")} | {
        "tunnel_code": (rec["adr"] or {}).get("tunnel_code"),
        "variants": [{"pg": v["pg"], "name": v["name"], "air": v["air"] and {k: v["air"][k] for k in (
            "passenger_limit", "cargo_limit", "passenger_forbidden", "cargo_forbidden")}} for v in rec["variants"]]}
