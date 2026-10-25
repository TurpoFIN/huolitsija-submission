"""RFC 822 parsing for inbound customer mail: headers, plain text (HTML stripped), the new part of a reply
(quoted history cut off) and attachments."""
import email
import re
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html import unescape

MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MSGID = re.compile(r"<[^<>\s]+>")


def decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:  # noqa: BLE001 - broken encodings fall back to the raw header
        return str(value)


def html_to_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html)
    s = re.sub(r"(?is)<blockquote.*?</blockquote>", "\n> (quoted)\n", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", s)
    s = unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


# Lines that start the quoted history of a reply ("On Tue, ... wrote:", "ti 24.10.2026 klo 9.12 X kirjoitti:",
# Outlook's "From: ... Sent: ..." block, "-----Original Message-----").
_QUOTE_START = re.compile(
    r"^(on .{4,200} wrote:|.{0,200}\bkirjoitti:?|-{2,}\s*(original message|alkuperäinen viesti)\s*-{2,}|_{8,})\s*$", re.I)
# Outlook's header block: "From: ..." directly followed by "Sent: ..." / "Lähetetty: ...". A lone "Lähettäjä: X" line is
# the shipper the customer is giving us, not quoted history.
_OUTLOOK_FROM = re.compile(r"^(from|lähettäjä|von|från)\s*:\s*\S", re.I)
_OUTLOOK_SENT = re.compile(r"^(sent|lähetetty|date|päivämäärä|gesendet|skickat)\s*:\s*\S", re.I)


def new_text(text: str) -> str:
    """The part of a reply the customer wrote now: everything above the quoted history."""
    out = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_START.match(line.strip()):
            break
        if _OUTLOOK_FROM.match(line.strip()) and any(_OUTLOOK_SENT.match(x.strip()) for x in lines[i + 1:i + 3]):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip() or (text or "").strip()


def ids(value) -> list[str]:
    return MSGID.findall(str(value or ""))


def parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    plain, html_part, attachments = None, None, []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        filename = decode(part.get_filename())
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        ctype = part.get_content_type()
        if filename or disp.startswith("attachment") or not ctype.startswith("text/"):
            if len(attachments) < MAX_ATTACHMENTS and len(payload) <= MAX_ATTACHMENT_BYTES:
                attachments.append({"filename": filename or f"attachment-{len(attachments) + 1}", "content_type": ctype,
                                    "size": len(payload), "data": payload})
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    text = plain if plain is not None else html_to_text(html_part or "")
    name, addr = parseaddr(decode(msg.get("From")))
    return {
        "message_id": (ids(msg.get("Message-ID")) or [""])[0],
        "in_reply_to": (ids(msg.get("In-Reply-To")) or [""])[0],
        "references": ids(msg.get("References")),
        "from_email": addr.lower().strip(), "from_name": name.strip(),
        "reply_to": parseaddr(decode(msg.get("Reply-To")))[1].lower() or None,
        "to": [a.lower() for _n, a in getaddresses([decode(v) for v in msg.get_all("To") or []]) if a],
        "cc": [a.lower() for _n, a in getaddresses([decode(v) for v in msg.get_all("Cc") or []]) if a],
        "subject": decode(msg.get("Subject")).strip(),
        "date": msg.get("Date"),
        "text": text.strip(), "new_text": new_text(text),
        "attachments": attachments,
        "auto_submitted": (msg.get("Auto-Submitted") or "").strip().lower(),
        "x_autoreply": (msg.get("X-Autoreply") or "").strip(),
        "x_autorespond": (msg.get("X-Autorespond") or "").strip(),
        "precedence": (msg.get("Precedence") or "").lower(),
        "list_id": msg.get("List-Id"),
        "auth_results": " ".join(str(v) for v in msg.get_all("Authentication-Results") or []),
        "content_language": msg.get("Content-Language"),
    }


def is_automatic(m: dict) -> str | None:
    """Explicit automation headers suppress replies; wording in a subject or sender is the model's decision."""
    if m.get("auto_submitted") and m["auto_submitted"] != "no":
        return "auto_submitted"
    if m.get("precedence") in ("bulk", "junk", "list", "auto_reply"):
        return "precedence"
    if m.get("list_id"):
        return "mailing_list"
    if m.get("x_autoreply") or m.get("x_autorespond"):
        return "auto_reply_header"
    return None


def auth_passed(m: dict) -> bool | None:
    """True when the receiving server recorded dmarc/dkim/spf=pass; None when there are no results to judge."""
    ar = (m.get("auth_results") or "").lower()
    if not ar:
        return None
    if re.search(r"dmarc=fail|dkim=fail.*spf=fail", ar):
        return False
    return bool(re.search(r"dmarc=pass|dkim=pass|spf=pass", ar))
