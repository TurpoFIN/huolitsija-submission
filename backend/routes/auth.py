"""Sign-up, sign-in, e-mail verification, password reset, invitations, profile, notifications."""
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from core import config
from core.audit import audit
from core.db import db
from core.deps import PUBLIC_USER_FIELDS, current_user, get_optional_user
from core.email import render, send_email
from core.events import emit
from core.permissions import ROLE_LABELS, org_type_for_role, permissions_for
from core.security import COOKIE_NAME, create_session_token, hash_password, random_token, verify_password
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from core.web import base_url

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    company_name: str = Field(min_length=2, max_length=200)
    locale: str = "fi"
    accept_terms: bool = False
    test_tenant: bool = False


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenIn(BaseModel):
    token: str


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: str = Field(min_length=10, max_length=200)


class InviteAcceptIn(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=200)


class ProfileIn(BaseModel):
    name: str | None = None
    locale: str | None = None
    phone: str | None = None
    notify_email: bool | None = None
    theme: str | None = None


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


def _set_cookie(response: Response, user: dict):
    token = create_session_token(user["id"], int(user.get("session_version", 0)))
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=config.COOKIE_SECURE, samesite="lax",
                        max_age=config.SESSION_DAYS * 86400, path="/")
    return token


def public_user(user: dict) -> dict:
    u = {k: v for k, v in user.items() if k not in ("_id", "password_hash")}
    u["permissions"] = sorted(permissions_for(u))
    u["role_label"] = ROLE_LABELS.get(u.get("role"), u.get("role"))
    return u


async def _issue_token(kind: str, user_id: str, hours: int, data: dict | None = None) -> str:
    token = random_token()
    await db.tokens.insert_one({"token": token, "kind": kind, "user_id": user_id, "data": data or {},
                                "expires_at": iso(now() + timedelta(hours=hours)), "used": False,
                                "created_at": now_iso()})
    return token


async def _use_token(token: str, kind: str) -> dict:
    t = await db.tokens.find_one({"token": token, "kind": kind}, NO_ID)
    if not t or t.get("used") or parse_iso(t["expires_at"]) < now():
        raise HTTPException(400, "This link is invalid or has expired")
    await db.tokens.update_one({"token": token}, {"$set": {"used": True, "used_at": now_iso()}})
    return t


async def send_verification(user: dict, request: Request | None):
    token = await _issue_token("verify_email", user["id"], 72)
    url = f"{base_url(request)}/verify-email?token={token}"
    fi = user.get("locale") == "fi"
    title = "Vahvista sähköpostiosoitteesi" if fi else "Confirm your e-mail address"
    lines = (["Vahvista osoitteesi, niin voit varata kuljetuksia Huolitsijassa."] if fi
             else ["Confirm your address so you can book shipments with Huolitsija."])
    html, text = render(title, lines, url, "Vahvista" if fi else "Confirm e-mail")
    await send_email(user["email"], title, html, text, tag="verify_email", company_id=user.get("company_id"))


@router.post("/signup")
async def signup(body: SignupIn, request: Request, response: Response):
    email = body.email.lower().strip()
    if not body.accept_terms:
        raise HTTPException(400, "Accept the terms (NSAB 2015) and privacy notice to continue")
    if await db.users.find_one({"email": email}):
        raise HTTPException(409, "An account with this e-mail already exists")
    if not re.search(r"[A-Za-z]", body.password) or not re.search(r"\d", body.password):
        raise HTTPException(400, "Use at least 10 characters with letters and numbers")
    company = {
        "id": new_id(), "name": body.company_name.strip(), "country": "FI", "business_id": None, "vat_id": None,
        "eori": None, "address": {}, "kyc_status": "not_started", "sanctions_status": "not_screened",
        "test_tenant": body.test_tenant or bool(re.match(r"^(e2e[.+]|test[.+]|kuvatesti[.+])", email)),
        "credit_limit_cents": 0, "payment_terms_days": config.DEFAULT_PAYMENT_TERMS_DAYS,
        "billing_email": email, "created_at": now_iso(), "created_by": None, "status": "active",
    }
    user = {
        "id": new_id(), "email": email, "name": body.name.strip(), "password_hash": hash_password(body.password),
        "role": "customer_admin", "org_type": "customer", "company_id": company["id"],
        "locale": body.locale if body.locale in ("fi", "en") else "fi", "email_verified": False,
        "session_version": 0, "created_at": now_iso(), "notify_email": True, "terms_accepted_at": now_iso(),
        "terms_version": "1.1", "privacy_version": "1.1",
    }
    company["created_by"] = user["id"]
    await db.companies.insert_one(dict(company))
    await db.users.insert_one(dict(user))
    await audit(user, "signup", "company", company["id"], {"email": email}, company_id=company["id"])
    await send_verification(user, request)
    token = _set_cookie(response, user)
    await emit("company.created", {"company": company, "actor": user})
    return {"user": public_user(user), "token": token}


