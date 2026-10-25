"""Customs authority channels today (s11): what Tulli answers right now and what the company still has to register.

Read-only. Nothing here sends SOAP, a declaration or any company identifier to Tulli or the EU:
- the TLS probe opens a connection to Tulli's published DME endpoints, verifies the server against DVV's chain and
  records what Tulli answers to a client without a certificate (no request body is sent);
- the envelope self-test wraps a declaration's official XML in a Tulli v1.21 ApplicationRequest and SOAP Upload,
  signed with a key generated for this check alone, validates both against Tulli's XSD/WSDL and throws them away.
"""
import asyncio
import hashlib
import json
import socket
import ssl
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree

from core import config
from core.util import now_iso
from services import ics2_gateway
from services import tulli_transport as wire
from services.tulli_exchange import transport as tulli_client

TLS_TTL = 600  # one handshake per endpoint per 10 minutes at most
_tls_cache: dict[str, tuple[float, dict]] = {}
SELF_TEST_IDS = {"declarant_id": "FI9991026-5", "builder_id": "FI9991026-5", "intermediary_id": "FI9991026-5",
                 "reference_prefix": "SELFT"}  # the envelope self-test's placeholder identifiers; never sent anywhere

COMPANY_STEPS = {
    "TULLI_DECLARANT_ID": {"fi": "EORI-numero (Tulli) – vaatii Y-tunnuksen", "en": "EORI number (Tulli) – needs the Y-tunnus"},
    "TULLI_BUILDER_ID": {"fi": "Sanomanrakentajan Y-tunnus", "en": "Message builder business ID (Y-tunnus)"},
    "TULLI_INTERMEDIARY_ID": {"fi": "Välittäjän Y-tunnus", "en": "Intermediary business ID (Y-tunnus)"},
    "TULLI_REFERENCE_PREFIX": {"fi": "Tullin antama viiden kirjaimen viiteprefiksi", "en": "Five-letter reference prefix assigned by Tulli"},
    "TULLI_CLIENT_CERT": {"fi": "DVV:n palvelinvarmenne (asiakasvarmenne)", "en": "DVV client certificate"},
    "TULLI_CLIENT_KEY": {"fi": "DVV-varmenteen yksityinen avain", "en": "Private key of the DVV certificate"},
    "TULLI_CUSTOMER_APPROVED": {"fi": "Tullin sanomaliikenneasiakkuus (lupa- ja päätöspalvelu) ja asiakastestaus",
                                "en": "Tulli message-exchange customer status (Authorisations and Decisions) and customer testing"},
    "TULLI_MODE": {"fi": "Kytkin TULLI_MODE=test, sitten production", "en": "Switch TULLI_MODE=test, then production"},
}


def mode(system: str) -> str:
    if system == "transit":
        return (config.env("TULLI_TRANSIT_MODE", config.env("TULLI_MODE", "disabled")) or "disabled").lower()
    if system == "ics2":
        return ics2_gateway.mode()
    return (getattr(config, f"TULLI_{system.upper()}_MODE", None) or config.TULLI_MODE or "disabled").lower()


def schema_package() -> dict:
    """The deployed Tulli v1.21 XSD/WSDL files checked against the sha256 of Tulli's official package."""
    manifest = json.loads((wire.SCHEMAS / "manifest.json").read_text())
    ok = {name: hashlib.sha256((wire.SCHEMAS / name).read_bytes()).hexdigest() == want
          for name, want in manifest["files"].items()}
    return {"version": "1.21", "source": manifest.get("source"), "guide": manifest.get("guide"), "files": len(ok),
            "matching_official_sha256": sum(ok.values()), "mismatched": sorted(n for n, good in ok.items() if not good)}


def _handshake(endpoint: str) -> dict:
    parts = urlsplit(endpoint)
    out = {"endpoint": endpoint, "host": parts.hostname, "checked_at": now_iso()}
    try:
        with socket.create_connection((parts.hostname, 443), timeout=10) as raw:
            with wire.server_ssl_context().wrap_socket(raw, server_hostname=parts.hostname) as conn:
                cert = conn.getpeercert() or {}
                out.update(server_verified=True, tls=conn.version(), server_certificate_expires=cert.get("notAfter"))
                conn.settimeout(10)
                try:
                    # Headers only, no body: TLS 1.3 servers that require a client certificate answer the first read
                    # with the "certificate required" alert.
                    conn.sendall(f"HEAD {parts.path} HTTP/1.1\r\nHost: {parts.hostname}\r\nConnection: close\r\n\r\n".encode())
                    first = conn.recv(256).split(b"\r\n")[0].decode("latin-1")
                    out.update(client_certificate_required=False, answer=first or "connection closed")
                except ssl.SSLError as exc:
                    reason = exc.reason or str(exc)
                    out.update(client_certificate_required="CERTIFICATE_REQUIRED" in reason.upper(), answer=reason)
    except (OSError, ssl.SSLError, ValueError) as exc:
        out.update(server_verified=False, error=f"{type(exc).__name__}: {exc}"[:300])
    return out


