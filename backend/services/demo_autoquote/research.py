"""Partner research for one lane: the open web (OpenAI web search) and Huolitsija's own sources (partner_discovery:
YTJ/PRH, Wikidata, the carrier directory) run in parallel. Every contact address is checked on a public page before it
can be used (the page the research cited, else the company's own contact pages via partner_network.published_contact),
must pass the B2B rule and must not be opted out. Nothing is hardcoded: no list of partners exists in this code."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from core import config
from core.db import db
from core.llm import _llm_slots, _parse_json
from core.util import new_id, now_iso
from services import partner_discovery as discovery
from services import partner_network as pn

log = logging.getLogger("demo_autoquote.research")
EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
ROLE_LABEL = {"carrier": "Carrier", "forwarder": "Forwarder", "trucker": "Trucking company", "agent": "Agent"}

SYSTEM = """You are the partner research agent of Huolitsija, a Finnish digital freight forwarder and customs broker.
Find real companies that can quote one specific shipment: carriers, freight forwarders, trucking companies and agents
at either end. Search the web, open their pages, and keep only companies whose own website shows they serve this lane,
this mode, or one of its two countries with this kind of freight. For each one give the e-mail address the company itself
publishes for sales, quotes, bookings or general enquiries, and the exact page URL where that address appears. Never
guess or construct an address; skip a company if its page publishes none. Every partner must be able to quote the whole
door-to-door shipment, itself or through its own network: for road, a trucker or forwarder running this lane; for sea and
air, forwarders, NVOCCs, carriers with door service, and forwarders at the far end that handle the full route. Skip
companies that cover only one leg (local trucking to a port, a port-only agent). Prefer a mix of origin-side and
far-end companies. Return JSON only:
{"partners": [{"name": "...", "role": "carrier|forwarder|trucker|agent", "country": "ISO-2", "website": "https://...",
"email": "...", "email_source_url": "https://...", "why": "<one sentence: what on their site shows they fit this lane>",
"why_fi": "<the same sentence written in Finnish>",
"evidence_urls": ["https://..."]}]}"""


async def web_search(run_id: str, brief: str, want: int) -> tuple[list[dict], dict]:
    """One OpenAI Responses call with the web_search tool. -> (partners, tool_call record)."""
    body = {"model": config.LLM_MODEL, "instructions": SYSTEM, "tools": [{"type": "web_search"}],
            "input": [{"role": "user", "content": [{"type": "input_text", "text": f"{brief}\n\nReturn up to {want} partners."}]}],
            "max_output_tokens": 12000, "store": False}
    started = time.monotonic()
    run = {"id": new_id(), "agent": "demo_partner_research", "created_at": now_iso(), "ref": {"type": "demo_run", "id": run_id},
           "provider": "openai", "prompt_preview": brief[:4000], "files": [], "status": "running", "tools": ["web_search"]}
    data: dict = {}
    try:
        async with _llm_slots():
            async with httpx.AsyncClient(timeout=240) as client:
                r = await client.post(f"{config.OPENAI_BASE_URL}/responses", json=body,
                                      headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
        data = r.json()
        text = "".join(c.get("text", "") for it in data.get("output") or [] if it.get("type") == "message"
                       for c in it.get("content") or [] if c.get("type") == "output_text")
        parsed = _parse_json(text)
        partners = parsed.get("partners") if isinstance(parsed, dict) else parsed
        partners = [p for p in partners or [] if isinstance(p, dict) and p.get("name")]
        run.update(status="ok", model=f"openai/{data.get('model')}", output_preview=text[:8000])
    except Exception as exc:  # noqa: BLE001
        run.update(status="error", error=str(exc)[:2000])
        partners = []
    finally:
        run["latency_ms"] = int((time.monotonic() - started) * 1000)
        await db.ai_runs.insert_one(dict(run))
    queries, pages = [], []
    for it in data.get("output") or []:
        if it.get("type") == "web_search_call":
            a = it.get("action") or {}
            queries += a.get("queries") or ([a["query"]] if a.get("query") else [])
            if a.get("url"):
                pages.append(a["url"])
    tool = {"tool": "openai.web_search", "model": run.get("model"), "input": brief[:600],
            "output": f"{len(partners)} companies" if run["status"] == "ok" else run.get("error", "")[:300],
            "queries": list(dict.fromkeys(queries))[:20], "urls": list(dict.fromkeys(pages))[:20],
            "latency_ms": run["latency_ms"], "ai_run_id": run["id"]}
    return partners, tool


def _domain(url: str | None) -> str | None:
    host = urlparse(url or "").hostname
    return discovery.base_domain(host) if host else None


def _deobfuscate(text: str) -> str:
    return re.sub(r"\s*[\[\(]\s*(at|ät)\s*[\]\)]\s*", "@", re.sub(r"\s*[\[\(]\s*(dot|piste)\s*[\]\)]\s*", ".", text, flags=re.I), flags=re.I)


async def verify_contact(p: dict) -> dict:
    """The address must appear on a public page: the cited page, else the company's own contact pages."""
    email = (p.get("email") or "").strip().lower()
    site = p.get("website") or ""
    if site and not site.startswith("http"):
        site = "https://" + site
    out = {**p, "email": email, "website": site, "domain": _domain(site) or (_domain("https://" + email.split("@")[-1]) if "@" in email else None)}
    checked = []
    if email and p.get("email_source_url", "").startswith("https://"):
        try:
            async with pn._client() as client:
                text, final, reason = await pn._fetch_public(client, p["email_source_url"])
            checked.append(f"{p['email_source_url']}: {reason}")
            if text and email in {e.lower() for e in EMAIL_RX.findall(_deobfuscate(text))}:
                out.update(verified=True, contact_source_url=final, verification="address found on the cited page")
                return out
        except Exception as exc:  # noqa: BLE001 - a slow or broken site only drops this candidate
            checked.append(f"{p.get('email_source_url')}: {type(exc).__name__}")
    if site.startswith("https://"):
        try:
            found, evidence = await pn.published_contact({"website": site, "contact_email": email})
        except Exception as exc:  # noqa: BLE001
            found, evidence = None, f"{type(exc).__name__}"
        if found:
            out.update(email=found.lower(), verified=True, contact_source_url=evidence,
                       verification="address found on the company's contact page" if found.lower() == email
                       else "the company's own published address (the cited one was not on its pages)")
            return out
        checked.append(evidence[:300])
    out.update(verified=False, verification="; ".join(checked)[:500] or "no public page to check")
    return out


