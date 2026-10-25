"""FastAPI dependencies: current user, role and permission guards."""
from fastapi import Depends, HTTPException, Request

from core.db import db
from core.permissions import permissions_for
from core.security import COOKIE_NAME, decode_session_token
from core.util import NO_ID

PUBLIC_USER_FIELDS = {"_id": 0, "password_hash": 0}


async def get_optional_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload:
        return None
    user = await db.users.find_one({"id": payload.get("sub")}, PUBLIC_USER_FIELDS)
    if not user or user.get("disabled"):
        return None
    if int(user.get("session_version", 0)) != int(payload.get("sv", 0)):
        return None
    user["permissions"] = sorted(permissions_for(user))
    return user


async def current_user(request: Request, user: dict | None = Depends(get_optional_user)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    from core.tenant_visibility import hidden_test_companies
    hidden_test_companies.set(None)
    if is_internal(user) and request.headers.get("x-include-test-tenants") != "true" and (request.query_params.get("include_test") or "").lower() not in ("true", "1", "yes", "on"):
        companies = await db.companies.find({"$or": [{"test_tenant": True}, {"test": True}]}, {"id": 1}).to_list(None)
        hidden_test_companies.set([c["id"] for c in companies])
    return user


def require_org(*org_types: str):
    async def dep(user: dict = Depends(current_user)) -> dict:
        if user.get("org_type") not in org_types:
            raise HTTPException(status_code=403, detail="Not available for this account")
        return user

    return dep


def require_perm(*perms: str):
    """User must hold every listed permission."""

    async def dep(user: dict = Depends(current_user)) -> dict:
        have = set(user.get("permissions") or [])
        missing = [p for p in perms if p not in have]
        if missing:
            raise HTTPException(status_code=403, detail=f"Missing permission: {', '.join(missing)}")
        return user

    return dep


require_internal = require_org("internal")
require_customer = require_org("customer")
require_partner = require_org("partner")


def is_internal(user: dict) -> bool:
    return user.get("org_type") == "internal"


def company_scope(user: dict) -> dict:
    """Mongo filter that limits a query to what the user may see.

    Internal users see everything; customers see their own company's records; partners see
    nothing by default (partner routes add their own carrier filter)."""
    if is_internal(user):
        return {}
    if user.get("org_type") == "customer":
        return {"company_id": user.get("company_id")}
    return {"company_id": "__none__"}


async def get_company(company_id: str) -> dict | None:
    return await db.companies.find_one({"id": company_id}, NO_ID)
