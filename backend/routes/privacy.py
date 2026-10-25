"""GDPR requests (public, self-service, ops), legal holds and retention runs (s12).

Public, no sign-in, one answer for every address (no account enumeration):
  POST /api/privacy/requests {type: export|erasure, email, locale}   -> privacy_requests row + e-mailed 6-digit code
  POST /api/privacy/requests/{id}/verify {code}   (5 attempts, 30 min, codes stored as HMAC)
       export  -> signed download token, GET /api/privacy/requests/{id}/download?token=&format=zip|json (24 h)
       erasure -> runs at once under Autopilot (the data subject verified it); with Autopilot off or the kill switch on
                  it waits for an owner-queue card (kind privacy_erasure, perm settings.write)
Signed in (password re-authentication):
  POST /api/privacy/me/export {password}  ·  POST /api/privacy/me/erasure {password, confirm: "POISTA"|"DELETE"}
  GET  /api/privacy/me/requests
Ops (read: settings.write or customers.write; act: settings.write):
  GET /api/privacy/requests · GET /api/privacy/requests/{id} · POST /api/privacy/ops/requests (request received by
  letter/phone, identity checked by ops) · POST /api/privacy/requests/{id}/process
Compliance: GET|POST /api/compliance/legal-holds · POST /api/compliance/legal-holds/{id}/release ·
  GET /api/compliance/retention/policy · GET /api/compliance/retention/runs · POST /api/compliance/retention/run {dry_run}
Export answers within one month (art. 12(3)): every row records `due_at`. Every step is audit-logged with the e-mail
as a hash. Rate limits: per e-mail and per IP (salted hash) per hour in `rate_limits` (TTL).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from pymongo import ReturnDocument

from core import security
from core.approvals import cancel_approvals, request_approval
from core.audit import actor_ref, agent_actor, audit
from core.compliance_jobs import autopilot_halted
from core.db import db, register_index
from core.deps import current_user, require_internal, require_perm
from core.email import render, send_email
from core.events import emit, on
from core.security import COOKIE_NAME, verify_password
from core.settings import get_settings
from core.util import iso, new_id, now, now_iso, parse_iso
from core.web import base_url
from routes.public_quote import client_ip
from services import gdpr, retention

router = APIRouter(prefix="/api/privacy", tags=["privacy"])
compliance_router = APIRouter(prefix="/api/compliance", tags=["compliance"])

register_index("privacy_requests", "id", unique=True)
register_index("privacy_requests", [("email_hash", 1), ("created_at", -1)])
register_index("legal_holds", "id", unique=True)
register_index("legal_holds", [("scope", 1), ("ref_id", 1)])
register_index("retention_runs", [("started_at", -1)])

CODE_MINUTES = 30
MAX_ATTEMPTS = 5
DOWNLOAD_HOURS = 24
LIMIT_EMAIL = int(os.environ.get("PRIVACY_RATE_LIMIT_PER_EMAIL_HOUR", "5"))
LIMIT_IP = int(os.environ.get("PRIVACY_RATE_LIMIT_PER_IP_HOUR", "20"))
TYPES = ("export", "erasure")
HIDDEN = {"_id": 0, "code_hash": 0, "download_jti": 0}
SAME_ANSWER = {
    "status": "verification_sent", "expires_in_minutes": CODE_MINUTES,
    "message_fi": "Jos osoitteeseen liittyy tietoja, lähetimme siihen vahvistuskoodin. Koodi on voimassa 30 minuuttia.",
    "message_en": "We sent a verification code to the address. The code is valid for 30 minutes.",
}


# ------------------------------------------------------------------------------------------------ helpers
def _secret() -> str:
    if not security._secret:  # noqa: SLF001 - loaded at startup by core.security.load_secret
        raise HTTPException(503, "The server is still starting; try again in a moment.")
    return security._secret  # noqa: SLF001


def _code_hash(request_id: str, code: str) -> str:
    return hmac.new(_secret().encode(), f"privacy:{request_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _jti_hash(jti: str) -> str:
    return hashlib.sha256(f"privacy-dl:{jti}".encode()).hexdigest()


def _one_month(dt: datetime) -> datetime:
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=y, month=m, day=day)
        except ValueError:
            continue
    return dt + timedelta(days=30)


async def _limit(bucket: str, ident: str, limit: int):
    t = datetime.now(timezone.utc)
    salt = _secret()[:16]
    key = f"{bucket}:{hashlib.sha256(f'{salt}:{ident}'.encode()).hexdigest()[:32]}:{t.strftime('%Y%m%d%H')}"
    doc = await db.rate_limits.find_one_and_update(
        {"key": key}, {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": t + timedelta(hours=2), "created_at": t}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if doc["count"] > limit:
        raise HTTPException(429, "Too many data-protection requests in the last hour. Try again in an hour.")


async def _auto_ok() -> bool:
    """Autopilot on and the kill switch off: a verified erasure runs without a person."""
    return bool((await get_settings()).get("autopilot_enabled")) and not await autopilot_halted()


def _public_view(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "type", "status", "created_at", "due_at", "verified_at", "completed_at",
                                  "email_masked", "channel")}


async def _new_request(type_: str, email: str, locale: str, *, channel: str, actor: dict | None = None,
                       ip_hash: str | None = None, extra: dict | None = None) -> dict:
    t = now()
    email = email.strip().lower()
    r = {"id": new_id(), "type": type_, "email": email, "email_hash": gdpr.email_hash(email),
         "email_masked": gdpr.mask_email(email), "locale": "en" if locale == "en" else "fi", "channel": channel,
         "status": "awaiting_verification", "attempts": 0, "max_attempts": MAX_ATTEMPTS, "code_hash": None,
         "code_expires_at": None, "download_jti": None, "download_expires_at": None, "downloads": 0, "approval_id": None,
         "result": None, "created_at": iso(t), "due_at": iso(_one_month(t)), "verified_at": None, "verified_by": None,
         "completed_at": None, "ip_hash": ip_hash, "created_by": actor_ref(actor) if actor else None, **(extra or {})}
    await db.privacy_requests.insert_one(dict(r))
    await audit(actor, "privacy_request_created", "privacy_request", r["id"],
                {"type": type_, "channel": channel, "subject_hash": r["email_hash"], "due_at": r["due_at"]})
    r.pop("_id", None)
    return r


async def _mail(r: dict, title_fi: str, title_en: str, lines_fi: list[str], lines_en: list[str], *, tag: str,
                url: str | None = None, label_fi: str = "Avaa", label_en: str = "Open", mask_after: bool = False):
    fi = r.get("locale") != "en"
    title = title_fi if fi else title_en
    html, text = render(title, [x for x in (lines_fi if fi else lines_en) if x], url, label_fi if fi else label_en)
    rec = await send_email(r["email"], title, html, text, tag=tag, ref={"type": "privacy_request", "id": r["id"]})
    if mask_after:  # a mail sent after an erasure must not leave the address behind in the outbox log
        await gdpr.raw("email_outbox").update_one({"id": rec["id"]}, {"$set": {"to": r.get("email_masked")}})
    return rec


async def _send_code(r: dict, request: Request | None):
    code = f"{secrets.randbelow(10 ** 6):06d}"
    expires = iso(now() + timedelta(minutes=CODE_MINUTES))
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"code_hash": _code_hash(r["id"], code),
                                                                    "code_expires_at": expires, "attempts": 0}})
    url = f"{base_url(request)}/privacy/verify?request={r['id']}"
    what_fi = "kopion tiedoistasi" if r["type"] == "export" else "tietojesi poistamisen"
    what_en = "a copy of your data" if r["type"] == "export" else "erasure of your data"
    await _mail(r, "Tietosuojapyynnön vahvistuskoodi", "Your data-protection request code",
                [f"Pyysit {what_fi} Huolitsijasta.", f"Vahvistuskoodi: {code}",
                 f"Koodi on voimassa {CODE_MINUTES} minuuttia ja sen voi käyttää kerran.",
                 "Jos et tehnyt pyyntöä, voit jättää viestin huomiotta."],
                [f"You asked Huolitsija for {what_en}.", f"Verification code: {code}",
                 f"The code is valid for {CODE_MINUTES} minutes and works once.",
                 "If you did not make this request, ignore this e-mail."],
                tag=f"privacy_code_{r['type']}", url=url, label_fi="Vahvista pyyntö", label_en="Confirm the request")
    await audit(None, "privacy_code_sent", "privacy_request", r["id"], {"subject_hash": r["email_hash"]})


def _download_token(r: dict) -> tuple[str, str, str]:
    jti = secrets.token_urlsafe(16)
    exp = now() + timedelta(hours=DOWNLOAD_HOURS)
    token = jwt.encode({"sub": r["id"], "pur": "privacy_export", "jti": jti, "exp": exp}, _secret(), algorithm="HS256")
    return token, _jti_hash(jti), iso(exp)


async def _issue_download(r: dict, actor: dict | None) -> dict:
    token, jti_hash, exp = _download_token(r)
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"status": "ready", "download_jti": jti_hash,
                                                                    "download_expires_at": exp}})
    await audit(actor, "privacy_export_ready", "privacy_request", r["id"], {"expires_at": exp})
    return {"id": r["id"], "type": "export", "status": "ready", "token": token, "expires_at": exp,
            "download_url": f"/api/privacy/requests/{r['id']}/download?token={token}"}


async def execute_erasure(r: dict, actor: dict | None) -> dict:
    locked = await db.privacy_requests.find_one_and_update(
        {"id": r["id"], "status": {"$in": ["verified", "awaiting_approval", "partially_held"]}},
        {"$set": {"status": "processing", "processing_at": now_iso()}}, projection=HIDDEN, return_document=ReturnDocument.AFTER)
    if not locked:
        cur = await db.privacy_requests.find_one({"id": r["id"]}, HIDDEN) or {}
        return {"id": r["id"], "status": cur.get("status"), "result": cur.get("result")}
    try:
        result = await gdpr.erase_subject(locked["email"], actor, request_id=r["id"])
    except Exception:
        await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"status": "verified"}})
        raise
    held = result["status"] == "partially_held"
    sets = {"status": "partially_held" if held else "completed", "result": result, "completed_at": now_iso()}
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": sets})
    if r.get("approval_id"):
        await cancel_approvals(r["id"], "privacy_erasure")
    n_ret = len(result["retained"])
    await _mail(locked, "Tietosi on poistettu", "Your data has been erased",
                ["Poistimme tai anonymisoimme tietosi pyyntösi mukaisesti.",
                 f"Lain vuoksi säilytettäviä tietueita: {n_ret} (kirjanpito, tulli tai oikeudellinen pidätys). Niiden "
                 "käsittelyä on rajoitettu, ja ne poistetaan säilytysajan päätyttyä.",
                 "Tilisi on suljettu, eikä sillä voi enää kirjautua." if result["login_disabled"] else ""],
                ["We erased or anonymised your data as you asked.",
                 f"Records we must keep by law: {n_ret} (bookkeeping, customs or a legal hold). Their processing is "
                 "restricted and they are deleted when the retention period ends.",
                 "Your account is closed and can no longer sign in." if result["login_disabled"] else ""],
                tag="privacy_erasure_done", mask_after=True)
    if not held:
        await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"email": None}})
    await emit("privacy.request_completed", {"request": {**_public_view(locked), "status": sets["status"]},
                                             "result": {"counts": result["counts"], "status": result["status"]}})
    return {"id": r["id"], "type": "erasure", "status": sets["status"], "result": _result_summary(result)}


def _result_summary(res: dict) -> dict:
    return {k: res.get(k) for k in ("status", "counts", "login_disabled", "retained", "held", "erased", "anonymised",
                                    "errors", "finished_at")}


async def _after_verified(r: dict, actor: dict | None, request: Request | None) -> dict:
    if r["type"] == "export":
        return await _issue_download(r, actor)
    if await _auto_ok():
        return await execute_erasure(r, agent_actor("compliance"))
    a = await request_approval(
        "privacy_erasure", "privacy_request", r["id"], f"Tietojen poisto / Erasure request: {r.get('email_masked')}",
        summary="Henkilö on vahvistanut poistopyynnön. Hyväksy, niin tiedot poistetaan; lain vaatimat tiedot säilytetään "
                "rajoitettuina. / The data subject verified the request. Approve to erase; records required by law are "
                "kept restricted.",
        reason="Autopilot is off" if not (await get_settings()).get("autopilot_enabled") else "Kill switch is on",
        requested_by=actor or agent_actor("compliance"), data={"request_id": r["id"], "channel": r.get("channel")},
        link="/ops/compliance?tab=privacy")
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"status": "awaiting_approval", "approval_id": a["id"]}})
    await audit(actor, "privacy_erasure_queued", "privacy_request", r["id"], {"approval_id": a["id"]})
    return {"id": r["id"], "type": "erasure", "status": "awaiting_approval", "approval_id": a["id"]}


@on("approval.decided")
async def _privacy_approval_decided(payload: dict):
    a = payload.get("approval") or {}
    if a.get("kind") != "privacy_erasure":
        return
    r = await db.privacy_requests.find_one({"id": a.get("ref_id")}, {"_id": 0})
    if not r or r["status"] != "awaiting_approval":
        return
    actor = payload.get("actor")
    if a.get("status") == "approved":
        await execute_erasure(r, actor)
        return
    note = (a.get("decision_note") or "").strip()
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"status": "rejected", "completed_at": now_iso(),
                                                                    "rejection_reason": note or None}})
    await audit(actor, "privacy_erasure_rejected", "privacy_request", r["id"], {"note": note})
    await _mail(r, "Poistopyyntöäsi ei toteutettu", "Your erasure request was not carried out",
                [f"Perustelu: {note or 'Tietoja tarvitaan lakisääteisiin velvoitteisiin.'}",
                 "Voit tehdä valituksen Tietosuojavaltuutetun toimistolle (tietosuoja.fi)."],
                [f"Reason: {note or 'The data is needed for legal obligations.'}",
                 "You can complain to the Office of the Data Protection Ombudsman (tietosuoja.fi)."],
                tag="privacy_erasure_rejected")


async def _bundle_response(email: str, fmt: str) -> Response:
    bundle = await gdpr.export_subject(email)
    if fmt == "json":
        import json

        return Response(json.dumps(bundle, ensure_ascii=False), media_type="application/json")
    data = await gdpr.export_zip(email, bundle)
    stamp = now().strftime("%Y%m%d")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="huolitsija-tietosi-{stamp}.zip"'})


# ------------------------------------------------------------------------------------------------ public flow
class RequestIn(BaseModel):
    type: str
    email: EmailStr
    locale: str = "fi"


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=12)


@router.post("/requests")
async def create_request(body: RequestIn, request: Request):
    if body.type not in TYPES:
        raise HTTPException(400, "Request type must be export or erasure.")
    email = str(body.email).strip().lower()
    ip = client_ip(request)
    await _limit("privacy_ip", ip, LIMIT_IP)
    await _limit("privacy_email", email, LIMIT_EMAIL)
    ip_hash = hashlib.sha256(f"{_secret()[:16]}:{ip}".encode()).hexdigest()[:32]
    r = await _new_request(body.type, email, body.locale, channel="public", ip_hash=ip_hash)
    await _send_code(r, request)
    return {"id": r["id"], "type": r["type"], **SAME_ANSWER, "due_at": r["due_at"]}


@router.post("/requests/{request_id}/verify")
async def verify_request(request_id: str, body: CodeIn, request: Request):
    r = await db.privacy_requests.find_one({"id": request_id}, {"_id": 0})
    if not r or r.get("channel") != "public":
        raise HTTPException(404, "Request not found.")
    if r["status"] == "locked":
        raise HTTPException(429, "Too many wrong codes. Make a new request.")
    if r["status"] != "awaiting_verification" or not r.get("code_hash"):
        raise HTTPException(409, "This request is already verified or closed.")
    if parse_iso(r["code_expires_at"]) < now():
        await db.privacy_requests.update_one({"id": request_id}, {"$set": {"status": "expired", "code_hash": None}})
        await audit(None, "privacy_code_expired", "privacy_request", request_id, {})
        raise HTTPException(410, "The code has expired. Make a new request.")
    code = "".join(ch for ch in body.code if ch.isdigit())
    if not hmac.compare_digest(_code_hash(request_id, code), r["code_hash"]):
        upd = await db.privacy_requests.find_one_and_update(
            {"id": request_id, "status": "awaiting_verification", "attempts": {"$lt": MAX_ATTEMPTS}},
            {"$inc": {"attempts": 1}}, projection={"_id": 0, "attempts": 1}, return_document=ReturnDocument.AFTER)
        attempts = (upd or {}).get("attempts", MAX_ATTEMPTS)
        await audit(None, "privacy_verify_failed", "privacy_request", request_id, {"attempts": attempts})
        if attempts >= MAX_ATTEMPTS:
            await db.privacy_requests.update_one({"id": request_id}, {"$set": {"status": "locked", "code_hash": None,
                                                                              "locked_at": now_iso()}})
            await audit(None, "privacy_request_locked", "privacy_request", request_id, {"attempts": attempts})
            raise HTTPException(429, "Too many wrong codes. Make a new request.")
        raise HTTPException(400, f"The code is wrong. {MAX_ATTEMPTS - attempts} attempts left.")
    won = await db.privacy_requests.find_one_and_update(
        {"id": request_id, "status": "awaiting_verification", "code_hash": r["code_hash"]},
        {"$set": {"status": "verified", "verified_at": now_iso(), "verified_by": "email_code", "code_hash": None}},
        projection={"_id": 0}, return_document=ReturnDocument.AFTER)
    if not won:
        raise HTTPException(409, "This request is already verified or closed.")
    await audit(None, "privacy_verified", "privacy_request", request_id, {"type": r["type"], "method": "email_code"})
    return await _after_verified(won, None, request)


@router.get("/requests/{request_id}/download")
async def download_export(request_id: str, token: str = "", format: str = "zip"):
    try:
        claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(403, "This download link is invalid or has expired.") from None
    if claims.get("pur") != "privacy_export" or claims.get("sub") != request_id:
        raise HTTPException(403, "This download link is invalid or has expired.")
    r = await db.privacy_requests.find_one({"id": request_id}, {"_id": 0})
    if not r or r.get("type") != "export" or not r.get("download_jti") \
            or not hmac.compare_digest(r["download_jti"], _jti_hash(claims.get("jti") or "")):
        raise HTTPException(403, "This download link is invalid or has expired.")
    resp = await _bundle_response(r["email"], "json" if format == "json" else "zip")
    await db.privacy_requests.update_one({"id": request_id}, {"$set": {"status": "completed", "completed_at": r.get("completed_at") or now_iso()},
                                                             "$inc": {"downloads": 1}})
    await audit(None, "privacy_export_downloaded", "privacy_request", request_id, {"format": format})
    if not r.get("completed_at"):
        await emit("privacy.request_completed", {"request": {**_public_view(r), "status": "completed"}})
    return resp


# ------------------------------------------------------------------------------------------------ self-service
class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ErasureIn(PasswordIn):
    confirm: str


async def _reauth(user: dict, password: str) -> dict:
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not full or not verify_password(password, full.get("password_hash")):
        await audit(user, "privacy_reauth_failed", "user", user["id"], {})
        raise HTTPException(403, "The password is wrong.")
    return full


@router.post("/me/export")
async def my_export(body: PasswordIn, format: str = "zip", user: dict = Depends(current_user)):
    full = await _reauth(user, body.password)
    await _limit("privacy_email", full["email"], LIMIT_EMAIL)
    r = await _new_request("export", full["email"], full.get("locale") or "fi", channel="self_service", actor=user,
                           extra={"status": "verified", "verified_at": now_iso(), "verified_by": "password"})
    resp = await _bundle_response(full["email"], "json" if format == "json" else "zip")
    await db.privacy_requests.update_one({"id": r["id"]}, {"$set": {"status": "completed", "completed_at": now_iso(),
                                                                    "downloads": 1}})
    await audit(user, "privacy_export_downloaded", "privacy_request", r["id"], {"format": format, "channel": "self_service"})
    return resp


@router.post("/me/erasure")
async def my_erasure(body: ErasureIn, response: Response, request: Request, user: dict = Depends(current_user)):
    if body.confirm.strip() not in ("POISTA", "DELETE"):
        raise HTTPException(400, 'Type POISTA (or DELETE) to confirm.')
    full = await _reauth(user, body.password)
    r = await _new_request("erasure", full["email"], full.get("locale") or "fi", channel="self_service", actor=user,
                           extra={"status": "verified", "verified_at": now_iso(), "verified_by": "password"})
    out = await _after_verified(r, user, request)
    if out["status"] in ("completed", "partially_held"):
        response.delete_cookie(COOKIE_NAME, path="/")
    return out


@router.get("/me/requests")
async def my_requests(user: dict = Depends(current_user)):
    h = gdpr.email_hash(user["email"])
    items = await db.privacy_requests.find({"email_hash": h}, HIDDEN).sort("created_at", -1).to_list(50)
    return {"items": [_public_view(r) for r in items]}


# ------------------------------------------------------------------------------------------------ ops
def require_any(*perms: str):
    async def dep(user: dict = Depends(require_internal)) -> dict:
        if not set(perms) & set(user.get("permissions") or []):
            raise HTTPException(403, f"Missing permission: {' or '.join(perms)}")
        return user

    return dep


class OpsRequestIn(BaseModel):
    type: str
    email: EmailStr
    received_via: str = Field(min_length=2, max_length=60)  # letter | phone | email | in_person ...
    identity_note: str = Field(min_length=5, max_length=1000)
    locale: str = "fi"


@router.get("/requests")
async def list_requests(status: str | None = None, type: str | None = None, limit: int = 100,
                        user: dict = Depends(require_any("settings.write", "customers.write"))):
    q: dict = {}
    if status:
        q["status"] = status
    if type:
        q["type"] = type
    items = await db.privacy_requests.find(q, {**HIDDEN, "result.erased": 0, "result.anonymised": 0}) \
        .sort("created_at", -1).to_list(min(limit, 500))
    overdue = sum(1 for r in items if r["status"] not in ("completed", "rejected", "expired", "locked")
                  and (r.get("due_at") or "9") < now_iso())
    return {"items": items, "overdue": overdue}


@router.get("/requests/{request_id}")
async def get_request(request_id: str, user: dict = Depends(require_any("settings.write", "customers.write"))):
    r = await db.privacy_requests.find_one({"id": request_id}, HIDDEN)
    if not r:
        raise HTTPException(404, "Request not found.")
    return r


@router.post("/ops/requests")
async def ops_create_request(body: OpsRequestIn, request: Request,
                             user: dict = Depends(require_any("settings.write", "customers.write"))):
    if body.type not in TYPES:
        raise HTTPException(400, "Request type must be export or erasure.")
    r = await _new_request(body.type, str(body.email), body.locale, channel="ops", actor=user,
                           extra={"status": "verified", "verified_at": now_iso(), "verified_by": actor_ref(user),
                                  "received_via": body.received_via.strip(), "identity_note": body.identity_note.strip()})
    await audit(user, "privacy_verified", "privacy_request", r["id"], {"type": body.type, "method": "ops",
                                                                       "received_via": body.received_via})
    return await _after_verified(r, user, request)


@router.post("/requests/{request_id}/process")
async def ops_process(request_id: str, user: dict = Depends(require_perm("settings.write"))):
    """Ops acts on a verified request now: erasure runs (this is the approval), export gets a fresh download token."""
    r = await db.privacy_requests.find_one({"id": request_id}, {"_id": 0})
    if not r:
        raise HTTPException(404, "Request not found.")
    if r["type"] == "export":
        if r["status"] not in ("verified", "ready", "completed"):
            raise HTTPException(409, "The request is not verified.")
        return await _issue_download(r, user)
    if r["status"] not in ("verified", "awaiting_approval", "partially_held"):
        raise HTTPException(409, f"The request is {r['status']}; only a verified request can be processed.")
    await audit(user, "privacy_erasure_approved", "privacy_request", request_id, {"via": "process"})
    return await execute_erasure(r, user)


# ------------------------------------------------------------------------------------------------ legal holds
class HoldIn(BaseModel):
    scope: str
    ref_id: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=5, max_length=1000)
    basis: str | None = None
    until: str | None = None


class ReleaseIn(BaseModel):
    note: str | None = None


@compliance_router.get("/legal-holds")
async def list_holds(active: bool = False, user: dict = Depends(require_any("settings.write", "customers.write"))):
    q = {"released_at": None} if active else {}
    return {"items": await gdpr.raw("legal_holds").find(q, {"_id": 0}).sort("created_at", -1).to_list(500),
            "scopes": list(gdpr.HOLD_SCOPES)}


@compliance_router.post("/legal-holds")
async def create_hold(body: HoldIn, user: dict = Depends(require_perm("settings.write"))):
    try:
        return await gdpr.create_hold(body.model_dump(), user)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@compliance_router.post("/legal-holds/{hold_id}/release")
async def release_hold(hold_id: str, body: ReleaseIn | None = None, user: dict = Depends(require_perm("settings.write"))):
    h = await gdpr.release_hold(hold_id, user, (body or ReleaseIn()).note)
    if not h:
        raise HTTPException(404, "Active hold not found.")
    # erasures this hold held back run again now (art. 17: erase once the ground for keeping is gone)
    rerun = []
    async for r in db.privacy_requests.find({"type": "erasure", "status": "partially_held",
                                             "result.held.hold_ids": hold_id}, {"_id": 0}):
        rerun.append((await execute_erasure(r, user))["id"])
    return {**h, "erasures_rerun": rerun}


# ------------------------------------------------------------------------------------------------ retention
class RunIn(BaseModel):
    dry_run: bool = True
    as_of: str | None = None  # dry run only: preview the sweep as it would run on this date (YYYY-MM-DD or ISO time)


@compliance_router.get("/retention/policy")
async def retention_policy(user: dict = Depends(require_internal)):
    return {"items": retention.policy_table(await get_settings()), "schedule": "daily 03:30 Europe/Helsinki",
            "counts_from": "end of the calendar (financial) year of the event for year-based periods"}


@compliance_router.get("/retention/runs")
async def retention_runs(limit: int = 30, user: dict = Depends(require_any("settings.write", "customers.write"))):
    items = await gdpr.raw("retention_runs").find({}).sort([("started_at", -1), ("_id", -1)]).to_list(min(limit, 200))
    return {"items": [{k: v for k, v in r.items() if k != "_id"} for r in items]}


@compliance_router.post("/retention/run")
async def retention_run(body: RunIn, user: dict = Depends(require_perm("settings.write"))):
    as_of = None
    if body.as_of:
        if not body.dry_run:
            raise HTTPException(400, "A look-ahead date is only allowed for a dry run.")
        try:
            as_of = parse_iso(body.as_of if "T" in body.as_of else f"{body.as_of[:10]}T00:00:00Z")
        except (TypeError, ValueError):
            raise HTTPException(400, "as_of must be a date (YYYY-MM-DD) or an ISO time.") from None
        if not now() <= as_of <= now() + timedelta(days=3660):
            raise HTTPException(400, "as_of must be between now and ten years ahead.")
    return await retention.run_sweep(dry_run=body.dry_run, actor=user, trigger="manual", as_of=as_of)


routers = [router, compliance_router]
