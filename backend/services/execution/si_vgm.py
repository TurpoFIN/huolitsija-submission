"""Shipping instructions and VGM after the carrier booking (s2 books; s9 executes).

Flow per FCL sea booking:
  carrier.booked -> start_for_booking(): execution record (booking ref, containers, cut-offs) -> jobs
  execution.si_submit: when every container has number + seal + cargo weight and the parties/HS are complete, build the
      DCSA eBL 3.0 CreateShippingInstructions, validate it against the official schema, pass the autonomy gate, and send
      it by the carrier's channel: DCSA API (POST /v3/shipping-instructions) or e-mail to the booking desk (JSON + text).
      Carrier notifications (RECEIVED / PENDING_UPDATE / COMPLETED ...) arrive at /api/execution/dcsa/{code}/v3/... or as
      e-mail replies. Waiting for data is a deferral, not a failure; the cut-off watch alerts while it waits.
  execution.vgm_submit: per container SOLAS VGM (SM1 weighbridge gross, or SM2 cargo + tare), DCSA VGM 1.0 POST
      /vgm-declarations, or e-mail with VERMAS D.16A + DCSA JSON.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import HTTPException

from core import config
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.notify import bi, notify_company
from core.util import NO_ID, new_id, now_iso
from services.execution import cutoffs, dcsa, edifact, mail
from services.execution.runtime import (AGENT_ACTOR, JobDeferred, decide, enqueue, job, raise_alert, reason, resolve_alerts,
                                        schedule)

log = logging.getLogger("execution.si_vgm")
register_index("execution_records", "id", unique=True)
register_index("execution_records", [("shipment_id", 1), ("carrier_booking_reference", 1)], unique=True)
register_index("shipping_instructions", "id", unique=True)
register_index("shipping_instructions", "carrier_reference", sparse=True)
register_index("vgm_declarations", [("record_id", 1), ("equipment_reference", 1)])

DCSA_SI_DONE = {"COMPLETED"}
DCSA_SI_OK = {"RECEIVED", "UPDATE_RECEIVED", "UPDATE_CONFIRMED", "UPDATE_CANCELLED"}
DCSA_SI_ACTION = {"PENDING_UPDATE", "UPDATE_DECLINED"}


def contact() -> dict:
    return {"name": "Huolitsija Toteutus", "email": mail.sender_address()}


def submitter() -> dict:
    return {"partyName": config.COMPANY_NAME[:70], "contactDetails": {"name": "Huolitsija Toteutus", "email": mail.sender_address()}}


def _is_person(actor: dict | None) -> bool:
    return bool(actor) and actor.get("type") != "agent" and actor.get("org_type") == "internal"


def main_sea_leg(s: dict) -> dict | None:
    sea = [lg for lg in s.get("legs") or [] if lg.get("mode") == "sea"]
    return next((lg for lg in sea if lg.get("role", "main") == "main"), sea[0] if sea else None)


async def _carrier(leg: dict | None, booking: dict) -> dict:
    c = None
    if leg and leg.get("carrier_id"):
        c = await db.carriers.find_one({"id": leg["carrier_id"]}, NO_ID)
    if not c and booking.get("carrier_code"):
        c = await db.carriers.find_one({"code": str(booking["carrier_code"]).upper()}, NO_ID)
    if not c and leg and leg.get("carrier"):
        c = await db.carriers.find_one({"name": leg["carrier"]}, NO_ID)
    return c or {"code": str(booking.get("carrier_code") or (leg or {}).get("carrier") or "UNKNOWN").upper(),
                 "name": (leg or {}).get("carrier") or booking.get("carrier_code") or "?"}


def _equipment(s: dict, booking: dict) -> list[str]:
    eq = []
    for e in booking.get("equipment") or (s.get("cargo") or {}).get("containers") or []:
        t = dcsa.iso_type(e.get("iso_type") or e.get("ISOEquipmentCode") or e.get("type")) or "45G1"
        eq += [t] * int(e.get("count") or e.get("units") or 1)
    return eq


def _containers(s: dict, leg: dict | None, booking: dict) -> list[dict]:
    eq = _equipment(s, booking)
    numbers = list(booking.get("container_numbers") or (leg or {}).get("container_numbers") or
                   (s.get("references") or {}).get("container_numbers") or [])
    n = max(len(eq), len(numbers), 1)
    cargo = s.get("cargo") or {}
    weight, packages = float(cargo.get("weight_kg") or 0), int(cargo.get("packages") or 0)
    out = []
    for i in range(n):
        out.append({"index": i, "equipment_reference": numbers[i] if i < len(numbers) else None,
                    "iso_type": eq[i] if i < len(eq) else (eq[0] if eq else "45G1"), "seal_number": None, "tare_kg": None,
                    "cargo_gross_kg": round(weight / n, 1) if weight else None,
                    "packages": max(1, packages // n + (1 if i < packages % n else 0)) if packages else None,
                    "weights_estimated": True, "vgm": None, "vgm_status": None, "shipper_owned": False})
    return out


async def get_record(shipment_id: str) -> dict | None:
    return await db.execution_records.find_one({"shipment_id": shipment_id, "state": {"$ne": "cancelled"}}, NO_ID,
                                               sort=[("created_at", -1)])


async def _require_record(shipment_id: str) -> dict:
    rec = await get_record(shipment_id)
    if not rec:
        raise HTTPException(404, "Execution has not started for this shipment (no carrier booking yet).")
    return rec


async def start_for_booking(shipment_id: str, booking: dict | None = None, actor: dict | None = None) -> dict:
    """Create the execution record for a carrier booking. Idempotent per (shipment, carrier booking reference)."""
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    booking = dict(booking or {})
    ref = (booking.get("carrier_booking_reference") or booking.get("carrierBookingReference")
           or (s.get("references") or {}).get("booking_no") or (s.get("references") or {}).get("carrier_booking_reference"))
    if not ref:
        raise HTTPException(409, "No carrier booking reference yet: execution starts after the carrier confirms the booking.")
    existing = await db.execution_records.find_one({"shipment_id": shipment_id, "carrier_booking_reference": ref}, NO_ID)
    if existing:
        return existing
    leg = main_sea_leg(s)
    carrier = await _carrier(leg, booking)
    fcl = bool(leg) and bool(_equipment(s, booking) or booking.get("container_numbers"))
    etd = booking.get("etd") or (leg or {}).get("etd") or s.get("etd")
    rec = {"id": new_id(), "shipment_id": shipment_id, "reference": s["reference"], "company_id": s.get("company_id"),
           "mode": s.get("mode"), "carrier_id": carrier.get("id"), "carrier_code": carrier.get("code"),
           "carrier_name": carrier.get("name"), "carrier_booking_reference": ref,
           "vessel": booking.get("vessel") or (leg or {}).get("vessel"), "voyage": booking.get("voyage") or (leg or {}).get("voyage"),
           "pol": (leg or {}).get("from", {}).get("code") if leg else None, "pod": (leg or {}).get("to", {}).get("code") if leg else None,
           "etd": etd, "si_required": fcl, "vgm_required": fcl,
           "containers": _containers(s, leg, booking) if fcl else [], "cutoffs": cutoffs.build(booking.get("cutoffs"), etd),
           "channel": dcsa.carrier_conf(carrier.get("code") or "").get("si_mode"), "vgm_signatory": booking.get("vgm_signatory"),
           "si_id": None, "si_submitted_at": None, "vgm_submitted_at": None, "gate_in_at": None,
           "state": "started" if fcl else "not_applicable",
           "note": None if fcl else "SI/VGM apply to FCL sea bookings; LCL goes on the consolidation master.",
           "booking": {k: v for k, v in booking.items() if k not in ("shipment",)}, "created_at": now_iso(), "updated_at": now_iso(),
           "history": [{"at": now_iso(), "event": "started", "by": (actor or AGENT_ACTOR).get("name")}]}
    try:
        await db.execution_records.insert_one(dict(rec))
    except Exception:  # noqa: BLE001  (a concurrent start won the unique index)
        return await db.execution_records.find_one({"shipment_id": shipment_id, "carrier_booking_reference": ref}, NO_ID)
    await audit(actor or AGENT_ACTOR, "execution.started", "shipment", shipment_id,
                {"booking": ref, "carrier": carrier.get("code"), "containers": len(rec["containers"])}, company_id=s.get("company_id"))
    await emit("execution.started", {"record": rec}, background=True)
    if fcl:
        await enqueue("execution.si_submit", {"shipment_id": shipment_id}, key=f"exec-si:{rec['id']}:1",
                      subject=("shipment", shipment_id), company_id=s.get("company_id"))
        await enqueue("execution.vgm_submit", {"shipment_id": shipment_id}, key=f"exec-vgm:{rec['id']}:1",
                      subject=("shipment", shipment_id), company_id=s.get("company_id"))
    return rec


# ------------------------------------------------------------------------------------------------- containers

async def update_container(shipment_id: str, index: int, changes: dict, actor: dict | None) -> dict:
    """Container facts from the haulier/terminal/ops: number, seal, tare, ISO type, cargo weight, weighbridge VGM."""
    rec = await _require_record(shipment_id)
    if index < 0 or index >= len(rec["containers"]):
        raise HTTPException(404, "No such container on this booking")
    c = rec["containers"][index]
    if "equipment_reference" in changes and changes["equipment_reference"]:
        no = re.sub(r"\s", "", str(changes["equipment_reference"])).upper()
        if not dcsa.iso6346_valid(no):
            raise HTTPException(400, f"{no} is not a valid container number (ISO 6346 check digit).")
        c["equipment_reference"] = no
    if changes.get("seal_number"):
        c["seal_number"] = str(changes["seal_number"]).strip()[:15]
    if changes.get("iso_type"):
        t = dcsa.iso_type(changes["iso_type"])
        if not t:
            raise HTTPException(400, "Unknown container type")
        c["iso_type"] = t
    for k in ("tare_kg", "cargo_gross_kg"):
        if changes.get(k) is not None:
            v = float(changes[k])
            if not 0 < v < 60000:
                raise HTTPException(400, f"{k} must be between 0 and 60,000 kg")
            c[k] = round(v, 1)
            if k == "cargo_gross_kg":
                c["weights_estimated"] = False
    if changes.get("packages") is not None:
        c["packages"] = int(changes["packages"])
    if changes.get("weighbridge_gross_kg") is not None:
        g = float(changes["weighbridge_gross_kg"])
        if not 1000 < g < 60000:
            raise HTTPException(400, "Weighbridge gross mass must be 1,000–60,000 kg")
        c["vgm"] = {"method": "SM1", "weight_kg": round(g, 1), "weighed_at": changes.get("weighed_at") or now_iso(),
                    "certificate": changes.get("certificate"), "source": "weighbridge"}
        if c.get("tare_kg") and not changes.get("cargo_gross_kg"):
            c["cargo_gross_kg"], c["weights_estimated"] = round(g - c["tare_kg"], 1), False
    if changes.get("vgm_signatory"):
        rec["vgm_signatory"] = str(changes["vgm_signatory"])[:255]
    _compute_vgm(c)
    await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"containers": rec["containers"], "vgm_signatory": rec.get("vgm_signatory"),
                                                                       "updated_at": now_iso()},
                                                              "$push": {"history": {"at": now_iso(), "event": "container_updated",
                                                                                    "index": index, "by": (actor or AGENT_ACTOR).get("name")}}})
    await audit(actor or AGENT_ACTOR, "execution.container_updated", "shipment", shipment_id, {"index": index, **{k: v for k, v in changes.items()}},
                company_id=rec.get("company_id"))
    if c.get("equipment_reference"):
        await db.shipments.update_one({"id": shipment_id}, {"$addToSet": {"references.container_numbers": c["equipment_reference"]}})
    return await get_record(shipment_id)


def _compute_vgm(c: dict):
    """SOLAS VI/2 method 2 when no weighbridge figure: cargo (with packing) + tare. Estimated splits are not declared."""
    if c.get("vgm") and c["vgm"].get("method") == "SM1":
        return
    if c.get("tare_kg") and c.get("cargo_gross_kg") and not c.get("weights_estimated"):
        c["vgm"] = {"method": "SM2", "weight_kg": round(c["tare_kg"] + c["cargo_gross_kg"], 1), "weighed_at": now_iso(), "source": "sm2"}


def si_missing(rec: dict, s: dict) -> list[dict]:
    miss = []
    for c in rec["containers"]:
        n = c["index"] + 1
        if not c.get("equipment_reference"):
            miss.append({"code": "container_number", "fi": f"Kontin {n} numero", "en": f"Container {n} number"})
        if not c.get("seal_number"):
            miss.append({"code": "seal", "fi": f"Kontin {n} sinetti", "en": f"Container {n} seal"})
        if not c.get("cargo_gross_kg"):
            miss.append({"code": "cargo_weight", "fi": f"Kontin {n} lastin paino", "en": f"Container {n} cargo weight"})
    p = s.get("parties") or {}
    if not (p.get("shipper") or {}).get("name"):
        miss.append({"code": "shipper", "fi": "Lähettäjä", "en": "Shipper"})
    if not (p.get("consignee") or {}).get("name"):
        miss.append({"code": "consignee", "fi": "Vastaanottaja", "en": "Consignee"})
    if not dcsa.hs6(s.get("cargo") or {}):
        miss.append({"code": "hs_code", "fi": "Tullinimike (6 numeroa)", "en": "HS code (6 digits)"})
    return miss


def vgm_missing(rec: dict, c: dict) -> list[dict]:
    n = c["index"] + 1
    miss = []
    if not c.get("equipment_reference"):
        miss.append({"code": "container_number", "fi": f"Kontin {n} numero", "en": f"Container {n} number"})
    if not c.get("vgm"):
        if not c.get("tare_kg"):
            miss.append({"code": "tare", "fi": f"Kontin {n} omapaino", "en": f"Container {n} tare"})
        if not c.get("cargo_gross_kg") or c.get("weights_estimated"):
            miss.append({"code": "cargo_weight", "fi": f"Kontin {n} punnittu lastin paino", "en": f"Container {n} weighed cargo"})
    if not rec.get("vgm_signatory"):
        miss.append({"code": "signatory", "fi": "VGM:n allekirjoittaja (lähettäjän edustaja)", "en": "VGM signatory (shipper's person)"})
    return miss


# ------------------------------------------------------------------------------------------------- SI

async def _desk_email(rec: dict) -> str | None:
    cf = dcsa.carrier_conf(rec.get("carrier_code") or "")
    if cf["desk_email"]:
        return cf["desk_email"]
    c = await db.carriers.find_one({"id": rec.get("carrier_id")}, {"_id": 0, "booking_desk_email": 1}) if rec.get("carrier_id") else None
    return (c or {}).get("booking_desk_email")


def _si_text(rec: dict, s: dict, si: dict) -> str:
    lines = [f"Shipping instructions / Lähetysohjeet — {rec['reference']}", f"Carrier booking: {rec['carrier_booking_reference']}",
             f"Vessel/voyage: {rec.get('vessel') or '-'} / {rec.get('voyage') or '-'}", f"POL → POD: {rec.get('pol') or '-'} → {rec.get('pod') or '-'}",
             f"Document: {si['transportDocumentTypeCode']}, freight {si['freightPaymentTermCode']}", "",
             f"Shipper: {si['documentParties']['shipper']['partyName']}",
             f"Consignee: {(si['documentParties'].get('consignee') or {}).get('partyName', '-')}", "",
             f"Goods: {' '.join(si['consignmentItems'][0]['descriptionOfGoods'])} · HS {', '.join(si['consignmentItems'][0]['HSCodes'])}", ""]
    for c in rec["containers"]:
        lines.append(f"  {c['equipment_reference']} {c['iso_type']} seal {c['seal_number']} · {c['packages'] or '-'} pkgs · "
                     f"{c['cargo_gross_kg']} kg cargo gross")
    lines += ["", f"The attached JSON is DCSA eBL {dcsa.spec_version('ebl')} CreateShippingInstructions, validated against the "
              "official schema. Please confirm receipt by replying to this e-mail; reply with corrections if anything is wrong.",
              "Liitteenä DCSA-muotoinen lähetysohje. Vahvistakaa vastaanotto vastaamalla tähän viestiin."]
    return "\n".join(lines)


async def submit_si(shipment_id: str, actor: dict | None = None, *, approved: bool = False) -> dict:
    rec = await _require_record(shipment_id)
    from services.execution.common import shipment
    s = await shipment(shipment_id)
    if not rec.get("si_required"):
        return {"status": "not_applicable", "note": rec.get("note")}
    missing = si_missing(rec, s)
    if missing:
        await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"si_missing": missing, "updated_at": now_iso()}})
        return {"status": "waiting", "missing": missing}
    prev = await db.shipping_instructions.find_one({"record_id": rec["id"], "status": {"$nin": ["superseded", "failed", "invalid"]}},
                                                   NO_ID, sort=[("version", -1)])
    doc_type = (s.get("references") or {}).get("bl_type") or "SWB"
    payload = dcsa.build_si(rec, s, contact=contact(), document_type="BOL" if doc_type == "BOL" else "SWB")
    if prev and prev.get("status") in ("submitted", "received", "completed"):
        original = {k: v for k, v in prev.get("payload", {}).items() if k != "shippingInstructionsReference"}
        if original == payload:
            return prev
    if prev and prev.get("carrier_reference") and prev.get("channel") == "dcsa":
        payload["shippingInstructionsReference"] = prev["carrier_reference"]
        component = "UpdateShippingInstructions"
    else:
        component = "CreateShippingInstructions"
    errors = dcsa.validate("ebl", component, payload)
    # Number after every earlier attempt, refused or failed ones included: the view, alert keys and the file name the
    # carrier receives all identify an SI by its version.
    last = await db.shipping_instructions.find_one({"record_id": rec["id"]}, {"_id": 0, "version": 1}, sort=[("version", -1)])
    version = (last or {}).get("version", 0) + 1
    si = {"id": new_id(), "record_id": rec["id"], "shipment_id": shipment_id, "reference": rec["reference"],
          "company_id": rec.get("company_id"), "carrier_code": rec.get("carrier_code"), "carrier_booking_reference": rec["carrier_booking_reference"],
          "version": version, "spec": f"DCSA eBL {dcsa.spec_version('ebl')} {component}", "payload": payload, "validation_errors": errors,
          "status": "invalid" if errors else "draft", "channel": None, "carrier_reference": (prev or {}).get("carrier_reference"),
          "carrier_status": None, "feedbacks": [], "history": [], "created_at": now_iso(), "updated_at": now_iso()}
    if errors:
        await db.shipping_instructions.insert_one(dict(si))
        await raise_alert("si_invalid", f"si-invalid:{rec['id']}:{version}", f"SI ei läpäise DCSA-tarkistusta: {rec['reference']}",
                          f"SI fails DCSA validation: {rec['reference']}", "; ".join(errors[:3]), "; ".join(errors[:3]),
                          severity="high", subject=("shipment", shipment_id), link=f"/ops/shipments/{shipment_id}")
        return {"status": "invalid", "errors": errors, "si_id": si["id"]}
    if not (approved or _is_person(actor)):
        extra = []
        if any(c.get("weights_estimated") for c in rec["containers"]):
            extra.append(reason("weights_estimated", "Konttikohtaiset painot on jaettu tasan", "Per-container weights are an even split"))
        d = await decide("si_submit", subject=("shipment", shipment_id), subject_label=f"{rec['reference']} · SI {rec['carrier_code']}",
                         inputs={"booking": rec["carrier_booking_reference"], "containers": len(rec["containers"])},
                         decision={"channel": rec.get("channel"), "component": component}, company_id=rec.get("company_id"),
                         extra_reasons=extra, title=f"Submit shipping instructions {rec['reference']}",
                         title_fi=f"Lähetä lähetysohjeet (SI) {rec['reference']}",
                         summary=f"{rec['carrier_name']} booking {rec['carrier_booking_reference']}, {len(rec['containers'])} containers",
                         summary_fi=f"{rec['carrier_name']} varaus {rec['carrier_booking_reference']}, {len(rec['containers'])} konttia",
                         link=f"/ops/shipments/{shipment_id}", approval_data={"shipment_id": shipment_id, "si_id": si["id"]})
        if not d.auto:
            si["status"] = "awaiting_approval"
            si["decision_id"] = d.id
            await db.shipping_instructions.insert_one(dict(si))
            return {"status": "awaiting_approval", "si_id": si["id"], "reasons": d.reasons}
    return await _send_si(rec, s, si, prev, actor)


async def _send_si(rec: dict, s: dict, si: dict, prev: dict | None, actor: dict | None) -> dict:
    cf = dcsa.carrier_conf(rec.get("carrier_code") or "")
    channel = "dcsa" if cf["si_mode"] == "dcsa" and cf["ebl_url"] else "email"
    si["channel"] = channel
    if channel == "dcsa":
        client = dcsa.ebl_client(rec["carrier_code"])
        if si["payload"].get("shippingInstructionsReference"):
            code, body = await client.put_si(si["payload"]["shippingInstructionsReference"], si["payload"])
        else:
            code, body = await client.post_si(si["payload"])
        si["http"] = {"status": code, "body": body}
        if code == 202:
            si["status"] = "submitted"
            si["carrier_reference"] = (body or {}).get("shippingInstructionsReference") or si.get("carrier_reference")
        else:
            si["status"] = "failed"
    else:
        desk = await _desk_email(rec)
        if not desk:
            si["status"] = "failed"
            si["error"] = "no booking desk e-mail for this carrier"
        else:
            name = f"SI-{rec['carrier_booking_reference']}-v{si['version']}"
            m = await mail.send(desk, f"Shipping instructions {rec['carrier_booking_reference']}", _si_text(rec, s, si["payload"]),
                                kind="si", ref_type="shipping_instructions", ref_id=si["id"], reference=rec["reference"],
                                company_id=rec.get("company_id"),
                                attachments=[(f"{name}.json", "application/json", json.dumps(si["payload"], indent=2).encode()),
                                             (f"{name}.txt", "text/plain", _si_text(rec, s, si["payload"]).encode())])
            si["message_id"], si["email_status"] = m["message_id"], m["status"]
            si["status"] = "submitted" if m["status"] == "sent" else "failed"
            si["error"] = m.get("error")
    si["submitted_at"] = now_iso() if si["status"] == "submitted" else None
    si["history"].append({"at": now_iso(), "status": si["status"], "by": (actor or AGENT_ACTOR).get("name")})
    await db.shipping_instructions.update_one({"id": si["id"]}, {"$set": si}, upsert=True)
    if si["status"] == "submitted":
        if prev:
            await db.shipping_instructions.update_one({"id": prev["id"]}, {"$set": {"status": "superseded"}})
        await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"si_id": si["id"], "si_submitted_at": si["submitted_at"],
                                                                           "state": "si_submitted", "si_missing": [], "updated_at": now_iso()}})
        await resolve_alerts(f"si-wait:{rec['id']}")
        await emit("si.submitted", {"si": si}, background=True)
        if si.get("carrier_reference"):  # a carrier may notify before our own write landed: replay what came early
            async for n in db.execution_messages.find({"kind": "dcsa_si_notification", "status": "unmatched",
                                                       "carrier_reference": si["carrier_reference"]}, NO_ID).sort("created_at", 1):
                await db.execution_messages.update_one({"id": n["id"]}, {"$set": {"status": "processed", "ref_id": si["id"]}})
                await apply_si_status(si, n["subject"], None, source="dcsa_notification_replayed")
                si = await db.shipping_instructions.find_one({"id": si["id"]}, NO_ID)
    else:
        await raise_alert("si_failed", f"si-failed:{si['id']}", f"SI:n lähetys epäonnistui: {rec['reference']}",
                          f"SI submission failed: {rec['reference']}", str(si.get("error") or si.get("http"))[:300],
                          str(si.get("error") or si.get("http"))[:300], severity="high", subject=("shipment", rec["shipment_id"]),
                          link=f"/ops/shipments/{rec['shipment_id']}")
    await audit(actor or AGENT_ACTOR, "execution.si_" + si["status"], "shipment", rec["shipment_id"],
                {"si_id": si["id"], "channel": channel, "version": si["version"], "carrier_reference": si.get("carrier_reference"),
                 "http_status": (si.get("http") or {}).get("status")}, company_id=rec.get("company_id"))
    return {k: v for k, v in si.items() if k != "_id"}


async def apply_si_status(si: dict, status: str, feedbacks: list | None = None, source: str = "dcsa") -> dict:
    status = (status or "").upper()
    new = ("completed" if status in DCSA_SI_DONE else "pending_update" if status in DCSA_SI_ACTION
           else "received" if status in DCSA_SI_OK else "rejected" if status in ("DECLINED", "REJECTED") else si.get("status"))
    sets = {"carrier_status": status, "status": new, "updated_at": now_iso()}
    if feedbacks is not None:
        sets["feedbacks"] = feedbacks
    await db.shipping_instructions.update_one({"id": si["id"]}, {"$set": sets, "$push": {"history": {"at": now_iso(), "status": new,
                                                                                                       "carrier_status": status, "source": source}}})
    si = {**si, **sets}
    rec = await db.execution_records.find_one({"id": si["record_id"]}, NO_ID)
    if new in ("received", "completed"):
        await db.execution_records.update_one({"id": si["record_id"]}, {"$set": {"state": "si_accepted" if new == "completed" else "si_received"}})
        await emit("si.accepted" if new == "completed" else "si.received", {"si": si}, background=True)
    elif new in ("pending_update", "rejected"):
        await db.execution_records.update_one({"id": si["record_id"]}, {"$set": {"si_submitted_at": None, "state": new}})
        text = "; ".join(str(f.get("message") or f) for f in (feedbacks or []))[:500] or status
        await raise_alert("si_action", f"si-action:{si['id']}:{status}", f"Kuljettaja pyytää korjausta SI:hin: {si['reference']}",
                          f"Carrier asks for an SI update: {si['reference']}", text, text, severity="high",
                          subject=("shipment", si["shipment_id"]), link=f"/ops/shipments/{si['shipment_id']}")
        await decide("si_update_requested", subject=("shipment", si["shipment_id"]), subject_label=f"{si['reference']} · SI",
                     inputs={"carrier_status": status, "feedbacks": feedbacks or []}, decision={"next": "correct data and resubmit"},
                     company_id=(rec or {}).get("company_id"), extra_reasons=[reason("carrier_feedback", f"Kuljettajan palaute: {text}",
                                                                                     f"Carrier feedback: {text}")],
                     title=f"Carrier asks for SI correction {si['reference']}", title_fi=f"Korjaa SI {si['reference']}",
                     summary=text, summary_fi=text, link=f"/ops/shipments/{si['shipment_id']}",
                     approval_data={"shipment_id": si["shipment_id"], "si_id": si["id"], "kind": "si_correction"})
        await emit("si.rejected", {"si": si}, background=True)
    return si


async def refresh_si(si_id: str) -> dict:
    si = await db.shipping_instructions.find_one({"id": si_id}, NO_ID)
    if not si:
        raise HTTPException(404, "SI not found")
    if si.get("channel") != "dcsa" or not si.get("carrier_reference"):
        return si
    code, body = await dcsa.ebl_client(si["carrier_code"]).get_si(si["carrier_reference"])
    await db.shipping_instructions.update_one({"id": si_id}, {"$set": {"last_get": {"status": code, "at": now_iso(),
                                                                                    "status_field": (body or {}).get("shippingInstructionsStatus")
                                                                                    if isinstance(body, dict) else None}}})
    if code == 200 and isinstance(body, dict) and body.get("shippingInstructionsStatus"):
        return await apply_si_status(si, body["shippingInstructionsStatus"], body.get("feedbacks"), source="dcsa_get")
    return await db.shipping_instructions.find_one({"id": si_id}, NO_ID)


async def dcsa_si_notification(code: str, body: dict) -> dict | None:
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    ref = data.get("shippingInstructionsReference") or (data.get("shippingInstructions") or {}).get("shippingInstructionsReference")
    status = data.get("shippingInstructionsStatus") or data.get("updatedShippingInstructionsStatus")
    si = await db.shipping_instructions.find_one({"carrier_reference": ref, "carrier_code": code}, NO_ID, sort=[("version", -1)]) if ref else None
    await db.execution_messages.insert_one({"id": new_id(), "direction": "in", "kind": "dcsa_si_notification", "ref_type": "shipping_instructions",
                                            "ref_id": (si or {}).get("id"), "from": f"dcsa:{code}", "subject": status,
                                            "carrier_code": code, "carrier_reference": ref,
                                            "text": json.dumps(body)[:20000], "status": "processed" if si else "unmatched",
                                            "created_at": now_iso()})
    if si and status:
        return await apply_si_status(si, status, data.get("feedbacks"), source="dcsa_notification")
    return None


ACCEPT_WORDS = re.compile(r"\b(received|confirmed|accepted|well noted|noted|ok|vastaanotettu|vahvistettu|kiitos, saatu|draft b/?l)\b", re.I)
PROBLEM_WORDS = re.compile(r"\b(missing|incorrect|wrong|amend|correct(ion)?|please provide|cannot|unable|reject|puuttuu|virhe|korjaa|"
                           r"väärä|hylätty)\b", re.I)


@mail.reply_handler("si")
async def _si_reply(rec_in: dict, out: dict, parsed: dict) -> dict:
    si = await db.shipping_instructions.find_one({"id": out["ref_id"]}, NO_ID)
    if not si:
        return {"matched": False}
    text = (parsed.get("text") or "").split("\n>")[0][:4000]
    problem, ok = bool(PROBLEM_WORDS.search(text)), bool(ACCEPT_WORDS.search(text))
    verdict, model = None, None
    if problem:
        verdict = "UPDATE_DECLINED" if re.search(r"reject|hylätty", text, re.I) else "PENDING_UPDATE"
    elif ok:
        verdict = "RECEIVED"
    else:
        from core import llm

        if llm.llm_available():
            r = await llm.llm_json("execution_si_reply", "You classify a carrier booking desk's reply to shipping instructions. "
                                   "Return JSON {status: RECEIVED|PENDING_UPDATE|UNKNOWN, feedback: string}.", text)
            verdict, model = (r or {}).get("status"), llm.provider_name()
    if verdict in ("RECEIVED", "PENDING_UPDATE", "UPDATE_DECLINED"):
        await apply_si_status(si, verdict, [{"message": text[:500]}] if verdict != "RECEIVED" else None, source="email")
        return {"status": verdict, "model": model}
    await decide("si_reply_unclear", subject=("shipment", si["shipment_id"]), subject_label=f"{si['reference']} · SI reply",
                 inputs={"text": text[:1000]}, decision={}, extra_reasons=[reason("unclear_reply", "Vastaus epäselvä", "Reply unclear")],
                 title=f"Read the carrier's SI reply {si['reference']}", title_fi=f"Lue kuljettajan SI-vastaus {si['reference']}",
                 summary=text[:300], summary_fi=text[:300], link=f"/ops/shipments/{si['shipment_id']}",
                 approval_data={"shipment_id": si["shipment_id"], "si_id": si["id"], "kind": "si_reply"})
    return {"status": "owner_queue"}


# ------------------------------------------------------------------------------------------------- VGM

async def submit_vgm(shipment_id: str, container_no: str | None = None, actor: dict | None = None, *, approved: bool = False) -> dict:
    rec = await _require_record(shipment_id)
    from services.execution.common import shipment
    s = await shipment(shipment_id)
    if not rec.get("vgm_required"):
        return {"status": "not_applicable"}
    todo = [c for c in rec["containers"] if c.get("vgm_status") not in ("submitted", "accepted")
            and (not container_no or c.get("equipment_reference") == container_no)]
    if not todo:
        return {"status": "nothing_to_send"}
    ready, waiting = [], []
    for c in todo:
        _compute_vgm(c)
        (waiting if vgm_missing(rec, c) else ready).append(c)
    if not ready:
        return {"status": "waiting", "missing": [m for c in waiting for m in vgm_missing(rec, c)]}
    at = now_iso()[:19] + "Z"
    decls = [dcsa.build_vgm(rec, c, s, submitter=submitter(), signatory=rec["vgm_signatory"], at=at) for c in ready]
    errors = dcsa.validate("vgm", "PostVGMDeclarationsRequest", {"VGMDeclarations": decls})
    if errors:
        await raise_alert("vgm_invalid", f"vgm-invalid:{rec['id']}:{at}", f"VGM ei läpäise DCSA-tarkistusta: {rec['reference']}",
                          f"VGM fails DCSA validation: {rec['reference']}", "; ".join(errors[:3]), "; ".join(errors[:3]), severity="high",
                          subject=("shipment", shipment_id))
        return {"status": "invalid", "errors": errors}
    if not (approved or _is_person(actor)):
        d = await decide("vgm_submit", subject=("shipment", shipment_id), subject_label=f"{rec['reference']} · VGM",
                         inputs={"containers": [c["equipment_reference"] for c in ready]},
                         decision={"methods": [c["vgm"]["method"] for c in ready]}, company_id=rec.get("company_id"),
                         title=f"Submit VGM {rec['reference']}", title_fi=f"Lähetä VGM {rec['reference']}",
                         summary=", ".join(f"{c['equipment_reference']} {c['vgm']['weight_kg']} kg {c['vgm']['method']}" for c in ready),
                         summary_fi=", ".join(f"{c['equipment_reference']} {c['vgm']['weight_kg']} kg {c['vgm']['method']}" for c in ready),
                         link=f"/ops/shipments/{shipment_id}", approval_data={"shipment_id": shipment_id})
        if not d.auto:
            return {"status": "awaiting_approval", "reasons": d.reasons}
    cf = dcsa.carrier_conf(rec.get("carrier_code") or "")
    channel = "dcsa" if cf["si_mode"] == "dcsa" and cf["vgm_url"] else "email"
    result = {"channel": channel}
    if channel == "dcsa":
        code, body = await dcsa.vgm_client(rec["carrier_code"]).post_vgm(decls)
        errs = [f for f in ((body or {}).get("feedbackElements") or []) if str(f.get("severity", "")).upper() == "ERROR"] if isinstance(body, dict) else []
        status = "accepted" if code == 200 and not errs else "rejected"
        result.update(http={"status": code, "body": body})
    else:
        desk = await _desk_email(rec)
        if not desk:
            status, result["error"] = "failed", "no booking desk e-mail for this carrier"
        else:
            edi = edifact.vermas(interchange_ref=f"V{new_id()[:12]}", sender_id="HUOLITSIJA", recipient_id=rec.get("carrier_code") or "CARRIER",
                                 booking_ref=rec["carrier_booking_reference"], carrier_code=rec.get("carrier_code") or "", containers=ready,
                                 responsible_party=((s.get("parties") or {}).get("shipper") or {}).get("name") or s.get("customer_name", ""),
                                 signatory=rec["vgm_signatory"], contact_email=mail.sender_address(), at=at)
            text = "\n".join([f"VGM (SOLAS VI/2) — booking {rec['carrier_booking_reference']} / {rec['reference']}", ""] +
                             [f"  {c['equipment_reference']} {c['iso_type']}: {c['vgm']['weight_kg']} kg, method {c['vgm']['method']}"
                              for c in ready] + ["", f"Authorized signatory: {rec['vgm_signatory']}",
                                                 "Attached: VERMAS D.16A and DCSA VGM 1.0 JSON. Please confirm receipt by reply."])
            m = await mail.send(desk, f"VGM {rec['carrier_booking_reference']}", text, kind="vgm", ref_type="execution_record", ref_id=rec["id"],
                                reference=rec["reference"], company_id=rec.get("company_id"),
                                attachments=[(f"VERMAS-{rec['carrier_booking_reference']}.edi", "application/edifact", edi.encode()),
                                             (f"VGM-{rec['carrier_booking_reference']}.json", "application/json",
                                              json.dumps({"VGMDeclarations": decls}, indent=2).encode())])
            status = "submitted" if m["status"] == "sent" else "failed"
            result.update(message_id=m["message_id"], email_status=m["status"])
    for c, decl in zip(ready, decls):
        c["vgm_status"] = status
        await db.vgm_declarations.insert_one({"id": new_id(), "record_id": rec["id"], "shipment_id": shipment_id, "reference": rec["reference"],
                                              "carrier_code": rec.get("carrier_code"), "carrier_booking_reference": rec["carrier_booking_reference"],
                                              "equipment_reference": c["equipment_reference"], "method": c["vgm"]["method"],
                                              "weight_kg": c["vgm"]["weight_kg"], "declaration": decl, "channel": channel, "status": status,
                                              "spec": f"DCSA VGM {dcsa.spec_version('vgm')}", "result": result, "created_at": now_iso()})
    sets = {"containers": rec["containers"], "updated_at": now_iso()}
    if all(c.get("vgm_status") in ("submitted", "accepted") for c in rec["containers"]):
        sets["vgm_submitted_at"] = now_iso()
    await db.execution_records.update_one({"id": rec["id"]}, {"$set": sets})
    await audit(actor or AGENT_ACTOR, f"execution.vgm_{status}", "shipment", shipment_id,
                {"containers": [c["equipment_reference"] for c in ready], "channel": channel}, company_id=rec.get("company_id"))
    if status in ("accepted", "submitted"):
        await emit("vgm.accepted" if status == "accepted" else "vgm.submitted", {"record": rec, "containers": ready}, background=True)
    else:
        await raise_alert("vgm_failed", f"vgm-failed:{rec['id']}:{at}", f"VGM:n lähetys epäonnistui: {rec['reference']}",
                          f"VGM submission failed: {rec['reference']}", str(result)[:300], str(result)[:300], severity="high",
                          subject=("shipment", shipment_id))
    return {"status": status, "containers": [c["equipment_reference"] for c in ready], "waiting": len(waiting), **result}


@mail.reply_handler("vgm")
async def _vgm_reply(rec_in: dict, out: dict, parsed: dict) -> dict:
    text = (parsed.get("text") or "").split("\n>")[0][:4000]
    rec = await db.execution_records.find_one({"id": out["ref_id"]}, NO_ID)
    if not rec:
        return {"matched": False}
    if PROBLEM_WORDS.search(text):
        for c in rec["containers"]:
            if c.get("vgm_status") == "submitted":
                c["vgm_status"] = "rejected"
        await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"containers": rec["containers"], "vgm_submitted_at": None}})
        await db.vgm_declarations.update_many({"record_id": rec["id"], "status": "submitted"}, {"$set": {"status": "rejected"}})
        await raise_alert("vgm_rejected", f"vgm-rejected:{rec['id']}:{rec_in['id']}", f"Kuljettaja hylkäsi VGM:n: {rec['reference']}",
                          f"Carrier rejected the VGM: {rec['reference']}", text[:300], text[:300], severity="high",
                          subject=("shipment", rec["shipment_id"]))
        return {"status": "rejected"}
    if ACCEPT_WORDS.search(text):
        for c in rec["containers"]:
            if c.get("vgm_status") == "submitted":
                c["vgm_status"] = "accepted"
        await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"containers": rec["containers"]}})
        await db.vgm_declarations.update_many({"record_id": rec["id"], "status": "submitted"}, {"$set": {"status": "accepted"}})
        return {"status": "accepted"}
    return {"status": "unclear"}


async def gate_in(shipment_id: str, at: str | None, actor: dict | None) -> dict:
    rec = await _require_record(shipment_id)
    await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"gate_in_at": at or now_iso(), "updated_at": now_iso()}})
    await audit(actor or AGENT_ACTOR, "execution.gate_in", "shipment", shipment_id, {"at": at}, company_id=rec.get("company_id"))
    return await get_record(shipment_id)


async def cancel_for_booking(shipment_id: str, reason_text: str, actor: dict | None = None) -> int:
    n = (await db.execution_records.update_many({"shipment_id": shipment_id, "state": {"$ne": "cancelled"}},
                                                {"$set": {"state": "cancelled", "cancel_reason": reason_text, "updated_at": now_iso()}})).modified_count
    await audit(actor or AGENT_ACTOR, "execution.cancelled", "shipment", shipment_id, {"reason": reason_text})
    return n


# ------------------------------------------------------------------------------------------------- jobs + events

async def _ask_for_missing(rec: dict, missing: list[dict]):
    first = await raise_alert("si_waiting", f"si-wait:{rec['id']}", f"SI odottaa tietoja: {rec['reference']}", f"SI waiting for data: {rec['reference']}",
                              ", ".join(m["fi"] for m in missing), ", ".join(m["en"] for m in missing), severity="medium",
                              subject=("shipment", rec["shipment_id"]), link=f"/ops/shipments/{rec['shipment_id']}")
    if first and rec.get("company_id"):
        await notify_company(rec["company_id"], bi(f"Lähetysohjeisiin tarvitaan tietoja ({rec['reference']})",
                                                   f"Shipping instructions need details ({rec['reference']})"),
                             bi("Puuttuu: " + ", ".join(m["fi"] for m in missing), "Missing: " + ", ".join(m["en"] for m in missing)),
                             link=f"/portal/shipments/{rec['shipment_id']}", email=True, kind="execution")


@job("execution.start", max_attempts=5, timeout=120, agent=True, label_fi="Toteutuksen aloitus", label_en="Start execution")
async def _start_job(j: dict) -> dict:
    rec = await start_for_booking(j["payload"]["shipment_id"], j["payload"].get("booking"))
    return {"record_id": rec["id"], "state": rec["state"]}


@job("execution.si_submit", max_attempts=5, timeout=120, agent=True, label_fi="Lähetysohjeet (SI)", label_en="Shipping instructions")
async def _si_job(j: dict) -> dict:
    sid = j["payload"]["shipment_id"]
    r = await submit_si(sid)
    if r["status"] == "waiting":
        rec = await get_record(sid)
        await _ask_for_missing(rec, r["missing"])
        raise JobDeferred(900, "waiting for SI data: " + ", ".join(m["en"] for m in r["missing"]))
    return {k: r.get(k) for k in ("status", "si_id", "channel", "carrier_reference")}


@job("execution.vgm_submit", max_attempts=5, timeout=120, agent=True, label_fi="VGM", label_en="VGM")
async def _vgm_job(j: dict) -> dict:
    r = await submit_vgm(j["payload"]["shipment_id"])
    if r["status"] == "waiting" or r.get("waiting"):
        raise JobDeferred(900, "waiting for VGM data")
    return {k: r.get(k) for k in ("status", "containers", "channel")}


@job("execution.si_poll", max_attempts=3, timeout=300, agent=True, label_fi="SI-tilan haku", label_en="SI status poll")
async def _si_poll(j: dict) -> dict:
    n = 0
    async for si in db.shipping_instructions.find({"channel": "dcsa", "status": {"$in": ["submitted", "received"]}}, {"_id": 0, "id": 1}):
        await refresh_si(si["id"])
        n += 1
    return {"polled": n}


schedule("execution.si_poll", job="execution.si_poll", every_seconds=1800, label_fi="DCSA SI -tilat", label_en="DCSA SI statuses")()


@on("carrier.booked")
async def _on_carrier_booked(p: dict):
    s = p.get("shipment") or {}
    booking = dict(p.get("booking") or p.get("carrier_booking") or {})
    sid = s.get("id") or p.get("shipment_id") or booking.get("shipment_id")
    if not sid:
        return
    ref = booking.get("carrier_booking_reference") or booking.get("carrierBookingReference") or booking.get("reference") or "ref"
    booking.setdefault("carrier_booking_reference", booking.get("carrierBookingReference") or booking.get("reference"))
    await enqueue("execution.start", {"shipment_id": sid, "booking": booking}, key=f"exec:{sid}:{ref}", subject=("shipment", sid),
                  company_id=s.get("company_id"))


@on("carrier.booking_cancelled")
async def _on_booking_cancelled(p: dict):
    sid = (p.get("shipment") or {}).get("id") or p.get("shipment_id")
    if sid:
        await cancel_for_booking(sid, "carrier booking cancelled")


@on("approval.decided")
async def _on_decided(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != "execution_review" or a.get("status") != "approved":
        return
    data = a.get("data") or {}
    actor = p.get("actor")
    if data.get("action") == "si_submit" and data.get("shipment_id"):
        await db.shipping_instructions.update_one({"id": data.get("si_id"), "status": "awaiting_approval"}, {"$set": {"status": "superseded"}})
        await submit_si(data["shipment_id"], actor, approved=True)
    elif data.get("action") == "vgm_submit" and data.get("shipment_id"):
        await submit_vgm(data["shipment_id"], None, actor, approved=True)
