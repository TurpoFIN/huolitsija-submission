"""The autonomous run: request -> intake -> clarification by e-mail -> partner research -> rate requests -> replies,
follow-ups and one counteroffer -> pick -> pricing -> stored Huolitsija quote -> offer e-mail. Every step calls
Huolitsija's own code where it exists and writes its event at the moment it happens (events.emit)."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import time
from datetime import date, timedelta

from core import config
from core.db import db
from core.events import emit as domain_emit
from core.lifecycle import on_startup
from core.llm import llm_json, llm_text
from core.settings import get_settings
from core.util import new_id, now_iso
from services import partner_network as pn

from . import clock, conf, mail, requester, research, simulator, texts
from . import public_requester
from .events import emit

log = logging.getLogger("demo_autoquote")


def _tk(key: str, **values) -> dict:
    """Title key and values for the flow map's own EN/FI wording (shared/flow-map-title-keys.md); title stays English."""
    return {"title_key": key, "title_vars": values}
_tasks: dict[str, asyncio.Task] = {}


@on_startup(order=95)
async def _interrupted_runs():
    """A run lives in this process; after a restart its quote phase cannot resume, so it is marked interrupted."""
    await db.demo_runs.update_many({"real_requester": True, "status": "running", "pending_requester_ids.0": {"$exists": True}},
                                  {"$set": {"status": "awaiting_requester"}})
    await db.demo_runs.update_many({"status": "running"}, {"$set": {"status": "interrupted", "updated_at": now_iso()}})
    # A run whose lifecycle continues in this process on another build: record the change in its trace.
    build = config.env("DEMO_BUILD_SHA") or None
    if build:
        async for run in db.demo_runs.find({"phase": "lifecycle", "status": {"$nin": ["done", "failed", "stopped", "interrupted"]},
                                            "build": {"$nin": [None, build]}}, {"_id": 0, "id": 1, "build": 1}):
            await db.demo_runs.update_one({"id": run["id"]}, {"$set": {"build": build},
                                                              "$push": {"builds": {"from": run["build"], "to": build, "at": now_iso()}}})
            await emit(run["id"], "intake", f"Backend restarted on build {build} · lifecycle continues", kind="tool",
                       data={"build_change": {"from": run["build"], "to": build}, **_tk("run.restarted", build=build)})
    await public_requester.resume_waiting()
    # The requester side (outbox copies, mailbox arrival proof) of every live run continues in this process.
    async for run in db.demo_runs.find({"status": {"$nin": ["done", "failed", "stopped", "interrupted"]}}, {"_id": 0, "id": 1}):
        asyncio.get_running_loop().create_task(requester.watch(run["id"]))

EUROPE = set("AL AD AT BA BE BG CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM TR UA XK".split())
SELLER_PAYS_MAIN = {"CPT", "CIP", "DAP", "DPU", "DDP", "CFR", "CIF"}
D_TERMS = {"DAP", "DPU", "DDP", "CPT", "CIP", "CFR", "CIF"}
FINAL = ("offer", "declined", "opted_out")
LINE_KEYS = ("code", "description", "basis", "qty", "unit_cost_cents", "unit_price_cents", "amount_cents", "stage",
             "is_pass_through", "vat_code", "vat_rate_pct", "payer")

EXTRAS_SYSTEM = """Read a shipper's freight quote request (and any answers they gave later). Return JSON only:
{"pickup_address": "<the pickup location as stated: street address, or postcode and city if that is what they give; null if nothing beyond the city name>",
 "delivery_address": "<same for delivery>",
 "dimensions": "<package dimensions as stated, else null>",
 "dangerous_goods_stated": true|false,   // true if the text says whether the goods are dangerous goods (either way)
 "stackable_stated": true|false,
 "goods_stated": true|false}             // true if the text says what the goods are
Never invent values."""

FOLLOW_UP_SYSTEM = """You are Huolitsija's sourcing agent. A freight partner answered our rate request without a complete
all-in price. Write a short reply (40-110 words, plain text, no greeting line with a name, no signature) that answers any
question they asked using ONLY the shipment facts given (say "not specified by the shipper, please quote standard" for
anything unknown) and asks for what is still missing among: all-in price in EUR, transit time, validity."""


def _real_iso() -> str:
    """Wall-clock time (lane 2's demo clock may run ahead of it under DEMO_WORLD_SANDBOX)."""
    from core import util
    return util.iso((getattr(util, "real_now", None) or util.now)())


def _ref() -> str:
    return "DQ-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


async def start(body: dict, user: dict | None) -> dict:
    origin, destination = (body.get("origin") or "").strip(), (body.get("destination") or "").strip()
    text = (body.get("text") or "").strip()
    request_text = f"Quote request from {origin} to {destination}." + (f" {text}" if text else "")
    run = {"id": body.get("run_id") or new_id(), "ref": _ref(), "status": "running", "phase": "quote", "seed": int(body.get("seed") or secrets.randbits(31)),
           "created_at": now_iso(), "created_real": _real_iso(), "updated_at": now_iso(), "event_seq": 0,
           "created_by": (user or {}).get("email"),
           "request_text": request_text, "origin_text": origin, "destination_text": destination,
           "profile": {k: v for k, v in (body.get("profile") or {}).items() if v not in (None, "")},
           "requester": {"address": conf.requester_address(), "name": (body.get("profile") or {}).get("contact_name")},
           "autonomy": "full", "recipients": [], "clock": clock.initial(1.0), "build": config.env("DEMO_BUILD_SHA") or None,
           "sandbox_partners": bool(conf.partner_sandbox()), "partners": []}
    if body.get("real_requester"):
        if not conf.real_requester_ready():
            from fastapi import HTTPException
            raise HTTPException(503, "Please try again shortly. Our quote service is getting ready.")
        run.update(real_requester=True, requester={"address": body["email"], "name": body.get("name")},
                   locale=body.get("locale") or "en", access_token_hash=body["access_token_hash"])
        run.update(await public_requester.rfq_mode(run))
    if body.get("reuse_rfqs_from"):
        run["reuse_from"] = await _check_reuse(body["reuse_rfqs_from"], run)
    ident = None if run.get("real_requester") else await _world_identity(run)
    if ident:  # the requester is a world-sim actor: its registry facts come through the sandbox boundary
        run["requester_registry"] = ident
        run["profile"].update({k: ident[k] for k in ("company", "contact_name") if ident.get(k)})
        run["requester"]["name"] = run["profile"].get("contact_name")
    await db.demo_runs.insert_one(dict(run))
    if run.get("real_requester"):
        await _ensure_thread(run["id"])
        await emit(run["id"], "outreach", "RFQ transport selected", kind="tool", data={"rfq_mode": run["rfq_mode"]})
    run.pop("_id", None)
    try:  # a new run's quote phase starts at real now, speed 1 (never a previous run's lifecycle time)
        await clock.reset_now(run["id"])
    except ValueError as exc:  # another run's lifecycle owns the world clock
        if run.get("real_requester"):
            await db.demo_runs.update_one({"id": run["id"]}, {"$set": {"status": "deferred"}})
            await db.demo_runs.update_one({"id": run["id"]}, {"$set": {"deferred_at": _real_iso()}})
            return {**run, "status": "deferred"}
        await db.demo_runs.update_one({"id": run["id"]}, {"$set": {"status": "failed", "error": str(exc)}})
        await emit(run["id"], "intake", "Run not started: " + str(exc), state="failed", kind="phase")
        return {**run, "status": "failed", "error": str(exc)}
    _tasks[run["id"]] = asyncio.get_running_loop().create_task(_main(run["id"]))
    return run


async def _world_identity(run: dict) -> dict | None:
    """{company, business_id, vat_id, address, signatory, ubo, contact_name} of the requester from the world sim."""
    try:
        from services.demo_world import requester_identity
    except ImportError:
        return None
    ident = requester_identity(run)
    if asyncio.iscoroutine(ident):
        ident = await ident
    return dict(ident) if ident else None


async def stop(run_id: str) -> None:
    # Any phase: a stopped lifecycle run must stop being the world's live run too.
    await db.demo_runs.update_one({"id": run_id, "status": {"$nin": ["done", "failed", "stopped"]}},
                                  {"$set": {"status": "stopped", "updated_at": now_iso()}})
    t = _tasks.pop(run_id, None)
    if t and not t.done():
        t.cancel()


async def _run(run_id: str) -> dict:
    return await db.demo_runs.find_one({"id": run_id}, {"_id": 0})


