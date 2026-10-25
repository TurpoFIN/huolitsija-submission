"""r3 N2: an HS batch failed whole ("AI classification failed (Missing bilingual preference_note)") when the model left
out one optional {fi, en} pair. The model answer here is a real hs_classifier answer to the bilingual prompt
(fixtures/hs_answer_bilingual_vn_furniture.json); the tests remove pairs the way the live answer did."""
import asyncio
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Motor is bound when services import core.db; all three cases use that same loop.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from services import hs  # noqa: E402


def teardown_module():
    from core.db import client
    client.close()
    LOOP.close()

FIXTURE = json.loads((Path(__file__).parent / "fixtures/hs_answer_bilingual_vn_furniture.json").read_text())


def classify(monkeypatch, answer):
    async def model(*_a, **_k):
        return copy.deepcopy(answer)
    monkeypatch.setattr(hs, "llm_json", model)
    return LOOP.run_until_complete(hs.classify_items(copy.deepcopy(FIXTURE["items"])))


def test_the_captured_answer_classifies_every_item(monkeypatch):
    res = classify(monkeypatch, FIXTURE["answer"])
    assert [r.get("cn8") for r in res] == ["94016100", "94036010", "94036010"]
    assert all(not str(r.get("reason") or "").startswith("AI classification failed") for r in res)


def test_a_missing_optional_note_pair_does_not_reject_the_batch(monkeypatch):
    answer = copy.deepcopy(FIXTURE["answer"])
    assert answer["items"][1]["preference_note"] and answer["items"][1]["preference_note_i18n"]
    del answer["items"][1]["preference_note_i18n"]                       # what the live answer did
    answer["items"][2]["alternatives"] = [{**alt, "why_i18n": {"fi": alt.get("why") or "x"}}  # a partial pair
                                          for alt in answer["items"][2].get("alternatives") or []]
    res = classify(monkeypatch, answer)
    assert [r.get("cn8") for r in res] == ["94016100", "94036010", "94036010"]
    assert all(not str(r.get("reason") or "").startswith("AI classification failed") for r in res)
    duty = res[1].get("duty") or {}
    assert duty.get("preference_note") == answer["items"][1]["preference_note"]  # the note is kept as written
    assert not duty.get("preference_note_i18n")                                  # without an invented pair


def test_a_missing_rationale_pair_sends_only_that_item_to_a_person(monkeypatch):
    answer = copy.deepcopy(FIXTURE["answer"])
    del answer["items"][0]["rationale_i18n"]
    res = classify(monkeypatch, answer)
    assert res[0]["reason"].startswith("AI classification failed (Missing bilingual rationale)")
    assert [r.get("cn8") for r in res[1:]] == ["94036010", "94036010"]
