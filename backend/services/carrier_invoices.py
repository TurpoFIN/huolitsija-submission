"""Carrier and agent purchase invoices (ostolaskut): ingest -> extract -> audit -> match to accruals -> decide.

Pipeline (job `finance.carrier_invoice_process`, one per invoice):
1. Extraction. Finvoice 3.0 / PEPPOL BIS 3 XML is parsed deterministically (services.finvoice; Finvoice is validated
   against Finanssiala's official XSD). PDFs and images go to the model (core.llm.llm_json, agent
   "carrier_invoice_parser") which must return the fields printed on the document with a confidence per field. When
   no model is configured, or the answer is not valid, the invoice is `needs_review` with an owner-queue card; no field
   is ever filled in by guesswork.
2. Deterministic audit (`audit.checks`, each with message_fi/message_en): arithmetic, IBAN mod-97, BIC shape, Finnish
   reference 7-3-1 / RF ISO 11649, ISO 6346 container check digits, invoice number, due date, duplicates, IBAN change
   against the supplier's previously approved invoices (payment-diversion guard), VAT treatment plausibility, supplier
   resolved to a `carriers` row.
3. EUR at the ECB reference rate of the invoice date (services.fx).
4. Matching: shipment references, container numbers, B/L / AWB / CMR numbers and booking numbers -> shipments -> their
   cost accruals for this supplier; invoice lines are allocated per shipment; variance vs. the accrual.
5. Decision through finance_core.agent_decide: within tolerance and all checks ok -> Autopilot approves
   (`carrier_invoice.approved`, payables are created from that event); over tolerance -> `disputed`, always an owner
   card (`carrier_invoice_dispute`) and, under Autopilot, a credit-note request e-mail to the carrier.

E-mail for disputes: AP_EMAIL_PROVIDER=smtp sends with smtplib to AP_SMTP_HOST/PORT (AP_SMTP_SECURITY none|starttls|ssl,
AP_SMTP_USER/PASSWORD optional) from AP_EMAIL_FROM (default ostolaskut@huolitsija.test); any other value uses
core.email.send_email (EMAIL_PROVIDER postmark|resend|log). Going live = point AP_SMTP_* at the company mail server.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import smtplib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from rapidfuzz import fuzz

from core import llm
from core.approvals import cancel_approvals, request_approval
from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.notify import bi, notify_role
from core.storage import get_file, put_file
from core.sandbox import is_test_company
from core.util import NO_ID, new_id, now_iso
from services import finance_core as fc
from services import finvoice
from services.fx import FxUnavailable, convert, eur_rate

log = logging.getLogger("finance.ap")

register_index("carrier_invoices", "id", unique=True)
register_index("carrier_invoices", "sha256")
register_index("carrier_invoices", [("status", 1), ("created_at", -1)])
register_index("carrier_invoices", [("supplier.carrier_id", 1), ("number", 1)])

STATUSES = ["received", "extracting", "needs_review", "matched", "disputed", "approved", "scheduled", "paid", "rejected",
            "duplicate"]
OPEN_REVIEW = ("received", "extracting", "needs_review", "matched", "disputed", "duplicate")
ALLOWED_TYPES = {"application/pdf": "pdf", "application/xml": "xml", "text/xml": "xml", "image/png": "image",
                 "image/jpeg": "image", "image/jpg": "image"}
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
      "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
FI_VAT_RATES = (25.5, 24.0, 14.0, 13.5, 10.0, 0.0)  # 24 % until 31.8.2024; transport services use 25.5 % or 0 % (AVL 71 §)

SHIPMENT_REF_RE = re.compile(r"\bHUO-\d{4}-\d{6}\b")
CONTAINER_RE = re.compile(r"\b([A-Z]{3}[UJZ])\s?(\d{6})\s?(\d)\b")
DOC_RE = re.compile(r"\b(?:B/?L|BOL|Bill of Lading|Konossementti|AWB|MAWB|HAWB|CMR|Waybill|Rahtikirja)\s*(?:No\.?|nro|number|#)?"
                    r"\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-]{4,24}(?: \d{4,10})?)", re.I)
BOOKING_RE = re.compile(r"\b(?:Booking|Varaus(?:numero)?|Bkg)\s*(?:No\.?|nro|number|ref\.?|#)?\s*[:#.]?\s*([A-Z0-9]{6,20})", re.I)


def T(fi: str, en: str) -> dict:
    return {"fi": fi, "en": en}


# --------------------------------------------------------------------------------------------- validators
def iban_valid(iban: str | None) -> bool:
    s = re.sub(r"\s", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def bic_valid(bic: str | None) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?", (bic or "").strip().upper()))


def fi_reference_valid(ref: str | None) -> bool:
    digits = re.sub(r"\s", "", ref or "")
    if not re.fullmatch(r"\d{4,20}", digits):
        return False
    base, check = digits[:-1], int(digits[-1])
    weights = (7, 3, 1)
    total = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(base)))
    return (10 - total % 10) % 10 == check


def rf_reference_valid(ref: str | None) -> bool:
    s = re.sub(r"\s", "", ref or "").upper()
    if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def iso6346_valid(code: str) -> bool:
    s = re.sub(r"\s", "", code or "").upper()
    if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", s):
        return False
    values, v = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if v % 11 == 0:
            v += 1
        values[ch] = v
        v += 1
    total = sum((values[c] if c.isalpha() else int(c)) * (2 ** i) for i, c in enumerate(s[:10]))
    return total % 11 % 10 == int(s[10])


def _norm(name: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    s = re.sub(r"\b(oy|oyj|ab|ltd|limited|gmbh|as|a s|sa|bv|nv|inc|llc|co|kg|ag|plc|sp z o o)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cents_from_amount(v) -> int | None:
    return finvoice.cents(v) if not isinstance(v, (int, float)) else int(
        (Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _public(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc.pop("_id", None)
    doc.pop("text", None)
    return doc


# --------------------------------------------------------------------------------------------- ingest
async def ingest_file(data: bytes, filename: str, content_type: str, *, source: str, actor: dict | None,
                      message_id: str | None = None, sender: str | None = None, email_meta: dict | None = None,
                      company_id: str | None = None) -> dict:
    """Store one purchase invoice file and queue its processing. Same bytes twice -> the existing invoice."""
    if not data:
        raise ValueError("The file is empty.")
    ctype = _content_type(content_type, filename, data)
    if ctype not in ALLOWED_TYPES:
        raise ValueError("Send the invoice as PDF, Finvoice/PEPPOL XML, PNG or JPEG.")
    scope = {}
    if company_id:
        company = await db.companies.raw.find_one({"id": company_id}, NO_ID)
        if not company:
            raise ValueError("The invoice company does not exist.")
        scope = {"company_id": company_id}
        if is_test_company(company):
            scope["test"] = True
    sha = hashlib.sha256(data).hexdigest()
    existing = await db.carrier_invoices.find_one({"sha256": sha}, NO_ID)
    if existing:
        if company_id and existing.get("company_id") != company_id:
            raise ValueError("This invoice file already belongs to another or unscoped company.")
        note = {"at": now_iso(), "source": source, "filename": filename, "message_id": message_id, "sender": sender}
        await db.carrier_invoices.update_one({"id": existing["id"]}, {"$push": {"duplicate_receipts": note}})
        await audit(actor, "carrier_invoice_duplicate_file", "carrier_invoice", existing["id"], note)
        existing = _public(existing)
        existing["duplicate_of_existing"] = True
        # A prior delivery may have stopped after saving the invoice but before
        # enqueue. Same key repairs that gap without starting a second process.
        if existing.get("status") in ("received", "extracting"):
            await fc.enqueue("finance.carrier_invoice_process", {"id": existing["id"]}, key=f"ci:{existing['id']}",
                             subject={"type": "carrier_invoice", "id": existing["id"]})
        return existing
    file_id = await put_file(data, filename or "invoice", ctype, {"kind": "carrier_invoice", "sha256": sha})
    is_finvoice = ALLOWED_TYPES[ctype] == "xml" and finvoice.detect(data) == "finvoice"
    doc = {"id": new_id(), "supplier": {"carrier_id": None, "name": None, "business_id": None, "vat_id": None,
                                        "country": None, "iban": None, "bic": None},
           "number": None, "invoice_date": None, "due_date": None, "currency": None, "net_cents": None, "vat_cents": None,
           "total_cents": None, "eur": {}, "fx": {}, "lines": [],
           "references": {"shipment_refs": [], "container_nos": [], "bl_nos": [], "booking_nos": []},
           "payment": {"iban": None, "bic": None, "reference": None, "message": None},
           "source": "finvoice" if (is_finvoice and source == "upload") else source, "received_via": source,
           "file_id": file_id, "filename": filename, "content_type": ctype, "size": len(data), "sha256": sha,
           "message_id": message_id, "sender": sender, "email": email_meta or None,
           "extraction": {"status": "pending", "model": None, "confidence": None, "warnings": [], "fields": {}},
           "matches": [], "audit": {"checks": [], "variance_eur_cents": None, "variance_pct": None, "within_tolerance": None},
           "vat_treatment": None, "document_type": None, "status": "received", "dispute": None, "payable_id": None,
           "credit_note_ids": [], "credit_for": None, "duplicate_receipts": [], "history": [],
           "created_by": (actor or {}).get("id") if actor else None, "created_at": now_iso(), "updated_at": now_iso(),
           **scope}
    await db.carrier_invoices.insert_one(dict(doc))
    await audit(actor, "carrier_invoice_received", "carrier_invoice", doc["id"],
                {"filename": filename, "source": source, "message_id": message_id, "sender": sender})
    doc = _public(doc)
    await emit("carrier_invoice.received", {"carrier_invoice": doc})
    await fc.enqueue("finance.carrier_invoice_process", {"id": doc["id"]}, key=f"ci:{doc['id']}",
                     subject={"type": "carrier_invoice", "id": doc["id"]})
    return doc


def _content_type(content_type: str | None, filename: str | None, data: bytes) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    name = (filename or "").lower()
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if name.endswith(".xml") or ct in ("application/xml", "text/xml") or data.lstrip()[:5] == b"<?xml":
        return "application/xml"
    return ct


# --------------------------------------------------------------------------------------------- extraction
PARSER_SYSTEM = """You read purchase invoices that carriers, freight agents and customs agents send to a Finnish
freight forwarder (Huolitsija Oy). Return exactly what is printed on the document. Never guess, never compute a value
that is not printed, never fill in a field from general knowledge: use null for anything you cannot read on the page.
Amounts are decimal numbers in the invoice currency as printed (e.g. 1234.50), dates ISO YYYY-MM-DD, IBAN and BIC
without spaces, VAT ids with their country prefix. Shipment references look like HUO-2026-000123; container numbers
are 4 letters + 7 digits (ISO 6346); B/L, AWB, CMR and booking numbers are copied exactly as printed."""

PARSER_PROMPT = """Extract this invoice into one JSON object with these keys:
{
 "document_type": "invoice" | "credit_note",
 "warnings": [str],
 "supplier": {"name": str, "business_id": str|null, "vat_id": str|null, "country": "ISO alpha-2"|null,
              "iban": str|null, "bic": str|null},
 "buyer_name": str|null,
 "number": str|null, "invoice_date": "YYYY-MM-DD"|null, "due_date": "YYYY-MM-DD"|null, "currency": "ISO 4217"|null,
 "net_amount": number|null, "vat_amount": number|null, "total_amount": number|null,
 "payment_reference": str|null, "original_invoice_number": str|null,
 "lines": [{"description": str, "amount": number (net, excl. VAT), "vat_rate_pct": number|null,
            "vat_amount": number|null, "shipment_ref": str|null, "container_no": str|null, "bl_no": str|null,
            "booking_no": str|null}],
 "references": {"shipment_refs": [str], "container_nos": [str], "bl_nos": [str], "booking_nos": [str]},
 "confidence": {"overall": 0..1, "supplier": 0..1, "number": 0..1, "amounts": 0..1, "iban": 0..1, "lines": 0..1}
}
Credit notes keep the printed sign; if the document is a credit note and amounts are printed positive, return them
positive and set document_type to credit_note. Report test/sample/do-not-pay markings or inconsistent evidence in warnings."""


def _llm_fields(raw) -> tuple[dict | None, list[str], float | None]:
    """Validate the model's answer. Returns (fields, problems, confidence)."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["The model did not return a JSON object."], None
    conf = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    try:
        overall = float(conf.get("overall")) if conf.get("overall") is not None else None
    except (TypeError, ValueError):
        overall = None
    if overall is None or not 0 <= overall <= 1:
        problems.append("No overall confidence between 0 and 1.")
    sup = raw.get("supplier") if isinstance(raw.get("supplier"), dict) else {}
    out = finvoice._empty("model")
    out["supplier"].update({"name": (sup.get("name") or None), "business_id": sup.get("business_id") or None,
                            "vat_id": (str(sup.get("vat_id")).replace(" ", "").upper() if sup.get("vat_id") else None),
                            "country": (str(sup.get("country")).upper()[:2] if sup.get("country") else None),
                            "iban": re.sub(r"\s", "", str(sup["iban"])).upper() if sup.get("iban") else None,
                            "bic": str(sup["bic"]).strip().upper() if sup.get("bic") else None})
    out["supplier"]["country"] = out["supplier"]["country"] or finvoice._country_of(out["supplier"]["vat_id"], None)
    out["number"] = str(raw["number"]).strip() if raw.get("number") not in (None, "") else None
    for k in ("invoice_date", "due_date"):
        out[k] = finvoice.fdate(str(raw.get(k) or ""))
        if raw.get(k) and not out[k]:
            problems.append(f"{k} is not a date: {raw.get(k)!r}")
    out["currency"] = str(raw.get("currency")).upper() if raw.get("currency") else None
    if out["currency"] and not re.fullmatch(r"[A-Z]{3}", out["currency"]):
        problems.append(f"currency is not an ISO code: {raw.get('currency')!r}")
    for src, dst in (("net_amount", "net_cents"), ("vat_amount", "vat_cents"), ("total_amount", "total_cents")):
        v = raw.get(src)
        out[dst] = _cents_from_amount(v) if v not in (None, "") else None
        if v not in (None, "") and out[dst] is None:
            problems.append(f"{src} is not a number: {v!r}")
    out["document_type"] = "credit_note" if raw.get("document_type") == "credit_note" else "invoice"
    if out["document_type"] == "credit_note":
        for k in ("net_cents", "vat_cents", "total_cents"):
            if out[k] is not None:
                out[k] = -abs(out[k])
    out["original_invoice_number"] = raw.get("original_invoice_number") or None
    out["payment"] = {"iban": out["supplier"]["iban"], "bic": out["supplier"]["bic"],
                      "reference": (str(raw.get("payment_reference")).strip() if raw.get("payment_reference") else None),
                      "message": None}
    out["buyer"]["name"] = raw.get("buyer_name")
    for ln in raw.get("lines") or []:
        if not isinstance(ln, dict):
            problems.append("A line is not an object.")
            continue
        amt = _cents_from_amount(ln.get("amount")) if ln.get("amount") not in (None, "") else None
        vat = _cents_from_amount(ln.get("vat_amount")) if ln.get("vat_amount") not in (None, "") else None
        if out["document_type"] == "credit_note":
            amt = -abs(amt) if amt is not None else None
            vat = -abs(vat) if vat is not None else None
        out["lines"].append({"description": ln.get("description"), "amount_cents": amt,
                             "vat_rate_pct": finvoice.pct(ln.get("vat_rate_pct")) if ln.get("vat_rate_pct") is not None else None,
                             "vat_cents": vat, "free_text": [x for x in (ln.get("shipment_ref"), ln.get("container_no"),
                                                                         ln.get("bl_no"), ln.get("booking_no")) if x],
                             "row_refs": [], "shipment_ref": ln.get("shipment_ref"), "container_no": ln.get("container_no"),
                             "bl_no": ln.get("bl_no"), "booking_no": ln.get("booking_no")})
    refs = raw.get("references") if isinstance(raw.get("references"), dict) else {}
    out["model_references"] = {k: [str(x) for x in (refs.get(k) or []) if x] for k in
                               ("shipment_refs", "container_nos", "bl_nos", "booking_nos")}
    if not out["number"] and out["total_cents"] is None and not out["supplier"]["name"]:
        problems.append("The answer has no supplier, number or total: nothing was read.")
    out["confidence"] = {k: v for k, v in conf.items() if isinstance(v, (int, float))}
    return out, problems, overall


