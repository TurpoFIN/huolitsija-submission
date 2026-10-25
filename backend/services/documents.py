"""Shipment documents: upload into GridFS, the required-documents matrix (RULE DOC-2) and the
cross-document tie-out (RULE DOC-3).

A document record (collection `documents`) is the contract other slices read:
    id, company_id, shipment_id, type (enum.doc.*), filename, content_type, size, sha256, pages, file_id,
    uploaded_by, uploaded_at, source portal|ops|seed|system, number, issue_date,
    extraction{status pending|processing|done|needs_review|failed, method, model, fields{key: {value, confidence, source}},
               line_items[], confidence, warnings[], reason, completed_at, confirmed_by, confirmed_at},
    checks[] (DOC-3 results for the shipment the document belongs to)

Extraction itself lives in services/extraction.py (a durable queue on the `extraction.status` field).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from difflib import SequenceMatcher

from fastapi import HTTPException

from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import emit
from core.storage import delete_file, put_file
from core.util import NO_ID, new_id, now_iso

log = logging.getLogger("documents")

DOC_TYPES = [
    "commercial_invoice", "packing_list", "bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim",
    "certificate_of_origin", "eur1", "dg_declaration", "power_of_attorney", "pod", "customs_decision", "other",
]
# Types the extraction agent reads into structured fields.
EXTRACTABLE = {"commercial_invoice", "packing_list", "bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim",
               "certificate_of_origin", "eur1", "other"}
TRANSPORT_DOCS = {"bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim"}

EU_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "EL", "HU", "IE", "IT", "LV",
                "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}

MAX_BYTES = 20 * 1024 * 1024
ACCEPTED = "PDF, PNG, JPG, WEBP, CSV or XLSX, up to 20 MB"

register_index("documents", "id", unique=True)
register_index("documents", [("company_id", 1), ("uploaded_at", -1)])
register_index("documents", [("shipment_id", 1), ("type", 1)])
register_index("documents", [("extraction.status", 1), ("extraction.queued_at", 1)])


# ---------------------------------------------------------------------------------------------
# File type sniffing and validation
# ---------------------------------------------------------------------------------------------
def sniff(data: bytes, filename: str) -> str | None:
    """Content type from the bytes themselves (never trust the browser's header)."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    name = (filename or "").lower()
    if head.startswith(b"PK\x03\x04") and name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".csv"):
        try:
            data[:4096].decode("utf-8")
            return "text/csv"
        except UnicodeDecodeError:
            try:
                data[:4096].decode("cp1252")
                return "text/csv"
            except UnicodeDecodeError:
                return None
    return None


