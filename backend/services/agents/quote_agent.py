"""Quote agent: prices are computed by services.pricing the moment a quote is requested; this agent decides whether
a quote that waits for approval may go to the customer. Within policy (Autopilot on, total under the Autopilot cap,
margin above the floor, freight actually charged) it sends the quote; dangerous goods are a legal human point; quotes
without freight (r3 N1) or without any price go to the owner."""
import copy
import hashlib
import json

from core.agent_runtime import approve_as_agent, decide, policy_reason, reason
from core.autonomy import quote_within_policy
from core.db import db
from core.jobs import enqueue, job
from core.settings import get_settings
from core.util import NO_ID
from services.agents.base import mode_key, sweeper

AGENT = "quote_agent"


@job("quote.autosend", agent=True, max_attempts=4, timeout=120, label_fi="Tarjouksen lähetys", label_en="Send quote")
async def autosend(j=None, *, quote=None, operation="send_quote", shipment=None):
    """Audited gate for every automatic delivery/booking, or resume a pending approval job."""
    if shipment is not None:
        return await _carrier_decision(shipment)
    if quote is not None:
        return await _decision(quote, operation=operation, strict=True)
    a = await db.raw.approvals.find_one({"id": j["payload"]["approval_id"]}, NO_ID)
    if not a or a["status"] != "pending" or a.get("kind") != "quote_threshold":
        return {"skipped": "not pending"}
    q = await db.raw.quotes.find_one({"id": a["ref_id"]}, NO_ID)
    if not q or q.get("status") != "pending_approval":
        return {"skipped": "quote moved on"}
    d = await _decision(q, a=a, strict=True)
    if d.auto:
        await approve_as_agent(a, AGENT, "Autopilot: inside the quote policy")
        return {"sent": True}
    return {"owner_queue": True, "reasons": [r["code"] for r in d.reasons]}


