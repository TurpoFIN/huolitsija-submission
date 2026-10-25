"""With Autopilot off a parked prospect draft is an owner-queue card: approving it sends exactly that e-mail as the
approver, declining it parks the lead in nurture. Real MongoDB (own database, dropped at the end); e-mail provider log.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

os.environ["DB_NAME"] = f"huo_test_s1_card_{uuid.uuid4().hex[:8]}"
os.environ["CRM_EMAIL_PROVIDER"] = "log"
os.environ.pop("CRM_SANDBOX_DOMAIN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import client, db  # noqa: E402
from core.events import emit  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from core.settings import get_settings  # noqa: E402
from services import prospecting  # noqa: E402

OWNER = {"id": "u-owner", "name": "Omistaja", "email": "owner@huolitsija.test", "org_type": "internal", "type": "user"}


async def _lead(name: str, domain: str) -> dict:
    lead = {"id": new_id(), "ref": f"L-{uuid.uuid4().hex[:6].upper()}", "name": name, "country": "FI", "domain": domain,
            "status": "contactable", "first_touch_at": None, "follow_ups_sent": 0, "touches": 0, "score": 70,
            "created_at": now_iso(), "updated_at": now_iso()}
    contact = {"id": new_id(), "lead_id": lead["id"], "email": f"info@{domain}", "role": "general", "status": "active",
               "created_at": now_iso()}
    await db.crm_leads.insert_one(dict(lead))
    await db.crm_contacts.insert_one(dict(contact))
    return lead


async def _decide(card: dict, status: str):
    await db.approvals.update_one({"id": card["id"]}, {"$set": {"status": status, "decided_by": OWNER}})
    await emit("approval.decided", {"approval": {**card, "status": status}, "actor": OWNER})


def test_parked_draft_is_an_owner_card_and_the_decision_acts():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        s = await get_settings()
        assert not s.get("autopilot_enabled")
        a, b = await _lead("Testi Portaat Oy", "portaat-testi.example"), await _lead("Testi Ovet Oy", "ovet-testi.example")
        out = {}
        for lead in (a, b):
            assert await prospecting.draft_for(lead, s)
            out[lead["id"]] = await db.approvals.find_one({"kind": "prospect_email", "ref_id": lead["id"], "status": "pending"}, {"_id": 0})
        again = await prospecting.draft_for(await db.crm_leads.find_one({"id": a["id"]}, {"_id": 0}), s)
        assert again and await db.approvals.count_documents({"kind": "prospect_email", "ref_id": a["id"]}) == 1
        await _decide(out[a["id"]], "approved")
        await _decide(out[b["id"]], "rejected")
        await asyncio.sleep(0.2)
        res = {"a": await db.crm_leads.find_one({"id": a["id"]}, {"_id": 0}), "b": await db.crm_leads.find_one({"id": b["id"]}, {"_id": 0}),
               "outbox": await db.email_outbox.find({}, {"_id": 0}).to_list(10),
               "acts": await db.crm_activities.find({"kind": "email_out"}, {"_id": 0}).to_list(10), "cards": out}
        await client.drop_database(os.environ["DB_NAME"])
        return res
    r = asyncio.run(go())
    card = r["cards"][r["a"]["id"]]
    assert card["title"].startswith("First e-mail to Testi Portaat Oy (info@portaat-testi.example)") and card["title_fi"]
    assert "Testi Portaat Oy" in card["summary"] and "company in formation" in card["summary"]
    assert r["a"]["status"] == "contacted" and r["a"]["first_touch_at"]
    assert [o["to"] for o in r["outbox"]] == ["info@portaat-testi.example"]          # exactly one mail, to the approved lead
    assert r["acts"][0]["actor"]["id"] == OWNER["id"] and r["acts"][0]["data"]["manual"] is True
    assert r["b"]["status"] == "nurture" and r["b"]["draft"] is None and not r["b"]["first_touch_at"]