async def _main(run_id: str) -> None:
    watcher = asyncio.get_running_loop().create_task(requester.watch(run_id))
    try:
        await quote_phase(run_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("demo run %s failed", run_id)
        await db.demo_runs.update_one({"id": run_id}, {"$set": {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}})
        await emit(run_id, "intake", "Run stopped by an error", state="failed", kind="phase",
                   data={"reasoning": f"{type(exc).__name__}: {exc}"[:500]})
    finally:
        await asyncio.sleep(0)
        if watcher.done():
            return
        # the watcher ends by itself once the offer is seen in the mailbox


# ---------------------------------------------------------------- intake

async def _parse(run_id: str, text: str) -> tuple[dict, dict, dict]:
    from services import quoting_agent
    t0 = time.monotonic()
    parsed = await quoting_agent.parse_request(text, locale="en")
    t1 = time.monotonic()
    extras = await llm_json("demo_intake", EXTRAS_SYSTEM, text, max_tokens=500, ref={"type": "demo_run", "id": run_id})
    extras = extras if isinstance(extras, dict) else {}
    tool = [{"tool": "huolitsija.quoting_agent.parse_request", "input": text[:400],
             "output": parsed.get("summary") or "", "latency_ms": int((t1 - t0) * 1000)},
            {"tool": "openai.extract_addresses", "input": "pickup / delivery / dimensions",
             "output": ", ".join(f"{k}={v}" for k, v in extras.items() if v not in (None, False))[:300],
             "latency_ms": int((time.monotonic() - t1) * 1000)}]
    return parsed, extras, {"tool_calls": tool}


def _missing(parsed: dict, extras: dict) -> list[str]:
    req, miss = parsed["request"], set(parsed.get("missing") or [])
    c = req.get("cargo") or {}
    out = []
    if not c.get("description") and not extras.get("goods_stated"):
        out.append("cargo.description")
    if not c.get("packages") and not c.get("containers"):
        out.append("cargo.packages")
    if not c.get("weight_kg") and not c.get("containers"):
        out.append("weight_kg")
    if not c.get("volume_m3") and not c.get("containers") and not extras.get("dimensions"):
        out.append("volume_m3")
    if "ready_date" in miss or not req.get("ready_date"):
        out.append("ready_date")
    if not req.get("incoterm"):
        out.append("incoterm")
    if not extras.get("pickup_address"):
        out.append("pickup_address")
    if not extras.get("delivery_address"):
        out.append("delivery_address")
    if not extras.get("dangerous_goods_stated") and not c.get("dangerous_goods"):
        out.append("dangerous_goods")
    return out


def _essential_missing(parsed: dict, extras: dict, mode: str) -> list[str]:
    """What must be known before any partner is asked for a rate. FCL (sea/rail): container type and count (no weight).
    Road groupage: gross weight, and packages or dimensions/volume. LCL sea and air (chargeable weight): gross weight,
    volume or dimensions, and packages or dimensions."""
    c = parsed["request"].get("cargo") or {}
    if c.get("containers") and mode in ("sea", "rail"):  # FCL: priced per container; containers mean nothing to air
        return [] if all(x.get("type") and x.get("count") for x in c["containers"]) else ["cargo.containers"]
    dims = bool(extras.get("dimensions"))
    out = [] if c.get("weight_kg") else ["weight_kg"]
    if mode in ("sea", "air") and not (c.get("volume_m3") or dims):
        out.append("volume_m3")
    if not (c.get("packages") or dims or (mode not in ("sea", "air") and c.get("volume_m3"))):
        out.append("cargo.packages")
    return out


def _mode(req: dict, weight: float | None) -> tuple[str, str]:
    if req.get("mode") in ("road", "sea", "air", "rail"):
        return req["mode"], "stated in the request"
    o, d = (req.get("origin") or {}).get("country"), (req.get("destination") or {}).get("country")
    if o in EUROPE and d in EUROPE and "IS" not in (o, d):
        return "road", "both ends are on the European road network"
    if weight is not None and weight <= 300:
        return "air", "light shipment between continents"
    return "sea", "intercontinental lane, cargo weight suits sea freight"


def _complete(req: dict) -> dict:
    """Fill what the pricing engine needs but the shipper left open, the way an ops person would."""
    req = dict(req)
    inc = req.get("incoterm")
    if inc and not req.get("incoterm_place"):
        side = req.get("destination") if inc in D_TERMS else req.get("origin")
        req["incoterm_place"] = (side or {}).get("name")
    if inc:
        req["customer_role"] = "seller" if inc in SELLER_PAYS_MAIN else "buyer"
    return req


# ---------------------------------------------------------------- the requester's e-mail thread (Huolitsija's e-mail channel)

async def _ensure_thread(run_id: str) -> tuple[dict, dict, dict]:
    """The requester as Huolitsija's e-mail channel knows them: a customer company + user (ensure_customer) and one
    email_threads row, so later replies (e.g. BOOK 1) run through the channel's own flow."""
    from services.email_channel import store
    from services.email_channel.customers import ensure_customer
    run = await _run(run_id)
    prof = run.get("profile") or {}
    reg = run.get("requester_registry") or {}
    sender = {"person_name": prof.get("contact_name"), "company_name": prof.get("company"),
              **{k: reg.get(k) for k in ("business_id", "vat_id", "address", "phone") if reg.get(k)}}
    user, company, _ = await ensure_customer(conf.requester_address(run), prof.get("contact_name"), {"sender": sender}, run.get("locale") or "en")
    if reg.get("company") and company.get("name") != reg["company"] and not company.get("business_id") \
            and company.get("kyc_status") in (None, "not_started"):
        # The requester mailbox's company from an earlier rehearsal takes the world's identity before onboarding.
        await db.companies.update_one({"id": company["id"]}, {"$set": {
            "name": reg["company"], "email_intake.business_id_stated": reg.get("business_id"),
            "email_intake.vat_id_stated": reg.get("vat_id")}})
        company = {**company, "name": reg["company"]}
    if run.get("thread_id"):
        t = await store.thread_by_id(run["thread_id"])
        if t:
            return t, user, company
    t = await store.create_thread({"subject": f"Quote request {run['origin_text']} → {run['destination_text']} [{run['ref']}]",
                                   "from_email": conf.requester_address(run), "from_name": prof.get("contact_name")}, language=run.get("locale") or "en")
    t = await store.update_thread(t["id"], company_id=company["id"], user_id=user["id"], demo_run_id=run_id)
    await db.demo_runs.update_one({"id": run_id}, {"$set": {"thread_id": t["id"], "company_id": company["id"]}})
    return t, user, company


async def _send_thread(run_id: str, thread: dict, title: str, lines: list[str], *, tag: str, subject: str,
                       attachments: list | None = None) -> dict:
    from services.email_channel import outbound
    if thread.get("language") == "fi":
        import json
        translated = await llm_json("demo_customer_translation",
            "Translate this freight forwarder's email into Finnish. Return JSON with title, subject, lines (array). "
            "Preserve all numbers, dates, URLs, references, BOOK command and names exactly. No added caveats or facts.",
            json.dumps({"title": title, "subject": subject, "lines": lines}),
            max_tokens=2500, ref={"type": "demo_run", "id": run_id})
        if not isinstance(translated.get("lines"), list) or not translated.get("title") or not translated.get("subject"):
            raise ValueError("Finnish email translation omitted required fields")
        title, subject, lines = translated["title"], translated["subject"], translated["lines"]
    row = await outbound.send(thread, title, lines, tag=tag, subject=subject, attachments=attachments)
    rec = await db.email_outbox.find_one({"id": row.get("outbox_id")}, {"_id": 0}) or {}
    ids = list(dict.fromkeys([row["message_id"], *(rec.get("message_ids") or [])] +
        ([mail.smtp2go_message_id(rec.get("provider_id"))]
         if rec.get("provider") == "smtp2go" and rec.get("provider_id") else [])))
    if rec:
        await db.email_outbox.update_one({"id": rec["id"]}, {"$addToSet": {"message_ids": {"$each": ids}}, "$set": {"demo_run_id": run_id}})
    return {**rec, "message_ids": ids, "text": row.get("text"), "email_message_id": row["id"], "subject": row.get("subject"),
            "to": rec.get("to") or thread.get("customer_email"), "status": rec.get("status") or row.get("status")}


# ---------------------------------------------------------------- clarification

async def _clarify(run_id: str, parsed: dict, extras: dict, missing: list[str], round_no: int) -> tuple[str, list[str]]:
    run = await _run(run_id)
    subject, lines = texts.clarification(run, parsed["request"], missing, round_no)
    thread, _user, _company = await _ensure_thread(run_id)
    from services.email_channel import store
    thread = await store.update_thread(thread["id"], state="awaiting_info")
    rec = await _send_thread(run_id, thread, "A few details for your quote", lines[:-1], tag="email_need_info", subject=subject)
    subject = rec.get("subject") or subject
    await db.demo_runs.update_one({"id": run_id}, {"$push": {"clarify": {"round": round_no, "subject": subject,
                                  "message_ids": rec["message_ids"], "sent_at": now_iso(), "missing": missing}}})
    await emit(run_id, "clarify", f"Asked {len(missing)} question{'s' if len(missing) != 1 else ''} by e-mail", state="waiting",
               kind="email_out", edge=("clarify", "requester"),
               data={**_tk("clarify.asked", n=len(missing)), "emails": [{"direction": "out", "from": conf.sender(), "to": rec.get("to"), "subject": subject,
                                 "text": rec.get("text"), "message_id": (rec["message_ids"] or [None])[0], "outbox_id": rec.get("id"),
                                 "email_message_id": rec.get("email_message_id"), "provider": rec.get("provider"),
                                 "status": rec.get("status")}],
                     "facts": {"missing": ", ".join(missing), "round": round_no},
                     "reasoning": "These details change the price or the carrier's answer, so the agent asks before sourcing rates."})
    return subject, rec["message_ids"]


async def _requester_reply(run: dict, ids: list[str]) -> dict | None:
    if run.get("real_requester"):
        ids = list(ids)
        async for sent in db.raw.email_outbox.find({"demo_run_id": run["id"], "message_ids": {"$in": ids}}, {"message_ids": 1}):
            ids = list(dict.fromkeys([*ids, *(sent.get("message_ids") or [])]))
    query = {"direction": "in", "$or": [{"in_reply_to": {"$in": ids}},
             {"references": {"$in": ids}}], "demo_consumed": {"$ne": True}}
    if run.get("real_requester"):
        query["from_email"] = conf.requester_address(run)
        query["automatic"] = {"$in": [None, False, ""]}
        query["source"] = {"$nin": ["demo-world", "demo_requester"]}
    else:
        query["$or"].append({"subject": {"$regex": run["ref"]}})
    return await db.email_messages.find_one(query, {"_id": 0}, sort=[("created_at", 1)])


async def _await_requester(run_id: str, ids: list[str], timeout: float | None = None) -> dict | None:
    """Real requesters wait durably without an expiry; the recovery job resumes on inbound."""
    run = await _run(run_id)
    deadline = time.monotonic() + (timeout or conf.requester_wait_s())
    while True:
        m = await _requester_reply(run, ids)
        if m:
            from pymongo import ReturnDocument
            return await db.email_messages.find_one_and_update({"id": m["id"], "demo_consumed": {"$ne": True}},
                {"$set": {"demo_consumed": True, "demo_run_id": run_id, "status": "processed"}},
                projection={"_id": 0}, return_document=ReturnDocument.AFTER)
        if run.get("real_requester") or time.monotonic() >= deadline:
            return None
        if (await _run(run_id)).get("status") != "running":
            return None
        await asyncio.sleep(1.5)


# ---------------------------------------------------------------- research + outreach

async def _research(run_id: str, req: dict, mode: str) -> list[dict]:
    o, d = req["origin"], req["destination"]
    lane = pn.lane_key(mode, o["country"], d["country"])
    await emit(run_id, "research", f"Researching partners for {o['name']} → {d['name']}", state="active",
               edge=("intake", "research"), data={"facts": {"lane": lane, "mode": mode},
                                                  **_tk("research.researching", **{"from": o["name"], "to": d["name"]})})
    want = conf.target_partners()
    brief = research.lane_brief(req, mode)
    (web, web_tool), (own, own_tool) = await asyncio.gather(research.web_search(run_id, brief, want + 4),
                                                            research.huolitsija_sources(lane))
    await emit(run_id, "research", f"Web search: {len(web_tool.get('queries') or [])} queries, {len(web_tool.get('urls') or [])} pages read",
               kind="tool", data={"tool_calls": [web_tool], **_tk("research.web_search", queries=len(web_tool.get("queries") or []),
                                                                   pages=len(web_tool.get("urls") or []))})
    await emit(run_id, "research", "Huolitsija partner network checked", kind="tool", data={"tool_calls": [own_tool]})
    checked = await asyncio.gather(*(research.verify_contact(p) for p in web))
    cands, dropped = [], []
    for p in checked:
        if not p.get("verified"):
            dropped.append({"name": p.get("name"), "why": p.get("verification")})
            continue
        ok, why = pn.discovery.email_is_b2b(p["email"], {"domain": p.get("domain")})
        if not ok or await pn.is_opted_out(p["email"], p.get("domain")) or (
                not conf.partner_sandbox() and await research.recently_contacted(p["email"])):
            dropped.append({"name": p.get("name"), "why": why if not ok else "opted out or contacted this week"})
            continue
        cands.append({"name": p["name"], "role": p.get("role") or "forwarder", "country": (p.get("country") or "").upper()[:2],
                      "website": p.get("website"), "domain": p.get("domain"), "contact_email": p["email"],
                      "contact_source_url": p.get("contact_source_url"), "reason": p.get("why"),
                      "reason_i18n": {"en": p.get("why"), "fi": p.get("why_fi") or None},
                      "verification": p.get("verification"), "found_by": "web search",
                      "sources": [{"source": "web_search", "url": u, "detail": "evidence"} for u in (p.get("evidence_urls") or [])[:4]]})
    for c in own:
        if await pn.is_opted_out(c["contact_email"], c.get("domain")) or (
                not conf.partner_sandbox() and await research.recently_contacted(c["contact_email"])):
            continue
        cands.append({"name": c["name"], "role": "carrier" if "carrier" in (c.get("roles") or []) else
                      "trucker" if "haulier" in (c.get("roles") or []) else "forwarder", "country": c.get("country"),
                      "website": c.get("website"), "domain": c.get("domain"), "contact_email": c["contact_email"],
                      "contact_source_url": c.get("contact_source_url"), "candidate_id": c["id"], "found_by": "Huolitsija network",
                      "reason": "; ".join(c.get("score_reasons") or []), "score": c.get("score"),
                      "verification": "published address found by Huolitsija's contact lookup", "sources": c.get("sources") or []})
    chosen, seen = [], set()
    # Web-researched partners first (lane-specific evidence), Huolitsija network next; one address per company domain.
    for c in cands:
        key = c.get("domain") or c["contact_email"].split("@")[-1]
        if key in seen:
            continue
        seen.add(key)
        chosen.append(c)
        if len(chosen) >= want:
            break
    await emit(run_id, "research", f"{len(chosen)} partners selected", state="done",
               data={**_tk("research.selected", n=len(chosen)), "facts": {"found on the web": len(web), "verified": len([p for p in checked if p.get("verified")]),
                               "from Huolitsija network": len(own), "selected": len(chosen)},
                     "partners": [{k: c.get(k) for k in ("name", "role", "country", "website", "contact_email",
                                                         "contact_source_url", "reason", "reason_i18n", "found_by", "verification")} for c in chosen],
                     "dropped": dropped[:12],
                     "reasoning": "Each partner shows on its own site that it serves this lane or country pair; every address "
                                  "was read from a public page before use."})
    return chosen


async def _send_rfq(run_id: str, cand: dict, req: dict, extras: dict, mode: str, reference: dict) -> dict | None:
    run = await _run(run_id)
    s = await get_settings()
    o, d = req["origin"], req["destination"]
    row = {"id": new_id(), "ref": pn._new_ref(), "campaign_key": f"demo:{run_id}:{cand.get('domain') or cand['contact_email']}",
           "candidate_id": cand.get("candidate_id"), "carrier_id": None, "carrier_name": cand["name"], "business_id": None,
           "domain": cand.get("domain"), "country": cand.get("country"), "role": cand.get("role"),
           "verification_only": False, "test_mailbox": False, "single_rfq_only": False,
           "lane": pn.lane_key(mode, o["country"], d["country"]), "mode": mode, "service": f"DOOR_TO_DOOR_{mode.upper()}",
           "basis": "PER_SHIPMENT", "request_scope": None, "cargo": req.get("cargo") or {},
           "origin": o.get("unlocode") or o.get("name"), "destination": d.get("unlocode") or d.get("name"), "equipment_type": None,
           "contact_email": cand["contact_email"], "contact_evidence": cand.get("contact_source_url"),
           "contact_source_url": cand.get("contact_source_url"), "sources": cand.get("sources") or [], "score": cand.get("score"),
           "reason": cand.get("reason"), "reason_i18n": cand.get("reason_i18n"), "found_by": cand.get("found_by"),
           "sell_cents": s["partner_sell_price_cents"],
           "limit_cents": None, "rounds": 0, "clarifications": 0, "reminders": 0, "offers": [], "status": "demo_selected", "log": [],
           "created_at": now_iso(), "updated_at": now_iso(), "demo_run_id": run_id, "demo_sandbox": bool(conf.partner_sandbox())}
    subject, lines = texts.rfq(run, row, req, extras, mode)
    reserved = await mail.reserve_recipient(run_id, row["contact_email"])
    if not reserved:
        return None
    row = await _native_partner_outreach(req,mode,row)
    await db.partner_outreach.insert_one(dict(row))
    node = f"p:{row['id']}"
    await emit(run_id, node, cand["name"], state="active", kind="step",
               data={"partner": {k: cand.get(k) for k in ("name", "role", "country", "website", "contact_email",
                                                          "contact_source_url", "reason", "reason_i18n", "found_by",
                                                          "verification")},
                     "reasoning": cand.get("reason") or ""})
    rec = await mail.send(run_id, row["contact_email"], subject, lines, kind="rfq", external=True, title=f"Rate request {row['ref']}",
                          headers={"X-Huolitsija-RFQ": row["ref"], "X-Huolitsija-Demo-Kind": "rfq",
                                   "List-Unsubscribe": f"<mailto:{conf.inbound_address()}?subject=stop%20{row['ref']}>"},
                          ref={"outreach_id": row["id"]})
    mid = rec["message_ids"][-1] if rec["message_ids"] else None
    msg = {"id": new_id(), "direction": "out", "kind": "rfq", "outreach_id": row["id"], "candidate_id": row.get("candidate_id"),
           "ref": row["ref"], "from": conf.sender(), "to": rec.get("to"), "to_original": row["contact_email"],
           "sandbox": rec.get("to") != row["contact_email"], "subject": subject, "text": rec.get("text"),
           "message_id": mid, "message_ids": rec["message_ids"], "provider_id": rec.get("provider_id"),
           "reply_to": conf.inbound_address(), "email_id": rec.get("id"), "provider": rec.get("provider"), "status": rec.get("status"),
           "at": now_iso(), "seq": time.time_ns(), "demo_run_id": run_id}
    await db.partner_messages.insert_one(dict(msg))
    transport_status = rec.get("status")
    sent_ok = transport_status == "sent"
    outreach_status = "demo_awaiting_reply" if sent_ok else "email_failed" if transport_status == "failed" else "email_pending"
    await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {
        "status": outreach_status, "send_to": rec.get("to"),
        "last_message_id": mid, "thread_ids": rec["message_ids"], "last_outbound_at": now_iso(), "rfq_subject": subject},
        "$push": {"log": {"kind": "rfq", "at": now_iso(), "email_id": rec.get("id"), "delivery_status": rec.get("status"),
                          "message_id": mid, "provider_id": rec.get("provider_id")}}})
    title = "Rate request sent · awaiting reply" if sent_ok else "Rate request failed" if transport_status == "failed" else "Rate request awaiting send confirmation"
    await emit(run_id, node, title, state="failed" if transport_status == "failed" else "waiting", kind="email_out", edge=("outreach", node),
               data={"emails": [{"direction": "out", "from": conf.sender(), "to": row["contact_email"], "subject": subject,
                                 "text": rec.get("text"), "message_id": mid, "outbox_id": rec.get("id"),
                                 "partner_message_id": msg["id"], "provider": rec.get("provider"), "status": rec.get("status"),
                                 **({"delivered_to": rec.get("to")} if msg["sandbox"] else {})}],
                     "tool_calls": [{"tool": f"core.email.send_email ({rec.get('provider')})", "input": row["contact_email"],
                                     "output": f"{rec.get('status')} {rec.get('provider_id') or ''}".strip()}]})
    row = {**row, "status": outreach_status, "send_to": rec.get("to"), "thread_ids": rec["message_ids"], "rfq_subject": subject}
    if sent_ok:
        simulator.schedule_first_reply(run, row, reference=reference, subject=subject, rfq_ids=rec["message_ids"])
    return row


