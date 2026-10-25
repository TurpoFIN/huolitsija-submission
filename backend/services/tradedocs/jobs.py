"""Milestone-driven issuing and sending of trade documents on s2's durable job system.

One idempotent job per shipment state, `tradedocs.progress {shipment_id}`:
  1. work out which milestones the shipment has reached (reached()), from its status, legs and milestones, so a missed
     event or a restart never leaves a document behind (catch-up);
  2. for every kind due on a reached milestone (catalog.auto_kinds): draft it, or issue the final version (transport
     documents pass the Autopilot gate first: decide("trade_documents", "issue_<kind>"));
  3. send each new version to its parties (dispatch.send, gated for outside parties).

Triggers: shipment.created, shipment.status_changed, customs.released, shipment.delivered, approval.decided
(document_review cards of this agent), and s2's 30-second sweep. Demo shipments are left to people (as s2 does).
Without s2's core.jobs (before it merges) the same work runs as a background task from the event.
TRADEDOCS_AUTO=false turns the automation off (manual issuing in the UI and API still works).
"""
from __future__ import annotations

import asyncio
import logging

from core import config
from core.audit import agent_actor
from core.db import db
from core.events import on
from core.util import NO_ID
from services.tradedocs import catalog, data, dispatch, service

log = logging.getLogger("tradedocs.jobs")
AGENT = "trade_documents"

try:  # s2's durable jobs (feat/autopilot-ops)
    from core.jobs import JobDeferred, enqueue, job
except ImportError:  # pragma: no cover - main before s2 merges
    JobDeferred, enqueue, job = None, None, None


def enabled() -> bool:
    return (config.env("TRADEDOCS_AUTO", "true") or "true").lower() not in ("0", "false", "no", "off")


def _achieved(s: dict, code: str, min_leg: int | None = None) -> bool:
    for m in s.get("milestones") or []:
        if m.get("code") == code and m.get("at") and (min_leg is None or (m.get("leg_seq") or 0) >= min_leg):
            return True
    return False


def arrival_evidence(s: dict) -> bool:
    """Automatic notices require a recorded arrival, not a schedule/status inference."""
    from datetime import datetime, timezone
    mains = [leg for leg in s.get("legs") or [] if leg.get("mode") == s.get("mode")]
    last_seq = mains[-1].get("seq") if mains else None
    for milestone in s.get("milestones") or []:
        if milestone.get("code") != "arrived" or milestone.get("source") not in ("carrier_api", "partner", "manual"):
            continue
        if last_seq is not None and milestone.get("leg_seq") != last_seq:
            continue
        try:
            at = datetime.fromisoformat(milestone["at"].replace("Z", "+00:00"))
            if at.tzinfo is not None and at <= datetime.now(timezone.utc):
                return True
        except (KeyError, ValueError, TypeError):
            continue
    return False


def reached(s: dict) -> list[str]:
    """Milestones (catalog.MILESTONES) the shipment has reached, in order."""
    status = s.get("status")
    if status == "cancelled":
        return []
    bc = (s.get("booking_confirmation") or {}).get("status")
    if bc in ("pending_owner", "rejected"):
        return []
    out = ["booking_confirmed"]
    moving = status in ("picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered") or \
        (status == "exception" and (s.get("atd") or _achieved(s, "picked_up")))
    if moving or _achieved(s, "picked_up") or _achieved(s, "received"):
        out += ["picked_up", "received"]
    legs = s.get("legs") or []
    mode = s.get("mode")
    mains = [lg for lg in legs if lg.get("mode") == mode and lg.get("role") in ("main", "feeder", "onward")] or \
        [lg for lg in legs if lg.get("mode") == mode]
    first, last = (mains[0] if mains else {}), (mains[-1] if mains else {})
    departed = bool(first.get("atd")) or _achieved(s, "departed") or status in ("in_transit", "at_hub", "customs_hold",
                                                                                 "out_for_delivery", "delivered")
    if departed:
        out.append("departed")
    arrived = bool(last.get("ata")) or _achieved(s, "arrived", last.get("seq")) or \
        status in ("customs_hold", "out_for_delivery", "delivered")
    if arrived:
        out.append("arrived")
    released = _achieved(s, "customs_released") or status in ("out_for_delivery", "delivered") or \
        (arrived and not s.get("customs_clearance"))
    if released:
        out.append("customs_released")
    if status == "delivered":
        out.append("delivered")
    return out


