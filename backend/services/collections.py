"""Collections (perintä) — the stage after dunning: final demand, credit hold, payment plan, hand-over to a collection
agency, write-off. One `collections_cases` row per invoice.

Entry (daily `finance.collections_sweep`, 08:00 Helsinki, and `invoice.dunning_exhausted`):
* s2 dunning present: `invoice.dunning.stage == "collection"` (the owner already approved the hand-over in s2's
  queue) or `invoice.dunning.final_sent_at` set.
* main (no `dunning` object yet): invoice overdue >= `collections_after_days_overdue` days and at least one payment
  reminder recorded (`invoice.emails[].kind == "reminder"`, written by billing.mark_overdue).

Claim on the case (Finnish B2B law):
* interest_cents = amount due x late interest % (invoice.late_interest_pct, else settings) x days past due / 365,
  Decimal, half-up — Korkolaki 633/1982 4 a § (reference rate + 8 pp between businesses); same formula as s2's
  dunning.late_interest_cents.
* fee_cents = settings `collections_fee_cents` (EUR 40, Korkolaki 10 e §: fixed compensation for recovery costs),
  or 0 when s2's dunning already invoiced it (`invoice.dunning.compensation_invoice_id`) — never charged twice.

Agent decisions (finance_core.agent_decide, kill switch and audit included):
* send_to_collections -> auto under Autopilot: final demand (maksuvaatimus) e-mail in the customer's language with
  amount, interest, fee, new due date (+14 d), reference and consequences; credit hold on the customer.
* handover_to_agency after `collections_agency_after_days` without payment: COLLECTIONS_AGENCY=file (default)
  writes the agency hand-over CSV onto the case; =none records the stage only (hand over by hand);
  =api is a STUB (no agency API is contracted yet): it records `handover.api_status = "not_implemented"` and still
  produces the CSV for a person to send.
* write_off is always a judgment: owner queue, never automatic.
`lien_possible` flags shipments of the debtor still in our custody (NSAB 2015 § 14 retention right). Flag only.
"""
from __future__ import annotations

import csv
import inspect
import io
import logging
import os
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.approvals import cancel_approvals
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import emit, on
from core.util import NO_ID, new_id, now_iso
from services import billing, fx
from services.finance_core import AGENT_ACTOR, agent_decide, finance_job, get_settings, policy_reason

log = logging.getLogger("finance.collections")

register_index("collections_cases", "id", unique=True)
register_index("collections_cases", "invoice_id", unique=True)
register_index("collections_cases", [("stage", 1), ("company_id", 1)])

STAGES = ("final_demand", "payment_plan", "agency", "legal", "paid", "written_off")
OPEN_STAGES = ("final_demand", "payment_plan", "agency", "legal")
ACTIONS = ("send_demand", "payment_plan", "agency", "write_off", "close")
CUSTODY_DONE = ("delivered", "cancelled", "closed", "completed")
DEMAND_DAYS = 14
APPROVAL_KIND = "collections_action"


class CollectionsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def agency_mode() -> str:
    return (os.environ.get("COLLECTIONS_AGENCY") or "file").strip().lower()


# ------------------------------------------------------------------------------------------------ math
def late_interest_cents(principal_cents: int, pct: float, days: int) -> int:
    """Korkolaki 4 a §: principal x pct/100 x days/365, half-up to a cent."""
    if principal_cents <= 0 or days <= 0 or not pct:
        return 0
    v = Decimal(int(principal_cents)) * Decimal(str(pct)) / Decimal(100) * Decimal(int(days)) / Decimal(365)
    return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _days_past_due(inv: dict, today: date | None = None) -> int:
    due = billing.to_date(inv.get("due_date"))
    return max(0, ((today or billing.today_fi()) - due).days) if due else 0


def _reminder_recorded(inv: dict) -> bool:
    return any((e.get("kind") or "") == "reminder" or (e.get("kind") or "").startswith("dunning_")
               for e in inv.get("emails") or [])


def entry_reason(inv: dict, settings: dict, today: date | None = None) -> str | None:
    """Why this invoice enters collections now, or None."""
    from services.invoice_disputes import is_open
    if is_open(inv):
        return None
    if inv.get("kind") != "invoice" or inv.get("status") not in ("overdue", "open") or billing._amount_due(inv) <= 0:
        return None
    dun = inv.get("dunning")
    if isinstance(dun, dict):
        if dun.get("stage") == "collection" or inv.get("collection"):
            return "dunning_collection"
        if dun.get("final_sent_at"):
            return "dunning_final_sent"
        if dun.get("exhausted_at"):
            return "dunning_exhausted"
        return None  # s2 is still dunning this invoice
    if inv.get("status") != "overdue":
        return None
    if _days_past_due(inv, today) < int(settings.get("collections_after_days_overdue") or 21):
        return None
    return "overdue_after_reminder" if _reminder_recorded(inv) else None