SHIPMENT_FACTS = ("cargo_description", "packages", "weight_kg", "dimensions", "dangerous_goods", "value_eur", "ready_date",
                  "incoterm", "pickup_address", "delivery_address")


async def _check_reuse(old_id: str, run: dict) -> str:
    """An ended run of the same shipment whose rate requests the new run takes over (no partner is asked twice)."""
    from fastapi import HTTPException
    old = await db.demo_runs.find_one({"id": old_id}, {"_id": 0})
    same = old and (old.get("origin_text"), old.get("destination_text")) == (run["origin_text"], run["destination_text"]) and \
        all((old.get("profile") or {}).get(k) == run["profile"].get(k) for k in SHIPMENT_FACTS)
    problem = ("unknown run" if not old else "that run is still live" if old["status"] not in ("stopped", "failed", "done")
               else "it is a different shipment" if not same
               else "they were already reused" if await db.partner_outreach.count_documents({"reused_from_run": old_id}) else None)
    if problem:
        raise HTTPException(409, f"Cannot reuse the rate requests: {problem}")
    return old_id


async def _reuse_outreach(run_id: str, old_id: str) -> list[dict]:
    """Take over the halted run's rate requests and every reply to them: the partners are not contacted again."""
    old = await db.demo_runs.find_one({"id": old_id}, {"_id": 0, "id": 1, "ref": 1, "recipients": 1})
    rows = await db.partner_outreach.find({"demo_run_id": old_id}, {"_id": 0}).sort("created_at", 1).to_list(20)
    ids = [r["id"] for r in rows]
    await db.partner_outreach.update_many({"id": {"$in": ids}}, {"$set": {"demo_run_id": run_id, "reused_from_run": old_id},
                                                                "$push": {"log": {"kind": "reused_by_run", "at": now_iso(), "run_id": run_id}}})
    await db.partner_messages.update_many({"outreach_id": {"$in": ids}}, {"$set": {"reused_by_run": run_id}})
    await db.partner_messages.update_many({"outreach_id": {"$in": ids}, "direction": "in"}, {"$set": {"demo_seen": False}})
    reuse = {"from_run": old_id, "from_ref": old["ref"], "outreach_ids": ids, "recipients": len(old.get("recipients") or []),
             "new_rfqs": 0}
    await db.demo_runs.update_one({"id": run_id}, {"$set": {"rfq_reuse": reuse, "recipients": old.get("recipients") or [],
                                                           "partners": ids}})
    await emit(run_id, "research", f"Partners from {old['ref']}", state="active", edge=("intake", "research"),
               data=_tk("research.reused", source=old["ref"]))
    await emit(run_id, "research", f"{len(rows)} partners selected · no new search", state="done",
               data={**_tk("research.selected_reused", n=len(rows)), "facts": {"taken over from": old["ref"], "partners": len(rows)},
                     "reasoning": f"The same shipment was asked for in {old['ref']}, which stopped before its offer. Its rate "
                                  "requests and replies carry over, so no partner gets the request twice."})
    await emit(run_id, "outreach", f"Reusing {len(rows)} rate requests", state="active", edge=("research", "outreach"),
               data=_tk("outreach.reusing", n=len(rows)))
    await emit(run_id, "outreach", "Shipment details attached", kind="step", edge=("clarify", "outreach"))
    for r in rows:
        node = f"p:{r['id']}"
        rfq = await db.partner_messages.find_one({"outreach_id": r["id"], "direction": "out", "kind": "rfq"}, {"_id": 0}) or {}
        await emit(run_id, node, r["carrier_name"], state="active", kind="step",
                   data={"partner": {"name": r["carrier_name"], "role": r.get("role"), "country": r.get("country"),
                                     "contact_email": r["contact_email"], "contact_source_url": r.get("contact_source_url"),
                                     "reason": r.get("reason"), "reason_i18n": r.get("reason_i18n"), "found_by": r.get("found_by")},
                         "reasoning": r.get("reason") or ""})
        await emit(run_id, node, f"Rate request from {old['ref']} · awaiting reply", state="waiting", kind="email_out",
                   edge=("outreach", node),
                   data={**_tk("partner.rfq_reused", source=old["ref"]), "emails": [{"direction": "out", "from": rfq.get("from"), "to": r["contact_email"], "subject": rfq.get("subject"),
                                     "text": rfq.get("text"), "message_id": rfq.get("message_id"), "provider": rfq.get("provider"),
                                     "status": rfq.get("status"), "sent_at": rfq.get("at")}],
                         "facts": {"first sent": rfq.get("at") or "—", "in run": old["ref"], "sent again": "no"}})
    await emit(run_id, "outreach", f"{len(rows)} rate requests reused · 0 new", state="done",
               data={**_tk("outreach.reused", n=len(rows), new=0), "facts": {"partners reached": len(rows), "new rate requests": 0, "taken over from": old["ref"]}})
    return [{**r, "demo_run_id": run_id, "reused_from_run": old_id} for r in rows]


