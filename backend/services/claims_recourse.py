"""Carrier recourse for paid cargo claims: once we have paid the customer, claim it back from the carrier of the leg.

    paid claim -> recourse draft (carrier of the claim's leg, else the main leg; the leg's regime cap in SDR + EUR at the
    stored IMF rate via services.claims.compute_cap; amount = min(paid to the customer, carrier cap); letter fi + en)
    -> owner queue card (kind carrier_recourse, core.agent_runtime.decide: always a person, never auto)
    -> approved: the letter goes to the carrier only with an e-mail address AND carriers.recourse_contract set;
       otherwise it stays "approved" with awaiting = carrier_contact / carrier_contract until ops send it
    -> recoveries recorded by ops (recovered); closed when settled or declined.

Carrier regime by leg mode (the carrier's own liability, not the forwarder's NSAB): road CMR art. 23 (8.33 SDR/kg),
sea Hague-Visby / Merilaki 13:30 (667 SDR per package or 2 SDR/kg, whichever is higher), air Montreal art. 22(3)
(26 SDR/kg from 28 Dec 2024), rail CIM art. 30 (17 SDR/kg). Limits come from services.claims.REGIMES, not from here.
State lives on the claim: claim.recourse = {status, carrier_id, carrier_name, regime, cap_sdr, cap_eur_cents,
amount_cents, letter_fi, letter_en, approval_id, recovered_cents, recoveries, history, ...}.
"""
from __future__ import annotations

import logging

from core.agent_runtime import agent_bot, decide, policy_reason
from core.audit import actor_ref, audit
from core.db import db
from core.events import on
from core.util import NO_ID, now_iso
from services import claims as svc
from services.billing import to_date, today_fi

log = logging.getLogger("claims_recourse")

AGENT = "claims_agent"
KIND = "carrier_recourse"
STATUSES = ("draft", "pending_approval", "approved", "sent", "recovered", "closed")
PAY_DAYS = 30
MODE_REGIME = {"road": "CMR", "sea": "HAGUE_VISBY", "air": "MONTREAL", "rail": "CIM"}
DOC_NO_KEYS = {"sea": ("bl_no", "B/L"), "air": ("awb_no", "AWB"), "road": ("cmr_no", "CMR"), "rail": ("cim_no", "CIM")}


class RecourseError(svc.ClaimError):
    pass


def _leg(shipment: dict, claim: dict) -> dict | None:
    """The leg named by the claim (claim.leg_seq), else the main leg (kind/role main, else the longest, as accruals)."""
    legs = shipment.get("legs") or []
    seq = claim.get("leg_seq")
    if seq is not None:
        named = next((lg for lg in legs if str(lg.get("seq")) == str(seq)), None)
        if named:
            return named
    if not legs:
        return None
    return next((lg for lg in legs if lg.get("kind") == "main"), None) or next(
        (lg for lg in legs if lg.get("role") == "main"), None) or max(legs, key=lambda lg: lg.get("distance_km") or 0)


def _regime(leg: dict | None, shipment: dict) -> str | None:
    """The carrier's own regime: the leg's liability_regime when it names a carrier convention, else by leg mode."""
    lr = svc.normalize_regime((leg or {}).get("liability_regime"), {}) if (leg or {}).get("liability_regime") else None
    if lr and lr != "NSAB":
        return lr
    return MODE_REGIME.get((leg or {}).get("mode") or shipment.get("mode") or "")


def _doc_no(leg: dict | None, shipment: dict) -> tuple[str | None, str | None]:
    """(label, number) of the transport document: the leg's own, else the shipment reference for that mode."""
    mode = (leg or {}).get("mode") or shipment.get("mode") or ""
    key, label = DOC_NO_KEYS.get(mode, (None, "Rahtikirja / Waybill"))
    no = (leg or {}).get("transport_doc_no") or ((shipment.get("references") or {}).get(key) if key else None)
    return (label, no) if no else (None, None)


