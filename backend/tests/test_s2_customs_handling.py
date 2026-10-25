"""s2: a shipment whose customs a broker clears by hand (customs_handling "broker") is left alone by the Autopilot
customs agent, in process against a throw-away MongoDB database (own database, dropped at the end).

What is proven: customs.review_and_file and customs.regate skip such a shipment's declaration and change nothing;
the same declaration on an ordinary shipment gets the agent's broker review (office of presentation from the arrival
port, recorded inspection location, transport document number). A port alone must never invent the inspection
location. The model and e-mail are off."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_customs_handling_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
os.environ["LLM_PROVIDER"] = "none"
import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)  # core.db binds Motor to the current loop


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_customs_handling.py in its own pytest process", allow_module_level=True)

from core import jobs  # noqa: E402
from core.db import db  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from services.agents import customs_agent  # noqa: E402,F401  (registers the customs jobs)

COMPANY = "co-handling"


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.companies.insert_one({"id": COMPANY, "name": "Käsiteltävä Oy", "business_id": "3990011-3", "test_tenant": True,
                                     "kyc_status": "approved", "sanctions_status": "clear", "country": "FI"}))
    yield
    run(db.raw.client.drop_database(os.environ["DB_NAME"]))


def _make(handling: str | None) -> tuple[str, str]:
    sid, did = new_id(), new_id()
    ship = {"id": sid, "reference": f"HUO-T-{sid[:6]}", "company_id": COMPANY, "mode": "sea", "direction": "import",
            "destination": {"name": "Helsinki", "unlocode": "FIHEL", "country": "FI"}, "test": True,
            "legs": [{"seq": 1, "role": "main", "mode": "sea", "to": {"name": "Helsinki", "unlocode": "FIHEL"},
                      "transport_doc_no": f"MAEU{sid[:8].upper()}"}]}
    if handling:
        ship["customs_handling"] = handling
    run(db.raw.shipments.insert_one(ship))
    run(db.raw.customs_declarations.insert_one({
        "id": did, "lrn": f"LRN-T-{did[:6]}", "shipment_id": sid, "company_id": COMPANY, "type": "import", "status": "draft",
        "items": [], "validations": [], "office": None, "location_of_goods": None, "transport": {}, "history": [],
        "created_at": now_iso(), "updated_at": now_iso(), "test": True}))
    return sid, did


def _review(did: str) -> dict:
    job = run(jobs.enqueue("customs.review_and_file", {"declaration_id": did}, key=f"customs:{did}:pytest",
                           subject=("customs_declaration", did)))
    assert run(jobs.work_once("w-customs", kinds=["customs.review_and_file"])) == 1
    return run(db.raw.jobs.find_one({"id": job["id"]}, {"_id": 0}))


def test_the_agent_leaves_a_broker_handled_shipment_alone():
    _sid, did = _make("broker")
    before = run(db.raw.customs_declarations.find_one({"id": did}, {"_id": 0}))
    j = _review(did)
    assert j["status"] == "done" and j["result"] == {"skipped": "a broker handles this shipment's customs by hand"}, j
    after = run(db.raw.customs_declarations.find_one({"id": did}, {"_id": 0}))
    assert after == before
    assert run(db.raw.agent_decisions.count_documents({"subject_id": did})) == 0


@pytest.mark.parametrize("recorded_location", [False, True])
def test_the_agent_does_the_broker_review_on_an_ordinary_shipment(recorded_location):
    sid, did = _make(None)
    location = {"type": "D", "address": {"street": "Sandbox Terminal, Testikatu 1", "city": "Helsinki",
                                         "postal_code": "00980", "country": "FI"}}
    if recorded_location:
        run(db.raw.shipments.update_one({"id": sid}, {"$set": {"inspection_location": location}}))
    _review(did)
    d = run(db.raw.customs_declarations.find_one({"id": did}, {"_id": 0}))
    assert d["office"]["code"] == "FI009801", d["office"]  # Helsinki, sea: Vuosaari harbour
    if recorded_location:
        assert d["goods_location"] == {**location, "source": "shipment.inspection_location"}
    else:
        assert not d.get("goods_location")
        assert not d.get("location_of_goods")
    from services.customs_import_data import validations
    location_checks = [c for c in validations(d) if c["rule"].startswith("IMP-LOCATION-")]
    assert len(location_checks) == 2
    assert all(c["ok"] == recorded_location for c in location_checks)
    assert d["transport"]["transport_document"]["number"] == f"MAEU{sid[:8].upper()}"
    # a second run finds the number where the declaration keeps it and does not write it again
    edits = len(d.get("edits") or [])
    run(db.raw.jobs.delete_many({"payload.declaration_id": did}))
    _review(did)
    again = run(db.raw.customs_declarations.find_one({"id": did}, {"_id": 0}))
    assert not any(c.get("field") == "transport" for e in (again.get("edits") or [])[edits:] for c in e["changes"])


def test_regate_skips_a_broker_handled_shipment():
    _sid, did = _make("broker")
    aid = new_id()
    run(db.raw.approvals.insert_one({"id": aid, "kind": "customs_filing", "status": "pending", "ref_type": "customs_declaration",
                                     "ref_id": did, "data": {}}))
    run(db.raw.customs_declarations.update_one({"id": did}, {"$set": {"status": "ready_for_review", "approval_id": aid}}))
    job = run(jobs.enqueue("customs.regate", {"approval_id": aid}, key=f"regate:{aid}:pytest", subject=("approval", aid)))
    assert run(jobs.work_once("w-customs", kinds=["customs.regate"])) == 1
    j = run(db.raw.jobs.find_one({"id": job["id"]}, {"_id": 0}))
    assert j["status"] == "done" and j["result"] == {"skipped": "a broker handles this shipment's customs by hand"}, j
    assert run(db.raw.approvals.find_one({"id": aid}, {"_id": 0}))["status"] == "pending"
