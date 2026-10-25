"""Autopilot control room API: switch state, kill switch, guardrails, customs delegation, decision log, jobs,
dead letters, schedules, alerts and health. Importing this module registers every agent job and schedule."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from core import jobs
from core.agent_runtime import credit_position, spend_today
from core.alerts import acknowledge, open_alerts
from core.audit import audit
from core.autonomy import (TEST_GRANT_MAX_MINUTES, active_test_grants, revoke_test_grants, set_test_grant,
                           validate_settings)
from core.db import db
from core.deps import require_internal, require_perm
from core.legal_points import public_list
from core.settings import get_settings, update_settings
from core.util import NO_ID, now_iso
import services.agents  # noqa: F401  (registers every agent job and schedule)
from services import monitoring

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])

GUARDRAIL_KEYS = ("guard_spend_per_day_cents", "guard_confidence_floor", "guard_claim_limit_cents",
                  "guard_default_credit_limit_cents", "insurance_claim_limit_cents", "owner_queue_sla_hours",
                  "owner_escalation_hours", "owner_alert_email", "customs_confirm_timeout_hours", "dunning_enabled",
                  "dunning_reminder1_days", "dunning_reminder2_days", "dunning_final_days", "dunning_collection_days",
                  "dunning_fixed_compensation_cents", "dunning_reminder_fee_cents", "rate_expiry_warning_days",
                  "autopilot_quote_max_cents", "autopilot_min_margin_pct")


@router.get("/status")
async def status(user: dict = Depends(require_internal)):
    s = await get_settings()
    doc = await db.raw.settings.find_one({"key": "app"}, {"_id": 0}) or {}
    pending = await db.raw.approvals.count_documents({"status": "pending"})
    overdue = await db.raw.approvals.count_documents({"status": "pending", "sla_due_at": {"$lt": now_iso()}})
    since = monitoring.day_start_iso()
    auto_today = await db.raw.agent_decisions.count_documents({"at": {"$gte": since}, "outcome": "auto"})
    queued_today = await db.raw.agent_decisions.count_documents({"at": {"$gte": since}, "outcome": "owner_queue"})
    return {
        "autopilot_enabled": s["autopilot_enabled"], "halted": s["autopilot_halted"],
        "changed": doc.get("autopilot_enabled_changed"), "halted_changed": doc.get("autopilot_halted_changed"),
        "guardrails": {k: s.get(k) for k in GUARDRAIL_KEYS},
        "customs_delegation": {"enabled": s["customs_filing_delegated"], "scope": s["customs_delegation_scope"],
                               "record": doc.get("customs_delegation_record")},
        "spend_today_cents": await spend_today(), "owner_queue": {"pending": pending, "overdue": overdue},
        "decisions_today": {"auto": auto_today, "owner_queue": queued_today},
        "jobs": await jobs.counts(), "health": await monitoring.latest_health(),
        "workers": [w async for w in db.raw.worker_heartbeats.find({}, {"_id": 0}).sort("beat_at", -1).limit(10)],
        "alerts": await open_alerts(20),
        "test_tenant_autopilot": await active_test_grants(),
    }


class KillIn(BaseModel):
    note: str | None = None


@router.post("/kill")
async def kill(body: KillIn, user: dict = Depends(require_perm("settings.write"))):
    """Kill switch: Autopilot off and every agent job held until a person resumes with the typed word."""
    before = await get_settings()
    await update_settings({"autopilot_enabled": False, "autopilot_halted": True})
    rec = {"at": now_iso(), "by": {"id": user["id"], "name": user.get("name")}, "note": body.note, "enabled": True}
    await db.raw.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted_changed": rec,
                                                               "autopilot_enabled_changed": {**rec, "enabled": False}}})
    await audit(user, "autopilot_killed", "settings", "app", {"before": {"autopilot_enabled": before["autopilot_enabled"],
                                                                         "autopilot_halted": before["autopilot_halted"]},
                                                              "note": body.note})
    await revoke_test_grants(user, "kill_switch")
    return await status(user)


class TestTenantAutopilotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    minutes: StrictInt = Field(30, ge=1, le=TEST_GRANT_MAX_MINUTES)
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    agents: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] | None = \
        Field(None, min_length=1, max_length=40)  # only these agents (and "email_channel"); omitted = every agent


@router.patch("/test-tenants/{company_id}")
async def test_tenant_autopilot(company_id: str, body: TestTenantAutopilotIn,
                                user: dict = Depends(require_perm("settings.write"))):
    """s6 r94: Autopilot for one test tenant only, for 1-60 minutes, with the global switch off. Every guard in
    decide() (kill switch, legal points, confidence, spend, credit, claim) still applies; the kill switch ends it."""
    return await set_test_grant(company_id, body.enabled, body.minutes, body.note, user, agents=body.agents)


class ConfirmIn(BaseModel):
    confirmation: str


@router.post("/resume")
async def resume(body: ConfirmIn, user: dict = Depends(require_perm("settings.write"))):
    """Lift the kill switch. Agents prepare work again; Autopilot itself stays as it is (off after a kill)."""
    if body.confirmation != "enable":
        raise HTTPException(400, "Type enable to enable autonomous actions")
    await update_settings({"autopilot_halted": False})
    await db.raw.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted_changed": {
        "at": now_iso(), "by": {"id": user["id"], "name": user.get("name")}, "enabled": False}}})
    await audit(user, "autopilot_resumed", "settings", "app", {})
    return await status(user)


@router.patch("/guardrails")
async def guardrails(body: dict, user: dict = Depends(require_perm("settings.write"))):
    unknown = set(body) - set(GUARDRAIL_KEYS)
    if unknown:
        raise HTTPException(400, "Unknown guardrail: " + ", ".join(sorted(unknown)))
    validate_settings(body)
    before = await get_settings()
    await update_settings(body)
    await audit(user, "autopilot_guardrails_changed", "settings", "app",
                {"changes": {k: {"from": before.get(k), "to": v} for k, v in body.items() if before.get(k) != v}})
    return (await status(user))["guardrails"]


class DelegationIn(BaseModel):
    enabled: bool
    scope: str = "direct"  # direct | direct_and_indirect
    confirmation: str = ""
    signer_name: str = ""
    signer_title: str = ""


@router.post("/customs-delegation")
async def customs_delegation(body: DelegationIn, user: dict = Depends(require_perm("settings.write"))):
    """The director's standing delegation to lodge customs declarations without a person approving each one."""
    if body.scope not in ("direct", "direct_and_indirect"):
        raise HTTPException(400, "Scope must be direct or direct_and_indirect")
    if body.enabled:
        if body.confirmation != "enable":
            raise HTTPException(400, "Type enable to enable autonomous actions")
        if len(body.signer_name.strip()) < 3 or len(body.signer_title.strip()) < 2:
            raise HTTPException(400, "Give the name and title of the person who grants the delegation")
    await update_settings({"customs_filing_delegated": body.enabled, "customs_delegation_scope": body.scope})
    rec = {"at": now_iso(), "by": {"id": user["id"], "name": user.get("name")}, "enabled": body.enabled,
           "scope": body.scope, "signer_name": body.signer_name.strip(), "signer_title": body.signer_title.strip()}
    await db.raw.settings.update_one({"key": "app"}, {"$set": {"customs_delegation_record": rec},
                                                      "$push": {"customs_delegation_history": rec}})
    await audit(user, "customs_delegation_changed", "settings", "app", rec)
    return (await status(user))["customs_delegation"]


