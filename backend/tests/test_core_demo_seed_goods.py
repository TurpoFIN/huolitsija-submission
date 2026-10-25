"""Demo customs seed: a shipment's documents and declaration describe the shipment's own goods (r3 review, demo
HUO-2026-000006 = seed K6 had Vietnamese oak furniture documents on an LED lamp consignment from Shanghai)."""
import json
import random
from pathlib import Path

from services.data_repair_demo_k6 import goods_match
from services.seed_customs import CATALOG, build_documents_data, cargo_from_documents, pick_shipments

COMPANY = {"name": "Pohjolan Kaluste Oy", "address": {"street": "Askonkatu 9", "postal_code": "15100", "city": "Lahti"},
           "vat_id": "FI12345678", "eori": "FI1234567-8"}
K6 = {"id": "k6", "seed_key": "K6", "company_id": "kaluste-id", "mode": "sea", "origin": {"country": "CN"},
      "created_at": "2026-08-27T05:00:00Z", "cargo": {"description": "LED furniture lamps and metal fittings (LCL)",
                                                       "packages": 48, "package_type": "cartons", "weight_kg": 1260}}
K5 = {"id": "k5", "seed_key": "K5", "company_id": "kaluste-id", "origin": {"country": "VN"}}
C3 = {"id": "c3", "seed_key": "C3", "company_id": "coffee-id", "origin": {"country": "DJ"}}
C1 = {"id": "c1", "seed_key": "C1", "company_id": "coffee-id", "origin": {"country": "BR"}}
E1 = {"id": "e1", "seed_key": "E1", "company_id": "electro-id", "origin": {"country": "HK"}}
KEYS = {"kaluste-id": "kaluste", "coffee-id": "coffee", "electro-id": "electro"}


def test_k6_documents_say_what_its_hold_says():
    data = build_documents_data(K6, COMPANY, "lighting", random.Random(1))
    assert (data["invoice"]["packages"], data["transport_packages"], data["packing_list"]) == (52, 48, False)
    assert {ln["description"] for ln in data["invoice"]["lines"]} == {ln[0] for ln in CATALOG["lighting"]["lines"]}
    assert data["invoice"]["seller"]["country"] == "CN"
    cargo = cargo_from_documents(data)
    assert (cargo["packages"], cargo["description"], cargo["hs_hint"]) == (48, "LED furniture lamps and metal fittings (LCL)", "9405")
    assert abs(cargo["weight_kg"] - 1260) < 30  # quantities reproduce the booked gross weight


def test_each_company_gets_a_shipment_its_documents_fit():
    picked = {s["seed_key"]: key for s, key in pick_shipments([K6, K5, C3, C1, E1], KEYS)}
    # K6 sorted first: it gets its own goods, not the company's oak furniture; coffee skips Djibouti for the Santos one
    assert picked == {"K6": "lighting", "C1": "coffee", "E1": "electro"}
    picked = {s["seed_key"]: key for s, key in pick_shipments([K5, K6], KEYS)}
    assert picked == {"K5": "kaluste"}


def test_every_catalogue_names_its_goods_in_both_languages():
    stored_fi = json.loads((Path(__file__).resolve().parents[2] / "frontend/src/i18n/storedText.fi.json").read_text())
    for key, cat in CATALOG.items():
        en, fi = cat["goods"]
        assert en and fi and en != fi, key
        assert stored_fi.get(en) == fi, key  # shipment lists show cargo_description through this table


def test_goods_match_tells_k6_from_oak_furniture():
    oak = [{"description": ln[0]} for ln in CATALOG["kaluste"]["lines"]]
    lamps = [{"description": ln[0]} for ln in CATALOG["lighting"]["lines"]]
    assert not goods_match(oak, CATALOG["lighting"]["lines"])
    assert goods_match(lamps, CATALOG["lighting"]["lines"])
    assert not goods_match([], CATALOG["lighting"]["lines"])
