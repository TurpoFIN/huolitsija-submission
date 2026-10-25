"""Company profile, KYC (live YTJ / VIES / EORI / sanctions checks), power of attorney, and the ops customer list."""
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, EmailStr

from core.audit import actor_ref, audit
from core.db import db
from core.deps import PUBLIC_USER_FIELDS, current_user, require_internal, require_perm
from core.events import emit
from core.refs import ytunnus_valid
from core.util import NO_ID, new_id, now_iso
from services import kyc as kyc_svc
from services import screening
from services.poa_pdf import TEXT_VERSION, consent_text, evidence_hash, render_poa_pdf

router = APIRouter(prefix="/api", tags=["companies"])

COMPANY_PROJECTION = {"_id": 0}


def _require_customer(user: dict):
    if user.get("org_type") != "customer" or not user.get("company_id"):
        raise HTTPException(403, "Company profile is for customer accounts")


CUSTOMER_SANCTIONS_EN = "Screening is being reviewed by Huolitsija compliance"
CUSTOMER_SANCTIONS_FI = "Huolitsijan compliance-tiimi käy tarkistuksen läpi"


def _customer_safe(c: dict) -> dict:
    """No tipping-off: a customer never sees sanctions hits, scores, list references or a compliance decision."""
    k = dict(c.get("kyc") or {})
    k.pop("cleared_hits", None)
    k.pop("sanctions_decision", None)
    if k.get("sanctions"):
        sc = {x: k["sanctions"].get(x) for x in ("status", "checked_at", "source", "lists", "screened_names", "summary",
                                                 "summary_fi")}
        if sc.get("status") not in ("pass", "not_run"):
            sc.update(status="in_review", summary=CUSTOMER_SANCTIONS_EN, summary_fi=CUSTOMER_SANCTIONS_FI)
        k["sanctions"] = sc
    if k.get("pep"):  # PEP matches and the compliance decision are internal
        pc = {x: k["pep"].get(x) for x in ("status", "checked_at", "source", "summary", "summary_fi")}
        if pc.get("status") not in ("pass", "not_applicable", "not_run") or k["pep"].get("approved"):
            pc.update(status="in_review" if pc.get("status") != "pass" else "pass", summary=CUSTOMER_PEP_EN,
                      summary_fi=CUSTOMER_PEP_FI)
        k["pep"] = pc
    k.pop("pep_decision", None)
    if isinstance(c.get("beneficial_owners"), dict):
        c["beneficial_owners"] = {x: y for x, y in c["beneficial_owners"].items() if x != "persons"}
    c["kyc"] = k
    if c.get("sanctions_status") in ("potential_match", "blocked"):
        c["sanctions_status"] = "in_review"
    if (k.get("eori") or {}).get("status") == "manual":
        # the broker's note and name are internal; the customer sees that Huolitsija verified the number
        k["eori"] = {"status": "manual", "eori": k["eori"].get("eori"), "checked_at": k["eori"].get("at"),
                     "source": "Verified by Huolitsija", "summary": f"EORI {k['eori'].get('eori')} verified by Huolitsija",
                     "summary_fi": f"Huolitsija on vahvistanut EORI-numeron {k['eori'].get('eori')}"}
    for row in c.get("kyc_checklist") or []:
        if row["key"] == "eori" and row.get("status") == "manual":
            row.update(manual=None, registry_result=None, summary=k["eori"]["summary"], summary_fi=k["eori"]["summary_fi"],
                       source=k["eori"]["source"])
        if row["key"] == "pep":
            row.update(summary=k.get("pep", {}).get("summary"), summary_fi=k.get("pep", {}).get("summary_fi"),
                       status=k.get("pep", {}).get("status") or row["status"])
        if row["key"] == "sanctions":
            row["hits"] = None
            if row["status"] not in ("pass", "not_run"):
                row.update(status="in_review", summary=CUSTOMER_SANCTIONS_EN, summary_fi=CUSTOMER_SANCTIONS_FI)
    if c.get("demo"):
        if c.get("poa_document"):
            c["poa_document"]["filename"] = c["poa_document"]["filename"].replace("-demo.pdf", ".pdf")
        if (c.get("poa") or {}).get("demo"):
            c["poa"] = {**c["poa"], "signature": {**c["poa"].get("signature", {}),
                         "typed_name": "Asiakkaan edustaja", "title": "Toimitusjohtaja"}}
    return c


