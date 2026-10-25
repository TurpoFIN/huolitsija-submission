"""s11 EBTI: official EU Binding Tariff Information as the second classification signal.

Real data only: backend/tests/fixtures/ebti_subset.csv is cut from the Commission's full EBTI download by
scripts/s11_ebti_fixture.py (holder names removed), and the live tests download today's official daily update file.
The expectations are computed from the CSV with the csv module, not from services.ebti's own parser.

In process against a throw-away MongoDB database (own database, dropped at the end)."""
import csv
import os
import re
import uuid
from datetime import date, datetime

os.environ["DB_NAME"] = f"huo_s11_ebti_{uuid.uuid4().hex[:8]}"
import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s11_ebti.py in its own pytest process", allow_module_level=True)

from core.db import client, db  # noqa: E402
from services import ebti  # noqa: E402
from services.agents.customs_agent import item_confidence  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ebti_subset.csv")
TODAY = date.today().isoformat()
csv.field_size_limit(10 ** 9)


def fixture_rows() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _day(v: str) -> str:
    v = v.strip()
    fmt = "%d/%m/%Y" if "/" in v[:6] else "%Y-%m-%d"  # the full export writes 16/10/2026, daily files 2026-10-16 00:00:00
    return datetime.strptime(v[:10], fmt).date().isoformat()


def current(row: dict) -> bool:
    end, start = _day(row["END_DATE_OF_VALIDITY"]), _day(row["START_DATE_OF_VALIDITY"])
    return row["STATUS"] == "VALID" and end >= TODAY and start <= TODAY


@pytest.fixture(scope="module", autouse=True)
def loaded():
    rows = fixture_rows()
    stats = run(ebti.load_rows(rows))
    yield stats
    run(client.drop_database(os.environ["DB_NAME"]))


def test_only_current_rulings_are_kept_and_holder_data_is_dropped(loaded):
    rows = fixture_rows()
    want = {r["BTI_REFERENCE"] for r in rows if current(r)}
    assert len(want) > 100
    got = set(run(db.ebti_rulings.distinct("ref")))
    assert got == want
    one = run(db.ebti_rulings.find_one({}, {"_id": 0}))
    assert "NAME_AND_ADDRESS" not in one and "name_and_address" not in one and "holder" not in one


def test_by_code_matches_the_official_rows():
    rows = [r for r in fixture_rows() if current(r)]
    for heading in ("8471", "9403", "6403"):
        want = {r["BTI_REFERENCE"] for r in rows if r["NOMENCLATURE_CODE"].startswith(heading)}
        res = run(ebti.by_code(heading, limit=200))
        assert res["count"] == len(want) and {x["ref"] for x in res["rulings"]} == want, heading
    # a real English ruling from the Irish customs administration: portable ADP machine (laptop) under 8471 30 00
    r = run(ebti.get("IEBTIIENEN004-2026-BTI160"))
    assert r and r["code"] == "8471300000" and r["country"] == "IE" and "portable ADP machine" in r["description"]
    assert r["url"].startswith("https://ec.europa.eu/taxation_customs/dds2/ebti/ebti_details.jsp")
    eight = run(ebti.by_code("84713000"))
    assert any(x["ref"] == "IEBTIIENEN004-2026-BTI160" for x in eight["rulings"])


def test_an_invalidated_ruling_is_removed_on_the_next_update():
    row = next(r for r in fixture_rows() if current(r) and r["NOMENCLATURE_CODE"].startswith("6403"))
    assert run(ebti.get(row["BTI_REFERENCE"]))
    stats = run(ebti.load_rows([{**row, "STATUS": "INVALID"}]))
    assert stats["removed"] == 1 and run(ebti.get(row["BTI_REFERENCE"])) is None
    run(ebti.load_rows([row]))  # put it back for the other tests
    assert run(ebti.get(row["BTI_REFERENCE"]))


