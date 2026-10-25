"""One decision gate for every agent: guardrails, legal human points, the decision log and the owner queue.

    from core.agent_runtime import decide, agent_bot

    d = await decide("kyc_agent", "approve_customer", subject=("company", cid), inputs={...}, decision={...},
                     confidence=0.97, model=None, company_id=cid, approval_kind="kyc_review",
                     title_fi="Asiakkaan hyväksyntä", title="Customer approval", summary="...")
    if d.auto: ...act now...
    else: ...the owner decides d.approval in /ops/approvals; act in the approval.decided handler...

d.auto is True only when Autopilot is on, the kill switch is off, no legal point applies and every guardrail holds:
spend per Helsinki day, the customer's credit limit, the claim limit and the confidence floor. Every call writes one
row to `agent_decisions` (agent, inputs, decision, confidence, model, cost, outcome, reasons).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.approvals import request_approval
from core.db import db, register_index
from core.events import emit, on
from core.legal_points import LEGAL_POINTS
from core.settings import get_settings
from core.util import NO_ID, iso, new_id, now, now_iso

log = logging.getLogger("agent_runtime")
HELSINKI = ZoneInfo("Europe/Helsinki")
INPUT_LIMIT = 20000

register_index("agent_decisions", "id", unique=True)
register_index("agent_decisions", [("at", -1)])
register_index("agent_decisions", [("agent", 1), ("at", -1)])
register_index("agent_decisions", [("subject_id", 1), ("at", -1)])
register_index("agent_decisions", [("outcome", 1), ("at", -1)])

AGENT_LABELS = {
    "kyc_agent": ("Asiakastuntemus", "KYC"),
    "quote_agent": ("Tarjoukset", "Quotes"),
    "booking_agent": ("Varaukset", "Bookings"),
    "carrier_agent": ("Kuljetusliikkeet", "Carriers"),
    "document_agent": ("Asiakirjat", "Documents"),
    "customs_agent": ("Tulli", "Customs"),
    "tracking_agent": ("Seuranta", "Tracking"),
    "exception_agent": ("Poikkeamat", "Exceptions"),
    "comms_agent": ("Asiakasviestit", "Customer messages"),
    "claims_agent": ("Korvausvaatimukset", "Claims"),
    "delivery_agent": ("Toimitukset", "Deliveries"),
    "invoice_agent": ("Laskutus", "Invoicing"),
    "dunning_agent": ("Perintä", "Dunning"),
    "payments_agent": ("Maksut", "Payments"),
    "rates_agent": ("Hinnasto", "Rates"),
    "autopilot": ("Autopilotti", "Autopilot"),
    "partner_network": ("Hankinta", "Sourcing"),
    "finance": ("Talous", "Finance"),
    "customs_aeo": ("Tullivalvonta (AEO)", "Customs controls (AEO)"),  # s11: who may file, four-eyes thresholds
}

REASON_TEXT = {
    "autopilot_off": ("Autopilotti on pois päältä", "Autopilot is off"),
    "halted": ("Hätäpysäytys on päällä", "The kill switch is on"),
    "switch_off": ("Toiminnon kytkin on pois päältä", "The switch for this work is off"),
    "low_confidence": ("Varmuus {confidence} alittaa rajan {floor}", "Confidence {confidence} is below the floor {floor}"),
    "spend_limit": ("Päivän kulutusraja {limit} ylittyisi ({total})", "Today's spend limit {limit} would be exceeded ({total})"),
    "credit_limit": ("Asiakkaan luottoraja {limit} ylittyisi ({total})", "The customer's credit limit {limit} would be exceeded ({total})"),
    "credit_hold": ("Asiakkaan luotto on pysäytetty", "The customer's credit is on hold"),
    "claim_limit": ("Korvausraja {limit} ylittyy ({total})", "The claim limit {limit} is exceeded ({total})"),
    "legal": ("{title}: {reason}", "{title}: {reason}"),
    "policy": ("{fi}", "{en}"),
}


def agent_bot(agent: str) -> dict:
    """Actor for an agent's own writes. org_type internal: it does ops work, inside the guardrails."""
    fi, en = AGENT_LABELS.get(agent, (agent, agent))
    return {"type": "agent", "id": f"agent:{agent}", "name": f"Autopilot · {en}", "name_fi": f"Autopilotti · {fi}",
            "org_type": "internal", "role": "ops_admin", "agent": agent}


