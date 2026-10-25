"""Capability-scoped public requester API. No list, clock, ops or world injection access."""
from __future__ import annotations

import asyncio
import re
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core import config
from core.db import db
from services.demo_autoquote import clock, conf, driver, events
from services.demo_autoquote import public_requester as admission
from services.demo_autoquote.payment import payment_state, simulate_payment
from routes import demo_autoquote as internal

router = APIRouter(prefix="/api/demo/public", tags=["public-quote"])


class PublicRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    text: str = Field(default="", max_length=3000)
    cargo: str = Field(default="", max_length=3000)
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(default="", max_length=120)
    company: str = Field(default="", max_length=180)
    locale: Literal["en", "fi"] = "en"


def _on():
    if not conf.real_requester_enabled():
        raise HTTPException(404, "Not found")


async def start_request(body: PublicRunIn, request: Request, *, run_id: str, token: str):
    _on()
    if not conf.real_requester_ready():
        raise HTTPException(503, "Please try again shortly. Our quote service is getting ready.")
    try:
        address = admission.email_address(body.email)
    except HTTPException as exc:
        raise HTTPException(422, [{"loc": ["body", "email"], "msg": exc.detail, "type": "value_error"}])
    run = await db.raw.demo_runs.find_one({"id": run_id}, {"_id": 0})
    if run and conf.requester_address(run) != address:
        raise HTTPException(409, "This request is already linked to its original email address.")
    if not run:
        run = await driver.start({**body.model_dump(), "text": body.text or body.cargo, "email": address, "real_requester": True,
        "run_id": run_id, "access_token_hash": admission.digest(token), "profile": {"contact_name": body.name, "company": body.company}}, None)
    track_url = f"{config.APP_BASE_URL}/track/{run['id']}?t={token}"
    if not run.get("acknowledged_at"):
        await acknowledge(run, track_url)
    return {"id": run["id"], "ref": run["ref"], "status": run["status"], "access_token": token,
            "email": address, "track_url": track_url}


async def acknowledge(run: dict, track_url: str):
    from services.email_channel import outbound
    from core.util import now_iso
    thread, _, _ = await driver._ensure_thread(run["id"])
    fi = run.get("locale") == "fi"
    sent = await outbound.send(thread, "Käsittelemme pyyntöäsi" if fi else "We're on it",
        ["Kiitos tarjouspyynnöstäsi. Käsittelemme pyyntöäsi ja jatkamme sähköpostitse." if fi else
         "Thank you for your quote request. We're on it and will continue by email."], tag="email_ack",
         action=(track_url, "Seuraa kuljetustasi" if fi else "Follow your shipment"))
    if sent.get("status") == "sent":
        await db.raw.demo_runs.update_one({"id": run["id"]}, {"$set": {"acknowledged_at": now_iso()}})


async def start_from_quote(q: dict, email: str, locale: str, request: Request):
    import json
    req = q.get("request") or {}
    def place(value):
        if isinstance(value, dict):
            return ", ".join(str(value[k]) for k in ("name", "country") if value.get(k)) or str(value.get("unlocode") or value.get("code") or "")
        return str(value or "")
    token = admission.digest(q["claim_token"] + ":public-demo:" + q["id"])
    body = PublicRunIn(origin=place(req.get("origin")), destination=place(req.get("destination")), email=email,
        text="Shipment facts supplied by requester: " + json.dumps(req, ensure_ascii=False)[:2900], locale=locale)
    return await start_request(body, request, run_id="public-" + q["id"], token=token)


async def _run(run_id: str, request: Request) -> dict:
    _on()
    run = await db.raw.demo_runs.find_one({"id": run_id, "real_requester": True}, {"_id": 0})
    if not run:
        raise HTTPException(404, "Request not found")
    token = request.headers.get("x-demo-token") or request.query_params.get("access_token") or ""
    admission.check_token(run, token)
    return run


def _redact(value, address: str):
    if isinstance(value, dict):
        return {k: _redact(v, address) for k, v in value.items() if k not in {
            "recipients", "access_token_hash", "rfq_mode", "rfq_day", "created_by", "requester_registry"}}
    if isinstance(value, list):
        return [_redact(v, address) for v in value]
    if isinstance(value, str):
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", lambda m: m[0] if m[0].lower() == address else "[private address]", value)
        value = re.sub(r"(/api/email-channel/(?:go|pay)/)[A-Za-z0-9._-]+", r"\1[private link]", value)
    return value


def public_event(ev: dict, run: dict) -> dict | None:
    if "rfq_mode" in (ev.get("data") or {}):
        return None
    return _redact(ev, conf.requester_address(run))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request, events_: bool = Query(True, alias="events")):
    run = await _run(run_id, request)
    projected = {k: run[k] for k in ("id", "ref", "status", "phase", "created_at", "updated_at", "created_real",
        "origin_text", "destination_text", "locale", "requester", "quote_id", "quote_number", "shipment_id",
        "shipment_reference", "clock", "offer", "end_reason", "event_seq", "awaiting") if k in run}
    projected["payment"] = await payment_state(run)
    out = {"run": _redact(projected, conf.requester_address(run)), "sim_now": clock.sim_now(run).isoformat()}
    if events_:
        out["events"] = [item for ev in await events.history(run_id) if (item := public_event(ev, run)) is not None]
    return out


@router.get("/runs/{run_id}/events")
async def get_events(run_id: str, request: Request, after: int = 0):
    run = await _run(run_id, request)
    rows = await events.history(run_id, after)
    items = [item for ev in rows if (item := public_event(ev, run)) is not None]
    return {"items": items, "total": len(items), "last_seq": rows[-1]["seq"] if rows else after}


@router.get("/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, after: int = 0):
    from fastapi.responses import StreamingResponse
    run = await _run(run_id, request)
    try:
        after = max(after, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        pass
    q = events.subscribe(run_id)

    async def gen():
        last = after
        try:
            yield "retry: 3000\n\n"
            for ev in await events.history(run_id, last):
                last = ev["seq"]
                if (item := public_event(ev, run)) is not None:
                    yield internal._sse(item)
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if ev["seq"] > last:
                    last = ev["seq"]
                    if (item := public_event(ev, run)) is not None:
                        yield internal._sse(item)
        finally:
            events.unsubscribe(run_id, q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no", "Referrer-Policy": "no-referrer"})


@router.get("/runs/{run_id}/files/{file_id}")
async def run_file(run_id: str, file_id: str, request: Request):
    await _run(run_id, request)
    return await internal.run_file(run_id, file_id, user={})


class PaymentIn(BaseModel):
    invoice_id: str | None = Field(default=None, max_length=100)


@router.post("/runs/{run_id}/simulate-payment")
async def pay(run_id: str, request: Request, body: PaymentIn | None = None):
    return await simulate_payment(await _run(run_id, request), body.invoice_id if body else None)