# ------------------------------------------------------------------------------------------------ credit hold (s7)
async def set_credit_hold(company_id: str, on_: bool, reason: str, actor: dict | None) -> dict:
    """s7's services.credit.set_hold when present; otherwise the same s7 fields + `credit.hold` event. Releasing only
    clears holds this module set, and only when the customer has no other open collections case."""
    try:
        from services import credit as s7  # type: ignore
        set_hold = getattr(s7, "set_hold", None)
    except ImportError:
        set_hold = None
    if not on_:
        if await db.collections_cases.find_one({"company_id": company_id, "stage": {"$in": list(OPEN_STAGES)}}, {"_id": 1}):
            return {"released": False, "why": "other_open_case"}
        c = await db.companies.find_one({"id": company_id}, {"_id": 0, "credit_hold": 1, "credit_hold_reason": 1, "credit_hold_source": 1}) or {}
        if not c.get("credit_hold") or c.get("credit_hold_source") != "collections":
            return {"released": False, "why": "not_held_by_collections"}
    if set_hold:
        kwargs = {"source": "collections"} if "source" in inspect.signature(set_hold).parameters else {}
        before = await db.companies.find_one({"id": company_id}, NO_ID) or {}
        position = await set_hold(company_id, on_, reason, actor, **kwargs)
        if on_ and not before.get("credit_hold"):
            # s7's reason is display text. Ownership is a separate field, never a nested write into that string.
            await db.companies.update_one({"id": company_id}, {"$set": {"credit_hold_source": "collections"}})
        elif not on_:
            await db.companies.update_one({"id": company_id}, {"$unset": {"credit_hold_source": ""}})
        return {"via": "services.credit.set_hold", "hold": on_, "position": position}
    if on_:
        await db.companies.update_one({"id": company_id}, {"$set": {
            "credit_hold": True, "credit_status": "hold",
            "credit_hold_reason": reason, "credit_hold_source": "collections"}})
        await emit("credit.hold", {"company_id": company_id, "position": None, "reason": reason})
    else:
        await db.companies.update_one({"id": company_id}, {"$set": {"credit_hold": False, "credit_status": "ok",
                                                                    "credit_hold_released_at": now_iso()},
                                                           "$unset": {"credit_hold_reason": "", "credit_hold_source": ""}})
    await audit(actor, "credit_hold_set" if on_ else "credit_hold_released", "company", company_id,
                {"reason": reason, "source": "collections"}, company_id=company_id)
    return {"via": "company_fields", "hold": on_}


# ------------------------------------------------------------------------------------------------ open a case
async def _lien_shipments(company_id: str) -> list[dict]:
    rows = await db.shipments.find({"company_id": company_id, "status": {"$nin": list(CUSTODY_DONE)}},
                                   {"_id": 0, "id": 1, "reference": 1, "status": 1}).to_list(50)
    return rows


def _case_eur(case: dict, rate: float | None) -> dict:
    if (case.get("currency") or "EUR") == "EUR" or not rate:
        return {"amount_due_cents": case["amount_due_cents"], "interest_cents": case["interest_cents"],
                "fee_cents": case["fee_cents"], "total_cents": case["amount_due_cents"] + case["interest_cents"] + case["fee_cents"]}
    a, i = fx.convert(case["amount_due_cents"], rate), fx.convert(case["interest_cents"], rate)
    return {"amount_due_cents": a, "interest_cents": i, "fee_cents": case["fee_cents"], "total_cents": a + i + case["fee_cents"]}


async def open_case(inv: dict, reason: str, *, actor: dict | None = None) -> tuple[dict, bool]:
    """Create the collections case for an invoice (idempotent). Returns (case, created_now)."""
    existing = await db.collections_cases.find_one({"invoice_id": inv["id"]}, NO_ID)
    if existing:
        return existing, False
    settings = await get_settings()
    today = billing.today_fi()
    principal = billing._amount_due(inv)
    days = _days_past_due(inv, today)
    pct = float(inv.get("late_interest_pct") or settings.get("late_interest_pct") or 0)
    dun = inv.get("dunning") if isinstance(inv.get("dunning"), dict) else {}
    comp_id = dun.get("compensation_invoice_id")
    fee = 0 if comp_id else int(settings.get("collections_fee_cents") or 0)
    cur = (inv.get("currency") or "EUR").upper()
    rate = None
    if cur != "EUR":
        try:
            rate = (await fx.eur_rate(cur, today))["rate"]
        except fx.FxUnavailable:
            rate = (inv.get("fx") or {}).get("rate")
    interest = late_interest_cents(principal, pct, days)
    lien = await _lien_shipments(inv["company_id"])
    case = {
        "id": new_id(), "invoice_id": inv["id"], "invoice_number": inv.get("number"), "company_id": inv["company_id"],
        "company_name": inv.get("company_name"), "currency": cur, "stage": "final_demand",
        "amount_due_cents": principal, "interest_cents": interest, "late_interest_pct": pct, "days_past_due": days,
        "interest_until": today.isoformat(), "fee_cents": fee, "fee_currency": "EUR",
        "fee_basis": ("Korkolaki 10 e § (EUR 40)" if fee else
                      f"Vakiokorvaus laskutettu jo / compensation already invoiced ({comp_id})" if comp_id else None),
        "compensation_invoice_id": comp_id, "entry": reason, "opened_at": now_iso(),
        "next_action_at": today.isoformat(), "actions": [], "credit_hold": False,
        "lien_possible": bool(lien), "lien_shipments": [{"id": s["id"], "reference": s.get("reference"),
                                                         "status": s.get("status")} for s in lien],
        "demand": None, "handover": None, "payment_plan": None, "pending_action": None,
        "invoice_due_date": inv.get("due_date"), "invoice_issue_date": inv.get("issue_date"),
        "reference": inv.get("reference"), "created_at": now_iso(), "updated_at": now_iso(),
        "test": bool(inv.get("test")) or None, "demo": bool(inv.get("demo")) or None,
    }
    case["eur"] = _case_eur(case, rate)
    case["actions"].append({"at": now_iso(), "action": "opened", "by": actor_ref(actor or AGENT_ACTOR),
                            "note": f"{reason}; {days} d past due"})
    try:
        await db.collections_cases.insert_one(dict(case))
    except Exception:  # duplicate invoice_id: another sweep opened it just now
        return await db.collections_cases.find_one({"invoice_id": inv["id"]}, NO_ID), False
    await db.invoices.update_one({"id": inv["id"]}, {"$set": {"collections_case_id": case["id"]}})
    await audit(actor or AGENT_ACTOR, "collections_case_opened", "invoice", inv["id"],
                {"case_id": case["id"], "reason": reason, "amount_due_cents": principal, "interest_cents": interest,
                 "fee_cents": fee, "lien_possible": case["lien_possible"]}, company_id=inv["company_id"])
    await emit("collections.opened", {"case": case})
    return case, True


