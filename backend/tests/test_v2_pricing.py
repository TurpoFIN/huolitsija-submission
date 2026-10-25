"""Pricing math through the real API: chargeable weight rules, markup, totals, VAT codes, insurance, duty and
import VAT, Incoterm split, DG acceptance and sanctions. Expected values are computed here from the rules in the
brief (IATA 1:6000, road 333 kg/m³ and 1 850 kg/ldm, LCL 1 m³ = 1 000 kg, FCL per container, 110 % insured value,
EUR 3 per item category up to EUR 150), not read back from the engine."""
import math

import pytest

from test_v2_support import v2_cleanup  # noqa: F401  (session teardown of test data)
from test_v2_support import by_service, cust_company_id, line, ops, ops_quote, ready, signup

VAT = {"FI_25_5": 25.5, "FI_13_5": 13.5, "FI_14": 13.5, "FI_10": 10.0, "ZERO_EXPORT": 0.0, "ZERO_INTL_TRANSPORT": 0.0,
       "EU_ICS_RC": 0.0, "OUT_OF_SCOPE_DISBURSEMENT": 0.0, "OUT_OF_SCOPE_FOREIGN": 0.0}


@pytest.fixture(scope="module")
def air_quote():
    return ops_quote({"mode": "air", "origin": "CNPVG", "destination": "FIHEL", "ready_date": ready(),
                      "cargo": {"load_type": "loose", "packages": 4, "package_type": "carton", "weight_kg": 120, "volume_m3": 1.2}})


@pytest.fixture(scope="module")
def fcl_quote():
    return ops_quote({"origin": "CNSHA", "destination": "FIHEL", "incoterm": "FOB", "incoterm_place": "Shanghai", "ready_date": ready(),
                      "customs_clearance": True,
                      "cargo": {"load_type": "FCL", "containers": [{"type": "40HC", "count": 2}], "weight_kg": 18000,
                                "value_cents": 6_000_000, "duty_rate_pct": 2.7, "customs_items": 7}})


def test_air_chargeable_weight_is_1_to_6000(air_quote):
    air = by_service(air_quote, "AIR")
    assert air, f"no air options: {air_quote['not_quoted']}"
    expected = math.ceil(max(120, 1.2 * 1_000_000 / 6000) * 2) / 2  # 200 kg volumetric beats 120 kg actual
    assert expected == 200
    for o in air:
        assert o["chargeable"]["chargeable_kg"] == expected
        freight = line(o, "AIR_FREIGHT")
        assert freight and freight["qty"] == expected and freight["basis"] == "PER_KG"
        # per-kg surcharges (fuel / security) are charged on the same chargeable weight
        for ln in o["lines"]:
            if ln["basis"] == "PER_KG" and ln["stage"] == "main":
                assert ln["qty"] == expected, ln


def test_road_groupage_uses_loading_metres():
    q = ops_quote({"origin": "DEBER", "destination": "FILHI", "ready_date": ready(),
                   "cargo": {"load_type": "loose", "packages": 3, "package_type": "pallet_eur", "weight_kg": 900, "volume_m3": 3.0}})
    ltl = by_service(q, "ROAD_LTL")
    assert ltl, q["not_quoted"]
    # 3 EUR pallets non-stackable = 1.2 ldm -> 1.2 x 1850 = 2220 kg > 3.0 m³ x 333 = 999 kg > 900 kg actual
    for o in ltl:
        assert o["chargeable"]["chargeable_kg"] == 2220
        assert o["chargeable"]["ldm"] == pytest.approx(1.2)
        freight = line(o, "ROAD_FREIGHT")  # road tariffs are per 100 kg: 22.2 x (EUR per 100 kg)
        assert freight["basis"] == "PER_100KG" and freight["qty"] == pytest.approx(22.2)
        assert freight["amount_cents"] == round(freight["unit_price_cents"] * 22.2)


def test_lcl_weight_or_measure():
    q = ops_quote({"mode": "sea", "origin": "CNSHA", "destination": "FIHEL", "ready_date": ready(),
                   "cargo": {"load_type": "loose", "packages": 6, "package_type": "pallet_fin", "weight_kg": 1800, "volume_m3": 4.2}})
    lcl = by_service(q, "LCL")
    assert lcl, q["not_quoted"]
    for o in lcl:
        assert o["chargeable"]["wm"] == pytest.approx(4.2)  # max(1.8 t, 4.2 m³)
        assert line(o, "OCEAN_FREIGHT")["qty"] == pytest.approx(4.2)


