"""Inbound mailbox on mail.tm (no DNS, no CAPTCHA): MAILTM_ADDRESS and MAILTM_PASSWORD (MAILTM_API, default
https://api.mail.tm). While Postmark holds inbound mail, CRM_REPLY_TO / PARTNER_REPLY_TO name this address. A durable job
fetches unseen messages over the mailbox's own authenticated API, sends each raw RFC 822 message through the same
pipeline as the webhook (dispatch_raw), and marks it seen only after that succeeded; a duplicate is ignored downstream
by Message-ID. Off when the env is unset."""
from __future__ import annotations

import logging

import httpx

from core import config
from core.db import db
from core.jobs import job, schedule
from core.util import now_iso

from .dispatch import dispatch_raw

log = logging.getLogger("email_channel.mailtm")


def configured() -> bool:
    from services.demo_autoquote import conf
    if conf.real_requester_enabled() and conf.inbound_provider() in ("mailslurp", "gmail"):
        return False
    return bool(config.env("MAILTM_ADDRESS") and config.env("MAILTM_PASSWORD"))


def _api() -> str:
    return (config.env("MAILTM_API") or "https://api.mail.tm").rstrip("/")


async def poll_once(client: httpx.AsyncClient | None = None, limit: int = 30) -> dict:
    if not configured():
        return {"status": "off"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    out = {"status": "ok", "fetched": 0, "results": [], "errors": []}
    try:
        r = await client.post(_api() + "/token", json={"address": config.env("MAILTM_ADDRESS"),
                                                       "password": config.env("MAILTM_PASSWORD")})
        r.raise_for_status()
        auth = {"Authorization": "Bearer " + r.json()["token"]}
        r = await client.get(_api() + "/messages", headers=auth, params={"page": 1})
        r.raise_for_status()
        for m in [m for m in r.json().get("hydra:member", []) if not m.get("seen")][:limit]:
            try:
                src = await client.get(f"{_api()}/sources/{m['id']}", headers=auth)
                src.raise_for_status()
                raw = (src.json().get("data") or "").encode("utf-8")
                res = await dispatch_raw(raw, provider_id="mailtm:" + m["id"], provider_authenticated=True, source="mailtm")
                await client.patch(f"{_api()}/messages/{m['id']}", json={"seen": True},
                                   headers={**auth, "Content-Type": "application/merge-patch+json"})
                out["fetched"] += 1
                out["results"].append({"id": m["id"], "subject": (m.get("subject") or "")[:120],
                                       **{k: res.get(k) for k in ("channel", "status", "id", "lead_id", "duplicate")}})
            except Exception as exc:  # noqa: BLE001 - left unseen, retried next poll
                log.warning("mail.tm message %s failed: %s", m.get("id"), exc)
                out["errors"].append({"id": m.get("id"), "error": f"{type(exc).__name__}: {exc}"[:300]})
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if own:
            await client.aclose()
    await db.system.update_one({"key": "email:mailtm_last_poll"},
                               {"$set": {"key": "email:mailtm_last_poll", "at": now_iso(),
                                         **{k: out.get(k) for k in ("status", "fetched", "error")}}}, upsert=True)
    return out


@job("email.poll_mailtm", label_fi="Saapuvat sähköpostit (mail.tm)", label_en="Inbound e-mail (mail.tm)")
async def poll_mailtm(j):
    res = await poll_once()
    return {k: res.get(k) for k in ("status", "fetched", "error")}


schedule("email.poll_mailtm", job="email.poll_mailtm",
         every_seconds=max(15, int(config.env("MAILTM_POLL_SECONDS") or 30)))()
