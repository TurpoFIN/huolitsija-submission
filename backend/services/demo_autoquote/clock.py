"""Per-run clock anchors; Huolitsija follows the one active run through the boundary.

reset_now(run_id) starts a quote at its own wall time and refuses while another
lifecycle is live. Switching owners freezes the previous run's stored clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.db import db
from core.util import iso
from services.demo_world import boundary

DEFAULT_SPEED = 1440.0
MAX_SPEED = 86400.0


def initial(speed: float = 1.0) -> dict:
    t = iso(boundary._wall())
    return {"speed": float(speed), "anchor_real": iso(boundary._wall()), "anchor_sim": t, "paused": False}


def sim_now(run: dict | None = None) -> datetime:
    if run is None:
        return boundary.demo_now()
    c = run.get('clock') or {}
    run_id = run.get('id') or c.get('run_id')
    if run_id and run_id == boundary.clock_state().get('run_id'):
        return boundary.demo_now()
    if not c.get('anchor_sim'):
        return boundary._wall()
    if c.get('active') is False:
        return boundary._parse(c.get('last_sim') or c['anchor_sim'])
    anchor = boundary._parse(c['anchor_sim'])
    if c.get('paused') or c.get('hold'):
        return anchor
    return anchor + timedelta(seconds=(boundary._wall()-boundary._parse(c['anchor_real'])).total_seconds()*float(c.get('speed') or 1))


async def reset_now(run_id: str) -> dict:
    await boundary.reset_clock(run_id)
    return (await db.demo_runs.find_one({'id': run_id}, {'_id': 0, 'clock': 1}))['clock']


async def set_clock(run_id: str, *, speed: float | None = None, paused: bool | None = None) -> dict:
    run = await db.demo_runs.find_one({"id": run_id}, {"_id": 0, "id": 1})
    if run is None:
        raise ValueError(f"unknown demo run {run_id}")
    await boundary.set_clock(run_id=run_id, speed=None if speed is None else min(float(speed), MAX_SPEED), paused=paused)
    return (await db.demo_runs.find_one({'id': run_id}, {'_id': 0, 'clock': 1}))['clock']


async def sleep_sim(run_id: str, sim_seconds: float, *, tick: float = 0.5) -> None:
    """Wait until `sim_seconds` of demo time have passed (follows speed changes and pauses); ends early when the run
    is stopped or failed."""
    async def stopped() -> bool:
        run = await db.demo_runs.find_one({"id": run_id}, {"_id": 0, "status": 1}) or {}
        return run.get("status") in ("stopped", "failed")
    await boundary.sleep_demo(sim_seconds, tick=tick, stop=stopped)
