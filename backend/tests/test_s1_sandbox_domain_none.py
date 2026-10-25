"""A sandbox domain edited to none/off (the host cannot delete a secret) means real recipients; a real domain still
rewrites every recipient to the sandbox. Covers CRM prospecting and partner-network RFQs."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DB_NAME", "huo_test_s1_sandbox")

import pytest  # noqa: E402

from services import partner_network, prospecting  # noqa: E402


@pytest.mark.parametrize("value", ["none", "NONE", " off ", "", "-"])
def test_none_means_live(monkeypatch, value):
    monkeypatch.setenv("CRM_SANDBOX_DOMAIN", value)
    monkeypatch.setenv("PARTNER_SANDBOX_DOMAIN", value)
    assert prospecting.sandbox_address("info@hoviportaat.fi") is None
    assert partner_network.sandbox_address("info@carrier.fi") is None


def test_real_sandbox_domain_still_rewrites(monkeypatch):
    monkeypatch.setenv("CRM_SANDBOX_DOMAIN", "prospects.sandbox.test")
    monkeypatch.setenv("PARTNER_SANDBOX_DOMAIN", "partners.sandbox.test")
    assert prospecting.sandbox_address("info@hoviportaat.fi").endswith("@prospects.sandbox.test")
    assert partner_network.sandbox_address("info@carrier.fi").endswith("@partners.sandbox.test")
