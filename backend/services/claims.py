"""Cargo claims: liability cap per regime in SDR + EUR at a dated IMF rate, time bar, decision via approval.

Regime table is data (limits change: Montreal is revised by ICAO every five years). Sources:
- NSAB 2015 / PSYM 2015: 8.33 SDR/kg (forwarder as contracting party), SDR 50,000 per assignment as intermediary,
  delay up to the freight, notice of concealed damage within 7 days (§ 27), time bar 1 year (§ 28), venue § 29.
- CMR art. 23: 8.33 SDR/kg; delay up to carriage charges (art. 23(5)); notice art. 30: 7 days concealed, 21 days delay.
- Montreal 1999 art. 22(3): 26 SDR/kg for carriage from 28 Dec 2024 (ICAO 2024 review; 22 from 28 Dec 2019,
  19 from 30 Dec 2009); notice art. 31: 14 days damage, 21 days delay; time bar art. 35: 2 years.
- Finnish Maritime Code 674/1994 13:30 (Hague-Visby): 667 SDR per package or 2 SDR/kg, whichever is higher.
- CIM (COTIF) art. 30: 17 SDR/kg; delay up to four times the carriage charge (art. 33).
The forwarder's own time bar (PSYM § 28, 1 year from delivery) is the binding deadline shown (RULE N-5); the
mode regime's time bar is shown alongside when it differs.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.approvals import request_approval
from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import on
from core.refs import next_ref
from core.util import NO_ID, new_id, now_iso
from services.billing import to_date, today_fi

log = logging.getLogger("claims")

register_index("claims", "id", unique=True)
register_index("claims", [("company_id", 1), ("status", 1)])
register_index("claims", "shipment_id")

TYPES = ("damage", "loss", "delay")
STATUSES = ("draft", "submitted", "in_review", "approved", "rejected", "paid")

REGIMES: dict[str, dict] = {
    "NSAB": {"label": "NSAB 2015 (PSYM 2015)", "per_kg": [("2015-01-01", 8.33)], "intermediary_sdr": 50_000,
             "delay": "freight", "time_bar_years": 1, "notice_days": {"damage": 7, "loss": None, "delay": None},
             "basis": "PSYM 2015 § 22–23", "basis_fi": "PSYM 2015 § 22–23", "notice_source": "PSYM 2015 § 27"},
    "CMR": {"label": "CMR", "per_kg": [("1956-05-19", 8.33)], "delay": "freight", "time_bar_years": 1,
            "notice_days": {"damage": 7, "loss": None, "delay": 21}, "basis": "CMR art. 23", "basis_fi": "CMR 23 art.",
            "notice_source": "CMR art. 30"},
    "MONTREAL": {"label": "Montreal 1999", "per_kg": [("2003-11-04", 17.0), ("2009-12-30", 19.0), ("2019-12-28", 22.0),
                                                      ("2024-12-28", 26.0)],
                 "delay": "per_kg", "time_bar_years": 2, "notice_days": {"damage": 14, "loss": None, "delay": 21},
                 "basis": "Montreal Convention art. 22(3), ICAO-revised limit",
                 "basis_fi": "Montrealin yleissopimus 22 art. 3 kohta, ICAO:n tarkistama raja", "notice_source": "Montreal art. 31"},
    "HAGUE_VISBY": {"label": "Hague-Visby / Merilaki", "per_kg": [("1994-01-01", 2.0)], "per_package": 667.0,
                    "delay": None, "time_bar_years": 1, "notice_days": {"damage": 3, "loss": None, "delay": None},
                    "basis": "Merilaki 674/1994 13:30", "basis_fi": "Merilaki 674/1994 13:30", "notice_source": "Hague-Visby art. III(6)"},
    "CIM": {"label": "CIM (COTIF)", "per_kg": [("2006-07-01", 17.0)], "delay": "4x_freight", "time_bar_years": 1,
            "notice_days": {"damage": 7, "loss": None, "delay": None}, "basis": "CIM art. 30, 33", "basis_fi": "CIM 30 ja 33 art.", "notice_source": "CIM art. 47"},
}


class ClaimError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_regime(value: str | None, shipment: dict | None = None) -> str:
    v = (value or "").upper().replace("-", "_").replace(" ", "_")
    for key in REGIMES:
        if key in v:
            return key
    if "HAGUE" in v or "VISBY" in v or "MARITIME" in v:
        return "HAGUE_VISBY"
    # RULE X-3 suggestion by mode
    s = shipment or {}
    mode = s.get("mode")
    oc = ((s.get("origin") or {}).get("country") or "").upper()
    dc = ((s.get("destination") or {}).get("country") or "").upper()
    if mode == "road" and oc and dc and oc != dc:
        return "CMR"
    return {"air": "MONTREAL", "sea": "HAGUE_VISBY", "rail": "CIM"}.get(mode or "", "NSAB")


def _per_kg(regime: dict, on: date) -> float:
    rate = regime["per_kg"][0][1]
    for valid_from, r in regime["per_kg"]:
        if on >= date.fromisoformat(valid_from):
            rate = r
    return rate


def _d2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _num_fi(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _packages(n: int, lang: str) -> str:
    if lang == "fi":
        return f"{n} kolli" if n == 1 else f"{n} kollia"
    return f"{n} package" if n == 1 else f"{n} packages"


def compute_cap(*, regime_key: str, claim_type: str, gross_kg: float | None, packages: int | None,
                forwarder_role: str | None, freight_cents: int | None, eur_per_sdr: float | None,
                on: date | None = None) -> dict:
    """Liability cap. cap_sdr for weight/package limits; cap_eur_cents = cap_sdr * EUR/SDR (or the freight limit)."""
    regime = REGIMES[regime_key]
    on = on or today_fi()
    notes: list[str] = []
    cap_sdr: float | None = None
    cap_eur_cents: int | None = None
    basis, basis_fi = regime["basis"], regime["basis_fi"]
    kg = float(gross_kg or 0)
    role = (forwarder_role or "principal").lower()
    if role == "intermediary":
        cap_sdr = float(REGIMES["NSAB"]["intermediary_sdr"])
        basis = "PSYM 2015 § 23 (intermediary): SDR 50,000 per assignment"
        basis_fi = "PSYM 2015 § 23 (välitysliike): 50 000 SDR toimeksiantoa kohden"
        notes.append(f"As intermediary we answer for choosing and instructing the carrier; the carrier's own liability "
                     f"under {regime['label']} is claimed from the carrier.")
    elif claim_type == "delay":
        mode = regime.get("delay")
        if mode == "freight":
            cap_eur_cents = int(freight_cents) if freight_cents else None
            basis = f"{regime['basis']}: delay limited to the freight charged"
            basis_fi = f"{regime['basis_fi']}: viivästysvastuu enintään veloitetun rahdin määrä"
        elif mode == "4x_freight":
            cap_eur_cents = int(freight_cents) * 4 if freight_cents else None
            basis = "CIM art. 33: delay limited to four times the carriage charge"
            basis_fi = "CIM 33 art.: viivästysvastuu enintään nelinkertainen kuljetusmaksu"
        elif mode == "per_kg":
            cap_sdr = _d2(_per_kg(regime, on) * kg)
            basis = f"{regime['basis']}: {_per_kg(regime, on):g} SDR/kg also covers delay"
            basis_fi = f"{regime['basis_fi']}: {_num_fi(_per_kg(regime, on))} SDR/kg kattaa myös viivästyksen"
        else:
            basis = f"{regime['label']} has no delay liability limit for goods; assessed under the contract"
            basis_fi = f"{regime['label']}: tavaran viivästykselle ei ole vastuunrajoitusta; arvioidaan sopimuksen mukaan"
            notes.append("No convention limit for delay under this regime.")
        if mode in ("freight", "4x_freight") and not freight_cents:
            notes.append("Freight amount unknown, so the delay limit cannot be shown in EUR yet.")
    else:
        per_kg = _per_kg(regime, on)
        if regime_key == "HAGUE_VISBY":
            n = int(packages or 0)
            by_pkg = float(regime["per_package"]) * n
            by_kg = per_kg * kg
            cap_sdr = _d2(max(by_pkg, by_kg))
            basis = (f"{regime['basis']}: max({regime['per_package']:g} SDR × {_packages(n, 'en')}, "
                     f"{per_kg:g} SDR × {kg:g} kg)")
            basis_fi = (f"{regime['basis_fi']}: suurempi arvoista {_num_fi(regime['per_package'])} SDR × {_packages(n, 'fi')} "
                        f"ja {_num_fi(per_kg)} SDR × {_num_fi(kg)} kg")
            if not packages:
                notes.append("Package count missing; the per-kg limit was used.")
        else:
            cap_sdr = _d2(per_kg * kg)
            basis = f"{regime['basis']}: {per_kg:g} SDR × {kg:g} kg gross"
            basis_fi = f"{regime['basis_fi']}: {_num_fi(per_kg)} SDR × {_num_fi(kg)} kg bruttopainoa"
        if not kg:
            notes.append("Gross weight of the affected goods is needed for the limit.")
        if regime_key != "NSAB":
            notes.append("NSAB 2015 § 2 network clause: the mandatory law of the transport stage applies where the loss "
                         "occurred in that stage.")
    if cap_sdr is not None and eur_per_sdr:
        cap_eur_cents = int(Decimal(str(cap_sdr)) * Decimal(str(eur_per_sdr)) * 100)
    return {"regime": regime_key, "regime_label": regime["label"], "basis": basis, "basis_fi": basis_fi, "cap_sdr": cap_sdr,
            "cap_eur_cents": cap_eur_cents, "notes": notes, "per_kg_sdr": _per_kg(regime, on)}


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def deadlines(*, regime_key: str, claim_type: str, delivery: date | None, notified: date | None) -> dict:
    regime = REGIMES[regime_key]
    out = {"delivery_date": delivery.isoformat() if delivery else None, "time_bar_date": None,
           "time_bar_alert_date": None, "regime_time_bar_date": None, "notice_deadline": None, "notice_late": False,
           "notice_source": regime["notice_source"]}
    if not delivery:
        return out
    tb = _add_years(delivery, 1)  # RULE N-5: PSYM 2015 § 28, one year from delivery
    out["time_bar_date"] = tb.isoformat()
    out["time_bar_alert_date"] = (tb - timedelta(days=20)).isoformat()
    if regime["time_bar_years"] != 1:
        out["regime_time_bar_date"] = _add_years(delivery, regime["time_bar_years"]).isoformat()
    days = regime["notice_days"].get(claim_type)
    if days:
        nd = delivery + timedelta(days=days)
        out["notice_deadline"] = nd.isoformat()
        out["notice_late"] = bool(notified and notified > nd)
    return out


async def _freight_cents(shipment: dict) -> int | None:
    """Freight charged = non-pass-through net of the shipment's invoices, else the shipment price."""
    total = 0
    async for inv in db.invoices.find({"shipment_id": shipment["id"], "kind": "invoice", "status": {"$ne": "void"}},
                                      {"_id": 0, "services_net_cents": 1}):
        total += int(inv.get("services_net_cents") or 0)
    if total:
        return total
    return (shipment.get("price") or {}).get("total_cents")


