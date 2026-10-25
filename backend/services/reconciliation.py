"""Payments operations (s2): the durable Stripe webhook, ISO 20022 camt.054 bank notifications matched to invoices by
payment reference, and the daily reconciliation of Stripe, the bank and the invoices.

Stripe webhook: the route verifies the signature (services.payments.verify_webhook), stores the raw event in
`stripe_events` (status received -> processed | failed) and queues job `payments.stripe_event` (key stripe:<event id>),
then answers at once. A failed event stays `failed` and is processed again by the job's retries, a dead-letter retry
or Stripe re-sending it; only a processed, signed event is answered `duplicate`.

Bank: a camt.054 file arrives by upload or signed webhook -> `bank_statements` (idempotent on statement id + file
hash) -> job `payments.camt054_import` writes one `bank_transactions` row per booked entry (unique archive reference)
and matches credit EUR entries to invoices by Finnish reference (viitenumero) or RF. Every match goes through
decide("payments_agent", "match_bank_payment", ...); anything unclear becomes a `payment_match` owner-queue card.

Daily at 06:30 Helsinki `payments.reconcile_daily` pulls open Checkout Sessions from Stripe, books paid sessions that
never reached us, lists discrepancies, stores an `owner_reports` document (kind reconciliation) and e-mails the owner.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import stripe
from lxml import etree
from pymongo.errors import DuplicateKeyError

from core.agent_runtime import agent_bot, decide, notify_owner, policy_reason
from core.alerts import raise_alert, resolve_alert
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import on
from core.jobs import JobFailed, enqueue, job, schedule
from core.lifecycle import on_startup
from core.notify import bi
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import billing, payments

log = logging.getLogger("reconciliation")
AGENT = "payments_agent"
HELSINKI = ZoneInfo("Europe/Helsinki")

register_index("stripe_events", "id", unique=True)
register_index("stripe_events", [("status", 1), ("received_at", -1)])
register_index("bank_statements", "id", unique=True)
register_index("bank_statements", [("statement_id", 1), ("file_hash", 1)], unique=True)
register_index("bank_statements", [("received_at", -1)])
register_index("bank_transactions", "id", unique=True)
register_index("bank_transactions", "archive_ref", unique=True)
register_index("bank_transactions", [("status", 1), ("booking_date", -1)])
register_index("bank_transactions", "statement_id")
register_index("bank_transactions", "invoice_id")
register_index("invoices", "rf_reference")

TX_STATUSES = ("new", "matched", "partial", "unmatched", "ignored")
STATUS_FI = {"void": "mitätöity", "credited": "hyvitetty", "draft": "luonnos", "paid": "maksettu", "open": "avoin",
             "overdue": "erääntynyt", "processing": "käsittelyssä"}


def _eur(cents: int | None, lang: str = "fi") -> str:
    s = f"{(cents or 0) / 100:,.2f}"
    return (s.replace(",", " ").replace(".", ",") + " €") if lang == "fi" else ("€" + s)


# =============================================================================================
# Stripe webhook: verify -> store -> queue
# =============================================================================================
async def receive_stripe_webhook(payload: bytes, signature: str | None) -> dict:
    """HTTP entry point. Raises payments.WebhookRejected (400) exactly where the inline handler did."""
    v = await payments.verify_webhook(payload, signature)
    event = v["event"]
    stored = await payments.store_event(event, v["text"], verified=v["verified"], confirmed=v["confirmed"])
    if not stored["new"] and stored["status"] == "processed" and v["verified"]:
        return {"ok": True, "duplicate": True}
    quick = await payments.triage_event(event)
    if quick is not None:
        await payments.mark_event(stored["id"], "processed", result=quick)
        return quick
    if not stored["id"]:  # an event without an id cannot be stored or queued: process it now, as before
        return await payments.process_event(event, verified=v["verified"], confirmed=v["confirmed"])
    jd = await enqueue_stripe_event(stored["id"])
    out = {"ok": True, "queued": True, "event_id": stored["id"], "job_id": jd["id"]}
    if not stored["new"]:
        out["retry"] = True
    return out


async def enqueue_stripe_event(event_id: str) -> dict:
    jd = await enqueue("payments.stripe_event", {"event_id": event_id}, key=f"stripe:{event_id}",
                       subject=("stripe_event", event_id), source="webhook")
    if jd.get("status") in ("done", "dead", "retry"):
        jd = await _rearm(jd)
    return jd


async def _rearm(jd: dict) -> dict:
    """Run a finished, dead or backing-off job for this event again now (the event is not processed yet)."""
    t = now_iso()
    if jd["status"] in ("done", "dead"):
        await db.raw.jobs.update_one({"id": jd["id"], "status": jd["status"]}, {"$set": {
            "status": "queued", "next_run_at": t, "attempts": 0, "defer_count": 0, "last_error": None,
            "lease_owner": None, "lease_until": None, "finished_at": None, "rearmed_at": t}})
        if jd["status"] == "dead":
            await db.raw.dead_letters.update_many({"job_id": jd["id"], "status": "open"}, {"$set": {
                "status": "retried", "retried_at": t, "retried_by": {"id": "stripe", "name": "Stripe"}}})
    elif jd["status"] == "retry":
        await db.raw.jobs.update_one({"id": jd["id"], "status": "retry"}, {"$set": {"next_run_at": t}})
    return await db.raw.jobs.find_one({"id": jd["id"]}, NO_ID) or jd


@job("payments.stripe_event", max_attempts=8, timeout=120, label_fi="Stripe-tapahtuman käsittely",
     label_en="Process a Stripe event")
async def stripe_event_job(j):
    event_id = (j.get("payload") or {}).get("event_id")
    ev = await db.raw.stripe_events.find_one({"id": event_id}, NO_ID)
    if not ev:
        raise JobFailed(f"Stripe event {event_id} is not stored")
    if ev.get("status") == "processed":
        return {"event_id": event_id, "already_processed": True}
    try:
        event = json.loads(ev.get("payload") or "")
    except ValueError:
        await payments.mark_event(event_id, "failed", error="stored payload is not JSON")
        raise JobFailed(f"Stripe event {event_id}: stored payload is not JSON")
    try:
        result = await payments.process_event(event, verified=bool(ev.get("verified")), confirmed=bool(ev.get("confirmed")))
    except billing.BillingError as exc:
        await payments.mark_event(event_id, "failed", error=str(exc)[:2000])
        if exc.status >= 500:
            raise
        # A business rule (void/draft invoice, unknown invoice): retrying cannot help until a person acts. The event
        # stays `failed`, so a dead-letter retry or Stripe re-sending it processes it again.
        raise JobFailed(f"Stripe event {event_id} ({ev.get('type')}): {exc}")
    except Exception as exc:  # noqa: BLE001 - transient: the job retries with backoff
        await payments.mark_event(event_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    await payments.mark_event(event_id, "processed", result=result)
    return {"event_id": event_id, "type": ev.get("type"), **result}


# =============================================================================================
# ISO 20022 camt.054 (BkToCstmrDbtCdtNtfctn), versions .001.02 - .001.08
# =============================================================================================
_ISO_NS = re.compile(r"urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3})\.001\.(\d{2})")
SUPPORTED_VERSIONS = {f"{n:02d}" for n in range(2, 9)}


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _t(el, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    v = found.text.strip()
    return v or None


def _cents(text: str | None, where: str) -> int:
    try:
        d = Decimal((text or "").strip())
    except InvalidOperation:
        raise ValueError(f"{where}: amount {text!r} is not a number")
    if not d.is_finite() or d < 0 or (d * 100) != (d * 100).to_integral_value():
        raise ValueError(f"{where}: amount {text!r} is not a positive amount in cents")
    return int(d * 100)


def _day(el) -> str | None:
    if el is None:
        return None
    v = _t(el, "Dt") or _t(el, "DtTm")
    return v[:10] if v else None


def _amount(el):
    """(Amt element) of a TxDtls in any version: Amt (v3+), AmtDtls/TxAmt/Amt or AmtDtls/InstdAmt/Amt (v2)."""
    for path in ("Amt", "AmtDtls/TxAmt/Amt", "AmtDtls/InstdAmt/Amt", "AmtDtls/CntrValAmt/Amt"):
        a = el.find(path)
        if a is not None and (a.text or "").strip():
            return a
    return None


def _party_name(el, role: str) -> str | None:
    return (_t(el, f"RltdPties/{role}/Nm") or _t(el, f"RltdPties/{role}/Pty/Nm")
            or _t(el, f"RltdPties/Ultmt{role}/Nm") or _t(el, f"RltdPties/Ultmt{role}/Pty/Nm"))


def _remittance(tx) -> tuple[str | None, str | None, str | None]:
    """(structured creditor reference, its type code, unstructured text)."""
    ref, kind = None, None
    if tx is not None:
        for strd in tx.findall("RmtInf/Strd"):
            r = _t(strd, "CdtrRefInf/Ref")
            if r:
                ref, kind = r, _t(strd, "CdtrRefInf/Tp/CdOrPrtry/Cd") or _t(strd, "CdtrRefInf/Tp/CdOrPrtry/Prtry")
                break
        text = " ".join(u.text.strip() for u in tx.findall("RmtInf/Ustrd") if u.text and u.text.strip()) or None
    else:
        text = None
    return ref, kind, text


def parse_camt054(data: bytes) -> dict:
    """Parse a camt.054 debit/credit notification. Raises ValueError with a plain sentence for anything else."""
    if not data or not data.strip():
        raise ValueError("The file is empty")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_comments=True, remove_pis=True)
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError(f"The file is not valid XML: {exc}")
    if root.getroottree().docinfo.doctype:
        raise ValueError("The file declares a DTD; bank notifications never do, so it is not accepted")
    ns = etree.QName(root).namespace or ""
    m = _ISO_NS.fullmatch(ns) if ns else None
    version = None
    if ns and m:
        if m.group(1) != "camt.054":
            raise ValueError(f"This is an ISO 20022 {m.group(1)} message, not a camt.054 debit/credit notification")
        if m.group(2) not in SUPPORTED_VERSIONS:
            raise ValueError(f"camt.054.001.{m.group(2)} is not supported; send version camt.054.001.02 to .001.08")
        version = f"camt.054.001.{m.group(2)}"
    _strip_ns(root)
    body = root.find("BkToCstmrDbtCdtNtfctn") if root.tag == "Document" else (root if root.tag == "BkToCstmrDbtCdtNtfctn" else None)
    if body is None:
        raise ValueError("The XML is not an ISO 20022 camt.054 notification (BkToCstmrDbtCdtNtfctn is missing)")
    hdr = body.find("GrpHdr")
    msg_id, created = _t(hdr, "MsgId"), _t(hdr, "CreDtTm")
    ntfs = body.findall("Ntfctn")
    if not ntfs:
        raise ValueError("The camt.054 file has no notification (Ntfctn)")
    entries, notifications = [], []
    for n_i, ntf in enumerate(ntfs, 1):
        nid = _t(ntf, "Id")
        iban = _t(ntf, "Acct/Id/IBAN")
        count = 0
        for e_i, ntry in enumerate(ntf.findall("Ntry"), 1):
            where = f"Entry {e_i}"
            amt_el = ntry.find("Amt")
            if amt_el is None:
                raise ValueError(f"{where} has no amount")
            e_amount, e_ccy = _cents(amt_el.text, where), (amt_el.get("Ccy") or "").upper() or None
            e_cd = _t(ntry, "CdtDbtInd")
            status = _t(ntry, "Sts/Cd") or _t(ntry, "Sts/Prtry") or _t(ntry, "Sts")
            booking, value = _day(ntry.find("BookgDt")), _day(ntry.find("ValDt"))
            e_ref = _t(ntry, "AcctSvcrRef")
            reversal = (_t(ntry, "RvslInd") or "").lower() == "true"
            txs = ntry.findall("NtryDtls/TxDtls") or [None]
            for t_i, tx in enumerate(txs, 1):
                a = _amount(tx) if tx is not None else None
                if a is None and len(txs) > 1:
                    raise ValueError(f"{where}, transaction {t_i} has no amount")
                amount = _cents(a.text, where) if a is not None else e_amount
                ccy = ((a.get("Ccy") if a is not None else None) or e_ccy or "").upper() or None
                cd = (_t(tx, "CdtDbtInd") if tx is not None else None) or e_cd
                if cd not in ("CRDT", "DBIT"):
                    raise ValueError(f"{where}: credit/debit indicator {cd!r} is not CRDT or DBIT")
                tx_ref = _t(tx, "Refs/AcctSvcrRef") if tx is not None else None
                archive = tx_ref or (e_ref if len(txs) == 1 else (f"{e_ref}:{t_i}" if e_ref else None))
                archive = archive or f"{nid or msg_id}:{e_i}:{t_i}"
                ref, ref_type, text = _remittance(tx)
                e2e = _t(tx, "Refs/EndToEndId") if tx is not None else None
                entries.append({
                    "notification_id": nid, "iban": iban, "entry_index": e_i, "tx_index": t_i,
                    "amount_cents": amount, "currency": ccy, "credit_debit": "credit" if cd == "CRDT" else "debit",
                    "reversal": reversal, "status": status, "booking_date": booking, "value_date": value,
                    "archive_ref": archive, "entry_ref": e_ref, "end_to_end_id": None if e2e == "NOTPROVIDED" else e2e,
                    "reference": ref, "reference_type": ref_type, "text": text,
                    "debtor": _party_name(tx, "Dbtr") if tx is not None else None,
                    "debtor_iban": _t(tx, "RltdPties/DbtrAcct/Id/IBAN") if tx is not None else None,
                    "creditor": _party_name(tx, "Cdtr") if tx is not None else None,
                })
                count += 1
        notifications.append({"id": nid, "iban": iban, "entries": count, "created_at": _t(ntf, "CreDtTm")})
    statement_id = (ntfs[0].findtext("Id") or "").strip() if len(ntfs) == 1 else None
    statement_id = statement_id or msg_id
    if not statement_id:
        raise ValueError("The camt.054 file has neither a notification id nor a message id")
    return {"version": version, "message_id": msg_id, "statement_id": statement_id,
            "created_at": created or notifications[0]["created_at"], "iban": notifications[0]["iban"],
            "notifications": notifications, "entries": entries}


# =============================================================================================
# References
# =============================================================================================
def _group5(digits: str) -> str:
    groups, s = [], digits
    while s:
        groups.insert(0, s[-5:])
        s = s[:-5]
    return " ".join(groups)


def _fi_check_ok(digits: str) -> bool:
    if len(digits) < 4 or not digits.isdigit():
        return False
    body, check = digits[:-1], int(digits[-1])
    w = [7, 3, 1]
    total = sum(int(d) * w[i % 3] for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _rf_check_ok(rf: str) -> bool:
    if not re.fullmatch(r"RF\d{2}[0-9A-Z]{1,21}", rf):
        return False
    num = "".join(str(int(c, 36)) for c in rf[4:] + rf[:4])
    return int(num) % 97 == 1


def normalise_reference(raw: str | None) -> dict:
    """{'reference': Finnish reference digits without leading zeros | None, 'rf': 'RF..' | None, 'valid': bool}."""
    s = re.sub(r"\s+", "", raw or "").upper()
    if not s:
        return {"reference": None, "rf": None, "valid": False}
    if s.startswith("RF"):
        return {"reference": None, "rf": s, "valid": _rf_check_ok(s)}
    if s.isdigit():
        d = s.lstrip("0")
        return {"reference": d or None, "rf": None, "valid": _fi_check_ok(d)}
    return {"reference": None, "rf": None, "valid": False}


async def find_invoice_by_reference(reference: str | None, rf: str | None) -> dict | None:
    q = None
    if reference:
        q = {"reference": {"$in": [_group5(reference), reference]}}
    elif rf:
        inv = await db.raw.invoices.find_one({"rf_reference": rf}, NO_ID, sort=[("issued_at", -1)])
        if inv:
            return inv
        body = rf[4:].lstrip("0")
        if body.isdigit():
            q = {"reference": {"$in": [_group5(body), body]}}
    if not q:
        return None
    return await db.raw.invoices.find_one(q, NO_ID, sort=[("issued_at", -1)])


# =============================================================================================
# Bank statements
# =============================================================================================
async def store_statement(data: bytes, filename: str, source: str, actor: dict | None) -> dict:
    """Store a camt.054 file and queue its import. Same statement id + same file bytes -> the stored statement."""
    from core.storage import delete_file, put_file

    if source not in ("upload", "webhook"):
        raise ValueError("source must be upload or webhook")
    parsed = parse_camt054(data)
    sha = hashlib.sha256(data).hexdigest()
    existing = await db.raw.bank_statements.find_one({"statement_id": parsed["statement_id"], "file_hash": sha}, NO_ID)
    if existing:
        return {**existing, "duplicate": True}
    name = (filename or "").strip()[:200] or f"camt054-{parsed['statement_id']}.xml"
    file_id = await put_file(data, name, "application/xml", {"kind": "bank_statement", "statement_id": parsed["statement_id"],
                                                             "sha256": sha})
    doc = {
        "id": new_id(), "filename": name, "file_id": file_id, "file_hash": sha, "size": len(data), "source": source,
        "received_at": now_iso(), "received_by": actor_ref(actor), "statement_id": parsed["statement_id"],
        "message_id": parsed["message_id"], "version": parsed["version"], "statement_created_at": parsed["created_at"],
        "iban": parsed["iban"], "entries_count": len(parsed["entries"]), "status": "queued", "matched": 0,
        "unmatched": 0, "ignored": 0, "partial": 0, "error": None, "imported_at": None, "job_id": None,
    }
    try:
        await db.raw.bank_statements.insert_one(dict(doc))
    except DuplicateKeyError:
        await delete_file(file_id)
        existing = await db.raw.bank_statements.find_one({"statement_id": parsed["statement_id"], "file_hash": sha}, NO_ID)
        return {**(existing or doc), "duplicate": True}
    jd = await enqueue("payments.camt054_import", {"statement_id": doc["id"]}, key=f"camt054:{doc['id']}",
                       subject=("bank_statement", doc["id"]), source=source)
    await db.raw.bank_statements.update_one({"id": doc["id"]}, {"$set": {"job_id": jd["id"]}})
    doc["job_id"] = jd["id"]
    await audit(actor, "bank_statement_received", "bank_statement", doc["id"],
                {"statement_id": doc["statement_id"], "entries": doc["entries_count"], "source": source,
                 "iban": doc["iban"]})
    return doc


async def _upsert_tx(st: dict, e: dict) -> dict | None:
    """Insert the bank line once per archive reference. Returns the row when it still needs a decision."""
    norm = normalise_reference(e["reference"])
    booked = (e["status"] or "BOOK").upper() == "BOOK"
    doc = {
        "id": new_id(), "statement_id": st["id"], "camt_statement_id": st["statement_id"],
        "notification_id": e["notification_id"], "account_iban": e["iban"], "archive_ref": e["archive_ref"],
        "entry_ref": e["entry_ref"], "end_to_end_id": e["end_to_end_id"], "amount_cents": e["amount_cents"],
        "currency": e["currency"], "credit_debit": e["credit_debit"], "reversal": e["reversal"],
        "entry_status": e["status"], "booking_date": e["booking_date"], "value_date": e["value_date"],
        "reference": norm["reference"], "rf": norm["rf"], "reference_raw": e["reference"],
        "reference_valid": norm["valid"], "reference_type": e["reference_type"], "debtor": e["debtor"],
        "debtor_iban": e["debtor_iban"], "creditor": e["creditor"], "text": e["text"], "status": "new",
        "invoice_id": None, "decision_id": None, "approval_id": None, "candidate_invoice_ids": [], "match_kind": None,
        "ignore_reason": None, "created_at": now_iso(), "updated_at": now_iso(),
    }
    try:
        await db.raw.bank_transactions.insert_one(dict(doc))
        return doc
    except DuplicateKeyError:
        cur = await db.raw.bank_transactions.find_one({"archive_ref": e["archive_ref"]}, NO_ID)
    if not cur:
        return None
    if cur["status"] == "new":
        return cur  # an earlier import stopped half-way
    if cur["status"] == "ignored" and cur.get("ignore_reason") == "not_booked" and booked:
        await db.raw.bank_transactions.update_one({"id": cur["id"], "status": "ignored"}, {"$set": {
            "status": "new", "entry_status": e["status"], "booking_date": e["booking_date"], "value_date": e["value_date"],
            "statement_id": st["id"], "ignore_reason": None, "updated_at": now_iso()}})
        return await db.raw.bank_transactions.find_one({"id": cur["id"]}, NO_ID)
    return None


async def _candidates(tx: dict, exclude: str | None = None) -> list[str]:
    """Invoices a person would look at first: same amount due, the invoice number in the message, the payer's name."""
    out: list[str] = []
    base = {"kind": "invoice", "status": {"$in": list(billing.OPEN_STATES)}, "demo": {"$ne": True}}
    for num in re.findall(r"HUO-INV-\d{4}-\d{4,}", (tx.get("text") or "").upper()):
        inv = await db.raw.invoices.find_one({"number": num}, {"_id": 0, "id": 1})
        if inv:
            out.append(inv["id"])
    async for inv in db.raw.invoices.find({**base, "$expr": {"$eq": [
            {"$subtract": [{"$subtract": ["$total_cents", {"$ifNull": ["$paid_cents", 0]}]}, {"$ifNull": ["$credited_cents", 0]}]},
            int(tx["amount_cents"])]}}, {"_id": 0, "id": 1}).limit(5):
        out.append(inv["id"])
    if tx.get("debtor"):
        rx = re.compile(f"^{re.escape(tx['debtor'].strip())}$", re.I)
        async for inv in db.raw.invoices.find({**base, "company_name": rx}, {"_id": 0, "id": 1}).sort("due_date", 1).limit(3):
            out.append(inv["id"])
    seen, uniq = set(), []
    for i in out:
        if i not in seen and i != exclude:
            seen.add(i)
            uniq.append(i)
    return uniq[:5]


