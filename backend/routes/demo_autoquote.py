"""Autonomous quote demo API (internal users only; off unless DEMO_AUTOQUOTE_ENABLED=true).

POST /api/demo/runs                      start a hands-off run {origin, destination, text, profile{...}, seed?}
GET  /api/demo/runs                      latest runs
GET  /api/demo/runs/{id}                 run + every event so far (?events=false: the run record only)
GET  /api/demo/runs/{id}/events          JSON event history, ?after=<seq>
GET  /api/demo/runs/{id}/stream          the run's events as SSE (history first, then live); resumes after
                                         ?after=<seq> or the Last-Event-ID header (= seq)
POST /api/demo/runs/{id}/stop            stop a running run
POST /api/demo/runs/{id}/clock           {speed?, paused?} simulation clock
GET  /api/demo/runs/{id}/files/{file_id} a document produced in the run
GET  /api/demo/runs/{id}/requester-inbox what the requester sandbox mailbox received in this run
GET  /api/demo/random-lane               a random city pair: Finland and a city outside the EU customs union
"""
from __future__ import annotations

import asyncio
import json
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from core.db import db
from core.deps import require_internal
from core.util import now_iso
from services.demo_autoquote import clock, conf, driver, events
from services.demo_autoquote.mail import requester_box

router = APIRouter(prefix="/api/demo", tags=["demo"])
HEARTBEAT_S = 15
# EU customs union members (and Monaco); the random lane takes its far end outside these.
EU_CU = set("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK MC".split())
NOT_SERVED = set("RU BY IR KP SY CU VE AF MM UA IL PS LB YE SD SS LY IQ SO ER".split())


def _on():
    if not conf.enabled():
        raise HTTPException(404, "Not found")


class RunIn(BaseModel):
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    text: str = Field(default="", max_length=3000)
    profile: dict = Field(default_factory=dict)
    seed: int | None = None
    reuse_rfqs_from: str | None = Field(default=None, max_length=64)  # same shipment again: no partner is asked twice


@router.post("/runs")
async def start_run(body: RunIn, user: dict = Depends(require_internal)):
    _on()
    profile = {k: str(v)[:400] for k, v in (body.profile or {}).items() if v not in (None, "")}
    run = await driver.start({**body.model_dump(), "profile": profile}, user)
    return run


@router.get("/runs")
async def list_runs(user: dict = Depends(require_internal)):
    _on()
    items = await db.demo_runs.find({}, {"_id": 0, "id": 1, "ref": 1, "status": 1, "phase": 1, "created_at": 1, "origin_text": 1,
                                         "destination_text": 1, "quote_number": 1, "offer.sell_cents": 1, "seed": 1}
                                    ).sort("created_at", -1).to_list(30)
    return {"items": items, "total": len(items)}


async def _get(run_id: str) -> dict:
    run = await db.demo_runs.find_one({"id": run_id}, {"_id": 0})
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: str, events_: bool = Query(True, alias="events"), user: dict = Depends(require_internal)):
    _on()
    run = await _get(run_id)
    run.pop("recipients", None)
    run.pop("access_token_hash", None)
    from services.demo_autoquote.payment import payment_state
    run["payment"] = await payment_state(run)
    out = {"run": run, "sim_now": clock.sim_now(run).isoformat()}
    if events_:
        out["events"] = await events.history(run_id)
    return out


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0, user: dict = Depends(require_internal)):
    _on()
    await _get(run_id)
    items = await events.history(run_id, after)
    return {"items": items, "total": len(items), "last_seq": items[-1]["seq"] if items else after}


