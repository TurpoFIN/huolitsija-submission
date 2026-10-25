"""The world sandbox boundary of the demo: the ONE place that knows the outside world is simulated.

DEMO_WORLD_SANDBOX=true (default false) is read here and nowhere else. Huolitsija's transports call into this module at
the last moment before anything leaves the process (mail providers, Tulli message exchange); with the flag off every
call returns None and the transport runs unchanged. Agents, ops code, parsers and the inbound pipeline never see it.

Mail rule under the flag (default deny). A mail goes out through the real provider only when its recipient is
  1. one of the demo's own mailboxes (requester sandbox, Huolitsija's inbound mailbox, the partner rehearsal mailbox), or
  2. reserved in demo_runs.recipients for its initial RFQ while the run is still in its quote phase (cap 8).
Everything else is captured: recorded as sent with provider "world" and handed to the world actor that owns the address
(world_actors), or to the run's unrouted sink. Mail to the requester during the lifecycle is delivered AND copied to the
customer actor, so the requester mailbox keeps the real mail."""
from __future__ import annotations

import asyncio
import logging
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Awaitable, Callable

from core import config
from core.db import db, register_index
from core.util import new_id, now_iso

log = logging.getLogger("demo_world.boundary")
register_index("world_mail", [("run_id", 1), ("created_at", 1)])
register_index("world_mail", "message_id")
register_index("world_actors", [("address", 1), ("run_id", 1)])

LIFECYCLE_PHASES = ("lifecycle",)
_mail_sinks: list[Callable[[dict], Awaitable[None]]] = []
_authorities: dict[str, Callable[[bytes, str], Awaitable[bytes]]] = {}

# An address match alone must never open a carrier/broker/world-purpose mail.
CUSTOMER_TAGS = set("email_ack email_need_info email_cannot_quote email_quote email_quote_review "
    "email_booking_estimate email_booking_details email_booking_failed email_booking_confirmed "
    "email_kyc_link email_kyc_approved email_kyc_in_review email_kyc_rejected "
    "email_docs_received email_update email_answer email_clarification_complete invoice_issued invoice_reminder invoice_paid "
    "invoice_credit_note invoice_void booking_confirmation".split())


def on() -> bool:
    return config.env_bool("DEMO_WORLD_SANDBOX", False)


def mail_sink(fn: Callable[[dict], Awaitable[None]]):
    """The world engine registers here to receive every captured mail."""
    if fn not in _mail_sinks:
        _mail_sinks.append(fn)
    return fn


def authority(system: str):
    def deco(fn: Callable[[bytes, str], Awaitable[bytes]]):
        _authorities[system] = fn
        return fn
    return deco


def addr(value: str | None) -> str:
    return parseaddr(value or "")[1].strip().lower()


def own_mailboxes() -> set[str]:
    names = ("DEMO_REQUESTER_ADDRESS", "DEMO_INBOUND_ADDRESS", "MAILTM_ADDRESS", "DEMO_PARTNER_SANDBOX_ADDRESS")
    return {addr(config.env(n)) for n in names if config.env(n)}


def requester_address() -> str:
    return addr(config.env("DEMO_REQUESTER_ADDRESS"))


async def _quote_phase_recipient(address: str, ref: dict | None = None, tag: str | None = None) -> bool:
    # The sole external-mail exception is the demo's capped quote mail path.
    ref = ref or {}
    if ref.get("type") != "demo_run" or ref.get("kind") != "rfq" \
            or tag != "demo_" + ref["kind"]:
        return False
    run = await db.raw.demo_runs.find_one({"id": ref.get("id"), "recipients": address, "phase": "quote",
        "status": {"$nin": ["stopped", "failed", "done"]}}, {"_id": 0, "recipients": 1})
    if not run or len(run.get("recipients") or []) > 8:
        return False
    full = await db.raw.demo_runs.find_one({"id": ref.get("id")}, {"_id": 0})
    if full.get("real_requester"):
        from services.demo_autoquote.public_requester import wall_now
        day = wall_now().date().isoformat()
        if full.get("rfq_mode") != "real" or full.get("rfq_day") != day:
            return False
        return bool(await db.raw.demo_public_limits.find_one({"_id": {"$in": [f"rfq:{day}:0", f"rfq:{day}:1"]},
                                                              "owner": full["id"]}))
    return True


