"""GDPR data-subject rights: access + portability (art. 15, 20), erasure (art. 17) and restriction (art. 18).

    from services import gdpr
    bundle = await gdpr.export_subject("matti@example.fi")          # machine-readable JSON, per collection
    data   = await gdpr.export_zip("matti@example.fi")              # bytes: export.json + README + uploaded files
    result = await gdpr.erase_subject("matti@example.fi", actor)    # what was erased / anonymised / retained, and why

Design
- A data subject is an e-mail address. `PERSONAL_DATA` below is the declarative inventory of every collection that can
  hold their data (built by reading every `db.<collection>` use in backend/), with how their rows are found, what the
  personal fields mean, and what erasure does: `delete`, `anonymise` (identifiers scrubbed, business record kept) or
  `retain` (a law requires the record: it is minimised, gets `retention: {basis, until}` and
  `processing_restricted: true`, art. 18). Collections owned by other branches (s3 partner_*, s6 email_*, s13 ledger) are
  listed too; querying a collection that does not exist returns nothing.
- Anonymising is one recursive scrub (`scrub`): a dict whose personal e-mail key matches the subject loses its person
  keys (name, e-mail, phone, title); an actor ref {id in the subject's user ids} becomes "Poistettu käyttäjä"; the
  subject's e-mail inside free text becomes "[erased]". Company names, business ids and amounts stay: they are the
  company's (a legal person's) data and bookkeeping content, not the subject's.
- Retention periods count from the END of the calendar year of the event (Kirjanpitolaki 2:10 §; Huolitsija's
  financial year is the calendar year). `until` is the last day the record must be kept; the next day it is erasable,
  and services.retention deletes it.
- Legal holds (`legal_holds`, scope company | shipment | invoice | customs_declaration | subject_email) win over
  erasure and retention: rows in scope are not changed, only flagged restricted. A held user still loses sign-in.
- Everything reads and writes the raw collections (`db.raw`), so an ops request never hides test tenants from the
  erasure, and every step is audit-logged with the e-mail as a hash (the audit log outlives the request).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from core import config
from core.audit import actor_ref, audit
from core.db import db
from core.settings import get_settings
from core.util import new_id, now, now_iso, parse_iso

ERASED_NAME = "Poistettu käyttäjä / Erased user"
ERASED_TEXT = "[Poistettu tietosuojapyynnöstä / Erased on a data-protection request]"
ERASED_TOKEN = "[erased]"
EXPORT_FORMAT = "huolitsija.gdpr-export/1"
MAX_EXPORT_FILE_BYTES = 50 * 1024 * 1024
LIST_CAP = 2000

# ------------------------------------------------------------------------------------------------ legal bases
BASES: dict[str, dict] = {
    "bookkeeping_voucher": {
        "fi": "Kirjanpitolaki 1336/1997 2 luku 10 § 2 mom.: tositteet ja liiketapahtumia koskeva kirjeenvaihto säilytetään "
              "vähintään 6 vuotta sen vuoden lopusta, jona tilikausi päättyi. GDPR 17(3)(b) ja 6(1)(c).",
        "en": "Finnish Accounting Act 1336/1997 ch. 2 s. 10(2): vouchers and correspondence on business transactions are "
              "kept at least 6 years from the end of the financial year. GDPR art. 17(3)(b), 6(1)(c).",
        "setting": "record_retention_years", "min_years": 6},
    "bookkeeping_books": {
        "fi": "Kirjanpitolaki 2 luku 10 § 1 mom.: tilinpäätös, toimintakertomus, päivä- ja pääkirja säilytetään vähintään "
              "10 vuotta tilikauden päättymisestä.",
        "en": "Accounting Act ch. 2 s. 10(1): financial statements, journals and general ledger are kept at least 10 years "
              "from the end of the financial year.",
        "setting": "retention_financial_statements_years", "min_years": 10},
    "customs_records": {
        "fi": "Unionin tullikoodeksi (EU) N:o 952/2013 51 art. (vähintään 3 vuotta) ja tullilaki 304/2016; Huolitsija "
              "säilyttää tulliasiakirjat ja valtakirjat asetuksen record_retention_years mukaan (oletus 6 v vuoden lopusta).",
        "en": "Union Customs Code Reg. (EU) 952/2013 art. 51 (at least 3 years) and Finnish Customs Act 304/2016; Huolitsija "
              "keeps customs records and powers of attorney for record_retention_years (default 6) from the end of the year.",
        "setting": "record_retention_years", "min_years": 3},
    "legal_claims": {
        "fi": "Vanhentumislaki 728/2003 4 § (3 v), NSAB 2015 / CMR -vastuuajat ja kirjanpitolaki 2:10 § (korvausten "
              "kirjeenvaihto): korvausvaatimukset säilytetään record_retention_years (vähintään 3 v) vuoden lopusta. "
              "GDPR 17(3)(e).",
        "en": "Finnish Limitation Act 728/2003 s. 4 (3 years), NSAB 2015 / CMR claim periods and Accounting Act ch. 2 s. 10 "
              "(settlement correspondence): claims are kept record_retention_years (at least 3) from the end of the year. "
              "GDPR art. 17(3)(e).",
        "setting": "record_retention_years", "min_years": 3},
    "kyc_due_diligence": {
        "fi": "Oikeutettu etu (GDPR 6(1)(f)) osoittaa pakotteiden ja vientivalvonnan huolellisuus (EU 833/2014, 765/2006, "
              "pakotelaki 659/1967). Säilytysaika 5 v asiakassuhteen päättymisestä rahanpesulain 444/2017 3:3 §:n mallin "
              "mukaan; huolitsija ei ole rahanpesulain 1:2 §:n ilmoitusvelvollinen, joten aika on valittu, ei pakollinen.",
        "en": "Legitimate interest (GDPR 6(1)(f)) in proving sanctions and export-control due diligence (EU 833/2014, "
              "765/2006, Finnish Sanctions Act 659/1967). Kept 5 years after the customer relationship ends, modelled on "
              "the AML Act 444/2017 ch. 3 s. 3; a forwarder is not an AML obliged entity, so the period is chosen, not required.",
        "setting": "retention_kyc_years_after_relationship", "min_years": 5},
    "objection_suppression": {
        "fi": "Kielto-/opt-out-merkintä säilytetään, jotta kieltoa voidaan noudattaa (GDPR 21(3), 17(3)(b)).",
        "en": "An opt-out entry is kept so the objection can be honoured (GDPR art. 21(3), 17(3)(b)).",
        "years": None},
    "legal_hold": {
        "fi": "Lakisääteinen säilytysvelvoite / oikeudellinen vaatimus (GDPR 17(3)(e), 18(1)(c)): tiedot on jäädytetty.",
        "en": "Legal hold for a legal claim or authority request (GDPR art. 17(3)(e), 18(1)(c)): records are frozen.",
        "years": None},
}


def basis_years(key: str, settings: dict) -> int | None:
    """Years a record is kept after the end of its event year. A setting can lengthen, never shorten, the legal minimum."""
    b = BASES[key]
    if b.get("setting"):
        return max(int(b["min_years"]), int(settings.get(b["setting"]) or b["min_years"]))
    return b.get("years")


def year_end_plus(event: Any, years: int | None) -> str | None:
    """Last day a record must be kept: 31 Dec of (event year + years). None = no end date."""
    if years is None:
        return None
    d = parse_iso(event) if isinstance(event, str) else event
    if d is None:
        d = now()
    return date(d.year + int(years), 12, 31).isoformat()


def first_event(doc: dict, *paths: str) -> str | None:
    for p in paths:
        v = get_path(doc, p)
        if v:
            return v if isinstance(v, str) else str(v)
    return None


def get_path(doc: dict, path: str):
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def email_hash(email: str) -> str:
    return hashlib.sha256(f"huolitsija-privacy:{(email or '').strip().lower()}".encode()).hexdigest()


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, dom = email.split("@", 1)
    return f"{local[:1]}***@{dom}"


def raw(name: str):
    """Raw Motor collection: no ops test-tenant filter (a privacy request covers test tenants too)."""
    return db.raw[name]


# ------------------------------------------------------------------------------------------------ legal holds
HOLD_SCOPES = ("company", "shipment", "invoice", "customs_declaration", "subject_email")


async def active_holds() -> list[dict]:
    today = now_iso()
    return await raw("legal_holds").find(
        {"released_at": None, "$or": [{"until": None}, {"until": {"$gte": today[:10]}}]}, {"_id": 0}).to_list(None)


def hold_hits(coll: str, doc: dict, holds: list[dict], subject_email: str | None = None) -> list[dict]:
    """Holds whose scope covers `doc` in `coll`."""
    out = []
    for h in holds:
        s, ref = h.get("scope"), h.get("ref_id")
        if not ref:
            continue
        hit = False
        if s == "subject_email":
            hit = bool(subject_email) and ref.strip().lower() == subject_email
        elif s == "company":
            hit = doc.get("company_id") == ref or (coll == "companies" and doc.get("id") == ref)
        elif s == "shipment":
            hit = doc.get("shipment_id") == ref or (coll == "shipments" and doc.get("id") == ref)
        elif s == "invoice":
            hit = (coll == "invoices" and ref in (doc.get("id"), doc.get("credit_note_for"))) or doc.get("invoice_id") == ref
        elif s == "customs_declaration":
            hit = ((coll == "customs_declarations" and doc.get("id") == ref) or doc.get("declaration_id") == ref
                   or ref in [u.get("declaration_id") for u in doc.get("used_by") or [] if isinstance(u, dict)]
                   or ref in (doc.get("customs_declaration_ids") or []))
        if hit:
            out.append(h)
    return out


async def create_hold(body: dict, actor: dict | None) -> dict:
    scope = (body.get("scope") or "").strip()
    if scope not in HOLD_SCOPES:
        raise ValueError(f"Scope must be one of {', '.join(HOLD_SCOPES)}")
    ref = (body.get("ref_id") or "").strip()
    if not ref:
        raise ValueError("Give the id (or e-mail) the hold covers")
    if scope == "subject_email":
        ref = ref.lower()
    reason = (body.get("reason") or "").strip()
    if len(reason) < 5:
        raise ValueError("Write the reason for the hold (at least 5 characters)")
    until = (body.get("until") or None)
    if until:
        try:
            until = date.fromisoformat(str(until)[:10]).isoformat()
        except ValueError:
            raise ValueError("Until must be a date (YYYY-MM-DD)") from None
    h = {"id": new_id(), "scope": scope, "ref_id": ref, "reason": reason[:1000],
         "basis": (body.get("basis") or "").strip()[:500] or BASES["legal_hold"]["en"],
         "created_by": actor_ref(actor), "created_at": now_iso(), "until": until, "released_at": None, "released_by": None}
    await raw("legal_holds").insert_one(dict(h))
    await audit(actor, "legal_hold_created", "legal_hold", h["id"],
                {"scope": scope, "ref_id": ref if scope != "subject_email" else email_hash(ref), "until": until})
    return h


async def release_hold(hold_id: str, actor: dict | None, note: str | None = None) -> dict | None:
    h = await raw("legal_holds").find_one_and_update(
        {"id": hold_id, "released_at": None},
        {"$set": {"released_at": now_iso(), "released_by": actor_ref(actor), "release_note": (note or "")[:1000] or None}},
        projection={"_id": 0}, return_document=True)
    if h:
        await audit(actor, "legal_hold_released", "legal_hold", hold_id, {"scope": h["scope"], "note": note})
    return h


# ------------------------------------------------------------------------------------------------ the subject
@dataclass
class Subject:
    email: str
    user_ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    company_ids: list[str] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)

    @property
    def rx(self) -> dict:
        return {"$regex": f"^{re.escape(self.email)}$", "$options": "i"}

    @property
    def ids(self) -> dict:
        return {"$in": self.user_ids or ["__none__"]}


async def load_subject(email: str) -> Subject:
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        raise ValueError("Give a valid e-mail address")
    s = Subject(email=e)
    async for u in raw("users").find({"email": {"$regex": f"^{re.escape(e)}$", "$options": "i"}}, {"_id": 0}):
        s.users.append(u)
        s.user_ids.append(u["id"])
        if (u.get("name") or "").strip():
            s.names.append(u["name"].strip())
        if len(re.sub(r"\D", "", u.get("phone") or "")) >= 6:
            s.phones.append(u["phone"])
        if u.get("company_id"):
            s.company_ids.append(u["company_id"])
    return s


def _or(*conds: dict | None) -> dict | None:
    conds = [c for c in conds if c]
    return {"$or": conds} if conds else None


def _contact_paths(prefixes: list[str], s: Subject) -> list[dict]:
    out = []
    for p in prefixes:
        out += [{f"{p}.email": s.rx}, {f"{p}.contact.email": s.rx}, {f"{p}.contact_email": s.rx}]
    return out


# ------------------------------------------------------------------------------------------------ the inventory
@dataclass(frozen=True)
class Spec:
    label_fi: str
    label_en: str
    found_by: str                                   # how a subject's rows are found (documentation)
    find: Callable[[Subject], dict | None]          # Mongo filter
    fields: dict                                    # personal fields: path -> meaning
    erase: str                                      # delete | anonymise | retain | keep
    basis: str | None = None                        # BASES key for retain
    event: tuple = ("created_at",)                  # retention counts from the end of this event's year
    minimise: tuple = ()                            # paths dropped from retained rows
    export_drop: tuple = ()                         # paths never exported (secrets, tipping-off)
    export: bool = True
    owner: str = "main"                             # branch that owns the collection


PARTY_PREFIXES = ["shipper", "consignee", "notify", "pickup", "delivery"]

PERSONAL_DATA: dict[str, Spec] = {
    "users": Spec(
        "Käyttäjätili", "User account", "users.email = subject",
        lambda s: {"email": s.rx},
        {"email": "sign-in e-mail", "name": "full name", "phone": "phone number", "locale": "language",
         "last_login_at": "last sign-in time", "terms_accepted_at": "terms acceptance", "role": "role in the company"},
        "anonymise", export_drop=("password_hash",)),
    "tokens": Spec(
        "Kirjautumis- ja vahvistuslinkit", "Sign-in, reset and verification links", "tokens.user_id in subject's users",
        lambda s: _or({"user_id": s.ids} if s.user_ids else None, {"data.email": s.rx}),
        {"kind": "link type", "created_at": "issued", "expires_at": "expiry", "used": "used or not"},
        "delete", export_drop=("token",)),
    "invites": Spec(
        "Kutsut", "Invitations", "invites.email = subject",
        lambda s: {"email": s.rx}, {"email": "invited e-mail", "invited_by_name": "who invited"}, "delete",
        export_drop=("token",)),
    "notifications": Spec(
        "Ilmoitukset", "In-app notifications", "notifications.user_id in subject's users",
        lambda s: {"user_id": s.ids} if s.user_ids else None,
        {"title": "notification title", "body": "notification text", "read": "read flag"}, "delete"),
    "email_outbox": Spec(
        "Lähetetyt sähköpostit", "E-mails sent to you", "email_outbox.to = subject",
        lambda s: {"to": s.rx}, {"to": "recipient", "subject": "subject line", "text": "message text"}, "delete",
        export_drop=("provider_response",)),
    "companies": Spec(
        "Yrityksen yhteys- ja allekirjoitustiedot", "Company contact and signature data",
        "billing_email / signatory.email / poa.signature.email = subject, or company created by the subject",
        lambda s: _or({"billing_email": s.rx}, {"signatory.email": s.rx}, {"poa.signature.email": s.rx},
                      {"poa_history.signature.email": s.rx}, {"contacts.email": s.rx}, {"contact.email": s.rx},
                      {"created_by": s.ids} if s.user_ids else None),
        {"billing_email": "invoicing e-mail", "signatory": "authorised signatory (name, title, e-mail)",
         "poa.signature": "power-of-attorney e-signature (typed name, e-mail, time, IP, browser)"},
        "anonymise", basis="customs_records", event=("poa.valid_to", "poa.revoked_at", "poa.signed_at"),
        export_drop=("kyc", "kyc_checklist", "ubos", "sanctions_status", "sanctions", "credit_limit_cents", "credit_hold",
                     "risk", "internal_note", "notes")),
    "quotes": Spec(
        "Tarjouspyynnöt ja varaukset", "Quotes and bookings",
        "requested_by / booking.created_by = subject's user, booking party or stop contact e-mail = subject, public "
        "quote e-mail = subject",
        lambda s: _or({"requested_by.id": s.ids} if s.user_ids else None,
                      {"booking.created_by.id": s.ids} if s.user_ids else None,
                      *_contact_paths([f"booking.{p}" for p in PARTY_PREFIXES], s),
                      {"email": s.rx}, {"contact_email": s.rx}, {"contact.email": s.rx}, {"request.contact.email": s.rx},
                      {"request.email": s.rx}),
        {"requested_by": "who asked", "booking.shipper|consignee|notify.contact": "party contact person",
         "booking.pickup|delivery.contact": "door contact", "request.notes": "free-text notes"},
        "anonymise", export_drop=("claim_token", "ip_hash", "approval", "ai", "margin", "costs")),
    "shipments": Spec(
        "Lähetykset", "Shipments", "created_by = subject's user or a party contact e-mail = subject",
        lambda s: _or({"created_by.id": s.ids} if s.user_ids else None,
                      *_contact_paths([f"parties.{p}" for p in ("shipper", "consignee", "notify")], s),
                      *_contact_paths(["pickup", "delivery", "booking.shipper", "booking.consignee"], s)),
        {"parties.*.contact": "party contact person", "created_by": "who booked", "milestones.by": "who updated"},
        "anonymise", export_drop=("internal", "costs", "margin", "position")),
    "shipment_notes": Spec(
        "Lähetysten sisäiset muistiinpanot (kirjoittaja)", "Internal shipment notes (as author)",
        "by.id = subject's user", lambda s: {"by.id": s.ids} if s.user_ids else None,
        {"by": "author", "text": "note text"}, "anonymise"),
    "threads": Spec(
        "Viestiketjut", "Message threads", "created_by.id = subject's user",
        lambda s: {"created_by.id": s.ids} if s.user_ids else None,
        {"subject": "thread subject", "created_by": "who started it"}, "anonymise"),
    "messages": Spec(
        "Viestit", "Messages", "author.id = subject's user (export: also customer-visible replies in your threads)",
        lambda s: {"author.id": s.ids} if s.user_ids else None,
        {"author": "who wrote it", "body": "message text", "attachments": "attached documents"},
        "anonymise", export_drop=("ai",)),
    "documents": Spec(
        "Ladatut asiakirjat", "Uploaded documents", "uploaded_by.id = subject's user, or a POA the subject signed",
        lambda s: {"uploaded_by.id": s.ids} if s.user_ids else None,
        {"filename": "file name", "uploaded_by": "uploader", "file_id": "stored file (included in the ZIP)"},
        "retain", basis="customs_records", event=("uploaded_at",), export_drop=("extraction", "checks")),
    "claims": Spec(
        "Korvausvaatimukset", "Claims", "created_by = subject's user or claimant contact e-mail = subject",
        lambda s: _or({"created_by.id": s.ids} if s.user_ids else None, {"contact.email": s.rx},
                      {"claimant.email": s.rx}, {"submitted_by.id": s.ids} if s.user_ids else None),
        {"created_by": "who filed it", "contact": "claim contact", "description": "what happened"},
        "retain", basis="legal_claims", event=("closed_at", "paid_at", "updated_at", "created_at"),
        minimise=("contact.phone", "claimant.phone")),
    "exceptions": Spec(
        "Poikkeamat", "Shipment exceptions", "created_by / resolved_by = subject's user",
        lambda s: _or({"created_by.id": s.ids} if s.user_ids else None,
                      {"resolved_by.id": s.ids} if s.user_ids else None),
        {"created_by": "who reported", "history.by": "who acted"}, "anonymise"),
    "customs_declarations": Spec(
        "Tulli-ilmoitukset", "Customs declarations", "a party contact e-mail = subject or created by the subject",
        lambda s: _or({"created_by.id": s.ids} if s.user_ids else None,
                      {"assigned_to.id": s.ids} if s.user_ids else None,
                      *_contact_paths(["importer", "exporter", "declarant", "consignee", "consignor"], s)),
        {"importer|exporter|declarant.contact": "party contact person", "created_by": "who prepared it"},
        "retain", basis="customs_records", event=("released_at", "decided_at", "filed_at", "created_at"),
        export_drop=("xml", "risk", "screening")),
    "invoices": Spec(
        "Laskut ja hyvityslaskut", "Invoices and credit notes", "buyer.billing_email = subject or created/issued by the subject",
        lambda s: _or({"buyer.billing_email": s.rx}, {"created_by.id": s.ids} if s.user_ids else None,
                      {"issued_by.id": s.ids} if s.user_ids else None),
        {"buyer.billing_email": "invoicing e-mail", "created_by|issued_by": "who made the invoice"},
        "retain", basis="bookkeeping_voucher", event=("issue_date", "supply_date", "created_at"),
        minimise=("buyer.phone", "internal_note"), export_drop=("internal_note", "warnings")),
    "payment_transactions": Spec(
        "Korttimaksut", "Card payments", "created_by.id = subject's user",
        lambda s: {"created_by.id": s.ids} if s.user_ids else None,
        {"created_by": "who paid", "amount_cents": "amount"}, "retain", basis="bookkeeping_voucher",
        export_drop=("url",)),
    "approvals": Spec(
        "Hyväksyntäpyynnöt", "Approval requests", "requested_by / decided_by = subject's user",
        lambda s: _or({"requested_by.id": s.ids} if s.user_ids else None,
                      {"decided_by.id": s.ids} if s.user_ids else None, {"data.email": s.rx}),
        {"requested_by": "who asked", "decided_by": "who decided"}, "anonymise", export=False),
    "audit_log": Spec(
        "Tapahtumaloki", "Audit log", "actor.id = subject's user, or the subject's e-mail in the entry",
        lambda s: _or({"actor.id": s.ids} if s.user_ids else None, {"data.email": s.rx},
                      {"entity": "user", "entity_id": s.ids} if s.user_ids else None),
        {"actor": "who did it", "action": "what", "at": "when"}, "anonymise"),
    "ai_runs": Spec(
        "Tekoälyajojen syötteet", "AI run inputs", "the subject's e-mail in the prompt",
        lambda s: {"prompt_preview": {"$regex": re.escape(s.email), "$options": "i"}},
        {"prompt_preview": "text sent to the model", "output_preview": "model answer"}, "delete", export=False),
    "partner_outreach": Spec(
        "Kumppanikyselyt (yhteyshenkilö)", "Partner RFQs (contact person)", "contact_email = subject",
        lambda s: {"contact_email": s.rx}, {"contact_email": "partner contact e-mail"}, "anonymise"),
    "partner_replies": Spec(
        "Kumppanien vastaukset", "Partner replies", "sender e-mail = subject",
        lambda s: _or({"from": s.rx}, {"from_email": s.rx}, {"sender": s.rx}),
        {"from": "sender", "body": "reply text"}, "anonymise"),
    "carriers": Spec(
        "Kuljetusyhtiöiden yhteyshenkilöt", "Carrier contact persons", "contact_email / contacts.email = subject",
        lambda s: _or({"contact_email": s.rx}, {"contacts.email": s.rx}),
        {"contact_email": "carrier contact e-mail", "contacts": "contact persons"}, "anonymise"),
    "privacy_requests": Spec(
        "Tietosuojapyynnöt", "Data-protection requests", "email = subject",
        lambda s: {"email": s.rx}, {"email": "requester", "type": "export or erasure", "status": "state"}, "keep",
        export_drop=("code_hash", "download_jti", "ip_hash", "result")),
    # --- other branches (defensive: a missing collection returns nothing) ---
    "partner_candidates": Spec(
        "Kumppaniehdokkaat (s3)", "Partner candidates (s3)", "email / contact_email / contacts.email = subject",
        lambda s: _or({"email": s.rx}, {"contact_email": s.rx}, {"contacts.email": s.rx}),
        {"contact_email": "contact e-mail", "contact_name": "contact person"}, "anonymise", owner="s3"),
    "partner_messages": Spec(
        "Kumppaniviestit (s3)", "Partner e-mails (s3)", "from / to = subject",
        lambda s: _or({"from": s.rx}, {"to": s.rx}, {"from_email": s.rx}, {"to_email": s.rx}),
        {"from|to": "sender/recipient", "text": "message text"}, "delete", owner="s3"),
    "partner_optouts": Spec(
        "Kumppaniviestinnän kiellot (s3)", "Partner e-mail opt-outs (s3)", "email = subject",
        lambda s: {"email": s.rx}, {"email": "opted-out address"}, "retain", basis="objection_suppression", owner="s3"),
    "email_threads": Spec(
        "Sähköpostiketjut (s6)", "E-mail threads (s6)", "customer_email = subject",
        lambda s: {"customer_email": s.rx}, {"customer_email": "customer e-mail"}, "anonymise", owner="s6"),
    "email_messages": Spec(
        "Sähköpostiviestit (s6)", "E-mail messages (s6)", "from / to = subject",
        lambda s: _or({"from": s.rx}, {"to": s.rx}), {"from|to": "sender/recipient", "text": "message text"},
        "delete", owner="s6"),
}

# Strings that are an actor's display name live under these keys (milestones.by, history.by, invited_by_name ...).
BY_KEYS = {"by", "author_name", "invited_by_name", "requested_by_name", "decided_by_name", "created_by_name",
           "assigned_to_name", "signed_by", "uploaded_by_name"}
PERSON_EMAIL_KEYS = {"email", "contact_email", "from_email", "sender_email", "customer_email"}
PERSON_KEYS = {"name", "contact_name", "contact_person", "email", "contact_email", "phone", "mobile", "phone_number",
               "title", "first_name", "last_name", "typed_name", "user_agent"}


def scrub(node: Any, s: Subject, path: str = "", skip: tuple = (), changed: list | None = None) -> Any:
    """Return `node` with the subject's identifiers removed; record changed paths in `changed`."""
    changed = changed if changed is not None else []
    e = s.email
    if path and any(path == p or path.startswith(p + ".") for p in skip):
        return node
    if isinstance(node, dict):
        out = {}
        personal = any(isinstance(node.get(k), str) and node[k].strip().lower() == e for k in PERSON_EMAIL_KEYS)
        actor = node.get("id") in s.user_ids and any(k in node for k in ("name", "type", "email"))
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if k == "_id":
                out[k] = v
            elif personal and k in PERSON_KEYS and v not in (None, ""):
                out[k] = None
                changed.append(p)
            elif actor and k in ("name", "email", "phone") and v not in (None, ""):
                out[k] = ERASED_NAME if k == "name" else None
                changed.append(p)
            elif k in BY_KEYS and isinstance(v, str) and v.strip() in s.names:
                out[k] = ERASED_NAME
                changed.append(p)
            else:
                out[k] = scrub(v, s, p, skip, changed)
        return out
    if isinstance(node, list):
        return [scrub(v, s, f"{path}.{i}" if path else str(i), skip, changed) for i, v in enumerate(node)]
    if isinstance(node, str):
        low = node.strip().lower()
        if low == e or (s.phones and node.strip() in s.phones):
            changed.append(path)
            return None
        hit = e in node.lower() or any(ph in node for ph in s.phones)
        if hit:
            changed.append(path)
            out = re.sub(re.escape(e), ERASED_TOKEN, node, flags=re.I)
            for ph in s.phones:
                out = out.replace(ph, ERASED_TOKEN)
            return out
    return node


