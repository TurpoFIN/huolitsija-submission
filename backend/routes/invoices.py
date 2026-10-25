"""Invoices, payments (Stripe Checkout + bank transfer) and the Stripe webhook."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictInt

from core.audit import audit
from core.db import db
from core.deps import current_user, is_internal, require_internal, require_perm
from core.util import NO_ID
from core.web import base_url
from services import billing, payments
from services import seed_billing  # noqa: F401  (registers the V4 demo seed, startup order 50)
from services.invoice_pdf import virtual_barcode

router = APIRouter(prefix="/api", tags=["invoices"])


class InvoiceDisputeIn(BaseModel):
    reason: str = Field(min_length=10, max_length=4000)
    amount_cents: StrictInt = Field(gt=0)


class DisputeMessageIn(BaseModel):
    text: str = Field(min_length=10, max_length=4000)


class DisputeResolutionIn(BaseModel):
    outcome: str
    note: str = Field(min_length=10, max_length=4000)
    credit_note_id: str | None = None


@router.post("/invoices/{invoice_id}/dispute")
async def file_dispute(invoice_id: str, body: InvoiceDisputeIn, user: dict = Depends(current_user)):
    from services import invoice_disputes
    return await invoice_disputes.submit(invoice_id, user, **body.model_dump())


@router.post("/invoices/{invoice_id}/dispute/{dispute_id}/messages")
async def dispute_message(invoice_id: str, dispute_id: str, body: DisputeMessageIn,
                          user: dict = Depends(current_user)):
    from services import invoice_disputes
    return await invoice_disputes.respond(invoice_id, dispute_id, user, **body.model_dump())


@router.post("/invoices/{invoice_id}/dispute/{dispute_id}/resolve")
async def resolve_dispute(invoice_id: str, dispute_id: str, body: DisputeResolutionIn,
                          user: dict = Depends(current_user)):
    from services import invoice_disputes
    return await invoice_disputes.resolve(invoice_id, dispute_id, user, **body.model_dump())

LIST_FIELDS = {"_id": 0, "lines": 0, "seller": 0, "events": 0, "emails": 0}


def _err(exc: Exception):
    if isinstance(exc, payments.PaymentsUnavailable):
        raise HTTPException(503, str(exc))
    if isinstance(exc, billing.BillingError):
        raise HTTPException(exc.status, str(exc))
    raise exc


async def _remember(request: Request):
    await billing.remember_base_url(base_url(request))


async def _load(invoice_id: str, user: dict) -> dict:
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if not is_internal(user):
        if (user.get("org_type") != "customer" or inv["company_id"] != user.get("company_id") or inv["status"] == "draft"
                or not inv.get("number")):  # a draft voided before issue was never sent to the customer
            raise HTTPException(404, "Invoice not found")
    return inv


def _scope(user: dict) -> dict:
    if is_internal(user):
        return {}
    if user.get("org_type") == "customer":
        return {"company_id": user.get("company_id"), "status": {"$ne": "draft"}, "number": {"$ne": None}}
    return {"company_id": "__none__"}


@router.get("/invoices")
async def list_invoices(request: Request, status: str | None = None, company_id: str | None = None,
                        shipment_id: str | None = None, kind: str | None = None, q: str | None = None,
                        limit: int = Query(200, le=1000), user: dict = Depends(current_user)):
    await _remember(request)
    base = _scope(user)
    if company_id and is_internal(user):
        base["company_id"] = company_id
    if shipment_id:
        base["shipment_id"] = shipment_id
    query = dict(base)
    if status and status != "all":
        statuses = [s for s in status.split(",") if s in billing.STATUSES]
        query["status"] = {"$in": statuses} if "status" not in base else {"$in": [s for s in statuses if s != "draft" or is_internal(user)]}
    if kind:
        query["kind"] = kind
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"number": rx}, {"company_name": rx}, {"shipment_reference": rx}, {"reference": rx},
                        {"customer_reference": rx}]
    items = await db.invoices.find(query, LIST_FIELDS).sort([("issue_date", -1), ("created_at", -1)]).to_list(limit)
    summary = await billing.list_counts(base)
    if not is_internal(user):  # customers never see drafts, not even as a sum
        summary["counts"].pop("draft", None)
        summary["totals"].pop("draft_cents", None)
    return {"items": [billing.public_invoice(i, internal=is_internal(user)) for i in items],
            "total": await db.invoices.count_documents(query), **summary}


def _payment_options(inv: dict) -> dict:
    from core import config

    due = billing._amount_due(inv)
    eur = (inv.get("currency") or "EUR") == "EUR"  # s13 multi-currency: barcode and card checkout are EUR-only
    return {"card_enabled": payments.stripe_enabled() and eur, "card_mode": payments.stripe_mode(),
            "bank_transfer": {"payee": (inv.get("seller") or {}).get("name"), "iban": (inv.get("seller") or {}).get("iban"), "bic": (inv.get("seller") or {}).get("bic"),
                              "reference": inv.get("reference"), "rf_reference": inv.get("rf_reference"),
                              "amount_cents": due, "due_date": inv.get("due_date"),
                              "virtual_barcode": virtual_barcode((inv.get("seller") or {}).get("iban"), due, inv.get("reference"), inv.get("due_date"))
                              if eur and inv.get("kind") == "invoice" and inv.get("status") in billing.OPEN_STATES else None}}


@router.get("/invoices/vat-codes")
async def vat_codes(user: dict = Depends(require_internal)):
    from core.settings import get_settings

    settings = await get_settings()
    today = billing.today_fi()
    out = []
    for code, spec in billing.VAT_CODES.items():
        if spec.get("alias_of"):
            continue  # legacy code name kept for quotes; the picker offers the canonical one
        rate = billing.vat_rate(code, today, settings)
        out.append({"code": code, "kind": spec["kind"], "rate_pct": rate, "label_fi": billing.vat_label(code, rate, "fi"),
                    "label_en": billing.vat_label(code, rate, "en"), "legal_basis": billing.legal_basis(code),
                    "history": [{"valid_from": vf, "rate_pct": r if r is not None else settings.get("vat_rate_standard_pct")}
                                for vf, r in spec["rates"]]})
    return {"items": out}


@router.get("/billing/customers")
async def billing_customers(q: str | None = None, user: dict = Depends(require_internal)):
    """Customer picker for manual invoices: billing facts only."""
    query: dict = {"status": {"$ne": "deleted"}}
    if q:
        query["name"] = {"$regex": re.escape(q.strip()), "$options": "i"}
    items = await db.companies.find(query, {"_id": 0, "id": 1, "name": 1, "business_id": 1, "vat_id": 1, "country": 1,
                                            "payment_terms_days": 1, "billing_email": 1, "credit_limit_cents": 1,
                                            "kyc_status": 1}).sort("name", 1).to_list(500)
    return {"items": items, "total": len(items)}


@router.get("/billing/uninvoiced")
async def uninvoiced(company_id: str | None = None, user: dict = Depends(require_internal)):
    """Delivered shipments with no invoice yet: the 'ready to invoice' queue."""
    q: dict = {"status": "delivered"}
    if company_id:
        q["company_id"] = company_id
    out = []
    async for s in db.shipments.find(q, {"_id": 0, "id": 1, "reference": 1, "company_id": 1, "customer_name": 1,
                                         "delivered_at": 1, "mode": 1, "origin": 1, "destination": 1, "price": 1}).sort("delivered_at", -1):
        if not await db.invoices.find_one({"shipment_id": s["id"], "kind": "invoice", "status": {"$ne": "void"}}, {"_id": 1}):
            out.append({**s, "origin": (s.get("origin") or {}).get("name"), "destination": (s.get("destination") or {}).get("name")})
    return {"items": out, "total": len(out)}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, request: Request, user: dict = Depends(current_user)):
    await _remember(request)
    inv = await _load(invoice_id, user)
    out = billing.public_invoice(inv, internal=is_internal(user))
    out["payment_options"] = _payment_options(inv)
    related_ids = list(inv.get("credit_note_ids") or []) + ([inv["credit_note_for"]] if inv.get("credit_note_for") else [])
    out["related"] = await db.invoices.find({"id": {"$in": related_ids}, "status": {"$ne": "draft"}},
                                            {"_id": 0, "id": 1, "number": 1, "kind": 1, "status": 1, "total_cents": 1,
                                             "issue_date": 1}).to_list(50)
    if is_internal(user):
        out["transactions"] = await db.payment_transactions.find({"invoice_id": invoice_id}, {"_id": 0, "url": 0}) \
            .sort("created_at", -1).to_list(20)
        out["audit"] = await db.audit_log.find({"entity": "invoice", "entity_id": invoice_id}, NO_ID).sort("at", -1).to_list(50)
        for a in out["audit"]:
            if (a.get("data") or {}).get("warnings"):
                a["data"]["warnings_i18n"] = [billing.text_i18n(w) for w in a["data"]["warnings"]]
    return out


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: str, user: dict = Depends(current_user)):
    from core.storage import get_file
    from services.invoice_pdf import render_invoice_pdf

    inv = await _load(invoice_id, user)
    name = f"{inv.get('number') or 'draft-' + invoice_id[:8]}.pdf"
    if inv.get("pdf_file_id") and inv["status"] != "draft":
        try:
            data, _, ctype = await get_file(inv["pdf_file_id"])
            return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})
        except Exception:  # noqa: BLE001 - stored copy missing: render again from the record
            pass
    data = render_invoice_pdf(inv, original_number=inv.get("credit_note_for_number"))
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


class LineIn(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    qty: float = 1
    unit: str | None = None
    unit_price_cents: int
    vat_code: str | None = None
    is_pass_through: bool = False
    code: str | None = None
    source: str | None = None
    source_ref: str | None = None


class InvoiceIn(BaseModel):
    company_id: str
    shipment_id: str | None = None
    lines: list[LineIn] = []
    notes: str | None = None
    customer_reference: str | None = None
    payment_terms_days: int | None = None
    supply_date: str | None = None
    issue: bool = False
    send: bool = True


class InvoicePatch(BaseModel):
    lines: list[LineIn] | None = None
    notes: str | None = None
    internal_note: str | None = None
    customer_reference: str | None = None
    payment_terms_days: int | None = None
    supply_date: str | None = None


@router.post("/invoices")
async def create_invoice(body: InvoiceIn, request: Request, user: dict = Depends(require_perm("billing.write"))):
    await _remember(request)
    try:
        inv = await billing.create_manual_invoice({**body.model_dump(), "lines": [ln.model_dump() for ln in body.lines]}, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(inv, internal=True)


@router.post("/invoices/from-shipment/{shipment_id}")
async def invoice_from_shipment(shipment_id: str, request: Request, user: dict = Depends(require_perm("billing.write"))):
    await _remember(request)
    try:
        inv = await billing.create_invoice_for_shipment(shipment_id, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(inv, internal=True)


@router.patch("/invoices/{invoice_id}")
async def patch_invoice(invoice_id: str, body: InvoicePatch, user: dict = Depends(require_perm("billing.write"))):
    data = body.model_dump(exclude_unset=True)
    if data.get("lines") is not None:
        data["lines"] = [ln if isinstance(ln, dict) else ln.model_dump() for ln in data["lines"]]
    try:
        inv = await billing.update_draft(invoice_id, data, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(inv, internal=True)


class IssueIn(BaseModel):
    send: bool = True


@router.post("/invoices/{invoice_id}/issue")
async def issue(invoice_id: str, request: Request, body: IssueIn | None = None,
                user: dict = Depends(require_perm("billing.write"))):
    await _remember(request)
    try:
        inv = await billing.issue_invoice(invoice_id, user, send=(body.send if body else True))
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(inv, internal=True)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/invoices/{invoice_id}/void")
async def void(invoice_id: str, body: ReasonIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        inv = await billing.void_invoice(invoice_id, body.reason, user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(inv, internal=True)


class CreditIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    line_ids: list[str] | None = None


@router.post("/invoices/{invoice_id}/credit")
async def credit(invoice_id: str, body: CreditIn, user: dict = Depends(require_perm("billing.write"))):
    try:
        cn = await billing.credit_invoice(invoice_id, reason=body.reason, line_ids=body.line_ids, actor=user)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    return billing.public_invoice(cn, internal=True)


class PaidIn(BaseModel):
    paid_at: str = Field(description="Value date of the bank transfer, YYYY-MM-DD")
    reference: str = Field(min_length=1, max_length=120, description="Bank archive id / reference of the transfer")
    amount_cents: int | None = None
    note: str | None = None


@router.post("/invoices/{invoice_id}/mark-paid")
async def mark_paid(invoice_id: str, body: PaidIn, user: dict = Depends(require_perm("billing.write"))):
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if any(p.get("key") == f"bank_transfer:{body.reference.strip()}" for p in inv.get("payments") or []):
        raise HTTPException(409, "This bank reference is already recorded on the invoice")
    if inv["kind"] == "invoice" and inv["status"] == "paid":
        raise HTTPException(409, "The invoice is already paid")
    paid_on = billing.to_date(body.paid_at)
    if not paid_on:
        raise HTTPException(400, "Give the payment date as YYYY-MM-DD")
    if paid_on > billing.today_fi():
        raise HTTPException(400, "The payment date cannot be in the future")
    due = billing._amount_due(inv) if inv["kind"] == "invoice" else int(inv.get("refund_due_cents") or 0) - int(inv.get("paid_cents") or 0)
    amount = body.amount_cents if body.amount_cents is not None else due
    if amount > due:
        raise HTTPException(400, f"The amount is more than the {due / 100:.2f} € still due. Record the amount due and "
                                 f"refund the difference, or check the bank line.")
    try:
        inv, applied = await billing.record_payment(invoice_id, amount_cents=amount, method="bank_transfer",
                                                    ref=body.reference.strip(), paid_at=paid_on.isoformat(), actor=user,
                                                    details={"note": body.note} if body.note else None)
    except Exception as exc:  # noqa: BLE001
        _err(exc)
    if not applied:
        raise HTTPException(409, "This bank reference is already recorded on the invoice")
    return billing.public_invoice(inv, internal=True)


@router.post("/invoices/{invoice_id}/send")
async def resend(invoice_id: str, request: Request, user: dict = Depends(require_perm("billing.write"))):
    await _remember(request)
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv or inv["status"] == "draft":
        raise HTTPException(409, "Issue the invoice before sending it")
    kind = "reminder" if inv["status"] == "overdue" else "issued"
    sent = await billing.send_invoice_email(inv, kind=kind)
    await audit(user, "invoice_sent", "invoice", invoice_id, {"kind": kind, "to": [s["to"] for s in sent]},
                company_id=inv["company_id"])
    return {"sent": [{"to": s["to"], "status": s["status"]} for s in sent], "kind": kind}


@router.post("/invoices/run-overdue")
async def run_overdue(user: dict = Depends(require_perm("billing.write"))):
    return {"marked_overdue": await billing.mark_overdue()}


# --------------------------------------------------------------------------------------------- payments

@router.get("/payments/config")
async def payments_config(user: dict = Depends(current_user)):
    return {"card_enabled": payments.stripe_enabled(), "mode": payments.stripe_mode()}


@router.post("/invoices/{invoice_id}/checkout")
async def checkout(invoice_id: str, request: Request, user: dict = Depends(current_user)):
    inv = await _load(invoice_id, user)
    if not is_internal(user) and "company.pay" not in (user.get("permissions") or []):
        raise HTTPException(403, "Your role cannot pay invoices; ask your company admin")
    base = base_url(request)
    await billing.remember_base_url(base)
    try:
        return await payments.create_checkout(inv, user=user, base_url=base)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@router.get("/invoices/{invoice_id}/payment-status")
async def invoice_payment_status(invoice_id: str, user: dict = Depends(current_user)):
    await _load(invoice_id, user)
    try:
        return await payments.sync_invoice(invoice_id)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@router.get("/payments/status/{session_id}")
async def payment_status(session_id: str, user: dict = Depends(current_user)):
    tx = await db.payment_transactions.find_one({"session_id": session_id}, NO_ID)
    if not tx or (not is_internal(user) and tx["company_id"] != user.get("company_id")):
        raise HTTPException(404, "Payment session not found")
    try:
        return await payments.sync_session(session_id)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Verified, stored in stripe_events and queued (job payments.stripe_event); answers at once."""
    from services import reconciliation

    payload = await request.body()
    try:
        return await reconciliation.receive_stripe_webhook(payload, request.headers.get("stripe-signature"))
    except payments.WebhookRejected as exc:
        raise HTTPException(400, str(exc))
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc))
