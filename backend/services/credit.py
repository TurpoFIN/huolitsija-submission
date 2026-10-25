"""Customer credit: position, booking check, automatic credit checks, holds (s7, shared/interfaces.md).

Position (credit_position): limit, open AR (issued unpaid invoices incl. partly paid), overdue (and max days overdue),
booked-but-uninvoiced shipments (price total), available = limit - open AR - booked uninvoiced, status
ok | watch (> 80 % used) | hold | blocked. This is the single source of the exposure number (s2 decide() should use
exposure() / credit_position()).

Limit basis: "set" once a person or an agent decided a limit (credit_limit_source), or credit_limit_cents > 0;
"pending" when a credit check recommended a limit that waits for approval; "provisional" for a customer that was never
assessed (no Business ID to check, or the register did not answer): credit_default_limit_cents applies until a check runs.

Credit checks (run_credit_check): free public signals (adapters/credit_bureau: YTJ, Vero tax-debt register, PRH
financials) plus the paid bureau when CREDIT_BUREAU is set. With Autopilot on (and not halted) the limit is set by the
"credit" agent through s2 decide() (fallback: core.autonomy.action + audit); otherwise a credit_limit approval card.
Hooks: KYC approved -> credit check; invoice issued -> hold when open AR > limit or overdue > credit_hold_overdue_days;
invoice paid -> automatic hold released when back within limit; credit.recheck_daily.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from fastapi import HTTPException

from adapters import credit_bureau as cb
from core.approvals import cancel_approvals, request_approval
from core.audit import actor_ref, agent_actor, audit
from core.db import db, register_index
from core.events import emit, on
from core.lifecycle import background_loop
from core.notify import bi, eur, notify_company, notify_role
from core.settings import get_settings
from core.util import NO_ID, new_id, now, now_iso

try:  # s2 agent runtime (not on this branch yet)
    from core.agent_runtime import decide
except ImportError:  # pragma: no cover
    decide = None
try:  # s2 durable jobs
    from core.jobs import enqueue, job, schedule
except ImportError:  # pragma: no cover
    enqueue = job = schedule = None

log = logging.getLogger("credit")
AGENT = agent_actor("credit")
OPEN_STATES = ("open", "overdue", "processing")
WATCH_PCT = 80

register_index("credit_checks", "id", unique=True)
register_index("credit_checks", [("company_id", 1), ("at", -1)])


def _today():
    from services.billing import today_fi
    return today_fi()


def _date(v):
    from services.billing import to_date
    return to_date(v)


# ---------------------------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------------------------
async def _receivables(company_id: str) -> dict:
    from services.billing import _amount_due

    today = _today()
    open_ar = overdue = max_days = 0
    count = 0
    # an unpaid prepayment invoice is not credit: nothing is booked until it is paid (services.prepayment)
    async for inv in db.invoices.find({"company_id": company_id, "kind": "invoice", "status": {"$in": list(OPEN_STATES)},
                                       "origin": {"$ne": "prepayment"}},
                                      {"_id": 0, "status": 1, "total_cents": 1, "paid_cents": 1, "credited_cents": 1,
                                       "due_date": 1}):
        due_amt = _amount_due(inv)
        if due_amt <= 0:
            continue
        count += 1
        open_ar += due_amt
        due = _date(inv.get("due_date"))
        if due and due < today and inv["status"] != "processing":
            overdue += due_amt
            max_days = max(max_days, (today - due).days)
    return {"open_ar_cents": open_ar, "overdue_cents": overdue, "max_days_overdue": max_days, "open_invoices": count}


async def _booked_uninvoiced(company_id: str) -> tuple[int, int]:
    invoiced = set(await db.invoices.distinct("shipment_id", {"company_id": company_id, "kind": "invoice",
                                                               "status": {"$nin": ["draft", "void"]}}))
    total = n = 0
    async for s in db.shipments.find({"company_id": company_id, "status": {"$nin": ["cancelled", "void"]}},
                                     {"_id": 0, "id": 1, "price": 1}):
        if s["id"] in invoiced:
            continue
        amt = int(((s.get("price") or {}).get("total_cents")) or 0)
        if amt > 0:
            total += amt
            n += 1
    return total, n


def limit_basis(company: dict, settings: dict) -> tuple[str, int]:
    raw = int(company.get("credit_limit_cents") or 0)
    if raw > 0 or company.get("credit_limit_source"):
        return "set", raw
    last = company.get("credit_last_check") or {}
    if last.get("status") == "ok":
        return "pending", 0
    return "provisional", int(settings.get("credit_default_limit_cents") or 0)


async def credit_position(company_id: str, *, company: dict | None = None, settings: dict | None = None) -> dict:
    company = company or await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    settings = settings or await get_settings()
    basis, limit = limit_basis(company, settings)
    rec = await _receivables(company_id)
    booked, booked_n = await _booked_uninvoiced(company_id)
    exposure_ = rec["open_ar_cents"] + booked
    available = limit - exposure_
    used_pct = round(exposure_ / limit * 100, 1) if limit else (100.0 if exposure_ else 0.0)
    hold = bool(company.get("credit_hold"))
    if hold:
        status = "hold"
    elif company.get("status") == "blocked" or limit <= 0 or available <= 0:
        status = "blocked"
    elif used_pct > WATCH_PCT:
        status = "watch"
    else:
        status = "ok"
    return {"company_id": company_id, "company_name": company.get("name"), "business_id": company.get("business_id"),
            "limit_cents": limit, "limit_basis": basis, "credit_limit_cents": int(company.get("credit_limit_cents") or 0),
            **rec, "booked_uninvoiced_cents": booked, "booked_uninvoiced_shipments": booked_n,
            "exposure_cents": exposure_, "available_cents": available, "used_pct": used_pct, "hold": hold,
            "hold_reason": company.get("credit_hold_reason") if hold else None,
            "hold_auto": bool(company.get("credit_hold_auto")) if hold else None, "status": status,
            "last_check": company.get("credit_last_check"), "hold_overdue_days": int(settings.get("credit_hold_overdue_days") or 30),
            "at": now_iso()}


async def exposure(company_id: str) -> int:
    pos = await credit_position(company_id)
    return pos["exposure_cents"]


async def _sync_status(company_id: str, pos: dict):
    await db.companies.update_one({"id": company_id, "credit_status": {"$ne": pos["status"]}},
                                  {"$set": {"credit_status": pos["status"]}})


# ---------------------------------------------------------------------------------------------
# Booking check
# ---------------------------------------------------------------------------------------------
def _blocker(fi: str, en: str, available: int) -> dict:
    return {"code": "credit", "message": en, "message_fi": fi, "available_cents": available, "link": "/portal/invoices"}


async def check_booking(company: dict, amount_cents: int) -> dict | None:
    """None when the booking fits the customer's credit; otherwise a booking blocker (bilingual, amounts in euros)."""
    if not company or not company.get("id"):
        return None
    settings = await get_settings()
    pos = await credit_position(company["id"], company=company, settings=settings)
    if pos["limit_basis"] == "provisional" and cb.normalise_bid(company.get("business_id")) \
            and not (company.get("credit_last_check") or {}).get("at"):
        # first booking of a customer that was never assessed: run the credit check now (Autopilot sets the limit)
        try:
            await run_credit_check(company["id"], actor=None, reason="booking")
        except Exception:  # noqa: BLE001
            log.exception("credit check before booking failed for %s", company["id"])
        company = await db.companies.find_one({"id": company["id"]}, NO_ID) or company
        pos = await credit_position(company["id"], company=company, settings=settings)
    await _sync_status(company["id"], pos)
    amt = int(amount_cents or 0)
    avail = pos["available_cents"]
    fi_av, en_av = eur(max(avail, 0), "fi"), eur(max(avail, 0), "en")
    fi_amt, en_amt = eur(amt, "fi"), eur(amt, "en")
    if pos["hold"]:
        return _blocker(f"Uudet varaukset on keskeytetty luottosyistä ({pos.get('hold_reason') or 'luottotarkistus'}). "
                        f"Maksa erääntyneet laskut tai ota yhteyttä: laskutus@huolitsija.fi.",
                        f"New bookings are on credit hold ({pos.get('hold_reason') or 'credit review'}). "
                        f"Pay the overdue invoices or contact billing@huolitsija.fi.", avail)
    if company.get("status") == "blocked":
        return _blocker("Yrityksen tili on estetty; varauksia ei voi tehdä.", "The company account is blocked; bookings are "
                        "not possible.", avail)
    if pos["limit_basis"] == "pending":
        return _blocker("Luottorajaa ei ole vielä vahvistettu: luottotarkistus odottaa hyväksyntää. Varaus avautuu, kun "
                        "luottoraja on asetettu.", "No credit limit yet: the credit check is waiting for approval. Booking "
                        "opens once the limit is set.", avail)
    if pos["limit_cents"] <= 0:  # s2: services.prepayment books it once the prepayment invoice is paid
        return {**_blocker("Yrityksellä ei ole luottorajaa: varaus vahvistuu, kun ennakkolasku on maksettu.",
                           "The company has no credit limit (prepayment only): the booking is confirmed once the "
                           "prepayment invoice is paid.", avail), "prepayment": True}
    if amt > avail:
        return _blocker(f"Varaus {fi_amt} ylittää käytettävissä olevan luoton {fi_av} (luottoraja "
                        f"{eur(pos['limit_cents'], 'fi')}, avoimet laskut {eur(pos['open_ar_cents'], 'fi')}, laskuttamattomat "
                        f"varaukset {eur(pos['booked_uninvoiced_cents'], 'fi')}). Maksa avoimia laskuja tai pyydä "
                        f"korotusta: laskutus@huolitsija.fi.",
                        f"This booking ({en_amt}) is above the available credit of {en_av} (limit "
                        f"{eur(pos['limit_cents'], 'en')}, open invoices {eur(pos['open_ar_cents'], 'en')}, booked not yet "
                        f"invoiced {eur(pos['booked_uninvoiced_cents'], 'en')}). Pay open invoices or ask for a higher "
                        f"limit: billing@huolitsija.fi.", avail)
    return None


