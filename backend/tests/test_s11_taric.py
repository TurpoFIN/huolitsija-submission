"""s11 TARIC: live EU TARIC consultation measures, duty maths, duty per declaration item, tariff quotas.

Fixtures are the official pages captured unchanged from https://ec.europa.eu/taxation_customs/dds2/taric/ by
scripts/taric_capture.py (manifest.json: url -> file, sha256, fetched_at). They are replayed through the same HTTP layer
and parser the service uses. Expected values below are literals read from the official pages (the consultation text
shown to a person: e.g. "Third country duty ... 14.00 %"), not values produced by this parser.

Tests named test_live_* fetch the consultation now; they skip cleanly when the site cannot be reached (or with
TARIC_LIVE=0).
"""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s11_taric_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s11_taric.py in its own pytest process", allow_module_level=True)

from services import taric  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "taric"
MANIFEST = json.loads((FIX / "manifest.json").read_text())


@pytest.fixture(scope="module", autouse=True)
def _db():
    yield
    from pymongo import MongoClient

    MongoClient(config.MONGO_URL).drop_database(os.environ["DB_NAME"])


@pytest.fixture()
def replay():
    pages = {}
    for url, meta in MANIFEST["pages"].items():
        text = (FIX / meta["file"]).read_bytes().decode("utf-8")  # bytes: keep the page's own CR LF
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == meta["sha256"], f"fixture changed: {meta['file']}"
        pages[url] = text
    taric._REPLAY = pages
    yield pages
    taric._REPLAY = None


def look(code, country, on="2026-10-24", flow="import"):
    return run(taric.lookup(code, country=country, flow=flow, on=on, use_cache=False))


def by_type(r, text):
    return [m for m in r["measures"] if m["type"] == text]


# ---------------------------------------------------------------------------------------------
# duty expressions and maths (expressions as printed by the consultation)
# ---------------------------------------------------------------------------------------------
def test_parse_duty_expressions():
    d = taric.parse_duty("14.00 %")
    assert (d["kind"], d["pct"]) == ("ad_valorem", 14.0)
    d = taric.parse_duty("32.00 EUR / hl")
    assert (d["kind"], d["amount"], d["currency"], d["unit"]) == ("specific", 32.0, "EUR", "hl")
    d = taric.parse_duty("2.30 % + 4.74 EUR / 100 kg")
    assert (d["kind"], d["pct"], d["amount"], d["unit"]) == ("compound", 2.3, 4.74, "100 kg")
    d = taric.parse_duty("9.00 % + EA(1) MAX 24.20 % +ADSZ(1)")
    assert d["kind"] == "agri_components" and d["agri_codes"] == ["ADSZ", "EA"]
    d = taric.parse_duty("12.00 % MIN 1.50 EUR / p/st")
    assert d["kind"] == "mixed_min_max" and d["pct"] == 12.0 and d["min"]["amount"] == 1.5 and d["min"]["unit"] == "p/st"
    d = taric.parse_duty("73.18 EurUP / 100 kg")
    assert d["kind"] == "reference_price"


def test_compute_duty():
    c = taric.compute_duty(taric.parse_duty("14.00 %"), 100000)
    assert (c["status"], c["duty_cents"]) == ("ok", 14000)
    # EUR 32 / hl on 750 litres = 7.5 hl -> EUR 240
    c = taric.compute_duty(taric.parse_duty("32.00 EUR / hl"), 500000, qty=750, qty_unit="l")
    assert (c["status"], c["duty_cents"]) == ("ok", 24000)
    c = taric.compute_duty(taric.parse_duty("32.00 EUR / hl"), 500000)
    assert c["status"] == "needs_quantity" and c["duty_cents"] is None and "hl" in c["reason"]["en"]
    # 2.3 % of EUR 1000 + EUR 4.74 per 100 kg x 250 kg = 23.00 + 11.85
    c = taric.compute_duty(taric.parse_duty("2.30 % + 4.74 EUR / 100 kg"), 100000, net_kg=250)
    assert (c["status"], c["duty_cents"]) == ("ok", 2300 + 1185)
    # MIN: 12 % of EUR 10 = 1.20 < EUR 1.50 x 2 pieces = 3.00
    c = taric.compute_duty(taric.parse_duty("12.00 % MIN 1.50 EUR / p/st"), 1000, qty=2, qty_unit="p/st")
    assert (c["duty_cents"], c["limit_applied"]) == (300, "min")
    c = taric.compute_duty(taric.parse_duty("9.00 % + EA(1) MAX 24.20 % +ADSZ(1)"), 100000, net_kg=100)
    assert c["status"] == "needs_agri_components" and c["duty_cents"] is None and "Meursing" in c["reason"]["en"]
    c = taric.compute_duty({"kind": "entry_price", "raw": "x"}, 100000, net_kg=100)
    assert c["status"] == "entry_price" and c["duty_cents"] is None and "2017/891" in c["reason"]["en"]


