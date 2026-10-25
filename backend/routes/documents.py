"""Documents API: upload, list, preview/download, AI extraction review, required-documents checklist and
cross-document tie-out."""
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from core.audit import actor_ref, audit
from core.db import db
from core.deps import company_scope, current_user, is_internal
from core.events import emit
from core.storage import get_file
from core.util import NO_ID, now_iso
from services import documents as docs_svc
from services import extraction

router = APIRouter(prefix="/api", tags=["documents"])


def _no_partner(user: dict):
    if user.get("org_type") == "partner":
        raise HTTPException(403, "Documents are not available for partner accounts")


async def _get_doc(doc_id: str, user: dict) -> dict:
    _no_partner(user)
    doc = await db.documents.find_one({"id": doc_id, **company_scope(user)}, NO_ID)
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


async def _get_shipment(shipment_id: str, user: dict) -> dict:
    s = await db.shipments.find_one({"id": shipment_id, **company_scope(user)}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


@router.get("/documents/specs")
async def specs(user: dict = Depends(current_user)):
    return {"types": docs_svc.DOC_TYPES, "extractable": sorted(docs_svc.EXTRACTABLE), "fields": extraction.field_specs_public(),
            "accepted": docs_svc.ACCEPTED, "max_bytes": docs_svc.MAX_BYTES}


@router.get("/documents")
async def list_documents(shipment_id: str | None = None, type: str | None = None, status: str | None = None,
                         q: str | None = None, company_id: str | None = None, include_test: bool = False,
                         limit: int = Query(200, le=500), user: dict = Depends(current_user)):
    _no_partner(user)
    base = dict(company_scope(user))
    if not include_test and not shipment_id and not is_internal(user):
        base["test"] = {"$ne": True}
    if company_id and is_internal(user):
        base["company_id"] = company_id
    if shipment_id:
        base["shipment_id"] = shipment_id
    if type:
        base["type"] = type
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        base["$or"] = [{"filename": rx}, {"number": rx}, {"extraction.fields.seller_name.value": rx},
                       {"extraction.fields.buyer_name.value": rx}]
    query = dict(base)
    if status:
        sts = [x.strip() for x in status.split(",") if x.strip()]
        query["extraction.status"] = sts[0] if len(sts) == 1 else {"$in": sts}
    proj = {"_id": 0, "extraction.line_items": 0, "extraction.fields": 0}
    items = await db.documents.find(query, proj).sort("uploaded_at", -1).to_list(limit)
    total = await db.documents.count_documents(query)
    counts = {}
    async for row in db.documents.aggregate([{"$match": base}, {"$group": {"_id": "$extraction.status", "n": {"$sum": 1}}}]):
        counts[row["_id"] or "none"] = row["n"]
    ship_ids = list({d["shipment_id"] for d in items if d.get("shipment_id")})
    refs = {s["id"]: s.get("reference") async for s in db.shipments.find({"id": {"$in": ship_ids}}, {"_id": 0, "id": 1, "reference": 1})}
    comp_ids = list({d["company_id"] for d in items if d.get("company_id")}) if is_internal(user) else []
    names = {c["id"]: c.get("name") async for c in db.companies.find({"id": {"$in": comp_ids}}, {"_id": 0, "id": 1, "name": 1})}
    for d in items:
        d["shipment_ref"] = refs.get(d.get("shipment_id"))
        if names:
            d["company_name"] = names.get(d.get("company_id"))
        ex = d.get("extraction") or {}
        d["warnings_count"] = len([w for w in ex.get("warnings") or [] if w.get("severity") in ("error", "warning")])
        d["failed_checks"] = len([c for c in d.get("checks") or [] if c.get("ok") is False])
    return {"items": items, "total": total, "counts": counts}


@router.post("/documents")
async def upload_document(request: Request, file: UploadFile = File(...), type: str | None = Form(None),
                          shipment_id: str | None = Form(None), company_id: str | None = Form(None),
                          user: dict = Depends(current_user)):
    _no_partner(user)
    shipment = await _get_shipment(shipment_id, user) if shipment_id else None
    if is_internal(user):
        cid = (shipment or {}).get("company_id") or company_id
        if cid and not await db.companies.find_one({"id": cid}, {"_id": 1}):
            raise HTTPException(404, "Company not found")
        if not cid:
            raise HTTPException(400, "Choose the customer or the shipment this document belongs to")
    else:
        cid = user.get("company_id")
    size = getattr(file, "size", None)
    if size and size > docs_svc.MAX_BYTES:
        raise HTTPException(413, f"The file is {size / 1048576:.1f} MB; the limit is 20 MB.")
    data = await file.read(docs_svc.MAX_BYTES + 1)  # never buffer more than the limit
    if len(data) > docs_svc.MAX_BYTES:
        raise HTTPException(413, "The file is larger than the 20 MB limit.")
    doc = await docs_svc.create_document(company_id=cid, shipment_id=shipment_id, doc_type=type or None,
                                         filename=file.filename or "document", data=data, actor=user,
                                         source="ops" if is_internal(user) else "portal",
                                         extra={"test": True} if (shipment or {}).get("test") else None)
    return doc


@router.get("/documents/required")
async def required(shipment_id: str, user: dict = Depends(current_user)):
    shipment = await _get_shipment(shipment_id, user)
    _no_partner(user)
    items = await docs_svc.required_documents(shipment)
    missing = [i for i in items if i["required"] and not i["present"]]
    return {"items": items, "missing": len(missing), "shipment": {"id": shipment["id"], "mode": shipment.get("mode"),
                                                                   "direction": shipment.get("direction"),
                                                                   "incoterm": shipment.get("incoterm")}}


@router.get("/documents/tieout")
async def tieout(shipment_id: str, user: dict = Depends(current_user)):
    await _get_shipment(shipment_id, user)
    _no_partner(user)
    checks = await docs_svc.run_tieout(shipment_id)
    return {"items": checks, "failed": len([c for c in checks if c.get("ok") is False])}


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, user: dict = Depends(current_user)):
    doc = await _get_doc(doc_id, user)
    if doc.get("shipment_id"):
        s = await db.shipments.find_one({"id": doc["shipment_id"]}, {"_id": 0, "reference": 1, "direction": 1, "mode": 1})
        doc["shipment_ref"] = (s or {}).get("reference")
    decls = await db.customs_declarations.find({"id": {"$in": [u["declaration_id"] for u in doc.get("used_by") or []]}},
                                               {"_id": 0, "id": 1, "lrn": 1, "status": 1}).to_list(20)
    doc["declarations"] = decls
    doc["can_delete"] = (is_internal(user) or (doc.get("uploaded_by") or {}).get("id") == user["id"]) and not any(
        d["status"] not in ("draft", "needs_info") for d in decls)
    if doc.get("extraction"):
        doc["extraction"] = extraction.localize_extraction(doc["extraction"])  # Finnish for warnings stored in English
    return doc