# ---------------------------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------------------------
async def set_hold(company_id: str, on: bool, reason: str, actor: dict | None = None, *, auto: bool = False,
                   position: dict | None = None) -> dict:
    """Sets / clears companies.credit_hold (+ credit_status), emits credit.hold, notifies finance and the customer."""
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    if bool(company.get("credit_hold")) == on:
        return company
    sets = {"credit_hold": on, "credit_hold_reason": reason if on else None, "credit_hold_auto": auto if on else None,
            "credit_hold_at": now_iso() if on else company.get("credit_hold_at"),
            "credit_hold_released_at": None if on else now_iso(), "updated_at": now_iso()}
    await db.companies.update_one({"id": company_id}, {"$set": sets})
    pos = await credit_position(company_id)
    await _sync_status(company_id, pos)
    await audit(actor, "credit_hold_set" if on else "credit_hold_released", "company", company_id,
                {"reason": reason, "auto": auto, "position": _pos_summary(pos)}, company_id=company_id)
    name = company.get("name")
    link = f"/ops/customers/{company_id}"
    if on:
        await emit("credit.hold", {"company_id": company_id, "position": pos, "reason": reason})
        await notify_role(["finance"], bi(f"Luottoesto: {name}", f"Credit hold: {name}"),
                          bi(f"{reason}. Avoimet laskut {eur(pos['open_ar_cents'], 'fi')}, erääntyneet "
                             f"{eur(pos['overdue_cents'], 'fi')}, luottoraja {eur(pos['limit_cents'], 'fi')}. Uudet varaukset "
                             f"on estetty, laskutus jatkuu.",
                             f"{reason}. Open invoices {eur(pos['open_ar_cents'], 'en')}, overdue "
                             f"{eur(pos['overdue_cents'], 'en')}, limit {eur(pos['limit_cents'], 'en')}. New bookings are "
                             f"blocked; invoicing continues."), link=link, email=True, kind="credit")
        await notify_company(company_id, bi("Uudet varaukset keskeytetty", "New bookings paused"),
                             bi(f"Luottoraja on täynnä tai laskuja on erääntynyt ({eur(pos['overdue_cents'], 'fi')}). Varaukset "
                                "avautuvat, kun avoimet laskut on maksettu.",
                                f"Your credit limit is used up or invoices are overdue ({eur(pos['overdue_cents'], 'en')}). "
                                "Bookings reopen once the open invoices are paid."),
                             link="/portal/invoices", admins_only=True, kind="credit")
    else:
        await emit("credit.released", {"company_id": company_id, "position": pos, "reason": reason})
        await notify_role(["finance"], bi(f"Luottoesto poistettu: {name}", f"Credit hold released: {name}"),
                          bi(reason, reason), link=link, kind="credit")
    return await db.companies.find_one({"id": company_id}, NO_ID)


