"""Tenant-scoped finance runs for the labelled test tenant (s5 acceptance on the live app).

The global jobs (dunning.run, finance.collections_sweep, payments.camt054_import of every statement, fx revaluation)
stay unchanged. These manual triggers do the same work for ONE company and refuse anything that is not the configured
test tenant:

* the company must exist with test_tenant=true and be TEST_BOOTSTRAP_COMPANY_ID;
* its e-mail alias TEST_BOOTSTRAP_EMAIL_ALIAS must be a user of that company.

E-mail: every mail for the test tenant goes to its alias only, prefixed "[TEST]" (outbound hook below; the alias shares
the sender domain, so it is also deliverable while the mail provider account is in its approval period). During a
scoped run, mail for any other company is suppressed.

Dates: a scoped run may pass `as_of` (a date after today, at most 60 days ahead) so a TEST invoice reaches its dunning
or collections stage without waiting. billing.today_fi() returns that date only inside the run, and every record the
run writes carries `simulated_as_of`.
"""
from __future__ import annotations

import contextvars
from datetime import date, timedelta

from core import config
from core import email as core_email
from core.audit import audit
from core.db import db
from core.util import NO_ID, now_iso
from services import billing

MAX_AHEAD_DAYS = 60
_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar("test_tenant_finance_scope", default=None)


class ScopeError(Exception):
    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


def configured_tenant() -> tuple[str | None, str | None]:
    return ((config.env("TEST_BOOTSTRAP_COMPANY_ID") or "").strip() or None,
            (config.env("TEST_BOOTSTRAP_EMAIL_ALIAS") or "").strip().lower() or None)


async def guard(company_id: str) -> dict:
    """The company the run may touch, or ScopeError."""
    cid, alias = configured_tenant()
    if not cid or not alias:
        raise ScopeError("Scoped finance runs need TEST_BOOTSTRAP_COMPANY_ID and TEST_BOOTSTRAP_EMAIL_ALIAS.")
    if company_id != cid:
        raise ScopeError("Scoped finance runs are only for the configured test tenant.", 403)
    company = await db.companies.raw.find_one({"id": company_id}, NO_ID)
    if not company:
        raise ScopeError("Company not found", 404)
    if company.get("test_tenant") is not True:
        raise ScopeError("This company is not a test tenant.", 403)
    if not await db.users.find_one({"email": alias, "company_id": company_id}, {"_id": 1}):
        raise ScopeError("The test tenant's e-mail alias is not a user of this company.")
    return {"company": company, "alias": alias}


def parse_as_of(value: str | None) -> date | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ScopeError("as_of must be YYYY-MM-DD", 400) from None
    today = billing.real_today_fi()
    if d < today or d > today + timedelta(days=MAX_AHEAD_DAYS):
        raise ScopeError(f"as_of must be between today and {MAX_AHEAD_DAYS} days ahead", 400)
    return d


class scoped:
    """`async with scoped(company_id, alias, as_of):` - mail and dates for one scoped run."""

    def __init__(self, company_id: str, alias: str, as_of: date | None = None):
        self.state = {"company_id": company_id, "alias": alias, "as_of": as_of}

    async def __aenter__(self):
        self._t1 = _scope.set(self.state)
        self._t2 = billing.today_override.set(self.state["as_of"])
        return self.state

    async def __aexit__(self, *exc):
        billing.today_override.reset(self._t2)
        _scope.reset(self._t1)
        return False


async def outbound_hook(mail: dict) -> dict:
    """Test tenant mail goes to its alias only; inside a scoped run, nothing goes to any other company."""
    cid, alias = configured_tenant()
    st = _scope.get()
    company = mail.get("company_id")
    if st and company != st["company_id"]:
        mail["suppress"] = "outside_test_tenant_scope"
        return mail
    if cid and alias and company == cid:
        if (mail.get("to") or "").strip().lower() != alias:
            mail.setdefault("headers", {})
            mail["headers"] = {**(mail.get("headers") or {}), "X-Huolitsija-Original-To": str(mail.get("to") or "")}
        mail["to"] = alias
        if not str(mail.get("subject") or "").startswith("[TEST]"):
            mail["subject"] = f"[TEST] {mail.get('subject') or ''}".strip()
    return mail


