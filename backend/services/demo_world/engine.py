"""Reactive outside world. It sends inputs; it never runs Huolitsija's operations.

Each received mail/domain observation is a durable job. Actor memory and pending
decisions survive restarts. Every random draw derives from run seed, actor and
input key. A later stage is possible only after the actual application state
supports it; the engine never sets shipment, declaration or invoice statuses.
"""
from datetime import timedelta
import hashlib
import json
import random

from core.db import db, register_index
from core.events import on
from core.jobs import enqueue, job, JobDeferred
from core.util import NO_ID, now, now_iso, parse_iso
from services.demo_autoquote import clock, conf as quote_conf
from services.demo_autoquote.events import emit
from . import boundary, broker, conf, injectors, llm

register_index("world_actions", "key", unique=True)

GOALS = {
    "w:customer": "Ship the supplied cargo, give complete accurate facts, accept a suitable offer, complete onboarding and pay invoices. "
        "Reply to an offer with BOOK 1 and both parties' names and complete addresses. Do not reply to mere acknowledgements. "
        "The names/addresses describe fictional world counterparties. Never claim KYC approved or any Huolitsija task done.",
    "w:carrier": "Earn the quoted freight by accepting feasible bookings, occasionally ask a cargo question or propose a small price change. "
        "On confirmation provide your own booking reference and transport document number, dates and existing vessel facts if supplied. "
        "For road transport provide your fictional truck's registration and registration country; these are your own fleet facts. "
        "For an export to a non-EU country, state your intended EU exit customs office and its official reference number. "
        "Research the real border route and office; never invent a customs office or code. "
        "Do not invent real vessel names, IMO, MMSI or actual AIS observations. Confirm accepted shipping instructions or VGM when asked.",
    "w:trucker": "Collect and deliver the cargo. Confirm feasible pickup/delivery slots or propose a nearby time.",
    "w:terminal": "Receive cargo, answer handling questions and release it only when the documents allow it.",
    "w:warehouse": "Receive, store and dispatch the stated goods. Report counted packages from the supplied facts.",
    "w:consignee": "Receive the ordered cargo and give an accurate signed receipt. Report any observed shortage or damage.",
    "w:bank": "Process the customer's authorised payments, sometimes after a delay. Preserve exact amount, currency and bank reference.",
    "w:insurer": "Answer an insurance certificate request from the actual shipment facts. Do not invent a real insurer's policy.",
}


def rng(run, actor, key):
    return random.Random(f"{run['seed']}:{actor}:{key}")


async def _run(run_id):
    return await db.raw.demo_runs.find_one({"id": run_id}, NO_ID)


def _alive(run):
    return run and run.get("phase") == "lifecycle" and run.get("status") not in ("stopped", "failed", "done")


async def _await_due(run, due_at):
    remaining = max(0,(parse_iso(due_at)-clock.sim_now(run)).total_seconds())
    state = boundary.clock_state()
    wall_seconds = remaining / max(0.1,float(state.get('speed') or 1))
    if state.get('paused') or wall_seconds > 30:
        # Deferral preserves attempts and releases the worker. A long payment
        # term or a paused presentation must not exhaust a world's job retries.
        raise JobDeferred(10 if state.get('paused') else 30,'Waiting for the outside actor scheduled time')
    await clock.sleep_sim(run['id'],remaining)


async def actor_for(run_id, actor):
    return await db.raw.world_actors.find_one({"run_id": run_id, "actor": actor, "active": True})


async def _register_actor(run, actor, address, name=None):
    if run.get("real_requester") and actor == "w:customer":
        return
    if not address:
        return
    address = boundary.addr(address)
    await db.raw.world_actors.update_one({"_id": run["id"] + ":" + actor + ":" + address}, {"$setOnInsert": {
        "run_id": run["id"], "actor": actor, "address": address, "name": name or actor[2:].title(),
        "persona": GOALS.get(actor, "Answer the logistics enquiry from your own known facts."), "state": {},
        "created_at": now_iso()}, "$set": {"active": True}}, upsert=True)


def _mail_title(outgoing, *, incoming=False, name=None):
    tag = outgoing.get("tag") or ""
    if incoming:
        label = {"w:customer": "Customer reply", "w:carrier": "Carrier reply", "w:trucker": "Trucker reply",
                 "w:consignee": "Consignee reply", "w:insurer": "Insurer reply"}.get(outgoing.get("actor"), "Reply")
        return (label + (" · " + name if name else "") + " received")[:59]
    if "carrier_booking" in tag:
        return ("Booking request sent" + (" to " + name if name else " to carrier"))[:59]
    labels = {"world_offer": "Freight offer sent to customer", "email_kyc_link": "Company details and POA requested",
              "email_booking_details": "Pickup and delivery details requested"}
    if tag in labels:
        return labels[tag]
    if "dunning" in tag:
        return "Payment reminder sent"
    if "invoice" in tag:
        return "Invoice sent to customer"
    if "insurance" in tag:
        return "Insurance certificate requested"
    subject = (outgoing.get("subject") or "").strip()
    if subject:
        return (subject + " · sent")[:59]
    return ((tag.replace("_", " ").strip().capitalize() or "Correspondence") + " sent")[:59]


