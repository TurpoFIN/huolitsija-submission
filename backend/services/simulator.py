"""Movement simulator for sandbox test companies (SIMULATOR_ENABLED=true; off by default).

A clock-driven stand-in for the carriers' tracking feeds and AIS: every SIMULATOR_STEP_SECONDS it moves each active
shipment of a sandbox company (core.sandbox) one planned milestone along its legs and sends that milestone through
the same tracking feed a carrier would (services.agents.tracking_agent, provider "simulator"), so everything
downstream (exception, customs, delivery and invoice agents) runs exactly as for a real shipment. It never touches a
real customer's shipment.

Per shipment it emits, in order: the legs' planned milestones; one carrier exception (a delay with a new ETA) after
the main leg departs; on arrival of an import with customs, a customs hold until the Tulli sandbox releases the
declaration (the release itself comes from Tulli, never from the simulator); proof of delivery on the last step.
"""
import logging
from datetime import timedelta

from core import config
from core.db import db
from core.jobs import job, schedule
from core.sandbox import is_sandbox_company
from core.util import NO_ID, iso, now, now_iso, parse_iso

log = logging.getLogger("simulator")

ENABLED = config.env_bool("SIMULATOR_ENABLED", False)
STEP_SECONDS = float(config.env("SIMULATOR_STEP_SECONDS", "30"))
ACTIVE = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception"]
SIM_ACTOR = {"type": "system", "id": "simulator", "name": "Simulaattori / Simulator"}


def enabled() -> bool:
    return config.env_bool("SIMULATOR_ENABLED", ENABLED)


def _legs_booked(s: dict) -> bool:
    legs = s.get("legs") or []
    return bool(legs) and all((lg.get("booking") or {}).get("status") == "confirmed" or lg.get("atd") for lg in legs)


async def _declaration(s: dict) -> dict | None:
    return await db.raw.customs_declarations.find_one({"shipment_id": s["id"], "status": {"$nin": ["cancelled", "invalidated"]}},
                                                      NO_ID, sort=[("created_at", -1)])


def _event_id(s: dict, code: str, seq) -> str:
    return f"{s['id']}:{code}:{seq}"


async def _emit(s: dict, ev: dict):
    from services.agents.tracking_agent import store_feed_event
    ev = {"shipment_id": s["id"], "shipment_ref": s.get("reference"), "at": now_iso(), **ev}
    await store_feed_event("simulator", ev, verified=True)


async def step(s: dict) -> str | None:
    """Advance one shipment by one step. Returns what was sent (or None when it waits)."""
    sim = s.get("simulator") or {}
    if sim.get("next_at") and parse_iso(sim["next_at"]) > now():
        return None
    if not _legs_booked(s):
        return None
    if await db.raw.feed_events.find_one({"provider": "simulator", "event.shipment_id": s["id"], "status": "queued"}, {"_id": 1}):
        return None  # the previous step is still being applied
    nxt = next((m for m in s.get("milestones") or [] if not m.get("at")), None)
    if not nxt:
        return None
    code, seq = nxt["code"], nxt.get("leg_seq")
    sent = None
    main_departed = any(m["code"] == "departed" and m.get("at") for m in s.get("milestones") or [])
    if main_departed and not sim.get("exception_sent") and s.get("status") not in ("customs_hold",):
        leg = next((lg for lg in s.get("legs") or [] if lg.get("atd") and not lg.get("ata")), None)
        eta = parse_iso((leg or {}).get("eta")) or now()
        new_eta = iso(eta + timedelta(hours=18))
        await _emit(s, {"event_id": _event_id(s, "exception", (leg or {}).get("seq")), "code": "exception",
                        "leg_seq": (leg or {}).get("seq"), "exception": {
                            "type": "delay", "severity": "medium", "new_eta": new_eta,
                            "title": "Carrier reports an 18-hour delay", "title_fi": "Kuljetusliike ilmoittaa 18 tunnin viiveestä",
                            "description": f"Port congestion at {((leg or {}).get('to') or {}).get('name') or 'the next port'}; new ETA {new_eta[:16]} UTC.",
                            "description_fi": f"Ruuhkaa satamassa {((leg or {}).get('to') or {}).get('name') or 'seuraava satama'}; uusi arvioitu saapuminen {new_eta[:16]} UTC."}})
        await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"simulator.exception_sent": now_iso()}})
        sent = "exception"
    elif code == "customs_released":
        d = await _declaration(s)
        if d and d.get("status") == "released":
            if s.get("status") == "customs_hold":
                return None  # services.customs.on_released lifts the hold and records the release
            await _emit(s, {"event_id": _event_id(s, code, seq), "code": "customs_released", "leg_seq": seq,
                            "note": f"Released by Tulli, MRN {d.get('mrn')}"})
            sent = code
        elif s.get("status") != "customs_hold" and not sim.get("customs_hold"):
            from services import shipments as svc
            await svc.add_hold(s["id"], "customs", "Awaiting release by Tulli (sandbox)", SIM_ACTOR, source="simulator")
            await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"simulator.customs_hold": now_iso()}})
            sent = "customs_hold"
        else:
            return None
    elif code == "export_cleared":
        d = await _declaration(s)
        if s.get("customs") and not (d and d.get("status") == "released"):
            return None
        await _emit(s, {"event_id": _event_id(s, code, seq), "code": code, "leg_seq": seq})
        sent = code
    elif code == "delivered":
        consignee = ((s.get("parties") or {}).get("consignee") or {}).get("contact_name") or "Vastaanottaja"
        await _emit(s, {"event_id": _event_id(s, code, seq), "code": code, "leg_seq": seq,
                        "pod": {"signed_by": consignee, "at": now_iso()}})
        sent = code
    else:
        leg = next((lg for lg in s.get("legs") or [] if lg["seq"] == seq), {})
        ev = {"event_id": _event_id(s, code, seq), "code": code, "leg_seq": seq}
        if code == "departed" and leg.get("mode") == "sea" and not (leg.get("vessel") or {}).get("name"):
            ev["vessel"] = {"name": "SIM AURORA", "imo": None, "mmsi": None}
        await _emit(s, ev)
        sent = code
    await db.raw.shipments.update_one({"id": s["id"]}, {"$set": {"simulator.next_at": iso(now() + timedelta(seconds=STEP_SECONDS)),
                                                                 "simulator.last": sent, "simulator.last_at": now_iso()}})
    return sent


@job("simulator.tick", max_attempts=1, timeout=120, label_fi="Kuljetussimulaattori (testiyritykset)",
     label_en="Movement simulator (test companies)")
async def tick(j=None):
    if not enabled():
        return {"skipped": "SIMULATOR_ENABLED is off"}
    out = {}
    companies: dict[str, bool] = {}
    async for s in db.raw.shipments.find({"status": {"$in": ACTIVE}, "demo": {"$ne": True}}, NO_ID).limit(200):
        cid = s.get("company_id")
        if cid not in companies:
            companies[cid] = is_sandbox_company(await db.raw.companies.find_one({"id": cid}, NO_ID))
        if not companies[cid]:
            continue
        try:
            r = await step(s)
        except Exception as exc:  # noqa: BLE001 - one bad shipment must not stop the others
            log.exception("simulator step failed for %s", s.get("reference"))
            r = f"error: {getattr(exc, 'detail', exc)}"
        if r:
            out[s.get("reference")] = r
    return out


schedule("simulator.every_step", job="simulator.tick", every_seconds=STEP_SECONDS,
         label_fi="Kuljetussimulaattori", label_en="Movement simulator")()
