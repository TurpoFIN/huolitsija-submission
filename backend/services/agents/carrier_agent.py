"""Carrier agent: picks a carrier for every leg of a confirmed booking (rate, lane, reliability, integration) and books
it through the carrier integration layer (services.carrier_booking: sandbox, e-mail/log, partner portal or live
DCSA, one code path). The leg's purchase cost counts against the daily spend limit. A booking request that no carrier
confirms within CARRIER_CONFIRM_HOURS goes to the owner; a re-booking after an exception uses the same path."""
from datetime import timedelta

from core import config
from core.agent_runtime import agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.util import NO_ID, now, parse_iso
from services.agents.base import agents_epoch, entity_lock, mode_key, sweeper

AGENT = "carrier_agent"
CONFIRM_HOURS = float(config.env("CARRIER_CONFIRM_HOURS", "24"))
ACTIVE = ("requested", "confirmed")


async def _active_booking(shipment_id: str, seq: int) -> dict | None:
    return await db.raw.carrier_bookings.find_one({"shipment_id": shipment_id, "leg_seq": seq, "status": {"$in": list(ACTIVE)}},
                                                  NO_ID, sort=[("created_at", -1)])


async def book_one_leg(s: dict, leg: dict, *, reason: str, exclude: set | None = None) -> dict:
    """Rank, decide, book one leg. Returns {status, booking_id?, approval_id?}."""
    from services import carrier_booking as cb
    if leg.get("booking_parent_seq"):
        parent = next(lg for lg in s["legs"] if lg["seq"] == leg["booking_parent_seq"])
        active = await _active_booking(s["id"], parent["seq"])
        if active and await cb._group_covered(s, cb.booking_leg(s, parent), await cb._accepted_option(s)):
            return {"status": active["status"], "booking_id": active["id"], "covered_by_leg_seq": parent["seq"]}
        return await book_one_leg(s, parent, reason=reason, exclude=exclude)
    crossing = leg.get("ferry_crossing") or ({"operator": leg.get("carrier")} if s.get("mode") == "road" and leg.get("mode") == "sea" else None)
    group = leg.get("booking_group")
    covered = await cb._group_covered(s, cb.booking_leg(s, leg), await cb._accepted_option(s)) if group else False
    if (crossing and not covered) or (group and not covered):
        operator = (crossing or {}).get("operator") or next(
            ((p.get("ferry_crossing") or {}).get("operator") for p in s.get("legs") or []
             if p.get("booking_parent_seq") == leg["seq"] and p.get("ferry_crossing")), None)
        why = f"ferry crossing needs a separate booking with {operator}" if operator else \
              "ferry crossing needs a separate booking; operator must be confirmed"
        if group:
            why += "; route is outside the accepted quote's coverage"
        d = await decide(AGENT, "book_leg", subject=("shipment", s["id"]), company_id=s.get("company_id"),
                         inputs={"leg": leg["seq"], "crossing": crossing, "reason": why},
                         extra_reasons=[policy_reason("ferry_separate_booking", "Lauttamatka tarvitsee erillisen varauksen: "
                                                      + (operator or "operaattori vahvistettava"), why)],
                         approval_kind="carrier_booking", title=why, title_fi="Lauttamatkan varaus odottaa käsittelyä",
                         link=f"/ops/shipments/{s['id']}",
                         approval_data={"shipment_id": s["id"], "leg_seq": leg["seq"], "reason": why,
                                        "suggested_operator": operator, "requires_separate_booking": True, "notify_owner_email": False})
        return {"status": "owner_queue", "approval_id": d.approval and d.approval["id"], "reason": why}
    from services.agents.quote_agent import autosend, carrier_human_authorized
    gate = await autosend(shipment=s)
    if not gate.auto:
        return {"status": "owner_queue", "approval_id": gate.approval["id"]}
    ranking = await cb.rank_carriers(s, leg, exclude=exclude or set())
    choice = ranking.get("choice")
    ref = s.get("reference")
    if not choice:
        d = await decide(AGENT, "book_leg", subject=("shipment", s["id"]), subject_label=f"{ref} / {leg['seq']}",
                         company_id=s.get("company_id"), inputs={"leg": leg.get("seq"), "ranked": ranking.get("ranked", [])[:5]},
                         extra_reasons=[policy_reason("no_carrier", "Osuudelle ei löytynyt kuljetusliikettä", "No eligible carrier for this leg")],
                         approval_kind="carrier_booking", title=f"Find a carrier for {ref} leg {leg['seq']}",
                         title_fi=f"Etsi kuljetusliike: {ref} osuus {leg['seq']}", link=f"/ops/shipments/{s['id']}",
                         approval_data={"shipment_id": s["id"], "leg_seq": leg["seq"]})
        return {"status": "owner_queue", "approval_id": d.approval and d.approval["id"]}
    cost = choice.get("cost_cents")
    if cost is None:
        cost = await cb.leg_cost_cents(s, leg)
    d = await decide(
        AGENT, "book_leg", subject=("shipment", s["id"]), subject_label=f"{ref} / {leg['seq']}", company_id=s.get("company_id"),
        inputs={"leg": {"seq": leg.get("seq"), "mode": leg.get("mode"), "from": (leg.get("from") or {}).get("unlocode"),
                        "to": (leg.get("to") or {}).get("unlocode"), "etd": leg.get("etd")},
                "ranked": ranking.get("ranked", [])[:5], "reason": reason},
        rule_check=await carrier_human_authorized(s),
        decision={"carrier_id": choice["carrier_id"], "carrier": choice.get("carrier_name"), "mode": choice.get("mode"),
                  "cost_cents": cost}, confidence=ranking.get("confidence"), spend_cents=int(cost or 0),
        extra_reasons=[] if cost is not None else [policy_reason("cost_unknown", "Osuuden ostohinta ei ole tiedossa",
                                                                 "The leg's purchase price is unknown")],
        approval_kind="carrier_booking", title=f"Book {choice.get('carrier_name')} for {ref} leg {leg['seq']}",
        title_fi=f"Varaa {choice.get('carrier_name')}: {ref} osuus {leg['seq']}",
        summary=f"{leg.get('mode')} {(leg.get('from') or {}).get('name')} → {(leg.get('to') or {}).get('name')}, "
                f"cost {(cost or 0) / 100:,.2f} EUR",
        link=f"/ops/shipments/{s['id']}", amount_cents=cost,
        approval_data={"shipment_id": s["id"], "leg_seq": leg["seq"], "carrier_id": choice["carrier_id"], "cost_cents": cost,
                       "reason": reason})
    if not d.auto:
        return {"status": "owner_queue", "approval_id": d.approval and d.approval["id"]}
    b = await cb.book_leg(s["id"], leg["seq"], choice["carrier_id"], agent_bot(AGENT), reason=reason, cost_cents=cost)
    return {"status": b.get("status"), "booking_id": b.get("id"), "carrier": choice.get("carrier_name")}