@on("demo.offer_sent")
async def offer_sent(payload):
    if not boundary.on():
        return
    run = payload["run"]
    if run.get("status") in ("stopped", "failed", "done"):
        return
    active = await boundary.lifecycle_run()
    if active and active["id"] != run["id"]:
        raise ValueError("One world lifecycle runs at a time on the shared simulation clock")
    await db.raw.demo_runs.update_one({"id": run["id"]}, {"$set": {"phase": "lifecycle", "status": "lifecycle", "world_started_at": now_iso()}})
    await clock.set_clock(run["id"], speed=1.0 if run.get("real_requester") else conf.speed())
    run = await _run(run["id"])
    identity = run.get("requester_registry") or {}
    await _register_actor(run, "w:customer", quote_conf.requester_address(), identity.get("contact_name"))
    customer = await actor_for(run['id'],'w:customer')
    if customer:
        await injectors.customer_session(run,customer)
    partner = (payload.get("offer") or {}).get("partner") or {}
    if partner.get("email"):
        await _register_actor(run, "w:carrier", partner["email"], partner.get("name"))
    for actor in ("w:trucker", "w:terminal", "w:warehouse", "w:consignee", "w:bank", "w:insurer", "w:broker"):
        await _register_actor(run, actor, f"{actor[2:]}-{run['id'][:8]}@demo.huolitsija.invalid")
    await emit(run["id"], "booking", "Booking started", kind="phase", state="active", data={"quote_id": run["quote_id"]})
    offer = payload.get("offer") or run.get("offer") or {}
    outgoing = await db.raw.email_outbox.find_one({"id": offer.get("outbox_id")}, NO_ID) or {}
    outgoing.update(message_id=offer.get("message_id"), subject=outgoing.get("subject") or f"[{run['quote_number']}] Offer",
                    text=outgoing.get("text") or json.dumps(offer, default=str), actor="w:customer", run_id=run["id"],
                    tag="world_offer", id="offer:" + run["id"])
    if not run.get("real_requester"):
        await queue_mail(outgoing)


@boundary.mail_sink
async def queue_mail(outgoing):
    run_id = outgoing.get("run_id")
    run = await _run(run_id) if run_id else None
    if not _alive(run):
        return
    actor = outgoing.get("actor")
    if run.get("real_requester") and actor == "w:customer":
        return
    # At a boundary a newly researched desk is still outside the company. Register
    # it from the actual outbound purpose; do not let default-deny mail disappear.
    if actor == "w:unrouted":
        tag = outgoing.get("tag") or ""
        actor = "w:carrier" if "carrier_booking" in tag else "w:trucker" if "slot" in tag else \
            "w:warehouse" if "warehouse" in tag else "w:insurer" if "insurance" in tag else None
        if not actor:
            await emit(run_id, "docs", _mail_title(outgoing), kind="email_out", data={"facts": {"purpose": tag, "recipient": outgoing.get("to")}})
            return
        await _register_actor(run, actor, outgoing["to"])
        outgoing = {**outgoing, "actor": actor}
    import re
    safe_text = re.sub(r"(/api/email-channel/(?:go|pay)/)[A-Za-z0-9._-]+", r"\1[private link]", outgoing.get("text") or "")
    registered = await actor_for(run_id, actor)
    await emit(run_id, _stage(outgoing), _mail_title(outgoing, name=(registered or {}).get("name")), kind="email_out", state="waiting", edge=(_stage(outgoing), actor),
               data={"emails": [{"direction": "out", "from": outgoing.get("from"), "to": outgoing.get("to"),
                   "subject": outgoing.get("subject"), "text": safe_text, "message_id": outgoing.get("message_id"),
                   "provider": outgoing.get('provider') or 'world', 'outbox_id': outgoing.get('outbox_id'),
                   'delivered_for_real': bool(outgoing.get('delivered_for_real'))}],
                   "documents": [{"id": a["file_id"], "title": a["filename"], "kind": "attachment", "file_id": a["file_id"],
                       "filename": a["filename"], "content_type": a["content_type"]} for a in outgoing.get("attachments") or [] if a.get("file_id")]})
    await db.raw.world_inputs.update_one({"_id": outgoing["id"]}, {"$setOnInsert": outgoing}, upsert=True)
    await enqueue("world.react_mail", {"run_id": run_id, "input_id": outgoing["id"]}, key="world:mail:" + outgoing["id"], source="world")