async def _company_view(company: dict, customer: bool = False) -> dict:
    c = dict(company)
    if kyc_svc.stale_pending(c):
        c["kyc_status"] = (c.get("kyc") or {}).get("prev_status") or "not_started"
        c["kyc_stale"] = True
        await db.companies.update_one({"id": c["id"], "kyc_status": "pending"},
                                      {"$set": {"kyc_status": c["kyc_status"], "kyc_stale": True}})
    users = await db.users.count_documents({"company_id": c["id"], "disabled": {"$ne": True}})
    c["users_count"] = users
    poa = c.get("poa") or {}
    c["poa_valid"] = kyc_svc.poa_valid(c)
    if poa.get("document_id"):
        d = await db.documents.find_one({"id": poa["document_id"]}, {"_id": 0, "id": 1, "filename": 1, "uploaded_at": 1})
        c["poa_document"] = d
    c["kyc_checklist"] = _checklist(c)
    return _customer_safe(c) if customer else c


CHECK_ORDER = ["ytunnus", "ytj", "vies", "eori", "sanctions", "ubo", "pep", "poa"]
CUSTOMER_PEP_EN = "Reviewed by Huolitsija compliance"
CUSTOMER_PEP_FI = "Huolitsijan compliance-tiimi käy tarkistuksen läpi"


def _checklist(c: dict) -> list[dict]:
    k = c.get("kyc") or {}
    out = []
    for key in CHECK_ORDER:
        v = k.get(key) or {}
        rr = v.get("registry_result") if key == "eori" else None
        if rr and not rr.get("summary_fi"):  # the EU EOS reply under a manual verification (stored English only)
            rr = {**rr, "summary_fi": kyc_svc.fi_summary("eori", rr, c)}
        out.append({"key": key, "status": v.get("status") or "not_run", "summary": v.get("summary"),
                    "summary_fi": v.get("summary_fi") or (kyc_svc.fi_summary("eori", v, c)  # revoked before summary_fi
                                                          if key == "eori" and v.get("summary") else None),
                    "checked_at": v.get("checked_at"), "source": v.get("source"), "url": v.get("url"),
                    "risk": key in kyc_svc.RISK_CHECKS, "hits": v.get("hits") if key == "sanctions" else None,
                    "lists": v.get("lists") if key == "sanctions" else None,
                    "screened_names": v.get("screened_names") if key == "sanctions" else None,
                    "manual": ({k: v.get(k) for k in ("by", "at", "note", "document_id", "eori")}
                               if key == "eori" and v.get("status") == "manual" else None),
                    "registry_result": rr})
    return out


# ---------------------------------------------------------------------------------------------
# Own company (customer)
# ---------------------------------------------------------------------------------------------
@router.get("/company")
async def get_company(user: dict = Depends(current_user)):
    _require_customer(user)
    c = await db.companies.find_one({"id": user["company_id"]}, COMPANY_PROJECTION)
    if not c:
        raise HTTPException(404, "Company not found")
    return await _company_view(c, customer=True)


class Address(BaseModel):
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str | None = None


class Ubo(BaseModel):
    id: str | None = None
    name: str
    birth_date: str | None = None
    country: str | None = None
    ownership_pct: float | None = None
    control: str | None = None
    pep: bool = False


class Signatory(BaseModel):
    name: str | None = None
    title: str | None = None
    email: EmailStr | None = None


class CompanyPatch(BaseModel):
    name: str | None = None
    business_id: str | None = None
    vat_id: str | None = None
    eori: str | None = None
    address: Address | None = None
    billing_email: EmailStr | None = None
    ubos: list[Ubo] | None = None
    ubo_none_declared: bool | None = None
    signatory: Signatory | None = None
    cbam_declarant_id: str | None = None
    cbam_under_threshold: bool | None = None
    source_of_business: str | None = None


