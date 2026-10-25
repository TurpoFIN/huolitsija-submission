"""Real DME signatures, pinned XSDs, Mongo and business parsers; no authority network."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

os.environ["DB_NAME"] = "huo_world_authority_test_" + uuid.uuid4().hex[:10]

from core.db import db, client
from services import tulli_transport as wire
from services.customs_messages import tulli_common as business
from services.customs_messages.tulli_responses import parse_response
from services.demo_world import authority, boundary


@pytest.fixture(autouse=True)
def clean_authority_storage():
    async def clear():
        for name in ('world_authority_messages', 'world_authority_submissions',
                     'world_authority_attachment_reviews', 'tulli_replies', 'tulli_poll_cursors',
                     'tulli_transmissions'):
            await db.raw[name].delete_many({})
    run(clear())


@pytest.fixture
def transport(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "demo-authority-test")])
    today = datetime.now(timezone.utc)
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(today - timedelta(days=1)).not_valid_after(today + timedelta(days=1)).sign(key, hashes.SHA256())
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return wire.Transport(wire.Settings("test", "FI1234567-8", "FI1234567-8", "FI1234567-8", "ABCDE",
                                       str(tmp_path / "cert.pem"), str(tmp_path / "key.pem"), True,
                                       "http://127.0.0.1:8202/api/demo/world/authority/tulli"))


def payload(system):
    decl = json.loads((Path(__file__).parent / "fixtures/tulli/export-declaration.json").read_text())
    if system == "import":
        decl.update(type="import", importer=decl["declarant"])
        decl["transport"].update(destination_country="FI", dispatch_country="CN")
        decl["items"][0]["taric_code"] = "8712003010"
    return business.serialize(system, "FI415B" if system == "import" else "FI515A", business.declaration(decl, system))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("system,control", [("import", False), ("export", False), ("import", True), ("export", True)])
def test_signed_upload_poll_parse_attachment_and_exit(monkeypatch, transport, system, control):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")

    async def decision(*args, **kwargs):
        return {"control": control, "request": "Provide commercial invoice", "reasoning": "Documentary risk check", "release": True}

    monkeypatch.setattr(authority.llm, "ask_json", decision)

    async def exercise():
        await db.raw.world_authority_messages.delete_many({})
        await db.raw.world_authority_submissions.delete_many({})
        app = wire.APPLICATIONS[system]
        upload = await transport.call("Upload", application=app, reference="ABCDE000000001", payload=payload(system))
        sid = upload.findtext("{" + wire.CT + "}ApplicationRequestMessageInformation/{" + wire.CT + "}MessageStorageId")
        assert sid
        duplicate = await transport.call("Upload", application=app, reference="ABCDE000000001", payload=payload(system))
        assert duplicate.findtext("{" + wire.CT + "}ApplicationRequestMessageInformation/{" + wire.CT + "}MessageStorageId") == sid
        assert await db.raw.world_authority_submissions.count_documents({}) == 1

        async def poll():
            wall = datetime.now(timezone.utc)
            listing = await transport.call("DownloadList", start=(wall - timedelta(days=1)).isoformat(), end=(wall + timedelta(minutes=1)).isoformat())
            events = []
            for info in listing.findall("{" + wire.CT + "}MessageInformation"):
                storage = info.findtext("{" + wire.CT + "}MessageStorageId")
                resp = await transport.call("Download", storage_id=storage)
                raw, ref = wire.application_response(resp, application=app, declarant_id=transport.settings.declarant_id, storage_id=storage)
                assert ref == "ABCDE000000001"
                events.append(parse_response(raw, system)["type"])
            return events

        assert await poll() == ["accepted", "under_control" if control else "released"]
        if control:
            response = await transport.call("UploadAttachment", application=app, reference="ABCDE000000002", storage_id=sid,
                payload=b'<invoice number="TEST-1"><total currency="EUR">1000</total></invoice>',
                attachment={"customs_requested": True, "title": "invoice.xml", "type": "N380", "mime_type": "application/xml"})
            assert response.find("{" + wire.CT + "}AttachmentRequestMessageInformation") is not None
            assert await poll() == ["accepted", "under_control", "released"]
        if system == "export":
            await authority.notify_exit(None)
            assert (await poll())[-1] == "exited"
        check = await transport.call("CheckConnectivity")
        assert check.findtext("{" + wire.CT + "}EchoResponse")
    run(exercise())


def test_invalid_business_payload_is_schema_rejection(monkeypatch, transport):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")

    async def exercise():
        response = await transport.call("Upload", application="IMP", reference="ABCDE000000003",
                                        payload='<FI415B xmlns="http://tulli.fi/schema/external/import/ext/v3"/>')
        assert response is not None
        row = await db.raw.world_authority_messages.find_one({"reference": "ABCDE000000003"})
        assert row["code"] == "DmeErrorMessage"
        wire.validate(wire.parse(row["payload"]), "DmeErrorMessage")
    run(exercise())


def test_boundary_off_and_production_envelope_refused(monkeypatch, transport):
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    request = transport.request("CheckConnectivity")
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "false")
    with pytest.raises(wire.TransportError, match="disabled"):
        run(authority.handle(request))
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    production = wire.Transport(wire.Settings(**{**transport.settings.__dict__, "mode": "production"}))
    with pytest.raises(wire.TransportError, match="TEST"):
        run(authority.handle(production.request("Upload", application="IMP", reference="ABCDE000000004", payload=payload("import"))))


@pytest.mark.parametrize('endpoint', [wire.ENDPOINTS['test'],wire.ENDPOINTS['production'],''])
def test_real_or_implicit_authority_url_refused_before_any_http(monkeypatch, transport, endpoint):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    import httpx
    async def forbidden_http(*a, **kw):
        raise AssertionError('No HTTP transport is allowed')
    monkeypatch.setattr(httpx.AsyncClient,'post',forbidden_http)
    wrong = wire.Transport(wire.Settings(**{**transport.settings.__dict__,'endpoint':endpoint}))
    with pytest.raises(wire.TransportError,match='real Tulli URLs are forbidden'):
        run(wrong.call('CheckConnectivity'))


def test_private_authority_poll_timer_follows_world_clock_only(monkeypatch, transport):
    from services import tulli_exchange
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    monkeypatch.setattr(boundary,'demo_rate',lambda:1440.0)
    wall = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return wall
    monkeypatch.setattr(tulli_exchange,'datetime',Clock)
    calls = []
    original = transport.call
    async def observe(operation, **kw):
        calls.append(operation)
        return await original(operation, **kw)
    monkeypatch.setattr(transport,'call',observe)
    async def exercise():
        nonlocal wall
        await db.raw.tulli_poll_cursors.delete_many({})
        await tulli_exchange.refresh_replies(transport)
        await tulli_exchange.refresh_replies(transport)
        assert calls == ['DownloadList']
        wall += timedelta(seconds=1)
        await tulli_exchange.refresh_replies(transport)
        assert calls == ['DownloadList','DownloadList']
        monkeypatch.setenv('DEMO_WORLD_SANDBOX','false')
        assert boundary.authority_poll_seconds(305) == 305
    run(exercise())


def test_normal_customs_agent_uploads_stored_invoice_bytes(monkeypatch, transport):
    """No upload/decision success doubles: real gate, DME client, signature and GridFS."""
    from core.settings import update_settings
    from core.storage import put_file, get_file
    from services import tulli, tulli_exchange
    from services.agents import customs_correspondence as agent
    from services.demo_world.injectors import source_documents
    monkeypatch.setenv("DEMO_WORLD_SANDBOX", "true")
    monkeypatch.setattr(tulli_exchange, "transport", lambda mode: transport)

    async def authority_decision(*args, **kwargs):
        return {"control": True, "request": "Provide commercial invoice", "reasoning": "Documentary check", "release": True}

    async def document_selection(*args, **kwargs):
        return {"document_ids": ["query-invoice"], "confidence": 0.99, "missing": [], "reasoning": "Commercial invoice requested"}

    monkeypatch.setattr(authority.llm, "ask_json", authority_decision)
    monkeypatch.setattr(agent, "llm_json", document_selection)

    async def exercise():
        await update_settings({"autopilot_enabled": True, "autopilot_halted": False})
        await db.raw.world_authority_messages.delete_many({})
        await db.raw.world_authority_submissions.delete_many({})
        decl = json.loads((Path(__file__).parent / "fixtures/tulli/export-declaration.json").read_text())
        decl.update(id="query-export", shipment_id="query-shipment", status="under_control")
        await db.raw.export_declarations.insert_one(dict(decl))
        gateway = tulli.MessageExchangeGateway("test", "export")

        async def configured_gateway(*args):
            return gateway
        monkeypatch.setattr(tulli, "gateway_for", configured_gateway)
        await gateway.submit(decl)
        responses = await gateway.poll(decl)
        query = next(x for x in responses if x["type"] == "under_control")
        assert query["notification"]["text"] == "Provide commercial invoice"
        assert query["notification"]["request_correlation"] and query["storage_id"]
        parties = {role: {"name": name, "address": {"street": "Test 1", "city": "Helsinki", "postal_code": "00100", "country": "FI"}}
                   for role, name in (("shipper", "Test Supplier"), ("consignee", "Test Buyer"))}
        invoice = source_documents({"id": "query-world", "profile": {"cargo_description": "Bicycles", "packages": 1,
            "weight_kg": 14, "net_weight_kg": 12, "value_eur": 1000, "origin_country": "FI"}}, parties)[0]
        fid = await put_file(invoice[2], filename=invoice[0], content_type=invoice[1])
        await db.raw.documents.insert_one({"id": "query-invoice", "shipment_id": decl["shipment_id"], "type": "commercial_invoice",
            "filename": invoice[0], "file_id": fid})
        outcome = await agent.answer_query({"payload": {"declaration_id": decl["id"], "system": "export", "response": query}})
        assert outcome == {"sent": ["query-invoice"]}
        submission = await db.raw.world_authority_submissions.find_one({"reference": (await db.raw.tulli_transmissions.find_one({"declaration_id": decl["id"]}))["reference"]})
        assert submission["state"] == "released"
        raw, _, _ = await get_file(submission["attachment_file_ids"][0])
        assert raw == invoice[2] and raw.startswith(b"%PDF")
        await agent.answer_query({"payload": {"declaration_id": decl["id"], "system": "export", "response": query}})
        assert await db.raw.customs_query_attachments.count_documents({"query": query["exchange_reply_id"]}) == 1
        assert await db.raw.customs_messages.count_documents({"declaration_id": decl["id"], "direction": "in"}) == 2
    run(exercise())


@pytest.mark.parametrize('cached_payload', [None, '<stale/>'])
def test_replies_drop_storage_ids_removed_after_cache_refresh(monkeypatch, transport, cached_payload):
    from services import tulli_exchange
    monkeypatch.setenv('DEMO_WORLD_SANDBOX', 'true')
    async def exercise():
        account = transport.settings.mode + ':' + transport.settings.intermediary_id
        await db.raw.tulli_poll_cursors.insert_one({'_id': account,
            'next_at': datetime.now(timezone.utc) + timedelta(minutes=5)})
        await db.raw.tulli_transmissions.insert_one({'declaration_id': 'stale-decl', 'system': 'export',
            'mode': 'test', 'declarant_id': transport.settings.declarant_id, 'reference': 'STALE'})
        await db.raw.world_authority_messages.insert_one({'storage_id': 'vanished',
            'intermediary': transport.settings.intermediary_id})
        await db.raw.tulli_replies.insert_one({'_id': 'stale-reply', 'account': account, 'applied': False,
            'payload': cached_payload, 'info': {'Application': 'EXP',
            'DeclarantBusinessId': transport.settings.declarant_id, 'ControlReference': 'STALE',
            'MessageStorageId': 'vanished'}})
        await db.raw.world_authority_messages.delete_one({'storage_id': 'vanished'})
        async def no_download(*a, **kw):
            raise AssertionError('A stale storage ID must not be downloaded or replayed')
        monkeypatch.setattr(transport, 'call', no_download)
        assert await tulli_exchange.replies(transport, 'export', {'id': 'stale-decl'}) == []
        assert not await db.raw.tulli_replies.find_one({'_id': 'stale-reply'})
        monkeypatch.setenv('DEMO_WORLD_SANDBOX', 'false')
        assert await boundary.authority_reply_exists('vanished', transport.settings.intermediary_id)
    run(exercise())


def test_poll_events_follow_pending_declaration_and_skip_stopped_runs(monkeypatch, transport):
    monkeypatch.setenv('DEMO_WORLD_SANDBOX','true')
    async def exercise():
        for ident, status in (('pending-live','lifecycle'),('pending-stopped','stopped'),('no-pending','lifecycle')):
            await db.raw.demo_runs.insert_one({'id':ident,'phase':'lifecycle','status':status,'quote_id':ident+'-quote',
                'event_seq':0,'created_at':datetime.now(timezone.utc).isoformat()})
            await db.raw.shipments.insert_one({'id':ident+'-shipment','quote_id':ident+'-quote'})
            await db.raw.export_declarations.insert_one({'id':ident+'-decl','shipment_id':ident+'-shipment',
                'lrn':ident+'-lrn','status':'exited' if ident == 'no-pending' else 'under_control'})
            await db.raw.world_authority_submissions.insert_one({'storage_id':ident+'-storage','run_id':ident,
                'lrn':ident+'-lrn','system':'export','application':'EXP','intermediary':transport.settings.intermediary_id})
        async def poll():
            wall = datetime.now(timezone.utc)
            return await transport.call('DownloadList',start=(wall-timedelta(days=1)).isoformat(),end=wall.isoformat())
        await poll()
        rows = await db.raw.demo_run_events.find({'node':'customs'}).to_list(20)
        assert len(rows) == 1 and rows[0]['run_id'] == 'pending-live'
        assert rows[0]['data']['facts']['operation'] == 'DownloadList'
        await db.raw.demo_runs.update_one({'id':'pending-live'},{'$set':{'status':'stopped'}})
        await poll()
        await authority._emit('pending-live','Tulli release received')
        assert await db.raw.demo_run_events.count_documents({'node':'customs'}) == 1
        # A connectivity probe never belongs to the newest run.
        await transport.call('CheckConnectivity')
        assert await db.raw.demo_run_events.count_documents({'node':'customs'}) == 1
        _, owner = await authority._declaration_run('export','pending-stopped-lrn')
        assert owner is None
    run(exercise())


def teardown_module():
    run(client.drop_database(db.raw.name))