def _paid_cents(cl: dict) -> int:
    return int((cl.get("decision") or {}).get("amount_cents") or 0)


def _eur_fi(cents: int | None) -> str:
    if cents is None:
        return "–"
    return f"{cents / 100:,.2f} €".replace(",", " ").replace(".", ",")


def _eur_en(cents: int | None) -> str:
    return "–" if cents is None else f"EUR {cents / 100:,.2f}"


def _letters(cl: dict, s: dict, r: dict) -> tuple[dict, dict]:
    """Claim letter to the carrier, Finnish and English: a template, no AI."""
    from core import config

    ref = s.get("reference") or cl.get("shipment_reference") or ""
    doc_label, doc_no = r.get("transport_doc_label"), r.get("transport_doc_no")
    loss = cl.get("date_of_loss") or cl.get("delivery_date") or "–"
    what_fi = {"damage": "vaurioitumisesta", "loss": "katoamisesta", "delay": "viivästymisestä"}.get(cl["type"], cl["type"])
    what_en = {"damage": "damage to", "loss": "loss of", "delay": "delay of"}.get(cl["type"], cl["type"])
    docs = [d.get("filename") or d.get("id") for d in cl.get("documents") or []]
    docs_fi = docs + [f"Korvauspäätös {cl['number']} ja maksutosite asiakkaalle"]
    docs_en = docs + [f"Settlement of claim {cl['number']} and proof of payment to the customer"]
    if doc_no:
        docs_fi.append(f"{doc_label} {doc_no}")
        docs_en.append(f"{doc_label} {doc_no}")
    basis_fi = r.get("cap_basis_fi") or r.get("cap_basis")
    cap_fi = (f"{str(r['cap_sdr']).removesuffix('.0').replace('.', ',')} SDR ≈ {_eur_fi(r['cap_eur_cents'])} ({basis_fi})"
              if r.get("cap_eur_cents") is not None
              else f"vastuunrajoitusta ei voitu laskea ({basis_fi or 'kuljetusmuoto puuttuu'})")
    cap_en = (f"{r['cap_sdr']:g} SDR ≈ {_eur_en(r['cap_eur_cents'])} ({r['cap_basis']})" if r.get("cap_eur_cents") is not None
              else f"the liability limit could not be computed ({r.get('cap_basis') or 'transport mode unknown'})")
    rate = r.get("sdr_rate") or {}
    rate_fi = (f" SDR-kurssi {str(rate['eur_per_sdr']).replace('.', ',')} EUR ({rate.get('date')}, IMF)."
               if rate.get("eur_per_sdr") else "")
    rate_en = f" SDR rate {rate['eur_per_sdr']} EUR ({rate.get('date')}, IMF)." if rate.get("eur_per_sdr") else ""
    carrier = r.get("carrier_name") or "–"
    subject_fi = f"Takautumisvaatimus: lähetys {ref}" + (f", {doc_label} {doc_no}" if doc_no else "") + f" ({cl['number']})"
    subject_en = f"Recourse claim: shipment {ref}" + (f", {doc_label} {doc_no}" if doc_no else "") + f" ({cl['number']})"
    body_fi = "\n".join([
        f"{carrier}",
        "",
        f"Vaadimme korvausta lähetyksen {ref} {what_fi}, joka tapahtui kuljetuksenne aikana.",
        f"Lähetys: {ref}" + (f" · {doc_label}: {doc_no}" if doc_no else ""),
        f"Vahinkopäivä: {loss}",
        f"Olemme korvanneet asiakkaallemme {_eur_fi(_paid_cents(cl))} (reklamaatio {cl['number']}).",
        f"Vaadittu määrä: {_eur_fi(r['amount_cents'])}",
        f"Vastuunrajoitus ({r.get('regime_label') or '–'}): {cap_fi}.{rate_fi}",
        f"Pyydämme maksamaan vaaditun määrän {PAY_DAYS} päivän kuluessa tämän kirjeen päiväyksestä tai ilmoittamaan "
        f"kantanne samassa ajassa. Viitteeksi {cl['number']}.",
        "",
        "Liitteet:",
        *[f"- {d}" for d in docs_fi],
        "",
        f"Yhteys: {config.COMPANY_EMAIL}",
    ])
    body_en = "\n".join([
        f"{carrier}",
        "",
        f"We claim compensation for the {what_en} shipment {ref}, which occurred while in your charge.",
        f"Shipment: {ref}" + (f" · {doc_label}: {doc_no}" if doc_no else ""),
        f"Date of loss: {loss}",
        f"We have compensated our customer {_eur_en(_paid_cents(cl))} (claim {cl['number']}).",
        f"Amount claimed: {_eur_en(r['amount_cents'])}",
        f"Liability limit ({r.get('regime_label') or '–'}): {cap_en}.{rate_en}",
        f"Please pay the amount claimed within {PAY_DAYS} days of the date of this letter or state your position within "
        f"the same time. Please quote {cl['number']}.",
        "",
        "Documents attached:",
        *[f"- {d}" for d in docs_en],
        "",
        f"Contact: {config.COMPANY_EMAIL}",
    ])
    return {"subject": subject_fi, "body": body_fi}, {"subject": subject_en, "body": body_en}


