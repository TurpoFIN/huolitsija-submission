"""Demo-only MailSlurp transport and durable same-inbox reply polling.

API contract: https://api.mailslurp.com/v2/api-docs (checked 2026-10-25).
No account creation or production credentials. Polling uses the existing leased
job worker; raw MIME goes through the ordinary idempotent inbound dispatcher.
"""
from __future__ import annotations

import base64
from email.utils import parseaddr

import httpx

from core import config
from core.db import db
from core.jobs import job, schedule
from core.util import now_iso
from services.demo_autoquote import conf

API = "https://api.mailslurp.com"


def configured() -> bool:
    return conf.inbound_provider() == "mailslurp" and conf.real_requester_ready()


def auth() -> dict:
    return {"x-api-key": config.env("HUOLITSIJA_DEMO_MAILSLURP_API_KEY")}


async def inbox(client: httpx.AsyncClient) -> tuple[str, str]:
    ident = config.env("DEMO_MAILSLURP_INBOX_ID")
    response = await client.get(f"{API}/inboxes/{ident}", headers=auth())
    response.raise_for_status()
    address = parseaddr(response.json().get("emailAddress") or "")[1].lower()
    if not address or address != conf.inbound_address():
        raise ValueError("Demo MailSlurp inbox does not match DEMO_INBOUND_ADDRESS")
    return ident, address


async def send(client: httpx.AsyncClient, *, to: str, subject: str, html: str, text: str,
               headers: dict, attachments: list) -> dict:
    """Called only AFTER core.email's requester lineage and capture boundary."""
    ident, address = await inbox(client)
    uploaded = []
    for filename, content_type, content in attachments:
        response = await client.post(f"{API}/attachments", headers=auth(), json={
            "filename": filename, "contentType": content_type,
            "base64Contents": base64.b64encode(content).decode()})
        response.raise_for_status()
        uploaded.extend(response.json())
    response = await client.post(f"{API}/inboxes/{ident}/confirm", headers=auth(), json={
        "from": address, "to": [to], "replyTo": address, "subject": subject,
        "body": html or text, "isHTML": bool(html), "customHeaders": headers,
        "bodyParts": [{"contentType": "text/plain", "contentBody": text},
                      {"contentType": "text/html", "contentBody": html}] if html else [],
        **({"attachments": uploaded} if uploaded else {})})
    response.raise_for_status()
    data = response.json()
    # The provider may replace our requested ID. Keep its RFC header IDs, never
    # confuse the opaque sent-email entity UUID with a wire Message-ID.
    ids = [f"<{str(value).strip('<>')}>" for value in
           [data.get("messageId"), *(data.get("messageIds") or [])] if value]
    return {"status": "sent", "provider_id": data["id"], "provider_response": data,
            "message_id": ids[0] if ids else headers.get("Message-ID"),
            "message_ids": list(dict.fromkeys([headers.get("Message-ID"), *ids])),
            "from": address, "reply_to": address}


async def poll_once(client: httpx.AsyncClient | None = None, limit: int = 30) -> dict:
    if not configured():
        return {"status": "off"}
    from .dispatch import dispatch_raw
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    out = {"status": "ok", "fetched": 0, "errors": []}
    try:
        ident, _ = await inbox(client)
        response = await client.get(f"{API}/inboxes/{ident}/emails", headers=auth(),
            params={"unreadOnly": "true", "sort": "ASC", "limit": min(limit, 100), "retryTimeout": 1, "minCount": 0})
        response.raise_for_status()
        for message in response.json()[:limit]:
            email_id = message["id"]
            try:
                raw = await client.get(f"{API}/emails/{email_id}/raw", headers=auth())
                raw.raise_for_status()
                await dispatch_raw(raw.content, provider_id="mailslurp:" + email_id,
                                   provider_authenticated=True, source="mailslurp")
                marked = await client.patch(f"{API}/emails/{email_id}/read", headers=auth(), params={"read": "true"})
                marked.raise_for_status()
                out["fetched"] += 1
            except Exception as exc:  # Keep failed processing retryable, even if raw fetch marked read.
                await client.patch(f"{API}/emails/{email_id}/read", headers=auth(), params={"read": "false"})
                out["errors"].append({"id": email_id, "error": type(exc).__name__})
    except Exception as exc:
        out.update(status="error", error=type(exc).__name__)
    finally:
        if own:
            await client.aclose()
    await db.system.update_one({"key": "email:mailslurp_last_poll"}, {"$set": {
        "key": "email:mailslurp_last_poll", "at": now_iso(), **out}}, upsert=True)
    return out


@job("email.poll_mailslurp", label_fi="Saapuvat demo-sähköpostit", label_en="Inbound demo email")
async def poll_mailslurp(j):
    return await poll_once()


schedule("email.poll_mailslurp", job="email.poll_mailslurp", every_seconds=30)()
