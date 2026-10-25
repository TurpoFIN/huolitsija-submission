"""Project actual Huolitsija domain outcomes into the agreed demo event stream."""
from core.db import db
from core.events import on
from core.util import NO_ID, now_iso
from services.demo_autoquote.events import emit


async def run_for(*, shipment=None, invoice=None, document=None, company=None):
    entity = shipment or invoice or document or {}
    sid = entity.get("shipment_id") or (shipment or {}).get("id")
    if not shipment and sid:
        shipment = await db.raw.shipments.find_one({"id": sid}, NO_ID)
    qid = (shipment or {}).get("quote_id") or entity.get("quote_id")
    query = {"quote_id": qid} if qid else {"company_id": (company or {}).get("id") or entity.get("company_id")}
    if not next(iter(query.values())):
        return None
    return await db.raw.demo_runs.find_one({**query, "phase": "lifecycle", "status": {"$nin": ["stopped", "failed", "done"]}}, NO_ID)


async def finish_if_responsibility_ended(run):
    if not run:
        return
    s = await db.raw.shipments.find_one({"quote_id": run["quote_id"], "status": "delivered"}, NO_ID)
    if not s:
        return
    pod = await db.raw.documents.find_one({"shipment_id": s["id"], "type": "pod", "file_id": {"$nin": [None, ""]}}, NO_ID)
    invoices = await db.raw.invoices.find({"shipment_id": s["id"], "kind": "invoice", "status": {"$ne": "void"}}, NO_ID).to_list(50)
    if not pod or not invoices or any(i.get("status") != "paid" or not i.get("pdf_file_id") for i in invoices):
        return
    from core.storage import get_file
    try:
        for fid in [pod["file_id"], *(i["pdf_file_id"] for i in invoices)]:
            content, _, _ = await get_file(fid)
            if not content:
                return
    except Exception:
        return
    pending_claims = await db.raw.claims.find({'shipment_id':s['id'],
        'status':{'$nin':['closed','paid','rejected']}},NO_ID).to_list(100)
    review_cards = []
    for claim in pending_claims:
        approval = await db.raw.approvals.find_one({'id':claim.get('approval_id'),'kind':'claim',
            'ref_id':claim['id'],'status':'pending'},{'id':1}) if claim.get('status') == 'in_review' else None
        if not approval:
            return
        review_cards.append({'claim':claim['number'],'status':'in_review','approval_id':approval['id']})
    pending_exceptions = await db.raw.exceptions.find({'shipment_id':s['id'],'status':{'$ne':'resolved'}},NO_ID).to_list(100)
    if pending_exceptions:
        from .engine import physical_blocker
        if not review_cards or await physical_blocker(s['id']):
            return
        reviewed_ids = {claim['id'] for claim in pending_claims}
        linked_ids = set()
        for exception in pending_exceptions:
            action = await db.raw.world_actions.find_one({'key':'claim:' + exception['id'],
                'run_id':run['id'],'status':'delivered'},NO_ID)
            claim_id = ((action or {}).get('claim') or {}).get('id')
            if exception.get('type') != 'damage' or claim_id not in reviewed_ids or claim_id in linked_ids:
                return
            linked_ids.add(claim_id)
    from . import broker
    if s.get("customs_clearance") and broker.customer_arranged(s):
        if not await broker.released(s):
            return
    elif s.get("customs_clearance"):
        coll, states = (db.raw.export_declarations, ["exited"]) if s.get("direction") == "export" else (db.raw.customs_declarations, ["released", "closed"])
        if not await coll.find_one({"shipment_id": s["id"], "status": {"$in": states}}, {"_id": 1}):
            return
    result = await db.raw.demo_runs.update_one({'id':run['id'],'phase':'lifecycle',
        'status':{'$nin':['stopped','failed','done']}},{'$set':{'phase':'done','status':'done','completed_at':now_iso(),
            'completion_reason':'claim_review_handoff' if review_cards else 'settled',
            'end_reason':'claim_review_handoff' if review_cards else 'settled','claim_review_cards':review_cards}})
    if not result.modified_count:
        return
    await emit(run["id"], "payment", "Delivered and paid · damage claim with a person for review" if review_cards else "Shipment settled",
        state="done", kind="phase", data={"facts": {
        "shipment": s["reference"], "POD": pod["filename"], "paid invoices": ", ".join(i["number"] for i in invoices),
        **({'claims awaiting review':', '.join(c['claim'] for c in review_cards)} if review_cards else {})},
        'end_reason':'claim_review_handoff' if review_cards else 'settled',
        **({'claim_review_cards':review_cards} if review_cards else {})})
    await db.raw.world_actors.update_many({"run_id": run["id"]}, {"$set": {"active": False}})


