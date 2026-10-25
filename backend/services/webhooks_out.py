"""Outbound webhooks for shippers: signed HTTP callbacks when their quotes, shipments and invoices change.

A company registers endpoints (portal or `POST /api/v1/webhooks`) for some or all of EVENTS. Domain events enqueue one
row per endpoint in `webhook_deliveries`; a background loop POSTs them and retries on failure:

    POST <endpoint url>
    Content-Type: application/json
    Huolitsija-Event: shipment.status_changed
    Huolitsija-Delivery: <delivery id>
    Huolitsija-Signature: t=1761300000,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

    {"id": "<event id>", "type": "shipment.status_changed", "created_at": "...", "data": {...}}

Receivers verify the signature with the endpoint secret (shown once at creation) and reject a `t` older than
5 minutes. Any 2xx is delivered; anything else retries after 1 min, 5 min, 30 min, 2 h and 12 h, then the row is
`dead`. Payloads use the customer views (no buy prices or margins).

Endpoints must be public HTTPS URLs. WEBHOOKS_ALLOW_PRIVATE=true also allows http and private/loopback hosts
(local tests and on-premise receivers on a private network).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pymongo import ReturnDocument

from core import config
from core.db import db, register_index
from core.events import on
from core.lifecycle import background_loop
from core.util import iso, new_id, now, now_iso, span

log = logging.getLogger("webhooks_out")

register_index("webhook_endpoints", "id", unique=True)
register_index("webhook_endpoints", [("company_id", 1), ("active", 1)])
register_index("webhook_deliveries", "id", unique=True)
register_index("webhook_deliveries", [("status", 1), ("next_attempt_at", 1)])
register_index("webhook_deliveries", [("endpoint_id", 1), ("created_at", -1)])

EVENTS: dict[str, tuple[str, str]] = {
    "quote.created": ("Tarjous luotu", "A quote was created"),
    "quote.accepted": ("Tarjous varattu", "A quote was booked"),
    "shipment.created": ("Lähetys luotu", "A shipment was created"),
    "shipment.status_changed": ("Lähetyksen tila muuttui", "A shipment changed status"),
    "shipment.delivered": ("Lähetys toimitettu", "A shipment was delivered"),
    "invoice.created": ("Lasku lähetetty", "An invoice was issued to you"),
    "invoice.paid": ("Lasku maksettu", "An invoice was paid"),
}
BACKOFF_SECONDS = [60, 300, 1800, 7200, 43200]
MAX_ATTEMPTS = 5
TIMEOUT_SECONDS = 10
ALLOW_PRIVATE = config.env_bool("WEBHOOKS_ALLOW_PRIVATE", False)
ENDPOINT_FIELDS = {"_id": 0, "secret": 0}
DELIVERY_FIELDS = {"_id": 0, "payload": 0, "lease_until": 0}


# ------------------------------------------------------------------------------------------------ signing
def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    t = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """Receiver-side check (documented in the API docs; used by the tests)."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


# ------------------------------------------------------------------------------------------------ endpoints
def _check_url(url: str) -> str:
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise HTTPException(400, "Give a full URL, for example https://erp.example.com/huolitsija/webhook.")
    if ALLOW_PRIVATE:
        return url
    if u.scheme != "https":
        raise HTTPException(400, "Webhook URLs must use HTTPS.")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, u.port or 443)}
    except OSError:
        raise HTTPException(400, f"The host {u.hostname} could not be resolved.") from None
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(400, "Webhook URLs must point to a public address.")
    return url


def _clean_events(events: list[str] | None) -> list[str]:
    asked = list(dict.fromkeys(e.strip() for e in events or [] if e and e.strip()))
    if not asked or "*" in asked:
        return ["*"]
    unknown = [e for e in asked if e not in EVENTS]
    if unknown:
        raise HTTPException(400, f"Unknown event: {', '.join(unknown)}. Valid events: {', '.join(EVENTS)} or *.")
    return [e for e in EVENTS if e in asked]


async def create_endpoint(company_id: str, *, url: str, events: list[str] | None, description: str | None,
                          created_by: dict | None) -> tuple[dict, str]:
    if await db.webhook_endpoints.count_documents({"company_id": company_id, "active": True}) >= 10:
        raise HTTPException(400, "A company can have at most 10 active webhook endpoints.")
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    doc = {"id": new_id(), "company_id": company_id, "url": _check_url(url), "events": _clean_events(events),
           "description": (description or "").strip()[:200] or None, "secret": secret, "active": True,
           "created_by": {"id": (created_by or {}).get("id"), "name": (created_by or {}).get("name")},
           "created_at": now_iso(), "last_delivery_at": None, "last_status": None}
    await db.webhook_endpoints.insert_one(dict(doc))
    doc.pop("_id", None)
    doc.pop("secret", None)
    return doc, secret


