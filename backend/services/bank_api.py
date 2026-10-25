"""Bank connection for supplier payments in BANK_PAYMENT_MODE=api: Finnish bank Web Services (the "Web Services"
corporate file transfer channel of Finnish banks: SOAP 1.1 + WS-Security X.509 signature, ApplicationRequest signed
with an enveloped XML-DSig, namespaces http://bxd.fi/CorporateFileService, http://model.bxd.fi, http://bxd.fi/xmldata/).

    BANK_PAYMENT_MODE=api
    BANK_API_PROVIDER=nordea          # nordea (default preset) | op | danske | samlink | custom
    BANK_API_URL=...                  # optional override of the preset endpoint
    BANK_API_CUSTOMER_ID=...          # WS user id from the bank agreement
    BANK_API_TARGET_ID=...            # file group (Nordea: <customer id>A1)
    BANK_API_CERT=/path/cert.pem      # bank-issued signing certificate + key (or BANK_API_P12 + BANK_API_P12_PASSWORD)
    BANK_API_KEY=/path/key.pem
    BANK_API_ENVIRONMENT=PRODUCTION   # PRODUCTION | TEST
    BANK_API_FILETYPE_PAIN001 / _PAIN002 / _CAMT054   # override the preset file types

Company operation requires the bank agreement and certificates plus a verified payment round trip.
BANK_API_RESPONSE_CERT optionally names a PEM bank signing certificate; Nordea uses the bundled published pin.
Other providers require an explicit trusted response certificate.

Nordea publishes a shared demo certificate for this channel (customer 11111111, target 11111111A1, environment
PRODUCTION, "payment files are not checked"): BANK_API_PROVIDER=nordea BANK_API_DEMO=true downloads it from
nordea.fi and uses it. That is how this adapter was exercised against the real Nordea endpoint; see
tests/test_s13_pay_bank_api.py. This demonstrates connection/signature acceptance only; it does not prove a company payment. The OP / Danske / Samlink presets follow the same
published Web Services specification but need an agreement certificate to test (their test environments are not public).

Both response signatures, signed timestamp and request binding are checked before business data is read.
The response certificate is pinned independently of the returned XML. Verification failure is not retry permission.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

from services.sepa import PaymentError, message_name

log = logging.getLogger("bank_api")

BXD = "http://bxd.fi/xmldata/"
DS = "http://www.w3.org/2000/09/xmldsig#"
ENV = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
COR = "http://bxd.fi/CorporateFileService"
MOD = "http://model.bxd.fi"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
EXC = "http://www.w3.org/2001/10/xml-exc-c14n#"
RSA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
X509V3 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3"
B64BIN = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"

PROVIDERS = {
    # Nordea Corporate File Service, SHA-2 endpoint (SHA-1 refused since 02/2025). CPS service description 2026:
    # pain.001 (also .001.09) uploaded as NDCORPAYS, status reports NDCORPAYL (pain.002), camt.054 NDCAMT54L.
    "nordea": {"url": "https://filetransfer.nordea.com/services/CorporateFileService/sha2",
               "pain001": "NDCORPAYS", "pain002": "NDCORPAYL", "camt054": "NDCAMT54L", "proven": False},
    # OP Corporate Web Services (agreement needed; customer test environment wsk.asiakastesti.op.fi).
    "op": {"url": "https://wsk.op.fi/services/CorporateFileServiceV2",
           "pain001": "pain.001.001.09", "pain002": "pain.002.001.10", "camt054": "camt.054.001.02", "proven": False},
    "danske": {"url": "https://businessws.danskebank.com/financialservice/edifileservice.asmx",
               "pain001": "pain.001.001.09", "pain002": "pain.002.001.10", "camt054": "camt.054.001.02", "proven": False},
    "samlink": {"url": "https://ws.samlink.fi/services/CorporateFileService",
                "pain001": "XP", "pain002": "XS", "camt054": "XT", "proven": False},
}
NORDEA_DEMO = {"zip_url": "https://www.nordea.fi/Images/147-487388/Certificate_PRODUCTION_DEMO2024Nov.zip",
               "p12_password": "WSNDEA1234", "customer_id": "11111111", "target_id": "11111111A1"}


class BankApiError(PaymentError):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def provider() -> str:
    return env("BANK_API_PROVIDER", "nordea").lower() or "nordea"


def demo() -> bool:
    return env("BANK_API_DEMO").lower() in ("1", "true", "yes", "on")


def settings() -> dict:
    p = provider()
    preset = PROVIDERS.get(p, {})
    d = NORDEA_DEMO if (demo() and p == "nordea") else {}
    return {"provider": p, "url": env("BANK_API_URL") or preset.get("url"),
            "customer_id": env("BANK_API_CUSTOMER_ID") or d.get("customer_id"),
            "target_id": env("BANK_API_TARGET_ID") or d.get("target_id"),
            "environment": env("BANK_API_ENVIRONMENT", "PRODUCTION").upper(),
            "software_id": env("BANK_API_SOFTWARE_ID", "Huolitsija 1.0")[:80],
            "pain001": env("BANK_API_FILETYPE_PAIN001") or preset.get("pain001"),
            "pain002": env("BANK_API_FILETYPE_PAIN002") or preset.get("pain002"),
            "camt054": env("BANK_API_FILETYPE_CAMT054") or preset.get("camt054"),
            "demo": bool(d), "proven": bool(preset.get("proven"))}


def status() -> dict:
    """What api mode would use (no secrets)."""
    s = settings()
    missing = [k for k in ("url", "customer_id", "target_id", "pain001") if not s.get(k)]
    if not s["demo"] and not (env("BANK_API_P12") or (env("BANK_API_CERT") and env("BANK_API_KEY"))):
        missing.append("certificate (BANK_API_CERT + BANK_API_KEY or BANK_API_P12)")
    return {**s, "configured": not missing, "missing": missing}


# ------------------------------------------------------------------------------------------------ credentials
_CREDS: dict = {}


async def _credentials():
    """(private key, certificate) from BANK_API_CERT/KEY (PEM), BANK_API_P12, or Nordea's public demo certificate."""
    key_id = (provider(), demo(), env("BANK_API_CERT"), env("BANK_API_KEY"), env("BANK_API_P12"))
    if _CREDS.get("id") == key_id:
        return _CREDS["key"], _CREDS["cert"]
    if env("BANK_API_P12"):
        with open(env("BANK_API_P12"), "rb") as fh:
            key, cert, _ = pkcs12.load_key_and_certificates(fh.read(), env("BANK_API_P12_PASSWORD").encode() or None)
    elif env("BANK_API_CERT") and env("BANK_API_KEY"):
        with open(env("BANK_API_KEY"), "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), env("BANK_API_KEY_PASSWORD").encode() or None)
        with open(env("BANK_API_CERT"), "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    elif demo() and provider() == "nordea":
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(env("BANK_API_DEMO_CERT_URL") or NORDEA_DEMO["zip_url"])
            r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in zf.namelist() if n.lower().endswith((".p12", ".pfx")))
        key, cert, _ = pkcs12.load_key_and_certificates(zf.read(name), NORDEA_DEMO["p12_password"].encode())
    else:
        raise BankApiError("Bank API certificate is not configured: set BANK_API_CERT and BANK_API_KEY (or BANK_API_P12).",
                           "Pankkiyhteyden varmennetta ei ole asetettu: aseta BANK_API_CERT ja BANK_API_KEY (tai BANK_API_P12).",
                           code="bank_api_config")
    if cert.not_valid_after_utc < datetime.now(timezone.utc):
        raise BankApiError(f"The bank certificate expired on {cert.not_valid_after_utc.date()}; renew it.",
                           f"Pankin varmenne vanheni {cert.not_valid_after_utc.date()}; uusi se.", code="bank_api_cert")
    _CREDS.update(id=key_id, key=key, cert=cert)
    return key, cert


# ------------------------------------------------------------------------------------------------ messages
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _sub(parent, ns, tag, text=None, **attrs):
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", **attrs)
    if text is not None:
        el.text = str(text)
    return el


def application_request(command: str, s: dict, key, cert, *, file_type: str | None = None, file_refs=None,
                        content: bytes | None = None, status: str | None = None) -> bytes:
    """ApplicationRequest (http://bxd.fi/xmldata/) in schema order, enveloped XML-DSig (C14N 20010315, RSA-SHA256),
    signature element with the ds: prefix (Nordea rejects a default-namespace Signature: code 18)."""
    ar = etree.Element(f"{{{BXD}}}ApplicationRequest", nsmap={None: BXD})
    _sub(ar, BXD, "CustomerId", s["customer_id"])
    _sub(ar, BXD, "Command", command)
    _sub(ar, BXD, "Timestamp", _now())
    if status:
        _sub(ar, BXD, "Status", status)
    _sub(ar, BXD, "Environment", s["environment"])
    if file_refs:
        fr = _sub(ar, BXD, "FileReferences")
        for r in file_refs:
            _sub(fr, BXD, "FileReference", r)
    if command != "GetUserInfo":
        _sub(ar, BXD, "TargetId", s["target_id"])
    _sub(ar, BXD, "SoftwareId", s["software_id"])
    if file_type:
        _sub(ar, BXD, "FileType", file_type)
    if content is not None:
        _sub(ar, BXD, "Content", _b64(content))
    digest = _b64(hashlib.sha256(etree.tostring(ar, method="c14n")).digest())
    sig = etree.SubElement(ar, f"{{{DS}}}Signature", nsmap={"ds": DS})
    si = _sub(sig, DS, "SignedInfo")
    _sub(si, DS, "CanonicalizationMethod", Algorithm=C14N)
    _sub(si, DS, "SignatureMethod", Algorithm=RSA256)
    ref = _sub(si, DS, "Reference", URI="")
    _sub(_sub(ref, DS, "Transforms"), DS, "Transform", Algorithm=DS + "enveloped-signature")
    _sub(ref, DS, "DigestMethod", Algorithm=SHA256)
    _sub(ref, DS, "DigestValue", digest)
    _sub(sig, DS, "SignatureValue", _b64(key.sign(etree.tostring(si, method="c14n"), padding.PKCS1v15(), hashes.SHA256())))
    _sub(_sub(_sub(sig, DS, "KeyInfo"), DS, "X509Data"), DS, "X509Certificate",
         _b64(cert.public_bytes(serialization.Encoding.DER)))
    return etree.tostring(ar, xml_declaration=True, encoding="UTF-8")


def soap_envelope(operation: str, ar_bytes: bytes, s: dict, key, cert) -> bytes:
    """SOAP 1.1 envelope with WS-Security: Timestamp (5 min), BinarySecurityToken, signature over Body + Timestamp
    (exclusive C14N, RSA-SHA256)."""
    body_id, ts_id, tok_id = (f"id-{uuid.uuid4().hex}" for _ in range(3))
    cert_b64 = _b64(cert.public_bytes(serialization.Encoding.DER))
    envl = etree.Element(f"{{{ENV}}}Envelope", nsmap={"soapenv": ENV, "wsu": WSU})
    sec = etree.SubElement(_sub(envl, ENV, "Header"), f"{{{WSSE}}}Security", nsmap={"wsse": WSSE})
    sec.set(f"{{{ENV}}}mustUnderstand", "1")
    created = datetime.now(timezone.utc)
    ts = _sub(sec, WSU, "Timestamp")
    ts.set(f"{{{WSU}}}Id", ts_id)
    _sub(ts, WSU, "Created", created.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    _sub(ts, WSU, "Expires", (created + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    bst = _sub(sec, WSSE, "BinarySecurityToken", cert_b64, EncodingType=B64BIN, ValueType=X509V3)
    bst.set(f"{{{WSU}}}Id", tok_id)
    body = _sub(envl, ENV, "Body")
    body.set(f"{{{WSU}}}Id", body_id)
    op = etree.SubElement(body, f"{{{COR}}}{operation}in", nsmap={"cor": COR, "mod": MOD})
    rh = _sub(op, MOD, "RequestHeader")
    _sub(rh, MOD, "SenderId", s["customer_id"])
    _sub(rh, MOD, "RequestId", uuid.uuid4().hex[:20])
    _sub(rh, MOD, "Timestamp", _now())
    _sub(rh, MOD, "Language", "EN")
    _sub(rh, MOD, "UserAgent", "Huolitsija")
    _sub(rh, MOD, "ReceiverId", s["target_id"])
    _sub(op, MOD, "ApplicationRequest", _b64(ar_bytes))
    sig = etree.SubElement(sec, f"{{{DS}}}Signature", nsmap={"ds": DS})
    si = _sub(sig, DS, "SignedInfo")
    _sub(si, DS, "CanonicalizationMethod", Algorithm=EXC)
    _sub(si, DS, "SignatureMethod", Algorithm=RSA256)
    for target_id, el in ((body_id, body), (ts_id, ts)):
        ref = _sub(si, DS, "Reference", URI=f"#{target_id}")
        _sub(_sub(ref, DS, "Transforms"), DS, "Transform", Algorithm=EXC)
        _sub(ref, DS, "DigestMethod", Algorithm=SHA256)
        _sub(ref, DS, "DigestValue", _b64(hashlib.sha256(etree.tostring(el, method="c14n", exclusive=True)).digest()))
    _sub(sig, DS, "SignatureValue",
         _b64(key.sign(etree.tostring(si, method="c14n", exclusive=True), padding.PKCS1v15(), hashes.SHA256())))
    _sub(_sub(_sub(sig, DS, "KeyInfo"), WSSE, "SecurityTokenReference"), WSSE, "Reference", URI=f"#{tok_id}",
         ValueType=X509V3)
    return etree.tostring(envl, xml_declaration=True, encoding="UTF-8")


def parse_response(content: bytes, *, bank_provider=None, expected=None, now=None) -> dict:
    """Verify both signature layers, then expose only authenticated business data."""
    from services.bank_response import verify_response, one
    try:
        body, ad = verify_response(content, provider=bank_provider or provider(),
                                   certificate_path=env("BANK_API_RESPONSE_CERT") or None,
                                   expected=expected, now=now)
        header = one(body[0], f"{{{MOD}}}ResponseHeader")
        out = {"soap_code": one(header, f"{{{MOD}}}ResponseCode").text,
               "soap_text": one(header, f"{{{MOD}}}ResponseText").text,
               "code": one(ad, f"{{{BXD}}}ResponseCode").text,
               "text": one(ad, f"{{{BXD}}}ResponseText").text,
               "file_descriptors": [], "content": None, "fault": None, "file_reference": None,
               "response_signature_verified": True}
        for fd in ad.iter(f"{{{BXD}}}FileDescriptor"):
            out["file_descriptors"].append({etree.QName(c).localname: c.text for c in fd})
        c = ad.find(f"{{{BXD}}}Content")
        if c is not None and c.text:
            out["content"] = base64.b64decode("".join(c.text.split()), validate=True)
        if out["file_descriptors"]:
            out["file_reference"] = out["file_descriptors"][0].get("FileReference")
        return out
    except Exception as exc:
        # No untrusted response text, certificates or payment data in errors/logs.
        raise BankApiError("Bank response authentication failed; do not automatically resend a payment file.",
                           "Pankin vastauksen aitoutta ei voitu varmistaa. Älä lähetä maksuaineistoa uudelleen automaattisesti.",
                           code="bank_api_response_signature") from exc


async def call(command: str, *, file_type: str | None = None, file_refs=None, content: bytes | None = None,
               status: str | None = None) -> dict:
    s = settings()
    if not s["url"] or not s["customer_id"] or not s["target_id"]:
        raise BankApiError("Bank API is not configured: set BANK_API_PROVIDER, BANK_API_CUSTOMER_ID and BANK_API_TARGET_ID.",
                           "Pankkiyhteyttä ei ole määritetty: aseta BANK_API_PROVIDER, BANK_API_CUSTOMER_ID ja BANK_API_TARGET_ID.",
                           code="bank_api_config")
    # Check configured trust before sending even the first byte of a payment file.
    from services.bank_response import trusted_certificate
    try:
        trusted_certificate(s["provider"], env("BANK_API_RESPONSE_CERT") or None)
    except (ValueError, OSError) as exc:
        raise BankApiError("Trusted bank response certificate is unavailable or expired.",
                           "Pankin vastauksen luotettu varmenne puuttuu tai on vanhentunut.",
                           code="bank_api_config") from exc
    key, cert = await _credentials()
    op = {"UploadFile": "uploadFile", "DownloadFileList": "downloadFileList", "DownloadFile": "downloadFile",
          "GetUserInfo": "getUserInfo", "DeleteFile": "deleteFile"}[command]
    ar = application_request(command, s, key, cert, file_type=file_type, file_refs=file_refs, content=content, status=status)
    envl = soap_envelope(op, ar, s, key, cert)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(s["url"], content=envl, headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""})
    request_id = etree.fromstring(envl).findtext(f".//{{{MOD}}}RequestId")
    out = parse_response(r.content, bank_provider=s["provider"], expected={
        "operation": op, "request_id": request_id, "customer_id": s["customer_id"], "target_id": s["target_id"]})
    out.update(http_status=r.status_code, provider=s["provider"], command=command, file_type=file_type,
               url=s["url"], demo=s["demo"])
    log.info("bank %s %s %s -> HTTP %s code %s %s", s["provider"], command, file_type or "", r.status_code, out["code"],
             out["text"])
    return out


# ------------------------------------------------------------------------------------------------ public API
async def submit(run: dict, xml: bytes) -> dict:
    """Upload the run's pain.001. Returns {provider, file_reference, code, text, ...}; raises BankApiError when the
    bank does not answer code 00."""
    s = settings()
    res = await call("UploadFile", file_type=s["pain001"], content=xml)
    if res.get("fault") or res.get("code") != "00":
        raise BankApiError(f"The bank refused the payment file: {res.get('code') or res.get('soap_code')} "
                           f"{res.get('text') or res.get('soap_text') or res.get('fault')}.",
                           f"Pankki hylkäsi maksuaineiston: {res.get('code') or res.get('soap_code')} "
                           f"{res.get('text') or res.get('soap_text') or res.get('fault')}.", code="bank_api_upload")
    return {k: v for k, v in res.items() if k != "content"} | {"uploaded_at": datetime.now(timezone.utc).isoformat()}


async def fetch_reports(status: str = "NEW") -> list[tuple[str, bytes]]:
    """Download new pain.002 and camt.054 files. Returns [(message name, xml bytes)]."""
    s = settings()
    out: list[tuple[str, bytes]] = []
    for ft in (s["pain002"], s["camt054"]):
        if not ft:
            continue
        lst = await call("DownloadFileList", file_type=ft, status=status)
        if lst.get("code") != "00":
            log.info("bank file list %s: %s %s", ft, lst.get("code"), lst.get("text"))
            continue
        for fd in lst["file_descriptors"]:
            ref = fd.get("FileReference")
            if not ref:
                continue
            got = await call("DownloadFile", file_type=ft, file_refs=[ref], status=status)
            data = got.get("content")
            if got.get("code") == "00" and data:
                try:
                    out.append((message_name(data), data))
                except PaymentError:
                    log.warning("bank file %s %s is not ISO 20022 XML; skipped", ft, ref)
    return out
