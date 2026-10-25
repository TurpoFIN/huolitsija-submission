"""Trade documents API: generate, amend, view, download and send B/L, HAWB, CMR, commercial invoice, packing list,
certificate of origin, EUR.1, DG declaration, arrival notice, delivery order and booking confirmation.

Customers see the issued (and draft) documents of their own company and download them; ops (shipments.write) create,
amend, cancel and send. Signed download links (from the e-mails) and the authenticity check need no sign-in.
"""
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.db import db
from core.deps import company_scope, current_user, is_internal, require_perm
from core.util import NO_ID
from services.tradedocs import catalog, data, dispatch, service, structured
from services.tradedocs import release
from services.tradedocs import jobs as _jobs  # noqa: F401  (registers the job kinds and event handlers)

router = APIRouter(prefix="/api", tags=["trade-documents"])


class IssueIn(BaseModel):
    kind: str
    final: bool = True


class AmendIn(BaseModel):
    changes: dict = Field(default_factory=dict)
    reason: str


class CancelIn(BaseModel):
    reason: str


def _visible(doc: dict, user: dict) -> bool:
    if is_internal(user):
        return True
    return user.get("org_type") == "customer" and doc.get("company_id") == user.get("company_id")


async def _doc(doc_id: str, user: dict) -> dict:
    doc = await service.get(doc_id)
    if not doc or not _visible(doc, user):
        raise HTTPException(404, "Document not found")
    return doc


