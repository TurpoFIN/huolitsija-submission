"""Outbound prospect and carrier e-mails name the sender honestly: until a Business ID is set they say the company is
in formation (master rule for any real outreach before incorporation)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DB_NAME", "huo_test_s1_identity")

from services import partner_network, prospecting  # noqa: E402


def test_in_formation_until_business_id():
    for lines in (prospecting._identity_lines(True, {}), prospecting._identity_lines(False, {}), *partner_network._identity({})):
        assert "company in formation" in lines[0], lines
    fi_p, fi_n = prospecting._identity_lines(True, {})[0], partner_network._identity({})[0][0]
    assert "perustettava yhtiö" in fi_p and "perustettava yhtiö" in fi_n
    with_bid = {"seller_business_id": "1234567-8"}
    assert "in formation" not in prospecting._identity_lines(False, with_bid)[0]
    assert "in formation" not in partner_network._identity(with_bid)[1][0]