async def _follow_up(run_id: str, row: dict, kind: str, text: str, reference: dict, *, simulated_thread: bool,
                     counter_cents: int | None = None, last_offer: dict | None = None) -> None:
    """Follow-ups on a simulated thread go to the simulator, on a real thread only to the partner rehearsal sandbox: a real
    partner gets nothing beyond the initial capped rate request, so its real reply is left to a person."""
    node = f"p:{row['id']}"
    if not simulated_thread and not conf.partner_sandbox():
        await emit(run_id, node, "Real reply · the demo does not answer it", state="waiting", kind="decision",
                   data={**_tk("partner.real_reply_held"),
                         "reasoning": f"{row['carrier_name']} replied for real. The demo sends a real partner nothing beyond the "
                                      f"rate request, so this {kind} is not sent; the reply stays in Huolitsija's partner inbox."})
        return
    run = await _run(run_id)
    fresh = await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0})
    thread = fresh.get("thread_ids") or []
    turn = len(fresh.get("demo_followups") or []) + 1
    if kind == "counteroffer":
        subject, lines = texts.counteroffer(fresh, counter_cents, None)
    else:
        subject, lines = texts.follow_up(fresh, kind, text)
    body = "\n\n".join(lines)
    if simulated_thread:
        mid = mail.new_message_id()
        await db.partner_messages.insert_one({"id": new_id(), "direction": "out", "kind": kind, "outreach_id": row["id"],
                                              "ref": row["ref"], "from": conf.sender(), "to": "partner simulator",
                                              "to_original": row["contact_email"], "subject": subject, "text": body,
                                              "message_id": mid, "provider": "demo-simulator", "status": "routed_to_simulator",
                                              "amount_cents": counter_cents, "at": now_iso(), "seq": time.time_ns(), "demo_run_id": run_id})
        ids = thread + [mid]
        simulator.schedule_follow_up_reply(run, fresh, turn=turn, kind=kind, text=body, reference=reference,
                                           subject=fresh.get("rfq_subject") or subject, thread_ids=ids,
                                           counter_cents=counter_cents, last_offer=last_offer)
        email = {"direction": "out", "from": conf.sender(), "to": row["contact_email"], "subject": subject, "text": body,
                 "message_id": mid, "provider": "demo-simulator", "status": "routed to the partner simulator", "simulated": True}
    else:
        headers = {"X-Huolitsija-RFQ": row["ref"], "X-Huolitsija-Demo-Kind": kind}
        if thread:
            from core.email import fit_references
            headers.update({"In-Reply-To": thread[-1], "References": fit_references(" ".join(thread))})
        rec = await mail.send(run_id, row["contact_email"], subject, lines, kind=f"partner_{kind}", external=True,
                              title=subject, headers=headers, ref={"outreach_id": row["id"]})
        mid = (rec["message_ids"] or [None])[-1]
        ids = thread + rec["message_ids"]
        await db.partner_messages.insert_one({"id": new_id(), "direction": "out", "kind": kind, "outreach_id": row["id"],
                                              "ref": row["ref"], "from": conf.sender(), "to": rec.get("to"),
                                              "to_original": row["contact_email"], "subject": subject, "text": body,
                                              "message_id": mid, "provider": rec.get("provider"), "status": rec.get("status"),
                                              "email_id": rec.get("id"), "amount_cents": counter_cents, "at": now_iso(),
                                              "seq": time.time_ns(), "demo_run_id": run_id})
        email = {"direction": "out", "from": conf.sender(), "to": row["contact_email"], "subject": subject, "text": body,
                 "message_id": mid, "outbox_id": rec.get("id"), "provider": rec.get("provider"), "status": rec.get("status")}
    sets = {"thread_ids": ids, "last_outbound_at": now_iso(), "status": "demo_awaiting_reply"}
    if counter_cents:
        sets["last_counter_cents"] = counter_cents
    await db.partner_outreach.update_one({"id": row["id"]}, {"$set": sets, "$push": {"demo_followups": {"kind": kind, "at": now_iso(),
                                                                                                      "simulated": simulated_thread}}})
    title = "Counteroffer sent" if kind == "counteroffer" else "Follow-up sent"
    await emit(run_id, node, f"{title} · awaiting reply", state="waiting", kind="email_out", edge=("replies", node),
               simulated=simulated_thread, data={"emails": [email], "facts": {"counter": texts.eur(counter_cents)} if counter_cents else {}})


async def _reply_origin(m: dict, row: dict, run: dict) -> dict:
    """When and in which run a partner reply arrived. A rerun takes over an earlier run's replies on purpose, so the map
    tells them apart from this run's own by these fields, not by the text: the run that owned the rate request at the
    receive time (the request's own run, then each takeover in the row's log)."""
    at = m.get("at")
    owner = run.get("id")
    if at and run.get("created_at") and at < run["created_at"]:
        rfq = await db.partner_messages.find_one({"outreach_id": row["id"], "direction": "out", "kind": "rfq"},
                                                 {"_id": 0, "demo_run_id": 1}) or {}
        owner = rfq.get("demo_run_id")
        for entry in row.get("log") or []:  # oldest first
            if entry.get("kind") == "reused_by_run" and entry.get("run_id") and (entry.get("at") or "") <= at:
                owner = entry["run_id"]
    ref = run.get("ref") if owner == run.get("id") else \
        (await db.demo_runs.find_one({"id": owner}, {"_id": 0, "ref": 1}) or {}).get("ref") if owner else None
    return {"received_at": at, "source_run_ref": ref}


async def _collect(run_id: str, rows: list[dict], req: dict, extras: dict, reference: dict, until: float,
                   offers: dict, state: dict) -> None:
    """Act on every reply Huolitsija's inbound pipeline has processed for this run's partners until `until`."""
    ids = [r["id"] for r in rows]
    by_id = {r["id"]: r for r in rows}
    for row in rows:
        if row.get("status") == "email_failed":
            state.setdefault("final", {})[row["id"]] = "send_failed"
    facts = f"Route: {texts.place(req.get('origin'))} → {texts.place(req.get('destination'))}\nCargo: {texts.cargo_line(req, extras)}\n" \
            f"Ready: {req.get('ready_date')}\nPickup: {extras.get('pickup_address')}\nDelivery: {extras.get('delivery_address')}\n" \
            f"Incoterm: {req.get('incoterm')} {req.get('incoterm_place') or ''}"
    while time.monotonic() < until:
        run = await _run(run_id)
        if run.get("status") != "running":
            return
        msgs = await db.partner_messages.find({"direction": "in", "outreach_id": {"$in": ids}, "demo_seen": {"$ne": True},
                                               "status": {"$nin": ["received"]}}, {"_id": 0}).sort("seq", 1).to_list(50)
        for m in msgs:
            await db.partner_messages.update_one({"id": m["id"]}, {"$set": {"demo_seen": True}})
            row = by_id[m["outreach_id"]]
            node = f"p:{row['id']}"
            simulated = str(m.get("provider_id") or "").startswith("demo-sim:")
            reply = {"source": "world" if simulated else "real",  # world: the reply simulator wrote it, not the company
                     **await _reply_origin(m, row, run)}
            if simulated and await simulator.real_reply_exists(row["id"]):
                continue  # a real reply from this partner wins
            if not simulated:
                state.setdefault("real", set()).add(row["id"])
                if (offers.get(row["id"]) or {}).get("simulated"):
                    offers.pop(row["id"])  # the partner's own answer replaces the simulated rate, whatever it says
            p = m.get("parsed") or {}
            email = {"direction": "in", "from": m.get("from"), "to": conf.inbound_address(), "subject": m.get("subject"),
                     "text": m.get("text"), "message_id": m.get("message_id"), "partner_message_id": m["id"],
                     "status": m.get("status"), "simulated": simulated}
            tool = {"tool": "huolitsija.partner_parser.parse_reply", "input": f"{len(m.get('text') or '')} chars",
                    "output": f"intent {p.get('intent')}, {p.get('status')}"
                              + (f", {texts.eur(p.get('cost_cents'))}" if p.get("cost_cents") else "")
                              + (f", {p.get('transit_days')} d" if p.get("transit_days") else "")
                              + (f", valid to {p.get('valid_to')}" if p.get("valid_to") else ""),
                    "method": p.get("method")}
            if m.get("status") in ("opted_out",) or p.get("intent") in ("opt_out", "decline"):
                state.setdefault("final", {})[row["id"]] = "declined"
                await emit(run_id, node, "Declined", state="done", kind="email_in", edge=(node, "replies"), simulated=simulated,
                           data={"emails": [email], "tool_calls": [tool], "reply": reply})
                continue
            if p.get("status") == "ok" and p.get("cost_cents"):
                offer = {"outreach_id": row["id"], "partner": row["carrier_name"], "email": row["contact_email"],
                         "cost_cents": p["cost_cents"], "transit_days": p.get("transit_days"), "valid_to": p.get("valid_to"),
                         "base_cents": p.get("base_cents"), "surcharges": p.get("surcharges") or [], "intent": p.get("intent"),
                         "message_id": m.get("message_id"), "partner_message_id": m["id"], "simulated": simulated,
                         "role": row.get("role"), "country": row.get("country")}
                prev = offers.get(row["id"])
                if prev and not prev.get("transit_days"):
                    offer["transit_days"] = offer["transit_days"] or prev.get("transit_days")
                offers[row["id"]] = offer
                state.setdefault("final", {})[row["id"]] = "offer"
                title = ("Accepted our counteroffer: " if p.get("intent") == "accept_counter" else "Rate received: ") + \
                    f"{texts.eur(p['cost_cents'])}" + (f" · {texts.days(offer['transit_days'])}" if offer.get("transit_days") else "")
                await emit(run_id, node, title, state="done", kind="email_in", edge=(node, "replies"), simulated=simulated,
                           data={**_tk("partner.counter_accepted" if p.get("intent") == "accept_counter" else "partner.rate",
                                       amount=p["cost_cents"] / 100, days=offer.get("transit_days")),
                                 "emails": [email], "tool_calls": [tool], "reply": reply, "facts": {"all-in": texts.eur(p["cost_cents"]),
                                 "transit": texts.days(offer.get("transit_days")), "valid until": offer.get("valid_to") or "—"}})
                await emit(run_id, "replies", f"{len(offers)} rate{'s' if len(offers) != 1 else ''} in", state="active",
                           data=_tk("replies.count", n=len(offers)))
                continue
            # incomplete answer or a question: answer from the known facts and ask for the missing items (once)
            await emit(run_id, node, "Reply needs a follow-up", state="active", kind="email_in", edge=(node, "replies"),
                       simulated=simulated, data={"emails": [email], "tool_calls": [tool], "reply": reply})
            if len((await db.partner_outreach.find_one({"id": row["id"]}, {"_id": 0, "demo_followups": 1}) or {}).get("demo_followups") or []) >= 2:
                state.setdefault("final", {})[row["id"]] = "declined"
                continue
            body = await llm_text("demo_partner_followup", FOLLOW_UP_SYSTEM,
                                  f"Shipment facts:\n{facts}\n\nTheir reply:\n\"\"\"\n{(m.get('text') or '')[:3000]}\n\"\"\"\n"
                                  f"Still missing: {', '.join(p.get('missing') or ['all-in price'])}",
                                  max_tokens=500, ref={"type": "demo_run", "id": run_id, "outreach_id": row["id"]})
            await _follow_up(run_id, row, "clarify", body.strip(), reference, simulated_thread=simulated)
        if len(state.get("final", {})) >= len(rows) and not state.get("pending_counter"):
            return
        await asyncio.sleep(1.5)


def _valid_on(offer: dict, ready: str | None) -> bool:
    """Native pricing quotes a partner rate only on a day it is valid (pricing.card_valid_on): a rate that expires before
    the cargo is ready cannot carry this shipment, however cheap. A validity that cannot be read is not valid."""
    from services import pricing
    if not ready or not offer.get("valid_to"):
        return True
    try:
        return pricing.card_valid_on({"valid_to": str(offer["valid_to"])}, date.fromisoformat(str(ready)[:10]))
    except ValueError:
        return False


def _expired(offers: dict, ready: str | None) -> list[dict]:
    return sorted((o for o in offers.values() if o.get("cost_cents") and not _valid_on(o, ready)), key=lambda o: o["cost_cents"])


def _compared(o: dict) -> dict:
    return {**{k: o.get(k) for k in ("partner", "cost_cents", "transit_days", "valid_to", "simulated")},
            "reply_source": "world" if o.get("simulated") else "real",
            **({"agreed": True, "rate_card_id": o["agreed_card"]} if o.get("agreed_card") else {})}