def _pos_summary(pos: dict) -> dict:
    return {k: pos.get(k) for k in ("limit_cents", "limit_basis", "open_ar_cents", "overdue_cents", "max_days_overdue",
                                    "booked_uninvoiced_cents", "available_cents", "status")}


def hold_reason(pos: dict) -> tuple[str, str] | None:
    """(fi, en) reason a customer should be on hold, or None."""
    if pos["limit_basis"] == "set" and pos["open_ar_cents"] > pos["limit_cents"]:
        return (f"Avoimet laskut {eur(pos['open_ar_cents'], 'fi')} ylittävät luottorajan {eur(pos['limit_cents'], 'fi')}",
                f"Open invoices {eur(pos['open_ar_cents'], 'en')} exceed the credit limit {eur(pos['limit_cents'], 'en')}")
    if pos["limit_basis"] == "provisional" and pos["open_ar_cents"] > pos["limit_cents"]:
        return (f"Avoimet laskut {eur(pos['open_ar_cents'], 'fi')} ylittävät alustavan luottorajan "
                f"{eur(pos['limit_cents'], 'fi')}",
                f"Open invoices {eur(pos['open_ar_cents'], 'en')} exceed the provisional limit {eur(pos['limit_cents'], 'en')}")
    if pos["max_days_overdue"] > pos["hold_overdue_days"]:
        return (f"Lasku {pos['max_days_overdue']} päivää erääntynyt (raja {pos['hold_overdue_days']} pv)",
                f"An invoice is {pos['max_days_overdue']} days overdue (limit {pos['hold_overdue_days']} days)")
    return None