async def _decision(run, actor, outgoing):
    key = "mail:" + outgoing["id"]
    action = await db.raw.world_actions.find_one({"key": key})
    stale_context = action and action.get('status') == 'pending' and actor['actor'] == 'w:customer' \
        and not action.get('world_time')
    if action and not stale_context:
        return action
    shipment = await db.raw.shipments.find_one({"quote_id": run["quote_id"]}, NO_ID)
    parties = None
    if actor["actor"] == "w:customer" and outgoing.get("tag") == "world_offer":
        from .identity import document_parties
        quote = await db.raw.quotes.find_one({"id": run["quote_id"]}, NO_ID) or {}
        parties = await document_parties(run, quote)
    draw = rng(run, actor["actor"], outgoing["id"])
    world_time = clock.sim_now(run).isoformat()
    reply = await llm.ask_json(actor["actor"], actor["persona"] +
        " You are an external participant in a private simulated environment, not Huolitsija's agent. React only to the actual outgoing message. "
        "Return {reply: boolean, text: string, delay_hours: number (0.1..12), memory: object, parties?: {shipper: {name,address:{street,postal_code,city,country}}, "
        "consignee:{name,address:{street,postal_code,city,country}}}, source_updates?: {cargo_description?,net_weight_kg?,origin_country?,package_type?,dimensions?}}. "
        "As the customer, when asked for missing technical specifications or a corrected invoice/packing list, supply your own consistent "
        "fictional goods facts in source_updates and explain them in the reply. That produces actual revised source PDFs for Huolitsija to read; "
        "never provide a fabricated classification approval or a regulatory result. Keep messages short and businesslike. "
        "Use the random inputs to vary questions, counteroffers and delays. Never claim an operation was performed by Huolitsija. "
        "For customer offer acceptance copy the supplied trading_parties verbatim into the message; do not rename them. "
        "The supplied world_time is your current local-world date, even when it differs from wall time. "
        "When asked for a corrected pickup date, give an explicit future YYYY-MM-DD working date based on world_time, "
        "rather than an ambiguous phrase such as 'next working day'. Do not change an accepted price or claim the booking is complete. "
        "For a receipt or informational notice set reply=false.",
        json.dumps({"mail": {k: outgoing.get(k) for k in ("subject", "text", "tag", "attachments")}, "state": actor.get("state"),
            "requester_identity": run.get("requester_registry"), "trading_parties": parties, "profile": run.get("profile"), "lane": run.get("lane"),
            "world_time": world_time,
            "shipment": shipment, "offer": run.get("offer") if outgoing.get("tag") == "world_offer" else None,
            "random": {"question": draw.random(), "counteroffer": draw.random(), "timing": draw.random()}}, default=str),
        run_id=run["id"], web_search=actor['actor'] == 'w:carrier')
    if parties:
        reply["parties"] = parties
        await db.raw.world_actors.update_many({"run_id": run["id"], "actor": "w:consignee"},
                                             {"$set": {"name": parties["consignee"]["name"], "identity": parties["consignee"]}})
    delay = min(12, max(0.1, float(reply.get("delay_hours") or 0.2))) * 3600
    fresh = {"key": key, "run_id": run["id"], "actor": actor["actor"], "decision": reply, 'world_time':world_time,
              "due_at": (clock.sim_now(run) + timedelta(seconds=delay)).isoformat(), "status": "pending", "created_at": now_iso()}
    if stale_context:
        await db.raw.world_actions.update_one({'key':key,'status':'pending','world_time':{'$exists':False}},
            {'$set':fresh,'$push':{'superseded_decisions':{'decision':action['decision'],'due_at':action.get('due_at'),
                'reason':'Customer input did not contain the current run time'}}})
    else:
        await db.raw.world_actions.update_one({"key": key}, {"$setOnInsert": fresh}, upsert=True)
    return await db.raw.world_actions.find_one({"key": key})


def _stage(outgoing):
    tag = outgoing.get("tag") or ""
    if "invoice" in tag or "dunning" in tag:
        return "invoicing"
    if "customs" in tag:
        return "customs"
    if "carrier_booking" in tag or tag in ("world_offer", "email_kyc_link", "email_booking_details"):
        return "booking"
    return "docs"


