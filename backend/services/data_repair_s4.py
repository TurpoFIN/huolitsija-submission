"""One-time corrections after the s4 live journey (2026-10-24). Idempotent; each runs once per database (marker).

1. Legs stamped by pre-departure milestones (D3). Before 5d1e59b "picked up" set ATD and "received at terminal" set ATA
   on a sea, air or rail leg with no pickup leg before it, so a shipment that had just sailed showed 100 % at the
   destination. Only stamps that exactly equal those milestones' times are moved; road and pickup legs, real arrivals
   and delivered or cancelled shipments are left alone.
2. Demo user names (D10). The UI showed the demo ops user as "Esimerkkikäyttäjä" while notes written at the time say
   "Ops Demo". Users now carry the display names; stored copies of the old names are rewritten (not the audit log,
   whose exact values the UI already maps). The claim approval reason that named the permission code is reworded.
3. Harbour legs at the airport (D3 follow-up). Quote legs took whichever location record shares a UN/LOCODE, so a sea
   leg to FIHEL could end at Helsinki-Vantaa airport, and the truck leg after it start there. Those endpoints move to
   the harbour and the shipment legs are re-routed. Air legs and the trucks after them keep the airport."""
import logging
import re

from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, now_iso

log = logging.getLogger(__name__)

ARRIVALS = ("arrived", "transshipment", "delivered")
LEGACY_DEMO_NAMES = {"Ops Demo": "Esimerkkikäyttäjä", "Partner Demo": "Esimerkkikumppani", "Customer Demo": "Asiakkaan edustaja"}
_NAME_RE = re.compile(r"\b(" + "|".join(map(re.escape, LEGACY_DEMO_NAMES)) + r")\b")
NAME_COLLECTIONS = ("companies", "approvals", "shipments", "tracking_events", "customs_declarations", "exceptions", "claims",
                    "documents", "notifications", "messages", "threads", "invoices", "quotes", "shipment_notes")


def _status(lg: dict) -> str:
    return "completed" if lg.get("ata") else "in_progress" if lg.get("atd") else "planned"


def unstamp_premature_legs(s: dict) -> bool:
    """Fix the legs of one shipment in place; True when something changed."""
    if s.get("status") in ("delivered", "cancelled"):
        return False
    done = [m for m in s.get("milestones") or [] if m.get("at")]
    at_of = lambda code: {m["at"] for m in done if m.get("code") == code}  # noqa: E731
    received, picked, arrivals = at_of("received"), at_of("picked_up"), {m["at"] for m in done if m.get("code") in ARRIVALS}
    departed = sorted((m for m in done if m.get("code") == "departed"), key=lambda m: m["at"])
    legs = s.get("legs") or []
    changed = False
    for i, lg in enumerate(legs):
        if lg.get("role") == "pickup" or lg.get("mode") == "road":
            continue
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        before = (lg.get("atd"), lg.get("ata"))
        if lg.get("ata") and lg["ata"] in received and lg["ata"] not in arrivals:
            lg["ata"] = None  # "received at terminal" is before this leg departs
        if lg.get("atd") and lg["atd"] in picked:
            dep = next((m for m in departed if m["at"] >= lg["atd"] and m.get("leg_seq") in (None, lg["seq"], (nxt or {}).get("seq"))), None)
            if dep and nxt and nxt.get("atd") == dep["at"] and not nxt.get("ata") and lg.get("ata") in (None, dep["at"]):
                # "departed" went to the next leg because this one already had an ATD: it was this leg that sailed
                nxt["atd"], nxt["status"] = None, "planned"
                lg["ata"] = None
            lg["atd"] = dep["at"] if dep else None
        if (lg.get("atd"), lg.get("ata")) != before:
            lg["status"] = _status(lg)
            changed = True
    return changed


@on_startup(order=67)
async def repair_premature_leg_stamps():
    marker = "s4-premature-leg-stamps:1"
    if await db.system.find_one({"key": marker}):
        return
    from services import positions

    fixed = []
    async for s in db.shipments.find({"status": {"$nin": ["delivered", "cancelled"]}, "milestones.code": {"$in": ["picked_up", "received"]}},
                                      NO_ID):
        if unstamp_premature_legs(s):
            await db.shipments.update_one({"id": s["id"]}, {"$set": {"legs": s["legs"], "updated_at": now_iso()}})
            fixed.append(s.get("reference"))
            try:
                await positions.refresh_shipment(s["id"], publish_always=False)
            except Exception:  # noqa: BLE001 - the position loop recomputes it within minutes anyway
                log.exception("position refresh after leg repair failed for %s", s.get("reference"))
    log.info("leg stamp repair: %d shipments %s", len(fixed), fixed[:20])
    await db.system.insert_one({"key": marker, "at": now_iso(), "shipments": fixed})


