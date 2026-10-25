"""Transactional e-mail. Provider is chosen by EMAIL_PROVIDER: postmark | resend | smtp2go | smtp | log.

Every message is stored in `email_outbox` with its delivery result, so ops can see what a
customer was sent even when the provider is `log` (no external delivery)."""
import asyncio
import base64
import html as html_lib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

import httpx

from core import config
from core.db import db
from core.util import new_id, now_iso

log = logging.getLogger("email")


def render(title: str, lines: list[str], action_url: str | None = None, action_label: str | None = None,
           footer: str | None = None) -> tuple[str, str]:
    """Plain, calm HTML + text body. Lines are plain text (escaped)."""
    esc = html_lib.escape
    body = "".join(f"<p style='margin:0 0 12px'>{esc(l)}</p>" for l in lines)
    button = ""
    if action_url:
        button = (f"<p style='margin:20px 0'><a href='{esc(action_url)}' style='background:#111;color:#fff;"
                  f"padding:8px 14px;border-radius:999px;text-decoration:none;font-weight:500'>"
                  f"{esc(action_label or 'Open')}</a></p>")
    foot = esc(footer or f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS} · Services under NSAB 2015 (PSYM 2015)")
    html = (f"<div style='font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:14px;color:#111;"
            f"max-width:560px;margin:0 auto;padding:24px'><p style='font-size:13px;color:#666;margin:0 0 16px'>"
            f"{esc(config.COMPANY_NAME)}</p><h1 style='font-size:18px;font-weight:500;margin:0 0 16px'>{esc(title)}</h1>"
            f"{body}{button}<p style='font-size:12px;color:#888;margin-top:28px'>{foot}</p></div>")
    text = "\n\n".join([title, *lines] + ([f"{action_label or 'Open'}: {action_url}"] if action_url else []))
    return html, text


