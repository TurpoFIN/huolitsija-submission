"""Messaging + AI comms agent against the local API (one real model call when AI is configured)."""
from __future__ import annotations

from test_v4_support import customer, customer_company_id, db, make_company, make_shipment, make_user, ops, wait_for

import asyncio

_LOOP = asyncio.new_event_loop()  # one loop per module: the backend's Motor client stays bound to the first loop it uses


def test_ai_reply_is_held_for_approval_then_sent_with_edits():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, status="in_transit")
    r = customer().post("/messages/threads", json={"shipment_id": s["id"], "body":
                        f"Hei, milloin {s['reference']} saapuu Helsinkiin? Tarvitsetteko meiltä vielä jotain?"})
    assert r.status_code == 200, r.text
    thread_id = r.json()["thread"]["id"]

    def drafted():
        t = db.threads.find_one({"id": thread_id})
        if t.get("ai_status") in ("draft_ready", "needs_person", "sent"):
            return t
        return None

    t = wait_for(drafted, timeout=120)
    assert t, "agent did not finish"
    msgs = list(db.messages.find({"thread_id": thread_id}))
    ai_msgs = [m for m in msgs if m["author"]["type"] == "ai"]
    if t["ai_status"] == "needs_person":
        # No model configured: nothing invented, nothing sent, ops told.
        assert not ai_msgs and t.get("ai_note")
        return
    assert t["ai_status"] == "draft_ready", "approval is required by default settings"
    draft = ai_msgs[-1]
    assert draft["status"] == "pending_approval" and draft["approval_id"]
    appr = db.approvals.find_one({"id": draft["approval_id"]})
    assert appr["kind"] == "ai_message" and appr["status"] == "pending" and appr["data"]["body"] == draft["body"]
    # The customer cannot see the draft.
    seen = customer().get(f"/messages/threads/{thread_id}").json()["messages"]
    assert all(m["id"] != draft["id"] for m in seen)
    assert all(m["status"] == "sent" for m in seen)
    # Ops edits and approves through the core approval queue.
    edited = draft["body"].rstrip() + "\n\n(Tarkistettu.)"
    r = ops().post(f"/messages/{draft['id']}/approve", json={"body": edited})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["body"] == edited and r.json()["edited"] is True
    seen = customer().get(f"/messages/threads/{thread_id}").json()["messages"]
    assert any(m["id"] == draft["id"] and m["body"] == edited for m in seen)
    assert db.approvals.find_one({"id": draft["approval_id"]})["status"] == "approved"
    # Approving twice is refused.
    assert ops().post(f"/messages/{draft['id']}/approve", json={}).status_code == 409


def test_ops_reply_supersedes_pending_draft_and_internal_notes_stay_internal():
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, status="in_transit")
    t = ops().post("/messages/threads", json={"shipment_id": s["id"], "body": "Hei! Lähetys on lähtenyt satamasta."}).json()["thread"]
    # A pending AI draft placed directly in the DB (no model call needed for this check).
    draft = {"id": "test-draft-" + t["id"][:8], "thread_id": t["id"], "company_id": t["company_id"], "author": {"type": "ai", "name": "AI"},
             "body": "luonnos", "status": "pending_approval", "visibility": "customer", "kind": "message", "attachments": [],
             "created_at": "2026-10-24T08:00:00Z", "approval_id": None}
    db.messages.insert_one(dict(draft))
    note = ops().post(f"/messages/threads/{t['id']}/messages", json={"body": "Sisäinen: tarkista rahtikirja", "visibility": "internal"})
    assert note.status_code == 200
    assert db.messages.find_one({"id": draft["id"]})["status"] == "pending_approval"  # internal note does not supersede
    reply = ops().post(f"/messages/threads/{t['id']}/messages", json={"body": "Päivitys: ETA ennallaan."})
    assert reply.status_code == 200
    assert db.messages.find_one({"id": draft["id"]})["status"] == "rejected"
    seen = customer().get(f"/messages/threads/{t['id']}").json()["messages"]
    bodies = [m["body"] for m in seen]
    assert "Päivitys: ETA ennallaan." in bodies and all("Sisäinen" not in b for b in bodies)
    assert customer().post(f"/messages/threads/{t['id']}/messages",
                           json={"body": "x", "visibility": "internal"}).json()["visibility"] == "customer"


