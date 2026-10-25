"""Management insight for internal users: KPIs and the demand / revenue forecast (definitions in services/insights.py).

Permissions: shipments.read_all for volume, on-time and win rate. Money (sell, margin, revenue, receivables, AR days, the
revenue forecast) also needs billing.write or quotes.approve (ops admin, finance, sales); others get those fields as null
with financials_visible=false."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import require_perm
from services import insights

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _financials(user: dict) -> bool:
    perms = set(user.get("permissions") or [])
    return bool(perms & {"billing.write", "quotes.approve"})


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise HTTPException(400, f"{name} must be a date like 2026-10-24")


@router.get("/kpis")
async def get_kpis(from_: str | None = Query(None, alias="from"), to: str | None = None,
                   interval: str = Query("week", pattern="^(week|month)$"), company_id: str | None = None,
                   user: dict = Depends(require_perm("shipments.read_all"))):
    end = _day(to, "to") or insights.today()
    start = _day(from_, "from") or end - timedelta(days=89)
    if (end - start).days > 3 * 366:
        raise HTTPException(400, "Choose a period of at most three years")
    return await insights.kpis(start, end, interval, company_id=company_id, financials=_financials(user))


@router.get("/forecast")
async def get_forecast(horizon: int = Query(12, ge=1, le=52), interval: str = Query("week", pattern="^(week|month)$"),
                       company_id: str | None = None, user: dict = Depends(require_perm("shipments.read_all"))):
    return await insights.forecast(horizon, interval, company_id=company_id, financials=_financials(user))
