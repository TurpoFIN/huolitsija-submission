"""Quotes, AI quote parsing, claim of public quotes, acceptance, requote, PDF, ops edits, and bookings (V2).

Approval: quotes above the settings threshold, with dangerous goods, or on lanes without rates go to
`pending_approval` with core.approvals.request_approval(kind="quote_threshold"); the `approval.decided`
handler below sends (or declines) them and notifies the customer.
"""
from __future__ import annotations

import copy
import logging
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from core.approvals import cancel_approvals, request_approval
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.deps import company_scope, current_user, get_company, is_internal, require_perm
from core.email import render, send_email
from core.events import emit, on
from core.lifecycle import background_loop
from core.llm import LLMUnavailable
from core.notify import bi, notify_company, notify_role, same
from core.refs import next_ref
from core.settings import get_settings
from core.util import NO_ID, iso, new_id, now, now_iso
from core.web import base_url
from services import carriers as net
from services import dangerous_goods, pricing
import services.prepayment  # noqa: F401,E402  (registers the prepayment.book job and its invoice.paid handler)

log = logging.getLogger("quotes")
router = APIRouter(prefix="/api", tags=["quotes"])

register_index("quotes", "id", unique=True)
register_index("quotes", [("company_id", 1), ("created_at", -1)])
register_index("quotes", [("status", 1), ("created_at", -1)])
register_index("quotes", "claim_token", sparse=True)

from services import customs_border

STATUSES = ("draft", "pending_approval", "sent", "accepted", "booked", "expired", "rejected")
APPROVAL_REASON_TEXT = {"territory_unverified": customs_border.UNVERIFIED_EN, "customs_required": customs_border.POSTPONED_EN, "autopilot_off": "Autopilot is off", "threshold": "total above the approval threshold", "dangerous_goods": "dangerous goods",
                        "out_of_network": "no rates loaded for this lane",
                        "estimate_only": "only benchmark estimates (partner rates requested)",
                        "no_freight": "an option charges no freight (the Incoterm leaves the main carriage to the other party)"}
# Freight lines of the main carriage (services/pricing.FREIGHT_CODE; MANUAL is a card without a known service).
FREIGHT_LINE_CODES = {"OCEAN_FREIGHT", "AIR_FREIGHT", "ROAD_FREIGHT", "RORO_FREIGHT", "RAIL_FREIGHT", "FEEDER_FREIGHT", "MANUAL"}


def charges_freight(option: dict) -> bool:
    """Whether the customer is charged for carriage on this option. Under an Incoterm that gives the main carriage to
    the other party (an importer under DAP, an exporter under FCA) every freight line moves to not_included, and the
    option is left with the document fee: a 35 EUR "quote" that must never go to the customer unreviewed."""
    return any(ln.get("code") in FREIGHT_LINE_CODES for ln in option.get("lines") or [])


# Finnish for the review notification (the approval card shows the English text above through backendText.fi.js).
APPROVAL_REASON_TEXT_FI = {"territory_unverified": customs_border.UNVERIFIED_FI, "customs_required": customs_border.POSTPONED_FI, "autopilot_off": "Autopilotti on pois päältä", "threshold": "summa ylittää hyväksyntärajan", "dangerous_goods": "vaarallisia aineita",
                           "out_of_network": "reitille ei ole hinnastoa",
                           "estimate_only": "vain vertailuindeksiarvioita (kumppanihinnat pyydetty)",
                           "no_freight": "vaihtoehto ei sisällä rahtia (toimituslauseke jättää päärahdin toiselle osapuolelle)"}
COST_FIELDS_OPTION = ("cost_cents", "margin_cents", "margin_pct", "markup_pct",
                      "margin_rule_id", "margin_rule_name", "margin_source", "margin_floor_cents", "cost_basis", "spot_option_id",
                      "replaced_line_codes")  # s7 margin rules / contract tariffs: internal
COST_FIELDS_LINE = ("unit_cost_cents", "cost_cents", "floor_raised_cents")


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str | None = None
    origin: dict | str | int | float | None = None  # anything else gets the plain "could not find" answer
    destination: dict | str | int | float | None = None
    ready_date: str | None = None
    incoterm: str | None = None
    incoterm_place: str | None = None
    customer_role: str | None = None
    cargo: dict | None = None
    insurance: dict | bool | None = None
    customs_clearance: bool | None = None
    notes: str | None = None
    company_id: str | None = None
    send: bool = True
    ai: dict | None = None


class OptionPick(BaseModel):
    option_id: str


class ClaimIn(BaseModel):
    token: str


class ParseIn(BaseModel):
    text: str
    locale: str | None = "fi"


# ------------------------------------------------------------------------------------------------
# Views and helpers
# ------------------------------------------------------------------------------------------------
def customer_view(q: dict) -> dict:
    """Strip buy prices, margins, internal notes and the claim token before a customer (or the public) sees a quote."""
    out = copy.deepcopy(q)
    for k in ("claim_token", "notes", "ip_hash", "claim_expires_at", "notify_email", "send_authorization"):
        out.pop(k, None)
    if isinstance(out.get("sourcing"), dict):
        for k in ("needs", "errors"):  # needs carry our estimated buy cost per leg
            out["sourcing"].pop(k, None)
    for o in out.get("options") or []:
        for k in COST_FIELDS_OPTION:
            o.pop(k, None)
        for ln in o.get("lines") or []:
            for k in COST_FIELDS_LINE:
                ln.pop(k, None)
    for r in out.get("revisions") or []:
        r.pop("cost_cents", None)
    return out


def view_for(user: dict | None, q: dict) -> dict:
    if user and is_internal(user):
        out = dict(q)
        out.pop("claim_token", None)
        return out
    return customer_view(q)


def recommended_option(q: dict) -> dict | None:
    opts = q.get("options") or []
    acc = next((o for o in opts if o["id"] == q.get("accepted_option_id")), None)
    return acc or next((o for o in opts if o.get("recommended")), None) or (opts[0] if opts else None)


def summary(q: dict, internal: bool) -> dict:
    req = q.get("request") or {}
    rec = recommended_option(q)
    out = {
        "id": q["id"], "number": q.get("number"), "status": q["status"], "source": q.get("source"),
        "company_id": q.get("company_id"), "customer_name": q.get("customer_name"), "mode": (rec or {}).get("mode") or req.get("mode"),
        "requested_mode": req.get("mode"), "origin": req.get("origin"), "destination": req.get("destination"),
        "cargo_summary": req.get("cargo_summary"), "cargo": req.get("cargo"), "incoterm": req.get("incoterm"),
        "incoterm_place": req.get("incoterm_place"), "ready_date": req.get("ready_date"), "direction": req.get("direction"),
        "valid_until": q.get("valid_until"), "best_total_cents": (rec or {}).get("total_cents"),
        "options_count": len(q.get("options") or []), "approval": q.get("approval"), "shipment_id": q.get("shipment_id"),
        "shipment_reference": q.get("shipment_reference"), "accepted_option_id": q.get("accepted_option_id"),
        "dangerous_goods": bool((req.get("cargo") or {}).get("dangerous_goods")),
        "created_at": q.get("created_at"), "updated_at": q.get("updated_at"),
        "recommended_option": None,
    }
    if rec:
        ro = {"id": rec["id"], "carrier_name": rec.get("carrier_name"), "mode": rec.get("mode"), "service": rec.get("service"),
              "service_name": rec.get("service_name"), "transit_days": rec.get("transit_days"),
              "door_to_door_days": rec.get("door_to_door_days"), "total_cents": rec.get("total_cents"), "co2e_kg": rec.get("co2e_kg")}
        if internal:
            ro.update(margin_cents=rec.get("margin_cents"), margin_pct=rec.get("margin_pct"), markup_pct=rec.get("markup_pct"),
                      cost_cents=rec.get("cost_cents"))
        out["recommended_option"] = ro
    return out


# Customers see what they asked for themselves (portal, AI, claimed public quotes) and ops quotes once sent.
CUSTOMER_VISIBLE = {"$or": [{"source": {"$ne": "ops"}}, {"published": True}]}


