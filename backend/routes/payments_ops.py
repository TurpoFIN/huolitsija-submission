"""Payments operations API (s2): bank notifications (camt.054 upload and signed bank webhook), bank transactions,
reconciliation reports and a manual reconciliation run."""
import hashlib
import hmac

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from core import config
from core.db import db
from core.deps import require_internal, require_perm
from core.jobs import enqueue
from core.util import NO_ID, now_iso
from services import reconciliation

router = APIRouter(prefix="/api", tags=["payments-ops"])

MAX_STATEMENT_BYTES = 20 * 1024 * 1024
TX_STATUSES = ("new", "matched", "partial", "unmatched", "ignored")


def _statement_out(st: dict) -> dict:
    return {k: v for k, v in st.items() if k != "_id"}


@router.post("/autopilot/bank-statements")
async def upload_statement(file: UploadFile = File(...), user: dict = Depends(require_perm("billing.write"))):
    data = await file.read(MAX_STATEMENT_BYTES + 1)
    if len(data) > MAX_STATEMENT_BYTES:
        raise HTTPException(413, "The file is larger than 20 MB")
    try:
        st = await reconciliation.store_statement(data, file.filename or "", "upload", user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _statement_out(st)


@router.get("/autopilot/bank-statements")
async def list_statements(limit: int = Query(50, ge=1, le=200), user: dict = Depends(require_internal)):
    items = await db.raw.bank_statements.find({}, NO_ID).sort([("received_at", -1), ("_id", -1)]).to_list(limit)
    return {"items": items}


@router.get("/autopilot/bank-transactions")
async def list_transactions(status: str | None = None, statement_id: str | None = None,
                            limit: int = Query(100, ge=1, le=500), user: dict = Depends(require_internal)):
    q: dict = {}
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if any(s not in TX_STATUSES for s in wanted):
            raise HTTPException(400, f"status must be one of {', '.join(TX_STATUSES)}")
        q["status"] = {"$in": wanted}
    if statement_id:
        q["statement_id"] = statement_id
    items = await db.raw.bank_transactions.find(q, NO_ID).sort([("booking_date", -1), ("created_at", -1)]).to_list(limit)
    counts = {s: 0 for s in TX_STATUSES}
    async for r in db.raw.bank_transactions.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[r["_id"]] = r["n"]
    return {"items": items, "counts": counts}


@router.post("/webhooks/bank/camt054")
async def bank_webhook(request: Request):
    """Bank (or bank-connector) push of a camt.054 file. Header X-Huolitsija-Signature: hex HMAC-SHA256 of the raw
    body with BANK_WEBHOOK_SECRET."""
    secret = config.env("BANK_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "The bank webhook is not configured (BANK_WEBHOOK_SECRET is not set)")
    body = await request.body()
    given = (request.headers.get("x-huolitsija-signature") or "").strip().lower()
    if given.startswith("sha256="):
        given = given[7:]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "Missing or invalid X-Huolitsija-Signature")
    if len(body) > MAX_STATEMENT_BYTES:
        raise HTTPException(413, "The file is larger than 20 MB")
    name = (request.headers.get("x-filename") or "").strip()
    try:
        st = await reconciliation.store_statement(body, name, "webhook", None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "statement": {k: st.get(k) for k in ("id", "statement_id", "status", "entries_count", "duplicate",
                                                             "received_at", "job_id")}}


@router.get("/autopilot/reconciliation-reports")
async def reconciliation_reports(limit: int = Query(30, ge=1, le=200), user: dict = Depends(require_internal)):
    items = await db.raw.owner_reports.find({"kind": "reconciliation"}, {"_id": 0, "html": 0}).sort(
        [("created_at", -1), ("_id", -1)]).to_list(limit)
    return {"items": items}


@router.get("/autopilot/reconciliation-reports/{report_id}")
async def reconciliation_report(report_id: str, user: dict = Depends(require_internal)):
    rep = await db.raw.owner_reports.find_one({"id": report_id, "kind": "reconciliation"}, NO_ID)
    if not rep:
        raise HTTPException(404, "Reconciliation report not found")
    return rep


@router.post("/autopilot/reconciliation/run")
async def run_reconciliation_now(user: dict = Depends(require_perm("billing.write"))):
    from core.audit import audit

    jd = await enqueue("payments.reconcile_daily", {}, key=f"reconcile:manual:{now_iso()}", source="manual")
    await audit(user, "reconciliation_run_requested", "job", jd["id"], {})
    return {"job_id": jd["id"], "status": jd["status"], "kind": jd["kind"]}
