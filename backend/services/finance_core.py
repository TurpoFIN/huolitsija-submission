"""Finance (s13) shared plumbing: scheduling, agent decisions, approval kinds, ledger accounts, small helpers.

Scheduling and agent decisions follow s2's published contract (shared/interfaces.md, s2 sections 1-2). When s2's
`core.jobs` / `core.agent_runtime` are importable they are used as-is; until then the same calls fall back to
`core.lifecycle.background_loop`, `core.approvals.request_approval`, `core.autonomy.action` and `core.audit`, so the
finance agents work on main today and move onto the durable runtime after the merge with no code change.

    from services.finance_core import finance_job, enqueue, agent_decide

    @finance_job("finance.payment_run", daily_at="10:00", label_fi="Maksuajo", label_en="Payment run")
    async def payment_run(j): ...

    await enqueue("finance.carrier_invoice_process", {"id": ci_id}, key=f"ci:{ci_id}")
    d = await agent_decide("approve_carrier_invoice", subject=("carrier_invoice", ci["id"]), ...)
    if d["auto"]: ...
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.approvals import APPROVAL_KINDS, request_approval
from core.audit import agent_actor, audit
from core.db import db, register_index
from core.lifecycle import background_loop
from core.settings import DEFAULTS, get_settings
from core.util import new_id, now_iso

log = logging.getLogger("finance")
HELSINKI = ZoneInfo("Europe/Helsinki")
AGENT = "finance"
AGENT_ACTOR = agent_actor("finance")

try:  # s2 durable jobs (feat/autopilot-ops)
    from core import jobs as _jobs  # type: ignore
except ImportError:  # pragma: no cover - depends on merge state
    _jobs = None
try:  # s2 agent runtime (decide + owner queue + guardrails)
    from core import agent_runtime as _runtime  # type: ignore
except ImportError:  # pragma: no cover
    _runtime = None

HAVE_JOBS = _jobs is not None
HAVE_RUNTIME = _runtime is not None

# ------------------------------------------------------------------------------------------- settings (additive)
FINANCE_DEFAULTS = {
    "ap_tolerance_pct": 2.0,             # carrier invoice may exceed the accrual by this % ...
    "ap_tolerance_abs_cents": 2500,      # ... or by this many cents, whichever is larger
    "ap_auto_approve_max_cents": 500000,  # Autopilot approves matched invoices up to this EUR amount
    "ap_payment_days_before_due": 2,     # pay this many days before the supplier's due date
    "collections_after_days_overdue": 21,
    "collections_fee_cents": 4000,       # Korkolaki 10 e §: EUR 40 fixed recovery compensation (B2B)
    "collections_agency_after_days": 30,
    "fx_revalue_month_end": True,
}
for _k, _v in FINANCE_DEFAULTS.items():
    DEFAULTS.setdefault(_k, _v)

# ------------------------------------------------------------------------------------------- owner-queue kinds
FINANCE_APPROVAL_KINDS = {
    "carrier_invoice_review": {"label": "Carrier invoice needs review", "perm": "billing.write"},
    "carrier_invoice_dispute": {"label": "Carrier invoice dispute", "perm": "billing.write"},
    "payment_run": {"label": "Supplier payment run", "perm": "billing.write"},
    "collections_action": {"label": "Collections action", "perm": "billing.write"},
}
for _k, _v in FINANCE_APPROVAL_KINDS.items():
    APPROVAL_KINDS.setdefault(_k, _v)

# ------------------------------------------------------------------------------------------- chart of accounts
# Finnish small-company chart (Liikekirjuri-style numbering). Editable per installation with env LEDGER_ACCOUNTS_JSON.
LEDGER_ACCOUNTS = {
    "receivables": "1700",          # Myyntisaamiset
    "accrued_income": "1760",       # Siirtosaamiset
    "vat_receivable": "1763",       # Arvonlisäverosaamiset (ostojen ALV)
    "disbursements_receivable": "1790",  # Läpikulkuerät (tulli, tuonnin ALV asiakkaan lukuun)
    "bank": "1910",                 # Pankkitili
    "payables": "2871",             # Ostovelat
    "accrued_expenses": "2960",     # Siirtovelat (kuluvaraukset)
    "vat_payable": "2939",          # Arvonlisäverovelka (myynnin ALV)
    "vat_rc_payable": "2940",       # ALV-velka käännetty verovelvollisuus (ostot EU/ei-EU)
    "sales_fi": "3000",             # Myynti 25,5 %
    "sales_zero_intl": "3010",      # Myynti 0 %, kansainvälinen kuljetus (AVL 71 §)
    "sales_zero_export": "3020",    # Myynti 0 %, vienti
    "sales_eu_rc": "3030",          # Palvelumyynti EU, käännetty verovelvollisuus
    "sales_non_eu": "3040",         # Palvelumyynti EU:n ulkopuolelle
    "freight_costs": "4000",        # Rahdit ja kuljetusostot
    "freight_costs_eu_rc": "4010",  # Kuljetusostot EU, käännetty verovelvollisuus
    "freight_costs_non_eu": "4020",  # Kuljetusostot EU:n ulkopuolelta
    "collection_fees_income": "3500",  # Perintä- ja muistutusmaksut
    "credit_losses": "8380",        # Luottotappiot
    "fx_gain": "9160",              # Kurssivoitot
    "fx_loss": "9660",              # Kurssitappiot
}


def ledger_accounts() -> dict:
    import json
    import os
    raw = os.environ.get("LEDGER_ACCOUNTS_JSON")
    if raw:
        try:
            return {**LEDGER_ACCOUNTS, **json.loads(raw)}
        except ValueError:
            log.warning("LEDGER_ACCOUNTS_JSON is not valid JSON; using the built-in chart")
    return dict(LEDGER_ACCOUNTS)


# ------------------------------------------------------------------------------------------- helpers
def lane_of(shipment: dict) -> dict:
    """Lane key used across finance and management: mode:OC>DC by country (same shape s3 uses)."""
    mode = shipment.get("mode") or "multimodal"
    oc = ((shipment.get("origin") or {}).get("country") or "??").upper()
    dc = ((shipment.get("destination") or {}).get("country") or "??").upper()
    return {"key": f"{mode}:{oc}>{dc}", "mode": mode, "origin_country": oc, "destination_country": dc}


def period_of(iso_or_date) -> str:
    s = str(iso_or_date or now_iso())
    return s[:7]


def helsinki_now() -> datetime:
    return datetime.now(HELSINKI)


async def halted() -> bool:
    """s2's kill switch (`autopilot_halted`) or Autopilot off: agents do nothing on their own."""
    doc = await db.settings.find_one({"key": "app"}, {"_id": 0, "autopilot_halted": 1}) or {}
    return bool(doc.get("autopilot_halted"))