async def recently_contacted(email: str, days: int = 7) -> bool:
    """A real company gets at most one demo rate request a week (rehearsals in the sandbox do not count)."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(await db.partner_outreach.find_one({"contact_email": email, "demo_run_id": {"$exists": True},
                                                    "demo_sandbox": {"$ne": True}, "created_at": {"$gte": since}}))


async def huolitsija_sources(lane: str, *, timeout: float = 100) -> tuple[list[dict], dict]:
    """Huolitsija's own discovery (cached a week per lane) and its contactable candidates."""
    started = time.monotonic()
    try:
        res = await asyncio.wait_for(discovery.discover_lane(lane, contact_lookup=pn.published_contact), timeout)
        status = res.get("status")
    except asyncio.TimeoutError:
        res, status = {}, "timed out (kept earlier results)"
    except Exception as exc:  # noqa: BLE001
        res, status = {}, f"failed: {type(exc).__name__}"
    rows = await db.partner_candidates.find({"lanes": lane, "status": {"$in": ["contactable", "contacted", "partner"]},
                                             "contact_email": {"$nin": [None, ""]}}, {"_id": 0}).sort("score", -1).to_list(40)
    demo_carriers = {c["id"] async for c in db.carriers.find({"demo": True}, {"_id": 0, "id": 1})}
    rows = [r for r in rows if r.get("carrier_id") not in demo_carriers]
    sources = sorted({s.get("source") for r in rows for s in r.get("sources") or [] if s.get("source")})
    tool = {"tool": "huolitsija.partner_discovery", "input": lane,
            "output": f"{status}: {res.get('found', '—')} companies found, {len(rows)} with a published address"
                      + (f" ({', '.join(sources)})" if sources else ""),
            "notes": (res.get("notes") or [])[:8], "latency_ms": int((time.monotonic() - started) * 1000)}
    return rows, tool


def lane_brief(req: dict, mode: str) -> str:
    o, d = req.get("origin") or {}, req.get("destination") or {}
    from services.pricing import cargo_summary
    try:
        cargo = cargo_summary({**(req.get("cargo") or {})})
    except Exception:  # noqa: BLE001 - research only needs a rough description
        cargo = json.dumps(req.get("cargo") or {})[:300]
    return (f"Lane: {o.get('name')} ({o.get('country')}) to {d.get('name')} ({d.get('country')}). Mode: {mode} freight.\n"
            f"Cargo: {cargo}.\nPickup and delivery at the two cities (door to door).")
