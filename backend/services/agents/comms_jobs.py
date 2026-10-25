"""Customer messages as durable work. A customer's message starts the comms agent at once (services.comms_agent);
this sweep catches any message that was missed (restart, crash) and answers it as a job, and when Autopilot is on
it releases AI replies that were held only because Autopilot was off (same gate: confidence floor, needs-human)."""
from datetime import timedelta

from core.agent_runtime import approve_as_agent, decide
from core.db import db
from core.jobs import enqueue, job
from core.util import NO_ID, iso, now
from services.agents.base import agents_epoch, mode_key, sweeper

AGENT = "comms_agent"


@job("messages.answer", agent=True, max_attempts=3, backoff=(60, 300, 900), timeout=300, label_fi="Asiakasviestiin vastaus",
     label_en="Answer customer message")
async def answer(j):
    from services.comms_agent import draft_reply
    t = await db.raw.threads.find_one({"id": j["payload"]["thread_id"]}, NO_ID)
    if not t or t.get("ai_status") in ("drafting", "draft_ready", "needs_person") or not t.get("needs_reply") \
            or t.get("last_author_type") != "customer":
        return {"skipped": (t or {}).get("ai_status") or "answered"}
    msg = await draft_reply(t["id"], j["payload"].get("message_id"))
    return {"message_id": (msg or {}).get("id"), "status": (msg or {}).get("status")}


@job("messages.release", agent=True, max_attempts=3, timeout=120, label_fi="Pidätetyn vastauksen lähetys",
     label_en="Release held reply")
async def release(j):
    a = await db.raw.approvals.find_one({"id": j["payload"]["approval_id"], "status": "pending"}, NO_ID)
    if not a:
        return {"skipped": "decided"}
    m = await db.raw.messages.find_one({"id": a["ref_id"]}, NO_ID)
    ai = (m or {}).get("ai") or {}
    if not m or m.get("status") != "pending_approval" or m.get("author", {}).get("type") != "ai" or ai.get("needs_human"):
        return {"skipped": "not releasable"}
    conf = ai.get("confidence")
    d = await decide(AGENT, "send_reply", subject=("thread", m["thread_id"]), company_id=m.get("company_id"),
                     inputs={"held_message_id": m["id"]}, decision={"reply": m["body"][:2000]},
                     confidence=float(conf) if isinstance(conf, (int, float)) else 0.0, create_approval=False,
                     title="Release held reply", title_fi="Pidätetyn vastauksen lähetys")
    if not d.auto:
        return {"held": [r["code"] for r in d.reasons]}
    await approve_as_agent(a, AGENT, "Autopilot: reply inside the confidence floor")
    return {"sent": m["id"]}


@sweeper
async def sweep_messages():
    n = 0
    mk = await mode_key()
    cutoff = iso(now() - timedelta(minutes=5))
    epoch = await agents_epoch()
    async for t in db.raw.threads.find({"ai_status": {"$nin": ["drafting", "draft_ready", "needs_person"]}, "needs_reply": True,
                                        "last_message_at": {"$lte": cutoff, "$gte": epoch},
                                        "last_author_type": "customer", "demo": {"$ne": True}},
                                       {"_id": 0, "id": 1, "company_id": 1, "last_message_at": 1}).limit(50):
        await enqueue("messages.answer", {"thread_id": t["id"]}, company_id=t.get("company_id"), subject=("thread", t["id"]),
                      key=f"answer:{t['id']}:{t.get('last_message_at')}", source="sweep")
        n += 1
    if mk == "on":
        async for a in db.raw.approvals.find({"kind": "ai_message", "status": "pending", "created_at": {"$gte": epoch}},
                                             {"_id": 0, "id": 1, "company_id": 1}).limit(50):
            await enqueue("messages.release", {"approval_id": a["id"]}, company_id=a.get("company_id"),
                          subject=("approval", a["id"]), key=f"release:{a['id']}:{mk}", source="sweep")
            n += 1
    return n