@job("world.react_mail", max_attempts=5, timeout=300, backoff=(5, 15, 30))
async def react_mail(j):
    run = await _run(j["payload"]["run_id"])
    if not _alive(run) or not boundary.on():
        return {"stopped": True}
    outgoing = await db.raw.world_inputs.find_one({"_id": j["payload"]["input_id"]})
    if run.get("real_requester") and outgoing.get("actor") == "w:customer":
        return {"real_requester": True}
    actor = await db.raw.world_actors.find_one({"run_id": run["id"], "actor": outgoing["actor"], "address": outgoing.get("to")}) \
        or await actor_for(run["id"], outgoing["actor"])
    if not actor:
        raise ValueError("World input has no actor")
    action = await _decision(run, actor, outgoing)
    if action["status"] == "delivered":
        return {"duplicate": True}
    await _await_due(run,action['due_at'])
    if not _alive(await _run(run["id"])):
        return {"stopped": True}
    stage, decision = _stage(outgoing), action["decision"]
    # Token-bearing portal links stay private, never in the trace's tool inputs.
    if actor["actor"] == "w:customer" and (outgoing.get("tag") == "email_kyc_link" or not actor.get("session_cookies")) and "/api/email-channel/go/" in ((outgoing.get("text") or "") + (outgoing.get("html") or "")):
        outcome = await injectors.customer_portal(run, actor, outgoing)
        await emit(run["id"], "booking", "Customer completed company profile", kind="world", simulated=True,
                   edge=("w:customer", "booking"), data={"facts": outcome or {}})
    updates = {k:v for k,v in (decision.get('source_updates') or {}).items()
               if k in ('cargo_description','net_weight_kg','origin_country','package_type','dimensions') and v not in (None,'')}
    if decision.get("reply") and decision.get("text"):
        attachments = []
        parties = decision.get("parties") or (actor.get("state") or {}).get("parties")
        if actor["actor"] == "w:customer" and parties and (outgoing.get("tag") == "world_offer" or updates):
            quote = await db.raw.quotes.find_one({'id':run['quote_id']}, NO_ID) or {}
            request = quote.get('request') or quote
            profile = {**(run.get('profile') or {}), **((actor.get('state') or {}).get('source_profile') or {}), **updates}
            for field in ('incoterm','incoterm_place'):
                if not profile.get(field) and request.get(field):
                    profile[field] = request[field]
            if not profile.get('package_type') and (request.get('cargo') or {}).get('package_type'):
                profile['package_type'] = request['cargo']['package_type']
            attachments = injectors.source_documents({**run,'profile':profile}, parties)
        result = await injectors.mail(run, actor, outgoing, str(decision["text"]), key=action["key"], attachments=attachments)
        await emit(run["id"], stage, _mail_title(outgoing, incoming=True, name=actor.get("name")), kind="world", state="active", simulated=True,
                   edge=(actor["actor"], stage), data={**({"title_key":"world.customer_reply", "title_vars":{"name":actor.get("name") or "Customer"}}
                       if actor['actor'] == 'w:customer' and actor.get('name') else {}), "emails": [{"direction": "in", **{k: result[k] for k in ("message_id", "from", "to", "subject", "text")},
                   "simulated": True}], "facts": {"channel": (result.get("result") or {}).get("channel", "execution" if outgoing.get("channel") == "execution" else "email")}})
    memory = decision.get("memory") if isinstance(decision.get("memory"), dict) else {}
    if decision.get("parties"):
        memory["parties"] = decision["parties"]
    if actor['actor'] == 'w:customer' and decision.get('source_updates'):
        memory['source_profile'] = {**((actor.get('state') or {}).get('source_profile') or {}), **updates}
    await db.raw.world_actors.update_one({"_id": actor["_id"]}, {"$set": {"state": {**(actor.get("state") or {}), **memory}}})
    await db.raw.world_actions.update_one({"key": action["key"]}, {"$set": {"status": "delivered", "delivered_at": now_iso()}})
    return {"replied": bool(decision.get("reply")), "actor": actor["actor"]}


async def schedule_movement(run, shipment, *, key):
    legs = shipment.get("legs") or []
    leg = next((x for x in legs if not x.get("ata")), legs[-1] if legs else {})
    count = await db.raw.feed_events.count_documents({"event.shipment_ref": shipment["reference"]})
    # Multiple carrier confirmations for the same physical state coalesce.
    key = f"{run['id']}:{shipment['id']}:{leg.get('seq')}:{count}"
    await enqueue("world.move", {"run_id": run["id"], "shipment_id": shipment["id"], "key": key}, key="world:move:" + key, source="world")


async def physical_blocker(shipment_id):
    async for exception in db.raw.exceptions.find({'shipment_id':shipment_id,'status':{'$in':['open','in_progress']}}):
        event = ((exception.get('evidence') or {}).get('event') or {}).get('exception') or {}
        if event.get('stops_goods') is True:
            return exception
        if exception.get('type') in ('damage','claim','claims') or (exception.get('owner') or {}).get('id') == 'claims_agent':
            continue
        return exception
    return None