def _clean_patch(body: CompanyPatch, current: dict) -> tuple[dict, list[str]]:
    from services.customs import eori_format_ok, eori_normalise

    sets, changed = {}, []
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        n = (data["name"] or "").strip()
        if len(n) < 2:
            raise HTTPException(400, "Company name is required")
        sets["name"] = n[:200]
    if "business_id" in data:
        bid = kyc_svc.normalise_business_id(data["business_id"])
        if not bid or not ytunnus_valid(bid):
            raise HTTPException(400, "Business ID must look like 1234567-8 and have a valid check digit")
        sets["business_id"] = bid
    if "vat_id" in data:
        v = re.sub(r"[\s.-]", "", data["vat_id"] or "").upper()
        if v and not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{2,12}", v):
            raise HTTPException(400, "VAT number must start with the country code, e.g. FI12345678")
        sets["vat_id"] = v or None
    if "eori" in data:
        e = eori_normalise(data["eori"]) if data["eori"] else None
        if e and not eori_format_ok(e):
            raise HTTPException(400, "EORI must be FI + Business ID (FI1234567-8) or another EU format")
        sets["eori"] = e
    if "address" in data and data["address"] is not None:
        a = {k: (v or "").strip() or None for k, v in data["address"].items()}
        a["country"] = (a.get("country") or "FI").upper()[:2]
        if a.get("postal_code") and a["country"] == "FI" and not re.fullmatch(r"\d{5}", a["postal_code"]):
            raise HTTPException(400, "Finnish postal codes have 5 digits")
        sets["address"] = a
    if "billing_email" in data:
        sets["billing_email"] = data["billing_email"]
    if "ubos" in data and data["ubos"] is not None:
        ubos = []
        for u in data["ubos"]:
            if u.get("ownership_pct") is not None and not (0 < float(u["ownership_pct"]) <= 100):
                raise HTTPException(400, "Ownership must be between 0 and 100 %")
            if u.get("birth_date"):
                try:
                    date.fromisoformat(u["birth_date"])
                except ValueError:
                    raise HTTPException(400, "Birth date must be YYYY-MM-DD")
            ubos.append({**u, "id": u.get("id") or new_id(), "name": u["name"].strip()[:160],
                         "country": (u.get("country") or "").upper()[:2] or None})
        sets["ubos"] = ubos
        if ubos:
            sets["ubo_none_declared"] = False
    if "ubo_none_declared" in data and data["ubo_none_declared"] is not None:
        sets["ubo_none_declared"] = bool(data["ubo_none_declared"])
    if "signatory" in data and data["signatory"] is not None:
        sets["signatory"] = data["signatory"]
    for k in ("cbam_declarant_id", "source_of_business"):
        if k in data:
            sets[k] = (data[k] or "").strip()[:200] or None
    if "cbam_under_threshold" in data:
        sets["cbam_under_threshold"] = bool(data["cbam_under_threshold"])
    for k, v in sets.items():
        if current.get(k) != v:
            changed.append(k)
    return sets, changed


@router.patch("/company")
async def patch_company(body: CompanyPatch, user: dict = Depends(require_perm("company.manage"))):
    _require_customer(user)
    c = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
    sets, changed = _clean_patch(body, c)
    if "business_id" in changed and await db.companies.find_one({"business_id": sets["business_id"], "id": {"$ne": c["id"]}}):
        raise HTTPException(409, "Another account already uses this Business ID; contact support")
    identity = {"business_id", "name", "ubos", "signatory"} & set(changed)
    if identity and c.get("kyc_status") in ("approved", "in_review", "rejected"):
        sets["kyc_status"] = "not_started" if "business_id" in changed else c["kyc_status"]
        sets["kyc_stale"] = True
    unset = {}
    if "eori" in changed and (c.get("kyc") or {}).get("eori"):
        unset["kyc.eori"] = ""  # the registry result (or a manual verification) was for the old number
        sets["kyc_stale"] = True
    if not changed:
        return await _company_view(c, customer=True)
    await db.companies.update_one({"id": c["id"]}, {"$set": {**sets, "updated_at": now_iso()}, **({"$unset": unset} if unset else {})})
    await audit(user, "company_updated", "company", c["id"], {"fields": changed}, company_id=c["id"])
    fresh = await db.companies.find_one({"id": c["id"]}, NO_ID)
    if sets.get("kyc_status") and sets["kyc_status"] != c.get("kyc_status"):
        await emit("company.kyc_changed", {"company": fresh, "actor": user})
    return await _company_view(fresh, customer=True)


class LookupIn(BaseModel):
    business_id: str