async def real_requester_run(to: str, ref: dict | None, tag: str | None) -> dict | None:
    """Per-run immutable address + exact business lineage + customer purpose."""
    from services.demo_autoquote import conf
    if not on() or not conf.real_requester_enabled() or tag not in CUSTOMER_TAGS:
        return None
    run = await related_run(ref)
    if not run or not run.get("real_requester") or addr(to) != conf.requester_address(run):
        return None
    if await db.raw.world_actors.find_one({"run_id": run["id"], "address": addr(to), "actor": {"$ne": "w:customer"}}):
        return None
    return run


def recipient_header(name: str) -> bool:
    import re
    key = str(name).strip().lower()
    if key in ("reply-to", "in-reply-to", "x-huolitsija-original-to"):
        return False
    return bool(re.search(r"(?:^|-)(?:to|cc|bcc|recipients?|envelope-to)$", key))


def envelope_recipients(to: str, headers: dict | None = None) -> list[str]:
    """Superset of every SMTP envelope target, including Resent-* headers.

    smtplib.send_message reads recipient headers itself. Validate them before any
    provider-specific serialization, rather than trusting the nominal To field.
    Empty/malformed recipient fields cannot open the real transport path.
    """
    fields = [str(to or "")]
    for key, value in (headers or {}).items():
        if recipient_header(key):
            fields.extend(str(x) for x in value) if isinstance(value, (list, tuple)) else fields.append(str(value))
    recipients = [email.strip().lower() for _, email in getaddresses(fields)]
    return list(dict.fromkeys(recipients)) or [""]


async def lifecycle_run() -> dict | None:
    """The run the world is playing now (newest run in its lifecycle phase)."""
    return await db.raw.demo_runs.find_one({"phase": {"$in": list(LIFECYCLE_PHASES)}, "status": {"$nin": ["stopped", "failed", "done"]}},
                                           {"_id": 0}, sort=[("updated_at", -1)])


async def related_run(ref: dict | None) -> dict | None:
    """Resolve explicit business lineage; platform-wide mail has no run lineage."""
    ref = ref or {}
    typ, ident = ref.get('type'), ref.get('id')
    query = {'status':{'$nin':['stopped','failed','done']},'phase':{'$in':['quote','lifecycle']}}
    if typ == 'demo_run':
        return await db.raw.demo_runs.find_one({**query,'id':ident}, {'_id':0})
    run_id = ref.get('demo_run_id')
    qid = ref.get('quote_id') or (ident if typ == 'quote' else None)
    sid = ref.get('shipment_id') or (ident if typ == 'shipment' else None)
    for kind, collection, field in (('invoice','invoices','invoice_id'),('carrier_booking','carrier_bookings','booking_id'),
                                     ('email_thread','email_threads','thread_id'),('document','documents','document_id')):
        value = ref.get(field) or (ident if typ == kind else None)
        if value:
            entity = await db.raw[collection].find_one({'id':value},{'_id':0}) or {}
            run_id = run_id or entity.get('demo_run_id')
            qid, sid = qid or entity.get('quote_id'), sid or entity.get('shipment_id')
    if run_id:
        # Explicit thread ownership remains authoritative even if the old run
        # has stopped; never reassign that mail to a newer run by quote/address.
        return await db.raw.demo_runs.find_one({**query,'id':run_id},{'_id':0})
    if sid and not qid:
        shipment = await db.raw.shipments.find_one({'id':sid},{'quote_id':1}) or {}
        qid = shipment.get('quote_id')
    return await db.raw.demo_runs.find_one({**query,'quote_id':qid},{'_id':0}) if qid else None


async def owner(address: str, run_id: str | None = None) -> tuple[str | None, str]:
    """(run_id, actor) of the world actor that owns `address`; the requester belongs to the customer actor."""
    query = {"address": address, "active": True, **({'run_id':run_id} if run_id else {'actor': {'$ne': 'w:customer'}})}
    row = await db.raw.world_actors.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    if row:
        run = await db.raw.demo_runs.find_one({'id':row['run_id'], 'phase':'lifecycle',
            'status':{'$nin':['stopped','failed','done']}},{'id':1})
        if run:
            return row["run_id"], row["actor"]
    if run_id and address and address == requester_address():
        return run_id, "w:customer"
    return run_id, "w:unrouted"