def _drop(doc: dict, paths: tuple) -> list[str]:
    gone = []
    for p in paths:
        parts = p.split(".")
        cur = doc
        for part in parts[:-1]:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(cur, dict) and parts[-1] in cur:
            cur.pop(parts[-1])
            gone.append(p)
    return gone


# ------------------------------------------------------------------------------------------------ export
def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items() if k != "_id"}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return None
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


async def _export_rows(name: str, spec: Spec, s: Subject) -> list[dict]:
    q = spec.find(s)
    if name == "messages" and s.user_ids:
        thread_ids = [t["id"] async for t in raw("threads").find({"created_by.id": s.ids}, {"id": 1})]
        q = _or(q, {"thread_id": {"$in": thread_ids}, "visibility": "customer"} if thread_ids else None)
    if q is None:
        return []
    rows = []
    async for d in raw(name).find(q, {"_id": 0}).limit(5000):
        if name == "email_outbox" and str(d.get("tag") or "").startswith("privacy"):
            d.pop("text", None)  # one-time codes are never exported
        _drop(d, spec.export_drop)
        rows.append(_jsonable(d))
    return rows


def _information(settings: dict) -> dict:
    """GDPR art. 15(1)(a)-(h) information that accompanies the copy."""
    from services.retention import policy_table

    return {
        "controller": {"name": config.COMPANY_NAME, "business_id": settings.get("seller_business_id") or None,
                       "address": config.COMPANY_ADDRESS, "contact": config.COMPANY_EMAIL},
        "purposes": {"fi": "Kuljetusten huolinta ja tullaus (sopimus, 6(1)(b)), kirjanpito ja tullin säilytysvelvoitteet "
                           "(6(1)(c)), pakote- ja vientivalvonta sekä palvelun turvallisuus (6(1)(f)).",
                     "en": "Freight forwarding and customs clearance (contract, 6(1)(b)), bookkeeping and customs record "
                           "keeping (6(1)(c)), sanctions/export-control checks and service security (6(1)(f))."},
        "recipients": {"fi": "Kuljetusyhtiöt ja agentit (lähetyksen osapuolet), Tulli, maksupalvelu (Stripe), "
                             "sähköpostipalvelu, tekoälypalvelu asiakirjojen lukemiseen.",
                       "en": "Carriers and agents (shipment parties), Finnish Customs, the payment processor (Stripe), "
                             "the e-mail provider, the AI provider that reads documents."},
        "retention": policy_table(settings),
        "rights": {"fi": "Oikeus saada pääsy, oikaista, poistaa, rajoittaa käsittelyä, vastustaa ja siirtää tiedot. "
                         "Valitusoikeus: Tietosuojavaltuutetun toimisto, tietosuoja.fi.",
                   "en": "Rights of access, rectification, erasure, restriction, objection and portability. Complaints: "
                         "Office of the Data Protection Ombudsman, tietosuoja.fi."},
        "source": {"fi": "Tiedot on saatu sinulta, yritykseltäsi ja lähetysten osapuolilta.",
                   "en": "The data came from you, your company and the parties of your shipments."},
        "automated_decisions": {"fi": "Autopilotti voi hinnoitella ja hyväksyä tarjouksia sääntöjen mukaan; ihminen "
                                      "tarkistaa poikkeukset.",
                                "en": "Autopilot can price and approve quotes by rules; a person reviews exceptions."},
    }