@job('world.file_claim', max_attempts=5, timeout=300, backoff=(5,15,30))
async def file_claim(j):
    p = j['payload']
    run = await _run(p['run_id'])
    if run and run.get("real_requester"):
        return {"waiting_for_requester": True}
    if not _alive(run) or not boundary.on():
        return {'stopped':True}
    exception = await db.raw.exceptions.find_one({'id':p['exception_id'],'type':'damage'},NO_ID)
    if not exception:
        return {'skipped':'no damage report'}
    shipment = await db.raw.shipments.find_one({'id':exception['shipment_id'],'quote_id':run['quote_id']},NO_ID)
    if not shipment:
        return {'skipped':'no shipment in this run'}
    # React to Huolitsija's actual claim-intake request, not just to the world event.
    if not await db.raw.agent_messages.find_one({'_id':'claim-intake:' + exception['id']}):
        raise JobDeferred(5,'Waiting for Huolitsija to request the customer claim')
    actor = await actor_for(run['id'],'w:customer')
    if actor and not actor.get('session_cookies') and await injectors.customer_session(run,actor):
        actor = await actor_for(run['id'],'w:customer')
    if not actor or not actor.get('session_cookies'):
        raise JobDeferred(5,'Waiting for the customer portal session')
    key = 'claim:' + exception['id']
    action = await db.raw.world_actions.find_one({'key':key})
    if not action:
        facts = await llm.ask_json('w:customer',GOALS['w:customer'] +
            ' Huolitsija requested a cargo damage claim. You own the affected goods. Return a written inspection account as '
            '{description:string,amount_claimed_cents:positive integer,gross_kg_affected:positive number,packages_affected:positive integer}. '
            'Use the reported damage and your actual goods facts. Do not claim photos exist or invent Huolitsija approval. '
            'Claim only the genuinely affected portion, at most the cargo value, gross weight and package count.',
            json.dumps({'exception':exception,'profile':run.get('profile'),'shipment':shipment,
                        'world_time':clock.sim_now(run).isoformat()},default=str),run_id=run['id'])
        profile = run.get('profile') or {}
        limits = (('amount_claimed_cents',float(profile.get('value_eur') or 0)*100),
                  ('gross_kg_affected',float((shipment.get('cargo') or {}).get('weight_kg') or 0)),
                  ('packages_affected',int((shipment.get('cargo') or {}).get('packages') or 0)))
        if len(str(facts.get('description') or '')) < 10 or any(not 0 < float(facts.get(k) or 0) <= cap for k,cap in limits):
            raise ValueError('Customer inspection claim exceeds its known goods facts')
        if any(not isinstance(facts.get(k),int) or isinstance(facts[k],bool) for k in ('amount_claimed_cents','packages_affected')):
            raise ValueError('Customer claim amounts and package counts must be integers')
        action = {'key':key,'run_id':run['id'],'actor':'w:customer','decision':facts,'status':'pending'}
        await db.raw.world_actions.update_one({'key':key},{'$setOnInsert':action},upsert=True)
        action = await db.raw.world_actions.find_one({'key':key})
    if action['status'] == 'delivered':
        return action['claim']
    claim = await injectors.customer_claim(shipment,exception,actor,action['decision'])
    await db.raw.world_actions.update_one({'key':key},{'$set':{'status':'delivered','claim':claim}})
    await emit(run['id'],'exceptions','Customer damage claim submitted',kind='world',simulated=True,
        edge=('w:customer','exceptions'),data={'facts':{'claim':claim['number'],'status':claim['status']}})
    return claim


async def _pickup_schedule(shipment, leg, booking):
    """Physical pickup comes from the native appointment or confirmed purchase.

    The actor may describe its action; it cannot replace a booked calendar with
    a delay relative to the simulator clock. A feed can report that action late.
    """
    slot = await db.raw.slot_bookings.find_one({'shipment_id':shipment['id'],
        'leg_seq':leg['seq'],'kind':'pickup','status':{'$ne':'cancelled'}}, NO_ID)
    if slot and slot.get('status') != 'confirmed':
        raise JobDeferred(10, 'Waiting for the native pickup appointment confirmation')
    window = (slot or {}).get('window') or booking.get('pickup_window') or leg.get('pickup_window')
    start = parse_iso(window.get('start')) if window else parse_iso(booking.get('etd') or leg.get('etd'))
    end = parse_iso(window.get('end')) if window else start
    if not start or not end or end < start:
        raise ValueError('Native booking has no valid pickup window or ETD')
    confirmed = parse_iso(booking.get('confirmed_at'))
    if confirmed and confirmed > end:
        raise JobDeferred(10, 'Native pickup date predates carrier confirmation; waiting for a revised booking')
    return {'at':start.isoformat(), 'window_end':end.isoformat(),
        'source':'slot_booking' if slot else 'pickup_window' if window else 'native_etd'}


async def confirmed_booking_for_leg(shipment, leg):
    """Resolve the physical leg's native purchase, including a through-road group.

    A copied leg link is only an identity hint. Re-read the real booking and its
    coverage so stale/cancelled/foreign links never authorize physical movement.
    """
    link = leg.get('booking') or {}
    query = {'shipment_id':shipment['id'],'status':'confirmed'}
    if link.get('id'):
        query['id'] = link['id']
    else:
        query['leg_seq'] = leg.get('booking_parent_seq') or leg.get('seq')
    booking = await db.raw.carrier_bookings.find_one(query,NO_ID)
    if not booking or leg.get('seq') not in (booking.get('covered_leg_seqs') or [booking.get('leg_seq')]):
        return None
    if link.get('carrier_id') and link['carrier_id'] != booking.get('carrier_id'):
        return None
    return booking