def test_agreement_and_customs_agent_confidence():
    bti = {"ref": "IEBTIIENEN004-2026-BTI160", "code": "8471300000", "country": "IE", "valid_to": "2029-07-13",
           "url": "x", "why": "both are laptops"}
    item = {"description": "Laptop computer 14 inch", "hs_code": "847130", "cn_code": "84713000",
            "hs_suggestion": {"source": "ai", "status": "ok", "confidence": 0.7, "ebti": {"match": bti}}}
    verdict, why = ebti.agreement(item)
    assert verdict == "agrees" and "IEBTIIENEN004-2026-BTI160" in why
    conf, reason = item_confidence(item)
    assert conf >= 0.95 and "BTI" in reason  # AI 0.70 alone is below the default floor; the BTI lifts it
    other = {**item, "cn_code": "84714100", "hs_code": "847141"}
    verdict, why = ebti.agreement(other)
    assert verdict == "disagrees" and "not 84714100" in why
    conf, reason = item_confidence(other)
    assert conf == 0.5 and "not 84714100" in reason
    plain = {**item, "hs_suggestion": {"source": "ai", "status": "ok", "confidence": 0.7}}
    assert ebti.agreement(plain) is None and item_confidence(plain)[0] == 0.7


def test_precedent_shows_only_real_rulings_without_a_model():
    from core import llm
    res = run(ebti.precedent({"description": "Notebook computer"}, ["84713000", "847141"]))
    assert res["codes"]["84713000"] >= 2 and res["codes"]["847141"] == run(ebti.by_code("847141"))["count"]
    assert res["shown"] and all(run(ebti.get(ref)) for ref in res["shown"])
    if not llm.llm_available():
        assert res["status"] == "no_model" and res["match"] is None
    else:
        assert res["status"] in ("match", "no_match", "error")
        if res["match"]:
            assert res["match"]["ref"] in res["shown"]


def test_unknown_code_has_no_rulings():
    res = run(ebti.precedent({"description": "x"}, ["01012100"]))
    assert res["status"] == "no_rulings" and res["shown"] == []


@pytest.mark.skipif(os.environ.get("S11_OFFLINE") == "1", reason="offline run")
def test_live_daily_update_from_the_commission():
    files = run(ebti.daily_files())
    assert files, "no daily EBTI files listed"
    newest = files[0]
    assert re.fullmatch(r"EBTI_\d{8}_\d{6}\.zip", newest["file"])
    assert newest["published"][:10] >= "2026-10-01"
    import io
    buf = io.BytesIO()
    n = run(ebti._download(newest["url"], buf))
    assert n > 1000
    rows = list(ebti.iter_zip(buf.getvalue()))
    assert rows and all(re.match(r"^[A-Z]{2}", r["BTI_REFERENCE"]) for r in rows)
    stats = run(ebti.load_rows(rows))
    assert stats["rows"] == len(rows)
    fresh = [r for r in rows if current(r)]
    if fresh:
        assert run(ebti.get(fresh[0]["BTI_REFERENCE"]))["code"] == ebti._code(fresh[0]["NOMENCLATURE_CODE"])


def test_full_rebuild_swaps_in_only_current_rulings():
    """Runs last: the full reload rebuilds the collection from the official rows and swaps it in."""
    rows = fixture_rows()
    run(db.ebti_rulings.insert_one({"ref": "STALE-REF", "code": "0101210000", "status": "VALID", "valid_to": "2099-01-01"}))
    stats = run(ebti.rebuild(rows))
    want = {r["BTI_REFERENCE"] for r in rows if current(r)}
    assert stats["rows"] == len(rows) and stats["kept"] == len(want)
    assert set(run(db.ebti_rulings.distinct("ref"))) == want  # the stale row is gone with the old collection
    idx = run(db.raw.ebti_rulings.index_information())
    assert any(v["key"] == [("ref", 1)] and v.get("unique") for v in idx.values())


def test_overlapping_full_rebuilds_never_swap_in_a_partial_set():
    """A redeploy can leave a killed run's job to be taken over while a manual run is loading: both must end with the
    complete official set, and staging left by a long-dead run is dropped."""
    rows = fixture_rows()
    want = {r["BTI_REFERENCE"] for r in rows if current(r)}
    dead = f"{ebti.STAGING}_{int(__import__('time').time()) - ebti.STAGING_ORPHAN_SECONDS - 60}_dead00"
    run(db.raw[dead].insert_one({"ref": "ORPHAN"}))
    run(db.raw[ebti.STAGING].insert_one({"ref": "LEGACY"}))  # the old fixed staging name
    a, b = run(asyncio.gather(ebti.rebuild(rows), ebti.rebuild(rows)))
    assert a["kept"] == b["kept"] == len(want)
    assert set(run(db.ebti_rulings.distinct("ref"))) == want
    left = run(db.raw.list_collection_names(filter={"name": {"$regex": f"^{ebti.STAGING}"}}))
    assert dead not in left and ebti.STAGING not in left and left == []
