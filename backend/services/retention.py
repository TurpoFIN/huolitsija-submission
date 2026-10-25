"""Retention sweep: delete or anonymise records whose legal or operational retention period has ended.

    from services import retention
    await retention.run_sweep(dry_run=True, actor=user)   # what would go
    retention.policy_table(settings)                      # the documented policy (GET /api/compliance/retention/policy)

Runs daily at 03:30 Helsinki as `compliance.retention_sweep` (core.compliance_jobs: s2 durable job when merged, else a
lifecycle loop); `autopilot_halted` (kill switch) skips the scheduled run. A person can run it from ops at any time.

Policy (periods from core/settings.py; a setting can lengthen, never shorten, a legal minimum):
| key                      | records                                   | kept                                   | basis |
| invoices                 | invoices, credit notes, card payments,    | record_retention_years (>= 6) from the | Kirjanpitolaki 2:10 § 2 mom. |
|                          | Stripe events, s13 carrier invoices/      | end of the year of the latest event;   | (vouchers) |
|                          | payables/payment runs                     | open receivables are never deleted     | |
| books                    | s13 ledger_entries, ledger_exports,       | retention_financial_statements_years   | Kirjanpitolaki 2:10 § 1 mom. |
|                          | fx_revaluations, financial_statements     | (>= 10) from year end                  | |
| customs_records          | customs declarations, shipment documents  | max(3, record_retention_years)         | UCC 952/2013 art. 51, tullilaki |
| powers_of_attorney       | companies.poa / poa_history + POA PDF     | same, from the end of validity/revoke  | same |
| shipments                | delivered/cancelled shipments + tracking, | max(customs, record) years; kept while | transport + customs record |
|                          | notes, exceptions                         | an invoice or declaration still exists | |
| claims                   | closed claims                             | record_retention_years (>= 3)          | Vanhentumislaki 4 §, KPL 2:10 § |
| messages                 | threads + messages                        | record years after the shipment / last | correspondence on business |
|                          |                                           | message                                | transactions (KPL 2:10 §) |
| notifications            | in-app notifications                      | record years from year end             | follows messages |
| email_outbox             | sent-mail log (privacy code mails: 1 day) | record years from year end             | follows messages |
| audit_log                | audit rows                                | record years from year end             | accountability (GDPR 5(2)) |
| kyc                      | companies.kyc, UBOs, signatory            | retention_kyc_years_after_relationship | legitimate interest; AML Act |
|                          |                                           | (5) after the relationship ended       | 444/2017 3:3 as the model |
| quotes_unconverted       | quotes never booked                       | retention_quote_unconverted_months (24)| no further purpose |
| public_anonymous_quotes  | unclaimed instant quotes                  | 30 days (claim link expiry)            | no further purpose |
| tokens                   | sign-in/reset/verify/invite links         | retention_token_days (7) after expiry  | security |
| privacy_requests         | codes and download tokens: at expiry;     | retention_privacy_request_years (3)    | proof of handling a request |
|                          | request rows                              | from year end                          | (GDPR 5(2), 12) |
Year-based legal periods count from 31 Dec of the event year (financial year = calendar year); month/day periods count
from the event. Legal holds (services.gdpr) and any row whose own `retention.until` is still ahead are never touched.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from core.audit import agent_actor, audit
from core.compliance_jobs import autopilot_halted, recurring
from core.settings import get_settings
from core.util import iso, new_id, now, now_iso
from services.gdpr import BASES, basis_years, hold_hits, raw, active_holds

log = logging.getLogger("compliance.retention")
BATCH = 5000
OPEN_INVOICE = ["issued", "sent", "overdue", "partially_paid", "reminded", "in_collection"]
PUBLIC_QUOTE_DAYS = 30  # routes/public_quote.CLAIM_DAYS: the claim link's life

POLICIES = [
    {"key": "invoices", "basis": "bookkeeping_voucher", "fi": "Laskut, hyvityslaskut ja maksut",
     "en": "Invoices, credit notes and payments",
     "collections": ["invoices", "payment_transactions", "stripe_events", "carrier_invoices", "payables", "payment_runs"]},
    {"key": "books", "basis": "bookkeeping_books", "fi": "Kirjanpito ja tilinpäätökset", "en": "Books and financial statements",
     "collections": ["ledger_entries", "ledger_exports", "fx_revaluations", "financial_statements"]},
    {"key": "customs_records", "basis": "customs_records", "fi": "Tulli-ilmoitukset ja lähetysasiakirjat",
     "en": "Customs declarations and shipment documents", "collections": ["customs_declarations", "documents"]},
    {"key": "powers_of_attorney", "basis": "customs_records", "fi": "Tullausvaltakirjat", "en": "Customs powers of attorney",
     "collections": ["companies", "documents"]},
    {"key": "claims", "basis": "legal_claims", "fi": "Suljetut korvausvaatimukset", "en": "Closed claims",
     "collections": ["claims"]},
    {"key": "shipments", "basis": "customs_records", "fi": "Päättyneet lähetykset", "en": "Finished shipments",
     "collections": ["shipments", "tracking_events", "position_reports", "shipment_notes", "exceptions"]},
    {"key": "messages", "basis": "bookkeeping_voucher", "fi": "Viestit", "en": "Messages", "collections": ["threads", "messages"]},
    {"key": "notifications", "basis": "bookkeeping_voucher", "fi": "Ilmoitukset", "en": "Notifications",
     "collections": ["notifications"]},
    {"key": "email_outbox", "basis": "bookkeeping_voucher", "fi": "Lähetetyt sähköpostit", "en": "Sent e-mail log",
     "collections": ["email_outbox"]},
    {"key": "audit_log", "basis": "bookkeeping_voucher", "fi": "Tapahtumaloki", "en": "Audit log", "collections": ["audit_log"]},
    {"key": "kyc", "basis": "kyc_due_diligence", "fi": "Asiakkaan tunnistustiedot (KYC)", "en": "Customer due diligence (KYC)",
     "collections": ["companies"]},
    {"key": "quotes_unconverted", "basis": None, "setting": "retention_quote_unconverted_months", "unit": "months",
     "fi": "Varaamattomat tarjoukset", "en": "Quotes never booked", "collections": ["quotes"],
     "why_fi": "Tarjous, josta ei tullut varausta, ei ole kirjanpitoaineistoa; sitä ei tarvita enää.",
     "why_en": "A quote that was never booked is not bookkeeping material; there is no purpose to keep it."},
    {"key": "public_anonymous_quotes", "basis": None, "days": PUBLIC_QUOTE_DAYS, "unit": "days",
     "fi": "Lunastamattomat pikatarjoukset", "en": "Unclaimed instant quotes", "collections": ["quotes"],
     "why_fi": "Nimetön pikatarjous poistetaan, kun sen lunastuslinkki vanhenee.",
     "why_en": "An anonymous instant quote is deleted when its claim link expires."},
    {"key": "tokens", "basis": None, "setting": "retention_token_days", "unit": "days after expiry",
     "fi": "Kirjautumis- ja vahvistuslinkit", "en": "Sign-in and verification links", "collections": ["tokens"],
     "why_fi": "Vanhentunutta linkkiä ei voi käyttää; säilytetään hetki väärinkäytösten selvittämiseksi.",
     "why_en": "An expired link cannot be used; kept briefly to investigate misuse."},
    {"key": "privacy_requests", "basis": None, "setting": "retention_privacy_request_years", "unit": "years from year end",
     "fi": "Tietosuojapyynnöt (koodit heti vanhentuessa)", "en": "Data-protection requests (codes at expiry)",
     "collections": ["privacy_requests", "email_outbox"],
     "why_fi": "Pyynnön käsittely on voitava osoittaa (GDPR 5(2) ja 12 art.).",
     "why_en": "Handling of the request must be provable (GDPR art. 5(2), 12)."},
]


def _years(p: dict, settings: dict) -> int | None:
    return basis_years(p["basis"], settings) if p.get("basis") else None


def policy_table(settings: dict) -> list[dict]:
    out = []
    for p in POLICIES:
        if p.get("basis"):
            years = _years(p, settings)
            if p["key"] == "shipments":
                years = max(years, basis_years("bookkeeping_voucher", settings))
            period = {"value": years, "unit": "years after the end of the event year" if p["key"] != "kyc"
                      else "years after the end of the year the customer relationship ended"}
            basis_fi, basis_en = BASES[p["basis"]]["fi"], BASES[p["basis"]]["en"]
        else:
            period = {"value": p.get("days") or settings.get(p["setting"]), "unit": p["unit"]}
            basis_fi, basis_en = p["why_fi"], p["why_en"]
        out.append({"key": p["key"], "label_fi": p["fi"], "label_en": p["en"], "collections": p["collections"],
                    "period": period, "basis_key": p.get("basis"), "basis_fi": basis_fi, "basis_en": basis_en})
    return out


def year_cut(years: int, today: date | None = None) -> str:
    """Records whose latest event is before this date have passed 31 Dec of (event year + years)."""
    t = today or now().date()
    return date(t.year - int(years), 1, 1).isoformat()


def older(cut: str, *fields: str) -> dict:
    """Every listed event field (the first one must exist) is before `cut`: the latest event decides."""
    return {"$and": [{fields[0]: {"$lt": cut}}] + [{"$nor": [{f: {"$gte": cut}}]} for f in fields[1:]]}


def months_ago(n: int, at: datetime | None = None) -> str:
    t = at or now()
    y, m = divmod(t.month - 1 - int(n), 12)
    day = min(t.day, 28)
    return iso(t.replace(year=t.year + y, month=m + 1, day=day))


class Ctx:
    def __init__(self, settings: dict, holds: list[dict], dry_run: bool, at: datetime | None = None):
        self.settings, self.holds, self.dry_run = settings, holds, dry_run
        self.now = at or now()  # a dry-run preview may look ahead ("what would go on date X"); real runs use now
        self.today = self.now.date()
        self.stats: dict[str, dict] = {}

    def s(self, key: str) -> dict:
        return self.stats.setdefault(key, {"scanned": 0, "deleted": 0, "anonymised": 0, "held": 0, "errors": 0,
                                           "kept_by_row_retention": 0, "by_collection": {}})


def _own_retention_ahead(doc: dict, today: date) -> bool:
    """A row retained by an erasure carries its own end date. Legal-hold flags are judged by the live holds instead."""
    r = doc.get("retention") or {}
    return r.get("basis") != "legal_hold" and bool(r.get("until")) and r["until"] >= today.isoformat()


# The person a row is about, for subject_email legal holds (the other scopes match on ids in the row itself).
SUBJECT_FIELDS = {"email_outbox": "to", "privacy_requests": "email", "users": "email"}


def _subject(coll: str, doc: dict) -> str | None:
    v = doc.get(SUBJECT_FIELDS.get(coll, ""))
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


async def _sweep(ctx: Ctx, key: str, coll: str, query: dict, *, hold_view=None, after_delete=None, check=None):
    st = ctx.s(key)
    ids, files = [], []
    async for d in raw(coll).find(query).limit(BATCH):
        st["scanned"] += 1
        view = hold_view(d) if hold_view else d
        if hold_hits(coll, view, ctx.holds, _subject(coll, d)):
            st["held"] += 1
            continue
        if _own_retention_ahead(d, ctx.today):
            st["kept_by_row_retention"] += 1
            continue
        if check is not None and not await check(d):
            st["scanned"] -= 1  # not erasable yet for a reason outside this row (e.g. its shipment is still kept)
            continue
        ids.append(d["_id"])
        files.append(d)
    if ids and not ctx.dry_run:
        try:
            await raw(coll).delete_many({"_id": {"$in": ids}})
            if after_delete:
                for d in files:
                    await after_delete(d)
        except Exception as exc:  # noqa: BLE001 - reported per policy, the sweep goes on
            log.exception("retention %s/%s failed", key, coll)
            st["errors"] += 1
            st.setdefault("error_messages", []).append(str(exc)[:300])
            return
    st["deleted"] += len(ids)
    st["by_collection"][coll] = st["by_collection"].get(coll, 0) + len(ids)


async def _delete_gridfs(d: dict):
    from core.storage import delete_file

    for f in ("file_id", "pdf_file_id"):
        if d.get(f):
            await delete_file(d[f])


# ------------------------------------------------------------------------------------------------ policies
async def p_invoices(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_voucher", ctx.settings), ctx.today)
    await _sweep(ctx, "invoices", "invoices",
                 {**older(cut, "created_at", "issue_date", "supply_date", "paid_at", "updated_at"),
                  "status": {"$nin": OPEN_INVOICE}}, after_delete=_delete_gridfs)
    await _sweep(ctx, "invoices", "payment_transactions", older(cut, "created_at", "updated_at"))
    await _sweep(ctx, "invoices", "stripe_events", older(cut, "received_at"))
    await _sweep(ctx, "invoices", "carrier_invoices", {**older(cut, "created_at", "invoice_date"),
                                                       "status": {"$nin": ["received", "needs_review", "approved", "scheduled"]}},
                 after_delete=_delete_gridfs)
    await _sweep(ctx, "invoices", "payables", {**older(cut, "created_at", "paid_at"), "status": {"$in": ["paid", "rejected", "cancelled"]}})
    await _sweep(ctx, "invoices", "payment_runs", older(cut, "created_at", "execution_date"), after_delete=_delete_gridfs)


async def p_books(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_books", ctx.settings), ctx.today)
    await _sweep(ctx, "books", "ledger_entries", older(cut, "date"))
    await _sweep(ctx, "books", "ledger_exports", older(cut, "created_at", "to"), after_delete=_delete_gridfs)
    await _sweep(ctx, "books", "fx_revaluations", older(cut, "as_of"))
    await _sweep(ctx, "books", "financial_statements", older(cut, "period_end", "created_at"), after_delete=_delete_gridfs)


async def p_customs(ctx: Ctx):
    cut = year_cut(basis_years("customs_records", ctx.settings), ctx.today)
    await _sweep(ctx, "customs_records", "customs_declarations",
                 older(cut, "created_at", "updated_at", "released_at", "decided_at", "filed_at"))
    await _sweep(ctx, "customs_records", "documents",
                 {**older(cut, "uploaded_at"), "shipment_id": {"$ne": None}, "type": {"$ne": "power_of_attorney"}},
                 after_delete=_delete_gridfs)


async def p_poa(ctx: Ctx):
    cut = year_cut(basis_years("customs_records", ctx.settings), ctx.today)
    st = ctx.s("powers_of_attorney")

    def ended(p: dict) -> bool:
        end = (p or {}).get("revoked_at") or (p or {}).get("valid_to")
        return bool(end) and str(end) < cut

    async for c in raw("companies").find({"$or": [{"poa_history.0": {"$exists": True}}, {"poa.valid_to": {"$lt": cut}},
                                                  {"poa.revoked_at": {"$lt": cut}}]}).limit(BATCH):
        olds = [p for p in c.get("poa_history") or [] if ended(p)]
        cur = c.get("poa") if ended(c.get("poa")) else None
        if not olds and not cur:
            continue
        st["scanned"] += len(olds) + (1 if cur else 0)
        if hold_hits("companies", c, ctx.holds):
            st["held"] += len(olds) + (1 if cur else 0)
            continue
        doc_ids = [p.get("document_id") for p in olds + ([cur] if cur else []) if p.get("document_id")]
        if not ctx.dry_run:
            upd: dict = {"$set": {"poa_history": [p for p in c.get("poa_history") or [] if not ended(p)]}}
            if cur:
                upd["$set"]["poa"] = None
            await raw("companies").update_one({"_id": c["_id"]}, upd)
            async for d in raw("documents").find({"id": {"$in": doc_ids}}):
                await raw("documents").delete_one({"_id": d["_id"]})
                await _delete_gridfs(d)
        st["deleted"] += len(olds) + (1 if cur else 0)
        st["by_collection"]["companies.poa"] = st["by_collection"].get("companies.poa", 0) + len(olds) + (1 if cur else 0)


async def p_claims(ctx: Ctx):
    cut = year_cut(basis_years("legal_claims", ctx.settings), ctx.today)
    await _sweep(ctx, "claims", "claims", {**older(cut, "created_at", "updated_at", "closed_at", "paid_at"),
                                           "status": {"$in": ["closed", "paid", "rejected", "withdrawn", "settled"]}})


async def p_shipments(ctx: Ctx):
    years = max(basis_years("customs_records", ctx.settings), basis_years("bookkeeping_voucher", ctx.settings))
    cut = year_cut(years, ctx.today)

    async def no_records(s: dict) -> bool:
        return not (await raw("invoices").find_one({"shipment_id": s["id"]}, {"_id": 1})
                    or await raw("customs_declarations").find_one({"shipment_id": s["id"]}, {"_id": 1}))

    async def children(s: dict):
        for c in ("tracking_events", "position_reports", "shipment_notes", "exceptions"):
            await raw(c).delete_many({"shipment_id": s["id"]})

    await _sweep(ctx, "shipments", "shipments",
                 {**older(cut, "created_at", "updated_at", "delivered_at"), "status": {"$in": ["delivered", "cancelled", "closed"]}},
                 check=no_records, after_delete=children)


async def p_messages(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_voucher", ctx.settings), ctx.today)

    async def shipment_done(t: dict) -> bool:
        if not t.get("shipment_id"):
            return True
        s = await raw("shipments").find_one({"id": t["shipment_id"]}, {"_id": 0, "updated_at": 1, "delivered_at": 1})
        return not s or max(str(s.get("updated_at") or ""), str(s.get("delivered_at") or "")) < cut

    async def msgs(t: dict):
        await raw("messages").delete_many({"thread_id": t["id"]})

    await _sweep(ctx, "messages", "threads", older(cut, "created_at", "last_message_at"), check=shipment_done, after_delete=msgs)
    await _sweep(ctx, "messages", "messages", {**older(cut, "created_at"), "thread_id": None})


async def p_notifications(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_voucher", ctx.settings), ctx.today)
    await _sweep(ctx, "notifications", "notifications", older(cut, "created_at"))


async def p_outbox(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_voucher", ctx.settings), ctx.today)
    await _sweep(ctx, "email_outbox", "email_outbox", older(cut, "created_at"))


def _audit_view(a: dict) -> dict:
    ent, eid = a.get("entity"), a.get("entity_id")
    return {"company_id": a.get("company_id"), "shipment_id": eid if ent == "shipment" else None,
            "invoice_id": eid if ent == "invoice" else None,
            "declaration_id": eid if ent in ("customs", "customs_declaration") else None}


async def p_audit(ctx: Ctx):
    cut = year_cut(basis_years("bookkeeping_voucher", ctx.settings), ctx.today)
    await _sweep(ctx, "audit_log", "audit_log", older(cut, "at"), hold_view=_audit_view)


async def p_kyc(ctx: Ctx):
    years = basis_years("kyc_due_diligence", ctx.settings)
    cut = year_cut(years, ctx.today)
    st = ctx.s("kyc")
    q = {"created_at": {"$lt": cut}, "kyc_purged_at": None,
         "$or": [{"kyc": {"$nin": [None, {}]}}, {"ubos.0": {"$exists": True}}, {"signatory": {"$nin": [None, {}]}}]}
    async for c in raw("companies").find(q).limit(BATCH):
        last = [str(c.get("created_at") or "")]
        for coll, f in (("shipments", "created_at"), ("invoices", "created_at"), ("quotes", "created_at")):
            d = await raw(coll).find_one({"company_id": c["id"]}, {"_id": 0, f: 1}, sort=[(f, -1)])
            last.append(str((d or {}).get(f) or ""))
        u = await raw("users").find_one({"company_id": c["id"]}, {"_id": 0, "last_login_at": 1}, sort=[("last_login_at", -1)])
        last.append(str((u or {}).get("last_login_at") or ""))
        if max(last) >= cut:
            continue  # the relationship ended less than `years` full years ago (or is still active)
        st["scanned"] += 1
        if hold_hits("companies", c, ctx.holds):
            st["held"] += 1
            continue
        if not ctx.dry_run:
            await raw("companies").update_one({"_id": c["_id"]}, {
                "$set": {"kyc": None, "kyc_checklist": None, "ubos": [], "signatory": None, "kyc_status": "not_started",
                         "kyc_purged_at": now_iso(), "kyc_purge_basis": "kyc_due_diligence"}})
        st["anonymised"] += 1
        st["by_collection"]["companies.kyc"] = st["by_collection"].get("companies.kyc", 0) + 1


async def purge_anonymous_public_quotes(ctx: Ctx | None = None) -> int:
    """Unclaimed anonymous instant quotes go when their claim link expires (moved here from routes/quotes.expire_quotes,
    which still calls this every 15 minutes)."""
    q = {"source": "public", "company_id": None, "claim_expires_at": {"$lt": iso(ctx.now) if ctx else now_iso()}}
    if ctx is None:
        res = await raw("quotes").delete_many(q)
        return res.deleted_count
    await _sweep(ctx, "public_anonymous_quotes", "quotes", q)
    return ctx.s("public_anonymous_quotes")["deleted"]


async def p_quotes(ctx: Ctx):
    months = int(ctx.settings.get("retention_quote_unconverted_months") or 24)
    cut = months_ago(months, ctx.now)
    await _sweep(ctx, "quotes_unconverted", "quotes",
                 {**older(cut, "created_at", "updated_at"), "shipment_id": None, "status": {"$nin": ["booked", "booking"]},
                  "$nor": [{"source": "public", "company_id": None}]})
    await purge_anonymous_public_quotes(ctx)


async def p_tokens(ctx: Ctx):
    days = int(ctx.settings.get("retention_token_days") or 7)
    await _sweep(ctx, "tokens", "tokens", {"expires_at": {"$lt": iso(ctx.now - timedelta(days=days))}})


async def p_privacy(ctx: Ctx):
    st = ctx.s("privacy_requests")
    n = iso(ctx.now)
    for field, exp in (("code_hash", "code_expires_at"), ("download_jti", "download_expires_at")):
        q = {field: {"$ne": None}, exp: {"$lt": n}}
        cnt = await raw("privacy_requests").count_documents(q)
        st["scanned"] += cnt
        if cnt and not ctx.dry_run:
            await raw("privacy_requests").update_many(q, {"$set": {field: None}})
        st["anonymised"] += cnt
    await _sweep(ctx, "privacy_requests", "email_outbox",
                 {"tag": {"$regex": "^privacy_code"}, "created_at": {"$lt": iso(ctx.now - timedelta(days=1))}})
    years = int(ctx.settings.get("retention_privacy_request_years") or 3)
    await _sweep(ctx, "privacy_requests", "privacy_requests", older(year_cut(years, ctx.today), "created_at", "completed_at"))


RUNNERS = {"invoices": p_invoices, "books": p_books, "customs_records": p_customs, "powers_of_attorney": p_poa,
           "claims": p_claims, "shipments": p_shipments, "messages": p_messages, "notifications": p_notifications,
           "email_outbox": p_outbox, "audit_log": p_audit, "kyc": p_kyc, "quotes_unconverted": p_quotes,
           "public_anonymous_quotes": None, "tokens": p_tokens, "privacy_requests": p_privacy}


async def run_sweep(*, dry_run: bool = False, actor: dict | None = None, trigger: str = "schedule",
                    only: list[str] | None = None, as_of: datetime | None = None) -> dict:
    """as_of: dry runs only - which rows the sweep would remove, keep by row retention or hold on that date, with the
    legal holds active now."""
    if as_of is not None and not dry_run:
        raise ValueError("A look-ahead date is only allowed for a dry run")
    if trigger == "schedule" and await autopilot_halted():
        return {"skipped": "autopilot_halted"}
    settings = await get_settings()
    ctx = Ctx(settings, await active_holds(), dry_run, as_of)
    actor = actor or agent_actor("compliance")
    run = {"id": new_id(), "started_at": now_iso(), "finished_at": None, "dry_run": bool(dry_run), "trigger": trigger,
           "actor": {k: actor.get(k) for k in ("type", "id", "name")}, "holds_active": len(ctx.holds), "per_policy": {},
           "as_of": iso(as_of) if as_of else None}
    await raw("retention_runs").insert_one(dict(run))
    for key, fn in RUNNERS.items():
        if fn is None or (only and key not in only):
            continue
        try:
            await fn(ctx)
        except Exception as exc:  # noqa: BLE001 - one policy failing must not stop the others
            log.exception("retention policy %s failed", key)
            ctx.s(key)["errors"] += 1
            ctx.s(key).setdefault("error_messages", []).append(str(exc)[:300])
    totals = {k: sum(v[k] for v in ctx.stats.values()) for k in ("scanned", "deleted", "anonymised", "held", "errors")}
    run.update(per_policy=ctx.stats, totals=totals, finished_at=now_iso())
    await raw("retention_runs").update_one({"id": run["id"]}, {"$set": {"per_policy": ctx.stats, "totals": totals,
                                                                        "finished_at": run["finished_at"]}})
    for key, st in ctx.stats.items():
        if st["deleted"] or st["anonymised"] or st["held"] or st["errors"]:
            await audit(actor, "retention_dry_run" if dry_run else "retention_applied", "retention_policy", key,
                        {"run_id": run["id"], **{k: st[k] for k in ("scanned", "deleted", "anonymised", "held", "errors")},
                         "by_collection": st["by_collection"]})
    await audit(actor, "retention_sweep", "retention_run", run["id"], {"dry_run": dry_run, "trigger": trigger, **totals})
    return run


@recurring("compliance.retention_sweep", daily_at="03:30", label_fi="Säilytysajat", label_en="Retention sweep")
async def retention_sweep(j: dict | None = None) -> dict:
    run = await run_sweep(trigger="schedule")
    return {"run_id": run.get("id"), "totals": run.get("totals"), "skipped": run.get("skipped")}