@router.post("/company/lookup")
async def lookup(body: LookupIn, user: dict = Depends(current_user)):
    return await kyc_svc.ytj_lookup(body.business_id)


@router.post("/company/kyc/run")
async def run_own_kyc(user: dict = Depends(require_perm("company.manage"))):
    _require_customer(user)
    c = await kyc_svc.run_kyc(user["company_id"], user)
    await db.companies.update_one({"id": c["id"]}, {"$set": {"kyc_stale": False}})
    c["kyc_stale"] = False
    return await _company_view(c, customer=True)


class PoaIn(BaseModel):
    type: str
    scope: list[str]
    valid_from: str
    valid_to: str
    typed_name: str
    title: str | None = None
    accept: bool


@router.post("/company/poa")
async def sign_poa(body: PoaIn, request: Request, user: dict = Depends(require_perm("company.manage"))):
    from services.documents import create_document

    _require_customer(user)
    c = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
    if body.type not in ("direct", "indirect"):
        raise HTTPException(400, "Choose direct or indirect representation")
    if body.type == "indirect" and c.get("representation_default") != "indirect":
        # RULE R-1: indirect representation makes Huolitsija jointly liable for the customs debt; it is offered only
        # after ops has agreed it in the contract (ops sets representation_default=indirect with a note)
        raise HTTPException(403, "Indirect representation must first be agreed with Huolitsija in your contract; "
                                 "sign a direct representation power of attorney or contact us")
    scope = [s for s in body.scope if s in ("import", "export", "transit", "ens", "correspondence")]
    if not scope:
        raise HTTPException(400, "Choose at least one declaration type")
    try:
        vf, vt = date.fromisoformat(body.valid_from), date.fromisoformat(body.valid_to)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if vt <= vf:
        raise HTTPException(400, "Valid to must be after valid from")
    if (vt - vf).days > 366 * 5:
        raise HTTPException(400, "A power of attorney can be valid for at most 5 years; renew it after that")
    if not body.accept:
        raise HTTPException(400, "Tick the box to confirm you are authorised to sign")
    typed = re.sub(r"\s+", " ", body.typed_name or "").strip()
    if len(typed.split(" ")) < 2:
        raise HTTPException(400, "Type your full name (first and last name) as your signature")
    if not c.get("business_id") or not c.get("name"):
        raise HTTPException(400, "Add the company name and Business ID before signing")
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else None)
    signed_at = now_iso()
    poa = {"type": body.type, "scope": scope, "valid_from": vf.isoformat(), "valid_to": vt.isoformat(),
           "signature": {"typed_name": typed, "title": (body.title or "").strip() or None, "email": user.get("email"),
                         "user_id": user["id"], "signed_at": signed_at, "ip": ip,
                         "user_agent": (request.headers.get("user-agent") or "")[:300],
                         "consent_text": consent_text(body.type, c["name"], user.get("locale") or "fi"),
                         "consent_lang": user.get("locale") or "fi"},
           "text_version": TEXT_VERSION, "signed_at": signed_at, "revoked_at": None}
    poa["evidence_hash"] = evidence_hash(c, poa)
    pdf = render_poa_pdf(c, poa)
    doc = await create_document(company_id=c["id"], shipment_id=None, doc_type="power_of_attorney",
                                filename=f"Valtakirja-{c['business_id']}-{vf.isoformat()}.pdf", data=pdf, actor=user,
                                source="system", extract=False, extra={"retention_years": 6})
    poa["document_id"] = doc["id"]
    history = [p for p in [c.get("poa")] if p and p.get("signed_at")]
    await db.companies.update_one({"id": c["id"]}, {"$set": {"poa": poa, "representation_default": body.type},
                                                    "$push": {"poa_history": {"$each": history}}})
    await audit(user, "poa_signed", "company", c["id"], {"type": body.type, "scope": scope, "valid_to": poa["valid_to"],
                                                          "document_id": doc["id"], "ip": ip}, company_id=c["id"])
    fresh = await db.companies.find_one({"id": c["id"]}, NO_ID)
    if fresh.get("kyc"):
        await db.companies.update_one({"id": c["id"]}, {"$set": {"kyc.poa": {
            "status": "pass", "checked_at": now_iso(), "source": "Signed in the portal",
            "summary": f"{body.type} representation, valid to {poa['valid_to']}"}}})
        fresh = await db.companies.find_one({"id": c["id"]}, NO_ID)
    # open drafts re-validate against the new POA
    from services.customs import save_and_recompute

    async for d in db.customs_declarations.find({"company_id": c["id"], "status": {"$in": ["draft", "needs_info"]}}, NO_ID):
        await save_and_recompute(d, user, "poa_updated")
    return await _company_view(fresh, customer=True)