async def tulli_endpoints(*, fresh: bool = False) -> dict:
    results = {}
    for env_name, endpoint in wire.ENDPOINTS.items():
        hit = _tls_cache.get(endpoint)
        if fresh or not hit or time.monotonic() - hit[0] > TLS_TTL:
            hit = (time.monotonic(), await asyncio.to_thread(_handshake, endpoint))
            _tls_cache[endpoint] = hit
        results[env_name] = hit[1]
    return results


def _missing_steps(missing: list[str]) -> list[dict]:
    steps = []
    for item in missing:
        key = next((k for k in COMPANY_STEPS if item.startswith(k) or k in item), None)
        steps.append({"setting": item, **(COMPANY_STEPS.get(key) or {"fi": item, "en": item})})
    return steps


def _tulli_system(system: str, schema: str) -> dict:
    m = mode(system)
    missing = tulli_client(m if m in wire.ENDPOINTS else "test").settings.missing()
    if m not in wire.ENDPOINTS:
        missing = ["TULLI_MODE=test or production"] + missing
    return {"system": system, "business_message": schema, "mode": m, "channel": "Tulli Direct Message Exchange v1.21",
            "can_send": m in wire.ENDPOINTS and not missing, "missing": _missing_steps(missing)}


def _ics2() -> dict:
    st = ics2_gateway.status()
    required = ["ICS2_EORI", "ICS2_AS4_ENDPOINT", "ICS2_AS4_CERT", "ICS2_AS4_KEY", "ICS2_PARTY_ID",
                "ICS2_RECEIVER_PARTY_ID", "ICS2_SERVICE", "ICS2_ACTION", "ICS2_FINAL_RECIPIENT"]
    missing = [k for k in required if not config.env(k)]
    return {"system": "ics2", "business_message": "ICS2 ENS (IE3F**)", "mode": st.get("mode"),
            "channel": "EU ICS2 via eDelivery AS4 access point", "can_send": st.get("mode") in {"conformance", "production"} and not missing,
            "missing": [{"setting": k, "fi": "EORI, EU Login/UUM&DS-roolit ja rekisteröity AS4-yhteyspiste" if k == "ICS2_EORI" else k,
                         "en": "EORI, EU Login/UUM&DS roles and a registered AS4 access point" if k == "ICS2_EORI" else k}
                        for k in missing]}


async def readiness(*, fresh: bool = False) -> dict:
    return {"checked_at": now_iso(), "schemas": schema_package(), "tulli_endpoints": await tulli_endpoints(fresh=fresh),
            "systems": [_tulli_system("import", "FI415B (H1)"), _tulli_system("export", "FI515A (B1)"),
                        _tulli_system("transit", "NCTS P6 national FI messages"), _ics2()],
            "sent": False}


def _ephemeral_settings(tmp: Path) -> wire.Settings:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Huolitsija envelope self-test (not a DVV certificate)")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).sign(key, hashes.SHA256()))
    (tmp / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    ids = SELF_TEST_IDS
    return wire.Settings("test", ids["declarant_id"], ids["builder_id"], ids["intermediary_id"], ids["reference_prefix"],
                         str(tmp / "cert.pem"), str(tmp / "key.pem"), True)


def envelope_self_test(payload: bytes, application: str, message: str) -> dict:
    """Sign and schema-validate a real business payload exactly as an upload would be built; never sent."""
    with tempfile.TemporaryDirectory(prefix="dme-selftest-") as tmp:
        client = wire.Transport(_ephemeral_settings(Path(tmp)))
        reference = client.settings.reference_prefix + "000000001"
        out = {"business_message": message, "payload_bytes": len(payload),
               "payload_sha256": hashlib.sha256(payload).hexdigest(), "application": application,
               "signing_key": "generated for this check only; not a DVV certificate", "sent": False}
        try:
            request = client.application_request(payload, application, reference)
            out["application_request"] = {"valid": True, "schema": "ApplicationRequest.xsd v1.21",
                                          "signature": "RSA-SHA256 enveloped, verified", "bytes": len(request)}
            soap = client.request("Upload", application=application, reference=reference, payload=payload)
            out["soap_upload"] = {"valid": True, "schema": "CustomsCorporateService_soap1_2.wsdl v1.21", "bytes": len(soap)}
            out["ok"] = True
        except (wire.TransportError, ValueError, etree.XMLSyntaxError) as exc:
            out.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        return out