# ---------------------------------------------------------------------------------------------
# lookup on captured official pages
# ---------------------------------------------------------------------------------------------
def test_bicycles_cn_third_country_and_anti_dumping(replay):
    r = look("8712003010", "CN")
    assert r["source"] == "EU TARIC consultation" and r["declarable"] is True
    assert r["source_url"].startswith("https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp")
    # page: "Third country duty (09-06-2006 - ) : 14.00 %  R0838/06"
    tc = r["third_country_duty"]
    assert tc["duty_expression"] == "14.00 %" and tc["type_code"] == "103" and tc["regulation"] == "R0838/06"
    ad = {a["code"]: m for m in by_type(r, "Definitive anti-dumping duty") for a in m["additional_codes"]}
    # page: "Additional Code B999: Other ... 48.50 %  R2146/25"; "C453: Universal Cycle Corporation (Guang Zhou) 48.50 % R0966/20"
    assert ad["B999"]["duty_expression"] == "48.50 %" and ad["B999"]["regulation"] == "R2146/25"
    assert ad["C453"]["duty_expression"] == "48.50 %" and ad["C453"]["regulation"] == "R0966/20"
    assert all(m["type_code"] == "552" and m["geo_code"] == "CN" for m in ad.values())
    # B772 Zhejiang Baoguilai: conditional - A1 with D008 (invoice with a signed declaration) 19.20 %, A2 without 48.50 %
    b772 = ad["B772"]
    assert b772["duty"] is None and b772["additional_codes"][0]["description"] == "Zhejiang Baoguilai Vehicle Co. Ltd."
    lines = {(c["code"], c["sequence"]): c for c in b772["conditions"]}
    assert lines[("A", 1)]["certificate"] == "D008" and lines[("A", 1)]["duty_expression"] == "19.20 %"
    assert lines[("A", 1)]["text"] == "Invoice with a signed declaration"
    assert lines[("A", 2)]["certificate"] is None and lines[("A", 2)]["duty_expression"] == "48.50 %"
    assert ad["B999"]["additional_codes"][0]["residual"] is True and not b772["additional_codes"][0]["residual"]
    assert r["supplementary_unit"] == "p/st"
    # export side is not in the import result
    assert not [m for m in r["measures"] if m["flow"] == "export"]


def test_parent_code_is_not_declarable(replay):
    r = look("8712003000", "CN")
    assert r["declarable"] is False and r["subcodes"] == ["8712003010", "8712003020", "8712003090"]
    d = run(taric.duty_for_item({"cn_code": "87120030", "customs_value_cents": 100000}, origin="CN", on="2026-10-24"))
    assert d["status"] == "not_declarable" and d["duty_cents"] is None


def test_duty_for_item_bicycles_residual_and_company_code(replay):
    item = {"taric_code": "8712003010", "customs_value_cents": 1000000, "net_kg": 300, "quantity": 20, "unit": "p/st"}
    d = run(taric.duty_for_item(item, origin="CN", preference="100", on="2026-10-24"))
    assert d["status"] == "ok" and d["source"] == "taric" and d["measure_type"] == "103"
    assert d["measure_rate_pct"] == 14.0
    # residual code (producer not declared): 48.50 % on top -> 62.5 % of EUR 10 000
    assert [e["additional_code"] for e in d["trade_defence"]] == ["B999"]
    assert d["trade_defence"][0]["regulation"] == "R2146/25"
    assert d["rate_pct"] == 62.5 and d["duty_cents"] == 625000
    assert any(x["code"] == "td_residual" for x in d["reasons"])
    # Zhejiang Baoguilai (B772) with the D008 invoice: 19.20 %; without it: 48.50 %
    d = run(taric.duty_for_item(item, origin="CN", on="2026-10-24", additional_code="B772", documents=["D008"]))
    assert d["trade_defence"][0]["rate_pct"] == 19.2 and d["duty_cents"] == 140000 + 192000
    d = run(taric.duty_for_item(item, origin="CN", on="2026-10-24", additional_code="B772"))
    assert d["trade_defence"][0]["rate_pct"] == 48.5


