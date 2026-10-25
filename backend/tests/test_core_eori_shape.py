"""Booking's EORI check (services.customs.eori_shape_ok) agrees with the EU EOS check and with the portal form.
Cases are shared with the frontend test (frontend/src/lib/eori.cases.json → eori.test.js)."""
import json
from pathlib import Path

import pytest

from services.customs import eori_normalise, eori_shape_ok
from services.kyc import eori_candidates

CASES = json.loads((Path(__file__).resolve().parents[2] / "frontend/src/lib/eori.cases.json").read_text())


@pytest.mark.parametrize("value,ok,normalised", CASES)
def test_eori_shape_matches_form_and_eos(value, ok, normalised):
    assert eori_shape_ok(value) is ok
    if ok:
        assert eori_normalise(value) == normalised
        assert eori_candidates(value)[0] == normalised  # the form EOS is asked first
