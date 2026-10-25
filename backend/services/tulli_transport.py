"""Finnish Customs direct message exchange v1.21 (technical guide 25 June 2026).

Transport receipts are not customs decisions. No retries of Upload: an interchange
reference is consumed even on rejection; uncertain delivery must be reconciled.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import re
import ssl

import httpx
from lxml import etree as E
from signxml import XMLSigner, XMLVerifier, methods
from signxml.algorithms import CanonicalizationMethod

SCHEMAS = Path(__file__).resolve().parents[1] / 'data/xsd/tulli/transport/1.21'
SOAP = 'http://www.w3.org/2003/05/soap-envelope'
CS = 'http://tulli.fi/ws/corporateservice/v1'
CT = 'http://tulli.fi/ws/corporateservicetypes/v1'
APP = 'http://tulli.fi/schema/corporateservice/appl/v1'
MSG = 'http://tulli.fi/schema/corporateservice/v1'
DS = 'http://www.w3.org/2000/09/xmldsig#'
XS = 'http://www.w3.org/2001/XMLSchema'
APPLICATIONS = {'import': 'IMP', 'export': 'EXP', 'transit': 'TRA'}
ENDPOINTS = {'test': 'https://ws-customertest.tulli.fi/services/DirectMessageExchange',
             'production': 'https://ws.tulli.fi/services/DirectMessageExchange'}
MAX_PAYLOAD = 5 * 1024 * 1024
SERVER_CA = Path(__file__).resolve().parents[1] / 'data/certificates/tulli/dvv-root-g3-rsa.pem'


def server_ssl_context():
    """Trust DVV's published root for Tulli only (technical guide §10).

    Keep system roots, certificate and hostname verification. The bundled public
    CA comes from DVV's HTTPS directory, never from an unverified peer handshake.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(SERVER_CA))
    # The production server omits this intermediate from its presented chain.
    context.load_verify_locations(cafile=str(SERVER_CA.with_name('dvv-service-g5r.pem')))
    return context


class TransportError(ValueError):
    pass