@router.get("/decisions")
async def decisions(agent: str | None = None, outcome: str | None = None, subject_id: str | None = None,
                    company_id: str | None = None, limit: int = Query(100, le=500), before: str | None = None,
                    user: dict = Depends(require_internal)):
    q: dict = {}
    if agent:
        q["agent"] = agent
    if outcome:
        q["outcome"] = outcome
    if subject_id:
        q["subject_id"] = subject_id
    if company_id:
        q["company_id"] = company_id
    if before:
        q["at"] = {"$lt": before}
    items = await db.raw.agent_decisions.find(q, {"_id": 0, "inputs": 0}).sort("at", -1).to_list(limit)
    agents = {}
    async for r in db.raw.agent_decisions.aggregate([{"$group": {"_id": {"a": "$agent", "o": "$outcome"}, "n": {"$sum": 1},
                                                                  "fi": {"$first": "$agent_label_fi"},
                                                                  "en": {"$first": "$agent_label_en"}}}]):
        a = agents.setdefault(r["_id"]["a"], {"agent": r["_id"]["a"], "label_fi": r["fi"], "label_en": r["en"],
                                               "auto": 0, "owner_queue": 0, "blocked": 0})
        a[r["_id"]["o"]] = a.get(r["_id"]["o"], 0) + r["n"]
    return {"items": items, "agents": sorted(agents.values(), key=lambda x: x["agent"]), "total": len(items)}


