"""EDI gateway: partners, inbound processing, outbound queue, transports, status reporting.

Inbound (any transport) → `receive_edifact(raw, partner=…, transport=…)`:
  every interchange is stored in `edi_messages` (direction "in", raw kept), checked (UNB sender = partner, recipient = us,
  UNT/UNZ counts), and each IFTMIN becomes a quote + booking through the portal's own code
  (routes.quotes.create_quote + routes.quotes.create_booking), acting as the partner's company. The answer is an IFTMCS:
  29 accepted (with our booking ref, carrier, ETD/ETA, price), 23 on hold (the owner approval the portal would also
  need), 27 not accepted (with the reasons), 1 cancellation done. Held bookings finish by themselves when the approval
  is decided (`approval.decided`). DCSA Booking 2.0 JSON goes through the same path and answers in DCSA JSON.
Outbound → `edi_messages` (direction "out", status queued → sent | retry | failed) delivered by the partner's transport
  (sftp | as2 | none = kept for pickup). Status: every milestone of the partner company's shipments → IFTSTA (and
  DCSA T&T 2.2 JSON when the partner wants "dcsa"), deduplicated per tracking event.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException
from starlette.requests import Request

from core import config
from core.audit import audit
from core.db import db, register_index
from core.events import on
from core.lifecycle import background_loop
from core.refs import next_seq
from core.util import NO_ID, iso, new_id, now, now_iso
from services.edi import codes, dcsa, transport
from services.edi.edifact import InterchangeError, parse, write_interchange
from services.edi.iftmin import parse_iftmin
from services.edi.outbound import iftmcs, iftsta

log = logging.getLogger("edi")

register_index("edi_partners", "id", unique=True)
register_index("edi_partners", "unb_id")
register_index("edi_partners", "company_id")
register_index("edi_messages", "id", unique=True)
register_index("edi_messages", [("partner_id", 1), ("created_at", -1)])
register_index("edi_messages", "dedupe_key", unique=True, sparse=True)
register_index("edi_messages", [("direction", 1), ("status", 1), ("next_attempt_at", 1)])
register_index("edi_messages", "related.shipment_id")

RETRY_MINUTES = [1, 5, 30, 120, 720]
PARTNER_PUBLIC = {"_id": 0, "hmac_secret": 0}


def our_party() -> dict:
    return {"name": config.COMPANY_NAME, "unb_id": config.EDI_OUR_UNB_ID,
            "address": {"street": "", "city": config.COMPANY_ADDRESS.split(",")[0].strip(), "country": "FI"}}


def edi_actor(partner: dict) -> dict:
    """The partner acts as its company's booking user (the portal's own permission and KYC checks apply)."""
    return {"id": f"edi:{partner['id']}", "name": f"EDI {partner.get('name') or partner['id']}", "email": None,
            "org_type": "customer", "role": "customer_user", "company_id": partner["company_id"],
            "permissions": ["company.book"], "locale": partner.get("locale") or "en"}


# ------------------------------------------------------------------------------------------------ partners

def _clean_partner_input(data: dict) -> dict:
    allowed = {"name", "company_id", "unb_id", "unb_qual", "transport", "sftp", "as2", "formats", "status_messages",
               "status_scope", "status_code_map", "active", "test", "locale", "terms_accepted"}
    out = {k: v for k, v in data.items() if k in allowed}
    if "transport" in out and out["transport"] not in ("sftp", "as2", "none"):
        raise HTTPException(400, "transport must be sftp, as2 or none.")
    if "sftp" in out:
        s = dict(out["sftp"] or {})
        for secret_field in ("password", "private_key"):
            if s.get(secret_field):
                raise HTTPException(400, "Do not send SFTP secrets; name the server env var in password_env or key_env.")
        out["sftp"] = {k: s.get(k) for k in ("host", "port", "user", "password_env", "key_env", "host_key", "inbound_dir",
                                             "outbound_dir", "archive_dir") if s.get(k) not in (None, "")}
    if "formats" in out:
        out["formats"] = [f for f in out["formats"] or [] if f in ("edifact", "dcsa")] or ["edifact"]
    if "status_scope" in out and out["status_scope"] not in ("all", "edi"):
        raise HTTPException(400, "status_scope must be all or edi.")
    return out


async def create_partner(data: dict, actor: dict) -> tuple[dict, str]:
    """Returns (partner, hmac_secret). The secret signs HTTPS EDI/DCSA calls; it is shown once."""
    clean = _clean_partner_input(data)
    if not clean.get("company_id") or not await db.companies.find_one({"id": clean["company_id"]}, {"_id": 1}):
        raise HTTPException(400, "Choose the customer company this EDI partner books for.")
    if not clean.get("unb_id"):
        raise HTTPException(400, "Give the partner's UNB interchange id (their sender id in UNB).")
    if await db.edi_partners.find_one({"unb_id": clean["unb_id"], "active": {"$ne": False}}, {"_id": 1}):
        raise HTTPException(409, f"An active EDI partner already uses UNB id {clean['unb_id']}.")
    secret = "edi_" + secrets.token_urlsafe(32)
    p = {"id": new_id(), "name": clean.get("name") or clean["unb_id"], "unb_qual": "ZZZ", "transport": "none",
         "formats": ["edifact"], "status_messages": True, "status_scope": "all", "active": True, "test": False,
         "locale": "en", "terms_accepted": None, **clean, "hmac_secret": secret, "created_at": now_iso(),
         "created_by": {"id": actor.get("id"), "name": actor.get("name")}}
    await db.edi_partners.insert_one(dict(p))
    await audit(actor, "edi.partner_created", "edi_partner", p["id"], {"unb_id": p["unb_id"], "transport": p["transport"]},
                company_id=p["company_id"])
    p.pop("hmac_secret", None)
    p.pop("_id", None)
    return p, secret


async def update_partner(partner_id: str, data: dict, actor: dict) -> dict:
    clean = _clean_partner_input(data)
    clean.pop("company_id", None)
    if not clean:
        raise HTTPException(400, "Nothing to change.")
    clean["updated_at"] = now_iso()
    r = await db.edi_partners.update_one({"id": partner_id}, {"$set": clean})
    if not r.matched_count:
        raise HTTPException(404, "EDI partner not found.")
    await audit(actor, "edi.partner_updated", "edi_partner", partner_id, {k: v for k, v in clean.items() if k != "as2"})
    return await db.edi_partners.find_one({"id": partner_id}, PARTNER_PUBLIC)


def verify_hmac(partner: dict, body: bytes, signature: str | None) -> bool:
    if not signature or not partner.get("hmac_secret"):
        return False
    want = hmac.new(partner["hmac_secret"].encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip().lower())


# ------------------------------------------------------------------------------------------------ inbound

async def _store_in(partner: dict | None, raw: bytes, *, transport_name: str, filename: str | None, standard: str,
                    mtype: str, extra: dict | None = None) -> tuple[dict, bool]:
    """Store an inbound payload; (doc, duplicate). Duplicates (same partner + same bytes) are not processed again."""
    digest = hashlib.sha256(raw).hexdigest()
    key = f"in:{(partner or {}).get('id') or 'unknown'}:{digest}"
    existing = await db.edi_messages.find_one({"dedupe_key": key}, NO_ID)
    if existing:
        return existing, True
    doc = {"id": new_id(), "direction": "in", "standard": standard, "type": mtype, "partner_id": (partner or {}).get("id"),
           "company_id": (partner or {}).get("company_id"), "transport": transport_name, "filename": filename,
           "raw": raw.decode("utf-8", errors="replace")[:500000], "sha256": digest, "dedupe_key": key, "status": "received",
           "results": [], "error": None, "related": {}, "created_at": now_iso(), **(extra or {})}
    await db.edi_messages.insert_one(dict(doc))
    return doc, False


async def _mark(doc_id: str, **fields):
    fields["updated_at"] = now_iso()
    await db.edi_messages.update_one({"id": doc_id}, {"$set": fields})


async def receive_edifact(raw: bytes, *, partner: dict | None, transport_name: str, filename: str | None = None) -> dict:
    """Process one EDIFACT interchange. Returns the stored inbound record (with results)."""
    doc, dup = await _store_in(partner, raw, transport_name=transport_name, filename=filename, standard="edifact", mtype="EDIFACT")
    if dup:
        return {**doc, "duplicate": True}
    try:
        ic = parse(raw)
    except (InterchangeError, UnicodeDecodeError) as exc:
        await _mark(doc["id"], status="rejected", error=f"Syntax: {exc}")
        return {**doc, "status": "rejected", "error": f"Syntax: {exc}"}
    if partner is None:
        partner = await db.edi_partners.find_one({"unb_id": ic.sender, "active": {"$ne": False}}, NO_ID)
        if partner:
            await _mark(doc["id"], partner_id=partner["id"], company_id=partner["company_id"])
    types = sorted({m.type for m in ic.messages})
    await _mark(doc["id"], type="+".join(types) or "EDIFACT", interchange_ref=ic.control_ref, sender=ic.sender,
                recipient=ic.recipient, test=ic.test)
    if not partner:
        err = f"No active EDI partner has UNB id {ic.sender}."
        await _mark(doc["id"], status="rejected", error=err)
        return {**doc, "status": "rejected", "error": err}
    if ic.sender != partner["unb_id"]:
        err = f"UNB sender {ic.sender} is not this partner's id {partner['unb_id']}."
        await _mark(doc["id"], status="rejected", error=err)
        return {**doc, "status": "rejected", "error": err}
    if ic.recipient != config.EDI_OUR_UNB_ID:
        err = f"UNB recipient {ic.recipient} is not us ({config.EDI_OUR_UNB_ID})."
        await _mark(doc["id"], status="rejected", error=err)
        return {**doc, "status": "rejected", "error": err}
    results = []
    for m in ic.messages:
        if m.type != "IFTMIN":
            results.append({"message_ref": m.ref, "type": m.type, "status": "ignored",
                            "reason": f"{m.type} is not a message we accept (we take IFTMIN)."})
            continue
        if (m.version, m.release) != ("D", "04A"):
            log.info("IFTMIN %s:%s from %s parsed with the D.04A segment rules", m.version, m.release, partner["unb_id"])
        instr = parse_iftmin(m)
        res = await handle_instruction(instr, partner, inbound_id=doc["id"], standard="edifact")
        results.append({"message_ref": m.ref, "type": m.type, "doc_number": instr.get("doc_number"), **res})
    status = "processed" if all(r["status"] in ("booked", "on_hold", "cancelled", "ignored", "duplicate") for r in results) else "processed_with_errors"
    related = next(({"quote_id": r.get("quote_id"), "shipment_id": r.get("shipment_id"), "shipment_reference": r.get("shipment_reference")}
                    for r in results if r.get("quote_id")), {})
    await _mark(doc["id"], status=status, results=results, related=related, processed_at=now_iso())
    return {**doc, "status": status, "results": results, "related": related}


async def receive_dcsa_booking(body: dict, partner: dict) -> dict:
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    doc, dup = await _store_in(partner, raw, transport_name="https", filename=None, standard="dcsa", mtype="DCSA_BOOKING")
    instr = dcsa.booking_to_instruction(body)
    if dup:
        res = (doc.get("results") or [{}])[0]
        return dcsa.booking_response(instr, res)
    res = await handle_instruction(instr, partner, inbound_id=doc["id"], standard="dcsa")
    await _mark(doc["id"], status="processed" if res["status"] != "rejected" else "processed_with_errors", results=[res],
                related={k: res.get(k) for k in ("quote_id", "shipment_id", "shipment_reference")}, processed_at=now_iso())
    return dcsa.booking_response(instr, res)


def _fake_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/edi", "headers": [], "query_string": b"",
                    "server": ("edi", 443), "scheme": "https", "client": ("edi", 0)})


def _quote_body(instr: dict) -> dict:
    def place(p):
        if not p:
            return None
        return p.get("unlocode") or p.get("name")

    body = {"mode": instr.get("mode"), "origin": place(instr.get("origin")), "destination": place(instr.get("destination")),
            "ready_date": instr.get("ready_date"), "cargo": {k: v for k, v in (instr.get("cargo") or {}).items() if v not in (None, [], "")},
            "notes": instr.get("instructions")}
    if instr.get("incoterm"):
        body["incoterm"] = instr["incoterm"]
        body["incoterm_place"] = instr.get("incoterm_place") or (instr.get("origin") or {}).get("name") \
            or (instr.get("origin") or {}).get("unlocode")
    return {k: v for k, v in body.items() if v not in (None, "")}


def _pick_option(q: dict, instr: dict) -> dict | None:
    opts = q.get("options") or []
    want = (instr.get("carrier_name") or "").strip().lower()
    if want:
        hit = next((o for o in opts if want in (o.get("carrier_name") or "").lower()), None)
        if hit:
            return hit
    if instr.get("mode"):
        same_mode = [o for o in opts if o.get("mode") == instr["mode"]]
        if same_mode:
            return next((o for o in same_mode if o.get("recommended")), same_mode[0])
    return next((o for o in opts if o.get("recommended")), opts[0] if opts else None)


def _booking_body(q: dict, opt: dict, instr: dict):
    from routes.quotes import BookingIn

    parties = instr.get("parties") or {}
    ship, cons = parties.get("shipper") or {}, parties.get("consignee") or {}

    def party(p):
        if not p:
            return None
        return {"name": p.get("name"), "address": {k: v for k, v in (p.get("address") or {}).items() if v},
                "contact": p.get("contact"), "eori": p.get("eori"), "vat_id": p.get("vat_id")}

    refs = instr.get("references") or {}
    body = {"quote_id": q["id"], "option_id": opt["id"], "shipper": party(ship), "consignee": party(cons),
            "notify": party(parties.get("notify")) if (parties.get("notify") or {}).get("name") else None,
            "pickup": {"date": instr.get("ready_date")} if instr.get("ready_date") else {},
            "delivery": {"date": instr.get("delivery_date")} if instr.get("delivery_date") else {},
            "customer_reference": refs.get("customer_ref") or instr.get("doc_number"),
            "po_numbers": refs.get("po_numbers") or [], "special_instructions": instr.get("instructions"),
            "incoterm_place": instr.get("incoterm_place"), "accept_terms": True}
    if (instr.get("cargo") or {}).get("dg"):
        body["cargo"] = {"dangerous_goods": True, "dg": instr["cargo"]["dg"]}
    return BookingIn(**{k: v for k, v in body.items() if v is not None})


async def _book(q: dict, instr: dict, partner: dict) -> dict:
    """Book an approved/sent quote through the portal booking handler after the agent gate."""
    from services.agents.quote_agent import outbound_allowed
    if not await outbound_allowed(q, operation="book_quote"):
        return {"status": "on_hold", "function": codes.FN_ON_HOLD, "quote_id": q["id"],
                "quote_number": q.get("number"), "reasons": ["Autopilot is off or quote policy requires a person's approval."]}
    from routes.quotes import _create_booking

    opt = _pick_option(q, instr)
    if not opt:
        return {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "quote_id": q["id"], "quote_number": q.get("number"),
                "reasons": ["No transport option could be priced for this lane; our team will contact you."]}
    try:
        out = await _create_booking(_booking_body(q, opt, instr), _fake_request(), edi_actor(partner))
    except HTTPException as exc:
        return {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "quote_id": q["id"], "quote_number": q.get("number"),
                "option_id": opt["id"], "reasons": [str(exc.detail)]}
    shipment = await db.shipments.find_one({"id": out["shipment_id"]}, NO_ID)
    return {"status": "booked", "function": codes.FN_ACCEPTED, "quote_id": q["id"], "quote_number": q.get("number"),
            "option_id": opt["id"], "shipment_id": out["shipment_id"], "shipment_reference": out["shipment_reference"],
            "_shipment": shipment, "_option": opt}


async def handle_instruction(instr: dict, partner: dict, *, inbound_id: str, standard: str) -> dict:
    """IFTMIN / DCSA booking → quote + booking, then the answer message. Returns a result dict without private keys."""
    from routes.quotes import create_quote

    actor = edi_actor(partner)
    company = await db.companies.find_one({"id": partner["company_id"]}, NO_ID)
    doc_no = instr.get("doc_number")
    # Same document number again: answer with the first result (idempotent for partner retries)
    prior = await db.edi_instructions.find_one({"partner_id": partner["id"], "doc_number": doc_no}, NO_ID) if doc_no else None
    res: dict
    if instr.get("function") == codes.FN_CANCEL:
        res = await _cancel(prior, partner, actor)
    elif prior and prior.get("result", {}).get("status") in ("booked", "on_hold"):
        res = {**prior["result"], "status": prior["result"]["status"], "duplicate": True}
        if res.get("shipment_id"):
            res["_shipment"] = await db.shipments.find_one({"id": res["shipment_id"]}, NO_ID)
    elif instr.get("errors"):
        res = {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "reasons": list(instr["errors"])}
    elif not company:
        res = {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "reasons": ["The partner's company no longer exists."]}
    elif not partner.get("terms_accepted"):
        res = {"status": "rejected", "function": codes.FN_NOT_ACCEPTED,
               "reasons": ["The EDI agreement has no terms acceptance on file; bookings by EDI need it (ask ops@huolitsija.fi)."]}
    else:
        try:
            q = await create_quote(_quote_body(instr), user=actor, company=company, source="edi", send=True,
                                   ai={"edi": {"partner_id": partner["id"], "doc_number": doc_no, "standard": standard}})
        except HTTPException as exc:
            q, res = None, {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "reasons": [str(exc.detail)]}
        if q is not None:
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"edi": {"partner_id": partner["id"], "doc_number": doc_no,
                                                                          "inbound_id": inbound_id, "standard": standard}}})
            if q["status"] == "sent":
                res = await _book(q, instr, partner)
            elif q["status"] == "pending_approval":
                reasons = q.get("approval", {}).get("reasons") or []
                from routes.quotes import APPROVAL_REASON_TEXT

                text = "; ".join(APPROVAL_REASON_TEXT.get(r, r) for r in reasons) or "review"
                res = {"status": "on_hold", "function": codes.FN_ON_HOLD, "quote_id": q["id"], "quote_number": q.get("number"),
                       "reasons": [f"Quote {q.get('number')} needs our approval ({text}); we book it and confirm as soon as it is approved."]}
            else:
                res = {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "quote_id": q["id"], "quote_number": q.get("number"),
                       "reasons": [f"Quote {q.get('number')} is {q['status']}; it cannot be booked automatically."]}
    shipment, option = res.pop("_shipment", None), res.pop("_option", None)
    if option is None and res.get("quote_id") and res.get("option_id"):
        qd = await db.quotes.find_one({"id": res["quote_id"]}, {"_id": 0, "options": 1})
        option = next((o for o in (qd or {}).get("options") or [] if o["id"] == res["option_id"]), None)
    record = {"partner_id": partner["id"], "company_id": partner["company_id"], "doc_number": doc_no, "standard": standard,
              "instruction": instr, "result": res, "inbound_id": inbound_id, "updated_at": now_iso()}
    if not res.get("duplicate"):
        await db.edi_instructions.update_one({"partner_id": partner["id"], "doc_number": doc_no},
                                             {"$set": record, "$setOnInsert": {"id": new_id(), "created_at": now_iso()}}, upsert=True)
    await audit(actor, "edi.instruction", "edi_message", inbound_id,
                {"doc_number": doc_no, "status": res["status"], "quote_id": res.get("quote_id"),
                 "shipment_reference": res.get("shipment_reference"), "standard": standard}, company_id=partner["company_id"])
    if standard == "edifact":
        out = await send_iftmcs(partner, instr, res, shipment=shipment, option=option, inbound_id=inbound_id)
        res["answer_message_id"] = out["id"]
    return res


async def _cancel(prior: dict | None, partner: dict, actor: dict) -> dict:
    from services.shipments import set_status

    if not prior or not prior.get("result", {}).get("shipment_id"):
        return {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "reasons": ["No booking found for this document number."]}
    sid = prior["result"]["shipment_id"]
    try:
        await set_status(sid, "cancelled", actor=actor, note="Cancelled by the shipper's EDI instruction (BGM function 1).",
                         source="edi")
    except HTTPException as exc:
        return {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "shipment_id": sid,
                "shipment_reference": prior["result"].get("shipment_reference"), "reasons": [str(exc.detail)]}
    return {"status": "cancelled", "function": codes.FN_CANCEL, "shipment_id": sid,
            "shipment_reference": prior["result"].get("shipment_reference"), "quote_id": prior["result"].get("quote_id")}


@on("approval.decided")
async def _finish_held_edi_booking(payload: dict):
    a = payload.get("approval") or {}
    if a.get("kind") != "quote_threshold" or a.get("ref_type") != "quote":
        return
    q = await db.quotes.find_one({"id": a.get("ref_id"), "edi": {"$exists": True}}, NO_ID)
    if not q:
        return
    rec = await db.edi_instructions.find_one({"partner_id": q["edi"]["partner_id"], "doc_number": q["edi"]["doc_number"]}, NO_ID)
    partner = await db.edi_partners.find_one({"id": q["edi"]["partner_id"]}, NO_ID)
    if not rec or not partner or rec["result"].get("status") != "on_hold":
        return
    if q["status"] == "sent":
        res = await _book(q, rec["instruction"], partner)
    else:
        res = {"status": "rejected", "function": codes.FN_NOT_ACCEPTED, "quote_id": q["id"], "quote_number": q.get("number"),
               "reasons": [f"Quote {q.get('number')} was not approved{': ' + a['decision_note'] if a.get('decision_note') else ''}."]}
    shipment, option = res.pop("_shipment", None), res.pop("_option", None)
    await db.edi_instructions.update_one({"id": rec["id"]}, {"$set": {"result": res, "updated_at": now_iso()}})
    await audit(edi_actor(partner), "edi.instruction_completed", "edi_message", rec.get("inbound_id"),
                {"doc_number": rec["doc_number"], "status": res["status"], "shipment_reference": res.get("shipment_reference")},
                company_id=partner["company_id"])
    if rec.get("standard") == "edifact":
        await send_iftmcs(partner, rec["instruction"], res, shipment=shipment, option=option, inbound_id=rec.get("inbound_id"))


# ------------------------------------------------------------------------------------------------ outbound

async def _control_ref() -> str:
    return f"{await next_seq('edi_interchange'):08d}"


async def send_iftmcs(partner: dict, instr: dict, res: dict, *, shipment: dict | None, option: dict | None,
                      inbound_id: str | None) -> dict:
    icref = await _control_ref()
    segs = iftmcs(instruction=instr, function=res["function"], our_ref=res.get("shipment_reference") or res.get("quote_number"),
                  shipment=shipment, option=option if res["status"] == "booked" else None, reasons=res.get("reasons"),
                  doc_date_iso=now_iso(), our_party=our_party())
    raw = write_interchange(config.EDI_OUR_UNB_ID, partner["unb_id"], [("IFTMCS", "D", "04A", segs)], control_ref=icref,
                            recipient_qual=partner.get("unb_qual") or "ZZZ", test=bool(partner.get("test")))
    return await queue_outbound(partner, "IFTMCS", raw.encode(), f"IFTMCS_{icref}.edi", standard="edifact",
                                related={"in_reply_to": inbound_id, "quote_id": res.get("quote_id"), "shipment_id": res.get("shipment_id"),
                                         "shipment_reference": res.get("shipment_reference"), "function": res["function"]},
                                interchange_ref=icref)


async def queue_outbound(partner: dict, mtype: str, data: bytes, filename: str, *, standard: str, related: dict,
                         interchange_ref: str | None = None, dedupe_key: str | None = None) -> dict:
    doc = {"id": new_id(), "direction": "out", "standard": standard, "type": mtype, "partner_id": partner["id"],
           "company_id": partner["company_id"], "transport": partner.get("transport") or "none", "filename": filename,
           "raw": data.decode("utf-8", errors="replace"), "sha256": hashlib.sha256(data).hexdigest(), "status": "queued",
           "attempts": 0, "next_attempt_at": now_iso(), "related": related, "interchange_ref": interchange_ref,
           "error": None, "created_at": now_iso()}
    if dedupe_key:
        doc["dedupe_key"] = dedupe_key
    await db.edi_messages.insert_one(dict(doc))
    return await deliver(doc, partner)


async def deliver(doc: dict, partner: dict | None = None) -> dict:
    partner = partner or await db.edi_partners.find_one({"id": doc["partner_id"]}, NO_ID)
    if not partner:
        await _mark(doc["id"], status="failed", error="Partner not found.")
        return {**doc, "status": "failed"}
    tname = partner.get("transport") or "none"
    data = doc["raw"].encode()
    attempts = int(doc.get("attempts") or 0) + 1
    try:
        if tname == "sftp":
            path = await transport.sftp_put(partner.get("sftp") or {}, doc["filename"], data)
            upd = {"status": "sent", "sent_at": now_iso(), "delivery": {"transport": "sftp", "path": path}}
        elif tname == "as2":
            if not config.EDI_AS2_ENABLED:
                raise transport.TransportError("AS2 is switched off (EDI_AS2_ENABLED).")
            ctype = "application/json" if doc["standard"] == "dcsa" else "application/EDIFACT"
            info = await transport.as2_send(partner, doc["filename"], data, content_type=ctype)
            upd = {"status": "sent", "sent_at": now_iso(), "delivery": {"transport": "as2", **info}}
        else:
            upd = {"status": "stored", "delivery": {"transport": "none", "note": "Kept for pickup over HTTPS (GET /api/edi/partner/outbox)."}}
        upd.update(attempts=attempts, error=None, next_attempt_at=None)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
        delay = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
        dead = attempts >= len(RETRY_MINUTES) + 1
        upd = {"status": "failed" if dead else "retry", "attempts": attempts, "error": f"{type(exc).__name__}: {exc}"[:500],
               "next_attempt_at": None if dead else iso(now() + timedelta(minutes=delay))}
        log.warning("EDI %s %s to %s: attempt %s failed: %s", doc["type"], doc["filename"], partner.get("unb_id"), attempts, exc)
    await _mark(doc["id"], **upd)
    return {**doc, **upd}


async def retry_due():
    async for doc in db.edi_messages.find({"direction": "out", "status": {"$in": ["queued", "retry"]},
                                           "next_attempt_at": {"$lte": now_iso()}}, NO_ID).limit(50):
        await deliver(doc)


# ------------------------------------------------------------------------------------------------ status (IFTSTA / DCSA)

async def send_status_updates(shipment_id: str | None = None) -> list[dict]:
    """IFTSTA (and DCSA events) for every reportable tracking event not yet sent to the partner."""
    sent = []
    partners = await db.edi_partners.find({"active": {"$ne": False}, "status_messages": {"$ne": False}}, NO_ID).to_list(200)
    by_company: dict[str, list[dict]] = {}
    for p in partners:
        by_company.setdefault(p["company_id"], []).append(p)
    if not by_company:
        return sent
    q: dict = {"company_id": {"$in": list(by_company)}, "code": {"$in": sorted(codes.REPORTED_MILESTONES)}}
    if shipment_id:
        q["shipment_id"] = shipment_id
    else:
        q["at"] = {"$gte": iso(now() - timedelta(days=14))}
    events = await db.tracking_events.find(q, NO_ID).sort("at", 1).to_list(2000)
    per_shipment: dict[str, list[dict]] = {}
    for ev in events:
        per_shipment.setdefault(ev["shipment_id"], []).append(ev)
    for sid, evs in per_shipment.items():
        shipment = await db.shipments.find_one({"id": sid}, NO_ID)
        if not shipment:
            continue
        for p in by_company.get(shipment["company_id"], []):
            if p.get("status_scope") == "edi" and not await db.edi_instructions.find_one(
                    {"partner_id": p["id"], "result.shipment_id": sid}, {"_id": 1}):
                continue
            since = p.get("created_at") or ""
            todo = []
            for ev in evs:
                if (ev.get("at") or "") < since and (ev.get("created_at") or ev.get("at") or "") < since:
                    continue
                if await db.edi_messages.find_one({"dedupe_key": f"sts:{p['id']}:{ev['id']}"}, {"_id": 1}):
                    continue
                todo.append(ev)
            if not todo:
                continue
            for ev in todo:  # one IFTSTA per event: partners map one file to one status
                out = await _send_iftsta(p, shipment, ev)
                sent.append(out)
                if "dcsa" in (p.get("formats") or []):
                    events_json = dcsa.tnt_events(shipment, [ev])
                    if events_json:
                        await queue_outbound(p, "DCSA_TNT", json.dumps(events_json, ensure_ascii=False, indent=1).encode(),
                                             f"DCSA_TNT_{shipment['reference']}_{ev['id'][:8]}.json", standard="dcsa",
                                             related={"shipment_id": sid, "shipment_reference": shipment["reference"],
                                                      "tracking_event_id": ev["id"]}, dedupe_key=f"dcsa:{p['id']}:{ev['id']}")
    return sent


async def _send_iftsta(p: dict, shipment: dict, ev: dict) -> dict:
    icref = await _control_ref()
    segs = iftsta(shipment=shipment, events=[ev], doc_number=f"{shipment['reference']}-{icref}", doc_date_iso=now_iso(),
                  our_party=our_party(), code_map=p.get("status_code_map"))
    raw = write_interchange(config.EDI_OUR_UNB_ID, p["unb_id"], [("IFTSTA", "D", "04A", segs)], control_ref=icref,
                            recipient_qual=p.get("unb_qual") or "ZZZ", test=bool(p.get("test")))
    try:
        return await queue_outbound(p, "IFTSTA", raw.encode(), f"IFTSTA_{icref}.edi", standard="edifact",
                                    related={"shipment_id": shipment["id"], "shipment_reference": shipment["reference"],
                                             "tracking_event_id": ev["id"], "milestone": ev["code"]},
                                    interchange_ref=icref, dedupe_key=f"sts:{p['id']}:{ev['id']}")
    except Exception as exc:  # noqa: BLE001 - a duplicate key means another worker sent it first
        if "duplicate key" in str(exc).lower():
            return {"status": "duplicate", "tracking_event_id": ev["id"]}
        raise


_pending: set[asyncio.Task] = set()


@on("shipment.status_changed")
async def _on_status(payload: dict):
    s = payload.get("shipment") or {}
    if not s.get("id") or not await db.edi_partners.find_one({"company_id": s.get("company_id"), "active": {"$ne": False}}, {"_id": 1}):
        return
    t = asyncio.create_task(send_status_updates(s["id"]))  # off the request path; the sweep below catches failures
    _pending.add(t)
    t.add_done_callback(_pending.discard)


# ------------------------------------------------------------------------------------------------ SFTP poll + loop

async def poll_partner(p: dict) -> list[dict]:
    files = await transport.sftp_fetch_inbound(p.get("sftp") or {})
    results, done = [], []
    for name, data in files:
        res = await receive_edifact(data, partner=p, transport_name="sftp", filename=name)
        results.append({"filename": name, "id": res["id"], "status": res.get("status"), "duplicate": res.get("duplicate", False)})
        done.append(name)
    await transport.sftp_archive(p.get("sftp") or {}, done)
    if files:
        await db.edi_partners.update_one({"id": p["id"]}, {"$set": {"last_poll_at": now_iso(), "last_poll_files": len(files)}})
    return results


async def poll_all() -> list[dict]:
    out = []
    async for p in db.edi_partners.find({"active": {"$ne": False}, "transport": "sftp"}, NO_ID):
        try:
            out.extend(await poll_partner(p))
        except Exception as exc:  # noqa: BLE001
            log.warning("EDI SFTP poll for %s failed: %s", p.get("unb_id"), exc)
            await db.edi_partners.update_one({"id": p["id"]}, {"$set": {"last_poll_error": str(exc)[:300], "last_poll_at": now_iso()}})
    return out


@background_loop(seconds=config.EDI_POLL_SECONDS)
async def edi_tick():
    if not await db.edi_partners.find_one({"active": {"$ne": False}}, {"_id": 1}):
        return
    if config.EDI_SFTP_ENABLED:
        await poll_all()
    await retry_due()
    await send_status_updates()