if outbound_hook not in core_email.OUTBOUND_HOOKS:
    core_email.OUTBOUND_HOOKS.append(outbound_hook)


async def _invoices(company_id: str) -> list[dict]:
    return await db.raw.invoices.find({"company_id": company_id, "kind": "invoice", "number": {"$ne": None},
                                       "status": {"$in": list(billing.OPEN_STATES)}}, NO_ID).sort("due_date", 1).to_list(200)


# --------------------------------------------------------------------------------------------- dunning (row 88)
async def run_dunning(company_id: str, *, actor: dict, as_of: date | None) -> dict:
    """One company's dunning, decided by the internal user who pressed the button (the owner's decision)."""
    from core.settings import get_settings
    from services import dunning

    g = await guard(company_id)
    out = {"company_id": company_id, "alias": g["alias"], "as_of": (as_of or billing.real_today_fi()).isoformat(),
           "simulated": as_of is not None, "done": [], "skipped": []}
    async with scoped(company_id, g["alias"], as_of):
        settings = await get_settings()
        today = billing.today_fi()
        for inv in await _invoices(company_id):
            due = billing.to_date(inv.get("due_date"))
            days = (today - due).days if due else None
            target = dunning.target_stage(days, settings) if days is not None else None
            current = (inv.get("dunning") or {}).get("stage")
            if not target or dunning._idx(current) >= dunning._idx(target):
                out["skipped"].append({"number": inv.get("number"), "days_past_due": days, "why": "not_due_or_done"})
                continue
            why = await dunning._blocked(inv)
            if why:
                out["skipped"].append({"number": inv.get("number"), "why": why})
                continue
            r = await dunning.execute_stage(inv["id"], target, expected_prev=current, actor=actor)
            if as_of and r.get("executed"):
                await db.raw.invoices.update_one({"id": inv["id"]}, {"$set": {"dunning.simulated_as_of": as_of.isoformat()}})
            out["done"].append({"invoice_id": inv["id"], "number": inv.get("number"), "days_past_due": days,
                                "stage": target, **r})
    await audit(actor, "test_tenant_dunning_run", "company", company_id,
                {"as_of": out["as_of"], "simulated": out["simulated"], "done": len(out["done"])}, company_id=company_id)
    return out


# --------------------------------------------------------------------------------------------- collections (row 89)
async def run_collections(company_id: str, *, actor: dict, as_of: date | None, send_demand: bool,
                          handover: bool = False) -> dict:
    from core.settings import get_settings
    from services import collections

    g = await guard(company_id)
    out = {"company_id": company_id, "alias": g["alias"], "as_of": (as_of or billing.real_today_fi()).isoformat(),
           "simulated": as_of is not None, "opened": [], "demands": [], "handover": [], "skipped": []}
    async with scoped(company_id, g["alias"], as_of):
        settings = await get_settings()
        today = billing.today_fi()
        for inv in await _invoices(company_id):
            reason = collections.entry_reason(inv, settings, today)
            if not reason:
                out["skipped"].append({"number": inv.get("number"), "why": "no_entry_reason"})
                continue
            case, created = await collections.open_case(inv, reason, actor=actor)
            if as_of and created:
                await db.collections_cases.update_one({"id": case["id"]}, {"$set": {"simulated_as_of": as_of.isoformat()}})
            out["opened"].append({"case_id": case["id"], "number": inv.get("number"), "reason": reason,
                                  "created": created, "stage": case.get("stage")})
            if send_demand and not (case.get("demand") or {}).get("sent_at"):
                out["demands"].append({"case_id": case["id"], **(await collections.send_demand(case["id"], actor=actor) or {})})
        if handover:
            # Same rule as the daily sweep: final demand sent, its agency window passed, nothing pending. The card goes
            # to the owner queue with the hand-over package; approving it hands the case to the agency (file mode).
            agency_days = int(settings.get("collections_agency_after_days") or 30)
            async for case in db.collections_cases.raw.find({"company_id": company_id, "stage": "final_demand",
                                                             "demand.sent_at": {"$ne": None}, "pending_action": None}, NO_ID):
                sent = billing.to_date(case["demand"]["sent_at"])
                if not sent or (today - sent).days < agency_days:
                    out["skipped"].append({"case_id": case["id"], "why": "agency_window_open",
                                           "days_since_demand": (today - sent).days if sent else None})
                    continue
                r = await collections.decide_handover(case)
                out["handover"].append({"case_id": case["id"], **r})
    await audit(actor, "test_tenant_collections_run", "company", company_id,
                {"as_of": out["as_of"], "opened": len(out["opened"]), "demands": len(out["demands"])}, company_id=company_id)
    return out