async def recheck_hold(company_id: str, cause: str) -> dict:
    """Hold when over limit / overdue too long; release an automatic hold when back within limit."""
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        return {}
    pos = await credit_position(company_id, company=company)
    why = hold_reason(pos)
    if why and not pos["hold"]:
        await set_hold(company_id, True, why[1], AGENT, auto=True)
        await db.companies.update_one({"id": company_id}, {"$set": {"credit_hold_reason_fi": why[0]}})
    elif not why and pos["hold"] and company.get("credit_hold_auto"):
        await set_hold(company_id, False, f"Back within the credit limit ({cause})", AGENT, auto=True)
    else:
        await _sync_status(company_id, pos)
    return await credit_position(company_id)


async def after_invoice_issued(inv: dict):
    """Called by services.billing.issue_invoice: invoicing is never blocked, the hold blocks new bookings."""
    if inv.get("kind") == "invoice" and inv.get("company_id"):
        await recheck_hold(inv["company_id"], f"invoice {inv.get('number')} issued")


@on("invoice.paid")
async def _on_invoice_paid(p: dict):
    inv = p.get("invoice") or {}
    if inv.get("company_id"):
        await recheck_hold(inv["company_id"], f"invoice {inv.get('number')} paid")


# ---------------------------------------------------------------------------------------------
# Credit checks and the limit decision
# ---------------------------------------------------------------------------------------------
def _merge_bureau(chk: dict, bureau: dict | None, settings: dict) -> dict:
    if not bureau:
        return chk
    chk["bureau"] = bureau
    if bureau.get("found") and bureau.get("rating"):
        chk["rating_free"], chk["recommended_limit_free_cents"] = chk.get("rating"), chk.get("recommended_limit_cents")
        hard = chk.get("rating") == "decline" and chk.get("status") == "ok"
        chk["rating"] = "decline" if hard else bureau["rating"]
        chk["recommended_limit_cents"] = 0 if hard else cb.recommend_limit(
            bureau["rating"], young=bool(chk.get("young")), settings=settings, bureau_limit_cents=bureau.get("limit_cents"))
        chk["provider"] = f"free+{bureau['provider']}"
        chk["status"] = "ok"
        chk["reasons"].append(cb.reason(
            "bureau_rating", f"{bureau['provider']}: luokitus {bureau.get('common_value')} ({bureau.get('description') or ''}), "
                             f"raja {eur(bureau.get('limit_cents') or 0, 'fi')}",
            f"{bureau['provider']}: rating {bureau.get('common_value')} ({bureau.get('description') or ''}), limit "
            f"{eur(bureau.get('limit_cents') or 0, 'en')}", 0, bureau["provider"], bureau.get("url")))
    return chk


async def _autopilot_state() -> tuple[bool, bool]:
    settings = await get_settings()
    raw = await db.settings.find_one({"key": "app"}, {"_id": 0, "autopilot_halted": 1}) or {}
    return bool(settings.get("autopilot_enabled")), bool(raw.get("autopilot_halted"))


