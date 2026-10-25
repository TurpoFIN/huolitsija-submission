"""s7 margin rules and customer contract tariffs (see shared/interfaces.md, section s7).

Margin rules [settings.write; read: internal]: GET|POST /api/margin-rules, GET|PATCH|DELETE /api/margin-rules/{id},
POST /api/margin-rules/simulate {request | quote_id, company_id?}, POST /api/margin-rules/defaults {active?}.
Contract tariffs [quotes.write]: GET|POST /api/tariffs, GET|PATCH /api/tariffs/{id}, POST /api/tariffs/{id}/activate|expire,
POST /api/tariffs/expire-sweep.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.settings import get_settings
from core.util import NO_ID, new_id, now_iso
from services import contract_tariffs as ct
from services import margin_rules as mr
from services import pricing

router = APIRouter(prefix="/api", tags=["pricing_rules"])


# ------------------------------------------------------------------------------------------------
# Margin rules
# ------------------------------------------------------------------------------------------------
@router.get("/margin-rules")
async def list_rules(active: bool | None = None, _u: dict = Depends(require_internal)):
    q = {} if active is None else {"active": active}
    rules = await db.margin_rules.find(q, NO_ID).sort([("active", -1), ("priority", -1), ("created_at", 1)]).to_list(2000)
    s = await get_settings()
    return {"rules": rules, "fallback": {"default_margin_pct": s["default_margin_pct"]},
            "precedence": ["company_id", "lane", "origin_country/destination_country", "service", "mode", "size", "priority"]}


@router.get("/margin-rules/floor-check")
async def floor_check(quote_id: str, _u: dict = Depends(require_internal)):
    """margin_rules.margin_floor_ok on a stored quote (the Autopilot send guard), for ops and the s2 agents."""
    q = await db.quotes.find_one({"id": quote_id}, NO_ID)
    if not q:
        raise HTTPException(404, "Quote not found")
    ok, reasons = await mr.margin_floor_ok(q)
    return {"quote_id": quote_id, "ok": ok, "reasons": reasons}


@router.get("/margin-rules/{rule_id}")
async def get_rule(rule_id: str, _u: dict = Depends(require_internal)):
    r = await db.margin_rules.find_one({"id": rule_id}, NO_ID)
    if not r:
        raise HTTPException(404, "Margin rule not found")
    return r


@router.post("/margin-rules")
async def create_rule(body: dict = Body(...), user: dict = Depends(require_perm("settings.write"))):
    try:
        doc = await mr.validate_rule(body)
    except mr.RuleError as exc:
        raise HTTPException(400, str(exc)) from None
    doc.update(id=new_id(), created_at=now_iso(), updated_at=now_iso(), created_by={"id": user["id"], "name": user.get("name")})
    await db.margin_rules.insert_one(dict(doc))
    await audit(user, "margin_rule_created", "margin_rule", doc["id"], {k: doc[k] for k in ("name", "scope", "margin_pct", "floor_cents")})
    return doc


@router.patch("/margin-rules/{rule_id}")
async def update_rule(rule_id: str, body: dict = Body(...), user: dict = Depends(require_perm("settings.write"))):
    r = await db.margin_rules.find_one({"id": rule_id}, NO_ID)
    if not r:
        raise HTTPException(404, "Margin rule not found")
    try:
        doc = await mr.validate_rule(body, existing=r)
    except mr.RuleError as exc:
        raise HTTPException(400, str(exc)) from None
    doc["updated_at"] = now_iso()
    await db.margin_rules.replace_one({"id": rule_id}, dict(doc))
    await audit(user, "margin_rule_updated", "margin_rule", rule_id, {"changed": sorted(body)})
    return doc


@router.delete("/margin-rules/{rule_id}")
async def delete_rule(rule_id: str, user: dict = Depends(require_perm("settings.write"))):
    r = await db.margin_rules.find_one({"id": rule_id}, NO_ID)
    if not r:
        raise HTTPException(404, "Margin rule not found")
    await db.margin_rules.delete_one({"id": rule_id})
    await audit(user, "margin_rule_deleted", "margin_rule", rule_id, {"name": r.get("name"), "scope": r.get("scope")})
    return {"ok": True}


@router.post("/margin-rules/defaults")
async def create_defaults(body: dict = Body(default={}), user: dict = Depends(require_perm("settings.write"))):
    """The documented starting policy (services.margin_rules.DEFAULT_RULES). Idempotent: existing defaults are kept."""
    created, kept = [], []
    for d in mr.DEFAULT_RULES:
        if await db.margin_rules.find_one({"default_key": d["default_key"]}, {"id": 1}):
            kept.append(d["default_key"])
            continue
        doc = await mr.validate_rule({**d, "active": bool(body.get("active", True))})
        doc.update(id=new_id(), default_key=d["default_key"], created_at=now_iso(), updated_at=now_iso(),
                   created_by={"id": user["id"], "name": user.get("name")})
        await db.margin_rules.insert_one(dict(doc))
        created.append(doc)
    await audit(user, "margin_rules_defaults", "margin_rule", None, {"created": [d["default_key"] for d in created], "kept": kept})
    return {"created": created, "kept": kept}


@router.post("/margin-rules/simulate")
async def simulate(body: dict = Body(...), _u: dict = Depends(require_internal)):
    """Price a request (or re-price a stored quote's request) with the current rules, without storing anything, and
    show per option which rule set the margin and what the margin is (stored margin next to it for a quote)."""
    stored = None
    company_id = body.get("company_id")
    if body.get("quote_id"):
        stored = await db.quotes.find_one({"id": body["quote_id"]}, NO_ID)
        if not stored:
            raise HTTPException(404, "Quote not found")
        request = {k: v for k, v in (stored.get("request") or {}).items() if k not in ("cargo_summary", "direction")}
        company_id = company_id or stored.get("company_id")
    elif isinstance(body.get("request"), dict):
        request = body["request"]
    else:
        raise HTTPException(400, "Send request (a quote request) or quote_id.")
    company = await db.companies.find_one({"id": company_id}, NO_ID) if company_id else None
    if company_id and not company:
        raise HTTPException(404, "Company not found")
    try:
        priced = await pricing.quote_pricing(request, company=company, strict=False)
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None
    old = {(o.get("carrier_id"), o.get("service"), o.get("price_basis") or "spot"): o for o in (stored or {}).get("options") or []}
    rows = []
    for o in priced["options"]:
        prev = old.get((o.get("carrier_id"), o.get("service"), o.get("price_basis") or "spot"))
        raised = sum(ln.get("floor_raised_cents") or 0 for ln in o.get("lines") or [])
        rows.append({"option_id": o["id"], "carrier_name": o.get("carrier_name"), "mode": o["mode"], "service": o.get("service"),
                     "price_basis": o.get("price_basis"), "contract_tariff_number": o.get("contract_tariff_number"),
                     "rule_id": o.get("margin_rule_id"), "rule_name": o.get("margin_rule_name"), "source": o.get("margin_source"),
                     "markup_pct": o.get("markup_pct"), "floor_cents": o.get("margin_floor_cents"), "floor_raised_cents": raised,
                     "cost_cents": o.get("cost_cents"), "cost_basis": o.get("cost_basis"), "total_cents": o["total_cents"],
                     "margin_cents": o.get("margin_cents"), "margin_pct": o.get("margin_pct"), "recommended": o.get("recommended"),
                     "stored": {"total_cents": prev.get("total_cents"), "margin_cents": prev.get("margin_cents"),
                                "markup_pct": prev.get("markup_pct")} if prev else None})
    ok, reasons = await mr.margin_floor_ok({"request": priced["request"], "options": priced["options"],
                                            "company_id": (company or {}).get("id")})
    return {"quote_id": (stored or {}).get("id"), "company_id": (company or {}).get("id"), "options": rows,
            "floor_ok": ok, "floor_reasons": reasons, "contract": priced.get("contract"),
            "route": f"{priced['request']['origin'].get('name')} → {priced['request']['destination'].get('name')}",
            "cargo_summary": priced["request"].get("cargo_summary")}


# ------------------------------------------------------------------------------------------------
# Contract tariffs
# ------------------------------------------------------------------------------------------------
@router.get("/tariffs")
async def list_tariffs(company_id: str | None = None, status: str | None = Query(None, pattern="^(draft|active|expired|superseded)$"),
                       _u: dict = Depends(require_perm("quotes.write"))):
    q = {k: v for k, v in (("company_id", company_id), ("status", status)) if v}
    rows = await db.customer_tariffs.find(q, NO_ID).sort("created_at", -1).to_list(500)
    names = {c["id"]: c.get("name") for c in await db.companies.find({"id": {"$in": list({r["company_id"] for r in rows})}},
                                                                    {"_id": 0, "id": 1, "name": 1}).to_list(None)}
    for r in rows:
        r["company_name"] = names.get(r["company_id"])
        r["lane_count"] = len(r.get("lanes") or [])
    return {"tariffs": rows}


@router.post("/tariffs")
async def create_tariff(body: dict = Body(...), user: dict = Depends(require_perm("quotes.write"))):
    try:
        return await ct.create_tariff(body, user, source="manual")
    except ct.TariffError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/tariffs/expire-sweep")
async def expire_sweep(_u: dict = Depends(require_perm("settings.write"))):
    return await ct.expire_sweep()


@router.get("/tariffs/{tariff_id}")
async def get_tariff(tariff_id: str, _u: dict = Depends(require_perm("quotes.write"))):
    t = await db.customer_tariffs.find_one({"id": tariff_id}, NO_ID)
    if not t:
        raise HTTPException(404, "Contract tariff not found")
    return t


@router.patch("/tariffs/{tariff_id}")
async def update_tariff(tariff_id: str, body: dict = Body(...), user: dict = Depends(require_perm("quotes.write"))):
    t = await db.customer_tariffs.find_one({"id": tariff_id}, NO_ID)
    if not t:
        raise HTTPException(404, "Contract tariff not found")
    if t["status"] in ("expired", "superseded"):
        raise HTTPException(400, f"This tariff is {t['status']}; make a new draft.")
    try:
        doc = await ct.validate_tariff(body, existing=t)
    except ct.TariffError as exc:
        raise HTTPException(400, str(exc)) from None
    doc["updated_at"] = now_iso()
    await db.customer_tariffs.replace_one({"id": tariff_id}, dict(doc))
    await audit(user, "tariff_updated", "customer_tariff", tariff_id, {"changed": sorted(body)}, company_id=t["company_id"])
    return doc


@router.post("/tariffs/{tariff_id}/activate")
async def activate_tariff(tariff_id: str, user: dict = Depends(require_perm("quotes.write"))):
    return await ct.activate(tariff_id, user)


@router.post("/tariffs/{tariff_id}/expire")
async def expire_tariff(tariff_id: str, user: dict = Depends(require_perm("quotes.write"))):
    return await ct.expire(tariff_id, user)
