"""Human approval queue for work where liability requires a person.

Kinds: quote_threshold, customs_filing, claim, ai_message, kyc_review, credit_limit.
A slice creates an approval with request_approval(); ops decide it in /ops/approvals;
the decision is emitted as `approval.decided` and the owning slice acts on it:

    @on("approval.decided")
    async def _(p):
        a = p["approval"]
        if a["kind"] != "quote_threshold": return
        ...
"""
from core.audit import actor_ref
from core.db import db
from core.permissions import permissions_for
from core.settings import get_settings
from core.util import NO_ID, new_id, now_iso

APPROVAL_KINDS = {
    "quote_threshold": {"label": "Quote above threshold", "perm": "quotes.approve"},
    "customs_filing": {"label": "Customs declaration filing", "perm": "customs.file"},
    "claim": {"label": "Claim decision", "perm": "claims.approve"},
    "ai_message": {"label": "AI reply to customer", "perm": "messages.ops"},
    "kyc_review": {"label": "KYC / sanctions review", "perm": "kyc.approve"},
    "credit_limit": {"label": "Credit limit", "perm": "customers.write"},
    "email_triage": {"label": "Customer e-mail the channel did not act on", "perm": "messages.ops"},
    # Owner queue kinds added by Autopilot operations (s2)
    "booking_review": {"label": "Booking review", "perm": "shipments.write"},
    "carrier_booking": {"label": "Carrier booking", "perm": "shipments.write"},
    "exception_action": {"label": "Exception action", "perm": "shipments.write"},
    "document_review": {"label": "Document review", "perm": "customs.review"},
    "invoice_issue": {"label": "Invoice issue", "perm": "billing.write"},
    "dunning_collection": {"label": "Debt collection", "perm": "billing.write"},
    "payment_match": {"label": "Unmatched payment", "perm": "billing.write"},
    "rate_update": {"label": "Rate update", "perm": "rates.write"},
    "partner_rate": {"label": "Partner rate", "perm": "rates.write"},
    "partner_rfq": {"label": "One real carrier RFQ", "perm": "rates.write"},  # s3 single real RFQ
    "legal_signature": {"label": "Signature or legal decision", "perm": "settings.write"},
    "trade_document": {"label": "Transport document review", "perm": "shipments.write"},
    "execution_review": {"label": "Execution review", "perm": "shipments.write"},
    "privacy_erasure": {"label": "Data erasure request (GDPR art. 17)", "perm": "settings.write"},  # s12
    "dg_review": {"label": "Dangerous goods review", "perm": "shipments.write"},  # s12, legal point dangerous_goods
    "prospect_email": {"label": "First e-mail to a prospect", "perm": "customers.write"},  # s7 CRM, Autopilot off
    "carrier_recourse": {"label": "Carrier recourse claim", "perm": "claims.approve"},  # s2, services/claims_recourse.py
    "email_triage": {"label": "Customer e-mail the channel did not act on", "perm": "messages.ops"},
}

# Owner-queue SLA per kind (hours). Kinds not listed use the owner_queue_sla_hours setting.
SLA_HOURS = {"ai_message": 2, "customs_filing": 4, "quote_threshold": 4, "booking_review": 4, "carrier_booking": 4,
             "exception_action": 4, "kyc_review": 24, "document_review": 8, "invoice_issue": 24, "payment_match": 48,
             "claim": 72, "dunning_collection": 72, "rate_update": 72, "legal_signature": 72, "trade_document": 8,
             "execution_review": 4, "carrier_recourse": 72}


