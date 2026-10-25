"""Any-city quoting (s5): city autocomplete, benchmark levels, routing plan. Quotes themselves stay in routes/quotes.py
and routes/public_quote.py (services.pricing calls services.global_quote underneath)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core.deps import require_internal, require_perm
from core.db import db
from services import benchmarks as bm
from services import global_quote, hubs, pricing

public = APIRouter(prefix="/api/public", tags=["public"])
internal = APIRouter(prefix="/api/global", tags=["global quoting"])
routers = [public, internal]


@public.get("/places")
async def places(q: str = "", limit: int = 8):
    """Cities, ports, airports and rail terminals for the route fields (offline: no geocoder call per keystroke)."""
    items = await pricing.search_locations(q[:120], limit=min(max(limit, 1), 20))
    for it in items:
        kinds = it.get("kinds") or []
        it["kind"] = "city" if kinds == ["city"] else next((k for k in ("port", "airport", "rail") if k in kinds), "city")
    return {"items": items}


@internal.get("/benchmarks")
async def list_benchmarks(user: dict = Depends(require_internal)):
    lv = await bm.levels(force=True)
    fx = bm._cache.get("fx")
    last = (await db.system.find_one({"key": "benchmarks:last_refresh"}) or {}).get("value")
    return {"items": sorted(lv.values(), key=lambda x: (x.get("mode") or "", x.get("lane_code"))), "fx": fx,
            "last_refresh": last, "models": {"road_eur_per_km": bm.ROAD_EUR_PER_KM, "rail_eur_per_feu_km": bm.RAIL_EUR_PER_FEU_KM,
                                             "as_of": bm.MODEL_AS_OF, "source": bm.MODEL_SOURCE}}


@internal.post("/benchmarks")
async def add_benchmark(body: dict, user: dict = Depends(require_perm("rates.write"))):
    """Load one dated index level (ops), e.g. this week's WCI lane from the Drewry page."""
    try:
        doc = await bm.store_level({**body, "source_url": body.get("source_url")}, by="ops")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    from core.audit import audit

    await audit(user, "benchmark_level_loaded", "benchmark_indices", doc["id"], {k: doc.get(k) for k in ("lane_code", "value", "as_of")})
    return {"level": {k: v for k, v in doc.items() if k != "_id"}}


@internal.post("/benchmarks/refresh")
async def refresh_benchmarks(user: dict = Depends(require_perm("settings.write"))):
    return {"fx": await bm.refresh_fx(), "wci": await bm.refresh_wci()}


@internal.get("/plan")
async def plan(origin: str, destination: str, mode: str | None = None, weight_kg: float = 1000, volume_m3: float = 3,
               container: str | None = None, user: dict = Depends(require_internal)):
    """Routing debug: resolved places, candidate hubs per mode (no prices, nothing stored)."""
    cargo = {"weight_kg": weight_kg, "volume_m3": volume_m3}
    if container:
        cargo = {"load_type": "FCL", "containers": [{"type": container, "count": 1}]}
    try:
        req = await pricing.normalize_request({"origin": origin, "destination": destination, "mode": mode, "cargo": cargo})
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None
    m = pricing.cargo_metrics(req["cargo"])
    routings = await global_quote.plan(req, m)
    return {"origin": req["origin"], "destination": req["destination"], "road_connected": hubs.road_connected(req["origin"], req["destination"]),
            "rail_network": hubs.rail_network(req["origin"], req["destination"]),
            "routings": [{"mode": r["mode"], "service": r["service"], "from": hubs.hub_ref(r["main"][0]) if r["main"][0].get("kind") else r["main"][0].get("name"),
                          "to": hubs.hub_ref(r["main"][1]) if r["main"][1].get("kind") else r["main"][1].get("name")} for r in routings]}