def _eur(cents: int | None) -> str:
    return f"{(cents or 0) / 100:,.2f} €".replace(",", " ").replace(".", ",")


def reason(code: str, **kw) -> dict:
    fi, en = REASON_TEXT.get(code, (code, code))
    try:
        return {"code": code, "fi": fi.format(**kw), "en": en.format(**kw)}
    except (KeyError, IndexError):
        return {"code": code, "fi": fi, "en": en}


def policy_reason(code: str, fi: str, en: str) -> dict:
    """A guardrail specific to one agent (e.g. 'trade defence measure needs a person')."""
    return {"code": code, "fi": fi, "en": en}


def helsinki_day_start(at: datetime | None = None) -> datetime:
    local = (at or now()).astimezone(HELSINKI)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))


async def spend_today() -> int:
    since = iso(helsinki_day_start())
    total = 0
    async for r in db.raw.agent_decisions.aggregate([
            {"$match": {"at": {"$gte": since}, "outcome": "auto"}},
            {"$group": {"_id": None, "s": {"$sum": {"$add": [{"$ifNull": ["$spend_cents", 0]},
                                                             {"$ifNull": ["$cost_cents", 0]}]}}}}]):
        total = int(r["s"] or 0)
    return total


async def credit_position(company_id: str, *, exclude_shipment_id: str | None = None) -> dict:
    """Open receivable + booked work not yet invoiced, against the customer's credit limit."""
    settings = await get_settings()
    company = await db.raw.companies.find_one({"id": company_id}, NO_ID) or {}
    limit = company.get("credit_limit_cents") or 0
    source = "company"
    if not limit:
        limit, source = int(settings["guard_default_credit_limit_cents"]), "default"
    open_cents = 0
    invoiced_shipments = set()
    async for inv in db.raw.invoices.find({"company_id": company_id, "kind": {"$ne": "credit_note"},
                                           "status": {"$in": ["open", "overdue", "processing"]},
                                           "origin": {"$ne": "prepayment"}},  # s2: not credit until paid
                                          {"_id": 0, "total_cents": 1, "paid_cents": 1, "credited_cents": 1, "shipment_id": 1}):
        open_cents += max(0, (inv.get("total_cents") or 0) - (inv.get("paid_cents") or 0) - (inv.get("credited_cents") or 0))
        if inv.get("shipment_id"):
            invoiced_shipments.add(inv["shipment_id"])
    booked_cents = 0
    async for s in db.raw.shipments.find({"company_id": company_id, "status": {"$nin": ["cancelled"]},
                                          "invoice_ids.0": {"$exists": False}},
                                         {"_id": 0, "id": 1, "price": 1, "status": 1}):
        if s["id"] in invoiced_shipments or s["id"] == exclude_shipment_id:
            continue
        booked_cents += int((s.get("price") or {}).get("total_cents") or 0)
    return {"limit_cents": int(limit), "limit_source": source, "open_cents": open_cents, "booked_cents": booked_cents,
            "used_cents": open_cents + booked_cents, "hold": bool(company.get("credit_hold"))}


@dataclass
class Decision:
    auto: bool
    outcome: str
    reasons: list = field(default_factory=list)
    approval: dict | None = None
    id: str = ""
    row: dict | None = None


def _trim(obj):
    import json
    try:
        s = json.dumps(obj, default=str)
    except (TypeError, ValueError):
        return {"repr": repr(obj)[:INPUT_LIMIT]}
    if len(s) <= INPUT_LIMIT:
        return json.loads(s)
    return {"truncated": s[:INPUT_LIMIT]}


