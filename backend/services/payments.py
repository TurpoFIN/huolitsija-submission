"""Card payments with Stripe Checkout (official `stripe` SDK, async via HTTPX).

Going live is a key swap: STRIPE_API_KEY sk_test_/rk_test_ -> sk_live_/rk_live_, STRIPE_WEBHOOK_SECRET from the
live endpoint. Payment is confirmed three independent ways, all idempotent (one payment per PaymentIntent):
  1. GET /api/payments/status/{session_id} or /api/invoices/{id}/payment-status after the Checkout redirect,
     which retrieves the session from Stripe (works without any public webhook URL);
  2. POST /api/webhooks/stripe with a verified signature (STRIPE_WEBHOOK_SECRET);
  3. the same webhook without a secret (local dev): the payload is never trusted, the Checkout Session is
     re-fetched from Stripe by id and only that answer is acted on.
"""
from __future__ import annotations

import json
import logging

import stripe

from core import config
from core.audit import audit
from core.db import db
from core.util import NO_ID, new_id, now_iso
from services import billing

log = logging.getLogger("payments")


class PaymentsUnavailable(Exception):
    pass


def stripe_enabled() -> bool:
    return bool(config.STRIPE_API_KEY)


def stripe_mode() -> str | None:
    key = config.STRIPE_API_KEY
    if not key:
        return None
    return "live" if key.startswith(("sk_live", "rk_live")) else "test"


def _client() -> stripe.StripeClient:
    if not stripe_enabled():
        raise PaymentsUnavailable("Card payments are not enabled yet. Pay by bank transfer with the reference number "
                                  "shown on the invoice.")
    return stripe.StripeClient(config.STRIPE_API_KEY, http_client=stripe.HTTPXClient(timeout=20), max_network_retries=2)


