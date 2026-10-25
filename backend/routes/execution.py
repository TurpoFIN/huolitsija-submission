"""Execution API (s9). Each area keeps its routes in routes/execution_<area>.py; this module only collects them."""
import importlib

from fastapi import APIRouter, Depends, HTTPException
from core import config

import services.execution  # noqa: F401  (registers jobs, schedules and event handlers)
from core.deps import require_perm
from services.execution import runtime

router = APIRouter(prefix="/api/execution", tags=["execution"])


@router.get("/runtime")
async def execution_runtime(user: dict = Depends(require_perm("shipments.read_all"))):
    """Which job system and agent runtime execution runs on, and whether the mailboxes are configured (no secrets)."""
    from services.execution import mail

    return {"jobs": "core.jobs" if runtime.using_core_jobs() else "execution_jobs (local fallback)",
            "agent_runtime": "core.agent_runtime" if runtime._core_ar is not None else "local decide()",
            "halted": await runtime.halted(), "smtp_configured": bool(mail.smtp_conf()["host"]),
            "imap_configured": bool(mail.imap_conf()["host"]), "sandbox_domain": mail.config.env("EXECUTION_SANDBOX_DOMAIN") or None}


AREAS = ["routes.execution_core", "routes.execution_slots", "routes.execution_consolidation", "routes.execution_warehouse"]
routers = [router]
for _name in AREAS:
    try:
        _mod = importlib.import_module(_name)
    except ModuleNotFoundError as exc:
        if exc.name != _name:
            raise
        continue
    routers.extend(getattr(_mod, "routers", None) or [_mod.router])


# Inline job runner for tests and the e2e script (ops admins only; does nothing the background worker would not do).
@router.post("/test/work")
async def execution_work(body: dict, user: dict = Depends(require_perm("shipments.write", "settings.write"))):
    if not config.env_bool("EXECUTION_TEST_API_ENABLED"):
        raise HTTPException(404, "Not found")
    kinds = body.get("kinds") or list(runtime._REGISTRY)
    if any(k not in runtime._REGISTRY for k in kinds):
        raise HTTPException(422, "Only execution jobs are allowed")
    if body.get("fire"):  # enqueue a fresh run of each listed kind (scheduled kinds: don't wait for the next slot)
        from core.util import new_id

        for k in kinds or []:
            await runtime.enqueue(k, {"fired_by": user["id"]}, key=f"fire:{k}:{new_id()}", source="api", priority=5)
    total = 0
    for _ in range(min(10, max(1, int(body.get("rounds") or 1)))):
        total += await runtime.work_once(f"api:{user['id']}", 50, kinds=kinds)
    return {"ran": total}