# --------------------------------------------------------------------------------------------- camt.054 (row 90)
async def import_camt054(company_id: str, data: bytes, filename: str, *, actor: dict, book: bool) -> dict:
    """A camt.054 whose every credit line pays an invoice of this company: stored and imported now, in scope."""
    from core.approvals import cancel_approvals
    from services import reconciliation as rc

    g = await guard(company_id)
    parsed = rc.parse_camt054(data)  # ValueError -> 400
    if not parsed["entries"]:
        raise ScopeError("The file has no entries.", 400)
    for e in parsed["entries"]:
        norm = rc.normalise_reference(e["reference"])
        inv = await rc.find_invoice_by_reference(norm["reference"], norm["rf"]) if e["credit_debit"] == "credit" else None
        if not inv or inv.get("company_id") != company_id:
            raise ScopeError(f"Entry {e['archive_ref']} does not pay an invoice of this company.", 400)
    async with scoped(company_id, g["alias"], None):
        st = await rc.store_statement(data, filename or "camt054-test.xml", "upload", actor)
        await db.raw.bank_statements.update_one({"id": st["id"]}, {"$set": {"company_id": company_id, "test": True, "scope": "test_tenant"}})
        res = await rc.camt054_import_job({"payload": {"statement_id": st["id"]}})
        booked = []
        if book:
            refs = [e["archive_ref"] for e in parsed["entries"]]
            async for tx in db.raw.bank_transactions.find({"archive_ref": {"$in": refs}, "status": "unmatched",
                                                           "match_kind": {"$in": ["exact", "partial"]}}, NO_ID):
                inv = await db.raw.invoices.find_one({"id": tx["invoice_id"], "company_id": company_id}, NO_ID)
                if not inv:
                    continue
                r = await rc._book(tx, inv, actor=actor, decision_id=tx.get("decision_id"), approval_id=tx.get("approval_id"))
                if tx.get("approval_id"):
                    await cancel_approvals(tx["id"])
                booked.append({"transaction_id": tx["id"], "archive_ref": tx["archive_ref"], **r})
    txs = await db.raw.bank_transactions.find({"statement_id": st["id"]}, NO_ID).to_list(100)
    await audit(actor, "test_tenant_camt054_import", "bank_statement", st["id"],
                {"entries": len(parsed["entries"]), "booked": len(booked)}, company_id=company_id)
    return {"company_id": company_id, "statement_id": st["id"], "import": res, "booked": booked, "transactions": txs,
            "imported_at": now_iso()}


# --------------------------------------------------------------------------------------------- FX revaluation (row 87)
async def revalue(company_id: str, *, actor: dict, as_of: date) -> dict:
    from services import fx_revaluation

    await guard(company_id)
    doc = await fx_revaluation.revalue(as_of, company_id=company_id)
    await audit(actor, "test_tenant_fx_revaluation", "fx_revaluation", doc["id"],
                {"as_of": doc["as_of"], "items": len(doc["items"])}, company_id=company_id)
    return doc