async def list_endpoints(company_id: str) -> list[dict]:
    return await db.webhook_endpoints.find({"company_id": company_id, "active": True}, ENDPOINT_FIELDS) \
        .sort("created_at", -1).to_list(50)


async def get_endpoint(company_id: str, endpoint_id: str) -> dict:
    ep = await db.webhook_endpoints.find_one({"id": endpoint_id, "company_id": company_id, "active": True}, ENDPOINT_FIELDS)
    if not ep:
        raise HTTPException(404, "Webhook endpoint not found")
    return ep


async def delete_endpoint(company_id: str, endpoint_id: str) -> dict:
    ep = await db.webhook_endpoints.find_one_and_update(
        {"id": endpoint_id, "company_id": company_id, "active": True},
        {"$set": {"active": False, "deleted_at": now_iso()}}, projection=ENDPOINT_FIELDS, return_document=ReturnDocument.AFTER)
    if not ep:
        raise HTTPException(404, "Webhook endpoint not found")
    await db.webhook_deliveries.update_many({"endpoint_id": endpoint_id, "status": {"$in": ["pending", "failed"]}},
                                            {"$set": {"status": "cancelled", "updated_at": now_iso()}})
    return ep


async def list_deliveries(company_id: str, endpoint_id: str | None = None, limit: int = 50) -> list[dict]:
    q = {"company_id": company_id}
    if endpoint_id:
        q["endpoint_id"] = endpoint_id
    return await db.webhook_deliveries.find(q, DELIVERY_FIELDS).sort("created_at", -1).to_list(max(1, min(limit, 200)))


# ------------------------------------------------------------------------------------------------ enqueue
async def enqueue(company_id: str | None, event: str, data: dict, *, only_endpoint: str | None = None) -> list[dict]:
    """One delivery per active endpoint of the company that subscribed to `event`."""
    if not company_id:
        return []
    q: dict = {"company_id": company_id, "active": True}
    if only_endpoint:
        q["id"] = only_endpoint
    else:
        q["events"] = {"$in": [event, "*"]}
    out = []
    event_id = f"evt_{new_id().replace('-', '')}"
    async for ep in db.webhook_endpoints.find(q, {"_id": 0, "id": 1, "url": 1}):
        body = {"id": event_id, "type": event, "created_at": now_iso(), "data": data}
        d = {"id": new_id(), "endpoint_id": ep["id"], "company_id": company_id, "url": ep["url"], "event": event,
             "event_id": event_id, "payload": body, "status": "pending", "attempts": 0, "next_attempt_at": now_iso(),
             "response_code": None, "last_error": None, "created_at": now_iso(), "updated_at": now_iso(), "delivered_at": None}
        await db.webhook_deliveries.insert_one(dict(d))
        d.pop("_id", None)
        out.append(d)
    return out


def _shipment_data(s: dict) -> dict:
    from routes.shipments import summary
    return {"shipment": summary(s, internal=False)}


def _quote_data(q: dict) -> dict:
    from routes.quotes import summary
    out = summary(q, internal=False)
    if q.get("shipment_id"):
        out["shipment_id"] = q["shipment_id"]
        out["shipment_reference"] = q.get("shipment_reference")
    return {"quote": out}


def _invoice_data(inv: dict) -> dict:
    from services.billing import public_invoice
    out = public_invoice(inv, internal=False)
    out.pop("lines", None)  # GET /api/v1/invoices/{id} has the lines
    out.pop("pdf_file_id", None)
    return {"invoice": out}


@on("shipment.created")
async def _on_shipment_created(p: dict):
    s = p.get("shipment") or {}
    await enqueue(s.get("company_id"), "shipment.created", _shipment_data(s))


@on("shipment.status_changed")
async def _on_shipment_status(p: dict):
    s = p.get("shipment") or {}
    data = _shipment_data(s)
    data.update(old_status=p.get("old_status"), new_status=p.get("new_status"))
    await enqueue(s.get("company_id"), "shipment.status_changed", data)


