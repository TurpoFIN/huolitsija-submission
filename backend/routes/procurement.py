"""Procurement (s3 slice 2): carrier allocations, overseas agents and agreements, partner scorecards, carrier ranking."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.util import NO_ID, now_iso
from services import agent_network as agents, allocations, scorecards

router = APIRouter(prefix="/api/procurement", tags=["procurement"])


# ------------------------------------------------------------------------------------------------- allocations
@router.get("/allocations")
async def list_allocations(carrier_id: str | None = None, user: dict = Depends(require_internal)):
    return {"items": await allocations.list_all(carrier_id)}


@router.post("/allocations")
async def create_allocation(body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await allocations.create(body, user)


@router.get("/allocations/{allocation_id}")
async def get_allocation(allocation_id: str, on_date: str | None = None, user: dict = Depends(require_internal)):
    """on_date picks the period shown in status_now (default today), e.g. the month a booking departs."""
    try:
        day = date.fromisoformat(on_date) if on_date else None
    except ValueError:
        raise HTTPException(400, "on_date must be YYYY-MM-DD")
    row = await allocations.get(allocation_id, day)
    if not row:
        raise HTTPException(404, "Allocation not found")
    return row


@router.patch("/allocations/{allocation_id}")
async def update_allocation(allocation_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await allocations.update(allocation_id, body, user)


@router.delete("/allocations/{allocation_id}")
async def end_allocation(allocation_id: str, user: dict = Depends(require_perm("rates.write"))):
    """Ends the allocation (kept for history; usage rows stay)."""
    return await allocations.update(allocation_id, {"status": "ended"}, user)


# ------------------------------------------------------------------------------------------------- agents
@router.get("/agents")
async def list_agents(country: str | None = None, user: dict = Depends(require_internal)):
    return {"items": await agents.list_agents(country)}


@router.post("/agents")
async def create_agent(body: dict, user: dict = Depends(require_perm("rates.write"))):
    row = await agents.create_agent(body, user)
    return await agents.get_agent(row["id"])


@router.get("/agents/select")
async def select_agent(country: str, role: str, mode: str, on_date: str | None = None, company_id: str | None = None,
                       user: dict = Depends(require_internal)):
    if role not in agents.ROLES or mode not in agents.MODES:
        raise HTTPException(400, "role must be origin|destination and mode road|sea|air|rail")
    day = date.fromisoformat(on_date) if on_date else None
    return {"selected": await agents.select_agent(country, role, mode, on_date=day, company_id=company_id)}


@router.post("/agents/from-candidate/{candidate_id}")
async def agent_from_candidate(candidate_id: str, body: dict | None = None, user: dict = Depends(require_perm("rates.write"))):
    return await agents.agent_from_candidate(candidate_id, body or {}, user)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user: dict = Depends(require_internal)):
    row = await agents.get_agent(agent_id)
    if not row:
        raise HTTPException(404, "Agent not found")
    return row


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await agents.update_agent(agent_id, body, user)


@router.post("/agents/{agent_id}/agreements")
async def create_agreement(agent_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await agents.create_agreement(agent_id, body, user)


@router.post("/agents/{agent_id}/rate-cards")
async def create_agent_rate(agent_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await agents.create_rate_card(agent_id, body, user)


@router.post("/agreements/{agreement_id}/{status}")
async def agreement_status(agreement_id: str, status: str, body: dict | None = None,
                           user: dict = Depends(require_perm("rates.write"))):
    if status not in ("sent", "signed", "terminated", "draft"):
        raise HTTPException(400, "Choose sent, signed, terminated or draft")
    return await agents.set_agreement_status(agreement_id, status, body or {}, user)


# ------------------------------------------------------------------------------------------------- scorecards
@router.get("/scorecards")
async def list_scorecards(refresh: bool = False, user: dict = Depends(require_internal)):
    return {"items": await scorecards.list_all(refresh=refresh), "weights": scorecards.WEIGHTS, "min_sample": scorecards.MIN_SAMPLE}


@router.get("/scorecards/{carrier_id}")
async def get_scorecard(carrier_id: str, refresh: bool = False, user: dict = Depends(require_internal)):
    row = await scorecards.get(carrier_id, refresh=refresh)
    if not row:
        raise HTTPException(404, "Carrier not found")
    return row


@router.get("/rank")
async def rank(mode: str, origin: str, destination: str, service: str | None = None, qty: float = 1.0,
               on_date: str | None = None, user: dict = Depends(require_internal)):
    if mode not in ("road", "sea", "air", "rail"):
        raise HTTPException(400, "mode must be road, sea, air or rail")
    day = date.fromisoformat(on_date) if on_date else None
    service = service or scorecards.DEFAULT_SERVICE[mode]  # prices are only comparable within one service
    return {"service": service,
            "items": await scorecards.rank_carriers(mode, origin, destination, service=service, on_date=day, qty=qty)}


@router.get("/booking-rank")
async def booking_rank(shipment_id: str, leg_seq: int, user: dict = Depends(require_internal)):
    """s2's carrier booking ranking for one leg (services.carrier_booking.rank_carriers), with the s3 scorecard and
    allocation inputs; read-only."""
    from services import carrier_booking

    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    leg = next((lg for lg in (s or {}).get("legs") or [] if int(lg.get("seq", -1)) == int(leg_seq)), None)
    if not s or not leg:
        raise HTTPException(404, "Shipment leg not found")
    return await carrier_booking.rank_carriers(s, leg)


@router.patch("/carriers/{carrier_id}/org-type")
async def set_org_type(carrier_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    org_type = body.get("org_type")
    if org_type not in ("carrier", "haulier", "agent"):
        raise HTTPException(400, "org_type must be carrier, haulier or agent")
    c = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Carrier not found")
    sets = {"org_type": org_type, "updated_at": now_iso()}
    if org_type == "agent" and not c.get("agent"):
        sets["agent"] = agents._agent_block({"countries": body.get("countries") or [c.get("country") or "FI"],
                                             "roles": body.get("roles"), "services": body.get("services"), "modes": body.get("modes")})
    await db.carriers.update_one({"id": carrier_id}, {"$set": sets})
    await audit(user, "carrier.org_type", "carrier", carrier_id, {"before": c.get("org_type") or "carrier", "after": org_type})
    agents._cache.clear()
    return await db.carriers.find_one({"id": carrier_id}, NO_ID)
