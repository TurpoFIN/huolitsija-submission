"""Per-system TEST message check (GET /api/customs/readiness/test-messages).

Inputs are the live test tenant's own shipments and stored import declaration, captured unedited from the live app
(fixtures/s4_test_messages). The business messages are validated by the official schemas (Tulli v1.21 package, Tulli
NCTS MIG 2.2.0, EU ICS2 3.2.0.0) and the envelopes by Tulli's v1.21 XSD/WSDL, not by this module's own reading.
The EOS EORI check calls the real EU service; nothing is sent to Tulli or an ICS2 access point."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s4_testmsg_{uuid.uuid4().hex[:8]}"
os.environ["JWT_SECRET"] = "test-messages-secret-for-local-tests-only"
import asyncio  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from lxml import etree as E  # noqa: E402

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s4_customs_test_messages.py in its own pytest process", allow_module_level=True)
from core.db import client, db  # noqa: E402
from services import customs_test_messages as tm  # noqa: E402
from services import ics2_gateway  # noqa: E402
from services import tulli_transport as wire  # noqa: E402
from services.customs_messages import ics2, ncts_fi  # noqa: E402
from services.customs_messages.schema_codec import validate  # noqa: E402

FX = Path(__file__).parent / "fixtures/s4_test_messages"
IMPORT_SHIPMENT = json.loads((FX / "shipment-HUO-2026-000042.json").read_text())
EXPORT_SHIPMENT = json.loads((FX / "shipment-HUO-2026-000072.json").read_text())
DECLARATION = json.loads((FX / "declaration-LRN-2026-000008.json").read_text())
TENANT = IMPORT_SHIPMENT["company_id"]
COMPANY = {"id": TENANT, "name": "E2E Testi 084712 Oy", "business_id": "3994713-8", "vat_id": "FI39947138",
           "eori": "FI3994713-8", "test_tenant": True,
           "address": {"street": "Mannerheimintie 12", "postal_code": "00100", "city": "Helsinki", "country": "FI"},
           "signatory": {"name": "Eeva Testaaja", "email": "e2e.20261024084712@huolitsija.fi"}}
EOS = {"status": "fail", "valid": False, "summary": "FI3994713-8 is not in the EU EORI registry.",
       "source": "EU EOS EORI validation", "url": "https://ec.europa.eu/taxation_customs/dds2/eos/validation/services/validation",
       "checked_at": "2026-10-25T03:00:00Z"}


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.users.insert_one({"id": "tm-ops", "email": "ops@tm.test", "org_type": "internal", "role": "ops_admin",
                                 "session_version": 0}))
    run(db.raw.users.insert_one({"id": "tm-cust", "email": "c@tm.test", "org_type": "customer", "company_id": TENANT,
                                 "role": "customer_admin", "session_version": 0}))
    run(db.raw.companies.insert_one(dict(COMPANY)))
    run(db.raw.companies.insert_one({"id": "prod-co", "name": "Real Customer Oy", "eori": "FI1234567-8"}))
    run(db.raw.shipments.insert_many([dict(IMPORT_SHIPMENT), dict(EXPORT_SHIPMENT),
                                      {**EXPORT_SHIPMENT, "id": "prod-ship", "company_id": "prod-co"}]))
    run(db.raw.customs_declarations.insert_one(dict(DECLARATION)))
    yield
    run(client.drop_database(os.environ["DB_NAME"]))


@pytest.fixture
def eos(monkeypatch):
    calls = []

    async def fake(eori, business_id=None):
        calls.append(eori)
        return {**EOS, "eori": eori}
    import services.kyc as kyc
    monkeypatch.setattr(kyc, "eori_check", fake)
    return calls


@pytest.mark.parametrize("system,shipment,schema", [
    ("import", IMPORT_SHIPMENT, None), ("export", EXPORT_SHIPMENT, None),
    ("transit", EXPORT_SHIPMENT, ncts_fi.DIRECTORY / "FI015A.xsd"), ("ics2", IMPORT_SHIPMENT, ics2.path("IE3F15"))])
def test_each_system_builds_a_schema_valid_test_message_in_its_envelope(system, shipment, schema, eos):
    stored = DECLARATION if system in {"import", "ics2"} else None
    res = run(tm.check_system(system, shipment, COMPANY, stored, {}))
    assert res["ok"] is True and res["business_xml_valid"] is True, res
    assert res["sent"] is False and res["can_send"] is False and res["lrn"].startswith("TEST") and len(res["lrn"]) <= 14
    assert res["eos_check"]["read_only"] and [r["eori"] for r in res["eos_check"]["results"]] == ["FI3994713-8"]
    assert all(v["why"] for v in res["test_values"])  # every placeholder is named and explained
    if system == "ics2":
        assert "Tulli v1.21 carries no ICS2" in res["envelope"]["signing"]
    else:
        env = res["envelope"]
        assert env["application"] == tm.TULLI_APPLICATION[system] and env["application_request"]["valid"]
        assert env["soap_upload"]["valid"] and env["channel"] == "Tulli Direct Message Exchange v1.21"


def test_messages_carry_the_shipments_own_data(eos):
    imp = run(tm.check_system("import", IMPORT_SHIPMENT, COMPANY, DECLARATION, {}))
    assert imp["based_on_declaration"]["lrn"] == "LRN-2026-000008"
    xml = tm.import_declaration(IMPORT_SHIPMENT, DECLARATION, COMPANY, tm.TestValues())
    assert [i["cn_code"] for i in xml["items"]] == ["87149950", "73151110", "85121000", "87149420"]
    tr = tm.transit_declaration(EXPORT_SHIPMENT, COMPANY, tm.TestValues())
    payload = ncts_fi.build("FI015A", tr["national_data"]["FI015A"])
    root = validate(payload, ncts_fi.DIRECTORY / "FI015A.xsd")
    text = E.tostring(root).decode()
    assert "UN1263" in text and "FI009801" in text and "FI3994713-8" in text
    ens = tm.ens_filing(IMPORT_SHIPMENT, DECLARATION, COMPANY, tm.TestValues())
    xml = ics2.build(ens["filing_type"], ens["ens_data"])
    assert "CNYTN" in xml and "FIHEL" in xml and "Shenzhen Velotek" in xml


def test_tulli_envelope_carries_the_business_message_byte_for_byte(eos):
    import tempfile
    from services import customs_readiness as ready
    decl = tm.export_declaration(EXPORT_SHIPMENT, COMPANY, tm.TestValues())
    payload, message, _ = tm._build("export", decl)
    with tempfile.TemporaryDirectory() as tmp:
        cli = wire.Transport(ready._ephemeral_settings(Path(tmp)))
        soap = E.fromstring(cli.request("Upload", application="EXP", reference="SELFT000000001", payload=payload))
    request = E.fromstring(base64.b64decode(soap.find(".//{%s}ApplicationRequestMessage" % wire.CT).text))
    wire.schema("ApplicationRequest").assertValid(request)
    assert base64.b64decode(request.find(".//{%s}Content" % wire.MSG).text) == payload


def test_schema_refusal_is_reported_not_wrapped(eos, monkeypatch):
    broken = {**EXPORT_SHIPMENT, "reference": "HUO-2026-000072",
              "cargo": {**EXPORT_SHIPMENT["cargo"], "description": "x" * 600}}
    res = run(tm.check_system("export", broken, COMPANY, None, {}))
    assert res["ok"] is False and res["business_xml_valid"] is False and res["errors"] and "envelope" not in res


def test_route_permissions_test_tenant_only_and_nothing_sent(eos, monkeypatch):
    from core.security import create_session_token
    from server import app

    async def never(*a, **k):
        raise AssertionError("a TEST message check must never send")
    monkeypatch.setattr(ics2_gateway, "request", never)
    monkeypatch.setattr(wire.Transport, "send", never, raising=False)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tm.test") as c:
            url = "/api/customs/readiness/test-messages"
            ids = [IMPORT_SHIPMENT["id"], EXPORT_SHIPMENT["id"]]
            assert (await c.get(url, params={"shipment_id": ids})).status_code == 401
            c.headers["Authorization"] = "Bearer " + create_session_token("tm-cust")
            assert (await c.get(url, params={"shipment_id": ids})).status_code == 403
            c.headers["Authorization"] = "Bearer " + create_session_token("tm-ops")
            r = await c.get(url, params={"shipment_id": ids})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["sent"] is False and set(body["summary"]) == {"import", "export", "transit", "ics2"}
            assert all(s["ok"] and s["schema_valid"] and s["envelope_valid"] for s in body["summary"].values()), body["summary"]
            assert (await c.get(url, params={"shipment_id": "prod-ship"})).status_code == 403
            assert (await c.get(url, params={"shipment_id": "nope"})).status_code == 404
            assert await db.raw.customs_messages.count_documents({}) == 0  # nothing stored
    run(go())
    assert set(eos) == {"FI3994713-8"}