async def build(cl: dict) -> dict:
    """The recourse draft for a paid claim: carrier, regime, cap, amount and letters. Writes nothing."""
    from services.sdr import get_rate

    s = await db.shipments.find_one({"id": cl["shipment_id"]}, NO_ID) or {}
    leg = _leg(s, cl)
    carrier_id = (leg or {}).get("carrier_id")
    carrier_name = (leg or {}).get("carrier_name") or (leg or {}).get("carrier")
    carrier = await db.carriers.find_one({"id": carrier_id}, NO_ID) if carrier_id else None
    if not carrier and carrier_name:
        carrier = await db.carriers.find_one({"name": carrier_name}, NO_ID)
    if carrier:
        carrier_id, carrier_name = carrier.get("id"), carrier.get("name") or carrier_name
    regime = _regime(leg, s)
    paid = _paid_cents(cl)
    notes: list[str] = []
    cap = {"cap_sdr": None, "cap_eur_cents": None, "basis": None, "basis_fi": None, "regime_label": None, "notes": []}
    rate = None
    if regime:
        rate = await get_rate()
        on = to_date(cl.get("date_of_loss")) or to_date(cl.get("delivery_date")) or today_fi()
        # The carrier answers as a carrier (principal); the forwarder's intermediary limit is not the carrier's.
        cap = svc.compute_cap(regime_key=regime, claim_type=cl["type"], gross_kg=cl.get("gross_kg_affected"),
                              packages=cl.get("packages_affected"), forwarder_role="principal",
                              freight_cents=(leg or {}).get("cost_cents"), eur_per_sdr=(rate or {}).get("eur_per_sdr"),
                              on=on)
        notes += [n for n in cap["notes"] if not n.startswith("NSAB 2015 § 2")]
        if cap["cap_sdr"] is not None and cap["cap_eur_cents"] is None:
            notes.append("No SDR rate on file, so the cap is in SDR only; the full amount paid is claimed.")
    else:
        notes.append("The leg's transport mode is unknown, so no carrier liability limit was applied.")
    if not carrier_name:
        notes.append("No carrier is recorded on the leg; add the carrier before the letter can be sent.")
    cap_eur = cap["cap_eur_cents"]
    amount = min(paid, int(cap_eur)) if cap_eur is not None else paid
    label, no = _doc_no(leg, s)
    r = {
        "status": "draft", "carrier_id": carrier_id, "carrier_name": carrier_name, "leg_seq": (leg or {}).get("seq"),
        "leg_mode": (leg or {}).get("mode") or s.get("mode"), "regime": regime, "regime_label": cap["regime_label"],
        "cap_sdr": cap["cap_sdr"], "cap_eur_cents": cap_eur, "cap_basis": cap["basis"],
        "cap_basis_fi": cap["basis_fi"], "paid_cents": paid,
        "amount_cents": amount, "currency": "EUR", "transport_doc_label": label, "transport_doc_no": no,
        "sdr_rate": {"eur_per_sdr": rate["eur_per_sdr"], "date": rate["date"], "source": rate.get("source")} if rate else None,
        "notes": notes, "carrier_email": _carrier_email(carrier), "carrier_contract": bool((carrier or {}).get("recourse_contract")),
        "approval_id": None, "awaiting": None, "recovered_cents": 0, "recoveries": [], "email_id": None,
    }
    r["letter_fi"], r["letter_en"] = _letters(cl, s, r)
    return r


