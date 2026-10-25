"""Origin and preference API (s11 origin): DE 4/17 assessment from official data, REX check, arrangements, product-
specific rules, tariff quota status and the export statement on origin. Internal users with customs.review."""
from datetime import date as Date

from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import require_perm
from services import customs_origin as co

router = APIRouter(prefix="/api/customs/origin", tags=["customs-origin"])


@router.get("/assess")
async def assess(code: str = Query(..., pattern=r"^\d{6}(?:\d{2})?(?:\d{2})?$", description="CN/TARIC code (6-10 digits)"),
                 origin: str = Query(..., min_length=2, max_length=2), date: Date | None = None,
                 value_eur: float | None = Query(None, ge=0, description="customs value of the item in EUR"),
                 net_kg: float | None = Query(None, ge=0), export_country: str | None = None,
                 _user: dict = Depends(require_perm("customs.review"))):
    item = {"taric_code": code, "customs_value_cents": int(round(value_eur * 100)) if value_eur is not None else None,
            "net_kg": net_kg}
    return await co.assess(item, origin=origin.upper(), on=date, export_country=export_country)


@router.get("/rex/{number}")
async def rex(number: str, _user: dict = Depends(require_perm("customs.review"))):
    if not co.REX_RE.fullmatch(number.replace(" ", "").upper()):
        raise HTTPException(400, "Not a REX number (format: ISO country code + REX + national part)")
    return await co.rex_check(number.replace(" ", "").upper())


@router.get("/arrangements")
async def arrangements(_user: dict = Depends(require_perm("customs.review"))):
    return co.list_arrangements()


@router.get("/psr")
async def psr(code: str = Query(..., min_length=4), origin: str = Query(..., min_length=2, max_length=2),
              destination: str = "FI", direction: str = Query("IMPORT", pattern="^(IMPORT|EXPORT|import|export)$"),
              _user: dict = Depends(require_perm("customs.review"))):
    return await co.product_specific_rule(code, origin.upper(), destination=destination.upper(), direction=direction.upper())


@router.get("/quota/{order_number}")
async def quota(order_number: str, date: Date | None = None, _user: dict = Depends(require_perm("customs.review"))):
    return await co.quota_status(order_number, date)


@router.get("/statement")
async def statement(arrangement: str, exporter_id: str = "", origin_country: str = "EU", lang: str = "en",
                    _user: dict = Depends(require_perm("customs.review"))):
    try:
        return co.origin_statement_text(arrangement, exporter_id, origin_country, lang)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