@router.post("/login")
async def login(body: LoginIn, response: Response):
    user = await db.users.find_one({"email": body.email.lower().strip()}, {"_id": 0})
    if not user or not verify_password(body.password, user.get("password_hash")):
        raise HTTPException(401, "E-mail or password is wrong")
    if user.get("disabled"):
        raise HTTPException(403, "This account is disabled")
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_login_at": now_iso()}})
    token = _set_cookie(response, user)
    return {"user": public_user(user), "token": token}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict | None = Depends(get_optional_user)):
    if not user:
        return {"user": None}
    company = None
    if user.get("company_id"):
        company = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
    return {"user": public_user(user), "company": company}


@router.post("/verify-email")
async def verify_email(body: TokenIn):
    t = await _use_token(body.token, "verify_email")
    await db.users.update_one({"id": t["user_id"]}, {"$set": {"email_verified": True, "email_verified_at": now_iso()}})
    return {"ok": True}


@router.post("/resend-verification")
async def resend_verification(request: Request, user: dict = Depends(current_user)):
    if user.get("email_verified"):
        return {"ok": True, "already_verified": True}
    await send_verification(user, request)
    return {"ok": True}


@router.post("/forgot-password")
async def forgot_password(body: ForgotIn, request: Request):
    user = await db.users.find_one({"email": body.email.lower().strip()}, PUBLIC_USER_FIELDS)
    if user:
        token = await _issue_token("reset_password", user["id"], 2)
        url = f"{base_url(request)}/reset-password?token={token}"
        fi = user.get("locale") == "fi"
        title = "Salasanan vaihto" if fi else "Reset your password"
        lines = ["Linkki on voimassa 2 tuntia."] if fi else ["The link is valid for 2 hours."]
        html, text = render(title, lines, url, "Vaihda salasana" if fi else "Reset password")
        await send_email(user["email"], title, html, text, tag="reset_password")
    return {"ok": True}  # same answer whether or not the account exists


@router.post("/reset-password")
async def reset_password(body: ResetIn, response: Response):
    t = await _use_token(body.token, "reset_password")
    await db.users.update_one({"id": t["user_id"]}, {"$set": {"password_hash": hash_password(body.password)},
                                                       "$inc": {"session_version": 1}})
    user = await db.users.find_one({"id": t["user_id"]}, {"_id": 0})
    _set_cookie(response, user)
    return {"user": public_user(user)}


@router.get("/invite/{token}")
async def invite_info(token: str):
    t = await db.tokens.find_one({"token": token, "kind": "invite"}, NO_ID)
    if not t or t.get("used") or parse_iso(t["expires_at"]) < now():
        raise HTTPException(404, "This invitation is invalid or has expired")
    d = t.get("data", {})
    return {"email": d.get("email"), "role": d.get("role"), "company_name": d.get("company_name"),
            "invited_by": d.get("invited_by_name")}


@router.post("/accept-invite")
async def accept_invite(body: InviteAcceptIn, response: Response):
    t = await _use_token(body.token, "invite")
    d = t["data"]
    if await db.users.find_one({"email": d["email"]}):
        raise HTTPException(409, "An account with this e-mail already exists")
    user = {
        "id": new_id(), "email": d["email"], "name": body.name.strip(), "password_hash": hash_password(body.password),
        "role": d["role"], "org_type": org_type_for_role(d["role"]), "company_id": d.get("company_id"),
        "carrier_id": d.get("carrier_id"), "locale": d.get("locale", "fi"), "email_verified": True,
        "session_version": 0, "created_at": now_iso(), "notify_email": True, "invited_by": d.get("invited_by"),
    }
    await db.users.insert_one(dict(user))
    await audit(user, "invite_accepted", "user", user["id"], {"role": user["role"]}, company_id=user.get("company_id"))
    _set_cookie(response, user)
    return {"user": public_user(user)}


@router.patch("/profile")
async def update_profile(body: ProfileIn, user: dict = Depends(current_user)):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "locale" in changes and changes["locale"] not in ("fi", "en"):
        raise HTTPException(400, "Unsupported language")
    if changes:
        await db.users.update_one({"id": user["id"]}, {"$set": changes})
    fresh = await db.users.find_one({"id": user["id"]}, PUBLIC_USER_FIELDS)
    return {"user": public_user(fresh)}


@router.post("/change-password")
async def change_password(body: PasswordIn, response: Response, user: dict = Depends(current_user)):
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not verify_password(body.current_password, full.get("password_hash")):
        raise HTTPException(400, "Current password is wrong")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(body.new_password)},
                                                    "$inc": {"session_version": 1}})
    full["session_version"] = int(full.get("session_version", 0)) + 1
    _set_cookie(response, full)
    return {"ok": True}


# ---- notifications (bell) ---------------------------------------------------------------
notif_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@notif_router.get("")
async def list_notifications(limit: int = 30, user: dict = Depends(current_user)):
    items = await db.notifications.find({"user_id": user["id"]}, NO_ID).sort("created_at", -1).to_list(min(limit, 100))
    unread = await db.notifications.count_documents({"user_id": user["id"], "read": False})
    return {"items": items, "unread": unread}


@notif_router.post("/read")
async def mark_read(user: dict = Depends(current_user)):
    await db.notifications.update_many({"user_id": user["id"], "read": False}, {"$set": {"read": True}})
    return {"ok": True}


routers = [router, notif_router]
