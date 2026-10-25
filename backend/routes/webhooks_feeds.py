"""Inbound tracking feeds. A carrier (or a tracking aggregator) pushes events here; each is stored durably, answered
202 at once and applied by the tracking.ingest job (retries, dead letters). Header X-Huolitsija-Signature: hex
HMAC-SHA256 of the raw body with TRACKING_WEBHOOK_SECRET. Body: one event or {"events": [...]}, shape in
services.agents.tracking_agent."""
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core import config
from core.db import db
from core.deps import require_internal
from services.agents.tracking_agent import store_feed_event

router = APIRouter(prefix="/api", tags=["webhooks"])
MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 500
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")


@router.post("/webhooks/tracking/{provider}", status_code=202)
async def tracking_webhook(provider: str, request: Request):
    if not PROVIDER_RE.match(provider) or provider == "simulator":
        raise HTTPException(404, "Unknown feed")
    secret = config.env("TRACKING_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "The tracking webhook is not configured (TRACKING_WEBHOOK_SECRET is not set)")
    body = await request.body()
    if len(body) > MAX_BYTES:
        raise HTTPException(413, "The body is larger than 2 MB")
    given = (request.headers.get("x-huolitsija-signature") or "").strip().lower()
    if given.startswith("sha256="):
        given = given[7:]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "Missing or invalid X-Huolitsija-Signature")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "The body is not JSON")
    events = data.get("events") if isinstance(data, dict) and "events" in data else [data]
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise HTTPException(422, f"Send one event or 1-{MAX_EVENTS} events in 'events'")
    accepted = duplicates = 0
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("event_id") or not ev.get("code"):
            raise HTTPException(422, "Every event needs event_id and code")
        if not any(ev.get(k) for k in ("shipment_ref", "transport_document_number", "container_number")):
            raise HTTPException(422, "Every event needs shipment_ref, transport_document_number or container_number")
        ev.pop("shipment_id", None)  # outside feeds address shipments by their public identifiers only
        _, new = await store_feed_event(provider, ev, verified=True)
        accepted += new
        duplicates += not new
    return {"ok": True, "accepted": accepted, "duplicates": duplicates}


@router.get("/autopilot/feed-events")
async def feed_events(status: str | None = Query(None), provider: str | None = Query(None), limit: int = Query(100, le=500),
                      user: dict = Depends(require_internal)):
    q = {}
    if status:
        q["status"] = status
    if provider:
        q["provider"] = provider
    items = await db.raw.feed_events.find(q, {"_id": 0}).sort("received_at", -1).limit(limit).to_list(limit)
    return {"items": items}
