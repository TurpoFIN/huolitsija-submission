"""One-time correction of demo shipment K6 (HUO-2026-000006 on live, Pohjolan Kaluste, Shanghai), r3 review 2026-10-24.

The customs seed generated each demo company's documents from one fixed catalogue for whichever import shipment sorted
first. On live that was K6, an LED lamp consignment from Shanghai held because "the invoice lists 52 cartons, the B/L 48",
so it got Vietnamese oak furniture documents, a 71-carton declaration and 71 cartons on the shipment. When that
declaration is still a draft, K6's seeded documents are regenerated from its own goods (invoice 52 cartons, B/L 48, no
packing list, as its hold says) and the same declaration is redrafted from them; the shipment's cargo follows the
documents. Nothing else is touched; a declaration already in review or filed is left alone and reported."""
import logging
import random

from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, now_iso

log = logging.getLogger(__name__)
SEED_DOC_TYPES = ("commercial_invoice", "packing_list", "bill_of_lading")


def goods_match(items: list[dict], lines: list[tuple]) -> bool:
    """Whether a declaration's items are the catalogue's goods (same descriptions, in any order)."""
    want = {ln[0].strip().lower() for ln in lines}
    have = {(it.get("description") or "").strip().lower() for it in items or []}
    return bool(have) and have == want


@on_startup(order=72)
async def repair_demo_k6_goods():
    # :1 ran on live at 765ad42 and stood down: K6's documents still said "released" from before domain_data_repair
    # moved the declaration back to draft. :2 first brings used_by up to date (data_repair_r3b).
    marker = "r3-demo-k6-goods:2"
    if await db.system.find_one({"key": marker}):
        return
    from core.audit import agent_actor
    from services.data_repair_r3b import resync_document_use
    from services.documents import delete_document
    from services.seed_customs import CATALOG, SEED_KEY_CATALOG, seed_shipment

    result = {"key": marker, "at": now_iso()}
    s = await db.shipments.find_one({"demo": True, "seed_key": "K6"}, NO_ID)
    decl = s and await db.customs_declarations.find_one({"shipment_id": s["id"], "type": "import", "demo": True}, NO_ID)
    key = SEED_KEY_CATALOG["K6"]
    if not s or not decl:
        result["outcome"] = "no K6 declaration"
    elif goods_match(decl.get("items"), CATALOG[key]["lines"]):
        result["outcome"] = "already K6's own goods"
    elif decl.get("status") not in ("draft", "needs_info"):
        result["outcome"] = f"left alone: declaration is {decl.get('status')}"
    else:
        seeded = {"shipment_id": s["id"], "demo": True, "source": "seed", "type": {"$in": list(SEED_DOC_TYPES)}}
        await resync_document_use(seeded)
        docs = await db.documents.find(seeded, NO_ID).to_list(20)
        in_use = sorted({u.get("status") for d in docs for u in d.get("used_by") or []} - {"draft", "needs_info", None})
        if in_use:
            result["outcome"] = f"left alone: documents used by a declaration that is {', '.join(in_use)}"
            log.info("demo K6 goods repair: %s", result["outcome"])
            await db.system.insert_one(result)
            return
        actor = agent_actor("demo_seed")
        removed = []
        for doc in docs:
            await delete_document(doc, actor)
            removed.append(doc.get("filename"))
        company = await db.companies.find_one({"id": s["company_id"]}, NO_ID)
        await seed_shipment(s, company, key, "draft", random.Random(20261024))
        after = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID) or {}
        result.update(outcome="regenerated", shipment=s.get("reference"), declaration=decl.get("lrn"), removed=removed,
                      items=[it.get("description") for it in after.get("items") or []],
                      packages=(after.get("totals") or {}).get("packages"))
    log.info("demo K6 goods repair: %s", {k: v for k, v in result.items() if k not in ("key", "at")})
    await db.system.insert_one(result)