def test_footwear_vn_evfta_preference(replay):
    r = look("6403999890", "VN")
    # page: "Third country duty (01-01-1999 - ) : 7.00 %"; "Viet Nam (VN) Tariff preference (01-01-2026 - 31-12-2026)
    # : 0.80 %  D0753/20"
    assert r["third_country_duty"]["duty_expression"] == "7.00 %"
    pref = [p for p in r["preferences"] if p["geo_code"] == "VN"]
    assert pref and pref[0]["duty_expression"] == "0.80 %" and pref[0]["type_code"] == "142"
    assert pref[0]["regulation"] == "D0753/20" and pref[0]["end"] == "2026-12-31"
    assert r["supplementary_unit"] == "pa"
    item = {"taric_code": "6403999890", "customs_value_cents": 200000}
    d = run(taric.duty_for_item(item, origin="VN", preference="300", on="2026-10-24"))
    assert (d["status"], d["measure_type"], d["rate_pct"], d["duty_cents"]) == ("ok", "142", 0.8, 1600)
    d = run(taric.duty_for_item(item, origin="VN", preference="100", on="2026-10-24"))
    assert (d["measure_type"], d["rate_pct"], d["duty_cents"]) == ("103", 7.0, 14000)
    # the sibling subcode 6403999810 is already at 0 % under the EVFTA on this date
    d = run(taric.duty_for_item({"taric_code": "6403999810", "customs_value_cents": 200000}, origin="VN",
                                preference="300", on="2026-10-24"))
    assert (d["measure_type"], d["duty_expression"], d["duty_cents"]) == ("142", "0 %", 0)
    d4 = run(taric.duty_for_item({"taric_code": "6403999810", "customs_value_cents": 100000}, origin="VN",
                                 preference="400", on="2026-10-24"))
    assert (d4["measure_type"], d4["duty_cents"]) == ("142", 0)
    # no preference for China -> no 142 measure -> blocking status, never a guessed rate
    d = run(taric.duty_for_item({"taric_code": "8471300000", "customs_value_cents": 1000}, origin="CN",
                                preference="300", on="2026-10-24"))
    assert d["status"] == "no_measure" and d["duty_cents"] is None


def test_laptops_zero(replay):
    r = look("8471300000", "CN")
    assert r["third_country_duty"]["duty_expression"] == "0 %"
    d = run(taric.duty_for_item({"taric_code": "8471300000", "customs_value_cents": 99999}, origin="CN",
                                on="2026-10-24"))
    assert (d["status"], d["rate_pct"], d["duty_cents"]) == ("ok", 0.0, 0)


def test_wine_specific_duty(replay):
    r = look("2204210600", "US")
    # page: "Third country duty (01-01-2010 - ) : 32.00 EUR / hl"; supplementary unit "l"
    assert r["third_country_duty"]["duty_expression"] == "32.00 EUR / hl" and r["supplementary_unit"] == "l"
    item = {"taric_code": "2204210600", "customs_value_cents": 300000, "quantity": 900, "unit": "l"}
    d = run(taric.duty_for_item(item, origin="US", on="2026-10-24"))
    assert d["status"] == "ok" and d["measure_duty_cents"] == 28800  # 9 hl x EUR 32
    d = run(taric.duty_for_item({**item, "quantity": None}, origin="US", on="2026-10-24"))
    assert d["status"] == "needs_quantity" and d["duty_cents"] is None


def test_oranges_entry_price_blocks(replay):
    r = look("0805102210", "MA", on="2026-01-15")
    tc = by_type(r, "Third country duty")[0]
    # page: condition V lines "Import price must be equal to or greater than the entry price ... 35.40 EUR/100kg -> 16.00 %"
    v = [c for c in tc["conditions"] if c["code"] == "V"]
    assert v[0]["duty_expression"] == "16.00 %" and v[0]["requirement"] == "35.40EUR/100kg"
    assert tc["duty"]["kind"] == "entry_price"
    d = run(taric.duty_for_item({"taric_code": "0805102210", "customs_value_cents": 500000, "net_kg": 1000},
                                origin="MA", on="2026-01-15"))
    assert d["status"] == "entry_price" and d["duty_cents"] is None and d["rate_pct"] is None
    assert "entry price" in d["reasons"][0]["en"] and d["reasons"][0]["fi"]