def _card_texts(tx: dict, inv: dict | None) -> dict:
    amount_fi, amount_en = _eur(tx["amount_cents"], "fi"), _eur(tx["amount_cents"], "en")
    if tx.get("currency") and tx["currency"] != "EUR":
        amount_fi = amount_en = f"{tx['amount_cents'] / 100:.2f} {tx['currency']}"
    who = tx.get("debtor") or "—"
    ref = tx.get("reference_raw") or "—"
    if inv:
        title = {"fi": f"Pankkimaksu laskulle {inv.get('number')}: {amount_fi}",
                 "en": f"Bank payment for invoice {inv.get('number')}: {amount_en}"}
    else:
        title = {"fi": f"Kohdistamaton pankkimaksu {amount_fi} ({who})", "en": f"Unmatched bank payment {amount_en} ({who})"}
    summary = {"fi": f"Maksaja {who}, viite {ref}, arkistointitunnus {tx['archive_ref']}, kirjauspäivä {tx.get('booking_date') or '—'}."
                     + (f" Viesti: {tx['text']}" if tx.get("text") else ""),
               "en": f"Payer {who}, reference {ref}, archive id {tx['archive_ref']}, booked {tx.get('booking_date') or '—'}."
                     + (f" Message: {tx['text']}" if tx.get("text") else "")}
    return {"title": title, "summary": summary}