def _carrier_email(carrier: dict | None) -> str | None:
    c = carrier or {}
    return (c.get("claims_email") or c.get("contact_email") or "").strip() or None


def _hist(actor: dict | None, action: str) -> dict:
    return {"at": now_iso(), "by": (actor or {}).get("name"), "action": action}


async def refresh_letters(cl: dict) -> dict:
    """Re-render the letters of an unsent recourse drafted before the Finnish cap basis existed. Text only: carrier,
    regime, cap, amount and the owner card stay as they are."""
    r = cl.get("recourse") or {}
    if not r or r.get("cap_basis_fi") or r.get("status") not in ("draft", "pending_approval", "approved") or r.get("email_id"):
        return cl
    fresh = await build(cl)
    if (fresh.get("cap_sdr"), fresh.get("amount_cents"), fresh.get("regime")) != (r.get("cap_sdr"), r.get("amount_cents"), r.get("regime")):
        return cl  # the inputs changed since drafting; leave the reviewed draft alone
    r = {**r, "cap_basis": fresh.get("cap_basis"), "cap_basis_fi": fresh.get("cap_basis_fi")}
    r["letter_fi"], r["letter_en"] = _letters(cl, (await db.shipments.find_one({"id": cl["shipment_id"]}, NO_ID)) or {}, r)
    await db.claims.update_one({"id": cl["id"], "recourse.status": r["status"]},
                               {"$set": {"recourse.cap_basis": r["cap_basis"], "recourse.cap_basis_fi": r["cap_basis_fi"],
                                         "recourse.letter_fi": r["letter_fi"],
                                         "recourse.letter_en": r["letter_en"]}})
    return {**cl, "recourse": r}