async def export_subject(email: str) -> dict:
    """GDPR art. 15 + 20 bundle: every collection's rows for the subject, with field documentation."""
    s = await load_subject(email)
    settings = await get_settings()
    collections = {}
    for name, spec in PERSONAL_DATA.items():
        if not spec.export:
            continue
        rows = await _export_rows(name, spec, s)
        if not rows:
            continue
        collections[name] = {"label_fi": spec.label_fi, "label_en": spec.label_en, "found_by": spec.found_by,
                             "fields": spec.fields, "count": len(rows), "items": rows}
    return {"format": EXPORT_FORMAT, "generated_at": now_iso(), "subject": {"email": s.email, "accounts": len(s.users)},
            "legal_basis": {"fi": "EU:n yleinen tietosuoja-asetus 2016/679, 15 ja 20 artikla",
                            "en": "EU General Data Protection Regulation 2016/679, articles 15 and 20"},
            "information": _information(settings), "collections": collections}


README = """Huolitsija - tietosuojapyynnön mukainen kopio tiedoistasi / copy of your personal data

export.json  Kaikki tietosi koneluettavassa JSON-muodossa kokoelmittain; kunkin kokoelman `fields` kertoo
             kenttien merkityksen (GDPR 15 ja 20 art.).
             All your data in machine-readable JSON per collection; `fields` documents each field (GDPR art. 15, 20).
files/       Lataamasi asiakirjat / documents you uploaded.
manifest.json  SHA-256 jokaisesta tiedostosta / of every file.
"""


