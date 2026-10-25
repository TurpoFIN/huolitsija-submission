"""Decision-first landing data: customer Overview and ops Today. Reads every slice's
collections through the field names fixed in ARCHITECTURE.md; missing collections yield empty lists."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from core.db import db
from core.deps import current_user, require_customer, require_internal
from core.util import NO_ID, iso, now, now_iso

router = APIRouter(prefix="/api", tags=["portal"])

ACTIVE = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception"]
SHIP_FIELDS = {"_id": 0, "id": 1, "reference": 1, "status": 1, "mode": 1, "origin": 1, "destination": 1, "eta": 1,
               "etd": 1, "customer_name": 1, "company_id": 1, "has_exception": 1, "holds": 1, "progress": 1,
               "legs.carrier": 1, "legs.carrier_id": 1, "milestone": 1, "updated_at": 1, "planned_eta": 1}


def _route(s: dict) -> str:
    return f"{(s.get('origin') or {}).get('name', '—')} → {(s.get('destination') or {}).get('name', '—')}"


@router.get("/portal/overview")
async def portal_overview(user: dict = Depends(require_customer)):
    cid = user["company_id"]
    company = await db.companies.find_one({"id": cid}, NO_ID) or {}
    t_now = now()
    week = iso(t_now + timedelta(days=7))
    actions: list[dict] = []

    if company.get("kyc_status") != "approved":
        actions.append({"kind": "kyc", "title": company.get("name"), "link": "/portal/company", "severity": "warning"})

    active = await db.shipments.find({"company_id": cid, "status": {"$in": ACTIVE}}, SHIP_FIELDS).sort("eta", 1).to_list(200)
    for s in active:
        s["route"] = _route(s)
        if s.get("has_exception") or s.get("status") == "exception":
            actions.append({"kind": "exception", "title": s["reference"], "detail": s["route"],
                            "link": f"/portal/shipments/{s['id']}", "severity": "danger"})

    async for d in db.customs_declarations.find({"company_id": cid, "status": {"$in": ["draft", "needs_info"]}}, NO_ID).limit(20):
        actions.append({"kind": "customs_confirm", "title": d.get("lrn") or d.get("id")[:8], "detail": d.get("shipment_reference"),
                        "link": f"/portal/customs/{d['id']}", "severity": "warning"})

    async for q in db.quotes.find({"company_id": cid, "status": "sent"}, NO_ID).sort("created_at", -1).limit(10):
        actions.append({"kind": "quote_ready", "title": q.get("number"), "detail": f"{(q.get('request') or {}).get('origin', {}).get('name', '')} → {(q.get('request') or {}).get('destination', {}).get('name', '')}",
                        "link": f"/portal/quotes/{q['id']}", "due": q.get("valid_until"), "severity": "info"})

    open_cents = 0
    async for inv in db.invoices.find({"company_id": cid, "status": {"$in": ["open", "overdue"]}}, NO_ID).sort("due_date", 1):
        open_cents += int(inv.get("total_cents") or 0)
        overdue = inv.get("status") == "overdue"
        actions.append({"kind": "invoice_overdue" if overdue else "invoice_due", "title": inv.get("number"),
                        "amount_cents": inv.get("total_cents"), "due": inv.get("due_date"),
                        "link": f"/portal/invoices/{inv['id']}", "severity": "danger" if overdue else "info"})

    unread = await db.threads.count_documents({"company_id": cid, "unread_customer": {"$gt": 0}})
    if unread:
        actions.append({"kind": "message", "title": str(unread), "link": "/portal/messages", "severity": "info"})

    order = {"danger": 0, "warning": 1, "info": 2}
    actions.sort(key=lambda a: order.get(a.get("severity"), 3))
    customs_open = await db.customs_declarations.count_documents(
        {"company_id": cid, "status": {"$nin": ["released", "closed", "invalidated"]}})
    return {
        "company": {"id": company.get("id"), "name": company.get("name"), "kyc_status": company.get("kyc_status")},
        "actions": actions[:30],
        "active": active[:50],
        "counts": {
            "active": len(active),
            "in_transit": sum(1 for s in active if s.get("status") in ("in_transit", "at_hub")),
            "arriving_week": sum(1 for s in active if s.get("eta") and s["eta"] <= week),
            "customs_open": customs_open,
            "open_invoices_cents": open_cents,
        },
        "generated_at": now_iso(),
    }


def helsinki_day_bounds(at):
    """UTC query bounds for one Helsinki calendar day (23/24/25 h across DST)."""
    local_day = at.astimezone(ZoneInfo("Europe/Helsinki")).date()
    start = datetime.combine(local_day, time.min, ZoneInfo("Europe/Helsinki"))
    end = datetime.combine(local_day + timedelta(days=1), time.min, ZoneInfo("Europe/Helsinki"))
    return iso(start), iso(end), local_day.isoformat()


@router.get("/ops/today")
async def ops_today(user: dict = Depends(require_internal)):
    t_now = now()
    today_start, today_end, business_date = helsinki_day_bounds(t_now)
    day_ago = iso(t_now - timedelta(days=1))

    approvals = {}
    async for row in db.approvals.aggregate([{"$match": {"status": "pending"}}, {"$group": {"_id": "$kind", "n": {"$sum": 1}}}]):
        approvals[row["_id"]] = row["n"]
    pending_approvals = await db.approvals.find({"status": "pending"}, NO_ID).sort("created_at", 1).to_list(8)

    exceptions = {}
    async for row in db.exceptions.aggregate([{"$match": {"status": {"$ne": "resolved"}}}, {"$group": {"_id": "$severity", "n": {"$sum": 1}}}]):
        exceptions[row["_id"]] = row["n"]
    top_exceptions = await db.exceptions.find({"status": {"$ne": "resolved"}}, NO_ID).sort([("severity_rank", -1), ("created_at", 1)]).to_list(6)

    customs = {}
    async for row in db.customs_declarations.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        customs[row["_id"]] = row["n"]

    status_counts = {}
    async for row in db.shipments.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        status_counts[row["_id"]] = row["n"]

    no_carrier = await db.shipments.find({"status": "booked", "legs": {"$elemMatch": {"carrier_id": None, "carrier": {"$in": [None, ""]}}}},
                                         SHIP_FIELDS).to_list(20)
    arriving = await db.shipments.find({"status": {"$in": ACTIVE}, "eta": {"$gte": today_start, "$lt": today_end}}, SHIP_FIELDS).sort("eta", 1).to_list(20)
    holds = await db.shipments.find({"status": {"$in": ACTIVE}, "holds.0": {"$exists": True}}, SHIP_FIELDS).to_list(20)
    for lst in (no_carrier, arriving, holds):
        for s in lst:
            s["route"] = _route(s)

    ar = {"open_cents": 0, "overdue_cents": 0, "overdue_count": 0}
    async for inv in db.invoices.find({"status": {"$in": ["open", "overdue"]}}, {"_id": 0, "status": 1, "total_cents": 1}):
        ar["open_cents"] += int(inv.get("total_cents") or 0)
        if inv.get("status") == "overdue":
            ar["overdue_cents"] += int(inv.get("total_cents") or 0)
            ar["overdue_count"] += 1

    ai = {"runs_24h": await db.ai_runs.count_documents({"created_at": {"$gte": day_ago}}),
          "errors_24h": await db.ai_runs.count_documents({"created_at": {"$gte": day_ago}, "status": "error"})}
    kyc_review = await db.companies.count_documents({"kyc_status": {"$in": ["in_review", "pending"]}})
    new_quotes = await db.quotes.count_documents({"created_at": {"$gte": day_ago}})
    return {
        "approvals": {"counts": approvals, "total": sum(approvals.values()), "oldest": pending_approvals},
        "exceptions": {"counts": exceptions, "total": sum(exceptions.values()), "top": top_exceptions},
        "customs": customs,
        "shipments": {"counts": status_counts, "active": sum(v for k, v in status_counts.items() if k in ACTIVE)},
        "no_carrier": no_carrier, "arriving_today": arriving, "business_date": business_date, "holds": holds,
        "ar": ar, "ai": ai, "kyc_review": kyc_review, "new_quotes_24h": new_quotes,
        "generated_at": now_iso(),
    }


@router.get("/ops/email-outbox")
async def email_outbox(limit: int = 100, user: dict = Depends(require_internal)):
    """What the app sent (or logged, when no e-mail provider is configured)."""
    if "settings.write" not in user["permissions"]:
        from fastapi import HTTPException

        raise HTTPException(403, "Missing permission: settings.write")
    items = await db.email_outbox.find({}, NO_ID).sort("created_at", -1).to_list(min(limit, 500))
    return {"items": items}