def _get(obj, key, default=None):
    try:
        v = obj[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if v is None else v


async def create_checkout(invoice: dict, *, user: dict, base_url: str, success_url: str | None = None,
                          cancel_url: str | None = None) -> dict:
    """Checkout Session for the amount due. Reuses a still-open session for the same amount.
    success_url/cancel_url override the portal return pages (the e-mail channel returns to a public receipt page)."""
    if invoice["kind"] != "invoice" or invoice["status"] not in ("open", "overdue"):
        raise billing.BillingError(f"A {invoice['status']} invoice cannot be paid", 409)
    if (invoice.get("currency") or "EUR") != "EUR":  # s13 multi-currency: Checkout charges EUR only; no wrong-currency charge
        raise billing.BillingError(f"Card payment is available for EUR invoices only; pay this {invoice['currency']} invoice by bank transfer", 409)
    amount = billing._amount_due(invoice)
    if amount <= 0:
        raise billing.BillingError("Nothing is due on this invoice", 409)
    if amount < 50:
        raise billing.BillingError("The amount due is below the card payment minimum (0,50 €); pay by bank transfer", 409)
    if not base_url:
        raise billing.BillingError("Cannot build the return address for the payment page", 500)
    client = _client()
    existing = await db.payment_transactions.find_one(
        {"invoice_id": invoice["id"], "status": "initiated", "amount_cents": amount, "livemode": stripe_mode() == "live"},
        NO_ID, sort=[("created_at", -1)])
    if existing and existing.get("url"):
        try:
            s = await client.v1.checkout.sessions.retrieve_async(existing["session_id"])
            if _get(s, "status") == "open":
                return {"url": _get(s, "url"), "session_id": existing["session_id"], "reused": True}
        except stripe.StripeError as exc:
            log.info("could not reuse session %s: %s", existing["session_id"], exc)
    locale = "fi" if (user.get("locale") or invoice.get("locale")) == "fi" else "en"
    tx_id = new_id()
    params = {
        "mode": "payment",
        "currency": "eur",
        # Invoices are owed in EUR: no Adaptive Pricing conversion into the payer's local currency.
        "adaptive_pricing": {"enabled": False},
        "line_items": [{"quantity": 1, "price_data": {
            "currency": "eur", "unit_amount": amount,
            "product_data": {"name": f"{'Lasku' if locale == 'fi' else 'Invoice'} {invoice['number']}",
                             "description": " · ".join(x for x in [invoice.get("company_name"), invoice.get("shipment_reference"),
                                                                    f"ref {invoice.get('reference')}"] if x)[:500]}}}],
        "metadata": {"invoice_id": invoice["id"], "company_id": invoice["company_id"], "invoice_number": invoice["number"],
                     "transaction_id": tx_id},
        "payment_intent_data": {"metadata": {"invoice_id": invoice["id"], "company_id": invoice["company_id"],
                                             "invoice_number": invoice["number"], "transaction_id": tx_id},
                                "description": f"Invoice {invoice['number']}"},
        "client_reference_id": invoice["id"],
        "success_url": success_url or f"{base_url}/portal/invoices/{invoice['id']}?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": cancel_url or f"{base_url}/portal/invoices/{invoice['id']}?cancelled=1",
        "locale": locale,
    }
    email = (invoice.get("buyer") or {}).get("billing_email") or user.get("email")
    if email:
        params["customer_email"] = email
    try:
        s = await client.v1.checkout.sessions.create_async(params, {"idempotency_key": f"inv-{invoice['id']}-{tx_id}"})
    except stripe.StripeError as exc:
        log.warning("checkout create failed for %s: %s", invoice["number"], exc)
        raise billing.BillingError(f"The card payment page could not be opened: {getattr(exc, 'user_message', None) or exc}", 502)
    tx = {"id": tx_id, "provider": "stripe", "invoice_id": invoice["id"], "company_id": invoice["company_id"],
          "session_id": _get(s, "id"), "url": _get(s, "url"), "amount_cents": amount, "currency": "EUR",
          "status": "initiated", "payment_status": _get(s, "payment_status"), "livemode": bool(_get(s, "livemode", False)),
          "created_at": now_iso(), "updated_at": now_iso(), "created_by": {"id": user.get("id"), "name": user.get("name")},
          "expires_at": _get(s, "expires_at")}
    await db.payment_transactions.insert_one(dict(tx))
    await audit(user, "checkout_started", "invoice", invoice["id"], {"session_id": tx["session_id"], "amount_cents": amount},
                company_id=invoice["company_id"])
    return {"url": tx["url"], "session_id": tx["session_id"], "reused": False}


async def _apply_session(s, *, source: str) -> dict:
    """Act on a Checkout Session object that came from Stripe (retrieved or signature-verified)."""
    session_id = _get(s, "id")
    meta = _get(s, "metadata", {}) or {}
    invoice_id = meta.get("invoice_id") if hasattr(meta, "get") else _get(meta, "invoice_id")
    tx = await db.payment_transactions.find_one({"session_id": session_id}, NO_ID)
    if not invoice_id and tx:
        invoice_id = tx["invoice_id"]
    if not invoice_id:
        return {"ok": False, "reason": "session is not linked to an invoice"}
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv:
        return {"ok": False, "reason": "invoice not found"}
    status, pay_status = _get(s, "status"), _get(s, "payment_status")
    amount_total, currency = _get(s, "amount_total"), (_get(s, "currency") or "").lower()
    pi = _get(s, "payment_intent")
    pi_id = pi if isinstance(pi, str) else _get(pi, "id")
    upd = {"status": "initiated", "payment_status": pay_status, "session_status": status, "updated_at": now_iso(),
           "last_source": source, "payment_intent": pi_id}
    applied = False
    if pay_status in ("paid", "no_payment_required") and status == "complete":
        expected = (tx or {}).get("amount_cents")
        if currency != "eur" or (expected is not None and amount_total != expected):
            upd["status"] = "review"
            upd["review_reason"] = f"amount {amount_total} {currency} differs from expected {expected} eur"
            from core.notify import bi, eur, notify_role

            await notify_role("finance", bi(f"Maksu tarkistettava: {inv.get('number')}", f"Payment needs review: {inv.get('number')}"),
                              bi(f"Maksettu {eur(amount_total, 'fi')} ({currency.upper()}) poikkeaa odotetusta "
                                 f"{eur(expected, 'fi') if expected is not None else '—'}", upd["review_reason"]),
                              link=f"/ops/billing/{invoice_id}")
        else:
            upd["status"] = "paid"
            _, applied = await billing.record_payment(
                invoice_id, amount_cents=int(amount_total), method="card", ref=pi_id or session_id,
                details={"provider": "stripe", "session_id": session_id, "payment_intent": pi_id,
                         "livemode": bool(_get(s, "livemode", False))})
    elif status == "complete" and pay_status == "unpaid":
        upd["status"] = "processing"
        await billing.mark_processing(invoice_id, "Payment submitted, waiting for the bank to confirm")
    elif status == "expired":
        upd["status"] = "expired"
    await db.payment_transactions.update_one({"session_id": session_id}, {"$set": upd})
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    return {"ok": True, "session_id": session_id, "transaction_status": upd["status"], "payment_status": pay_status,
            "invoice_id": invoice_id, "invoice_status": inv["status"], "applied_now": applied,
            "amount_total": amount_total, "currency": currency}


async def sync_session(session_id: str) -> dict:
    client = _client()
    try:
        s = await client.v1.checkout.sessions.retrieve_async(session_id)
    except stripe.InvalidRequestError:
        raise billing.BillingError("Payment session not found", 404)
    except stripe.StripeError as exc:
        raise billing.BillingError(f"Stripe could not be reached: {exc}", 502)
    return await _apply_session(s, source="poll")


async def sync_invoice(invoice_id: str) -> dict:
    """Latest session of an invoice, confirmed against Stripe."""
    tx = await db.payment_transactions.find_one({"invoice_id": invoice_id}, NO_ID, sort=[("created_at", -1)])
    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not tx:
        return {"ok": True, "invoice_id": invoice_id, "invoice_status": inv["status"] if inv else None,
                "transaction_status": None}
    if tx["status"] in ("paid", "expired"):
        return {"ok": True, "invoice_id": invoice_id, "invoice_status": inv["status"], "transaction_status": tx["status"],
                "session_id": tx["session_id"]}
    return await sync_session(tx["session_id"])


# --------------------------------------------------------------------------------------------- webhook
# The HTTP route (routes/invoices.py -> services.reconciliation.receive_stripe_webhook) runs verify_webhook,
# store_event and triage_event, then hands the event to the durable job `payments.stripe_event`, which calls
# process_event. handle_webhook below runs the same pieces inline (kept for callers and tests that want the result).

class WebhookRejected(Exception):
    pass


HANDLED_TYPES = ("payment_intent.succeeded", "charge.refunded")


def handles(etype: str) -> bool:
    return (etype or "").startswith("checkout.session.") or etype in HANDLED_TYPES


async def verify_webhook(payload: bytes, signature: str | None) -> dict:
    """Signature check. Without a secret (local dev) the object is re-fetched from Stripe, so the stored copy is
    Stripe's answer, never the posted body. Returns {event, verified, confirmed, text}; raises WebhookRejected."""
    secret = config.STRIPE_WEBHOOK_SECRET
    if secret:
        try:
            stripe.Webhook.construct_event(payload, signature or "", secret)
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise WebhookRejected(f"Invalid Stripe signature: {exc}")
        text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
        return {"event": json.loads(text), "verified": True, "confirmed": True, "text": text}
    try:
        event = json.loads(payload or b"{}")
    except ValueError:
        raise WebhookRejected("Body is not JSON")
    if not isinstance(event, dict):
        raise WebhookRejected("Body is not a Stripe event")
    if not stripe_enabled():
        raise WebhookRejected("Unsigned event cannot be verified without Stripe configuration")
    etype = _get(event, "type") or ""
    confirmed = False
    if handles(etype):
        obj = _get(_get(event, "data", {}), "object", {}) or {}
        event = {**event, "data": {**(event.get("data") or {}), "object": await _confirm_object(etype, obj)}}
        confirmed = True
    return {"event": event, "verified": False, "confirmed": confirmed, "text": json.dumps(event)}


async def _confirm_object(etype: str, obj) -> dict:
    """The object as Stripe reports it now (for unsigned events)."""
    client = _client()
    oid = _get(obj, "id")
    try:
        if etype.startswith("checkout.session."):
            if not oid:
                raise WebhookRejected("Event has no session id")
            return (await client.v1.checkout.sessions.retrieve_async(oid)).to_dict()
        if etype == "payment_intent.succeeded":
            return (await client.v1.payment_intents.retrieve_async(oid)).to_dict()
        if etype == "charge.refunded":
            return (await client.v1.charges.retrieve_async(oid)).to_dict()
    except stripe.StripeError as exc:
        what = {"checkout": "Session", "payment_intent": "PaymentIntent", "charge": "Charge"}[etype.split(".")[0]]
        raise WebhookRejected(f"{what} {oid} could not be confirmed with Stripe: {exc}")
    return obj


async def store_event(event: dict, text: str, *, verified: bool, confirmed: bool) -> dict:
    """Write the event to stripe_events once (unique id). Returns {id, new, status, verified} where status is the
    stored status before this call: received | processed | failed (rows from before the job existed count as
    processed)."""
    event_id = _get(event, "id")
    if not event_id:
        return {"id": None, "new": True, "status": None, "verified": verified}
    from pymongo.errors import DuplicateKeyError

    etype = _get(event, "type") or ""
    try:
        res = await db.stripe_events.update_one({"id": event_id}, {"$setOnInsert": {
            "id": event_id, "type": etype, "verified": verified, "confirmed": confirmed, "received_at": now_iso(),
            "payload": text, "status": "received", "processed_at": None, "error": None, "attempts": 0,
            "livemode": bool(_get(event, "livemode", False))}}, upsert=True)
        if res.upserted_id is not None:
            return {"id": event_id, "new": True, "status": "received", "verified": verified}
    except DuplicateKeyError:
        pass
    cur = await db.stripe_events.find_one({"id": event_id}, NO_ID) or {}
    status = cur.get("status") or "processed"
    if (verified and not cur.get("verified")) or (not verified and confirmed and not cur.get("verified")):
        # A signed copy replaces an unsigned one; an unsigned repeat carries Stripe's fresh answer and is processed
        # again (as before: unsigned repeats were never answered `duplicate`).
        status = "received" if status == "processed" else status
        await db.stripe_events.update_one({"id": event_id}, {"$set": {
            "verified": verified, "confirmed": confirmed, "payload": text, "type": etype, "status": status}})
    return {"id": event_id, "new": False, "status": status, "verified": bool(cur.get("verified"))}


async def mark_event(event_id: str | None, status: str, *, result: dict | None = None, error: str | None = None):
    if not event_id:
        return
    sets = {"status": status, "error": error, "result": result}
    if status == "processed":
        sets["processed_at"] = now_iso()
    await db.stripe_events.update_one({"id": event_id}, {"$set": sets, "$inc": {"attempts": 1}})


async def triage_event(event: dict) -> dict | None:
    """The answer for an event that needs no work (not ours, or a type we do not handle); None when it does."""
    etype = _get(event, "type") or ""
    if not handles(etype):
        return {"ok": True, "ignored": etype}
    obj = _get(_get(event, "data", {}), "object", {}) or {}
    if etype.startswith("checkout.session."):
        meta = _get(obj, "metadata", {}) or {}
        if meta.get("invoice_id"):
            return None
        if _get(obj, "id") and await db.payment_transactions.find_one({"session_id": _get(obj, "id")}, {"_id": 1}):
            return None
        if etype == "checkout.session.async_payment_failed":
            return {"ok": True, "handled": etype}
        return {"ok": False, "reason": "session is not linked to an invoice"}
    if etype == "payment_intent.succeeded":
        meta = _get(obj, "metadata", {}) or {}
        if not meta.get("invoice_id") or _get(obj, "status") != "succeeded" or (_get(obj, "currency") or "") != "eur":
            return {"ok": True, "ignored": "not an invoice payment"}
    return None


async def process_event(event: dict, *, verified: bool, confirmed: bool = False) -> dict:
    """Act on a stored event. `confirmed` = the object is Stripe's own (signed, or re-fetched by verify_webhook)."""
    etype = _get(event, "type") or ""
    obj = _get(_get(event, "data", {}), "object", {}) or {}
    trusted = verified or confirmed
    if not handles(etype):
        return {"ok": True, "ignored": etype}
    if not trusted:
        if not stripe_enabled():
            raise WebhookRejected("Unsigned event cannot be verified without Stripe configuration")
        obj = await _confirm_object(etype, obj)

    if etype.startswith("checkout.session."):
        sid = _get(obj, "id")
        if not sid:
            raise WebhookRejected("Event has no session id")
        s = obj
        if etype == "checkout.session.async_payment_failed":
            meta = _get(s, "metadata", {}) or {}
            if meta.get("invoice_id"):
                await billing.reopen_after_failed_payment(meta["invoice_id"], "The bank declined the payment")
            await db.payment_transactions.update_one({"session_id": sid}, {"$set": {"status": "failed", "updated_at": now_iso()}})
            return {"ok": True, "handled": etype}
        result = await _apply_session(s, source=f"webhook:{etype}")
        return {"handled": etype, **result}
    if etype == "payment_intent.succeeded":
        pi = obj
        meta = _get(pi, "metadata", {}) or {}
        inv_id = meta.get("invoice_id")
        if not inv_id or _get(pi, "status") != "succeeded" or (_get(pi, "currency") or "") != "eur":
            return {"ok": True, "ignored": "not an invoice payment"}
        inv, applied = await billing.record_payment(
            inv_id, amount_cents=int(_get(pi, "amount_received") or _get(pi, "amount")), method="card", ref=_get(pi, "id"),
            details={"provider": "stripe", "payment_intent": _get(pi, "id"), "livemode": bool(_get(pi, "livemode", False))})
        await db.payment_transactions.update_one({"payment_intent": _get(pi, "id")}, {"$set": {"status": "paid", "updated_at": now_iso()}})
        return {"ok": True, "handled": etype, "invoice_status": inv["status"], "applied_now": applied}
    # charge.refunded
    ch = obj
    pi_id = _get(ch, "payment_intent")
    pi_id = pi_id if isinstance(pi_id, str) else _get(pi_id, "id")
    meta = _get(ch, "metadata", {}) or {}
    inv_id = meta.get("invoice_id")
    if not inv_id and pi_id:
        tx = await db.payment_transactions.find_one({"payment_intent": pi_id}, NO_ID)
        inv_id = (tx or {}).get("invoice_id")
    if not inv_id:
        return {"ok": True, "ignored": "refund not linked to an invoice"}
    inv = await db.invoices.find_one({"id": inv_id}, {"_id": 0, "refunds": 1}) or {}
    charge_id = _get(ch, "id")
    before = sum(r.get("amount_cents", 0) for r in inv.get("refunds") or [] if r.get("charge_id") == charge_id)
    total_refunded = int(_get(ch, "amount_refunded") or 0)
    if total_refunded > before:
        await billing.record_refund(inv_id, amount_cents=total_refunded - before, ref=f"{charge_id}:{total_refunded}",
                                    charge_id=charge_id)
    return {"ok": True, "handled": etype}


async def handle_webhook(payload: bytes, signature: str | None) -> dict:
    """Verify, store and process inline. The HTTP route stores and queues instead (services.reconciliation)."""
    v = await verify_webhook(payload, signature)
    event = v["event"]
    stored = await store_event(event, v["text"], verified=v["verified"], confirmed=v["confirmed"])
    if not stored["new"] and stored["status"] == "processed" and v["verified"]:
        return {"ok": True, "duplicate": True}
    quick = await triage_event(event)
    if quick is not None:
        await mark_event(stored["id"], "processed", result=quick)
        return quick
    try:
        result = await process_event(event, verified=v["verified"], confirmed=v["confirmed"])
    except Exception as exc:  # noqa: BLE001 - the event stays retryable
        await mark_event(stored["id"], "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    await mark_event(stored["id"], "processed", result=result)
    return result