async def _shipment(shipment_id: str, user: dict) -> dict:
    if user.get("org_type") == "partner":
        raise HTTPException(403, "Not available for partner accounts")
    s = await db.shipments.find_one({"id": shipment_id, **company_scope(user)}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


def _strip(doc: dict, user: dict) -> dict:
    out = service.public(doc)
    out.pop("structured", None)
    if not is_internal(user):
        out.pop("data", None)
        out["dispatches"] = [{k: v for k, v in d.items() if k in ("at", "role", "status", "version", "lang")}
                             for d in out.get("dispatches") or []]
    return out


@router.get("/shipments/{shipment_id}/trade-documents")
async def list_docs(shipment_id: str, history: bool = False, user: dict = Depends(current_user)):
    s = await _shipment(shipment_id, user)
    docs = await service.list_for_shipment(shipment_id, include_superseded=history)
    out = {"documents": [_strip(d, user) for d in docs]}
    if is_internal(user):
        out["delivery_release"] = s.get("delivery_release")
        ctx = await data.build_context(s)
        out["available"] = service.suggestions(ctx)
        from services.tradedocs.jobs import reached
        out["milestones"] = reached(s)
        house_bl = any(k["kind"] == "house_bl" and k["applies"] for k in out["available"])
        out["ebl_missing"] = structured.dcsa_missing(ctx) if house_bl else []
    return out


@router.post("/shipments/{shipment_id}/trade-documents")
async def create_doc(shipment_id: str, body: IssueIn, user: dict = Depends(require_perm("shipments.write"))):
    await _shipment(shipment_id, user)
    doc = await service.issue(shipment_id, body.kind, actor=user, final=body.final, source="ops")
    return _strip(doc, user)


@router.post("/shipments/{shipment_id}/delivery-release")
async def record_delivery_release(shipment_id: str, body: release.ReleaseIn,
                                  user: dict = Depends(require_perm("shipments.write"))):
    await _shipment(shipment_id, user)
    return await release.record(shipment_id, body, user)


@router.get("/trade-documents/kinds")
async def kinds(user: dict = Depends(current_user)):
    return {"kinds": [{"kind": k, **{x: v for x, v in spec.items()}} for k, spec in catalog.KINDS.items()],
            "milestones": catalog.MILESTONES}


@router.get("/trade-documents/verify/{doc_no}")
async def verify(doc_no: str, v: int | None = None):
    """Public authenticity check (the QR code on every page): is this number and version genuine and current?"""
    if not re.fullmatch(r"[A-Z0-9\-]{4,24}", doc_no or ""):
        raise HTTPException(404, "Unknown document")
    q = {"doc_no": doc_no}
    if v:
        q["version"] = v
    doc = await db.trade_documents.find_one(q, {"_id": 0, "doc_no": 1, "kind": 1, "version": 1, "status": 1, "current": 1,
                                                "issued_at": 1, "sha256": 1, "superseded_by": 1}, sort=[("version", -1)])
    if not doc:
        raise HTTPException(404, "Unknown document")
    latest = await db.trade_documents.find_one({"doc_no": doc_no, "current": True}, {"_id": 0, "version": 1, "status": 1})
    return {"genuine": True, "doc_no": doc["doc_no"], "kind": doc["kind"], "title": catalog.KINDS[doc["kind"]]["title"],
            "version": doc["version"], "status": doc["status"], "issued_at": doc["issued_at"], "sha256": doc["sha256"],
            "current_version": (latest or {}).get("version"), "current_status": (latest or {}).get("status"),
            "is_current": bool(doc.get("current"))}


@router.get("/trade-documents/{doc_id}")
async def get_doc(doc_id: str, user: dict = Depends(current_user)):
    return _strip(await _doc(doc_id, user), user)


@router.get("/trade-documents/{doc_id}/pdf")
async def get_pdf(doc_id: str, lang: str = Query("fi"), download: bool = False, user: dict = Depends(current_user)):
    doc = await _doc(doc_id, user)
    blob, name = await service.pdf_bytes(doc["id"], lang)
    disp = "attachment" if download else "inline"
    return Response(blob, media_type="application/pdf",
                    headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(name)}", "Cache-Control": "private, max-age=60"})


@router.get("/trade-documents/{doc_id}/data")
async def get_data(doc_id: str, format: str = Query("json"), user: dict = Depends(current_user)):
    doc = await _doc(doc_id, user)  # customers may download the structured files of their own documents too
    out = await service.export(doc["id"], format)
    if isinstance(out, str):
        return Response(out, media_type="application/xml",
                        headers={"Content-Disposition": f"inline; filename={doc['doc_no']}-v{doc['version']}.{format}.xml"})
    return out


@router.get("/trade-documents/{doc_id}/download/{part}")
async def signed_download(doc_id: str, part: str, exp: int, sig: str):
    """The link in the e-mails: no sign-in, HMAC-signed, expires."""
    if not dispatch.check(doc_id, part, exp, sig):
        raise HTTPException(403, "This link has expired or is not valid. Ask Huolitsija for a new one.")
    doc = await service.get(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if part.startswith("pdf_"):
        blob, name = await service.pdf_bytes(doc_id, part[4:])
        return Response(blob, media_type="application/pdf", headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
    out = await service.export(doc_id, part)
    if isinstance(out, str):
        return Response(out, media_type="application/xml")
    return Response(json.dumps(out, ensure_ascii=False, indent=1), media_type="application/json")


@router.get("/trade-documents/{doc_no}/versions")
async def versions(doc_no: str, user: dict = Depends(current_user)):
    rows = await service.versions(doc_no)
    rows = [r for r in rows if _visible(r, user)]
    if not rows:
        raise HTTPException(404, "Document not found")
    trail = []
    if is_internal(user):
        ids = [r["id"] for r in rows]
        trail = await db.audit_log.find({"entity": "trade_document", "entity_id": {"$in": ids}}, NO_ID).sort("at", 1).to_list(200)
    return {"versions": [_strip(r, user) for r in rows], "audit": trail}


@router.post("/trade-documents/{doc_no}/amend")
async def amend(doc_no: str, body: AmendIn, user: dict = Depends(require_perm("shipments.write"))):
    doc = await service.amend(doc_no, body.changes, body.reason, actor=user)
    return _strip(doc, user)


@router.post("/trade-documents/{doc_no}/refresh")
async def refresh(doc_no: str, user: dict = Depends(require_perm("shipments.write"))):
    doc = await service.refresh(doc_no, actor=user, reason="Updated from the shipment's current data")
    if not doc:
        raise HTTPException(409, "The document already matches the shipment.")
    return _strip(doc, user)


@router.post("/trade-documents/{doc_no}/cancel")
async def cancel(doc_no: str, body: CancelIn, user: dict = Depends(require_perm("shipments.write"))):
    return _strip(await service.cancel(doc_no, body.reason, actor=user), user)


@router.post("/trade-documents/{doc_id}/send")
async def send(doc_id: str, user: dict = Depends(require_perm("shipments.write"))):
    """A person sends it: no Autopilot gate (the person is the decision)."""
    await _doc(doc_id, user)
    return await dispatch.send(doc_id, milestone="manual", actor=user, gated=False)


@router.post("/shipments/{shipment_id}/trade-documents/run")
async def run_now(shipment_id: str, user: dict = Depends(require_perm("shipments.write"))):
    """Run the milestone agent for this shipment now (what the job does), e.g. after fixing data."""
    await _shipment(shipment_id, user)
    from services.tradedocs.jobs import progress
    from core.jobs import JobDeferred
    try:
        return await progress(shipment_id)
    except JobDeferred:
        raise HTTPException(409, "Trade documents are being processed. Retry shortly.")


@router.post("/trade-documents/{doc_id}/ebl-issue")
async def ebl_issue(doc_id: str, user: dict = Depends(require_perm("shipments.write"))):
    """Send the house B/L to the shipper's eBL platform (DCSA eBL Issuance API 3.0)."""
    from services.tradedocs import ebl
    doc = await _doc(doc_id, user)
    if doc["kind"] != "house_bl":
        raise HTTPException(400, "Only a house bill of lading can be issued as an eBL.")
    return await ebl.request_issuance(doc, actor=user)


@router.post("/webhooks/dcsa/v3/ebl-issuance-responses", status_code=204)
async def ebl_issuance_response(request: Request):
    """The eBL platform's verdict (DCSA IssuanceResponse). Authenticated with the bearer token EBL_WEBHOOK_TOKEN."""
    import hmac as _hmac

    from core import config
    from services.tradedocs import ebl
    token = config.env("EBL_WEBHOOK_TOKEN")
    got = (request.headers.get("authorization") or "")[7:]
    if not token or not _hmac.compare_digest(token, got):
        raise HTTPException(401, "Unauthorized")
    body = await request.json()
    if body.get("issuanceResponseCode") not in ("ISSU", "BREQ", "REFU") or not body.get("transportDocumentReference"):
        raise HTTPException(400, "Not a DCSA IssuanceResponse")
    if not await ebl.record_response(body):
        raise HTTPException(404, "Unknown transportDocumentReference")
    return Response(status_code=204)
