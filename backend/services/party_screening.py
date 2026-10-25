"""Party screening for bookings, shipments, customers and carriers (RULE K-1; legal human point sanctions_potential_match).

Every party of a shipment is screened with services.screening against all loaded lists (EU, UN, OFAC SDN, UK, US CSL):
shipper, consignee, notify party, the customer company, every leg carrier (db.carriers by leg carrier_id, else the leg's
carrier name) and any agents / partners on the shipment (`agents` / `partners` lists or extra keys under `parties`).

    pre = await screen_parties_for_booking({"shipper": .., "consignee": .., "notify": .., "company": company,
                                            "legs": option["legs"], "quote_id": q["id"]})      # before the shipment exists
    row = await screen_shipment(shipment_id, "booking", actor=user)                         # rows link to the shipment
    row = await screen_company(company_id, "rescreen") / await screen_carrier(carrier_id, "manual", actor=user)

Each run is stored in `party_screenings` {id, subject_type: shipment|company|booking|carrier, subject_id, subject_label,
trigger: booking|rescreen|manual|list_change, list_versions{list: version}, status, parties[{role, name, country, kind,
status, hits[], closest}], created_at, decided{...}}. Status: clear | potential_match | error, and after a person decides:
cleared (false positive) | blocked_sanctions (confirmed).

- potential_match: the shipment gets a `sanctions` hold (it cannot move), `compliance_hold: true` and a neutral
  `compliance_hold_reason` ("Varaus odottaa vaatimustenmukaisuustarkistusta / Booking pending compliance review": the
  customer sees the shipment document, so list names and scores stay in party_screenings and the approval card). An
  approval card `sanctions_review` (perm kyc.approve) is opened; event `compliance.hold_set`. The agent never clears a
  potential match: only a person deciding the card does (approve = false positive, reject = confirmed match; the
  decision data may also say {"outcome": "false_positive" | "confirmed"}).
  False positive: the hold is lifted, the (screened name, list, ref) pairs are remembered in `sanctions_clearances`
  for that party's identity only (address, company or carrier id) and for 365 days, so the same hit does not come back
  on every re-screen while a namesake elsewhere is still stopped; event `compliance.hold_cleared`. Confirmed: the hold stays,
  the shipment's `compliance_status` becomes `blocked_sanctions`, ops are notified.
- error (no list loaded): never clear. The shipment is held as well; a later complete screening without matches
  lifts an error hold (that is not a match decision).
- A new list version (screening.refresh_lists emits `sanctions.lists_changed`) re-screens every open shipment, every
  KYC-approved customer and every active carrier: job `compliance.rescreen_all` via core.compliance_jobs, also daily
  at 05:00 as a backstop. Automatic runs respect the kill switch and are audit-logged as agent_actor("compliance").
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException

from core.approvals import APPROVAL_KINDS, request_approval
from core.audit import actor_ref, agent_actor, audit
from core.compliance_jobs import autopilot_halted, enqueue_or_run, recurring
from core.db import db, register_index
from core.events import emit, on
from core.notify import bi, notify_role, same
from core.util import NO_ID, iso, new_id, now, now_iso
from services import screening

log = logging.getLogger("party_screening")

register_index("party_screenings", "id", unique=True)
register_index("party_screenings", [("subject_type", 1), ("subject_id", 1), ("created_at", -1)])
register_index("party_screenings", [("status", 1), ("created_at", -1)])
register_index("sanctions_clearances", "key", unique=True)

APPROVAL_KINDS.setdefault("sanctions_review", {"label": "Sanctions / denied-party potential match", "perm": "kyc.approve"})

AGENT = agent_actor("compliance")
HOLD_FI = "Varaus odottaa vaatimustenmukaisuustarkistusta"
HOLD_EN = "Booking pending compliance review"
HOLD_REASON = f"{HOLD_FI} / {HOLD_EN}"
CLOSED_STATUSES = ("delivered", "cancelled", "closed")
LEGAL_POINT = "sanctions_potential_match"
LEGAL_FI = "Mahdollinen pakotelistaosuma: ihminen ratkaisee, onko kyse väärästä osumasta (EU 2580/2001, 269/2014 ja 833/2014)."
LEGAL_EN = "Potential sanctions / denied-party match: a person decides whether it is a false positive."
ROLE_LABELS = {"shipper": ("Lähettäjä", "Shipper"), "consignee": ("Vastaanottaja", "Consignee"),
               "notify": ("Ilmoitettava osapuoli", "Notify party"), "customer": ("Asiakasyritys", "Customer company"),
               "carrier": ("Kuljetusliike", "Carrier"), "agent": ("Agentti / kumppani", "Agent / partner"),
               "beneficial_owner": ("Tosiasiallinen edunsaaja", "Beneficial owner"), "signatory": ("Allekirjoittaja", "Signatory")}
_HIT_FIELDS = ("list", "list_label", "sub_list", "ref", "matched_name", "primary_name", "type", "programmes", "countries",
               "listed_on", "score", "explanation")


# ---------------------------------------------------------------------------------------------- parties
def _party(role: str, name, *, country=None, kind="any", source=None, ref=None, identity=None) -> dict | None:
    """identity: what tells this party apart from a namesake (address, company id, carrier id). A person's
    false-positive decision is remembered for that identity only, never for the bare name."""
    name = (name or "").strip() if isinstance(name, str) else ""
    if not name:
        return None
    return {"role": role, "name": name[:300], "country": ((country or "").strip().upper()[:2] or None), "kind": kind,
            "source": source, "ref": ref, "identity": identity or ref}


def _address_identity(addr: dict | None) -> str | None:
    a = addr or {}
    parts = [" ".join(str(a.get(k) or "").lower().split()) for k in ("street", "postal_code", "city")]
    return "addr:" + "/".join(parts) if any(parts) else None


def _from_party_doc(role: str, p: dict | None) -> dict | None:
    if not isinstance(p, dict):
        return None
    return _party(role, p.get("name"), country=(p.get("address") or {}).get("country") or p.get("country"),
                  kind=p.get("kind") if p.get("kind") in ("person", "entity") else "any", source="booking",
                  identity=_address_identity(p.get("address")))


async def _carrier_parties(legs: list[dict] | None) -> list[dict]:
    out = []
    for lg in legs or []:
        cid = lg.get("carrier_id")
        proj = {"_id": 0, "id": 1, "name": 1, "country": 1}
        car = await db.carriers.find_one({"id": cid}, proj) if cid else None
        label = (lg.get("carrier") or lg.get("carrier_name") or "").strip()
        if not car and label:
            # A leg may name the carrier by its code ("MSC"); screen the registered name, not the acronym, which
            # would collide with listed aliases such as OFAC's "MSC" for Mujahidin Shura Council.
            car = await db.carriers.find_one({"$or": [{"code": label.upper()}, {"name": label}]}, proj)
            cid = cid or (car or {}).get("id")
        name = (car or {}).get("name") or label
        p = _party("carrier", name, country=(car or {}).get("country"), kind="entity", source="leg", identity=cid and f"carrier:{cid}",
                   ref=f"leg {lg.get('seq')}" if lg.get("seq") else None)
        if p:
            p["carrier_id"] = cid
            out.append(p)
    return out


def _agent_parties(s: dict) -> list[dict]:
    out = []
    for key in ("agents", "partners", "overseas_agents"):
        for a in s.get(key) or []:
            if isinstance(a, dict):
                p = _party("agent", a.get("name"), country=a.get("country") or (a.get("address") or {}).get("country"),
                           kind="entity", source=key, ref=a.get("role") or a.get("id"))
                if p:
                    out.append(p)
    for key, p in (s.get("parties") or {}).items():
        if key not in ("shipper", "consignee", "notify") and isinstance(p, dict):
            q = _from_party_doc("agent", p)
            if q:
                q["ref"] = key
                out.append(q)
    return out


def _company_party(company: dict | None) -> dict | None:
    if not company:
        return None
    return _party("customer", company.get("name"), country=company.get("country") or "FI", kind="entity", source="company",
                  ref=company.get("id"))


def _dedupe(parties: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in parties:
        if not p:
            continue
        k = (p["role"], screening.normalize(p["name"]))
        if k[1] and k not in seen:
            seen.add(k)
            out.append(p)
    return out


async def shipment_parties(s: dict) -> list[dict]:
    parties = [_from_party_doc(r, (s.get("parties") or {}).get(r)) for r in ("shipper", "consignee", "notify")]
    company = await db.companies.find_one({"id": s.get("company_id")}, NO_ID) if s.get("company_id") else None
    parties.append(_company_party(company))
    parties += await _carrier_parties(s.get("legs"))
    parties += _agent_parties(s)
    return _dedupe(parties)


# ---------------------------------------------------------------------------------------------- screening core
CLEARANCE_DAYS = 365  # a false-positive decision is reviewed again after a year (or when the party's identity changes)


def _hit_key(query: str, h: dict) -> str:  # same shape as services.kyc's per-company cleared_hits
    return "|".join(((query or "").strip().lower(), h.get("list") or "", str(h.get("ref") or h.get("matched_name") or "")))


def _clear_key(p: dict, h: dict) -> str | None:
    """Remembered decisions are scoped to the party's identity: a namesake at another address (or with no address)
    is screened afresh, so clearing one "Transsphere Oy" never clears the listed one."""
    if not p.get("identity"):
        return None
    return "|".join((_hit_key(p["name"], h), p.get("country") or "", p["identity"]))


async def _cleared_keys(p: dict, hits: list[dict], extra: set | None = None) -> set:
    keys = [k for k in (_clear_key(p, h) for h in hits) if k]
    got = {d["key"] async for d in db.sanctions_clearances.find(
        {"key": {"$in": keys}, "expires_at": {"$gt": now_iso()}}, {"_id": 0, "key": 1})} if keys else set()
    kyc = {_hit_key(p["name"], h) for h in hits} & (extra or set())
    return got | kyc


async def _screen(parties: list[dict], *, extra_cleared: set | None = None) -> tuple[str, list[dict], dict]:
    """Screen each party; (overall status, party rows, list versions). Previously cleared hits do not count."""
    rows = []
    for p in parties:
        try:
            r = await screening.screen_name(p["name"], kind=p["kind"] if p["kind"] in ("person", "entity") else "any",
                                            country=p.get("country"))
        except ValueError as exc:
            r = {"status": "error", "reason": str(exc), "hits": [], "closest": None}
        hits = [{k: h.get(k) for k in _HIT_FIELDS} for h in r.get("hits") or []]
        cleared = await _cleared_keys(p, hits, extra_cleared)
        for h in hits:
            h["previously_cleared"] = _clear_key(p, h) in cleared or _hit_key(p["name"], h) in cleared
        live = [h for h in hits if not h["previously_cleared"]]
        status = "error" if r.get("status") == "error" else "potential_match" if live else "clear"
        row = {**{k: p.get(k) for k in ("role", "name", "country", "kind", "source", "ref", "identity", "carrier_id")
                  if p.get(k) is not None},
               "status": status, "hits": hits, "closest": ({k: r["closest"].get(k) for k in _HIT_FIELDS}
                                                          if r.get("closest") else None),
               "top_score": r.get("top_score"), "complete": r.get("complete"), "reason": r.get("reason")}
        second = await screening.csl_live_search(p["name"]) if screening.csl_api_key() else None
        if second is not None:
            row["csl_api"] = second
            strong = [x for x in second.get("results") or [] if (x.get("score") or 0) >= 95]
            if strong and status == "clear":
                row["status"] = "potential_match"
                row["hits"].append({"list": "csl", "list_label": "US Consolidated Screening List (live search API)",
                                    "sub_list": strong[0].get("sub_list"), "ref": strong[0].get("id"),
                                    "matched_name": strong[0].get("matched_name") or strong[0].get("name"),
                                    "primary_name": strong[0].get("name"), "score": round((strong[0].get("score") or 0) / 100, 3),
                                    "explanation": f"The live CSL search API scored “{strong[0].get('name')}” at "
                                                   f"{strong[0].get('score')} / 100 ({strong[0].get('source')}).",
                                    "previously_cleared": False})
        rows.append(row)
    statuses = {r["status"] for r in rows}
    overall = "potential_match" if "potential_match" in statuses else "error" if ("error" in statuses or not rows) else "clear"
    versions = await screening._current_versions(force=True)
    return overall, rows, versions


async def _store_row(subject_type: str, subject_id: str | None, label: str | None, trigger: str, status: str,
                     parties: list[dict], versions: dict, actor: dict | None, **extra) -> dict:
    row = {"id": new_id(), "subject_type": subject_type, "subject_id": subject_id, "subject_label": label, "trigger": trigger,
           "list_versions": versions, "status": status, "parties": parties, "created_at": now_iso(),
           "actor": actor_ref(actor) if actor else AGENT, "decided": None, **extra}
    await db.party_screenings.insert_one(dict(row))
    return row


def _hits_summary(rows: list[dict], limit: int = 4) -> str:
    out = []
    for p in rows:
        for h in p.get("hits") or []:
            if not h.get("previously_cleared"):
                out.append(f"{p['role']} “{p['name']}” ~ “{h.get('matched_name')}” ({h.get('list_label')}, {h.get('ref')}, "
                           f"score {h.get('score') or 0:.2f})")
    return "; ".join(out[:limit]) + (f" (+{len(out) - limit} more)" if len(out) > limit else "")


def hold_reason_internal(code: str | None, parties: list[dict]) -> str:
    """Ops-only reason for a hold (never shown to the customer)."""
    if code == "screening_error":
        return "Sanctions screening could not run (no list loaded); held until a complete screening."
    hits = _hits_summary(parties)
    return f"Potential match: {hits}" if hits else "Potential sanctions / denied-party match"


# ---------------------------------------------------------------------------------------------- booking
async def screen_parties_for_booking(parties: dict) -> dict:
    """Screen a booking's parties before the shipment exists.

    parties: {shipper, consignee, notify (party dicts with name + address.country), company (company doc), legs
    (quote option legs), quote_id}. Returns {id, status, hold, message, message_fi, message_en}: on potential_match
    or error `hold` is True and the message is the neutral text for the customer."""
    try:
        plist = [_from_party_doc(r, parties.get(r)) for r in ("shipper", "consignee", "notify")]
        plist.append(_company_party(parties.get("company")))
        plist += await _carrier_parties(parties.get("legs"))
        plist = _dedupe(plist)
        status, rows, versions = await _screen(plist)
    except Exception as exc:  # noqa: BLE001 - a failed screening is an error, never clear
        log.exception("booking screening failed")
        status, rows, versions = "error", [{"role": "booking", "name": "", "status": "error", "hits": [],
                                            "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}], {}
    company = parties.get("company") or {}
    row = await _store_row("booking", parties.get("quote_id"), company.get("name"), "booking", status, rows, versions, None,
                           company_id=company.get("id"))
    hold = status != "clear"
    return {"id": row["id"], "status": status, "hold": hold, "message": HOLD_REASON if hold else None,
            "message_fi": HOLD_FI if hold else None, "message_en": HOLD_EN if hold else None}


# ---------------------------------------------------------------------------------------------- shipments
async def _set_shipment_hold(s: dict, row: dict, code: str):
    from services import shipments as svc

    if not any(h.get("type") == "sanctions" for h in s.get("holds") or []):
        try:
            await svc.add_hold(s["id"], "sanctions", HOLD_REASON, AGENT)
        except HTTPException as exc:
            log.warning("shipment %s: sanctions hold not added: %s", s.get("reference"), exc.detail)
    await db.shipments.update_one({"id": s["id"]}, {"$set": {
        "compliance_hold": True, "compliance_hold_reason": HOLD_REASON,
        "compliance_hold_reason_i18n": {"fi": HOLD_FI, "en": HOLD_EN}, "compliance_hold_code": code,
        "compliance_hold_since": s.get("compliance_hold_since") or now_iso(), "compliance_screening_id": row["id"],
        "compliance_status": "pending_review" if code == LEGAL_POINT else "screening_error", "sanctions_cleared": False,
        "updated_at": now_iso()}})


async def _lift_shipment_hold(s: dict, actor: dict, *, status: str):
    from services import shipments as svc

    if any(h.get("type") == "sanctions" for h in s.get("holds") or []):
        try:
            await svc.release_hold(s["id"], "sanctions", actor)
        except HTTPException as exc:
            log.warning("shipment %s: sanctions hold not released: %s", s.get("reference"), exc.detail)
    await db.shipments.update_one({"id": s["id"]}, {"$set": {
        "compliance_hold": False, "compliance_hold_code": None, "compliance_status": status, "sanctions_cleared": True,
        "compliance_cleared_at": now_iso(), "updated_at": now_iso()}})


async def _open_card(subject_type: str, subject_id: str, label: str, row: dict, *, company_id=None, link=None) -> dict:
    summary = _hits_summary(row["parties"])
    title_en = f"Sanctions review: {label}"
    card = await request_approval(
        "sanctions_review", subject_type, subject_id, title_en, summary=summary,
        reason="Potential match on a sanctions or denied-party list; approve = false positive, reject = confirmed match",
        company_id=company_id, requested_by=AGENT, link=link,
        data={"screening_id": row["id"], "subject_type": subject_type, "subject_label": label, "legal_point": LEGAL_POINT,
              "outcomes": {"approved": "false_positive", "rejected": "confirmed"}})
    await db.approvals.update_one({"id": card["id"]}, {"$set": {
        "data.screening_id": row["id"], "summary": summary, "title_fi": f"Pakotelistatarkistus: {label}",
        "summary_fi": summary, "reason_fi": "Mahdollinen osuma pakote- tai vientikieltolistalla; hyväksy = väärä osuma, "
                                            "hylkää = vahvistettu osuma",
        "legal_point": LEGAL_POINT, "legal_reason_fi": LEGAL_FI, "legal_reason_en": LEGAL_EN}})
    await db.party_screenings.update_one({"id": row["id"]}, {"$set": {"approval_id": card["id"]}})
    row["approval_id"] = card["id"]
    return {**card, "data": {**(card.get("data") or {}), "screening_id": row["id"]}}


async def screen_shipment(shipment_id: str, trigger: str = "manual", actor: dict | None = None,
                          booking_screening_id: str | None = None) -> dict:
    """Screen every party of a shipment, store the run and apply the result (hold + card, or lift an error hold)."""
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    parties = await shipment_parties(s)
    try:
        status, rows, versions = await _screen(parties)
    except Exception as exc:  # noqa: BLE001
        log.exception("shipment %s screening failed", s.get("reference"))
        status, rows, versions = "error", [{"role": "shipment", "name": s.get("reference"), "status": "error", "hits": [],
                                            "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}], {}
    row = await _store_row("shipment", s["id"], s.get("reference"), trigger, status, rows, versions, actor,
                           company_id=s.get("company_id"), booking_screening_id=booking_screening_id)
    if s.get("compliance_status") == "blocked_sanctions":
        pass  # a confirmed match stays blocked whatever later screenings say
    elif status == "potential_match":
        already = s.get("compliance_hold") and s.get("compliance_hold_code") == LEGAL_POINT
        await _set_shipment_hold(s, row, LEGAL_POINT)
        card = await _open_card("shipment", s["id"], s.get("reference") or s["id"], row, company_id=s.get("company_id"),
                                link=f"/ops/shipments/{s['id']}")
        if not already:  # a re-screen of a shipment already waiting for a decision only refreshes the card
            await audit(AGENT, "compliance.hold_set", "shipment", s["id"],
                        {"screening_id": row["id"], "approval_id": card["id"], "trigger": trigger, "legal_point": LEGAL_POINT},
                        company_id=s.get("company_id"))
            await emit("compliance.hold_set", {"shipment_id": s["id"], "screening": row, "approval": card, "trigger": trigger})
    elif status == "error":
        if not s.get("compliance_hold"):
            await _set_shipment_hold(s, row, "screening_error")
            await audit(AGENT, "compliance.hold_set", "shipment", s["id"],
                        {"screening_id": row["id"], "trigger": trigger, "reason": "screening could not run"},
                        company_id=s.get("company_id"))
            await notify_role(["ops_agent"], bi(f"Pakotelistaseulonta ei onnistunut: {s.get('reference')}",
                                                f"Sanctions screening could not run: {s.get('reference')}"),
                              bi("Lähetys on pidätetty, kunnes seulonta onnistuu.", "The shipment is held until screening completes."),
                              link=f"/ops/shipments/{s['id']}", kind="compliance")
            await emit("compliance.hold_set", {"shipment_id": s["id"], "screening": row, "approval": None, "trigger": trigger})
    elif s.get("compliance_hold") and s.get("compliance_hold_code") == "screening_error":
        # an error hold is not a match decision: a complete screening without matches lifts it
        pending = await db.approvals.find_one({"kind": "sanctions_review", "ref_id": s["id"], "status": "pending"}, {"_id": 1})
        if not pending:
            await _lift_shipment_hold(s, AGENT, status="clear")
            await audit(AGENT, "compliance.hold_cleared", "shipment", s["id"],
                        {"screening_id": row["id"], "reason": "screening completed without matches"}, company_id=s.get("company_id"))
            await emit("compliance.hold_cleared", {"shipment_id": s["id"], "screening": row, "by": AGENT})
    elif not s.get("compliance_hold"):
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"compliance_status": "clear", "compliance_screening_id": row["id"]}})
    return await db.party_screenings.find_one({"id": row["id"]}, NO_ID)


async def screen_booked_shipment(shipment_id: str, actor: dict | None, pre: dict | None = None) -> dict | None:
    """Booking hook (routes/quotes.create_booking): screen the new shipment; if even that fails, hold it anyway."""
    try:
        return await screen_shipment(shipment_id, "booking", actor=actor, booking_screening_id=(pre or {}).get("id"))
    except Exception:  # noqa: BLE001 - the booking stands, but an unscreened shipment never moves
        log.exception("booking screening of shipment %s failed", shipment_id)
        s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
        if s:
            await _set_shipment_hold(s, {"id": (pre or {}).get("id")}, "screening_error")
        return None


async def screen_carrier_quietly(carrier_id: str, actor: dict | None) -> None:
    """Carrier create/update hook (routes/carriers.py): never fails the carrier edit."""
    try:
        await screen_carrier(carrier_id, "manual", actor=actor)
    except Exception:  # noqa: BLE001
        log.exception("carrier %s screening failed", carrier_id)


# ---------------------------------------------------------------------------------------------- companies + carriers
def _kyc_hit_key(query: str, h: dict) -> tuple:  # same key as services.kyc._hit_key
    return ((query or "").strip().lower(), h.get("list") or "", str(h.get("ref") or h.get("matched_name") or ""))


async def screen_company(company_id: str, trigger: str = "manual", actor: dict | None = None) -> dict:
    """Re-screen a customer (name, beneficial owners, signatory). A new potential match sets sanctions_status
    potential_match (new bookings stop) and opens a sanctions_review card; KYC's earlier false-positive decisions hold."""
    c = await db.companies.find_one({"id": company_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Company not found")
    parties = [_company_party(c)]
    for u in c.get("ubos") or []:
        parties.append(_party("beneficial_owner", u.get("name"), country=u.get("country"), kind="person", source="kyc"))
    parties.append(_party("signatory", (c.get("signatory") or {}).get("name"), kind="person", source="kyc"))
    kyc_cleared = {"|".join(x) for x in ((c.get("kyc") or {}).get("cleared_hits") or []) if isinstance(x, (list, tuple))}
    status, rows, versions = await _screen(_dedupe(parties), extra_cleared=kyc_cleared)
    row = await _store_row("company", c["id"], c.get("name"), trigger, status, rows, versions, actor, company_id=c["id"])
    if status == "potential_match" and c.get("sanctions_status") == "clear":  # KYC review already covers the others
        await db.companies.update_one({"id": c["id"]}, {"$set": {"sanctions_status": "potential_match",
                                                                "sanctions_rescreen": {"screening_id": row["id"], "at": now_iso()}}})
        card = await _open_card("company", c["id"], c.get("name") or c["id"], row, company_id=c["id"],
                                link=f"/ops/customers/{c['id']}")
        await audit(AGENT, "compliance.hold_set", "company", c["id"], {"screening_id": row["id"], "approval_id": card["id"],
                                                                     "trigger": trigger}, company_id=c["id"])
        await emit("compliance.hold_set", {"company_id": c["id"], "screening": row, "approval": card, "trigger": trigger})
    return await db.party_screenings.find_one({"id": row["id"]}, NO_ID)


async def screen_carrier(carrier_id: str, trigger: str = "manual", actor: dict | None = None) -> dict:
    car = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    if not car:
        raise HTTPException(404, "Carrier not found")
    status, rows, versions = await _screen([_party("carrier", car.get("name"), country=car.get("country"), kind="entity", identity=f"carrier:{car['id']}",
                                                   source="carrier", ref=car.get("code"))])
    row = await _store_row("carrier", car["id"], car.get("name"), trigger, status, rows, versions, actor)
    sets = {"sanctions_screening": {"status": status, "screening_id": row["id"], "at": now_iso()}}
    prev = car.get("sanctions_status")
    if prev != "blocked" and not (prev == "potential_match" and status != "potential_match"):  # a person decides those
        sets["sanctions_status"] = status
    await db.carriers.update_one({"id": car["id"]}, {"$set": sets})
    if status == "potential_match" and prev != "blocked":
        card = await _open_card("carrier", car["id"], car.get("name") or car["id"], row, link=f"/ops/carriers/{car['id']}")
        await audit(AGENT, "compliance.hold_set", "carrier", car["id"], {"screening_id": row["id"], "approval_id": card["id"],
                                                                       "trigger": trigger})
        await emit("compliance.hold_set", {"carrier_id": car["id"], "screening": row, "approval": card, "trigger": trigger})
    return await db.party_screenings.find_one({"id": row["id"]}, NO_ID)


# ---------------------------------------------------------------------------------------------- decisions
async def _remember_clearances(row: dict, actor: dict, approval_id: str):
    for p in row.get("parties") or []:
        for h in p.get("hits") or []:
            if h.get("previously_cleared"):
                continue
            key = _clear_key(p, h)
            if not key:
                continue  # no identity to scope the decision to: the next screening asks a person again
            await db.sanctions_clearances.update_one({"key": key}, {"$set": {
                "key": key, "name": p["name"], "country": p.get("country"), "identity": p.get("identity"),
                "list": h.get("list"), "ref": h.get("ref"), "matched_name": h.get("matched_name"),
                "by": actor_ref(actor), "at": now_iso(), "expires_at": iso(now() + timedelta(days=CLEARANCE_DAYS)),
                "screening_id": row["id"], "approval_id": approval_id}}, upsert=True)


@on("approval.decided")
async def _on_sanctions_decided(p: dict):
    a, actor = p["approval"], p.get("actor") or {}
    if a.get("kind") != "sanctions_review" or a.get("status") not in ("approved", "rejected"):
        return
    outcome = (a.get("decision_data") or {}).get("outcome") or ("false_positive" if a["status"] == "approved" else "confirmed")
    if outcome not in ("false_positive", "confirmed"):
        outcome = "false_positive" if a["status"] == "approved" else "confirmed"
    row = await db.party_screenings.find_one({"id": (a.get("data") or {}).get("screening_id")}, NO_ID)
    decided = {"outcome": outcome, "by": actor_ref(actor), "at": now_iso(), "note": a.get("decision_note"), "approval_id": a["id"]}
    new_status = "cleared" if outcome == "false_positive" else "blocked_sanctions"
    if row:
        await db.party_screenings.update_one({"id": row["id"]}, {"$set": {"decided": decided, "status": new_status}})
        if outcome == "false_positive":
            await _remember_clearances(row, actor, a["id"])
    ref_type, ref_id = a.get("ref_type"), a.get("ref_id")
    if ref_type == "shipment":
        s = await db.shipments.find_one({"id": ref_id}, NO_ID)
        if not s:
            return
        if outcome == "false_positive":
            await _lift_shipment_hold(s, actor, status="cleared")
            await audit(actor, "compliance.hold_cleared", "shipment", ref_id, {"approval_id": a["id"], "outcome": outcome},
                        company_id=s.get("company_id"))
            await emit("compliance.hold_cleared", {"shipment_id": ref_id, "screening": row, "by": actor_ref(actor)})
        else:
            await db.shipments.update_one({"id": ref_id}, {"$set": {"compliance_status": "blocked_sanctions",
                                                                   "compliance_blocked_at": now_iso(), "updated_at": now_iso()}})
            await audit(actor, "compliance.blocked", "shipment", ref_id, {"approval_id": a["id"]}, company_id=s.get("company_id"))
            await notify_role(["ops_agent"], bi(f"Pakotelistaosuma vahvistettu: {s.get('reference')}",
                                                f"Sanctions match confirmed: {s.get('reference')}"),
                              bi("Lähetys pysyy pidätettynä. Älä luovuta tavaraa tai varoja; ota yhteyttä vaatimustenmukaisuuteen.",
                                 "The shipment stays held. Do not release goods or funds; contact compliance."),
                              link=f"/ops/shipments/{ref_id}", kind="compliance")
            await emit("compliance.blocked", {"shipment_id": ref_id, "screening": row, "by": actor_ref(actor)})
    elif ref_type == "company":
        if outcome == "false_positive":
            keys = [list(_kyc_hit_key(pp["name"], h)) for pp in (row or {}).get("parties") or [] for h in pp.get("hits") or []]
            await db.companies.update_one({"id": ref_id, "sanctions_status": "potential_match"},
                                          {"$set": {"sanctions_status": "clear"}})
            if keys:
                await db.companies.update_one({"id": ref_id}, {"$addToSet": {"kyc.cleared_hits": {"$each": keys}}})
            await emit("compliance.hold_cleared", {"company_id": ref_id, "screening": row, "by": actor_ref(actor)})
        else:
            await db.companies.update_one({"id": ref_id}, {"$set": {"sanctions_status": "blocked", "status": "blocked"}})
            await notify_role(["ops_agent"], bi("Asiakkaan pakotelistaosuma vahvistettu", "Customer sanctions match confirmed"),
                              same((row or {}).get("subject_label") or ref_id), link=f"/ops/customers/{ref_id}", kind="compliance")
            await emit("compliance.blocked", {"company_id": ref_id, "screening": row, "by": actor_ref(actor)})
        await audit(actor, f"compliance.{'hold_cleared' if outcome == 'false_positive' else 'blocked'}", "company", ref_id,
                    {"approval_id": a["id"]}, company_id=ref_id)
    elif ref_type == "carrier":
        sets = {"sanctions_status": "clear"} if outcome == "false_positive" else {"sanctions_status": "blocked", "active": False}
        await db.carriers.update_one({"id": ref_id}, {"$set": sets})
        await audit(actor, f"compliance.{'hold_cleared' if outcome == 'false_positive' else 'blocked'}", "carrier", ref_id,
                    {"approval_id": a["id"]})


# ---------------------------------------------------------------------------------------------- re-screening
@recurring("compliance.rescreen_all", daily_at="05:00", label_fi="Pakotelistojen uudelleenseulonta",
           label_en="Sanctions re-screening")
async def rescreen_all(j: dict | None = None) -> dict:
    """Re-screen open shipments, KYC-approved customers and active carriers. Automatic runs (list change, daily) stop
    while the kill switch is on; a person's run (payload manual=True) goes ahead."""
    payload = (j or {}).get("payload") or {}
    trigger = payload.get("trigger") or "rescreen"
    if not payload.get("manual") and await autopilot_halted():
        await audit(AGENT, "compliance.rescreen_deferred", "compliance", None, {"trigger": trigger, "reason": "autopilot halted"})
        return {"status": "deferred", "reason": "autopilot halted"}
    actor = payload.get("actor")
    counts = {"shipments": 0, "companies": 0, "carriers": 0, "potential_match": 0, "error": 0}
    async for s in db.shipments.find({"status": {"$nin": list(CLOSED_STATUSES)}}, {"_id": 0, "id": 1, "company_id": 1}):
        row = await screen_shipment(s["id"], trigger, actor=actor)
        counts["shipments"] += 1
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        await audit(AGENT, "compliance.rescreen", "shipment", s["id"], {"screening_id": row["id"], "status": row["status"],
                                                                       "trigger": trigger, "lists": payload.get("lists")},
                    company_id=s.get("company_id"))
    async for c in db.companies.find({"kyc_status": "approved"}, {"_id": 0, "id": 1}):
        row = await screen_company(c["id"], trigger, actor=actor)
        counts["companies"] += 1
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        await audit(AGENT, "compliance.rescreen", "company", c["id"], {"screening_id": row["id"], "status": row["status"],
                                                                      "trigger": trigger}, company_id=c["id"])
    async for car in db.carriers.find({"active": {"$ne": False}}, {"_id": 0, "id": 1}):
        row = await screen_carrier(car["id"], trigger, actor=actor)
        counts["carriers"] += 1
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        await audit(AGENT, "compliance.rescreen", "carrier", car["id"], {"screening_id": row["id"], "status": row["status"],
                                                                        "trigger": trigger})
    log.info("rescreen_all (%s): %s", trigger, counts)
    await db.compliance_runs.update_one({"name": "compliance.rescreen_all.last"}, {"$set": {
        "name": "compliance.rescreen_all.last", "trigger": trigger, "finished_at": now_iso(), "result": counts}}, upsert=True)
    return {"status": "done", "trigger": trigger, **counts}


@on("sanctions.lists_changed")
async def _on_lists_changed(p: dict):
    key = "rescreen:" + ",".join(f"{c['list']}={c['version']}" for c in p.get("changes") or [])
    await enqueue_or_run("compliance.rescreen_all", {"trigger": "list_change", "lists": p.get("lists")}, key=key)
