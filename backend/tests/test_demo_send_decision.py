"""The offer's send decision carries stable reason codes next to the English sentences, read from the quote agent's
recorded decision (or the card's reasons before it decides). In memory: no MongoDB, mail or model."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from services.demo_autoquote import driver as d

CUSTOMS = {"code": "customs_required", "en": "Customs formalities required.", "fi": "Tullimuodollisuudet vaaditaan."}


def fake_db(monkeypatch, decision):
    monkeypatch.setattr(d, "db", NS(approvals=NS(find_one=AsyncMock(return_value={"id": "card", "status": "pending"})),
                                    agent_decisions=NS(find_one=AsyncMock(return_value=decision))))


@pytest.mark.asyncio
async def test_reason_codes_come_from_the_recorded_decision(monkeypatch):
    fake_db(monkeypatch, {"id": "dec", "outcome": "owner_queue", "reasons": [CUSTOMS]})
    info = await d._send_decision({"id": "q", "approval": {"approval_id": "card", "reasons": ["autopilot_off"]}})
    assert info["reason_codes"] == ["customs_required"]
    assert info["decision_reasons"] == ["Customs formalities required."]


@pytest.mark.asyncio
async def test_reason_codes_fall_back_to_the_card_before_a_decision(monkeypatch):
    fake_db(monkeypatch, None)
    info = await d._send_decision({"id": "q", "approval": {"approval_id": "card", "reasons": ["customs_required"]}})
    assert info["reason_codes"] == ["customs_required"] and info["decision_reasons"] == []
