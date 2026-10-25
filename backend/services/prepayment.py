"""s2: a prepayment-only customer (credit limit 0, set by a person) books by paying first.

routes.quotes.book: when the only booking blocker is services.credit.check_booking's "prepayment only", the booking
request is kept on the quote (quote.prepayment) and a prepayment invoice for the chosen option is issued at once,
due on issue, to the billing address and to the person who booked (the e-mail channel adds the Stripe link).
invoice.paid -> job prepayment.book: the kept booking is made as that person, without the prepayment blocker, and the
paid invoice is linked to the new shipment (billing.create_invoice_for_shipment returns it instead of a second one)."""
import logging

from fastapi import HTTPException

from core import jobs
from core.audit import audit
from core.db import db
from core.events import on
from core.util import NO_ID, now_iso

log = logging.getLogger("prepayment")


def only_prepayment(blockers: list[dict]) -> bool:
    return bool(blockers) and all(b.get("prepayment") for b in blockers)


def summary(inv: dict) -> dict:
    return {"invoice_id": inv["id"], "invoice_number": inv.get("number"), "status": inv.get("status"),
            "total_cents": inv.get("total_cents"), "due_date": inv.get("due_date"),
            "pay_link": f"/portal/invoices/{inv['id']}"}


async def request(q: dict, opt: dict, booking: dict, user: dict, company: dict) -> dict:
    """Issue (or return the open) prepayment invoice for this quote option and keep the booking request."""
    from services import billing

    pp = q.get("prepayment") or {}
    inv = await db.invoices.find_one({"id": pp["invoice_id"]}, NO_ID) if pp.get("invoice_id") else None
    if inv and inv.get("status") != "void":
        if pp.get("option_id") != opt["id"]:
            raise HTTPException(409, f"Prepayment invoice {inv.get('number')} is open for another option of this quote; "
                                     "pay it or ask us to cancel it.")
        if inv.get("status") == "draft":
            raise HTTPException(409, f"Prepayment invoice {inv.get('number')} is being checked by our finance team.")
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"prepayment.booking": booking, "updated_at": now_iso()}})
        return inv
    send_also = user.get("email") if user.get("org_type") == "customer" else None
    inv = await billing.create_prepayment_invoice(q, opt, company, actor=user, send_also_to=send_also)
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"prepayment": {
        "invoice_id": inv["id"], "invoice_number": inv.get("number"), "option_id": opt["id"],
        "amount_cents": inv.get("total_cents"), "status": "awaiting_payment" if inv.get("status") != "draft" else "invoice_review",
        "booking": booking, "requested_by": {"id": user["id"], "name": user.get("name"), "email": user.get("email")},
        "requested_at": now_iso()}, "updated_at": now_iso()}})
    await audit(user, "prepayment_requested", "quote", q["id"], {"invoice_id": inv["id"], "option_id": opt["id"],
                                                                  "total_cents": inv.get("total_cents")},
                company_id=q.get("company_id"))
    return inv


@on("invoice.paid")
async def _on_paid(p: dict):
    inv = p.get("invoice") or {}
    if inv.get("origin") == "prepayment" and inv.get("quote_id"):
        await jobs.enqueue("prepayment.book", {"invoice_id": inv["id"]}, key=f"prepayment:{inv['id']}",
                           company_id=inv.get("company_id"), subject=("quote", inv["quote_id"]))


@jobs.job("prepayment.book", max_attempts=4, backoff=(30, 120, 600), timeout=180,
          label_fi="Varaus ennakkomaksun jälkeen", label_en="Booking after prepayment")
async def book_after_payment(j):
    return await book_paid(j["payload"]["invoice_id"])


async def book_paid(invoice_id: str) -> dict:
    from core.permissions import permissions_for
    from routes.quotes import BookingIn, book
    from services.billing import _shipment_milestone, public_base_url
    from services.email_channel import store
    from services.email_channel.flow import _fake_request

    inv = await db.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv or inv.get("origin") != "prepayment" or inv.get("status") != "paid":
        return {"skipped": "not a paid prepayment invoice"}
    q = await db.quotes.find_one({"id": inv.get("quote_id")}, NO_ID)
    pp = (q or {}).get("prepayment") or {}
    if not q or pp.get("invoice_id") != invoice_id:
        return {"skipped": "quote not waiting for this invoice"}
    if q.get("shipment_id") or pp.get("status") == "booked":
        return {"skipped": "already booked", "shipment_id": q.get("shipment_id")}
    user = await db.users.find_one({"id": (pp.get("requested_by") or {}).get("id")}, {"_id": 0, "password_hash": 0})
    if not user or user.get("disabled"):
        raise RuntimeError("the person who booked is no longer active")
    user["permissions"] = sorted(permissions_for(user))
    try:
        res = await book(BookingIn(**pp["booking"]), _fake_request(await public_base_url() or ""), user, prepaid=inv)
    except HTTPException as exc:
        await db.quotes.update_one({"id": q["id"]}, {"$set": {"prepayment.status": "booking_failed",
                                                             "prepayment.error": str(exc.detail)[:500]}})
        from core.notify import bi, notify_role, same
        await notify_role(["ops_agent", "finance"], bi(f"Ennakkomaksettu varaus ei onnistunut: {q.get('number')}",
                                                       f"Prepaid booking failed: {q.get('number')}"),
                          same(str(exc.detail)[:200]), link=f"/ops/quotes/{q['id']}", kind="booking")
        return {"failed": str(exc.detail)}
    sid, ref = res["shipment_id"], res["shipment_reference"]
    await db.invoices.update_one({"id": invoice_id}, {"$set": {"shipment_id": sid, "shipment_reference": ref}})
    await db.shipments.update_one({"id": sid}, {"$set": {"prepaid_invoice_id": invoice_id, "payment_terms": "prepayment"}})
    await db.quotes.update_one({"id": q["id"]}, {"$set": {"prepayment.status": "booked", "prepayment.booked_at": now_iso(),
                                                         "prepayment.shipment_id": sid}})
    await _shipment_milestone(sid, "INVOICED", "Laskutettu (ennakkomaksu)", None, note=inv.get("number"))
    t = await db.email_threads.find_one({"quote_id": q["id"]}, NO_ID)
    if t and not t.get("shipment_id"):
        await store.update_thread(t["id"], shipment_id=sid, shipment_reference=ref, ref=ref, state="booked", needs_owner=False)
        await db.shipments.update_one({"id": sid}, {"$set": {"email_thread_id": t["id"], "channel": "email"}})
    await audit({"id": "system", "name": "Prepayment"}, "prepayment_booked", "quote", q["id"],
                {"invoice_id": invoice_id, "shipment_id": sid}, company_id=q.get("company_id"))
    return {"booked": ref, "shipment_id": sid}