async def progress(shipment_id: str) -> dict:
    from services.agents.base import entity_lock
    async with entity_lock(f"tradedocs:{shipment_id}"):
        return await _progress(shipment_id)


async def _progress(shipment_id: str) -> dict:
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not enabled():
        return {"skipped": "trade document automation disabled"}
    if not s or s.get("status") == "cancelled":
        return {"skipped": "no shipment"}
    if s.get("compliance_hold"):
        return {"skipped": "shipment compliance hold"}
    if s.get("demo"):
        return {"skipped": "demo shipment"}
    ms = reached(s)
    if not ms:
        return {"skipped": "booking not confirmed"}
    from core.settings import get_settings
    if (await get_settings()).get("autopilot_halted"):
        return {"skipped": "autopilot halted"}
    ctx = await data.build_context(s)
    done = {"issued": [], "drafted": [], "waiting": [], "sent": [], "failed": [], "not_applicable": []}
    for kind in catalog.ORDER:
        spec = catalog.KINDS[kind]
        if kind not in set().union(*(catalog.auto_kinds(ctx, m) for m in ms)):
            continue
        if kind == "arrival_notice" and not arrival_evidence(s):
            done["not_applicable"].append({"kind": kind, "reason": "Recorded carrier, partner or manual arrival evidence is required"})
            continue
        final_due = spec["issue_on"] in ms
        cur = await service.current(shipment_id, kind)
        if cur and (cur["status"] == "issued" or (cur["status"] == "draft" and not final_due)):
            await _send_if_new(cur, ms[-1], done)
            continue
        if final_due and spec["transport"]:
            probe = cur or {"id": f"{shipment_id}:{kind}", "doc_no": "—", "version": 0, "kind": kind, "shipment_id": shipment_id,
                            "company_id": s.get("company_id")}
            g = await dispatch.gate(f"issue_{kind}", probe,
                                    title=f"Issue {spec['title']['en']} for {s.get('reference')}",
                                    title_fi=f"Laadi {spec['title']['fi']} lähetykselle {s.get('reference')}",
                                    summary=f"{ctx['origin'].get('name')} → {ctx['destination'].get('name')}",
                                    inputs={"kind": kind, "milestone": spec["issue_on"], "shipment": s.get("reference")})
            if not g["auto"]:
                if not cur:  # the parties still get a draft to check while the owner decides
                    cur = await service.issue(shipment_id, kind, final=False, source="autopilot", actor=agent_actor(AGENT))
                    done["drafted"].append(cur["doc_no"])
                    await _send_if_new(cur, ms[-1], done)
                done["waiting"].append({"kind": kind, **{k: g.get(k) for k in ("approval_id", "outcome")}})
                continue
        try:
            doc = await service.issue(shipment_id, kind, final=final_due, source="autopilot", actor=agent_actor(AGENT))
        except Exception as exc:  # noqa: BLE001  - 409: does not apply (data changed); recorded, never blocks the others
            done["not_applicable"].append({"kind": kind, "reason": getattr(exc, "detail", str(exc))})
            continue
        (done["issued"] if final_due else done["drafted"]).append(doc["doc_no"])
        await _send_if_new(doc, ms[-1], done)
        if final_due and kind == "house_bl" and not doc.get("ebl"):
            from services.tradedocs import ebl
            done["ebl"] = await ebl.request_issuance(doc)
    return {"milestones": ms, **done}


async def _send_if_new(doc: dict, milestone: str, done: dict):
    r = await dispatch.send(doc["id"], milestone=milestone)
    done["sent"] += [e["to"] for e in r.get("sent") or []]
    done["failed"] += [e["to"] for e in r.get("failed") or []]
    if r.get("waiting"):
        done["waiting"].append({"kind": doc["kind"], "send": True, "approval_id": r["waiting"].get("approval_id")})