def rename_legacy_demo_names(value):
    """Deep copy with the old demo user names replaced (exact values and inside composed notes)."""
    if isinstance(value, dict):
        return {k: rename_legacy_demo_names(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_legacy_demo_names(v) for v in value]
    if isinstance(value, str) and _NAME_RE.search(value):
        return _NAME_RE.sub(lambda m: LEGACY_DEMO_NAMES[m.group(1)], value)
    return value


@on_startup(order=68)
async def repair_demo_user_names():
    marker = "s4-demo-user-names:1"
    if await db.system.find_one({"key": marker}):
        return
    counts = {}
    for name in NAME_COLLECTIONS:
        n = 0
        async for row in db[name].find({}, NO_ID):
            new = rename_legacy_demo_names(row)
            if new != row and row.get("id"):
                await db[name].update_one({"id": row["id"]}, {"$set": {k: v for k, v in new.items() if new[k] != row.get(k)}})
                n += 1
        if n:
            counts[name] = n
    r = await db.approvals.update_many({"reason": "Claim decisions need claims.approve"},
                                       {"$set": {"reason": "Claim decisions need a person allowed to decide claims"}})
    if r.modified_count:
        counts["approval_reasons"] = r.modified_count
    log.info("demo user name repair: %s", counts)
    await db.system.insert_one({"key": marker, "at": now_iso(), "updated": counts})


def _at(e: dict, pt: dict | None) -> bool:
    return bool(pt) and e.get("lat") is not None and abs(e["lat"] - pt["lat"]) < 1e-4 and abs(e["lon"] - pt["lon"]) < 1e-4


def airport_points_on_harbour_legs(legs: list[dict], coords: dict) -> list[tuple[int, str, dict]]:
    """(leg index, "from"|"to", point) for each endpoint to move: a sea or rail leg that ends or starts at the airport
    record of a UN/LOCODE (coords from pricing._coordinates_by_kind), and the road leg joined to it there."""
    moves = []
    for i, lg in enumerate(legs):
        if lg.get("mode") not in ("sea", "rail"):
            continue
        for end, j, other in (("from", i - 1, "to"), ("to", i + 1, "from")):
            e = lg.get(end) or {}
            c = coords.get(e.get("unlocode") or "") or {}
            target = (c.get("port") if lg["mode"] == "sea" else None) or c.get("primary")
            if not _at(e, c.get("airport")) or not target or _at(e, target):
                continue
            moves.append((i, end, target))
            if 0 <= j < len(legs) and legs[j].get("mode") == "road" and _at(legs[j].get(other) or {}, c["airport"]):
                moves.append((j, other, target))
    return moves


@on_startup(order=69)
async def repair_airport_points_on_harbour_legs():
    marker = "s4-harbour-leg-points:1"
    if await db.system.find_one({"key": marker}):
        return
    from services import geo, positions, pricing

    shipments = await db.shipments.find({"status": {"$nin": ["delivered", "cancelled"]}}, {"_id": 0, "id": 1, "reference": 1, "legs": 1}).to_list(None)
    quotes = await db.quotes.find({"options.legs": {"$exists": True}}, {"_id": 0, "id": 1, "options": 1}).to_list(None)
    codes = {(lg.get(end) or {}).get("unlocode") for s in shipments for lg in s.get("legs") or [] for end in ("from", "to")}
    codes |= {(lg.get(end) or {}).get("unlocode") for q in quotes for o in q.get("options") or [] for lg in o.get("legs") or []
              for end in ("from", "to")}
    await pricing.load_network_coordinates({c for c in codes if c})
    coords = pricing._LOC_OVERRIDE
    fixed = {"shipments": [], "quotes": 0}
    for s in shipments:
        moves = airport_points_on_harbour_legs(s.get("legs") or [], coords)
        if not moves:
            continue
        for i, end, pt in moves:
            s["legs"][i][end] = {**s["legs"][i][end], **pt}
        try:
            for i in sorted({i for i, _, _ in moves}):
                lg = s["legs"][i]
                info = await geo.route_info_async(lg["mode"], lg["from"], lg["to"])
                lg.update(path=info["path"], path_source=info["source"], path_note=info["note"], distance_km=info["distance_km"],
                          corridors=info.get("corridors") or [], path_version=geo.PATH_VERSION)
        except Exception:  # noqa: BLE001 - keep the old geometry rather than half a fix
            log.exception("re-routing %s after the harbour point fix failed", s.get("reference"))
            continue
        dist = round(sum(lg.get("distance_km") or 0 for lg in s["legs"]), 1)
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"legs": s["legs"], "distance_km": dist, "updated_at": now_iso()}})
        fixed["shipments"].append(s.get("reference"))
        try:
            await positions.refresh_shipment(s["id"], publish_always=False)
        except Exception:  # noqa: BLE001
            log.exception("position refresh after the harbour point fix failed for %s", s.get("reference"))
    for q in quotes:
        changed = False
        for o in q.get("options") or []:
            for i, end, pt in airport_points_on_harbour_legs(o.get("legs") or [], coords):
                o["legs"][i][end] = {**o["legs"][i][end], **pt}
                changed = True
        if changed:
            await db.quotes.update_one({"id": q["id"]}, {"$set": {"options": q["options"]}})
            fixed["quotes"] += 1
    log.info("harbour leg point repair: %s", fixed)
    await db.system.insert_one({"key": marker, "at": now_iso(), **fixed})
