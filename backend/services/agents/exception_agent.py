"""Exception agent (handling; detection stays in services.exceptions_agent). Every new exception gets a plan:

  delay        tell the customer the new ETA; re-book a leg that has not left yet when the slip is above
               EXC_REBOOK_HOURS [48]; resolve once the customer knows.
  capacity     re-book the leg with another carrier (carrier agent, same guardrails).
  damage/loss  claim intake: ask the customer for photos, the delivery-note remarks and the claim (NSAB 2015 § 27
               notice within 7 days); the claims agent decides the claim.
  missing_docs ask the customer for the documents; the detection rule clears it when they are on file.
  customs_hold the customs agent is already on it; payment: dunning owns it.
  other types  owner queue.

Autopilot off, the kill switch, or a critical exception -> owner queue card (exception_action) with the plan."""
from core import config
from core.agent_runtime import agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import enqueue, job
from core.util import NO_ID, now_iso, parse_iso
from services.agents.base import agents_epoch, customer_message, entity_lock, mode_key, sweeper

AGENT = "exception_agent"
REBOOK_HOURS = float(config.env("EXC_REBOOK_HOURS", "48"))
HANDLED = {"delay", "capacity", "damage", "missing_docs", "customs_hold", "payment"}


def _fi_dt(iso: str | None) -> str:
    from services.exceptions_agent import _fi_dt as f
    return f(parse_iso(iso)) if iso else "—"


def _en_dt(iso: str | None) -> str:
    from services.exceptions_agent import _en_dt as f
    return f(parse_iso(iso)) if iso else "—"


def plan_for(ex: dict, s: dict | None) -> dict:
    t = ex.get("type")
    ev = ((ex.get("evidence") or {}).get("event") or {}).get("exception") or {}
    leg_seq = (ex.get("evidence") or {}).get("leg_seq")
    leg = next((lg for lg in (s or {}).get("legs") or [] if lg.get("seq") == leg_seq), None)
    plan = {"type": t, "actions": [], "leg_seq": leg_seq}
    rule = str(ex.get("rule") or "").upper()  # visibility rules (s10): R-ROLLOVER, R-CUTOFF, R-CONGESTION
    if rule:
        plan["rule"] = rule
    if rule in ("R-ROLLOVER", "R-CUTOFF"):
        # the cargo missed the booked vessel: rebook on the next sailing even though that vessel has departed
        plan.update(new_eta=ev.get("new_eta") or (s or {}).get("eta"))
        plan["actions"] += ["rebook", "notify_customer"] + (["resolve"] if ex.get("detected_by") != "rule" else [])
        return plan
    if rule == "R-CONGESTION":
        t = "delay"  # notify and re-plan the ETA; a rebook only before departure, as for any delay
    if t == "delay":
        evidence = ex.get("evidence") or {}
        eta_rule = str(ex.get("rule_key") or "").startswith("eta:")  # R-ETA: ETA vs the plan, slip in the evidence
        if eta_rule and not leg:
            leg = next((lg for lg in (s or {}).get("legs") or [] if not lg.get("ata")), None)
            plan["leg_seq"] = (leg or {}).get("seq")
        new_eta = ev.get("new_eta") or (evidence.get("eta") if eta_rule else None) or (s or {}).get("eta")
        plan.update(new_eta=new_eta)
        plan["actions"].append("notify_customer")
        old = parse_iso(evidence.get("planned_eta") if eta_rule else (leg or {}).get("eta"))
        new = parse_iso(new_eta)
        slip_h = (new - old).total_seconds() / 3600 if old and new else 0
        plan["slip_hours"] = round(slip_h, 1)
        if leg and not leg.get("atd") and slip_h >= REBOOK_HOURS:
            plan["actions"].insert(0, "rebook")
        if eta_rule and new_eta:
            # Re-plan on the new ETA so the rule's condition is gone, then close it with the record of what was done.
            plan.update(planned_eta=evidence.get("planned_eta"), basis=evidence.get("basis"))
            plan["actions"] += ["replan", "resolve"]
        elif ex.get("detected_by") != "rule":  # a rule exception clears itself when its condition goes away
            plan["actions"].append("resolve")
    elif t == "capacity":
        plan["actions"] += ["rebook", "notify_customer"] + (["resolve"] if ex.get("detected_by") != "rule" else [])
    elif t == "damage":
        plan["actions"] += ["claim_intake"]
    elif t == "missing_docs":
        plan["actions"] += ["request_documents"]
    elif t in ("customs_hold", "payment"):
        plan["actions"] += ["note_owner_process"]
    return plan