async def _pdf_text(data: bytes) -> str:
    """Text layer of a PDF (no OCR), used only to find references for matching."""
    def run():
        try:
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:20])
        except Exception:  # noqa: BLE001 - scanned or broken PDF: no text layer
            return ""
    return await asyncio.to_thread(run)


async def _extract(ci: dict, data: bytes) -> tuple[dict | None, dict]:
    """(fields or None, extraction record)."""
    kind = ALLOWED_TYPES.get(ci["content_type"])
    if kind == "xml":
        try:
            fields = finvoice.parse(data)
        except finvoice.InvoiceParseError as exc:
            return None, {"status": "failed", "model": None, "confidence": None, "warnings": [str(exc)], "fields": {}}
        rec = {"status": "done", "model": None, "method": fields["format"], "confidence": None, "warnings": [],
               "fields": {k: v for k, v in fields.items() if k not in ("text", "lines")}}
        if fields["format"] == "finvoice-3.0":
            v = finvoice.validate_finvoice(data)
            rec["xsd"] = v
            if not v["valid"]:
                rec["warnings"].append("Finvoice XSD: " + "; ".join(v["errors"][:3]))
        return fields, rec
    files = [(ci["content_type"], data, ci.get("filename") or "invoice")]
    try:
        raw = await llm.llm_json("carrier_invoice_parser", PARSER_SYSTEM, PARSER_PROMPT, files=files, max_tokens=4000,
                                 ref={"type": "carrier_invoice", "id": ci["id"]})
    except llm.LLMUnavailable as exc:
        return None, {"status": "needs_review", "model": None, "confidence": None, "fields": {},
                      "warnings": [f"No AI model available: {exc}"], "reason": "llm_unavailable"}
    except Exception as exc:  # noqa: BLE001 - provider errors and unparseable answers go to a person
        return None, {"status": "needs_review", "model": llm.provider_name(), "confidence": None, "fields": {},
                      "warnings": [f"The model call failed: {str(exc)[:300]}"], "reason": "llm_error"}
    fields, problems, conf = _llm_fields(raw)
    if fields is None or problems:
        return None, {"status": "needs_review", "model": llm.provider_name(), "confidence": conf, "fields": {},
                      "raw": raw if isinstance(raw, (dict, list)) else None, "warnings": problems, "reason": "invalid_output"}
    warnings = [str(x)[:500] for x in raw.get("warnings", []) if isinstance(x, str)] if isinstance(raw.get("warnings"), list) else []
    if ci["content_type"] == "application/pdf":
        fields["text"] = await _pdf_text(data)
        if re.search(r"do not pay|ei saa maksaa|testilasku", fields["text"], re.I):
            warnings.append("Document marked as test or do not pay")
    rec = {"status": "done", "model": llm.provider_name(), "method": "llm", "confidence": conf, "warnings": warnings,
           "field_confidence": fields.get("confidence") or {},
           "fields": {k: v for k, v in fields.items() if k not in ("text", "lines")}}
    return fields, rec


# --------------------------------------------------------------------------------------------- references
def _doc_refs(text: str) -> set[str]:
    out = set()
    for m in DOC_RE.finditer(text or ""):
        out.add(m.group(1).strip().upper())
        out.add(re.sub(r"\s+", " ", m.group(0)).strip().upper())
    return out