async def capture_mail(*, to: str, subject: str, text: str, html: str | None = None, from_addr: str | None = None,
                       reply_to: str | None = None, headers: dict | None = None, attachments: list | None = None,
                       message_id: str | None = None, in_reply_to: str | None = None, channel: str = "core",
                       ref: dict | None = None, company_id: str | None = None, tag: str | None = None,
                       delivery: dict | None = None) -> dict | None:
    """None: send it for real (flag off, or an allowed recipient). A dict: the mail was captured by the world; the caller
    records it as sent with these fields and does not call its provider."""
    if not on():
        return None
    recipients = envelope_recipients(to, headers)
    related = await related_run(ref)
    # Quota capture precedes even our own rehearsal mailbox exception: RFQs
    # redirected there must still be recorded as world mail, never transported.
    quota_rfq = bool(related and related.get("real_requester") and related.get("rfq_mode") == "captured_quota"
                     and tag == "demo_rfq" and (ref or {}).get("kind") == "rfq")
    own = own_mailboxes()
    denied = [address for address in recipients if quota_rfq or not address or (address not in own
        and not await _quote_phase_recipient(address, ref, tag)
        and not await real_requester_run(address, ref, tag))]
    # Providers may reinterpret custom headers. Recipient headers are forbidden
    # there even when their addresses are owned; never pass them to an API.
    forbidden_headers = any(recipient_header(key) for key in (headers or {}))
    lifecycle_copy = not denied and not forbidden_headers and requester_address() in recipients and related is not None \
        and related.get('phase') == 'lifecycle' and (delivery or {}).get('status') == 'sent'
    if delivery is not None and not lifecycle_copy:
        return None
    if not denied and not forbidden_headers and not lifecycle_copy:
        return None
    # Mixed envelopes are captured in full: an allowed To cannot smuggle an
    # external Bcc through the provider. Route the capture to the denied actor.
    to_addr = denied[0] if denied else requester_address() if lifecycle_copy else addr(to)
    run_id, actor = (None, 'w:unrouted') if ref and not related else await owner(to_addr, (related or {}).get('id'))
    headers = dict(headers or {})
    mid = message_id or headers.get("Message-ID") or make_msgid(idstring="hw", domain="huolitsija.fi")
    row = {"id": new_id(), "run_id": run_id, "actor": actor, "channel": channel, "to": to_addr, "to_raw": to,
           "envelope_recipients": recipients,
           "from": from_addr or config.EMAIL_FROM, "reply_to": reply_to, "subject": subject, "text": (text or "")[:50000],
           "html": (html or "")[:100000] if html else None, "headers": {k: str(v)[:1000] for k, v in headers.items()},
           "message_id": mid, "in_reply_to": in_reply_to or headers.get("In-Reply-To"),
           "references": headers.get("References"), "ref": ref or {}, "company_id": company_id, "tag": tag,
           "attachments": [{"filename": a[0], "content_type": a[1], "size": len(a[2] or b"")} for a in attachments or []],
           "provider": (delivery or {}).get('provider') or 'world', "outbox_id": (delivery or {}).get('id'),
           "delivered_for_real": lifecycle_copy, "created_at": now_iso(), "status": "captured"}
    await db.raw.world_mail.insert_one(dict(row))
    if attachments:
        from core.storage import put_file
        files = []
        for name, ctype, data in attachments:
            data = data.encode("utf-8") if isinstance(data, str) else data
            fid = await put_file(data, filename=name, content_type=ctype)
            files.append({"filename": name, "content_type": ctype, "file_id": fid, "size": len(data)})
        await db.raw.world_mail.update_one({"id": row["id"]}, {"$set": {"attachments": files}})
        row["attachments"] = files
    row.pop("_id", None)
    for sink in list(_mail_sinks):
        asyncio.get_running_loop().create_task(_safe(sink, row))
    if lifecycle_copy:
        return None
    return {"status": "sent", "provider": "world", "provider_id": row["id"], "message_id": mid,
            "world": {"run_id": run_id, "actor": actor}, "sent_at": now_iso()}


