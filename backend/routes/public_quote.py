"""Public instant quote (no sign-in): location search, indicative options, claim token, simple per-IP rate limit.

POST /api/public/quote stores an anonymous quote (no number, no company) with a claim token; after sign-up the
customer calls POST /api/quotes/claim {token} to attach it to their company. Unclaimed quotes are deleted after
30 days. The client IP is never stored in clear text (rate limit keys are salted hashes and expire within 2 hours).
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.db import db, register_index
from core.security import random_token
from core.settings import get_settings
from core.util import NO_ID, iso
from routes.quotes import QuoteIn, _base_doc, _price, customer_view, apply_approval_policy
from services import customs_border
from services import pricing

router = APIRouter(prefix="/api/public", tags=["public"])

register_index("rate_limits", "key", unique=True)
register_index("rate_limits", "expires_at", expireAfterSeconds=0)

LIMIT_PER_HOUR = int(os.environ.get("PUBLIC_QUOTE_RATE_LIMIT_PER_HOUR", "20"))
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
CLAIM_DAYS = 30


def client_ip(request: Request) -> str:
    """Right-most X-Forwarded-For entry added by our own ingress (clients can only prepend), else the socket peer."""
    xff = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if xff and TRUSTED_PROXY_HOPS > 0:
        return xff[-min(TRUSTED_PROXY_HOPS, len(xff))]
    return request.client.host if request.client else "unknown"


async def check_rate_limit(request: Request, bucket: str = "public_quote", limit: int | None = None):
    limit = limit or LIMIT_PER_HOUR
    now = datetime.now(timezone.utc)
    window = now.strftime("%Y%m%d%H")
    salt = (await db.system.find_one({"key": "jwt_secret"}) or {}).get("value", "")[:16]
    ip_hash = hashlib.sha256(f"{salt}:{client_ip(request)}".encode()).hexdigest()[:32]
    key = f"{bucket}:{ip_hash}:{window}"
    from pymongo import ReturnDocument

    doc = await db.rate_limits.find_one_and_update(
        {"key": key}, {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": now + timedelta(hours=2), "created_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if doc["count"] > limit:
        minutes = 60 - now.minute
        raise HTTPException(429, f"Too many instant quotes from your network in the last hour. Try again in {minutes} "
                                 f"minutes, or sign in for unlimited quotes.")
    return ip_hash


@router.get("/locations")
async def public_locations(q: str = "", mode: str | None = None, limit: int = 10):
    return {"items": await pricing.search_locations(q, limit=min(max(limit, 1), 20), mode=mode)}


@router.post("/quote")
async def public_quote(body: QuoteIn, request: Request):
    ip_hash = await check_rate_limit(request)
    settings = await get_settings()
    data = body.model_dump(exclude_none=True, exclude={"company_id", "send", "ai"})
    notify_email = _clean_email(data.pop("notify_email", None))
    locale = data.pop("locale", None)
    priced = await _price(data, None, strict=False)
    q = _base_doc(priced, source="public", settings=settings)
    token = random_token(24)
    q.update(claim_token=token, claim_expires_at=iso(datetime.now(timezone.utc) + timedelta(days=CLAIM_DAYS)), ip_hash=ip_hash,
             notify_email=notify_email, notify_locale=locale if locale in ("fi", "en") else "fi")
    from services import global_quote

    from services.demo_autoquote import conf as demo_conf
    if demo_conf.real_requester_enabled():
        q["sourcing"] = {"status": "requested",
            "message_en": "We're getting live rates from our carrier partners. Leave your email and we'll send your quote there.",
            "message_fi": "Pyydämme ajantasaiset hinnat kuljetuskumppaneiltamme. Jätä sähköpostiosoitteesi, niin lähetämme tarjouksen sinne."}
    else:
        await global_quote.open_sourcing(q, priced.get("sourcing_needs") or [])
    if not demo_conf.real_requester_enabled() and customs_border.required_review(q["request"]):
        await apply_approval_policy(q, settings, None, send=True)
    await db.quotes.insert_one(dict(q))
    return {"quote": customer_view(q), "claim_token": token}


def _clean_email(value) -> str | None:
    import re as _re

    v = str(value or "").strip()
    if not v:
        return None
    if len(v) > 254 or not _re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}", v):
        raise HTTPException(400, "Enter a valid e-mail address for the price update.")
    return v


class NotifyIn(BaseModel):
    token: str
    email: str
    locale: str | None = "fi"


@router.post("/quote/{quote_id}/notify")
async def public_quote_notify(quote_id: str, body: NotifyIn, request: Request):
    """Anonymous visitor asks to be e-mailed when the firm price lands (kept with the quote, deleted with it)."""
    await check_rate_limit(request, bucket="public_notify", limit=10)
    q = await _public(quote_id, body.token)
    email = _clean_email(body.email)
    from services.demo_autoquote import conf as demo_conf
    if demo_conf.real_requester_enabled():
        from routes.demo_public import start_from_quote
        receipt = await start_from_quote(q, email or "", body.locale or "en", request)
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"notify_email": email,
            "notify_locale": body.locale, "demo_run_id": receipt["id"]}})
        return {"ok": True, "sourcing": customer_view(q).get("sourcing"), "run_id": receipt["id"],
                "ref": receipt["ref"], "access_token": receipt["access_token"], "track_url": receipt["track_url"],
                "status": receipt["status"]}
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"notify_email": email,
                                                          "notify_locale": body.locale if body.locale in ("fi", "en") else "fi"}})
    return {"ok": True, "sourcing": (customer_view(q).get("sourcing") or None)}


async def _public(quote_id: str, token: str) -> dict:
    q = await db.quotes.find_one({"id": quote_id, "claim_token": token}, NO_ID) if token else None
    if not q:
        raise HTTPException(404, "Quote not found or the link has expired.")
    return q


@router.get("/quote/{quote_id}")
async def get_public_quote(quote_id: str, token: str = ""):
    q = await _public(quote_id, token)
    out = customer_view(q)
    out["claimed"] = bool(q.get("company_id"))
    return {"quote": out}


@router.get("/quote/{quote_id}/pdf")
async def public_quote_pdf(quote_id: str, token: str = "", lang: str = "fi"):
    from services.quote_pdf import render_quote_pdf

    q = await _public(quote_id, token)
    pdf = await render_quote_pdf(customer_view(q), None, lang=lang if lang in ("fi", "en") else "fi")
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="huolitsija-quote.pdf"'})