def scope_for(user: dict) -> dict:
    scope = dict(company_scope(user))
    if not is_internal(user):
        scope.update(CUSTOMER_VISIBLE)
    return scope


async def load_quote(quote_id: str, user: dict) -> dict:
    q = await db.quotes.find_one({"id": quote_id, **scope_for(user)}, NO_ID)
    if not q:
        raise HTTPException(404, "Quote not found")
    return q


def route_label(req: dict) -> str:
    o, d = req.get("origin") or {}, req.get("destination") or {}
    return f"{o.get('name') or o.get('unlocode')} → {d.get('name') or d.get('unlocode')}"


def approval_reasons(q: dict, settings: dict) -> list[str]:
    reasons = []
    customs_reason = customs_border.review_reason(q)
    if customs_reason:
        reasons.append(customs_reason)
    totals = [o.get("total_cents") or 0 for o in q.get("options") or []]
    if totals and max(totals) > float(settings["quote_approval_threshold_eur"]) * 100:
        reasons.append("threshold")
    if ((q.get("request") or {}).get("cargo") or {}).get("dangerous_goods"):
        reasons.append("dangerous_goods")
    if not q.get("options"):
        reasons.append("out_of_network")
    elif not all(charges_freight(o) for o in q["options"]):
        reasons.append("no_freight")
    if q.get("options") and not any(o.get("firmness", "firm") == "firm" for o in q["options"]):
        reasons.append("estimate_only")
    return reasons


async def apply_approval_policy(q: dict, settings: dict, actor: dict | None, *, send: bool, automated: bool = False) -> dict:
    """Decide sent / pending_approval / draft and open an approval when needed (mutates and returns q)."""
    from services.agents import quote_agent
    req = q.get("request") or {}
    customs_border.apply(req)
    q.update({key: req.get(key) for key in ("customs_required", "customs_territory_verified", "customs_handling", "vat_excise_formalities", "customs_notice")})
    automated = automated or not actor or actor.get("type") in ("agent", "system", "ai") or actor.get("role") == "system"
    customs_hold = customs_border.quote_needs_review(q) and (customs_border.review_reason(q) == "territory_unverified" or not quote_agent.human_authorized(q))
    if (automated or customs_hold) and send:
        gate = await quote_agent.autosend(quote=q)
        if not gate.auto:
            quote_agent.hold(q, gate)
            return q
        await cancel_approvals(q["id"], "quote_threshold")
        q["approval"] = {"required": False, "reasons": [], "status": "approved",
                         "decided_by": {"type": "agent", "id": "agent:quote_agent"}, "agent_decision_id": gate.id}
        q.update(status="sent", published=True)
        return q
    reasons = approval_reasons(q, settings)
    prev = q.get("approval") or {}
    totals = [o.get("total_cents") or 0 for o in q.get("options") or []]
    amount = max(totals) if totals else None
    from core.autonomy import autopilot_scope, quote_within_policy, action
    on = await autopilot_scope(q.get("company_id"), settings, agent="quote_agent") != "off"  # s6 r94: a test tenant's grant counts here
    autonomous = bool(send and quote_within_policy(q, {**settings, "autopilot_enabled": on}) and "no_freight" not in reasons and "customs_required" not in reasons and "territory_unverified" not in reasons)
    if autonomous:  # the quote agent's decision gate (kill switch, audit row) has the last word
        from services.agents.quote_agent import gate_inline
        gate = await gate_inline(q, reasons, amount)
        autonomous = gate.auto
    if autonomous:
        await action("quote_approved", "quote", q["id"], company_id=q.get("company_id"),
                     data={"amount_cents": amount, "previous_status": q.get("status"), "agent_decision_id": gate.id},
                     reversible=True)
        prev = {"status": "approved", "reasons": reasons, "amount_cents": amount, "decided_by": {"id": "autopilot", "name": "Autopilot"}, "decided_at": now_iso()}
    still_approved = autonomous or (prev.get("status") == "approved" and set(reasons) <= set(prev.get("reasons") or [])
                      and (amount or 0) <= int((prev.get("amount_cents") or 0) * 1.05))
    if "territory_unverified" in reasons or "customs_required" in reasons and not quote_agent.human_authorized(q):
        still_approved = False
    if not send:
        # ops draft: nothing goes to the customer yet, so the review is requested when it is sent
        q["approval"] = {**prev, "required": bool(reasons) and not still_approved, "reasons": reasons,
                         "reason": reasons[0] if reasons else None}
        q["status"] = "draft"
        return q
    if reasons and not still_approved:
        text = ", ".join(APPROVAL_REASON_TEXT[r] for r in reasons)
        appr = await request_approval(
            "quote_threshold", "quote", q["id"], f"Quote {q.get('number') or ''} · {route_label(q['request'])}".strip(),
            summary=f"{q.get('customer_name') or 'Customer'} · {q['request'].get('cargo_summary')} · {len(q.get('options') or [])} options",
            reason=text, company_id=q.get("company_id"), requested_by=actor, data={"reasons": reasons, "quote_fingerprint": quote_agent.fingerprint(q)}, amount_cents=amount,
            link=f"/ops/quotes/{q['id']}", extra={"title_fi": f"Tarjous {q.get('number') or ''} · {route_label(q['request'])}".strip()})
        q["approval"] = {"required": True, "reasons": reasons, "reason": reasons[0], "approval_id": appr["id"], "status": "pending",
                         "amount_cents": amount, "requested_at": now_iso()}
        q["status"] = "pending_approval"
        text_fi = ", ".join(APPROVAL_REASON_TEXT_FI.get(r, APPROVAL_REASON_TEXT[r]) for r in reasons)
        await notify_role(["sales", "ops_agent"], bi(f"Tarkistettava tarjous: {q.get('number') or 'uusi'}",
                                                     f"Quote to review: {q.get('number') or 'new'}"),
                          bi(f"{route_label(q['request'])} — {text_fi}", f"{route_label(q['request'])} — {text}"),
                          link=f"/ops/quotes/{q['id']}", kind="approval")
    else:
        if prev.get("status") == "pending" and prev.get("approval_id"):
            await cancel_approvals(q["id"], "quote_threshold")  # the new price no longer needs a review
            prev = {**prev, "status": "cancelled"}
        if reasons:
            q["approval"] = {**prev, "required": True, "reasons": reasons}
        else:
            q["approval"] = {"required": False, "reasons": [], "reason": None, "approval_id": prev.get("approval_id"),
                             "status": prev.get("status")}
        q["status"] = "sent" if send else "draft"
    if q["status"] in ("sent", "pending_approval") and q.get("source") != "ops" or q["status"] == "sent":
        q["published"] = True
    if send and q["status"] == "sent":
        quote_agent.authorize_human(q, actor)
    return q


async def booking_blockers(q: dict, company: dict | None) -> list[dict]:
    out = []
    if q.get("status") not in ("sent", "accepted"):
        label = {"pending_approval": "is still being reviewed by our team", "booked": "is already booked",
                 "expired": "has expired", "rejected": "was declined", "draft": "has not been sent yet"}.get(q.get("status"), "cannot be booked")
        out.append({"code": "status", "message": f"This quote {label}.", "link": None})
    elif q.get("valid_until") and q["valid_until"] < now().date().isoformat():
        out.append({"code": "expired", "message": f"This quote expired on {q['valid_until']}. Request a new price.", "link": None})
    customs_reason = customs_border.review_reason(q)
    if customs_reason and (customs_reason == "territory_unverified" or not customs_border.human_approved(q)):
        out.append({"code": customs_reason, "message": APPROVAL_REASON_TEXT[customs_reason],
                    "message_fi": APPROVAL_REASON_TEXT_FI[customs_reason], "link": None})
    if not company:
        out.append({"code": "company", "message": "Your account is not linked to a company.", "link": "/portal/company"})
        return out
    kyc = company.get("kyc_status") or "not_started"
    if kyc != "approved":
        state = {"not_started": "not started", "pending": "being checked", "in_review": "in review", "rejected": "rejected"}.get(kyc, kyc)
        out.append({"code": "kyc", "link": "/portal/company",
                    "message": f"Bookings open once your company is verified (verification {state}). "
                               f"Complete or check it in Company settings: /portal/company."})
    sanc = company.get("sanctions_status") or "not_screened"
    if sanc != "clear":
        text = {"not_screened": "Your company has not been screened against sanctions lists yet",
                "potential_match": "Your company's sanctions screening is under compliance review",
                "blocked": "Your company's sanctions screening blocks bookings"}.get(sanc, f"Sanctions screening status: {sanc}")
        out.append({"code": "sanctions", "link": "/portal/company",
                    "message": f"{text}; bookings need a clear result. See /portal/company."})
    if q.get("status") in ("sent", "accepted") and kyc == "approved" and sanc == "clear":
        from services import credit  # s7 credit limit / hold
        opt = recommended_option(q)
        blk = await credit.check_booking(company, int((opt or {}).get("total_cents") or 0))
        if blk:
            out.append(blk)
    return out