async def decide(agent: str, action: str, *, subject: tuple[str, str], inputs: dict | None = None,
                 decision: dict | None = None, confidence: float | None = None, model: str | None = None,
                 cost_cents: int = 0, spend_cents: int = 0, company_id: str | None = None,
                 exposure_cents: int | None = None, exclude_shipment_id: str | None = None,
                 claim_cents: int | None = None, legal_point: str | None = None,
                 extra_reasons: list[dict] | None = None, switch: str = "autopilot_enabled",
                 approval_kind: str | None = None, title: str = "", title_fi: str | None = None,
                 summary: str = "", summary_fi: str | None = None, link: str | None = None,
                 amount_cents: int | None = None, approval_data: dict | None = None,
                 create_approval: bool = True, subject_label: str | None = None, rule_check: bool = False) -> Decision:
    # rule_check (s11, additive): a rule-based control decided for a person's own action (customs AEO filing
    # controls). No agent acts, so the Autopilot switch and the kill switch are not reasons; the row records the
    # controls' verdict (auto = every control passed, blocked = refused) in the same decision log.
    settings = await get_settings()
    reasons: list[dict] = []
    if settings.get("autopilot_halted") and not rule_check:
        reasons.append(reason("halted"))
    scope = None
    if switch == "autopilot_enabled" and not rule_check:
        from core.autonomy import autopilot_scope  # s6 r94: global, one test tenant's grant, or off
        scope = await autopilot_scope(company_id, settings, agent=agent)
    if not (settings.get(switch) or scope == "test_tenant") and not rule_check:
        reasons.append(reason("autopilot_off" if switch == "autopilot_enabled" else "switch_off"))
    legal = LEGAL_POINTS.get(legal_point or "")
    if legal:
        reasons.append({"code": "legal", "legal_point": legal_point,
                        "fi": f"{legal['title_fi']}: {legal['reason_fi']}", "en": f"{legal['title_en']}: {legal['reason_en']}"})
    elif legal_point:  # a code missing from core/legal_points.py still stops the agent: never auto on a typo
        log.error("decide(): unknown legal point %r from %s.%s", legal_point, agent, action)
        reasons.append({"code": "legal", "legal_point": legal_point,
                        "fi": f"Lakisääteinen ihmisen päätös ({legal_point}): henkilö päättää",
                        "en": f"Legally required human decision ({legal_point}): a person decides"})
    floor = float(settings.get("guard_confidence_floor") or 0.85)
    conf = 1.0 if confidence is None else float(confidence)
    if conf < floor:
        reasons.append(reason("low_confidence", confidence=f"{conf:.2f}", floor=f"{floor:.2f}"))
        reasons[-1]["fi"] = reasons[-1]["fi"].replace(f"{conf:.2f}", f"{conf:.2f}".replace(".", ",")) \
            .replace(f"{floor:.2f}", f"{floor:.2f}".replace(".", ","))
    committed = int(spend_cents or 0) + int(cost_cents or 0)
    guard_spend = {}
    if committed:
        today = await spend_today()
        limit = int(settings.get("guard_spend_per_day_cents") or 0)
        guard_spend = {"today_cents": today, "limit_cents": limit, "this_cents": committed}
        if today + committed > limit:
            reasons.append(reason("spend_limit", limit=_eur(limit), total=_eur(today + committed)))
    guard_credit = {}
    if exposure_cents is not None and company_id:
        pos = await credit_position(company_id, exclude_shipment_id=exclude_shipment_id)
        guard_credit = {**pos, "this_cents": int(exposure_cents)}
        if pos["hold"]:  # companies.credit_hold: set by credit control (s7) and collections (s13)
            reasons.append(reason("credit_hold"))
        if pos["used_cents"] + int(exposure_cents) > pos["limit_cents"]:
            reasons.append(reason("credit_limit", limit=_eur(pos["limit_cents"]), total=_eur(pos["used_cents"] + exposure_cents)))
    guard_claim = {}
    if claim_cents is not None:
        lim = int(settings.get("guard_claim_limit_cents") or 0)
        ins = int(settings.get("insurance_claim_limit_cents") or 0)
        guard_claim = {"claim_cents": int(claim_cents), "limit_cents": lim, "insurance_limit_cents": ins}
        if claim_cents > ins and not legal:
            lp = LEGAL_POINTS["claim_above_insurance_limit"]
            legal_point = "claim_above_insurance_limit"
            reasons.append({"code": "legal", "legal_point": legal_point,
                            "fi": f"{lp['title_fi']}: {lp['reason_fi']}", "en": f"{lp['title_en']}: {lp['reason_en']}"})
        elif claim_cents > lim:
            reasons.append(reason("claim_limit", limit=_eur(lim), total=_eur(claim_cents)))
    # s12: a sanctions hold or an open dangerous-goods review stops every agent action on the shipment (carrier booking,
    # customs filing, invoicing...), whichever record the action is about.
    sid = subject[1] if subject and subject[0] == "shipment" else None
    if subject and subject[0] in ("customs_declaration", "invoice", "document", "exception", "claim") and subject[1]:
        coll = {"customs_declaration": "customs_declarations", "invoice": "invoices", "document": "documents",
                "exception": "exceptions", "claim": "claims"}[subject[0]]
        sid = ((await db[coll].find_one({"id": subject[1]}, {"_id": 0, "shipment_id": 1})) or {}).get("shipment_id")
    if sid:
        held = await db.shipments.find_one({"id": sid}, {"_id": 0, "compliance_hold": 1, "dg_review_required": 1})
        for flag, code in (("compliance_hold", "sanctions_potential_match"), ("dg_review_required", "dangerous_goods")):
            if (held or {}).get(flag) and code != legal_point and code in LEGAL_POINTS:
                lp = LEGAL_POINTS[code]
                reasons.append({"code": "legal", "legal_point": code,
                                "fi": f"{lp['title_fi']}: {lp['reason_fi']}", "en": f"{lp['title_en']}: {lp['reason_en']}"})
                legal_point = legal_point or code
    reasons += extra_reasons or []
    auto = not reasons
    outcome = "auto" if auto else ("owner_queue" if create_approval and approval_kind else "blocked")
    fi_label, en_label = AGENT_LABELS.get(agent, (agent, agent))
    row = {
        "id": new_id(), "at": now_iso(), "agent": agent, "agent_label_fi": fi_label, "agent_label_en": en_label,
        "action": action, "subject_type": subject[0], "subject_id": subject[1], "subject_label": subject_label,
        "company_id": company_id, "inputs": _trim(inputs or {}), "decision": _trim(decision or {}),
        "confidence": None if confidence is None else round(float(confidence), 3), "model": model,
        "cost_cents": int(cost_cents or 0), "spend_cents": int(spend_cents or 0), "outcome": outcome,
        "reasons": reasons, "legal_point": legal_point, "autopilot_enabled": bool(settings.get("autopilot_enabled")),
        "autopilot_scope": scope,
        "guardrails": {"confidence_floor": floor, "spend": guard_spend, "credit": guard_credit, "claim": guard_claim},
        "approval_id": None, "title": title, "title_fi": title_fi or title,
    }
    row["summary_fi"] = summary_fi or summary
    if rule_check:
        row["rule_check"] = True
    approval = None
    if not auto and create_approval and approval_kind:
        why_en = "; ".join(r["en"] for r in reasons)
        extra = card_fields_from_row(row)
        approval = await request_approval(
            approval_kind, subject[0], subject[1], title or action, summary=summary, reason=why_en,
            company_id=company_id, requested_by=agent_bot(agent), link=link, amount_cents=amount_cents,
            data={**(approval_data or {}), "agent_decision_id": row["id"], "agent": agent, "action": action,
                  "decision": row["decision"], "confidence": row["confidence"]}, extra=extra)
        if approval.get("agent_decision_id") != row["id"]:
            # An older pending card for the same work: keep it, refresh its reasons and the decision it carries so the
            # owner (in either language) and the approval.decided handlers see today's state.
            fresh = {**extra, "reason": why_en, "summary": summary or approval.get("summary"),
                     "data": {**(approval.get("data") or {}), **(approval_data or {}), "agent_decision_id": row["id"],
                              "agent": agent, "action": action, "decision": row["decision"], "confidence": row["confidence"]}}
            await db.raw.approvals.update_one({"id": approval["id"]}, {"$set": fresh})
            approval.update(fresh)
        row["approval_id"] = approval["id"]
    await db.raw.agent_decisions.insert_one(dict(row))
    try:
        await emit("agent.decided", {"decision": row})
    except Exception:  # noqa: BLE001
        log.exception("agent.decided handlers failed")
    return Decision(auto=auto, outcome=outcome, reasons=reasons, approval=approval, id=row["id"], row=row)


