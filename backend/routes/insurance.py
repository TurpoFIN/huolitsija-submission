"""s7 cargo insurance routes (see shared/interfaces.md, section s7).

Ops [quotes.write] act on any quote or shipment; customers [company.book] on their own company's only.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from core.db import db
from core.deps import current_user, is_internal
from core.util import NO_ID
from services import insurance as svc

router = APIRouter(prefix="/api", tags=["insurance"])


class QuoteIn(BaseModel):
    quote_id: str | None = None
    shipment_id: str | None = None
    option_id: str | None = None
    cover: str = "ICC_A"
    insured_value_cents: int | None = None
    currency: str = "EUR"


class CancelIn(BaseModel):
    reason: str = ""


def _may(user: dict, company_id: str | None):
    perms = set(user.get("permissions") or [])
    if is_internal(user) and "quotes.write" in perms:
        return
    if user.get("org_type") == "customer" and "company.book" in perms and company_id and company_id == user.get("company_id"):
        return
    raise HTTPException(403, "Not allowed")


async def _insurer_user(user: dict = Depends(current_user)) -> dict:
    perms = set(user.get("permissions") or [])
    if not ((is_internal(user) and "quotes.write" in perms) or (user.get("org_type") == "customer" and "company.book" in perms)):
        raise HTTPException(403, "Missing permission: quotes.write")
    return user


async def _subject(body: QuoteIn) -> dict:
    if bool(body.quote_id) == bool(body.shipment_id):
        raise HTTPException(400, "Give either quote_id or shipment_id.")
    if body.shipment_id:
        s = await db.shipments.find_one({"id": body.shipment_id}, NO_ID)
        if not s:
            raise HTTPException(404, "Shipment not found")
        return s
    q = await db.quotes.find_one({"id": body.quote_id}, NO_ID)
    if not q:
        raise HTTPException(404, "Quote not found")
    return q


@router.get("/insurance/providers")
async def providers(user: dict = Depends(_insurer_user)):
    return await svc.providers()


@router.post("/insurance/quote")
async def quote(body: QuoteIn, user: dict = Depends(_insurer_user)):
    subject = await _subject(body)
    _may(user, subject.get("company_id"))
    return await svc.quote_insurance(subject, body.cover, body.insured_value_cents, body.currency, actor=user, option_id=body.option_id)


@router.get("/insurance/quotes")
async def list_quotes(shipment_id: str | None = None, quote_id: str | None = None, user: dict = Depends(_insurer_user)):
    q: dict = {}
    if shipment_id:
        q["subject.shipment_id"] = shipment_id
    if quote_id:
        q["subject.quote_id"] = quote_id
    if not is_internal(user):
        q["company_id"] = user.get("company_id")
    rows = await db.insurance_quotes.find(q, NO_ID).sort("created_at", -1).to_list(200)
    return {"items": [svc._public(r) for r in rows]}


@router.get("/insurance/quotes/{insurance_quote_id}")
async def get_quote(insurance_quote_id: str, user: dict = Depends(_insurer_user)):
    iq = await db.insurance_quotes.find_one({"id": insurance_quote_id}, NO_ID)
    if not iq:
        raise HTTPException(404, "Insurance quote not found")
    _may(user, iq.get("company_id"))
    return svc._public(iq)


@router.post("/insurance/{insurance_quote_id}/bind")
async def bind(insurance_quote_id: str, user: dict = Depends(_insurer_user)):
    iq = await db.insurance_quotes.find_one({"id": insurance_quote_id}, {"_id": 0, "company_id": 1})
    if not iq:
        raise HTTPException(404, "Insurance quote not found")
    _may(user, iq.get("company_id"))
    return await svc.bind_insurance(insurance_quote_id, user)


@router.get("/insurance/policies")
async def policies(shipment_id: str | None = None, quote_id: str | None = None, user: dict = Depends(_insurer_user)):
    q: dict = {}
    if shipment_id:
        q["shipment_id"] = shipment_id
    if quote_id:
        q["quote_id"] = quote_id
    if not is_internal(user):
        q["company_id"] = user.get("company_id")
    rows = await db.insurance_policies.find(q, NO_ID).sort("created_at", -1).to_list(200)
    return {"items": [svc._public(r) for r in rows]}


@router.get("/insurance/policies/{policy_id}")
async def get_policy(policy_id: str, user: dict = Depends(_insurer_user)):
    pol = await db.insurance_policies.find_one({"id": policy_id}, NO_ID)
    if not pol:
        raise HTTPException(404, "Policy not found")
    _may(user, pol.get("company_id"))
    return svc._public(pol)


@router.get("/insurance/policies/{policy_id}/certificate")
async def get_certificate(policy_id: str, user: dict = Depends(_insurer_user)):
    pol = await db.insurance_policies.find_one({"id": policy_id}, {"_id": 0, "company_id": 1})
    if not pol:
        raise HTTPException(404, "Policy not found")
    _may(user, pol.get("company_id"))
    data, filename, ctype = await svc.certificate(policy_id)
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return Response(content=data, media_type=ctype, headers=headers)


@router.post("/insurance/policies/{policy_id}/cancel")
async def cancel(policy_id: str, body: CancelIn, user: dict = Depends(_insurer_user)):
    if not is_internal(user):
        raise HTTPException(403, "Ops cancel policies")
    return await svc.cancel_policy(policy_id, user, body.reason)
