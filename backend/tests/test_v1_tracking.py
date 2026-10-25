"""Carrier tracking adapters: DCSA Track & Trace 2.2 and DHL Unified Tracking. Inactive until their keys are set; the
DHL adapter talks to the real api-eu.dhl.com endpoint (an invalid key is rejected by DHL itself)."""
import asyncio
import os

import pytest

from test_v1_support import customer, ops
from services import tracking


def test_adapter_registry_lists_switches():
    r = ops().get("/tracking/adapters")
    assert r.status_code == 200
    items = r.json()["items"]
    keys = {i["key"] for i in items}
    assert "dhl" in keys and any(k.startswith("dcsa:") for k in keys)
    for i in items:
        assert isinstance(i["configured"], bool) and i["env"], i
    assert customer().get("/tracking/adapters").status_code == 403


def test_dhl_adapter_reaches_the_real_api():
    os.environ["DHL_API_KEY"] = "huolitsija-test-invalid-key"
    a = tracking.DHLUnifiedTracking()
    assert a.configured()
    with pytest.raises(PermissionError) as e:
        asyncio.run(a.track("1234567890"))
    assert "DHL rejected the API key" in str(e.value)