@router.get("/decisions/{decision_id}")
async def decision(decision_id: str, user: dict = Depends(require_internal)):
    d = await db.raw.agent_decisions.find_one({"id": decision_id}, NO_ID)
    if not d:
        raise HTTPException(404, "Decision not found")
    return d


@router.get("/jobs")
async def list_jobs(status: str | None = None, kind: str | None = None, subject_id: str | None = None,
                    limit: int = Query(100, le=500), user: dict = Depends(require_internal)):
    q: dict = {}
    if status:
        q["status"] = status
    if kind:
        q["kind"] = kind
    if subject_id:
        q["subject.id"] = subject_id
    items = await db.raw.jobs.find(q, {"_id": 0, "history": 0}).sort("created_at", -1).to_list(limit)
    labels = jobs.registered_jobs()
    for j in items:
        meta = labels.get(j["kind"], {})
        j["label_fi"], j["label_en"] = meta.get("label_fi", j["kind"]), meta.get("label_en", j["kind"])
    return {"items": items, "counts": await jobs.counts(), "kinds": labels}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: dict = Depends(require_internal)):
    j = await db.raw.jobs.find_one({"id": job_id}, NO_ID)
    if not j:
        raise HTTPException(404, "Job not found")
    return j


class CancelJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, body: CancelJobIn, user: dict = Depends(require_perm("settings.write"))):
    """Stop one queued, retrying or running job (e.g. one that crashes the server). Audit-logged."""
    return await jobs.cancel_job(job_id, user, body.note)


@router.get("/dead-letters")
async def dead_letters(status: str = "open", limit: int = Query(100, le=500), user: dict = Depends(require_internal)):
    q = {} if status == "all" else {"status": status}
    items = await db.raw.dead_letters.find(q, {"_id": 0, "trace": 0}).sort("at", -1).to_list(limit)
    labels = jobs.registered_jobs()
    for d in items:
        meta = labels.get(d["kind"], {})
        d["label_fi"], d["label_en"] = meta.get("label_fi", d["kind"]), meta.get("label_en", d["kind"])
    return {"items": items, "open": await db.raw.dead_letters.count_documents({"status": "open"})}


@router.post("/dead-letters/{dead_letter_id}/retry")
async def retry_dead_letter(dead_letter_id: str, user: dict = Depends(require_perm("settings.write"))):
    return await jobs.retry_dead_letter(dead_letter_id, user)


class DismissIn(BaseModel):
    note: str


@router.post("/dead-letters/{dead_letter_id}/dismiss")
async def dismiss_dead_letter(dead_letter_id: str, body: DismissIn, user: dict = Depends(require_perm("settings.write"))):
    if len(body.note.strip()) < 5:
        raise HTTPException(400, "Write a short reason")
    return await jobs.dismiss_dead_letter(dead_letter_id, user, body.note.strip())


def _stripe_event_view(ev: dict) -> dict:
    import json
    try:
        payload_live = (json.loads(ev.get("payload") or "{}") or {}).get("livemode")
    except ValueError:
        payload_live = None
    return {"id": ev["id"], "type": ev.get("type"), "status": ev.get("status"), "error": ev.get("error"),
            "received_at": ev.get("received_at"), "verified": ev.get("verified"), "livemode": ev.get("livemode"),
            "payload_livemode": payload_live, "ignored": ev.get("ignored")}


@router.get("/stripe-events/{event_id}")
async def stripe_event(event_id: str, user: dict = Depends(require_internal)):
    ev = await db.raw.stripe_events.find_one({"id": event_id}, NO_ID)
    if not ev:
        raise HTTPException(404, "Stripe event not found")
    return _stripe_event_view(ev)