def card_fields_from_row(row: dict) -> dict:
    """Owner-queue card fields for one decision row (Finnish texts, reasons, the legal point and its basis)."""
    reasons = row.get("reasons") or []
    extra = {"title_fi": row.get("title_fi") or row.get("title"), "summary_fi": row.get("summary_fi") or "",
             "reason_fi": "; ".join(r["fi"] for r in reasons), "reasons": reasons, "agent_decision_id": row["id"],
             "agent": row["agent"]}
    lp_code = row.get("legal_point")
    if lp_code and lp_code in LEGAL_POINTS:
        lp = LEGAL_POINTS[lp_code]
        extra.update(legal_point=lp_code, legal_basis=lp["basis"], legal_reason_fi=lp["reason_fi"],
                     legal_reason_en=lp["reason_en"], legal_title_fi=lp["title_fi"], legal_title_en=lp["title_en"])
    return extra


def card_fields(d: "Decision") -> dict:
    """For a caller that creates the owner-queue card itself (decide(..., create_approval=False)): the fields to pass as
    request_approval(extra=...). An auto decision gets a record-only card the agent decides at once."""
    if d.auto:
        return {"agent_decides": True, "agent_decision_id": d.id, "agent": d.row["agent"]}
    return card_fields_from_row(d.row)


