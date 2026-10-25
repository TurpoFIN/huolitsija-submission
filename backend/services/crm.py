"""CRM: leads, role-address contacts, opportunities through the sales stages, activity timeline, follow-ups,
opt-outs and the per-day send counter. The prospecting agent (services.prospecting) fills it; people use it through
/api/crm/*.

Stages: prospect -> contacted -> replied -> qualified -> quoting -> tender -> negotiation -> won | lost.
Converting a qualified lead creates (or links) a customer company with kyc_status not_started, so the KYC agent picks
it up, and an opportunity. A quote created for that company moves its open opportunity to `quoting`.
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from core import config
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now, now_iso

log = logging.getLogger("crm")
HEL = ZoneInfo("Europe/Helsinki")

register_index("crm_leads", "id", unique=True)
register_index("crm_leads", "ref", unique=True, sparse=True)
register_index("crm_leads", "business_id", unique=True, sparse=True)
register_index("crm_leads", [("status", 1), ("score", -1)])
register_index("crm_leads", "next_follow_up_at")
register_index("crm_contacts", "id", unique=True)
register_index("crm_contacts", [("lead_id", 1), ("email", 1)], unique=True)
register_index("crm_contacts", "email")
register_index("crm_opportunities", "id", unique=True)
register_index("crm_opportunities", [("stage", 1), ("updated_at", -1)])
register_index("crm_opportunities", "company_id")
register_index("crm_opportunities", "lead_id")
register_index("crm_activities", "id", unique=True)
register_index("crm_activities", [("lead_id", 1), ("at", -1)])
register_index("crm_activities", [("opportunity_id", 1), ("at", -1)])
register_index("crm_activities", [("direction", 1), ("message_id", 1)], unique=True, name="crm_act_dir_mid",
               partialFilterExpression={"message_id": {"$type": "string"}})
register_index("crm_optouts", "email", unique=True, sparse=True)
register_index("crm_optouts", "domain")
register_index("crm_send_counters", "day", unique=True)

STAGES = ["prospect", "contacted", "replied", "qualified", "quoting", "tender", "negotiation", "won", "lost"]
OPEN_STAGES = STAGES[:7]
STAGE_LABELS = {"prospect": ("Prospekti", "Prospect"), "contacted": ("Yhteydenotto tehty", "Contacted"),
                "replied": ("Vastannut", "Replied"), "qualified": ("Kvalifioitu", "Qualified"),
                "quoting": ("Tarjous", "Quoting"), "tender": ("Tarjouskilpailu", "Tender"),
                "negotiation": ("Neuvottelu", "Negotiation"), "won": ("Voitettu", "Won"), "lost": ("Menetetty", "Lost")}
LEAD_STATUSES = ["new", "scored", "no_contact", "contactable", "awaiting_person", "contacted", "replied", "qualified",
                 "nurture", "converted", "disqualified", "opted_out"]
LAWFUL_BASIS = {
    "code": "fi_sahkviestl_200_b2b_role_address",
    "fi": "Yritysten välinen suoramarkkinointi julkaistuun rooliosoitteeseen (Laki sähköisen viestinnän palveluista "
          "917/2014, 200 § 3 mom.), kieltäytymismahdollisuus jokaisessa viestissä; oikeutettu etu (GDPR 6.1 f).",
    "en": "Business-to-business direct marketing to a published role address (Finnish Act on Electronic Communications "
          "Services 917/2014, s. 200(3)), opt-out in every message; legitimate interest (GDPR Art. 6(1)(f)).",
}
LEAD_EDITABLE = {"name", "website", "status", "owner", "notes", "next_follow_up_at", "tags", "country", "city"}
OPP_EDITABLE = {"name", "estimated_annual_value_cents", "next_follow_up_at", "lost_reason", "owner", "lanes"}


def bot() -> dict:
    return {"type": "agent", "id": "sales", "name": "AI: sales"}


def _ref() -> str:
    return "L-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


def helsinki_day(at: datetime | None = None) -> str:
    return (at or now()).astimezone(HEL).date().isoformat()


# ---------------------------------------------------------------- activities

async def add_activity(kind: str, *, lead_id: str | None = None, opportunity_id: str | None = None, actor: dict | None = None,
                       summary: str = "", summary_fi: str = "", data: dict | None = None, **extra) -> dict:
    doc = {"id": new_id(), "kind": kind, "lead_id": lead_id, "opportunity_id": opportunity_id, "at": now_iso(),
           "actor": _actor(actor), "summary": summary, "summary_fi": summary_fi or summary, "data": data or {}, **extra}
    await db.crm_activities.insert_one(dict(doc))
    return doc


def _actor(actor: dict | None) -> dict:
    if not actor:
        return {"type": "system", "id": None, "name": "System"}
    if actor.get("type") == "agent":
        return actor
    return {"type": "user", "id": actor.get("id"), "name": actor.get("name") or actor.get("email")}


async def activities(lead_id: str | None = None, opportunity_id: str | None = None, limit: int = 200) -> list[dict]:
    q: dict = {}
    if lead_id:
        q["lead_id"] = lead_id
    if opportunity_id:
        q["opportunity_id"] = opportunity_id
    return await db.crm_activities.find(q, NO_ID).sort("at", -1).to_list(min(limit, 1000))


# ---------------------------------------------------------------- leads

async def create_lead(data: dict, actor: dict | None, *, source: str = "manual") -> dict:
    name = (data.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(400, "Enter the company name")
    bid = (data.get("business_id") or "").strip() or None
    if bid and await db.crm_leads.find_one({"business_id": bid}):
        raise HTTPException(409, "This company is already a lead")
    lead = {"id": new_id(), "ref": _ref(), "name": name, "business_id": bid, "country": data.get("country") or "FI",
            "company_form": data.get("company_form"), "tol_code": data.get("tol_code"), "tol_label": data.get("tol_label"),
            "tol_label_fi": data.get("tol_label_fi"), "registered": data.get("registered"), "website": data.get("website"),
            "domain": data.get("domain"), "address": data.get("address") or {}, "vat_registered": data.get("vat_registered"),
            "employer_register": data.get("employer_register"), "source": data.get("source") or {"source": source},
            "origin": source, "status": "new", "score": None, "score_reasons": [], "trade": None,
            "contact_ids": [], "primary_email": None, "first_touch_at": None, "last_touch_at": None, "touches": 0,
            "follow_ups_sent": 0, "next_follow_up_at": None, "opportunity_id": None, "company_id": None,
            "draft": None, "lawful_basis": None, "owner": data.get("owner"), "notes": data.get("notes"),
            "created_at": now_iso(), "updated_at": now_iso()}
    await db.crm_leads.insert_one(dict(lead))
    await add_activity("lead_created", lead_id=lead["id"], actor=actor,
                       summary=f"Lead created from {source}", summary_fi=f"Liidi luotu ({source})",
                       data={"source": lead["source"]})
    await audit(actor, "crm_lead_created", "crm_lead", lead["id"], {"name": name, "business_id": bid, "source": source})
    await emit("crm.lead_created", {"lead": lead}, background=True)
    return lead


async def get_lead(lead_id: str) -> dict:
    lead = await db.crm_leads.find_one({"id": lead_id}, NO_ID)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


async def lead_detail(lead_id: str) -> dict:
    lead = await get_lead(lead_id)
    lead["contacts"] = await db.crm_contacts.find({"lead_id": lead_id}, NO_ID).to_list(50)
    lead["opportunity"] = await db.crm_opportunities.find_one({"id": lead.get("opportunity_id")}, NO_ID) \
        if lead.get("opportunity_id") else None
    lead["activities"] = await activities(lead_id=lead_id, limit=100)
    return lead


async def list_leads(status: str | None = None, q: str | None = None, limit: int = 200) -> list[dict]:
    query: dict = {}
    if status:
        query["status"] = {"$in": status.split(",")}
    if q:
        query["$or"] = [{"name": {"$regex": re.escape(q), "$options": "i"}}, {"business_id": q}]
    return await db.crm_leads.find(query, NO_ID).sort([("score", -1), ("created_at", -1)]).to_list(min(limit, 1000))


async def update_lead(lead_id: str, body: dict, actor: dict | None) -> dict:
    lead = await get_lead(lead_id)
    if body.get("convert") is True or body.get("status") == "converted":
        return (await convert_lead(lead_id, actor))["lead"]
    sets = {k: v for k, v in body.items() if k in LEAD_EDITABLE}
    if "status" in sets and sets["status"] not in LEAD_STATUSES:
        raise HTTPException(400, f"Status must be one of: {', '.join(LEAD_STATUSES)}")
    if not sets:
        raise HTTPException(400, f"Nothing to change; editable fields: {', '.join(sorted(LEAD_EDITABLE))}")
    sets["updated_at"] = now_iso()
    await db.crm_leads.update_one({"id": lead_id}, {"$set": sets})
    await add_activity("lead_updated", lead_id=lead_id, actor=actor, summary="Lead updated: " + ", ".join(sorted(sets)),
                       summary_fi="Liidiä muokattu: " + ", ".join(sorted(sets)), data={k: v for k, v in sets.items()})
    await audit(actor, "crm_lead_updated", "crm_lead", lead_id, sets)
    return {**lead, **sets}


async def upsert_contact(lead: dict, email: str, role: str, source_url: str | None) -> dict:
    email = email.lower().strip()
    doc = {"id": new_id(), "lead_id": lead["id"], "email": email, "role": role, "kind": "role_address",
           "source_url": source_url, "lawful_basis": LAWFUL_BASIS, "status": "active", "created_at": now_iso()}
    await db.crm_contacts.update_one({"lead_id": lead["id"], "email": email}, {"$setOnInsert": doc}, upsert=True)
    return await db.crm_contacts.find_one({"lead_id": lead["id"], "email": email}, NO_ID)


# ---------------------------------------------------------------- opportunities

async def create_opportunity(body: dict, actor: dict | None) -> dict:
    lead = await get_lead(body["lead_id"]) if body.get("lead_id") else None
    company = await db.companies.find_one({"id": body["company_id"]}, NO_ID) if body.get("company_id") else None
    if body.get("company_id") and not company:
        raise HTTPException(404, "Company not found")
    if not lead and not company:
        raise HTTPException(400, "An opportunity needs a lead_id or a company_id")
    stage = body.get("stage") or "prospect"
    if stage not in STAGES:
        raise HTTPException(400, f"Stage must be one of: {', '.join(STAGES)}")
    value = body.get("estimated_annual_value_cents")
    if value is not None and (type(value) is not int or value < 0):
        raise HTTPException(400, "Estimated annual value must be whole cents")
    opp = {"id": new_id(), "name": body.get("name") or (lead or company)["name"], "lead_id": (lead or {}).get("id"),
           "company_id": (company or {}).get("id") or (lead or {}).get("company_id"), "stage": stage,
           "estimated_annual_value_cents": value, "currency": "EUR", "lanes": body.get("lanes") or [], "notes": [],
           "quote_ids": [], "stage_history": [{"from": None, "to": stage, "at": now_iso(), "by": _actor(actor)}],
           "next_follow_up_at": body.get("next_follow_up_at"), "lost_reason": None, "owner": body.get("owner"),
           "created_at": now_iso(), "updated_at": now_iso()}
    await db.crm_opportunities.insert_one(dict(opp))
    if lead:
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"opportunity_id": opp["id"], "updated_at": now_iso()}})
    await add_activity("opportunity_created", lead_id=opp["lead_id"], opportunity_id=opp["id"], actor=actor,
                       summary=f"Opportunity created at stage {stage}",
                       summary_fi=f"Myyntimahdollisuus luotu vaiheeseen {STAGE_LABELS[stage][0]}")
    await audit(actor, "crm_opportunity_created", "crm_opportunity", opp["id"], {"stage": stage, "lead_id": opp["lead_id"]})
    return opp


async def get_opportunity(opp_id: str) -> dict:
    opp = await db.crm_opportunities.find_one({"id": opp_id}, NO_ID)
    if not opp:
        raise HTTPException(404, "Opportunity not found")
    return opp


async def list_opportunities(stage: str | None = None, company_id: str | None = None, limit: int = 500) -> list[dict]:
    q: dict = {}
    if stage:
        q["stage"] = {"$in": stage.split(",")}
    if company_id:
        q["company_id"] = company_id
    return await db.crm_opportunities.find(q, NO_ID).sort("updated_at", -1).to_list(min(limit, 2000))


async def set_stage(opp_id: str, stage: str, actor: dict | None, *, reason: str | None = None) -> dict:
    if stage not in STAGES:
        raise HTTPException(400, f"Stage must be one of: {', '.join(STAGES)}")
    opp = await get_opportunity(opp_id)
    old = opp["stage"]
    if old == stage:
        return opp
    entry = {"from": old, "to": stage, "at": now_iso(), "by": _actor(actor), "reason": reason}
    sets = {"stage": stage, "updated_at": now_iso()}
    if stage == "won":
        sets["won_at"] = now_iso()
    if stage == "lost":
        sets["lost_reason"] = reason or opp.get("lost_reason")
    res = await db.crm_opportunities.update_one({"id": opp_id, "stage": old}, {"$set": sets, "$push": {"stage_history": entry}})
    if res.modified_count != 1:
        raise HTTPException(409, "The opportunity changed meanwhile; reload and try again")
    opp.update(sets)
    lead_status = {"replied": "replied", "qualified": "qualified", "lost": "disqualified"}.get(stage)
    if opp.get("lead_id") and lead_status:
        await db.crm_leads.update_one({"id": opp["lead_id"], "status": {"$nin": ["converted", "opted_out"]}},
                                      {"$set": {"status": lead_status, "updated_at": now_iso()}})
    await add_activity("stage_changed", lead_id=opp.get("lead_id"), opportunity_id=opp_id, actor=actor,
                       summary=f"Stage {old} -> {stage}" + (f" ({reason})" if reason else ""),
                       summary_fi=f"Vaihe {STAGE_LABELS[old][0]} → {STAGE_LABELS[stage][0]}" + (f" ({reason})" if reason else ""),
                       data=entry)
    await audit(actor, "crm_stage_changed", "crm_opportunity", opp_id, {"from": old, "to": stage, "reason": reason},
                company_id=opp.get("company_id"))
    await emit("crm.stage_changed", {"opportunity": opp, "from": old, "to": stage}, background=True)
    return opp


async def update_opportunity(opp_id: str, body: dict, actor: dict | None) -> dict:
    opp = await get_opportunity(opp_id)
    sets = {k: v for k, v in body.items() if k in OPP_EDITABLE}
    v = sets.get("estimated_annual_value_cents")
    if v is not None and (type(v) is not int or v < 0):
        raise HTTPException(400, "Estimated annual value must be whole cents")
    if sets:
        sets["updated_at"] = now_iso()
        await db.crm_opportunities.update_one({"id": opp_id}, {"$set": sets})
        opp.update(sets)
        await add_activity("opportunity_updated", lead_id=opp.get("lead_id"), opportunity_id=opp_id, actor=actor,
                           summary="Opportunity updated: " + ", ".join(sorted(sets)),
                           summary_fi="Myyntimahdollisuutta muokattu: " + ", ".join(sorted(sets)), data=sets)
    if body.get("note"):
        await add_note(opp_id, str(body["note"])[:4000], actor)
    if body.get("stage"):
        opp = await set_stage(opp_id, body["stage"], actor, reason=body.get("reason") or body.get("lost_reason"))
    if not sets and not body.get("stage") and not body.get("note"):
        raise HTTPException(400, "Nothing to change")
    return await get_opportunity(opp_id)


async def add_note(opp_id: str, text: str, actor: dict | None, *, kind: str = "note", data: dict | None = None) -> dict:
    note = {"id": new_id(), "kind": kind, "text": text, "at": now_iso(), "by": _actor(actor), **(data or {})}
    await db.crm_opportunities.update_one({"id": opp_id}, {"$push": {"notes": note}, "$set": {"updated_at": now_iso()}})
    opp = await get_opportunity(opp_id)
    await add_activity("note", lead_id=opp.get("lead_id"), opportunity_id=opp_id, actor=actor, summary=text[:300],
                       summary_fi=text[:300], data={"note_id": note["id"], "kind": kind, **(data or {})})
    return note


async def pipeline_summary() -> dict:
    rows = {r["_id"]: r async for r in db.crm_opportunities.aggregate([
        {"$group": {"_id": "$stage", "count": {"$sum": 1},
                    "value": {"$sum": {"$ifNull": ["$estimated_annual_value_cents", 0]}}}}])}
    stages = [{"stage": s, "label_fi": STAGE_LABELS[s][0], "label_en": STAGE_LABELS[s][1],
               "count": rows.get(s, {}).get("count", 0), "estimated_annual_value_cents": rows.get(s, {}).get("value", 0)}
              for s in STAGES]
    open_rows = [r for r in stages if r["stage"] in OPEN_STAGES]
    return {"stages": stages, "open_count": sum(r["count"] for r in open_rows),
            "open_estimated_annual_value_cents": sum(r["estimated_annual_value_cents"] for r in open_rows),
            "leads": {r["_id"]: r["n"] async for r in db.crm_leads.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])},
            "follow_ups_due": await db.crm_opportunities.count_documents(
                {"stage": {"$in": OPEN_STAGES}, "next_follow_up_at": {"$ne": None, "$lte": now_iso()}}),
            "currency": "EUR"}


# ---------------------------------------------------------------- convert a qualified lead

async def convert_lead(lead_id: str, actor: dict | None) -> dict:
    """Qualified lead -> customer company (kyc_status not_started, picked up by the KYC agent) + opportunity."""
    lead = await get_lead(lead_id)
    if lead["status"] in ("opted_out", "disqualified"):
        raise HTTPException(409, f"A lead with status {lead['status']} cannot be converted")
    company = None
    if lead.get("company_id"):
        company = await db.companies.find_one({"id": lead["company_id"]}, NO_ID)
    if not company and lead.get("business_id"):
        company = await db.companies.find_one({"business_id": lead["business_id"]}, NO_ID)
    created = False
    if not company:
        from core.refs import ytunnus_to_vat
        bid = lead.get("business_id")
        company = {"id": new_id(), "name": lead["name"], "country": lead.get("country") or "FI", "business_id": bid,
                   "vat_id": ytunnus_to_vat(bid) if bid and lead.get("country", "FI") == "FI" else None, "eori": None,
                   "address": lead.get("address") or {}, "kyc_status": "not_started", "kyc": {},
                   "sanctions_status": "not_screened", "credit_limit_cents": 0,
                   "payment_terms_days": config.DEFAULT_PAYMENT_TERMS_DAYS, "billing_email": lead.get("primary_email"),
                   "website": lead.get("website"), "status": "active", "source": "crm", "crm_lead_id": lead_id,
                   "created_at": now_iso(), "created_by": _actor(actor).get("id")}
        await db.companies.insert_one(dict(company))
        created = True
        await emit("company.created", {"company": company, "actor": actor})
    opp = await db.crm_opportunities.find_one({"id": lead.get("opportunity_id")}, NO_ID) if lead.get("opportunity_id") else None
    if opp:
        await db.crm_opportunities.update_one({"id": opp["id"]}, {"$set": {"company_id": company["id"], "updated_at": now_iso()}})
        if opp["stage"] in ("prospect", "contacted", "replied"):
            opp = await set_stage(opp["id"], "qualified", actor, reason="lead converted")
    else:
        await db.crm_leads.update_one({"id": lead_id}, {"$set": {"company_id": company["id"]}})
        opp = await create_opportunity({"lead_id": lead_id, "company_id": company["id"], "stage": "qualified"}, actor)
    await db.crm_leads.update_one({"id": lead_id}, {"$set": {"status": "converted", "company_id": company["id"],
                                                             "opportunity_id": opp["id"], "converted_at": now_iso(),
                                                             "next_follow_up_at": None, "updated_at": now_iso()}})
    await add_activity("converted", lead_id=lead_id, opportunity_id=opp["id"], actor=actor,
                       summary=f"Converted to customer {company['name']}" + (" (new company, KYC not started)" if created else ""),
                       summary_fi=f"Muutettu asiakkaaksi {company['name']}" + (" (uusi yritys, KYC aloittamatta)" if created else ""),
                       data={"company_id": company["id"], "created": created})
    await audit(actor, "crm_lead_converted", "crm_lead", lead_id, {"company_id": company["id"], "created": created},
                company_id=company["id"])
    return {"lead": await get_lead(lead_id), "company": company, "company_created": created,
            "opportunity": await get_opportunity(opp["id"])}


# ---------------------------------------------------------------- quotes move opportunities

@on("quote.created")
async def _on_quote_created(p: dict):
    q = p.get("quote") or {}
    if not q.get("company_id"):
        return
    opp = await db.crm_opportunities.find_one({"company_id": q["company_id"], "stage": {"$in": ["prospect", "contacted",
                                               "replied", "qualified"]}}, NO_ID, sort=[("updated_at", -1)])
    if not opp:
        return
    await db.crm_opportunities.update_one({"id": opp["id"]}, {"$addToSet": {"quote_ids": q["id"]}})
    await set_stage(opp["id"], "quoting", bot(), reason=f"quote {q.get('number') or q['id']} created")


# ---------------------------------------------------------------- opt-out (signed token, address or whole domain)

def _optout_secret() -> bytes:
    from core import security
    return ("crm-optout:" + (config.env("CRM_OPTOUT_SECRET") or security._secret or config.JWT_SECRET or "")).encode()


def optout_token(email: str) -> str:
    raw = email.lower().strip().encode()
    sig = hmac.new(_optout_secret(), raw, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + sig


def optout_email_from_token(token: str) -> str | None:
    try:
        b64, sig = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        good = hmac.new(_optout_secret(), raw, hashlib.sha256).hexdigest()[:32]
        return raw.decode() if hmac.compare_digest(good, sig) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def optout_url(email: str) -> str:
    base = config.env("CRM_PUBLIC_BASE_URL") or config.APP_BASE_URL or "http://localhost:8000"
    return f"{base.rstrip('/')}/api/crm/opt-out?t={urlquote(optout_token(email))}"


def _domain(email: str) -> str | None:
    from adapters.web_contacts import base_domain
    return base_domain(email.split("@")[1]) if "@" in email else None


async def is_opted_out(email: str | None, domain: str | None = None) -> bool:
    email = (email or "").lower().strip()
    q: list[dict] = [{"email": email}] if email else []
    dom = domain or (_domain(email) if email else None)
    if dom:
        q.append({"domain": dom, "scope": "domain"})
    return bool(q) and bool(await db.crm_optouts.find_one({"$or": q}))


async def add_optout(email: str, *, source: str, scope: str = "address", actor: dict | None = None) -> dict:
    email = email.lower().strip()
    domain = _domain(email)
    doc = {"id": new_id(), "email": email, "domain": domain, "scope": scope, "source": source, "at": now_iso()}
    await db.crm_optouts.update_one({"email": email}, {"$setOnInsert": doc}, upsert=True)
    if scope == "domain":
        await db.crm_optouts.update_one({"email": email}, {"$set": {"scope": "domain"}})
    cq = {"$or": [{"email": email}] + ([{"email": {"$regex": "@(.+\\.)?" + re.escape(domain) + "$"}}] if scope == "domain" and domain else [])}
    contacts = await db.crm_contacts.find(cq, NO_ID).to_list(500)
    await db.crm_contacts.update_many(cq, {"$set": {"status": "opted_out", "opted_out_at": now_iso()}})
    lead_ids = sorted({c["lead_id"] for c in contacts})
    for lid in lead_ids:
        active = await db.crm_contacts.count_documents({"lead_id": lid, "status": "active"})
        sets = {"next_follow_up_at": None, "draft": None, "updated_at": now_iso()}
        if not active:
            sets["status"] = "opted_out"
        await db.crm_leads.update_one({"id": lid, "status": {"$ne": "converted"}}, {"$set": sets})
        await add_activity("opt_out", lead_id=lid, actor=actor or {"type": "system", "id": "recipient", "name": email},
                           summary=f"{email} opted out ({scope}, {source}); no further e-mail",
                           summary_fi=f"{email} kielsi viestit ({scope}, {source}); ei enää sähköpostia",
                           data={"email": email, "scope": scope, "source": source})
    await audit(actor or {"type": "system", "id": "recipient", "name": email}, "crm_opt_out", "crm_optout", email,
                {"scope": scope, "source": source, "leads": lead_ids})
    return {"email": email, "scope": scope, "leads": lead_ids}


# ---------------------------------------------------------------- daily send counter (Helsinki day)

async def reserve_send(limit: int) -> bool:
    day = helsinki_day()
    await db.crm_send_counters.update_one({"day": day}, {"$setOnInsert": {"day": day, "count": 0}}, upsert=True)
    res = await db.crm_send_counters.update_one({"day": day, "count": {"$lt": int(limit)}},
                                                {"$inc": {"count": 1}, "$set": {"updated_at": now_iso()}})
    return res.modified_count == 1


async def release_send():
    await db.crm_send_counters.update_one({"day": helsinki_day(), "count": {"$gt": 0}}, {"$inc": {"count": -1}})


async def sends_today() -> int:
    doc = await db.crm_send_counters.find_one({"day": helsinki_day()}, NO_ID)
    return (doc or {}).get("count", 0)