async def _decide(tx: dict, inv: dict | None, *, match_kind: str | None, confidence: float | None, extra: list[dict],
                  candidates: list[str]):
    texts = _card_texts(tx, inv)
    due = billing._amount_due(inv) if inv else None
    return await decide(
        AGENT, "match_bank_payment", subject=("bank_transaction", tx["id"]), subject_label=tx["archive_ref"],
        company_id=(inv or {}).get("company_id"),
        inputs={"archive_ref": tx["archive_ref"], "amount_cents": tx["amount_cents"], "currency": tx["currency"],
                "reference": tx.get("reference"), "rf": tx.get("rf"), "reference_valid": tx.get("reference_valid"),
                "debtor": tx.get("debtor"), "booking_date": tx.get("booking_date"), "text": tx.get("text"),
                "invoice": {"id": inv["id"], "number": inv.get("number"), "status": inv["status"], "amount_due_cents": due}
                if inv else None},
        decision={"invoice_id": (inv or {}).get("id"), "amount_cents": tx["amount_cents"], "match": match_kind},
        confidence=confidence, extra_reasons=extra, approval_kind="payment_match",
        title=texts["title"]["en"], title_fi=texts["title"]["fi"], summary=texts["summary"]["en"],
        summary_fi=texts["summary"]["fi"], link=f"/ops/billing/{inv['id']}" if inv else "/ops/billing",
        amount_cents=tx["amount_cents"],
        approval_data={"transaction_id": tx["id"], "archive_ref": tx["archive_ref"], "candidate_invoice_ids": candidates,
                       "invoice_id": (inv or {}).get("id"), "amount_cents": tx["amount_cents"], "currency": tx["currency"]})


