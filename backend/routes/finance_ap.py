"""Finance (s13) accounts-payable intake: cost accruals, carrier/agent purchase invoices and the purchase-invoice inbox.

All internal; write actions need billing.write. Payables and payment runs live in routes/finance_pay.py.
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from core.db import db
from core.deps import require_internal, require_perm
from core.tenant_visibility import hidden_test_companies
from core.util import NO_ID
from services import accruals as acc_svc
from services import ap_mailbox, ap_postmark
from services import carrier_invoices as ci_svc

router = APIRouter(prefix="/api/finance", tags=["finance"])
WRITE = require_perm("billing.write")
MAX_BYTES = 20 * 1024 * 1024


def _hide_test(q: dict) -> dict:
    ids = hidden_test_companies.get()
    if ids is not None:
        q["test"] = {"$ne": True}
        if ids:
            q["company_id"] = {"$nin": ids}
    return q


def _rx(q: str) -> dict:
    return {"$regex": re.escape(q.strip()), "$options": "i"}


# --------------------------------------------------------------------------------------------- accruals
@router.get("/accruals")
async def list_accruals(status: str | None = None, shipment_id: str | None = None, lane: str | None = None,
                        period: str | None = None, q: str | None = None, supplier: str | None = None,
                        limit: int = Query(200, ge=1, le=1000), skip: int = Query(0, ge=0),
                        user: dict = Depends(require_internal)):
    base = _hide_test({})
    if shipment_id:
        base["shipment_id"] = shipment_id
    if lane:
        base["lane.key"] = lane
    if period:
        base["period"] = period
    if supplier:
        base["$or"] = [{"supplier.carrier_id": supplier}, {"supplier.name": _rx(supplier)}]
    if q:
        base.setdefault("$and", []).append({"$or": [{"shipment_ref": _rx(q)}, {"supplier.name": _rx(q)}, {"lane.key": _rx(q)}]})
    query = dict(base)
    if status:
        query["status"] = {"$in": status.split(",")}
    items = await db.cost_accruals.find(query, {**NO_ID, "allocations": 0}).sort("created_at", -1).skip(skip).to_list(limit)
    total = await db.cost_accruals.count_documents(query)
    counts = {s: 0 for s in acc_svc.ACCRUAL_STATUSES}
    async for row in db.cost_accruals.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    totals = {"expected_eur_cents": 0, "actual_eur_cents": 0, "variance_eur_cents": 0, "open_eur_cents": 0}
    async for row in db.cost_accruals.aggregate([
            {"$match": query},
            {"$group": {"_id": None, "expected": {"$sum": {"$ifNull": ["$expected_eur_cents", 0]}},
                        "actual": {"$sum": {"$ifNull": ["$actual_eur_cents", 0]}},
                        "variance": {"$sum": {"$ifNull": ["$variance_eur_cents", 0]}},
                        "open": {"$sum": {"$cond": [{"$eq": ["$status", "open"]}, {"$ifNull": ["$expected_eur_cents", 0]}, 0]}}}}]):
        totals = {"expected_eur_cents": row["expected"], "actual_eur_cents": row["actual"],
                  "variance_eur_cents": row["variance"], "open_eur_cents": row["open"]}
    return {"items": items, "total": total, "counts": counts, "totals": totals}


@router.post("/accruals/rebuild")
async def rebuild_accruals(body: dict | None = Body(None), user: dict = Depends(WRITE)):
    body = body or {}
    if body.get("shipment_id"):
        try:
            rows = await acc_svc.accrue_shipment(str(body["shipment_id"]))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"processed": 1, "rows": len(rows), "items": rows}
    limit = int(body.get("limit") or 500)
    return await acc_svc.rebuild(limit=max(1, min(limit, 5000)))


# --------------------------------------------------------------------------------------------- carrier invoices
LIST_FIELDS = {**NO_ID, "text": 0, "extraction.fields": 0, "extraction.raw": 0, "history": 0}


@router.get("/carrier-invoices")
async def list_carrier_invoices(status: str | None = None, supplier: str | None = None, q: str | None = None,
                                source: str | None = None, limit: int = Query(200, ge=1, le=1000),
                                skip: int = Query(0, ge=0), user: dict = Depends(require_internal)):
    base = _hide_test({})
    if supplier:
        base["$or"] = [{"supplier.carrier_id": supplier}, {"supplier.name": _rx(supplier)}]
    if source:
        base["source"] = source
    if q:
        base.setdefault("$and", []).append({"$or": [{"number": _rx(q)}, {"supplier.name": _rx(q)}, {"filename": _rx(q)},
                                                    {"references.shipment_refs": q.strip().upper()},
                                                    {"matches.shipment_ref": _rx(q)}]})
    query = dict(base)
    if status:
        query["status"] = {"$in": status.split(",")}
    items = await db.carrier_invoices.find(query, LIST_FIELDS).sort("created_at", -1).skip(skip).to_list(limit)
    total = await db.carrier_invoices.count_documents(query)
    counts = {s: 0 for s in ci_svc.STATUSES}
    async for row in db.carrier_invoices.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    return {"items": items, "total": total, "counts": counts}


@router.post("/carrier-invoices")
async def upload_carrier_invoice(file: UploadFile = File(...), company_id: str | None = Form(None),
                                 user: dict = Depends(WRITE)):
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "The file is larger than the 20 MB limit.")
    try:
        doc = await ci_svc.ingest_file(data, file.filename or "invoice", file.content_type or "", source="upload",
                                       actor=user, company_id=company_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return doc


async def _one(ci_id: str) -> dict:
    doc = await db.carrier_invoices.find_one({"id": ci_id}, {**NO_ID, "text": 0})
    if not doc:
        raise HTTPException(404, "Carrier invoice not found")
    return doc


@router.get("/carrier-invoices/{ci_id}")
async def get_carrier_invoice(ci_id: str, user: dict = Depends(require_internal)):
    doc = await _one(ci_id)
    doc["accruals"] = await db.cost_accruals.find({"id": {"$in": [m["accrual_id"] for m in doc.get("matches") or []]}},
                                                  {**NO_ID, "allocations": 0}).to_list(50)
    doc["approvals"] = await db.approvals.raw.find({"ref_id": ci_id}, NO_ID).sort("created_at", -1).to_list(20)
    if doc.get("credit_note_ids"):
        doc["credit_notes"] = await db.carrier_invoices.find({"id": {"$in": doc["credit_note_ids"]}}, LIST_FIELDS).to_list(20)
    return doc


@router.get("/carrier-invoices/{ci_id}/file")
async def carrier_invoice_file(ci_id: str, user: dict = Depends(require_internal)):
    doc = await _one(ci_id)
    data, name, ctype = await ci_svc.get_file(doc["file_id"])
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doc.get("filename") or name or "invoice")
    return Response(content=data, media_type=doc.get("content_type") or ctype,
                    headers={"Content-Disposition": f'inline; filename="{safe}"', "Cache-Control": "private, no-store"})


async def _act(fn, ci_id: str, **kw):
    try:
        return await fn(ci_id, **kw)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/carrier-invoices/{ci_id}/approve")
async def approve_carrier_invoice(ci_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    return await _act(ci_svc.approve, ci_id, actor=user, note=(body or {}).get("note"))


@router.post("/carrier-invoices/{ci_id}/dispute")
async def dispute_carrier_invoice(ci_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    return await _act(ci_svc.dispute, ci_id, actor=user, note=(body or {}).get("note"))


@router.post("/carrier-invoices/{ci_id}/reject")
async def reject_carrier_invoice(ci_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    return await _act(ci_svc.reject, ci_id, actor=user, note=(body or {}).get("note"))


@router.post("/carrier-invoices/{ci_id}/rematch")
async def rematch_carrier_invoice(ci_id: str, body: dict | None = Body(None), user: dict = Depends(WRITE)):
    """Re-run extraction, audit and matching. Body {note, fields?}: `fields` are the invoice fields typed in by a person
    (supplier{name, vat_id, iban, bic}, number, invoice_date, due_date, currency, net_cents, vat_cents, total_cents,
    lines[{description, amount_cents, vat_rate_pct, vat_cents, shipment_ref}])."""
    fields = (body or {}).get("fields")
    if fields is not None and not isinstance(fields, dict):
        raise HTTPException(400, "fields must be an object with the invoice fields.")
    return await _act(ci_svc.rematch, ci_id, actor=user, fields=fields)


# --------------------------------------------------------------------------------------------- inbox
@router.post("/ap-mailbox/poll")
async def poll_mailbox(user: dict = Depends(WRITE)):
    if not ap_mailbox.configured():
        raise HTTPException(409, "The purchase-invoice mailbox is not configured: set AP_IMAP_HOST, AP_IMAP_USER and "
                                 "AP_IMAP_PASSWORD.")
    try:
        res = await ap_mailbox.poll(actor=user)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None
    return res


@router.get("/ap-mailbox/messages")
async def mailbox_messages(status: str | None = None, limit: int = Query(100, ge=1, le=500),
                           user: dict = Depends(require_internal)):
    q = {"status": status} if status else {}
    items = await db.ap_messages.find(q, NO_ID).sort("fetched_at", -1).to_list(limit)
    meta = await db.system.find_one({"key": "ap_mailbox_poll"}, NO_ID) or {}
    return {"items": items, "total": await db.ap_messages.count_documents(q), "configured": ap_mailbox.configured(),
            "last_poll": meta.get("at"), "require_auth": ap_mailbox.require_auth(),
            "postmark_configured": bool(ap_postmark.addresses() and os.environ.get("EMAIL_CHANNEL_POSTMARK_TOKEN"))}
