"""Supplier payables and payment runs (s13 Finance, accounts payable).

Flow: carrier_invoice.approved -> one `payables` row per carrier invoice (status approved) -> daily payment run
(finance.payment_run 10:00 Helsinki, or POST /api/finance/payment-runs) takes every approved payable due on or before
execution date + settings.ap_payment_days_before_due, builds one ISO 20022 pain.001 file (services.sepa, validated
against the official XSD), stores it and asks finance_core.agent_decide("approve_payment_run"). Autopilot approves
when no stop reason applies (an IBAN that differs from the one we last paid the supplier, a run above the daily spend
guard); otherwise the run waits on an owner card (approval kind payment_run).

Approved runs are sent by send():
- BANK_PAYMENT_MODE=file (default): the approved pain.001 is downloaded from GET /api/finance/payment-runs/{id}/file
  and uploaded to the bank's corporate web bank. This upload is the one human step until api mode is configured.
- BANK_PAYMENT_MODE=api: services.bank_api.submit() uploads it to the bank (BANK_API_PROVIDER).

Bank feedback: pain.002 status reports (ingest_pain002) and camt.054 debit notifications (match_bank_debit; s2's
camt.054 importer calls it, and POST /api/finance/bank/camt054-debits does until s2 merges). Paying a foreign-currency
payable books the realised FX difference (services.fx.realised_difference, side "ap") on the payable.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta

from core.approvals import request_approval
from core.audit import audit
from core.config import COMPANY_ADDRESS, COMPANY_NAME
from core.db import db, register_index
from core.events import emit, on
from core.refs import next_ref
from core.settings import get_settings
from core.storage import get_file, put_file
from core.util import NO_ID, new_id, now_iso
from services import fx, sepa
from services.finance_core import AGENT_ACTOR, agent_decide, finance_job, helsinki_now, policy_reason
from services.sepa import PaymentError

log = logging.getLogger("payables")

register_index("payables", "carrier_invoice_id", unique=True, sparse=True)
register_index("payables", "end_to_end_id")
register_index("payables", [("status", 1), ("due_date", 1)])
register_index("payment_runs", "number", unique=True)
register_index("payment_runs", "msg_id")
register_index("payment_status_reports", "msg_id")

PAYABLE_STATUSES = ("approved", "scheduled", "sent", "paid", "rejected", "cancelled")
RUN_STATUSES = ("draft", "pending_approval", "approved", "sent", "accepted", "rejected", "partially_accepted")
OPEN_RUN_STATUSES = ("draft", "pending_approval", "approved", "sent", "partially_accepted")


def payment_mode() -> str:
    """BANK_PAYMENT_MODE: file (default) | api."""
    m = os.environ.get("BANK_PAYMENT_MODE", "file").strip().lower()
    return m if m in ("file", "api") else "file"


# ------------------------------------------------------------------------------------------------ helpers
def _supplier_key(s: dict) -> str:
    for k in ("carrier_id", "business_id", "vat_id"):
        if s.get(k):
            return f"{k}:{str(s[k]).strip().upper()}"
    return "name:" + re.sub(r"\W+", "", str(s.get("name") or "").lower())


def _ref_kind(ref: str | None) -> str | None:
    try:
        return sepa.reference_kind(ref)
    except PaymentError:
        return "invalid"


def _flags_of(ci: dict) -> dict:
    flags = {k: True for k in ("test", "demo") if ci.get(k)}
    if ci.get("company_id"):
        flags["company_id"] = ci["company_id"]
    return flags


async def debtor() -> dict:
    """Our company as the payer: settings seller_iban / seller_bic / seller_business_id, COMPANY_NAME, COMPANY_ADDRESS,
    BANK_SERVICE_CODE (payer service code the bank assigns in the payment-service agreement, optional)."""
    s = await get_settings()
    return {"name": COMPANY_NAME, "iban": sepa.normalise_iban(s.get("seller_iban")),
            "bic": (s.get("seller_bic") or "").strip().upper(), "business_id": s.get("seller_business_id") or "",
            "service_code": os.environ.get("BANK_SERVICE_CODE", "").strip() or None,
            "address": sepa.parse_company_address(COMPANY_ADDRESS)}


def _creditor(p: dict) -> dict:
    s = p.get("supplier") or {}
    return {"name": s.get("name"), "iban": s.get("iban"), "account": s.get("account"), "bic": s.get("bic"),
            "country": s.get("country"), "address": s.get("address") or {}}


def _payment_of(p: dict) -> dict:
    return {"id": p["id"], "end_to_end_id": p.get("end_to_end_id") or p["id"].replace("-", "")[:32],
            "amount_cents": p["amount_cents"], "currency": p["currency"], "creditor": _creditor(p),
            "reference": p.get("reference") if p.get("reference_type") in ("fi", "rf") else None,
            "invoice_number": p.get("invoice_number"), "message": p.get("message")}


TARGET_FIXED = {(1, 1), (5, 1), (12, 25), (12, 26)}


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def is_banking_day(d: date) -> bool:
    """TARGET2 / Finnish bank business day: not Sat/Sun, 1 Jan, Good Friday, Easter Monday, 1 May, 25-26 Dec."""
    if d.weekday() >= 5 or (d.month, d.day) in TARGET_FIXED:
        return False
    e = _easter(d.year)
    return d not in (e - timedelta(days=2), e + timedelta(days=1))


def next_banking_day(d: date) -> date:
    while not is_banking_day(d):
        d += timedelta(days=1)
    return d


# ------------------------------------------------------------------------------------------------ payables
async def create_payable(ci: dict, actor: dict | None = None) -> dict | None:
    """One payable per approved carrier invoice (idempotent per carrier_invoice_id). Returns the payable or None for a
    credit note / zero invoice."""
    existing = await db.payables.find_one({"carrier_invoice_id": ci["id"]}, NO_ID)
    if existing:
        return existing
    total = int(ci.get("amount_due_cents") if ci.get("amount_due_cents") is not None else ci.get("total_cents") or 0)
    if total <= 0:
        log.info("carrier invoice %s total %s: no payable (credit note or zero)", ci.get("id"), total)
        return None
    cur = (ci.get("currency") or "EUR").upper()
    eur_total = (ci.get("eur") or {}).get("total_cents")
    fx_doc = ci.get("fx") or {}
    if total != ci.get("total_cents"):
        eur_total = None
    if eur_total is None:
        if cur == "EUR":
            eur_total = total
        else:
            eur_total, fx_doc = await fx.to_eur(total, cur, (ci.get("invoice_date") or now_iso())[:10])
    sup = dict(ci.get("supplier") or {})
    pay = ci.get("payment") or {}
    iban = sepa.normalise_iban(pay.get("iban") or sup.get("iban")) or None
    bic = (pay.get("bic") or sup.get("bic") or "").strip().upper().replace(" ", "") or None
    supplier = {"carrier_id": sup.get("carrier_id"), "name": sup.get("name"), "business_id": sup.get("business_id"),
                "vat_id": sup.get("vat_id"), "country": (sup.get("country") or (iban or "")[:2] or None),
                "iban": iban, "bic": bic, "address": sup.get("address") or {}}
    if pay.get("account") and not iban:
        supplier["account"] = pay["account"]
    ref = (pay.get("reference") or "").strip() or None
    now = now_iso()
    p = {"id": new_id(), "carrier_invoice_id": ci["id"], "invoice_number": ci.get("number"), "supplier": supplier,
         "supplier_key": _supplier_key(sup), "currency": cur, "amount_cents": total, "eur_cents": int(eur_total),
         "fx": fx_doc, "due_date": (ci.get("due_date") or now)[:10], "reference": ref, "reference_type": _ref_kind(ref),
         "message": pay.get("message"), "status": "approved", "payment_run_id": None, "end_to_end_id": None,
         "e2e_history": [], "paid_at": None, "realised_fx_cents": None, "bank_debit": None, "rejection": None,
         "history": [{"at": now, "status": "approved", "by": (actor or AGENT_ACTOR).get("name")}],
         "created_at": now, "updated_at": now, **_flags_of(ci)}
    try:
        await db.payables.insert_one(dict(p))
    except Exception:  # duplicate carrier_invoice_id: a concurrent sync/event created it first
        return await db.payables.find_one({"carrier_invoice_id": ci["id"]}, NO_ID)
    await db.carrier_invoices.update_one({"id": ci["id"]}, {"$set": {"payable_id": p["id"]}})
    await audit(actor or AGENT_ACTOR, "payable_created", "payable", p["id"],
                {"carrier_invoice_id": ci["id"], "currency": cur, "amount_cents": total, "eur_cents": p["eur_cents"]})
    await emit("payable.approved", {"payable": p})
    return p


@on("carrier_invoice.approved")
async def _on_carrier_invoice_approved(payload: dict):
    ci = payload.get("carrier_invoice") or {}
    if ci.get("id"):
        fresh = await db.carrier_invoices.find_one({"id": ci["id"]}, NO_ID) or ci
        await create_payable(fresh, payload.get("actor"))


async def sync() -> dict:
    """Create payables for approved carrier invoices that have none (an event can be missed)."""
    created = []
    async for ci in db.carrier_invoices.find({"status": "approved"}, NO_ID):
        if await db.payables.count_documents({"carrier_invoice_id": ci["id"]}, limit=1):
            continue
        p = await create_payable(ci)
        if p:
            created.append(p["id"])
    return {"created": len(created), "ids": created}


async def _set_payables(ids: list[str], status: str, extra: dict | None = None, *, by: str = "finance",
                        ci_status: str | None = None):
    if not ids:
        return
    now = now_iso()
    await db.payables.update_many({"id": {"$in": ids}}, {"$set": {"status": status, "updated_at": now, **(extra or {})},
                                                         "$push": {"history": {"at": now, "status": status, "by": by}}})
    if ci_status:
        ci_ids = [p["carrier_invoice_id"] async for p in db.payables.find({"id": {"$in": ids}}, {"_id": 0, "carrier_invoice_id": 1})
                  if p.get("carrier_invoice_id")]
        await db.carrier_invoices.update_many({"id": {"$in": ci_ids}}, {"$set": {"status": ci_status, "updated_at": now}})


# ------------------------------------------------------------------------------------------------ payment runs
async def last_paid_iban(supplier_key: str, exclude_ids: list[str] | None = None) -> str | None:
    p = await db.payables.find_one({"supplier_key": supplier_key, "status": "paid", "id": {"$nin": exclude_ids or []},
                                    "supplier.iban": {"$nin": [None, ""]}}, NO_ID, sort=[("paid_at", -1)])
    return (p or {}).get("supplier", {}).get("iban")


async def due_payables(execution_date: str, payable_ids: list[str] | None = None) -> list[dict]:
    s = await get_settings()
    horizon = (date.fromisoformat(execution_date) + timedelta(days=int(s.get("ap_payment_days_before_due") or 0))).isoformat()
    q: dict = {"status": "approved", "due_date": {"$lte": horizon}}
    if payable_ids is not None:
        q["id"] = {"$in": list(payable_ids)}
    return await db.payables.find(q, NO_ID).sort([("due_date", 1), ("created_at", 1)]).to_list(5000)


async def _payable_problem_card(p: dict, err: PaymentError, run_number: str | None = None):
    await request_approval(
        "payment_run", "payable", p["id"],
        f"Maksua ei voi tehdä: {(p.get('supplier') or {}).get('name')} {p.get('invoice_number') or ''}".strip(),
        summary=f"{err.fi} / {err.en}", reason=err.code, requested_by=AGENT_ACTOR, amount_cents=p.get("eur_cents"),
        data={"purpose": "payable_invalid", "code": err.code, "message_fi": err.fi, "message_en": err.en,
              "title_en": f"Cannot pay {(p.get('supplier') or {}).get('name')} {p.get('invoice_number') or ''}".strip(),
              "run_number": run_number, "payable_id": p["id"]},
        link=f"/ops/finance/payables?id={p['id']}")


async def create_payment_run(execution_date: str | None = None, actor: dict | None = None,
                             payable_ids: list[str] | None = None) -> dict:
    """Build, validate, store and route for approval one payment run. Raises PaymentError (plain sentence) when
    nothing is due, our own account details are missing or no payable can be paid."""
    actor = actor or AGENT_ACTOR
    await sync()
    today = helsinki_now().date()
    exec_d = date.fromisoformat(execution_date[:10]) if execution_date else today
    if exec_d < today:
        raise PaymentError(f"Execution date {exec_d} is in the past.", f"Eräpäivä {exec_d} on menneisyydessä.", code="date")
    exec_d = next_banking_day(exec_d)
    exec_s = exec_d.isoformat()
    deb = await debtor()
    sepa.check_debtor(deb)
    candidates = await due_payables(exec_s, payable_ids)
    if not candidates:
        raise PaymentError(f"No approved payables are due by {exec_s}.", f"Ei hyväksyttyjä maksettavia {exec_s} mennessä.",
                           code="nothing_due")
    valid, skipped = [], []
    for p in candidates:
        try:
            sepa.check_payment(_payment_of(p), deb["iban"])
            valid.append(p)
        except PaymentError as err:
            skipped.append({"payable_id": p["id"], "code": err.code, "message_en": err.en, "message_fi": err.fi})
            await _payable_problem_card(p, err)
    if not valid:
        raise PaymentError(f"None of the {len(candidates)} due payables can be paid: {skipped[0]['message_en']}",
                           f"Yhtäkään {len(candidates)} erääntyvästä maksusta ei voi maksaa: {skipped[0]['message_fi']}",
                           code="no_valid_payables")

    run_id = new_id()
    number = await next_ref("PR", width=4)
    claim = await db.payables.update_many({"id": {"$in": [p["id"] for p in valid]}, "status": "approved"},
                                          {"$set": {"status": "scheduled", "payment_run_id": run_id, "updated_at": now_iso()}})
    items = await db.payables.find({"payment_run_id": run_id, "status": "scheduled"}, NO_ID).sort("due_date", 1).to_list(5000)
    if not items:
        raise PaymentError("Another payment run took these payables first.", "Toinen maksuajo ehti ottaa nämä maksut.",
                           code="conflict")
    log.info("payment run %s claimed %s payables", number, claim.modified_count)
    for n, p in enumerate(items, 1):
        p["end_to_end_id"] = f"{number}-{n:04d}"
        await db.payables.update_one({"id": p["id"]}, {"$set": {"end_to_end_id": p["end_to_end_id"]},
                                                       "$addToSet": {"e2e_history": p["end_to_end_id"]},
                                                       "$push": {"history": {"at": now_iso(), "status": "scheduled",
                                                                             "by": actor.get("name"), "run": number}}})
    msg_id = f"{number}-{run_id.replace('-', '')[:8].upper()}"
    totals: dict[str, int] = {}
    for p in items:
        totals[p["currency"]] = totals.get(p["currency"], 0) + int(p["amount_cents"])
    eur_total = sum(int(p.get("eur_cents") or 0) for p in items)
    run = {"id": run_id, "number": number, "execution_date": exec_s,
           "debtor": {"name": deb["name"], "iban": deb["iban"], "bic": deb["bic"]}, "items": [p["id"] for p in items],
           "count": len(items), "totals": totals, "eur_total_cents": eur_total, "status": "draft", "msg_id": msg_id,
           "file_id": None, "xsd_valid": False, "version": sepa.default_version(), "blocks": [], "skipped": skipped,
           "bank": {"mode": payment_mode(), "response": None, "status_report_id": None, "status_reports": []},
           "approval_id": None, "decision_id": None, "created_at": now_iso(), "created_by": actor.get("name"),
           "updated_at": now_iso(), **({"test": True} if items and all(p.get("test") for p in items) else {})}
    await db.payment_runs.insert_one(dict(run))
    try:
        xml, summary = sepa.build_pain001(msg_id=msg_id, debtor=deb, payments=[_payment_of(p) for p in items],
                                          execution_date=exec_s, version=run["version"])
    except (PaymentError, ValueError) as exc:
        await _set_payables([p["id"] for p in items], "approved", {"payment_run_id": None, "end_to_end_id": None})
        await db.payment_runs.update_one({"id": run_id}, {"$set": {"status": "rejected", "error": str(exc)}})
        raise
    file_id = await put_file(xml, f"{number}-{run['version']}.xml", "application/xml",
                             {"payment_run_id": run_id, "kind": "pain.001"})
    pmt_of = {it["id"]: b["pmt_inf_id"] for b in summary["blocks"] for it in b["items"]}
    for p in items:
        await db.payables.update_one({"id": p["id"]}, {"$set": {"pmt_inf_id": pmt_of.get(p["id"])}})
    ci_ids = [p["carrier_invoice_id"] for p in items if p.get("carrier_invoice_id")]
    await db.carrier_invoices.update_many({"id": {"$in": ci_ids}}, {"$set": {"status": "scheduled", "updated_at": now_iso()}})
    blocks = [{k: v for k, v in b.items() if k != "items"} for b in summary["blocks"]]
    await db.payment_runs.update_one({"id": run_id}, {"$set": {
        "status": "pending_approval", "file_id": file_id, "xsd_valid": True, "blocks": blocks, "ctrl_sum": summary["ctrl_sum"],
        "nb_of_txs": summary["nb_of_txs"], "file_created_at": summary["created_at"], "updated_at": now_iso()}})
    await audit(actor, "payment_run_created", "payment_run", run_id,
                {"number": number, "count": len(items), "totals": totals, "eur_total_cents": eur_total,
                 "execution_date": exec_s, "skipped": len(skipped)})

    # ---- agent decision: Autopilot approves unless a stop reason applies
    reasons, iban_changes = [], []
    for p in items:
        last = await last_paid_iban(p["supplier_key"], exclude_ids=[p["id"]])
        now_iban = (p.get("supplier") or {}).get("iban")
        if last and now_iban and last != now_iban:
            name = (p.get("supplier") or {}).get("name")
            iban_changes.append({"payable_id": p["id"], "supplier": name, "last_paid_iban": last, "iban": now_iban})
            reasons.append(policy_reason(
                "iban_changed", f"{name}: tilinumero {now_iban} poikkeaa aiemmin maksetusta {last}",
                f"{name}: IBAN {now_iban} differs from the IBAN last paid {last}"))
    settings = await get_settings()
    raw = await db.settings.find_one({"key": "app"}, {"_id": 0, "guard_spend_per_day_cents": 1}) or {}
    limit = settings.get("guard_spend_per_day_cents") or raw.get("guard_spend_per_day_cents")
    if limit and eur_total > int(limit):
        reasons.append(policy_reason("spend_limit", f"Maksuajo {eur_total / 100:.2f} € ylittää päivärajan {int(limit) / 100:.2f} €",
                                     f"Run total EUR {eur_total / 100:.2f} exceeds the daily spend guard EUR {int(limit) / 100:.2f}"))
    tot_txt = ", ".join(f"{c} {v / 100:,.2f}" for c, v in sorted(totals.items()))
    d = await agent_decide(
        "approve_payment_run", subject=("payment_run", run_id), subject_label=number,
        inputs={"count": len(items), "totals": totals, "eur_total_cents": eur_total, "execution_date": exec_s,
                "iban_changes": iban_changes, "msg_id": msg_id},
        decision={"approve": True, "execution_date": exec_s}, amount_cents=eur_total, spend_cents=eur_total,
        extra_reasons=reasons, approval_kind="payment_run",
        title=f"Approve payment run {number}: {len(items)} payments, {tot_txt}",
        title_fi=f"Hyväksy maksuajo {number}: {len(items)} maksua, {tot_txt}",
        summary=f"Execution date {exec_s}. pain.001 validated against the official XSD.",
        summary_fi=f"Maksupäivä {exec_s}. pain.001 tarkistettu virallista XSD:tä vasten.",
        link=f"/ops/finance/payment-runs/{run_id}", approval_data={"purpose": "approve_run", "run_number": number})
    await db.payment_runs.update_one({"id": run_id}, {"$set": {
        "decision_id": d["id"], "approval_id": (d.get("approval") or {}).get("id"), "iban_changes": iban_changes,
        "decision": {"auto": d["auto"], "outcome": d["outcome"], "reasons": d["reasons"]}}})
    if d["auto"]:
        await approve_run(run_id, AGENT_ACTOR, auto=True)
    return await get_run(run_id)


async def get_run(run_id: str) -> dict | None:
    return await db.payment_runs.find_one({"id": run_id}, NO_ID)


async def _close_run_cards(run_id: str, status: str, actor: dict | None, note: str | None):
    """A run decided outside its owner card (payment-run screen, Autopilot) closes the card too."""
    await db.approvals.update_many(
        {"kind": "payment_run", "ref_type": "payment_run", "ref_id": run_id, "status": "pending"},
        {"$set": {"status": status, "decided_at": now_iso(), "decision_note": note,
                  "decided_by": {"id": (actor or {}).get("id"), "name": (actor or {}).get("name")}}})


async def approve_run(run_id: str, actor: dict | None, *, auto: bool = False, note: str | None = None) -> dict:
    """Approve only. Bank submission is a separate explicit action, including in API mode."""
    res = await db.payment_runs.update_one(
        {"id": run_id, "status": "pending_approval"},
        {"$set": {"status": "approved", "approved_at": now_iso(), "approved_by": (actor or {}).get("name"),
                  "approved_auto": auto, "approval_note": note, "updated_at": now_iso()}})
    run = await get_run(run_id)
    if not res.modified_count:
        return run
    await _close_run_cards(run_id, "approved", actor, note)
    await audit(actor, "payment_run_approved", "payment_run", run_id, {"number": run["number"], "auto": auto, "note": note})
    return await get_run(run_id)


async def reject_run(run_id: str, actor: dict | None, note: str | None = None, *, allow_approved: bool = False) -> dict:
    """Cancel a run that has not reached the bank; its payables go back to approved for the next run."""
    states = ["draft", "pending_approval"] + (["approved"] if allow_approved else [])
    res = await db.payment_runs.update_one({"id": run_id, "status": {"$in": states}, "bank.reconciliation_required": {"$ne": True}},
                                           {"$set": {"status": "rejected", "rejected_at": now_iso(),
                                                     "rejected_by": (actor or {}).get("name"), "rejection_note": note,
                                                     "updated_at": now_iso()}})
    run = await get_run(run_id)
    if res.modified_count:
        ids = [p["id"] async for p in db.payables.find({"payment_run_id": run_id, "status": "scheduled"}, {"_id": 0, "id": 1})]
        await _set_payables(ids, "approved", {"payment_run_id": None, "end_to_end_id": None, "pmt_inf_id": None},
                            by=(actor or {}).get("name") or "finance", ci_status="approved")
        await _close_run_cards(run_id, "rejected", actor, note)
        await audit(actor, "payment_run_rejected", "payment_run", run_id, {"number": run["number"], "note": note})
    return run


async def send(run: dict, actor: dict | None = None) -> dict:
    """Hand an approved run to the bank.

    BANK_PAYMENT_MODE=file (default): nothing is transmitted. The run stays `approved` and its pain.001 is ready at
    GET /api/finance/payment-runs/{id}/file for upload to the bank's corporate web bank: this upload is the one human
    step until api mode is configured (then confirm with POST .../send {"uploaded": true}, or just ingest the bank's
    pain.002, which marks it sent).
    BANK_PAYMENT_MODE=api: services.bank_api.submit() uploads the file -> run and payables `sent`, event payment_run.sent."""
    run = await get_run(run["id"])  # stale callers must not bypass an authentication hold
    if (run.get("bank") or {}).get("reconciliation_required"):
        raise PaymentError("Bank outcome is unverified; reconcile with the bank before any resend.",
                           "Pankin käsittelytila on varmistamatta. Selvitä tila pankista ennen uudelleenlähetystä.",
                           code="bank_reconciliation_required")
    if run["status"] != "approved":
        return run
    if payment_mode() != "api":
        await db.payment_runs.update_one({"id": run["id"]}, {"$set": {
            "bank.mode": "file", "bank.ready_for_upload_at": now_iso(), "updated_at": now_iso()}})
        return await get_run(run["id"])
    from services import bank_api
    data, _, _ = await get_file(run["file_id"])
    try:
        result = await bank_api.submit(run, data)
    except PaymentError as err:  # bank refused or not configured: the run stays approved, a person decides
        unverified = err.code == "bank_api_response_signature"
        if unverified:
            await db.payment_runs.update_one({"id": run["id"]}, {"$set": {"bank.reconciliation_required": True}})
        await db.payment_runs.update_one({"id": run["id"]}, {"$set": {
            "bank.mode": "api", "bank.error": {"code": err.code, "message_en": err.en, "message_fi": err.fi,
                                               "at": now_iso()}, "updated_at": now_iso()}})
        await audit(actor or AGENT_ACTOR, "payment_run_upload_failed", "payment_run", run["id"],
                    {"number": run["number"], "error": err.en})
        await request_approval(
            "payment_run", "payment_run", run["id"], f"Maksuaineiston lähetys pankkiin epäonnistui: {run['number']}",
            summary=(f"{err.fi} Tarkista maksun tila pankista. Kortin kuittaus ei lähetä tai peru maksua."
                     if unverified else f"{err.fi} / {err.en} Hyväksy = yritä uudelleen, hylkää = maksuajo perutaan."),
            reason=err.code, requested_by=AGENT_ACTOR, amount_cents=run.get("eur_total_cents"),
            data={"purpose": "bank_response_unverified" if unverified else "upload_failed", "code": err.code, "message_en": err.en,
                  "title_en": f"Sending payment run {run['number']} to the bank failed",
                  "summary_en": ("Reconcile with the bank. Acknowledging this card does not resend or cancel."
                                 if unverified else "Approve = try again, reject = cancel the run.")},
            link=f"/ops/finance/payment-runs/{run['id']}")
        raise
    await db.payment_runs.update_one({"id": run["id"]}, {"$set": {"bank.mode": "api", "bank.response": result,
                                                                  "bank.provider": result.get("provider"),
                                                                  "bank.error": None}})
    return await mark_sent(run["id"], actor or AGENT_ACTOR, via="api")


async def mark_sent(run_id: str, actor: dict | None, *, via: str) -> dict:
    res = await db.payment_runs.update_one({"id": run_id, "status": "approved"},
                                           {"$set": {"status": "sent", "sent_at": now_iso(), "sent_via": via,
                                                     "sent_by": (actor or {}).get("name"), "updated_at": now_iso()}})
    run = await get_run(run_id)
    if res.modified_count:
        ids = [p["id"] async for p in db.payables.find({"payment_run_id": run_id, "status": "scheduled"}, {"_id": 0, "id": 1})]
        await _set_payables(ids, "sent", by=(actor or {}).get("name") or "finance")
        await audit(actor, "payment_run_sent", "payment_run", run_id, {"number": run["number"], "via": via})
        await emit("payment_run.sent", {"run": run})
    return run


@on("approval.decided")
async def _on_approval_decided(payload: dict):
    a = payload.get("approval") or {}
    if a.get("kind") != "payment_run":
        return
    actor = payload.get("actor")
    approved = a.get("status") == "approved"
    if a.get("ref_type") == "payment_run":
        if (a.get("data") or {}).get("purpose") == "bank_response_unverified":
            return  # acknowledgement cannot resolve an unknown bank outcome
        if (a.get("data") or {}).get("purpose") == "upload_failed":
            run = await get_run(a["ref_id"])
            if approved and run and run["status"] == "approved":
                try:
                    await send(run, actor)
                except PaymentError:
                    pass  # a new card was opened with the bank's answer
            elif not approved:
                await reject_run(a["ref_id"], actor, note=a.get("decision_note"), allow_approved=True)
        elif approved:
            await approve_run(a["ref_id"], actor, note=a.get("decision_note"))
        else:
            await reject_run(a["ref_id"], actor, note=a.get("decision_note"))
    elif a.get("ref_type") == "payable":
        p = await db.payables.find_one({"id": a["ref_id"]}, NO_ID)
        if not p or p["status"] not in ("rejected", "approved"):
            return
        if approved:  # pay again in the next run (fix the IBAN on the carrier invoice/payable first)
            fix = {k: v for k, v in ((a.get("decision_data") or {}).items()) if k in ("iban", "bic")}
            extra = {"payment_run_id": None, "end_to_end_id": None, "pmt_inf_id": None}
            for k, v in fix.items():
                extra[f"supplier.{k}"] = sepa.normalise_iban(v) if k == "iban" else str(v).strip().upper()
            await _set_payables([p["id"]], "approved", extra, by=(actor or {}).get("name") or "owner", ci_status="approved")
        else:
            await _set_payables([p["id"]], "cancelled", by=(actor or {}).get("name") or "owner")
        await audit(actor, f"payable_{'retry' if approved else 'cancelled'}", "payable", p["id"],
                    {"approval_id": a["id"], "note": a.get("decision_note")})


# ------------------------------------------------------------------------------------------------ pain.002
TX_PAID = {"ACSC", "ACCC"}
TX_REJECTED = {"RJCT", "CANC"}


async def ingest_pain002(xml_bytes: bytes, actor: dict | None = None, *, source: str = "upload") -> dict:
    """Bank payment status report -> payable / run statuses. Validates against the official pain.002 XSD first.
    ACSC = settled (payable paid), RJCT = rejected (payable rejected + owner card with the reason code), every other
    status (ACCP, ACTC, ACSP, ACWC, PDNG, RCVD ...) = accepted for execution (payable sent)."""
    name = sepa.message_name(xml_bytes)
    if name not in sepa.PAIN002_VERSIONS:
        raise PaymentError(f"This is a {name} file, not a pain.002 payment status report.",
                           f"Tiedosto on {name}, ei pain.002-maksun tilaraportti.", code="format")
    errors = sepa.validate(xml_bytes, name)
    if errors:
        raise PaymentError(f"The {name} file does not validate against the official XSD: {errors[0]}",
                           f"{name}-tiedosto ei ole virallisen XSD:n mukainen: {errors[0]}", code="xsd")
    rpt = sepa.parse_pain002(xml_bytes)
    run = await db.payment_runs.find_one({"msg_id": rpt["orgnl_msg_id"]}, NO_ID)
    if not run:
        raise PaymentError(f"No payment run has message id {rpt['orgnl_msg_id']}.",
                           f"Yhdelläkään maksuajolla ei ole sanomatunnusta {rpt['orgnl_msg_id']}.", code="not_found")
    if run["status"] in ("draft", "pending_approval", "rejected") and rpt["group_status"] != "RJCT":
        raise PaymentError(f"Payment run {run['number']} is {run['status']}; it was never approved for sending.",
                           f"Maksuajo {run['number']} on tilassa {run['status']}; sitä ei ole hyväksytty lähetettäväksi.",
                           code="state")
    expected_payables = await db.payables.find({"payment_run_id": run["id"]}, NO_ID).to_list(5000)
    expected_e2e = {p.get("end_to_end_id") for p in expected_payables}
    expected_blocks = {p.get("pmt_inf_id") for p in expected_payables}
    if any(t.get("orgnl_end_to_end_id") not in expected_e2e for t in rpt["transactions"]) or any(
            b.get("orgnl_pmt_inf_id") not in expected_blocks for b in rpt["blocks"]):
        raise PaymentError("The status report contains a payment reference outside this run.",
                           "Tilaraportissa on tämän maksuajon ulkopuolinen maksuviite.", code="reference")
    report_id = new_id()
    file_id = await put_file(xml_bytes, f"{run['number']}-{name}-{report_id[:8]}.xml", "application/xml",
                             {"payment_run_id": run["id"], "kind": name})
    if run["status"] == "approved":
        await mark_sent(run["id"], actor or AGENT_ACTOR, via=f"pain002:{source}")
    by_e2e = {t["orgnl_end_to_end_id"]: t for t in rpt["transactions"] if t.get("orgnl_end_to_end_id")}
    by_block = {b["orgnl_pmt_inf_id"]: b for b in rpt["blocks"]}
    outcome = {"paid": [], "sent": [], "rejected": [], "unchanged": []}
    payables = await db.payables.find({"payment_run_id": run["id"]}, NO_ID).to_list(5000)
    for p in payables:
        t = by_e2e.get(p.get("end_to_end_id"))
        b = by_block.get(p.get("pmt_inf_id"))
        status = (t or {}).get("status") or (b or {}).get("status") or rpt["group_status"]
        reasons = (t or {}).get("reasons") or (b or {}).get("reasons") or rpt["group_reasons"]
        if not status or p["status"] in ("paid", "cancelled"):
            outcome["unchanged"].append(p["id"])
            continue
        if status in TX_REJECTED:
            r0 = (reasons or [{}])[0]
            rej = {"code": r0.get("code"), "text_en": r0.get("text_en"), "text_fi": r0.get("text_fi"), "info": r0.get("info"),
                   "status": status, "report_msg_id": rpt["msg_id"], "at": now_iso()}
            if p["status"] != "rejected":
                await _set_payables([p["id"]], "rejected", {"rejection": rej}, by="bank", ci_status="approved")
                code = rej["code"] or status
                why_en = rej["text_en"] or rej["info"] or "no reason text"
                why_fi = rej["text_fi"] or rej["info"] or "ei syytekstiä"
                await request_approval(
                    "payment_run", "payable", p["id"],
                    f"Pankki hylkäsi maksun: {(p.get('supplier') or {}).get('name')} ({code} {why_fi})",
                    summary=f"{run['number']} / {p.get('end_to_end_id')}: {code} {why_fi} / {why_en}. "
                            f"Hyväksy = maksetaan uudelleen seuraavassa ajossa, hylkää = maksu perutaan.",
                    reason=code, requested_by=AGENT_ACTOR, amount_cents=p.get("eur_cents"),
                    data={"purpose": "payment_rejected", "code": code, "reason_en": why_en, "reason_fi": why_fi,
                          "info": rej["info"], "run_id": run["id"], "run_number": run["number"],
                          "end_to_end_id": p.get("end_to_end_id"),
                          "title_en": f"Bank rejected payment to {(p.get('supplier') or {}).get('name')} ({code} {why_en})",
                          "summary_en": "Approve = pay again in the next run, reject = cancel the payment."},
                    link=f"/ops/finance/payment-runs/{run['id']}")
                await audit(AGENT_ACTOR, "payable_rejected_by_bank", "payable", p["id"], rej)
            outcome["rejected"].append(p["id"])
        elif status in TX_PAID:
            if p["status"] != "paid":
                at = rpt.get("created_at") or now_iso()
                await _set_payables([p["id"]], "paid", {"paid_at": at, "paid_via": "pain002"}, by="bank", ci_status="paid")
                fresh = await db.payables.find_one({"id": p["id"]}, NO_ID)
                await emit("payable.paid", {"payable": fresh})
            outcome["paid"].append(p["id"])
        else:
            if p["status"] in ("scheduled", "approved"):
                await _set_payables([p["id"]], "sent", by="bank")
            outcome["sent"].append(p["id"])
    # run status from the payables' current state
    states = [x["status"] async for x in db.payables.find({"payment_run_id": run["id"]}, {"_id": 0, "status": 1})]
    n_rej = sum(s == "rejected" for s in states)
    n_ok = sum(s in ("sent", "paid") for s in states)
    explicit = {t["status"] for t in rpt["transactions"]} | {b["status"] for b in rpt["blocks"]} | {rpt["group_status"]}
    explicit.discard(None)
    if states and n_rej == len(states):
        new_status = "rejected"
    elif n_rej:
        new_status = "partially_accepted"
    elif n_ok and explicit and explicit <= {"PDNG", "RCVD"}:
        new_status = "sent"
    else:
        new_status = "accepted"
    await db.payment_status_reports.insert_one({
        "id": report_id, "payment_run_id": run["id"], "run_number": run["number"], "msg_id": rpt["msg_id"],
        "version": name, "orgnl_msg_id": rpt["orgnl_msg_id"], "group_status": rpt["group_status"], "file_id": file_id,
        "source": source, "outcome": {k: len(v) for k, v in outcome.items()}, "received_at": now_iso(),
        "transactions": rpt["transactions"][:2000]})
    await db.payment_runs.update_one({"id": run["id"]}, {
        "$set": {"status": new_status, "bank.status_report_id": report_id, "bank.group_status": rpt["group_status"],
                 "updated_at": now_iso()},
        "$push": {"bank.status_reports": {"id": report_id, "msg_id": rpt["msg_id"], "group_status": rpt["group_status"],
                                          "at": now_iso()}}})
    await audit(actor or AGENT_ACTOR, "payment_status_report_ingested", "payment_run", run["id"],
                {"msg_id": rpt["msg_id"], "group_status": rpt["group_status"], "run_status": new_status,
                 **{k: len(v) for k, v in outcome.items()}})
    return {"report_id": report_id, "run": await get_run(run["id"]), "group_status": rpt["group_status"],
            "outcome": outcome}


# ------------------------------------------------------------------------------------------------ bank debits
async def match_bank_debit(end_to_end_id: str, amount_cents: int, currency: str, booked_at: str,
                           eur_amount_cents: int | None = None) -> dict | None:
    """A booked debit on our account (camt.054 DBIT entry) for one of our payments. Returns the paid payable, or None
    when the EndToEndId is not ours or the amount does not match.

    Realised FX for a non-EUR payable = fx.realised_difference(booked eur_cents, settled EUR, side="ap"), where settled
    EUR is the EUR amount the bank debited (eur_amount_cents) when it reports it, else the instructed amount at the
    ECB reference rate of the booking date."""
    if not end_to_end_id:
        return None
    p = await db.payables.find_one({"$or": [{"end_to_end_id": end_to_end_id}, {"e2e_history": end_to_end_id}]}, NO_ID)
    if not p:
        return None
    if p.get("bank_debit"):
        return p
    cur = (currency or "").upper()
    amount_cents = int(amount_cents)
    booked = str(booked_at or now_iso())
    if cur == p["currency"]:
        if amount_cents != int(p["amount_cents"]):
            await audit(AGENT_ACTOR, "bank_debit_amount_mismatch", "payable", p["id"],
                        {"end_to_end_id": end_to_end_id, "debited": amount_cents, "currency": cur,
                         "expected": p["amount_cents"]})
            return None
    elif cur == "EUR" and p["currency"] != "EUR":
        eur_amount_cents = amount_cents if eur_amount_cents is None else eur_amount_cents
    else:
        await audit(AGENT_ACTOR, "bank_debit_currency_mismatch", "payable", p["id"],
                    {"end_to_end_id": end_to_end_id, "currency": cur, "expected": p["currency"]})
        return None
    settled_fx = None
    if p["currency"] == "EUR":
        settled_eur, realised = amount_cents, None
    else:
        if eur_amount_cents is not None:
            settled_eur = int(eur_amount_cents)
        else:
            settled_eur, settled_fx = await fx.to_eur(int(p["amount_cents"]), p["currency"], booked[:10])
        realised = fx.realised_difference(int(p["eur_cents"]), settled_eur, side="ap")
    paid_at = booked if len(booked) > 10 else f"{booked[:10]}T00:00:00Z"
    debit = {"end_to_end_id": end_to_end_id, "amount_cents": amount_cents, "currency": cur,
             "eur_amount_cents": eur_amount_cents, "settled_eur_cents": settled_eur, "fx": settled_fx,
             "booked_at": booked, "matched_at": now_iso()}
    res = await db.payables.update_one(
        {"id": p["id"], "bank_debit": None},
        {"$set": {"status": "paid", "paid_at": paid_at, "paid_via": "camt054", "bank_debit": debit,
                  "realised_fx_cents": realised, "settled_eur_cents": settled_eur, "updated_at": now_iso()},
         "$push": {"history": {"at": now_iso(), "status": "paid", "by": "bank"}}})
    fresh = await db.payables.find_one({"id": p["id"]}, NO_ID)
    if not res.modified_count:
        return fresh
    if p.get("carrier_invoice_id"):
        await db.carrier_invoices.update_one({"id": p["carrier_invoice_id"]},
                                             {"$set": {"status": "paid", "paid_at": paid_at, "updated_at": now_iso()}})
    await audit(AGENT_ACTOR, "payable_paid", "payable", p["id"], {"end_to_end_id": end_to_end_id, **debit,
                                                                   "realised_fx_cents": realised})
    if p.get("payment_run_id"):
        states = [x["status"] async for x in db.payables.find({"payment_run_id": p["payment_run_id"]}, {"_id": 0, "status": 1})]
        if states and all(s == "paid" for s in states):
            await db.payment_runs.update_one({"id": p["payment_run_id"], "status": {"$in": ["approved", "sent"]}},
                                             {"$set": {"status": "accepted", "updated_at": now_iso()}})
    await emit("payable.paid", {"payable": fresh})
    return fresh


async def ingest_camt054_debits(xml_bytes: bytes, actor: dict | None = None) -> dict:
    """Interim camt.054 reader for supplier payments until s2's importer calls match_bank_debit itself.

    Only DBIT entries are handled here (EndToEndId in NtryDtls/TxDtls/Refs). Incoming CRDT entries are s2's
    reconciliation of customer receipts and are ignored."""
    name = sepa.message_name(xml_bytes)
    if name not in sepa.CAMT054_VERSIONS:
        raise PaymentError(f"This is a {name} file, not a camt.054 debit/credit notification "
                           f"({' or '.join(sepa.CAMT054_VERSIONS)}).",
                           f"Tiedosto on {name}, ei camt.054-tapahtumailmoitus.", code="format")
    errors = sepa.validate(xml_bytes, name)
    if errors:
        raise PaymentError(f"The {name} file does not validate against the official XSD: {errors[0]}",
                           f"{name}-tiedosto ei ole virallisen XSD:n mukainen: {errors[0]}", code="xsd")
    doc = sepa.parse_camt054(xml_bytes)
    results, ignored_credits = [], 0
    for e in doc["entries"]:
        if e["credit_debit"] != "DBIT":
            ignored_credits += 1
            continue
        if e.get("status") and e["status"] not in ("BOOK",):
            continue
        txs = e["transactions"] or [{}]
        for tx in txs:
            e2e = tx.get("end_to_end_id")
            if not e2e or e2e == "NOTPROVIDED":
                results.append({"end_to_end_id": e2e, "matched": False, "reason": "no EndToEndId"})
                continue
            eur_amt = None
            if tx.get("instructed_cents") is not None and tx.get("instructed_currency") != (tx.get("currency") or e["currency"]):
                amount, cur = tx["instructed_cents"], tx["instructed_currency"]
                tx_cur = tx.get("currency") or e["currency"]
                tx_amt = tx.get("amount_cents") if tx.get("amount_cents") is not None else (e["amount_cents"] if len(txs) == 1 else None)
                eur_amt = tx_amt if tx_cur == "EUR" else None
            elif tx.get("amount_cents") is not None:
                amount, cur = tx["amount_cents"], tx["currency"]
            else:
                amount, cur = e["amount_cents"], e["currency"]
            p = await match_bank_debit(e2e, amount, cur, e.get("booked_at") or now_iso()[:10], eur_amount_cents=eur_amt)
            results.append({"end_to_end_id": e2e, "matched": bool(p), "payable_id": (p or {}).get("id"),
                            "status": (p or {}).get("status"), "realised_fx_cents": (p or {}).get("realised_fx_cents")})
    await audit(actor or AGENT_ACTOR, "camt054_debits_ingested", "bank", doc.get("msg_id"),
                {"version": name, "matched": sum(r["matched"] for r in results), "unmatched": sum(not r["matched"] for r in results),
                 "ignored_credits": ignored_credits})
    return {"version": name, "msg_id": doc.get("msg_id"), "results": results, "ignored_credit_entries": ignored_credits,
            "matched": sum(r["matched"] for r in results)}


# ------------------------------------------------------------------------------------------------ jobs
@finance_job("finance.payment_run", daily_at="10:00", label_fi="Maksuajo", label_en="Payment run")
async def payment_run_job(job: dict):
    """Daily 10:00 Helsinki: one payment run when something is due (nothing due = nothing happens)."""
    await sync()
    exec_d = next_banking_day(helsinki_now().date()).isoformat()
    if not await due_payables(exec_d):
        return {"skipped": "nothing due", "execution_date": exec_d}
    try:
        run = await create_payment_run(exec_d, AGENT_ACTOR)
    except PaymentError as err:
        return {"skipped": err.code, "message": err.en}
    return {"run_id": run["id"], "number": run["number"], "status": run["status"], "count": run["count"]}


@finance_job("finance.bank_status_poll", every_seconds=600 if payment_mode() == "api" else None,
             label_fi="Pankin tilaraportit", label_en="Bank status reports")
async def bank_status_poll_job(job: dict):
    """Every 600 s in api mode: download new pain.002 / camt.054 files from the bank and ingest them."""
    if payment_mode() != "api":
        return {"skipped": "BANK_PAYMENT_MODE is not api"}
    from services import bank_api
    out = {"pain002": 0, "camt054": 0, "errors": []}
    for kind, data in await bank_api.fetch_reports():
        try:
            if kind.startswith("pain.002"):
                await ingest_pain002(data, AGENT_ACTOR, source="api")
                out["pain002"] += 1
            elif kind.startswith("camt.054"):
                await ingest_camt054_debits(data, AGENT_ACTOR)
                out["camt054"] += 1
        except PaymentError as err:
            out["errors"].append(err.en)
    return out