async def estimate(shipment: dict, *, claim_type: str, gross_kg: float | None, packages: int | None) -> dict:
    from services.sdr import get_rate

    if claim_type not in TYPES:
        raise ClaimError("Claim type must be damage, loss or delay")
    regime_key = normalize_regime(shipment.get("liability_regime"), shipment)
    rate = await get_rate()
    delivery = to_date(shipment.get("delivered_at") or _milestone_at(shipment, "DELIVERED")) or (
        to_date(shipment.get("eta")) if claim_type == "loss" else None)
    cap = compute_cap(regime_key=regime_key, claim_type=claim_type, gross_kg=gross_kg, packages=packages,
                      forwarder_role=shipment.get("forwarder_role"), freight_cents=await _freight_cents(shipment),
                      eur_per_sdr=(rate or {}).get("eur_per_sdr"), on=delivery or today_fi())
    dl = deadlines(regime_key=regime_key, claim_type=claim_type, delivery=delivery, notified=today_fi())
    return {**cap, **dl, "forwarder_role": shipment.get("forwarder_role") or "principal",
            "regime_source": "shipment" if shipment.get("liability_regime") else "suggested by mode (RULE X-3)",
            "sdr_rate": {"eur_per_sdr": rate["eur_per_sdr"], "date": rate["date"], "source": rate["source"],
                         "stale": rate.get("stale", False)} if rate else None}