async def _apply_limit(company_id: str, limit: int, actor: dict | None, *, check_id: str | None, via: str) -> dict:
    company = await db.companies.find_one({"id": company_id}, NO_ID) or {}
    src = {"by": actor_ref(actor), "at": now_iso(), "check_id": check_id, "via": via}
    await db.companies.update_one({"id": company_id}, {"$set": {"credit_limit_cents": int(limit), "credit_limit_source": src,
                                                               "updated_at": now_iso()}})
    await audit(actor, "credit_limit_set", "company", company_id, {"from": company.get("credit_limit_cents"), "to": int(limit),
                                                                  "check_id": check_id, "via": via}, company_id=company_id)
    await recheck_hold(company_id, "credit limit set")
    return await db.companies.find_one({"id": company_id}, NO_ID)


def _should_act(company: dict, chk: dict, reason: str) -> bool:
    """A daily re-check only changes a limit to lower it, or when none is set; other checks always propose."""
    current = int(company.get("credit_limit_cents") or 0)
    rec = int(chk.get("recommended_limit_cents") or 0)
    if rec == current and company.get("credit_limit_source"):
        return False
    src = company.get("credit_limit_source") or {}
    person_set = current > 0 and src.get("via") != "autopilot"
    if person_set and rec < current and chk.get("rating") not in ("D", "decline"):
        # A limit a person set (or seeded) is only cut automatically when the company deteriorates (D / decline);
        # otherwise the lower recommendation is stored on the check for a person to see.
        return False
    if reason != "daily":
        return True
    return rec < current or not company.get("credit_limit_source")


async def run_credit_check(company_id: str, actor: dict | None = None, reason: str = "manual") -> dict:
    """Free signals (+ bureau when configured) -> stored credit_checks row; limit set by the credit agent under Autopilot,
    otherwise a credit_limit approval card."""
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    settings = await get_settings()
    bid = cb.normalise_bid(company.get("business_id"))
    chk = await cb.free_check(company.get("business_id") or "", settings)
    if bid and chk.get("status") == "ok" and chk.get("provider") == "free":
        chk = _merge_bureau(chk, await cb.bureau_check(bid), settings)
    doc = {"id": new_id(), "company_id": company_id, "company_name": company.get("name"), "at": now_iso(),
           "reason": reason, "actor": actor_ref(actor), **chk, "outcome": None, "decision_id": None, "approval_id": None,
           "previous_limit_cents": int(company.get("credit_limit_cents") or 0)}
    await db.credit_checks.insert_one(dict(doc))
    await db.companies.update_one({"id": company_id}, {"$set": {"credit_last_check": {
        "id": doc["id"], "at": doc["at"], "rating": doc.get("rating"), "score": doc.get("score"), "status": doc.get("status"),
        "provider": doc.get("provider"), "recommended_limit_cents": doc.get("recommended_limit_cents")}}})
    outcome, decision_id, approval_id = "no_decision", None, None
    if doc.get("status") == "ok" and _should_act(company, doc, reason):
        outcome, decision_id, approval_id = await _decide_limit(company, doc, actor)
    elif doc.get("status") == "ok":
        outcome = "unchanged"
    await db.credit_checks.update_one({"id": doc["id"]}, {"$set": {"outcome": outcome, "decision_id": decision_id,
                                                                  "approval_id": approval_id}})
    doc.update(outcome=outcome, decision_id=decision_id, approval_id=approval_id)
    await audit(actor or AGENT, "credit_checked", "company", company_id,
                {"check_id": doc["id"], "rating": doc.get("rating"), "score": doc.get("score"), "outcome": outcome,
                 "recommended_limit_cents": doc.get("recommended_limit_cents"), "provider": doc.get("provider")},
                company_id=company_id)
    await emit("credit.checked", {"company_id": company_id, "check": doc})
    return doc