async def _safe(fn, row):
    try:
        await fn(row)
    except Exception:  # noqa: BLE001 - the world never breaks Huolitsija's send
        log.exception("world mail sink failed for %s", row.get("id"))


async def authority_post(system: str, request: bytes, operation: str) -> bytes:
    """A submission to an authority (Tulli DME) under the flag: answered by the simulated authority, never sent out."""
    fn = _authorities.get(system)
    if fn is None:
        from . import authority as _authority  # noqa: F401 - registers the Tulli authority
        fn = _authorities.get(system)
    if fn is None:
        raise RuntimeError(f"no simulated authority for {system}")
    return await fn(request, operation)


async def authority_reply_exists(storage_id: str, intermediary: str) -> bool:
    """A private-authority reset must not leave cached replies to vanished files.

    Live authority storage is never queried or invalidated through this hook.
    """
    if not on():
        return True
    return bool(await db.raw.world_authority_messages.find_one(
        {'storage_id': storage_id, 'intermediary': intermediary}, {'_id': 1}))


def authority_poll_seconds(seconds: float) -> float:
    """The private authority's five-minute interval follows its world's clock.

    Live external authority rate limits always remain wall time. No application
    decision or response is changed by accelerating this boundary's timer.
    """
    return seconds / max(1.0, demo_rate()) if on() else seconds


def authority_endpoint_allowed(endpoint: str) -> bool:
    """Only this private, loopback authority URL is a sandbox endpoint override."""
    if not on():
        return False
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    return parsed.scheme in ('http','https') and parsed.hostname in ('127.0.0.1','localhost','::1') \
        and parsed.path == '/api/demo/world/authority/tulli' and not (parsed.username or parsed.password or parsed.query or parsed.fragment)


def authority_configuration_error(mode: str, endpoint: str) -> str | None:
    if not on():
        return None
    if mode != 'test':
        return 'World authority requires TEST mode'
    if not authority_endpoint_allowed(endpoint):
        return 'TULLI_ENDPOINT must be the loopback world authority; real Tulli URLs are forbidden in sandbox mode'
    return None


async def registry_http(method: str, url: str, *, content: bytes | None = None):
    """Only registered fictional world identities get registry answers here.

    KYC, its parsers and its decision rules see the normal registry protocols.
    All other lookups keep using their original transport.
    """
    if not on():
        return None
    from .registry import http_response
    return await http_response(method, url, content=content)


async def prh_owners_xml(business_id: str):
    if not on():
        return None
    from .registry import owners_xml
    return await owners_xml(business_id)


async def pep_scope(business_id: str | None):
    """Use the registered persona ID, never a coincident person's name, to scope PEP transport and cache."""
    if not on() or not business_id:
        return None
    from .registry import persona
    row = await persona(business_id)
    return row["business_id"] if row else None


async def pep_http(method: str, url: str, *, persona_business_id: str | None, params=None, json=None):
    if not on() or not persona_business_id:
        return None
    from .registry import pep_response
    return await pep_response(method, url, persona_business_id=persona_business_id, params=params, json=json)


async def registry_pep_hit(business_id: str | None, name: str):
    if not on() or not business_id:
        return None
    from .registry import pep_hit
    return await pep_hit(business_id, name)


# The demo may evaluate its selected purchase with production policy. Background
# sourcing never negotiates for it, including while real RFQ replies are parsed.
from contextlib import asynccontextmanager  # noqa: E402
from contextvars import ContextVar  # noqa: E402

_PROCUREMENT_DATABASE = 'huo_demo_q'
_selected_purchase = ContextVar('world_selected_purchase',default=None)


@asynccontextmanager
async def selected_partner_offer(run_id: str, outreach_id: str):
    token = _selected_purchase.set((run_id,outreach_id))
    try:
        yield
    finally:
        _selected_purchase.reset(token)


async def partner_autopilot(outreach: dict | None = None):
    if not on():
        return None
    # A typo or another database must never enable automatic partner negotiation.
    scope = _selected_purchase.get()
    if db.raw.name != _PROCUREMENT_DATABASE or not scope or not outreach:
        return False
    run_id, outreach_id = scope
    if outreach.get('id') != outreach_id or outreach.get('demo_run_id') != run_id:
        return False
    return bool(await db.raw.demo_runs.find_one({'id':run_id,'phase':'quote','status':'running'}, {'_id':1}))