def test_biscuits_meursing_blocks(replay):
    d = run(taric.duty_for_item({"taric_code": "1905319100", "customs_value_cents": 100000, "net_kg": 100},
                                origin="US", on="2026-10-24"))
    assert d["status"] == "needs_agri_components" and d["duty_cents"] is None
    assert d["duty_expression"] == "9.00 % + EA(1) MAX 24.20 % +ADSZ(1)"


def test_steel_quota_and_out_of_quota_duty(replay):
    r = look("7208100000", "CN")
    q = [p for p in r["preferences"] if p["order_number"] == "099600"]
    # page: "Non preferential tariff quota (01-07-2026 - ) : 0 % (Order number: 099600) R1457/26";
    #       "Additional duties (01-07-2026 - ) : 50.00 %  R1457/26"
    assert q and q[0]["type_code"] == "122" and q[0]["duty_expression"] == "0 %" and q[0]["regulation"] == "R1457/26"
    item = {"taric_code": "7208100000", "customs_value_cents": 1000000, "net_kg": 20000}
    out = run(taric.duty_for_item(item, origin="CN", preference="100", on="2026-10-24"))
    assert [a["duty_expression"] for a in out["additional_duties"]] == ["50.00 %"]
    # anti-dumping: residual C999 0 %, countervailing residual C999 35.90 %
    assert sorted((e["type_code"], e["rate_pct"]) for e in out["trade_defence"]) == [("552", 0.0), ("554", 35.9)]
    assert out["rate_pct"] == 0 + 50 + 0 + 35.9 and out["duty_cents"] == 859000
    inq = run(taric.duty_for_item(item, origin="CN", preference="120", on="2026-10-24"))
    assert inq["measure_type"] == "122" and inq["order_number"] == "099600" and inq["additional_duties"] == []


def test_quota_099600_exhausted(replay):
    q = run(taric.quota("099600", on="2026-10-24"))
    # quota_tariff_details.jsp: period 01-10-2026 - 31-12-2026, initial amount 5564010 Kilogram, balance 0,
    # exhaustion date 01-10-2026, critical No
    assert q["period"]["start"] == "2026-10-01" and q["period"]["end"] == "2026-12-31"
    assert q["initial_amount"] == 5564010 and q["balance"] == 0 and q["unit"] == "Kilogram"
    assert q["status"] == "exhausted" and q["exhaustion_date"] == "2026-10-01" and q["critical"] is False
    assert "7208100000" in q["associated_codes"]


def test_export_to_russia_restrictions(replay):
    r = look("8471300000", "RU", flow="export")
    regs = {(m["type"], m["regulation"]) for m in r["restrictions"]}
    assert ("Export control", "R0833/14") in regs and ("Export authorization (Dual use)", "R2003/25") in regs
    du = [m for m in r["restrictions"] if m["type"] == "Export authorization (Dual use)"][0]
    certs = {c["certificate"]: c for c in du["conditions"] if c["certificate"]}
    # page: "X060 Individual Export Authorisation - Article 12(1)(a) of the Regulation (EU) 2021/821"; "Y901 Product
    # not included in the dual use list"; last line: "Import/export not allowed after control"
    assert certs["X060"]["text"].startswith("Individual Export Authorisation - Article 12(1)(a)")
    assert certs["Y901"]["text"] == "Product not included in the dual use list"
    assert du["conditions"][-1]["certificate"] is None and du["conditions"][-1]["action_kind"] == "not_allowed"
    assert not [m for m in r["measures"] if m["flow"] == "import"]


def test_cache_roundtrip(replay):
    a = run(taric.lookup("8471300000", country="CN", on="2026-10-24"))
    taric._REPLAY = {}  # nothing reachable now: the cached result must be served
    b = run(taric.lookup("8471300000", country="CN", on="2026-10-24"))
    assert b["cache"]["hit"] is True and b["third_country_duty"]["duty_expression"] == "0 %"
    with pytest.raises(taric.TaricUnavailable):
        run(taric.lookup("8471300000", country="JP", on="2026-10-24"))
    assert a["fetched_at"] == b["fetched_at"]


