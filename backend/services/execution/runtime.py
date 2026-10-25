"""Execution facade over s2 durable jobs and agent policy; alerts and signed partner links."""
import inspect
import logging
import os
from datetime import timedelta
import jwt
from pymongo.errors import DuplicateKeyError
from core import security, jobs as _core_jobs, agent_runtime as _core_ar
from core.audit import agent_actor, audit
from core.db import db, register_index
from core.events import emit
from core.util import now, now_iso, new_id
from core.jobs import JobDeferred, JobFailed, schedule, enqueue, work_once

log = logging.getLogger("execution")
AGENT = "execution"
AGENT_ACTOR = _core_ar.agent_bot(AGENT)
_REGISTRY = {}
register_index("execution_alerts", "key", unique=True, sparse=True)
register_index("execution_alerts", [("status", 1), ("created_at", -1)])

def using_core_jobs(): return True

def job(kind, **kwargs):
    def register(fn):
        _REGISTRY[kind] = fn
        return _core_jobs.job(kind, **kwargs)(fn)
    return register

async def halted():
    doc = await db.settings.find_one({"key": "app"}) or {}
    return bool(doc.get("autopilot_halted"))

def reason(code, fi, en): return _core_ar.policy_reason(code, fi, en)

async def decide(action, *, create_approval=True, **kwargs):
    return await _core_ar.decide(AGENT, action, switch="autopilot_enabled",
        approval_kind="execution_review" if create_approval else None, create_approval=create_approval, **kwargs)

# ------------------------------------------------------------------------------------------------- alerts

async def raise_alert(code: str, key: str, title_fi: str, title_en: str, body_fi: str = "", body_en: str = "", *,
                      severity: str = "medium", subject: tuple[str, str] | None = None, link: str | None = None,
                      company_id: str | None = None) -> dict | None:
    """Open an execution alert once per key (in-app to ops + e-mail when OWNER_ALERT_EMAIL is set)."""
    doc = {"id": new_id(), "code": code, "key": key, "severity": severity, "status": "open", "title_fi": title_fi,
           "title_en": title_en, "body_fi": body_fi, "body_en": body_en, "subject_type": (subject or (None, None))[0],
           "subject_id": (subject or (None, None))[1], "link": link, "company_id": company_id, "created_at": now_iso()}
    try:
        await db.execution_alerts.insert_one(dict(doc))
    except DuplicateKeyError:
        return None
    from core.notify import bi, notify_role

    try:
        await notify_role(["ops_admin", "ops_agent"], bi(title_fi, title_en), bi(body_fi, body_en), link=link, kind="alert")
    except Exception:  # noqa: BLE001
        log.exception("alert notification failed")
    owner = os.environ.get("OWNER_ALERT_EMAIL", "").strip()
    if owner:
        from core.email import render, send_email

        html, text = render(title_fi, [body_fi, title_en, body_en])
        await send_email(owner, f"[Huolitsija] {title_fi}", html, text, tag="execution_alert", company_id=company_id)
    await emit("alert.opened", {"alert": doc}, background=True)
    return doc


async def resolve_alerts(key_prefix: str):
    await db.execution_alerts.update_many({"key": {"$regex": f"^{key_prefix}"}, "status": "open"},
                                          {"$set": {"status": "resolved", "resolved_at": now_iso()}})


# ------------------------------------------------------------------------------------------------- signed links

def sign_link(purpose: str, data: dict, days: int = 14) -> str:
    return jwt.encode({"p": purpose, "d": data, "exp": now() + timedelta(days=days)}, security._secret, algorithm="HS256")


def read_link(purpose: str, token: str) -> dict | None:
    try:
        payload = jwt.decode(token, security._secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return payload.get("d") if payload.get("p") == purpose else None


async def maybe_await(x):
    return await x if inspect.isawaitable(x) else x