async def export_zip(email: str, bundle: dict | None = None) -> bytes:
    from core.storage import get_file

    bundle = bundle or await export_subject(email)
    buf = io.BytesIO()
    manifest, total, skipped = {}, 0, []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in (bundle["collections"].get("documents") or {}).get("items", []):
            if not d.get("file_id"):
                continue
            try:
                data, filename, _ct = await get_file(d["file_id"])
            except Exception:  # noqa: BLE001 - a missing GridFS file is reported, not fatal
                skipped.append({"document_id": d.get("id"), "reason": "file not found"})
                continue
            if total + len(data) > MAX_EXPORT_FILE_BYTES:
                skipped.append({"document_id": d.get("id"), "reason": "size limit; ask ops for this file"})
                continue
            total += len(data)
            name = f"files/{d['id'][:8]}-{re.sub(r'[^A-Za-z0-9._-]+', '_', d.get('filename') or filename or 'file')}"
            z.writestr(name, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        bundle = {**bundle, "files_skipped": skipped}
        body = json.dumps(bundle, ensure_ascii=False, indent=2).encode()
        z.writestr("export.json", body)
        manifest["export.json"] = hashlib.sha256(body).hexdigest()
        z.writestr("README.txt", README)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------ erasure
def _retention(basis_key: str, event: Any, settings: dict, holds: list[dict] | None = None) -> dict:
    if holds:
        until = max((h.get("until") or "9999-12-31") for h in holds)
        return {"basis": "legal_hold", "basis_fi": BASES["legal_hold"]["fi"], "basis_en": BASES["legal_hold"]["en"],
                "hold_ids": [h["id"] for h in holds], "reason": "; ".join(h.get("reason") or "" for h in holds)[:500],
                "until": None if until == "9999-12-31" else until, "set_at": now_iso()}
    b = BASES[basis_key]
    return {"basis": basis_key, "basis_fi": b["fi"], "basis_en": b["en"],
            "until": year_end_plus(event, basis_years(basis_key, settings)), "set_at": now_iso()}


def _item(coll: str, doc: dict, **kw) -> dict:
    return {"collection": coll, "id": doc.get("id") or str(doc.get("_id")), **kw}


async def _erase_user(doc: dict, s: Subject, held: list[dict], settings: dict, out: dict):
    """Sign-in always ends (password removed, sessions revoked). Identity is anonymised unless a hold covers it."""
    sets = {"disabled": True, "password_hash": None, "erased_at": now_iso(), "notify_email": False}
    if held:
        sets.update(processing_restricted=True, retention=_retention("legal_hold", None, settings, held))
        out["retained"].append(_item("users", doc, basis="legal_hold", until=sets["retention"]["until"],
                                     hold_ids=[h["id"] for h in held], login_disabled=True))
    else:
        sets.update(email=f"erased-{doc['id'][:12]}@erased.invalid", name=ERASED_NAME, phone=None, erased=True)
        out["anonymised"].append(_item("users", doc, fields=["email", "name", "phone", "password_hash"]))
    await raw("users").update_one({"_id": doc["_id"]}, {"$set": sets, "$inc": {"session_version": 1}})


async def _erase_document_row(coll: str, doc: dict, out: dict, what: str = "row"):
    await raw(coll).delete_one({"_id": doc["_id"]})
    if coll == "documents" and doc.get("file_id"):
        from core.storage import delete_file

        await delete_file(doc["file_id"])
        what = "row and stored file"
    out["erased"].append(_item(coll, doc, what=what))


async def _process(coll: str, spec: Spec, doc: dict, s: Subject, holds: list[dict], settings: dict, out: dict,
                   ctx: dict):
    held = hold_hits(coll, doc, holds, s.email)
    if coll == "users":
        return await _erase_user(doc, s, held, settings, out)
    if held:
        ret = _retention("legal_hold", None, settings, held)
        await raw(coll).update_one({"_id": doc["_id"]}, {"$set": {"processing_restricted": True, "retention": ret}})
        out["retained"].append(_item(coll, doc, basis="legal_hold", until=ret["until"], hold_ids=ret["hold_ids"]))
        return
    action = spec.erase
    if coll == "quotes" and doc.get("source") == "public" and not doc.get("company_id"):
        action = "delete"  # anonymous instant quote: nothing requires it
    if coll == "documents" and not doc.get("shipment_id") and doc.get("type") != "power_of_attorney":
        action = "delete"  # not a customs/transport record
    if coll == "invoices" and doc.get("status") == "draft" and not doc.get("number"):
        action = "anonymise"  # an unnumbered draft is not a voucher yet
    if action == "keep":
        return
    if action == "delete":
        return await _erase_document_row(coll, doc, out)
    changed: list[str] = []
    skip = ("poa", "poa_history") if coll == "companies" else ()
    new = scrub(doc, s, skip=skip, changed=changed)
    if (new.get("retention") or {}).get("basis") == "legal_hold":  # the hold that froze this row has been released
        new.pop("retention")
        new.pop("processing_restricted", None)
        changed.append("retention")
    if coll == "messages" and (new.get("author") or {}).get("id") in s.user_ids and new.get("body"):
        new["body"] = ERASED_TEXT
        changed.append("body")
        ctx.setdefault("threads", set()).add(doc.get("thread_id"))
    if coll == "companies":
        changed += _retain_poa(new, s, settings, out)
    if action == "retain":
        changed += _drop(new, spec.minimise)
        if coll == "documents" and doc.get("type") == "power_of_attorney":
            event = ctx.get("poa_valid_to", {}).get(doc.get("id")) or doc.get("uploaded_at")
        else:
            event = first_event(doc, *spec.event)
        new["retention"] = _retention(spec.basis, event, settings)
        new["processing_restricted"] = True
        await raw(coll).replace_one({"_id": doc["_id"]}, new)
        out["retained"].append(_item(coll, doc, basis=spec.basis, until=new["retention"]["until"],
                                     fields_removed=sorted(set(changed))))
        return
    if changed:
        await raw(coll).replace_one({"_id": doc["_id"]}, new)
        out["anonymised"].append(_item(coll, doc, fields=sorted(set(changed))[:50]))


def _retain_poa(company: dict, s: Subject, settings: dict, out: dict) -> list[str]:
    """A POA the subject signed is a customs record: keep the evidence (hash covers name, e-mail, time, IP), drop the
    browser string, restrict processing until the end of the customs retention period."""
    changed = []
    poas = [("poa", company.get("poa"))] + [(f"poa_history.{i}", p) for i, p in enumerate(company.get("poa_history") or [])]
    for path, poa in poas:
        sig = (poa or {}).get("signature") or {}
        if (sig.get("email") or "").lower() != s.email and sig.get("user_id") not in s.user_ids:
            continue
        if sig.pop("user_agent", None):
            changed.append(f"{path}.signature.user_agent")
        event = poa.get("revoked_at") or poa.get("valid_to") or poa.get("signed_at")
        sig["retention"] = _retention("customs_records", event, settings)
        sig["processing_restricted"] = True
        out["retained"].append({"collection": "companies", "id": company.get("id"), "path": f"{path}.signature",
                                "basis": "customs_records", "until": sig["retention"]["until"],
                                "fields_removed": [f"{path}.signature.user_agent"]})
    return changed


async def _refresh_thread_previews(thread_ids: set):
    for tid in [t for t in thread_ids if t]:
        last = await raw("messages").find_one({"thread_id": tid, "visibility": "customer"}, {"_id": 0, "body": 1},
                                              sort=[("created_at", -1)])
        preview = " ".join(((last or {}).get("body") or "").split())[:160]
        await raw("threads").update_one({"id": tid}, {"$set": {"last_message_preview": preview}})


async def erase_subject(email: str, actor: dict | None, *, request_id: str | None = None) -> dict:
    """GDPR art. 17 erasure honouring legal retention (17(3)(b), (e)) and legal holds; returns the full account."""
    s = await load_subject(email)
    settings = await get_settings()
    holds = await active_holds()
    out = {"subject_hash": email_hash(s.email), "subject_masked": mask_email(s.email), "accounts": len(s.users),
           "started_at": now_iso(), "actor": actor_ref(actor), "request_id": request_id,
           "erased": [], "anonymised": [], "retained": [], "errors": []}
    ctx: dict = {"poa_valid_to": {}}
    async for c in raw("companies").find({"poa.signature.email": s.rx}, {"_id": 0, "poa": 1}):
        if (c.get("poa") or {}).get("document_id"):
            ctx["poa_valid_to"][c["poa"]["document_id"]] = c["poa"].get("revoked_at") or c["poa"].get("valid_to")
    for coll, spec in PERSONAL_DATA.items():
        q = spec.find(s)
        if coll == "documents" and ctx["poa_valid_to"]:
            q = _or(q, {"id": {"$in": list(ctx["poa_valid_to"])}})
        if q is None or spec.erase == "keep":
            continue
        async for doc in raw(coll).find(q):
            try:
                await _process(coll, spec, doc, s, holds, settings, out, ctx)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the erasure; it is reported
                out["errors"].append({"collection": coll, "id": doc.get("id"), "error": str(exc)[:300]})
    await _refresh_thread_previews(ctx.get("threads") or set())
    counts: dict = {}
    for kind in ("erased", "anonymised", "retained"):
        for it in out[kind]:
            counts.setdefault(it["collection"], {"erased": 0, "anonymised": 0, "retained": 0})[kind] += 1
        out[kind] = out[kind][:LIST_CAP]
    out["counts"] = counts
    out["held"] = [it for it in out["retained"] if it.get("basis") == "legal_hold"]
    out["login_disabled"] = bool(s.users)
    out["status"] = "partially_held" if out["held"] else "completed"
    out["finished_at"] = now_iso()
    await audit(actor, "privacy_erasure_executed", "privacy_request", request_id,
                {"subject_hash": out["subject_hash"], "counts": counts, "held": len(out["held"]),
                 "errors": len(out["errors"])})
    return out