async def request_approval(kind: str, ref_type: str, ref_id: str, title: str, *, summary: str = "",
                           reason: str = "", company_id: str | None = None, requested_by: dict | None = None,
                           data: dict | None = None, amount_cents: int | None = None,
                           link: str | None = None, extra: dict | None = None) -> dict:
    existing = await db.approvals.find_one({"kind": kind, "ref_id": ref_id, "status": "pending"}, NO_ID)
    if existing:
        return existing
    doc = {
        "id": new_id(), "kind": kind, "ref_type": ref_type, "ref_id": ref_id, "title": title,
        "summary": summary, "reason": reason, "company_id": company_id, "amount_cents": amount_cents,
        "status": "pending", "requested_by": actor_ref(requested_by) if requested_by is not None else actor_ref(None),
        "data": data or {}, "link": link, "created_at": now_iso(), "decided_at": None, "decided_by": None,
        "decision_note": None,
        **(extra or {}),
    }
    try:
        from datetime import timedelta

        from core.util import iso, now
        hours = SLA_HOURS.get(kind) or float((await get_settings()).get("owner_queue_sla_hours") or 24)
        doc.update(sla_hours=hours, sla_due_at=iso(now() + timedelta(hours=hours)), escalation_level=0, escalated_at=None)
    except Exception:  # noqa: BLE001
        pass
    await db.approvals.insert_one(dict(doc))
    if doc.get("agent_decides"):
        return doc  # an agent decides this card itself at once (inside its guardrails): no owner notification
    try:  # owner notification (in-app + e-mail) and the decision-log link; never blocks the caller
        from core.events import emit
        await emit("approval.requested", {"approval": doc})
    except Exception:  # noqa: BLE001
        pass
    return doc


async def cancel_approvals(ref_id: str, kind: str | None = None):
    q = {"ref_id": ref_id, "status": "pending"}
    if kind:
        q["kind"] = kind
    await db.approvals.update_many(q, {"$set": {"status": "cancelled", "decided_at": now_iso()}})


# Four-eyes (Autopilot off): the person who asked for a customs filing or a claim payment cannot approve it.
def needs_four_eyes(a: dict) -> bool:
    if a.get("kind") == "customs_filing":
        return True
    return a.get("kind") == "claim" and (a.get("data") or {}).get("decision") == "approve"


async def eligible_colleagues(perm: str, exclude_user_id: str | None) -> int:
    """Active internal users other than `exclude_user_id` who hold `perm`."""
    n = 0
    async for u in db.users.find({"org_type": "internal", "disabled": {"$ne": True}, "id": {"$ne": exclude_user_id}},
                                 {"_id": 0, "role": 1, "extra_permissions": 1}):
        n += perm in permissions_for(u)
    return n


# Kind-specific approval controls (s11, additive): kind -> module exposing
#   async def approval_controls(a, user, *, record=False, cache=None) -> None | {
#       "refusal": {status, detail, code, fi, en, reasons, override_allowed} | None,   # this user may not approve at all
#       "four_eyes": {required, excluded, colleagues, colleagues_any, reasons}}         # thresholds, Autopilot on or off
# customs_filing: services.customs_aeo (who may file, four-eyes thresholds, segregation of duties).
APPROVAL_CONTROLS = {"customs_filing": "services.customs_aeo"}


async def approval_controls(a: dict, user: dict, *, record: bool = False, _cache: dict | None = None) -> dict | None:
    mod = APPROVAL_CONTROLS.get(a.get("kind"))
    if not mod or not user.get("id"):
        return None
    import importlib
    return await importlib.import_module(mod).approval_controls(a, user, record=record, cache=_cache)


async def four_eyes_block(a: dict, user: dict, _cache: dict | None = None) -> str | None:
    """None when `user` may approve `a`; "colleague" when someone else must; "single_user" when nobody else can."""
    cache = _cache if _cache is not None else {}
    fe = None
    if a.get("kind") in APPROVAL_CONTROLS and a.get("status", "pending") == "pending":
        key = ("controls", a.get("id"), user.get("id"))
        if key not in cache:
            cache[key] = await approval_controls(a, user, _cache=cache)
        fe = (cache[key] or {}).get("four_eyes")
    if fe and fe.get("required") and fe.get("excluded"):
        # above a threshold the preparer and the classifiers never approve alone, Autopilot on or off
        return "colleague" if fe.get("colleagues") else "single_user"
    if fe is not None:
        return None  # a real declaration uses the risk controls, including the below-threshold single approver
    if not needs_four_eyes(a) or not user.get("id") or (a.get("requested_by") or {}).get("id") != user["id"]:
        return None
    if "autopilot" not in cache:
        cache["autopilot"] = bool((await get_settings()).get("autopilot_enabled"))
    if cache["autopilot"]:
        return None
    if fe is not None:  # customs: a colleague is an authorised filer (customs_authorised_filers), not just customs.file
        return "colleague" if fe.get("colleagues_any") else "single_user"
    perm = APPROVAL_KINDS[a["kind"]]["perm"]
    if perm not in cache:
        cache[perm] = await eligible_colleagues(perm, user["id"])
    return "colleague" if cache[perm] else "single_user"
