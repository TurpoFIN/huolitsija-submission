"""Approval must never upload a payment, even when production API mode is selected."""
import uuid
from unittest.mock import AsyncMock
import pytest
from services import payables
from test_s13_support import db, ops
from test_s13_pay_flow import carrier_invoice, payable_for, DEBTOR_IBAN, DEBTOR_BIC, EXEC

@pytest.mark.asyncio
async def test_approval_in_api_mode_never_calls_send(monkeypatch):
    import asyncio
    from core.db import client
    monkeypatch.setattr(client, "_io_loop", asyncio.get_running_loop())
    run_id = str(uuid.uuid4())
    db.payment_runs.insert_one({"id": run_id, "number": "PL-" + run_id, "status": "pending_approval"})
    monkeypatch.setenv("BANK_PAYMENT_MODE", "api")
    send = AsyncMock(side_effect=AssertionError("approval attempted bank submission"))
    monkeypatch.setattr(payables, "send", send)
    first = await payables.approve_run(run_id, {"id": "test", "name": "Finance test"})
    second = await payables.approve_run(run_id, {"id": "test", "name": "Finance test"})
    assert first["status"] == second["status"] == "approved"
    assert first["approved_at"] == second["approved_at"]
    send.assert_not_called()
    assert db.audit_log.count_documents({"entity_id": run_id, "action": "payment_run_approved"}) == 1


def test_payable_list_is_idempotent_and_approval_does_not_send():
    ci = carrier_invoice(name="Hyväksyntä Oy", iban=DEBTOR_IBAN, bic=DEBTOR_BIC, currency="EUR",
                         total_cents=12550, eur_total_cents=12550, reference=None, country="FI", due=EXEC)
    p = payable_for(ci)
    assert payable_for(ci)["id"] == p["id"]
    response = ops().get("/finance/payables", params={"q": ci["number"]})
    assert response.status_code == 200
    row, = response.json()["items"]
    assert row["amount_cents"] == 12550 and row["status"] == "approved"
    assert response.json()["eur_total_cents"] == 12550
    # Independent of payer setup: exercise the real approval route on a stored run.
    run_id = str(uuid.uuid4())
    db.payment_runs.insert_one({"id": run_id, "number": "PL-" + run_id, "status": "pending_approval",
                                "payable_ids": [p["id"]]})
    approved = ops().post(f"/finance/payment-runs/{run_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and not approved.json().get("sent_at")
    assert db.payables.find_one({"id": p["id"]})["status"] == "approved"