@job("carrier.select_and_book", agent=True, max_attempts=6, backoff=(60, 300, 900, 1800, 3600, 7200), timeout=300,
     label_fi="Kuljetusliikkeen valinta ja varaus", label_en="Carrier selection and booking")
async def select_and_book(j):
    from services import carrier_booking as cb
    sid = j["payload"]["shipment_id"]
    async with entity_lock(f"carrier:{sid}"):
        s = await db.raw.shipments.find_one({"id": sid}, NO_ID)
        if not s or s.get("status") in ("cancelled", "delivered") or s.get("demo"):
            return {"skipped": "not bookable"}
        if (s.get("booking_confirmation") or {}).get("status") != "confirmed":
            return {"skipped": "booking not confirmed"}
        out, waiting = {}, []
        for leg in s.get("legs") or []:
            if leg.get("atd") or leg.get("status") == "completed":
                continue
            if leg.get("booking_parent_seq"):
                parent = await _active_booking(sid, leg["booking_parent_seq"])
                out[leg["seq"]] = out.get(leg["booking_parent_seq"], parent["status"] if parent else "owner_queue")
                continue
            if leg.get("booking_group") and not await cb._group_covered(s, cb.booking_leg(s, leg), await cb._accepted_option(s)):
                r = await book_one_leg(s, leg, reason="route is outside the accepted quote's coverage")
                out[leg["seq"]] = r["status"]
                continue
            active = await _active_booking(sid, leg["seq"])
            if active and active["status"] == "confirmed":
                out[leg["seq"]] = "confirmed"
                continue
            if active and active["status"] == "requested":
                active = await cb.poll_booking(active["id"])
                if active.get("status") == "confirmed":
                    out[leg["seq"]] = "confirmed"
                    continue
                created = parse_iso(active.get("created_at"))
                if created and now() - created > timedelta(hours=CONFIRM_HOURS):
                    d = await decide(AGENT, "confirm_carrier_booking", subject=("shipment", sid),
                                     subject_label=f"{s.get('reference')} / {leg['seq']}", company_id=s.get("company_id"),
                                     extra_reasons=[policy_reason("no_confirmation",
                                                                  f"{active.get('carrier_name')} ei ole vahvistanut varausta {CONFIRM_HOURS:.0f} tunnissa",
                                                                  f"{active.get('carrier_name')} has not confirmed within {CONFIRM_HOURS:.0f} hours")],
                                     approval_kind="carrier_booking", title=f"Confirm carrier booking {s.get('reference')} leg {leg['seq']}",
                                     title_fi=f"Vahvista kuljetusvaraus {s.get('reference')} osuus {leg['seq']}",
                                     link=f"/ops/shipments/{sid}", approval_data={"shipment_id": sid, "leg_seq": leg["seq"],
                                                                                    "booking_id": active["id"]})
                    out[leg["seq"]] = "owner_queue"
                    continue
                waiting.append(leg["seq"])
                out[leg["seq"]] = "requested"
                continue
            if await db.raw.approvals.find_one({"kind": "carrier_booking", "status": "pending", "ref_id": sid,
                                                "data.leg_seq": leg["seq"]}, {"_id": 1}):
                out[leg["seq"]] = "owner_queue"
                continue
            r = await book_one_leg(s, leg, reason=j["payload"].get("reason") or "initial booking")
            out[leg["seq"]] = r["status"]
            if r["status"] == "requested":
                waiting.append(leg["seq"])
        if waiting:
            raise JobDeferred(600, f"waiting for carrier confirmation on legs {waiting}")
        return {str(k): v for k, v in out.items()}  # MongoDB documents need string keys