def _refs(fields: dict) -> tuple[dict, list[dict]]:
    """References from the parsed fields + regex over all text. Returns (references, per-line refs)."""
    text_parts = [fields.get("text") or "", " ".join(fields.get("free_text") or []), fields.get("order_ref") or "",
                  fields.get("buyer_reference") or "", (fields.get("payment") or {}).get("message") or ""]
    refs = {"shipment_refs": set(), "container_nos": set(), "bl_nos": set(), "booking_nos": set()}
    mr = fields.get("model_references") or {}
    for k in refs:
        refs[k].update(x.strip().upper() for x in mr.get(k) or [] if x)
    line_refs = []
    for ln in fields.get("lines") or []:
        t = " ".join([ln.get("description") or "", " ".join(ln.get("free_text") or []), " ".join(ln.get("row_refs") or [])])
        text_parts.append(t)
        lr = {"shipment_refs": set(SHIPMENT_REF_RE.findall(t.upper())),
              "container_nos": {"".join(m) for m in CONTAINER_RE.findall(t.upper())},
              "bl_nos": _doc_refs(t), "booking_nos": {m.upper() for m in BOOKING_RE.findall(t)}}
        for key, fld in (("shipment_refs", "shipment_ref"), ("container_nos", "container_no"), ("bl_nos", "bl_no"),
                         ("booking_nos", "booking_no")):
            if ln.get(fld):
                lr[key].add(re.sub(r"\s", "", str(ln[fld]).upper()) if key == "container_nos" else str(ln[fld]).strip().upper())
        # Row identifiers often carry the B/L or booking number without a label.
        for rr in ln.get("row_refs") or []:
            lr["bl_nos"].add(rr.strip().upper())
            lr["booking_nos"].add(rr.strip().upper())
        line_refs.append(lr)
        for k in refs:
            refs[k].update(lr[k])
    alltext = " ".join(text_parts)
    refs["shipment_refs"].update(SHIPMENT_REF_RE.findall(alltext.upper()))
    refs["container_nos"].update("".join(m) for m in CONTAINER_RE.findall(alltext.upper()))
    refs["bl_nos"].update(_doc_refs(alltext))
    refs["booking_nos"].update(m.upper() for m in BOOKING_RE.findall(alltext))
    return {k: sorted(v) for k, v in refs.items()}, line_refs


async def _shipments_for(refs: dict) -> dict[str, dict]:
    ors = []
    if refs.get("shipment_refs"):
        ors.append({"reference": {"$in": refs["shipment_refs"]}})
    if refs.get("container_nos"):
        ors += [{"references.container_numbers": {"$in": refs["container_nos"]}},
                {"legs.container_numbers": {"$in": refs["container_nos"]}}]
    docs = sorted(set(refs.get("bl_nos") or []))
    if docs:
        variants = docs + [d.replace(" ", "") for d in docs]
        ors += [{"legs.transport_doc_no": {"$in": variants}}, {"references.awb_no": {"$in": variants}},
                {"references.bl_no": {"$in": variants}}]
    if refs.get("booking_nos"):
        ors.append({"references.booking_no": {"$in": refs["booking_nos"]}})
    if not ors:
        return {}
    out = {}
    async for s in db.shipments.raw.find({"$or": ors}, {"_id": 0, "id": 1, "reference": 1, "company_id": 1, "legs": 1,
                                                        "references": 1}).limit(50):
        out[s["id"]] = s
    return out


def _line_shipments(lr: dict, shipments: dict[str, dict]) -> set[str]:
    hit = set()
    for sid, s in shipments.items():
        conts = set((s.get("references") or {}).get("container_numbers") or [])
        docs = {(s.get("references") or {}).get(k) for k in ("awb_no", "bl_no", "booking_no")}
        for lg in s.get("legs") or []:
            conts.update(lg.get("container_numbers") or [])
            docs.add(lg.get("transport_doc_no"))
        docs = {d.upper() for d in docs if d} | {d.upper().replace(" ", "") for d in docs if d}
        if s.get("reference") in lr["shipment_refs"] or conts & lr["container_nos"] or docs & (lr["bl_nos"] | lr["booking_nos"]):
            hit.add(sid)
    return hit


# --------------------------------------------------------------------------------------------- supplier
async def resolve_supplier(sup: dict) -> dict | None:
    """carriers row by VAT id / business id, then by name (rapidfuzz token_sort_ratio >= 90)."""
    vat = (sup.get("vat_id") or "").replace(" ", "").upper()
    bid = (sup.get("business_id") or "").strip()
    ors = []
    if vat:
        ors += [{"vat_id": vat}, {"billing.vat_id": vat}]
    if bid:
        ors += [{"business_id": bid}, {"billing.business_id": bid}]
    if ors:
        row = await db.carriers.find_one({"$or": ors}, NO_ID)
        if row:
            return {**row, "_resolved_by": "vat_id" if vat else "business_id", "_score": 100}
    name = _norm(sup.get("name"))
    if not name:
        return None
    best, best_score = None, 0.0
    async for row in db.carriers.find({}, {"_id": 0, "id": 1, "name": 1, "code": 1, "country": 1, "contact_email": 1,
                                           "vat_id": 1, "business_id": 1, "kind": 1, "iban": 1, "bic": 1,
                                           "ap_email": 1, "billing_email": 1}):
        score = max(fuzz.token_sort_ratio(name, _norm(row.get("name"))), fuzz.ratio(name, _norm(row.get("code"))))
        if score > best_score:
            best, best_score = row, score
    if best and best_score >= 90:
        return {**best, "_resolved_by": "name", "_score": round(best_score, 1)}
    return None


async def known_ibans(carrier_id: str | None, vat_id: str | None, exclude_id: str) -> set[str]:
    """IBANs this supplier has been paid to / approved for before (plus one stored on the carrier row)."""
    ors = []
    if carrier_id:
        ors.append({"supplier.carrier_id": carrier_id})
    if vat_id:
        ors.append({"supplier.vat_id": vat_id})
    out: set[str] = set()
    if ors:
        async for ci in db.carrier_invoices.find({"$or": ors, "id": {"$ne": exclude_id},
                                                  "status": {"$in": ["approved", "scheduled", "paid"]}},
                                                 {"_id": 0, "payment.iban": 1, "supplier.iban": 1}):
            iban = (ci.get("payment") or {}).get("iban") or (ci.get("supplier") or {}).get("iban")
            if iban:
                out.add(iban)
    if carrier_id:
        row = await db.carriers.find_one({"id": carrier_id}, {"_id": 0, "iban": 1, "bank": 1})
        for v in [(row or {}).get("iban"), ((row or {}).get("bank") or {}).get("iban")]:
            if v:
                out.add(re.sub(r"\s", "", v).upper())
    return out


# --------------------------------------------------------------------------------------------- audit checks
def _chk(rule: str, ok: bool, fi: str, en: str, **extra) -> dict:
    return {"rule": rule, "ok": bool(ok), "message_fi": fi, "message_en": en, **extra}


def _eur(c: int | None, cur: str = "EUR") -> str:
    if c is None:
        return "—"
    return f"{c / 100:,.2f} {cur}".replace(",", " ")


