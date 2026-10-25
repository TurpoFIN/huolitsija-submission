"""Identifier validation: Business ID (Y-tunnus), EORI, VAT ID, MRN — format rules, adversarial input, and the
live registers (EU VIES, EU EOS EORI) for real Finnish companies. No mocks.

Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_validation.py
"""
import asyncio
import random

import pytest

import test_v3_support as S  # noqa: F401  (puts backend/ on sys.path)
from core.refs import ytunnus_valid
from services import kyc
from services.customs import eori_format_ok, eori_normalise, mrn_ok
from services.seed_customs import _mrn


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- Business ID ------------------------------------------------------------------------------
@pytest.mark.parametrize("bid", ["3596623-5", "0112038-9", "0109862-8", "2331972-7"])
def test_real_business_ids_are_valid(bid):
    assert ytunnus_valid(bid)


@pytest.mark.parametrize("bid", ["3596623-4", "0112038-8", "1234567-8", "359662-35", "3596623", "35966235-",
                                 "", "   ", "abcdefg-h", "3596623-5; DROP", None])
def test_bad_business_ids_are_rejected(bid):
    assert not ytunnus_valid(bid)


@pytest.mark.parametrize("raw,expected", [("3596623-5", "3596623-5"), (" 3596623-5 ", "3596623-5"), ("35966235", "3596623-5"),
                                          ("FI35966235", "3596623-5"), ("fi 3596623 5", "3596623-5"), ("112038-9", "0112038-9"),
                                          ("３５９６６２３-５", "3596623-5"), ("3596623—5", "3596623-5"),
                                          ("٣٥٩٦٦٢٣-٥", "3596623-5"), ("3596623-55", None), ("FI", None), ("", None), (None, None)])
def test_business_id_normalisation(raw, expected):
    assert kyc.normalise_business_id(raw) == expected


def test_check_digit_rule_on_random_ids():
    """Brute-force the rule: for any 7-digit body exactly one check digit is valid, or none when remainder is 1."""
    rnd = random.Random(7)
    for _ in range(300):
        body = f"{rnd.randint(0, 9999999):07d}"
        valid = [d for d in range(10) if ytunnus_valid(f"{body}-{d}")]
        rem = sum(int(c) * w for c, w in zip(body, (7, 9, 10, 5, 8, 4, 2))) % 11
        assert valid == ([] if rem == 1 else [0 if rem == 0 else 11 - rem]), body


# ---- EORI -------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("FI35966235", "FI3596623-5"), ("fi3596623-5", "FI3596623-5"), (" FI 3596623-5", "FI3596623-5"),
                                          ("DE1234567890123", "DE1234567890123"), ("", None), (None, None)])
def test_eori_normalisation(raw, expected):
    assert eori_normalise(raw) == expected


@pytest.mark.parametrize("eori,ok", [("FI3596623-5", True), ("FI35966235", True), ("FI3596623-4", False), ("FI359662-35", False),
                                     ("DE1234567890123", True), ("XX", False), ("FI" + "1" * 30, False), ("", False)])
def test_eori_format(eori, ok):
    assert eori_format_ok(eori_normalise(eori) if eori else eori) is ok


def test_eori_live_registry_hyphenated_form():
    """EU EOS stores Finnish EORIs with the hyphen: FI0109862-8 valid, the same digits without it are not."""
    res = run(kyc.eori_check("FI0109862-8"))
    assert res["status"] == "pass", res
    assert res["eori"] == "FI0109862-8" and res["data"]["name"]


def test_eori_live_not_registered_company():
    """Helios One Oy has no EORI yet: the registry says not valid for both spellings, and the check says how to fix it."""
    res = run(kyc.eori_check(None, "3596623-5"))
    assert res["status"] == "warn", res
    assert set(res["tried"]) == {"FI3596623-5", "FI35966235"}
    assert "EORI Number Registration Service" in res["summary"]
    res = run(kyc.eori_check("FI3596623-5"))
    assert res["status"] == "fail" and res["valid"] is False


# ---- VAT ID (VIES) -------------------------------------------------------------------------------
def test_vies_live_valid_finnish_vat():
    res = run(kyc.vies_check("FI35966235"))
    assert res["status"] == "pass", res
    assert res["vat_id"] == "FI35966235"


def test_vies_live_unknown_number():
    res = run(kyc.vies_check("FI12345671"))
    assert res["status"] in ("fail", "error"), res  # error only when VIES itself is down (reported, never "pass")
    assert res.get("valid") is not True


@pytest.mark.parametrize("vat", ["", "12345678", "FI", "<script>", "FI 3596 6235 ' OR 1=1"])
def test_vies_rejects_garbage_without_calling_out(vat):
    res = run(kyc.vies_check(vat))
    assert res["status"] in ("fail", "error") and res.get("valid") is not True


# ---- MRN --------------------------------------------------------------------------------------
def test_mrn_format_and_check_digit():
    from services.customs import mrn_advisories, mrn_check_digit

    rnd = random.Random(1)
    for _ in range(50):
        m = _mrn(rnd)
        assert mrn_ok(m) and mrn_advisories(m) == [], m
    # ISO 6346 reference: container CSQU305438 has check digit 3 (same algorithm, BIC example)
    assert mrn_check_digit("CSQU305438") == "3"
    good = _mrn(random.Random(5))
    typo = good[:8] + ("1" if good[8] != "1" else "2") + good[9:]  # one mistyped digit
    assert mrn_ok(typo) and any("Check digit" in a for a in mrn_advisories(typo))
    assert any("Procedure letter" in a for a in mrn_advisories(good[:16] + "A" + mrn_check_digit(good[:16] + "A")))
    for bad in ("", "26FI000000000000000", "26FIDEMO12345678R", "XXFIDEMO12345678R1", "26FIDEMO12345678R12", "26FIDEMO1234567éR1"):
        assert not mrn_ok(bad), bad
    assert mrn_ok(good.lower())  # typed in lower case: accepted, stored upper case by record_response


# ---- API: the lookup endpoint validates before calling YTJ ---------------------------------------
def test_lookup_endpoint_validation_and_live_ytj():
    c = S.client(S.CUSTOMER)
    r = c.post("/company/lookup", json={"business_id": "3596623-4"})
    assert r.status_code == 400 and "check digit" in r.json()["detail"]
    r = c.post("/company/lookup", json={"business_id": "x" * 5000})
    assert r.status_code in (400, 422)
    r = c.post("/company/lookup", json={"business_id": "FI35966235"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["found"] and j["name"] == "Helios One Oy" and j["business_id"] == "3596623-5"
    assert j["address"]["city"] == "Espoo" and j["source"].startswith("PRH") and j["fetched_at"]
    # a well-formed ID that is not issued: an honest "not found", not an error and not invented data
    r = c.post("/company/lookup", json={"business_id": "3990001-4"})
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["found"] is False
