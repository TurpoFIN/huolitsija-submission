"""Authority decision ledger. Estimated declaration totals are never payment evidence."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from core.audit import actor_ref, audit
from core.db import db
from core.events import emit
from core.util import NO_ID, now_iso


def vat_decision_values(decl: dict, body: dict, vat_cents: int) -> dict:
    """Explicit document-backed accounting inputs; never derive them from estimates or payer."""
    values = {"currency": "EUR", "importer_business_id": None,
              "vat_base_cents": None, "deductible_vat_cents": None,
              "vat_base_source": None, "deductible_vat_source": None}
    if body.get("currency", "EUR") != "EUR":
        raise HTTPException(400, "Customs decision accounting amounts must be in EUR")
    owner = body.get("importer_business_id")
    if owner is not None:
        if not isinstance(owner, str) or not owner.strip() or len(owner.strip()) > 64 or not body.get("document_id"):
            raise HTTPException(400, "The importer business ID requires the customs decision document")
        values["importer_business_id"] = owner.strip()
    for field, source_field in (("vat_base_cents", "vat_base_source"),
                                ("deductible_vat_cents", "deductible_vat_source")):
        amount = body.get(field)
        if amount is None:
            continue
        if type(amount) is not int or not 0 <= amount <= 10_000_000_000:
            raise HTTPException(400, "Decision amounts must be explicit non-negative whole cents")
        source = body.get(source_field)
        if not isinstance(source, dict) or any(not isinstance(source.get(k), str) or not source[k].strip()
                                               for k in ("document_id", "basis")):
            raise HTTPException(400, "Each VAT accounting amount requires a source document and assessment basis")
        if values["importer_business_id"] is None:
            raise HTTPException(400, "VAT accounting amounts require the decision importer business ID")
        if (decl.get("submission") or {}).get("channel") == "sandbox" or decl.get("sandbox"):
            raise HTTPException(400, "Simulated decisions cannot provide authoritative VAT accounting amounts")
        if field == "deductible_vat_cents" and amount > vat_cents:
            raise HTTPException(400, "Deductible VAT cannot exceed the decided import VAT")
        values[field] = amount
        values[source_field] = {k: source[k].strip() for k in ("document_id", "basis")}
    return values


def decision_values(decl: dict, body: dict) -> dict:
    from services.customs import mrn_ok

    mrn = str(body.get("mrn") or "").strip().upper()
    if not mrn_ok(mrn) or mrn != decl.get("mrn"):
        raise HTTPException(400, "MRN must match the accepted declaration")
    try:
        decided = date.fromisoformat(body.get("decision_date") or "")
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter the customs decision date")
    if decided > datetime.now(ZoneInfo("Europe/Helsinki")).date():
        raise HTTPException(400, "The decision date cannot be in the future")
    expected = {it["seq"] for it in decl.get("items") or []}
    rows = body.get("items") or []
    if not expected or any(not isinstance(r, dict) or type(r.get("seq")) is not int for r in rows) or len(rows) != len(expected) or {r.get("seq") for r in rows} != expected:
        raise HTTPException(400, "Enter decided duty and import VAT for every goods item exactly once")
    clean = []
    for r in rows:
        for field in ("duty_cents", "vat_cents"):
            v = r.get(field)
            if type(v) is not int or not 0 <= v <= 10_000_000_000:
                raise HTTPException(400, "Decision amounts must be explicit non-negative whole cents")
        clean.append({k: r[k] for k in ("seq", "duty_cents", "vat_cents")})
    paid_by = body.get("paid_by")
    if paid_by not in ("forwarder", "importer"):
        raise HTTPException(400, "Record who pays the customs debt: forwarder or importer")
    return {"mrn": mrn, "decision_date": decided.isoformat(), "items": clean, "paid_by": paid_by,
            "duty_cents": sum(r["duty_cents"] for r in clean), "vat_cents": sum(r["vat_cents"] for r in clean),
            "source": "tulli_decision", "document_id": body.get("document_id"),
            **vat_decision_values(decl, body, sum(r["vat_cents"] for r in clean))}


async def record_decision(decl: dict, body: dict, actor: dict) -> dict:
    if decl.get("status") not in ("accepted", "under_control", "released", "closed"):
        raise HTTPException(409, "Record an accepted MRN before entering the customs decision")
    if decl.get("customs_decision"):
        raise HTTPException(409, "A decision is already recorded; correct it through a credit note and customs amendment")
    decision = decision_values(decl, body)
    if decision.get("document_id") and not await db.documents.find_one({"id": decision["document_id"],
            "shipment_id": decl.get("shipment_id"), "type": "customs_decision"}):
        raise HTTPException(400, "Choose the customs decision document for this shipment")
    decision.update(recorded_at=now_iso(), recorded_by=actor_ref(actor))
    for field in ("vat_base_source", "deductible_vat_source"):
        source = decision.get(field)
        if source is None:
            continue
        if not await db.documents.find_one({"id": source["document_id"], "shipment_id": decl.get("shipment_id"),
                                             "type": {"$in": ["customs_decision", "other"]}}):
            raise HTTPException(400, "Choose a VAT decision or assessment document for this shipment")
        source.update(recorded_at=decision["recorded_at"], recorded_by=decision["recorded_by"])
    result = await db.customs_declarations.update_one({"id": decl["id"], "customs_decision": {"$exists": False}},
                {"$set": {"customs_decision": decision, "updated_at": now_iso()}})
    if not result.modified_count:
        raise HTTPException(409, "A decision was recorded by someone else; reload")
    await audit(actor, "customs_decision_recorded", "customs_declaration", decl["id"], decision,
                company_id=decl.get("company_id"))
    decl = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
    await emit("customs.decision_recorded", {"declaration": decl, "actor": actor})
    return decl