@router.get("/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, after: int = 0, user: dict = Depends(require_internal)):
    _on()
    await _get(run_id)
    try:
        after = max(after, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        pass
    q = events.subscribe(run_id)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            last = after
            for ev in await events.history(run_id, after):
                last = ev["seq"]
                yield _sse(ev)
            loop = asyncio.get_running_loop()
            sent = loop.time()
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=max(0.1, HEARTBEAT_S - (loop.time() - sent)))
                except asyncio.TimeoutError:
                    yield f": heartbeat {now_iso()}\n\n"
                    sent = loop.time()
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield _sse(ev)
                sent = loop.time()
        finally:
            events.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _sse(ev: dict) -> str:
    return f"id: {ev['seq']}\nevent: step\ndata: {json.dumps(ev, separators=(',', ':'), default=str)}\n\n"


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, user: dict = Depends(require_internal)):
    _on()
    await _get(run_id)
    await driver.stop(run_id)
    await events.emit(run_id, "intake", "Run stopped by ops", kind="phase", state="done")
    return {"ok": True}


class ClockIn(BaseModel):
    speed: float | None = Field(default=None, gt=0, le=clock.MAX_SPEED)
    paused: bool | None = None


class PaymentIn(BaseModel):
    invoice_id: str | None = Field(default=None, max_length=100)


@router.post("/runs/{run_id}/simulate-payment")
async def simulate_run_payment(run_id: str, body: PaymentIn | None = None, user: dict = Depends(require_internal)):
    _on()
    from services.demo_autoquote.payment import simulate_payment
    return await simulate_payment(await _get(run_id), body.invoice_id if body else None)


@router.post("/runs/{run_id}/clock")
async def set_clock(run_id: str, body: ClockIn, user: dict = Depends(require_internal)):
    _on()
    await _get(run_id)
    c = await clock.set_clock(run_id, speed=body.speed, paused=body.paused)
    label = "paused" if c.get("paused") else f"speed {c['speed']:g}×"
    await events.emit(run_id, "clock", f"Clock {label}", kind="clock", data={"clock": c})
    return {"clock": c}


@router.get("/runs/{run_id}/files/{file_id}")
async def run_file(run_id: str, file_id: str, user: dict = Depends(require_internal)):
    _on()
    run = await _get(run_id)
    known = (run.get("offer") or {}).get("file_id") == file_id or await db.demo_run_events.find_one(
        {"run_id": run_id, "data.documents.file_id": file_id})
    if not known:
        raise HTTPException(404, "File not found in this run")
    from core.storage import get_file
    data, name, ctype = await get_file(file_id)
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.get("/runs/{run_id}/requester-inbox")
async def requester_inbox(run_id: str, user: dict = Depends(require_internal)):
    _on()
    run = await _get(run_id)
    box = requester_box()
    if box is None:
        return {"address": None, "items": []}
    since = run["created_at"]
    items = []
    for m in await box.messages():
        if (m.get("createdAt") or "")[:19] < since[:19]:
            continue
        items.append({"id": m["id"], "from": (m.get("from") or {}).get("address"), "subject": m.get("subject"),
                      "intro": m.get("intro"), "at": m.get("createdAt"), "seen": m.get("seen"),
                      "attachments": len(m.get("attachments") or []) if isinstance(m.get("attachments"), list) else m.get("hasAttachments")})
    return {"address": conf.requester_address(), "items": [i for i in items if run["ref"] in (i.get("subject") or "")]}


@router.get("/random-lane")
async def random_lane(seed: int | None = None, user: dict = Depends(require_internal)):
    _on()
    from services import gazetteer
    p = gazetteer._places()
    rows = p["rows"][: p["n_cities"]]
    fi = [r for r in rows if r.get("country") == "FI" and (r.get("population") or 0) >= 50000]
    far = [r for r in rows if r.get("country") not in EU_CU | NOT_SERVED and (r.get("population") or 0) >= 1000000]
    if not fi or not far:
        raise HTTPException(503, "City data is still loading; try again in a minute")
    rnd = random.Random(seed)
    a, b = rnd.choice(fi), rnd.choice(far)
    pair = [a, b] if rnd.random() < 0.5 else [b, a]

    def label(r):
        return f"{r.get('name')}, {gazetteer.country_name(r.get('country')) or r.get('country')}"

    return {"origin": label(pair[0]), "destination": label(pair[1]),
            "direction": "export" if pair[0] is a else "import"}