def parse(raw):
    raw = raw.encode() if isinstance(raw, str) else raw
    if len(raw) > 24 * 1024 * 1024:
        raise TransportError('Tulli response exceeds size limit')
    root = E.fromstring(raw, E.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    if root.getroottree().docinfo.doctype:
        raise TransportError('DTD is not permitted in Tulli messages')
    return root


def encode(raw):
    return base64.b64encode(raw.encode() if isinstance(raw, str) else raw).decode('ascii')


def decode(value):
    try:
        return base64.b64decode(''.join((value or '').split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise TransportError('Invalid Base64 in Tulli response') from exc


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def child(parent, namespace, name, text=None):
    node = E.SubElement(parent, E.QName(namespace, name))
    if text is not None:
        node.text = str(text)
    return node


@lru_cache(maxsize=8)
def schema(name):
    if name == 'service':
        # Use the inline schema from the official WSDL, resolving its sibling imports.
        doc = E.parse(str(SCHEMAS / 'CustomsCorporateService_soap1_2.wsdl'))
        source = doc.find('.//{' + XS + '}schema')
        node = E.Element(source.tag, attrib=dict(source.attrib), nsmap=source.nsmap)
        node.extend(deepcopy(c) for c in source)
        node.set('{http://www.w3.org/XML/1998/namespace}base', str(SCHEMAS) + '/')
        return E.XMLSchema(node)
    return E.XMLSchema(E.parse(str(SCHEMAS / (name + '.xsd'))))


def validate(node, name):
    try:
        schema(name).assertValid(node)
    except E.DocumentInvalid as exc:
        # Schema path/error only; never expose message contents or certificates.
        raise TransportError('Invalid Tulli ' + name + ' XML: ' + exc.error_log.last_error.message[:300]) from exc


@dataclass(frozen=True)
class Settings:
    mode: str
    declarant_id: str
    builder_id: str
    intermediary_id: str
    reference_prefix: str
    certificate: str
    private_key: str
    customer_approved: bool = False
    endpoint: str = ''

    def missing(self):
        missing = []
        from services.demo_world.boundary import authority_configuration_error
        world_error = authority_configuration_error(self.mode, self.endpoint)
        if world_error:
            missing.append(world_error)
        if self.mode not in ENDPOINTS:
            missing.append('TULLI_MODE=test or production')
        for name, value in [('TULLI_DECLARANT_ID', self.declarant_id),
                            ('TULLI_BUILDER_ID', self.builder_id),
                            ('TULLI_INTERMEDIARY_ID', self.intermediary_id)]:
            if not re.fullmatch(r'[A-Z]{2}[A-Za-z0-9-]{7,15}', value or ''):
                missing.append(name)
        if not re.fullmatch(r'[A-Z]{5}', self.reference_prefix or ''):
            missing.append('TULLI_REFERENCE_PREFIX (five letters assigned by Tulli)')
        for name, value in [('TULLI_CLIENT_CERT', self.certificate), ('TULLI_CLIENT_KEY', self.private_key)]:
            if not value or not Path(value).is_file():
                missing.append(name)
        if not self.customer_approved:
            missing.append('Tulli message-exchange customer status (Authorisations and Decisions; TULLI_CUSTOMER_APPROVED)')
        if self.endpoint and self.endpoint != ENDPOINTS.get(self.mode):
            from services.demo_world.boundary import authority_endpoint_allowed
            if self.mode != 'test' or not authority_endpoint_allowed(self.endpoint):
                missing.append('TULLI_ENDPOINT must match the official endpoint for this environment')
        return missing

    def require_ready(self):
        if self.missing():
            raise TransportError('Tulli configuration incomplete: ' + ', '.join(self.missing()))


class Transport:
    def __init__(self, settings: Settings):
        self.settings = settings

    def signed(self, node, schema_name):
        cfg = self.settings
        cfg.require_ready()
        cert = Path(cfg.certificate).read_bytes()
        signed = XMLSigner(method=methods.enveloped, signature_algorithm='rsa-sha256',
                           digest_algorithm='sha256',
                           c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0).sign(
                               node, key=Path(cfg.private_key).read_bytes(), cert=cert)
        # signxml uses URI="" for a document without an ID; this is mandatory in §12.
        if signed.find('.//{' + DS + '}Reference').get('URI') != '':
            raise TransportError('Tulli requires a whole-document XML signature')
        validate(signed, schema_name)
        XMLVerifier().verify(signed, x509_cert=cert)
        return E.tostring(signed, xml_declaration=True, encoding='UTF-8')

    def application_request(self, payload, application, reference):
        cfg = self.settings
        payload = payload.encode() if isinstance(payload, str) else payload
        if len(payload) > MAX_PAYLOAD:
            raise TransportError('Tulli payload exceeds 5 MiB')
        parse(payload)
        if application not in APPLICATIONS.values():
            raise TransportError('Unsupported Tulli application')
        if not re.fullmatch(re.escape(cfg.reference_prefix) + r'[A-Za-z0-9_-]{1,9}', reference):
            raise TransportError('Reference must use the Tulli-assigned prefix and be 6–14 characters')
        root = E.Element(E.QName(APP, 'ApplicationRequest'), nsmap={'req': APP, 'mess': MSG})
        for key, value in [('MessageBuilderBusinessId', cfg.builder_id),
                           ('MessageBuilderSoftwareInfo', 'Huolitsija DME 1.21'),
                           ('DeclarantBusinessId', cfg.declarant_id), ('Timestamp', stamp()),
                           ('Application', application), ('Reference', reference),
                           ('Environment', cfg.mode.upper())]:
            child(root, APP, key, value)
        content = child(root, APP, 'ApplicationContent')
        child(content, MSG, 'Content', encode(payload))
        child(content, MSG, 'ContentFormat', 'application/xml')
        return self.signed(root, 'ApplicationRequest')

    def request(self, operation, *, application=None, reference=None, payload=None,
                storage_id=None, start=None, end=None, document_id=None, attachment=None):
        self.settings.require_ready()
        root = E.Element(E.QName(SOAP, 'Envelope'), nsmap={'soap': SOAP, 'cs': CS, 'cst': CT})
        child(root, SOAP, 'Header')
        body = child(root, SOAP, 'Body')
        name = 'CheckRequest' if operation == 'CheckConnectivity' else operation + 'Request'
        request = child(body, CS, name)
        header = child(request, CT, 'RequestHeader')
        for key, value in [('IntermediaryBusinessId', self.settings.intermediary_id), ('Timestamp', stamp()),
                           ('Language', 'FI'), ('IntermediarySoftwareInfo', 'Huolitsija DME 1.21')]:
            child(header, CT, key, value)
        if operation == 'Upload':
            child(request, CT, 'ApplicationRequestMessage', encode(self.application_request(payload, application, reference)))
        elif operation == 'UploadAttachment':
            if not attachment or not isinstance(attachment.get('customs_requested'), bool):
                raise TransportError('Attachment metadata and CustomsRequest must be supplied')
            if len(payload) > MAX_PAYLOAD:
                raise TransportError('Tulli attachment exceeds 5 MiB')
            cfg = self.settings
            att = 'http://tulli.fi/schema/corporateservice/attch/v1'
            root_attachment = E.Element(E.QName(att, 'AttachmentRequest'), nsmap={'att': att, 'mess': MSG})
            for key, value in [('MessageBuilderBusinessId',cfg.builder_id), ('DeclarantBusinessId',cfg.declarant_id),
                ('Application',application), ('AttachmentTitle',attachment.get('title')),
                ('AttachmentDescription',attachment.get('description')), ('AttachmentDocumentType',attachment.get('type')),
                ('RelatedMessageStorageId',storage_id), ('CustomsRequest',str(attachment['customs_requested']).lower()),
                ('AttachmentRelationIdentifier',attachment.get('relation_id')), ('LanguageCode','FI')]:
                if value is not None:
                    child(root_attachment, att, key, value)
            content = child(root_attachment, att, 'AttachmentContent')
            child(content, MSG, 'Content', encode(payload))
            child(content, MSG, 'ContentFormat', attachment.get('mime_type'))
            child(request, CT, 'Reference', reference)
            child(request, CT, 'AttachmentRequestMessage', encode(self.signed(root_attachment, 'AttachmentRequest')))
        elif operation == 'DownloadList':
            criteria = child(request, CT, 'DownloadMessageListFilteringCriteria')
            child(criteria, CT, 'StartTimestamp', start)
            child(criteria, CT, 'EndTimestamp', end)
            child(criteria, CT, 'MessageStatus', 'ALL')
            if application:
                child(criteria, CT, 'Application', application)
        elif operation == 'Download':
            criteria = child(request, CT, 'DownloadMessageFilteringCriteria')
            child(criteria, CT, 'MessageStorageId', storage_id)
            if document_id:
                child(criteria, CT, 'DocumentId', document_id)
        elif operation == 'CheckConnectivity':
            echo_ns = 'http://tulli.fi/schema/DME/echocontent/v1'
            echo = E.Element(E.QName(echo_ns, 'EchoContent'), nsmap={None: echo_ns})
            child(echo, echo_ns, 'Text', 'Huolitsija connectivity check')
            child(request, CT, 'EchoRequest', encode(self.signed(echo, 'EchoContent')))
        else:
            raise TransportError('Unsupported Tulli operation')
        validate(request, 'service')
        return E.tostring(root, xml_declaration=True, encoding='UTF-8')

    @staticmethod
    def response(raw, operation):
        root = parse(raw)
        if root.tag != E.QName(SOAP, 'Envelope'):
            raise TransportError('Expected SOAP 1.2 response')
        body = root.find('{' + SOAP + '}Body')
        if body is None or len(body) != 1:
            raise TransportError('Expected exactly one SOAP response')
        node = body[0]
        if node.tag == E.QName(SOAP, 'Fault'):
            raise TransportError('Tulli returned a SOAP fault; reconcile the transmission before retrying')
        name = 'CheckResponse' if operation == 'CheckConnectivity' else operation + 'Response'
        if node.tag != E.QName(CS, name):
            raise TransportError('Unexpected Tulli response operation')
        validate(node, 'service')
        code = node.findtext('{' + CT + '}ResponseHeader/{' + CT + '}ResponseCode')
        if code != '000':
            raise TransportError('Tulli transport response code ' + str(code))
        return node

    async def call(self, operation, **values):
        request = self.request(operation, **values)
        cfg = self.settings
        from services.demo_world import boundary
        if boundary.on():
            return self.response(await boundary.authority_post("tulli", request, operation), operation)
        context = server_ssl_context()
        context.load_cert_chain(cfg.certificate, cfg.private_key)
        # §13.5 requires at least 120 seconds. Never disable verification or follow redirects.
        async with httpx.AsyncClient(verify=context, timeout=130, follow_redirects=False) as client:
            response = await client.post(ENDPOINTS[cfg.mode], content=request,
                                         headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
        response.raise_for_status()
        return self.response(response.content, operation)


def application_response(node, *, application, declarant_id, storage_id):
    """Check the inner identity before letting business XML influence a declaration."""
    raw = decode(node.findtext('{' + CT + '}ApplicationResponseMessage'))
    root = parse(raw)
    validate(root, 'ApplicationResponse')
    for key, expected in [('Application', application), ('DeclarantBusinessId', declarant_id),
                          ('MessageStorageId', storage_id)]:
        if root.findtext('{' + APP + '}' + key) != expected:
            raise TransportError('Tulli response identity mismatch: ' + key)
    # Authority responses are authenticated by the verified mTLS connection. The
    # official examples have no signature; do not trust a supplied arbitrary certificate.
    content = root.find('{' + APP + '}ApplicationResponseContent')
    if content is None or content.findtext('{' + MSG + '}ContentFormat') != 'application/xml':
        raise TransportError('Expected Tulli XML business response')
    return decode(content.findtext('{' + MSG + '}Content')), root.findtext('{' + APP + '}ControlReference')
