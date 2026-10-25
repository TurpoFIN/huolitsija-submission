"""Strict verification of Finnish WS bank responses, before interpreting any business data.

Trust comes from the pinned bank signing certificate, never a key supplied in the response.
Only the authenticated SOAP Body and authenticated ApplicationResponse are returned.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from lxml import etree
from signxml import XMLVerifier, SignatureConfiguration, SignatureMethod, DigestAlgorithm

ENV = 'http://schemas.xmlsoap.org/soap/envelope/'
WSSE = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'
WSU = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd'
DS = 'http://www.w3.org/2000/09/xmldsig#'
MOD = 'http://model.bxd.fi'
COR = 'http://bxd.fi/CorporateFileService'
BXD = 'http://bxd.fi/xmldata/'
C14N = 'http://www.w3.org/TR/2001/REC-xml-c14n-20010315'
EXC = 'http://www.w3.org/2001/10/xml-exc-c14n#'
PIN = Path(__file__).resolve().parents[1] / 'data/bank/nordea-response-signing-2025.pem'


def parse_xml(raw: bytes):
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Bank response exceeds the supported size')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(raw, parser=parser)
    if root.getroottree().docinfo.doctype or any(isinstance(el, etree._Entity) for el in root.iter()):
        raise ValueError('DTD and entities are not permitted')
    # This profile never uses inherited xml:base/lang/space. Reject rather than dropping
    # their ancestor context when canonicalizing a subtree.
    if root.xpath('//@xml:*', namespaces={'xml': 'http://www.w3.org/XML/1998/namespace'}):
        raise ValueError('Inherited XML attributes are unsupported in bank responses')
    ids = [v for el in root.iter() for k, v in el.attrib.items()
           if etree.QName(k).localname.lower() == 'id']
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate XML IDs')
    return root


class BankXMLVerifier(XMLVerifier):
    def _c14n(self, nodes, algorithm, inclusive_ns_prefixes=None):
        # libxml2 can inject spurious xmlns="" into descendants of an in-tree
        # default-namespace SignedInfo. Serialize/reparse with the full in-scope
        # namespace map before canonicalization. No whitespace or namespaces are
        # removed, and there is no alternate-signature or digest fallback.
        nodes = nodes if isinstance(nodes, list) else [nodes]
        detached = [parse_xml(etree.tostring(n, with_tail=False)) for n in nodes]
        return super()._c14n(detached, algorithm, inclusive_ns_prefixes)


def one(root, path):
    elements = root.findall(path)
    if len(elements) != 1:
        raise ValueError('Bank response has a missing or duplicate required element')
    return elements[0]


def verify_xml(root, cert, *, location, references, now=None):
    signature = one(root, location + '{' + DS + '}Signature')
    if len(root.findall('.//{' + DS + '}Signature')) != 1:
        raise ValueError('Unexpected additional XML signature')
    si = one(signature, '{' + DS + '}SignedInfo')
    cm = one(si, '{' + DS + '}CanonicalizationMethod').get('Algorithm')
    if cm not in (C14N, EXC):
        raise ValueError('Unsupported canonicalization algorithm')
    refs = si.findall('{' + DS + '}Reference')
    if len(refs) != len(references) or {r.get('URI') for r in refs} != set(references):
        raise ValueError('Signature does not cover the expected response elements')
    for ref in refs:
        transforms = [t.get('Algorithm') for t in ref.findall('{' + DS + '}Transforms/{' + DS + '}Transform')]
        expected = ([DS + 'enveloped-signature', C14N] if ref.get('URI') == '' else [EXC])
        if transforms != expected:
            raise ValueError('Unexpected XML signature transforms')
    config = SignatureConfiguration(location=location, expect_references=len(references), verification_time=now,
                                    signature_methods=frozenset({SignatureMethod.RSA_SHA256}),
                                    digest_algorithms=frozenset({DigestAlgorithm.SHA256}))
    result = BankXMLVerifier().verify(root, x509_cert=cert, expect_config=config)
    return result if isinstance(result, list) else [result]


def trusted_certificate(provider, configured_path=None, *, now=None):
    path = Path(configured_path) if configured_path else PIN if provider == 'nordea' else None
    if path is None:
        raise ValueError('BANK_API_RESPONSE_CERT must name the trusted bank signing certificate')
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    instant = now or datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= instant <= cert.not_valid_after_utc:
        raise ValueError('Bank response signing certificate is not currently valid')
    return cert


def verify_response(raw, *, provider, certificate_path=None, expected=None, now=None):
    instant = now or datetime.now(timezone.utc)
    cert = trusted_certificate(provider, certificate_path, now=instant)
    root = parse_xml(raw)
    if root.tag != '{' + ENV + '}Envelope':
        raise ValueError('Expected SOAP envelope')
    body = one(root, '{' + ENV + '}Body')
    security = one(one(root, '{' + ENV + '}Header'), '{' + WSSE + '}Security')
    timestamp = one(security, '{' + WSU + '}Timestamp')
    bid, tid = body.get('{' + WSU + '}Id'), timestamp.get('{' + WSU + '}Id')
    if not bid or not tid or bid == tid:
        raise ValueError('Missing signed body or timestamp ID')
    verified = verify_xml(root, cert, location='./{' + ENV + '}Header/{' + WSSE + '}Security/',
                          references=['#' + bid, '#' + tid], now=instant)
    signed = {v.signed_xml.tag: v.signed_xml for v in verified if v.signed_xml is not None}
    if set(signed) != {'{' + ENV + '}Body', '{' + WSU + '}Timestamp'}:
        raise ValueError('Unexpected signed content')
    body, timestamp = signed['{' + ENV + '}Body'], signed['{' + WSU + '}Timestamp']
    created = datetime.fromisoformat(one(timestamp, '{' + WSU + '}Created').text.replace('Z', '+00:00'))
    expires = datetime.fromisoformat(one(timestamp, '{' + WSU + '}Expires').text.replace('Z', '+00:00'))
    if created.tzinfo is None or expires.tzinfo is None or not timedelta(0) < expires - created <= timedelta(minutes=10):
        raise ValueError('Invalid bank response validity interval')
    if created > instant + timedelta(seconds=90) or expires < instant - timedelta(seconds=90):
        raise ValueError('Stale or future bank response')
    if len(body) != 1:
        raise ValueError('Expected a single signed bank operation')
    operation = body[0]
    header = one(operation, '{' + MOD + '}ResponseHeader')
    encoded = one(operation, '{' + MOD + '}ApplicationResponse').text or ''
    application = parse_xml(base64.b64decode(''.join(encoded.split()), validate=True))
    if application.tag != '{' + BXD + '}ApplicationResponse':
        raise ValueError('Expected ApplicationResponse')
    app_result = verify_xml(application, cert, location='./', references=[''], now=instant)[0]
    app = app_result.signed_xml
    if app is None or app.tag != '{' + BXD + '}ApplicationResponse':
        raise ValueError('ApplicationResponse root was not authenticated')
    if expected:
        if operation.tag != '{' + COR + '}' + expected['operation'] + 'out':
            raise ValueError('Response operation does not match request')
        for field, value in [('RequestId', expected['request_id']), ('SenderId', expected['customer_id']),
                             ('ReceiverId', expected['target_id'])]:
            if one(header, '{' + MOD + '}' + field).text != value:
                raise ValueError('Response does not match the outstanding request')
        if one(app, '{' + BXD + '}CustomerId').text != expected['customer_id']:
            raise ValueError('ApplicationResponse belongs to another customer')
    return body, app