def test_customers_are_isolated():
    other = make_company()
    other_client, _ = make_user(other["id"])
    t = other_client.post("/messages/threads", json={"subject": "Laskutusosoite", "body": "Uusi osoite: Testikatu 2"}).json()["thread"]
    assert customer().get(f"/messages/threads/{t['id']}").status_code == 404
    assert customer().post(f"/messages/threads/{t['id']}/messages", json={"body": "hi"}).status_code == 404
    assert all(x["company_id"] == customer_company_id() for x in customer().get("/messages/threads").json()["items"])
    assert customer().post("/messages/threads", json={"body": "no subject"}).status_code == 400


def test_post_message_contract_and_routine_update_dedupe():
    """Service-level contract (needs DB_NAME=huo_v4 in the test process, same DB as the API)."""
    import asyncio
    import os

    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, status="in_transit")

    async def run():
        # Imported inside the running loop: Motor binds its client to the loop that first creates it.
        from services import comms_agent, messaging

        m = await messaging.post_message(company_id=customer_company_id(), shipment_id=s["id"], body="Testiviesti",
                                         author={"type": "ops", "id": None, "name": "Ops"})
        u1 = await comms_agent.routine_update(s, "departed")
        u2 = await comms_agent.routine_update(s, "departed")
        return m, u1, u2

    os.environ.setdefault("DB_NAME", "huo_v4")
    m, u1, u2 = _LOOP.run_until_complete(run())
    assert m["thread_id"] and m["status"] == "sent"
    assert u1 and u1["kind"] == "update" and u2 is None
    assert s["reference"] in u1["body"]


def test_saved_edit_then_approve_keeps_edited_flag_and_rejection_feeds_back():
    """A draft edited and saved first, then approved without a body, is still marked edited with the AI's original
    text kept; a person's rejection is stored with who rejected it (the comms agent feeds those notes back)."""
    import asyncio
    import os

    company = make_company()
    s = make_shipment(company, status="in_transit")

    async def run():
        from services import messaging

        a = await messaging.post_message(company_id=company["id"], shipment_id=s["id"], body="AI:n alkuperäinen luonnos",
                                         author={"type": "ai", "id": "comms_agent", "name": "Huolitsija AI"}, needs_approval=True)
        b = await messaging.post_message(company_id=company["id"], shipment_id=s["id"], body="Toinen luonnos",
                                         author={"type": "ai", "id": "comms_agent", "name": "Huolitsija AI"}, needs_approval=True)
        return a, b

    os.environ.setdefault("DB_NAME", "huo_v4")
    a, b = _LOOP.run_until_complete(run())
    assert a["status"] == "pending_approval" and a.get("approval_id")
    saved = ops().patch(f"/messages/{a['id']}", json={"body": "Ihmisen korjaama teksti"})
    assert saved.status_code == 200, saved.text
    sent = ops().post(f"/messages/{a['id']}/approve", json={})
    assert sent.status_code == 200, sent.text
    m = db.messages.find_one({"id": a["id"]})
    assert m["status"] == "sent" and m["body"] == "Ihmisen korjaama teksti"
    assert m["edited"] is True and m["original_body"] == "AI:n alkuperäinen luonnos"
    rej = ops().post(f"/messages/{b['id']}/reject", json={"note": "Älä lupaa toimituspäivää"})
    assert rej.status_code == 200, rej.text
    r = db.messages.find_one({"id": b["id"]})
    assert r["status"] == "rejected" and r["rejected_reason"] == "Älä lupaa toimituspäivää" and r.get("rejected_by")