def test_fcl_per_container_and_markup_math(fcl_quote):
    fcl = by_service(fcl_quote, "FCL")
    assert len(fcl) >= 3
    for o in fcl:
        markup = o["markup_pct"]
        assert markup == pytest.approx(18.0)  # settings default_margin_pct (no company override)
        assert line(o, "OCEAN_FREIGHT")["qty"] == 2
        for ln in o["lines"]:
            assert ln["amount_cents"] == round(ln["unit_price_cents"] * ln["qty"]), ln
            if ln["is_pass_through"]:
                assert ln["unit_price_cents"] == ln["unit_cost_cents"], ln  # disbursements at cost
            elif ln["unit_cost_cents"] > 0 and not ln.get("fixed_price"):
                assert ln["unit_price_cents"] == round(ln["unit_cost_cents"] * (1 + markup / 100)), ln
        assert o["total_cents"] == sum(ln["amount_cents"] for ln in o["lines"])
        assert o["vat_cents"] == sum(round(ln["amount_cents"] * VAT[ln["vat_code"]] / 100) for ln in o["lines"])
        own = [ln for ln in o["lines"] if not ln["is_pass_through"]]
        assert o["margin_cents"] == sum(ln["amount_cents"] - ln["cost_cents"] for ln in own)
        assert o["total_incl_vat_cents"] == o["total_cents"] + o["vat_cents"]


def test_fcl_vat_codes_and_duty(fcl_quote):
    o = by_service(fcl_quote, "FCL")[0]
    for ln in o["lines"]:
        if ln["is_pass_through"]:
            assert ln["vat_code"] == "OUT_OF_SCOPE_DISBURSEMENT", ln
        elif ln["stage"] in ("main", "destination", "origin", "fees") and ln.get("leg_seq") in (None, 1):
            assert ln["vat_code"] in ("ZERO_INTL_TRANSPORT", "FI_25_5"), ln
    assert line(o, "OCEAN_FREIGHT")["vat_code"] == "ZERO_INTL_TRANSPORT"
    customs = line(o, "CUSTOMS")
    assert customs is None  # No customs representative registration: filing fees are postponed.
    postponed = next(n for n in o["not_included"] if n["code"] == "CUSTOMS")
    assert postponed["status"] == "postponed" and postponed["amount_cents"] is None
    assert postponed["reason_code"] == "customs.postponed" and postponed["reason_fi"]
    assert not line(o, "CUSTOMS_ITEMS")
    duty = line(o, "DUTY")
    assert duty and duty["is_pass_through"]
    # customs value under FOB = goods + freight to the EU border (main leg); 2.7 % of it
    assert duty["amount_cents"] > round(6_000_000 * 0.027)
    # FOB: the seller pays origin THC -> not in our lines, listed as not included
    assert not line(o, "OTHC")
    assert any(n["code"] == "OTHC" and n["reason_code"] == "incoterm.seller" for n in o["not_included"])
    # demo company is VAT-registered: import VAT postponed (not a line)
    assert not line(o, "IMPORT_VAT")
    assert any(n["code"] == "IMPORT_VAT" and n["reason_code"] == "vat.postponed" for n in o["not_included"])


def test_domestic_is_standard_rated():
    q = ops_quote({"origin": "FIHEL", "destination": "FIOUL", "ready_date": ready(),
                   "cargo": {"load_type": "loose", "packages": 4, "package_type": "pallet_eur", "weight_kg": 1200, "volume_m3": 4}})
    assert q["options"], q["not_quoted"]
    for o in q["options"]:
        assert {ln["vat_code"] for ln in o["lines"]} == {"FI_25_5"}
        assert o["vat_cents"] == sum(round(ln["amount_cents"] * 25.5 / 100) for ln in o["lines"])
        assert o["liability_regime"] == "NSAB"


def test_export_customs_zero_rated_and_fca_split():
    q = ops_quote({"origin": "FIHEL", "destination": "USNYC", "incoterm": "FCA", "incoterm_place": "Helsinki",
                   "customer_role": "seller", "customs_clearance": True, "ready_date": ready(),
                   "cargo": {"load_type": "FCL", "containers": [{"type": "40GP", "count": 1}], "weight_kg": 12000}})
    assert q["options"], q["not_quoted"]
    o = q["options"][0]
    exp = line(o, "EXPORT_CUSTOMS")
    assert exp is None
    postponed = next(n for n in o["not_included"] if n["code"] == "EXPORT_CUSTOMS")
    assert postponed["status"] == "postponed" and postponed["amount_cents"] is None
    assert q["request"]["customs_required"] is True and q["request"]["customs_handling"] == "us"
    # FCA seller: the ocean freight is the buyer's
    assert not line(o, "OCEAN_FREIGHT")
    assert any(n["code"] == "OCEAN_FREIGHT" and n["reason_code"] == "incoterm.buyer" for n in o["not_included"])