async def _book(tx: dict, inv: dict, *, actor: dict, decision_id: str | None, approval_id: str | None = None,
                amount_cents: int | None = None) -> dict:
    """record_payment for the bank line (idempotent on bank_transfer:<archive ref>) and mark the row."""
    due_before = billing._amount_due(inv)
    amount = int(amount_cents or tx["amount_cents"])
    paid_at = tx.get("booking_date") or billing.today_fi().isoformat()
    details = {"bank_transaction_id": tx["id"], "bank_statement_id": tx["statement_id"], "payer": tx.get("debtor"),
               "bank_reference": tx.get("reference_raw")}
    if approval_id:
        details["approval_id"] = approval_id
    inv2, applied = await billing.record_payment(inv["id"], amount_cents=amount, method="bank_transfer",
                                                 ref=tx["archive_ref"], paid_at=paid_at, actor=actor, details=details)
    status = "partial" if amount < due_before else "matched"
    await db.raw.bank_transactions.update_one({"id": tx["id"]}, {"$set": {
        "status": status, "invoice_id": inv["id"], "decision_id": decision_id, "approval_id": approval_id or tx.get("approval_id"),
        "booked_amount_cents": amount, "applied": applied, "matched_at": now_iso(), "matched_by": actor_ref(actor),
        "invoice_status_after": inv2.get("status"), "updated_at": now_iso()}})
    return {"status": status, "invoice_id": inv["id"], "applied": applied}


