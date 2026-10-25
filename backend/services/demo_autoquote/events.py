"""Run log of the autonomous quote demo. Every step the agent takes writes one event (demo_run_events) and one
audit_log row at the moment it happens; the flow map is drawn from these events, live over SSE, never from a script.

Event: {id, run_id, seq, at, t (seconds since the run started), sim_at (run clock), kind, node, state, edge {from, to}, title, data,
simulated}. node is one of NODES or "p:<outreach id>" for a partner; state is active | done | waiting | idle | failed."""
from __future__ import annotations

import asyncio
import logging

from pymongo import ReturnDocument

from core.audit import agent_actor, audit
from core.db import db, register_index
from core.util import iso, new_id, now, now_iso, parse_iso, real_now

from .clock import sim_now

log = logging.getLogger("demo_autoquote")
register_index("demo_runs", "id", unique=True)
register_index("demo_runs", [("created_at", -1)])
register_index("demo_run_events", [("run_id", 1), ("seq", 1)], unique=True)

NODES = ("requester", "intake", "clarify", "research", "outreach", "replies", "pricing", "offer")
ACTOR = agent_actor("autonomous_quote")
_subs: dict[str, set[asyncio.Queue]] = {}


def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subs.setdefault(run_id, set()).add(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    _subs.get(run_id, set()).discard(q)


async def emit(run_id: str, node: str, title: str, *, state: str | None = None, edge: tuple[str, str] | None = None,
               kind: str = "step", data: dict | None = None, simulated: bool = False) -> dict:
    # Provenance follows the actor at the serialization boundary, including
    # outbound native requests addressed to a world actor and simulated RFQs.
    # Keep caller data immutable and keep real-delivery evidence independent.
    data = dict(data or {})
    actor_id = (data.get('actor') or {}).get('id')
    world_nodes = [value for value in (node, *(edge or ()), actor_id) if isinstance(value,str) and value.startswith("w:")]
    if world_nodes or kind == "world" or simulated:
        actor = dict(data.get("actor") or {})
        if world_nodes:
            actor.setdefault("id", world_nodes[0])
        actor["source"] = "world"
        data["actor"] = actor
    run = await db.demo_runs.find_one_and_update({"id": run_id}, {"$inc": {"event_seq": 1}, "$set": {"updated_at": now_iso()}},
                                                 projection={"_id": 0, "event_seq": 1, "created_at": 1, "created_real": 1, "clock": 1},
                                                 return_document=ReturnDocument.AFTER)
    if not run:
        raise ValueError(f"unknown demo run {run_id}")
    # at and t are wall time (the recording's timeline); sim_at is the demo clock, which is also Huolitsija's clock
    started = parse_iso(run.get("created_real") or run.get("created_at")) or real_now()
    ev = {"id": new_id(), "run_id": run_id, "seq": run["event_seq"], "at": iso(real_now()),
          "t": round((real_now() - started).total_seconds(), 1), "sim_at": iso(sim_now(run)), "kind": kind, "node": node, "state": state,
          "edge": {"from": edge[0], "to": edge[1]} if edge else None, "title": title, "data": data or {},
          "simulated": bool(simulated)}
    await db.demo_run_events.insert_one(dict(ev))
    await audit(ACTOR, f"demo_autoquote_{kind}", "demo_run", run_id,
                {"node": node, "title": title, "state": state, "edge": ev["edge"], "simulated": ev["simulated"]})
    for q in list(_subs.get(run_id, ())):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            log.warning("demo run %s: a slow stream subscriber dropped an event", run_id)
    return ev


async def history(run_id: str, after_seq: int = 0) -> list[dict]:
    return await db.demo_run_events.find({"run_id": run_id, "seq": {"$gt": after_seq}}, {"_id": 0}).sort("seq", 1).to_list(5000)
