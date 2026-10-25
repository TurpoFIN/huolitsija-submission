"""Customs API: declaration queue and detail, edits, customer confirmation, approval-gated filing, manual
Tulli responses, generated XML / PDF, and HS classification."""
import asyncio
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.audit import actor_ref, audit
from core.db import db
from core.deps import company_scope, current_user, is_internal, require_internal, require_perm
from core.util import NO_ID, now_iso
from services import customs as svc
from services import hs as hs_svc
from services import seed_customs  # noqa: F401  (registers the startup seed, order 40)
from services import tulli

router = APIRouter(prefix="/api", tags=["customs"])
_bg: set = set()


def _no_partner(user: dict):
    if user.get("org_type") == "partner":
        raise HTTPException(403, "Customs declarations are not available for partner accounts")


async def _get(decl_id: str, user: dict) -> dict:
    _no_partner(user)
    d = await db.customs_declarations.find_one({"id": decl_id, **company_scope(user)}, NO_ID)
    if not d:
        raise HTTPException(404, "Declaration not found")
    return d


def _public(d: dict, user: dict) -> dict:
    if not is_internal(user):
        sub = d.get("submission") or {}
        d["submission"] = {k: v for k, v in sub.items() if k in ("channel", "state", "submitted_at", "note")}
        d.pop("edits", None)
        if d.get("demo"):
            d["history"] = [{**h, "note": (h.get("note") or "").replace(" (demo)", ""),
                             "by": {**(h.get("by") or {}), "name": (h.get("by") or {}).get("name", "").replace(" Demo", "")}}
                            for h in d.get("history") or []]
        d["validations"] = [v for v in d.get("validations") or [] if v.get("owner") == "customer" or not v.get("ok")]
    return d


# ---------------------------------------------------------------------------------------------
# HS
# ---------------------------------------------------------------------------------------------
class HsIn(BaseModel):
    description: str
    origin_country: str | None = None
    hs_hint: str | None = None


@router.post("/hs/suggest")
async def hs_suggest(body: HsIn, user: dict = Depends(current_user)):
    _no_partner(user)
    if len((body.description or "").strip()) < 3:
        raise HTTPException(400, "Describe the goods (what they are, material, use)")
    return await hs_svc.suggest(body.description.strip()[:600], (body.origin_country or "").upper()[:2] or None,
                                hs_hint=body.hs_hint, ref={"type": "hs_lookup", "id": user["id"]},
                                company_id=user.get("company_id"))


@router.get("/hs/search")
async def hs_search(q: str = Query(..., min_length=2), limit: int = Query(15, le=50), user: dict = Depends(current_user)):
    _no_partner(user)
    return {"items": hs_svc.search(q, limit=limit), "edition": hs_svc.HS_EDITION}


@router.get("/hs/code/{code}")
async def hs_code(code: str, origin: str | None = None, user: dict = Depends(current_user)):
    e = hs_svc.hs_entry(re.sub(r"\D", "", code)[:6])
    if not e:
        raise HTTPException(404, "Not an HS 2022 code")
    return {**e, "flags": hs_svc.flags_for(code, origin), "taric_url": hs_svc.taric_url(code, origin, now_iso()[:10])}


# ---------------------------------------------------------------------------------------------
# Queue / list
# ---------------------------------------------------------------------------------------------
@router.get("/customs")
async def list_declarations(status: str | None = None, q: str | None = None, company_id: str | None = None,
                            shipment_id: str | None = None, mine: bool = False, include_test: bool = False,
                            limit: int = Query(300, le=1000), user: dict = Depends(current_user)):
    _no_partner(user)
    base = dict(company_scope(user))
    if not include_test and not shipment_id and not is_internal(user):
        base["test"] = {"$ne": True}  # automated-test data stays out of queues and lists
    if company_id and is_internal(user):
        base["company_id"] = company_id
    if shipment_id:
        base["shipment_id"] = shipment_id
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        base["$or"] = [{"lrn": rx}, {"mrn": rx}, {"shipment_ref": rx}, {"company_name": rx}, {"items.description": rx},
                       {"items.cn_code": rx}]
    if mine and is_internal(user):
        base["assignee.id"] = user["id"]
    query = dict(base)
    if status:
        query["status"] = {"$in": status.split(",")}
    docs = await db.customs_declarations.find(query, svc.list_projection()).sort("updated_at", -1).to_list(limit)
    counts = {}
    async for row in db.customs_declarations.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    items = [svc.summarize(d) for d in docs]
    ship_ids = [i["shipment_id"] for i in items if i.get("shipment_id")]
    etas = {s["id"]: s async for s in db.shipments.find({"id": {"$in": ship_ids}},
                                                         {"_id": 0, "id": 1, "eta": 1, "status": 1, "mode": 1, "holds": 1,
                                                          "origin": 1, "destination": 1})}
    for i in items:
        s = etas.get(i.get("shipment_id")) or {}
        i["eta"] = s.get("eta")
        i["shipment_status"] = s.get("status")
        i["mode"] = s.get("mode")
        i["on_hold"] = s.get("status") == "customs_hold" or any(h.get("type") == "customs" for h in s.get("holds") or [])
        i["route"] = f"{(s.get('origin') or {}).get('name') or '—'} → {(s.get('destination') or {}).get('name') or '—'}" if s else None
    return {"items": items, "total": len(items), "counts": counts, "gateway": tulli.get_gateway().status()}