def test_expired_cache_is_not_served(replay):
    from core.db import db
    from datetime import datetime, timedelta, timezone
    run(taric.lookup("8471300000", country="CN", on="2026-10-24"))
    run(db.taric_cache.update_many({}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}}))
    taric._REPLAY = {}
    with pytest.raises(taric.TaricUnavailable):
        run(taric.lookup("8471300000", country="CN", on="2026-10-24"))


def test_missing_conditions_fail_closed(replay):
    taric._REPLAY = {u: p for u, p in replay.items() if "measures_conditions.jsp" not in u}
    with pytest.raises(taric.TaricUnavailable, match="Conditions"):
        look("8712003010", "CN")


@pytest.mark.parametrize("status", ["entry_price", "needs_agri_components", "unknown"])
def test_manual_percentage_cannot_clear_unresolved_taric(status):
    rules = taric.rules({"items": [{"seq": 1, "edited": ["duty_rate_pct"], "duty_rate_pct": 0,
                                    "taric": {"status": status}}]})
    assert any(r["rule"] == "TARIC-2" and not r["ok"] and r["severity"] == "blocking" for r in rules)


def declaration(code="8712003010", country="CN"):
    return {"id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "status": "draft", "lrn": "TARIC-TEST",
            "items": [{"seq": 1, "taric_code": code, "cn_code": code[:8], "hs_code": code[:6],
                       "description": "Non-motorised aluminium bicycles", "origin_country": country,
                       "invoice_amount": 10000, "quantity": 20, "unit": "p/st", "net_kg": 300, "gross_kg": 350,
                       "hs_suggestion": {"duty": {"rate_pct": 1}}, "edited": [], "documents": []}],
            "valuation": {"exchange_rate": {"rate": 1, "source": "EUR"}, "invoice_currency": "EUR",
                          "invoice_total": 10000, "freight_known": True}}


def test_declaration_duty_and_licence_validation(replay, monkeypatch):
    from services import customs
    from core.db import db
    monkeypatch.setattr(customs, "now_iso", lambda: "2026-10-24T12:00:00Z")
    d = declaration()
    run(customs.recompute(d, company={"vat_registered": True}, shipment={"direction": "import"}, settings={"x": 1}))
    it = d["items"][0]
    assert it["duty_source"] == "taric" and it["duty_cents"] == 625000
    assert it["duty_rate_pct"] == 62.5 and d["totals"]["duty_cents"] == 625000
    assert not [f for f in it["flags"] if f["kind"] == "trade_defence"]
    assert {r["rule"] for r in d["validations"] if not r["ok"]} >= {"LIC-1", "TD-1"}
    assert any(r["rule"] == "TARIC-1" and r["ok"] for r in d["validations"])
    # A real TARIC condition code from the captured page, explicitly declared with a statement.
    it["documents"] = customs._coerce_item("documents", [{"code": "Y152", "reference": "Importer declaration of origin"}])
    it["taric_additional_codes"] = ["B999"]
    run(customs.recompute(d, company={"vat_registered": True}, shipment={"direction": "import"}, settings={"x": 1}))
    assert not [r for r in d["validations"] if r["rule"] in ("TARIC-1", "TARIC-2", "LIC-1", "TD-1") and not r["ok"]]
    run(db.customs_declarations.insert_one(dict(d)))
    saved = run(db.customs_declarations.find_one({"id": d["id"]}))
    assert saved["items"][0]["duty_source"] == "taric" and saved["items"][0]["documents"][0]["code"] == "Y152"
    if os.environ.get("TARIC_EVIDENCE_DIR"):
        (Path(os.environ["TARIC_EVIDENCE_DIR"]) / "declaration-observed.json").write_text(json.dumps(d, indent=2))


def test_ai_only_on_outage_and_recovers(replay, monkeypatch):
    from services import customs
    from core.db import db
    monkeypatch.setattr(customs, "now_iso", lambda: "2026-10-24T12:00:00Z")
    run(db.taric_cache.delete_many({}))
    taric._REPLAY = {}
    d = declaration("8471300000")
    run(customs.recompute(d, company={"vat_registered": True}, shipment={"direction": "import"}, settings={"x": 1}))
    assert (d["items"][0]["duty_source"], d["items"][0]["duty_cents"]) == ("ai_estimate", 10000)
    assert any(r["rule"] == "TARIC-1" and not r["ok"] and r["severity"] == "blocking" for r in d["validations"])
    taric._REPLAY = replay
    run(customs.recompute(d, company={"vat_registered": True}, shipment={"direction": "import"}, settings={"x": 1}))
    assert (d["items"][0]["duty_source"], d["items"][0]["duty_cents"]) == ("taric", 0)
    assert not [r for r in d["validations"] if r["rule"] in ("TARIC-1", "LIC-1") and not r["ok"]]


