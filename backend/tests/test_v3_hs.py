"""HS 2022 classification: real nomenclature data, real retrieval, real model call (no mocks).

The AI test runs only when OPENAI_API_KEY is set; otherwise it is skipped with the reason.
Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_hs.py
"""
import asyncio
import os
import re
import sys

os.environ["DB_NAME"] = "huo_v3_test_hs"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Motor binds to the event loop that exists when core.db is imported; create it first and reuse it.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import pytest  # noqa: E402

from services import hs  # noqa: E402

HAVE_AI = bool(os.environ.get("OPENAI_API_KEY"))


def test_nomenclature_loaded():
    hs._load()
    n6 = sum(1 for c in hs._ENTRIES if len(c) == 6)
    assert n6 > 5000
    # HS 2022 additions exist (8524 flat panel modules, 8549 e-waste, 9405 21 LED desk lamps).
    for code in ("8524", "8549", "940521"):
        assert hs.hs_entry(code), code
    # UN Comtrade aggregates are not HS codes.
    assert hs.hs_entry("999999") is None
    e = hs.hs_entry("9401610000")
    assert e["code"] == "940161" and e["heading"] == "9401" and e["chapter"] == "94"
    assert "wooden" in e["description"].lower() and e["section"] == "XX" and e["section_name"]


@pytest.mark.parametrize("query,expected", [
    ("Solid oak dining chair, upholstered seat", "940161"),
    ("Green arabica coffee beans, not roasted", "090111"),
    ("Laptop computer 14 inch", "847130"),
    ("Lithium-ion battery pack 48V for e-bike", "850760"),
    ("Puiset ruokapöydän tuolit, verhoiltu istuin", "940161"),
])
def test_search_recall(query, expected):
    codes = [c["code"] for c in hs.search(query, 20)]
    assert expected in codes, (query, codes[:10])


def test_code_lookup():
    assert [c["code"] for c in hs.search("9401 61")] == ["940161"]
    heading = [c["code"] for c in hs.search("9403")]
    assert "940360" in heading and all(c.startswith("9403") for c in heading)
    assert hs.search("") == []


def _kinds(flags):
    return {f["kind"]: f for f in flags}


def test_flags():
    steel = _kinds(hs.flags_for("72083900"))
    assert steel["cbam"]["rule"] == "B-1"
    assert "cbam" in _kinds(hs.flags_for("310210"))
    assert "cbam" in _kinds(hs.flags_for("760612"))
    assert "cbam" not in _kinds(hs.flags_for("720421"))  # ferrous scrap is excluded from Annex I
    assert "cbam" not in _kinds(hs.flags_for("940161"))
    assert "possible" in _kinds(hs.flags_for("250700"))["cbam"]["message"].lower()  # only CN 2507 00 80
    assert _kinds(hs.flags_for("854370"))["dual_use"]["rule"] == "K-3"
    ru = _kinds(hs.flags_for("940161", "RU"))["ru_by_origin"]
    assert ru["severity"] == "blocking" and ru["rule"] == "K-2"
    assert "ru_by_origin" in _kinds(hs.flags_for(None, "by"))
    assert _kinds(hs.flags_for("930100"))["military"]["severity"] == "blocking"
    assert _kinds(hs.flags_for("123456"))["invalid_code"]["rule"] == "H-1"


def test_taric_url():
    url = hs.taric_url("94016100", "CN", "2026-10-24")
    assert url.startswith("https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp?")
    assert "Taric=9401610000" in url and "SimDate=20261024" in url and "Area=CN" in url
    assert "Area=" not in hs.taric_url("940161")
    assert re.search(r"SimDate=\d{8}", hs.taric_url("940161"))


def test_vague_and_empty_need_a_person_without_ai_call():
    res = LOOP.run_until_complete(hs.classify_items([{"description": "General cargo"}, {"description": ""}]))
    assert all(r["status"] == "needs_review" and r["hs6"] is None and r["reason"] for r in res)
    assert "S-2" in res[0]["reason"]


@pytest.mark.skipif(not HAVE_AI, reason="No AI key set (OPENAI_API_KEY): model call not tested")
def test_suggest_with_model():
    r = LOOP.run_until_complete(hs.suggest("Men's cotton knitted T-shirts, crew neck", "BD",
                                           hs_hint="6109.10.00", ref={"type": "test"}))
    assert r["source"] == "ai" and r["model"]
    assert r["hs6"] == "610910", r
    assert r["status"] == "ok" and 0 <= r["confidence"] <= 1
    assert r["cn8"] is None or (hs.cn_entry(r["cn8"]) and r["cn8"].startswith(r["hs6"]))
    assert r["hs_hint_check"]["valid"] and r["hs_hint_check"]["agrees"] is True
    assert r["duty"]["source"] == "ai_estimate" and "not authoritative" in r["duty"]["label"]
    assert "Taric=6109" in r["taric_url"] and "Area=BD" in r["taric_url"]
    assert r["candidates"][0]["code"] == "610910" and len(r["candidates"]) >= 3


