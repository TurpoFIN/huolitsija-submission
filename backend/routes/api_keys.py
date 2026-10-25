"""Portal management of API keys and webhook endpoints (customer admins), plus an ops overview.

    GET    /api/portal/api-keys                 keys, scope catalogue, webhook endpoints, recent deliveries
    POST   /api/portal/api-keys                 {name, scopes[]} -> {key, secret}   (secret shown once)
    DELETE /api/portal/api-keys/{id}            revoke
    POST   /api/portal/webhooks                 {url, events[], description} -> {endpoint, secret}
    DELETE /api/portal/webhooks/{id}
    POST   /api/portal/webhooks/{id}/test       signed ping, delivered at once
    GET    /api/ops/api-keys?company_id=        ops: every company's keys (no secrets, no hashes)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import api_keys
from core.audit import audit
from core.db import db
from core.deps import get_company, require_customer, require_perm
from services import webhooks_out

router = APIRouter(prefix="/api", tags=["api-keys"])


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str]


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["*"])
    description: str | None = None


async def _admin(user: dict = Depends(require_customer)) -> dict:
    if "company.manage" not in (user.get("permissions") or []):
        raise HTTPException(403, "Only your company's admin can manage API keys and webhooks.")
    if not user.get("company_id"):
        raise HTTPException(400, "Your account is not linked to a company.")
    return user


@router.get("/portal/api-keys")
async def portal_keys(user: dict = Depends(_admin)):
    cid = user["company_id"]
    return {
        "keys": await api_keys.list_keys(cid),
        "scopes": [{"scope": s, "fi": fi, "en": en} for s, (fi, en) in api_keys.SCOPES.items()],
        "webhooks": await webhooks_out.list_endpoints(cid),
        "events": [{"event": e, "fi": fi, "en": en} for e, (fi, en) in webhooks_out.EVENTS.items()],
        "deliveries": await webhooks_out.list_deliveries(cid, limit=20),
        "docs_url": "/api/v1/docs", "openapi_url": "/api/v1/openapi.json",
        "rate_limit_per_min": api_keys.DEFAULT_RATE_LIMIT,
    }


@router.post("/portal/api-keys")
async def portal_create_key(body: KeyIn, user: dict = Depends(_admin)):
    company = await get_company(user["company_id"])
    if not company:
        raise HTTPException(400, "Your account is not linked to a company.")
    key, secret = await api_keys.issue_key(company, name=body.name, scopes=body.scopes, created_by=user)
    await audit(user, "api_key_created", "api_key", key["id"], {"name": key["name"], "prefix": key["prefix"], "scopes": key["scopes"]},
                company_id=company["id"])
    return {"key": key, "secret": secret}


@router.delete("/portal/api-keys/{key_id}")
async def portal_revoke_key(key_id: str, user: dict = Depends(_admin)):
    key = await api_keys.revoke_key(user["company_id"], key_id, user)
    await audit(user, "api_key_revoked", "api_key", key_id, {"prefix": key.get("prefix")}, company_id=user["company_id"])
    return {"key": key}


@router.post("/portal/webhooks")
async def portal_create_webhook(body: WebhookIn, user: dict = Depends(_admin)):
    ep, secret = await webhooks_out.create_endpoint(user["company_id"], url=body.url, events=body.events,
                                                    description=body.description, created_by=user)
    await audit(user, "webhook_endpoint_created", "webhook_endpoint", ep["id"], {"url": ep["url"], "events": ep["events"]},
                company_id=user["company_id"])
    return {"endpoint": ep, "secret": secret}


@router.delete("/portal/webhooks/{endpoint_id}")
async def portal_delete_webhook(endpoint_id: str, user: dict = Depends(_admin)):
    ep = await webhooks_out.delete_endpoint(user["company_id"], endpoint_id)
    await audit(user, "webhook_endpoint_deleted", "webhook_endpoint", endpoint_id, {"url": ep["url"]}, company_id=user["company_id"])
    return {"endpoint": ep}


@router.post("/portal/webhooks/{endpoint_id}/test")
async def portal_test_webhook(endpoint_id: str, user: dict = Depends(_admin)):
    await webhooks_out.get_endpoint(user["company_id"], endpoint_id)
    rows = await webhooks_out.enqueue(user["company_id"], "ping", {"message": "Huolitsija webhook test"}, only_endpoint=endpoint_id)
    return {"delivery": await webhooks_out.deliver_now(rows[0]["id"])}


@router.get("/ops/api-keys")
async def ops_keys(company_id: str | None = None, user: dict = Depends(require_perm("customers.write"))):
    q = {"company_id": company_id} if company_id else {}
    keys = await db.api_keys.find(q, api_keys.PUBLIC_FIELDS).sort("created_at", -1).to_list(500)
    names = {c["id"]: c.get("name") async for c in db.raw.companies.find({"id": {"$in": list({k["company_id"] for k in keys})}},
                                                                           {"_id": 0, "id": 1, "name": 1})}
    for k in keys:
        k["company_name"] = names.get(k["company_id"])
    return {"items": keys, "total": len(keys)}