async def _rebook(s: dict, seq: int | None, reason: str) -> dict:
    """Re-book through the carrier layer (services.carrier_booking.rebook_leg) after the same spend guardrail the
    carrier agent applies to a first booking."""
    from services import carrier_booking as cb
    leg = next((lg for lg in s.get("legs") or [] if lg.get("seq") == seq), None) or \
        next((lg for lg in s.get("legs") or [] if not lg.get("atd")), None)
    if not leg:
        return {"status": "nothing_to_rebook"}
    current = (leg.get("booking") or {}).get("carrier_id") or leg.get("carrier_id")
    ranking = await cb.rank_carriers(s, leg, exclude={current} if current else set())
    choice = ranking.get("choice") or {}
    cost = choice.get("cost_cents")
    ref = s.get("reference")
    d = await decide("carrier_agent", "rebook_leg", subject=("shipment", s["id"]), subject_label=f"{ref} / {leg['seq']}",
                     company_id=s.get("company_id"), inputs={"reason": reason, "ranked": ranking.get("ranked", [])[:5],
                                                             "current_carrier_id": current},
                     decision={"carrier_id": choice.get("carrier_id"), "carrier": choice.get("carrier_name"), "cost_cents": cost},
                     confidence=ranking.get("confidence"), spend_cents=int(cost or 0),
                     approval_kind="carrier_booking", title=f"Re-book {ref} leg {leg['seq']}",
                     title_fi=f"Varaa uudelleen: {ref} osuus {leg['seq']}", link=f"/ops/shipments/{s['id']}",
                     approval_data={"shipment_id": s["id"], "leg_seq": leg["seq"], "carrier_id": choice.get("carrier_id"),
                                    "cost_cents": cost, "reason": reason, "rebook": True})
    if not d.auto:
        return {"status": "owner_queue", "approval_id": d.approval and d.approval["id"]}
    r = await cb.rebook_leg(s["id"], leg["seq"], agent_bot("carrier_agent"), reason=reason,
                            exclude_carrier_ids=[current] if current else [])
    b = r.get("booking") or {}
    return {"status": r.get("outcome"), "booking_id": b.get("id"), "carrier": b.get("carrier_name")}


@job("exceptions.handle", agent=True, max_attempts=5, backoff=(30, 120, 600, 1800, 3600), timeout=300,
     label_fi="Poikkeaman käsittely", label_en="Exception handling")
async def handle(j):
    from services.exceptions_agent import resolve_exception
    eid = j["payload"]["exception_id"]
    async with entity_lock(f"exception:{eid}"):
        ex = await db.raw.exceptions.find_one({"id": eid}, NO_ID)
        if not ex or ex.get("demo") or ex["status"] not in ("open", "in_progress"):
            return {"skipped": "not open"}
        if (ex.get("agent") or {}).get("handled_at"):
            return {"skipped": "already handled"}
        s = await db.raw.shipments.find_one({"id": ex.get("shipment_id")}, NO_ID) if ex.get("shipment_id") else None
        plan = plan_for(ex, s)
        extra = []
        if ex.get("type") not in HANDLED:
            extra.append(policy_reason("unhandled_type", "Tämän tyyppisen poikkeaman ratkaisee henkilö",
                                       "A person resolves this type of exception"))
        if ex.get("severity") == "critical":
            extra.append(policy_reason("critical", "Kriittinen poikkeama: henkilö päättää", "Critical exception: a person decides"))
        if s is None and ex.get("type") not in ("payment",):
            extra.append(policy_reason("no_shipment", "Poikkeamalla ei ole lähetystä", "The exception has no shipment"))
        ref = (s or {}).get("reference") or ex.get("shipment_reference") or ex.get("company_name")
        d = await decide(AGENT, "handle_exception", subject=("exception", eid), subject_label=ref,
                         company_id=ex.get("company_id"),
                         inputs={"type": ex.get("type"), "severity": ex.get("severity"), "title": ex.get("title"),
                                 "detected_by": ex.get("detected_by"), "evidence": ex.get("evidence")},
                         decision=plan, confidence=None, extra_reasons=extra, approval_kind="exception_action",
                         title=f"Handle exception: {ex.get('title')}", title_fi=f"Käsittele poikkeama: {(ex.get('title_i18n') or {}).get('fi') or ex.get('title')}",
                         summary=f"{ref}: {', '.join(plan['actions']) or 'no automatic action'}",
                         link=f"/ops/exceptions?id={eid}", approval_data={"exception_id": eid, "plan": plan})
        if not d.auto:
            await db.raw.exceptions.update_one({"id": eid}, {"$set": {"agent": {"plan": plan, "decision_id": d.id,
                                                                                "owner_queue": d.approval and d.approval["id"]}}})
            return {"owner_queue": d.approval and d.approval["id"], "plan": plan}
        return await execute(ex, s, plan, d.id, resolve_exception)