def fingerprint(q):
    """Authority belongs to the exact priced request, never to a later sourcing revision."""
    payload = {k: q.get(k) for k in ("company_id", "request", "options", "valid_until", "revision")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def human_actor(actor):
    return bool(actor and actor.get("id") and actor.get("type", "user") == "user"
                and actor.get("org_type") == "internal" and actor.get("role") != "system")


def authorize_human(q, actor):
    from services import customs_border
    if human_actor(actor) and customs_border.review_reason(q) != "territory_unverified":
        q["send_authorization"] = {"fingerprint": fingerprint(q), "actor_id": actor["id"]}
        if "customs_required" in ((q.get("approval") or {}).get("reasons") or []):
            q["send_authorization"]["customs_review_content"] = copy.deepcopy(customs_review_content(q))


def human_authorized(q):
    auth = q.get("send_authorization") or {}
    return bool(auth.get("actor_id") and auth.get("fingerprint") == fingerprint(q))


def customs_review_content(q):
    """Fields the person approved: all request facts, each sell total and transport route.

    Revision bookkeeping does not change a customs decision. Carrier/route, request
    (including territories and handling), currency or total changes do.
    """
    options = [{"total_cents": o.get("total_cents"), "currency": o.get("currency"),
                "mode": o.get("mode"), "service": o.get("service"),
                "legs": [{k: lg.get(k) for k in ("seq", "mode", "kind", "from", "to", "carrier_id", "service")}
                         for lg in o.get("legs") or []]} for o in q.get("options") or []]
    return {"request": q.get("request"), "options": options, "company_id": q.get("company_id")}


async def carry_customs_review(previous, q):
    """Carry a genuine human customs approval through equivalent repricing using native authority."""
    from services import customs_border
    a = previous.get("approval") or {}
    if (a.get("status") != "approved" or "customs_required" not in (a.get("reasons") or [])
            or not (previous.get("send_authorization") or {}).get("actor_id")):
        return
    user = await db.raw.users.find_one({"id": previous["send_authorization"]["actor_id"],
                                       "org_type": "internal", "disabled": {"$ne": True}}, NO_ID)
    approved_content = previous["send_authorization"].get("customs_review_content")
    if approved_content is None and human_authorized(previous):
        approved_content = customs_review_content(previous)
    if approved_content == customs_review_content(q) and human_actor(user):
        authorize_human(q, user)
        q["customs_review_changed"] = False
    else:
        req = q.get("request") or {}
        q["customs_review_changed"] = customs_border.assess(req.get("origin"), req.get("destination"))["customs_required"] is not False


async def _decision(q, *, a=None, operation="send_quote", strict=False, create_approval=True):
    from routes.quotes import charges_freight
    from services import customs_border
    settings = await get_settings()
    opts = q.get("options") or []
    reasons = (q.get("approval") or {}).get("reasons") or []
    manual = human_authorized(q) and customs_border.review_reason(q) != "territory_unverified"
    a = a or {}
    extra = []
    customs_hold = customs_border.quote_needs_review(q)
    territory_unverified = customs_border.review_reason(q) == "territory_unverified"
    if customs_hold and not manual:
        from routes.quotes import approval_reasons, APPROVAL_REASON_TEXT, APPROVAL_REASON_TEXT_FI
        for code in approval_reasons(q, settings):
            extra.append(policy_reason(code, APPROVAL_REASON_TEXT_FI[code], APPROVAL_REASON_TEXT[code]))
    if not opts:
        extra.append(policy_reason("no_price", "Reitille ei löytynyt hintaa; henkilö hinnoittelee", "No price found for this route; a person prices it"))
    elif not all(charges_freight(o) for o in opts):
        extra.append(policy_reason("no_freight", "Tarjous ei sisällä rahtia (r3 N1); henkilö tarkistaa", "The quote charges no freight (r3 N1); a person checks it"))
    if opts and not quote_within_policy(q, {**settings, "autopilot_enabled": True}):
        cap = settings["autopilot_quote_max_cents"] / 100
        extra.append(policy_reason("quote_policy",
                                   f"Hinta yli {cap:,.0f} € tai kate alle {settings['autopilot_min_margin_pct']} %".replace(",", " "),
                                   f"Price above EUR {cap:,.0f} or margin below {settings['autopilot_min_margin_pct']} %"))
    if opts:  # s7: each option's own margin above its rule floor, and a known cost
        from services.margin_rules import margin_floor_ok
        floor_ok, floor_reasons = await margin_floor_ok(q)
        if not floor_ok:
            extra += [policy_reason(r["code"], r["fi"], r["en"]) for r in floor_reasons]
    dg = bool(((q.get("request") or {}).get("cargo") or {}).get("dangerous_goods"))
    totals = [o.get("total_cents") or 0 for o in opts]
    margins = [round((o["total_cents"] - o["cost_cents"]) / o["total_cents"] * 100, 1) for o in opts
               if isinstance(o.get("total_cents"), int) and isinstance(o.get("cost_cents"), int) and o["total_cents"] > 0]
    if strict and not settings.get("autopilot_enabled") and not manual:
        from core.autonomy import autopilot_scope
        if await autopilot_scope(q.get("company_id"), settings, agent=AGENT) == "test_tenant":
            extra.append(reason("autopilot_off"))
    if manual:
        extra = []
    booking = operation == "book_quote" or q.get("source") == "edi"
    title = f"Approve quote and booking {q.get('number')}" if booking else (a.get("title") or f"Quote {q.get('number')}")
    title_fi = f"Hyväksy tarjous ja varaus {q.get('number')}" if booking else f"Tarjous {q.get('number')} odottaa hyväksyntää"
    return await decide(
        AGENT, operation, subject=("quote", q["id"]), subject_label=q.get("number"), company_id=q.get("company_id"),
        inputs={"reasons": reasons, "customs_border": (q.get("request") or {}).get("customs_border"),
                "customs_handling": customs_border.handling(q.get("request") or {}), "options": [{"carrier": o.get("carrier_name"), "total_cents": o.get("total_cents"),
                                                 "cost_cents": o.get("cost_cents"), "mode": o.get("mode")} for o in opts],
                "policy": {"max_cents": settings["autopilot_quote_max_cents"], "min_margin_pct": settings["autopilot_min_margin_pct"]}},
        decision={"send": True, "book": booking, "max_total_cents": max(totals) if totals else None, "min_margin_pct": min(margins) if margins else None},
        confidence=None, legal_point="dangerous_goods" if dg and not manual else None, extra_reasons=extra, rule_check=manual,
        create_approval=create_approval, approval_kind="quote_threshold", approval_data={"quote_fingerprint": fingerprint(q), "notify_owner_email": False}, title=title,
        title_fi=title_fi, summary=customs_border.UNVERIFIED_EN if territory_unverified else customs_border.POSTPONED_EN if customs_hold else (a.get("summary") or ""),
        summary_fi=customs_border.UNVERIFIED_FI if territory_unverified else customs_border.POSTPONED_FI if customs_hold else None,
        link=f"/ops/quotes/{q['id']}", amount_cents=max(totals) if totals else None)


async def gate_inline(q: dict, reasons: list, amount: int | None):
    """The moment a priced quote inside the Autopilot policy is created (routes.quotes.apply_approval_policy): one
    audited decision, no card (if it is not auto, the route opens its usual quote_threshold card)."""
    settings = await get_settings()
    opts = q.get("options") or []
    return await decide(
        AGENT, "send_quote", subject=("quote", q["id"]), subject_label=q.get("number"), company_id=q.get("company_id"),
        inputs={"reasons": reasons, "options": [{"carrier": o.get("carrier_name"), "total_cents": o.get("total_cents"),
                                                 "cost_cents": o.get("cost_cents"), "mode": o.get("mode")} for o in opts],
                "policy": {"max_cents": settings["autopilot_quote_max_cents"], "min_margin_pct": settings["autopilot_min_margin_pct"]}},
        decision={"send": True, "max_total_cents": amount}, confidence=None, create_approval=False,
        title=f"Send quote {q.get('number') or ''}".strip(), title_fi=f"Tarjouksen {q.get('number') or ''} lähetys".strip())


async def outbound_allowed(q, *, operation="send_quote"):
    """Gate again at delivery: legacy sent status is not permission to send or book."""
    if q.get("status") not in ("draft", "sent", "accepted", "pending_approval"):
        return False
    d = await autosend(quote=q, operation=operation)
    if not d.auto:
        hold(q, d)
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": q["status"], "approval": q["approval"]}})
        return False
    return q.get("status") in ("sent", "accepted")


def hold(q, d):
    q["status"] = "pending_approval"
    q["approval"] = {"required": True, "status": "pending", "approval_id": d.approval["id"],
                     "reasons": list(dict.fromkeys(r["code"] for r in d.reasons)),
                     "reason": d.reasons[0]["code"], "amount_cents": (d.row.get("decision") or {}).get("max_total_cents")}


@sweeper
async def sweep_quotes():
    n = 0
    mk = await mode_key()
    async for a in db.raw.approvals.find({"kind": "quote_threshold", "status": "pending"}, {"_id": 0, "id": 1, "ref_id": 1,
                                                                                         "company_id": 1}).limit(100):
        await enqueue("quote.autosend", {"approval_id": a["id"]}, company_id=a.get("company_id"),
                      subject=("quote", a["ref_id"]), key=f"quote:{a['id']}:{mk}", source="sweep")
        n += 1
    return n


def carrier_fingerprint(s):
    payload = {k: s.get(k) for k in ("quote_id", "option_id", "origin", "destination", "cargo", "price")}
    payload["legs"] = [{k: leg.get(k) for k in ("seq", "mode", "from", "to", "carrier_id", "booking_group", "booking_parent_seq")}
                       for leg in s.get("legs") or []]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def carrier_human_authorized(s):
    a = await db.raw.approvals.find_one({"kind": "booking_review", "ref_id": s["id"], "status": "approved",
                                        "data.outbound_fingerprint": carrier_fingerprint(s)}, NO_ID)
    by = (a or {}).get("decided_by") or {}
    if not by.get("id") or by.get("type") in ("agent", "ai", "system") or by["id"].startswith("agent:"):
        return False
    # admin.decide deliberately stores only {id, name}; authority comes from the current user record.
    user = await db.raw.users.find_one({"id": by["id"], "org_type": "internal", "disabled": {"$ne": True}}, NO_ID)
    return human_actor(user)


async def _carrier_decision(s):
    """A customer's booking/payment is recorded; our subsequent carrier commitment needs authority."""
    manual = await carrier_human_authorized(s)
    settings = await get_settings()
    extra = []
    if not settings.get("autopilot_enabled") and not manual:
        from core.autonomy import autopilot_scope
        if await autopilot_scope(s.get("company_id"), settings, agent=AGENT) == "test_tenant":
            extra.append(reason("autopilot_off"))
    d = await decide(AGENT, "book_carrier", subject=("shipment", s["id"]), subject_label=s.get("reference"),
                     company_id=s.get("company_id"), inputs={"quote_id": s.get("quote_id"), "option_id": s.get("option_id")},
                     decision={"place_carrier_booking": True}, rule_check=manual, extra_reasons=extra,
                     approval_kind="booking_review", title=f"Release carrier booking {s.get('reference')}",
                     title_fi=f"Hyväksy kuljetusvaraus {s.get('reference')}",
                     summary="Booking request received; a person must approve our carrier booking and outbound messages.",
                     summary_fi="Varauspyyntö vastaanotettu; henkilön on hyväksyttävä kuljetusvaraus ja lähtevät viestit.",
                     link=f"/ops/shipments/{s['id']}",
                     approval_data={"shipment_id": s["id"], "outbound_fingerprint": carrier_fingerprint(s),
                                    "notify_owner_email": False})
    if d.auto:
        await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"carrier_release": {
            "status": "approved", "agent_decision_id": d.id}}})
    return d
