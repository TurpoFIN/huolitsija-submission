"""s3 r2: the compact sanctions index (slotted subjects, shared repeated values, array postings) reads like the dict
index it replaced, so matching code and stored hits are unchanged. Pure, no database."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services import screening  # noqa: E402

DOCS = [{"list": "eu", "ref": "EU.1", "primary_name": "Ivan Petrovich IVANOV", "names": ["Ivan Petrovich IVANOV", "Ivan Ivanov"],
         "type": "person", "programmes": ["RUS"], "countries": ["RU"], "listed_on": "2022-02-28", "remarks": None, "sub_list": None},
        {"list": "ofac", "ref": "OFAC.2", "primary_name": "IVANOV TRADING LLC", "names": ["IVANOV TRADING LLC"],
         "low_quality_names": ["Ivanov Trade"], "type": "entity", "programmes": ["RUS"], "countries": ["RU"],
         "listed_on": None, "remarks": "Moscow", "sub_list": "SDN"}]


def test_compact_index_reads_like_the_dict_index():
    ix = screening._build(DOCS, {"eu": "v1", "ofac": "v1"})
    e = ix.entries[0]
    assert e["list"] == "eu" and e.get("sub_list") is None and e.get("sub_list", "x") == "x" and e["countries"] == ("RU",)
    assert e["programmes"] is ix.entries[1]["programmes"]  # repeated values are one object
    assert screening.list_label(ix.entries[1]).endswith(": SDN")
    assert len(ix.alias_entry) == 4 and ix.n == 4 and list(ix.alias_entry) == [0, 0, 1, 1]
    assert list(ix.postings["t:" + screening._tkey("ivanov")]) == [0, 1, 2, 3]
    norm, hits, closest, top = screening._match(ix, "Ivan Ivanov", "person", "RU", 5)
    assert hits and hits[0]["ref"] == "EU.1" and hits[0]["matched_name"] == "Ivan Ivanov"