# ------------------------------------------------------------------------------------------------ job + triggers
if job:
    @job("tradedocs.progress", agent=True, max_attempts=6, timeout=240, label_fi="Kauppa- ja kuljetusasiakirjat",
         label_en="Trade documents")
    async def progress_job(j):
        result = await progress(j["payload"]["shipment_id"])
        if result.get("failed"):
            raise RuntimeError("Trade document mail submission failed; retry outstanding recipients")
        return result

    @job("tradedocs.send", agent=True, max_attempts=6, timeout=120, label_fi="Asiakirjan lähetys", label_en="Send document")
    async def send_job(j):
        p = j["payload"]
        doc = await service.get(p["doc_id"])
        shipment = await db.shipments.find_one({"id": doc["shipment_id"]}, NO_ID) if doc else None
        if not shipment or shipment.get("demo"):
            return {"skipped": "demo or missing shipment; delivery remains manual"}
        result = await dispatch.send(p["doc_id"], milestone=p.get("milestone"), gated=True)
        if result.get("failed"):
            raise RuntimeError("Trade document mail submission failed; retry outstanding recipients")
        return result


async def schedule_progress(s: dict, reason: str):
    if not enabled() or not s or s.get("demo"):
        return
    booking = s.get("booking_confirmation") or {}
    key = f"tradedocs:{s['id']}:{s.get('status')}:{booking.get('status')}:{reason}"
    if enqueue:
        try:
            from services.agents.base import mode_key
            key += ":" + await mode_key()
        except Exception:  # noqa: BLE001
            pass
        await enqueue("tradedocs.progress", {"shipment_id": s["id"]}, key=key, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), source="event")
    else:
        asyncio.create_task(_safe_progress(s["id"]))


@on("tradedoc.amended")
async def _amended(p):
    doc = p.get("document") or {}
    if not enabled() or not doc or not enqueue:
        return
    shipment = await db.shipments.find_one({"id": doc["shipment_id"]}, NO_ID)
    if not shipment or shipment.get("demo"):
        return  # same manual-only boundary as milestone progression
    await enqueue("tradedocs.send", {"doc_id": doc["id"], "milestone": "amended"},
                  key=f"tradedocs-amended:{doc['id']}", company_id=doc.get("company_id"),
                  subject=("shipment", doc["shipment_id"]), source="event")


async def _safe_progress(shipment_id: str):
    try:
        await progress(shipment_id)
    except Exception:  # noqa: BLE001
        log.exception("trade documents for %s failed", shipment_id)


@on("shipment.created")
async def _created(p):
    await schedule_progress(p.get("shipment"), "created")


@on("shipment.status_changed")
async def _status(p):
    await schedule_progress(p.get("shipment"), f"status-{p.get('new_status')}")


@on("shipment.delivered")
async def _delivered(p):
    await schedule_progress(p.get("shipment"), "delivered")


@on("customs.released")
async def _released(p):
    decl = p.get("declaration") or {}
    if decl.get("shipment_id"):
        s = await db.shipments.find_one({"id": decl["shipment_id"]}, NO_ID)
        await schedule_progress(s, "customs-released")


@on("approval.decided")
async def _decided(p):
    a = p.get("approval") or {}
    d = a.get("data") or {}
    if a.get("kind") != "trade_document" or not d.get("action", "").startswith(("issue_", "send_")) or "doc_no" not in d:
        return
    if a.get("status") != "approved":
        return
    actor = p.get("actor")
    if d["action"].startswith("send_"):
        await dispatch.send(d["trade_document_id"], milestone="approved", actor=actor, gated=False)
        return
    kind = d["action"][len("issue_"):]
    sid = (d.get("trade_document_id") or "").split(":")[0]
    cur = await service.get(d["trade_document_id"])
    if cur:
        sid = cur["shipment_id"]
    doc = await service.issue(sid, kind, final=True, actor=actor, source="owner_approved")
    await dispatch.send(doc["id"], milestone="approved", actor=actor, gated=False)


try:  # s2's sweep: shipments with work nobody picked up (missed events, restarts, switch changes)
    from services.agents.base import agents_epoch, sweeper

    @sweeper
    async def sweep_trade_documents():
        n = 0
        q = {"demo": {"$ne": True}, "status": {"$nin": ["cancelled"]}, "created_at": {"$gte": await agents_epoch()}}
        async for s in db.shipments.find(q, {"_id": 0, "id": 1, "status": 1, "company_id": 1, "demo": 1, "booking_confirmation": 1}).limit(200):
            await schedule_progress(s, "sweep")
            n += 1
        return n
except ImportError:  # pragma: no cover
    pass
