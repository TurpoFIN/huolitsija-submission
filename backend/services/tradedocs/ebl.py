"""Electronic house B/L through an eBL platform with the DCSA eBL Issuance API 3.0 (DCSA_EBL_ISS 3.0.3).

    req = build_issuance_request(doc)        # IssuanceRequest {document, issueTo, eBLVisualisationByCarrier,
                                             #                  issuanceManifestSignedContent}
    res = await request_issuance(doc)        # PUT {EBL_ISSUANCE_URL}/v3/ebl-issuance-requests -> 204 accepted / 409

The platform answers on POST /api/webhooks/dcsa/ebl-issuance-responses (IssuanceResponse ISSU | BREQ | REFU), which
routes/trade_docs.py records on the document (`ebl.status`).

Spec details implemented here:
- `document` is the DCSA TransportDocument (structured.dcsa_ebl); `documentChecksum` / `issueToChecksum` /
  `eBLVisualisationByCarrierChecksum` are SHA-256 (RFC 6234) over the RFC 8785 canonical JSON (JCS) of each part
  (eBLVisualisationByCarrierChecksum over its `content`).
- `issuanceManifestSignedContent` is a compact JWS (RFC 7515) of the IssuanceManifest, signed with the forwarder's key
  (EBL_SIGNING_KEY_PEM, ES256 or RS256 by key type; `kid` = EBL_SIGNING_KEY_ID).
- `issueTo`: the shipper's account on its eBL platform: shipment parties.shipper.ebl_platform (WAVE, CARX, BOLE, IDT,
  EDOX, IQAX, SECR, TRGO, ETEU, TRAC, ...) and parties.shipper.ebl_party_code.

Go-live is configuration only: EBL_PLATFORM=dcsa, EBL_ISSUANCE_URL (the platform's endpoint), EBL_ISSUANCE_TOKEN
(bearer), EBL_SIGNING_KEY_PEM (+ EBL_SIGNING_KEY_ID), TRADEDOCS_ISSUER_SCAC. Without them nothing is sent and the
status says what is missing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math

import httpx

from core import config
from core.audit import audit
from core.db import db
from core.util import now_iso

API_VERSION = "3.0.3"
PLATFORMS = {"WAVE", "CARX", "IDT", "BOLE", "EDOX", "IQAX", "SECR", "TRGO", "ETEU", "TRAC", "BRIT", "COVA", "ETIT", "KTNE",
             "CRED", "BLOC", "DOCU", "AEOT", "SGTD", "GSBN", "WISE"}


# ------------------------------------------------------------------------------------------------ RFC 8785 (JCS)
def _num(x) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        raise ValueError("JCS: non-finite number")
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)  # shortest round-trip form, same digits as ECMAScript Number.prototype.toString for |x| in [1e-6, 1e21)
    if "e" in r:
        mant, exp = r.split("e")
        e = int(exp)
        r = f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def canonical(v) -> str:
    """RFC 8785 canonical JSON, including ECMAScript number formatting and UTF-16 ordering."""
    import rfc8785
    return rfc8785.dumps(v).decode("utf-8")


def checksum(v) -> str:
    return hashlib.sha256(canonical(v).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ the request
def issue_to(doc: dict) -> dict | None:
    sh = (doc.get("data") or {}).get("shipper") or {}
    platform = (sh.get("ebl_platform") or "").upper()
    code = sh.get("ebl_party_code")
    if platform not in PLATFORMS or not code:
        return None
    out = {"partyName": sh["name"][:70], "sendToPlatform": platform,
           "identifyingCodes": [{"codeListProvider": platform, "partyCode": str(code)[:150]}]}
    if sh.get("eori"):
        out["taxLegalReferences"] = [{"type": "EORI", "countryCode": sh["eori"][:2], "value": sh["eori"][:35]}]
    return out


def missing(doc: dict) -> list[str]:
    from services.tradedocs import structured
    out = structured.dcsa_missing(doc.get("data") or {})
    if not issue_to(doc):
        out.append("shipper's eBL platform and account (parties.shipper.ebl_platform / ebl_party_code)")
    for var in ("EBL_ISSUANCE_URL", "EBL_ISSUANCE_TOKEN", "EBL_SIGNING_KEY_PEM"):
        if not config.env(var):
            out.append(var)
    return out


def sign_manifest(manifest: dict, pem: str, kid: str | None = None) -> str:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    alg = "ES256" if isinstance(key, ec.EllipticCurvePrivateKey) else "RS256" if isinstance(key, rsa.RSAPrivateKey) else None
    if not alg:
        raise ValueError("EBL_SIGNING_KEY_PEM must be an EC P-256 or RSA private key")
    headers = {"kid": kid} if kid else None
    return jwt.encode(manifest, key, algorithm=alg, headers=headers)


def build_issuance_request(doc: dict, pdf: bytes | None = None, *, pem: str | None = None, kid: str | None = None,
                           to: dict | None = None) -> dict:
    document = (doc.get("structured") or {}).get("dcsa_ebl_3")
    if not document:
        raise ValueError("The document has no DCSA eBL export (house B/L only).")
    to = to or issue_to(doc)
    if not to:
        raise ValueError("The shipper's eBL platform account is not known.")
    req = {"document": document, "issueTo": to}
    manifest = {"documentChecksum": checksum(document), "issueToChecksum": checksum(to)}
    if pdf:
        vis = {"name": f"{doc['doc_no']}-v{doc['version']}.pdf", "content": base64.b64encode(pdf).decode(),
               "contentType": "application/pdf"}
        req["eBLVisualisationByCarrier"] = vis
        manifest["eBLVisualisationByCarrierChecksum"] = checksum(vis["content"])
    req["issuanceManifestSignedContent"] = sign_manifest(manifest, pem or config.env("EBL_SIGNING_KEY_PEM"),
                                                         kid or config.env("EBL_SIGNING_KEY_ID") or None)
    return req


async def request_issuance(doc: dict, *, actor: dict | None = None) -> dict:
    """PUT the issuance request to the configured platform. {status: sent|already|failed|not_configured, ...}."""
    if (config.env("EBL_PLATFORM", "none") or "none").lower() != "dcsa":
        return {"status": "not_configured", "missing": ["EBL_PLATFORM=dcsa"]}
    miss = missing(doc)
    if miss:
        await _set(doc, {"status": "blocked", "missing": miss, "at": now_iso()})
        return {"status": "blocked", "missing": miss}
    from services.tradedocs import service
    pdf, _ = await service.pdf_bytes(doc["id"], "en")
    body = build_issuance_request(doc, pdf)
    url = config.env("EBL_ISSUANCE_URL").rstrip("/") + "/v3/ebl-issuance-requests"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, json=body, headers={"API-Version": API_VERSION,
                                                     "Authorization": f"Bearer {config.env('EBL_ISSUANCE_TOKEN')}"})
    status = {204: "sent", 409: "already"}.get(r.status_code, "failed")
    rec = {"status": status, "http_status": r.status_code, "at": now_iso(), "platform": body["issueTo"]["sendToPlatform"],
           "document_checksum": checksum(body["document"]), "error": None if status != "failed" else r.text[:500]}
    await _set(doc, rec)
    await audit(actor, "tradedoc.ebl_issuance_requested", "trade_document", doc["id"],
                {"doc_no": doc["doc_no"], **{k: v for k, v in rec.items() if k != "at"}}, company_id=doc.get("company_id"))
    return rec


async def record_response(resp: dict) -> dict | None:
    """IssuanceResponse from the platform (ISSU issued, BREQ bad request, REFU refused)."""
    ref = resp.get("transportDocumentReference")
    doc = await db.trade_documents.find_one({"doc_no": ref, "current": True}, {"_id": 0})
    if not doc:
        return None
    code = resp.get("issuanceResponseCode")
    rec = {"status": {"ISSU": "issued", "BREQ": "bad_request", "REFU": "refused"}.get(code, "unknown"), "response_code": code,
           "reason": resp.get("reason"), "errors": resp.get("errors") or [], "at": now_iso()}
    await _set(doc, rec)
    await audit(None, "tradedoc.ebl_issuance_response", "trade_document", doc["id"], {"doc_no": ref, **rec},
                company_id=doc.get("company_id"))
    return rec


async def _set(doc: dict, rec: dict):
    await db.trade_documents.update_one({"id": doc["id"]}, {"$set": {"ebl": rec}, "$push": {"ebl_log": rec}})