@on("shipment.created")
async def shipment_created(p):
    from core.jobs import enqueue
    s = p["shipment"]
    run = await run_for(shipment=s)
    if not run:
        return
    await db.raw.demo_runs.update_one({"id": run["id"]}, {"$set": {"shipment_id": s["id"], "shipment_reference": s["reference"]}})
    if run.get("real_requester"):
        from . import conf
        from services.demo_autoquote import clock
        await clock.set_clock(run["id"], speed=conf.speed())
    await emit(run["id"], "booking", "Shipment booked", state="done", data={"facts": {"shipment": s["reference"]}})
    await enqueue('world.customer_instructions', {'run_id': run['id'], 'shipment_id': s['id']}, key='world:customs-instructions:' + s['id'], source='world')


@on("carrier.booked")
async def carrier_booked(p):
    from . import engine
    context = await _carrier_context(p, confirmation=True)
    if context is None:
        return
    s, b, message = context
    run = await run_for(shipment=s)
    if not run:
        return
    carrier = await db.raw.carriers.find_one({"id": b.get("carrier_id")}, NO_ID) or {}
    if carrier.get("contact_email"):
        await engine._register_actor(run, "w:carrier", carrier["contact_email"], carrier.get("name"))
    seq = b.get("leg_seq") or 1
    provenance = await _carrier_actor(message)
    await emit(run["id"], f"leg:{seq}", "Carrier booking confirmed", state="active", data={**provenance,"facts": {
        "carrier": carrier.get("name") or b.get("carrier_name"), "mode": b.get("mode"), "name": carrier.get("name"),
        "booking": b.get("carrier_booking_reference") or b.get("carrier_ref")}})
    await engine.schedule_movement(run, s, key="booked:" + str(b.get("id") or b.get("booking_id") or s["id"] + ":" + str(seq)))


@on("carrier.reply_received")
async def carrier_reply(p):
    context = await _carrier_context(p)
    if context is None:
        return
    shipment, _, message = context
    run = await run_for(shipment=shipment)
    if run:
        result = (p.get("message") or {}).get("status")
        reference = (p.get("parsed") or {}).get("carrier_ref")
        title = "Booking confirmed · ref " + str(reference) if result == "confirmed" and reference else {
            "awaiting_confirmation": "Counteroffer accepted; confirmation requested", "rejected": "Carrier declined booking",
            "answered": "Carrier cargo question answered", "owner_queue": "Carrier reply requires review"}.get(result, "Carrier booking reply processed")
        provenance = await _carrier_actor(message)
        await emit(run["id"], "booking", title[:59], kind="decision", state="active", data={**provenance,"facts": {
            "result": (p.get("message") or {}).get("status")}, "reasoning": (p.get("parsed") or {}).get("reason")})


