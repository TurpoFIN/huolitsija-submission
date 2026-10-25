"""Reconciliation 'overpaid': a refund recorded on the invoice's credit note settles the difference (HUO-INV-2026-000011 /
credit note 000029: fully paid card invoice, 7.08 credited and refunded, still flagged as overpaid)."""
import asyncio
import os
import uuid

os.environ.setdefault("DB_NAME", f"huo_recon_cn_{uuid.uuid4().hex[:8]}")


def test_refunded_credit_note_is_not_overpaid():
    async def scenario():
        from core.db import db
        from services import reconciliation
        tag = uuid.uuid4().hex[:8]
        inv = {"id": "inv-" + tag, "kind": "invoice", "number": "T-" + tag, "status": "paid", "company_id": "c-" + tag,
               "total_cents": 904647, "paid_cents": 904647, "credited_cents": 708}
        cn = {"id": "cn-" + tag, "kind": "credit_note", "credit_note_for": inv["id"], "status": "open", "company_id": "c-" + tag,
              "total_cents": -708, "paid_cents": 0}
        open_ = {"id": "inv2-" + tag, "kind": "invoice", "number": "U-" + tag, "status": "paid", "company_id": "c-" + tag,
                 "total_cents": 1000, "paid_cents": 1500, "credited_cents": 0}
        await db.raw.invoices.insert_many([dict(inv), dict(cn), dict(open_)])

        def over(items, number):
            return [i["over_cents"] for i in items if i["kind"] == "overpaid" and i["invoice_number"] == number]

        items = await reconciliation.discrepancies()
        assert over(items, inv["number"]) == [708]  # credited, refund not yet recorded: the owner must act
        assert over(items, open_["number"]) == [500]
        await db.raw.invoices.update_one({"id": cn["id"]}, {"$set": {"status": "paid", "paid_cents": 708}})
        items = await reconciliation.discrepancies()
        assert over(items, inv["number"]) == []  # refund recorded on the credit note
        assert over(items, open_["number"]) == [500]

    _run(scenario())


def _run(coro):
    """On the loop the Motor client is bound to (see test_s2_payments.run)."""
    from core.db import client
    loop = client.get_io_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
