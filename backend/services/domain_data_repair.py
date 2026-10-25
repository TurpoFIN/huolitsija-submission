"""Idempotent correction of known generated records, never customer source documents."""
from core.audit import audit
from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, new_id, now_iso


@on_startup(order=65)
async def repair_generated_domain_data():
    from services import customs
    from services.seed_billing import _link_quote

    marker = "domain-data-repair:1"
    if await db.system.find_one({"key": marker}):
        return
    # Only recognize test accounts from the review's explicit naming convention.
    test_ids = set()
    async for u in db.users.find({"email": {"$regex": r"^(e2e[.+]|test[.+]|kuvatesti[.+])", "$options": "i"}}, NO_ID):
        if u.get("company_id"):
            test_ids.add(u["company_id"])
    async for c in db.companies.find({"name": {"$regex": r"^(E2E Testi |Kuvatesti )", "$options": "i"}}, NO_ID):
        test_ids.add(c["id"])
    if test_ids:
        await db.companies.update_many({"id": {"$in": list(test_ids)}}, {"$set": {"test_tenant": True}})
    async for c in db.companies.find({"demo": True, "kyc.decided_by": "seed"}, NO_ID):
        await db.companies.update_one({"id": c["id"]}, {"$set": {"kyc_status": "not_started", "kyc": {},
                                      "sanctions_status": "not_screened"}})
        await audit(None, "seed_kyc_reset_unverified", "company", c["id"], {"reason": "No registry/sanctions checks were run"}, company_id=c["id"])
    async for d in db.customs_declarations.find({"demo": True}, NO_ID):
        s = await db.shipments.find_one({"id": d.get("shipment_id")}, NO_ID)
        if not s:
            continue
        # Align the generated shipment to its generated invoice/packing data; no independent rounding.
        cargo = {**(s.get("cargo") or {}), "packages": sum(i.get("packages") or 0 for i in d.get("items") or []),
                 "weight_kg": round(sum(i.get("gross_kg") or 0 for i in d.get("items") or []), 2),
                 "net_weight_kg": round(sum(i.get("net_kg") or 0 for i in d.get("items") or []), 2)}
        if cargo["packages"] and cargo["weight_kg"]:
            await db.shipments.update_one({"id": s["id"]}, {"$set": {"cargo": cargo}})
        d = await customs.recompute(d)
        if customs.blocking(d) and d.get("status") in ("approved", "submitted", "accepted", "released", "closed") or s.get("status") == "customs_hold":
            d.update(status="needs_info" if customs.blocking(d, "customer") else "draft", mrn=None,
                     submission=None, released_at=None, accepted_at=None, approved_at=None, approved_by=None, approval_id=None,
                     history=[{"at": now_iso(), "to": "draft", "from": d.get("status"), "by": {"name": "Järjestelmä"},
                               "note": "Asiakirjat odottavat tarkistusta"}])
        await db.customs_declarations.update_one({"id": d["id"]}, {"$set": {k: v for k, v in d.items() if k != "_id"}})
        await db.documents.update_many({"used_by.declaration_id": d["id"]}, {"$set": {"used_by.$[u].status": d["status"]}},
                                       array_filters=[{"u.declaration_id": d["id"]}])  # as customs.transition does
    await db.quotes.update_many({"demo": True}, {"$set": {"published": True}})
    # Ensure customer quote/booking surfaces are populated from the real pricing engine.
    async for s in db.shipments.find({"demo": True, "quote_id": {"$in": [None, ""]}}, NO_ID).limit(12):
        await _link_quote(s)
    await ensure_demo_sent_quote()
    await db.system.insert_one({"key": marker, "at": now_iso(), "test_tenants": len(test_ids)})


async def ensure_demo_sent_quote() -> str | None:
    """The demo customer has one bookable sent quote: a copy of a demo quote whose every option charges freight (the
    first one with options used to be the DAP import lane, priced at the 35 EUR document fee). Returns its number."""
    from routes.quotes import charges_freight
    from services.seed_core import demo_company_id
    cid = demo_company_id("kaluste")
    if await db.quotes.find_one({"company_id": cid, "demo": True, "status": "sent"}):
        return None
    source = None
    async for cand in db.quotes.find({"company_id": cid, "demo": True, "options.0": {"$exists": True}}, NO_ID).sort("created_at", 1):
        if all(charges_freight(o) for o in cand["options"]):
            source = cand
            break
    if not source:
        return None
    from copy import deepcopy
    from datetime import timedelta
    from core.util import iso, now
    from core.refs import next_ref
    q = deepcopy(source)
    q.update(id=new_id(), number=await next_ref("Q"), status="sent", published=True,
             shipment_id=None, shipment_reference=None, accepted_option_id=None, approval=None,
             valid_until=iso(now() + timedelta(days=14)), created_at=now_iso(), updated_at=now_iso())
    for o in q["options"]:
        o["valid_until"] = q["valid_until"]
    await db.quotes.insert_one(q)
    return q["number"]


def _customer_seed_text(value):
    """Clean only generated display labels; provenance flags and source documents stay intact."""
    if isinstance(value, dict):
        return {k: _customer_seed_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_customer_seed_text(v) for v in value]
    if isinstance(value, str):
        if value.startswith("Demo values: freight and insurance"):
            return "Freight and insurance are valuation estimates; verify them against transport invoices."
        return {"Demo seed": "Järjestelmä", "Customer Demo": "Asiakkaan edustaja", "Ops Demo": "Huolitsija",
                "Huolitsija ops (demo)": "Huolitsija"}.get(value, value.replace(" (demo)", "").replace("(demo value — ", "("))
    return value


@on_startup(order=66)
async def repair_generated_display_labels():
    marker = "domain-customer-seed-labels:1"
    if await db.system.find_one({"key": marker}):
        return
    for collection, fields in (("customs_declarations", ("history", "office", "location_of_goods", "valuation", "items")),
                               ("shipments", ("milestones", "holds")),
                               ("tracking_events", ("by", "note")),
                               ("messages", ("author",))):
        async for row in db[collection].find({"demo": True}, NO_ID):
            changes = {k: _customer_seed_text(row[k]) for k in fields if k in row}
            if changes:
                await db[collection].update_one({"id": row["id"]}, {"$set": changes})
    await db.system.insert_one({"key": marker, "at": now_iso()})