async def _carrier_context(payload, *, confirmation=False):
    """Bind external evidence to the persisted shipment and its native purchase.

    A provider ID in an unrelated or mismatched event cannot establish who acted
    on this shipment. Old confirmations without message evidence remain unknown.
    """
    shipment_hint = payload.get('shipment') or {}
    booking_hint = payload.get('booking') or {}
    message_hint = payload.get('message') or {}
    sid = shipment_hint.get('id')
    bid = booking_hint.get('id') or message_hint.get('booking_id')
    if not sid or not bid:
        return None
    shipment = await db.raw.shipments.find_one({'id':sid}, NO_ID)
    booking = await db.raw.carrier_bookings.find_one({'id':bid,'shipment_id':sid}, NO_ID)
    if not shipment or not booking:
        return None
    if shipment_hint.get('quote_id') and shipment_hint['quote_id'] != shipment.get('quote_id'):
        return None
    if booking_hint.get('shipment_id') and booking_hint['shipment_id'] != sid:
        return None
    if message_hint.get('booking_id') and message_hint['booking_id'] != bid:
        return None
    if confirmation:
        message_id = booking.get('confirmation_message_id')
        if booking_hint.get('confirmation_message_id') and booking_hint['confirmation_message_id'] != message_id:
            return None
    else:
        message_id = message_hint.get('id')
    message = await db.raw.carrier_booking_messages.find_one({'id':message_id,'booking_id':bid},NO_ID) if message_id else None
    if not confirmation and message_id and not message:
        return None
    return shipment, booking, message


async def _carrier_actor(message):
    """Classify the confirming native message, never the run or booking request.

    Missing confirmation evidence stays unknown and carries no actor chip.
    World outbox evidence must be an exact stored message/provider identifier.
    """
    message = message or {}
    provider_id = str(message.get('provider_id') or '').strip()
    if provider_id.startswith('demo-sim:'):
        source = 'world'
    else:
        identifiers = [provider_id] if provider_id else [message['id']] if message.get('id') else []
        world = await db.raw.email_outbox.find_one({'provider':'world','status':'sent',
            '$or':[{'id':{'$in':identifiers}},{'provider_id':{'$in':identifiers}}]}, {'_id':1}) if identifiers else None
        source = 'world' if world else 'real' if provider_id else None
    return {'actor':{'id':'w:carrier' if source == 'world' else 'carrier','source':source}} if source else {}


@on("shipment.status_changed")
async def shipment_status(p):
    s = p["shipment"]
    run = await run_for(shipment=s)
    if run:
        delivered = s.get("status") == "delivered"
        await emit(run["id"], "delivery" if delivered else "leg:1", "Delivery recorded" if delivered else "Transport updated",
                   state="done" if delivered else "active", data={"facts": {"status": s["status"], "shipment": s["reference"]}})
        await finish_if_responsibility_ended(run)


async def _document(p):
    d = p.get("document") or {}
    run = await run_for(document=d)
    if not run:
        return
    items = []
    files = d.get("files") or ({"file": d["file_id"]} if d.get("file_id") else {})
    for key, fid in files.items():
        if key not in ("pdf_en", "file"):
            continue
        filename = d.get("filename") or f"{d['doc_no']}-v{d['version']}-en.pdf"
        items.append({"id": d["id"], "title": d.get("doc_no") or filename, "kind": d.get("kind") or d.get("type"),
                      "file_id": fid, "filename": filename, "content_type": d.get("content_type") or "application/pdf"})
    node = "delivery" if d.get("type") == "pod" else "docs"
    await emit(run["id"], node, "Document saved", kind="document", state="done" if node == "delivery" else "active", data={"documents": items})
    await finish_if_responsibility_ended(run)


on("document.uploaded")(_document)
on("tradedoc.issued")(_document)
on("tradedoc.amended")(_document)


@on("document.extracted")
async def extracted(p):
    d = p.get("document") or {}
    run = await run_for(document=d)
    if run:
        ex = d.get("extraction") or {}
        await emit(run["id"], "docs", "Document read", kind="tool", data={"facts": {"file": d.get("filename"), "result": ex.get("status")},
            "tool_calls": [{"tool": "Huolitsija document extraction", "input": d.get("filename"), "output": ex.get("status")} ]})


@on("invoice.issued")
async def invoice_issued(p):
    from core.jobs import enqueue
    inv = p["invoice"]
    run = await run_for(invoice=inv)
    if not run:
        return
    documents = [{"id": inv["id"], "title": inv["number"], "kind": "invoice", "file_id": inv["pdf_file_id"],
                  "filename": inv["number"] + ".pdf", "content_type": "application/pdf"}] if inv.get("pdf_file_id") else []
    await emit(run["id"], "invoicing", "Invoice issued", state="done", kind="document", data={"documents": documents,
        "facts": {"invoice": inv["number"], "total EUR": f"{inv['total_cents']/100:.2f}", "reference": inv.get("rf_reference") or inv.get("reference")}})
    if not run.get("real_requester"):
        await enqueue("world.pay", {"run_id": run["id"], "invoice_id": inv["id"]}, key="world:pay:" + inv["id"], source="world")