async def _checks(ci: dict, f: dict, carrier: dict | None, xsd: dict | None) -> tuple[list[dict], str | None]:
    checks: list[dict] = []
    cur = f.get("currency") or "EUR"
    if xsd is not None:
        checks.append(_chk("finvoice_xsd", xsd["valid"], "Finvoice 3.0 -sanoma on skeeman mukainen" if xsd["valid"]
                           else "Finvoice-sanoma ei läpäise Finvoice 3.0 -skeemaa: " + "; ".join(xsd["errors"][:2]),
                           "Finvoice 3.0 message is schema-valid" if xsd["valid"]
                           else "Finvoice message fails the Finvoice 3.0 schema: " + "; ".join(xsd["errors"][:2])))
    checks.append(_chk("invoice_number", bool(f.get("number")), "Laskunumero löytyy" if f.get("number") else "Laskunumero puuttuu",
                       "Invoice number present" if f.get("number") else "Invoice number missing"))
    lines = f.get("lines") or []
    net, vat, total = f.get("net_cents"), f.get("vat_cents"), f.get("total_cents")
    if lines and net is not None and all(ln.get("amount_cents") is not None for ln in lines):
        s = sum(ln["amount_cents"] for ln in lines)
        ok = abs(s - net) <= len(lines)
        checks.append(_chk("lines_sum", ok, f"Rivien summa {_eur(s, cur)} = veroton {_eur(net, cur)}" if ok else
                           f"Rivien summa {_eur(s, cur)} ei täsmää verottomaan summaan {_eur(net, cur)}",
                           f"Line total {_eur(s, cur)} = net {_eur(net, cur)}" if ok else
                           f"Line total {_eur(s, cur)} does not equal the net amount {_eur(net, cur)}"))
    elif net is None:
        checks.append(_chk("lines_sum", False, "Veroton summa puuttuu", "Net amount missing"))
    if net is not None and vat is not None and total is not None:
        ok = abs(net + vat - total) <= max(1, len(lines))
        checks.append(_chk("total", ok, f"Veroton + ALV = yhteensä ({_eur(total, cur)})" if ok else
                           f"Veroton {_eur(net, cur)} + ALV {_eur(vat, cur)} ≠ yhteensä {_eur(total, cur)}",
                           f"Net + VAT = total ({_eur(total, cur)})" if ok else
                           f"Net {_eur(net, cur)} + VAT {_eur(vat, cur)} ≠ total {_eur(total, cur)}"))
    else:
        checks.append(_chk("total", False, "Summat puuttuvat (veroton, ALV tai yhteensä)", "Amounts missing (net, VAT or total)"))
    pay = f.get("payment") or {}
    iban = pay.get("iban") or (f.get("supplier") or {}).get("iban")
    bic = pay.get("bic") or (f.get("supplier") or {}).get("bic")
    checks.append(_chk("iban", iban_valid(iban), f"IBAN {iban} on kelvollinen" if iban_valid(iban) else
                       (f"IBAN {iban} ei läpäise tarkistetta (mod 97)" if iban else "IBAN puuttuu"),
                       f"IBAN {iban} is valid" if iban_valid(iban) else
                       (f"IBAN {iban} fails the mod-97 check" if iban else "IBAN missing")))
    if bic or (iban or "")[:2] not in EU:
        checks.append(_chk("bic", bic_valid(bic), f"BIC {bic} on muodoltaan oikein" if bic_valid(bic) else f"BIC {bic or '—'} on virheellinen",
                           f"BIC {bic} has a valid shape" if bic_valid(bic) else f"BIC {bic or '—'} is not valid"))
    ref = (pay.get("reference") or "").replace(" ", "")
    if ref:
        ok = rf_reference_valid(ref) if ref.upper().startswith("RF") else fi_reference_valid(ref)
        kind = "RF-viite (ISO 11649)" if ref.upper().startswith("RF") else "viitenumero (7-3-1)"
        kind_en = "RF reference (ISO 11649)" if ref.upper().startswith("RF") else "Finnish reference (7-3-1)"
        checks.append(_chk("reference", ok, f"{kind} {ref} on oikein" if ok else f"{kind} {ref}: tarkiste ei täsmää",
                           f"{kind_en} {ref} is valid" if ok else f"{kind_en} {ref}: check digit does not match"))
    bad = [c for c in (ci.get("references") or {}).get("container_nos", []) if not iso6346_valid(c)]
    if (ci.get("references") or {}).get("container_nos"):
        checks.append(_chk("container_numbers", not bad, "Konttinumerot ovat oikein (ISO 6346)" if not bad else
                           f"Konttinumeron tarkiste väärin: {', '.join(bad)}",
                           "Container numbers are valid (ISO 6346)" if not bad else
                           f"Container check digit wrong: {', '.join(bad)}"))
    idate, ddate = f.get("invoice_date"), f.get("due_date")
    if idate and ddate:
        ok = ddate >= idate
        checks.append(_chk("due_date", ok, f"Eräpäivä {ddate} ≥ laskun päivä {idate}" if ok else f"Eräpäivä {ddate} on ennen laskun päivää {idate}",
                           f"Due date {ddate} ≥ invoice date {idate}" if ok else f"Due date {ddate} is before the invoice date {idate}"))
    else:
        checks.append(_chk("due_date", False, "Laskun päivä tai eräpäivä puuttuu", "Invoice date or due date missing"))
    # duplicate: same supplier + number on another invoice
    if f.get("number"):
        q = {"id": {"$ne": ci["id"]}, "number": f["number"], "status": {"$nin": ["rejected", "duplicate"]}}
        sup = f.get("supplier") or {}
        ors = [{"supplier.carrier_id": carrier["id"]}] if carrier else []
        if sup.get("vat_id"):
            ors.append({"supplier.vat_id": sup["vat_id"]})
        if sup.get("name"):
            ors.append({"supplier.name": sup["name"]})
        dup = await db.carrier_invoices.find_one({**q, "$or": ors}, {"_id": 0, "id": 1, "status": 1}) if ors else None
        checks.append(_chk("duplicate", dup is None, "Ei aiempaa laskua samalla numerolla" if dup is None else
                           f"Sama toimittaja ja laskunumero {f['number']} on jo käsitelty", "No earlier invoice with this number"
                           if dup is None else f"Same supplier and invoice number {f['number']} already received",
                           duplicate_of=(dup or {}).get("id")))
    # IBAN change vs. known IBANs (payment-diversion fraud guard)
    known = await known_ibans((carrier or {}).get("id"), (f.get("supplier") or {}).get("vat_id"), ci["id"])
    if iban and known and iban not in known:
        checks.append(_chk("iban_change", False, f"IBAN on vaihtunut: aiemmin {', '.join(sorted(known))}, nyt {iban}. "
                           "Varmista muutos toimittajalta puhelimitse ennen maksua.",
                           f"IBAN changed: previously {', '.join(sorted(known))}, now {iban}. "
                           "Confirm the change with the supplier by phone before paying.", known=sorted(known)))
    else:
        checks.append(_chk("iban_change", True, "IBAN sama kuin aiemmin" if known else "Toimittajan ensimmäinen lasku: IBAN tallennetaan",
                           "IBAN same as before" if known else "First invoice from this supplier: IBAN is recorded"))
    # VAT treatment plausibility
    treatment, vat_check = _vat_treatment(f)
    checks.append(vat_check)
    checks.append(_chk("supplier", carrier is not None,
                       f"Toimittaja tunnistettu: {carrier['name']}" if carrier else f"Toimittajaa {(f.get('supplier') or {}).get('name') or '—'} ei löydy kuljetusliikkeistä",
                       f"Supplier resolved: {carrier['name']}" if carrier else f"Supplier {(f.get('supplier') or {}).get('name') or '—'} not found among carriers"))
    return checks, treatment


def _vat_treatment(f: dict) -> tuple[str | None, dict]:
    sup = f.get("supplier") or {}
    country = (sup.get("country") or finvoice._country_of(sup.get("vat_id"), None) or "").upper()
    vat = f.get("vat_cents")
    rates = {ln.get("vat_rate_pct") for ln in f.get("lines") or [] if ln.get("vat_rate_pct") is not None}
    if not country:
        return None, _chk("vat_treatment", False, "Toimittajan maata ei tunneta: ALV-käsittely tarkistettava",
                          "Supplier country unknown: check the VAT treatment")
    if country == "FI":
        ok = all(r in (25.5, 0.0, 24.0) for r in rates) if rates else vat is not None
        treatment = "zero_rated" if (vat == 0 and rates <= {0.0}) else "domestic"
        return treatment, _chk("vat_treatment", ok,
                               "Kotimainen toimittaja: ALV 25,5 % tai 0 % kansainvälinen kuljetus (AVL 71 §)" if ok else
                               f"Kotimaisen kuljetuspalvelun ALV-kanta {sorted(rates)} ei ole 25,5 % tai 0 % (AVL 71 §)",
                               "Finnish supplier: VAT 25.5 % or 0 % for international transport (VAT Act 71 §)" if ok else
                               f"Finnish transport VAT rate {sorted(rates)} is neither 25.5 % nor 0 % (VAT Act 71 §)",
                               country=country)
    treatment = "eu_reverse_charge" if country in EU else "non_eu_reverse_charge"
    ok = (vat or 0) == 0 and all(r == 0 for r in rates)
    if treatment == "eu_reverse_charge":
        return treatment, _chk("vat_treatment", ok, f"EU-toimittaja ({country}): 0 %, käännetty verovelvollisuus" if ok else
                               f"EU-toimittaja ({country}) veloittaa ALV:tä {_eur(vat)}: palvelun pitäisi olla käännetty verovelvollisuus",
                               f"EU supplier ({country}): 0 %, reverse charge" if ok else
                               f"EU supplier ({country}) charges VAT {_eur(vat)}: the service should be reverse charged",
                               country=country)
    return treatment, _chk("vat_treatment", ok, f"EU:n ulkopuolinen toimittaja ({country}): 0 %, käännetty verovelvollisuus" if ok else
                           f"EU:n ulkopuolinen toimittaja ({country}) veloittaa veroa {_eur(vat)}: tarkista",
                           f"Non-EU supplier ({country}): 0 %, reverse charge" if ok else
                           f"Non-EU supplier ({country}) charges tax {_eur(vat)}: check", country=country)