# ---- CN 2026 nomenclature (Eurostat Comext CN.txt, codes valid for the whole of 2026) -----------------------------
@pytest.mark.parametrize("code", ["87149950", "85121000", "87149420", "87149490", "73151110", "94016100", "94036010"])
def test_cn2026_codes_present(code):
    e = hs.cn_entry(code)
    assert e and e["hs6"] == code[:6] and e["description"]


@pytest.mark.parametrize("code", ["87149620", "85121035", "87149410", "94016199", "9401610", ""])
def test_invented_or_historical_cn_codes_absent(code):
    assert hs.cn_entry(code) is None  # 87149410 existed until 2011; the others were never CN codes


def test_taric_code_maps_to_its_cn8():
    assert hs.cn_entry("9401 61 00 00")["code"] == "94016100"


def test_cn2026_matches_hs2022():
    hs._load()
    assert len(hs._CN) == 9793
    hs6 = [c for c in hs._ENTRIES if len(c) == 6]
    assert all(hs._CN_BY_HS6.get(c) for c in hs6)  # every HS 2022 subheading has CN 2026 subdivisions
    assert [o["code"] for o in hs.cn_options("871499")] == ["87149910", "87149930", "87149950", "87149990"]


def test_resolve_cn_never_returns_an_invented_code():
    r = hs.resolve_cn("871499", "87149620")       # wrong HS6 and not a CN code
    assert r["cn8"] is None and r["resolved"] == "choose" and len(r["cn_options"]) == 4
    assert "not a CN 2026 code" in r["note"]
    r = hs.resolve_cn("851210", "85121035")       # single subdivision: resolves to it, says why
    assert r["cn8"] == "85121000" and r["resolved"] == "single"
    r = hs.resolve_cn("871494", "87149410")       # historical code
    assert r["cn8"] is None and {o["code"] for o in r["cn_options"]} == {"87149420", "87149490"}
    assert hs.resolve_cn("731511", "73151110") == {"cn8": "73151110", "cn_options": hs.cn_options("731511"),
                                                    "note": None, "resolved": "valid"}
    assert hs.resolve_cn("731511", "94016100")["cn8"] is None  # real code, wrong subheading


def test_apply_answer_drops_invented_cn():
    p = hs._prepare({"description": "LED bicycle front light", "origin_country": "CN"})
    hs._ground([p])
    base = hs._base_result(p)
    r = hs._apply_answer(base, p, {"hs6": "851210", "cn8": "85121035", "confidence": 0.9}, p["shown"], "test")
    assert r["cn8"] == "85121000" and r["cn8_proposed"] == "85121035" and r["cn_resolution"] == "single"
    r = hs._apply_answer(base, p, {"hs6": "871499", "cn8": "87149620", "confidence": 0.9}, p["shown"], "test")
    assert r["cn8"] is None and len(r["cn_options"]) == 4 and "Choose the CN subdivision" in r["cn8_note"]


@pytest.mark.parametrize("proposed", [None, "87149420", "87149490"])
@pytest.mark.parametrize("insufficient", [False, True])
def test_missing_goods_facts_cannot_select_a_cn_subdivision(proposed, insufficient):
    p = hs._prepare({"description": "Hydraulic disc brake set, front+rear", "origin_country": "CN"})
    hs._ground([p])
    r = hs._apply_answer(hs._base_result(p), p, {
        "hs6": "871494", "cn8": proposed, "confidence": 0.99,
        "insufficient_description": insufficient,
        "missing_info": "Confirm vehicle type and complete brakes versus parts.",
    }, p["shown"], "test")
    assert r["status"] == "needs_review"
    assert "Confirm vehicle type" in r["reason"]
    assert r["cn8"] is None and r["cn8_description"] is None
    assert r["cn_resolution"] == "choose"
    assert {o["code"] for o in r["cn_options"]} == {"87149420", "87149490"}
    assert r["cn8_proposed"] == proposed  # retained as a proposal, never a selected code
    from services.customs import apply_hs_result
    item = {}
    apply_hs_result(item, r)
    assert item["cn_code"] is None and item["cn_confirmed"] is False
    assert item["hs_suggestion"]["cn8_proposed"] == proposed