def notice_key(exception_id: str) -> str:
    """The one dedupe key of the customer's notice about an exception. The exception agent is its single sender: the
    portal/e-mail message (agent_messages), the SMS (sms_outbox, per user) and the AI draft all check this key."""
    return f"exception-notice:{exception_id}"


async def notice_sent(exception_id: str) -> bool:
    return bool(await db.raw.agent_messages.find_one({"_id": notice_key(exception_id)}, {"_id": 1}))


async def send_exception_notice(ex: dict, s: dict, fi: str, en: str) -> dict | None:
    """The customer's notice about an exception, once: a message in the shipment thread (e-mail per the user's
    settings), s14's exception SMS to users who opted in, and any pending AI draft about it withdrawn."""
    from services import messaging, notify_channels
    key = notice_key(ex["id"])
    msg = await customer_message(s["company_id"], s["id"], fi, en, subject=f"{s.get('reference')}: poikkeama / exception",
                                 key=key)
    await notify_channels.notify_shipment_sms(s, "exception", dedupe_key=key)
    if ex.get("ai_message_id"):
        await messaging.supersede_ai_drafts_for_message(ex["ai_message_id"], "The exception agent sent the customer notice")
    return msg


async def execute(ex: dict, s: dict | None, plan: dict, decision_id: str | None, resolve_exception) -> dict:
    bot = agent_bot(AGENT)
    eid = ex["id"]
    done = []
    ref = (s or {}).get("reference")
    for act in plan["actions"]:
        if act == "rebook" and s:
            r = await _rebook(s, plan.get("leg_seq"), f"exception {ex.get('type')}: {ex.get('title')}")
            done.append({"rebook": r})
        elif act == "notify_customer" and s:
            eta = plan.get("new_eta") or s.get("eta")
            fi_title = (ex.get("title_i18n") or {}).get("fi") or ex.get("title")
            await send_exception_notice(
                ex, s,
                f"Lähetyksessä {ref} on poikkeama: {fi_title}. Uusi arvioitu saapuminen on {_fi_dt(eta)}. "
                f"Seuraamme tilannetta ja ilmoitamme, jos aikataulu muuttuu.",
                f"There is an exception on shipment {ref}: {ex.get('title')}. The new estimated arrival is {_en_dt(eta)}. "
                f"We are following it and will tell you if the schedule changes again.")
            done.append("customer_notified")
        elif act == "replan" and s:
            pending = any(isinstance(x, dict) and (x.get("rebook") or {}).get("status") == "owner_queue" for x in done)
            # a re-booking waiting for a person decides the new plan; until then the exception stays open
            done.append({"replan": {"status": "waits_for_rebook"} if pending else await _replan(s, ex, plan, bot)})
        elif act == "claim_intake" and s:
            await customer_message(
                s["company_id"], s["id"],
                f"Pahoittelemme lähetyksen {ref} vahinkoa. Kirjaa näkyvät vauriot rahtikirjaan tai luovutuskuittaukseen, "
                f"ota valokuvat ja jätä korvausvaatimus Huolitsijassa (Reklamaatiot). Piilevistä vaurioista on ilmoitettava "
                f"7 päivän kuluessa toimituksesta (PSYM 2015 § 27).",
                f"We are sorry about the damage to shipment {ref}. Note visible damage on the delivery receipt, take "
                f"photos and file a claim in Huolitsija (Claims). Concealed damage must be notified within 7 days of "
                f"delivery (NSAB 2015 § 27).", subject=f"{ref}: vahinko / damage", key=f"claim-intake:{eid}")
            done.append("claim_intake_sent")
        elif act == "request_documents" and s:
            missing = ((ex.get("evidence") or {}).get("missing") or [])
            await customer_message(
                s["company_id"], s["id"],
                f"Lähetyksestä {ref} puuttuu asiakirjoja{': ' + ', '.join(missing) if missing else ''}. Lataa ne "
                f"lähetyksen Asiakirjat-välilehdelle, jotta tulliselvitys ei viivästy.",
                f"Shipment {ref} is missing documents{': ' + ', '.join(missing) if missing else ''}. Please upload them on "
                f"the shipment's Documents tab so customs clearance is not delayed.",
                subject=f"{ref}: asiakirjat / documents", key=f"docs-missing:{eid}")
            done.append("documents_requested")
        elif act == "note_owner_process":
            done.append("left_to_" + ("customs_agent" if ex.get("type") == "customs_hold" else "dunning_agent"))
        elif act == "resolve":
            pass
    status = "in_progress"
    upd = {"agent": {"plan": plan, "decision_id": decision_id, "handled_at": now_iso(), "done": done},
           "status": "in_progress", "updated_at": now_iso(), "owner": {"type": "agent", "id": AGENT, "name": bot["name"]}}
    await db.raw.exceptions.update_one({"id": eid, "status": {"$in": ["open", "in_progress"]}},
                                       {"$set": upd, "$push": {"history": {"at": now_iso(), "by": bot["name"], "action": "handled",
                                                                           "note": ", ".join(str(x) for x in done)[:300]}}})
    if "resolve" in plan["actions"]:
        rebook = next((x["rebook"] for x in done if isinstance(x, dict) and "rebook" in x), None)
        if rebook and rebook.get("status") == "owner_queue":
            return {"handled": done, "status": status}
        text = "Customer informed of the new ETA" + (f" {plan['new_eta'][:16]} UTC" if plan.get("new_eta") else "")
        if any(isinstance(x, dict) and "replan" in x for x in done):
            text += f"; plan moved from {str(plan.get('planned_eta') or '?')[:16]} UTC"
        if rebook:
            text += f"; leg re-booked ({rebook.get('carrier') or rebook.get('status')})"
        await resolve_exception(eid, resolution=text + " (Autopilot).", actor=bot)
        status = "resolved"
    return {"handled": done, "status": status}


