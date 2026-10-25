"""Team management: customer admins manage their company's users; ops admins manage staff and partners."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr

from core.audit import audit
from core.db import db
from core.deps import PUBLIC_USER_FIELDS, current_user, require_perm
from core.email import render, send_email
from core.permissions import (CUSTOMER_ROLES, INTERNAL_ROLES, PERMISSIONS, ROLE_LABELS, ROLE_PERMISSIONS,
                              org_type_for_role)
from core.util import NO_ID, now_iso
from core.web import base_url
from routes.auth import _issue_token, public_user

router = APIRouter(prefix="/api", tags=["team"])


class InviteIn(BaseModel):
    email: EmailStr
    role: str
    carrier_id: str | None = None
    company_id: str | None = None
    locale: str = "fi"


class RoleIn(BaseModel):
    role: str | None = None
    disabled: bool | None = None


async def _send_invite(request: Request, inviter: dict, email: str, role: str, company: dict | None,
                       carrier_id: str | None, locale: str):
    if await db.users.find_one({"email": email}):
        raise HTTPException(409, "A user with this e-mail already exists")
    data = {"email": email, "role": role, "company_id": company["id"] if company else None,
            "company_name": company["name"] if company else "Huolitsija", "carrier_id": carrier_id,
            "invited_by": inviter["id"], "invited_by_name": inviter.get("name"), "locale": locale}
    token = await _issue_token("invite", inviter["id"], 24 * 7, data)
    url = f"{base_url(request)}/invite/{token}"
    fi = locale == "fi"
    org = data["company_name"]
    title = f"Kutsu: {org} Huolitsijassa" if fi else f"You're invited to {org} on Huolitsija"
    lines = [f"{inviter.get('name')} kutsui sinut ({ROLE_LABELS.get(role, role)})." if fi
             else f"{inviter.get('name')} invited you as {ROLE_LABELS.get(role, role)}."]
    html, text = render(title, lines, url, "Hyväksy kutsu" if fi else "Accept invitation")
    await send_email(email, title, html, text, tag="invite", company_id=data["company_id"])
    await db.invites.insert_one({**data, "token": token, "status": "pending", "created_at": now_iso()})
    await audit(inviter, "invite_sent", "user", None, {"email": email, "role": role}, company_id=data["company_id"])
    return {"ok": True, "email": email, "role": role}


# ---- customer company team --------------------------------------------------------------
@router.get("/team")
async def my_team(user: dict = Depends(current_user)):
    if user.get("org_type") != "customer":
        raise HTTPException(403, "Customer accounts only")
    users = await db.users.find({"company_id": user["company_id"]}, PUBLIC_USER_FIELDS).sort("created_at", 1).to_list(500)
    invites = await db.invites.find({"company_id": user["company_id"], "status": "pending"},
                                    {"_id": 0, "token": 0}).to_list(100)
    return {"users": [public_user(u) for u in users], "invites": invites,
            "roles": [{"id": r, "label": ROLE_LABELS[r]} for r in CUSTOMER_ROLES]}


@router.post("/team/invite")
async def invite_team(body: InviteIn, request: Request, user: dict = Depends(require_perm("company.manage"))):
    if body.role not in CUSTOMER_ROLES:
        raise HTTPException(400, "Unknown role")
    company = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
    return await _send_invite(request, user, body.email.lower(), body.role, company, None, body.locale)


@router.patch("/team/{user_id}")
async def update_team_member(user_id: str, body: RoleIn, user: dict = Depends(require_perm("company.manage"))):
    target = await db.users.find_one({"id": user_id, "company_id": user["company_id"]}, NO_ID)
    if not target:
        raise HTTPException(404, "User not found")
    changes = {}
    if body.role is not None:
        if body.role not in CUSTOMER_ROLES:
            raise HTTPException(400, "Unknown role")
        changes["role"] = body.role
    if body.disabled is not None:
        if user_id == user["id"]:
            raise HTTPException(400, "You cannot disable yourself")
        changes["disabled"] = body.disabled
    if changes.get("role") == "customer_user" or changes.get("disabled"):
        admins = await db.users.count_documents({"company_id": user["company_id"], "role": "customer_admin",
                                                 "disabled": {"$ne": True}, "id": {"$ne": user_id}})
        if admins == 0:
            raise HTTPException(400, "The company needs at least one active admin")
    await db.users.update_one({"id": user_id}, {"$set": changes, "$inc": {"session_version": 1 if changes.get("disabled") else 0}})
    await audit(user, "team_member_updated", "user", user_id, changes, company_id=user["company_id"])
    return {"ok": True}


# ---- internal staff and partners ----------------------------------------------------------
@router.get("/ops/users")
async def ops_users(user: dict = Depends(require_perm("users.manage"))):
    users = await db.users.find({"org_type": {"$in": ["internal", "partner"]}}, PUBLIC_USER_FIELDS).sort("created_at", 1).to_list(1000)
    invites = await db.invites.find({"company_id": None, "status": "pending"}, {"_id": 0, "token": 0}).to_list(200)
    return {"users": [public_user(u) for u in users], "invites": invites}


@router.post("/ops/users/invite")
async def ops_invite(body: InviteIn, request: Request, user: dict = Depends(require_perm("users.manage"))):
    if body.role not in INTERNAL_ROLES + ["partner"]:
        raise HTTPException(400, "Unknown role")
    if body.role == "partner" and not body.carrier_id:
        raise HTTPException(400, "Choose the carrier this partner user works for")
    return await _send_invite(request, user, body.email.lower(), body.role, None, body.carrier_id, body.locale)


@router.patch("/ops/users/{user_id}")
async def ops_update_user(user_id: str, body: RoleIn, user: dict = Depends(require_perm("users.manage"))):
    target = await db.users.find_one({"id": user_id, "org_type": {"$in": ["internal", "partner"]}}, NO_ID)
    if not target:
        raise HTTPException(404, "User not found")
    changes = {}
    if body.role is not None:
        if org_type_for_role(body.role) != target["org_type"]:
            raise HTTPException(400, "Role does not fit this account type")
        changes["role"] = body.role
    if body.disabled is not None:
        if user_id == user["id"]:
            raise HTTPException(400, "You cannot disable yourself")
        changes["disabled"] = body.disabled
    await db.users.update_one({"id": user_id}, {"$set": changes, "$inc": {"session_version": 1}})
    await audit(user, "staff_updated", "user", user_id, changes)
    return {"ok": True}


@router.get("/ops/roles")
async def roles_matrix(user: dict = Depends(require_perm("users.manage"))):
    return {
        "permissions": [{"id": k, "label": v} for k, v in PERMISSIONS.items()],
        "roles": [{"id": r, "label": ROLE_LABELS[r], "org_type": org_type_for_role(r),
                   "permissions": ROLE_PERMISSIONS.get(r, [])} for r in ROLE_PERMISSIONS],
    }
