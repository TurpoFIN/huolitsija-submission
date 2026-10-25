"""Owner alerts: one open alert per problem (key), e-mail + in-app bell when it opens, a reminder while it stays open,
and a quiet resolve when the check passes again.

    await raise_alert("worker_stale", "critical", bi("Työjono pysähtynyt", "Job worker stopped"), bi(fi, en))
    await resolve_alert("worker_stale")
"""
from datetime import timedelta

from core.db import db, register_index
from core.events import emit
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso

register_index("alerts", "id", unique=True)
register_index("alerts", [("key", 1), ("status", 1)])
register_index("alerts", [("status", 1), ("opened_at", -1)])

REMIND_HOURS = 6
SEVERITIES = ("info", "warning", "critical")


async def raise_alert(key: str, severity: str, title: dict, body: dict, *, link: str | None = "/ops/autopilot",
                      data: dict | None = None) -> dict:
    from core.agent_runtime import notify_owner
    severity = severity if severity in SEVERITIES else "warning"
    t = now_iso()
    cur = await db.raw.alerts.find_one({"key": key, "status": "open"}, NO_ID)
    if cur:
        sets = {"last_seen_at": t, "body": body.get("en"), "body_fi": body.get("fi"), "data": data or cur.get("data")}
        if SEVERITIES.index(severity) > SEVERITIES.index(cur.get("severity", "info")):
            sets["severity"] = severity
        remind = parse_iso(cur.get("notified_at")) is None or now() - parse_iso(cur["notified_at"]) > timedelta(hours=REMIND_HOURS)
        if remind and severity != "info":
            sets["notified_at"] = t
            await notify_owner({"fi": f"Edelleen auki: {title['fi']}", "en": f"Still open: {title['en']}"}, body, link,
                               kind="alert", dedupe_key=f"alert:{cur['id']}:{t}")
        await db.raw.alerts.update_one({"id": cur["id"]}, {"$set": sets, "$inc": {"count": 1}})
        return {**cur, **sets}
    doc = {"id": new_id(), "key": key, "severity": severity, "status": "open", "title": title.get("en"),
           "title_fi": title.get("fi"), "body": body.get("en"), "body_fi": body.get("fi"), "link": link,
           "data": data or {}, "opened_at": t, "last_seen_at": t, "count": 1, "notified_at": None,
           "resolved_at": None, "acknowledged_at": None, "acknowledged_by": None}
    await db.raw.alerts.insert_one(dict(doc))
    if severity != "info":
        doc["notified_at"] = t
        await db.raw.alerts.update_one({"id": doc["id"]}, {"$set": {"notified_at": t}})
        await notify_owner({"fi": f"Hälytys: {title['fi']}", "en": f"Alert: {title['en']}"}, body, link, kind="alert",
                           dedupe_key=f"alert:{doc['id']}")
    await emit("alert.opened", {"alert": doc})
    return doc


async def resolve_alert(key: str, note: str | None = None) -> dict | None:
    cur = await db.raw.alerts.find_one({"key": key, "status": "open"}, NO_ID)
    if not cur:
        return None
    t = now_iso()
    await db.raw.alerts.update_one({"id": cur["id"]}, {"$set": {"status": "resolved", "resolved_at": t,
                                                                  "resolution_note": note}})
    cur.update(status="resolved", resolved_at=t)
    await emit("alert.resolved", {"alert": cur})
    return cur


async def acknowledge(alert_id: str, actor: dict) -> dict:
    from fastapi import HTTPException
    res = await db.raw.alerts.update_one({"id": alert_id}, {"$set": {
        "acknowledged_at": now_iso(), "acknowledged_by": {"id": actor.get("id"), "name": actor.get("name")}}})
    if not res.matched_count:
        raise HTTPException(404, "Alert not found")
    return await db.raw.alerts.find_one({"id": alert_id}, NO_ID)


async def open_alerts(limit: int = 50) -> list[dict]:
    return [a async for a in db.raw.alerts.find({"status": "open"}, NO_ID).sort("opened_at", -1).limit(limit)]


def older_than(at: str | None, **delta) -> bool:
    dt = parse_iso(at)
    return dt is None or now() - dt > timedelta(**delta)


__all__ = ["raise_alert", "resolve_alert", "acknowledge", "open_alerts", "older_than", "iso"]