async def match_transaction(tx: dict) -> dict:
    """Decide one bank line: auto-book an exact or partial reference match, everything else to the owner queue."""
    if tx["credit_debit"] != "credit":
        return await _match_debit(tx)
    if (tx.get("entry_status") or "BOOK").upper() != "BOOK":
        return await _ignore(tx, "not_booked")
    extra: list[dict] = []
    inv = await find_invoice_by_reference(tx.get("reference"), tx.get("rf"))
    match_kind, confidence = None, 0.0
    credit_note = bool(inv and inv.get("kind") != "invoice")
    if credit_note:
        extra.append(policy_reason("credit_note", f"Viite kuuluu hyvityslaskulle {inv.get('number')}",
                                   f"The reference belongs to credit note {inv.get('number')}"))
        inv = None
    if tx.get("reversal"):
        extra.append(policy_reason("reversal", "Pankki on palauttanut tämän maksun (peruutus)", "The bank reversed this payment"))
    if (tx.get("currency") or "EUR") != "EUR":
        extra.append(policy_reason("currency", f"Maksun valuutta on {tx['currency']}, ei euro",
                                   f"The payment is in {tx['currency']}, not EUR"))
    if not inv:
        if credit_note:
            pass
        elif not tx.get("reference") and not tx.get("rf"):
            extra.append(policy_reason("no_reference", "Maksussa ei ole viitettä", "The payment has no reference"))
        else:
            extra.append(policy_reason("no_match", f"Viitteellä {tx.get('reference_raw')} ei löytynyt laskua",
                                       f"No invoice has the reference {tx.get('reference_raw')}"))
    else:
        due = billing._amount_due(inv)
        num = inv.get("number")
        if inv["status"] not in billing.OPEN_STATES:
            if inv["status"] == "paid":
                extra.append(policy_reason("already_paid", f"Lasku {num} on jo maksettu", f"Invoice {num} is already paid"))
            else:
                extra.append(policy_reason("invoice_not_open", f"Lasku {num} on tilassa {STATUS_FI.get(inv['status'], inv['status'])}",
                                           f"Invoice {num} is {inv['status']}"))
        elif tx["amount_cents"] > due:
            extra.append(policy_reason("overpaid", f"Maksu {_eur(tx['amount_cents'])} ylittää avoimen summan {_eur(due)}",
                                       f"The payment {_eur(tx['amount_cents'], 'en')} is more than the {_eur(due, 'en')} due"))
        elif tx["amount_cents"] == due:
            match_kind, confidence = "exact", 1.0
        else:
            match_kind, confidence = "partial", 0.9
        if inv.get("demo"):
            extra.append(policy_reason("demo_invoice", "Lasku on esittelyaineistoa", "The invoice is demo data"))
    candidates = [inv["id"]] if inv else await _candidates(tx)
    if inv and not match_kind:
        candidates += [c for c in await _candidates(tx, exclude=inv["id"]) if c not in candidates]
    # No match proposed -> no confidence: the policy reasons alone say why a person decides.
    d = await _decide(tx, inv, match_kind=match_kind, confidence=confidence if match_kind else None, extra=extra,
                      candidates=candidates)
    if d.auto and inv and match_kind:
        try:
            return await _book(tx, inv, actor=agent_bot(AGENT), decision_id=d.id)
        except billing.BillingError as exc:  # the invoice changed between the check and the booking
            d = await _decide(tx, inv, match_kind=None, confidence=None, candidates=candidates, extra=[
                policy_reason("booking_failed", f"Kirjaus epäonnistui: {exc}", f"Booking failed: {exc}")])
    await db.raw.bank_transactions.update_one({"id": tx["id"]}, {"$set": {
        "status": "unmatched", "invoice_id": inv["id"] if inv and match_kind else None, "match_kind": match_kind,
        "decision_id": d.id, "approval_id": (d.approval or {}).get("id"), "candidate_invoice_ids": candidates,
        "reasons": d.reasons, "updated_at": now_iso()}})
    return {"status": "unmatched", "approval_id": (d.approval or {}).get("id"), "candidates": candidates}


async def _match_debit(tx: dict) -> dict:
    """An outgoing payment: finance (services.payables, s13) matches it to the supplier payable it paid when that module
    is installed; otherwise the line is ignored here (receivables only)."""
    try:
        from services import payables  # optional: present once finance (s13) is merged
    except ImportError:
        payables = None
    match = getattr(payables, "match_bank_debit", None)
    if match is None or (tx.get("entry_status") or "BOOK").upper() != "BOOK":
        return await _ignore(tx, "debit")
    payable = await match(tx.get("end_to_end_id"), tx["amount_cents"], tx.get("currency"), tx.get("booking_date"))
    if not payable:
        return await _ignore(tx, "debit")
    await db.raw.bank_transactions.update_one({"id": tx["id"]}, {"$set": {
        "status": "matched", "match_kind": "payable", "payable_id": payable.get("id"), "invoice_id": None,
        "matched_at": now_iso(), "updated_at": now_iso()}})
    return {"status": "matched", "payable_id": payable.get("id")}


async def _ignore(tx: dict, why: str, actor: dict | None = None) -> dict:
    await db.raw.bank_transactions.update_one({"id": tx["id"]}, {"$set": {
        "status": "ignored", "ignore_reason": why, "resolved_by": actor_ref(actor) if actor else None,
        "updated_at": now_iso()}})
    return {"status": "ignored", "ignore_reason": why}


@job("payments.camt054_import", max_attempts=4, timeout=600, label_fi="Pankin maksuaineiston tuonti (camt.054)",
     label_en="Bank notification import (camt.054)")