async def decide_send(case: dict, inv: dict) -> dict:
    """agent_decide("send_to_collections"): auto -> final demand + credit hold now; else owner card."""
    num = inv.get("number") or inv["id"]
    principal = case["amount_due_cents"]
    amount = billing._fmt_eur(principal, "fi", case["currency"])
    d = await agent_decide(
        "send_to_collections", subject=("invoice", inv["id"]), subject_label=num,
        inputs={"invoice_number": num, "days_past_due": case["days_past_due"], "amount_due_cents": principal,
                "currency": case["currency"], "entry": case["entry"], "lien_possible": case["lien_possible"]},
        decision={"stage": "final_demand", "interest_cents": case["interest_cents"], "fee_cents": case["fee_cents"],
                  "credit_hold": True},
        confidence=1.0, amount_cents=case["eur"]["amount_due_cents"], company_id=case["company_id"],
        approval_kind=APPROVAL_KIND,
        title=f"Final demand and credit hold: {num}", title_fi=f"Maksuvaatimus ja luottokielto: {num}",
        summary=f"{case.get('company_name')}: {principal / 100:.2f} {case['currency']} unpaid {case['days_past_due']} days "
                f"after the due date. Send the final demand (interest + fee) and put the customer on credit hold.",
        summary_fi=f"{case.get('company_name')}: {amount} maksamatta {case['days_past_due']} päivää eräpäivän jälkeen. "
                   f"Lähetä maksuvaatimus (viivästyskorko + perintäkulut) ja aseta asiakas luottokieltoon.",
        link=f"/ops/finance/collections?case={case['id']}",
        approval_data={"case_id": case["id"], "action": "send_demand", "invoice_id": inv["id"]})
    if d["auto"]:
        return {"auto": True, "decision_id": d["id"], **await send_demand(case["id"], actor=AGENT_ACTOR, decision_id=d["id"])}
    await _mark_pending(case["id"], "send_demand", d)
    return {"auto": False, "decision_id": d["id"], "approval_id": (d.get("approval") or {}).get("id"),
            "reasons": d["reasons"]}


async def _mark_pending(case_id: str, action: str, d: dict):
    approval_id = (d.get("approval") or {}).get("id")
    await db.collections_cases.update_one({"id": case_id}, {
        "$set": {"pending_action": {"action": action, "approval_id": approval_id, "decision_id": d["id"],
                                    "at": now_iso(), "reasons": d.get("reasons")}, "updated_at": now_iso()},
        "$push": {"actions": {"at": now_iso(), "action": f"{action}_queued", "by": actor_ref(AGENT_ACTOR),
                              "note": "; ".join(r.get("en", "") for r in d.get("reasons") or []) or None}}})


# ------------------------------------------------------------------------------------------------ final demand
def _demand_texts(case: dict, inv: dict, new_due: date, lang: str) -> tuple[str, list[str]]:
    fi = lang == "fi"
    cur = case["currency"]
    m = lambda c, k=cur: billing._fmt_eur(c, lang, k)  # noqa: E731
    num = inv.get("number") or ""
    due = billing._fmt_date(inv.get("due_date"), lang)
    nd = billing._fmt_date(new_due.isoformat(), lang)
    pct = f"{case['late_interest_pct']:g}"
    from core import config

    seller = inv.get("seller") or {}
    iban, bic = seller.get("iban") or config.COMPANY_IBAN, seller.get("bic") or config.COMPANY_BIC
    total_same = case["amount_due_cents"] + case["interest_cents"] + (case["fee_cents"] if cur == "EUR" else 0)
    if fi:
        subject = f"Maksuvaatimus: lasku {num}"
        lines = [f"Laskun {num} eräpäivä oli {due}. Muistutuksista huolimatta maksua ei ole kirjattu "
                 f"({case['days_past_due']} päivää myöhässä).",
                 f"Pääoma {m(case['amount_due_cents'])}.",
                 f"Viivästyskorko korkolain 4 a §:n mukaan {pct.replace('.', ',')} % vuodessa {case['days_past_due']} päivältä: "
                 f"{m(case['interest_cents'])}."]
        if case["fee_cents"]:
            lines.append(f"Korkolain 10 e §:n mukainen vakiokorvaus perintäkuluista: {billing._fmt_eur(case['fee_cents'], 'fi')}.")
        elif case.get("compensation_invoice_id"):
            lines.append("Vakiokorvaus perintäkuluista on laskutettu erikseen; sitä ei veloiteta uudelleen.")
        lines += [f"Yhteensä {m(total_same)}" + (f" + {billing._fmt_eur(case['fee_cents'], 'fi')}" if cur != "EUR" and case["fee_cents"] else "")
                  + f". Maksettava viimeistään {nd}.",
                  f"Viitenumero {inv.get('reference')}" + (f" · IBAN {iban}" + (f" · BIC {bic}" if bic else "") if iban else "") + ".",
                  "Asiakkuutenne on luottokiellossa: uusia toimeksiantoja ei oteta vastaan ennen kuin saatava on maksettu.",
                  f"Jos maksua ei ole kirjattu {nd} mennessä, saatava siirretään perintätoimistolle, mikä aiheuttaa "
                  f"lisäkuluja. Jos olette jo maksaneet, tämä viesti on aiheeton."]
    else:
        subject = f"Final demand for payment: invoice {num}"
        lines = [f"Invoice {num} was due on {due}. Despite our reminders no payment has been recorded "
                 f"({case['days_past_due']} days late).",
                 f"Principal {m(case['amount_due_cents'])}.",
                 f"Late payment interest under the Finnish Interest Act 4 a §, {pct} % a year for {case['days_past_due']} days: "
                 f"{m(case['interest_cents'])}."]
        if case["fee_cents"]:
            lines.append(f"Fixed compensation for recovery costs under the Finnish Interest Act 10 e §: "
                         f"{billing._fmt_eur(case['fee_cents'], 'en')}.")
        elif case.get("compensation_invoice_id"):
            lines.append("The fixed compensation for recovery costs was invoiced separately and is not charged again.")
        lines += [f"Total {m(total_same)}" + (f" + {billing._fmt_eur(case['fee_cents'], 'en')}" if cur != "EUR" and case["fee_cents"] else "")
                  + f", payable by {nd} at the latest.",
                  f"Reference {inv.get('reference')} (RF {inv.get('rf_reference')})"
                  + (f" · IBAN {iban}" + (f" · BIC {bic}" if bic else "") if iban else "") + ".",
                  "Your account is on credit hold: we take no new orders until the debt is paid.",
                  f"If the payment is not recorded by {nd}, the debt will be handed over to a collection agency, "
                  f"which adds costs. If you have already paid, please ignore this message."]
    return subject, lines