class DraftIn(BaseModel):
    shipment_id: str


@router.post("/customs")
async def create_draft(body: DraftIn, user: dict = Depends(current_user)):
    _no_partner(user)
    if is_internal(user) and "customs.review" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: customs.review")
    s = await db.shipments.find_one({"id": body.shipment_id, **company_scope(user)}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    if s.get("direction") != "import":
        raise HTTPException(400, "Only import declarations are drafted here; this shipment is " + (s.get("direction") or "unknown"))
    decl = await svc.draft_declaration_for_shipment(body.shipment_id, user)
    scr = await svc.screen_seller(decl)
    if scr:
        await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"screening": scr}})
        decl = await svc.save_and_recompute(await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID), user, "screened")
    return _public(decl, user)


@router.get("/customs/gateway")
async def gateway_status(declaration_id: str | None = None, user: dict = Depends(require_internal)):
    """The configured gateway; with ?declaration_id= the one that declaration files through (a sandbox test company
    always files with sandbox Tulli, whatever TULLI_MODE says)."""
    if declaration_id:
        d = await db.customs_declarations.find_one({"id": declaration_id}, {"_id": 0, "id": 1, "company_id": 1})
        if not d:
            raise HTTPException(404, "Declaration not found")
        st = (await tulli.gateway_for(d)).status()
    else:
        st = tulli.get_gateway().status()
    return {**st, "template_notes": tulli.TEMPLATE_NOTES}


# ---------------------------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------------------------
@router.get("/customs/{decl_id}")
async def get_declaration(decl_id: str, user: dict = Depends(current_user)):
    d = await _get(decl_id, user)
    s = await db.shipments.find_one({"id": d.get("shipment_id")}, {"_id": 0, "id": 1, "reference": 1, "mode": 1, "status": 1,
                                                                   "eta": 1, "origin": 1, "destination": 1, "incoterm": 1,
                                                                   "direction": 1}) if d.get("shipment_id") else None
    d["shipment"] = s
    if d.get("approval_id"):
        d["approval"] = await db.approvals.find_one({"id": d["approval_id"]}, {"_id": 0, "id": 1, "status": 1, "decided_by": 1,
                                                                               "decided_at": 1, "decision_note": 1, "created_at": 1})
    d["gateway"] = tulli.get_gateway().status()
    perms = set(user.get("permissions") or [])
    d["can"] = {
        "edit": d["status"] in (svc.EDITABLE_BROKER if is_internal(user) else svc.EDITABLE_CUSTOMER) and (
            "customs.review" in perms if is_internal(user) else True),
        "confirm": not is_internal(user) and d["status"] in ("draft", "needs_info"),
        "request_approval": "customs.file" in perms and d["status"] in ("ready_for_review", "draft", "needs_info", "rejected")
        and not (d.get("approval") or {}).get("status") == "pending",
        "decide_approval": "customs.file" in perms and (d.get("approval") or {}).get("status") == "pending",
        "record_response": "customs.file" in perms and d["status"] in ("approved", "submitted", "accepted", "under_control",
                                                                       "rejected", "released", "not_released", "invalidated"),
        "file": "customs.file" in perms and d["status"] == "approved",
    }
    return _public(d, user)


class CustomsPatch(BaseModel):
    items: list[dict] | None = None
    office: dict | str | None = None
    location_of_goods: str | None = None
    goods_location: dict | None = None
    representation_type: str | None = None
    valuation: dict | None = None
    delivery_terms: dict | None = None
    transport: dict | None = None


class ExportStatisticalValue(BaseModel):
    seq: int = Field(gt=0)
    value_eur: float = Field(gt=0, allow_inf_nan=False)
    basis: str = Field(min_length=10, max_length=2000)


class ShipmentCustomsInstructions(BaseModel):
    goods_location: dict
    transaction_nature: str
    export_statistical_values: list[ExportStatisticalValue] | None = None
    export_office: str | None = None


