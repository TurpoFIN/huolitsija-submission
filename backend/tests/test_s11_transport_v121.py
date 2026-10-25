"""Independent protocol evidence: supplied, unmodified official Tulli examples/XSDs."""
import base64
import asyncio
import copy
import json
import ssl
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree as E
from signxml import XMLVerifier
import httpx

from services import tulli_transport as t

FIX = Path(__file__).parent / 'fixtures/tulli/transport-v1.21'


def test_tulli_context_trusts_official_dvv_root_without_weakening_tls():
    context = t.server_ssl_context()
    trusted = {hashlib.sha256(cert).hexdigest() for cert in context.get_ca_certs(binary_form=True)}
    assert 'd3ed3fc40ad26b52e001e1e18f4b9449529deb75a81d5eb680d7b62db23ba96d' in trusted
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_missing_tulli_trust_bundle_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(t, 'SERVER_CA', tmp_path / 'missing.pem')
    with pytest.raises(FileNotFoundError):
        t.server_ssl_context()


@pytest.fixture
def transport(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'local-protocol-test.invalid')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1)).sign(key, hashes.SHA256()))
    cp, kp = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return t.Transport(t.Settings('test', 'FI9991026-5', 'FI9991026-5', 'FI9991026-5',
                                  'LOCAL', str(cp), str(kp), True))


EXAMPLES = []
for path in sorted(FIX.glob('*.xml')):
    root = t.parse(path.read_bytes())
    kind = E.QName(root).localname
    if kind == 'Envelope':
        body = root.find('{' + t.SOAP + '}Body')
        if body is not None and E.QName(body[0]).namespace == t.CS:
            EXAMPLES.append((path.name, 'service'))
    elif kind in ('ApplicationRequest', 'ApplicationResponse', 'AttachmentRequest'):
        EXAMPLES.append((path.name, kind))


@pytest.mark.parametrize('filename,schema', EXAMPLES)
def test_unmodified_official_example_validates(filename, schema):
    root = t.parse((FIX / filename).read_bytes())
    if schema == 'service':
        root = root.find('{' + t.SOAP + '}Body')[0]
    t.validate(root, schema)


def test_generated_upload_signature_and_two_base64_layers(transport):
    payload = (FIX / 'Example04_UploadApplicationContent.xml').read_bytes()
    raw = transport.request('Upload', application='EXP', reference='LOCAL000000001', payload=payload)
    body = t.parse(raw).find('{' + t.SOAP + '}Body')[0]
    app = t.decode(body.findtext('{' + t.CT + '}ApplicationRequestMessage'))
    signed = XMLVerifier().verify(app, x509_cert=Path(transport.settings.certificate).read_bytes()).signed_xml
    assert t.decode(signed.findtext('{' + t.APP + '}ApplicationContent/{' + t.MSG + '}Content')) == payload
    assert signed.findtext('{' + t.APP + '}Environment') == 'TEST'
    assert signed.findtext('{' + t.APP + '}Reference') == 'LOCAL000000001'
    root = t.parse(app)
    assert root.find('.//{' + t.DS + '}Reference').get('URI') == ''
    root.find('{' + t.APP + '}Reference').text = 'LOCAL000000002'
    with pytest.raises(Exception):
        XMLVerifier().verify(root, x509_cert=Path(transport.settings.certificate).read_bytes())


@pytest.mark.parametrize('operation,kwargs', [
    ('DownloadList', {'application': 'IMP', 'start': '2026-10-24T00:00:00Z', 'end': '2026-10-24T01:00:00Z'}),
    ('Download', {'storage_id': 'IMP123456'}), ('CheckConnectivity', {})])
def test_generated_requests_use_official_wsdl(transport, operation, kwargs):
    body = t.parse(transport.request(operation, **kwargs)).find('{' + t.SOAP + '}Body')[0]
    t.validate(body, 'service')


def test_official_upload_receipt_is_only_a_transport_receipt():
    node = t.Transport.response((FIX / 'Example07_UploadResponse.xml').read_bytes(), 'Upload')
    assert node.findtext('{' + t.CT + '}ApplicationRequestMessageInformation/{' + t.CT + '}MessageStorageId') == 'ELEX2018052407505819E8B11'
    assert not node.xpath('//*[local-name()="MRN"]')
    with pytest.raises(t.TransportError, match='operation'):
        t.Transport.response((FIX / 'Example07_UploadResponse.xml').read_bytes(), 'Download')


def test_official_download_unwrap_and_identity_checks():
    node = t.Transport.response((FIX / 'Example15_DownloadResponse.xml').read_bytes(), 'Download')
    raw, ref = t.application_response(node, application='CWAR', declarant_id='FI3451020-1',
                                     storage_id='CWAR201902281148446D39112')
    assert ref == '4e30990a861d40'
    assert t.parse(raw).tag == t.parse((FIX / 'Example17_ApplicationResponseContent.xml').read_bytes()).tag
    with pytest.raises(t.TransportError, match='identity mismatch'):
        t.application_response(node, application='IMP', declarant_id='FI3451020-1',
                               storage_id='CWAR201902281148446D39112')