@router.get("/company/poa/terms")
async def poa_terms(lang: str | None = None, user: dict = Depends(current_user)):
    """POA texts in the user's language: what each representation type means, scopes, consent sentence."""
    from services.poa_pdf import terms

    _require_customer(user)
    c = await db.companies.find_one({"id": user["company_id"]}, {"_id": 0, "name": 1, "representation_default": 1})
    t = terms(c or {}, lang or user.get("locale") or "fi")
    t["indirect_allowed"] = (c or {}).get("representation_default") == "indirect"
    return t


@router.post("/company/poa/revoke")
async def revoke_poa(user: dict = Depends(require_perm("company.manage"))):
    _require_customer(user)
    c = await db.companies.find_one({"id": user["company_id"]}, NO_ID)
    if not (c.get("poa") or {}).get("signed_at"):
        raise HTTPException(400, "There is no signed power of attorney to revoke")
    await db.companies.update_one({"id": c["id"]}, {"$set": {"poa.revoked_at": now_iso(),
                                                             "poa.revoked_by": actor_ref(user)}})
    await audit(user, "poa_revoked", "company", c["id"], {}, company_id=c["id"])
    return await _company_view(await db.companies.find_one({"id": c["id"]}, NO_ID), customer=True)


# ---------------------------------------------------------------------------------------------
# Screening (internal)
# ---------------------------------------------------------------------------------------------
@router.get("/screening/lists")
async def screening_lists(user: dict = Depends(require_internal)):
    return {"items": await screening.lists_status()}


class ScreenIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    kind: str = Field("any", pattern="^(any|entity|person|vessel)$")
    country: str | None = Field(None, max_length=2)


@router.post("/screening/check")
async def screening_check(body: ScreenIn, user: dict = Depends(require_internal)):
    try:
        res = await screening.screen_name(body.name, kind=body.kind, country=body.country)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await audit(user, "screening_check", "screening", None, {"name": body.name, "status": res.get("status")})
    return res


@router.post("/screening/refresh")
async def screening_refresh(user: dict = Depends(require_perm("kyc.approve"))):
    import asyncio

    asyncio.get_running_loop().create_task(screening.refresh_lists(force=True))
    return {"ok": True, "items": await screening.lists_status()}