def _texts(company: dict, chk: dict) -> dict:
    lim, name, rating = int(chk.get("recommended_limit_cents") or 0), company.get("name"), chk.get("rating")
    top_fi = "; ".join(r["fi"] for r in chk.get("reasons") or [] if r.get("points"))[:600] or "ei huomautettavaa"
    top_en = "; ".join(r["en"] for r in chk.get("reasons") or [] if r.get("points"))[:600] or "nothing adverse"
    return {"title": f"Credit limit {eur(lim, 'en')} for {name} (rating {rating})",
            "title_fi": f"Luottoraja {eur(lim, 'fi')}: {name} (luokitus {rating})",
            "summary": f"Credit check {chk.get('provider')}: score {chk.get('score')}, rating {rating}. {top_en}",
            "summary_fi": f"Luottotarkistus {chk.get('provider')}: pisteet {chk.get('score')}, luokitus {rating}. {top_fi}"}


async def _decide_limit(company: dict, chk: dict, actor: dict | None) -> tuple[str, str | None, str | None]:
    cid, lim = company["id"], int(chk.get("recommended_limit_cents") or 0)
    t = _texts(company, chk)
    data = {"check_id": chk["id"], "limit_cents": lim, "rating": chk.get("rating"), "score": chk.get("score"),
            "previous_limit_cents": int(company.get("credit_limit_cents") or 0)}
    if decide is not None:
        d = await decide("credit", "set_credit_limit", subject=("company", cid), subject_label=company.get("name"),
                         inputs={"check_id": chk["id"], "provider": chk.get("provider"), "score": chk.get("score"),
                                 "rating": chk.get("rating"), "reasons": [r["code"] for r in chk.get("reasons") or []]},
                         decision={"limit_cents": lim}, confidence=None, company_id=cid, switch="autopilot_enabled",
                         approval_kind="credit_limit", title=t["title"], title_fi=t["title_fi"], summary=t["summary"],
                         summary_fi=t["summary_fi"], link=f"/ops/customers/{cid}", amount_cents=lim, approval_data=data,
                         create_approval=True)
        if d.auto:
            await _apply_limit(cid, lim, AGENT, check_id=chk["id"], via="autopilot")
            return "limit_set", d.id, None
        return ("approval_requested" if d.approval else "blocked"), d.id, (d.approval or {}).get("id")
    enabled, halted = await _autopilot_state()
    if enabled and not halted:
        from core.autonomy import action

        await _apply_limit(cid, lim, AGENT, check_id=chk["id"], via="autopilot")
        act = await action("credit_limit_set", "company", cid, company_id=cid,
                           data={**data, "reasons": [{"code": r["code"], "fi": r["fi"], "en": r["en"], "url": r.get("url")}
                                                     for r in chk.get("reasons") or []]}, reversible=True)
        await cancel_approvals(cid, "credit_limit")
        return "limit_set", act["id"], None
    await cancel_approvals(cid, "credit_limit")  # a newer check replaces an older pending card
    card = await request_approval("credit_limit", "company", cid, t["title"], summary=t["summary"],
                                  reason=("Autopilot is halted" if halted else "Autopilot is off") + ": a person decides the limit",
                                  company_id=cid, requested_by=actor or AGENT, data={**data, **{k: t[k] for k in ("title_fi", "summary_fi")}},
                                  amount_cents=lim, link=f"/ops/customers/{cid}")
    await db.approvals.update_one({"id": card["id"]}, {"$set": {"title_fi": t["title_fi"], "summary_fi": t["summary_fi"]}})
    return "approval_requested", None, card["id"]


@on("approval.decided")
async def _on_credit_approval(p: dict):
    a, actor = p["approval"], p.get("actor") or {}
    if a.get("kind") != "credit_limit":
        return
    data = a.get("data") or {}
    if a.get("status") != "approved":
        await audit(actor, "credit_limit_declined", "company", a["ref_id"], {"check_id": data.get("check_id")},
                    company_id=a["ref_id"])
        return
    lim = (a.get("decision_data") or {}).get("limit_cents")
    lim = int(lim if isinstance(lim, int) and lim >= 0 else data.get("limit_cents") or 0)
    await _apply_limit(a["ref_id"], lim, actor, check_id=data.get("check_id"), via="approval")
    if data.get("check_id"):
        await db.credit_checks.update_one({"id": data["check_id"]}, {"$set": {"outcome": "limit_set_by_approval"}})


async def set_limit_manual(company_id: str, limit_cents: int, actor: dict, note: str | None = None) -> dict:
    if type(limit_cents) is not int or limit_cents < 0 or limit_cents > 100_000_000_00:
        raise HTTPException(400, "Credit limit must be zero or more whole cents")
    await cancel_approvals(company_id, "credit_limit")
    return await _apply_limit(company_id, limit_cents, actor, check_id=None, via=f"manual{': ' + note if note else ''}")