@router.post("/stripe-events/{event_id}/ignore")
async def ignore_stripe_event(event_id: str, body: DismissIn, user: dict = Depends(require_perm("settings.write"))):
    """Owner decision: a failed or stuck TEST-mode Stripe event (a drill, a test payment for no invoice of ours) is
    ignored, so the daily reconciliation stops listing it. Live-mode events are never ignored here."""
    note = body.note.strip()
    if len(note) < 5:
        raise HTTPException(400, "Write a short reason")
    ev = await db.raw.stripe_events.find_one({"id": event_id}, NO_ID)
    if not ev:
        raise HTTPException(404, "Stripe event not found")
    view = _stripe_event_view(ev)
    if view["livemode"] is not False or view["payload_livemode"] not in (False, None):
        raise HTTPException(409, "Only test-mode events (livemode false) can be ignored here")
    if ev.get("status") not in ("failed", "received"):
        raise HTTPException(409, f"The event is {ev.get('status')}; only failed or unprocessed events can be ignored")
    ignored = {"at": now_iso(), "by": {"id": user.get("id"), "name": user.get("name")}, "note": note[:300],
               "previous_status": ev.get("status")}
    await db.raw.stripe_events.update_one({"id": event_id, "status": ev["status"]}, {"$set": {"status": "ignored", "ignored": ignored}})
    await audit(user, "stripe_event_ignored", "stripe_event", event_id,
                {"type": ev.get("type"), "previous_status": ev.get("status"), "livemode": False, "note": note[:300]})
    return _stripe_event_view({**ev, "status": "ignored", "ignored": ignored})


@router.get("/schedules")
async def schedules(user: dict = Depends(require_internal)):
    items = await db.raw.job_schedules.find({}, NO_ID).sort("name", 1).to_list(200)
    for s in items:
        if s.get("last_job_id"):
            j = await db.raw.jobs.find_one({"id": s["last_job_id"]}, {"_id": 0, "status": 1, "finished_at": 1,
                                                                       "last_error": 1, "duration_ms": 1})
            s["last_job"] = j
    return {"items": items}


@router.post("/schedules/{name}/run")
async def run_schedule(name: str, user: dict = Depends(require_perm("settings.write"))):
    return await jobs.run_schedule_now(name, user)


class ScheduleToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]


@router.patch("/schedules/{name}")
async def toggle_schedule(name: str, body: ScheduleToggleIn, user: dict = Depends(require_perm("settings.write"))):
    """Pause or resume one schedule (e.g. a refresh that is crashing the server) without halting Autopilot.
    Jobs already queued are not touched; sync_schedules() keeps `enabled` across restarts."""
    from core.audit import actor_ref
    before = await db.raw.job_schedules.find_one({"name": name}, NO_ID)
    if not before:
        raise HTTPException(404, "Schedule not found")
    change = {"at": now_iso(), "by": actor_ref(user), "note": body.note}
    s = await db.raw.job_schedules.find_one_and_update(
        {"name": name}, {"$set": {"enabled": body.enabled, "enabled_changed": change, "updated_at": change["at"]}},
        projection=NO_ID, return_document=True)
    await audit(user, "schedule_enabled" if body.enabled else "schedule_disabled", "job_schedule", name,
                {"before": {"enabled": before.get("enabled", True)}, "after": {"enabled": body.enabled},
                 "note": body.note})
    return s


@router.get("/process")
async def process_info(user: dict = Depends(require_internal)):
    """s3 r2: memory per server process. this = the process that answered; workers = every heartbeating process with
    its RSS, peak RSS, pid and start; creates = RSS sampled right after the last bounded creates."""
    from core.procinfo import proc_mem
    from core.jobs import WORKER_ID
    return {"this": {**proc_mem(), "worker_id": WORKER_ID},
            "workers": [w async for w in db.raw.worker_heartbeats.find({}, {"_id": 0}).sort("beat_at", -1).limit(10)],
            "creates": await db.raw.process_samples.find({}, {"_id": 0, "expires_at": 0}).sort("at", -1).to_list(20)}


@router.get("/health")
async def health(user: dict = Depends(require_internal)):
    return await monitoring.deep_health(store=False)


@router.get("/alerts")
async def alerts(status: str = "open", limit: int = Query(50, le=200), user: dict = Depends(require_internal)):
    q = {} if status == "all" else {"status": status}
    return {"items": await db.raw.alerts.find(q, NO_ID).sort("opened_at", -1).to_list(limit)}


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, user: dict = Depends(require_internal)):
    return await acknowledge(alert_id, user)


@router.get("/legal-points")
async def legal_points(user: dict = Depends(require_internal)):
    return {"items": public_list()}


@router.get("/credit/{company_id}")
async def credit(company_id: str, user: dict = Depends(require_internal)):
    return await credit_position(company_id)


@router.get("/reports")
async def reports(kind: str | None = None, limit: int = Query(30, le=200), user: dict = Depends(require_internal)):
    q = {"kind": kind} if kind else {}
    return {"items": await db.raw.owner_reports.find(q, {"_id": 0, "html": 0}).sort("created_at", -1).to_list(limit)}