async def autopilot_on() -> bool:
    s = await get_settings()
    return bool(s.get("autopilot_enabled")) and not await halted()


# ------------------------------------------------------------------------------------------- jobs (s2 shim)
_HANDLERS: dict[str, object] = {}
register_index("finance_schedule_runs", "key", unique=True)


def finance_job(kind: str, *, every_seconds: int | None = None, daily_at: str | None = None,
                monthly_day: int | None = None, label_fi: str = "", label_en: str = "", max_attempts: int = 5,
                timeout: int = 300, agent: bool = True):
    """Register `fn(job_doc)` as a durable job; optionally schedule it (every N s, daily HH:MM or monthly on day N
    at daily_at, Europe/Helsinki). Without s2's runtime the schedule runs on a background loop and fires once per
    slot across instances (unique key in `finance_schedule_runs`)."""

    def deco(fn):
        _HANDLERS[kind] = fn
        if HAVE_JOBS:
            target = fn
            if monthly_day:
                async def target(j, _fn=fn):  # s2 schedules are daily: run only on the month's day
                    if helsinki_now().day != monthly_day and not (j.get("payload") or {}).get("force"):
                        return {"skipped": f"runs on day {monthly_day} of the month"}
                    return await _fn(j)
                target.__name__ = fn.__name__
            _jobs.job(kind, max_attempts=max_attempts, timeout=timeout, agent=agent,
                      label_fi=label_fi, label_en=label_en)(target)
            wrapped = fn
            if every_seconds:
                _jobs.schedule(kind, job=kind, every_seconds=every_seconds, label_fi=label_fi, label_en=label_en)()
            elif daily_at and not monthly_day:
                _jobs.schedule(kind, job=kind, daily_at=daily_at, label_fi=label_fi, label_en=label_en)()
            elif monthly_day:
                # s2 schedules are daily at most coarse; the handler checks the day itself.
                _jobs.schedule(kind, job=kind, daily_at=daily_at or "06:00", label_fi=label_fi, label_en=label_en)()
            return wrapped
        if every_seconds:
            @background_loop(seconds=every_seconds)
            async def _loop():
                if agent and await halted():
                    return
                await _run_inline(kind, {}, key=None)
            _loop.__name__ = f"loop_{kind.replace('.', '_')}"
        elif daily_at:
            hh, mm = (int(x) for x in daily_at.split(":"))

            @background_loop(seconds=60)
            async def _daily():
                now = helsinki_now()
                if monthly_day and now.day != monthly_day:
                    return
                if (now.hour, now.minute) < (hh, mm):
                    return
                slot = f"schedule:{kind}:{now.date().isoformat()}"
                if agent and await halted():
                    return
                try:
                    await db.finance_schedule_runs.insert_one({"key": slot, "kind": kind, "at": now_iso()})
                except Exception:  # duplicate key: another instance or an earlier tick ran this slot
                    return
                await _run_inline(kind, {"scheduled_for": now.date().isoformat()}, key=slot)
            _daily.__name__ = f"daily_{kind.replace('.', '_')}"
        return fn

    return deco