async def _agreed_rates(run_id: str, rows: list[dict], req: dict, mode: str, offers: dict, state: dict) -> tuple[dict, set]:
    """A rerun of the same shipment takes over rate requests whose winning rate an earlier run's procurement already
    accepted as a native rate card. That card is a lane rate: it is used at its own terms (price, transit, validity,
    source message), never countered again and never rewritten, and only if native pricing quotes it for this very
    request (global_quote.preview: lane, service, cargo scope and validity on the ready date, pricing.price_card /
    card_valid_on). Otherwise the partner is left out like an expired rate, since procurement cannot take a second
    offer on that thread."""
    from services import global_quote
    carded = [r for r in rows if r.get("rate_card_id")]
    if not carded:
        return {}, set()
    try:
        pv = await global_quote.preview({**req, "mode": mode})
    except Exception:  # noqa: BLE001  a preview that fails prices no card
        pv = {}
    priced = set()
    for o in pv.get("options") or []:
        priced |= {o.get("rate_card_id")} | {lg.get("rate_card_id") for lg in o.get("legs") or []}
    agreed, skipped = {}, set()
    for row in carded:
        card = await db.rate_cards.find_one({"id": row["rate_card_id"]}, {"_id": 0}) or {}
        node = f"p:{row['id']}"
        why = ("its rate was not accepted by procurement" if card.get("status") != "accepted" or card.get("archived") else
               "the partner's real reply in this run replaces the accepted rate" if row["id"] in state.get("real", set()) else
               f"the accepted rate is valid until {card.get('valid_to')}, before the ready date {req.get('ready_date')}"
               if not _valid_on({"valid_to": card.get("valid_to")}, req.get("ready_date")) else
               "native pricing does not quote its accepted rate for this request (lane, service or cargo scope)"
               if card["id"] not in priced else None)
        if why:
            skipped.add(row["id"])
            await emit(run_id, node, "Agreed rate does not fit this request", state="done", kind="decision",
                       data={**_tk("partner.agreed_rate_skipped"), "reasoning": f"{row['carrier_name']}: {why}.",
                             "agreed": {"rate_card_id": card.get("id"), "valid_to": card.get("valid_to")}})
            continue
        mid = (card.get("source_detail") or {}).get("message_id")
        src = await db.partner_messages.find_one({"outreach_id": row["id"], "message_id": mid}, {"_id": 0, "provider_id": 1}) if mid else None
        simulated = str((src or {}).get("provider_id") or "").startswith("demo-sim:")
        agreed[row["id"]] = {"outreach_id": row["id"], "partner": row["carrier_name"], "email": row["contact_email"],
                             "cost_cents": card["all_in_cost_cents"], "transit_days": card.get("transit_days"),
                             "valid_to": card.get("valid_to"), "base_cents": None, "surcharges": [], "intent": "agreed",
                             "message_id": mid, "partner_message_id": None, "simulated": simulated,
                             "role": row.get("role"), "country": row.get("country"), "agreed_card": card["id"]}
        await emit(run_id, node, f"Rate already agreed: {texts.eur(card['all_in_cost_cents'])} · {texts.days(card.get('transit_days'))}",
                   state="done", kind="decision", edge=(node, "replies"), simulated=simulated,
                   data={**_tk("partner.rate_agreed", amount=card["all_in_cost_cents"] / 100, days=card.get("transit_days")),
                         "reply": {"source": "world" if simulated else "real"},
                         "agreed": {"rate_card_id": card["id"], "cost_cents": card["all_in_cost_cents"],
                                    "transit_days": card.get("transit_days"), "valid_to": card.get("valid_to"),
                                    "accepted_at": card.get("created_at"), "rfq_ref": (card.get("source_detail") or {}).get("ref")},
                         "reasoning": f"Huolitsija's procurement already accepted {row['carrier_name']}'s "
                                      f"{texts.eur(card['all_in_cost_cents'])} all-in as rate card {card['id'][:8]}, valid until "
                                      f"{card.get('valid_to')}; native pricing quotes it for this request, so it is used as agreed, "
                                      f"without a new counteroffer.",
                         "facts": {"all-in": texts.eur(card["all_in_cost_cents"]), "transit": texts.days(card.get("transit_days")),
                                   "valid until": card.get("valid_to") or "—"}})
    return agreed, skipped


def _counterable(best: dict, rows: list[dict], state: dict) -> bool:
    """Only a simulated rate on a simulated thread is countered: a real partner gets nothing beyond the rate request, and
    the thread of an accepted rate card is not negotiated again."""
    row = next(r for r in rows if r["id"] == best["outreach_id"])
    return bool(best.get("simulated")) and row["id"] not in state.get("real", set()) and not row.get("rate_card_id")


def _candidates(offers: dict, agreed: dict, skipped: set, state: dict) -> dict:
    """The one gate into ranking, applied after every collection round: real evidence wins. A partner's real reply
    (a rate, or a decline that _collect has already removed) supersedes any simulated or agreed card for that partner;
    an agreed card stands only while that partner has sent no real reply in this run, and in place of its simulated
    offers; a carded partner whose card does not fit is out."""
    real = state.get("real", set())
    out = {}
    for rid, o in offers.items():
        if rid in real:
            out[rid] = o
        elif rid in agreed:
            out[rid] = dict(agreed[rid])
        elif rid not in skipped:
            out[rid] = o
    out.update({rid: dict(o) for rid, o in agreed.items() if rid not in real and rid not in out
                and state.get("final", {}).get(rid) != "declined"})
    return out


async def _rate_review(run_id: str, pick: dict, ranked: list[dict], asked: int) -> None:
    """A real reply on a thread whose rate procurement already accepted beats that card, but procurement takes no second
    offer on the thread by itself: a person decides on the owner-queue card, with the comparison; nothing is sent."""
    from core.agent_runtime import agent_bot
    from core.approvals import request_approval
    comparison = [{**_compared(o), "outreach_id": o.get("outreach_id")} for o in ranked]
    summary = (f"{pick['partner']} replied {texts.eur(pick['cost_cents'])} after Huolitsija had accepted an earlier rate from "
               f"them; {len(ranked)} of {asked} partners quoted. A person decides which rate to buy.")
    card = await request_approval("partner_rate", "demo_run", run_id, "A real reply changes an accepted rate", summary=summary,
                                  reason="real_reply_after_acceptance", requested_by=agent_bot("quote_agent"),
                                  data={"demo_run_id": run_id, "outreach_id": pick["outreach_id"], "comparison": comparison})
    await db.demo_runs.update_one({"id": run_id, "status": "running"},
                                  {"$set": {"status": "awaiting_approval", "awaiting": ["partner rate after a real reply"],
                                            "rate_approval_id": card["id"], "updated_at": now_iso()}})
    await emit(run_id, "replies", "A real reply changes an accepted rate · a person decides", state="waiting", kind="decision",
               data={**_tk("replies.real_reply_after_agreed", partner=pick["partner"]), "approval_id": card["id"],
                     "reasoning": summary, "ranking": comparison})


def _replies(rows: list[dict], state: dict) -> list[dict]:
    """Where each partner's thread stands at the end of the rounds, for a person's card."""
    return [{"partner": r["carrier_name"], "outreach_id": r["id"], "reply": state.get("final", {}).get(r["id"]) or "no reply",
             "source": "real" if r["id"] in state.get("real", set()) else "world"} for r in rows]


async def _no_rate(run_id: str, rows: list[dict], state: dict) -> None:
    """The counter round left no partner rate at all, valid or not: every partner that had quoted has since declined (a
    real decline supersedes its earlier rate). This is not the rate-validity case: a person decides on the owner-queue
    card, with where each thread stands; nothing is sent to any partner and no quote is drafted."""
    from core.agent_runtime import agent_bot
    from core.approvals import request_approval
    replies = _replies(rows, state)
    declined = sum(1 for r in replies if r["reply"] == "declined")
    summary = (f"{declined} of {len(rows)} partners declined; the other {len(rows) - declined} have no usable rate for this "
               f"request. A person decides.")
    card = await request_approval("partner_rate", "demo_run", run_id, "No usable partner rate", summary=summary,
                                  reason="no_partner_rate", requested_by=agent_bot("quote_agent"),
                                  data={"demo_run_id": run_id, "replies": replies, "comparison": []})
    await db.demo_runs.update_one({"id": run_id, "status": "running"},
                                  {"$set": {"status": "awaiting_approval", "awaiting": ["a partner rate"],
                                            "rate_approval_id": card["id"], "updated_at": now_iso()}})
    await emit(run_id, "replies", "No usable partner rate · a person decides", state="waiting", kind="decision",
               data={**_tk("replies.no_rate", declined=declined, n=len(rows)), "approval_id": card["id"],
                     "reasoning": summary, "replies": replies, "ranking": []})


async def _no_valid_rate(run_id: str, ready: str | None, expired: list[dict], asked: int,
                         replies: list[dict] | None = None) -> None:
    """Every rate expires before the cargo is ready. Production has no validity-extension request to a partner
    (partner_network.compose knows rfq, counter, accept, decline and clarify), so a person decides on the owner-queue
    card with the comparison attached; nothing is sent to any partner and no quote is drafted."""
    from core.agent_runtime import agent_bot
    from core.approvals import request_approval
    best = expired[0]
    comparison = [{**_compared(o), "outreach_id": o.get("outreach_id")} for o in expired]
    summary = (f"{len(expired)} of {asked} partners quoted; every rate expires before the ready date {ready}. "
               f"Best: {best['partner']} {texts.eur(best['cost_cents'])}, valid until {best['valid_to']}.")
    card = await request_approval("partner_rate", "demo_run", run_id, "No valid rate for the ready date", summary=summary,
                                  reason="rate_validity", requested_by=agent_bot("quote_agent"),
                                  data={"demo_run_id": run_id, "ready_date": ready, "comparison": comparison,
                                        **({"replies": replies} if replies else {})})
    await db.demo_runs.update_one({"id": run_id, "status": "running"},
                                  {"$set": {"status": "awaiting_approval", "awaiting": ["partner rate valid on the ready date"],
                                            "rate_approval_id": card["id"], "updated_at": now_iso()}})
    await emit(run_id, "replies", "No valid rate for the ready date · a person decides", state="waiting", kind="decision",
               data={**_tk("replies.no_valid_rate", ready=ready), "approval_id": card["id"], "reasoning": summary,
                     "ranking": comparison})


def _rank(offers: dict, ready: str | None = None) -> list[dict]:
    vals = [o for o in offers.values() if o.get("cost_cents") and _valid_on(o, ready)]
    if not vals:
        return []
    fastest = min((o.get("transit_days") or 99) for o in vals)
    return sorted(vals, key=lambda o: o["cost_cents"] * (1 + 0.015 * max(0, (o.get("transit_days") or fastest + 3) - fastest)))


async def _reference(run_id: str, req: dict, mode: str) -> dict:
    """Huolitsija's own price for this request (global quoting preview): the market level the simulator and the
    fallback use. Its sources (index, as-of date, URL) stay in the trace."""
    from services import global_quote
    t0 = time.monotonic()
    try:
        pv = await global_quote.preview({**req, "mode": mode})
    except Exception as exc:  # noqa: BLE001
        await emit(run_id, "pricing", "Market reference unavailable", kind="tool",
                   data={"tool_calls": [{"tool": "huolitsija.global_quote.preview", "output": f"{type(exc).__name__}: {exc}"[:300]}]})
        return {}
    opts = [o for o in pv.get("options") or [] if o.get("mode") == mode] or pv.get("options") or []
    if not opts:
        return {}
    opt = next((o for o in opts if o.get("recommended")), opts[0])
    cost = sum(int(ln.get("unit_cost_cents") or 0) * float(ln.get("qty") or 1) for ln in opt.get("lines") or [])
    cost = int(opt.get("cost_cents") or cost or round((opt.get("total_cents") or 0) * 0.82))
    sources = []
    for ln in opt.get("lines") or []:
        src = ln.get("source") or {}
        if src.get("url") or src.get("index"):
            sources.append({"label": f"{ln.get('description')}: {src.get('index') or src.get('kind')} {src.get('as_of') or ''}".strip(),
                            "url": src.get("url")})
    ref = {"cost_cents": cost, "total_cents": opt.get("total_cents"), "transit_days": opt.get("transit_days"),
           "service": opt.get("service"), "option": opt, "valid_until": pv.get("valid_until"), "sources": sources[:8]}
    await emit(run_id, "pricing", "Market reference priced", kind="tool",
               data={"tool_calls": [{"tool": "huolitsija.global_quote.preview", "input": f"{mode} {opt.get('service')}",
                                     "output": f"cost {texts.eur(cost)}, sell {texts.eur(opt.get('total_cents'))}, "
                                               f"{opt.get('transit_days')} days, {opt.get('firmness')}",
                                     "latency_ms": int((time.monotonic() - t0) * 1000)}], "sources": sources[:8]})
    return ref