def test_close_call_and_low_confidence_go_to_a_broker():
    p = hs._prepare({"description": "Bicycle rear derailleur", "origin_country": "CN"})
    hs._ground([p])
    base = hs._base_result(p)
    r = hs._apply_answer(base, p, {"hs6": "871499", "cn8": "87149950", "confidence": 0.7}, p["shown"], "test")
    assert r["status"] == "needs_review" and "below 0.80" in r["reason"]
    r = hs._apply_answer(base, p, {"hs6": "871499", "cn8": "87149950", "confidence": 0.85,
                                   "alternatives": [{"hs6": "871496", "confidence": 0.75}]}, p["shown"], "test")
    assert r["status"] == "needs_review" and "Close call" in r["reason"]
    r = hs._apply_answer(base, p, {"hs6": "871499", "cn8": "87149950", "confidence": 0.9,
                                   "alternatives": [{"hs6": "871496", "confidence": 0.05}]}, p["shown"], "test")
    assert r["status"] == "ok"


def test_retrieval_finds_goods_named_only_in_cn_texts():
    # "Derailleur" appears only in the CN 2026 text of 8714 99 50, not in the HS 2022 subheading text
    assert hs.search("Bicycle rear derailleur, 11-speed, aluminium/steel", 5)[0]["code"] == "871499"


def test_sibling_subheadings_and_invoice_context_are_shown():
    items = [hs._prepare({"description": d, "origin_country": "CN"}) for d in VELOTEK]
    hs._ground(items)
    brake = items[3]
    assert "8714" in brake["headings"] and {"871491", "871494", "871496", "871499"} <= brake["shown"]
    assert any("derailleur" in c.lower() for c in brake["context"])
    block = hs._item_block(3, brake)
    assert "== 8714" in block and "CN: 87149420" in block


def test_bicycle_light_candidate_includes_its_cn_reference():
    # R7 real replies reported missing reference data: this valid HS candidate
    # was below the first four residual matches and lost its CN subdivision.
    items = [hs._prepare({"description": d, "origin_country": "CN"}) for d in VELOTEK]
    hs._ground(items)
    light = items[2]
    assert "851210" in light["shown"]
    block = hs._item_block(2, light)
    assert "CN: 85121000 " in block
    assert hs.cn_entry("85121000")["description"][:100] in block


# ---- trade defence (anti-dumping / countervailing), advisory only ---------------------------------------------
def test_trade_defence_flags():
    f = _kinds(hs.flags_for("871499", "CN"))["trade_defence"]
    assert f["scope"] == "possible" and "71/97" in f["reference"] and "does not include" in f["message"]
    assert _kinds(hs.flags_for("87149950", "CN"))["trade_defence"]["scope"] == "covered"
    assert "trade_defence" not in _kinds(hs.flags_for("871499", "TW"))
    assert "trade_defence" not in _kinds(hs.flags_for("87149930", "CN"))   # luggage carriers: not an essential part
    assert "trade_defence" not in _kinds(hs.flags_for("73151110", "CN"))   # chains are not in Reg. 71/97
    assert _kinds(hs.flags_for("87120030", "MY"))["trade_defence"]["message"].find("consigned from Malaysia") > 0
    assert "2022/191" in _kinds(hs.flags_for("73181588", "CN"))["trade_defence"]["reference"]
    assert _kinds(hs.flags_for("73181595", "CN"))["trade_defence"]["scope"] == "partly"
    assert "trade_defence" not in _kinds(hs.flags_for("76082020", "CN"))   # welded tubes are outside Reg. 2021/546
    assert all("rate_pct" not in f for f in hs.flags_for("87116010", "CN"))


# ---- live model: the Velotek invoice lines, twice --------------------------------------------------------------
VELOTEK = ["Bicycle rear derailleur, 11-speed, aluminium/steel", "Bicycle chain, 11-speed, nickel-plated steel, 116 links",
           "LED bicycle front light, USB rechargeable, 800 lm", "Hydraulic disc brake set, front+rear"]
VELOTEK_HS6 = ["871499", "731511", "851210", "871494"]


@pytest.mark.skipif(not HAVE_AI, reason="No AI key set (OPENAI_API_KEY): model call not tested")
def test_velotek_lines_classified_correctly_and_stably():
    runs = []
    for _ in range(2):
        res = LOOP.run_until_complete(hs.classify_items([{"description": d, "origin_country": "CN"} for d in VELOTEK],
                                                        ref={"type": "test"}))
        runs.append([(r["hs6"], r["cn8"], r["status"]) for r in res])
        for r in res:
            assert r["cn8"] is None or hs.cn_entry(r["cn8"]), r["cn8"]   # never an invented CN code
        assert [r["hs6"] for r in res] == VELOTEK_HS6, runs
        assert res[0]["cn8"] == "87149950"
        assert "trade_defence" in _kinds(res[0]["flags"]) and "trade_defence" not in _kinds(res[1]["flags"])
    # decided lines are stable to the CN8; a line sent to a broker (needs_review) is stable in HS6 and status, while the
    # model's CN8 suggestion for it may differ between calls (real model, no fixed seed)
    stable = [[(h, c if st == "ok" else None, st) for h, c, st in run] for run in runs]
    assert stable[0] == stable[1], runs
