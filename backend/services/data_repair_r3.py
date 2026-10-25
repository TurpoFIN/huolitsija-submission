"""One-time corrections after the s1 r3 review (2026-10-24). Idempotent; each runs once per database (marker).

N1. A quote option whose freight lines all moved to not_included (the Incoterm leaves the main carriage to the other
    party: an importer under DAP, an exporter under FCA) was auto-sent at the 35 EUR document fee and could be booked.
    Sent and accepted ones go back to ops review through the quote approval policy (reason no_freight), and the demo
    customer gets a sent quote that does charge freight in place of the withdrawn one."""
import logging

from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, now_iso

log = logging.getLogger(__name__)


@on_startup(order=70)
async def withdraw_quotes_without_freight():
    marker = "r3-no-freight-quotes:1"
    if await db.system.find_one({"key": marker}):
        return
    from core.settings import get_settings
    from routes import quotes as v2
    from services.domain_data_repair import ensure_demo_sent_quote

    settings = await get_settings()
    withdrawn = []
    async for q in db.quotes.find({"status": {"$in": ["sent", "accepted"]}, "options.0": {"$exists": True}}, NO_ID):
        if all(v2.charges_freight(o) for o in q["options"]):
            continue
        before = q["status"]
        await v2.apply_approval_policy(q, settings, None, send=True)
        await db.quotes.update_one({"id": q["id"], "status": before}, {"$set": {
            "status": q["status"], "approval": q.get("approval"), "published": q.get("published"), "accepted_option_id": None,
            "withdrawn": {"at": now_iso(), "from": before, "reason": "no_freight"}, "updated_at": now_iso()}})
        withdrawn.append(q.get("number"))
    replacement = await ensure_demo_sent_quote()
    log.info("no-freight quote repair: %d withdrawn to review %s; demo replacement %s", len(withdrawn), withdrawn[:20], replacement)
    await db.system.insert_one({"key": marker, "at": now_iso(), "withdrawn": withdrawn, "demo_replacement": replacement})
