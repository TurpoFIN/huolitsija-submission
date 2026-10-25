"""s2 r30/r33: a test haulier / test warehouse bound to one test tenant (core/test_counterparty.py), and the execution
mail recipient allowlist (EXECUTION_ALLOWED_RECIPIENT_DOMAINS, services/execution/mail.py).

In process against a throw-away MongoDB database (own database, dropped at the end)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_testcp_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
for k in ("EXECUTION_SMTP_HOST", "EXECUTION_SANDBOX_DOMAIN", "EXECUTION_ALLOWED_RECIPIENT_DOMAINS"):
    os.environ.pop(k, None)
import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s2_test_counterparty.py in its own pytest process", allow_module_level=True)

TID = str(uuid.uuid4())    # test tenant
REAL = str(uuid.uuid4())   # real customer
OPS = {"id": "u-ops", "name": "Olli Ops", "email": "olli@huolitsija.example", "org_type": "internal", "role": "ops_admin",
       "locale": "fi", "session_version": 0}


def shipment_doc(sid, cid, ref):
    return {"id": sid, "reference": ref, "company_id": cid, "status": "booked", "mode": "road",
            "legs": [{"seq": 1, "mode": "road", "from": {"name": "Helsinki"}, "to": {"name": "Tampere"},
                      "etd": "2026-11-02T08:00:00Z", "eta": "2026-11-02T14:00:00Z"}],
            "milestones": [], "created_at": "2026-10-25T00:00:00Z"}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        await ensure_indexes()
        await db.raw.companies.insert_one({"id": TID, "name": "E2E Testi Oy", "test_tenant": True, "business_id": "3994713-8"})
        await db.raw.companies.insert_one({"id": REAL, "name": "Oikea Asiakas Oy", "business_id": "3596001-2"})
        await db.raw.shipments.insert_one(shipment_doc("s-test", TID, "HUO-T-1"))
        await db.raw.shipments.insert_one(shipment_doc("s-real", REAL, "HUO-R-1"))
        await db.raw.carriers.insert_one({"id": "c-real", "name": "Oikea Kuljetus Oy", "code": "OIKEA", "modes": ["road"],
                                          "active": True, "contact_email": "dispatch@oikea.example"})
    run(go())


def teardown_module(_m):
    from core.db import client
    run(client.drop_database(os.environ["DB_NAME"]))


def test_test_carrier_is_bound_inactive_and_hidden_from_ops_views():
    from core.db import db
    from core.tenant_visibility import hidden_test_companies
    from routes.carriers import create_carrier, update_carrier
    with pytest.raises(HTTPException) as e:
        run(create_carrier({"name": "S2 testihaulier", "code": "S2T30X", "modes": ["road"], "test_company_id": REAL}, OPS))
    assert e.value.status_code == 422
    c = run(create_carrier({"name": "S2 testihaulier", "code": "S2T30", "modes": ["road"], "active": True,
                            "contact_email": "marc+huolitsija-s2-30@heliosone.fi", "test_company_id": TID}, OPS))["carrier"]
    assert c["company_id"] == TID and c["test_tenant"] is True and c["active"] is False
    with pytest.raises(HTTPException) as e:
        run(update_carrier(c["id"], {"active": True}, OPS))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:  # the code stays unique even while the test carrier is hidden
        hidden_test_companies.set([TID])
        try:
            run(create_carrier({"name": "Dup", "code": "S2T30", "modes": ["road"]}, OPS))
        finally:
            hidden_test_companies.set(None)
    assert e.value.status_code == 409

    async def ids():
        return [x["id"] async for x in db.carriers.find({}, {"_id": 0, "id": 1})]
    hidden_test_companies.set([TID])  # an ops view without "show test tenants"
    try:
        assert run(ids()) == ["c-real"]
    finally:
        hidden_test_companies.set(None)
    assert sorted(run(ids())) == sorted(["c-real", c["id"]])


def test_test_carrier_only_on_its_own_test_tenants_leg_and_slot():
    from core.db import db
    from services import shipments as sh
    from services.execution.slots import request_slot
    car = run(db.raw.carriers.find_one({"code": "S2T30"}, {"_id": 0}))
    if not car:
        test_test_carrier_is_bound_inactive_and_hidden_from_ops_views()
        car = run(db.raw.carriers.find_one({"code": "S2T30"}, {"_id": 0}))
    with pytest.raises(HTTPException) as e:
        run(sh.update_leg("s-real", 1, {"carrier_id": car["id"]}, OPS))
    assert e.value.status_code == 409
    real = run(db.raw.shipments.find_one({"id": "s-real"}, {"_id": 0}))
    assert real["legs"][0].get("carrier_id") is None
    try:
        run(sh.update_leg("s-test", 1, {"carrier_id": car["id"]}, OPS))
    except HTTPException as exc:  # pragma: no cover - only the binding check matters here
        assert exc.status_code != 409, exc.detail
    except Exception:  # noqa: BLE001  (positions refresh on a minimal shipment document)
        pass
    assert run(db.raw.shipments.find_one({"id": "s-test"}, {"_id": 0}))["legs"][0]["carrier_id"] == car["id"]
    # a leg that somehow carries the test haulier on a real shipment still cannot request a slot
    run(db.raw.shipments.update_one({"id": "s-real"}, {"$set": {"legs.0.carrier_id": car["id"]}}))
    with pytest.raises(HTTPException) as e:
        run(request_slot("s-real", 1, "pickup", {"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T10:00:00Z"}, OPS))
    assert e.value.status_code == 409
    assert run(db.raw.slot_bookings.count_documents({"shipment_id": "s-real"})) == 0
    run(db.raw.shipments.update_one({"id": "s-real"}, {"$unset": {"legs.0.carrier_id": ""}}))


def test_test_warehouse_only_for_its_own_test_tenant():
    from core.db import db
    from routes.execution_warehouse import WarehouseIn, create
    from services.execution.warehouse import create_order
    with pytest.raises(HTTPException) as e:
        run(create(WarehouseIn(name="S2 testivarasto", email="marc+huolitsija-s2-33@heliosone.fi", test_company_id=REAL), OPS))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run(create(WarehouseIn(name="S2 testivarasto", provider="odoo", test_company_id=TID), OPS))
    assert e.value.status_code == 422
    wh = run(create(WarehouseIn(name="S2 testivarasto", email="marc+huolitsija-s2-33@heliosone.fi", test_company_id=TID), OPS))
    assert wh["company_id"] == TID and wh["test_tenant"] is True and "test_company_id" not in wh
    with pytest.raises(HTTPException) as e:
        run(create_order("s-real", "inbound", wh["id"], actor=OPS))
    assert e.value.status_code == 409
    assert run(db.raw.warehouse_orders.count_documents({"shipment_id": "s-real"})) == 0
    plain = run(create(WarehouseIn(name="Tavallinen varasto", email="varasto@example.com"), OPS))
    assert "test_tenant" not in plain and "company_id" not in plain


def test_execution_mail_allowlist_keeps_other_recipients_logged(monkeypatch):
    from services.execution import mail
    monkeypatch.setenv("EXECUTION_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("EXECUTION_SMTP_PORT", "1")  # nothing listens: an attempted send ends "failed"
    monkeypatch.setenv("EXECUTION_ALLOWED_RECIPIENT_DOMAINS", "heliosone.fi")
    outside = run(mail.send("dispatch@oikea.example", "Slot", "x", kind="slot", ref_type="slot_booking", ref_id="a"))
    assert outside["status"] == "logged" and outside["note"] == "recipient outside EXECUTION_ALLOWED_RECIPIENT_DOMAINS"
    inside = run(mail.send("marc+huolitsija-s2-30@heliosone.fi", "Slot", "x", kind="slot", ref_type="slot_booking", ref_id="b"))
    assert inside["status"] == "failed"  # it was really attempted over SMTP
    monkeypatch.delenv("EXECUTION_ALLOWED_RECIPIENT_DOMAINS")
    unrestricted = run(mail.send("dispatch@oikea.example", "Slot", "x", kind="slot", ref_type="slot_booking", ref_id="c"))
    assert unrestricted["status"] == "failed" and "note" not in unrestricted