ACTOR = {"id": "taric-broker", "name": "TARIC test broker", "org_type": "internal", "role": "customs_broker"}


def test_official_certificate_descriptions(replay):
    from services import customs_licences as licences
    reqs = run(licences.requirements({"taric_code": "8471300000"}, flow="export", country="RU", on="2026-10-24"))
    dual = next(r for r in reqs if r["certificate_code"] == "X060")
    assert dual["kind"] == "licence" and "Article 12(1)(a)" in dual["text_en"]
    assert dual["text_fi"] and "2021/821" in dual["text_fi"]
    assert any(a["code"] == "Y901" and a["kind"] == "exemption" for a in dual["alternatives"])


def test_licence_register_balance_validity_idempotency(replay):
    from services import customs_licences as licences
    from core.db import db
    from fastapi import HTTPException
    d = declaration("8471300000", "CN")
    d.update(flow="export", destination_country="RU")
    d["items"][0]["net_kg"] = 3
    reqs = run(licences.requirements(d["items"][0], flow="export", country="RU", on="2026-10-24"))
    rows = []
    for req in reqs:
        rows.append(run(licences.create_licence({"company_id": d["company_id"], "certificate_code": req["certificate_code"],
            "number": "AUTH-" + req["certificate_code"], "quantity_total": 5, "unit": "kg", "goods": ["8471"],
            "valid_from": "2026-10-01", "valid_to": "2026-10-31"}, ACTOR)))
    run(licences.attribute(d, on="2026-10-24"))
    assert all(r["satisfied_by"] for r in d["items"][0]["licence_requirements"])
    assert len(d["items"][0]["documents"]) == len(reqs)
    from copy import deepcopy
    second = deepcopy(d["items"][0])
    second["seq"] = 2
    d["items"].append(second)
    with pytest.raises(HTTPException, match="LIC-1"):
        run(licences.attribute(d, consume=True, on="2026-10-24"))
    assert all(run(db.customs_licences.find_one({"id": row["id"]}))["quantity_balance"] == 5 for row in rows)
    d["items"].pop()
    result = run(licences.attribute(d, consume=True, on="2026-10-24"))
    assert len(result["consumed"]) == len(reqs)
    assert run(licences.attribute(d, consume=True, on="2026-10-24"))["consumed"] == []
    for row in rows:
        saved = run(db.customs_licences.find_one({"id": row["id"]}))
        assert saved["quantity_balance"] == 2 and len(saved["usage"]) == 1
    # Another declaration cannot use the first declaration's allocation.
    d["id"] = str(uuid.uuid4())
    with pytest.raises(HTTPException, match="LIC-1"):
        run(licences.attribute(d, consume=True, on="2026-10-24"))
    run(licences.attribute(d, on="2026-11-01"))
    assert any(not r["ok"] for r in licences.rules(d))
    with pytest.raises(HTTPException):
        run(licences.update_licence(rows[0]["id"], {"quantity_balance": 99}, ACTOR))
    assert run(licences.delete_licence(rows[0]["id"], ACTOR))["status"] == "revoked"
    exp = run(licences.create_licence({"company_id": "expired-case", "certificate_code": "X060", "number": "EXPIRED",
                                      "valid_from": "2026-01-01", "valid_to": "2026-10-23"}, ACTOR))
    hit, why = run(licences._register_match(exp["company_id"], ["X060"], d["items"][0], "2026-10-24", d["id"]))
    assert hit is None and "not valid on 2026-10-24" in why


def test_licence_atomic_race_and_invalid_values():
    from services import customs_licences as licences
    from core.db import db
    from fastapi import HTTPException
    lic = run(licences.create_licence({"company_id": "race", "certificate_code": "X060", "number": "RACE",
                                     "quantity_total": 5, "unit": "kg"}, ACTOR))
    async def compete():
        return await asyncio.gather(*[licences._consume(lic, {"seq": 1, "net_kg": 3}, {"id": f"race-{i}"})
                                      for i in range(2)], return_exceptions=True)
    results = run(compete())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, HTTPException) for r in results) == 1
    assert run(db.customs_licences.find_one({"id": lic["id"]}))["quantity_balance"] == 2
    for quantity in (-1, float("nan"), "invalid"):
        with pytest.raises(HTTPException):
            run(licences.create_licence({"company_id": "race", "certificate_code": "X060", "number": "BAD",
                                        "quantity_total": quantity, "unit": "kg"}, ACTOR))