@router.patch("/customs/shipments/{shipment_id}/instructions")
async def shipment_customs_instructions(shipment_id: str, body: ShipmentCustomsInstructions,
                                        user: dict = Depends(current_user)):
    """Customer statement of presentation location and transaction facts; no filing authority."""
    _no_partner(user)
    shipment = await db.shipments.find_one({"id": shipment_id, **company_scope(user)}, NO_ID)
    if not shipment:
        raise HTTPException(404, "Shipment not found")
    if not is_internal(user) and not ({"company.book", "company.manage"} & set(user.get("permissions") or [])):
        raise HTTPException(403, "Your role cannot provide customs instructions")
    if shipment.get("status") in ("delivered", "cancelled"):
        raise HTTPException(409, "The shipment is closed")
    location = body.goods_location
    address = location.get("address") or {}
    if location.get("type") not in ("A", "B", "C", "D") or not all(address.get(k) for k in ("street", "postal_code", "city", "country")):
        raise HTTPException(422, "Give the presentation location type and full postal address")
    from services.tulli_codelists import entry
    if not entry("CL091", body.transaction_nature):
        raise HTTPException(422, "Transaction nature is not a CL091 code")
    fields = {"goods_location": {"type": location["type"], "address": {k: str(address[k])[:200] for k in ("street", "postal_code", "city", "country")},
              "source": "customer_statement"}, "transaction_nature": body.transaction_nature,
              "customs_instructions": {"by": actor_ref(user), "at": now_iso()}}
    if body.export_statistical_values is not None:
        if shipment.get('direction') != 'export' or len({v.seq for v in body.export_statistical_values}) != len(body.export_statistical_values):
            raise HTTPException(422, 'Export statistical values need unique export goods-item sequences')
        fields['export_statistical_values'] = [{**v.model_dump(), 'source': 'customer_statement'} for v in body.export_statistical_values]
    if body.export_office:
        from services.customs_offices import verify
        await verify(body.export_office, 'EXP')
        fields['export_office'] = {'code': body.export_office, 'source': 'customer_statement'}
    await db.shipments.update_one({"id": shipment_id}, {"$set": fields})
    await audit(user, "shipment_customs_instructions", "shipment", shipment_id, fields, company_id=shipment["company_id"])
    from core.events import emit
    await emit("shipment.customs_data_updated", {"shipment": {**shipment, **fields}, "actor": user})
    return {"shipment_id": shipment_id, **fields}


@router.patch("/customs/{decl_id}")
async def patch_declaration(decl_id: str, body: CustomsPatch, user: dict = Depends(current_user)):
    d = await _get(decl_id, user)
    if is_internal(user) and "customs.review" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: customs.review")
    if not is_internal(user) and not ({"company.book", "company.manage"} & set(user.get("permissions") or [])):
        raise HTTPException(403, "Your role cannot edit declarations")
    payload = body.model_dump(exclude_unset=True)
    d = await svc.apply_patch(d, payload, user)
    return await get_declaration(decl_id, user)


@router.post("/customs/{decl_id}/submit-for-review")
async def submit_for_review(decl_id: str, user: dict = Depends(current_user)):
    d = await _get(decl_id, user)
    if is_internal(user) and "customs.review" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: customs.review")
    await svc.submit_for_review(d, user)
    return await get_declaration(decl_id, user)


@router.post("/customs/{decl_id}/approve")
async def approve(decl_id: str, user: dict = Depends(require_perm("customs.file"))):
    d = await _get(decl_id, user)
    await svc.request_filing(d, user)
    return await get_declaration(decl_id, user)


@router.post("/customs/{decl_id}/file")
async def file_again(decl_id: str, user: dict = Depends(require_perm("customs.file"))):
    d = await _get(decl_id, user)
    if d["status"] != "approved":
        raise HTTPException(409, "Only an approved declaration can be filed")
    await svc.file_declaration(d, user)
    return await get_declaration(decl_id, user)