# ---------------------------------------------------------------------------------------------
# Hooks and jobs
# ---------------------------------------------------------------------------------------------
_bg: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _kyc_credit_check(company_id: str):
    try:
        await run_credit_check(company_id, actor=None, reason="kyc_approved")
    except Exception:  # noqa: BLE001
        log.exception("credit check after KYC failed for %s", company_id)


@on("company.kyc_changed")
async def _on_kyc_changed(p: dict):
    c = p.get("company") or {}
    if c.get("kyc_status") != "approved" or not c.get("id"):
        return
    if enqueue is not None:
        try:
            await enqueue("credit.check", {"company_id": c["id"], "reason": "kyc_approved"},
                          key=f"credit.check:{c['id']}:kyc:{now_iso()[:13]}", company_id=c["id"], subject=("company", c["id"]))
            return
        except Exception:  # noqa: BLE001  (unknown kind etc.: run in-process)
            log.exception("enqueue credit.check failed")
    _spawn(_kyc_credit_check(c["id"]))


async def recheck_all() -> dict:
    """Daily: re-check active, KYC-approved customers (free registers: bankruptcy, tax debt) and every hold."""
    n_checked = n_holds = 0
    stale = (now() - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    async for c in db.raw.companies.find({"kyc_status": "approved", "status": {"$ne": "blocked"},
                                          "$or": [{"credit_last_check.at": {"$lt": stale}},
                                                  {"credit_last_check": {"$exists": False}}]}, {"_id": 0, "id": 1}):
        if (await _autopilot_state())[1]:
            break  # kill switch: no agent acts
        try:
            await run_credit_check(c["id"], actor=None, reason="daily")
            n_checked += 1
        except Exception:  # noqa: BLE001
            log.exception("daily credit check failed for %s", c["id"])
        await asyncio.sleep(0.5)  # be gentle with the public registers
    ids = set(await db.raw.invoices.distinct("company_id", {"kind": "invoice", "status": {"$in": list(OPEN_STATES)}}))
    ids |= set(await db.raw.companies.distinct("id", {"credit_hold": True}))
    for cid in ids:
        if cid:
            await recheck_hold(cid, "daily re-check")
            n_holds += 1
    return {"checked": n_checked, "holds_rechecked": n_holds, "at": now_iso()}


if job is not None:  # s2 durable jobs
    @job("credit.check", max_attempts=3, backoff=(300, 1800, 7200), timeout=120, agent=True,
         label_fi="Luottotarkistus", label_en="Credit check")
    async def _credit_check_job(j: dict) -> dict:
        p = j.get("payload") or {}
        chk = await run_credit_check(p["company_id"], actor=None, reason=p.get("reason") or "job")
        return {"check_id": chk["id"], "rating": chk.get("rating"), "outcome": chk.get("outcome")}

    @job("credit.recheck_daily", max_attempts=3, timeout=3600, agent=True,
         label_fi="Luottojen päivittäinen tarkistus", label_en="Daily credit re-check")
    async def _recheck_job(j: dict) -> dict:
        return await recheck_all()

    schedule("credit.recheck_daily", job="credit.recheck_daily", daily_at="06:00",
             label_fi="Luottojen päivittäinen tarkistus", label_en="Daily credit re-check")()
else:
    @background_loop(seconds=6 * 3600)
    async def credit_recheck_loop():
        # once per 20 h across instances: compare-and-set on a marker in db.system
        marker = "credit-recheck-daily"
        cutoff = (now() - timedelta(hours=20)).timestamp()
        doc = await db.system.find_one({"_id": marker}) or {}
        if doc.get("at_ts", 0) > cutoff:
            return
        r = await db.system.update_one({"_id": marker, "at_ts": doc.get("at_ts", 0)},
                                       {"$set": {"at_ts": now().timestamp(), "at": now_iso()}}, upsert=not doc)
        if not (r.modified_count or r.upserted_id):
            return
        res = await recheck_all()
        await db.system.update_one({"_id": marker}, {"$set": {"result": res}})


def customer_view(pos: dict) -> dict:
    return {k: pos.get(k) for k in ("limit_cents", "limit_basis", "open_ar_cents", "overdue_cents", "max_days_overdue",
                                    "booked_uninvoiced_cents", "exposure_cents", "available_cents", "used_pct", "hold",
                                    "status", "at")}

