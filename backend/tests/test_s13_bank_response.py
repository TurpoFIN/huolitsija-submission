"""Validate independently captured Nordea demo bytes and reject altered/unsigned responses."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lxml import etree
from services import bank_api, bank_response as br

RAW = (Path(__file__).parent / 'fixtures/bank/nordea-getuserinfo-demo.xml').read_bytes()
NOW = datetime(2026, 10, 24, 22, 44, 32, tzinfo=timezone.utc)
EXPECTED = dict(operation='getUserInfo', request_id='2d965f37a9084a549288',
                customer_id='11111111', target_id='11111111A1')


@pytest.fixture(autouse=True)
def pinned_default(monkeypatch):
    monkeypatch.delenv('BANK_API_RESPONSE_CERT', raising=False)
    monkeypatch.setenv('BANK_API_PROVIDER', 'nordea')


def test_published_demo_response_verifies_both_layers_and_parses_code():
    out = bank_api.parse_response(RAW, expected=EXPECTED, now=NOW)
    assert out['response_signature_verified'] is True
    assert out['code'] == '24' and out['text'] == 'Content not found.'
    assert out['content'] is None


@pytest.mark.parametrize('mutation', ['body', 'signature', 'unsigned', 'duplicate_body', 'duplicate_id',
                                     'wrapping', 'external_reference', 'sha1', 'xml_attributes', 'doctype'])
def test_tampered_or_ambiguous_response_is_rejected(mutation):
    d = br.parse_xml(RAW)
    sig = d.find('.//{' + br.DS + '}Signature')
    body = d.find('{' + br.ENV + '}Body')
    if mutation == 'body':
        body.find('.//{' + br.MOD + '}ResponseCode').text = '00'
    elif mutation == 'signature':
        sig.find('{' + br.DS + '}SignatureValue').text = base64.b64encode(b'bad signature').decode()
    elif mutation == 'unsigned':
        sig.getparent().remove(sig)
    elif mutation == 'duplicate_body':
        d.append(deepcopy(body))
    elif mutation == 'duplicate_id':
        etree.SubElement(body, 'extra', Id=body.get('{' + br.WSU + '}Id'))
    elif mutation == 'wrapping':
        header = d.find('{' + br.ENV + '}Header')
        d.remove(body);header.append(body)
        etree.SubElement(d, '{' + br.ENV + '}Body')
    elif mutation == 'external_reference':
        sig.find('.//{' + br.DS + '}Reference').set('URI', 'https://example.invalid/forged')
    elif mutation == 'sha1':
        sig.find('.//{' + br.DS + '}SignatureMethod').set('Algorithm', br.DS + 'rsa-sha1')
    elif mutation == 'xml_attributes':
        d.set('{http://www.w3.org/XML/1998/namespace}base', 'https://example.invalid/')
    raw = etree.tostring(d)
    if mutation == 'doctype':raw = b'<!DOCTYPE Envelope [<!ENTITY x "unsafe">]>' + raw
    with pytest.raises(bank_api.BankApiError) as err:
        bank_api.parse_response(raw, expected=EXPECTED, now=NOW)
    assert err.value.code == 'bank_api_response_signature'


@pytest.mark.parametrize('field', ['request_id', 'customer_id', 'target_id', 'operation'])
def test_valid_response_for_another_request_cannot_be_used(field):
    with pytest.raises(bank_api.BankApiError):
        bank_api.parse_response(RAW, expected={**EXPECTED, field: 'different'}, now=NOW)


@pytest.mark.parametrize('when', [NOW - timedelta(hours=1), NOW + timedelta(hours=1)])
def test_replayed_or_future_signed_response_rejected(when):
    with pytest.raises(bank_api.BankApiError):
        bank_api.parse_response(RAW, expected=EXPECTED, now=when)


def test_certificate_expiration_and_unconfigured_other_bank_fail_closed():
    with pytest.raises(ValueError):
        br.trusted_certificate('nordea', now=datetime(2028, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError):br.trusted_certificate('custom')


@pytest.mark.parametrize('mutation', ['code', 'signature', 'unsigned', 'reference'])
def test_inner_signature_is_verified_independently(mutation):
    d=br.parse_xml(RAW)
    app=br.parse_xml(base64.b64decode(d.find('.//{' + br.MOD + '}ApplicationResponse').text))
    sig=app.find('{' + br.DS + '}Signature')
    if mutation == 'code':app.find('{' + br.BXD + '}ResponseCode').text='00'
    elif mutation == 'signature':sig.find('{' + br.DS + '}SignatureValue').text=base64.b64encode(b'bad').decode()
    elif mutation == 'unsigned':app.remove(sig)
    else:sig.find('.//{' + br.DS + '}Reference').set('URI','#unsigned')
    with pytest.raises(Exception):
        br.verify_xml(app, br.trusted_certificate('nordea',now=NOW),location='./',references=[''])


def test_embedded_attacker_certificate_does_not_establish_bank_trust():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from signxml import XMLSigner, InvalidSignature
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Untrusted test signer')])
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(1).not_valid_before(NOW-timedelta(days=1)).not_valid_after(NOW+timedelta(days=1))
          .sign(key,hashes.SHA256()))
    app=etree.fromstring(b'<ApplicationResponse xmlns="http://bxd.fi/xmldata/"><ResponseCode>00</ResponseCode></ApplicationResponse>')
    signed=XMLSigner(c14n_algorithm=br.C14N).sign(app,key=key,cert=cert.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(InvalidSignature):
        br.verify_xml(signed,br.trusted_certificate('nordea',now=NOW),location='./',references=[''],now=NOW)