async def _replan(s: dict, ex: dict, plan: dict, bot: dict) -> dict:
    """Accept the new ETA as the plan (shipments.eta_replanned; eta_planned stays the booking promise for on-time KPIs),
    with a replans[] record, a shipment event and an audit row."""
    from core.audit import audit
    from services.shipments import _event
    new_eta = plan["new_eta"]
    rec = {"at": now_iso(), "from": plan.get("planned_eta"), "to": new_eta, "basis": plan.get("basis"),
           "slip_hours": plan.get("slip_hours"), "exception_id": ex["id"], "by": {"type": "agent", "id": AGENT, "name": bot["name"]}}
    r = await db.raw.shipments.update_one({"id": s["id"], "eta_replanned": {"$ne": new_eta}},
                                          {"$set": {"eta_replanned": new_eta, "updated_at": now_iso()}, "$push": {"replans": rec}})
    if not r.modified_count:
        return {"status": "already_planned", "to": new_eta}
    note = f"Plan {str(rec['from'] or '?')[:16]} → {new_eta[:16]} UTC (exception {ex['id'][:8]}, slip {rec['slip_hours']} h)"
    await _event(s, "replanned", "Re-planned on the new ETA", source="exception_agent", actor=bot, note=note,
                 raw={"replan": rec})
    await audit(bot, "shipment.replanned", "shipment", s["id"], rec, company_id=s.get("company_id"))
    return {"status": "replanned", "from": rec["from"], "to": new_eta}


@on("exception.opened")
async def _on_opened(p):
    ex = p["exception"]
    if ex.get("demo"):
        return
    await enqueue("exceptions.handle", {"exception_id": ex["id"]}, company_id=ex.get("company_id"),
                  subject=("exception", ex["id"]), key=f"exception:{ex['id']}:{await mode_key()}")


@on("approval.decided")
async def _on_decided(p):
    a = p["approval"]
    if a.get("kind") != "exception_action" or a["status"] != "approved":
        return
    from services.exceptions_agent import resolve_exception
    ex = await db.raw.exceptions.find_one({"id": (a.get("data") or {}).get("exception_id") or a["ref_id"]}, NO_ID)
    if not ex or ex["status"] not in ("open", "in_progress") or (ex.get("agent") or {}).get("handled_at"):
        return
    s = await db.raw.shipments.find_one({"id": ex.get("shipment_id")}, NO_ID) if ex.get("shipment_id") else None
    plan = (a.get("data") or {}).get("plan") or plan_for(ex, s)
    await execute(ex, s, plan, (a.get("data") or {}).get("agent_decision_id"), resolve_exception)


@sweeper
async def sweep_exceptions():
    n = 0
    mk = await mode_key()
    async for ex in db.raw.exceptions.find({"status": "open", "demo": {"$ne": True}, "agent.handled_at": {"$exists": False},
                                            "created_at": {"$gte": await agents_epoch()}},
                                           {"_id": 0, "id": 1, "company_id": 1}).limit(100):
        await enqueue("exceptions.handle", {"exception_id": ex["id"]}, company_id=ex.get("company_id"),
                      subject=("exception", ex["id"]), key=f"exception:{ex['id']}:{mk}", source="sweep")
        n += 1
    return n