def _smtp_send(msg: EmailMessage, smtp: dict):
    """Blocking SMTP submission (runs in a thread). smtp: host, port, user, password, security starttls|ssl|none."""
    security = (smtp.get("security") or "starttls").lower()
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], int(smtp.get("port") or 465), timeout=20, context=context)
    else:
        server = smtplib.SMTP(smtp["host"], int(smtp.get("port") or 587), timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if smtp.get("user"):
            server.login(smtp["user"], smtp.get("password") or "")
        refused = server.send_message(msg)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def smtp_settings(prefix: str = "SMTP") -> dict:
    return {"host": config.env(f"{prefix}_HOST"), "port": config.env(f"{prefix}_PORT"), "user": config.env(f"{prefix}_USER"),
            "password": config.env(f"{prefix}_PASSWORD"), "security": config.env(f"{prefix}_SECURITY", "starttls")}


def email_configured() -> bool:
    """Whether the selected provider has the credentials send_email actually uses."""
    credentials = {"postmark": config.POSTMARK_SERVER_TOKEN, "resend": config.RESEND_API_KEY,
                   "smtp2go": config.SMTP2GO_API_KEY, "smtp": smtp_settings().get("host")}
    return bool(config.EMAIL_FROM and credentials.get(config.EMAIL_PROVIDER))

# async fn(mail: dict) -> dict | None, run before every send. The e-mail channel (s6) uses it to thread mail to
# customers who deal with us by e-mail only (Message-ID / In-Reply-To, reference in the subject, portal links
# that work without a password). A hook returns the changed mail dict or None for "unchanged".
OUTBOUND_HOOKS: list = []


SMTP2GO_HEADER_MAX = 255


def fit_references(value: str, limit: int = SMTP2GO_HEADER_MAX) -> str:
    """A References header within `limit` characters: the thread's first id and the newest ids that fit (RFC 5322 3.6.4)."""
    ids = value.split()
    if len(ids) < 2:
        return value[:limit]
    root, rest = ids[0], ids[1:]
    while rest and len(" ".join([root, *rest])) > limit:
        rest.pop(0)
    out = " ".join([root, *rest])
    return out if len(out) <= limit else " ".join(ids[-1:])[:limit]


async def send_email(to: str, subject: str, html: str, text: str, *, tag: str = "general",
                     company_id: str | None = None, ref: dict | None = None, headers: dict | None = None,
                     reply_to: str | None = None, from_addr: str | None = None, provider: str | None = None,
                     smtp: dict | None = None, attachments: list | None = None, idempotency_key: str | None = None) -> dict:
    """headers/reply_to/from_addr/provider/smtp are optional; callers that pass none get the configured default.
    headers may carry the message's own "Message-ID" (threaded mail, s6); attachments = [(filename, content_type, bytes)]."""
    headers = dict(headers or {})
    if OUTBOUND_HOOKS and "Message-ID" not in headers:
        mail = {"to": to, "subject": subject, "html": html, "text": text, "tag": tag, "company_id": company_id,
                "ref": ref or {}, "headers": headers, "reply_to": reply_to, "from_addr": from_addr, "provider": provider,
                "smtp": smtp, "attachments": list(attachments or [])}
        for hook in OUTBOUND_HOOKS:
            try:
                mail = (await hook(mail)) or mail
            except Exception:  # noqa: BLE001 - a hook never stops a mail
                log.exception("outbound e-mail hook %s failed", getattr(hook, "__name__", hook))
        to, subject, html, text, ref = mail["to"], mail["subject"], mail["html"], mail["text"], mail["ref"]
        headers, reply_to, from_addr = dict(mail["headers"] or {}), mail["reply_to"], mail["from_addr"]
        provider, smtp, attachments = mail["provider"], mail["smtp"], mail["attachments"]
        if mail.get("suppress"):
            rec = {"id": new_id(), "to": to, "subject": subject, "tag": tag, "provider": "none", "company_id": company_id,
                   "ref": ref or {}, "status": "suppressed", "reason": mail["suppress"], "created_at": now_iso(),
                   "text": text[:20000]}
            await db.email_outbox.insert_one(dict(rec))
            rec.pop("_id", None)
            return rec
    # Normalize text attachments after outbound hooks, before any provider serializes them.
    attachments = [(n, ct, b.encode("utf-8") if isinstance(b, str) else b) for n, ct, b in attachments or []]
    own_id = headers.pop("Message-ID", None)
    provider = provider or config.EMAIL_PROVIDER
    sender = from_addr or config.EMAIL_FROM
    resend_key = config.RESEND_API_KEY
    resend_blocked = False
    rec = {"id": new_id(), "to": to, "subject": subject, "tag": tag, "provider": provider,
           "company_id": company_id, "ref": ref or {}, "status": "queued", "created_at": now_iso(),
           "text": text[:20000], "from": sender,
           "attachments": [{"filename": n, "content_type": ct, "size": len(b)} for n, ct, b in attachments]}
    from services.demo_world import boundary
    real_run = await boundary.real_requester_run(to, ref, tag)
    if real_run:
        from services.demo_autoquote import conf as demo_conf
        provider = config.env("DEMO_EMAIL_PROVIDER") or "smtp2go"
        sender, reply_to = demo_conf.sender(), demo_conf.inbound_address()
        if provider == "mailslurp":
            sender = reply_to
            own_id = own_id or make_msgid(domain="huolitsija-demo.local")
        elif provider == "gmail":
            from services.email_channel import gmail_demo
            sender = gmail_demo.sender()
            own_id = own_id or make_msgid(domain="heliosone.fi")
        elif provider == "resend":
            resend_key = config.env("RESEND_API_KEY") or config.env("HUOLITSIJA_DEMO_RESEND_API_KEY")
            sender = config.env("DEMO_RESEND_FROM") or "onboarding@resend.dev"
            allowed = {value.strip().lower() for value in (config.env("DEMO_RESEND_ALLOWED_TO") or
                config.env("DEMO_RESEND_ALLOWED_RECIPIENTS")).split(",") if value.strip()}
            resend_blocked = bool(allowed and parseaddr(to)[1].lower() not in allowed)
            own_id = own_id or make_msgid(domain="huolitsija-demo.local")
        rec.update(provider=provider, **{"from": sender, "reply_to": reply_to, "demo_run_id": real_run["id"]})
    elif provider == "mailslurp":
        # MailSlurp is a customer-only demo transport. Preserve the existing
        # capped initial-RFQ transport; world mail still meets capture() below.
        provider = "smtp2go"
        rec["provider"] = provider
    async def capture():
        # Last transport boundary, after hooks and provider serialization choices.
        # No provider call follows a captured/forbidden envelope.
        world = await boundary.capture_mail(to=to, subject=subject, text=text, html=html, from_addr=sender, reply_to=reply_to,
                                            headers=headers, attachments=attachments, message_id=own_id, ref=ref,
                                            company_id=company_id, tag=tag)
        if not world:
            return False
        rec.update(world)
        await db.email_outbox.insert_one(dict(rec))
        rec.pop("_id", None)
        return True
    try:
        if resend_blocked:
            if await capture():
                return rec
            rec.update(status="failed", error="Demo requester is outside DEMO_RESEND_ALLOWED_TO")
            log.warning("demo customer mail blocked by Resend recipient allowlist: run=%s", real_run["id"])
            await db.raw.demo_runs.update_one({"id": real_run["id"]}, {"$push": {"internal_transport_events": {
                "at": now_iso(), "provider": "resend", "reason": "recipient_allowlist", "status": "failed"}}})
        elif provider == "gmail":
            from services.email_channel import gmail_demo
            if await capture():
                return rec
            async with httpx.AsyncClient(timeout=20) as client:
                rec.update(await gmail_demo.send(client, to=to, subject=subject, html=html, text=text,
                    headers={**headers, **({"Message-ID": own_id} if own_id else {})}, attachments=attachments,
                    run=real_run or await boundary.related_run(ref), ref=ref, tag=tag))
        elif provider == "mailslurp" and real_run:
            from services.email_channel import mailslurp
            if await capture():
                return rec
            if not mailslurp.configured():
                raise ValueError("Demo MailSlurp is not configured")
            async with httpx.AsyncClient(timeout=20) as client:
                rec.update(await mailslurp.send(client, to=to, subject=subject, html=html, text=text,
                    headers={**headers, "Message-ID": own_id}, attachments=attachments))
        elif provider == "smtp" and (smtp or smtp_settings()).get("host"):
            msg = EmailMessage()
            domain = (parseaddr(sender)[1].split("@") + ["huolitsija.fi"])[1]
            msg["Message-ID"] = own_id or make_msgid(domain=domain)
            msg["Date"] = formatdate(localtime=False)
            msg["From"], msg["To"], msg["Subject"] = sender, to, subject
            if reply_to:
                msg["Reply-To"] = reply_to
            for name, value in (headers or {}).items():
                msg[name] = value
            msg.set_content(text)
            msg.add_alternative(html, subtype="html")
            for name, ctype, data in attachments or []:
                maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
                msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
            if await capture():
                return rec
            await asyncio.get_running_loop().run_in_executor(None, _smtp_send, msg, smtp or smtp_settings())
            rec.update(status="sent", provider_id=msg["Message-ID"], message_id=msg["Message-ID"])
        elif provider == "postmark" and config.POSTMARK_SERVER_TOKEN:
            async with httpx.AsyncClient(timeout=15) as client:
                if await capture():
                    return rec
                r = await client.post(
                    "https://api.postmarkapp.com/email",
                    headers={"X-Postmark-Server-Token": config.POSTMARK_SERVER_TOKEN, "Accept": "application/json"},
                    json={"From": sender, "To": to, "Subject": subject, "HtmlBody": html,
                          "TextBody": text, "Tag": tag, "MessageStream": "outbound",
                          **({"ReplyTo": reply_to} if reply_to else {}),
                          **({"Headers": [{"Name": k, "Value": v} for k, v in
                                          {**headers, **({"Message-ID": own_id} if own_id else {})}.items()]}
                             if headers or own_id else {}),
                          **({"Attachments": [{"Name": n, "ContentType": c, "Content": base64.b64encode(d).decode()}
                                              for n, c, d in attachments]} if attachments else {})},
                )
            data = r.json() if r.content else {}
            rec.update(status="sent" if r.status_code == 200 and data.get("ErrorCode", 0) == 0 else "failed",
                       provider_id=data.get("MessageID"), provider_response=data, message_id=own_id)
        elif provider == "resend" and resend_key:
            async with httpx.AsyncClient(timeout=15) as client:
                if await capture():
                    return rec
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}",
                             **({"Idempotency-Key": idempotency_key} if real_run and idempotency_key else {})},
                    json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text,
                          "tags": [{"name": "tag", "value": tag}], **({"reply_to": reply_to} if reply_to else {}),
                          **({"headers": {**headers, **({"Message-ID": own_id} if own_id else {})}}
                             if headers or own_id else {}),
                          **({"attachments": [{"filename": n, "content": base64.b64encode(d).decode(), "content_type": _c}
                                              for n, _c, d in attachments]} if attachments else {})},
                )
            data = r.json() if r.content else {}
            rec.update(status="sent" if r.status_code in (200, 201) else "failed", provider_id=data.get("id"),
                       provider_response=data, message_id=own_id)
            if real_run and rec["status"] == "sent":
                rec["status"] = "accepted"  # A signed email.sent receipt proves sending.
        elif provider == "smtp2go" and config.SMTP2GO_API_KEY:
            custom = {**headers, **({"Reply-To": reply_to} if reply_to else {}), **({"Message-ID": own_id} if own_id else {})}
            if len(custom.get("References") or "") > SMTP2GO_HEADER_MAX:  # SMTP2GO refuses any header value over 255
                custom["References"] = fit_references(custom["References"])
            async with httpx.AsyncClient(timeout=15) as client:
                if await capture():
                    return rec
                r = await client.post(
                    "https://api.smtp2go.com/v3/email/send",
                    headers={"X-Smtp2go-Api-Key": config.SMTP2GO_API_KEY, "Accept": "application/json"},
                    json={"sender": sender, "to": [to], "subject": subject, "html_body": html, "text_body": text,
                          **({"custom_headers": [{"header": k, "value": v} for k, v in custom.items()]} if custom else {}),
                          **({"attachments": [{"filename": n, "fileblob": base64.b64encode(d).decode(), "mimetype": c}
                                              for n, c, d in attachments]} if attachments else {})},
                )
            data = (r.json() if r.content else {}).get("data") or {}
            ok = r.status_code == 200 and data.get("succeeded") == 1 and not data.get("failed")
            rec.update(status="sent" if ok else "failed", provider_id=data.get("email_id"), provider_response=data,
                       message_id=own_id, **({} if ok else {"error": str(data.get("error") or data.get("failures"))[:500]}))
        elif provider == "log":
            if await capture():
                return rec
            rec.update(status="logged", provider="log", message_id=own_id)
        else:
            if await capture():
                return rec
            rec.update(status="failed", error="Email provider is not configured", message_id=own_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("email to %s failed: %s", to, exc)
        rec.update(status="failed", error=str(exc))
    await db.email_outbox.insert_one(dict(rec))
    rec.pop("_id", None)
    if real_run and provider == "resend" and rec.get("provider_id"):
        from services.demo_autoquote import resend as demo_resend
        await demo_resend.reconcile(rec["provider_id"])
        rec = await db.email_outbox.find_one({"id": rec["id"]}, {"_id": 0})
    if real_run and rec.get("status") == "sent":
        from services.demo_autoquote.mail import smtp2go_message_id
        ids = [x for x in (own_id, rec.get("message_id"),
                          smtp2go_message_id(rec.get("provider_id")) if provider == "smtp2go" else None,
                          *(rec.get("message_ids") or [])) if x]
        rec["message_ids"] = list(dict.fromkeys(ids))
        await db.email_outbox.update_one({"id": rec["id"]}, {"$addToSet": {"message_ids": {"$each": list(dict.fromkeys(ids))}}})
        if (ref or {}).get("email_message_id"):
            await db.email_messages.update_one({"id": ref["email_message_id"]}, {"$addToSet": {"provider_message_ids": {"$each": ids}}})
    if rec.get('status') == 'sent':
        try:
            # Owned-requester copies become world inputs only after the actual
            # provider accepts the send; a failed delivery cannot elicit a reply.
            await boundary.capture_mail(to=to, subject=subject, text=text, html=html, from_addr=sender,
                reply_to=reply_to, headers=headers, attachments=attachments,
                message_id=rec.get('message_id') or own_id, ref=ref, company_id=company_id, tag=tag, delivery=rec)
        except Exception:  # provider acceptance and its outbox record remain authoritative
            log.exception('world copy failed for accepted outbox %s', rec['id'])
    return rec