@router.get("/documents/{doc_id}/file")
async def download(doc_id: str, download: bool = False, user: dict = Depends(current_user)):
    doc = await _get_doc(doc_id, user)
    try:
        data, _name, ct = await get_file(doc["file_id"])
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "The stored file is missing")
    disp = "attachment" if download else "inline"
    fname = doc.get("filename") or "document"
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
    return Response(content=data, media_type=doc.get("content_type") or ct, headers={
        "Content-Disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}",
        "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


def _render_preview(data: bytes, content_type: str, page: int, width: int) -> bytes:
    import io

    from PIL import Image

    if content_type == "application/pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            if page < 1 or page > len(pdf):
                raise HTTPException(404, f"The document has {len(pdf)} page(s)")
            p = pdf[page - 1]
            w, _h = p.get_size()
            img = p.render(scale=max(0.2, min(4.0, width / w))).to_pil()
        finally:
            pdf.close()
    else:
        if page != 1:
            raise HTTPException(404, "The document has 1 page")
        img = Image.open(io.BytesIO(data))
        if img.width > width:
            img = img.resize((width, max(1, int(img.height * width / img.width))))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@router.get("/documents/{doc_id}/preview")
async def preview(doc_id: str, page: int = 1, width: int = Query(1200, ge=200, le=2400),
                  user: dict = Depends(current_user)):
    """One page as PNG (mobile browsers and headless Chromium do not render PDFs in an iframe)."""
    import asyncio

    doc = await _get_doc(doc_id, user)
    ct = doc.get("content_type") or ""
    if not (ct == "application/pdf" or ct.startswith("image/")):
        raise HTTPException(415, "No preview for spreadsheets; download the file")
    try:
        data, _name, _ct = await get_file(doc["file_id"])
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "The stored file is missing")
    png = await asyncio.get_running_loop().run_in_executor(None, _render_preview, data, ct, page, width)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300", "X-Page-Count": str(doc.get("pages") or 1)})


class DocPatch(BaseModel):
    type: str | None = None
    shipment_id: str | None = None
    fields: dict | None = None
    line_items: list[dict] | None = None
    confirm: bool | None = None


@router.patch("/documents/{doc_id}")
async def patch_document(doc_id: str, body: DocPatch, user: dict = Depends(current_user)):
    doc = await _get_doc(doc_id, user)
    if not is_internal(user) and "company.book" not in (user.get("permissions") or []) and \
            "company.manage" not in (user.get("permissions") or []):
        raise HTTPException(403, "Your role cannot edit documents")
    sets: dict = {}
    changes: dict = {}
    if body.type and body.type != doc.get("type"):
        if body.type not in docs_svc.DOC_TYPES:
            raise HTTPException(400, f"Unknown document type '{body.type}'")
        sets.update({"type": body.type, "type_source": "user"})
        changes["type"] = body.type
    if body.shipment_id is not None and body.shipment_id != doc.get("shipment_id"):
        if body.shipment_id:
            s = await _get_shipment(body.shipment_id, user)
            if s.get("company_id") != doc.get("company_id") and doc.get("company_id"):
                raise HTTPException(400, "That shipment belongs to another company")
        sets["shipment_id"] = body.shipment_id or None
        changes["shipment_id"] = body.shipment_id
    doc_type = sets.get("type") or doc.get("type")
    spec = extraction.FIELD_SPECS.get(doc_type) or extraction.FIELD_SPECS["other"]
    ex = dict(doc.get("extraction") or {})
    if body.fields is not None:
        fields = dict(ex.get("fields") or {})
        for k, v in body.fields.items():
            if k not in spec["fields"]:
                raise HTTPException(400, f"'{k}' is not a field of a {doc_type.replace('_', ' ')}")
            nv = extraction._norm_value(spec["fields"][k]["kind"], v)
            if nv in (None, "", []):
                fields.pop(k, None)
            else:
                prev = fields.get(k) or {}
                if prev.get("value") != nv:
                    fields[k] = {"value": nv, "confidence": 1.0, "source": "user", "previous": prev.get("value"),
                                 "edited_by": actor_ref(user), "edited_at": now_iso()}
        ex["fields"] = fields
        changes["fields"] = list(body.fields.keys())
    if body.line_items is not None:
        li_spec = spec.get("line_items") or {}
        items = []
        for raw in body.line_items:
            it = {k: extraction._norm_value(s["kind"], raw.get(k)) for k, s in li_spec.items() if raw.get(k) not in (None, "")}
            if it:
                it["confidence"] = 1.0
                it["source"] = "user"
                items.append(it)
        ex["line_items"] = items
        changes["line_items"] = len(items)
    if body.fields is not None or body.line_items is not None or body.type:
        ex["warnings"] = extraction.post_check(doc_type, ex.get("fields") or {}, ex.get("line_items") or [])
    if body.confirm:
        errors = [w for w in ex.get("warnings") or [] if w.get("severity") == "error"
                  and w.get("code") not in ("type_mismatch", "model_note")]
        if errors:
            raise HTTPException(400, "Fix these before confirming: " + "; ".join(w["message"] for w in errors[:4]))
        ex.update({"status": "done", "confirmed_by": actor_ref(user), "confirmed_at": now_iso(), "reason": None})
        changes["confirmed"] = True
    if ex != (doc.get("extraction") or {}):
        sets["extraction"] = ex
        f = ex.get("fields") or {}
        for key in ("invoice_number", "bl_number", "awb_number", "cmr_number", "packing_list_number", "certificate_number"):
            if (f.get(key) or {}).get("value"):
                sets["number"] = f[key]["value"]
                break
    if not sets:
        return await get_document(doc_id, user)
    await db.documents.update_one({"id": doc_id}, {"$set": sets})
    await audit(user, "document_updated", "document", doc_id, changes, company_id=doc.get("company_id"))
    fresh = await db.documents.find_one({"id": doc_id}, NO_ID)
    if fresh.get("shipment_id"):
        await docs_svc.run_tieout(fresh["shipment_id"])
        fresh = await db.documents.find_one({"id": doc_id}, NO_ID)
    if body.confirm:
        # Drafting the declaration takes a while (HS agent); run it after the response.
        await emit("document.extracted", {"document": fresh, "confirmed": True}, background=True)
    return await get_document(doc_id, user)


@router.post("/documents/{doc_id}/reextract")
async def reextract(doc_id: str, user: dict = Depends(current_user)):
    doc = await _get_doc(doc_id, user)
    if doc.get("type") not in docs_svc.EXTRACTABLE and doc.get("type_source") == "user":
        raise HTTPException(400, f"A {doc['type'].replace('_', ' ')} is not read automatically")
    if (doc.get("extraction") or {}).get("status") in ("pending", "processing"):
        raise HTTPException(409, "Already being read")
    await extraction.requeue(doc_id)
    await audit(user, "document_reextract", "document", doc_id, {}, company_id=doc.get("company_id"))
    return await db.documents.find_one({"id": doc_id}, NO_ID)


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, user: dict = Depends(current_user)):
    doc = await _get_doc(doc_id, user)
    if not is_internal(user) and (doc.get("uploaded_by") or {}).get("id") != user["id"]:
        raise HTTPException(403, "You can delete only documents you uploaded")
    await docs_svc.delete_document(doc, user)
    return {"ok": True}