def _request_for_repricing(req: dict) -> dict:
    r = {k: v for k, v in (req or {}).items() if k not in ("cargo_summary", "direction")}
    return r


async def _price(request: dict, company: dict | None, *, strict: bool) -> dict:
    try:
        return await pricing.quote_pricing(request, company=company, strict=strict)
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None


def _base_doc(priced: dict, *, source: str, settings: dict) -> dict:
    return {
        "id": new_id(), "number": None, "company_id": None, "customer_name": None, "requested_by": None, "source": source,
        "status": "sent", **{k: priced["request"].get(k) for k in ("customs_required", "customs_territory_verified", "customs_handling", "vat_excise_formalities", "customs_notice")}, "request": priced["request"], "options": priced["options"], "not_quoted": priced["not_quoted"],
        "warnings": priced["warnings"], "valid_until": priced["valid_until"], "terms_version": settings["terms_version"],
        "approval": {"required": False, "reasons": [], "reason": None, "approval_id": None, "status": None},
        "sourcing": None, "ai": None, "accepted_option_id": None, "shipment_id": None, "shipment_reference": None, "booking": None,
        "revision": 1, "revisions": [], "notes": [], "customer_message": None, "priced_at": now_iso(),
        "created_at": now_iso(), "updated_at": now_iso(), "demo": False,
    }


async def create_quote(body: dict, *, user: dict, company: dict | None, source: str, send: bool = True,
                       ai: dict | None = None) -> dict:
    settings = await get_settings()
    priced = await _price(body, company, strict=True)
    q = _base_doc(priced, source=source, settings=settings)
    q.update(number=await next_ref("Q"), company_id=company["id"] if company else None,
             customer_name=(company or {}).get("name"), requested_by=actor_ref(user), ai=ai)
    if body.get("notes"):
        q["request"]["notes"] = str(body["notes"])[:2000]
    from services import global_quote

    await global_quote.open_sourcing(q, priced.get("sourcing_needs") or [], actor=user)
    await apply_approval_policy(q, settings, user, send=send, automated=source in ("email", "edi"))
    await db.quotes.insert_one(dict(q))
    await audit(user, "quote_created", "quote", q["id"], {"number": q["number"], "status": q["status"],
                                                          "options": len(q["options"])}, company_id=q.get("company_id"))
    await emit("quote.created", {"quote": q, "actor": user})
    return q


async def _guard_started_group_repricing(q: dict) -> None:
    from services.carrier_booking import guard_started_group
    if q.get("id"):
        async for shipment in db.shipments.find({"quote_id": q["id"], "legs.booking_group": {"$exists": True}}, NO_ID):
            await guard_started_group(shipment, operation="reprice")


async def reprice_in_place(q: dict, user: dict | None) -> dict:
    await _guard_started_group_repricing(q)
    previous = copy.deepcopy(q)
    company = await get_company(q["company_id"]) if q.get("company_id") else None
    settings = await get_settings()
    priced = await _price(_request_for_repricing(q["request"]), company, strict=False)
    rev = {"revision": q.get("revision", 1), "priced_at": q.get("priced_at"), "valid_until": q.get("valid_until"),
           "best_total_cents": (recommended_option(q) or {}).get("total_cents"), "options": len(q.get("options") or [])}
    q.update(request={**priced["request"], **({"notes": q["request"].get("notes")} if q["request"].get("notes") else {})},
             options=priced["options"], not_quoted=priced["not_quoted"], warnings=priced["warnings"],
             valid_until=priced["valid_until"], revision=q.get("revision", 1) + 1, revisions=(q.get("revisions") or []) + [rev],
             priced_at=now_iso(), updated_at=now_iso(), accepted_option_id=None)
    from services import global_quote

    await global_quote.open_sourcing(q, priced.get("sourcing_needs") or [], actor=user)
    from services.agents.quote_agent import carry_customs_review
    await carry_customs_review(previous, q)
    if q.get("company_id") or not user or user.get("role") == "system" or user.get("type") == "agent" or customs_border.quote_needs_review(q):
        await apply_approval_policy(q, settings, user, send=True)
    else:
        q["status"] = "sent"
    return q