@on("invoice.paid")
async def invoice_paid(p):
    inv = p["invoice"]
    run = await run_for(invoice=inv)
    if run:
        await emit(run["id"], "payment", "Payment matched", state="done", kind="decision", data={"facts": {
            "invoice": inv["number"], "paid EUR": f"{int(inv.get('paid_cents') or inv['total_cents'])/100:.2f}"}})
        await finish_if_responsibility_ended(run)


@on("company.kyc_changed")
async def company_kyc(p):
    c = p["company"]
    run = await run_for(company=c)
    if run:
        await emit(run["id"], "booking", "Customer checks updated", kind="decision", data={"facts": {
            "KYC": c.get("kyc_status"), "sanctions": c.get("sanctions_status")}})


async def _customs(p):
    d = p["declaration"]
    run = await run_for(document=d)
    if run:
        await emit(run["id"], "customs", "Customs " + d["status"].replace("_", " "), state="done" if d["status"] in ("released", "exited") else "active",
                   data={"facts": {"LRN": d.get("lrn"), "MRN": d.get("mrn"), "status": d["status"]}})
        await finish_if_responsibility_ended(run)


on("customs.approved")(_customs)
on("customs.released")(_customs)
on("customs.export.released")(_customs)
on("customs.export.exited")(_customs)


@on("customs.query_answered")
async def customs_query_answered(p):
    run = await run_for(document=p["declaration"])
    if run:
        await emit(run["id"], "customs", "Customs query answered", kind="decision", state="waiting",
                   data={"facts": {"documents sent": len(p["document_ids"])}})


@on("exception.opened")
async def exception_opened(p):
    e = p["exception"]
    run = await run_for(document=e)
    if run:
        await emit(run["id"], "exceptions", "Exception opened", state="active", data={"facts": {"type": e.get("type"), "title": e.get("title")}})
        if e.get('type') == 'damage':
            from core.jobs import enqueue
            await enqueue('world.file_claim',{'run_id':run['id'],'exception_id':e['id']},
                key='world:claim:' + e['id'],source='world')


@on("exception.resolved")
async def exception_resolved(p):
    e = p["exception"]
    run = await run_for(document=e)
    if run:
        await emit(run["id"], "exceptions", "Exception resolved", state="done", kind="decision", data={"facts": {
            "type": e.get("type"), "resolution": e.get("resolution")}})
        await finish_if_responsibility_ended(run)


@on('approval.requested')
async def claim_review_requested(p):
    approval = p['approval']
    if approval.get('kind') != 'claim' or approval.get('ref_type') != 'claim':
        return
    claim = await db.raw.claims.find_one({'id':approval['ref_id']},NO_ID)
    run = await run_for(document=claim) if claim else None
    if run:
        await emit(run['id'],'exceptions','Claim awaiting settlement review',kind='decision',state='waiting',
            data={'facts':{'claim':claim['number'],'status':approval['status'],
                'approval_id':approval['id'],'legal point':approval.get('legal_point')}})


@on("customs.message_stored")
async def customs_file(p):
    from core.storage import put_file
    d, m = p["declaration"], p["message"]
    run = await run_for(document=d)
    if not run:
        return
    filename = f"{d['lrn']}-{m.get('type') or 'customs'}-{m['direction']}.xml"
    fid = await put_file(m["xml"].encode(), filename, "application/xml", {"company_id": d.get("company_id"), "shipment_id": d.get("shipment_id")})
    await emit(run["id"], "customs", "Customs message saved", kind="document", data={"documents": [{"id": m["id"],
        "title": filename, "kind": m.get("type"), "filename": filename, "file_id": fid, "content_type": "application/xml"}],
        "facts": {"schema valid": (m.get("validation") or {}).get("valid")}})
