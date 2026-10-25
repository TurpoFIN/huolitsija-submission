"""EDI transports: SFTP (asyncssh) and AS2 (pyas2lib: signed + encrypted, synchronous signed MDN).

Secrets are never stored in Mongo: a partner's SFTP password or private key is named by an env var
(`sftp.password_env` / `sftp.key_env`), our AS2 key comes from EDI_AS2_KEY_PEM / EDI_AS2_CERT_PEM (PEM text or a file path).
A partner's AS2 certificate is public and lives on the partner record."""
from __future__ import annotations

import logging
import os
import posixpath
from datetime import datetime, timezone

import httpx

from core import config

log = logging.getLogger("edi.transport")


class TransportError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ SFTP

def _sftp_conn_args(sftp: dict) -> dict:
    import asyncssh

    args = {"host": sftp.get("host"), "port": int(sftp.get("port") or 22), "username": sftp.get("user")}
    if not args["host"] or not args["username"]:
        raise TransportError("The partner's SFTP host and user are not set.")
    if sftp.get("password_env"):
        pw = os.environ.get(sftp["password_env"], "")
        if not pw:
            raise TransportError(f"SFTP password env {sftp['password_env']} is not set on this server.")
        args["password"] = pw
    if sftp.get("key_env"):
        key = os.environ.get(sftp["key_env"], "")
        if not key:
            raise TransportError(f"SFTP key env {sftp['key_env']} is not set on this server.")
        args["client_keys"] = [asyncssh.import_private_key(key)]
    if "password" not in args and "client_keys" not in args:
        raise TransportError("The partner's SFTP login needs password_env or key_env.")
    # Pin the server's host key when the partner gave it (known_hosts line); otherwise accept and log the key.
    args["known_hosts"] = asyncssh.import_known_hosts(sftp["host_key"]) if sftp.get("host_key") else None
    return args


async def sftp_fetch_inbound(sftp: dict) -> list[tuple[str, bytes]]:
    """Read every regular file in the inbound dir (oldest first). Files are not removed here: call sftp_archive after
    they are processed, so a crash re-reads them (processing is idempotent by content hash)."""
    import asyncssh

    out: list[tuple[str, bytes]] = []
    inbound = sftp.get("inbound_dir") or "in"
    async with asyncssh.connect(**_sftp_conn_args(sftp)) as conn:
        async with conn.start_sftp_client() as client:
            entries = await client.readdir(inbound)
            files = [e for e in entries if e.filename not in (".", "..") and not e.filename.startswith(".")
                     and e.attrs.type == asyncssh.FILEXFER_TYPE_REGULAR and not e.filename.endswith(".tmp")]
            files.sort(key=lambda e: (e.attrs.mtime or 0, e.filename))
            for e in files:
                path = posixpath.join(inbound, e.filename)
                async with client.open(path, "rb") as f:
                    out.append((e.filename, await f.read()))
    return out


async def sftp_archive(sftp: dict, filenames: list[str]):
    import asyncssh

    if not filenames:
        return
    inbound = sftp.get("inbound_dir") or "in"
    archive = sftp.get("archive_dir")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    async with asyncssh.connect(**_sftp_conn_args(sftp)) as conn:
        async with conn.start_sftp_client() as client:
            for name in filenames:
                src = posixpath.join(inbound, name)
                if archive:
                    await client.rename(src, posixpath.join(archive, f"{stamp}_{name}"))
                else:
                    await client.remove(src)


async def sftp_put(sftp: dict, filename: str, data: bytes) -> str:
    """Upload as <name>.tmp, then rename: the partner never reads a half-written file."""
    import asyncssh

    outbound = sftp.get("outbound_dir") or "out"
    final = posixpath.join(outbound, filename)
    tmp = final + ".tmp"
    async with asyncssh.connect(**_sftp_conn_args(sftp)) as conn:
        async with conn.start_sftp_client() as client:
            async with client.open(tmp, "wb") as f:
                await f.write(data)
            await client.rename(tmp, final)
    return final