def _milestone_at(shipment: dict, code: str) -> str | None:
    for m in reversed(shipment.get("milestones") or []):
        if (m.get("code") or "").upper() == code and m.get("at") and not m.get("planned"):
            return m["at"]
    return None


# --------------------------------------------------------------------------------------------- lifecycle

def _validate(body: dict, s: dict) -> None:
    """Field rules shared by create and draft edits. Raises ClaimError with a sentence the customer can act on."""
    import math

    from services.billing import to_date, today_fi

    ctype = body.get("type")
    if ctype not in TYPES:
        raise ClaimError("Claim type must be damage, loss or delay")
    desc = (body.get("description") or "").strip()
    if len(desc) < 10:
        raise ClaimError("Describe what happened (at least a sentence)")
    amount = body.get("amount_claimed_cents")
    if amount is None or not isinstance(amount, (int, float)) or not math.isfinite(amount) or int(amount) <= 0:
        raise ClaimError("Enter the amount claimed")
    if int(amount) > 10_000_000_000:
        raise ClaimError("The amount claimed is above 100 000 000 €; contact claims@ to file it")
    kg = body.get("gross_kg_affected")
    if kg is not None and (not isinstance(kg, (int, float)) or not math.isfinite(kg) or kg < 0):
        raise ClaimError("Gross weight must be a number of kilograms")
    if ctype in ("damage", "loss") and not kg:
        raise ClaimError("Enter the gross weight of the affected goods in kg")
    total_kg = ((s.get("cargo") or {}).get("weight_kg") or 0)
    if kg and total_kg and float(kg) > float(total_kg) * 1.001:
        raise ClaimError(f"Affected weight is more than the shipment's gross weight ({total_kg} kg)")
    pk = body.get("packages_affected")
    if pk is not None and (not isinstance(pk, (int, float)) or pk < 0 or int(pk) != pk):
        raise ClaimError("Packages affected must be a whole number")
    if body.get("notice_given_on"):
        n = to_date(body["notice_given_on"])
        if not n:
            raise ClaimError("Give the notice date as YYYY-MM-DD")
        if n > today_fi():
            raise ClaimError("The notice date cannot be in the future")
        booked = to_date(s.get("created_at"))
        if booked and n < booked - timedelta(days=1):
            raise ClaimError(f"The notice date is before the shipment was booked ({booked.isoformat()})")
    if body.get("date_of_loss"):
        d = to_date(body["date_of_loss"])
        if not d:
            raise ClaimError("Give the date of loss as YYYY-MM-DD")
        if d > today_fi():
            raise ClaimError("The date of loss cannot be in the future")
        booked = to_date(s.get("created_at"))
        if booked and d < booked - timedelta(days=1):
            raise ClaimError(f"The date of loss is before the shipment was booked ({booked.isoformat()})")