async def send_demand(case_id: str, *, actor: dict | None, decision_id: str | None = None,
                      approval_id: str | None = None) -> dict:
    """Send the maksuvaatimus to the billing recipients and put the customer on credit hold."""
    from core.config import COMPANY_ADDRESS, COMPANY_NAME
    from core.email import render, send_email
    from core.notify import bi, notify_role

    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise CollectionsError("Collections case not found", 404)
    inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID)
    from services.invoice_disputes import is_open
    if inv and is_open(inv):
        return {"sent": [], "skipped": "invoice_disputed"}
    if not inv or billing._amount_due(inv) <= 0 or inv.get("status") in ("paid", "void", "credited"):
        await close_paid(case["invoice_id"], actor=actor, note="nothing due when the demand was about to go out")
        return {"sent": [], "skipped": "nothing_due"}
    if case["stage"] not in ("final_demand", "payment_plan"):
        raise CollectionsError(f"The case is at stage {case['stage']}; a final demand is no longer sent", 409)
    today = billing.today_fi()
    new_due = today + timedelta(days=DEMAND_DAYS)
    lang = await billing.billing_locale(inv["company_id"])
    lang = "en" if lang == "en" else "fi"
    subject, lines = _demand_texts(case, inv, new_due, lang)
    html, text = render(subject, lines, None, None,
                        footer=f"{COMPANY_NAME} · {COMPANY_ADDRESS} · "
                               + ("Palvelut PSYM 2015 -ehdoin" if lang == "fi" else "Services under NSAB 2015 (PSYM 2015)"))
    sent = []
    quiet = billing.QUIET_EMAIL.get() or bool(inv.get("demo"))
    recipients = await billing._company_recipients(inv["company_id"], (inv.get("buyer") or {}).get("billing_email"))
    if not quiet:
        for to in recipients:
            sent.append(await send_email(to, subject, html, text, tag="collections_final_demand",
                                         company_id=inv["company_id"], ref={"type": "invoice", "id": inv["id"]}))
    hold = await set_credit_hold(inv["company_id"], True, f"Collections: invoice {inv.get('number')} unpaid", actor)
    t = now_iso()
    agency_days = int((await get_settings()).get("collections_agency_after_days") or 30)
    demand = {"sent_at": t, "due_date": new_due.isoformat(), "language": lang, "subject": subject,
              "to": [s["to"] for s in sent], "email_ids": [s["id"] for s in sent],
              "status": [s["status"] for s in sent], "quiet": quiet, "decision_id": decision_id, "approval_id": approval_id}
    await db.collections_cases.update_one({"id": case_id}, {
        "$set": {"demand": demand, "credit_hold": True, "pending_action": None, "updated_at": t,
                 "next_action_at": (today + timedelta(days=agency_days)).isoformat()},
        "$push": {"actions": {"at": t, "action": "send_demand", "by": actor_ref(actor),
                              "note": f"to {', '.join(demand['to']) or '—'}; due {new_due.isoformat()}"}}})
    await db.invoices.update_one({"id": inv["id"]}, {"$push": {"emails": {
        "kind": "collections_final_demand", "at": t, "to": demand["to"], "status": demand["status"]}}})
    await audit(actor, "collections_final_demand_sent", "invoice", inv["id"],
                {"case_id": case_id, "to": demand["to"], "new_due_date": demand["due_date"], "credit_hold": hold,
                 "decision_id": decision_id, "approval_id": approval_id}, company_id=inv["company_id"])
    await notify_role("finance", bi(f"Maksuvaatimus lähetetty: {inv.get('number')}", f"Final demand sent: {inv.get('number')}"),
                      bi(f"{inv.get('company_name')} on luottokiellossa. Uusi eräpäivä {billing._fmt_date(demand['due_date'], 'fi')}.",
                         f"{inv.get('company_name')} is on credit hold. New due date {demand['due_date']}."),
                      link=f"/ops/finance/collections?case={case_id}")
    return {"sent": [{"to": s["to"], "status": s["status"], "id": s["id"]} for s in sent], "credit_hold": hold,
            "due_date": demand["due_date"]}


# ------------------------------------------------------------------------------------------------ agency hand-over
HANDOVER_COLUMNS = ["case_id", "debtor_name", "business_id", "vat_id", "street", "postal_code", "city", "country",
                    "email", "invoice_number", "invoice_date", "due_date", "currency", "principal", "interest",
                    "interest_rate_pct", "interest_from", "interest_until", "fees", "fees_currency", "reference",
                    "rf_reference", "final_demand_sent", "lien_possible"]


