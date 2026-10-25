"""Recurring compliance work on s2's durable jobs when they are deployed, else on a core.lifecycle loop.

    from core.compliance_jobs import recurring

    @recurring("compliance.retention_sweep", daily_at="03:30", label_fi="Säilytysajat", label_en="Retention sweep")
    async def retention_sweep(j: dict | None = None) -> dict: ...

With core.jobs present the function becomes `@job(name, agent=True)` plus `schedule(name, job=name, ...)()`, so the
kill switch holds it and failures go to dead letters. Without core.jobs it runs in a background loop; a daily job
records its last run in `compliance_runs` so it fires once a day across restarts and instances.
`enqueue_or_run(name, payload, key)` enqueues a job (s2) or runs the function at once in the background.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core.db import db
from core.lifecycle import background_loop
from core.util import now_iso

log = logging.getLogger("compliance.jobs")
HEL = ZoneInfo("Europe/Helsinki")
_FUNCS: dict = {}
_bg: set = set()

try:  # s2 feat/autopilot-ops
    from core import jobs as _jobs  # type: ignore
except ImportError:  # pragma: no cover - depends on merge order
    _jobs = None


def has_durable_jobs() -> bool:
    return _jobs is not None


async def autopilot_halted() -> bool:
    doc = await db.settings.find_one({"key": "app"}, {"_id": 0, "autopilot_halted": 1}) or {}
    return bool(doc.get("autopilot_halted"))


def recurring(name: str, *, every_seconds: int | None = None, daily_at: str | None = None, agent: bool = True,
              label_fi: str = "", label_en: str = "", timeout: int = 1800):
    if not (every_seconds or daily_at):
        raise ValueError("every_seconds or daily_at is required")

    def deco(fn):
        _FUNCS[name] = fn
        if _jobs is not None:
            _jobs.job(name, max_attempts=3, timeout=timeout, agent=agent, label_fi=label_fi, label_en=label_en)(fn)
            kw = {"daily_at": daily_at} if daily_at else {"every_seconds": every_seconds}
            _jobs.schedule(name, job=name, label_fi=label_fi, label_en=label_en, **kw)()
            return fn

        async def tick():
            if agent and await autopilot_halted():
                return
            if daily_at:
                local = datetime.now(HEL)
                hh, mm = (int(x) for x in daily_at.split(":"))
                if (local.hour, local.minute) < (hh, mm):
                    return
                day = local.date().isoformat()
                got = await db.compliance_runs.find_one_and_update(
                    {"name": name, "day": {"$ne": day}}, {"$set": {"name": name, "day": day, "started_at": now_iso()}},
                    upsert=False)
                if got is None:
                    if await db.compliance_runs.find_one({"name": name}):
                        return  # already ran today (or another instance claimed it)
                    try:
                        await db.compliance_runs.insert_one({"name": name, "day": day, "started_at": now_iso()})
                    except Exception:  # noqa: BLE001 - duplicate insert by another instance
                        return
            res = await fn({"kind": name, "payload": {}, "source": "loop"})
            await db.compliance_runs.update_one({"name": name}, {"$set": {"finished_at": now_iso(),
                                                                        "result": res if isinstance(res, dict) else None}},
                                                upsert=True)

        background_loop(seconds=60 if daily_at else every_seconds)(tick)
        return fn

    return deco


async def enqueue_or_run(name: str, payload: dict | None = None, key: str | None = None, *, fn=None):
    """One-off work: a durable job when core.jobs exists (needs @job registered under `name`), else a background task."""
    if _jobs is not None and name in _jobs.registered_jobs():
        return await _jobs.enqueue(name, payload or {}, key=key, source="event")
    target = fn or _FUNCS.get(name)
    if target is None:
        raise ValueError(f"unknown compliance job {name}")
    task = asyncio.create_task(target({"kind": name, "payload": payload or {}, "source": "inline"}))
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return {"kind": name, "status": "running_inline", "key": key}
