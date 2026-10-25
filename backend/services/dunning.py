"""Finnish B2B dunning (perintä): payment reminders by days past due, the fixed compensation for recovery costs and the
hand-over to collection, all behind one decision gate.

Stages by days past the due date (Helsinki date), settings in brackets:
    reminder_1    (dunning_reminder1_days, 7)    reminder e-mail
    reminder_2    (dunning_reminder2_days, 21)   second reminder + the fixed compensation (dunning_fixed_compensation_cents,
                                                 EUR 40, Korkolaki 633/1982 10 a §) as its own small invoice, once
    final_notice  (dunning_final_days, 35)       final demand with the date after which the debt goes to collection
    collection    (dunning_collection_days, 45)  always the owner's decision; records `collection` on the invoice
The daily job `dunning.run` (08:00 Helsinki) takes each issued, unpaid invoice to the highest stage it has reached
and not done. Every stage goes through decide("dunning_agent", "dunning_<stage>") with approval_kind
"dunning_collection": with Autopilot on reminders go out at once, otherwise the owner approves each card. Late
interest (principal × late_interest_pct × days / 365) is shown in the reminder, never invoiced here.
The stage is claimed with a compare-and-set on `dunning.stage`, so two servers never send the same reminder twice.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.agent_runtime import agent_bot, decide, notify_owner, policy_reason
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import on
from core.jobs import job, schedule
from core.notify import bi, notify_company
from core.settings import get_settings
from core.util import NO_ID, now_iso
from services import billing

log = logging.getLogger("dunning")
AGENT = "dunning_agent"
STAGES = ("reminder_1", "reminder_2", "final_notice", "collection")
STAGE_DAYS = {"reminder_1": "dunning_reminder1_days", "reminder_2": "dunning_reminder2_days",
              "final_notice": "dunning_final_days", "collection": "dunning_collection_days"}
COMPENSATION_FROM = "reminder_2"
STAGE_LABEL = {"reminder_1": ("Maksumuistutus", "Payment reminder"),
               "reminder_2": ("Toinen maksumuistutus", "Second payment reminder"),
               "final_notice": ("Viimeinen maksukehotus", "Final payment demand"),
               "collection": ("Perintään siirto", "Hand-over to collection")}
COMPENSATION_TEXT = {"fi": "Vakiokorvaus perintäkuluista (korkolaki 10 a §)",
                     "en": "Vakiokorvaus perintäkuluista (korkolaki 10 a §) / Fixed compensation for recovery costs "
                           "(Finnish Interest Act 10 a §)"}
REMINDER_FEE_TEXT = {"fi": "Muistutusmaksu", "en": "Muistutusmaksu / Reminder fee"}

register_index("invoices", [("origin", 1), ("dunning_for", 1)])
register_index("invoices", [("kind", 1), ("status", 1), ("due_date", 1)])


def _idx(stage: str | None) -> int:
    return STAGES.index(stage) if stage in STAGES else -1


def thresholds(settings: dict) -> list[tuple[str, int]]:
    return [(s, int(settings.get(STAGE_DAYS[s]) or 0)) for s in STAGES]


def target_stage(days_past_due: int, settings: dict) -> str | None:
    out = None
    for stage, days in thresholds(settings):
        if days_past_due >= days:
            out = stage
    return out


def late_interest_cents(principal_cents: int, pct: float, days: int) -> int:
    """Korkolaki 4 a § late interest for `days`: principal × pct/100 × days/365, rounded half up to a cent."""
    if principal_cents <= 0 or days <= 0 or not pct:
        return 0
    v = Decimal(int(principal_cents)) * Decimal(str(pct)) / Decimal(100) * Decimal(int(days)) / Decimal(365)
    return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _eur(cents: int | None, lang: str) -> str:
    return billing._fmt_eur(int(cents or 0), lang)


def _pct(value: float, lang: str) -> str:
    s = f"{float(value):g}"
    return s.replace(".", ",") if lang == "fi" else s


async def _blocked(inv: dict) -> str | None:
    """Payment under way or an open question about a payment: do not chase the customer now."""
    from services.invoice_disputes import is_open
    latest = await db.raw.invoices.find_one({"id": inv["id"]}, {"dispute": 1})
    if is_open(latest or inv):
        return "invoice_disputed"
    if await db.raw.payment_transactions.find_one({"invoice_id": inv["id"], "status": "processing"}, {"_id": 1}):
        return "card_payment_processing"
    if await db.raw.approvals.find_one({"kind": "payment_match", "status": "pending",
                                        "$or": [{"data.candidate_invoice_ids": inv["id"]}, {"data.invoice_id": inv["id"]}]},
                                       {"_id": 1}):
        return "payment_match_pending"
    if await db.raw.approvals.find_one({"kind": "dunning_collection", "status": "pending", "ref_id": inv["id"]}, {"_id": 1}):
        return "owner_card_pending"
    return None


# ---------------------------------------------------------------------------------------------
# Daily run
# ---------------------------------------------------------------------------------------------
async def run_dunning(today: date | None = None) -> dict:
    settings = await get_settings()
    if not settings.get("dunning_enabled"):
        return {"skipped": "dunning_disabled"}
    today = today or billing.today_fi()
    first = min(d for _, d in thresholds(settings))
    q = {"kind": "invoice", "status": {"$in": ["open", "overdue"]}, "demo": {"$ne": True}, "origin": {"$ne": "dunning"},
         "number": {"$ne": None}, "due_date": {"$lte": (today - timedelta(days=first)).isoformat()}}
    out = {"checked": 0, "done": [], "queued": [], "skipped": {}}
    async for inv in db.raw.invoices.find(q, NO_ID).sort("due_date", 1).limit(1000):
        out["checked"] += 1
        try:
            r = await process_invoice(inv, settings=settings, today=today)
        except Exception as exc:  # noqa: BLE001 - one bad invoice must not stop the run
            log.exception("dunning failed for %s", inv.get("id"))
            r = {"skipped": f"error: {type(exc).__name__}: {exc}"[:300]}
        if r.get("done"):
            out["done"].append({"invoice_id": inv["id"], "number": inv.get("number"), "stage": r["done"]})
        elif r.get("queued"):
            out["queued"].append({"invoice_id": inv["id"], "number": inv.get("number"), "stage": r["queued"],
                                  "approval_id": r.get("approval_id")})
        else:
            key = r.get("skipped") or "nothing_due"
            out["skipped"][key] = out["skipped"].get(key, 0) + 1
    return out


async def process_invoice(inv: dict, *, settings: dict, today: date) -> dict:
    due_amount = billing._amount_due(inv)
    due = billing.to_date(inv.get("due_date"))
    if due_amount <= 0 or not due:
        return {"skipped": "nothing_due"}
    days = (today - due).days
    target = target_stage(days, settings)
    state = inv.get("dunning") or {}
    current = state.get("stage")
    if not target or _idx(current) >= _idx(target):
        return {"skipped": "stage_done" if target else "not_due"}
    if (state.get("rejected") or {}).get("stage") == target:
        return {"skipped": "owner_declined"}
    why = await _blocked(inv)
    if why:
        return {"skipped": why}
    pct = float(inv.get("late_interest_pct") or settings.get("late_interest_pct") or 0)
    interest = late_interest_cents(due_amount, pct, days)
    comp_cents = int(settings.get("dunning_fixed_compensation_cents") or 0)
    compensation = (_idx(target) >= _idx(COMPENSATION_FROM) and not state.get("compensation_invoice_id")
                    and comp_cents > 0 and target != "collection")
    extra = []
    if target == "collection":
        extra.append(policy_reason("collection_needs_owner", "Perintään siirto vaatii aina omistajan päätöksen",
                                   "Handing a debt to collection always needs the owner"))
    label_fi, label_en = STAGE_LABEL[target]
    num, company = inv.get("number"), inv.get("company_name") or "—"
    summary_fi = (f"{company}: lasku {num} on {days} päivää myöhässä, avoinna {_eur(due_amount, 'fi')}, viivästyskorkoa "
                  f"kertynyt {_eur(interest, 'fi')}." + (f" Vakiokorvaus {_eur(comp_cents, 'fi')} laskutetaan erikseen." if compensation else "")
                  + (f" Edellinen vaihe: {STAGE_LABEL[current][0].lower()}." if current else " Muistutuksia ei ole lähetetty."))
    summary_en = (f"{company}: invoice {num} is {days} days late, {_eur(due_amount, 'en')} due, late interest so far "
                  f"{_eur(interest, 'en')}." + (f" The {_eur(comp_cents, 'en')} compensation is invoiced separately." if compensation else "")
                  + (f" Previous step: {STAGE_LABEL[current][1].lower()}." if current else " No reminder has been sent."))
    d = await decide(
        AGENT, f"dunning_{target}", subject=("invoice", inv["id"]), subject_label=num, company_id=inv["company_id"],
        inputs={"number": num, "due_date": inv.get("due_date"), "days_past_due": days, "amount_due_cents": due_amount,
                "late_interest_pct": pct, "previous_stage": current, "thresholds": dict(thresholds(settings))},
        decision={"stage": target, "interest_cents": interest, "compensation": compensation}, confidence=1.0,
        extra_reasons=extra, approval_kind="dunning_collection",
        title=f"{label_en}: {num}", title_fi=f"{label_fi}: {num}", summary=summary_en, summary_fi=summary_fi,
        link=f"/ops/billing/{inv['id']}", amount_cents=due_amount,
        approval_data={"invoice_id": inv["id"], "stage": target, "previous_stage": current, "days_past_due": days})
    if not d.auto:
        return {"queued": target, "approval_id": (d.approval or {}).get("id")}
    r = await execute_stage(inv["id"], target, expected_prev=current, actor=agent_bot(AGENT), decision_id=d.id)
    return {"done": target, **r} if r.get("executed") else r


# ---------------------------------------------------------------------------------------------
# One stage
# ---------------------------------------------------------------------------------------------
async def claim_stage(invoice_id: str, stage: str, expected_prev: str | None) -> bool:
    """Compare-and-set `dunning.stage` from expected_prev to stage. Of any number of concurrent callers exactly one
    gets True; the others must not send anything."""
    t = now_iso()
    if expected_prev is not None:
        r = await db.raw.invoices.update_one({"id": invoice_id, "dunning.stage": expected_prev,
                                             "dispute.status": {"$nin": ["submitted", "in_review"]}},
                                             {"$set": {"dunning.stage": stage, "dunning.updated_at": t}})
        return r.modified_count == 1
    r = await db.raw.invoices.update_one({"id": invoice_id, "dunning": {"$type": "object"}, "dunning.stage": None,
                                         "dispute.status": {"$nin": ["submitted", "in_review"]}},
                                         {"$set": {"dunning.stage": stage, "dunning.updated_at": t}})
    if r.modified_count:
        return True
    r = await db.raw.invoices.update_one({"id": invoice_id, "$or": [{"dunning": {"$exists": False}}, {"dunning": None}],
                                         "dispute.status": {"$nin": ["submitted", "in_review"]}},
                                         {"$set": {"dunning": {"stage": stage, "history": [], "updated_at": t}}})
    return r.modified_count == 1


async def execute_stage(invoice_id: str, stage: str, *, expected_prev: str | None, actor: dict | None,
                        decision_id: str | None = None, approval_id: str | None = None) -> dict:
    inv = await db.raw.invoices.find_one({"id": invoice_id}, NO_ID)
    if not inv or inv.get("status") not in ("open", "overdue") or billing._amount_due(inv) <= 0:
        return {"skipped": "not_open"}
    from services.invoice_disputes import is_open
    if is_open(inv):
        return {"skipped": "invoice_disputed"}
    state = inv.get("dunning") or {}
    if state.get("stage") != expected_prev or _idx(stage) <= _idx(expected_prev):
        return {"skipped": "stage_changed"}
    t = now_iso()
    if not await claim_stage(invoice_id, stage, expected_prev):
        return {"skipped": "lost_race"}  # another server (or the owner's card) did this stage just now
    settings = await get_settings()
    today = billing.today_fi()
    due_amount = billing._amount_due(inv)
    days = (today - billing.to_date(inv["due_date"])).days
    pct = float(inv.get("late_interest_pct") or settings.get("late_interest_pct") or 0)
    interest = late_interest_cents(due_amount, pct, days)
    entry = {"stage": stage, "at": t, "by": actor_ref(actor), "days_past_due": days, "amount_due_cents": due_amount,
             "interest_cents": interest, "late_interest_pct": pct, "email_id": None, "email_ids": [],
             "compensation_invoice_id": None, "decision_id": decision_id, "approval_id": approval_id}
    comp = None
    if stage != "collection" and _idx(stage) >= _idx(COMPENSATION_FROM) and not state.get("compensation_invoice_id"):
        try:
            comp = await _compensation_invoice(inv, settings, actor)
        except billing.BillingError as exc:
            entry["compensation_error"] = str(exc)
            log.warning("compensation invoice for %s failed: %s", inv.get("number"), exc)
        if comp:
            entry["compensation_invoice_id"] = comp["id"]
    elif state.get("compensation_invoice_id"):
        comp = await db.raw.invoices.find_one({"id": state["compensation_invoice_id"]}, NO_ID)
    push = {"dunning.history": entry}
    extra_sets = {"dunning.last_at": t}
    if comp and entry["compensation_invoice_id"]:
        extra_sets["dunning.compensation_invoice_id"] = comp["id"]
    if stage == "collection":
        extra_sets["collection"] = {"at": t, "by": actor_ref(actor), "approval_id": approval_id, "agency": None}
        await db.raw.invoices.update_one({"id": invoice_id}, {"$push": push, "$set": extra_sets})
        await _notify_collection(inv, days, due_amount, interest)
    else:
        sent, texts = await _send_reminder(inv, stage, days=days, due_amount=due_amount, interest=interest, pct=pct,
                                           comp=comp, settings=settings)
        entry["email_ids"] = [s["id"] for s in sent]
        entry["email_id"] = entry["email_ids"][0] if sent else None
        push["emails"] = {"kind": f"dunning_{stage}", "at": t, "to": [s["to"] for s in sent], "status": [s["status"] for s in sent]}
        await db.raw.invoices.update_one({"id": invoice_id}, {"$push": push, "$set": extra_sets})
        sub_fi, sub_en = texts["fi"]["subject"], texts["en"]["subject"]
        first_fi, first_en = texts["fi"]["lines"][0], texts["en"]["lines"][0]
        await notify_company(inv["company_id"], bi(sub_fi, sub_en), bi(first_fi, first_en),
                             link=f"/portal/invoices/{invoice_id}", kind="billing")
    await audit(actor, f"dunning_{stage}", "invoice", invoice_id,
                {"days_past_due": days, "amount_due_cents": due_amount, "interest_cents": interest,
                 "compensation_invoice_id": entry["compensation_invoice_id"], "emails": entry["email_ids"]},
                company_id=inv["company_id"])
    return {"executed": True, "stage": stage, "email_ids": entry["email_ids"], "interest_cents": interest,
            "compensation_invoice_id": entry["compensation_invoice_id"]}


async def _compensation_invoice(inv: dict, settings: dict, actor: dict | None) -> dict | None:
    """The EUR 40 (setting) compensation as its own issued invoice with its own reference; once per late invoice."""
    existing = await db.raw.invoices.find_one({"origin": "dunning", "dunning_for": inv["id"], "status": {"$ne": "void"}}, NO_ID)
    if existing:  # an earlier attempt that stopped before or during issuing finishes now
        return existing if existing["status"] != "draft" else await billing.issue_invoice(existing["id"], actor, send=True)
    amount = int(settings.get("dunning_fixed_compensation_cents") or 0)
    if amount <= 0:
        return None
    company = await billing._company(inv["company_id"])
    if not company:
        raise billing.BillingError("The customer company was not found", 409)
    loc = "en" if inv.get("locale") == "en" else "fi"
    lines = [billing._line(COMPENSATION_TEXT[loc], qty=1, unit_price_cents=amount, vat_code="OUT_OF_SCOPE_DISBURSEMENT",
                           code="DUNNING_COMPENSATION", source="dunning", source_ref=inv["id"])]
    fee = int(settings.get("dunning_reminder_fee_cents") or 0)
    if fee > 0:
        lines.append(billing._line(REMINDER_FEE_TEXT[loc], qty=1, unit_price_cents=fee, vat_code="OUT_OF_SCOPE_DISBURSEMENT",
                                   code="DUNNING_REMINDER_FEE", source="dunning", source_ref=inv["id"]))
    inv_note = (f"Laskun {inv.get('number')} myöhästynyt maksu (eräpäivä {billing._fmt_date(inv.get('due_date'), 'fi')})."
                if loc == "fi" else f"Late payment of invoice {inv.get('number')} (due {billing._fmt_date(inv.get('due_date'), 'en')}).")
    draft = await billing._new_invoice(company, lines, settings=settings, actor=actor, shipment=None, origin="dunning",
                                       supply_date=billing.today_fi(), notes=inv_note,
                                       customer_reference=inv.get("number"), demo=bool(inv.get("demo")))
    await db.raw.invoices.update_one({"id": draft["id"]}, {"$set": {"dunning_for": inv["id"],
                                                                    "dunning_for_number": inv.get("number")}})
    return await billing.issue_invoice(draft["id"], actor, send=True)


def _texts(inv: dict, stage: str, *, days: int, due_amount: int, interest: int, pct: float, comp: dict | None,
           settings: dict, lang: str) -> dict:
    fi = lang == "fi"
    num = inv.get("number") or ""
    due_s = billing._fmt_date(inv.get("due_date"), lang)
    seller = inv.get("seller") or {}
    iban = settings.get("seller_iban") or seller.get("iban")
    bic = settings.get("seller_bic") or seller.get("bic")
    label = STAGE_LABEL[stage][0 if fi else 1]
    subject = f"{label}: lasku {num}" if fi else f"{label}: invoice {num}"
    if fi:
        lines = [f"Laskun {num} eräpäivä oli {due_s}, eikä maksua ole vielä kirjattu ({days} päivää myöhässä).",
                 f"Avoinna {_eur(due_amount, 'fi')}."]
        lines.append(f"Viivästyskorkoa (korkolaki 4 a §, {_pct(pct, 'fi')} % vuodessa) on kertynyt {days} päivältä "
                     f"{_eur(interest, 'fi')}. Korkoa ei laskuteta tällä viestillä.")
    else:
        lines = [f"Invoice {num} was due on {due_s} and we have not received the payment yet ({days} days late).",
                 f"Outstanding {_eur(due_amount, 'en')}."]
        lines.append(f"Late payment interest (Finnish Interest Act 4 a §, {_pct(pct, 'en')} % a year) for {days} days "
                     f"comes to {_eur(interest, 'en')}. It is not invoiced with this message.")
    if comp:
        if fi:
            lines.append(f"Korkolain 10 a §:n mukainen vakiokorvaus perintäkuluista {_eur(comp['total_cents'], 'fi')} "
                         f"laskutetaan erillisellä laskulla {comp.get('number')} (viitenumero {comp.get('reference')}).")
        else:
            lines.append(f"The fixed compensation for recovery costs under the Finnish Interest Act 10 a §, "
                         f"{_eur(comp['total_cents'], 'en')}, is invoiced separately on {comp.get('number')} "
                         f"(reference {comp.get('reference')}).")
    if stage == "final_notice":
        deadline = billing.to_date(inv["due_date"]) + timedelta(days=int(settings.get("dunning_collection_days") or 45))
        deadline = max(deadline, billing.today_fi() + timedelta(days=7))
        lines.append(f"Ellei maksua ole tilillämme viimeistään {billing._fmt_date(deadline.isoformat(), 'fi')}, saatava "
                     f"siirretään perintään, mistä aiheutuu lisäkuluja." if fi else
                     f"Unless the payment is in our account by {billing._fmt_date(deadline.isoformat(), 'en')}, the debt "
                     f"will be handed over to collection, which adds costs.")
    if iban:
        lines.append((f"Maksa tilille IBAN {iban}" + (f", BIC {bic}" if bic else "") + f", viitenumero {inv.get('reference')}.")
                     if fi else
                     (f"Pay to IBAN {iban}" + (f", BIC {bic}" if bic else "") +
                      f", reference {inv.get('reference')} (RF {inv.get('rf_reference')})."))
    else:
        lines.append(f"Viitenumero {inv.get('reference')} (RF {inv.get('rf_reference')})." if fi else
                     f"Reference {inv.get('reference')} (RF {inv.get('rf_reference')}).")
    lines.append("Jos lasku on jo maksettu, tämä viesti on aiheeton." if fi else
                 "If you have already paid, please ignore this message.")
    return {"subject": subject, "lines": lines, "button": "Näytä ja maksa" if fi else "View and pay"}


async def _send_reminder(inv: dict, stage: str, *, days: int, due_amount: int, interest: int, pct: float,
                         comp: dict | None, settings: dict) -> tuple[list[dict], dict]:
    """E-mail in the invoice language (Finnish by default) to the billing address or the company admins."""
    from core import config
    from core.email import render, send_email

    texts = {lang: _texts(inv, stage, days=days, due_amount=due_amount, interest=interest, pct=pct, comp=comp,
                          settings=settings, lang=lang) for lang in ("fi", "en")}
    if billing.QUIET_EMAIL.get():
        return [], texts
    loc = "en" if inv.get("locale") == "en" else "fi"
    tx = texts[loc]
    base = await billing.public_base_url()
    url = f"{base}/portal/invoices/{inv['id']}" if base else None
    html, text = render(tx["subject"], tx["lines"], url, tx["button"] if url else None,
                        footer=f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS} · "
                               + ("Palvelut PSYM 2015 -ehdoin" if loc == "fi" else "Services under NSAB 2015 (PSYM 2015)"))
    sent = []
    for to in await billing._company_recipients(inv["company_id"], (inv.get("buyer") or {}).get("billing_email")):
        sent.append(await send_email(to, tx["subject"], html, text, tag=f"dunning_{stage}", company_id=inv["company_id"],
                                     ref={"type": "invoice", "id": inv["id"], "stage": stage}))
    return sent, texts


async def _notify_collection(inv: dict, days: int, due_amount: int, interest: int):
    num, company = inv.get("number"), inv.get("company_name") or "—"
    await notify_owner(
        bi(f"Perintään: lasku {num}", f"To collection: invoice {num}"),
        bi(f"{company}: lasku {num} on {days} päivää myöhässä, avoinna {_eur(due_amount, 'fi')} ja viivästyskorkoa "
           f"{_eur(interest, 'fi')}. Huolitsijassa ei ole perintätoimistoliittymää: anna saatava perintätoimistolle itse "
           f"ja merkitse se laskun muistiinpanoihin.",
           f"{company}: invoice {num} is {days} days late, {_eur(due_amount, 'en')} due plus {_eur(interest, 'en')} late "
           f"interest. Huolitsija has no collection-agency integration: hand the debt to your collection agency yourself "
           f"and note it on the invoice."),
        f"/ops/billing/{inv['id']}", kind="dunning", dedupe_key=f"collection:{inv['id']}")


# ---------------------------------------------------------------------------------------------
# Job, schedule, owner decisions
# ---------------------------------------------------------------------------------------------
@job("dunning.run", agent=True, max_attempts=3, timeout=1800, label_fi="Maksumuistutukset ja perintä",
     label_en="Payment reminders and collection")
async def dunning_job(j):
    return await run_dunning()


schedule("dunning.daily_0800", job="dunning.run", daily_at="08:00",
         label_fi="Maksumuistutukset klo 8", label_en="Payment reminders at 08:00")()


@on("approval.decided")
async def _on_dunning_decided(p: dict):
    a = p["approval"]
    if a.get("kind") != "dunning_collection":
        return
    data = a.get("data") or {}
    stage, invoice_id = data.get("stage"), data.get("invoice_id") or a.get("ref_id")
    if stage not in STAGES or not invoice_id:
        return
    actor = p.get("actor")
    if a["status"] == "approved":
        await execute_stage(invoice_id, stage, expected_prev=data.get("previous_stage"), actor=actor,
                            decision_id=data.get("agent_decision_id"), approval_id=a["id"])
    elif a["status"] == "rejected":
        await db.raw.invoices.update_one({"id": invoice_id}, {"$set": {"dunning.rejected": {
            "stage": stage, "at": now_iso(), "by": actor_ref(actor), "approval_id": a["id"], "note": a.get("decision_note")}}})
        await audit(actor, "dunning_declined", "invoice", invoice_id, {"stage": stage, "approval_id": a["id"]},
                    company_id=a.get("company_id"))