# --------------------------------------------------------------------------------------------- matching
async def _tolerance(accrued: int) -> int:
    s = await fc.get_settings()
    return max(int(s.get("ap_tolerance_abs_cents") or 0),
               int((Decimal(abs(accrued)) * Decimal(str(s.get("ap_tolerance_pct") or 0)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _supplier_matches(acc: dict, carrier: dict | None, sup: dict) -> bool:
    a = acc.get("supplier") or {}
    if carrier and a.get("carrier_id"):
        return a["carrier_id"] == carrier["id"]
    names = [n for n in [(carrier or {}).get("name"), sup.get("name")] if n]
    return any(fuzz.token_sort_ratio(_norm(n), _norm(a.get("name"))) >= 90 for n in names)


async def _match(ci: dict, f: dict, carrier: dict | None, line_refs: list[dict], rate: float | None) -> tuple[list[dict], dict]:
    """Allocate invoice lines to shipments and compare with their accruals. Returns (matches, info)."""
    shipments = await _shipments_for(ci["references"])
    info = {"candidate_shipments": [s.get("reference") for s in shipments.values()], "unallocated_cents": 0, "problems": []}
    if ci.get("company_id") and any(s.get("company_id") != ci["company_id"] for s in shipments.values()):
        info["problems"].append(T("Laskun viite kuuluu toiselle asiakkaalle",
                                  "An invoice reference belongs to another company"))
        return [], info
    if not shipments:
        info["problems"].append(T("Laskulta ei löytynyt yhtään tunnettua lähetystä, konttia tai rahtikirjaa",
                                  "No known shipment, container or B/L number found on the invoice"))
        return [], info
    lines = f.get("lines") or []
    alloc: dict[str, int] = {}
    if not lines or all(ln.get("amount_cents") is None for ln in lines):
        if len(shipments) == 1 and f.get("net_cents") is not None:
            alloc[next(iter(shipments))] = f["net_cents"]
        else:
            info["unallocated_cents"] = f.get("net_cents") or 0
    else:
        for ln, lr in zip(lines, line_refs):
            amt = ln.get("amount_cents") or 0
            hit = _line_shipments(lr, shipments)
            if len(hit) == 1:
                sid = next(iter(hit))
            elif len(shipments) == 1:
                sid = next(iter(shipments))
            else:
                info["unallocated_cents"] += amt
                continue
            alloc[sid] = alloc.get(sid, 0) + amt
    matches = []
    sup = f.get("supplier") or {}
    for sid, amt in alloc.items():
        s = shipments[sid]
        accs = await db.cost_accruals.find({"shipment_id": sid, "status": {"$nin": ["released", "written_off"]}}, NO_ID).to_list(20)
        accs = [a for a in accs if _supplier_matches(a, carrier, sup)]
        if not accs:
            info["problems"].append(T(f"{s.get('reference')}: ei kuluvarausta toimittajalle {sup.get('name') or '—'}",
                                      f"{s.get('reference')}: no accrual for supplier {sup.get('name') or '—'}"))
            continue
        acc = accs[0]
        if acc.get("status") == "no_booked_rate" or acc.get("expected_eur_cents") is None:
            info["problems"].append(T(f"{s.get('reference')}: kuluvarauksessa ei ole summaa", f"{s.get('reference')}: the accrual has no amount"))
            continue
        invoiced_eur = amt if rate is None else convert(amt, rate)
        other = sum(v for k, v in (acc.get("allocations") or {}).items() if k != ci["id"] and k not in (ci.get("credit_note_ids") or []))
        accrued = int(acc["expected_eur_cents"]) - int(other)
        variance = invoiced_eur - accrued
        tol = await _tolerance(accrued)
        matches.append({"accrual_id": acc["id"], "shipment_id": sid, "shipment_ref": s.get("reference"),
                        "accrued_eur_cents": accrued, "invoiced_eur_cents": invoiced_eur, "variance_eur_cents": variance,
                        "invoiced_cents": amt, "tolerance_eur_cents": tol, "within_tolerance": variance <= tol})
    return matches, info


async def _apply_matches(ci_id: str, matches: list[dict], *, extra_alloc: dict[str, int] | None = None):
    """Write this invoice's allocation onto its accruals and recompute actual/variance/status."""
    prev = await db.cost_accruals.find({"carrier_invoice_ids": ci_id}, {"_id": 0, "id": 1}).to_list(50)
    touched = {p["id"] for p in prev} | {m["accrual_id"] for m in matches}
    by_acc = {m["accrual_id"]: m for m in matches}
    for acc_id in touched:
        acc = await db.cost_accruals.find_one({"id": acc_id}, NO_ID)
        if not acc:
            continue
        allocations = dict(acc.get("allocations") or {})
        ids = list(acc.get("carrier_invoice_ids") or [])
        if acc_id in by_acc:
            allocations[ci_id] = by_acc[acc_id]["invoiced_eur_cents"]
            if ci_id not in ids:
                ids.append(ci_id)
        else:
            allocations.pop(ci_id, None)
            ids = [i for i in ids if i != ci_id]
        actual = sum(allocations.values()) if allocations else None
        expected = acc.get("expected_eur_cents") or 0
        variance = (actual - expected) if actual is not None else None
        tol = await _tolerance(expected)
        if actual is None:
            status = "open"
        elif variance > tol:
            status = "disputed"
        elif variance < -tol:
            status = "partially_matched"
        else:
            status = "matched"
        await db.cost_accruals.update_one({"id": acc_id}, {"$set": {
            "allocations": allocations, "carrier_invoice_ids": ids, "actual_eur_cents": actual,
            "variance_eur_cents": variance, "status": status, "updated_at": now_iso()}})


# --------------------------------------------------------------------------------------------- process
async def process(ci_id: str, *, manual_fields: dict | None = None, actor: dict | None = None) -> dict:
    ci = await db.carrier_invoices.find_one({"id": ci_id}, NO_ID)
    if not ci:
        raise ValueError(f"Carrier invoice {ci_id} not found")
    if ci["status"] in ("approved", "scheduled", "paid", "rejected") and not manual_fields:
        return _public(ci)
    await _set(ci_id, {"status": "extracting"})
    data, _name, _ct = await get_file(ci["file_id"])
    if manual_fields:
        fields = _manual(manual_fields, ci)
        rec = {"status": "done", "method": "manual", "model": None, "confidence": None, "warnings": [],
               "by": (actor or {}).get("name"), "fields": {k: v for k, v in fields.items() if k not in ("text", "lines")}}
    else:
        fields, rec = await _extract(ci, data)
    if fields is None:
        await _set(ci_id, {"status": "needs_review", "extraction": rec})
        ci = await db.carrier_invoices.find_one({"id": ci_id}, NO_ID)
        reason = "; ".join(rec.get("warnings") or []) or "extraction failed"
        await _review_card(ci, [fc.policy_reason("extraction_failed", "Laskun tietoja ei saatu luettua: " + reason,
                                                 "The invoice could not be read: " + reason)])
        return _public(await db.carrier_invoices.find_one({"id": ci_id}, NO_ID))
    return await _evaluate(ci_id, fields, rec)


def _manual(m: dict, ci: dict) -> dict:
    """Fields typed in by a person (rematch with fields)."""
    out = finvoice._empty("manual")
    sup = m.get("supplier") or {}
    out["supplier"].update({k: sup.get(k) for k in out["supplier"] if sup.get(k)})
    for k in ("number", "invoice_date", "due_date", "currency", "original_invoice_number"):
        if m.get(k):
            out[k] = str(m[k]).strip()
    out["currency"] = (out["currency"] or "EUR").upper()
    for k in ("net_cents", "vat_cents", "total_cents"):
        if m.get(k) is not None:
            out[k] = int(m[k])
    out["payment"].update({k: (m.get("payment") or {}).get(k) for k in ("iban", "bic", "reference", "message")})
    out["payment"]["iban"] = out["payment"]["iban"] or out["supplier"].get("iban")
    out["payment"]["bic"] = out["payment"]["bic"] or out["supplier"].get("bic")
    for ln in m.get("lines") or []:
        out["lines"].append({"description": ln.get("description"), "amount_cents": ln.get("amount_cents"),
                             "vat_rate_pct": ln.get("vat_rate_pct"), "vat_cents": ln.get("vat_cents"),
                             "free_text": [x for x in (ln.get("shipment_ref"), ln.get("container_no"), ln.get("bl_no")) if x],
                             "row_refs": [], "shipment_ref": ln.get("shipment_ref"), "container_no": ln.get("container_no"),
                             "bl_no": ln.get("bl_no")})
    out["document_type"] = "credit_note" if (m.get("document_type") == "credit_note" or (out["total_cents"] or 0) < 0) else "invoice"
    out["model_references"] = {k: list(m.get("references", {}).get(k) or []) for k in
                               ("shipment_refs", "container_nos", "bl_nos", "booking_nos")} if m.get("references") else {}
    return out


async def _evaluate(ci_id: str, f: dict, rec: dict) -> dict:
    ci = await db.carrier_invoices.find_one({"id": ci_id}, NO_ID)
    refs, line_refs = _refs(f)
    carrier = await resolve_supplier(f.get("supplier") or {})
    sup = dict(f.get("supplier") or {})
    supplier = {"carrier_id": (carrier or {}).get("id"), "name": sup.get("name") or (carrier or {}).get("name"),
                "business_id": sup.get("business_id"), "vat_id": sup.get("vat_id"),
                "country": sup.get("country") or (carrier or {}).get("country"),
                "iban": (f.get("payment") or {}).get("iban") or sup.get("iban"),
                "bic": (f.get("payment") or {}).get("bic") or sup.get("bic"),
                "carrier_name": (carrier or {}).get("name"), "resolved_by": (carrier or {}).get("_resolved_by"),
                "contact_email": (carrier or {}).get("ap_email") or (carrier or {}).get("billing_email") or (carrier or {}).get("contact_email")}
    cur = (f.get("currency") or "EUR").upper()
    lines = []
    for ln, lr in zip(f.get("lines") or [], line_refs):
        lines.append({"description": ln.get("description"), "shipment_ref": (sorted(lr["shipment_refs"]) or [None])[0],
                      "container_no": (sorted(lr["container_nos"]) or [None])[0], "bl_no": (sorted(lr["bl_nos"]) or [None])[0],
                      "amount_cents": ln.get("amount_cents"), "vat_rate_pct": ln.get("vat_rate_pct"), "vat_cents": ln.get("vat_cents"),
                      "quantity": ln.get("quantity"), "unit_price_cents": ln.get("unit_price_cents")})
    base = {"supplier": supplier, "number": f.get("number"), "invoice_date": f.get("invoice_date"), "due_date": f.get("due_date"),
            "currency": cur, "net_cents": f.get("net_cents"), "vat_cents": f.get("vat_cents"), "total_cents": f.get("total_cents"),
            "lines": lines, "references": refs, "payment": {k: (f.get("payment") or {}).get(k) for k in ("iban", "bic", "reference", "message")},
            "document_type": f.get("document_type") or "invoice", "original_invoice_number": f.get("original_invoice_number"),
            "extraction": rec, "text": (f.get("text") or "")[:20000]}
    base["payment"]["iban"] = base["payment"]["iban"] or supplier["iban"]
    base["payment"]["bic"] = base["payment"]["bic"] or supplier["bic"]
    ci.update(base)
    checks, treatment = await _checks(ci, f, carrier, rec.get("xsd"))
    if rec.get("warnings"):
        checks.append(_chk("extraction_warning", False, "Laskun tulkinta vaatii tarkistuksen", "; ".join(rec["warnings"])))
    # FX at the invoice date (ECB)
    rate = None
    eur, fx = {}, {}
    try:
        fxr = await eur_rate(cur, f.get("invoice_date") or now_iso()[:10])
        fx = {"currency": cur, "rate": fxr["rate"], "rate_date": fxr["rate_date"], "source": fxr["source"]}
        rate = None if cur == "EUR" else fxr["rate"]
        eur = {k: (None if f.get(k) is None else (f[k] if rate is None else convert(f[k], rate)))
               for k in ("net_cents", "vat_cents", "total_cents")}
        checks.append(_chk("fx", True, f"Kurssi {cur}: {fxr['rate']} ({fxr['rate_date']}, EKP)" if rate else "Euromääräinen lasku",
                           f"Rate {cur}: {fxr['rate']} ({fxr['rate_date']}, ECB)" if rate else "Invoice in euro"))
    except FxUnavailable as exc:
        checks.append(_chk("fx", False, f"EKP-kurssia ei saatu: {exc}", f"No ECB rate: {exc}"))
    base.update(eur=eur, fx=fx, vat_treatment=treatment)
    ci.update(eur=eur, fx=fx)
    if base["document_type"] == "credit_note":
        await _set(ci_id, {**base, "audit": {"checks": checks, "variance_eur_cents": None, "variance_pct": None,
                                              "within_tolerance": None}})
        return await _credit_note(ci_id, checks)
    matches, info = await _match(ci, f, carrier, line_refs, rate) if fx else ([], {"problems": [], "unallocated_cents": 0})
    accrued = sum(m["accrued_eur_cents"] for m in matches)
    variance = sum(m["variance_eur_cents"] for m in matches)
    within = bool(matches) and all(m["within_tolerance"] for m in matches) and not info["unallocated_cents"] and not info["problems"]
    audit_doc = {"checks": checks, "variance_eur_cents": variance if matches else None,
                 "variance_pct": round(100 * variance / accrued, 2) if accrued else None,
                 "within_tolerance": within if matches else None, "match_info": info}
    if matches and await db.cost_accruals.count_documents({"id": {"$in": [m["accrual_id"] for m in matches]}, "test": True}):
        base["test"] = True
    if matches:
        matched_shipments = await db.shipments.raw.find(
            {"id": {"$in": [m["shipment_id"] for m in matches]}}, {"_id": 0, "company_id": 1}).to_list(len(matches))
        companies = {s.get("company_id") for s in matched_shipments if s.get("company_id")}
        if len(companies) == 1:
            base["company_id"] = next(iter(companies))
        if companies and await db.companies.raw.count_documents(
                {"id": {"$in": list(companies)}, "$or": [{"test_tenant": True}, {"test": True}]}):
            base["test"] = True
    await _set(ci_id, {**base, "matches": matches, "audit": audit_doc})
    await _apply_matches(ci_id, matches)
    ci = await db.carrier_invoices.find_one({"id": ci_id}, NO_ID)
    failed = [c for c in checks if not c["ok"]]
    dup = next((c for c in failed if c["rule"] == "duplicate"), None)
    if dup:
        await _apply_matches(ci_id, [])
        await _set(ci_id, {"status": "duplicate", "duplicate_of": dup.get("duplicate_of")})
        await _review_card(await _get(ci_id), [fc.policy_reason("duplicate", dup["message_fi"], dup["message_en"])])
        return await _get_public(ci_id)
    if not matches or info["unallocated_cents"] or info["problems"]:
        reasons = [fc.policy_reason("unmatched", p["fi"], p["en"]) for p in info["problems"]]
        if info["unallocated_cents"]:
            reasons.append(fc.policy_reason("unallocated", f"Kohdistamatta {_eur(info['unallocated_cents'], cur)}",
                                            f"Unallocated {_eur(info['unallocated_cents'], cur)}"))
        if not reasons:
            reasons.append(fc.policy_reason("unmatched", "Laskua ei voitu kohdistaa kuluvaraukseen",
                                            "The invoice could not be matched to an accrual"))
        reasons += [fc.policy_reason(f"check_{c['rule']}", c["message_fi"], c["message_en"]) for c in failed]
        await _set(ci_id, {"status": "needs_review"})
        await _review_card(await _get(ci_id), reasons)
        return await _get_public(ci_id)
    over = [m for m in matches if not m["within_tolerance"]]
    if over:
        return await _dispute(ci_id, over, failed)
    await _set(ci_id, {"status": "matched"})
    ci = await _get(ci_id)
    await emit("carrier_invoice.matched", {"carrier_invoice": _public(dict(ci))})
    return await _decide_approve(ci, failed)


async def _decide_approve(ci: dict, failed: list[dict], *, note: str | None = None) -> dict:
    s = await fc.get_settings()
    reasons = [fc.policy_reason(f"check_{c['rule']}", c["message_fi"], c["message_en"]) for c in failed]
    eur_total = (ci.get("eur") or {}).get("total_cents")
    if eur_total is not None and eur_total > int(s.get("ap_auto_approve_max_cents") or 0):
        reasons.append(fc.policy_reason("amount_over_limit", f"Summa {_eur(eur_total)} ylittää automaattisen hyväksynnän rajan "
                                        f"{_eur(int(s.get('ap_auto_approve_max_cents') or 0))}",
                                        f"Amount {_eur(eur_total)} exceeds the auto-approval limit {_eur(int(s.get('ap_auto_approve_max_cents') or 0))}"))
    if any(c["rule"] == "iban_change" for c in failed) and not any(r["code"] == "check_iban_change" for r in reasons):
        reasons.append(fc.policy_reason("check_iban_change", "IBAN vaihtunut", "IBAN changed"))
    label = f"{ci['supplier'].get('name') or '—'} {ci.get('number') or ''}".strip()
    d = await fc.agent_decide(
        "approve_carrier_invoice", subject=("carrier_invoice", ci["id"]), subject_label=label,
        inputs={"matches": ci.get("matches"), "checks": [c["rule"] for c in ci["audit"]["checks"] if not c["ok"]],
                "eur_total_cents": eur_total, "note": note},
        decision={"status": "approved"}, confidence=(ci.get("extraction") or {}).get("confidence"),
        model=(ci.get("extraction") or {}).get("model"), amount_cents=eur_total, extra_reasons=reasons,
        approval_kind="carrier_invoice_review", title=f"Approve carrier invoice {label}",
        title_fi=f"Hyväksy ostolasku {label}", summary=_summary_en(ci), summary_fi=_summary_fi(ci),
        link=f"/ops/finance/carrier-invoices/{ci['id']}", approval_data=_card_data(ci))
    await _set(ci["id"], {"decision": {"id": d["id"], "outcome": d["outcome"], "reasons": d["reasons"],
                                       "approval_id": (d.get("approval") or {}).get("id"), "at": now_iso()}})
    if d["auto"]:
        return await approve(ci["id"], actor=fc.AGENT_ACTOR, note="Autopilot: matched within tolerance")
    return await _get_public(ci["id"])


def _card_data(ci: dict) -> dict:
    return {"carrier_invoice_id": ci["id"], "number": ci.get("number"), "supplier": ci.get("supplier"),
            "currency": ci.get("currency"), "total_cents": ci.get("total_cents"), "eur": ci.get("eur"),
            "matches": [{k: m.get(k) for k in ("shipment_ref", "shipment_id", "accrual_id", "accrued_eur_cents",
                                               "invoiced_eur_cents", "variance_eur_cents", "tolerance_eur_cents")}
                        for m in ci.get("matches") or []],
            "failed_checks": [c for c in (ci.get("audit") or {}).get("checks", []) if not c["ok"]]}


def _summary_fi(ci: dict) -> str:
    parts = [f"{m['shipment_ref']}: varattu {_eur(m['accrued_eur_cents'])}, laskutettu {_eur(m['invoiced_eur_cents'])}, "
             f"ero {_eur(m['variance_eur_cents'])}" for m in ci.get("matches") or []]
    return "; ".join(parts) or "Ei kohdistettu"


def _summary_en(ci: dict) -> str:
    parts = [f"{m['shipment_ref']}: accrued {_eur(m['accrued_eur_cents'])}, invoiced {_eur(m['invoiced_eur_cents'])}, "
             f"variance {_eur(m['variance_eur_cents'])}" for m in ci.get("matches") or []]
    return "; ".join(parts) or "Not matched"


async def _review_card(ci: dict, reasons: list[dict]):
    label = f"{(ci.get('supplier') or {}).get('name') or ci.get('filename') or '—'} {ci.get('number') or ''}".strip()
    d = await fc.agent_decide(
        "approve_carrier_invoice", subject=("carrier_invoice", ci["id"]), subject_label=label,
        inputs={"status": ci.get("status"), "extraction": (ci.get("extraction") or {}).get("status")},
        decision={"status": ci.get("status")}, confidence=(ci.get("extraction") or {}).get("confidence"),
        extra_reasons=reasons, approval_kind="carrier_invoice_review",
        amount_cents=(ci.get("eur") or {}).get("total_cents"),
        title=f"Carrier invoice needs review: {label}", title_fi=f"Ostolasku tarkistettava: {label}",
        summary="; ".join(r["en"] for r in reasons), summary_fi="; ".join(r["fi"] for r in reasons),
        link=f"/ops/finance/carrier-invoices/{ci['id']}", approval_data=_card_data(ci))
    await _set(ci["id"], {"decision": {"id": d["id"], "outcome": d["outcome"], "reasons": d["reasons"],
                                       "approval_id": (d.get("approval") or {}).get("id"), "at": now_iso()}})


async def _dispute(ci_id: str, over: list[dict], failed: list[dict], *, actor: dict | None = None, note: str | None = None) -> dict:
    ci = await _get(ci_id)
    dispute = {"opened_at": now_iso(), "reason": "over_tolerance" if over else "manual", "note": note,
               "shipments": [{k: m.get(k) for k in ("shipment_ref", "accrued_eur_cents", "invoiced_eur_cents",
                                                    "variance_eur_cents", "tolerance_eur_cents")} for m in (over or ci.get("matches") or [])],
               "claimed_eur_cents": sum(max(0, m["variance_eur_cents"]) for m in (over or ci.get("matches") or [])),
               "sent_at": None, "message_id": None, "to": None, "by": (actor or {}).get("name") if actor else None}
    await _set(ci_id, {"status": "disputed", "dispute": dispute})
    ci = await _get(ci_id)
    await emit("carrier_invoice.disputed", {"carrier_invoice": _public(dict(ci))})
    label = f"{ci['supplier'].get('name') or '—'} {ci.get('number') or ''}".strip()
    reasons = [fc.policy_reason("over_tolerance", f"{m['shipment_ref']}: laskutettu {_eur(m['invoiced_eur_cents'])}, varattu "
                                f"{_eur(m['accrued_eur_cents'])}, ylitys {_eur(m['variance_eur_cents'])} (raja {_eur(m['tolerance_eur_cents'])})",
                                f"{m['shipment_ref']}: invoiced {_eur(m['invoiced_eur_cents'])}, accrued {_eur(m['accrued_eur_cents'])}, "
                                f"over by {_eur(m['variance_eur_cents'])} (tolerance {_eur(m['tolerance_eur_cents'])})") for m in over]
    card_kw = dict(title=f"Carrier invoice over the booked rate: {label}", title_fi=f"Ostolasku ylittää varatun hinnan: {label}",
                   summary=_summary_en(ci), summary_fi=_summary_fi(ci), link=f"/ops/finance/carrier-invoices/{ci_id}")
    if actor is None:
        d = await fc.agent_decide(
            "dispute_carrier_invoice", subject=("carrier_invoice", ci_id), subject_label=label,
            inputs={"matches": ci.get("matches")}, decision={"status": "disputed", "send_credit_request": True},
            confidence=(ci.get("extraction") or {}).get("confidence"), model=(ci.get("extraction") or {}).get("model"),
            amount_cents=(ci.get("eur") or {}).get("total_cents"),
            extra_reasons=[fc.policy_reason(f"check_{c['rule']}", c["message_fi"], c["message_en"]) for c in failed],
            approval_kind="carrier_invoice_dispute", approval_data=_card_data(ci), **card_kw)
        approval = d.get("approval")
        if d["auto"]:
            await send_dispute_email(ci_id, actor=fc.AGENT_ACTOR)
        await _set(ci_id, {"decision": {"id": d["id"], "outcome": d["outcome"], "reasons": d["reasons"], "at": now_iso()}})
    else:
        approval = None
        await audit(actor, "carrier_invoice_disputed", "carrier_invoice", ci_id, {"note": note})
        await send_dispute_email(ci_id, actor=actor)
    if not approval:
        # The owner always sees a dispute, also when Autopilot already asked the carrier for a credit note.
        ci = await _get(ci_id)
        approval = await request_approval(
            "carrier_invoice_dispute", "carrier_invoice", ci_id, card_kw["title_fi"], summary=card_kw["summary_fi"],
            reason="; ".join(r["fi"] for r in reasons), requested_by=actor or fc.AGENT_ACTOR,
            data={**_card_data(ci), "reasons": reasons, "title_en": card_kw["title"], "summary_en": card_kw["summary"],
                  "dispute_sent_at": (ci.get("dispute") or {}).get("sent_at")},
            amount_cents=(ci.get("eur") or {}).get("total_cents"), link=card_kw["link"])
    await db.carrier_invoices.update_one({"id": ci_id}, {"$set": {"dispute.approval_id": approval["id"]}})
    return await _get_public(ci_id)


# --------------------------------------------------------------------------------------------- credit notes
async def _credit_note(ci_id: str, checks: list[dict]) -> dict:
    cn = await _get(ci_id)
    sup = cn.get("supplier") or {}
    q = {"id": {"$ne": ci_id}, "document_type": {"$ne": "credit_note"}, "status": {"$in": ["disputed", "matched", "needs_review"]}}
    ors = [{"supplier.carrier_id": sup["carrier_id"]}] if sup.get("carrier_id") else []
    if sup.get("vat_id"):
        ors.append({"supplier.vat_id": sup["vat_id"]})
    orig = None
    if ors and cn.get("original_invoice_number"):
        orig = await db.carrier_invoices.find_one({**q, "$or": ors, "number": cn["original_invoice_number"]}, NO_ID)
    if orig is None and ors:
        refs = cn.get("references") or {}
        cands = await db.carrier_invoices.find({**q, "$or": ors}, NO_ID).sort("created_at", -1).to_list(50)
        for c in cands:
            if c.get("number") and c["number"] in (cn.get("text") or ""):
                orig = c
                break
            cr = c.get("references") or {}
            if set(cr.get("shipment_refs") or []) & set(refs.get("shipment_refs") or []) or \
                    set(cr.get("container_nos") or []) & set(refs.get("container_nos") or []):
                orig = c
                break
    if orig is None:
        await _set(ci_id, {"status": "needs_review"})
        await _review_card(await _get(ci_id), [fc.policy_reason("credit_unmatched", "Hyvityslaskulle ei löytynyt alkuperäistä laskua",
                                                                 "No original invoice found for this credit note")])
        return await _get_public(ci_id)
    rate = (cn.get("fx") or {}).get("rate") if cn.get("currency") != "EUR" else None
    if cn.get("net_cents") is None:
        await _set(ci_id, {"status": "needs_review", "credit_for": orig["id"]})
        await _review_card(await _get(ci_id), [fc.policy_reason("credit_no_amount", "Hyvityslaskun summa puuttuu",
                                                                 "The credit note has no amount")])
        return await _get_public(ci_id)
    credit_eur = cn["net_cents"] if rate is None else convert(cn["net_cents"], rate)
    matches = []
    om = orig.get("matches") or []
    # Allocate the credit to the original's shipments: by reference, else all to a single-shipment original.
    refs = set((cn.get("references") or {}).get("shipment_refs") or [])
    targets = [m for m in om if m.get("shipment_ref") in refs] or (om if len(om) == 1 else [])
    if not targets:
        await _set(ci_id, {"status": "needs_review", "credit_for": orig["id"]})
        await _review_card(await _get(ci_id), [fc.policy_reason("credit_unallocated", "Hyvitystä ei voitu kohdistaa lähetykselle",
                                                                 "The credit could not be allocated to a shipment")])
        return await _get_public(ci_id)
    per = credit_eur // len(targets)
    rest = credit_eur - per * len(targets)
    for i, m in enumerate(targets):
        amt = per + (rest if i == 0 else 0)
        matches.append({"accrual_id": m["accrual_id"], "shipment_id": m["shipment_id"], "shipment_ref": m["shipment_ref"],
                        "accrued_eur_cents": m["accrued_eur_cents"], "invoiced_eur_cents": amt, "variance_eur_cents": amt,
                        "credit_for": orig["id"]})
    await _set(ci_id, {"matches": matches, "credit_for": orig["id"], "status": "matched"})
    await db.carrier_invoices.update_one({"id": orig["id"]}, {"$addToSet": {"credit_note_ids": ci_id}})
    await _apply_matches(ci_id, matches)
    # Net the original against all its credit notes.
    orig = await _get(orig["id"])
    credits = await db.carrier_invoices.find({"id": {"$in": orig.get("credit_note_ids") or []}}, NO_ID).to_list(20)
    net_ok, net_rows = True, []
    for m in orig.get("matches") or []:
        cr = sum(x["invoiced_eur_cents"] for c in credits for x in c.get("matches") or [] if x["accrual_id"] == m["accrual_id"])
        net_inv = m["invoiced_eur_cents"] + cr
        var = net_inv - m["accrued_eur_cents"]
        tol = await _tolerance(m["accrued_eur_cents"])
        net_rows.append({**m, "credited_eur_cents": cr, "net_invoiced_eur_cents": net_inv, "net_variance_eur_cents": var,
                         "within_tolerance": var <= tol})
        net_ok = net_ok and var <= tol
    amount_due = (orig.get("total_cents") or 0) + sum(c.get("total_cents") or 0 for c in credits)
    await _set(orig["id"], {"credit_netting": {"rows": net_rows, "within_tolerance": net_ok, "at": now_iso()},
                            "amount_due_cents": amount_due})
    await audit(fc.AGENT_ACTOR, "carrier_credit_note_netted", "carrier_invoice", orig["id"],
                {"credit_note_id": ci_id, "within_tolerance": net_ok, "amount_due_cents": amount_due})
    if net_ok:
        orig = await _get(orig["id"])
        failed = [c for c in checks if not c["ok"] and c["rule"] not in ("duplicate",)]
        res = await _decide_approve(orig, failed, note=f"credit note {cn.get('number')} nets the dispute within tolerance")
        if res.get("status") == "approved":
            await cancel_approvals(orig["id"], "carrier_invoice_dispute")
    return await _get_public(ci_id)


# --------------------------------------------------------------------------------------------- actions
async def _get(ci_id: str) -> dict:
    return await db.carrier_invoices.find_one({"id": ci_id}, NO_ID)


async def _get_public(ci_id: str) -> dict:
    return _public(await _get(ci_id))


async def _set(ci_id: str, changes: dict):
    changes = {**changes, "updated_at": now_iso()}
    await db.carrier_invoices.update_one({"id": ci_id}, {"$set": changes})


async def approve(ci_id: str, *, actor: dict | None, note: str | None = None) -> dict:
    ci = await _get(ci_id)
    if not ci:
        raise LookupError("Carrier invoice not found")
    if ci["status"] in ("approved", "scheduled", "paid"):
        return _public(ci)
    if ci.get("total_cents") is None or not ci.get("number"):
        raise PermissionError("Enter the invoice number and amounts (rematch with fields) before approving.")
    if ci["status"] == "duplicate":
        raise PermissionError("A duplicate invoice cannot be approved.")
    if ci["status"] == "rejected":
        raise PermissionError("A rejected invoice cannot be approved; upload it again if it was rejected by mistake.")
    amount_due = ci.get("amount_due_cents")
    if amount_due is None:
        amount_due = ci["total_cents"]
    await _set(ci_id, {"status": "approved", "approved_at": now_iso(), "amount_due_cents": amount_due,
                       "approved_by": (actor or {}).get("name") or (actor or {}).get("id"), "approval_note": note})
    await db.carrier_invoices.update_one({"id": ci_id}, {"$push": {"history": {"at": now_iso(), "status": "approved",
                                                                              "by": (actor or {}).get("name"), "note": note}}})
    await audit(actor, "carrier_invoice_approved", "carrier_invoice", ci_id, {"note": note, "amount_due_cents": amount_due})
    await cancel_approvals(ci_id)
    # An approved variance (owner accepted it, or a credit note netted it) closes the accrual as matched.
    await db.cost_accruals.update_many({"carrier_invoice_ids": ci_id, "status": "disputed"},
                                       {"$set": {"status": "matched", "variance_accepted_by": (actor or {}).get("name"),
                                                 "updated_at": now_iso()}})
    ci = await _get(ci_id)
    await emit("carrier_invoice.approved", {"carrier_invoice": _public(dict(ci)), "actor": actor})
    for cid in ci.get("credit_note_ids") or []:
        cn = await _get(cid)
        if cn and cn["status"] not in ("approved", "rejected"):
            await _set(cid, {"status": "approved", "approved_at": now_iso(), "approval_note": f"nets invoice {ci.get('number')}"})
            await emit("carrier_invoice.approved", {"carrier_invoice": _public(await _get(cid)), "actor": actor})
    return _public(ci)


async def reject(ci_id: str, *, actor: dict | None, note: str | None = None) -> dict:
    ci = await _get(ci_id)
    if not ci:
        raise LookupError("Carrier invoice not found")
    if ci["status"] in ("scheduled", "paid"):
        raise PermissionError("The invoice is already in a payment run; cancel the payable first.")
    await db.payables.update_many({"carrier_invoice_id": ci_id, "status": "approved"}, {"$set": {"status": "cancelled", "updated_at": now_iso()}})
    await _set(ci_id, {"status": "rejected", "rejected_at": now_iso(), "rejection_note": note})
    await _apply_matches(ci_id, [])
    await audit(actor, "carrier_invoice_rejected", "carrier_invoice", ci_id, {"note": note})
    await cancel_approvals(ci_id)
    return await _get_public(ci_id)


async def dispute(ci_id: str, *, actor: dict, note: str | None = None) -> dict:
    ci = await _get(ci_id)
    if not ci:
        raise LookupError("Carrier invoice not found")
    if ci["status"] in ("scheduled", "paid", "rejected"):
        raise PermissionError(f"The invoice is {ci['status']}; it can no longer be disputed.")
    if not ci.get("matches"):
        raise PermissionError("Match the invoice to a shipment before disputing it.")
    await cancel_approvals(ci_id, "carrier_invoice_review")
    over = [m for m in ci["matches"] if m["variance_eur_cents"] > 0] or ci["matches"]
    return await _dispute(ci_id, over, [], actor=actor, note=note)


async def rematch(ci_id: str, *, actor: dict, fields: dict | None = None) -> dict:
    ci = await _get(ci_id)
    if not ci:
        raise LookupError("Carrier invoice not found")
    if ci["status"] in ("approved", "scheduled", "paid"):
        raise PermissionError(f"The invoice is already {ci['status']}.")
    await cancel_approvals(ci_id)
    await audit(actor, "carrier_invoice_rematch", "carrier_invoice", ci_id, {"manual_fields": bool(fields)})
    return await process(ci_id, manual_fields=fields, actor=actor)


# --------------------------------------------------------------------------------------------- dispute e-mail
def _smtp_send(msg: EmailMessage, host: str, port: int, security: str, user: str | None, password: str | None):
    if security == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
    try:
        s.ehlo()
        if security == "starttls":
            s.starttls()
            s.ehlo()
        if user and password:
            s.login(user, password)
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass


def _dispute_text(ci: dict) -> tuple[str, str]:
    d = ci.get("dispute") or {}
    num = ci.get("number") or "—"
    rows_fi = [f"- {r['shipment_ref']}: sovittu {_eur(r['accrued_eur_cents'])}, laskutettu {_eur(r['invoiced_eur_cents'])}, "
               f"erotus {_eur(r['variance_eur_cents'])}" for r in d.get("shipments") or []]
    rows_en = [f"- {r['shipment_ref']}: agreed {_eur(r['accrued_eur_cents'])}, invoiced {_eur(r['invoiced_eur_cents'])}, "
               f"difference {_eur(r['variance_eur_cents'])}" for r in d.get("shipments") or []]
    subject = f"Hyvityspyyntö / Credit note request – lasku / invoice {num}"
    body = "\n".join([
        "Hei,", "",
        f"laskunne {num} ({_eur(ci.get('total_cents'), ci.get('currency') or 'EUR')}) ylittää varauksessa sovitun hinnan:",
        *rows_fi, "",
        f"Pyydämme hyvityslaskua erotuksesta {_eur(d.get('claimed_eur_cents'))} viitaten laskuun {num}, tai perustelua "
        "lisäveloitukselle. Emme maksa laskun kiistanalaista osaa ennen selvitystä.", "",
        "Ystävällisin terveisin", "Huolitsija Oy, ostoreskontra", "", "— In English —", "",
        "Hello,", "",
        f"your invoice {num} ({_eur(ci.get('total_cents'), ci.get('currency') or 'EUR')}) exceeds the rate agreed at booking:",
        *rows_en, "",
        f"Please send a credit note for the difference of {_eur(d.get('claimed_eur_cents'))} referring to invoice {num}, "
        "or explain the additional charge. We will not pay the disputed part until this is resolved.", "",
        "Kind regards", "Huolitsija Oy, accounts payable"])
    return subject, body


async def send_dispute_email(ci_id: str, *, actor: dict | None) -> dict:
    ci = await _get(ci_id)
    d = ci.get("dispute") or {}
    if d.get("sent_at"):
        return d
    to = (ci.get("supplier") or {}).get("contact_email") or ci.get("sender")
    if ci.get("supplier", {}).get("carrier_id") and not to:
        row = await db.carriers.find_one({"id": ci["supplier"]["carrier_id"]}, {"_id": 0, "contact_email": 1, "ap_email": 1,
                                                                              "billing_email": 1})
        to = (row or {}).get("ap_email") or (row or {}).get("billing_email") or (row or {}).get("contact_email")
    if not to:
        await db.carrier_invoices.update_one({"id": ci_id}, {"$set": {"dispute.email_status": "no_address"}})
        await notify_role("finance", bi("Hyvityspyyntöä ei voitu lähettää", "Credit request not sent"),
                          bi(f"Laskun {ci.get('number')} toimittajalle ei ole sähköpostiosoitetta",
                             f"No e-mail address for the supplier of invoice {ci.get('number')}"),
                          link=f"/ops/finance/carrier-invoices/{ci_id}")
        return d
    subject, text = _dispute_text(ci)
    sender = os.environ.get("AP_EMAIL_FROM") or "ostolaskut@huolitsija.test"
    provider = (os.environ.get("AP_EMAIL_PROVIDER") or "core").lower()
    rec = {"id": new_id(), "to": to, "subject": subject, "tag": "carrier_invoice_dispute", "provider": provider,
           "ref": {"type": "carrier_invoice", "id": ci_id}, "status": "queued", "created_at": now_iso(), "text": text[:20000]}
    message_id = None
    from services.demo_world import boundary
    world = await boundary.capture_mail(to=to, subject=subject, text=text, from_addr=sender, in_reply_to=ci.get("message_id"),
                                        channel="accounts_payable", tag="carrier_invoice_dispute",
                                        ref={"type": "carrier_invoice", "id": ci_id}) if provider == "smtp" else None
    if world:
        message_id = world["message_id"]
        rec.update(status="sent", provider="world", provider_id=message_id, world=world["world"])
        await db.email_outbox.insert_one(dict(rec))
    elif provider == "smtp":
        msg = EmailMessage()
        msg["From"] = formataddr(("Huolitsija Oy ostoreskontra", sender))
        msg["To"] = to
        msg["Subject"] = subject
        message_id = make_msgid(domain=sender.split("@")[-1])
        msg["Message-ID"] = message_id
        if ci.get("message_id"):
            msg["In-Reply-To"] = ci["message_id"]
            msg["References"] = ci["message_id"]
        msg.set_content(text)
        host = os.environ.get("AP_SMTP_HOST") or "127.0.0.1"
        port = int(os.environ.get("AP_SMTP_PORT") or 25)
        security = (os.environ.get("AP_SMTP_SECURITY") or ("ssl" if port == 465 else "starttls" if port == 587 else "none")).lower()
        try:
            await asyncio.to_thread(_smtp_send, msg, host, port, security, os.environ.get("AP_SMTP_USER"),
                                    os.environ.get("AP_SMTP_PASSWORD"))
            rec.update(status="sent", provider_id=message_id)
        except Exception as exc:  # noqa: BLE001 - recorded; the card stays with the owner
            log.warning("dispute e-mail to %s failed: %s", to, exc)
            rec.update(status="failed", error=str(exc)[:500])
        await db.email_outbox.insert_one(dict(rec))
    else:
        from core.email import render, send_email
        html, txt = render(subject, text.split("\n"))
        rec = await send_email(to, subject, html, txt, tag="carrier_invoice_dispute", ref={"type": "carrier_invoice", "id": ci_id})
        message_id = rec.get("provider_id")
    sent = rec.get("status") in ("sent", "logged")
    upd = {"dispute.email_status": rec.get("status"), "dispute.to": to, "dispute.outbox_id": rec.get("id"),
           "dispute.provider": rec.get("provider")}
    if sent:
        upd.update({"dispute.sent_at": now_iso(), "dispute.message_id": message_id})
    await db.carrier_invoices.update_one({"id": ci_id}, {"$set": upd})
    await audit(actor, "carrier_invoice_dispute_email", "carrier_invoice", ci_id,
                {"to": to, "status": rec.get("status"), "message_id": message_id, "provider": rec.get("provider")})
    return (await _get(ci_id)).get("dispute") or {}


# --------------------------------------------------------------------------------------------- job + events
@fc.finance_job("finance.carrier_invoice_process", label_fi="Ostolaskun käsittely", label_en="Process carrier invoice")
async def process_job(job: dict):
    ci = await process((job.get("payload") or {})["id"])
    return {"id": ci["id"], "status": ci["status"]}


@on("approval.decided")
async def _on_decided(payload: dict):
    a = payload.get("approval") or {}
    actor = payload.get("actor")
    if a.get("kind") not in ("carrier_invoice_review", "carrier_invoice_dispute") or a.get("ref_type") != "carrier_invoice":
        return
    ci = await _get(a["ref_id"])
    if not ci:
        return
    note = a.get("decision_note")
    if a["kind"] == "carrier_invoice_review":
        if a["status"] == "approved":
            try:
                await approve(ci["id"], actor=actor, note=note)
            except PermissionError as exc:
                await audit(actor, "carrier_invoice_approve_blocked", "carrier_invoice", ci["id"], {"reason": str(exc)})
        elif a["status"] == "rejected":
            await reject(ci["id"], actor=actor, note=note)
        return
    if a["status"] == "approved":  # accept the variance
        try:
            await approve(ci["id"], actor=actor, note=note or "variance accepted")
        except PermissionError as exc:
            await audit(actor, "carrier_invoice_approve_blocked", "carrier_invoice", ci["id"], {"reason": str(exc)})
    elif a["status"] == "rejected":  # keep disputed, make sure the carrier was asked
        await send_dispute_email(ci["id"], actor=actor)


async def file_of(ci_id: str) -> tuple[bytes, str, str]:
    ci = await _get(ci_id)
    if not ci:
        raise LookupError("Carrier invoice not found")
    return await get_file(ci["file_id"])


def today() -> str:
    return date.today().isoformat()