@on("approval.decided")
async def _on_decided(p):
    a = p["approval"]
    if a.get("kind") != "carrier_booking" or a["status"] != "approved":
        return
    from services import carrier_booking as cb
    data = a.get("data") or {}
    if data.get("manual_group_change"):
        return  # A person arranges the remaining carriage; approval must not cancel/rebook a started group.
    actor = p.get("actor") or agent_bot(AGENT)
    if data.get("booking_id"):
        return  # a person confirms an e-mailed booking on the shipment page (carrier reference needed)
    if data.get("rebook") and data.get("shipment_id"):
        s = await db.raw.shipments.find_one({"id": data["shipment_id"]}, NO_ID) or {}
        leg = next((lg for lg in s.get("legs") or [] if lg.get("seq") == int(data["leg_seq"])), {})
        current = (leg.get("booking") or {}).get("carrier_id") or leg.get("carrier_id")
        await cb.rebook_leg(data["shipment_id"], int(data["leg_seq"]), actor, reason=data.get("reason") or "approved by the owner",
                            exclude_carrier_ids=[current] if current else [])
        return
    if data.get("carrier_id") and data.get("shipment_id") is not None:
        override = (a.get("decision_data") or {}).get("carrier_id") or data["carrier_id"]
        await cb.book_leg(data["shipment_id"], int(data["leg_seq"]), override, actor,
                          reason=data.get("reason") or "approved by the owner", cost_cents=data.get("cost_cents"))


@sweeper
async def sweep_carriers():
    n = 0
    mk = await mode_key()
    async for s in db.raw.shipments.find({"status": {"$in": ["booked", "picked_up", "in_transit", "at_hub"]},
                                          "booking_confirmation.status": "confirmed", "demo": {"$ne": True},
                                          "created_at": {"$gte": await agents_epoch()}},
                                         {"_id": 0, "id": 1, "company_id": 1, "legs.seq": 1, "legs.booking": 1, "legs.atd": 1}).limit(100):
        if all((lg.get("booking") or {}).get("status") == "confirmed" or lg.get("atd") for lg in s.get("legs") or []):
            continue
        await enqueue("carrier.select_and_book", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"carrier:{s['id']}:sweep:{mk}", source="sweep")
        n += 1
    return n