# ------------------------------------------------------------------------------------------------
# Routes: list, create, parse, companies, claim
# ------------------------------------------------------------------------------------------------
@router.get("/quotes")
async def list_quotes(status: str | None = None, source: str | None = None, q: str | None = None,
                      company_id: str | None = None, include_anonymous: bool = False, limit: int = 300,
                      user: dict = Depends(current_user)):
    internal = is_internal(user)
    base: dict = scope_for(user)
    if not internal and user.get("org_type") != "customer":
        raise HTTPException(403, "Not available for this account")
    if internal and company_id:
        base["company_id"] = company_id
    search: list[dict] = []
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        search = [{"$or": [{f: rx} for f in ("number", "customer_name", "request.origin.name", "request.destination.name",
                                             "request.origin.city", "request.destination.city", "request.origin.unlocode",
                                             "request.destination.unlocode")]}]
    # anonymous web quotes nobody has claimed are leads, not work: listed only under source=public
    hide_anonymous = internal and not company_id and not include_anonymous and source != "public"
    anonymous_filter = [{"$or": [{"source": {"$ne": "public"}}, {"company_id": {"$ne": None}}]}] if hide_anonymous else []

    def match(*, with_source: bool, with_anonymous_filter: bool) -> dict:
        m = dict(base)
        if with_source and source:
            m["source"] = source
        clauses = search + (anonymous_filter if with_anonymous_filter else [])
        if clauses:
            m["$and"] = clauses
        return m

    listed = match(with_source=True, with_anonymous_filter=True)
    counts = {s: 0 for s in STATUSES}
    async for row in db.quotes.aggregate([{"$match": listed}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    counts["all"] = sum(counts[s] for s in STATUSES)
    query = dict(listed)
    if status and status != "all":
        query["status"] = status
    docs = await db.quotes.find(query, NO_ID).sort("created_at", -1).to_list(min(max(limit, 1), 500))
    items = [summary(d, internal) for d in docs]
    sources = {}
    if internal:
        every = match(with_source=False, with_anonymous_filter=False)
        async for row in db.quotes.aggregate([{"$match": every}, {"$group": {"_id": "$source", "n": {"$sum": 1}}}]):
            sources[row["_id"] or "portal"] = row["n"]
        sources["public_unclaimed"] = await db.quotes.count_documents({**every, "source": "public", "company_id": None})
    return {"items": items, "total": len(items), "counts": counts, "source_counts": sources}


@router.post("/quotes")
async def post_quote(body: QuoteIn, request: Request, user: dict = Depends(current_user)):
    """Answers within 25 s; a slower quote finishes in the background and is polled at /api/requests/{id}."""
    from core.bounded import bounded
    return await bounded("quote.create", _post_quote(body, request, user), user=user)


async def _post_quote(body: QuoteIn, request: Request, user: dict):
    data = body.model_dump(exclude_none=True)
    if is_internal(user):
        if "quotes.write" not in user["permissions"]:
            raise HTTPException(403, "Missing permission: quotes.write")
        if not body.company_id:
            raise HTTPException(400, "Choose the customer company for this quote.")
        company = await get_company(body.company_id)
        if not company:
            raise HTTPException(400, "Customer company not found.")
        source = "ops"
        send = body.send
    else:
        if "company.book" not in user["permissions"]:
            raise HTTPException(403, "Your role cannot request quotes.")
        company = await get_company(user["company_id"])
        if not company:
            raise HTTPException(400, "Your account is not linked to a company.")
        source = "ai" if body.ai else "portal"
        send = True
    q = await create_quote(data, user=user, company=company, source=source, send=send, ai=body.ai)
    if is_internal(user) and q["status"] == "sent":
        await notify_company(q["company_id"], bi(f"Tarjous {q['number']} on valmis", f"Quote {q['number']} is ready"),
                             same(route_label(q["request"])),
                             link=f"/portal/quotes/{q['id']}", email=True, kind="quote", base_url=base_url(request))
    return {"quote": view_for(user, q)}


@router.post("/quotes/parse")
async def parse_quote_text(body: ParseIn, user: dict = Depends(current_user)):
    from services import quoting_agent

    text = (body.text or "").strip()
    if len(text) < 8:
        raise HTTPException(400, "Describe the shipment in a sentence or two (what, how much, from where, to where, when).")
    if len(text) > 4000:
        raise HTTPException(400, "Keep the description under 4 000 characters.")
    try:
        return await quoting_agent.parse_request(text, locale=body.locale or user.get("locale") or "fi", user=user)
    except LLMUnavailable:
        raise HTTPException(503, "AI is not available right now. Fill in the form instead.") from None
    except quoting_agent.ParseFailed as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/quotes/companies")
async def quote_companies(q: str | None = None, user: dict = Depends(require_perm("quotes.write"))):
    query: dict = {}
    if q:
        rx = re.escape(q.strip())
        query["$or"] = [{"name": {"$regex": rx, "$options": "i"}}, {"business_id": {"$regex": rx, "$options": "i"}}]
    items = await db.companies.find(query, {"_id": 0, "id": 1, "name": 1, "business_id": 1, "kyc_status": 1,
                                            "sanctions_status": 1, "country": 1}).sort("name", 1).to_list(50)
    return {"items": items, "total": len(items)}


@router.post("/quotes/claim")
async def claim_quote(body: ClaimIn, user: dict = Depends(current_user)):
    if user.get("org_type") != "customer" or not user.get("company_id"):
        raise HTTPException(403, "Sign in with a customer account to save this quote.")
    q = await db.quotes.find_one({"claim_token": body.token}, NO_ID)
    if not q:
        raise HTTPException(404, "This quote link is invalid. Run the quote again.")
    if q.get("company_id"):
        if q["company_id"] == user["company_id"]:
            return {"id": q["id"], "quote": customer_view(q)}
        raise HTTPException(409, "This quote has already been saved to another account.")
    if q.get("claim_expires_at") and q["claim_expires_at"] < now_iso():
        raise HTTPException(410, "This quote link has expired. Run the quote again.")
    company = await get_company(user["company_id"])
    q.update(company_id=company["id"], customer_name=company.get("name"), requested_by=actor_ref(user),
             number=q.get("number") or await next_ref("Q"), claimed_at=now_iso(), updated_at=now_iso())
    # price again with the company's own terms (markup, VAT registration, approval policy)
    await reprice_in_place(q, user)
    await db.quotes.replace_one({"id": q["id"]}, dict(q))
    await audit(user, "quote_claimed", "quote", q["id"], {"number": q["number"]}, company_id=company["id"])
    return {"id": q["id"], "quote": customer_view(q)}


# ------------------------------------------------------------------------------------------------
# Routes: single quote
# ------------------------------------------------------------------------------------------------
@router.get("/quotes/{quote_id}")
async def get_quote(quote_id: str, user: dict = Depends(current_user)):
    q = await load_quote(quote_id, user)
    out = view_for(user, q)
    company = await get_company(q["company_id"]) if q.get("company_id") else None
    out["booking_blockers"] = await booking_blockers(q, company)
    out["company"] = {k: (company or {}).get(k) for k in ("id", "name", "kyc_status", "sanctions_status", "vat_registered")} if company else None
    return {"quote": out}


@router.post("/quotes/{quote_id}/accept")
async def accept_quote(quote_id: str, body: OptionPick, user: dict = Depends(current_user)):
    q = await load_quote(quote_id, user)
    if not is_internal(user) and "company.book" not in user["permissions"]:
        raise HTTPException(403, "Your role cannot book shipments.")
    if q["status"] not in ("sent", "accepted"):
        company = await get_company(q["company_id"]) if q.get("company_id") else None
        return {"quote": view_for(user, q), "booking_blockers": await booking_blockers(q, company)}
    opt = next((o for o in q.get("options") or [] if o["id"] == body.option_id), None)
    if not opt:
        raise HTTPException(404, "That option is not part of this quote.")
    if opt.get("bookable") is False:
        raise HTTPException(409, "This option is an estimate. The firm price is being sourced from our partners; "
                                 "you can book it when the quote updates.")
    today = now().date().isoformat()
    if (opt.get("valid_until") or q.get("valid_until") or today) < today:
        raise HTTPException(409, f"This option expired on {opt.get('valid_until')}. Use Requote to get current prices.")
    await db.quotes.update_one({"id": quote_id}, {"$set": {"accepted_option_id": opt["id"], "status": "accepted",
                                                           "accepted_at": now_iso(), "accepted_by": actor_ref(user),
                                                           "updated_at": now_iso()}})
    q = await db.quotes.find_one({"id": quote_id}, NO_ID)
    await audit(user, "quote_option_accepted", "quote", quote_id, {"option_id": opt["id"], "total_cents": opt["total_cents"]},
                company_id=q.get("company_id"))
    company = await get_company(q["company_id"]) if q.get("company_id") else None
    return {"quote": view_for(user, q), "booking_blockers": await booking_blockers(q, company)}


@router.post("/quotes/{quote_id}/requote")
async def requote(quote_id: str, user: dict = Depends(current_user)):
    q = await load_quote(quote_id, user)
    if q["status"] == "booked":
        await _guard_started_group_repricing(q)
        raise HTTPException(409, "This quote is already booked.")
    if not is_internal(user) and "company.book" not in user["permissions"]:
        raise HTTPException(403, "Your role cannot request quotes.")
    await reprice_in_place(q, user)
    await db.quotes.replace_one({"id": quote_id}, dict(q))
    await audit(user, "quote_requoted", "quote", quote_id, {"revision": q["revision"]}, company_id=q.get("company_id"))
    return {"quote": view_for(user, q)}


@router.get("/quotes/{quote_id}/pdf")
async def quote_pdf(quote_id: str, lang: str = "fi", user: dict = Depends(current_user)):
    from services.quote_pdf import render_quote_pdf

    q = await load_quote(quote_id, user)
    company = await get_company(q["company_id"]) if q.get("company_id") else None
    pdf = await render_quote_pdf(customer_view(q), company, lang=lang if lang in ("fi", "en") else "fi")
    name = f"{q.get('number') or 'quote'}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


class LineIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    description: str
    qty: float = 1
    unit_cost_cents: int = 0
    unit_price_cents: int | None = None
    vat_code: str = "ZERO_INTL_TRANSPORT"
    is_pass_through: bool = False
    stage: str | None = None
    payer: str | None = None


class OptionEdit(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    lines: list[LineIn] | None = None
    margin_pct: float | None = None


class ManualOption(BaseModel):
    model_config = ConfigDict(extra="allow")
    carrier_id: str | None = None
    carrier_name: str | None = None
    mode: str
    service_name: str
    transit_days: int
    lines: list[LineIn]
    margin_pct: float | None = None


class QuotePatch(BaseModel):
    options: list[OptionEdit] | None = None
    add_manual_option: ManualOption | None = None
    remove_option_ids: list[str] | None = None
    valid_until: str | None = None
    note: str | None = None
    customer_message: str | None = None
    send: bool = False


def _clean_lines(lines: list[LineIn], previous: list[dict] | None = None) -> list[dict]:
    """Validate edited lines. Finnish texts survive edits: an unchanged line keeps its description_fi / note_fi,
    a standard charge code gets the standard Finnish name, anything else falls back to the text as typed."""
    prev = {(p.get("code"), p.get("description")): p for p in previous or []}
    out = []
    for ln in lines:
        d = ln.model_dump()
        if d["vat_code"] not in pricing.VAT_CODES:
            raise HTTPException(400, f"Unknown VAT code {d['vat_code']} on line {d['description']}.")
        if d["qty"] < 0 or d["unit_cost_cents"] < 0 or (d.get("unit_price_cents") or 0) < 0:
            raise HTTPException(400, f"Line {d['description']}: quantities and prices cannot be negative.")
        d["code"] = re.sub(r"[^A-Z0-9_]", "", d["code"].upper()) or "MANUAL"
        d["description"] = d["description"].strip()[:200] or d["code"]
        old = prev.get((d["code"], d["description"])) or {}
        en_std, fi_std = pricing.LINE_TEXT.get(d["code"], (None, None))
        if not d.get("description_fi") or d["description_fi"] == d["description"]:
            if old.get("description_fi") and old["description_fi"] != old.get("description"):
                d["description_fi"] = old["description_fi"]
            elif fi_std and d["description"] == en_std:
                d["description_fi"] = fi_std
            else:
                d["description_fi"] = d["description"]
        if d.get("note") and d.get("note") == old.get("note") and old.get("note_fi"):
            d["note_fi"] = old["note_fi"]
        else:
            d["note_fi"] = pricing.note_fi(d.get("note"))
        d["stage"] = d.get("stage") or pricing.STAGE_OF_CODE.get(d["code"], "main")
        d["payer"] = d.get("payer") if d.get("payer") in ("SHIPPER", "CONSIGNEE") else "CONSIGNEE"
        out.append(d)
    return out


@router.patch("/quotes/{quote_id}")
async def patch_quote(quote_id: str, body: QuotePatch, request: Request, user: dict = Depends(require_perm("quotes.write"))):
    q = await db.quotes.find_one({"id": quote_id}, NO_ID)
    if not q:
        raise HTTPException(404, "Quote not found")
    if q["status"] in ("booked",):
        await _guard_started_group_repricing(q)
        raise HTTPException(409, "A booked quote cannot be edited; change the shipment instead.")
    settings = await get_settings()
    vat_std = float(settings.get("vat_rate_standard_pct", 25.5))
    by_id = {o["id"]: o for o in q.get("options") or []}
    changes = []
    try:
        for edit in body.options or []:
            opt = by_id.get(edit.id)
            if not opt:
                raise HTTPException(404, f"Option {edit.id} not found on this quote.")
            if edit.lines is not None:
                opt["lines"] = _clean_lines(edit.lines, opt.get("lines"))
            pricing.recompute_option(opt, markup_pct=edit.margin_pct, vat_standard=vat_std)
            opt["edited_by"] = actor_ref(user)
            opt["edited_at"] = now_iso()
            changes.append({"option": edit.id, "total_cents": opt["total_cents"], "margin_pct": edit.margin_pct})
        if body.remove_option_ids:
            q["options"] = [o for o in q["options"] if o["id"] not in set(body.remove_option_ids)]
            changes.append({"removed": body.remove_option_ids})
        if body.add_manual_option:
            q.setdefault("options", []).append(await _manual_option(q, body.add_manual_option, user, vat_std))
            changes.append({"added_manual_option": body.add_manual_option.service_name})
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None
    if body.valid_until:
        try:
            vu = date.fromisoformat(body.valid_until[:10])
        except ValueError:
            raise HTTPException(400, "Valid until must be a date (YYYY-MM-DD).") from None
        if vu < now().date():
            raise HTTPException(400, "Valid until cannot be in the past.")
        q["valid_until"] = vu.isoformat()
        for o in q.get("options") or []:
            o["valid_until"] = min(o.get("valid_until") or vu.isoformat(), vu.isoformat()) if o.get("rate_source") != "manual" else vu.isoformat()
    if body.note:
        q.setdefault("notes", []).append({"id": new_id(), "text": body.note[:2000], "by": actor_ref(user), "at": now_iso()})
    if body.customer_message is not None:
        q["customer_message"] = body.customer_message.strip()[:2000] or None
    # RULE D-5: DG quotes must carry a DG surcharge
    q["warnings"] = [w for w in q.get("warnings") or [] if w.get("code") != "dg.surcharge_missing"]
    if ((q.get("request") or {}).get("cargo") or {}).get("dangerous_goods"):
        missing = [o["carrier_name"] for o in q.get("options") or [] if not any(ln["code"] == "DG" for ln in o.get("lines") or [])]
        if missing:
            q["warnings"].append({"code": "dg.surcharge_missing", "level": "warning", "params": {"carriers": ", ".join(missing)},
                                  "message": f"Dangerous goods quote without a DG surcharge line: {', '.join(missing)}."})
    if q.get("options"):
        q["options"] = pricing.rank(q["options"], limit=20)
        q["valid_until"] = min(o["valid_until"] for o in q["options"]) if not body.valid_until else q["valid_until"]
    q["updated_at"] = now_iso()
    if changes or body.valid_until:
        q.setdefault("revisions", []).append({"revision": q.get("revision", 1), "edited_by": actor_ref(user), "at": now_iso(),
                                              "changes": changes, "valid_until": q.get("valid_until")})
        q["revision"] = q.get("revision", 1) + 1
    if body.send:
        if q["status"] in ("rejected", "expired"):
            q["status"] = "draft"
        await apply_approval_policy(q, settings, user, send=True)
    await db.quotes.replace_one({"id": quote_id}, dict(q))
    await audit(user, "quote_edited", "quote", quote_id, {"changes": changes, "send": body.send}, company_id=q.get("company_id"))
    if body.send and q["status"] == "sent" and q.get("company_id"):
        await notify_company(q["company_id"], bi(f"Tarjous {q['number']} on valmis", f"Quote {q['number']} is ready"),
                             same(route_label(q["request"])),
                             link=f"/portal/quotes/{q['id']}", email=True, kind="quote", base_url=base_url(request))
    return {"quote": view_for(user, q)}


async def _manual_option(q: dict, m: ManualOption, user: dict, vat_std: float) -> dict:
    if m.mode not in ("sea", "road", "air", "rail"):
        raise HTTPException(400, "Mode must be sea, road, air or rail.")
    carrier = await db.carriers.find_one({"id": m.carrier_id}, NO_ID) if m.carrier_id else None
    name = (carrier or {}).get("name") or (m.carrier_name or "").strip()
    if not name:
        raise HTTPException(400, "Choose a carrier or type its name.")
    if not m.lines:
        raise HTTPException(400, "A manual option needs at least one price line.")
    req = q["request"]
    o, d = req["origin"], req["destination"]
    ready = date.fromisoformat(req["ready_date"])
    etd = ready + timedelta(days=1)
    eta = etd + timedelta(days=max(0, m.transit_days))
    km = net.leg_distance_km(m.mode, o, d)
    weight_t = pricing.cargo_metrics(req["cargo"])["co2_weight_t"]
    factor = (carrier or {}).get("co2_factor_g_tkm") or pricing._default_factor(
        {"sea": "FCL", "road": "ROAD_FTL", "air": "AIR", "rail": "RAIL_FCL"}[m.mode], km)
    regime = pricing._liability(req, {"mode": m.mode})
    opt = {
        "id": "opt_m" + new_id().replace("-", "")[:9], "mode": m.mode, "service": "MANUAL", "service_name": m.service_name.strip()[:120],
        "carrier_id": (carrier or {}).get("id"), "carrier_name": name, "carrier_code": (carrier or {}).get("code"), "rate_card_id": None,
        "legs": [{"seq": 1, "kind": "main", "mode": m.mode, "from": pricing._leg_loc(o), "to": pricing._leg_loc(d),
                  "carrier_id": (carrier or {}).get("id"), "carrier_name": name, "transit_days": m.transit_days,
                  "distance_km": round(km), "service": "MANUAL", "etd": etd.isoformat(), "eta": eta.isoformat(),
                  "co2e_kg": round(km * weight_t * factor / 1000.0, 1)}],
        "via": [], "rate_source": "manual", "on_time_pct": (carrier or {}).get("on_time_pct"),
        "lines": _clean_lines(m.lines), "not_included": [], "etd": etd.isoformat(), "eta": eta.isoformat(),
        "transit_days": m.transit_days, "door_to_door_days": (eta - ready).days, "schedule_source": "manual",
        "co2e_kg": round(km * weight_t * factor / 1000.0, 1),
        "co2": {"method": "GLEC Framework default intensities (well-to-wheel), indicative", "distance_km": round(km),
                "weight_t": round(weight_t, 3), "weight_assumed": False},
        "liability_regime": regime, "liability": {"regime": regime, **pricing.LIABILITY[regime]}, "forwarder_role": "principal",
        "chargeable": {}, "valid_until": q.get("valid_until"), "warnings": [], "dg_accepted": True, "recommended": False,
        "rank_reason": None, "tags": [], "created_by": actor_ref(user),
    }
    if req["cargo"].get("dangerous_goods") and carrier:
        ok, reason = net.dg_acceptance(carrier, req["cargo"].get("dg") or {})
        if not ok:
            raise HTTPException(400, reason)
    pricing.recompute_option(opt, markup_pct=m.margin_pct, vat_standard=vat_std)
    return opt


# ------------------------------------------------------------------------------------------------
# Bookings
# ------------------------------------------------------------------------------------------------
class PartyIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    address: dict | None = None
    contact: dict | None = None
    eori: str | None = None
    vat_id: str | None = None


class StopIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    address: dict | None = None
    date: str | None = None
    window: str | None = None
    contact: dict | None = None
    instructions: str | None = None


class BookingIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    quote_id: str
    option_id: str
    shipper: PartyIn
    consignee: PartyIn
    notify: PartyIn | None = None
    pickup: StopIn = StopIn()
    delivery: StopIn = StopIn()
    cargo: dict | None = None
    incoterm_place: str | None = None
    customer_reference: str | None = None
    po_numbers: list[str] = []
    special_instructions: str | None = None
    accept_terms: bool = False


def _check_party(p: PartyIn | None, label: str, required: bool = True) -> dict | None:
    if p is None or (not required and not (p.name or "").strip()):
        return None
    d = p.model_dump()
    if not (d.get("name") or "").strip():
        raise HTTPException(400, f"Give the {label}'s name.")
    addr = d.get("address") or {}
    for f, txt in (("street", "street address"), ("city", "city"), ("country", "country")):
        if not (addr.get(f) or "").strip():
            raise HTTPException(400, f"Give the {label}'s {txt}.")
    addr["country"] = addr["country"].strip().upper()[:2]
    if addr["country"] in net.SANCTIONED_COUNTRIES:
        raise HTTPException(400, "Parties in Russia or Belarus need a compliance review; contact ops@huolitsija.fi.")
    d["address"] = addr
    if (d.get("eori") or "").strip():
        from services.customs import eori_normalise, eori_shape_ok

        if not eori_shape_ok(d["eori"]):
            raise HTTPException(400, f"The {label}'s EORI number looks wrong (e.g. FI1234567-8).")
        d["eori"] = eori_normalise(d["eori"])  # FI1234567-8, as Tulli and EU EOS hold it
    else:
        d["eori"] = None
    return d


def _check_stop(s: StopIn, label: str, *, date_required: bool) -> dict:
    d = s.model_dump()
    addr = d.get("address") or {}
    for f, txt in (("street", "street address"), ("city", "city"), ("country", "country")):
        if not (addr.get(f) or "").strip():
            raise HTTPException(400, f"Give the {label} {txt}.")
    addr["country"] = addr["country"].strip().upper()[:2]
    d["address"] = addr
    if d.get("date"):
        try:
            dd = date.fromisoformat(d["date"][:10])
        except ValueError:
            raise HTTPException(400, f"The {label} date must be a date (YYYY-MM-DD).") from None
        if dd < now().date():
            raise HTTPException(400, f"The {label} date cannot be in the past.")
        d["date"] = dd.isoformat()
    elif date_required:
        raise HTTPException(400, f"Give the {label} date.")
    return d


def _check_dg(cargo: dict) -> dict:
    dg = dict(cargo.get("dg") or {})
    un = re.sub(r"\s+", "", str(dg.get("un_number") or "").upper())
    un = un if un.startswith("UN") else f"UN{un}"
    if not re.fullmatch(r"UN\d{4}", un):
        raise HTTPException(400, "Dangerous goods: give the UN number (four digits, e.g. UN1263).")
    if not (dg.get("proper_shipping_name") or "").strip():
        raise HTTPException(400, "Dangerous goods: give the proper shipping name.")
    cls = str(dg.get("class") or "").strip()
    if not re.fullmatch(r"[1-9](\.[1-6])?[A-Z]?", cls):
        raise HTTPException(400, "Dangerous goods: give the class (e.g. 3 or 6.1).")
    pg = str(dg.get("packing_group") or "").strip().upper()
    if cls.split(".")[0] in ("3", "4", "5", "8") or cls == "6.1":
        if pg not in ("I", "II", "III"):
            raise HTTPException(400, "Dangerous goods: give the packing group (I, II or III).")
    if not re.search(r"\d{6,}", re.sub(r"[\s\-()]", "", str(dg.get("emergency_phone") or ""))):
        raise HTTPException(400, "Dangerous goods: give a 24-hour emergency phone number.")
    dg.update(un_number=un, **{"class": cls}, packing_group=pg or None)
    return dg


@router.post("/bookings")
async def create_booking(body: BookingIn, request: Request, user: dict = Depends(current_user)):
    """Answers within 25 s; a slower booking finishes in the background and is polled at /api/requests/{id}."""
    from core.bounded import bounded
    return await bounded("booking.create", book(body, request, user), user=user)


async def book(body: BookingIn, request: Request, user: dict, *, prepaid: dict | None = None):
    """prepaid: the paid prepayment invoice for this quote option (services.prepayment); it lifts that one blocker."""
    if not is_internal(user) and "company.book" not in user["permissions"]:
        raise HTTPException(403, "Your role cannot book shipments.")
    q = await load_quote(body.quote_id, user)
    opt = next((o for o in q.get("options") or [] if o["id"] == body.option_id), None)
    if not opt:
        raise HTTPException(404, "That option is not part of this quote.")
    if opt.get("bookable") is False:
        raise HTTPException(409, "This option is an estimate. The firm price is being sourced from our partners; "
                                 "you can book it when the quote updates.")
    company = await get_company(q["company_id"]) if q.get("company_id") else None
    blockers = await booking_blockers(q, company)
    if any(b["code"] in ("kyc", "sanctions", "company") for b in blockers):
        raise HTTPException(409, " ".join(b["message"] for b in blockers if b["code"] in ("kyc", "sanctions", "company")))
    from services import prepayment
    if prepaid and prepaid.get("quote_id") == q["id"] and prepaid.get("option_id") == opt["id"] \
            and prepaid.get("status") == "paid" and prepaid.get("origin") == "prepayment":
        blockers = [b for b in blockers if not b.get("prepayment")]
    prepay = prepayment.only_prepayment(blockers)
    if blockers and not prepay:
        raise HTTPException(409, blockers[0]["message"])
    if (opt.get("valid_until") or "9999") < now().date().isoformat():
        raise HTTPException(409, f"This option expired on {opt['valid_until']}. Use Requote to get current prices.")
    if not body.accept_terms:
        raise HTTPException(400, "Accept the terms (NSAB 2015 (PSYM 2015) and Huolitsija's general terms) to book.")
    req = q["request"]
    shipper = _check_party(body.shipper, "shipper")
    consignee = _check_party(body.consignee, "consignee")
    notify = _check_party(body.notify, "notify party", required=False)
    # door addresses default to the parties' addresses; pickup date defaults to the quoted ready date
    if not ((body.pickup.address or {}).get("street")):
        body.pickup.address = dict(shipper["address"])
    if not ((body.delivery.address or {}).get("street")):
        body.delivery.address = dict(consignee["address"])
    if not body.pickup.date:
        body.pickup.date = max(req.get("ready_date") or "", now().date().isoformat())
    pickup = _check_stop(body.pickup, "pickup", date_required=True)
    delivery = _check_stop(body.delivery, "delivery", date_required=False)
    if req.get("incoterm") and not (body.incoterm_place or req.get("incoterm_place") or "").strip():
        raise HTTPException(400, f"Name the place for {req['incoterm']} (Incoterms need a named place).")
    cargo = {**(req.get("cargo") or {}), **{k: v for k, v in (body.cargo or {}).items() if v not in (None, "")}}
    try:
        norm = pricing.normalize_cargo(cargo)
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None
    cargo = {**cargo, **{k: v for k, v in norm.items() if v is not None}}
    if req["cargo"].get("dangerous_goods") and not cargo.get("dangerous_goods"):
        raise HTTPException(400, "This quote was priced as dangerous goods; give the DG details or request a new quote.")
    if cargo.get("dangerous_goods"):
        if not req["cargo"].get("dangerous_goods"):
            raise HTTPException(409, "The quote was priced without dangerous goods. Request a new quote with the DG details.")
        cargo["dg"] = _check_dg(cargo)
        for leg in opt.get("legs") or []:  # RULE D-2 with the full DG data
            car = await db.carriers.find_one({"id": leg.get("carrier_id")}, NO_ID) if leg.get("carrier_id") else None
            if car:
                ok, reason = net.dg_acceptance(car, cargo["dg"])
                if not ok:
                    raise HTTPException(409, f"{reason}. Choose another option or contact our ops team.")
    dg_res = None
    if cargo.get("dangerous_goods") or cargo.get("dg_items"):  # s12: UN dataset check for the booked mode
        dg_res = dangerous_goods.check_cargo(cargo, opt.get("mode"), opt.get("legs"))
        if dg_res["status"] == "forbidden":
            await dangerous_goods.record_check(dg_res, stage="booking_refused", entity="quote", entity_id=q["id"],
                                               company_id=q.get("company_id"))
            raise HTTPException(409, dangerous_goods.first_block(dg_res)["en"])
    warnings = []
    qw, bw = (req.get("cargo") or {}).get("weight_kg"), cargo.get("weight_kg")
    if qw and bw and abs(bw - qw) / qw > 0.10:
        warnings.append(f"Weight changed from {qw:,.0f} kg to {bw:,.0f} kg; the final price follows the actual chargeable weight.")
    from services import party_screening  # s12: every party against the sanctions / denied-party lists
    pre_screen = await party_screening.screen_parties_for_booking({"shipper": shipper, "consignee": consignee, "notify": notify,
                                                                   "company": company, "legs": opt.get("legs"), "quote_id": q["id"]})
    if pre_screen["hold"]:
        warnings.append(pre_screen["message"])  # neutral text; list details stay internal
    if prepay:  # s2: issue the prepayment invoice; the booking is made when it is paid (services.prepayment)
        inv = await prepayment.request(q, opt, body.model_dump(mode="json"), user, company)
        q = await db.quotes.find_one({"id": q["id"]}, NO_ID)
        return {"booking": None, "shipment_id": None, "shipment_reference": None, "prepayment": prepayment.summary(inv),
                "warnings": warnings, "quote": view_for(user, q)}
    # one booking per quote, even with double clicks
    locked = await db.quotes.find_one_and_update({"id": q["id"], "status": {"$in": ["sent", "accepted"]}},
                                                 {"$set": {"status": "booking", "status_before_booking": q["status"],
                                                           "booking_started_at": now_iso(), "updated_at": now_iso()}})
    if not locked:
        raise HTTPException(409, "This quote is already being booked.")
    settings = await get_settings()
    booking = {
        "id": new_id(), "quote_id": q["id"], "quote_number": q.get("number"), "option_id": opt["id"],
        "shipper": shipper, "consignee": consignee, "notify": notify, "pickup": pickup, "delivery": delivery,
        "cargo": cargo, "incoterm": req.get("incoterm"), "incoterm_place": (body.incoterm_place or req.get("incoterm_place") or None),
        "customer_reference": (body.customer_reference or "").strip()[:80] or None,
        "po_numbers": [p.strip()[:60] for p in body.po_numbers if p and p.strip()][:20],
        "special_instructions": (body.special_instructions or "").strip()[:2000] or None,
        "accept_terms": True, "terms_version": settings["terms_version"], "terms_accepted_at": now_iso(),
        "forwarder_role": opt.get("forwarder_role", "principal"), "liability_regime": opt.get("liability_regime"),
        "insurance": (req.get("insurance") or {}), "warnings": warnings, "created_by": actor_ref(user), "created_at": now_iso(),
    }
    try:
        from services.shipments import create_shipment  # V1 (lazy: may be mid-deploy)
    except ImportError:
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": q["status"]}})
        raise HTTPException(503, "Booking is temporarily unavailable; your quote is saved. Try again in a few minutes.") from None
    v1_quote, v1_option, v1_booking = _v1_payload(q, opt, booking)
    try:
        shipment = await create_shipment(company_id=q["company_id"], quote=v1_quote, option=v1_option, booking=v1_booking, actor=user)
    except HTTPException:
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": q["status"]}})
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("create_shipment failed for quote %s", q["id"])
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": q["status"]}})
        raise HTTPException(502, "We could not create the shipment; nothing was booked. Try again or contact ops@huolitsija.fi.") from exc
    booking.update(shipment_id=shipment.get("id"), shipment_reference=shipment.get("reference"))
    await party_screening.screen_booked_shipment(shipment["id"], user, pre_screen)  # hold + review card on a potential match
    if dg_res:
        booking["dg_check"] = {k: dg_res[k] for k in ("status", "tunnel_code", "aircraft")}
        try:
            await dangerous_goods.after_booking(dg_res, shipment, company_id=q.get("company_id"), user=user)
        except Exception:  # noqa: BLE001 - the booking stands; the check is in the audit log when it got that far
            log.exception("dg review for shipment %s failed", shipment.get("id"))
    await db.quotes.update_one({"id": q["id"]}, {"$set": {
        "status": "booked", "accepted_option_id": opt["id"], "shipment_id": shipment.get("id"),
        "shipment_reference": shipment.get("reference"), "booking": booking, "booked_at": now_iso(), "updated_at": now_iso()}})
    q = await db.quotes.find_one({"id": q["id"]}, NO_ID)
    await audit(user, "quote_booked", "quote", q["id"], {"shipment_id": shipment.get("id"), "option_id": opt["id"],
                                                         "total_cents": opt["total_cents"]}, company_id=q["company_id"])
    from services.agents.quote_agent import autosend
    gate = await autosend(shipment=shipment)
    if not gate.auto:
        await db.shipments.update_one({"id": shipment["id"]}, {"$set": {"carrier_release": {
            "status": "pending_approval", "approval_id": gate.approval["id"], "agent_decision_id": gate.id}}})
    await emit("quote.accepted", {"quote": q, "option": opt, "actor": user})
    try:
        await _send_booking_confirmation(q, opt, booking, shipment, user, company, request)
        await notify_role(["ops_agent"], bi(f"Uusi varaus {shipment.get('reference') or ''}".strip(),
                                            f"New booking {shipment.get('reference') or ''}".strip()),
                          same(f"{q.get('customer_name')} · {route_label(req)} · {opt.get('carrier_name')}"),
                          link=f"/ops/shipments/{shipment.get('id')}", kind="booking")
    except Exception:  # noqa: BLE001 - the booking stands; ops sees it in the shipments list
        log.exception("booking %s: confirmation e-mail or ops notification failed", shipment.get("reference"))
    return {"booking": booking, "shipment_id": shipment.get("id"), "shipment_reference": shipment.get("reference"),
            "quote": view_for(user, q)}


def _v1_payload(q: dict, opt: dict, booking: dict) -> tuple[dict, dict, dict]:
    """Shape quote/option/booking the way V1 create_shipment reads them (legs with role + carrier, door addresses,
    parties and references), keeping the original documents untouched."""
    quote = copy.deepcopy(q)
    quote["request"]["cargo"] = booking["cargo"]
    if booking.get("incoterm_place"):
        quote["request"]["incoterm_place"] = booking["incoterm_place"]
    option = copy.deepcopy(opt)
    for lg in option.get("legs") or []:
        lg.setdefault("role", {"pre_carriage": "pickup", "on_carriage": "delivery"}.get(lg.get("kind"), "main"))
        lg["carrier"] = lg.get("carrier_name")
        if lg["role"] == "pickup" and lg.get("from"):
            lg["from"] = {**lg["from"], "address": booking["pickup"]["address"]}
        if lg["role"] == "delivery" and lg.get("to"):
            lg["to"] = {**lg["to"], "address": booking["delivery"]["address"]}
    v1 = {
        **booking,
        "pickup_address": booking["pickup"]["address"], "delivery_address": booking["delivery"]["address"],
        "ready_date": booking["pickup"].get("date"),
        "parties": {k: booking[k] for k in ("shipper", "consignee", "notify") if booking.get(k)},
        "references": {"customer_ref": booking.get("customer_reference"), "po_numbers": booking.get("po_numbers") or [],
                       "quote_number": q.get("number")},
    }
    return quote, option, v1


async def _send_booking_confirmation(q, opt, booking, shipment, user, company, request):
    fi = (user.get("locale") or "fi") == "fi"
    ref = shipment.get("reference") or q.get("number")
    total = f"{opt['total_cents'] / 100:,.2f}"
    total = (total.replace(",", "\u00a0").replace(".", ",") if fi else total) + " EUR"

    def day(iso: str | None) -> str:
        if not iso:
            return "-"
        d = date.fromisoformat(iso[:10])
        return f"{d.day}.{d.month}.{d.year}" if fi else d.strftime("%d %b %Y")
    from services.quote_pdf import LIABILITY_FI

    regime = opt.get("liability_regime") or "NSAB"
    liab = LIABILITY_FI.get(regime) if fi else pricing.LIABILITY.get(regime, {}).get("label", "")
    terms = f"{base_url(request) or 'https://huolitsija.fi'}/terms"
    if fi:
        title = f"Varauspyyntö vastaanotettu {ref}"
        lines = ["Varauspyyntö on vastaanotettu. Kuljetusliikkeen vahvistusta odotetaan.",
                 f"Kiitos varauksesta. Tarjous {q.get('number')}: {route_label(q['request'])}.",
                 f"{opt.get('carrier_name')} · {opt.get('service_name')} · arvioitu lähtö {day(opt.get('etd'))}, saapuminen {day(opt.get('eta'))}.",
                 f"Hinta {total} ilman arvonlisäveroa. Tullit ja tuonnin arvonlisävero eivät sisälly, ellei niitä ole eritelty tarjouksessa.",
                 f"Toimimme sopimuskuljettajana (huolitsija päämiehenä). Vastuu: {liab}.",
                 f"Palvelut suoritetaan NSAB 2015 (PSYM 2015) -yleisten määräysten mukaisesti. Ehdot: {terms}"]
    else:
        title = f"Booking request received {ref}"
        lines = ["Your booking request has been received. Carrier confirmation is pending.",
                 f"Thank you for your booking. Quote {q.get('number')}: {route_label(q['request'])}.",
                 f"{opt.get('carrier_name')} · {opt.get('service_name')} · ETD {day(opt.get('etd'))}, ETA {day(opt.get('eta'))}.",
                 f"Price {total} excl. VAT. Duties and import VAT are not included unless listed in the quote.",
                 f"We act as contracting forwarder (principal). Liability: {liab}.",
                 f"Services are performed according to NSAB 2015 (PSYM 2015). Terms: {terms}"]
    url = f"{base_url(request)}/portal/shipments/{shipment.get('id')}"
    html, text = render(title, lines, url, "Avaa lähetys" if fi else "Open shipment")
    recipients = {user.get("email")} | ({company.get("billing_email")} if company and company.get("billing_email") else set())
    for to in [r for r in recipients if r]:
        await send_email(to, title, html, text, tag="booking_confirmation", company_id=q.get("company_id"),
                         ref={"type": "quote", "id": q["id"], "shipment_id": shipment.get("id")})


# ------------------------------------------------------------------------------------------------
# Events and housekeeping
# ------------------------------------------------------------------------------------------------
@on("approval.decided")
async def _quote_approval_decided(payload: dict):
    a = payload.get("approval") or {}
    if a.get("kind") != "quote_threshold" or a.get("ref_type") != "quote":
        return
    q = await db.quotes.find_one({"id": a["ref_id"]}, NO_ID)
    if not q or q["status"] != "pending_approval":
        return
    actor = payload.get("actor")
    appr = {**(q.get("approval") or {}), "status": a["status"], "decided_at": a.get("decided_at"),
            "decided_by": a.get("decided_by"), "note": a.get("decision_note")}
    if a["status"] == "approved":
        from services.agents.quote_agent import authorize_human, fingerprint, human_actor
        if customs_border.review_reason(q) == "territory_unverified" or customs_border.quote_needs_review(q) and not human_actor(actor or a.get("decided_by")):
            await db.approvals.update_one({"id": a["id"]}, {"$set": {"status": "pending", "decided_at": None, "decided_by": None}})
            return
        approved_fingerprint = (a.get("data") or {}).get("quote_fingerprint")
        if approved_fingerprint and approved_fingerprint != fingerprint(q):
            return  # a sourcing revision must be reviewed on its own card
        authorize_human(q, actor or a.get("decided_by"))
        if not q.get("options"):
            appr["status"] = "pending"
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"approval": {**appr, "note": "Approved without a price: add an option and send."}}})
            return
        status = "sent"
        if q.get("valid_until") and q["valid_until"] < now().date().isoformat():
            status = "expired"
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": status, "approval": appr, "updated_at": now_iso(),
                                                              "published": True, "send_authorization": q.get("send_authorization")}})
        if q.get("company_id") and status == "sent":
            from services.agents.quote_agent import outbound_allowed
            q.update(status=status, approval=appr)
            if not await outbound_allowed(q):
                return
            await notify_company(q["company_id"], bi(f"Tarjous {q.get('number')} on valmis", f"Quote {q.get('number')} is ready"),
                                 same(route_label(q["request"])),
                                 link=f"/portal/quotes/{q['id']}", email=True, kind="quote", base_url=base_url(None))
    else:
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": "rejected", "approval": appr, "updated_at": now_iso()}})
        if q.get("company_id"):
            note = same(a["decision_note"]) if a.get("decision_note") else bi("Ota yhteyttä, niin etsimme vaihtoehdon.",
                                                                               "Contact us for alternatives.")
            await notify_company(q["company_id"], bi(f"Tarjousta {q.get('number')} ei voitu vahvistaa",
                                                     f"Quote {q.get('number')} could not be confirmed"), note,
                                 link=f"/portal/quotes/{q['id']}", email=True, kind="quote", base_url=base_url(None))
    await audit(actor, f"quote_approval_{a['status']}", "quote", q["id"], {"approval_id": a["id"]}, company_id=q.get("company_id"))