def _dec(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


async def handover_csv(case: dict, inv: dict) -> bytes:
    company = await db.companies.find_one({"id": case["company_id"]}, NO_ID) or {}
    buyer = inv.get("buyer") or {}
    addr = buyer.get("address") or company.get("address") or {}
    due = billing.to_date(inv.get("due_date"))
    row = {"case_id": case["id"], "debtor_name": buyer.get("name") or company.get("name"),
           "business_id": buyer.get("business_id") or company.get("business_id"),
           "vat_id": buyer.get("vat_id") or company.get("vat_id"), "street": addr.get("street"),
           "postal_code": addr.get("postal_code"), "city": addr.get("city"),
           "country": addr.get("country") or buyer.get("country"),
           "email": buyer.get("billing_email") or company.get("billing_email"),
           "invoice_number": inv.get("number"), "invoice_date": inv.get("issue_date"), "due_date": inv.get("due_date"),
           "currency": case["currency"], "principal": _dec(billing._amount_due(inv)),
           "interest": _dec(case["interest_cents"]), "interest_rate_pct": f"{case['late_interest_pct']:g}",
           "interest_from": (due + timedelta(days=1)).isoformat() if due else None,
           "interest_until": case.get("interest_until"), "fees": _dec(case["fee_cents"]), "fees_currency": "EUR",
           "reference": inv.get("reference"), "rf_reference": inv.get("rf_reference"),
           "final_demand_sent": ((case.get("demand") or {}).get("sent_at") or "")[:10],
           "lien_possible": "yes" if case.get("lien_possible") else "no"}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HANDOVER_COLUMNS, delimiter=";", lineterminator="\r\n")
    w.writeheader()
    w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue().encode("utf-8-sig")


# ------------------------------------------------------------------------------------------------ hand-over package
def _pdf(title: str, lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, h = A4
    y = h - 60
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, title)
    y -= 28
    c.setFont("Helvetica", 10)
    for ln in lines:
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = h - 60
        c.drawString(50, y, ln)
        y -= 15
    c.save()
    return buf.getvalue()


def _letter_lines(case: dict, inv: dict, company: dict, lang: str, test: bool) -> tuple[str, list[str]]:
    from core.config import COMPANY_NAME
    debtor = (inv.get("buyer") or {}).get("name") or company.get("name") or "—"
    num, cur = inv.get("number") or "—", case["currency"]
    principal, interest, fee = _dec(billing._amount_due(inv)), _dec(case["interest_cents"]), _dec(case["fee_cents"])
    demand = ((case.get("demand") or {}).get("sent_at") or "")[:10] or "—"
    mark = ["TEST - acceptance record, not a real collection claim", ""] if test else []
    if lang == "fi":
        return ("Perintätoimeksianto" + (" (TEST)" if test else ""), mark + [
            f"Toimeksiantaja: {COMPANY_NAME}", f"Velallinen: {debtor}", "",
            f"Siirrämme perittäväksi laskun {num}, eräpäivä {inv.get('due_date') or '—'}.",
            f"Pääoma {principal} {cur}, viivästyskorko {interest} {cur} ({case['late_interest_pct']:g} %),",
            f"perintäkulut {fee} EUR. Maksuvaatimus lähetetty {demand}; maksua ei ole saatu.",
            f"Viite: {inv.get('reference') or '—'}. Liitteet: saatavaerittely (CSV) ja tiliote (PDF).",
            "", f"Tapaus {case['id']}"])
    return ("Collection assignment" + (" (TEST)" if test else ""), mark + [
        f"Creditor: {COMPANY_NAME}", f"Debtor: {debtor}", "",
        f"We assign invoice {num}, due {inv.get('due_date') or '—'}, for collection.",
        f"Principal {principal} {cur}, late interest {interest} {cur} ({case['late_interest_pct']:g} %),",
        f"collection fees {fee} EUR. Final demand sent {demand}; no payment received.",
        f"Reference: {inv.get('reference') or '—'}. Attached: claim file (CSV) and statement (PDF).",
        "", f"Case {case['id']}"])


def _statement_lines(case: dict, inv: dict, test: bool) -> list[str]:
    cur = case["currency"]
    out = (["TEST - acceptance record", ""] if test else []) + [
        f"Invoice {inv.get('number')}  issued {inv.get('issue_date') or '—'}  due {inv.get('due_date') or '—'}",
        f"Invoice total {_dec(inv.get('total_cents') or 0)} {cur}", ""]
    for p in inv.get("payments") or []:
        out.append(f"Payment {str(p.get('paid_at') or p.get('at') or '')[:10]}  {_dec(p.get('amount_cents') or 0)} {cur}")
    out += ["", f"Open principal {_dec(billing._amount_due(inv))} {cur}",
            f"Late interest {_dec(case['interest_cents'])} {cur} at {case['late_interest_pct']:g} % from "
            f"{(billing.to_date(inv.get('due_date')) + timedelta(days=1)).isoformat() if inv.get('due_date') else '—'}",
            f"Fees {_dec(case['fee_cents'])} EUR", ""]
    for e in case.get("actions") or []:
        out.append(f"{str(e.get('at') or '')[:10]}  {e.get('action')}  {str(e.get('note') or '')[:70]}")
    return out