def inspect_pdf(data: bytes) -> dict:
    """Page count and readability. Raises HTTPException with a sentence the user can act on."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                raise HTTPException(400, "This PDF is password-protected. Upload an unprotected copy.")
        pages = len(reader.pages)
    except HTTPException:
        raise
    except (PdfReadError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(400, f"The PDF could not be read ({type(exc).__name__}). Upload the original file.")
    if pages == 0:
        raise HTTPException(400, "The PDF has no pages.")
    return {"pages": pages}


def safe_filename(name: str) -> str:
    name = (name or "document").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:180] or "document"


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------
def public_doc(doc: dict | None) -> dict | None:
    if not doc:
        return None
    doc = {k: v for k, v in doc.items() if k != "_id"}
    return doc


def field_value(doc: dict | None, key: str):
    """Value of one extracted field (None when missing)."""
    if not doc:
        return None
    f = ((doc.get("extraction") or {}).get("fields") or {}).get(key)
    if isinstance(f, dict):
        return f.get("value")
    return f


async def create_document(*, company_id: str | None, shipment_id: str | None, doc_type: str | None, filename: str,
                          data: bytes, actor: dict | None, source: str = "portal", extract: bool = True,
                          extra: dict | None = None) -> dict:
    if not data:
        raise HTTPException(400, "The file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"The file is {len(data) / 1048576:.1f} MB; the limit is 20 MB.")
    filename = safe_filename(filename)
    content_type = sniff(data, filename)
    if not content_type:
        raise HTTPException(415, f"Unsupported file. Upload {ACCEPTED}.")
    if doc_type and doc_type not in DOC_TYPES:
        raise HTTPException(400, f"Unknown document type '{doc_type}'.")
    pages = 1
    if content_type == "application/pdf":
        pages = inspect_pdf(data)["pages"]
    sha = hashlib.sha256(data).hexdigest()
    file_id = await put_file(data, filename, content_type, {"company_id": company_id, "shipment_id": shipment_id,
                                                            "sha256": sha})
    # A type the user chose as "other" is filed as-is; unknown types (None) go to the agent for detection.
    wants_extraction = extract and (doc_type is None or (doc_type in EXTRACTABLE and doc_type != "other"))
    doc = {
        "id": new_id(), "company_id": company_id, "shipment_id": shipment_id, "type": doc_type or "other",
        "type_source": "user" if doc_type else "pending", "filename": filename, "content_type": content_type,
        "size": len(data), "sha256": sha, "pages": pages, "file_id": file_id, "uploaded_by": actor_ref(actor),
        "uploaded_at": now_iso(), "source": source, "number": None, "issue_date": None,
        "extraction": {"status": "pending" if wants_extraction else "not_applicable", "queued_at": now_iso(),
                       "fields": {}, "line_items": [], "warnings": [], "attempts": 0},
        "checks": [], "visibility": ["customer", "ops"], "used_by": [],
    }
    if extra:
        doc.update(extra)
    await db.documents.insert_one(dict(doc))
    await audit(actor, "document_uploaded", "document", doc["id"],
                {"filename": filename, "type": doc["type"], "shipment_id": shipment_id, "size": len(data)},
                company_id=company_id)
    await emit("document.uploaded", {"document": public_doc(doc), "actor": actor})
    if wants_extraction:
        from services import extraction

        extraction.kick()
    return public_doc(doc)


async def delete_document(doc: dict, actor: dict):
    used = [u for u in doc.get("used_by") or [] if u.get("status") not in ("draft", "needs_info", None)]
    if used:
        raise HTTPException(409, "This document is used by a customs declaration that is already in review or filed; "
                                 "upload a corrected version instead.")
    await db.documents.delete_one({"id": doc["id"]})
    if doc.get("file_id"):
        await delete_file(doc["file_id"])
    await audit(actor, "document_deleted", "document", doc["id"], {"filename": doc.get("filename")},
                company_id=doc.get("company_id"))
    if doc.get("shipment_id"):
        await run_tieout(doc["shipment_id"])


# ---------------------------------------------------------------------------------------------
# RULE DOC-2: required documents per mode x direction x incoterm x goods flags
# ---------------------------------------------------------------------------------------------
def _country(loc: dict | None) -> str | None:
    if not isinstance(loc, dict):
        return None
    c = loc.get("country") or (loc.get("unlocode") or "")[:2] or None
    return c.upper() if c else None


def crosses_eu_border(shipment: dict) -> bool:
    o, d = _country(shipment.get("origin")), _country(shipment.get("destination"))
    if not o or not d:
        return shipment.get("direction") in ("import", "export", "transit")
    return (o in EU_COUNTRIES) != (d in EU_COUNTRIES)


TYPE_FI = {"commercial_invoice": "kauppalasku", "packing_list": "pakkausluettelo", "bill_of_lading": "konossementti",
           "sea_waybill": "merirahtikirja", "air_waybill": "lentorahtikirja", "cmr": "CMR-rahtikirja", "cim": "CIM-rahtikirja"}


def _fi(n: float, d: int = 1) -> str:
    """Finnish number format: space as thousands separator, comma as decimal mark."""
    return f"{n:,.{d}f}".replace(",", " ").replace(".", ",")


def required_documents_spec(shipment: dict, company: dict | None = None) -> list[dict]:
    mode = (shipment.get("mode") or "").lower()
    direction = (shipment.get("direction") or "").lower()
    cargo = shipment.get("cargo") or {}
    customs = crosses_eu_border(shipment) and direction in ("import", "export", "transit")
    spec: list[dict] = []

    def add(t, required, reason, expected_from="customer", alternatives=None, reason_fi=None):
        spec.append({"type": t, "required": required, "reason": reason, "reason_fi": reason_fi, "expected_from": expected_from,
                     "alternatives": alternatives or []})

    if customs or direction in ("import", "export", "cross_trade"):
        add("commercial_invoice", True, "Basis of the customs value (UCC art. 70) and of the import VAT"
            if direction == "import" else "Needed for the export declaration and VAT zero-rating evidence",
            reason_fi="Tullausarvon (UCC 70 art.) ja maahantuonnin arvonlisäveron peruste" if direction == "import"
            else "Tarvitaan vienti-ilmoitukseen ja arvonlisäverottomuuden näytöksi")
        add("packing_list", True, "Package count, marks and weights for the declaration and the carrier",
            reason_fi="Kollimäärä, merkit ja painot tulli-ilmoitusta ja rahdinkuljettajaa varten")
    elif direction == "domestic":
        add("commercial_invoice", False, "Optional for domestic transport", reason_fi="Valinnainen kotimaan kuljetuksessa")
    transport = {"sea": ("bill_of_lading", ["sea_waybill"], "Ocean carrier"),
                 "air": ("air_waybill", [], "Airline / agent"),
                 "road": ("cmr", [], "Road carrier"),
                 "rail": ("cim", [], "Rail carrier")}.get(mode)
    if transport and (direction != "domestic" or mode != "road"):
        t, alts, who = transport
        who_fi = {"sea": "varustamo", "air": "lentoyhtiö tai sen agentti", "road": "maantiekuljettaja",
                  "rail": "rautatiekuljettaja"}[mode]
        add(t, True, f"Transport document issued by the {who.lower()}", expected_from="carrier", alternatives=alts,
            reason_fi=f"Kuljetusasiakirja, jonka antaa {who_fi}")
    if mode == "multimodal":
        add("bill_of_lading", True, "Multimodal / combined transport document", expected_from="carrier",
            alternatives=["sea_waybill", "cmr", "air_waybill"], reason_fi="Yhdistetyn kuljetuksen kuljetusasiakirja")
    if cargo.get("dangerous_goods"):
        add("dg_declaration", True, "Dangerous goods declaration (ADR/IMDG/IATA) from the shipper",
            reason_fi="Lähettäjän vaarallisten aineiden ilmoitus (ADR/IMDG/IATA)")
    claims_preference = bool(cargo.get("preference_claimed") or cargo.get("origin_proof"))
    if customs and direction == "import":
        add("certificate_of_origin", claims_preference,
            "Proof of preferential origin to claim a reduced duty rate" if claims_preference
            else "Only needed if a preferential duty rate or a non-preferential origin proof is claimed",
            alternatives=["eur1"],
            reason_fi="Etuuskohteluun oikeuttavan alkuperän todiste alennettua tullia varten" if claims_preference
            else "Tarvitaan vain, jos haetaan etuustullia tai esitetään muu alkuperätodiste")
    if customs:
        poa = (company or {}).get("poa") or {}
        on_file = bool(poa.get("valid_to") and poa.get("valid_to") >= now_iso()[:10])
        add("power_of_attorney", not on_file,
            "Customs representation needs a valid power of attorney (RULE R-2)" + (" — on file in the company profile"
                                                                                  if on_file else ""),
            reason_fi="Tulliedustus edellyttää voimassa olevaa valtakirjaa (R-2)" + (" — tallessa yrityksen tiedoissa"
                                                                                   if on_file else ""))
    return spec


async def required_documents(shipment: dict) -> list[dict]:
    company = await db.companies.find_one({"id": shipment.get("company_id")}, NO_ID) if shipment.get("company_id") else None
    docs = await db.documents.find({"shipment_id": shipment["id"]}, NO_ID).sort("uploaded_at", 1).to_list(500)
    poa = (company or {}).get("poa") or {}
    out = []
    for item in required_documents_spec(shipment, company):
        types = [item["type"], *item["alternatives"]]
        mine = [d for d in docs if d.get("type") in types]
        if item["type"] == "power_of_attorney" and not mine and poa.get("document_id"):
            pd = await db.documents.find_one({"id": poa["document_id"]}, NO_ID)
            if pd:
                mine = [pd]
        if not mine:
            state = "on_file" if item["type"] == "power_of_attorney" and not item["required"] else "missing"
        else:
            sts = [(d.get("extraction") or {}).get("status") for d in mine]
            if any(s in ("pending", "processing") for s in sts):
                state = "extracting"
            elif any(s == "done" for s in sts) or all(s in ("not_applicable", None) for s in sts):
                state = "verified" if any((d.get("extraction") or {}).get("confirmed_at") for d in mine) else "uploaded"
            elif any(s == "needs_review" for s in sts):
                state = "needs_review"
            else:
                state = "failed"
        out.append({**item, "present": bool(mine), "state": state, "document_ids": [d["id"] for d in mine]})
    return out


# ---------------------------------------------------------------------------------------------
# RULE DOC-3: cross-document tie-out
# ---------------------------------------------------------------------------------------------
LEGAL_FORMS = {"oy", "oyj", "ab", "ltd", "limited", "llc", "inc", "gmbh", "ag", "sa", "sarl", "bv", "nv", "plc",
               "co", "company", "corp", "corporation", "spa", "srl", "as", "aps", "kg", "pte", "pvt", "sp", "zoo",
               "the", "and", "ky", "tmi"}


def norm_party(name: str | None) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in LEGAL_FORMS]
    return " ".join(toks)


def party_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm_party(a), norm_party(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(overlap, SequenceMatcher(None, na, nb).ratio())


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "")
    s = re.sub(r"[^0-9,.\-]", "", s)
    if s.count(",") and s.count("."):
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif s.count(",") == 1:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _containers(v) -> set[str]:
    if not v:
        return set()
    if isinstance(v, str):
        v = re.split(r"[,;/\s]+", v)
    return {re.sub(r"[^A-Z0-9]", "", str(x).upper()) for x in v if x and re.search(r"[A-Za-z]{4}\s?\d{6,7}", str(x))}


async def run_tieout(shipment_id: str, tolerance_pct: float = 2.0) -> list[dict]:
    """Compare gross weight, package count, container numbers, consignee and invoice reference across the
    shipment's documents (RULE DOC-3). Results are stored on every document of the shipment and returned."""
    shipment = await db.shipments.find_one({"id": shipment_id}, NO_ID) or {}
    docs = await db.documents.find({"shipment_id": shipment_id, "extraction.status": {"$in": ["done", "needs_review"]}},
                                   NO_ID).to_list(200)
    company = await db.companies.find_one({"id": shipment.get("company_id")}, NO_ID) if shipment.get("company_id") else None
    checks: list[dict] = []

    def vals(key: str, conv=_num):
        out = []
        for d in docs:
            v = conv(field_value(d, key))
            if v not in (None, "", set()):
                out.append({"document_id": d["id"], "type": d["type"], "filename": d.get("filename"), "value": v})
        return out

    # gross weight within tolerance
    gw = vals("total_gross_weight_kg")
    booked = _num((shipment.get("cargo") or {}).get("weight_kg"))
    if len(gw) >= 2 or (gw and booked):
        values = [g["value"] for g in gw]
        lo, hi = min(values), max(values)
        diff_pct = (hi - lo) / hi * 100 if hi else 0
        ok = diff_pct <= tolerance_pct
        msg = (f"Gross weight ties out across {len(gw)} documents ({lo:,.1f}–{hi:,.1f} kg)" if ok else
               f"Gross weight differs by {diff_pct:.1f} % across documents ({lo:,.1f} vs {hi:,.1f} kg); tolerance {tolerance_pct:g} %")
        msg_fi = (f"Bruttopaino täsmää {len(gw)} asiakirjassa ({_fi(lo)}–{_fi(hi)} kg)" if ok else
                  f"Bruttopaino eroaa {_fi(diff_pct)} % asiakirjojen välillä ({_fi(lo)} / {_fi(hi)} kg); toleranssi {_fi(tolerance_pct, 0)} %")
        if booked and gw:
            bdiff = abs(booked - values[0]) / max(booked, values[0]) * 100
            if bdiff > 10:
                msg += f". Booked weight {booked:,.0f} kg differs by {bdiff:.0f} % — update the booking if the documents are right"
                msg_fi += (f". Varauksen paino {_fi(booked, 0)} kg poikkeaa {_fi(bdiff, 0)} % — päivitä varaus, jos asiakirjat "
                           "ovat oikein")
        one = len(gw) < 2
        checks.append({"rule": "DOC-3", "key": "gross_weight", "ok": None if one else ok,
                       "values": gw + ([{"document_id": None, "type": "booking", "value": booked}] if booked else []),
                       "params": {"low": lo, "high": hi, "diff_pct": round(diff_pct, 2), "tolerance_pct": tolerance_pct,
                                  "booked": booked, "documents": len(gw)},
                       "message": f"Only one document states the gross weight ({values[0]:,.1f} kg)" if one else msg,
                       "message_fi": f"Vain yksi asiakirja ilmoittaa bruttopainon ({_fi(values[0])} kg)" if one else msg_fi})
    pk = vals("total_packages")
    if len(pk) >= 2:
        ok = len({int(round(p["value"])) for p in pk}) == 1
        checks.append({"rule": "DOC-3", "key": "packages", "ok": ok, "values": pk,
                       "message": "Package counts match" if ok else
                       "Package counts differ: " + ", ".join(f"{p['type'].replace('_', ' ')} {int(p['value'])}" for p in pk),
                       "message_fi": "Kollimäärät täsmäävät" if ok else
                       "Kollimäärät eroavat: " + ", ".join(f"{TYPE_FI.get(p['type'], p['type'])} {int(p['value'])}" for p in pk)})
    cn = vals("container_numbers", _containers)
    if len(cn) >= 2:
        sets = [c["value"] for c in cn]
        ok = all(s == sets[0] for s in sets)
        for c in cn:
            c["value"] = sorted(c["value"])
        checks.append({"rule": "DOC-3", "key": "containers", "ok": ok, "values": cn,
                       "message": "Container numbers match" if ok else "Container numbers differ between documents",
                       "message_fi": "Konttinumerot täsmäävät" if ok else "Konttinumerot eroavat asiakirjojen välillä"})
    # consignee: invoice buyer / consignee vs packing list / transport doc consignee vs the customer
    cons = []
    for d in docs:
        name = field_value(d, "consignee_name") or (field_value(d, "buyer_name") if d["type"] == "commercial_invoice" else None)
        if name:
            cons.append({"document_id": d["id"], "type": d["type"], "filename": d.get("filename"), "value": name})
    if cons:
        ref = (company or {}).get("name") if shipment.get("direction") == "import" else None
        base = ref or cons[0]["value"]
        worst = min(party_similarity(base, c["value"]) for c in cons)
        ok = worst >= 0.8
        checks.append({"rule": "DOC-3", "key": "consignee", "ok": ok if (len(cons) >= 2 or ref) else None,
                       "values": cons + ([{"document_id": None, "type": "customer", "value": ref}] if ref else []),
                       "message": "Consignee matches on every document" if ok else
                       "Consignee differs between documents: " + "; ".join(sorted({c["value"] for c in cons})),
                       "message_fi": "Vastaanottaja on sama kaikissa asiakirjoissa" if ok else
                       "Vastaanottaja eroaa asiakirjojen välillä: " + "; ".join(sorted({c["value"] for c in cons}))})
    inv_nos = {str(field_value(d, "invoice_number")).strip().upper() for d in docs
               if d["type"] == "commercial_invoice" and field_value(d, "invoice_number")}
    refs = [{"document_id": d["id"], "type": d["type"], "value": field_value(d, "invoice_number")} for d in docs
            if d["type"] == "packing_list" and field_value(d, "invoice_number")]
    if inv_nos and refs:
        ok = all(str(r["value"]).strip().upper() in inv_nos for r in refs)
        checks.append({"rule": "DOC-3", "key": "invoice_reference", "ok": ok, "values": refs,
                       "message": "Packing list refers to the commercial invoice" if ok else
                       "Packing list refers to a different invoice number than the commercial invoice",
                       "message_fi": "Pakkausluettelo viittaa kauppalaskuun" if ok else
                       "Pakkausluettelo viittaa eri laskunumeroon kuin kauppalasku"})
    stamp = now_iso()
    for c in checks:
        c["checked_at"] = stamp
    await db.documents.update_many({"shipment_id": shipment_id}, {"$set": {"checks": checks}})
    return checks


async def link_to_shipment(doc_id: str, shipment_id: str | None):
    await db.documents.update_one({"id": doc_id}, {"$set": {"shipment_id": shipment_id}})
