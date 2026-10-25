"""Document agent: every uploaded document is read (services.extraction: the exact CSV/XLSX reader, else the AI
reader) and this agent decides what happens next. A clean read above the confidence floor is accepted and the
customs draft follows from it (services.customs._auto_draft); a read that needs a person goes to the owner queue;
an unreadable file is asked again from the customer."""
from core.agent_runtime import decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import enqueue, job
from core.settings import get_settings
from core.util import NO_ID
from services.agents.base import customer_message

AGENT = "document_agent"
NEEDED = {"commercial_invoice", "packing_list", "bill_of_lading", "sea_waybill", "air_waybill", "cmr", "cim",
          "certificate_of_origin", "eur1"}


@job("documents.review", agent=True, max_attempts=4, timeout=120, label_fi="Asiakirjan tarkistus", label_en="Document review")
async def review(j):
    doc = await db.raw.documents.find_one({"id": j["payload"]["document_id"]}, NO_ID)
    if not doc or doc.get("demo"):
        return {"skipped": "no document"}
    ex = doc.get("extraction") or {}
    if ex.get("status") not in ("done", "needs_review", "failed"):
        return {"skipped": ex.get("status")}
    if (doc.get("agent_review") or {}).get("extraction_at") == ex.get("completed_at"):
        return {"skipped": "already reviewed"}
    settings = await get_settings()
    floor = float(settings.get("guard_confidence_floor") or 0.85)
    conf = ex.get("confidence")
    extra = []
    if ex.get("status") != "done":
        extra.append(policy_reason("needs_review", "Lukija ei saanut kaikkia tietoja varmasti: " + (ex.get("reason") or ""),
                                   "The reader could not read everything with certainty: " + (ex.get("reason") or "")))
    errors = [w for w in ex.get("warnings") or [] if w.get("severity") == "error"]
    for w in errors[:3]:
        extra.append(policy_reason(f"check_{w.get('code')}", (w.get("message_i18n") or {}).get("fi") or w.get("message"),
                                   w.get("message")))
    label = f"{doc.get('filename')} ({doc.get('type')})"
    d = await decide(AGENT, "accept_extraction", subject=("document", doc["id"]), subject_label=label,
                     company_id=doc.get("company_id"),
                     inputs={"type": doc.get("type"), "method": ex.get("method"), "source": ex.get("source"),
                             "warnings": [w.get("code") for w in ex.get("warnings") or []],
                             "fields": sorted((ex.get("fields") or {}).keys()), "items": len(ex.get("line_items") or [])},
                     decision={"accept": True}, confidence=conf if conf is not None else 0.0, model=ex.get("model"),
                     extra_reasons=extra, approval_kind="document_review" if doc.get("type") in NEEDED else None,
                     title=f"Check document {doc.get('filename')}", title_fi=f"Tarkista asiakirja {doc.get('filename')}",
                     summary=ex.get("reason") or "", link=f"/ops/shipments/{doc['shipment_id']}" if doc.get("shipment_id") else None,
                     approval_data={"document_id": doc["id"], "shipment_id": doc.get("shipment_id")})
    await db.raw.documents.update_one({"id": doc["id"]}, {"$set": {"agent_review": {
        "decision_id": d.id, "outcome": d.outcome, "extraction_at": ex.get("completed_at"),
        "approval_id": d.approval and d.approval["id"]}}})
    unreadable = ex.get("status") == "failed" or "no readable text" in (ex.get("reason") or "")
    if unreadable and doc.get("shipment_id") and doc.get("company_id"):
        await customer_message(
            doc["company_id"], doc["shipment_id"],
            f"Emme saaneet luettua tiedostoa {doc.get('filename')}. Lataa alkuperäinen PDF, CSV tai XLSX "
            f"(ei kuvakaappausta) lähetyksen Asiakirjat-välilehdelle.",
            f"We could not read the file {doc.get('filename')}. Please upload the original PDF, CSV or XLSX "
            f"(not a screenshot) on the shipment's Documents tab.",
            subject=f"{doc.get('filename')}", key=f"doc-unreadable:{doc['id']}")
    return {"outcome": d.outcome, "confidence": conf, "floor": floor}


@on("document.extracted")
async def _on_extracted(p):
    doc = p.get("document") or {}
    if doc.get("demo") or not doc.get("id"):
        return
    ex = doc.get("extraction") or {}
    await enqueue("documents.review", {"document_id": doc["id"]}, company_id=doc.get("company_id"),
                  subject=("document", doc["id"]), key=f"docreview:{doc['id']}:{ex.get('completed_at')}")