async def build_handover_package(case: dict, inv: dict) -> dict:
    """The agency hand-over package on the case: claim file (CSV), statement (PDF), cover letter FI + EN (PDF).
    Stored before the owner card is raised, so the owner reviews exactly what the agency would receive."""
    from core.storage import put_file
    company = await db.companies.find_one({"id": case["company_id"]}, NO_ID) or {}
    test = bool(company.get("test_tenant") or company.get("test") or inv.get("test"))
    base = inv.get("number") or case["id"]
    meta = {"case_id": case["id"], "company_id": case["company_id"]}
    files = []
    claim = await handover_csv(case, inv)
    files.append({"kind": "claim", "filename": f"perinta-{base}.csv", "content_type": "text/csv",
                  "file_id": await put_file(claim, f"perinta-{base}.csv", "text/csv", meta)})
    st = _pdf(("TEST " if test else "") + f"Tiliote / Statement {base}", _statement_lines(case, inv, test))
    files.append({"kind": "statement", "filename": f"tiliote-{base}.pdf", "content_type": "application/pdf",
                  "file_id": await put_file(st, f"tiliote-{base}.pdf", "application/pdf", meta)})
    for lang in ("fi", "en"):
        title, lines = _letter_lines(case, inv, company, lang, test)
        name = f"{'saate' if lang == 'fi' else 'cover-letter'}-{base}.pdf"
        files.append({"kind": f"letter_{lang}", "filename": name, "content_type": "application/pdf",
                      "file_id": await put_file(_pdf(title, lines), name, "application/pdf", meta)})
    package = {"at": now_iso(), "test": test, "agency_mode": agency_mode(), "files": files}
    await db.collections_cases.update_one({"id": case["id"]}, {"$set": {"handover_package": package, "updated_at": now_iso()}})
    return package


async def hand_over(case_id: str, *, actor: dict | None, decision_id: str | None = None,
                    approval_id: str | None = None) -> dict:
    from core.notify import bi, notify_role
    from core.storage import put_file

    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise CollectionsError("Collections case not found", 404)
    if case["stage"] not in ("final_demand", "payment_plan"):
        raise CollectionsError(f"The case is at stage {case['stage']} and cannot be handed over", 409)
    inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID)
    from services.invoice_disputes import is_open
    if inv and is_open(inv):
        return {"skipped": "invoice_disputed"}
    if not inv or billing._amount_due(inv) <= 0:
        await close_paid(case["invoice_id"], actor=actor, note="nothing due at hand-over")
        return {"skipped": "nothing_due"}
    mode = agency_mode()
    handover = {"at": now_iso(), "mode": mode, "by": actor_ref(actor), "decision_id": decision_id,
                "approval_id": approval_id, "file_id": None, "filename": None}
    if mode in ("file", "api"):
        data = await handover_csv(case, inv)
        fname = f"perinta-{inv.get('number') or case['id']}.csv"
        handover["file_id"] = await put_file(data, fname, "text/csv", {"case_id": case_id, "company_id": case["company_id"]})
        handover["filename"] = fname
    if mode == "api":
        # STUB: no collection-agency API is contracted. The file above is the hand-over; a person sends it.
        handover["api_status"] = "not_implemented"
        handover["api_message"] = "COLLECTIONS_AGENCY=api is not implemented; send the CSV to the agency by hand."
    await db.collections_cases.update_one({"id": case_id}, {
        "$set": {"stage": "agency", "handover": handover, "pending_action": None, "updated_at": now_iso(),
                 "next_action_at": None},
        "$push": {"actions": {"at": now_iso(), "action": "agency", "by": actor_ref(actor),
                              "note": f"mode {mode}" + (f"; file {handover['filename']}" if handover["filename"] else "")}}})
    await audit(actor, "collections_handed_over", "invoice", case["invoice_id"],
                {"case_id": case_id, "mode": mode, "file_id": handover["file_id"]}, company_id=case["company_id"])
    await notify_role("finance", bi(f"Perintään: {case.get('invoice_number')}", f"Handed to collection: {case.get('invoice_number')}"),
                      bi("Lähetä perintäaineisto (CSV) perintätoimistolle." if mode != "none" else "Siirrä saatava perintätoimistolle.",
                         "Send the hand-over file (CSV) to the collection agency." if mode != "none" else
                         "Hand the debt over to the collection agency."),
                      link=f"/ops/finance/collections?case={case_id}")
    return {"stage": "agency", "handover": handover}


async def decide_handover(case: dict) -> dict:
    inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID)
    package = await build_handover_package(case, inv) if inv else None
    d = await agent_decide(
        "handover_to_agency", subject=("collections_case", case["id"]), subject_label=case.get("invoice_number") or case["id"],
        inputs={"days_since_demand": case.get("demand") and (billing.today_fi() - billing.to_date(case["demand"]["sent_at"])).days,
                "amount_due_cents": case["amount_due_cents"], "currency": case["currency"], "mode": agency_mode()},
        decision={"stage": "agency"}, confidence=1.0, amount_cents=case["eur"]["amount_due_cents"],
        company_id=case["company_id"], approval_kind=APPROVAL_KIND,
        title=f"Hand over to collection agency: {case.get('invoice_number')}",
        title_fi=f"Siirto perintätoimistolle: {case.get('invoice_number')}",
        summary="The final demand's due date has passed without payment.",
        summary_fi="Maksuvaatimuksen eräpäivä on ohitettu ilman maksua.",
        link=f"/ops/finance/collections?case={case['id']}",
        approval_data={"case_id": case["id"], "action": "agency", "invoice_id": case["invoice_id"],
                       "package": [{k: f[k] for k in ("kind", "filename")} | {"path": f"/api/finance/collections/{case['id']}/package/{f['kind']}"}
                                   for f in (package or {}).get("files", [])]})
    if d["auto"]:
        return {"auto": True, **await hand_over(case["id"], actor=AGENT_ACTOR, decision_id=d["id"])}
    await _mark_pending(case["id"], "agency", d)
    return {"auto": False, "approval_id": (d.get("approval") or {}).get("id")}