# ---------------------------------------------------------------------------------------------
# Ops: customers
# ---------------------------------------------------------------------------------------------
@router.get("/ops/customers")
async def ops_customers(q: str | None = None, kyc: str | None = None, sanctions: str | None = None,
                        include_test: bool = False, limit: int = Query(500, le=2000), user: dict = Depends(require_internal)):
    base: dict = {"status": {"$ne": "archived"}}
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        base["$or"] = [{"name": rx}, {"business_id": rx}, {"vat_id": rx}, {"eori": rx}]
    query = dict(base)
    if kyc:
        query["kyc_status"] = {"$in": kyc.split(",")}
    if sanctions:
        query["sanctions_status"] = {"$in": sanctions.split(",")}
    proj = {"_id": 0, "kyc.sanctions.hits": 0, "kyc.ytj.data": 0, "poa_history": 0}
    comps = await db.companies.find(query, proj).sort("name", 1).to_list(limit)
    ids = [c["id"] for c in comps]
    users = {r["_id"]: r["n"] async for r in db.users.aggregate([{"$match": {"company_id": {"$in": ids}}},
                                                                 {"$group": {"_id": "$company_id", "n": {"$sum": 1}}}])}
    ships = {r["_id"]: r["n"] async for r in db.shipments.aggregate([{"$match": {"company_id": {"$in": ids}, "test": {"$ne": True}}},
                                                                     {"$group": {"_id": "$company_id", "n": {"$sum": 1}}}])}
    open_cd = {r["_id"]: r["n"] async for r in db.customs_declarations.aggregate([
        {"$match": {"company_id": {"$in": ids}, "test": {"$ne": True},
                    "status": {"$nin": ["released", "closed", "invalidated", "not_released"]}}},
        {"$group": {"_id": "$company_id", "n": {"$sum": 1}}}])}
    pending = {a["ref_id"] for a in await db.approvals.find({"kind": "kyc_review", "status": "pending"},
                                                           {"_id": 0, "ref_id": 1}).to_list(1000)}
    items = []
    for c in comps:
        items.append({
            "id": c["id"], "name": c.get("name"), "business_id": c.get("business_id"), "vat_id": c.get("vat_id"),
            "eori": c.get("eori"), "city": (c.get("address") or {}).get("city"), "kyc_status": c.get("kyc_status") or "not_started",
            "sanctions_status": c.get("sanctions_status") or "not_screened", "kyc_checked_at": c.get("kyc_checked_at"),
            "credit_limit_cents": c.get("credit_limit_cents"), "payment_terms_days": c.get("payment_terms_days"),
            "representation_default": c.get("representation_default"), "poa_valid": kyc_svc.poa_valid(c),
            "poa_valid_to": (c.get("poa") or {}).get("valid_to"), "users_count": users.get(c["id"], 0),
            "shipments_count": ships.get(c["id"], 0), "open_declarations": open_cd.get(c["id"], 0),
            "open_ar_cents": None,  # owned by V4 (billing); filled when invoices exist
            "kyc_review_pending": c["id"] in pending, "status": c.get("status"), "created_at": c.get("created_at"),
            "demo": c.get("demo"),
        })
    counts = {}
    async for r in db.companies.aggregate([{"$match": base}, {"$group": {"_id": "$kyc_status", "n": {"$sum": 1}}}]):
        counts[r["_id"] or "not_started"] = r["n"]
    sanc = {}
    async for r in db.companies.aggregate([{"$match": base}, {"$group": {"_id": "$sanctions_status", "n": {"$sum": 1}}}]):
        sanc[r["_id"] or "not_screened"] = r["n"]
    try:
        from services import billing  # V4, lazy

        if hasattr(billing, "open_ar_by_company"):
            ar = await billing.open_ar_by_company(ids)
            for i in items:
                i["open_ar_cents"] = ar.get(i["id"])
    except Exception:  # noqa: BLE001
        pass
    return {"items": items, "total": len(items), "counts": counts, "sanctions_counts": sanc}