@pytest.mark.parametrize('change', [{'customer_approved': False}, {'mode': 'disabled'},
                                   {'certificate': ''}, {'private_key': ''}, {'reference_prefix': ''},
                                   {'endpoint': 'https://untrusted.invalid/'}])
def test_configuration_fails_closed(transport, change):
    transport.settings = replace(transport.settings, **change)
    with pytest.raises(t.TransportError):
        transport.request('CheckConnectivity')


def test_bad_xml_and_invalid_reference_blocked(transport):
    with pytest.raises(t.TransportError, match='DTD'):
        t.parse(b'<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>')
    with pytest.raises(t.TransportError, match='Reference'):
        transport.request('Upload', application='IMP', reference='INVENTED', payload=b'<x/>')
    with pytest.raises(t.TransportError, match='5 MiB'):
        transport.application_request(b'x' * (t.MAX_PAYLOAD + 1), 'IMP', 'LOCAL000000001')


@pytest.mark.parametrize('system,application', [('import','IMP'),('export','EXP')])
def test_actual_declaration_xml_in_valid_signed_transport(transport, system, application):
    from services.customs_messages import tulli_import, tulli_export
    from services.customs_messages.tulli_common import assert_valid
    decl = json.loads((FIX.parent / 'export-declaration.json').read_text())
    decl['message_id'] = 'LOCAL000000001'
    if system == 'import':
        decl['type'] = 'import'
        decl['importer'] = copy.deepcopy(decl['declarant'])
        decl['transport'].update(destination_country='FI', dispatch_country='CN')
        decl['items'][0]['taric_code'] = '8712003010'
    payload = (tulli_import if system == 'import' else tulli_export).build_message(decl)
    raw = transport.request('Upload', application=application, reference=decl['message_id'], payload=payload)
    request = t.parse(raw).find('{' + t.SOAP + '}Body')[0]
    signed = t.parse(t.decode(request.findtext('{' + t.CT + '}ApplicationRequestMessage')))
    business = t.decode(signed.findtext('{' + t.APP + '}ApplicationContent/{' + t.MSG + '}Content'))
    assert assert_valid(business, system)['valid']
    assert t.parse(business).findtext('{*}Identification') == decl['message_id']


def test_real_http_client_uses_verified_mtls_official_endpoint_and_no_redirects(transport, monkeypatch):
    original = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == t.ENDPOINTS['test']
        assert request.method == 'POST'
        assert request.headers['content-type'] == 'application/soap+xml; charset=utf-8'
        t.validate(t.parse(request.content).find('{' + t.SOAP + '}Body')[0], 'service')
        return httpx.Response(200, content=(FIX / 'Example03_CheckConnectivityResponse.xml').read_bytes())
    def factory(**kwargs):
        assert isinstance(kwargs['verify'], ssl.SSLContext)
        assert kwargs['verify'].verify_mode == ssl.CERT_REQUIRED
        assert kwargs['verify'].check_hostname
        assert hashlib.sha256(t.SERVER_CA.read_bytes()).hexdigest() == 'f5a66b266f559b9bc1fe9d88ae44fa6308a4d6b878ba93acac053135c7900ae2'
        assert 'd3ed3fc40ad26b52e001e1e18f4b9449529deb75a81d5eb680d7b62db23ba96d' in {
            hashlib.sha256(cert).hexdigest() for cert in kwargs['verify'].get_ca_certs(binary_form=True)}
        assert kwargs['timeout'] >= 120
        assert kwargs['follow_redirects'] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(t.httpx, 'AsyncClient', factory)
    asyncio.run(transport.call('CheckConnectivity'))
    assert len(calls) == 1


def test_attachment_generated_against_official_schema(transport):
    reference = 'LOCAL000000001'
    attachment = {'title':'Invoice', 'type':'N380', 'mime_type':'application/pdf', 'customs_requested':True}
    raw = transport.request('UploadAttachment', application='IMP', reference=reference, storage_id='IMP123456',
                            payload=b'%PDF-1.7\nLocal protocol test only', attachment=attachment)
    request = t.parse(raw).find('{' + t.SOAP + '}Body')[0]
    signed = t.decode(request.findtext('{' + t.CT + '}AttachmentRequestMessage'))
    t.validate(t.parse(signed), 'AttachmentRequest')
    XMLVerifier().verify(signed, x509_cert=Path(transport.settings.certificate).read_bytes())


def test_document_download_uses_official_document_id(transport):
    request = transport.request('Download', storage_id='IMP123456', document_id='CD-48368')
    assert t.parse(request).findtext('.//{' + t.CT + '}DocumentId') == 'CD-48368'