# ---------------------------------------------------------------- pricing + offer

async def _price(run_id: str, req: dict, mode: str, pick: dict | None, reference: dict) -> dict:
    s = await get_settings()
    margin = float(s.get("default_margin_pct") or 18.0)
    opt = reference.get("option") or {}
    extra_lines = [ln for ln in opt.get("lines") or [] if ln.get("code") in ("DOC_FEE",) or str(ln.get("stage")) == "customs"]
    lines = []
    if pick:
        sell = int(math.ceil(pick["cost_cents"] * (1 + margin / 100) / 500.0) * 500)
        main = next((ln for ln in opt.get("lines") or [] if ln.get("stage") == "main"), {})
        intl = req["origin"].get("country") != req["destination"].get("country")
        lines.append({"code": "FREIGHT", "description": f"{mode.capitalize()} freight {req['origin']['name']} → {req['destination']['name']}, door to door",
                      "basis": "PER_SHIPMENT", "qty": 1, "unit_cost_cents": pick["cost_cents"], "unit_price_cents": sell,
                      "amount_cents": sell, "stage": "main", "is_pass_through": False,
                      "vat_code": main.get("vat_code") or ("ZERO_INTL_TRANSPORT" if intl else "FI_25_5"),
                      "vat_rate_pct": main.get("vat_rate_pct") if main.get("vat_code") else (0.0 if intl else float(s.get("vat_rate_standard_pct", 25.5))),
                      "payer": main.get("payer") or "SHIPPER"})
        transit = pick.get("transit_days") or reference.get("transit_days")
        valid = min(pick.get("valid_to") or "9999", (date.today() + timedelta(days=14)).isoformat())
        basis = "partner_offer"
    else:
        sell_lines = [ln for ln in opt.get("lines") or [] if ln not in extra_lines]
        for ln in sell_lines:
            lines.append({k: ln.get(k) for k in LINE_KEYS})
        transit = reference.get("transit_days")
        valid = (reference.get("valid_until") or (date.today() + timedelta(days=14)).isoformat())[:10]
        basis = "market_reference"
    for ln in extra_lines:
        lines.append({k: ln.get(k) for k in LINE_KEYS})
    for ln in lines:
        ln["cost_cents"] = int((ln.get("unit_cost_cents") or 0) * float(ln.get("qty") or 1))
        ln["vat_cents"] = int(round((ln.get("amount_cents") or 0) * float(ln.get("vat_rate_pct") or 0) / 100))
    total = sum(int(ln.get("amount_cents") or 0) for ln in lines)
    cost = sum(ln["cost_cents"] for ln in lines)
    vat = sum(ln["vat_cents"] for ln in lines)
    return {"mode": mode, "lines": lines, "sell_cents": total, "cost_cents": cost, "margin_cents": total - cost, "vat_cents": vat,
            "margin_pct": margin, "transit_days": transit, "valid_until": valid, "basis": basis,
            **{k: opt.get(k) for k in ("co2e_kg", "liability_regime", "liability") if opt.get(k) is not None}}


async def _native_partner_outreach(req: dict, mode: str, row: dict) -> dict:
    """Keep researched demo RFQs in the production candidate/service/cargo contract."""
    from services import partner_discovery, pricing
    cid = row.get('candidate_id') or partner_discovery.candidate_id(row.get('business_id'),row.get('domain'),row['carrier_name'])
    await db.partner_candidates.update_one({'id':cid},{'$setOnInsert':{
        'id':cid,'name':row['carrier_name'],'domain':row.get('domain'),'country':row.get('country'),
        'contact_email':row['contact_email'],'contact_source_url':row.get('contact_source_url'),
        'sources':row.get('sources') or [],'created_at':now_iso(),'status':'contacted'}},upsert=True)
    cargo = pricing.normalize_cargo(req.get('cargo') or {})
    metrics = pricing.cargo_metrics(cargo)
    services, _ = pricing.eligible_services(metrics,mode,cargo)
    preferences = {'road':('ROAD_LTL','ROAD_FTL'),'sea':('FCL','LCL'),
                   'air':('AIR','COURIER'),'rail':('RAIL_FCL','RAIL_WAGON')}
    service = next((v for v in preferences[mode] if v in services),None)
    if not service:
        raise ValueError('No native service carries the quoted cargo')
    # The demo requested one all-in consignment, not a per-kg tariff. Preserve
    # that literal basis; the native pricing engine supplies its leg cost lines.
    import hashlib
    import json
    scoped_cargo = {**cargo,'origin_location':req['origin'],'destination_location':req['destination']}
    scope = hashlib.sha256(json.dumps(scoped_cargo,sort_keys=True).encode()).hexdigest()
    adapted = {'candidate_id':cid,'service':service,'basis':'PER_SHIPMENT',
               'request_scope':scope,'cargo':scoped_cargo}
    row = {**row,**adapted}
    return row


async def _promote_partner_rate(req: dict, mode: str, pick: dict) -> dict:
    """Adapt the demo RFQ to production procurement; its policy decides acceptance.

    Only the researched partner and its actual parsed offer are supplied here.
    Carrier creation, rate validation and acceptance remain partner_network's work.
    """
    row = await db.partner_outreach.find_one({'id':pick['outreach_id']}, {'_id':0})
    if not row or row.get('verification_only') or row.get('test_mailbox'):
        raise ValueError('The winning RFQ is not an operational transport partner')
    if row.get('rate_card_id'):
        card = await db.rate_cards.find_one({'id':row['rate_card_id']}, {'_id':0})
        if card and card.get('status') == 'accepted' and not card.get('archived'):
            if any(card.get(k) != pick.get(v) for k,v in
                   (('all_in_cost_cents','cost_cents'),('transit_days','transit_days'),('valid_to','valid_to'))) \
                    or (card.get('source_detail') or {}).get('message_id') != pick.get('message_id'):
                raise ValueError('Native procurement already accepted a different offer; review the winning RFQ')
            return card
        raise ValueError('The partner rate has not been accepted by procurement')
    row = await _native_partner_outreach(req,mode,row)
    await db.partner_outreach.update_one({'id':row['id']},{'$set':{k:row[k] for k in
        ('candidate_id','service','basis','request_scope','cargo')}})
    from services.demo_world import boundary
    async with boundary.selected_partner_offer(row['demo_run_id'],row['id']):
        card = await pn.process_offer(row,{k:pick.get(k) for k in
            ('cost_cents','transit_days','valid_to','message_id')})
    if card.get('status') != 'accepted' or not card.get('carrier_id') or card.get('archived'):
        raise ValueError('Procurement did not accept the winning partner rate')
    return card


async def _store_quote(run_id: str, req: dict, mode: str, priced: dict, pick: dict | None, summary: str) -> tuple[dict, dict]:
    from services import global_quote
    _thread, user, company = await _ensure_thread(run_id)
    card = await _promote_partner_rate(req,mode,pick) if pick else None
    ai = {"agent": "autonomous_quote", "summary": summary, "demo_run_id": run_id}
    # The production quote builder reads the newly accepted card. Never replace
    # its legs, carrier IDs, rate-card IDs, cost allocation or bookability.
    q = await global_quote.quote_any({**req, "mode": mode}, company=company, user=user, source="ai", send=False,
                                     notify_email=conf.requester_address(await _run(run_id)), locale=(await _run(run_id)).get("locale") or "en", ai=ai)
    options = q.get('options') or []
    matching = [o for o in options if not card or o.get('rate_card_id') == card['id']
                or any(lg.get('rate_card_id') == card['id'] for lg in o.get('legs') or [])]
    option = next((o for o in matching if o.get('recommended')),next(iter(matching),None))
    if not option:
        raise ValueError('Native quoting produced no option from the winning partner rate')
    option = {**option,'service_name':f'Huolitsija door-to-door {mode} freight','white_label':True,'recommended':True,
        'price_basis':priced['basis'],
        'rank_reason':'Best partner offer from the rate requests' if pick else 'Huolitsija market rate',
        'source':{'kind':priced['basis'],**({k:pick.get(k) for k in
            ('outreach_id','message_id','simulated','partner')} if pick else {})}}
    # Every customer surface must show this very same native price and terms.
    priced.update(lines=option['lines'],sell_cents=option['total_cents'],cost_cents=option['cost_cents'],
        margin_cents=option['margin_cents'],transit_days=option['transit_days'],
        valid_until=option.get('valid_until') or q['valid_until'],vat_cents=option.get('vat_cents',0),
        margin_pct=option.get('markup_pct',priced.get('margin_pct')))
    priced.update({k:option[k] for k in ('co2e_kg','liability_regime','liability') if k in option})
    await db.quotes.update_one({'id':q['id']},{'$set':{'options':[option],
        'alternatives':[{**o,'recommended':False} for o in options if o['id'] != option['id']],
        'demo_run_id':run_id}})
    return await _send_gate(run_id, await db.quotes.find_one({'id':q['id']},{'_id':0})), company


async def _send_gate(run_id: str, q: dict) -> dict:
    """Production's own agent path for sending a quote: the usual quote_threshold card, decided by the quote.autosend
    job's quote agent (Autopilot switch, kill switch, quote policy, margin floor: one audited decision). A card the agent
    may not decide stays in the owner queue for a person. Never apply_approval_policy(send=True): that is a person
    clicking Send. Returns the quote once the decision has landed (status sent, or still pending_approval)."""
    from core.agent_runtime import agent_bot
    from core.approvals import request_approval
    from routes.quotes import APPROVAL_REASON_TEXT, approval_reasons, route_label
    from services.agents import quote_agent
    reasons = approval_reasons(q, await get_settings())
    amount = max([o.get("total_cents") or 0 for o in q.get("options") or []] or [0]) or None
    label = f"Quote {q['number']} · {route_label(q['request'])}"
    a = await request_approval("quote_threshold", "quote", q["id"], label, company_id=q.get("company_id"),
                               summary=f"{q.get('customer_name') or 'Customer'} · {q['request'].get('cargo_summary') or ''}".strip(" ·"),
                               reason=", ".join(APPROVAL_REASON_TEXT.get(r, r) for r in reasons) or "Sent by the quote agent",
                               requested_by=agent_bot("quote_agent"), data={"reasons": reasons, "demo_run_id": run_id},
                               amount_cents=amount, link=f"/ops/quotes/{q['id']}", extra={"title_fi": f"Tarjous {q['number']}"})
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"status": "pending_approval", "updated_at": now_iso(), "approval": {
        "required": True, "reasons": reasons, "reason": reasons[0] if reasons else None, "approval_id": a["id"],
        "status": "pending", "amount_cents": amount, "requested_at": now_iso()}}})
    try:
        await quote_agent.autosend({"payload": {"approval_id": a["id"]}})  # the job's own code; the sweeper retries it
    except Exception as exc:  # noqa: BLE001 - the card stays pending for the sweeper or a person
        log.warning("quote agent decision failed: %s", exc)
    for _ in range(20):  # the approval.decided handler marks the quote sent
        cur = await db.quotes.find_one({"id": q["id"]}, {"_id": 0})
        if cur["status"] != "pending_approval" or (await db.approvals.find_one({"id": a["id"]}, {"_id": 0, "status": 1}))["status"] == "pending":
            break
        await asyncio.sleep(0.5)
    return cur