# ------------------------------------------------------------------------------------------------ write-off / plan / close
async def request_write_off(case_id: str, actor: dict, note: str | None) -> dict:
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise CollectionsError("Collections case not found", 404)
    if case["stage"] not in OPEN_STAGES:
        raise CollectionsError(f"The case is already {case['stage']}", 409)
    await cancel_approvals(case_id, APPROVAL_KIND)  # a write-off supersedes a pending hand-over card
    d = await agent_decide(
        "write_off", subject=("collections_case", case_id), subject_label=case.get("invoice_number") or case_id,
        inputs={"amount_due_cents": case["amount_due_cents"], "currency": case["currency"], "requested_by": actor_ref(actor),
                "note": note},
        decision={"stage": "written_off"}, amount_cents=case["eur"]["amount_due_cents"], company_id=case["company_id"],
        extra_reasons=[policy_reason("judgment", "Luottotappion kirjaus on aina harkintaa: omistaja päättää",
                                     "Writing off a debt is always a judgment: the owner decides")],
        approval_kind=APPROVAL_KIND, title=f"Write off: {case.get('invoice_number')}",
        title_fi=f"Luottotappio: {case.get('invoice_number')}",
        summary=note or "Write the unpaid invoice off as a credit loss.",
        summary_fi=note or "Kirjaa maksamaton lasku luottotappioksi.",
        link=f"/ops/finance/collections?case={case_id}",
        approval_data={"case_id": case_id, "action": "write_off", "invoice_id": case["invoice_id"]})
    await _mark_pending(case_id, "write_off", d)
    return {"auto": False, "approval_id": (d.get("approval") or {}).get("id"), "reasons": d["reasons"]}


async def write_off(case_id: str, *, actor: dict | None, approval_id: str | None = None) -> dict:
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case or case["stage"] not in OPEN_STAGES:
        return {"skipped": "not_open"}
    t = now_iso()
    await db.collections_cases.update_one({"id": case_id}, {
        "$set": {"stage": "written_off", "written_off_at": t, "closed_at": t, "pending_action": None, "updated_at": t,
                 "write_off": {"at": t, "by": actor_ref(actor), "approval_id": approval_id,
                               "amount_cents": case["amount_due_cents"], "currency": case["currency"],
                               "eur_cents": case["eur"]["amount_due_cents"]}},
        "$push": {"actions": {"at": t, "action": "write_off", "by": actor_ref(actor), "note": None}}})
    await db.invoices.update_one({"id": case["invoice_id"]}, {"$set": {"written_off_at": t, "collections_stage": "written_off"}})
    await audit(actor, "collections_written_off", "invoice", case["invoice_id"],
                {"case_id": case_id, "approval_id": approval_id, "amount_cents": case["amount_due_cents"]},
                company_id=case["company_id"])
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    await emit("collections.closed", {"case": case})
    return {"stage": "written_off"}


async def payment_plan(case_id: str, installments: list[dict], actor: dict) -> dict:
    case = await db.collections_cases.find_one({"id": case_id}, NO_ID)
    if not case:
        raise CollectionsError("Collections case not found", 404)
    if case["stage"] not in ("final_demand", "payment_plan", "agency"):
        raise CollectionsError(f"The case is {case['stage']}; a payment plan cannot be agreed", 409)
    if not installments:
        raise CollectionsError("Add at least one instalment with a due date and an amount")
    today = billing.today_fi()
    clean = []
    for i, it in enumerate(installments):
        d = billing.to_date(it.get("due_date"))
        try:
            amount = int(it.get("amount_cents"))
        except (TypeError, ValueError):
            amount = 0
        if not d or d < today:
            raise CollectionsError(f"Instalment {i + 1}: give a due date (YYYY-MM-DD) today or later")
        if amount <= 0:
            raise CollectionsError(f"Instalment {i + 1}: the amount must be above zero cents")
        clean.append({"seq": i + 1, "due_date": d.isoformat(), "amount_cents": amount, "paid": False})
    clean.sort(key=lambda x: x["due_date"])
    total = sum(x["amount_cents"] for x in clean)
    inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID) or {}
    due_now = billing._amount_due(inv)
    if total < due_now:
        raise CollectionsError(f"The instalments add up to {total / 100:.2f} {case['currency']}, less than the "
                               f"{due_now / 100:.2f} {case['currency']} still due")
    plan = {"installments": clean, "total_cents": total, "currency": case["currency"], "agreed_at": now_iso(),
            "by": actor_ref(actor)}
    await db.collections_cases.update_one({"id": case_id}, {
        "$set": {"stage": "payment_plan", "payment_plan": plan, "next_action_at": clean[0]["due_date"],
                 "updated_at": now_iso()},
        "$push": {"actions": {"at": now_iso(), "action": "payment_plan", "by": actor_ref(actor),
                              "note": f"{len(clean)} instalments, {total / 100:.2f} {case['currency']}"}}})
    await audit(actor, "collections_payment_plan", "invoice", case["invoice_id"], {"case_id": case_id, "plan": plan},
                company_id=case["company_id"])
    return {"stage": "payment_plan", "payment_plan": plan}


async def close_paid(invoice_id: str, *, actor: dict | None, note: str | None = None) -> dict | None:
    """Invoice settled: case -> paid, credit hold released when no other case keeps it."""
    case = await db.collections_cases.find_one({"invoice_id": invoice_id, "stage": {"$in": list(OPEN_STAGES)}}, NO_ID)
    if not case:
        return None
    t = now_iso()
    r = await db.collections_cases.update_one({"id": case["id"], "stage": {"$in": list(OPEN_STAGES)}}, {
        "$set": {"stage": "paid", "closed_at": t, "pending_action": None, "updated_at": t, "next_action_at": None},
        "$push": {"actions": {"at": t, "action": "paid", "by": actor_ref(actor), "note": note}}})
    if not r.modified_count:
        return None
    await cancel_approvals(invoice_id, APPROVAL_KIND)
    await cancel_approvals(case["id"], APPROVAL_KIND)
    hold = await set_credit_hold(case["company_id"], False, f"Collections: invoice {case.get('invoice_number')} paid", actor)
    await db.collections_cases.update_one({"id": case["id"]}, {"$set": {"credit_hold": False, "credit_hold_release": hold}})
    await audit(actor or AGENT_ACTOR, "collections_case_closed", "invoice", invoice_id,
                {"case_id": case["id"], "stage": "paid", "credit_hold": hold}, company_id=case["company_id"])
    case = await db.collections_cases.find_one({"id": case["id"]}, NO_ID)
    await emit("collections.closed", {"case": case})
    return case


