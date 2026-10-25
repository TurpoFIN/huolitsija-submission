"""Beneficial owners, corporate parents and PEP checks (s12). Ops only; the customer sees its KYC checklist instead.

    POST /api/compliance/pep-check                      {name, country?, birth_date?, refresh?}   kyc.approve
    GET  /api/compliance/companies/{id}/owners          last registry result (computed once if missing)
                                                        kyc.approve or customers.write
    POST /api/compliance/companies/{id}/owners/refresh  registries + PEP check of every person   kyc.approve

Job `compliance.pep_refresh` (weekly; daily trigger at 04:40 Helsinki that runs when the last full run is >= 6.5 days
old) re-checks the owners and persons of every active company. A new, unapproved PEP / family-member hit re-runs KYC,
which puts the company in review with a `pep_relationship` card. Kill switch respected; every run audit-logged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.audit import agent_actor, audit
from core.compliance_jobs import autopilot_halted, recurring
from core.db import db
from core.deps import current_user, require_perm
from core.util import NO_ID, now_iso
from services import beneficial_owners, pep

router = APIRouter(prefix="/api", tags=["compliance"])


def _ops_reader(user: dict = Depends(current_user)) -> dict:
    perms = set(user.get("permissions") or [])
    if user.get("org_type") != "internal" or not ({"kyc.approve", "customers.write"} & perms):
        raise HTTPException(403, "Missing permission: kyc.approve or customers.write")
    return user


class PepCheckIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    country: str | None = Field(default=None, max_length=60)
    birth_date: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")
    refresh: bool = False


@router.post("/compliance/pep-check")
async def pep_check(body: PepCheckIn, user: dict = Depends(require_perm("kyc.approve"))):
    try:
        res = await pep.check_person(body.name, country=body.country, birth_date=body.birth_date, refresh=body.refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await audit(user, "pep_check", "person", None, {"name": body.name, "country": body.country, "status": res["status"],
                                                     "matches": pep.match_keys(res)})
    return res


async def _company(company_id: str) -> dict:
    c = await db.companies.find_one({"id": company_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Company not found")
    return c


async def refresh_company(company: dict, actor: dict | None, *, refresh_pep: bool = False) -> dict:
    owners = await beneficial_owners.owners_for_company(company)
    persons = pep.kyc_persons(company, owners)
    for p in persons:
        r = await pep.check_person(p["name"], country=p.get("country"), birth_date=p.get("birth_date"), refresh=refresh_pep,
                                   persona_business_id=company.get("business_id"))
        p.update(status=r["status"], matches=r["matches"], sources=r["sources"], checked_at=r["checked_at"])
    owners["persons"] = persons
    await db.companies.update_one({"id": company["id"]}, {"$set": {"beneficial_owners": owners}})
    await audit(actor, "owners_refreshed", "company", company["id"],
                {"sources": [(s["source"], s["status"]) for s in owners["sources_checked"]],
                 "pep": [(p["name"], p["status"]) for p in persons]}, company_id=company["id"])
    return owners


@router.get("/compliance/companies/{company_id}/owners")
async def company_owners(company_id: str, user: dict = Depends(_ops_reader)):
    c = await _company(company_id)
    if c.get("beneficial_owners"):
        return {"company_id": company_id, **c["beneficial_owners"]}
    return {"company_id": company_id, **await refresh_company(c, user)}


@router.post("/compliance/companies/{company_id}/owners/refresh")
async def company_owners_refresh(company_id: str, user: dict = Depends(require_perm("kyc.approve"))):
    c = await _company(company_id)
    return {"company_id": company_id, **await refresh_company(c, user, refresh_pep=True)}


def _new_hits(company: dict, persons: list[dict]) -> list[str]:
    approved = set(((company.get("kyc") or {}).get("pep_decision") or {}).get("approved") or [])
    seen = set(((company.get("kyc") or {}).get("pep") or {}).get("hit_keys") or [])
    keys = {f"{' '.join(sorted(pep._norm(p['name']).split()))}|{k}" for p in persons if p["status"] in ("pep", "rca")
            for k in pep.match_keys(p)}
    return sorted(keys - approved - seen)


@recurring("compliance.pep_refresh", daily_at="04:40", label_fi="PEP- ja omistajatietojen viikkotarkistus",
           label_en="Weekly PEP and owner re-check")
async def pep_refresh(j: dict | None = None) -> dict:
    from services import kyc

    state = await db.compliance_state.find_one({"name": "pep_refresh"}, NO_ID) or {}
    last = state.get("last_full_at")
    force = bool(((j or {}).get("payload") or {}).get("force"))
    if not force and last and datetime.now(timezone.utc) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) < timedelta(days=6.5):
        return {"skipped": "weekly", "last_full_at": last}
    actor = agent_actor("compliance")
    done, rerun, halted = 0, [], False
    async for c in db.companies.find({"status": {"$nin": ["blocked", "closed", "deleted"]},
                                      "kyc_status": {"$in": ["approved", "in_review"]}}, NO_ID):
        if await autopilot_halted():
            halted = True
            break
        owners = await refresh_company(c, actor, refresh_pep=True)
        done += 1
        new = _new_hits(c, owners["persons"])
        if new:
            try:
                await kyc.run_kyc(c["id"], actor)  # puts the company in review with the pep_relationship card
                rerun.append({"company_id": c["id"], "new_hits": new})
            except HTTPException as exc:
                rerun.append({"company_id": c["id"], "new_hits": new, "error": exc.detail})
    res = {"companies": done, "kyc_rerun": rerun, "halted": halted, "finished_at": now_iso()}
    if not halted:
        await db.compliance_state.update_one({"name": "pep_refresh"}, {"$set": {"last_full_at": now_iso(), "result": res}},
                                             upsert=True)
    await audit(actor, "pep_refresh", "compliance", None, res)
    return res