async def _run_inline(kind: str, payload: dict, key: str | None):
    fn = _HANDLERS[kind]
    doc = {"id": new_id(), "kind": kind, "payload": payload, "key": key, "status": "running", "attempts": 1,
           "created_at": now_iso(), "source": "finance_fallback"}
    try:
        result = await fn(doc)
        await db.finance_job_runs.insert_one({**doc, "status": "done", "result": _trim(result), "finished_at": now_iso()})
        return result
    except Exception as exc:  # noqa: BLE001 - logged and visible in finance_job_runs
        name = type(exc).__name__
        if name == "JobDeferred":
            await db.finance_job_runs.insert_one({**doc, "status": "deferred", "error": str(exc), "finished_at": now_iso()})
            return None
        log.exception("finance job %s failed", kind)
        await db.finance_job_runs.insert_one({**doc, "status": "failed", "error": str(exc)[:2000], "finished_at": now_iso()})
        return None


def _trim(result):
    if result is None or isinstance(result, (int, float, str, bool)):
        return result
    import json
    text = json.dumps(result, default=str)
    return result if len(text) <= 8000 else {"trimmed": text[:8000]}


async def enqueue(kind: str, payload: dict, *, key: str | None = None, run_at=None, company_id=None, subject=None,
                  source: str = "event"):
    """Run a finance job now (or at run_at) through s2's queue; without it, run in a background task once per key."""
    if isinstance(subject, dict):
        subject = (subject["type"], subject["id"])
    if HAVE_JOBS:
        return await _jobs.enqueue(kind, payload, key=key, run_at=run_at, company_id=company_id, subject=subject,
                                   source=source)
    if key:
        try:
            await db.finance_schedule_runs.insert_one({"key": f"enqueue:{key}", "kind": kind, "at": now_iso()})
        except Exception:  # already enqueued once
            return {"kind": kind, "key": key, "status": "duplicate"}
    asyncio.get_running_loop().create_task(_run_inline(kind, payload, key))
    return {"kind": kind, "key": key, "status": "queued"}


async def run_job_now(kind: str, payload: dict | None = None):
    """Tests and the ops "run now" buttons: run the handler inline and return its result."""
    return await _run_inline(kind, payload or {}, key=None)