async def create_claim(body: dict, actor: dict, *, company_id: str | None) -> dict:
    s = await db.shipments.find_one({"id": body.get("shipment_id")}, NO_ID)
    if not s or (company_id and s.get("company_id") != company_id):
        raise ClaimError("Shipment not found", 404)
    _validate(body, s)
    ctype = body["type"]
    desc = body["description"].strip()
    amount = body["amount_claimed_cents"]
    kg = body.get("gross_kg_affected")
    docs = await _check_docs(body.get("document_ids") or [], s["company_id"])
    est = await estimate(s, claim_type=ctype, gross_kg=kg, packages=body.get("packages_affected"))
    cl = {
        "id": new_id(), "number": await next_ref("HUO-CLM"), "shipment_id": s["id"], "shipment_reference": s.get("reference"),
        "company_id": s["company_id"], "company_name": s.get("customer_name"), "type": ctype, "description": desc[:5000],
        "amount_claimed_cents": int(amount), "currency": "EUR", "gross_kg_affected": float(kg) if kg else None,
        "packages_affected": int(body["packages_affected"]) if body.get("packages_affected") else None,
        "date_of_loss": body.get("date_of_loss"), "notice_given_on": body.get("notice_given_on") or None,
        "liability_regime": est["regime"], "regime_label": est["regime_label"],
        "regime_source": est["regime_source"], "forwarder_role": est["forwarder_role"], "cap_sdr": est["cap_sdr"],
        "cap_eur_cents": est["cap_eur_cents"], "cap_basis": est["basis"], "cap_basis_fi": est.get("basis_fi"), "cap_notes": est["notes"],
        "sdr_rate": est["sdr_rate"], "notified_at": None, "delivery_date": est["delivery_date"],
        "time_bar_date": est["time_bar_date"], "time_bar_alert_date": est["time_bar_alert_date"],
        "regime_time_bar_date": est["regime_time_bar_date"], "notice_deadline": est["notice_deadline"],
        "notice_late": False, "notice_source": est["notice_source"], "status": "draft", "approval_id": None,
        "decision": None, "proposal": None, "document_ids": [d["id"] for d in docs], "documents": docs,
        "created_at": now_iso(), "updated_at": now_iso(), "created_by": actor_ref(actor), "demo": bool(s.get("demo")),
        "history": [{"at": now_iso(), "by": actor.get("name"), "action": "created"}],
    }
    if not cl["company_name"]:
        c = await db.companies.find_one({"id": s["company_id"]}, {"_id": 0, "name": 1})
        cl["company_name"] = (c or {}).get("name")
    await db.claims.insert_one(dict(cl))
    await audit(actor, "claim_created", "claim", cl["id"], {"number": cl["number"]}, company_id=cl["company_id"])
    if body.get("submit"):
        return await submit_claim(cl["id"], actor)
    return cl


