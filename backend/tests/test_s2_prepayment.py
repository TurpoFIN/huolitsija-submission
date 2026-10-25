"""s2: a prepayment-only customer (credit limit 0, set by a person) books by paying first (services/prepayment.py).

book() issues a prepayment invoice instead of refusing; paying it (billing.record_payment -> invoice.paid) books the
shipment as the same customer and links the paid invoice to it, and the shipment is never invoiced twice. A customer
with a credit hold, or an option other than the one prepaid, is still refused.

In process against a throw-away MongoDB database (own database, dropped at the end)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_prepay_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s2_prepayment.py in its own pytest process", allow_module_level=True)

CID = str(uuid.uuid4())
FIHEL = {"unlocode": "FIHEL", "code": "FIHEL", "name": "Helsinki", "country": "FI", "lat": 60.17, "lon": 24.94}
SESTO = {"unlocode": "SESTO", "code": "SESTO", "name": "Stockholm", "country": "SE", "lat": 59.33, "lon": 18.07}
CUSTOMER = {"id": "u-cust", "email": "asiakas@ennakko.example", "name": "Aino Asiakas", "role": "customer_admin",
            "org_type": "customer", "company_id": CID, "locale": "fi", "session_version": 0, "email_verified": True}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        await ensure_indexes()
        await db.raw.companies.insert_one({
            "id": CID, "name": "Ennakko Testi Oy", "business_id": "3994713-8", "country": "FI", "test_tenant": True,
            "kyc_status": "approved", "sanctions_status": "clear", "status": "active", "credit_limit_cents": 0,
            "credit_limit_source": "manual", "billing_email": "laskut@ennakko.example",
            "address": {"street": "Testikatu 1", "postal_code": "00100", "city": "Helsinki", "country": "FI"},
            "created_at": "2026-01-01T00:00:00Z"})
        await db.raw.users.insert_one(dict(CUSTOMER))
        await db.raw.carriers.insert_one({"id": "c-road", "name": "Testi Kuljetus Oy", "code": "TESTIK", "modes": ["road"],
                                          "active": True})
    run(go())


def teardown_module(_m):
    from core.db import client
    run(client.drop_database(os.environ["DB_NAME"]))


def quote(tag: str) -> dict:
    from core.db import db
    from core.util import now
    ready = (now() + timedelta(days=7)).date().isoformat()
    lines = [{"code": "ROAD_FREIGHT", "stage": "main", "leg_seq": 1, "qty": 1, "unit_cost_cents": 22000, "cost_cents": 22000,
              "unit_price_cents": 29000, "amount_cents": 29000, "is_pass_through": False},
             {"code": "DOC_FEE", "stage": "fees", "leg_seq": None, "qty": 1, "unit_cost_cents": 0, "cost_cents": 0,
              "unit_price_cents": 3500, "amount_cents": 3500, "is_pass_through": False}]
    opts = []
    for n in (1, 2):
        opts.append({"id": f"opt_{tag}_{n}", "mode": "road", "service": "ROAD_LTL", "carrier_id": "c-road",
                     "carrier_name": "Testi Kuljetus Oy", "total_cents": 32500, "cost_cents": 22000, "firmness": "firm",
                     "liability_regime": "CMR", "forwarder_role": "principal", "valid_until": (now() + timedelta(days=14)).date().isoformat(),
                     "legs": [{"seq": 1, "kind": "main", "mode": "road", "from": dict(FIHEL), "to": dict(SESTO),
                               "carrier_id": "c-road", "carrier_name": "Testi Kuljetus Oy", "transit_days": 2, "service": "ROAD_LTL"}],
                     "lines": lines})
    q = {"id": f"q_{tag}", "number": f"Q-{tag}", "company_id": CID, "customer_name": "Ennakko Testi Oy", "status": "sent",
         "source": "portal", "valid_until": (now() + timedelta(days=14)).date().isoformat(),
         "request": {"mode": "road", "origin": dict(FIHEL), "destination": dict(SESTO), "ready_date": ready,
                     "incoterm": "DAP", "incoterm_place": "Stockholm", "direction": "export", "cargo_summary": "2 EUR-lavaa",
                     "cargo": {"load_type": "LTL", "packages": 2, "package_type": "pallet_eur", "weight_kg": 400,
                               "volume_m3": 1.92, "description": "TEST steel parts", "dangerous_goods": False}},
         "options": opts}
    run(db.raw.quotes.insert_one(dict(q)))
    return q


def body(q: dict, n: int = 1):
    from routes.quotes import BookingIn
    party = lambda name, city, country: {"name": name, "address": {"street": "Testikatu 1", "postal_code": "00100",  # noqa: E731
                                                                   "city": city, "country": country},
                                         "contact": {"name": "Aino", "email": "asiakas@ennakko.example"}}
    return BookingIn(quote_id=q["id"], option_id=q["options"][n - 1]["id"], shipper=party("Ennakko Testi Oy", "Helsinki", "FI"),
                     consignee=party("Mottagare AB", "Stockholm", "SE"), accept_terms=True)


def request():
    from services.email_channel.flow import _fake_request
    return _fake_request("https://app.example")


def user():
    from core.permissions import permissions_for
    return {**CUSTOMER, "permissions": sorted(permissions_for(CUSTOMER))}


def test_prepayment_only_customer_gets_an_invoice_then_the_booking_when_it_is_paid():
    from core.db import db
    from routes.quotes import book
    from services import billing, prepayment
    q = quote("pp1")
    res = run(book(body(q), request(), user()))
    assert res["shipment_id"] is None and res["prepayment"]["invoice_number"]
    inv = run(db.raw.invoices.find_one({"id": res["prepayment"]["invoice_id"]}, {"_id": 0}))
    assert inv["origin"] == "prepayment" and inv["status"] == "open" and inv["due_date"] == inv["issue_date"]
    assert inv["total_cents"] > 32500  # VAT on top of the option total
    assert inv["quote_id"] == q["id"] and inv["option_id"] == q["options"][0]["id"]
    mails = run(db.raw.email_outbox.find({"ref.id": inv["id"]}, {"_id": 0, "to": 1}).to_list(None))
    assert {m["to"] for m in mails} == {"laskut@ennakko.example", CUSTOMER["email"]}
    qd = run(db.raw.quotes.find_one({"id": q["id"]}, {"_id": 0}))
    assert qd["prepayment"]["status"] == "awaiting_payment" and qd["status"] == "sent" and not qd.get("shipment_id")

    again = run(book(body(q), request(), user()))  # a second click returns the same open invoice
    assert again["prepayment"]["invoice_id"] == inv["id"]
    with pytest.raises(HTTPException) as e:  # another option while this one waits for payment
        run(book(body(q, 2), request(), user()))
    assert e.value.status_code == 409
    assert run(prepayment.book_paid(inv["id"])) == {"skipped": "not a paid prepayment invoice"}

    paid, applied = run(billing.record_payment(inv["id"], amount_cents=inv["total_cents"], method="card",
                                               ref="cs_test_prepay_1", actor=None))
    assert applied and paid["status"] == "paid"
    job = run(db.raw.jobs.find_one({"kind": "prepayment.book"}, {"_id": 0}))
    assert job and job["payload"] == {"invoice_id": inv["id"]}
    out = run(prepayment.book_paid(inv["id"]))
    sid = out["shipment_id"]
    s = run(db.raw.shipments.find_one({"id": sid}, {"_id": 0}))
    assert s["company_id"] == CID and s["prepaid_invoice_id"] == inv["id"] and s["payment_terms"] == "prepayment"
    assert run(db.raw.invoices.find_one({"id": inv["id"]}, {"_id": 0}))["shipment_id"] == sid
    qd = run(db.raw.quotes.find_one({"id": q["id"]}, {"_id": 0}))
    assert qd["status"] == "booked" and qd["prepayment"]["status"] == "booked" and qd["shipment_id"] == sid
    assert run(billing.create_invoice_for_shipment(sid, None))["id"] == inv["id"]  # never a second invoice
    assert run(prepayment.book_paid(inv["id"]))["skipped"] == "already booked"


def test_a_credit_hold_is_still_refused():
    from core.db import db
    from routes.quotes import book
    run(db.raw.companies.update_one({"id": CID}, {"$set": {"credit_hold": True, "credit_hold_reason": "overdue"}}))
    try:
        with pytest.raises(HTTPException) as e:
            run(book(body(quote("pp2")), request(), user()))
        assert e.value.status_code == 409
        assert run(db.raw.invoices.count_documents({"quote_id": "q_pp2"})) == 0
    finally:
        run(db.raw.companies.update_one({"id": CID}, {"$set": {"credit_hold": False}}))