async def create_recourse(claim_id: str, actor: dict | None = None, *, leg_seq: int | str | None = None) -> dict:
    """Draft the recourse for a paid claim and put it on the owner queue. Idempotent: an open recourse is returned as is;
    only a draft (no card yet) or a declined one is drafted again."""
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise RecourseError("Claim not found", 404)
    if cl["status"] != "paid":
        raise RecourseError("Recourse is opened for paid claims only", 409)
    old = cl.get("recourse") or {}
    if old and not (old.get("status") == "draft" or (old.get("status") == "closed" and old.get("closed_reason") == "declined")):
        return cl
    if _paid_cents(cl) <= 0:
        raise RecourseError("Nothing was paid on this claim", 409)
    if leg_seq is not None:
        cl["leg_seq"] = leg_seq
        await db.claims.update_one({"id": claim_id}, {"$set": {"leg_seq": leg_seq}})
    actor = actor or agent_bot(AGENT)
    r = await build(cl)
    r.update(created_at=now_iso(), updated_at=now_iso(), created_by=actor_ref(actor),
             history=(old.get("history") or []) + [_hist(actor, "drafted")])
    await db.claims.update_one({"id": claim_id}, {"$set": {"recourse": r, "updated_at": now_iso()}})
    who = r["carrier_name"] or "?"
    d = await decide(
        AGENT, "carrier_recourse", subject=("claim", claim_id), subject_label=cl["number"], company_id=cl["company_id"],
        inputs={"paid_cents": r["paid_cents"], "regime": r["regime"], "cap_sdr": r["cap_sdr"], "cap_eur_cents": r["cap_eur_cents"],
                "carrier_id": r["carrier_id"], "leg_seq": r["leg_seq"]},
        decision={"decision": "send_recourse", "amount_cents": r["amount_cents"], "carrier_id": r["carrier_id"]},
        extra_reasons=[policy_reason("recourse_person", "Takautumisvaatimus kuljetusliikkeelle lähtee vasta henkilön hyväksynnällä",
                                     "A recourse claim goes to the carrier only after a person approves it")],
        approval_kind=KIND, title=f"Carrier recourse {cl['number']}: {who}", title_fi=f"Takautumisvaatimus {cl['number']}: {who}",
        summary=f"{who} · {r['regime_label'] or 'no regime'} · paid {_eur_en(r['paid_cents'])} · claim {_eur_en(r['amount_cents'])}",
        summary_fi=f"{who} · {r['regime_label'] or 'ei kuljetusmuotoa'} · maksettu {_eur_fi(r['paid_cents'])} · "
                   f"vaaditaan {_eur_fi(r['amount_cents'])}",
        link=f"/ops/claims?id={claim_id}", amount_cents=r["amount_cents"],
        approval_data={"amount_cents": r["amount_cents"], "carrier_id": r["carrier_id"], "carrier_name": r["carrier_name"]})
    approval_id = (d.approval or {}).get("id")
    await db.claims.update_one({"id": claim_id}, {"$set": {"recourse.status": "pending_approval" if approval_id else "draft",
                                                           "recourse.approval_id": approval_id,
                                                           "recourse.agent_decision_id": d.id, "recourse.updated_at": now_iso()},
                                                  "$push": {"recourse.history": _hist(actor, "sent for approval")}})
    await audit(actor, "claim_recourse_drafted", "claim", claim_id,
                {"carrier_id": r["carrier_id"], "amount_cents": r["amount_cents"], "approval_id": approval_id},
                company_id=cl["company_id"])
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def send_letter(claim_id: str, actor: dict | None) -> dict:
    """Send an approved recourse letter: only with a carrier e-mail address and the carrier's recourse contract flag."""
    from core.email import render, send_email

    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if cl:
        cl = await refresh_letters(cl)
    r = (cl or {}).get("recourse") or {}
    if not r:
        raise RecourseError("No recourse on this claim", 404)
    if r["status"] != "approved":
        raise RecourseError(f"Recourse is {r['status']}", 409)
    carrier = await db.carriers.find_one({"id": r["carrier_id"]}, NO_ID) if r.get("carrier_id") else None
    to, contract = _carrier_email(carrier), bool((carrier or {}).get("recourse_contract"))
    awaiting = None if (to and contract) else ("carrier_contact" if not to else "carrier_contract")
    if awaiting:
        await db.claims.update_one({"id": claim_id}, {"$set": {"recourse.awaiting": awaiting, "recourse.carrier_email": to,
                                                               "recourse.carrier_contract": contract,
                                                               "recourse.updated_at": now_iso()}})
        return await db.claims.find_one({"id": claim_id}, NO_ID)
    fi, en = r["letter_fi"], r["letter_en"]
    subject = f"{fi['subject']} / {en['subject']}"
    html, text = render(subject, fi["body"].split("\n") + ["— In English —"] + en["body"].split("\n"))
    rec = await send_email(to, subject, html, text, tag="claim_recourse", company_id=cl["company_id"],
                           ref={"claim_id": claim_id, "carrier_id": r["carrier_id"], "shipment_id": cl["shipment_id"]})
    ok = rec.get("status") != "failed"
    await db.claims.update_one({"id": claim_id}, {"$set": {
        "recourse.status": "sent" if ok else "approved", "recourse.awaiting": None if ok else "email_failed",
        "recourse.email_id": rec["id"], "recourse.sent_to": to, "recourse.sent_at": now_iso() if ok else None,
        "recourse.carrier_email": to, "recourse.carrier_contract": contract, "recourse.updated_at": now_iso()},
        "$push": {"recourse.history": _hist(actor, f"letter sent to {to}" if ok else f"letter to {to} failed")}})
    await audit(actor, "claim_recourse_sent" if ok else "claim_recourse_send_failed", "claim", claim_id,
                {"to": to, "email_id": rec["id"]}, company_id=cl["company_id"])
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def record_recovery(claim_id: str, *, amount_cents: int, received_on: str | None, reference: str | None,
                          note: str | None, close: bool, actor: dict) -> dict:
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    r = (cl or {}).get("recourse") or {}
    if not r:
        raise RecourseError("No recourse on this claim", 404)
    if r["status"] not in ("approved", "sent", "recovered"):
        raise RecourseError(f"Recourse is {r['status']}; a recovery is recorded after approval", 409)
    if int(amount_cents) <= 0:
        raise RecourseError("Enter the amount received from the carrier")
    if received_on and not to_date(received_on):
        raise RecourseError("Give the date received as YYYY-MM-DD")
    total = int(r.get("recovered_cents") or 0) + int(amount_cents)
    if total > int(r.get("paid_cents") or 0):
        raise RecourseError("Recoveries would exceed the amount paid to the customer")
    row = {"amount_cents": int(amount_cents), "received_on": received_on or today_fi().isoformat(), "reference": reference,
           "note": (note or "").strip() or None, "by": actor_ref(actor), "at": now_iso()}
    status = "closed" if close else "recovered"
    sets = {"recourse.status": status, "recourse.recovered_cents": total, "recourse.updated_at": now_iso()}
    if close:
        sets.update({"recourse.closed_reason": "settled", "recourse.closed_at": now_iso()})
    await db.claims.update_one({"id": claim_id}, {"$set": sets, "$push": {
        "recourse.recoveries": row, "recourse.history": _hist(actor, f"recovered {_eur_en(int(amount_cents))}" + (", closed" if close else ""))}})
    await audit(actor, "claim_recourse_recovered", "claim", claim_id, {"amount_cents": int(amount_cents), "total_cents": total,
                                                                      "closed": close}, company_id=cl["company_id"])
    return await db.claims.find_one({"id": claim_id}, NO_ID)