@router.get("/ops/customers/{company_id}")
async def ops_customer(company_id: str, user: dict = Depends(require_internal)):
    c = await db.companies.find_one({"id": company_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Customer not found")
    view = await _company_view(c)
    view["users"] = await db.users.find({"company_id": company_id}, {**PUBLIC_USER_FIELDS, "session_version": 0}).to_list(200)
    view["shipments_count"] = await db.shipments.count_documents({"company_id": company_id, "test": {"$ne": True}})
    view["recent_shipments"] = await db.shipments.find({"company_id": company_id, "test": {"$ne": True}}, {
        "_id": 0, "id": 1, "reference": 1, "status": 1, "mode": 1, "direction": 1, "eta": 1}).sort("created_at", -1).to_list(8)
    view["declarations"] = await db.customs_declarations.find({"company_id": company_id, "test": {"$ne": True}}, {
        "_id": 0, "id": 1, "lrn": 1, "mrn": 1, "status": 1, "shipment_ref": 1, "updated_at": 1,
        "totals.customs_value_cents": 1}).sort("updated_at", -1).to_list(10)
    view["approvals"] = await db.approvals.find({"ref_id": company_id}, NO_ID).sort("created_at", -1).to_list(10)
    view["audit"] = await db.audit_log.find({"entity": "company", "entity_id": company_id}, NO_ID).sort("at", -1).to_list(30)
    view["screening_lists"] = await screening.lists_status()
    view["open_ar_cents"] = None
    perms = set(user.get("permissions") or [])
    view["can"] = {"decide": "kyc.approve" in perms, "edit": "customers.write" in perms,
                   "run_kyc": bool({"kyc.approve", "customers.write"} & perms)}
    return view


class OpsCustomerPatch(BaseModel):
    credit_limit_cents: int | None = None
    payment_terms_days: int | None = None
    representation_default: str | None = None
    billing_email: EmailStr | None = None
    vat_registered: bool | None = None
    cbam_declarant_id: str | None = None
    guarantee_ref: str | None = None
    guarantee_amount_cents: int | None = None
    deferred_payment_authorisation_no: str | None = None
    note: str | None = None


@router.patch("/ops/customers/{company_id}")
async def ops_patch_customer(company_id: str, body: OpsCustomerPatch, user: dict = Depends(require_perm("customers.write"))):
    c = await db.companies.find_one({"id": company_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Customer not found")
    data = body.model_dump(exclude_unset=True)
    note = data.pop("note", None)
    if "credit_limit_cents" in data and (data["credit_limit_cents"] is None or data["credit_limit_cents"] < 0):
        raise HTTPException(400, "Credit limit must be zero or more")
    if "payment_terms_days" in data and not (0 <= (data["payment_terms_days"] or -1) <= 120):
        raise HTTPException(400, "Payment terms must be 0–120 days")
    if "representation_default" in data and data["representation_default"] not in ("direct", "indirect"):
        raise HTTPException(400, "Representation is direct or indirect")
    if data.get("representation_default") == "indirect" and not note:
        raise HTTPException(400, "Indirect representation makes us jointly liable for the customs debt (RULE R-1): "
                                 "note the contract that agrees it")
    changes = {k: {"from": c.get(k), "to": v} for k, v in data.items() if c.get(k) != v}
    if not changes:
        return await ops_customer(company_id, user)
    await db.companies.update_one({"id": company_id}, {"$set": {**data, "updated_at": now_iso()}})
    await audit(user, "customer_updated", "company", company_id, {"changes": changes, "note": note}, company_id=company_id)
    return await ops_customer(company_id, user)


class KycDecisionIn(BaseModel):
    decision: str
    note: str | None = None
    sanctions_decision: str | None = None


class EoriVerificationIn(BaseModel):
    status: str = Field(pattern="^(verified_manually|revoked)$")
    note: str = Field(min_length=5, max_length=1000)
    document_id: str | None = None


@router.post("/ops/customers/{company_id}/eori-verification")
async def eori_verification(company_id: str, body: EoriVerificationIn, user: dict = Depends(require_perm("kyc.approve"))):
    """Audited broker override for RULE E-1 when EU EOS does not confirm the EORI (note required)."""
    await kyc_svc.verify_eori_manually(company_id, user, body.note, body.document_id, revoke=body.status == "revoked")
    return await ops_customer(company_id, user)


@router.post("/ops/customers/{company_id}/kyc-decision")
async def kyc_decision(company_id: str, body: KycDecisionIn, user: dict = Depends(require_perm("kyc.approve"))):
    await kyc_svc.decide_kyc(company_id, body.decision, user, body.note, body.sanctions_decision)
    return await ops_customer(company_id, user)


@router.post("/ops/customers/{company_id}/kyc/run")
async def ops_run_kyc(company_id: str, user: dict = Depends(require_internal)):
    if not ({"kyc.approve", "customers.write"} & set(user.get("permissions") or [])):
        raise HTTPException(403, "Missing permission: kyc.approve")
    await kyc_svc.run_kyc(company_id, user)
    await db.companies.update_one({"id": company_id}, {"$set": {"kyc_stale": False}})
    return await ops_customer(company_id, user)


@router.patch("/ops/customers/{company_id}/test-tenant")
async def mark_test_tenant(company_id: str, body: dict, user: dict = Depends(require_perm("customers.write"))):
    from core.tenant_visibility import hidden_test_companies
    token = hidden_test_companies.set(None)
    try:
        if type(body.get("test_tenant")) is not bool:
            raise HTTPException(400, "test_tenant must be true or false")
        c = await db.companies.find_one({"id": company_id}, NO_ID)
        if not c:
            raise HTTPException(404, "Company not found")
        await db.companies.update_one({"id": company_id}, {"$set": {"test_tenant": body["test_tenant"]}})
        await audit(user, "test_tenant_changed", "company", company_id,
                    {"before": bool(c.get("test_tenant")), "after": body["test_tenant"]}, company_id=company_id)
        return {"id": company_id, "test_tenant": body["test_tenant"]}
    finally:
        hidden_test_companies.reset(token)
