"""Approvals queue, AI runs, audit log, settings, integrations status, health."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import config
from core.approvals import APPROVAL_KINDS, approval_controls, four_eyes_block
from core.audit import actor_ref, audit
from core.build import build_info
from core.db import db
from core.deps import current_user, require_internal, require_perm
from core.events import emit
from core.llm import llm_available, provider_name
from core.email import email_configured
from core.settings import get_settings, update_settings
from core.util import NO_ID, now_iso

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/requests/{request_id}")
async def request_outcome(request_id: str, user: dict = Depends(current_user)):
    """Outcome of a create that answered 202 (core.bounded)."""
    from core.bounded import outcome
    return await outcome(request_id, user)


@router.get("/health")
async def health(deep: bool = False):
    """Liveness for the platform (200 while the database answers) plus the latest self-check summary.
    deep=1 runs every check now and answers 503 when one fails, for an outside uptime monitor."""
    from services import monitoring
    try:
        await db.command("ping")
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "time": now_iso(), "status": "fail",
                                                      "checks": {"database": "fail"}})
    h = await monitoring.deep_health(store=True) if deep else (await monitoring.latest_health() or {})
    out = {"ok": True, "time": now_iso(), "status": h.get("status") or "unknown", "checked_at": h.get("checked_at"),
           "checks": {k: v.get("status") for k, v in (h.get("checks") or {}).items()}}
    if deep and h.get("status") == "fail":
        return JSONResponse(status_code=503, content={**out, "ok": False})
    return out


@router.get("/version")
async def version():
    """The commit this server runs, so a probe can tell which build is live."""
    return build_info()


class DecisionIn(BaseModel):
    decision: str  # approved | rejected
    note: str | None = None
    data: dict | None = None  # optional edits the approver made (e.g. adjusted price)
    override: bool = False  # four-eyes override when no other active user can approve (note required, audited)


FOUR_EYES_MIN_NOTE = 10


def _four_eyes_refusal(status: int, code: str, detail: str) -> JSONResponse:
    # `detail` keeps the API's error shape; `code` lets the UI show its own text (approvals.four_eyes.<code>).
    fi = {"four_eyes_required": "Toisen valtuutetun henkilön on hyväksyttävä ilmoitus tai pyyntö.",
          "four_eyes_single_user": "Toista valtuutettua hyväksyjää ei ole. Perustele auditoitu poikkeus.",
          "four_eyes_override_note": "Kirjoita poikkeuksen perustelu (vähintään 10 merkkiä)."}[code]
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, "fi": fi, "en": detail,
                                                     "override_allowed": code != "four_eyes_required"})


@router.get("/approvals")
async def list_approvals(status: str = "pending", kind: str | None = None, limit: int = 200,
                         user: dict = Depends(require_internal)):
    q: dict = {}
    if status != "all":
        q["status"] = status
    if kind:
        q["kind"] = kind
    items = await db.approvals.find(q, NO_ID).sort("created_at", -1).to_list(min(limit, 500))
    perms = set(user["permissions"])
    cache: dict = {}
    for a in items:
        meta = APPROVAL_KINDS.get(a["kind"], {})
        a["kind_label"] = meta.get("label", a["kind"])
        a["can_decide"] = meta.get("perm") in perms
        a["four_eyes"] = await four_eyes_block(a, user, cache) if a["status"] == "pending" else None
    counts = {}
    async for row in db.approvals.aggregate([{"$match": {"status": "pending"}}, {"$group": {"_id": "$kind", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    return {"items": items, "pending_counts": counts}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, user: dict = Depends(require_internal)):
    a = await db.approvals.find_one({"id": approval_id}, NO_ID)
    if not a:
        raise HTTPException(404, "Approval not found")
    return a


@router.post("/approvals/{approval_id}/decide")
async def decide(approval_id: str, body: DecisionIn, user: dict = Depends(require_internal)):
    a = await db.approvals.find_one({"id": approval_id}, NO_ID)
    if not a:
        raise HTTPException(404, "Approval not found")
    if a["status"] != "pending":
        raise HTTPException(409, f"Already {a['status']}")
    perm = APPROVAL_KINDS.get(a["kind"], {}).get("perm")
    if perm and perm not in user["permissions"]:
        raise HTTPException(403, f"Missing permission: {perm}")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be approved or rejected")
    if body.decision == "approved":  # s11: kind-specific controls (customs AEO: authorised filers, POA, EORI, KYC)
        controls = await approval_controls(a, user, record=True)
        if (controls or {}).get("refusal"):
            ref = controls["refusal"]
            return JSONResponse(status_code=ref["status"], content={k: v for k, v in ref.items() if k != "status"})
    override = None
    block = await four_eyes_block(a, user) if body.decision == "approved" else None  # rejecting your own request is fine
    if block == "colleague":
        return _four_eyes_refusal(409, "four_eyes_required",
                                  f"Four-eyes rule: you requested this approval, so another person with {perm} must approve it.")
    if block == "single_user":
        if not body.override:
            return _four_eyes_refusal(409, "four_eyes_single_user",
                                      f"Four-eyes rule: you requested this approval and no other active user has {perm}. "
                                      f"Approve with an override reason (at least {FOUR_EYES_MIN_NOTE} characters); the override is audited.")
        if len((body.note or "").strip()) < FOUR_EYES_MIN_NOTE:
            return _four_eyes_refusal(400, "four_eyes_override_note",
                                      f"Write why you approve your own request (at least {FOUR_EYES_MIN_NOTE} characters).")
        override = {"by": actor_ref(user), "at": now_iso(), "note": body.note.strip(),
                    "why": f"no other active user has {perm}"}
    changes = {"status": body.decision, "decided_at": now_iso(),
               "decided_by": {"id": user["id"], "name": user.get("name")}, "decision_note": body.note,
               "decision_data": body.data or {}, "four_eyes_override": override}
    await db.approvals.update_one({"id": approval_id}, {"$set": changes})
    a.update(changes)
    await audit(user, f"approval_{body.decision}", a["ref_type"], a["ref_id"],
                {"approval_id": approval_id, "kind": a["kind"], "note": body.note}, company_id=a.get("company_id"))
    if override:
        await audit(user, "approval_four_eyes_override", a["ref_type"], a["ref_id"],
                    {"approval_id": approval_id, "kind": a["kind"], "note": override["note"], "why": override["why"]},
                    company_id=a.get("company_id"))
    await emit("approval.decided", {"approval": a, "actor": user})
    return a


@router.get("/ai-runs")
async def ai_runs(agent: str | None = None, status: str | None = None, limit: int = 100,
                  user: dict = Depends(require_perm("ai.view"))):
    q = {}
    if agent:
        q["agent"] = agent
    if status:
        q["status"] = status
    items = await db.ai_runs.find(q, NO_ID).sort("created_at", -1).to_list(min(limit, 500))
    stats = {}
    async for row in db.ai_runs.aggregate([{"$group": {"_id": {"agent": "$agent", "status": "$status"}, "n": {"$sum": 1},
                                                        "avg_ms": {"$avg": "$latency_ms"}}}]):
        k = row["_id"]["agent"]
        s = stats.setdefault(k, {"agent": k, "ok": 0, "error": 0, "avg_ms": 0})
        s[row["_id"]["status"]] = row["n"]
        if row["_id"]["status"] == "ok":
            s["avg_ms"] = int(row["avg_ms"] or 0)
    return {"items": items, "agents": list(stats.values()), "provider": provider_name(), "available": llm_available()}


@router.get("/audit")
async def audit_log(entity: str | None = None, entity_id: str | None = None, limit: int = 100,
                    user: dict = Depends(current_user)):
    q: dict = {}
    if entity:
        q["entity"] = entity
    if entity_id:
        q["entity_id"] = entity_id
    if user.get("org_type") != "internal":
        q["company_id"] = user.get("company_id")
    items = await db.audit_log.find(q, NO_ID).sort("at", -1).to_list(min(limit, 500))
    return {"items": items}


@router.get("/settings")
async def read_settings(user: dict = Depends(require_internal)):
    return await get_settings()


@router.patch("/settings")
async def write_settings(body: dict, user: dict = Depends(require_perm("settings.write"))):
    from core.autonomy import validate_settings
    validate_settings(body)
    s = await update_settings(body)
    await audit(user, "settings_updated", "settings", "app", body)
    return s


@router.get("/integrations/status")
async def integrations_status(user: dict = Depends(require_internal)):
    """What is live, what is in test/sandbox mode, what is not configured. No secret values."""
    def mode_of(key: str | None, live_prefixes: tuple, test_prefixes: tuple):
        if not key:
            return "not_configured"
        if key.startswith(live_prefixes):
            return "live"
        if key.startswith(test_prefixes):
            return "test"
        return "configured"

    settings = await get_settings()
    return {
        "stripe": {"mode": mode_of(config.STRIPE_API_KEY, ("sk_live", "rk_live"), ("sk_test", "rk_test")),
                   "webhook": bool(config.STRIPE_WEBHOOK_SECRET)},
        "email": {"provider": config.EMAIL_PROVIDER,
                  "mode": "live" if email_configured() else "log_only",
                  "from": config.EMAIL_FROM},
        "sms": {"provider": config.SMS_PROVIDER,  # s14: core/sms.py; test credentials send from Twilio's magic number
                "mode": ("test" if config.TWILIO_FROM == "+15005550006" else "live")
                if config.SMS_PROVIDER == "twilio" and config.TWILIO_ACCOUNT_SID else "log_only",
                "enabled": bool(settings.get("sms_notifications_enabled"))},
        "ai": {"provider": provider_name(), "available": llm_available(), "model": config.LLM_MODEL},
        "tulli": {"mode": config.TULLI_MODE, "endpoint_set": bool(config.TULLI_ENDPOINT),
                  "declarant_id_set": bool(config.TULLI_DECLARANT_ID)},
        "digitraffic": {"enabled": config.DIGITRAFFIC_ENABLED},
        "company": {"business_id": settings["seller_business_id"] or None, "vat_id": settings["seller_vat_id"] or None,
                    "eori": config.COMPANY_EORI or None},
    }


@router.post("/settings/autonomy/{name}")
async def autonomy_switch(name: str, body: dict, user: dict = Depends(require_perm("settings.write"))):
    from core.autonomy import switch
    return await switch(name, body.get("enabled"), body.get("confirmation", ""), user)


@router.get("/autonomy/status")
async def autonomy_status(user: dict = Depends(require_internal)):
    s = await get_settings()
    meta = await db.settings.find_one({"key": "app"}, NO_ID) or {}
    return {"autopilot_enabled": s["autopilot_enabled"], "partner_network_enabled": s["partner_network_enabled"],
            "changes": {k: v for k, v in meta.items() if k.endswith("_changed")},
            "actions": await db.autonomy_actions.find({}, NO_ID).sort("at", -1).to_list(100)}


@router.get("/environment")
async def environment():
    return {"example_data": config.SEED_DEMO}


@router.post("/autonomy/actions/{action_id}/reverse")
async def reverse_action(action_id: str, user: dict = Depends(require_perm("settings.write"))):
    from core.autonomy import action
    a = await db.autonomy_actions.find_one({"id": action_id}, NO_ID)
    if not a or not a.get("reversible") or a.get("reversal"):
        raise HTTPException(409, "This action cannot be reversed")
    entity, rid = a["entity"], a["entity_id"]
    if entity == "quote":
        q = await db.quotes.find_one({"id": rid}, NO_ID)
        if not q or q.get("status") not in ("sent", "pending_approval", "draft"):
            raise HTTPException(409, "Accepted or booked quotes need a cancellation agreement")
        await db.quotes.update_one({"id": rid}, {"$set": {"status": "draft", "published": False, "approval": {"required": True}}})
    elif entity == "invoice":
        from services.billing import void_invoice, BillingError
        try:
            await void_invoice(rid, "Autopilot action reversed by operator", user)
        except BillingError as exc:
            raise HTTPException(exc.status, str(exc))
    elif entity == "rate_card":
        await db.rate_cards.update_one({"id": rid}, {"$set": {"status": "revoked"}})
        r = await db.rate_cards.find_one({"id": rid}, NO_ID)
        if r and r.get("outreach_id"):
            await db.partner_outreach.update_one({"id": r["outreach_id"]}, {"$set": {"status": "revoked"}})
    elif entity == "exception":
        from services.exceptions_agent import _set_has_exception
        e = await db.exceptions.find_one({"id": rid}, NO_ID)
        if not e or e.get("status") != "resolved":
            raise HTTPException(409, "Only a resolved exception can be reopened")
        await db.exceptions.update_one({"id": rid}, {"$set": {"status": "open", "resolved_at": None,
                                     "resolution": None, "resolved_by": None}})
        await _set_has_exception(e.get("shipment_id"))
    else:
        raise HTTPException(409, "Use the owning workflow to correct this action")
    reversal = {"at": now_iso(), "by": {"id": user["id"], "name": user.get("name")}}
    await db.autonomy_actions.update_one({"id": action_id}, {"$set": {"reversal": reversal}})
    await audit(user, "autopilot_action_reversed", entity, rid, {"action_id": action_id, **reversal})
    return {"reversed": True}