def test_refresh_job_registered_and_refreshes_open_codes(replay):
    from core.db import db
    from core.jobs import registered_jobs, registered_schedules
    run(db.customs_declarations.delete_many({}))
    run(db.customs_declarations.insert_one({"id": "refresh-open", "status": "draft",
        "items": [{"taric_code": "8471300000", "origin_country": "CN"}] * 2}))
    run(db.customs_declarations.insert_one({"id": "refresh-closed", "status": "closed",
        "items": [{"taric_code": "9999999999", "origin_country": "CN"}]}))
    result = run(taric.refresh_open("2026-10-24"))
    assert result == {"codes": 1, "refreshed": 1, "failed": []}
    assert "taric.refresh" in registered_jobs()
    assert registered_schedules()["taric.refresh_daily"]["daily_at"] == "05:15"


def test_reference_api_authenticated_routes(replay):
    import httpx
    from server import app
    from core.db import db
    from core.security import create_session_token, load_secret
    async def check():
        await load_secret(db)
        await db.users.update_one({"id": ACTOR["id"]}, {"$set": ACTOR}, upsert=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://taric.test") as c:
            assert (await c.get("/api/customs/taric?code=8471300000")).status_code == 401
            c.headers["Authorization"] = "Bearer " + create_session_token(ACTOR["id"])
            r = await c.get("/api/customs/taric?code=8471300000&country=CN&date=2026-10-24")
            assert r.status_code == 200 and r.json()["third_country_duty"]["duty_expression"] == "0 %"
            r = await c.get("/api/customs/taric/quota/099600?date=2026-10-24")
            assert r.status_code == 200 and r.json()["status"] == "exhausted"
            assert (await c.get("/api/customs/taric?code=8471300000&date=2026-02-31")).status_code == 422
            r = await c.post("/api/customs/licences", json={"company_id": "api", "certificate_code": "X060", "number": "API-1"})
            assert r.status_code == 201
            lid = r.json()["id"]
            assert (await c.patch("/api/customs/licences", json={"id": lid, "note": "checked"})).status_code == 200
            r = await c.get("/api/customs/licences?company_id=api")
            assert r.status_code == 200 and r.json()["items"][0]["note"] == "checked"
            assert (await c.delete("/api/customs/licences/" + lid)).json()["status"] == "revoked"
            await db.users.update_one({"id": ACTOR["id"]}, {"$set": {"org_type": "customer", "role": "customer_admin"}})
            assert (await c.get("/api/customs/licences")).status_code == 403
            await db.users.update_one({"id": ACTOR["id"]}, {"$set": ACTOR})
    run(check())


# ---------------------------------------------------------------------------------------------
# live (skips when the consultation cannot be reached)
# ---------------------------------------------------------------------------------------------
LIVE = pytest.mark.skipif(os.environ.get("TARIC_LIVE") == "0", reason="TARIC_LIVE=0: live EU TARIC tests switched off")


def _live_or_skip(coro):
    try:
        return run(coro)
    except taric.TaricUnavailable as e:
        pytest.skip(f"EU TARIC consultation unreachable: {e}")


@LIVE
def test_live_bicycles_today():
    r = _live_or_skip(taric.lookup("8712003010", country="CN", flow="import", use_cache=False))
    assert r["third_country_duty"] and r["third_country_duty"]["duty"]["kind"] == "ad_valorem"
    assert any(m["type_code"] == "552" for m in r["measures"])
    assert any(a["residual"] for m in r["trade_defence"] for a in m["additional_codes"] or [])
    if os.environ.get("TARIC_EVIDENCE_DIR"):
        (Path(os.environ["TARIC_EVIDENCE_DIR"]) / "live-bicycles.json").write_text(json.dumps(r, indent=2))


@LIVE
def test_live_quota():
    q = _live_or_skip(taric.quota("099600"))
    assert q["periods"] and q["source_url"].startswith(taric.BASE)
    if os.environ.get("TARIC_EVIDENCE_DIR"):
        (Path(os.environ["TARIC_EVIDENCE_DIR"]) / "live-quota.json").write_text(json.dumps(q, indent=2))