@job("world.move", max_attempts=5, timeout=300, backoff=(5, 15, 30))
async def move(j):
    run = await _run(j["payload"]["run_id"])
    if not _alive(run) or not boundary.on():
        return {"stopped": True}
    s = await db.raw.shipments.find_one({"id": j["payload"]["shipment_id"]}, NO_ID)
    if not s or s.get("status") in ("cancelled", "delivered"):
        return {"finished": True}
    key = j["payload"]["key"]
    previous = await db.raw.world_actions.find_one({"key": "move:" + key})
    if previous and previous["status"] == "delivered":
        return {"duplicate": True}
    legs = s.get("legs") or []
    leg = next((x for x in legs if not x.get("ata")), legs[-1] if legs else {})
    booking = await confirmed_booking_for_leg(s,leg)
    if not booking:
        raise JobDeferred(5, "Waiting for a carrier-confirmed leg")
    if await physical_blocker(s['id']):
        raise JobDeferred(5, "Waiting for Huolitsija to handle the transport exception")
    milestones = {m.get("code") for m in s.get("milestones") or [] if m.get("leg_seq") in (leg.get("seq"), None) and m.get("at")}
    # This is the outside actor's physical state, not a schedule of future outcomes.
    if leg == legs[0] and "picked_up" not in milestones:
        allowed = ["picked_up"]
    elif "received" not in milestones:
        allowed = ["received", "exception"]
    elif "departed" not in milestones:
        allowed = ["departed", "exception"]
    elif "arrived" not in milestones:
        allowed = ["arrived", "eta", "exception"]
    elif leg != legs[-1]:
        allowed = ["arrived"]
    elif s.get("status") != "out_for_delivery":
        allowed = ["out_for_delivery", "exception"]
    else:
        allowed = ["delivered", "exception"]
    pickup = await _pickup_schedule(s,leg,booking) if allowed == ['picked_up'] else None
    if await db.raw.feed_events.find_one({"event.shipment_ref": s["reference"], "event.leg_seq": leg.get("seq"), "event.code": "exception"}, {"_id": 1}):
        allowed = [code for code in allowed if code != "exception"]
    if await db.raw.feed_events.find_one({"event.shipment_ref": s["reference"], "event.leg_seq": leg.get("seq"), "event.code": "eta"}, {"_id": 1}):
        allowed = [code for code in allowed if code != "eta"]
    broker_gate = s.get("customs_clearance") and broker.customer_arranged(s)
    if broker_gate and (("departed" in allowed and s.get("direction") == "export") or
                        ("out_for_delivery" in allowed and s.get("direction") == "import")):
        if not await broker.released(s):
            await enqueue("world.broker_release", {"run_id": run["id"], "shipment_id": s["id"]},
                key="world:broker-release:" + s["id"], source="world")
            raise JobDeferred(5, "Waiting for the verified customer broker release")
    if not broker_gate and "departed" in allowed and s.get("direction") == "export" and s.get("customs_clearance"):
        d = await db.raw.export_declarations.find_one({"shipment_id": s["id"], "status": {"$in": ["released", "exited"]}}, NO_ID)
        if not d:
            raise JobDeferred(10, "Waiting for export customs release")
    if not broker_gate and "out_for_delivery" in allowed and s.get("direction") == "import" and s.get("customs_clearance"):
        d = await db.raw.customs_declarations.find_one({"shipment_id": s["id"], "status": {"$in": ["released", "closed"]}}, NO_ID)
        if not d:
            raise JobDeferred(10, "Waiting for import customs release")
    actor = "w:trucker" if "picked_up" in allowed or "delivered" in allowed else "w:carrier"
    action = previous
    if not action:
        draw = rng(run, actor, key)
        decision = await llm.ask_json(actor, GOALS[actor] + " Choose your next physical event from the allowed events. "
            "Return {code, delay_hours: number 1..24, note, exception?: {type: delay|damage|other, title,description,new_eta?,stops_goods:boolean}}. "
            "stops_goods is true only if the goods physically cannot move; a minor damage claim can travel with the remaining goods. "
        "Risk draw below 0.25 favours an exception if allowed; above it favours normal progress. At most one exception per leg. "
            "ETA changes reflect your fictional operations, never claim real AIS observations. Do not skip a physical state.",
            json.dumps({"shipment": s, "leg": leg, "allowed": allowed, "risk_draw": draw.random()}, default=str), run_id=run["id"])
        if decision.get("code") not in allowed:
            raise ValueError("World actor chose an impossible transport transition")
        action = {"key": "move:" + key, "run_id": run["id"], "actor": actor, "decision": decision, "status": "pending",
                  "due_at": pickup['at'] if pickup else
                      (clock.sim_now(run) + timedelta(hours=min(24, max(1, float(decision.get("delay_hours") or 1))))).isoformat()}
        await db.raw.world_actions.update_one({"key": action["key"]}, {"$setOnInsert": action}, upsert=True)
    # Re-read the native schedule on retries too. A legacy pending actor delay
    # must not retain the old early-pickup bug or override a revised appointment.
    if pickup:
        await db.raw.world_actions.update_one({'key':action['key'],'status':'pending'},
            {'$set':{'due_at':pickup['at'],'pickup_schedule':pickup}})
        action = {**action, 'due_at':pickup['at'], 'pickup_schedule':pickup}
    decision = action["decision"]
    await _await_due(run,action['due_at'])
    if not _alive(await _run(run["id"])):
        return {"stopped": True}
    # Confirmation may be cancelled while the outside actor thinks or waits.
    # Re-read the native purchase for every physical leg, including shared ones.
    current_booking = await db.raw.carrier_bookings.find_one({'id':booking['id'],
        'shipment_id':s['id'],'status':'confirmed'}, NO_ID)
    if not current_booking:
        raise JobDeferred(5,'Waiting for a current carrier-confirmed purchase')
    if pickup:
        current_shipment = await db.raw.shipments.find_one({'id':s['id']}, NO_ID)
        current_leg = next((x for x in (current_shipment or {}).get('legs',[]) if x.get('seq') == leg['seq']),None)
        if not current_leg:
            raise JobDeferred(5,'Waiting for a current carrier-confirmed pickup')
        if await _pickup_schedule(current_shipment,current_leg,current_booking) != pickup:
            raise JobDeferred(5,'Native pickup schedule changed; retrying the current appointment')
    event_id = hashlib.sha256(action["key"].encode()).hexdigest()[:24]
    event = {"event_id": event_id, "shipment_ref": s["reference"], "leg_seq": leg["seq"], "code": decision["code"],
             "at": pickup['at'] if pickup else now_iso(), "note": decision.get("note") or "", "location": leg.get("to") if decision["code"] in ("arrived", "delivered") else leg.get("from")}
    if decision["code"] == "exception":
        event["exception"] = decision.get("exception") or {"type": "delay", "title": "Terminal slot delayed", "description": decision.get("note") or "Awaiting a new slot"}
    if decision["code"] == "eta":
        event["eta"] = (now() + timedelta(hours=12)).isoformat()
    if decision["code"] == "delivered":
        party = (s.get("parties") or {}).get("consignee") or {}
        consignee_actor = await actor_for(run['id'],'w:consignee') or {}
        consignee = party.get('contact_name') or (party.get('contact') or {}).get('name') \
            or (consignee_actor.get('identity') or {}).get('contact_name')
        if not consignee:
            raise ValueError("Delivery actor has no consignee person to sign its POD")
        event["pod"] = {"signed_by": consignee, "at": now_iso()}
    carrier = await db.raw.carriers.find_one({"id": booking["carrier_id"]}, NO_ID) or {}
    provider = carrier.get("code") or booking["carrier_id"]
    await injectors.tracking(run, provider, event)
    await emit(run["id"], f"leg:{leg['seq']}", decision["code"].replace("_", " ").capitalize() + " reported", kind="world", simulated=True,
               edge=(actor, f"leg:{leg['seq']}"), data={"facts": {"event": decision["code"], "provider": provider, "mode": leg.get("mode"),
                   "name": carrier.get("name"), "note": event["note"]}})
    await db.raw.world_actions.update_one({"key": action["key"]}, {"$set": {"status": "delivered", "feed_key": provider + ":" + event_id}})
    await enqueue("world.after_feed", {"run_id": run["id"], "shipment_id": s["id"], "feed_key": provider + ":" + event_id,
        "code": decision["code"]}, key="world:after-feed:" + event_id, source="world")
    return {"event": decision["code"]}