# ------------------------------------------------------------------------------------------- agent decisions
async def agent_decide(action: str, *, subject: tuple[str, str], subject_label: str, inputs: dict, decision: dict,
                       confidence: float | None = None, model: str | None = None, amount_cents: int | None = None,
                       spend_cents: int | None = None, company_id: str | None = None,
                       extra_reasons: list[dict] | None = None, approval_kind: str | None = None,
                       title: str = "", title_fi: str = "", summary: str = "", summary_fi: str = "",
                       link: str | None = None, approval_data: dict | None = None,
                       switch: str = "autopilot_enabled") -> dict:
    """One finance agent decision. Returns {auto, outcome, reasons, approval, id}.

    auto=True only when the switch is on, nothing is halted, the confidence floor holds and no stop reason applies.
    Otherwise (and when approval_kind is given) the work goes to the owner queue with the reasons."""
    if HAVE_RUNTIME:
        d = await _runtime.decide(
            AGENT, action, subject=subject, subject_label=subject_label, inputs=inputs, decision=decision,
            confidence=confidence, model=model, spend_cents=spend_cents, company_id=company_id,
            extra_reasons=extra_reasons or [], switch=switch, approval_kind=approval_kind, title=title,
            title_fi=title_fi, summary=summary, summary_fi=summary_fi, link=link, amount_cents=amount_cents,
            approval_data={**(approval_data or {}), "finance_action": (approval_data or {}).get("action"), "reasons": extra_reasons or []}, create_approval=bool(approval_kind))
        return {"auto": d.auto, "outcome": d.outcome, "reasons": d.reasons, "approval": d.approval, "id": d.id}

    settings = await get_settings()
    reasons = list(extra_reasons or [])
    if not settings.get(switch):
        reasons.append(policy_reason("switch_off", "Autopilot ei ole päällä", "Autopilot is off"))
    if await halted():
        reasons.append(policy_reason("halted", "Autopilot on pysäytetty", "Autopilot is halted"))
    floor = float(settings.get("guard_confidence_floor") or 0.8)
    if confidence is not None and confidence < floor:
        reasons.append(policy_reason("low_confidence", f"Varmuus {confidence:.2f} alle rajan {floor:.2f}",
                                     f"Confidence {confidence:.2f} below floor {floor:.2f}"))
    auto = not reasons
    row = {"id": new_id(), "at": now_iso(), "agent": AGENT, "action": action, "subject_type": subject[0],
           "subject_id": subject[1], "subject_label": subject_label, "company_id": company_id, "inputs": inputs,
           "decision": decision, "confidence": confidence, "model": model, "outcome": "auto" if auto else "owner_queue",
           "reasons": reasons, "autopilot_enabled": bool(settings.get("autopilot_enabled")), "title": title,
           "title_fi": title_fi, "approval_id": None}
    approval = None
    if not auto and approval_kind:
        approval = await request_approval(
            approval_kind, subject[0], subject[1], title_fi or title, summary=summary_fi or summary,
            reason="; ".join(r["fi"] for r in reasons), company_id=company_id, requested_by=AGENT_ACTOR,
            data={**(approval_data or {}), "agent_decision_id": row["id"], "reasons": reasons, "title_en": title,
                  "summary_en": summary}, amount_cents=amount_cents, link=link)
        row["approval_id"] = approval["id"]
    await db.agent_decisions.insert_one(dict(row))
    await audit(AGENT_ACTOR, f"finance_{action}_{row['outcome']}", subject[0], subject[1],
                {"decision": decision, "reasons": reasons, "approval_id": row["approval_id"]}, company_id=company_id)
    if auto:
        from core.autonomy import action as autonomy_action
        await autonomy_action(f"finance_{action}", subject[0], subject[1], company_id=company_id,
                              data={"decision": decision, "inputs": inputs})
    return {"auto": auto, "outcome": row["outcome"], "reasons": reasons, "approval": approval, "id": row["id"]}


def policy_reason(code: str, fi: str, en: str) -> dict:
    if HAVE_RUNTIME and hasattr(_runtime, "policy_reason"):
        return _runtime.policy_reason(code, fi, en)
    return {"code": code, "fi": fi, "en": en}


def days_ago(n: int) -> str:
    return (helsinki_now() - timedelta(days=n)).date().isoformat()
