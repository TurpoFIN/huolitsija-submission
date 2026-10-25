"""Sanctions screening against the real EU, UN and OFAC lists (no mocks, real downloads).

Runs against its own database (huo_v3_test_screening) when it is the first module to import core.
The first run downloads the three lists (about 1-2 minutes); later runs within 24 h reuse the cache,
except test_forced_refresh_replaces_version which re-downloads the small UN list on purpose.

Empty input: screen_name raises ValueError (a party without a name cannot be screened; the caller
must ask for the name instead of recording "clear").
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if "core.db" not in sys.modules:
    os.environ.setdefault("DB_NAME", "huo_v3_test_screening")

# Motor binds its client to the event loop that is current at import time; use one loop for the module.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import pytest  # noqa: E402

from core.db import db  # noqa: E402
from services import screening  # noqa: E402


def run(coro):
    return LOOP.run_until_complete(coro)


DEMO_COMPANIES = ["Pohjolan Kaluste Oy", "Nordic Bike Parts Oy", "Saimaan Kahvipaahtimo Oy", "Arktinen Elektroniikka Oy",
                  "Kymen Teräskauppa Oy", "Lapin Retkivaruste Oy", "Turun Venetarvike Oy", "Helsinki Design House Oy"]


@pytest.fixture(scope="module", autouse=True)
def lists_loaded():
    status = run(screening.refresh_lists())
    return {s["list"]: s for s in status}


def test_lists_load_with_plausible_counts(lists_loaded):
    for key, minimum in (("eu", 1000), ("un", 500), ("ofac", 5000)):
        s = lists_loaded[key]
        assert s["loaded"], f"{key} not loaded: {s['error']}"
        assert s["status"] == "ok", s["error"]
        assert s["entries"] > minimum, (key, s["entries"])
        assert s["fetched_at"] and s["sha256"] and s["source_url"].startswith("https://")
        n = run(db.sanctions_entries.count_documents({"list": key, "version": s["version"]}))
        assert n == s["entries"]
    assert lists_loaded["eu"]["generated_at"] and lists_loaded["un"]["generated_at"]


def test_second_refresh_within_24h_does_not_download(lists_loaded):
    again = {s["list"]: s for s in run(screening.refresh_lists())}
    for key in ("eu", "un", "ofac"):
        assert again[key]["fetched_at"] == lists_loaded[key]["fetched_at"]
        assert again[key]["version"] == lists_loaded[key]["version"]


@pytest.mark.parametrize("name,ref_list", [
    ("Rosneft Oil Company", None), ("Sovcomflot", None), ("Bank Rossiya", "eu"), ("Banco Nacional de Cuba", "ofac"),
    ("Islamic Revolutionary Guard Corps", None), ("Rosneft Oil Company Finland Oy", None), ("AlQaida", None),
    ("Rosneft Finland Oy", None), ("Sovcomflot Europe Ltd", None),
])
def test_known_sanctioned_entities_are_potential_matches(name, ref_list):
    r = run(screening.screen_name(name, kind="entity"))
    assert r["status"] == "potential_match", r
    assert r["complete"] is True
    top = r["hits"][0]
    assert top["score"] >= screening.MATCH_THRESHOLD
    assert top["ref"] and top["ref"] in top["explanation"]
    assert top["list_label"] in top["explanation"]
    if ref_list:
        assert any(h["list"] == ref_list for h in r["hits"]), r["hits"]


@pytest.mark.parametrize("name", ["Sergey Shoygu", "Sergei Shoigu", "Evgeny Prigozhin", "Arkadiy Rotenberg"])
def test_transliterated_person_names_hit(name):
    r = run(screening.screen_name(name, kind="person"))
    assert r["status"] == "potential_match", r
    assert r["hits"][0]["type"] == "person"


@pytest.mark.parametrize("name", ["Helios One Oy", *DEMO_COMPANIES, "International Bank Oy", "Nordea Bank Abp",
                                  "Kesko Oyj", "Hapag-Lloyd AG", "Maersk Finland Oy", "Hapag-Lloyd Finland Oy",
                                  "Schenker Finland Oy", "Nokia Solutions Europe Oy", "Wartsila Finland Oy"])
def test_normal_finnish_and_trade_names_are_clear(name):
    r = run(screening.screen_name(name, kind="entity", country="FI"))
    assert r["status"] == "clear", r["hits"]
    assert r["hits"] == []
    assert r["complete"] is True and r["top_score"] < screening.MATCH_THRESHOLD


@pytest.mark.parametrize("name", ["", "   ", "---"])
def test_empty_name_is_rejected(name):
    with pytest.raises(ValueError):
        run(screening.screen_name(name))


def test_screen_parties_reports_worst_status():
    r = run(screening.screen_parties([
        {"name": "Pohjolan Kaluste Oy", "kind": "entity", "country": "FI", "role": "consignee"},
        {"name": "Sovcomflot", "kind": "entity", "role": "carrier"},
    ]))
    assert r["status"] == "potential_match"
    roles = {x["role"]: x["status"] for x in r["results"]}
    assert roles == {"consignee": "clear", "carrier": "potential_match"}


def test_screening_is_fast_once_index_is_warm():
    run(screening.screen_name("warm up the index"))
    names = DEMO_COMPANIES + ["Mohammed Ali Hassan", "Islamic Revolutionary Guard Corps", "Global Trading International Ltd",
                              "Abu Bakr al-Baghdadi", "Rosneft Oil Company", "Sergey Shoygu", "Oy Holding Company Ab"]
    times = []
    for n in names:
        t = time.perf_counter()
        run(screening.screen_name(n))
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    assert times[len(times) // 2] < 150, times
    assert times[-1] < 600, times


def test_forced_refresh_replaces_version_and_drops_old_entries(lists_loaded):
    before = lists_loaded["un"]
    after = {s["list"]: s for s in run(screening.refresh_lists(force=True, only=["un"]))}["un"]
    assert after["status"] == "ok"
    assert after["version"] != before["version"]
    assert run(db.sanctions_entries.count_documents({"list": "un", "version": before["version"]})) == 0
    assert run(db.sanctions_entries.count_documents({"list": "un"})) == after["entries"]
    r = run(screening.screen_name("Abu Bakr al-Baghdadi", kind="person"))
    assert r["status"] == "potential_match"