@job("world.after_feed", max_attempts=5, timeout=60, backoff=(5, 10, 20))
async def after_feed(j):
    p = j["payload"]
    run = await _run(p["run_id"])
    if not _alive(run):
        return {"stopped": True}
    feed = await db.raw.feed_events.find_one({"key": p["feed_key"]}, NO_ID) or {}
    if feed.get("status") in ("rejected", "unmatched"):
        raise ValueError("Huolitsija rejected the world tracking input: " + str(feed.get("error") or feed["status"]))
    if feed.get("status") != "applied":
        raise JobDeferred(2, "Waiting for Huolitsija to process the carrier input")
    s = await db.raw.shipments.find_one({"id": p["shipment_id"]}, NO_ID)
    if p["code"] == "departed" and s and not broker.customer_arranged(s):
        from .authority import notify_exit
        await notify_exit(run["id"])
    s = await db.raw.shipments.find_one({"id": p["shipment_id"]}, NO_ID)
    if s and s.get("status") not in ("cancelled", "delivered"):
        await schedule_movement(run, s, key=p["feed_key"])
    return {"applied": True}


@job("world.pay", max_attempts=5, timeout=300, backoff=(5, 15, 30))
async def pay(j):
    p = j["payload"]
    run = await _run(p["run_id"])
    if not _alive(run) or not boundary.on():
        return {"stopped": True}
    if run.get("real_requester") and not p.get("explicit_request"):
        return {"waiting_for_requester": True}
    invoice = await db.raw.invoices.find_one({"id": p["invoice_id"]}, NO_ID)
    if not invoice or invoice.get("status") in ("paid", "void", "draft"):
        return {"skipped": (invoice or {}).get("status")}
    key = "pay:" + invoice["id"]
    action = await db.raw.world_actions.find_one({"key": key})
    if not action and p.get("explicit_request"):
        action = {"key": key, "run_id": run["id"], "actor": "w:bank", "status": "pending",
                  "decision": {"reason": "Requester pressed simulate payment"}, "due_at": clock.sim_now(run).isoformat()}
        await db.raw.world_actions.update_one({"key": key}, {"$setOnInsert": action}, upsert=True)
    if not action:
        draw = rng(run, "w:bank", invoice["id"])
        decision = await llm.ask_json("w:bank", GOALS["w:bank"] + " The customer received this issued invoice and authorises payment. "
            "Return {delay_hours: number 1..72, late_payment: boolean, reason: string}. Prepayment invoices are needed to book, pay those promptly (1..6 hours, late_payment=false). "
            "For final freight invoices a risk draw below 0.25 means late payment; otherwise normal treasury processing.",
            json.dumps({"invoice": invoice, "risk_draw": draw.random()}, default=str), run_id=run["id"])
        payment_at = clock.sim_now(run) + timedelta(hours=min(72, max(1, float(decision.get("delay_hours") or 1))))
        due = parse_iso(str(invoice['due_date'])[:10] + 'T23:59:59Z') if invoice.get('due_date') else None
        if decision.get('late_payment') and invoice.get('origin') != 'prepayment' and due:
            payment_at = max(payment_at, due + timedelta(hours=min(72, max(1, float(decision.get('delay_hours') or 1)))))
        action = {"key": key, "run_id": run["id"], "actor": "w:bank", "decision": decision, "status": "pending",
                  "due_at": payment_at.isoformat()}
        await db.raw.world_actions.update_one({"key": key}, {"$setOnInsert": action}, upsert=True)
    if action["status"] == "delivered":
        return {"duplicate": True}
    await _await_due(run,action['due_at'])
    if not _alive(await _run(run["id"])):
        return {"stopped": True}
    statement = await injectors.bank(invoice, "CW" + hashlib.sha256(key.encode()).hexdigest()[:22])
    await emit(run["id"], "payment", "Bank credit received", kind="world", simulated=True, edge=("w:bank", "payment"),
               data={"facts": {"invoice": invoice["number"], "reference": invoice.get("rf_reference") or invoice.get("reference"),
                               "statement_id": statement["id"]}})
    await db.raw.world_actions.update_one({"key": key}, {"$set": {"status": "delivered", "statement_id": statement["id"]}})
    return {"statement_id": statement["id"]}