async def _check_docs(ids: list[str], company_id: str) -> list[dict]:
    out = []
    for i in ids:
        d = await db.documents.find_one({"id": i}, {"_id": 0, "id": 1, "company_id": 1, "filename": 1, "type": 1})
        if not d or d.get("company_id") != company_id:
            raise ClaimError("An attached document was not found", 404)
        out.append({"id": d["id"], "filename": d.get("filename"), "type": d.get("type")})
    return out


async def update_claim(claim_id: str, body: dict, actor: dict) -> dict:
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise ClaimError("Claim not found", 404)
    changes: dict = {}
    if "document_ids" in body:  # evidence can be added until a decision is made
        if cl["status"] in ("approved", "rejected", "paid"):
            raise ClaimError("The claim is decided; documents can no longer be added", 409)
        docs = await _check_docs(body["document_ids"] or [], cl["company_id"])
        changes.update(document_ids=[d["id"] for d in docs], documents=docs)
    editable = {"description", "amount_claimed_cents", "gross_kg_affected", "packages_affected", "date_of_loss", "type",
                "notice_given_on"}
    if editable & set(body):
        if cl["status"] != "draft":
            raise ClaimError("Only draft claims can be edited", 409)
        for k in editable & set(body):
            changes[k] = body[k]
    if not changes:
        return cl
    merged = {**cl, **changes}
    if editable & set(body):
        s = await db.shipments.find_one({"id": cl["shipment_id"]}, NO_ID) or {}
        _validate(merged, s)
        est = await estimate(s, claim_type=merged["type"], gross_kg=merged.get("gross_kg_affected"),
                             packages=merged.get("packages_affected"))
        changes.update(cap_sdr=est["cap_sdr"], cap_eur_cents=est["cap_eur_cents"], cap_basis=est["basis"], cap_basis_fi=est.get("basis_fi"),
                       cap_notes=est["notes"], sdr_rate=est["sdr_rate"])
    changes["updated_at"] = now_iso()
    await db.claims.update_one({"id": claim_id}, {"$set": changes})
    await audit(actor, "claim_updated", "claim", claim_id, {"fields": sorted(changes)}, company_id=cl["company_id"])
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def submit_claim(claim_id: str, actor: dict) -> dict:
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise ClaimError("Claim not found", 404)
    if cl["status"] != "draft":
        raise ClaimError(f"Claim is already {cl['status']}", 409)
    # Written notice can come before the claim: a reservation on the CMR / delivery receipt or an e-mail at
    # delivery counts (PSYM 2015 § 27); the customer states that date, otherwise the claim itself is the notice.
    notified = min(to_date(cl.get("notice_given_on")) or today_fi(), today_fi())
    delivery = to_date(cl.get("delivery_date"))
    dl = deadlines(regime_key=cl["liability_regime"], claim_type=cl["type"], delivery=delivery, notified=notified)
    if dl["time_bar_date"] and notified > to_date(dl["time_bar_date"]):
        raise ClaimError(f"The one-year time bar passed on {dl['time_bar_date']} (PSYM 2015 § 28)", 409)
    await db.claims.update_one({"id": claim_id}, {"$set": {
        "status": "submitted", "notified_at": notified.isoformat() if cl.get("notice_given_on") else now_iso(),
        "submitted_at": now_iso(), "notice_late": dl["notice_late"], "updated_at": now_iso()},
        "$push": {"history": {"at": now_iso(), "by": actor.get("name"), "action": "submitted"}}})
    from core.notify import bi, eur, notify_role

    type_fi = {"damage": "vaurio", "loss": "katoaminen", "delay": "viivästys"}.get(cl["type"], cl["type"])
    await notify_role("finance", bi(f"Reklamaatio {cl['number']} jätetty", f"Claim {cl['number']} submitted"),
                      bi(f"{cl['company_name']} · {type_fi} · {eur(cl['amount_claimed_cents'], 'fi')}",
                         f"{cl['company_name']} · {cl['type']} · {eur(cl['amount_claimed_cents'], 'en')}"),
                      link=f"/ops/claims?id={claim_id}", kind="claim")
    await audit(actor, "claim_submitted", "claim", claim_id, {"notice_late": dl["notice_late"]}, company_id=cl["company_id"])
    from core.events import emit
    await emit("claim.submitted", {"claim_id": claim_id, "company_id": cl["company_id"]})
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def start_review(claim_id: str, actor: dict) -> dict:
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise ClaimError("Claim not found", 404)
    if cl["status"] != "submitted":
        raise ClaimError(f"Claim is {cl['status']}", 409)
    await db.claims.update_one({"id": claim_id}, {"$set": {"status": "in_review", "reviewer": actor_ref(actor),
                                                           "updated_at": now_iso()},
                                                  "$push": {"history": {"at": now_iso(), "by": actor.get("name"), "action": "review started"}}})
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def propose_decision(claim_id: str, *, decision: str, amount_cents: int | None, note: str, actor: dict,
                           approval_extra: dict | None = None) -> dict:
    """Ops proposes approve(amount)/reject; a person with claims.approve decides it in the approval queue."""
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise ClaimError("Claim not found", 404)
    if cl["status"] not in ("submitted", "in_review"):
        raise ClaimError(f"Claim is {cl['status']}", 409)
    if decision not in ("approve", "reject"):
        raise ClaimError("decision must be approve or reject")
    if not (note or "").strip():
        raise ClaimError("Write the reasoning for the decision")
    if decision == "approve":
        if amount_cents is None or int(amount_cents) <= 0:
            raise ClaimError("Enter the amount to pay")
        if int(amount_cents) > cl["amount_claimed_cents"]:
            raise ClaimError("The amount is more than the customer claimed")
    above_cap = decision == "approve" and cl.get("cap_eur_cents") is not None and int(amount_cents) > cl["cap_eur_cents"]
    proposal = {"decision": decision, "amount_cents": int(amount_cents) if decision == "approve" else 0,
                "note": note.strip(), "by": actor_ref(actor), "at": now_iso(), "above_cap": above_cap}
    appr = await request_approval(
        "claim", "claim", claim_id, f"Claim {cl['number']}: {'pay' if decision == 'approve' else 'reject'}",
        summary=f"{cl['company_name']} · {cl['type']} · claimed {cl['amount_claimed_cents'] / 100:.2f} EUR"
                + (f" · propose {int(amount_cents) / 100:.2f} EUR" if decision == "approve" else " · propose rejection"),
        reason=("Proposed amount is above the liability cap" if above_cap else "Claim decisions need a person allowed to decide claims"),
        company_id=cl["company_id"], requested_by=actor, data=proposal,
        amount_cents=proposal["amount_cents"] or None, link=f"/ops/claims?id={claim_id}",
        extra={"title_fi": f"Reklamaatio {cl['number']}: {'maksetaan' if decision == 'approve' else 'hylätään'}", **(approval_extra or {})})
    await db.claims.update_one({"id": claim_id}, {"$set": {"status": "in_review", "proposal": proposal,
                                                           "approval_id": appr["id"], "updated_at": now_iso()},
                                                  "$push": {"history": {"at": now_iso(), "by": actor.get("name"),
                                                                        "action": f"proposed {decision}"}}})
    await audit(actor, "claim_decision_proposed", "claim", claim_id, proposal, company_id=cl["company_id"])
    return await db.claims.find_one({"id": claim_id}, NO_ID)