# ------------------------------------------------------------------------------------------------ sweep
async def sweep() -> dict:
    settings = await get_settings()
    today = billing.today_fi()
    out = {"opened": [], "demands": [], "queued": [], "handover": [], "skipped": 0}
    q = {"kind": "invoice", "status": {"$in": ["overdue", "open"]},
         "$or": [{"dunning.stage": "collection"}, {"dunning.final_sent_at": {"$ne": None}},
                 {"dunning.exhausted_at": {"$ne": None}},
                 {"status": "overdue", "dunning": {"$exists": False},
                  "due_date": {"$lte": (today - timedelta(days=int(settings.get("collections_after_days_overdue") or 21))).isoformat()}}]}
    async for inv in db.invoices.find(q, NO_ID):
        reason = entry_reason(inv, settings, today)
        if not reason:
            out["skipped"] += 1
            continue
        case, created = await open_case(inv, reason)
        if not created:
            continue
        out["opened"].append(case["id"])
        r = await decide_send(case, inv)
        (out["demands"] if r.get("auto") else out["queued"]).append(case["id"])
    # hand-over: final demand sent, its agency window passed, still unpaid, nothing pending
    agency_days = int(settings.get("collections_agency_after_days") or 30)
    async for case in db.collections_cases.find({"stage": "final_demand", "demand.sent_at": {"$ne": None},
                                                 "pending_action": None}, NO_ID):
        sent = billing.to_date(case["demand"]["sent_at"])
        if not sent or (today - sent).days < agency_days:
            continue
        inv = await db.invoices.find_one({"id": case["invoice_id"]}, NO_ID)
        if not inv or billing._amount_due(inv) <= 0:
            await close_paid(case["invoice_id"], actor=AGENT_ACTOR, note="settled")
            continue
        r = await decide_handover(case)
        out["handover"].append({"case_id": case["id"], "auto": r.get("auto")})
    return out


@finance_job("finance.collections_sweep", daily_at="08:00", label_fi="Perintä", label_en="Collections")
async def collections_sweep_job(j: dict):
    return await sweep()


# ------------------------------------------------------------------------------------------------ events
@on("invoice.paid")
async def _on_paid(p: dict):
    inv = p.get("invoice") or {}
    if inv.get("id"):
        await close_paid(inv["id"], actor=AGENT_ACTOR, note="invoice paid")


@on("invoice.dunning_exhausted")
async def _on_dunning_exhausted(p: dict):
    inv = p.get("invoice") or {}
    inv = await db.invoices.find_one({"id": inv.get("id")}, NO_ID) if inv.get("id") else None
    if not inv or inv.get("kind") != "invoice" or billing._amount_due(inv) <= 0 or inv.get("status") not in ("open", "overdue"):
        return
    from services.invoice_disputes import is_open
    if is_open(inv):
        return
    case, created = await open_case(inv, "dunning_exhausted")
    if created:
        await decide_send(case, inv)


@on("approval.decided")
async def _on_approval(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != APPROVAL_KIND:
        return
    data = a.get("data") or {}
    case_id, action = data.get("case_id"), data.get("finance_action") or data.get("action")
    if not case_id:
        return
    actor = p.get("actor")
    if a.get("status") != "approved":
        await db.collections_cases.update_one({"id": case_id}, {
            "$set": {"pending_action": None, "updated_at": now_iso()},
            "$push": {"actions": {"at": now_iso(), "action": f"{action}_rejected", "by": actor_ref(actor),
                                  "note": a.get("decision_note")}}})
        return
    if action == "send_demand":
        await send_demand(case_id, actor=actor, approval_id=a["id"])
    elif action == "agency":
        await hand_over(case_id, actor=actor, approval_id=a["id"])
    elif action == "write_off":
        await write_off(case_id, actor=actor, approval_id=a["id"])


# ------------------------------------------------------------------------------------------------ list
async def list_cases(stage: str | None = None, company_id: str | None = None, limit: int = 200) -> dict:
    base: dict = {}
    if company_id:
        base["company_id"] = company_id
    q = dict(base)
    if stage:
        q["stage"] = stage
    items = await db.collections_cases.find(q, NO_ID).sort("opened_at", -1).to_list(limit)
    counts = {s: 0 for s in STAGES}
    totals = {"open_count": 0, "open_amount_due_eur_cents": 0, "open_interest_eur_cents": 0, "open_fee_eur_cents": 0,
              "open_claim_eur_cents": 0, "credit_hold_count": 0, "lien_possible_count": 0, "pending_count": 0}
    async for c in db.collections_cases.find(base, {"_id": 0, "stage": 1, "eur": 1, "credit_hold": 1,
                                                    "lien_possible": 1, "pending_action": 1}):
        counts[c["stage"]] = counts.get(c["stage"], 0) + 1
        if c["stage"] in OPEN_STAGES:
            e = c.get("eur") or {}
            totals["open_count"] += 1
            totals["open_amount_due_eur_cents"] += int(e.get("amount_due_cents") or 0)
            totals["open_interest_eur_cents"] += int(e.get("interest_cents") or 0)
            totals["open_fee_eur_cents"] += int(e.get("fee_cents") or 0)
            totals["open_claim_eur_cents"] += int(e.get("total_cents") or 0)
            totals["credit_hold_count"] += 1 if c.get("credit_hold") else 0
            totals["lien_possible_count"] += 1 if c.get("lien_possible") else 0
            totals["pending_count"] += 1 if c.get("pending_action") else 0
    return {"items": items, "total": len(items), "counts": counts, "totals": totals, "agency_mode": agency_mode()}
