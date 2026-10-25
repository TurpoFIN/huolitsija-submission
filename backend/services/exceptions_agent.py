"""Shipment exceptions: rule detection on live data, deduplication, auto-clear, AI assessment.

Rules (thresholds from env, defaults in brackets):
  R-ETA     ETA slipped more than EXC_ETA_SLIP_HOURS [24] against the planned ETA          -> delay
  R-CUSTOMS customs hold longer than EXC_CUSTOMS_HOLD_HOURS [48]                            -> customs_hold
  R-DOCS    required documents missing within EXC_DOCS_DAYS_BEFORE_ARRIVAL [3] days of ETA -> missing_docs
  R-POS     no position update on an active leg for EXC_POSITION_STALE_HOURS [12]           -> other (tracking gap)
  R-PAY     invoice overdue more than EXC_INVOICE_OVERDUE_DAYS [7] days                     -> payment

Each rule hit has a stable rule_key; an open exception with that key is updated, never duplicated, and is
resolved automatically ("cleared by rule") when the condition no longer holds. The AI exception agent writes
{summary, impact, recommended_actions[], customer_message_draft}; the customer draft becomes a pending
ai_message in the shipment thread, so it reaches the customer only after a person approves it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from core import config
from core.audit import actor_ref, audit, agent_actor
from core.db import db, register_index
from core.events import emit
from core.lifecycle import background_loop
from core.llm import LLMUnavailable, llm_json
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso

log = logging.getLogger("exceptions")

register_index("exceptions", "id", unique=True)
register_index("exceptions", [("status", 1), ("severity", 1), ("sla_due", 1)])
register_index("exceptions", "shipment_id")
register_index("exceptions", "rule_key")

TYPES = ("delay", "customs_hold", "missing_docs", "damage", "address_issue", "capacity", "weather", "payment", "other")
SEVERITIES = ("low", "medium", "high", "critical")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SLA_HOURS = {"critical": 2, "high": 8, "medium": 24, "low": 72}
ACTIVE_SHIPMENT = ("booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception")
AGENT = agent_actor("exception_agent")


def _f(name: str, default: float) -> float:
    try:
        return float(config.env(name, str(default)))
    except ValueError:
        return default


class ExceptionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sla_due(severity: str, start: datetime | None = None) -> str:
    return iso((start or now()) + timedelta(hours=SLA_HOURS.get(severity, 24)))


async def _set_has_exception(shipment_id: str | None):
    if not shipment_id:
        return
    n = await db.exceptions.count_documents({"shipment_id": shipment_id, "status": {"$in": ["open", "in_progress"]}})
    await db.shipments.update_one({"id": shipment_id}, {"$set": {"has_exception": n > 0}})


async def create_exception(*, shipment_id: str | None, type: str, severity: str, title: str, description: str = "",
                           detected_by: str, actor: dict | None = None, rule_key: str | None = None,
                           evidence: dict | None = None, company_id: str | None = None, invoice_id: str | None = None,
                           customer_visible: bool = True, owner: dict | None = None, demo: bool = False,
                           assess: bool | None = None, title_i18n: dict | None = None,
                           description_i18n: dict | None = None, rule: str | None = None) -> tuple[dict, bool]:
    """Returns (exception, created). With a rule_key an open exception is updated instead of duplicated."""
    if type not in TYPES:
        raise ExceptionError(f"Unknown exception type {type}")
    if severity not in SEVERITIES:
        raise ExceptionError(f"Unknown severity {severity}")
    if detected_by not in ("ai", "rule", "carrier", "ops", "partner"):
        raise ExceptionError("Unknown detection source")
    if rule_key:
        ex = await db.exceptions.find_one({"rule_key": rule_key, "status": {"$in": ["open", "in_progress"]}}, NO_ID)
        if ex:
            upd = {"last_detected_at": now_iso(), "evidence": evidence or ex.get("evidence"), "description": description or ex["description"],
                   "title": title, "title_i18n": title_i18n or ex.get("title_i18n"),
                   "description_i18n": description_i18n or ex.get("description_i18n")}
            if SEV_RANK[severity] > SEV_RANK[ex["severity"]]:
                upd.update(severity=severity, sla_due=sla_due(severity))
                upd["history"] = ex.get("history", []) + [{"at": now_iso(), "by": "rule", "action": "escalated",
                                                           "note": f"{ex['severity']} -> {severity}"}]
            await db.exceptions.update_one({"id": ex["id"]}, {"$set": upd})
            ex.update(upd)
            return ex, False
    shipment = await db.shipments.find_one({"id": shipment_id}, NO_ID) if shipment_id else None
    if shipment_id and not shipment:
        raise ExceptionError("Shipment not found", 404)
    ex = {"id": new_id(), "shipment_id": shipment_id, "shipment_reference": (shipment or {}).get("reference"),
          "company_id": company_id or (shipment or {}).get("company_id"),
          "company_name": (shipment or {}).get("customer_name"), "invoice_id": invoice_id, "type": type,
          "severity": severity, "status": "open", "title": title[:200], "description": description[:4000],
          "title_i18n": title_i18n, "description_i18n": description_i18n,
          "detected_by": detected_by, "rule_key": rule_key, "rule": rule, "evidence": evidence or {}, "ai_assessment": None,
          "ai_message_id": None, "owner": owner, "sla_due": sla_due(severity), "customer_visible": customer_visible,
          "created_at": now_iso(), "updated_at": now_iso(), "last_detected_at": now_iso(), "resolved_at": None,
          "resolution": None, "resolved_by": None, "created_by": actor_ref(actor) if actor else actor_ref(None),
          "history": [{"at": now_iso(), "by": (actor or {}).get("name") or detected_by, "action": "opened"}],
          "demo": bool(demo or (shipment or {}).get("demo"))}
    if not ex["company_name"] and ex["company_id"]:
        c = await db.companies.find_one({"id": ex["company_id"]}, {"_id": 0, "name": 1})
        ex["company_name"] = (c or {}).get("name")
    await db.exceptions.insert_one(dict(ex))
    await _set_has_exception(shipment_id)
    await audit(actor, "exception_opened", "exception", ex["id"], {"type": type, "severity": severity, "rule_key": rule_key},
                company_id=ex["company_id"])
    if detected_by in ("ai", "rule"):
        from core.autonomy import action
        await action("exception_opened", "exception", ex["id"], company_id=ex["company_id"],
                     data={"rule_key": rule_key}, reversible=False)
    await emit("exception.opened", {"exception": ex})
    from core.notify import bi, notify_role

    subject = ex["shipment_reference"] or ex["company_name"]
    sev_fi = {"critical": "kriittinen", "high": "korkea", "medium": "keskitaso", "low": "matala"}.get(severity, severity)
    await notify_role(["ops_agent"], bi(f"Poikkeama ({sev_fi}): {subject}", f"{severity.capitalize()} exception: {subject}"),
                      bi((title_i18n or {}).get("fi") or title, (title_i18n or {}).get("en") or title),
                      link=f"/ops/exceptions?id={ex['id']}", kind="exception")
    auto_assess = SEV_RANK[severity] >= SEV_RANK["high"] if assess is None else assess
    if auto_assess:
        from services.comms_agent import spawn

        spawn(assess_exception(ex["id"]))
    return ex, True


async def resolve_exception(exception_id: str, *, resolution: str, actor: dict | None, by_rule: bool = False) -> dict:
    ex = await db.exceptions.find_one({"id": exception_id}, NO_ID)
    if not ex:
        raise ExceptionError("Exception not found", 404)
    if ex["status"] == "resolved":
        return ex
    if not (resolution or "").strip():
        raise ExceptionError("Describe how it was resolved")
    upd = {"status": "resolved", "resolved_at": now_iso(), "resolution": resolution.strip()[:2000],
           "resolved_by": {"type": "rule", "name": "Cleared by rule"} if by_rule else actor_ref(actor), "updated_at": now_iso()}
    await db.exceptions.update_one({"id": exception_id}, {"$set": upd, "$push": {"history": {
        "at": now_iso(), "by": "rule" if by_rule else (actor or {}).get("name"), "action": "resolved", "note": resolution[:300]}}})
    # A draft customer message about a cleared problem must not go out later.
    if ex.get("ai_message_id"):
        m = await db.messages.find_one({"id": ex["ai_message_id"]}, {"_id": 0, "status": 1, "thread_id": 1})
        if m and m["status"] == "pending_approval" and by_rule:
            from core.approvals import cancel_approvals

            await db.messages.update_one({"id": ex["ai_message_id"]}, {"$set": {
                "status": "rejected", "rejected_reason": "Exception cleared before approval", "rejected_at": now_iso()}})
            await cancel_approvals(ex["ai_message_id"], kind="ai_message")
            from services.messaging import _refresh_pending

            await _refresh_pending(m["thread_id"])
    await _set_has_exception(ex.get("shipment_id"))
    if by_rule:
        from core.autonomy import action
        await action("exception_resolved", "exception", exception_id, company_id=ex.get("company_id"),
                     data={"resolution": resolution}, reversible=True)
    await audit(actor, "exception_resolved", "exception", exception_id, {"resolution": resolution, "by_rule": by_rule},
                company_id=ex.get("company_id"))
    resolved = await db.exceptions.find_one({"id": exception_id}, NO_ID)
    await emit("exception.resolved", {"exception": resolved})
    return resolved


# --------------------------------------------------------------------------------------------- detection

async def _planned_eta(s: dict) -> tuple[datetime | None, str]:
    if s.get("eta_replanned"):  # the exception agent's accepted re-plan; eta_planned stays the booking promise (KPIs)
        return parse_iso(s["eta_replanned"]), "replanned ETA"
    for k in ("planned_eta", "eta_planned", "original_eta", "eta_original"):
        if s.get(k):
            return parse_iso(s[k]), k
    for m in s.get("milestones") or []:
        if (m.get("code") or "").upper() in ("ARRIVED", "DELIVERED") and m.get("planned") and m.get("at"):
            return parse_iso(m["at"]), f"planned milestone {m['code']}"
    if s.get("quote_id"):
        q = await db.quotes.find_one({"id": s["quote_id"]}, {"_id": 0, "options": 1, "accepted_option_id": 1})
        opt_id = s.get("option_id") or (q or {}).get("accepted_option_id")
        for o in (q or {}).get("options") or []:
            if o.get("id") == opt_id and o.get("eta"):
                return parse_iso(o["eta"]), "quoted ETA"
    base = await db.v4_eta_baselines.find_one({"shipment_id": s["id"]}, NO_ID)
    if base:
        return parse_iso(base["eta"]), "first ETA seen"
    if s.get("eta"):
        await db.v4_eta_baselines.insert_one({"shipment_id": s["id"], "eta": s["eta"], "at": now_iso()})
    return None, ""


def _hold_since(s: dict) -> datetime | None:
    for h in s.get("holds") or []:
        if (h.get("type") or "").lower().startswith("customs") and h.get("since"):
            return parse_iso(h["since"])
    if s.get("status") == "customs_hold":
        for m in reversed(s.get("milestones") or []):
            code = (m.get("code") or "").upper()
            if not m.get("planned") and m.get("at") and ("CUSTOMS" in code or "HOLD" in code or "CLEARANCE" in code):
                return parse_iso(m["at"])
        return parse_iso(s.get("status_changed_at") or s.get("updated_at"))
    return None


DOC_LABELS = {  # (en, fi), same wording as the portal's enum.doc.* labels
    "commercial_invoice": ("commercial invoice", "kauppalasku"), "packing_list": ("packing list", "pakkausluettelo"),
    "bill_of_lading": ("bill of lading", "konossementti"), "sea_waybill": ("sea waybill", "merirahtikirja"),
    "air_waybill": ("air waybill", "lentorahtikirja"), "cmr": ("CMR consignment note", "CMR-rahtikirja"),
    "cim": ("CIM consignment note", "CIM-rahtikirja"), "certificate_of_origin": ("certificate of origin", "alkuperätodistus"),
    "eur1": ("EUR.1", "EUR.1"), "dg_declaration": ("DG declaration", "VAK-ilmoitus"),
    "power_of_attorney": ("power of attorney", "valtakirja"), "pod": ("proof of delivery", "toimitustodiste"),
    "customs_decision": ("customs decision", "tullipäätös"),
}


def doc_label(entry: str, lang: str) -> str:
    """'bill_of_lading or sea_waybill' -> 'bill of lading or sea waybill' / 'konossementti tai merirahtikirja'."""
    idx = 1 if lang == "fi" else 0
    parts = [p.strip() for p in str(entry).split(" or ")]
    return (" tai " if lang == "fi" else " or ").join(DOC_LABELS.get(p, (p.replace("_", " "),) * 2)[idx] for p in parts)


def _fi_dt(dt) -> str:
    from zoneinfo import ZoneInfo

    d = dt.astimezone(ZoneInfo("Europe/Helsinki"))
    return f"{d.day}.{d.month}.{d.year} klo {d.hour}.{d.minute:02d}"


def _en_dt(dt) -> str:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo("Europe/Helsinki")).strftime("%-d %b %Y %H:%M")


async def evaluate_shipment(s: dict) -> list[dict]:
    """Rule hits for one shipment: [{rule_key, type, severity, title, description, evidence}]."""
    hits = []
    t_now = now()
    sid, ref = s["id"], s.get("reference")
    if s.get("status") not in ACTIVE_SHIPMENT:
        return hits
    # R-ETA
    eta = parse_iso(s.get("eta"))
    planned, basis = await _planned_eta(s)
    if eta and planned:
        slip_h = (eta - planned).total_seconds() / 3600
        if slip_h > _f("EXC_ETA_SLIP_HOURS", 24):
            sev = "high" if slip_h > 72 else "medium"
            hits.append({"rule_key": f"eta:{sid}", "type": "delay", "severity": sev,
                         "title": f"ETA slipped {round(slip_h)} h",
                         "description": f"{ref}: ETA now {iso(eta)} vs planned {iso(planned)} ({basis}).",
                         "title_i18n": {"en": f"ETA slipped {round(slip_h)} h", "fi": f"Saapumisarvio myöhässä {round(slip_h)} h"},
                         "description_i18n": {
                             "en": f"{ref}: ETA now {_en_dt(eta)}, planned {_en_dt(planned)}.",
                             "fi": f"{ref}: arvioitu saapuminen nyt {_fi_dt(eta)}, suunniteltu {_fi_dt(planned)}."},
                         "evidence": {"eta": iso(eta), "planned_eta": iso(planned), "slip_hours": round(slip_h, 1), "basis": basis}})
    # R-CUSTOMS
    since = _hold_since(s)
    if since:
        held_h = (t_now - since).total_seconds() / 3600
        if held_h > _f("EXC_CUSTOMS_HOLD_HOURS", 48):
            sev = "critical" if held_h > 96 else "high"
            reason = next((h.get("reason") for h in s.get("holds") or [] if (h.get("type") or "").startswith("customs")), None)
            hits.append({"rule_key": f"customs:{sid}", "type": "customs_hold", "severity": sev,
                         "title": f"Customs hold {round(held_h)} h",
                         "description": f"{ref} has been held by customs since {iso(since)}." + (f" Reason: {reason}" if reason else ""),
                         "title_i18n": {"en": f"Customs hold {round(held_h)} h", "fi": f"Tullipysäytys {round(held_h)} h"},
                         "description_i18n": {
                             "en": f"{ref} has been held by customs since {_en_dt(since)}." + (f" Reason: {reason}" if reason else ""),
                             "fi": f"{ref} on ollut tullin pysäyttämänä {_fi_dt(since)} alkaen." + (f" Syy: {reason}" if reason else "")},
                         "evidence": {"since": iso(since), "hours": round(held_h, 1), "reason": reason}})
    # R-DOCS
    if eta and not s.get("ata") and s.get("status") not in ("out_for_delivery",):
        days_left = (eta - t_now).total_seconds() / 86400
        if days_left <= _f("EXC_DOCS_DAYS_BEFORE_ARRIVAL", 3):
            from services.comms_agent import document_status

            docs = await document_status(s)
            if docs["missing"]:
                sev = "high" if days_left <= 1 else "medium"
                hits.append({"rule_key": f"docs:{sid}", "type": "missing_docs", "severity": sev,
                             "title": f"Missing documents: {', '.join(docs['missing'])}",
                             "description": f"{ref} arrives {'in %.1f days' % days_left if days_left > 0 else 'now'}; "
                                            f"not on file: {', '.join(docs['missing'])}.",
                             "title_i18n": {
                                 "en": "Missing documents: " + ", ".join(doc_label(d, "en") for d in docs["missing"]),
                                 "fi": "Asiakirjoja puuttuu: " + ", ".join(doc_label(d, "fi") for d in docs["missing"])},
                             "description_i18n": {
                                 "en": f"{ref} arrives " + (f"in {days_left:.1f} days" if days_left > 0 else "now") +
                                       "; not on file: " + ", ".join(doc_label(d, "en") for d in docs["missing"]) + ".",
                                 "fi": f"{ref} saapuu " + (f"{days_left:.1f} päivän kuluttua".replace(".", ",") if days_left > 0 else "nyt") +
                                       "; puuttuu: " + ", ".join(doc_label(d, "fi") for d in docs["missing"]) + "."},
                             "evidence": {"missing": docs["missing"], "on_file": docs["on_file"], "eta": iso(eta)}})
    # R-POS
    if s.get("status") in ("picked_up", "in_transit"):
        pos_at = parse_iso((s.get("position") or {}).get("updated_at"))
        started = parse_iso(s.get("atd") or next((lg.get("atd") for lg in s.get("legs") or [] if lg.get("atd")), None))
        ref_time = pos_at or started
        if ref_time:
            stale_h = (t_now - ref_time).total_seconds() / 3600
            if stale_h > _f("EXC_POSITION_STALE_HOURS", 12):
                hits.append({"rule_key": f"pos:{sid}", "type": "other", "severity": "medium" if stale_h > 48 else "low",
                             "customer_visible": False,  # a tracking gap is ours to chase, not news for the customer
                             "title": f"No position update for {round(stale_h)} h",
                             "title_i18n": {"en": f"No position update for {round(stale_h)} h",
                                            "fi": f"Ei sijaintipäivitystä {round(stale_h)} h"},
                             "description_i18n": {
                                 "en": f"{ref}: last position " + (_en_dt(pos_at) if pos_at else "never received") + ".",
                                 "fi": f"{ref}: viimeisin sijainti " + (_fi_dt(pos_at) if pos_at else "ei koskaan vastaanotettu") + "."},
                             "description": f"{ref}: last position {iso(pos_at) if pos_at else 'never received'} "
                                            f"(source {(s.get('position') or {}).get('source') or '—'}).",
                             "evidence": {"position_updated_at": iso(pos_at) if pos_at else None, "hours": round(stale_h, 1)}})
    return hits


async def run_detection(assess: bool | None = None) -> dict:
    """One pass over active shipments and overdue invoices. Returns counts. assess=None: AI-assess new high/critical."""
    created = updated = cleared = 0
    triggered: set[str] = set()
    evaluated_prefixes: set[str] = set()
    async for s in db.shipments.find({"status": {"$in": list(ACTIVE_SHIPMENT)}}, NO_ID):
        evaluated_prefixes.add(s["id"])
        for h in await evaluate_shipment(s):
            triggered.add(h["rule_key"])
            # Same incident already reported by a carrier, partner or ops: add the rule's facts to that exception
            # instead of opening a second one for the same problem.
            twin = await db.exceptions.find_one({"shipment_id": s["id"], "type": h["type"], "detected_by": {"$ne": "rule"},
                                                 "status": {"$in": ["open", "in_progress"]}}, {"_id": 0, "id": 1})
            if twin and not await db.exceptions.find_one({"rule_key": h["rule_key"], "status": {"$in": ["open", "in_progress"]}},
                                                         {"_id": 1}):
                await db.exceptions.update_one({"id": twin["id"]}, {"$set": {f"evidence.rule_{h['rule_key'].split(':')[0]}": h["evidence"],
                                                                            "last_detected_at": now_iso()}})
                updated += 1
                continue
            _, was_new = await create_exception(shipment_id=s["id"], detected_by="rule", assess=assess, **h)
            created += was_new
            updated += not was_new
    # R-PAY
    from services.billing import today_fi, to_date

    limit = _f("EXC_INVOICE_OVERDUE_DAYS", 7)
    async for inv in db.invoices.find({"status": "overdue", "kind": "invoice", "due_date": {"$nin": [None, ""]}}, NO_ID):
        days = (today_fi() - to_date(inv["due_date"])).days
        if days > limit:
            key = f"pay:{inv['id']}"
            triggered.add(key)
            due = max(0, inv["total_cents"] - inv.get("paid_cents", 0) - inv.get("credited_cents", 0))
            _, was_new = await create_exception(
                shipment_id=inv.get("shipment_id") if inv.get("shipment_id") and await db.shipments.find_one(
                    {"id": inv["shipment_id"]}, {"_id": 1}) else None,
                company_id=inv["company_id"], invoice_id=inv["id"], type="payment",
                severity="high" if days > 30 else "medium", title=f"Invoice {inv['number']} overdue {days} d",
                title_i18n={"en": f"Invoice {inv['number']} overdue {days} d", "fi": f"Lasku {inv['number']} erääntynyt {days} pv"},
                description=f"{inv['company_name']}: {due / 100:.2f} EUR due {inv['due_date']}.", detected_by="rule",
                rule_key=key, evidence={"invoice_id": inv["id"], "days_overdue": days, "amount_due_cents": due},
                customer_visible=False, assess=assess)
            created += was_new
            updated += not was_new
    # Autopilot owns autonomous resolution; detection remains visible to operators when it is off.
    from core.settings import get_settings
    if not (await get_settings())["autopilot_enabled"]:
        return {"created": created, "updated": updated, "cleared": cleared}
    # Auto-clear rule exceptions whose condition is gone.
    async for ex in db.exceptions.find({"detected_by": "rule", "status": {"$in": ["open", "in_progress"]},
                                        "rule_key": {"$regex": "^(eta|customs|docs|pos|pay):"}}, NO_ID):
        if ex["rule_key"] in triggered:
            continue
        kind, _, ref_id = ex["rule_key"].partition(":")
        if kind == "pay":
            inv = await db.invoices.find_one({"id": ref_id}, {"_id": 0, "status": 1})
            why = f"Invoice is {(inv or {}).get('status', 'gone')}"
        else:
            s = await db.shipments.find_one({"id": ref_id}, {"_id": 0, "status": 1})
            why = {"eta": "ETA back within tolerance", "customs": "Customs hold released",
                   "docs": "Required documents on file", "pos": "Position updates resumed"}.get(kind, "Condition cleared")
            if s and s.get("status") not in ACTIVE_SHIPMENT:
                why = f"Shipment {s.get('status')}"
        await resolve_exception(ex["id"], resolution=f"Cleared automatically: {why}.", actor=None, by_rule=True)
        cleared += 1
    return {"created": created, "updated": updated, "cleared": cleared}


@background_loop(seconds=300)
async def detection_loop():
    if config.env_bool("EXCEPTION_DETECTION", True):
        await run_detection()


# --------------------------------------------------------------------------------------------- AI assessment

ASSESS_SYSTEM = """You are the exception agent of Huolitsija Oy, a Finnish freight forwarder and customs broker.
Assess one shipment exception for the operations team using only the facts given. Be concrete and short.
Do not invent dates, causes or amounts that are not in the facts; say what is unknown.
The operations team works in Finnish: write "summary", "impact" and "recommended_actions" in Finnish (Finnish date
style 26.10.2026, times "klo 10.55"), even when the facts are in English.
Also return summary_i18n, impact_i18n and recommended_actions_i18n, each {"fi": ..., "en": ...}.
The actions translations must be arrays, all others strings. Both languages must preserve the same facts.
Return JSON: {"summary": str (1-2 sentences, what happened), "impact": str (effect on delivery date, cost or
customs, with numbers from the facts), "recommended_actions": [str, 2-5 imperative steps for ops, most urgent first],
"customer_message_draft": str (message to the customer in LANGUAGE, 2-5 sentences, plain text, factual, no blame,
no compensation promises, says what we are doing and when they will hear next; empty string if the customer should
not be contacted), "severity_suggestion": "low"|"medium"|"high"|"critical"}"""


async def assess_exception(exception_id: str, *, requested_by: dict | None = None) -> dict | None:
    ex = await db.exceptions.find_one({"id": exception_id}, NO_ID)
    if not ex:
        return None
    from services.comms_agent import customer_locale, shipment_context

    ctx = await shipment_context(ex.get("shipment_id"), ex["company_id"]) if ex.get("shipment_id") else None
    lang = await customer_locale(ex["company_id"]) if ex.get("company_id") else "fi"
    facts = {"exception": {k: ex.get(k) for k in ("type", "severity", "title", "description", "evidence", "detected_by",
                                                  "created_at", "customer_visible")}, "shipment_context": ctx}
    prompt = (f"LANGUAGE for the customer message: {'Finnish' if lang == 'fi' else 'English'}\n"
              f"FACTS:\n{json.dumps(facts, ensure_ascii=False, default=str)}")
    try:
        out = await llm_json("exception_agent", ASSESS_SYSTEM, prompt, max_tokens=2200,
                             ref={"type": "exception", "id": exception_id}, company_id=ex.get("company_id"))
        from services.locale_backfill import bilingual_text
        variants = bilingual_text(out, ("summary", "impact", "recommended_actions"))
        if not isinstance(out, dict) or not out.get("summary"):
            raise ValueError("Model returned no summary")
    except LLMUnavailable as exc:
        await db.exceptions.update_one({"id": exception_id}, {"$set": {"ai_assessment": {
            "status": "unavailable", "note": f"Needs a person: {exc}", "assessed_at": now_iso()}}})
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("exception agent failed for %s: %s", exception_id, exc)
        await db.exceptions.update_one({"id": exception_id}, {"$set": {"ai_assessment": {
            "status": "error", "note": f"AI assessment failed: {str(exc)[:200]}", "assessed_at": now_iso()}}})
        return None
    assessment = {**{k+"_i18n":v for k,v in variants.items()}, "status": "ok", "summary": out.get("summary"), "impact": out.get("impact"),
                  "recommended_actions": [str(a) for a in (out.get("recommended_actions") or [])][:6],
                  "customer_message_draft": (out.get("customer_message_draft") or "").strip(),
                  "severity_suggestion": out.get("severity_suggestion") if out.get("severity_suggestion") in SEVERITIES else None,
                  "language": lang, "assessed_at": now_iso(), "requested_by": requested_by}
    upd = {"ai_assessment": assessment, "updated_at": now_iso()}
    from services.agents.exception_agent import notice_sent

    if assessment["customer_message_draft"] and ex.get("customer_visible") and ex.get("shipment_id") \
            and not await notice_sent(exception_id):  # the exception agent already told the customer
        from services import messaging

        if ex.get("ai_message_id"):
            await messaging.supersede_ai_drafts_for_message(ex["ai_message_id"])
        thread = await messaging.get_or_create_thread(company_id=ex["company_id"], shipment_id=ex["shipment_id"],
                                                      subject=ex.get("shipment_reference"), created_by=AGENT)
        msg = await messaging.add_message(thread, body=assessment["customer_message_draft"],
                                          author={"type": "ai", "id": "exception_agent", "name": "Huolitsija AI"},
                                          needs_approval=True, ai={"agent": "exception_agent", "exception_id": exception_id},
                                          approval_reason=f"Customer message about exception: {ex['title']}")
        upd["ai_message_id"] = msg["id"]
    await db.exceptions.update_one({"id": exception_id}, {"$set": upd})
    return await db.exceptions.find_one({"id": exception_id}, NO_ID)