# ------------------------------------------------------------------------------------------------ AS2

def pem(value: str) -> bytes:
    v = (value or "").strip()
    if not v:
        return b""
    if v.startswith("-----BEGIN"):
        return v.encode()
    with open(v, "rb") as f:
        return f.read()


def as2_ready() -> bool:
    return bool(config.EDI_AS2_ENABLED and config.EDI_AS2_KEY_PEM and config.EDI_AS2_CERT_PEM)


def our_as2_org():
    from pyas2lib import as2

    if not as2_ready():
        raise TransportError("AS2 is off: set EDI_AS2_ENABLED=true, EDI_AS2_KEY_PEM and EDI_AS2_CERT_PEM.")
    key = pem(config.EDI_AS2_KEY_PEM) + b"\n" + pem(config.EDI_AS2_CERT_PEM)
    return as2.Organization(as2_name=config.EDI_AS2_ID, sign_key=key, sign_key_pass=config.EDI_AS2_KEY_PASS or None,
                            decrypt_key=key, decrypt_key_pass=config.EDI_AS2_KEY_PASS or None)


def as2_partner(p: dict):
    from pyas2lib import as2

    a = p.get("as2") or {}
    cert = (a.get("cert_pem") or "").encode()
    if not (a.get("as2_id") and cert):
        raise TransportError("The partner's AS2 id and certificate are not set.")
    return as2.Partner(as2_name=a["as2_id"], verify_cert=cert, encrypt_cert=cert, validate_certs=bool(a.get("validate_certs", False)),
                       sign=True, encrypt=True, enc_alg=a.get("enc_alg") or "aes_256_cbc", digest_alg="sha256",
                       mdn_mode="SYNC", mdn_digest_alg="sha256")


def _headers_bytes(headers) -> bytes:
    return b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items())


async def as2_send(p: dict, filename: str, data: bytes, *, content_type: str = "application/EDIFACT") -> dict:
    """Send one payload; returns {message_id, mdn_status, mdn_detail, http_status}. Raises TransportError when the
    partner's MDN is not 'processed' or the HTTP call fails."""
    from pyas2lib import as2

    url = (p.get("as2") or {}).get("url")
    if not url:
        raise TransportError("The partner's AS2 URL is not set.")
    msg = as2.Message(our_as2_org(), as2_partner(p))
    msg.build(data, filename=filename, subject=f"EDI {filename}", content_type=content_type,
              disposition_notification_to=config.COMPANY_EMAIL)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, content=msg.content, headers=dict(msg.headers))
    if r.status_code >= 300:
        raise TransportError(f"AS2 HTTP {r.status_code} from {url}")
    raw_mdn = _headers_bytes(r.headers) + b"\r\n" + r.content
    mdn = as2.Mdn()
    status, detail = mdn.parse(raw_mdn, find_message_cb=lambda mid, rcpt: msg)
    if status != "processed":
        raise TransportError(f"AS2 MDN {status}: {detail}")
    return {"message_id": msg.message_id, "mdn_status": status, "mdn_detail": detail, "http_status": r.status_code,
            "mic": msg.mic.decode() if isinstance(getattr(msg, "mic", None), bytes) else None}


def as2_parse_inbound(raw: bytes, partner_record: dict | None):
    """Parse an inbound AS2 request (headers + CRLF + body). Returns (status, payload, mdn, message_id, error)."""
    from pyas2lib import as2

    org = our_as2_org()
    partner = as2_partner(partner_record) if partner_record else None
    msg = as2.Message()
    status, (exc, _tb), mdn = msg.parse(raw, find_org_cb=lambda as2_to: org if as2_to == org.as2_name else None,
                                        find_partner_cb=lambda as2_from: partner if partner and as2_from == partner.as2_name else None)
    return status, (msg.content if status == "processed" else None), mdn, getattr(msg, "message_id", None), (str(exc) if exc else None)
