"""Customs readiness: Tulli v1.21 envelope built around Tulli's own example business messages, the deployed schema
package checked against the official sha256 manifest, route permissions, and one live handshake with Tulli.

The payloads are Tulli's published FI415B / FI515A examples (backend/data/xsd/tulli/*/examples); the envelope is
validated by the official v1.21 XSD/WSDL, not by this module's own reading of them."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s11_ready_{uuid.uuid4().hex[:8]}"
os.environ["JWT_SECRET"] = "readiness-test-secret-for-local-tests-only"
import asyncio  # noqa: E402
import base64  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from lxml import etree as E  # noqa: E402

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:
    pytest.skip("run test_s11_readiness.py in its own pytest process", allow_module_level=True)
from core.db import client, db  # noqa: E402
from services import customs_readiness as ready  # noqa: E402
from services import tulli_transport as wire  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data/xsd/tulli"
FI415B = (DATA / "import/2.1.7/examples/FI415B.xml").read_bytes()
FI515A = (DATA / "export/1.1.1/examples/FI515A.xml").read_bytes()


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def database():
    run(db.raw.users.insert_one({"id": "rd-ops", "email": "ops@ready.test", "org_type": "internal", "role": "ops_admin",
                                 "session_version": 0}))
    run(db.raw.users.insert_one({"id": "rd-cust", "email": "c@ready.test", "org_type": "customer",
                                 "role": "customer_admin", "session_version": 0}))
    yield
    run(client.drop_database(os.environ["DB_NAME"]))


def test_deployed_schema_package_matches_the_official_manifest():
    pkg = ready.schema_package()
    assert pkg["files"] == 15 and pkg["matching_official_sha256"] == 15 and pkg["mismatched"] == []


@pytest.mark.parametrize("payload,application,message", [(FI415B, "IMP", "FI415B"), (FI515A, "EXP", "FI515A")])
def test_official_business_message_in_a_signed_v121_envelope(payload, application, message):
    res = ready.envelope_self_test(payload, application, message)
    assert res["ok"], res
    assert res["sent"] is False and "not a DVV certificate" in res["signing_key"]
    assert res["application_request"]["valid"] and res["soap_upload"]["valid"]
    # independent check: decode what the transport produced and validate it with the official XSD/WSDL again
    with __import__("tempfile").TemporaryDirectory() as tmp:
        cli = wire.Transport(ready._ephemeral_settings(Path(tmp)))
        soap = E.fromstring(cli.request("Upload", application=application, reference="SELFT000000001", payload=payload))
    msg = soap.find(".//{%s}ApplicationRequestMessage" % wire.CT).text
    request = E.fromstring(base64.b64decode(msg))
    wire.schema("ApplicationRequest").assertValid(request)
    content = request.find(".//{%s}Content" % wire.MSG).text
    assert base64.b64decode(content) == payload  # the business message travels byte for byte


def test_broken_payload_is_refused_not_wrapped():
    res = ready.envelope_self_test(b"<not-closed>", "IMP", "FI415B")
    assert res["ok"] is False and res["sent"] is False and "application_request" not in res


def test_missing_company_steps_are_named(monkeypatch):
    for k in ("TULLI_DECLARANT_ID", "TULLI_CLIENT_CERT", "TULLI_CLIENT_KEY"):
        monkeypatch.setattr(config, k, "", raising=False)
    imp = ready._tulli_system("import", "FI415B (H1)")
    names = [m["setting"] for m in imp["missing"]]
    assert imp["can_send"] is False and imp["mode"] == "disabled"
    assert names[0].startswith("TULLI_MODE") and "TULLI_CLIENT_CERT" in names and "TULLI_DECLARANT_ID" in names
    assert any(n.startswith("Tulli message-exchange customer status") for n in names)
    assert all(m["fi"] and m["en"] for m in imp["missing"])


def test_route_permissions_and_shape(monkeypatch):
    from core.security import create_session_token
    from server import app

    fake = {"endpoint": "x", "host": "x", "server_verified": True, "client_certificate_required": True}
    monkeypatch.setattr(ready, "_handshake", lambda endpoint: {**fake, "endpoint": endpoint})
    ready._tls_cache.clear()

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rd.test") as c:
            assert (await c.get("/api/customs/readiness")).status_code == 401
            c.headers["Authorization"] = "Bearer " + create_session_token("rd-cust")
            assert (await c.get("/api/customs/readiness")).status_code == 403
            c.headers["Authorization"] = "Bearer " + create_session_token("rd-ops")
            r = await c.get("/api/customs/readiness")
            assert r.status_code == 200, r.text
            body = r.json()
            assert [s["system"] for s in body["systems"]] == ["import", "export", "transit", "ics2"]
            assert set(body["tulli_endpoints"]) == {"test", "production"} and body["sent"] is False
            assert (await c.get("/api/customs/readiness", params={"declaration_id": "nope"})).status_code == 404
            r = await c.get("/api/customs/readiness", params={"example": "FI415B"})
            st = r.json()["envelope_self_tests"][0]
            assert st["ok"] and st["payload_sha256"] == __import__("hashlib").sha256(FI415B).hexdigest(), st
            assert "published FI415B example" in st["payload_source"] and st["sent"] is False
            assert (await c.get("/api/customs/readiness", params={"example": "FI999"})).status_code == 422
    run(go())
    ready._tls_cache.clear()


@pytest.mark.skipif(os.environ.get("S11_OFFLINE") == "1", reason="offline run")
def test_live_tulli_answers_with_certificate_required():
    """Tulli's real state today: server verified against DVV's chain; it asks for and requires a client certificate."""
    ends = run(ready.tulli_endpoints(fresh=True))
    test = ends["test"]
    assert test["host"] == "ws-customertest.tulli.fi"
    assert test["server_verified"] is True, test
    assert test["client_certificate_required"] is True, test