def test_insurance_110_percent():
    value = 2_000_000
    q = ops_quote({"origin": "EETLL", "destination": "FITMP", "ready_date": ready(), "insurance": {"cover": "ICC_A"},
                   "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur", "weight_kg": 600, "volume_m3": 2,
                             "value_cents": value}})
    o = q["options"][0]
    ins = line(o, "INSURANCE")
    assert ins and ins["is_pass_through"] and ins["vat_code"] == "OUT_OF_SCOPE_DISBURSEMENT"
    freight = sum(ln["amount_cents"] for ln in o["lines"] if ln["stage"] in ("origin", "main"))
    insured = round((value + freight) * 1.10)
    assert ins["insured_value_cents"] == insured
    assert ins["amount_cents"] == max(round(insured * 0.25 / 100), 3500)


def test_low_value_duty_three_euro_per_item():
    q = ops_quote({"mode": "air", "origin": "CNPVG", "destination": "FIHEL", "ready_date": ready(), "customs_clearance": True,
                   "cargo": {"load_type": "loose", "packages": 1, "package_type": "carton", "weight_kg": 2, "volume_m3": 0.01,
                             "value_cents": 6000, "customs_items": 2}})
    o = q["options"][0]
    duty = line(o, "DUTY")
    assert duty and duty["amount_cents"] == 600  # 2 item categories x EUR 3, value <= EUR 150 from 1.7.2026


def test_import_vat_is_a_line_for_non_registered_company():
    c, user = signup()
    r = c.post("/quotes", json={"origin": "CNSHA", "destination": "FIHEL", "incoterm": "FOB", "incoterm_place": "Shanghai",
                                "ready_date": ready(), "customs_clearance": True,
                                "cargo": {"load_type": "FCL", "containers": [{"type": "20GP", "count": 1}], "weight_kg": 9000,
                                          "value_cents": 2_500_000, "duty_rate_pct": 0}})
    assert r.status_code == 200, r.text
    o = r.json()["quote"]["options"][0]
    vat = line(o, "IMPORT_VAT")
    assert vat and vat["is_pass_through"] and vat["vat_code"] == "OUT_OF_SCOPE_DISBURSEMENT"
    assert vat["amount_cents"] >= round(2_500_000 * 0.255)  # 25.5 % of at least the goods value
    assert "cost_cents" not in o and "unit_cost_cents" not in vat  # customer view hides buy prices


def test_dangerous_goods_excludes_carriers_that_refuse():
    q = ops_quote({"mode": "air", "origin": "CNPVG", "destination": "FIHEL", "ready_date": ready(),
                   "cargo": {"load_type": "loose", "packages": 2, "package_type": "carton", "weight_kg": 80, "volume_m3": 0.4,
                             "dangerous_goods": True, "dg": {"un_number": "UN3480", "class": "9", "packing_group": "II"}}})
    names = {o["carrier_name"] for o in q["options"]}
    assert not any("Finnair" in n for n in names), names
    assert any("Finnair" in (n.get("carrier_name") or "") and "UN3480" in n.get("reason", "") for n in q["not_quoted"]), q["not_quoted"]
    assert all(line(o, "DG") for o in q["options"])  # RULE D-5: every DG option carries a DG surcharge
    assert q["status"] == "draft" and q["approval"]["required"] and "dangerous_goods" in q["approval"]["reasons"]


@pytest.mark.parametrize("country_code", ["RUSPB", "BYMSQ"])
def test_sanctioned_origins_rejected(country_code):
    r = ops().post("/quotes", json={"origin": country_code, "destination": "FIHEL", "company_id": cust_company_id(), "send": False,
                                    "cargo": {"load_type": "loose", "weight_kg": 100, "volume_m3": 1}})
    assert r.status_code == 400
    assert "Russia" in r.json()["detail"] or "Belarus" in r.json()["detail"]


def test_options_are_ranked_and_one_is_recommended(fcl_quote):
    opts = fcl_quote["options"]
    assert sum(1 for o in opts if o["recommended"]) == 1
    cheapest = min(opts, key=lambda o: o["total_cents"])
    assert "cheapest" in cheapest["tags"]
    fastest = min(opts, key=lambda o: o["door_to_door_days"])
    assert any("fastest" in o["tags"] and o["door_to_door_days"] == fastest["door_to_door_days"] for o in opts)
    for o in opts:
        assert o["etd"] <= o["eta"] and o["co2e_kg"] > 0 and o["valid_until"] >= ready(0)
        assert o["liability_regime"] == {"sea": "HAGUE_VISBY", "rail": "CIM", "road": "CMR", "air": "MONTREAL"}[o["mode"]]