async def link_card(d: "Decision", approval: dict):
    """Record on the decision row which card carries it (outcome owner_queue when a person decides)."""
    upd = {"approval_id": approval["id"]}
    if not d.auto:
        upd["outcome"] = "owner_queue"
        d.outcome = "owner_queue"
    await db.raw.agent_decisions.update_one({"id": d.id}, {"$set": upd})
    d.approval = approval


async def record_action(action_doc: dict):
    """Autonomous actions taken by older code paths (core.autonomy.action) are logged as decisions too; an action that
    an agent's decide() already logged (data.agent_decision_id) is not logged twice."""
    if (action_doc.get("data") or {}).get("agent_decision_id"):
        return
    if await db.raw.agent_decisions.find_one({"action_id": action_doc["id"]}, {"_id": 1}):
        return
    settings = await get_settings()
    row = {"id": new_id(), "at": action_doc.get("at") or now_iso(), "agent": "autopilot", "agent_label_fi": "Autopilotti",
           "agent_label_en": "Autopilot", "action": action_doc["kind"], "subject_type": action_doc.get("entity"),
           "subject_id": action_doc.get("entity_id"), "subject_label": None, "company_id": action_doc.get("company_id"),
           "inputs": _trim(action_doc.get("data") or {}), "decision": {"kind": action_doc["kind"]}, "confidence": None,
           "model": None, "cost_cents": 0, "spend_cents": 0, "outcome": "auto", "reasons": [], "legal_point": None,
           "autopilot_enabled": bool(settings.get("autopilot_enabled")), "guardrails": {}, "approval_id": None,
           "action_id": action_doc["id"], "reversible": action_doc.get("reversible"), "title": action_doc["kind"],
           "title_fi": action_doc["kind"]}
    await db.raw.agent_decisions.insert_one(row)