async def camt054_import_job(j):
    from core.storage import get_file

    sid = (j.get("payload") or {}).get("statement_id")
    st = await db.raw.bank_statements.find_one({"id": sid}, NO_ID)
    if not st:
        raise JobFailed(f"Bank statement {sid} not found")
    data, _, _ = await get_file(st["file_id"])
    try:
        parsed = parse_camt054(data)
    except ValueError as exc:
        await db.raw.bank_statements.update_one({"id": sid}, {"$set": {"status": "failed", "error": str(exc)[:1000]}})
        raise JobFailed(str(exc))
    for e in parsed["entries"]:
        tx = await _upsert_tx(st, e)
        if tx is not None:
            await match_transaction(tx)
    refs = [e["archive_ref"] for e in parsed["entries"]]
    counts = {s: 0 for s in TX_STATUSES}
    async for r in db.raw.bank_transactions.aggregate([{"$match": {"archive_ref": {"$in": refs}}},
                                                       {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[r["_id"]] = r["n"]
    await db.raw.bank_statements.update_one({"id": sid}, {"$set": {
        "status": "imported", "imported_at": now_iso(), "matched": counts["matched"] + counts["partial"],
        "partial": counts["partial"], "unmatched": counts["unmatched"] + counts["new"], "ignored": counts["ignored"],
        "error": None}})
    return {"statement_id": sid, **counts}


@on("approval.decided")
async def _on_payment_match_decided(p: dict):
    a = p["approval"]
    if a.get("kind") != "payment_match" or not (a.get("data") or {}).get("transaction_id"):
        return
    tx = await db.raw.bank_transactions.find_one({"id": a["data"]["transaction_id"]}, NO_ID)
    if not tx or tx["status"] in ("matched", "partial", "ignored"):
        return
    actor = p.get("actor") or None
    if a["status"] == "rejected":
        await _ignore(tx, "owner_rejected", actor)
        await audit(actor, "bank_payment_ignored", "bank_transaction", tx["id"], {"approval_id": a["id"],
                                                                                  "note": a.get("decision_note")})
        return
    if a["status"] != "approved":
        return
    dd = a.get("decision_data") or {}
    cands = (a.get("data") or {}).get("candidate_invoice_ids") or []
    inv_id = dd.get("invoice_id") or (a["data"].get("invoice_id")) or (cands[0] if len(cands) == 1 else None)
    problem = None
    inv = await db.raw.invoices.find_one({"id": inv_id}, NO_ID) if inv_id else None
    amount = dd.get("amount_cents")
    if (tx.get("currency") or "EUR") != "EUR" and not amount:
        problem = policy_reason("eur_amount_needed", "Anna euromäärä, joka kirjataan laskulle",
                                "Give the amount in euros to book on the invoice")
    elif not inv:
        problem = policy_reason("choose_invoice", "Valitse lasku, jolle maksu kirjataan", "Choose the invoice to book the payment to")
    elif inv["status"] in ("void", "draft") or inv.get("kind") != "invoice":
        problem = policy_reason("invoice_not_open", f"Laskulle {inv.get('number') or inv['id']} ei voi kirjata maksua",
                                f"A payment cannot be booked on {inv.get('number') or inv['id']}")
    if not problem:
        try:
            await _book(tx, inv, actor=actor or agent_bot(AGENT), decision_id=a["data"].get("agent_decision_id"),
                        approval_id=a["id"], amount_cents=int(amount) if amount else None)
            return
        except billing.BillingError as exc:
            problem = policy_reason("booking_failed", f"Kirjaus epäonnistui: {exc}", f"Booking failed: {exc}")
    # Approved, but the payment cannot be booked as decided: a fresh card says what is missing.
    d = await _decide(tx, inv if inv and inv.get("kind") == "invoice" else None, match_kind=None, confidence=None,
                      extra=[problem], candidates=cands)
    await db.raw.bank_transactions.update_one({"id": tx["id"]}, {"$set": {
        "status": "unmatched", "decision_id": d.id, "approval_id": (d.approval or {}).get("id"), "reasons": d.reasons,
        "updated_at": now_iso()}})


# =============================================================================================
# Daily reconciliation
# =============================================================================================
def _helsinki_day(value: str | None) -> date | None:
    if not value:
        return None
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    dt = parse_iso(value)
    return dt.astimezone(HELSINKI).date() if dt else None


async def _reconcile_stripe() -> dict:
    if not payments.stripe_enabled():
        return {"checked": False, "skipped": "stripe_not_configured"}
    out = {"checked": True, "mode": payments.stripe_mode(), "synced": [], "sessions_listed": 0, "recovered": [],
           "not_ours": 0, "errors": []}
    since = iso(now() - timedelta(days=30))
    async for tx in db.raw.payment_transactions.find({"status": {"$in": ["initiated", "processing"]}, "created_at": {"$gte": since},
                                                       "livemode": payments.stripe_mode() == "live"},
                                                      NO_ID).sort("created_at", 1).limit(500):
        try:
            r = await payments.sync_session(tx["session_id"])
        except billing.BillingError as exc:
            out["errors"].append({"session_id": tx["session_id"], "error": str(exc)[:300]})
            continue
        out["synced"].append({"session_id": tx["session_id"], "invoice_id": tx["invoice_id"],
                              "transaction_status": r.get("transaction_status"), "payment_status": r.get("payment_status"),
                              "applied_now": bool(r.get("applied_now"))})
    try:
        client = payments._client()
        params: dict = {"created": {"gte": int((now() - timedelta(days=3)).timestamp())}, "limit": 100}
        for _page in range(100):
            page = await client.v1.checkout.sessions.list_async(params)
            data = list(payments._get(page, "data", []) or [])
            for s in data:
                out["sessions_listed"] += 1
                if payments._get(s, "status") != "complete" or payments._get(s, "payment_status") not in ("paid", "no_payment_required"):
                    continue
                meta = payments._get(s, "metadata", {}) or {}
                inv_id = meta.get("invoice_id") if hasattr(meta, "get") else None
                if not inv_id:
                    continue
                inv = await db.raw.invoices.find_one({"id": inv_id}, {"_id": 0, "id": 1, "number": 1, "payments": 1})
                if not inv:
                    out["not_ours"] += 1
                    continue
                pi = payments._get(s, "payment_intent")
                pi_id = pi if isinstance(pi, str) else payments._get(pi, "id")
                keys = {f"card:{pi_id}", f"card:{payments._get(s, 'id')}"}
                if any(p.get("key") in keys for p in inv.get("payments") or []):
                    continue
                r = await payments._apply_session(s, source="reconcile")
                out["recovered"].append({"session_id": payments._get(s, "id"), "invoice_id": inv_id,
                                         "invoice_number": inv.get("number"), "amount_cents": r.get("amount_total"),
                                         "transaction_status": r.get("transaction_status"), "applied_now": r.get("applied_now")})
            if not payments._get(page, "has_more") or not data:
                break
            params["starting_after"] = payments._get(data[-1], "id")
    except stripe.StripeError as exc:
        out["errors"].append({"list": True, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


async def discrepancies() -> list[dict]:
    items: list[dict] = []
    since = iso(now() - timedelta(days=30))
    async for inv in db.raw.invoices.find({"demo": {"$ne": True}, "payments": {"$elemMatch": {"method": "card", "recorded_at": {"$gte": since}}}},
                                          {"_id": 0, "id": 1, "number": 1, "payments": 1}):
        for p in inv.get("payments") or []:
            if p.get("method") != "card" or (p.get("recorded_at") or "") < since:
                continue
            ors = [{"payment_intent": p.get("ref")}, {"session_id": p.get("ref")}]
            if p.get("session_id"):
                ors.append({"session_id": p["session_id"]})
            tx = await db.raw.payment_transactions.find_one({"invoice_id": inv["id"], "$or": ors}, NO_ID)
            if not tx or tx.get("status") != "paid":
                items.append({"kind": "card_payment_without_transaction", "invoice_id": inv["id"], "invoice_number": inv.get("number"),
                              "ref": p.get("ref"), "amount_cents": p.get("amount_cents"),
                              "transaction_status": (tx or {}).get("status"),
                              "fi": f"Korttimaksu {_eur(p.get('amount_cents'))} laskulla {inv.get('number')} ilman vastaavaa Stripe-tapahtumaa",
                              "en": f"Card payment {_eur(p.get('amount_cents'), 'en')} on {inv.get('number')} has no matching Stripe transaction"})
    async for tx in db.raw.payment_transactions.find({"status": "review"}, NO_ID).limit(200):
        items.append({"kind": "stripe_review", "invoice_id": tx.get("invoice_id"), "session_id": tx.get("session_id"),
                      "amount_cents": tx.get("amount_cents"), "detail_en": tx.get("review_reason"),
                      "fi": f"Stripe-maksu tarkistettava: {tx.get('session_id')}", "en": f"Stripe payment to review: {tx.get('session_id')}"})
    old = iso(now() - timedelta(minutes=30))
    async for ev in db.raw.stripe_events.find({"$or": [{"status": "failed"}, {"status": "received", "received_at": {"$lt": old}}]},
                                              {"_id": 0, "id": 1, "type": 1, "status": 1, "error": 1}).limit(200):
        items.append({"kind": "stripe_event_failed", "event_id": ev["id"], "type": ev.get("type"), "error": ev.get("error"),
                      "fi": f"Stripe-tapahtumaa {ev['id']} ({ev.get('type')}) ei ole käsitelty",
                      "en": f"Stripe event {ev['id']} ({ev.get('type')}) is not processed"})
    async for tx in db.raw.bank_transactions.find({"status": {"$in": ["unmatched", "new", "partial"]}}, NO_ID).sort("booking_date", 1).limit(300):
        if tx["status"] == "partial":
            inv = await db.raw.invoices.find_one({"id": tx.get("invoice_id")}, {"_id": 0, "total_cents": 1, "paid_cents": 1,
                                                                                 "credited_cents": 1, "status": 1, "number": 1})
            if not inv or inv.get("status") not in billing.OPEN_STATES or billing._amount_due(inv) == 0:
                continue
            items.append({"kind": "bank_partial", "transaction_id": tx["id"], "invoice_id": tx.get("invoice_id"),
                          "amount_cents": tx["amount_cents"], "still_due_cents": billing._amount_due(inv),
                          "fi": f"Osamaksu {_eur(tx['amount_cents'])} laskulle {inv.get('number')}, avoinna vielä {_eur(billing._amount_due(inv))}",
                          "en": f"Part payment {_eur(tx['amount_cents'], 'en')} on {inv.get('number')}, {_eur(billing._amount_due(inv), 'en')} still due"})
            continue
        items.append({"kind": "bank_unmatched", "transaction_id": tx["id"], "archive_ref": tx["archive_ref"],
                      "amount_cents": tx["amount_cents"], "currency": tx.get("currency"), "approval_id": tx.get("approval_id"),
                      "fi": f"Kohdistamaton pankkimaksu {_eur(tx['amount_cents'])} ({tx.get('debtor') or '—'}, {tx.get('booking_date') or '—'})",
                      "en": f"Unmatched bank payment {_eur(tx['amount_cents'], 'en')} ({tx.get('debtor') or '—'}, {tx.get('booking_date') or '—'})"})
    async for inv in db.raw.invoices.find({"kind": "invoice", "demo": {"$ne": True}, "$expr": {"$gt": [
            {"$ifNull": ["$paid_cents", 0]}, {"$subtract": ["$total_cents", {"$ifNull": ["$credited_cents", 0]}]}]}},
            {"_id": 0, "id": 1, "number": 1, "total_cents": 1, "paid_cents": 1, "credited_cents": 1}).limit(200):
        over = int(inv.get("paid_cents") or 0) - int(inv["total_cents"]) + int(inv.get("credited_cents") or 0)
        # A refund recorded on the invoice's credit note (credit note marked paid) already returned the difference.
        async for cn in db.raw.invoices.find({"kind": "credit_note", "credit_note_for": inv["id"]}, {"_id": 0, "paid_cents": 1}):
            over -= abs(int(cn.get("paid_cents") or 0))
        if over <= 0:
            continue
        items.append({"kind": "overpaid", "invoice_id": inv["id"], "invoice_number": inv.get("number"), "over_cents": over,
                      "fi": f"Lasku {inv.get('number')} on maksettu {_eur(over)} liikaa: palauta erotus tai tee hyvitys",
                      "en": f"Invoice {inv.get('number')} is overpaid by {_eur(over, 'en')}: refund or credit the difference"})
    return items


async def totals(yesterday: date) -> dict:
    out = {"open_cents": 0, "open_count": 0, "overdue_cents": 0, "overdue_count": 0, "received_yesterday": {},
           "received_yesterday_cents": 0, "day": yesterday.isoformat()}
    from core.tenant_visibility import finance_scope
    scope = await finance_scope()  # company-wide receivables never include test tenants (demo flag included)
    async for inv in db.raw.invoices.find({**scope, "kind": "invoice", "status": {"$in": list(billing.OPEN_STATES)}},
                                          {"_id": 0, "status": 1, "total_cents": 1, "paid_cents": 1, "credited_cents": 1}):
        due = billing._amount_due(inv)
        out["open_cents"] += due
        out["open_count"] += 1
        if inv["status"] == "overdue":
            out["overdue_cents"] += due
            out["overdue_count"] += 1
    floor = (yesterday - timedelta(days=1)).isoformat()
    async for inv in db.raw.invoices.find({**scope, "payments.paid_at": {"$gte": floor}},
                                          {"_id": 0, "payments": 1}):
        for p in inv.get("payments") or []:
            if _helsinki_day(p.get("paid_at")) == yesterday:
                m = p.get("method") or "other"
                out["received_yesterday"][m] = out["received_yesterday"].get(m, 0) + int(p.get("amount_cents") or 0)
                out["received_yesterday_cents"] += int(p.get("amount_cents") or 0)
    return out


METHOD_FI = {"card": "kortti", "bank_transfer": "tilisiirto"}
KIND_TEXT = {"card_payment_without_transaction": ("korttimaksua ilman Stripe-tapahtumaa", "card payments without a Stripe transaction"),
             "stripe_review": ("Stripe-maksua tarkistettavana", "Stripe payments to review"),
             "stripe_event_failed": ("käsittelemätöntä Stripe-tapahtumaa", "unprocessed Stripe events"),
             "stripe_error": ("Stripe-virhettä", "Stripe errors"),
             "bank_unmatched": ("kohdistamatonta pankkimaksua", "unmatched bank payments"),
             "bank_partial": ("osamaksua", "part payments"),
             "overpaid": ("liikaa maksettua laskua", "overpaid invoices")}


def summary_lines(day: str, data: dict, lang: str) -> tuple[str, str]:
    fi = lang == "fi"
    t, s, c = data["totals"], data["stripe"], data["counts"]
    n = sum(c.values())
    title = (f"Täsmäytys {day}: " + ("kunnossa" if not n else f"{n} poikkeamaa")) if fi else \
        (f"Reconciliation {day}: " + ("all clear" if not n else f"{n} differences"))
    rec = t["received_yesterday"]
    by = ", ".join(f"{METHOD_FI.get(m, m) if fi else m.replace('_', ' ')} {_eur(v, lang)}" for m, v in sorted(rec.items())) or "—"
    parts = [
        (f"Avoimet saatavat {_eur(t['open_cents'], lang)} ({t['open_count']} laskua), joista erääntynyt "
         f"{_eur(t['overdue_cents'], lang)} ({t['overdue_count']})." if fi else
         f"Open receivables {_eur(t['open_cents'], lang)} ({t['open_count']} invoices), overdue "
         f"{_eur(t['overdue_cents'], lang)} ({t['overdue_count']})."),
        (f"Eilen ({t['day']}) vastaanotettu {_eur(t['received_yesterday_cents'], lang)}: {by}." if fi else
         f"Received yesterday ({t['day']}) {_eur(t['received_yesterday_cents'], lang)}: {by}."),
    ]
    if s.get("checked"):
        parts.append(f"Stripe: {len(s['synced'])} avointa maksua tarkistettu, {s['sessions_listed']} maksusivua käyty läpi, "
                     f"{len(s['recovered'])} puuttunutta maksua kirjattu." if fi else
                     f"Stripe: {len(s['synced'])} open payments checked, {s['sessions_listed']} checkout sessions listed, "
                     f"{len(s['recovered'])} missing payments booked.")
    else:
        parts.append("Stripe-avainta ei ole asetettu, korttimaksuja ei tarkistettu." if fi else
                     "No Stripe key is set, so card payments were not checked.")
    if n:
        parts.append(("Poikkeamat: " if fi else "Differences: ") + "; ".join(
            f"{v} {KIND_TEXT.get(k, (k, k))[0 if fi else 1]}" for k, v in c.items() if v) + ".")
    return title, " ".join(parts)


async def run_reconciliation() -> dict:
    today = billing.today_fi()
    stripe_part = await _reconcile_stripe()
    items = await discrepancies()
    for e in stripe_part.get("errors") or []:
        items.append({"kind": "stripe_error", **e, "fi": f"Stripe-virhe: {e.get('error')}", "en": f"Stripe error: {e.get('error')}"})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    data = {"stripe": stripe_part, "discrepancies": items[:300], "counts": counts,
            "totals": await totals(today - timedelta(days=1))}
    status = "attention" if items else "ok"
    report = {"id": new_id(), "kind": "reconciliation", "created_at": now_iso(), "day": today.isoformat(), "status": status,
              "data": data}
    await db.raw.owner_reports.insert_one(dict(report))
    title_fi, body_fi = summary_lines(report["day"], data, "fi")
    title_en, body_en = summary_lines(report["day"], data, "en")
    await notify_owner(bi(title_fi, title_en), bi(body_fi, body_en), "/ops/autopilot", kind="reconciliation",
                       dedupe_key=f"reconciliation:{report['id']}")
    if items:
        await raise_alert("reconciliation", "warning", bi(f"Täsmäytyksessä {len(items)} poikkeamaa",
                                                          f"Reconciliation found {len(items)} differences"),
                          bi(body_fi, body_en), link="/ops/autopilot", data={"report_id": report["id"], "counts": counts})
    else:
        await resolve_alert("reconciliation", "clean reconciliation")
    return {"report_id": report["id"], "status": status, "discrepancies": len(items), "counts": counts}


@job("payments.reconcile_daily", max_attempts=3, timeout=900, label_fi="Maksujen päivittäinen täsmäytys",
     label_en="Daily payment reconciliation")
async def reconcile_daily_job(j):
    return await run_reconciliation()


schedule("payments.reconcile_0630", job="payments.reconcile_daily", daily_at="06:30",
         label_fi="Maksujen täsmäytys klo 6.30", label_en="Payment reconciliation at 06:30")()



@on_startup(order=95)
async def ensure_initial_reconciliation():
    """A new installation must not wait until tomorrow's 06:30 for its first report.

    Use the durable queue and a stable key so restarts/overlapping deploys cannot
    duplicate the catch-up. Keep the daily schedule and explicit disable intact.
    """
    schedule_row = await db.raw.job_schedules.find_one({"name": "payments.reconcile_0630"}, NO_ID)
    if schedule_row and schedule_row.get("enabled") is False:
        return
    if await db.raw.owner_reports.find_one({"kind": "reconciliation"}, {"_id": 1}):
        return
    await enqueue("payments.reconcile_daily", {}, key="payments:initial-reconciliation", source="startup")