@router.post("/customs/{decl_id}/record-response")
async def record_response(decl_id: str, event: str = Form(...), mrn: str | None = Form(None), note: str | None = Form(None),
                          at: str | None = Form(None), file: UploadFile | None = File(None),
                          user: dict = Depends(require_perm("customs.file"))):
    from services.documents import create_document

    d = await _get(decl_id, user)
    if event not in svc.RECORDABLE or d["status"] not in svc.RESPONDABLE:
        await svc.record_response(d, event, user, mrn=mrn, note=note)  # raises the explanatory 400/409
    doc_id = None
    if file is not None and file.filename:
        from services.documents import MAX_BYTES

        data = await file.read(MAX_BYTES + 1)
        doc = await create_document(company_id=d.get("company_id"), shipment_id=d.get("shipment_id"),
                                    doc_type="customs_decision", filename=file.filename, data=data, actor=user,
                                    source="ops", extract=False,
                                    extra={"declaration_id": decl_id, "visibility": ["customer", "ops"]})
        doc_id = doc["id"]
    if at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?", at):
        raise HTTPException(400, "Time must be an ISO date or date-time")
    at_iso = None
    if at:
        at_iso = at if "T" in at else f"{at}T12:00:00Z"
        if not at_iso.endswith("Z"):
            at_iso += "Z" if len(at_iso) > 16 else ":00Z"
    await svc.record_response(d, event, user, mrn=mrn, note=note, document_id=doc_id, at=at_iso)
    return await get_declaration(decl_id, user)


async def _redraft_bg(shipment_id: str, decl_id: str, user: dict):
    try:
        decl = await svc.draft_declaration_for_shipment(shipment_id, user)
        scr = await svc.screen_seller(decl)
        if scr:
            await db.customs_declarations.update_one({"id": decl["id"]}, {"$set": {"screening": scr}})
            await svc.save_and_recompute(await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID), user, "screened")
    finally:
        await db.customs_declarations.update_one({"id": decl_id}, {"$set": {"drafting": None}})


@router.post("/customs/{decl_id}/redraft")
async def redraft(decl_id: str, user: dict = Depends(require_perm("customs.review"))):
    d = await _get(decl_id, user)
    if d["status"] not in ("draft", "needs_info"):
        raise HTTPException(409, "Only a draft can be rebuilt from the documents")
    if d.get("drafting"):
        raise HTTPException(409, "Already being redrafted")
    await db.customs_declarations.update_one({"id": decl_id}, {"$set": {"drafting": {"since": now_iso(), "by": actor_ref(user)}}})
    t = asyncio.get_running_loop().create_task(_redraft_bg(d["shipment_id"], decl_id, user))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return await get_declaration(decl_id, user)


@router.post("/customs/{decl_id}/items/{seq}/classify")
async def classify_item(decl_id: str, seq: int, user: dict = Depends(require_perm("customs.review"))):
    d = await _get(decl_id, user)
    if d["status"] not in svc.EDITABLE_BROKER:
        raise HTTPException(409, "This declaration can no longer be edited")
    await svc.reclassify_item(d, seq, user)
    return await get_declaration(decl_id, user)




class AssignIn(BaseModel):
    user_id: str | None = None


@router.post("/customs/{decl_id}/assign")
async def assign(decl_id: str, body: AssignIn, user: dict = Depends(require_perm("customs.review"))):
    await _get(decl_id, user)
    a = None
    if body.user_id:
        u = await db.users.find_one({"id": body.user_id, "org_type": "internal"}, {"_id": 0, "id": 1, "name": 1})
        if not u:
            raise HTTPException(404, "User not found")
        a = {"id": u["id"], "name": u.get("name")}
    await db.customs_declarations.update_one({"id": decl_id}, {"$set": {"assignee": a}})
    await audit(user, "customs_assigned", "customs_declaration", decl_id, {"assignee": a})
    return await get_declaration(decl_id, user)


@router.get("/customs/{decl_id}/xml")
async def declaration_xml(decl_id: str, download: bool = False, user: dict = Depends(current_user)):
    d = await _get(decl_id, user)
    try:
        xml = tulli.build_message(d, "MANUAL")
    except ValueError as exc:
        raise HTTPException(422, {"code": "customs_xml_invalid", "message": str(exc)}) from exc
    headers = {"X-Template-Version": tulli.TEMPLATE_VERSION}
    if download:
        headers["Content-Disposition"] = f"attachment; filename=\"{d['lrn']}.xml\""
    return Response(content=xml, media_type="application/xml", headers=headers)


@router.get("/customs/{decl_id}/pdf")
async def declaration_pdf(decl_id: str, download: bool = False, user: dict = Depends(current_user)):
    d = await _get(decl_id, user)
    pdf = await asyncio.get_running_loop().run_in_executor(None, tulli.render_summary_pdf, d)
    disp = "attachment" if download else "inline"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"{disp}; filename=\"{d['lrn']}.pdf\""})


@router.post("/customs/{decl_id}/decision")
async def record_decision(decl_id: str, body: dict, user: dict = Depends(require_perm("customs.file"))):
    from services.customs_decisions import record_decision as record
    await record(await _get(decl_id, user), body, user)
    return await get_declaration(decl_id, user)