async def approve_as_agent(approval: dict, agent: str, note: str = "Autopilot") -> dict | None:
    """Decide a pending owner-queue card as the agent (only after decide() returned auto)."""
    bot = agent_bot(agent)
    res = await db.raw.approvals.update_one({"id": approval["id"], "status": "pending"}, {"$set": {
        "status": "approved", "decided_at": now_iso(), "decided_by": {"id": bot["id"], "name": bot["name"]},
        "decision_note": note}})
    if not res.modified_count:
        return None
    a = await db.raw.approvals.find_one({"id": approval["id"]}, NO_ID)
    await emit("approval.decided", {"approval": a, "actor": bot})
    return a


# ---------------------------------------------------------------------------------------------
# Owner notifications for every new owner-queue card
# ---------------------------------------------------------------------------------------------
async def owner_users() -> list[dict]:
    return [u async for u in db.raw.users.find({"role": "ops_admin", "org_type": "internal", "disabled": {"$ne": True}},
                                               {"_id": 0, "password_hash": 0})]


async def owner_extra_email() -> str | None:
    from core import config
    settings = await get_settings()
    return (settings.get("owner_alert_email") or config.env("OWNER_ALERT_EMAIL") or "").strip() or None


async def notify_owner(title: dict, body: dict, link: str | None, *, kind: str = "approval", email: bool = True,
                       dedupe_key: str | None = None):
    """In-app bell + e-mail to every ops admin (the owner) and the optional owner mailbox."""
    from core.email import render, send_email
    from core.notify import bi, notify_user
    title = bi(title.get("fi") or title["en"], title["en"])  # callers pass bi(fi, en); a missing Finnish falls back
    body = bi(body.get("fi") or body.get("en") or "", body.get("en") or "")
    since = iso(now() - timedelta(minutes=2))
    users = await owner_users()
    for u in users:
        if link and await db.raw.notifications.find_one({"user_id": u["id"], "link": link, "created_at": {"$gte": since}},
                                                        {"_id": 1}):
            # The slice that created the card already rang this owner's bell; still send the owner e-mail once.
            if email and u.get("email"):
                lang = "en" if u.get("locale") == "en" else "fi"
                html, text = render(title[lang], [body[lang]] if body.get(lang) else [], None, None)
                await send_email(u["email"], title[lang], html, text, tag=kind, ref={"dedupe": dedupe_key, "link": link})
            continue
        await notify_user(u, title, body, link, email=email, kind=kind)
    extra = await owner_extra_email()
    if email and extra and extra not in {u.get("email") for u in users}:
        html, text = render(title["fi"], [body["fi"], body["en"]], None, None)
        await send_email(extra, title["fi"], html, text, tag=kind, ref={"dedupe": dedupe_key, "link": link})


@on("approval.requested")
async def _notify_owner_of_card(p):
    from core.approvals import APPROVAL_KINDS
    from core.notify import bi
    a = p["approval"]
    label = APPROVAL_KINDS.get(a["kind"], {}).get("label", a["kind"])
    hours = a.get("sla_hours")
    title_fi = a.get("title_fi") or (a.get("data") or {}).get("title_fi") or a.get("title")  # credit/insurance keep it in data
    await notify_owner(
        bi(f"Hyväksyttävää: {title_fi}", f"To approve: {a.get('title')}"),
        bi(f"{a.get('summary_fi') or a.get('summary') or ''} Vastaa {hours:.0f} tunnin kuluessa." if hours else
           (a.get('summary_fi') or a.get('summary') or ''),
           f"{a.get('summary') or label} Respond within {hours:.0f} hours." if hours else (a.get('summary') or label)),
        f"/ops/approvals?focus={a['id']}", kind="approval", dedupe_key=f"approval:{a['id']}",
        email=(a.get("data") or {}).get("notify_owner_email") is not False)