@on("claim.paid")
async def _on_paid(p: dict):
    cl = await db.claims.find_one({"id": p.get("claim_id")}, {"_id": 0, "id": 1, "demo": 1, "recourse": 1})
    if not cl or cl.get("demo") or cl.get("recourse"):
        return
    try:
        await create_recourse(p["claim_id"])
    except svc.ClaimError as exc:
        log.info("no recourse for claim %s: %s", p.get("claim_id"), exc)


@on("approval.decided")
async def _on_decided(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != KIND or a.get("ref_type") != "claim":
        return
    cl = await db.claims.find_one({"id": a["ref_id"]}, NO_ID)
    r = (cl or {}).get("recourse") or {}
    if not r or r.get("approval_id") != a["id"] or r.get("status") != "pending_approval":
        return
    actor = p.get("actor") or {}
    by = {"id": actor.get("id"), "name": actor.get("name")}
    if a["status"] == "approved":
        await db.claims.update_one({"id": cl["id"]}, {"$set": {"recourse.status": "approved", "recourse.approved_by": by,
                                                               "recourse.approved_at": now_iso(), "recourse.updated_at": now_iso()},
                                                      "$push": {"recourse.history": _hist(actor, "approved")}})
        await send_letter(cl["id"], actor)
    else:
        await db.claims.update_one({"id": cl["id"]}, {"$set": {"recourse.status": "closed", "recourse.closed_reason": "declined",
                                                               "recourse.decline_note": a.get("decision_note"),
                                                               "recourse.closed_at": now_iso(), "recourse.updated_at": now_iso()},
                                                      "$push": {"recourse.history": _hist(actor, "declined")}})