async def mark_paid(claim_id: str, *, paid_at: str | None, reference: str | None, actor: dict) -> dict:
    cl = await db.claims.find_one({"id": claim_id}, NO_ID)
    if not cl:
        raise ClaimError("Claim not found", 404)
    if cl["status"] != "approved":
        raise ClaimError("Only approved claims can be marked paid", 409)
    await db.claims.update_one({"id": claim_id}, {"$set": {
        "status": "paid", "payment": {"paid_at": paid_at or now_iso(), "reference": reference, "by": actor_ref(actor)},
        "updated_at": now_iso()}, "$push": {"history": {"at": now_iso(), "by": actor.get("name"), "action": "paid"}}})
    await audit(actor, "claim_paid", "claim", claim_id, {"reference": reference}, company_id=cl["company_id"])
    from core.events import emit
    await emit("claim.paid", {"claim_id": claim_id, "company_id": cl["company_id"]})  # carrier recourse (claims_recourse)
    return await db.claims.find_one({"id": claim_id}, NO_ID)


@on("approval.decided")
async def _on_decided(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != "claim" or a.get("ref_type") != "claim":
        return
    cl = await db.claims.find_one({"id": a["ref_id"]}, NO_ID)
    if not cl or cl["status"] != "in_review":
        return
    actor = p.get("actor") or {}
    proposal = {**(cl.get("proposal") or {}), **(a.get("data") or {})}
    edits = a.get("decision_data") or {}
    if a["status"] == "approved":
        amount = int(edits.get("amount_cents") or proposal.get("amount_cents") or 0)
        final = "approved" if proposal.get("decision") == "approve" else "rejected"
        decision = {"amount_cents": amount if final == "approved" else 0, "note": a.get("decision_note") or proposal.get("note"),
                    "decided_by": {"id": actor.get("id"), "name": actor.get("name")}, "decided_at": now_iso(),
                    "proposal_note": proposal.get("note")}
        await db.claims.update_one({"id": cl["id"]}, {"$set": {"status": final, "decision": decision, "updated_at": now_iso()},
                                                      "$push": {"history": {"at": now_iso(), "by": actor.get("name"), "action": final}}})
        from core.notify import bi, eur, notify_company, same

        approved = final == "approved"
        await notify_company(cl["company_id"],
                             bi(f"Reklamaatio {cl['number']}: " + ("hyväksytty" if approved else "hylätty"),
                                f"Claim {cl['number']}: " + ("approved" if approved else "rejected")),
                             bi(f"Korvaus {eur(amount, 'fi')}", f"Compensation {eur(amount, 'en')}") if approved
                             else same((decision["note"] or "")[:300]),
                             link="/portal/claims", email=not cl.get("demo"), kind="claim")
    else:
        await db.claims.update_one({"id": cl["id"]}, {"$set": {"proposal": None, "approval_id": None, "updated_at": now_iso(),
                                                               "last_proposal_declined": {"note": a.get("decision_note"),
                                                                                          "by": actor.get("name"), "at": now_iso()}},
                                                      "$push": {"history": {"at": now_iso(), "by": actor.get("name"),
                                                                            "action": "proposal declined"}}})
