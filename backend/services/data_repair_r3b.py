"""One-time cleanup after the 765ad42 deploy (2026-10-24 17:21 UTC), where two server processes ran the startup
repairs at the same time (core/lifecycle.py now serialises them).

- Pending approvals: request_approval keeps one pending approval per (kind, record); the race opened two for 12
  withdrawn quotes. The one the record points to stays, the copies go.
- Notifications: the same notification to the same person within a few seconds is a copy; the first stays.
- Demo replacement quote: ensure_demo_sent_quote copied the demo quote twice (Q-2026-000063 and -000064 on live);
  the later copy goes.
- Document use: documents keep a copy of each declaration's status in used_by. domain_data_repair (order 65) moved
  declarations back to draft without updating it, so documents of a draft (K6's among them) still said "released" and
  could not be replaced; used_by now follows the declarations again."""
import logging
from datetime import datetime

from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, now_iso

log = logging.getLogger(__name__)
COPY_WINDOW_S = 10  # copies from the race were written within the same few seconds


def _ts(value: str | None) -> float:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def resync_document_use(query: dict | None = None) -> int:
    """Set each document's used_by statuses to its declarations' current status; drop entries of deleted ones."""
    changed = 0
    async for d in db.documents.find({**(query or {}), "used_by.0": {"$exists": True}}, NO_ID):
        ids = [u.get("declaration_id") for u in d["used_by"]]
        status = {x["id"]: x.get("status") async for x in db.customs_declarations.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "status": 1})}
        fresh = [{**u, "status": status[u["declaration_id"]]} for u in d["used_by"] if u.get("declaration_id") in status]
        if fresh != d["used_by"]:
            await db.documents.update_one({"id": d["id"]}, {"$set": {"used_by": fresh}})
            changed += 1
    return changed


async def dedupe_pending_approvals() -> list[str]:
    groups: dict[tuple, list[dict]] = {}
    async for a in db.approvals.find({"status": "pending"}, NO_ID).sort("created_at", 1):
        groups.setdefault((a.get("kind"), a.get("ref_id")), []).append(a)
    removed = []
    for (kind, ref_id), items in groups.items():
        if len(items) < 2:
            continue
        keep = items[0]
        if kind == "quote_threshold":
            q = await db.quotes.find_one({"id": ref_id}, {"_id": 0, "approval": 1}) or {}
            keep = next((a for a in items if a["id"] == (q.get("approval") or {}).get("approval_id")), keep)
        for a in items:
            if a["id"] != keep["id"]:
                await db.approvals.delete_one({"id": a["id"], "status": "pending"})
                removed.append(a.get("title") or a["id"])
    return removed


async def dedupe_notifications() -> int:
    last: dict[tuple, float] = {}
    removed = 0
    async for n in db.notifications.find({}, NO_ID).sort("created_at", 1):
        key = (n.get("user_id"), n.get("title"), n.get("body"), n.get("link"), n.get("kind"))
        t = _ts(n.get("created_at"))
        if key in last and t - last[key] <= COPY_WINDOW_S:
            await db.notifications.delete_one({"id": n["id"]})
            removed += 1
            continue
        last[key] = t
    return removed


async def dedupe_demo_replacement_quotes() -> list[str]:
    """Same demo company, both sent, never booked, same options, created within seconds: the later one (the higher
    number when the seconds match) is a copy."""
    kept: list[dict] = []
    removed = []
    async for q in db.quotes.find({"demo": True, "status": "sent", "shipment_id": None}, NO_ID).sort([("created_at", 1), ("number", 1)]):
        sig = [(o.get("carrier_name"), o.get("service"), o.get("total_cents")) for o in q.get("options") or []]
        twin = next((k for k in kept if k["company_id"] == q.get("company_id") and k["sig"] == sig
                     and _ts(q.get("created_at")) - k["t"] <= COPY_WINDOW_S), None)
        if twin:
            await db.quotes.delete_one({"id": q["id"], "status": "sent"})
            removed.append(q.get("number"))
        else:
            kept.append({"company_id": q.get("company_id"), "sig": sig, "t": _ts(q.get("created_at"))})
    return removed


@on_startup(order=71)
async def clean_up_startup_race():
    marker = "r3-startup-race:1"
    if await db.system.find_one({"key": marker}):
        return
    result = {"key": marker, "at": now_iso(),
              "documents_resynced": await resync_document_use(),
              "approvals_removed": await dedupe_pending_approvals(),
              "notifications_removed": await dedupe_notifications(),
              "demo_quotes_removed": await dedupe_demo_replacement_quotes()}
    log.info("startup race cleanup: %s", {k: v for k, v in result.items() if k not in ("key", "at")})
    await db.system.insert_one(result)