async def _send_decision(q: dict) -> dict:
    """Who released the offer, from the records themselves: the card and the quote agent's latest decision on it."""
    a = await db.approvals.find_one({"id": (q.get("approval") or {}).get("approval_id")}, {"_id": 0}) or {}
    d = await db.agent_decisions.find_one({"agent": "quote_agent", "action": "send_quote", "subject_id": q["id"]}, {"_id": 0},
                                          sort=[("at", -1)]) or {}
    return {"approval_id": a.get("id"), "card_status": a.get("status"), "decided_by": (a.get("decided_by") or {}).get("name"),
            "decided_by_id": (a.get("decided_by") or {}).get("id"),
            "decision_id": d.get("id"), "decision_outcome": d.get("outcome"),
            "decision_reasons": [r.get("en") or r.get("code") for r in d.get("reasons") or []],
            # stable codes for the map's own EN/FI wording; the card's reasons when the agent has not decided yet
            "reason_codes": [r["code"] for r in d.get("reasons") or [] if r.get("code")]
                            or list((q.get("approval") or {}).get("reasons") or [])}


async def _await_release(run_id: str, q: dict) -> dict | None:
    """The offer goes out only when its card is approved (quote status sent): by the quote agent's audited decision, or
    by a person in the owner queue while the run waits. Otherwise the run stops at awaiting approval."""
    waited = False
    deadline = time.monotonic() + conf.approval_wait_s()
    while True:
        q = await db.quotes.find_one({"id": q["id"]}, {"_id": 0})
        info = await _send_decision(q)
        if q["status"] == "sent" and info["card_status"] == "approved":
            if waited:
                await db.demo_runs.update_one({"id": run_id, "status": "awaiting_approval"}, {"$set": {"status": "running"}})
                await emit(run_id, "offer", f"Approved by {info['decided_by'] or 'the owner'}", state="active", kind="decision",
                           data={"send_decision": info, **_tk("offer.approved", name=info["decided_by"] or "the owner")})
            return {**q, "send_decision": info}
        if q["status"] in ("rejected", "expired") or info["card_status"] in ("rejected", "cancelled"):
            await db.demo_runs.update_one({"id": run_id}, {"$set": {"status": "stopped", "updated_at": now_iso()}})
            await emit(run_id, "offer", f"Offer {q['number']} not approved", state="done", kind="decision",
                       data={"send_decision": info, **_tk("offer.not_approved", quote=q["number"])})
            return None
        if not waited:
            waited = True
            await db.demo_runs.update_one({"id": run_id}, {"$set": {"status": "awaiting_approval", "quote_id": q["id"],
                                                                   "quote_number": q["number"], "updated_at": now_iso()}})
            await emit(run_id, "offer", f"Offer {q['number']} awaiting approval", state="waiting", kind="decision",
                       data={**_tk("offer.awaiting_approval", quote=q["number"]), "facts": {"quote": q["number"], "review": ", ".join(info["decision_reasons"]) or "owner queue"},
                             "send_decision": info, "reasoning": "The quote agent may not send this offer on its own; "
                                                                 "it waits on the owner-queue card for a person."})
        if time.monotonic() > deadline or (await _run(run_id)).get("status") in ("stopped", "failed"):
            return None
        await asyncio.sleep(3)


async def _quote_pdf(q: dict, company: dict) -> tuple[bytes | None, str | None]:
    try:
        from core.storage import put_file
        from services.quote_pdf import render_quote_pdf
        pdf = await render_quote_pdf({**q, "options": [o for o in q["options"] if o.get("recommended")]}, company, lang="en")
        fid = await put_file(pdf, f"{q['number']}.pdf", "application/pdf", {"kind": "quote_pdf", "quote_id": q["id"]})
        return pdf, fid
    except Exception as exc:  # noqa: BLE001 - the e-mail still carries every term
        log.warning("quote PDF failed: %s", exc)
        return None, None


# ---------------------------------------------------------------- the phase

