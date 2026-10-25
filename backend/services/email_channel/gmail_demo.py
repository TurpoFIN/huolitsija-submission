"""Demo-only Gmail OAuth send and read-only, instance-owned reply polling.

Uses only HUOLITSIJA_GMAIL_* credentials; MAIL_AGENT_* is deliberately unrelated.
The OAuth grant must already have gmail.readonly and gmail.send. No scopes are
requested or changed here. sendAs.list accepts readonly, but the configured
alias must actually appear with verificationStatus=accepted. Admin routing alone
is insufficient. Google API contracts checked against official docs 2026-10-25:
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/list
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get

Two instances may share the same alias. An inbound message is dispatched ONLY
when its RFC reply headers contain an actual Gmail-sent Message-ID recorded in
THIS database, its sender matches the run's immutable requester or the approved
initial-RFQ partner, and its destination matches this instance's alias/optional
plus address. No label/read-state mutation.
Mongo receipts lease each dispatch; completed cursors and retry receipts survive
restarts. The ordinary inbound Message-ID uniqueness provides crash recovery.
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core import config
from core.db import db, register_index
from core.jobs import job, schedule
from core.util import now_iso

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ALIAS = "huolitsija@heliosone.fi"
CREDENTIALS = ("HUOLITSIJA_GMAIL_CLIENT_ID", "HUOLITSIJA_GMAIL_CLIENT_SECRET", "HUOLITSIJA_GMAIL_REFRESH_TOKEN")
register_index("demo_gmail_sent", "message_id", unique=True,
               partialFilterExpression={"message_id": {"$type": "string"}})
register_index("demo_gmail_sent", [("route", 1), ("created_wall", 1)])
register_index("demo_gmail_receipts", [("route", 1), ("status", 1)])


class GmailDemoError(ValueError):
    """Sanitized integration failure; never includes tokens or provider bodies."""


def _wall():
    return datetime.now(timezone.utc)


def sender() -> str:
    return config.env("DEMO_GMAIL_FROM") or f"Huolitsija <{ALIAS}>"


def reply_to() -> str:
    return config.env("DEMO_INBOUND_ADDRESS").lower()


def _valid_route(value: str) -> bool:
    if parseaddr(value)[1] != value or any(x in value for x in ('\r', '\n', ' ', ',', '<', '>', '"')):
        return False
    local, sep, domain = value.rpartition("@")
    tag = local.removeprefix("huolitsija+")
    return bool(sep and domain == "heliosone.fi" and (local == "huolitsija" or
                local.startswith("huolitsija+") and tag and len(local) <= 64
                and all(char.isascii() and (char.isalnum() or char in "._-") for char in tag)))


def configured() -> bool:
    return (config.env_bool("DEMO_AUTOQUOTE_ENABLED") and config.env_bool("DEMO_REAL_REQUESTER")
            and config.env_bool("DEMO_WORLD_SANDBOX")
            and config.env("DEMO_INBOUND_PROVIDER") == "gmail"
            and parseaddr(sender())[1].lower() == ALIAS and _valid_route(reply_to())
            and all(config.env(name) for name in CREDENTIALS))


def _route_key() -> str:
    return hashlib.sha256(reply_to().encode()).hexdigest()[:24]


def _body(response: httpx.Response, operation: str) -> dict:
    if not 200 <= response.status_code < 300:
        raise GmailDemoError(f"Gmail {operation} rejected (HTTP {response.status_code})")
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise GmailDemoError(f"Gmail {operation} returned invalid JSON") from None
    if not isinstance(data, dict):
        raise GmailDemoError(f"Gmail {operation} returned invalid metadata")
    return data


async def _auth(client: httpx.AsyncClient) -> dict:
    response = await client.post(TOKEN_URL, data={"grant_type": "refresh_token",
        "client_id": config.env(CREDENTIALS[0]), "client_secret": config.env(CREDENTIALS[1]),
        "refresh_token": config.env(CREDENTIALS[2])})
    data = _body(response, "token refresh")
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise GmailDemoError("Gmail token refresh returned no access token")
    return {"Authorization": "Bearer " + data["access_token"]}


async def verify_alias(client: httpx.AsyncClient, auth: dict) -> None:
    data = _body(await client.get(API + "/settings/sendAs", headers=auth), "alias verification")
    if not any(str(row.get("sendAsEmail", "")).lower() == ALIAS and row.get("verificationStatus") == "accepted"
               for row in data.get("sendAs", []) if isinstance(row, dict)):
        raise GmailDemoError("Gmail sender alias is not an accepted sendAs identity")


def _safe_headers(headers: dict) -> dict:
    # MIME envelope and sender identity cannot be overridden through custom headers.
    forbidden = {"from", "sender", "reply-to", "to", "cc", "bcc", "return-path", "content-type", "mime-version"}
    result = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in forbidden or str(key).lower().startswith("resent-"):
            raise GmailDemoError("Gmail custom envelope/sender headers are forbidden")
        if value is not None:
            result[str(key)] = str(value)
    return result


async def send(client: httpx.AsyncClient, *, to: str, subject: str, html: str, text: str,
               headers: dict | None = None, attachments: list | None = None, run: dict,
               ref: dict | None = None, tag: str | None = None) -> dict:
    """Called only after core.email's lineage and world capture boundary."""
    if not configured() or config.env("DEMO_EMAIL_PROVIDER") != "gmail":
        raise GmailDemoError("Demo Gmail provider is not configured")
    recipients = [address.lower() for _, address in getaddresses([to])]
    if len(recipients) != 1 or not recipients[0]:
        raise GmailDemoError("Demo Gmail requires one recipient")
    address = recipients[0]
    stored = await db.raw.demo_runs.find_one({"id": (run or {}).get("id")}, {"_id": 0})
    if not stored:
        raise GmailDemoError("Demo Gmail run does not exist")
    requester = (stored.get("requester") or {}).get("address", "").lower()
    role, outreach = "requester", None
    if tag == "demo_rfq" or (ref or {}).get("kind") == "rfq" or not stored.get("real_requester") or requester != address:
        role = "partner"
        outreach = await _rfq_outreach(stored, address, headers or {}, ref or {}, tag)
    elif tag:
        from services.demo_world.boundary import CUSTOMER_TAGS
        if tag not in CUSTOMER_TAGS:
            raise GmailDemoError("Demo Gmail requester mail has a non-customer purpose")
    allowed = {value.strip().lower() for value in (config.env("DEMO_GMAIL_ALLOWED_TO") or
        config.env("DEMO_GMAIL_ALLOWED_RECIPIENTS")).split(",") if value.strip()}
    if role == "requester" and allowed and address not in allowed:
        raise GmailDemoError("Demo requester is outside DEMO_GMAIL_ALLOWED_TO")
    route = reply_to()
    if stored.get("gmail_reply_to") and stored["gmail_reply_to"] != route:
        raise GmailDemoError("Demo Gmail reply route differs from the bound run")
    custom = _safe_headers(headers or {})
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"], msg["To"], msg["Subject"] = sender(), to, subject
    msg["Reply-To"], msg["Date"] = route, formatdate(localtime=False)
    msg["Message-ID"] = custom.pop("Message-ID", None) or make_msgid(domain="heliosone.fi")
    for name, value in custom.items():
        msg[name] = value
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, content_type, content in attachments or []:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(content.encode() if isinstance(content, str) else content,
                           maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    auth = await _auth(client)
    await verify_alias(client, auth)
    # Store immutable route before transport. Exact sent IDs, below, remain the
    # ownership proof; an address or client-supplied run alone never opens polling.
    bound = await db.raw.demo_runs.update_one({"id": stored["id"], "$or": [
        {"gmail_reply_to": {"$exists": False}}, {"gmail_reply_to": route}]}, {"$set": {"gmail_reply_to": route}})
    if not bound.matched_count:
        raise GmailDemoError("Demo Gmail reply route changed concurrently")
    if role == "partner":
        # OAuth/alias verification may cross UTC midnight or an operator stop.
        # Recheck the live quota/phase immediately before the external request.
        outreach = await _rfq_outreach(stored, address, headers or {}, ref or {}, tag)
    started = _wall()
    data = _body(await client.post(API + "/messages/send", headers=auth,
        json={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}), "send")
    ident = data.get("id")
    if not isinstance(ident, str) or not ident:
        raise GmailDemoError("Gmail send returned no provider message ID")
    metadata = _body(await client.get(API + "/messages/" + ident, headers=auth,
        params=[("format", "metadata"), ("metadataHeaders", "Message-ID"), ("metadataHeaders", "From")]), "sent metadata")
    actual = {str(h.get("name", "")).lower(): str(h.get("value", ""))
              for h in (metadata.get("payload") or {}).get("headers", []) if isinstance(h, dict)}
    from .mime import ids
    mids = ids(actual.get("message-id"))
    if len(mids) != 1 or parseaddr(actual.get("from", ""))[1].lower() != ALIAS:
        raise GmailDemoError("Gmail accepted mail but sender/Message-ID verification failed; delivery unverified")
    await db.raw.demo_gmail_sent.update_one({"_id": ident}, {"$setOnInsert": {
        "route": route, "run_id": stored["id"], "recipient": address, "message_id": mids[0],
        "role": role, "outreach_id": (outreach or {}).get("id"),
        "contact": (outreach or {}).get("contact_email"),
        "requested_message_id": str(msg["Message-ID"]), "created_wall": started, "provider": "gmail"}}, upsert=True)
    return {"status": "sent", "provider_id": ident, "message_id": mids[0],
            "message_ids": list(dict.fromkeys([str(msg["Message-ID"]), mids[0]])),
            "from": sender(), "reply_to": route}


async def _native_outreach(run_id: str, outreach_id: str | None) -> dict | None:
    row = await db.raw.partner_outreach.find_one({"id": outreach_id, "demo_run_id": run_id}, {"_id": 0}) if outreach_id else None
    if not row or not row.get("contact_email"):
        return None
    contact = str(row["contact_email"]).lower()
    for field, collection in (("candidate_id", "partner_candidates"), ("carrier_id", "carriers")):
        if row.get(field):
            native = await db.raw[collection].find_one({"id": row[field]}, {"_id": 0})
            # A named source must exist; when it has an address, it must agree.
            if not native or native.get("contact_email") and str(native["contact_email"]).lower() != contact:
                return None
    return row


async def _rfq_outreach(run: dict, recipient: str, headers: dict, ref: dict, tag: str | None) -> dict:
    from services.demo_world import boundary
    from services.demo_autoquote import conf
    if config.env_bool("DEMO_CAPTURE_RFQS") or ref.get("id") != run["id"]:
        raise GmailDemoError("Demo Gmail real RFQ transport is disabled or unrelated")
    row = await _native_outreach(run["id"], ref.get("outreach_id"))
    if not row or not await boundary._quote_phase_recipient(str(row["contact_email"]).lower(), ref, tag):
        raise GmailDemoError("Demo Gmail RFQ lacks approved quota and native outreach lineage")
    contact = str(row["contact_email"]).lower()
    original = next((str(value).lower() for key, value in headers.items()
                     if str(key).lower() == "x-huolitsija-original-to"), None)
    box = conf.partner_sandbox()
    if recipient != contact and not (box and recipient == box[0] and original == contact):
        raise GmailDemoError("Demo Gmail RFQ envelope does not match its native partner")
    return row


async def _owned(raw: bytes, internal_ms: int) -> tuple[dict | None, str | None]:
    from .mime import parse
    parsed = parse(raw)
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    destinations = {address.lower() for _, address in getaddresses(
        [str(value) for name in ("To", "Cc", "Delivered-To", "X-Original-To") for value in msg.get_all(name, [])])}
    if reply_to() not in destinations:
        return None, "destination"
    refs = list(dict.fromkeys([parsed.get("in_reply_to"), *(parsed.get("references") or [])]))
    refs = [value for value in refs if value]
    if not refs:
        return None, "headers"
    sent = await db.raw.demo_gmail_sent.find_one({"route": reply_to(), "message_id": {"$in": refs}},
                                               {"_id": 0}, sort=[("created_wall", -1)])
    if not sent:
        return None, "headers"
    run = await db.raw.demo_runs.find_one({"id": sent["run_id"], "gmail_reply_to": reply_to()}, {"_id": 0})
    if not run:
        return None, "run"
    if sent.get("role", "requester") == "partner":
        outreach = await _native_outreach(run["id"], sent.get("outreach_id"))
        if not outreach or str(outreach["contact_email"]).lower() != sent.get("contact"):
            return run, "partner_lineage"
        expected = sent["recipient"]
    else:
        expected = (run.get("requester") or {}).get("address", "").lower()
        if not run.get("real_requester") or expected != sent["recipient"]:
            return run, "requester_lineage"
    if parsed.get("from_email") != expected:
        return run, "sender"
    # Gmail internalDate is trusted transport metadata, not a forged Date header.
    if internal_ms < int(sent["created_wall"].timestamp() * 1000) - 1000:
        return run, "before_send"
    return run, None


async def _dispatch(client: httpx.AsyncClient, auth: dict, ident: str) -> str:
    from .dispatch import dispatch_raw
    data = _body(await client.get(API + "/messages/" + ident, headers=auth, params={"format": "raw"}), "message read")
    try:
        raw = base64.urlsafe_b64decode(str(data["raw"]) + "=" * (-len(str(data["raw"])) % 4))
        internal_ms = int(data["internalDate"])
    except (KeyError, TypeError, ValueError):
        raise GmailDemoError("Gmail message lacks raw data or transport timestamp") from None
    run, reason = await _owned(raw, internal_ms)
    if not run:
        return "unmatched"
    key = _route_key() + ":" + ident
    owner, wall = uuid.uuid4().hex, _wall()
    try:
        receipt = await db.raw.demo_gmail_receipts.find_one_and_update({"_id": key, "$or": [
            {"status": "retry"}, {"status": "processing", "lease_until": {"$lt": wall}},
            {"status": {"$exists": False}}]}, {"$set": {"route": reply_to(), "gmail_id": ident,
                "run_id": run["id"], "status": "processing", "owner": owner,
                "lease_until": wall + timedelta(seconds=90)}}, upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return "duplicate"
    if not receipt:
        return "duplicate"
    if reason:
        await db.raw.demo_gmail_receipts.update_one({"_id": key, "owner": owner}, {"$set": {
            "status": "ignored", "reason": reason, "finished_wall": _wall()}})
        await db.raw.demo_runs.update_one({"id": run["id"]}, {"$push": {"internal_transport_events": {
            "at": now_iso(), "provider": "gmail", "reason": "inbound_" + reason, "status": "ignored"}}})
        return "ignored"
    try:
        await dispatch_raw(raw, provider_id="gmail:" + ident, provider_authenticated=True, source="gmail_demo")
    except Exception:
        await db.raw.demo_gmail_receipts.update_one({"_id": key, "owner": owner}, {"$set": {"status": "retry"}})
        raise GmailDemoError("Gmail owned reply dispatch failed; retry queued") from None
    await db.raw.demo_gmail_receipts.update_one({"_id": key, "owner": owner}, {"$set": {
        "status": "done", "finished_wall": _wall()}})
    return "dispatched"


async def poll_once(client: httpx.AsyncClient | None = None, limit: int = 30) -> dict:
    if not configured():
        return {"status": "off"}
    earliest = await db.raw.demo_gmail_sent.find_one({"route": reply_to()}, {"_id": 0}, sort=[("created_wall", 1)])
    if not earliest:
        return {"status": "idle", "fetched": 0}
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    out = {"status": "ok", "fetched": 0, "ignored": 0, "unmatched": 0, "duplicates": 0, "errors": []}
    cursor_key = "gmail:" + _route_key()
    try:
        auth = await _auth(client)
        cursor = await db.raw.demo_gmail_cursors.find_one({"_id": cursor_key}) or {}
        after = int(cursor.get("after") or earliest["created_wall"].timestamp() - 1)
        token = cursor.get("page_token")
        pending = [row["gmail_id"] async for row in db.raw.demo_gmail_receipts.find({"route": reply_to(), "$or": [
            {"status": "retry"}, {"status": "processing", "lease_until": {"$lt": _wall()}}]}).limit(100)]
        params = {"q": f"deliveredto:{reply_to()} after:{after}", "maxResults": max(1, min(limit, 100))}
        if token:
            params["pageToken"] = token
        scan_started = int(cursor.get("scan_started") or _wall().timestamp()) if token else int(_wall().timestamp())
        data = _body(await client.get(API + "/messages", headers=auth, params=params), "message list")
        identifiers = list(dict.fromkeys([*pending, *(row["id"] for row in data.get("messages", []) if isinstance(row, dict) and row.get("id"))]))
        for ident in identifiers:
            try:
                result = await _dispatch(client, auth, ident)
                field = {"dispatched": "fetched", "duplicate": "duplicates"}.get(result, result)
                out[field] += 1
            except Exception as exc:
                out["errors"].append({"id": ident, "error": type(exc).__name__})
        # Never move the timestamp beyond a failed unclaimed fetch. Paging is
        # durable; a restart continues the same query before advancing its floor.
        if not out["errors"]:
            nxt = data.get("nextPageToken")
            await db.raw.demo_gmail_cursors.update_one({"_id": cursor_key}, {"$set": {
                "after": after if nxt else max(after, scan_started - 120), "page_token": nxt,
                "scan_started": scan_started if nxt else None,
                "last_poll_wall": _wall()}}, upsert=True)
    except Exception as exc:
        out.update(status="error", error=type(exc).__name__)
    finally:
        if own:
            await client.aclose()
    await db.raw.system.update_one({"key": "email:gmail_demo_last_poll"}, {"$set": {
        "key": "email:gmail_demo_last_poll", "at": now_iso(), **out}}, upsert=True)
    return out


@job("email.poll_gmail_demo", label_fi="Saapuvat demo-sähköpostit (Gmail)", label_en="Inbound demo email (Gmail)")
async def poll_gmail_demo(j):
    return await poll_once()


schedule("email.poll_gmail_demo", job="email.poll_gmail_demo", every_seconds=30)()
