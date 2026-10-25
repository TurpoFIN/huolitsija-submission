"""Official TARIC consultation and the internal licence register."""
from datetime import date as CalendarDate
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import require_internal, require_perm
from services import customs_licences, taric

router = APIRouter(prefix="/api/customs", tags=["customs-reference"],
                   dependencies=[Depends(require_internal)])


async def _official(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(400, {"fi": "Virheellinen TARIC-haku", "en": str(exc)})
    except taric.TaricUnavailable as exc:
        raise HTTPException(503, {"fi": "EU:n TARIC-palvelu ei ole saatavilla", "en": str(exc)})


@router.get("/taric")
async def lookup(code: str = Query(..., min_length=4, max_length=10, pattern=r"^\d+$"),
                 country: str | None = None, flow: Literal["import", "export"] = "import",
                 date: CalendarDate | None = None):
    return await _official(taric.lookup(code, country=country, flow=flow, on=date))


@router.get("/taric/quota/{order}")
async def quota(order: str, date: CalendarDate | None = None):
    return await _official(taric.quota(order, on=date))


@router.get("/licences")
async def licences(company_id: str | None = None, certificate_code: str | None = None,
                   limit: int = Query(200, ge=1, le=1000)):
    return {"items": await customs_licences.list_licences(company_id=company_id,
                                                         certificate_code=certificate_code, limit=limit)}


@router.post("/licences", status_code=201)
async def create(body: dict, actor: dict = Depends(require_perm("customs.review"))):
    return await customs_licences.create_licence(body, actor)


@router.patch("/licences/{licence_id}")
async def update(licence_id: str, body: dict, actor: dict = Depends(require_perm("customs.review"))):
    return await customs_licences.update_licence(licence_id, body, actor)


@router.patch("/licences")
async def update_body(body: dict, actor: dict = Depends(require_perm("customs.review"))):
    if not body.get("id"):
        raise HTTPException(400, {"fi": "Luvan tunniste puuttuu", "en": "Licence id is required"})
    return await customs_licences.update_licence(body["id"], body, actor)


@router.delete("/licences/{licence_id}")
async def revoke(licence_id: str, actor: dict = Depends(require_perm("customs.review"))):
    return await customs_licences.delete_licence(licence_id, actor)