# ---------------------------------------------------------------------------------------------
# Demo clock. Time is part of the world: under the flag, Huolitsija's own clock (core.util.now) is the demo clock, so
# due dates, 24-hour waits and the daily finance jobs happen on demo time. Each run owns its anchors; the single
# application process follows its active run. Speed 1 in the quote phase, faster in the lifecycle. Operational timers stay on wall time
# through core.util.span; only the business-calendar jobs below follow demo days.
# ---------------------------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

CALENDAR_JOBS = {"dunning.run", "finance.collections_sweep", "payments.reconcile_daily", "finance.payment_run",
                 "credit.recheck_daily", "tariffs.expire"}
_REAL = datetime.now
_state: dict = {}
_clock_lock = asyncio.Lock()


def _wall() -> datetime:
    return _REAL(timezone.utc)


def _parse(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def demo_now() -> datetime:
    s = _state
    if not s:
        return _wall()
    if s.get("paused") or s.get("hold"):
        return s["anchor_sim"]
    return s["anchor_sim"] + timedelta(seconds=(_wall() - s["anchor_real"]).total_seconds() * s["speed"])


def demo_rate() -> float:
    s = _state
    return 1.0 if not s or s.get("paused") or s.get("hold") else max(1.0, float(s.get("speed") or 1.0))


def clock_state() -> dict:
    s = _state or {"speed": 1.0, "paused": False}
    return {"run_id": s.get('run_id'), "speed": float(s.get("speed") or 1.0), "paused": bool(s.get("paused") or s.get("hold")), "user_paused": bool(s.get("paused")),
            "hold": s.get("hold"), "now": demo_now().isoformat(),
            "wall": _wall().isoformat()}


async def _save_run(*, active=True):
    if _state.get('run_id'):
        c = {**_state, 'anchor_real': _state['anchor_real'].isoformat(),
             'anchor_sim': _state['anchor_sim'].isoformat(), 'last_sim': demo_now().isoformat(), 'active': active}
        await db.raw.demo_runs.update_one({'id': _state['run_id']}, {'$set': {'clock': c}})


async def _save():
    s = _state
    if not s:
        return
    await db.raw.world_clock.update_one({"_id": "demo"}, {"$set": {
        "run_id": s.get('run_id'),
        "anchor_real": s["anchor_real"], "anchor_sim": s["anchor_sim"], "speed": s["speed"], "paused": s["paused"],
        "hold": s.get("hold"), "last_sim": demo_now(), "saved_at": _wall()}}, upsert=True)
    await _save_run()


async def load_clock():
    """Resume after a restart: never go back in time and never jump forward by the downtime."""
    doc = await db.raw.world_clock.find_one({"_id": "demo"}) or {}
    wall = _wall()
    owner = await db.raw.demo_runs.find_one({'id': doc.get('run_id'),
        'status': {'$nin': ['stopped', 'failed', 'done']}}) if doc.get('run_id') else None
    if not owner:
        if doc.get('run_id'):
            await db.raw.demo_runs.update_one({'id': doc['run_id']}, {'$set': {
                'clock.active': False, 'clock.last_sim': _parse(doc.get('last_sim') or wall).isoformat()}})
        doc = {}
    last = _parse(doc["last_sim"]) if doc.get("last_sim") else wall
    _state.update(anchor_real=wall, anchor_sim=last if doc.get("paused") or doc.get("hold") else max(last, wall), speed=float(doc.get("speed") or 1.0),
                  paused=bool(doc.get("paused")), hold=doc.get('hold'), run_id=doc.get('run_id'))
    await _save()


async def _clock_owner(run_id):
    run = await db.raw.demo_runs.find_one({'id': run_id})
    if not run or run.get('status') in ('stopped', 'failed', 'done'):
        raise ValueError('Clock requires an active demo run')
    other = await db.raw.demo_runs.find_one({'id': {'$ne': run_id}, 'phase': 'lifecycle',
        'status': {'$nin': ['stopped', 'failed', 'done']}})
    if other:
        raise ValueError('Another lifecycle run owns the application clock')
    return run


async def reset_clock(run_id: str) -> dict:
    """Start a new quote at wall time; never rewind an active lifecycle."""
    async with _clock_lock:
        run = await _clock_owner(run_id)
        if run.get('phase') == 'lifecycle':
            raise ValueError('Cannot reset a lifecycle clock')
        await _save_run(active=False)
        wall = _wall()
        _state.update(run_id=run_id, anchor_sim=wall, anchor_real=wall, speed=1.0, paused=False, hold=None)
        await _save()
        return clock_state()


async def set_clock(*, run_id: str, speed: float | None = None, paused: bool | None = None) -> dict:
    async with _clock_lock:
        run = await _clock_owner(run_id)
        if not _state:
            await load_clock()
        if _state.get('run_id') != run_id:
            await _save_run(active=False)
            from services.demo_autoquote.clock import sim_now
            cur = sim_now(run)
            c = run.get('clock') or {}
            _state.update(run_id=run_id, speed=float(c.get('speed') or 1), paused=bool(c.get('paused')), hold=c.get('hold'))
        else:
            cur = demo_now()
        _state.update(anchor_sim=cur, anchor_real=_wall())
        if speed is not None:
            _state["speed"] = max(0.1, min(float(speed), 86400.0))
        if paused is not None:
            _state["paused"] = bool(paused)
        await _save()
        return clock_state()


async def sleep_demo(seconds: float, *, tick: float = 0.5, stop=None) -> None:
    """Wait `seconds` of demo time (follows speed changes and pauses). `stop()` -> True ends the wait early."""
    target = demo_now() + timedelta(seconds=seconds)
    while True:
        if stop is not None and await stop():
            return
        left = (target - demo_now()).total_seconds()
        if left <= 0:
            return
        real_left = tick if _state.get("paused") or _state.get("hold") else left / max(0.1, float(_state.get("speed") or 1.0))
        await asyncio.sleep(max(0.05, min(tick, real_left)))


def install():
    """Under the flag Huolitsija's clock and business calendar follow the demo clock."""
    if not on():
        return
    import core.jobs
    import core.util
    core.util._clock = demo_now
    core.util._rate = demo_rate
    core.jobs.DEMO_CALENDAR = lambda job: job in CALENDAR_JOBS


async def refresh_clock_hold():
    """Freeze only this world's calendar when a native owner card stops its next leg.

    A hold is independent of the operator's pause. Clearing a native card removes
    the hold without approving it or changing any business status.
    """
    if not on() or not _state.get('run_id'):
        return
    run = await db.raw.demo_runs.find_one({'id':_state['run_id'],'phase':'lifecycle',
        'status':{'$nin':['stopped','failed','done']}})
    blocking = []
    if run and run.get('quote_id'):
        async for shipment in db.raw.shipments.find({'quote_id':run['quote_id'],
                'status':{'$nin':['cancelled','delivered']}}):
            leg = next((lg for lg in shipment.get('legs') or [] if not lg.get('ata')),None)
            async for approval in db.raw.approvals.find({'status':'pending',
                    'kind':{'$in':['carrier_booking','booking_review']},
                    '$or':[{'ref_id':shipment['id']},{'data.shipment_id':shipment['id']}]}):
                seq = (approval.get('data') or {}).get('leg_seq')
                if approval['kind'] == 'booking_review' or leg and (seq is None or seq == leg['seq']):
                    blocking.append(approval['id'])
    hold = {'reason':'native_owner_queue','approval_ids':sorted(blocking)} if blocking else None
    async with _clock_lock:
        if hold != _state.get('hold'):
            current = demo_now()
            _state.update(anchor_sim=current,anchor_real=_wall(),hold=hold)
            await _save()


from core.events import on as _on_event  # noqa: E402


@_on_event('approval.requested')
@_on_event('approval.decided')
async def _native_queue_changed(payload):
    await refresh_clock_hold()


async def _heartbeat():
    while True:
        await asyncio.sleep(2)
        try:
            await refresh_clock_hold()
            await _save()
        except Exception:  # noqa: BLE001
            log.debug("demo clock heartbeat failed", exc_info=True)


def _register():
    from core.lifecycle import on_startup

    @on_startup(order=1)
    async def _start_clock():
        await load_clock()
        install()
        asyncio.get_running_loop().create_task(_heartbeat())


_register()
