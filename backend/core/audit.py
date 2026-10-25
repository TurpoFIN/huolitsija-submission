from core.db import db
from core.util import new_id, now_iso


def actor_ref(user: dict | None) -> dict:
    if not user:
        return {"type": "system", "id": None, "name": "System"}
    if user.get("type") == "agent":
        return user
    return {"type": "user", "id": user.get("id"), "name": user.get("name") or user.get("email"),
            "org_type": user.get("org_type")}


def agent_actor(agent: str) -> dict:
    """Actor for work done by an AI agent, e.g. agent_actor("hs_classifier")."""
    return {"type": "agent", "id": agent, "name": f"AI: {agent.replace('_', ' ')}"}


async def audit(actor: dict | None, action: str, entity: str, entity_id: str | None,
                data: dict | None = None, company_id: str | None = None):
    await db.audit_log.insert_one({
        "id": new_id(), "at": now_iso(), "actor": actor_ref(actor), "action": action,
        "entity": entity, "entity_id": entity_id, "company_id": company_id, "data": data or {},
    })
