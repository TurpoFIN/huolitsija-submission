"""Live HUO-2026-000080: the SI was refused by our own DCSA eBL 3.0 schema check because the whole cargo description was
sent as one descriptionOfGoods line ("is too long"). DCSA allows 150 lines of 35 characters."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.execution import dcsa  # noqa: E402

DESCRIPTION = "TEST S4 SI/VGM export paper rolls - acceptance test, no cargo"


def _si(description):
    record = {"carrier_booking_reference": "TESTBKG-S4-0002",
              "containers": [{"index": 0, "equipment_reference": "TSTU1234573", "seal_number": "TESTSEAL1", "iso_type": "45G1",
                              "cargo_gross_kg": 18000, "packages": 20}]}
    shipment = {"reference": "HUO-2026-000080", "incoterm": "FCA", "direction": "export", "customer_name": "E2E Testi 084712 Oy",
                "destination": {"country": "DE"}, "references": {"customer_ref": "TEST-S4-SIVGM2"},
                "cargo": {"description": description, "hs_hint": "480519", "package_type": "roll"},
                "parties": {"shipper": {"name": "E2E Testi 084712 Oy", "address": {"street": "Mannerheimintie 12", "postal_code": "00100",
                                                                                 "city": "Helsinki", "country": "FI"}},
                            "consignee": {"name": "TEST Consignee GmbH", "address": {"street": "Teststrasse 1", "postal_code": "0000",
                                                                                    "city": "Hamburg", "country": "DE"}}}}
    return dcsa.build_si(record, shipment, contact={"name": "Huolitsija ops", "email": "ops@huolitsija.fi"})


def test_long_goods_description_is_split_into_dcsa_lines_and_validates():
    si = _si(DESCRIPTION)
    lines = si["consignmentItems"][0]["descriptionOfGoods"]
    assert all(len(line) <= 35 for line in lines) and " ".join(lines) == DESCRIPTION
    errors = dcsa.validate("ebl", "CreateShippingInstructions", si)
    assert not [e for e in errors if "descriptionOfGoods" in e], errors


def test_missing_description_still_has_a_line():
    assert _si(None)["consignmentItems"][0]["descriptionOfGoods"] == ["General cargo"]