async def quote_phase(run_id: str) -> None:
    run = await _run(run_id)
    pending_ids = run.get("pending_requester_ids")
    resumed = bool(pending_ids)
    checkpoint = run.get("clarification_checkpoint") if resumed else None
    text = run.get("clarification_text") or run["request_text"]
    if checkpoint:
        parsed, extras, missing = checkpoint["parsed"], checkpoint["extras"], checkpoint["missing"]
        mode, why_mode = checkpoint["mode"], checkpoint["why_mode"]
    elif resumed:
        # Legacy waits have no parsed checkpoint. Their persisted lane is enough
        # to consume the existing question's reply FIRST; parse combined text
        # below rather than re-running intake on the original request.
        lane = run.get("lane") or {}
        if not lane.get("origin") or not lane.get("destination") or not lane.get("mode"):
            raise ValueError("Requester wait is missing its persisted lane")
        parsed = {"request": {"origin": lane["origin"], "destination": lane["destination"]}}
        extras, missing = {}, run.get("awaiting") or []
        mode, why_mode = lane["mode"], "Persisted clarification lane"
    else:
        await emit(run_id, "requester", "Quote request typed", state="done", edge=("requester", "intake"),
               data={"facts": {"from": run["origin_text"], "to": run["destination_text"], "autonomy": "fully autonomous"},
                     "emails": [{"direction": "in", "from": "requester", "subject": "Quote request", "text": run["request_text"]}]})
        await emit(run_id, "intake", "Reading the request", state="active")
        parsed, extras, tool = await _parse(run_id, text)
        missing = _missing(parsed, extras)
        mode, why_mode = _mode(parsed["request"], (parsed["request"].get("cargo") or {}).get("weight_kg"))
    req = parsed["request"]
    if not req.get("origin") or not req.get("destination"):
        raise ValueError("The origin or destination city was not recognised")
    await db.demo_runs.update_one({"id": run_id}, {"$set": {"lane": {"origin": req["origin"], "destination": req["destination"], "mode": mode}}})
    if not resumed:
        await emit(run_id, "intake", f"Request understood · {len(missing)} detail{'s' if len(missing) != 1 else ''} missing", state="done",
               data={**tool, **_tk("intake.understood", n=len(missing)), "facts": {"lane": f"{texts.place(req['origin'])} → {texts.place(req['destination'])}", "mode": f"{mode} ({why_mode})",
                                       "missing": ", ".join(missing) or "none"},
                     "reasoning": parsed.get("summary") or ""})
    reuse = run.get("reuse_from")  # the same shipment again: the halted run's partners and replies carry over
    research_task = None if reuse or run.get("real_requester") else asyncio.get_running_loop().create_task(_research(run_id, req, mode))

    round_no = int(run.get("clarification_round") or 0)
    while (missing or pending_ids) and (round_no < 2 or run.get("real_requester") or pending_ids):
        round_no += 1
        if pending_ids:
            ids, pending_ids = pending_ids, None
            question_subject = (checkpoint or {}).get("question_subject")
        else:
            await emit(run_id, "clarify", "Writing questions to the requester", state="active", edge=("intake", "clarify") if round_no == 1 else None)
            question_subject, ids = await _clarify(run_id, parsed, extras, missing, round_no)
        await db.demo_runs.update_one({"id": run_id}, {"$set": {"pending_requester_ids": ids,
            "clarification_text": text, "clarification_round": round_no - 1,
            **({"clarification_checkpoint": {"parsed": parsed, "extras": extras, "missing": missing,
                "mode": mode, "why_mode": why_mode, "question_subject": question_subject}} if not resumed or checkpoint else {})}})
        reply = await _await_requester(run_id, ids)
        if reply is None:
            if run.get("real_requester"):
                if research_task:
                    research_task.cancel()
                await db.demo_runs.update_one({"id": run_id, "status": "running"}, {"$set": {
                    "status": "awaiting_requester", "awaiting": missing, "updated_at": now_iso()}})
                await emit(run_id, "clarify", "Awaiting your reply", state="waiting")
                return
            if _essential_missing(parsed, extras, mode):
                break  # nothing to price yet: handled below
            await emit(run_id, "clarify", "No answer yet · continuing with what is known", state="done")
            break
        await emit(run_id, "clarify", "Reply received · reading it", state="active", kind="email_in", edge=("requester", "clarify"),
                   data={"emails": [{"direction": "in", "from": reply.get("from_email"), "to": conf.inbound_address(),
                                     "subject": reply.get("subject"), "text": (reply.get("text") or "")[:6000],
                                     "message_id": reply.get("message_id"), "email_message_id": reply.get("id"),
                                     "simulated": str(reply.get("provider_id") or reply.get("source") or "").startswith("demo")}]})
        text = f"{text}\n\nCustomer's answers:\n{reply.get('text') or ''}"
        await db.demo_runs.update_one({"id": run_id}, {"$set": {"clarification_text": text,
            "clarification_round": round_no}, "$unset": {"pending_requester_ids": ""}})
        parsed, extras, tool = await _parse(run_id, text)
        req = parsed["request"]
        all_missing = _missing(parsed, extras)
        missing = [m for m in all_missing if m not in ("dangerous_goods",) or round_no < 1]
        missing = [m for m in missing if m in ("weight_kg", "cargo.packages", "volume_m3", "ready_date", "pickup_address", "delivery_address")]
        checkpoint = {"parsed": parsed, "extras": extras, "missing": missing, "all_missing": all_missing,
            "mode": mode, "why_mode": why_mode, "question_subject": question_subject,
            "reply": {"id": reply["id"], "message_id": reply.get("message_id"), "subject": reply.get("subject")}}
        await db.demo_runs.update_one({"id": run_id}, {"$set": {"clarification_checkpoint": checkpoint}})
        last_round = not missing or round_no >= 2
        await emit(run_id, "clarify", "Details complete" if not missing else
                   f"{len(missing)} still open · continuing with what is known" if last_round else f"{len(missing)} still open",
                   state="done" if last_round else "active",
                   data={**tool, "facts": {"cargo": texts.cargo_line(req, extras), "ready": req.get("ready_date") or "—",
                                           "incoterm": f"{req.get('incoterm') or '—'} {req.get('incoterm_place') or ''}".strip(),
                                           "pickup": extras.get("pickup_address") or "—", "delivery": extras.get("delivery_address") or "—"}})
        if not all_missing and run.get("real_requester"):
            from .clarification_ack import complete
            await complete(run_id, round_no)
        if missing and round_no >= 2 and not run.get("real_requester"):
            break
    still = _essential_missing(parsed, extras, mode)
    if still:  # no partner is asked for a rate on a shipment Huolitsija cannot describe
        if research_task:
            research_task.cancel()
        await db.demo_runs.update_one({"id": run_id, "status": "running"}, {"$set": {"status": "awaiting_requester",
                                                                                    "awaiting": still, "updated_at": now_iso()}})
        await emit(run_id, "clarify", "Awaiting the requester's reply", state="waiting",
                   data={"facts": {"needed before sourcing": ", ".join(still)}})
        return
    req = _complete(req)
    if not req.get("ready_date"):
        d = date.today() + timedelta(days=3)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        req["ready_date"] = d.isoformat()
    await db.demo_runs.update_one({"id": run_id}, {"$set": {"request": req, "extras": extras}})

    if not reuse and research_task is None:
        research_task = asyncio.get_running_loop().create_task(_research(run_id, req, mode))
    reference = await _reference(run_id, req, mode)
    if reuse:
        rows = await _reuse_outreach(run_id, reuse)
    else:
        partners = await research_task
        await emit(run_id, "outreach", f"Sending {len(partners)} rate requests", state="active", edge=("research", "outreach"),
                   data={**_tk("outreach.sending", n=len(partners)), "facts": {"shipment": texts.cargo_line(req, extras)}})
        await emit(run_id, "outreach", "Shipment details attached", kind="step", edge=("clarify", "outreach"))
        rows = []
        for cand in partners:
            row = await _send_rfq(run_id, cand, req, extras, mode, reference)
            if row:
                rows.append(row)
        await db.demo_runs.update_one({"id": run_id}, {"$set": {"partners": [r["id"] for r in rows]}})
        sent_count = sum(row.get("status") == "demo_awaiting_reply" for row in rows)
        failed_count = sum(row.get("status") == "email_failed" for row in rows)
        pending_count = len(rows) - sent_count - failed_count
        title = f"{sent_count} rate requests sent"
        if failed_count or pending_count:
            title += f" · {failed_count} failed · {pending_count} pending"
        await emit(run_id, "outreach", title, state="failed" if failed_count and not sent_count and not pending_count else "done",
                   data={**(_tk("outreach.sent", n=sent_count) if not failed_count and not pending_count else {}),
                         "facts": {"partners reached": sent_count, "failed": failed_count, "pending": pending_count}})
    await emit(run_id, "replies", "Awaiting replies", state="waiting")

    offers: dict = {}
    state: dict = {}
    await _collect(run_id, rows, req, extras, reference, time.monotonic() + conf.reply_window_s(), offers, state)
    agreed, skipped = await _agreed_rates(run_id, rows, req, mode, offers, state)

    ready = req.get("ready_date")
    pool = lambda: _candidates(offers, agreed, skipped, state)  # noqa: E731  the one gate into ranking, every round
    ranked = _rank(pool(), ready)
    expired = _expired(pool(), ready)
    if expired:
        await emit(run_id, "replies", f"{len(expired)} rate{'s' if len(expired) != 1 else ''} not valid on {ready}", state="active",
                   kind="decision", data={**_tk("replies.expired", n=len(expired), ready=ready),
                                          "reasoning": "; ".join(f"{o['partner']} {texts.eur(o['cost_cents'])} valid until {o['valid_to']}"
                                                                 for o in expired)
                                                       + f". The cargo is ready on {ready}; Huolitsija quotes only a rate valid that day.",
                                          "expired": [_compared(o) for o in expired]})
    if not ranked and expired:
        await _no_valid_rate(run_id, ready, expired, len(rows), _replies(rows, state))
        return
    if ranked and ranked[0].get("agreed_card"):  # an accepted rate card is never countered again
        best = ranked[0]
        await emit(run_id, "replies", f"Comparing {len(ranked)} rates · best rate already agreed", state="active", kind="decision",
                   data={**_tk("replies.best_agreed", n=len(ranked), partner=best["partner"]),
                         "reasoning": f"{best['partner']}'s {texts.eur(best['cost_cents'])} all-in is already an accepted rate card "
                                      f"({best['agreed_card'][:8]}) and is the best valid rate"
                                      + (f"; next {ranked[1]['partner']} at {texts.eur(ranked[1]['cost_cents'])}" if len(ranked) > 1 else "")
                                      + ". No counteroffer."})
    elif len(ranked) >= 2 and _counterable(ranked[0], rows, state):
        best = ranked[0]
        row = next(r for r in rows if r["id"] == best["outreach_id"])
        counter = int(math.floor(best["cost_cents"] * 0.95 / 1000.0) * 1000)
        await emit(run_id, "replies", f"Comparing {len(ranked)} rates · countering the best", state="active",
                   kind="decision", data={**_tk("replies.comparing", n=len(ranked)), "reasoning": f"{best['partner']} is the best rate at {texts.eur(best['cost_cents'])}; "
                                                       f"next {ranked[1]['partner']} at {texts.eur(ranked[1]['cost_cents'])}. "
                                                       f"Asking {best['partner']} for {texts.eur(counter)} all-in."})
        state["pending_counter"] = row["id"]
        state.get("final", {}).pop(row["id"], None)
        await _follow_up(run_id, row, "counteroffer", "", reference, simulated_thread=best["simulated"] and row["id"] not in state.get("real", set()),
                         counter_cents=counter, last_offer=best)
        deadline = time.monotonic() + 75
        before = dict(best)
        while time.monotonic() < deadline:
            await _collect(run_id, rows, req, extras, reference, min(deadline, time.monotonic() + 3), offers, state)
            cur = offers.get(row["id"]) or {}
            if cur.get("message_id") != before.get("message_id"):
                break
        state.pop("pending_counter", None)
        if (offers.get(row["id"]) or {}).get("message_id") == before.get("message_id"):
            await emit(run_id, f"p:{row['id']}", f"Rate stands at {texts.eur(before['cost_cents'])}", state="done",
                       data=_tk("partner.rate_stands", amount=before["cost_cents"] / 100))
        ranked = _rank(pool(), ready)  # real replies of this round win here too
        if not ranked:  # the round left no valid rate: the answers expire before the ready date, or every partner declined
            if late := _expired(pool(), ready):
                await _no_valid_rate(run_id, ready, late, len(rows), _replies(rows, state))
            else:
                await _no_rate(run_id, rows, state)
            return

    pick = ranked[0] if ranked else None
    if pick and not pick.get("agreed_card") and any(r["id"] == pick["outreach_id"] and r.get("rate_card_id") for r in rows):
        await _rate_review(run_id, pick, ranked, len(rows))  # a real reply beat an accepted card: a person decides
        return
    waiting = [r for r in rows if r["id"] not in state.get("final", {})]
    if pick:
        nxt = ranked[1] if len(ranked) > 1 else None
        reasoning = (f"Picked {pick['partner']}: {texts.eur(pick['cost_cents'])} all-in, {pick.get('transit_days') or '—'} days, "
                     f"valid until {pick.get('valid_to') or '—'}." + (f" Next best {nxt['partner']} at {texts.eur(nxt['cost_cents'])}." if nxt else "")
                     + f" {len(ranked)} of {len(rows)} partners quoted; {len(waiting)} awaiting reply.")
        await emit(run_id, "replies", f"Best rate: {pick['partner']}", state="done", kind="decision", edge=("replies", "pricing"),
                   data={**_tk("replies.best", partner=pick["partner"]), "reasoning": reasoning,
                         "reply": {"source": "world" if pick.get("simulated") else "real"},
                         "ranking": [_compared(o) for o in ranked],
                         **({"expired": [_compared(o) for o in late]} if (late := _expired(pool(), ready)) else {})})
    else:
        # production's designed fallback; the offer still passes the native card and the quote agent's audited decision
        await emit(run_id, "replies", "Priced from market rates · no partner rate", state="done", kind="decision", edge=("replies", "pricing"),
                   data={**_tk("replies.market_rates"), "price_basis": "market_reference",
                         "reasoning": f"No complete rate yet from {len(rows)} partners; the offer uses Huolitsija's own lane pricing.",
                         "sources": reference.get("sources") or []})
    for r in waiting:
        await emit(run_id, f"p:{r['id']}", "Awaiting reply", state="waiting")

    await emit(run_id, "pricing", "Pricing the offer", state="active")
    priced = await _price(run_id, req, mode, pick, reference)
    summary = f"{texts.place(req['origin'])} → {texts.place(req['destination'])}, {mode}, {texts.cargo_line(req, extras)}"
    q, company = await _store_quote(run_id, req, mode, priced, pick, summary)
    await emit(run_id, "pricing", f"{texts.eur(priced['sell_cents'])} · {texts.days(priced['transit_days'])}", state="done", edge=("pricing", "offer"),
               data={**_tk("pricing.priced", amount=priced["sell_cents"] / 100, days=priced["transit_days"]), "price_basis": priced["basis"],
                     **({"reply": {"source": "world" if pick.get("simulated") else "real"}} if pick else {}), "facts": {"quote": q["number"], "cost": texts.eur(priced["cost_cents"]), "sell": texts.eur(priced["sell_cents"]),
                               "margin": f"{texts.eur(priced['margin_cents'])} ({priced['margin_pct']:.0f}% on freight)",
                               "valid until": priced["valid_until"]},
                     "lines": priced["lines"], "sources": reference.get("sources") or [],
                     "reasoning": (f"Partner cost {texts.eur(pick['cost_cents'])} from {pick['partner']}"
                                   + (" (simulated reply)" if pick.get("simulated") else "") + f" plus {priced['margin_pct']:.0f}% margin, "
                                   "plus Huolitsija's documentation fee." if pick else
                                   "Huolitsija's lane pricing from its benchmark sources (listed) plus its documentation fee.")})

    q = await _await_release(run_id, q)
    if q is None:  # not released: the card waits in the owner queue (or was declined); nothing is e-mailed
        return
    info = q.pop("send_decision")
    await emit(run_id, "offer", f"Offer {q['number']} drafted", state="active",
               data={**_tk("offer.drafted", quote=q["number"]), "send_decision": info, "reasoning": "Released on its owner-queue card by "
                     + ("the quote agent's audited decision inside the Autopilot quote policy." if info["decision_outcome"] == "auto"
                        and info["decided_by_id"] == "agent:quote_agent" else f"{info['decided_by']} in the owner queue.")})
    pdf, file_id = await _quote_pdf(q, company)
    subject, lines, title = texts.offer(run, q, req, extras, priced)
    from services.email_channel import store
    from services.quote_pdf import public_terms_url
    thread, _user, _company = await _ensure_thread(run_id)
    thread = await store.update_thread(thread["id"], ref=q["number"], quote_id=q["id"], quote_number=q["number"], state="quoted")
    lines = lines[:-1] + texts.book_lines(public_terms_url()) + lines[-1:]  # a reply BOOK runs through the channel's handle_book
    rec = await _send_thread(run_id, thread, title, lines, tag="email_quote", subject=subject,
                             attachments=[(f"{q['number']}.pdf", "application/pdf", pdf)] if pdf else None)
    subject = rec.get("subject") or subject
    offer = {"quote_id": q["id"], "quote_number": q["number"], "sell_cents": priced["sell_cents"], "cost_cents": priced["cost_cents"],
             "transit_days": priced["transit_days"], "valid_until": priced["valid_until"], "option": q["options"][0],
             "price_basis": priced["basis"],
             "partner": ({"outreach_id": pick["outreach_id"], "name": pick["partner"], "email": pick["email"],
                          "simulated": pick.get("simulated"), "sandbox": bool(conf.partner_sandbox())} if pick else None),
             "message_id": (rec["message_ids"] or [None])[0], "message_ids": rec["message_ids"], "outbox_id": rec.get("id"),
             "email_message_id": rec.get("email_message_id"), "thread_id": thread["id"], "sent_at": now_iso(), "file_id": file_id}
    await db.demo_runs.update_one({"id": run_id}, {"$set": {"status": "offer_sent", "quote_id": q["id"], "quote_number": q["number"],
                                                           "offer": offer}})
    await emit(run_id, "offer", f"Offer sent · {texts.eur(priced['sell_cents'])}", state="done", kind="email_out", edge=("offer", "requester"),
               data={**_tk("offer.sent", amount=priced["sell_cents"] / 100), "price_basis": priced["basis"], "emails": [{"direction": "out", "from": conf.sender(), "to": conf.requester_address(run), "subject": subject,
                                 "text": rec.get("text"), "message_id": offer["message_id"], "outbox_id": rec.get("id"),
                                 "provider": rec.get("provider"), "status": rec.get("status")}],
                     "documents": [{"id": file_id, "title": f"Quote {q['number']}", "kind": "quote_pdf", "file_id": file_id,
                                    "filename": f"{q['number']}.pdf", "content_type": "application/pdf"}] if file_id else [],
                     "facts": {"quote": q["number"], "total": texts.eur(priced["sell_cents"]), "transit": texts.days(priced["transit_days"]),
                               "valid until": priced["valid_until"]}})
    await emit(run_id, "offer", "Quote phase complete", kind="phase", state="done", data={"quote_id": q["id"]})
    if conf.lifecycle_on():
        await domain_emit("demo.offer_sent", {"run": await _run(run_id), "quote": q, "offer": offer}, background=True)
