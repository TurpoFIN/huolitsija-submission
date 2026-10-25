"""Shipper API keys: issue, authenticate, scope and rate-limit machine access to /api/v1.

    key, secret = await issue_key(company, name="ERP", scopes=["quotes:write", "shipments:read"], created_by=user)
    # secret = "huo_live_3f9a1c2b_<43 chars>" is returned once; only its SHA-256 is stored.

    @router.get("/api/v1/shipments")
    async def shipments(ctx: ApiContext = Depends(require_scope("shipments:read"))): ...

A request authenticates with `Authorization: Bearer huo_…` or `X-API-Key: huo_…`. The API then acts as the key's
company exactly like a customer user of that company (same scoping, same views without cost fields), with
permissions derived from the key's scopes. Every request counts against the key's fixed 60-second window
(`api_rate_windows`, TTL-cleaned); responses carry X-RateLimit-Limit / -Remaining / -Reset and a 429 with
Retry-After when the window is used up.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from pymongo import ReturnDocument

from core import config
from core.db import db, register_index
from core.tenant_visibility import hidden_test_companies
from core.util import new_id, now_iso, parse_iso

register_index("api_keys", "id", unique=True)
register_index("api_keys", "hash", unique=True)
register_index("api_keys", [("company_id", 1), ("created_at", -1)])
register_index("api_rate_windows", "expires_at", expireAfterSeconds=0)

# scope -> (Finnish, English) description. Order is the order the portal shows them.
SCOPES: dict[str, tuple[str, str]] = {
    "quotes:read": ("Lue tarjoukset", "Read quotes"),
    "quotes:write": ("Pyydä tarjouksia", "Request quotes"),
    "bookings:write": ("Varaa kuljetuksia tarjouksesta", "Book shipments from a quote"),
    "shipments:read": ("Lue lähetykset", "Read shipments"),
    "tracking:read": ("Lue seurantatapahtumat", "Read tracking events"),
    "documents:write": ("Lähetä asiakirjoja", "Upload documents"),
    "invoices:read": ("Lue laskut", "Read invoices"),
    "webhooks:manage": ("Hallitse webhookeja", "Manage webhooks"),
}
# The customer permission each scope stands in for when the API calls the portal's own logic.
SCOPE_PERMISSIONS = {"quotes:write": "company.book", "bookings:write": "company.book", "documents:write": "company.book"}
DEFAULT_RATE_LIMIT = int(config.env("API_RATE_LIMIT_PER_MIN", "120") or 120)
WINDOW_SECONDS = 60
PUBLIC_FIELDS = {"_id": 0, "hash": 0}


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def clean_scopes(scopes: list[str] | None) -> list[str]:
    asked = list(dict.fromkeys(s.strip() for s in scopes or [] if s and s.strip()))
    unknown = [s for s in asked if s not in SCOPES]
    if unknown:
        raise HTTPException(400, f"Unknown scope: {', '.join(unknown)}. Valid scopes: {', '.join(SCOPES)}.")
    if not asked:
        raise HTTPException(400, "Choose at least one scope for the key.")
    return [s for s in SCOPES if s in asked]


async def issue_key(company: dict, *, name: str, scopes: list[str], created_by: dict | None,
                    rate_limit_per_min: int | None = None) -> tuple[dict, str]:
    """Create a key for `company`. Returns (stored doc without the hash, the full secret, shown once)."""
    name = (name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "Give the key a name (for example the system that will use it).")
    env = "test" if (company.get("test_tenant") or company.get("test")) else "live"
    kid = new_id()
    prefix = f"huo_{env}_{secrets.token_hex(4)}"
    secret = f"{prefix}_{secrets.token_urlsafe(32)}"
    doc = {
        "id": kid, "company_id": company["id"], "name": name, "prefix": prefix, "hash": hash_key(secret),
        "scopes": clean_scopes(scopes), "environment": env,
        "rate_limit_per_min": int(rate_limit_per_min or DEFAULT_RATE_LIMIT),
        "created_by": {"id": (created_by or {}).get("id"), "name": (created_by or {}).get("name"),
                       "email": (created_by or {}).get("email")},
        "created_at": now_iso(), "last_used_at": None, "request_count": 0, "revoked_at": None, "revoked_by": None,
    }
    await db.api_keys.insert_one(dict(doc))
    doc.pop("_id", None)
    doc.pop("hash", None)
    return doc, secret


async def list_keys(company_id: str) -> list[dict]:
    return await db.api_keys.find({"company_id": company_id}, PUBLIC_FIELDS).sort("created_at", -1).to_list(200)


async def revoke_key(company_id: str, key_id: str, actor: dict | None) -> dict:
    k = await db.api_keys.find_one_and_update(
        {"id": key_id, "company_id": company_id},
        {"$set": {"revoked_at": now_iso(), "revoked_by": {"id": (actor or {}).get("id"), "name": (actor or {}).get("name")}}},
        projection=PUBLIC_FIELDS, return_document=ReturnDocument.AFTER)
    if not k:
        raise HTTPException(404, "API key not found")
    return k


# ------------------------------------------------------------------------------------------------ request auth
@dataclass
class ApiContext:
    key: dict
    company: dict
    user: dict  # synthetic customer user of the key's company; pass it to the portal's route functions


def presented_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip().startswith("huo_"):
        return auth[7:].strip()
    x = (request.headers.get("x-api-key") or "").strip()
    return x or None


def api_user(key: dict, company: dict) -> dict:
    perms = sorted({SCOPE_PERMISSIONS[s] for s in key.get("scopes") or [] if s in SCOPE_PERMISSIONS})
    owner = key.get("created_by") or {}
    return {
        "id": f"api_key:{key['id']}", "name": f"API: {key.get('name')}", "email": owner.get("email"),
        "org_type": "customer", "role": "customer_user", "company_id": company["id"], "locale": company.get("locale") or "fi",
        "permissions": perms, "api_key_id": key["id"], "notify_email": False,
    }


async def _rate_limit(key: dict, response: Response | None) -> None:
    limit = int(key.get("rate_limit_per_min") or DEFAULT_RATE_LIMIT)
    now_s = int(time.time())
    window = now_s - now_s % WINDOW_SECONDS
    reset = window + WINDOW_SECONDS
    row = await db.api_rate_windows.find_one_and_update(
        {"_id": f"{key['id']}:{window}"},
        {"$inc": {"count": 1}, "$setOnInsert": {"key_id": key["id"],
                                                "expires_at": datetime.fromtimestamp(reset + WINDOW_SECONDS, tz=timezone.utc)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    used = int(row.get("count") or 1)
    headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(max(0, limit - used)),
               "X-RateLimit-Reset": str(reset)}
    if used > limit:
        raise HTTPException(429, f"Rate limit of {limit} requests per minute reached for this key. Retry after {reset - now_s} s.",
                            headers={**headers, "Retry-After": str(max(1, reset - now_s))})
    if response is not None:
        for k, v in headers.items():
            response.headers[k] = v


async def authenticate(request: Request, response: Response | None = None) -> ApiContext:
    secret = presented_key(request)
    if not secret:
        raise HTTPException(401, "Send your API key as 'Authorization: Bearer huo_…' or 'X-API-Key: huo_…'.",
                            headers={"WWW-Authenticate": "Bearer"})
    key = await db.api_keys.find_one({"hash": hash_key(secret)}, {"_id": 0})
    if not key or key.get("revoked_at"):
        raise HTTPException(401, "This API key is not valid or has been revoked.", headers={"WWW-Authenticate": "Bearer"})
    hidden_test_companies.set(None)  # a key sees its own company, test tenant or not
    company = await db.raw.companies.find_one({"id": key["company_id"]}, {"_id": 0})
    if not company or company.get("disabled"):
        raise HTTPException(403, "The company of this API key is not active.")
    await _rate_limit(key, response)
    last = parse_iso(key.get("last_used_at"))
    upd: dict = {"$inc": {"request_count": 1}}
    if not last or (parse_iso(now_iso()) - last) > timedelta(seconds=30):
        upd["$set"] = {"last_used_at": now_iso()}
    await db.api_keys.update_one({"id": key["id"]}, upd)
    key.pop("hash", None)
    return ApiContext(key=key, company=company, user=api_user(key, company))


def require_scope(*scopes: str):
    """Dependency: a valid key holding every listed scope."""

    async def dep(request: Request, response: Response) -> ApiContext:
        ctx = await authenticate(request, response)
        missing = [s for s in scopes if s not in (ctx.key.get("scopes") or [])]
        if missing:
            raise HTTPException(403, f"This API key lacks the scope {', '.join(missing)}. Create a key with it in the portal "
                                     f"(Rajapinta / API).")
        return ctx

    return dep