@on("shipment.delivered")
async def _on_shipment_delivered(p: dict):
    s = p.get("shipment") or {}
    await enqueue(s.get("company_id"), "shipment.delivered", _shipment_data(s))


@on("quote.created")
async def _on_quote_created(p: dict):
    q = p.get("quote") or {}
    if q.get("status") in ("sent", "pending_approval"):  # ops drafts are not the customer's yet
        await enqueue(q.get("company_id"), "quote.created", _quote_data(q))


@on("quote.accepted")
async def _on_quote_accepted(p: dict):
    q = p.get("quote") or {}
    await enqueue(q.get("company_id"), "quote.accepted", _quote_data(q))


@on("invoice.issued")  # the customer never sees drafts, so "created" for them is the moment of issue
async def _on_invoice_issued(p: dict):
    inv = p.get("invoice") or {}
    await enqueue(inv.get("company_id"), "invoice.created", _invoice_data(inv))


@on("invoice.paid")
async def _on_invoice_paid(p: dict):
    inv = p.get("invoice") or {}
    await enqueue(inv.get("company_id"), "invoice.paid", _invoice_data(inv))


# ------------------------------------------------------------------------------------------------ delivery
async def _attempt(d: dict) -> dict:
    ep = await db.webhook_endpoints.find_one({"id": d["endpoint_id"]}, {"_id": 0})
    if not ep or not ep.get("active"):
        await db.webhook_deliveries.update_one({"id": d["id"]}, {"$set": {"status": "cancelled", "updated_at": now_iso()}})
        return {**d, "status": "cancelled"}
    body = json.dumps(d["payload"], separators=(",", ":"), ensure_ascii=False, default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Huolitsija-Webhooks/1.0",
               "Huolitsija-Event": d["event"], "Huolitsija-Delivery": d["id"], "Huolitsija-Signature": sign(ep["secret"], body)}
    attempts = int(d.get("attempts") or 0) + 1
    code, err = None, None
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            r = await client.post(ep["url"], content=body, headers=headers)
        code = r.status_code
        if not 200 <= code < 300:
            err = f"HTTP {code}: {r.text[:300]}"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:300]
    upd = {"attempts": attempts, "response_code": code, "last_error": err, "updated_at": now_iso(), "lease_until": None,
           "duration_ms": int((time.monotonic() - started) * 1000)}
    if err is None:
        upd.update(status="delivered", delivered_at=now_iso(), next_attempt_at=None)
    elif attempts >= MAX_ATTEMPTS:
        upd.update(status="dead", next_attempt_at=None)
    else:
        upd.update(status="failed", next_attempt_at=iso(now() + timedelta(seconds=BACKOFF_SECONDS[attempts - 1])))
    await db.webhook_deliveries.update_one({"id": d["id"]}, {"$set": upd})
    await db.webhook_endpoints.update_one({"id": ep["id"]}, {"$set": {"last_delivery_at": now_iso(), "last_status": upd["status"]}})
    if upd["status"] == "dead":
        log.warning("webhook delivery %s to %s is dead after %s attempts: %s", d["id"], ep["url"], attempts, err)
    out = {**d, **upd}
    out.pop("payload", None)
    out.pop("lease_until", None)
    return out


async def _claim(query: dict) -> dict | None:
    lease = iso(now() + span(TIMEOUT_SECONDS + 30))
    return await db.webhook_deliveries.find_one_and_update(
        query, {"$set": {"status": "sending", "lease_until": lease}}, projection={"_id": 0},
        sort=[("next_attempt_at", 1)], return_document=ReturnDocument.AFTER)


async def deliver_now(delivery_id: str) -> dict:
    d = await _claim({"id": delivery_id, "status": {"$in": ["pending", "failed"]}})
    if not d:
        return await db.webhook_deliveries.find_one({"id": delivery_id}, DELIVERY_FIELDS) or {}
    return await _attempt(d)


async def deliver_due(max_items: int = 25) -> int:
    n = 0
    t = now_iso()
    while n < max_items:
        d = await _claim({"$or": [{"status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": t}},
                                  {"status": "sending", "lease_until": {"$lt": t}}]})
        if not d:
            break
        await _attempt(d)
        n += 1
    return n


@background_loop(seconds=5)
async def webhooks_deliver():
    """Runs on core.lifecycle until s2's core.jobs is merged; then `webhooks.deliver` becomes a @schedule job."""
    await deliver_due()
