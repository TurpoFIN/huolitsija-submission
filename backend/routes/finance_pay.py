"""Finance (s13) accounts-payable payment routes: payables, payment runs (ISO 20022 pain.001), bank status reports
(pain.002) and bank debit notifications (camt.054). Reads need an internal user, writes need billing.write."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.events import emit
from core.storage import get_file
from core.tenant_visibility import hidden_test_companies
from core.util import NO_ID, now_iso
from services import payables as svc
from services.sepa import PaymentError

router = APIRouter(prefix="/api/finance", tags=["finance"])
WRITE = require_perm("billing.write")
MAX_XML_BYTES = 20 * 1024 * 1024


def _http(err: PaymentError) -> HTTPException:
    status = {"nothing_due": 409, "conflict": 409, "state": 409, "not_found": 404, "bank_api_upload": 502,
              "bank_api_response": 502, "bank_api_config": 503, "bank_api_cert": 503}.get(err.code, 400)
    return HTTPException(status, err.en)


def _hide_test(q: dict) -> dict:
    if hidden_test_companies.get() is not None:
        q["test"] = {"$ne": True}
    return q


async def _xml_from(request: Request) -> bytes:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "Attach the XML file in the form field 'file'.")
        data = await f.read()
    else:
        data = await request.body()
    if not data or not data.strip():
        raise HTTPException(400, "The request has no XML file.")
    if len(data) > MAX_XML_BYTES:
        raise HTTPException(413, "The file is larger than 20 MB.")
    return data


# ------------------------------------------------------------------------------------------------ payables
@router.get("/payables")
async def list_payables(status: str | None = None, currency: str | None = None, due_before: str | None = None,
                        due_within_days: int | None = None, payment_run_id: str | None = None, q: str | None = None,
                        limit: int = 500, user: dict = Depends(require_internal)):
    base = _hide_test({})
    query = dict(base)
    if status:
        query["status"] = {"$in": [s.strip() for s in status.split(",") if s.strip()]}
    if currency:
        query["currency"] = {"$in": [c.strip().upper() for c in currency.split(",") if c.strip()]}
    if due_within_days is not None:
        due_before = (date.today() + timedelta(days=int(due_within_days))).isoformat()
    if due_before:
        query["due_date"] = {"$lte": due_before[:10]}
    if payment_run_id:
        query["payment_run_id"] = payment_run_id
    if q:
        import re
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"supplier.name": rx}, {"invoice_number": rx}, {"end_to_end_id": rx}, {"reference": rx}]
    items = await db.payables.find(query, NO_ID).sort([("due_date", 1), ("created_at", 1)]).to_list(min(limit, 5000))
    counts: dict[str, int] = {}
    async for row in db.payables.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    totals: dict[str, int] = {}
    eur_total = 0
    async for row in db.payables.aggregate([{"$match": query}, {"$group": {"_id": "$currency", "amount": {"$sum": "$amount_cents"},
                                                                          "eur": {"$sum": "$eur_cents"}, "n": {"$sum": 1}}}]):
        totals[row["_id"]] = int(row["amount"])
        eur_total += int(row["eur"] or 0)
    return {"items": items, "total": await db.payables.count_documents(query), "counts": counts, "totals": totals,
            "eur_total_cents": eur_total, "mode": svc.payment_mode()}


@router.post("/payables/sync")
async def sync_payables(user: dict = Depends(WRITE)):
    out = await svc.sync()
    await audit(user, "payables_synced", "payable", None, {"created": out["created"]})
    return out


# ------------------------------------------------------------------------------------------------ payment runs
@router.post("/payment-runs", status_code=201)
async def create_run(body: dict | None = None, user: dict = Depends(WRITE)):
    body = body or {}
    ids = body.get("payable_ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(400, "payable_ids must be a list of payable ids.")
    try:
        return await svc.create_payment_run(body.get("execution_date"), user, payable_ids=ids)
    except PaymentError as err:
        raise _http(err) from None
    except ValueError as err:  # malformed date
        raise HTTPException(400, f"Execution date must be YYYY-MM-DD ({err}).") from None


@router.get("/payment-runs")
async def list_runs(status: str | None = None, limit: int = 200, user: dict = Depends(require_internal)):
    base = _hide_test({})
    query = dict(base)
    if status:
        query["status"] = {"$in": [s.strip() for s in status.split(",") if s.strip()]}
    items = await db.payment_runs.find(query, NO_ID).sort("created_at", -1).to_list(min(limit, 1000))
    counts: dict[str, int] = {}
    async for row in db.payment_runs.aggregate([{"$match": base}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    return {"items": items, "total": await db.payment_runs.count_documents(query), "counts": counts,
            "mode": svc.payment_mode()}


async def _run_or_404(run_id: str) -> dict:
    run = await svc.get_run(run_id)
    if not run:
        raise HTTPException(404, "Payment run not found.")
    return run


@router.get("/payment-runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require_internal)):
    run = await _run_or_404(run_id)
    run["payables"] = await db.payables.find({"payment_run_id": run_id}, NO_ID).sort("end_to_end_id", 1).to_list(5000)
    run["approval"] = await db.approvals.find_one({"id": run.get("approval_id")}, NO_ID) if run.get("approval_id") else None
    run["decision"] = run.get("decision") or (await db.agent_decisions.find_one({"id": run.get("decision_id")}, NO_ID)
                                              if run.get("decision_id") else None)
    run["status_reports"] = await db.payment_status_reports.find({"payment_run_id": run_id}, {"_id": 0, "transactions": 0}) \
        .sort("received_at", -1).to_list(50)
    run["file_url"] = f"/api/finance/payment-runs/{run_id}/file" if run.get("file_id") else None
    return run


@router.post("/payment-runs/{run_id}/approve")
async def approve(run_id: str, body: dict | None = None, user: dict = Depends(WRITE)):
    """Approve a run waiting on its owner card. Closes the card (same audit + approval.decided path as /ops/approvals)."""
    run = await _run_or_404(run_id)
    if run["status"] != "pending_approval":
        raise HTTPException(409, f"Payment run {run['number']} is {run['status']}, not waiting for approval.")
    note = (body or {}).get("note")
    a = await db.approvals.find_one({"kind": "payment_run", "ref_type": "payment_run", "ref_id": run_id,
                                     "status": "pending"}, NO_ID)
    if a:
        changes = {"status": "approved", "decided_at": now_iso(), "decided_by": {"id": user["id"], "name": user.get("name")},
                   "decision_note": note, "decision_data": {}}
        await db.approvals.update_one({"id": a["id"]}, {"$set": changes})
        a.update(changes)
        await audit(user, "approval_approved", "payment_run", run_id, {"approval_id": a["id"], "kind": "payment_run",
                                                                       "note": note})
        await emit("approval.decided", {"approval": a, "actor": user})  # handler approves; sending is a separate action
        return await svc.get_run(run_id)
    return await svc.approve_run(run_id, user, note=note)


@router.post("/payment-runs/{run_id}/send")
async def send(run_id: str, body: dict | None = None, user: dict = Depends(WRITE)):
    """api mode: upload to the bank now. file mode: confirm that the file was uploaded to the corporate web bank
    ({"uploaded": true}); the run and its payables become `sent`."""
    run = await _run_or_404(run_id)
    if run["status"] != "approved":
        raise HTTPException(409, f"Payment run {run['number']} is {run['status']}; only an approved run can be sent.")
    if svc.payment_mode() == "api":
        try:
            return await svc.send(run, user)
        except PaymentError as err:
            raise _http(err) from None
    if not (body or {}).get("uploaded"):
        raise HTTPException(409, "File mode: download the pain.001 file, upload it in the bank's corporate web bank, "
                                 "then confirm with {\"uploaded\": true}.")
    return await svc.mark_sent(run_id, user, via="file:web_bank")


@router.get("/payment-runs/{run_id}/file")
async def run_file(run_id: str, user: dict = Depends(require_internal)):
    run = await _run_or_404(run_id)
    if not run.get("file_id"):
        raise HTTPException(404, "This payment run has no file.")
    data, filename, _ = await get_file(run["file_id"])
    await audit(user, "payment_file_downloaded", "payment_run", run_id, {"number": run["number"]})
    return Response(content=data, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ------------------------------------------------------------------------------------------------ bank files
@router.post("/bank/pain002")
async def bank_pain002(request: Request, user: dict = Depends(WRITE)):
    """Payment status report from the bank (raw XML body or multipart field 'file')."""
    data = await _xml_from(request)
    try:
        return await svc.ingest_pain002(data, user)
    except PaymentError as err:
        raise _http(err) from None
    except Exception as err:  # noqa: BLE001 - malformed XML
        if type(err).__name__ == "XMLSyntaxError":
            raise HTTPException(400, f"The file is not well-formed XML: {err}") from None
        raise


@router.post("/bank/camt054-debits")
async def bank_camt054_debits(request: Request, user: dict = Depends(WRITE)):
    """camt.054 debit notification: DBIT entries settle our supplier payments (EndToEndId). Incoming CRDT entries are
    s2's reconciliation and are ignored here."""
    data = await _xml_from(request)
    try:
        return await svc.ingest_camt054_debits(data, user)
    except PaymentError as err:
        raise _http(err) from None
    except Exception as err:  # noqa: BLE001
        if type(err).__name__ == "XMLSyntaxError":
            raise HTTPException(400, f"The file is not well-formed XML: {err}") from None
        raise