@job('world.customer_instructions', max_attempts=4, timeout=180, backoff=(5, 15, 30))
async def customer_instructions(j):
    p = j['payload']
    run = await _run(p['run_id'])
    if run and run.get("real_requester"):
        return {"waiting_for_requester": True}
    if not _alive(run) or not boundary.on():
        return {'stopped': True}
    s = await db.raw.shipments.find_one({'id': p['shipment_id']}, NO_ID)
    if not s or broker.customer_arranged(s) or not s.get('customs_clearance') or s.get('customs_instructions'):
        return {'skipped': 'already supplied or no customs'}
    actor = await actor_for(run['id'], 'w:customer')
    if actor and not actor.get('session_cookies') and await injectors.customer_session(run,actor):
        actor = await actor_for(run['id'],'w:customer')
    if not actor or not actor.get('session_cookies'):
        raise JobDeferred(5, 'Waiting for customer portal session')
    key = 'customs-instructions:' + s['id']
    action = await db.raw.world_actions.find_one({'key': key})
    if not action:
        offices = []
        if s.get('direction') == 'export':
            from services import customs_offices
            offices = await customs_offices.offices_for('FI', 'EXP')
            offices = [{k:o.get(k) for k in ('code','name','address','unlocode')} for o in offices if customs_offices.fresh(o)]
            if not offices:
                raise JobDeferred(10, 'Waiting for official COL export offices')
        decision = await llm.ask_json('w:customer', GOALS['w:customer'] + ' Your shipper must state where the goods will be presented '
            'to Finnish customs and the actual nature of this fictional commercial transaction. Return '
            '{goods_location: {type: "D", address: {street,postal_code,city,country}}, transaction_nature: "11", '
            'export_statistical_values?: [{seq:1,value_eur:number,basis:string}], export_office?: official_code}. '
            'Use the fictional Finland-side delivery or pickup address from the parties. This is your explicit statement '
            'as the shipper, not an inference by Huolitsija. For export state the value from your own books at the Finnish border, '
            'including domestic transport/insurance but excluding charges after the border, and explain the component amounts '
            'and the invoice delivery terms. You own the fictional commercial transaction; give an explicit exporter declaration '
            'of its border value. For export choose your preferred export office from the supplied real COL offices (centralised export processing '
            'or a suitable office serving your location). Copy its official code exactly. Do not assert an HS classification or a customs approval. '
            'Do not use a real third-party business identity.',
            json.dumps({'parties': s.get('parties'), 'direction': s.get('direction'), 'identity': run.get('requester_registry'),
                        'profile': run.get('profile'), 'official_export_offices': offices,
                        'quote': await db.raw.quotes.find_one({'id':run['quote_id']}, NO_ID)}, default=str), run_id=run['id'])
        action = {'key': key, 'run_id': run['id'], 'decision': decision, 'status': 'pending'}
        await db.raw.world_actions.update_one({'key': key}, {'$setOnInsert': action}, upsert=True)
    result = await injectors.customer_instructions(s, actor, action['decision'])
    await db.raw.world_actions.update_one({'key': key}, {'$set': {'status': 'delivered'}})
    await emit(run['id'], 'customs', 'Customer supplied customs instructions', kind='world', simulated=True,
               edge=('w:customer','customs'), data={'facts': result})
    return result


@job('world.broker_release', max_attempts=4, timeout=60, backoff=(5, 15, 30))
async def broker_release(j):
    run = await _run(j['payload']['run_id'])
    if not _alive(run) or not boundary.on():
        return {'stopped': True}
    shipment = await db.raw.shipments.find_one({'id': j['payload']['shipment_id']}, NO_ID)
    if not shipment:
        return {'stopped': True}
    return await broker.provide_release(run, shipment)