@background_loop(seconds=900)
async def expire_quotes():
    today = now().date().isoformat()
    res = await db.quotes.update_many({"status": {"$in": ["sent", "accepted"]}, "valid_until": {"$lt": today}},
                                      {"$set": {"status": "expired", "expired_at": now_iso()}})
    if res.modified_count:
        log.info("expired %s quotes", res.modified_count)
    # a booking whose process died between the lock and the shipment (deploy, crash) is released after 10 minutes
    stale = iso(now() - timedelta(minutes=10))
    stuck = await db.quotes.update_many(
        {"status": "booking", "shipment_id": None, "booking_started_at": {"$lt": stale}},
        [{"$set": {"status": {"$ifNull": ["$status_before_booking", "sent"]}, "updated_at": now_iso()}}])
    if stuck.modified_count:
        log.warning("released %s quotes stuck in booking", stuck.modified_count)
    # anonymous public quotes nobody claimed are deleted after 30 days (GDPR: no purpose to keep them); the rule lives
    # in the retention policy table (services/retention.py) and runs here too so the 15-minute cadence stays
    from services.retention import purge_anonymous_public_quotes
    await purge_anonymous_public_quotes()


# s3 bounded callers (EDI, e-mail channel, API v1) call the inner booking function under this name.
_create_booking = book
