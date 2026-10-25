"""Partner mailbox reader (IMAP) for the Sourcing Autopilot.

Env: PARTNER_IMAP_HOST, PARTNER_IMAP_PORT (993 / 143), PARTNER_IMAP_USER, PARTNER_IMAP_PASSWORD,
PARTNER_IMAP_SECURITY (ssl | starttls | none), PARTNER_IMAP_FOLDER (INBOX).

Unseen messages are fetched with BODY.PEEK and marked \\Seen only after they were handed over, so a crash between
fetch and processing re-reads them; partner_messages deduplicates by Message-ID.
"""
import asyncio
import email
import imaplib
import re
import ssl
from email.header import decode_header, make_header
from email.message import Message
from html import unescape

from core import config

MAX_PER_POLL = 50


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - broken encodings fall back to the raw header
        return value


def _text(msg: Message) -> str:
    plain, html_part = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html_part is None:
            html_part = text
    if plain is not None:
        return plain
    if html_part is not None:
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_part)
        stripped = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", stripped)
        return unescape(re.sub(r"<[^>]+>", " ", stripped))
    return ""


def parse_raw(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    return {"message_id": (msg.get("Message-ID") or "").strip(), "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")), "subject": _decode(msg.get("Subject")), "text": _text(msg),
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(), "references": (msg.get("References") or "").strip(),
            "auth_results": " ".join(msg.get_all("Authentication-Results") or []), "date": msg.get("Date"),
            "source": "imap"}


def _connect() -> imaplib.IMAP4:
    host, security = config.env("PARTNER_IMAP_HOST"), config.env("PARTNER_IMAP_SECURITY", "ssl").lower()
    port = int(config.env("PARTNER_IMAP_PORT") or (993 if security == "ssl" else 143))
    if security == "ssl":
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=20)
    else:
        conn = imaplib.IMAP4(host, port, timeout=20)
        if security == "starttls":
            conn.starttls(ssl.create_default_context())
    conn.login(config.env("PARTNER_IMAP_USER"), config.env("PARTNER_IMAP_PASSWORD"))
    return conn


def _fetch_unseen_blocking() -> list[dict]:
    conn = _connect()
    try:
        conn.select(config.env("PARTNER_IMAP_FOLDER", "INBOX"))
        typ, data = conn.uid("search", None, "UNSEEN")
        if typ != "OK":
            return []
        out = []
        for uid in (data[0] or b"").split()[:MAX_PER_POLL]:
            typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if typ == "OK" and raw:
                item = parse_raw(raw)
                item["imap_uid"] = uid.decode()
                out.append(item)
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _mark_seen_blocking(uids: list[str]):
    if not uids:
        return
    conn = _connect()
    try:
        conn.select(config.env("PARTNER_IMAP_FOLDER", "INBOX"))
        for uid in uids:
            conn.uid("store", uid.encode(), "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def fetch_unseen() -> list[dict]:
    return await asyncio.get_running_loop().run_in_executor(None, _fetch_unseen_blocking)


async def mark_seen(uids: list[str]):
    await asyncio.get_running_loop().run_in_executor(None, _mark_seen_blocking, uids)


def parse_postmark(body: dict) -> dict:
    """Map the provider's documented inbound payload to the existing RFQ parser contract."""
    if not isinstance(body, dict) or not isinstance(body.get("MessageID"), str) or not body["MessageID"].strip():
        raise ValueError("Postmark MessageID is required")
    headers = {str(h.get("Name", "")).lower(): str(h.get("Value", ""))
               for h in body.get("Headers", []) if isinstance(h, dict)}
    text = body.get("StrippedTextReply") or body.get("TextBody")
    if not text and body.get("HtmlBody"):
        text = unescape(re.sub(r"<[^>]+>", " ", body["HtmlBody"]))
    return {"message_id": body["MessageID"], "provider_id": body["MessageID"],
            "from": (body.get("FromFull") or {}).get("Email") or body.get("From", ""),
            "to": body.get("OriginalRecipient") or body.get("To", ""), "subject": body.get("Subject", ""),
            "text": text or "", "in_reply_to": headers.get("in-reply-to", ""),
            "references": headers.get("references", ""), "auth_results": headers.get("authentication-results", ""),
            "date": body.get("Date"), "source": "postmark"}
