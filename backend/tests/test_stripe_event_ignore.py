"""Owner ignores a failed TEST-mode Stripe event (drill / test payment for no invoice); live-mode events are refused.
Reconciliation stops listing an ignored event."""
import asyncio
import json
import os
import uuid

os.environ.setdefault("DB_NAME", f"huo_stripe_ign_{uuid.uuid4().hex[:8]}")


def test_ignore_only_test_mode_failed_events():
    async def scenario():
        import httpx
        from fastapi import FastAPI
        from core.db import db
        from routes import autopilot
        from services import reconciliation
        tag = uuid.uuid4().hex[:8]
        rows = [{"id": f"evt_drill_{tag}", "type": "payment_intent.succeeded", "status": "failed", "error": "Invoice not found",
                 "livemode": False, "payload": json.dumps({"id": f"evt_drill_{tag}", "livemode": False})},
                {"id": f"evt_live_{tag}", "type": "payment_intent.succeeded", "status": "failed", "error": "x",
                 "livemode": True, "payload": json.dumps({"livemode": True})},
                {"id": f"evt_ok_{tag}", "type": "payment_intent.succeeded", "status": "processed", "livemode": False, "payload": "{}"}]
        await db.raw.stripe_events.insert_many([dict(r) for r in rows])
        app = FastAPI()
        app.include_router(autopilot.router)
        actor = {"id": "owner", "name": "Owner", "role": "admin", "org_type": "internal", "permissions": ["settings.write"]}
        from core import deps
        app.dependency_overrides[deps.require_internal] = lambda: actor
        for r in app.routes:
            for d in getattr(getattr(r, "dependant", None), "dependencies", []) or []:
                app.dependency_overrides[d.call] = lambda: actor
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            listed = {i.get("event_id") for i in await reconciliation.discrepancies() if i["kind"] == "stripe_event_failed"}
            assert f"evt_drill_{tag}" in listed
            g = (await c.get(f"/api/autopilot/stripe-events/evt_drill_{tag}")).json()
            assert g["livemode"] is False and g["payload_livemode"] is False
            assert (await c.post(f"/api/autopilot/stripe-events/evt_drill_{tag}/ignore", json={"note": "x"})).status_code == 400
            assert (await c.post(f"/api/autopilot/stripe-events/evt_live_{tag}/ignore", json={"note": "TEST drill"})).status_code == 409
            assert (await c.post(f"/api/autopilot/stripe-events/evt_ok_{tag}/ignore", json={"note": "TEST drill"})).status_code == 409
            r = await c.post(f"/api/autopilot/stripe-events/evt_drill_{tag}/ignore", json={"note": "TEST incident drill, Stripe test mode"})
            assert r.status_code == 200 and r.json()["status"] == "ignored", r.text
        listed = {i.get("event_id") for i in await reconciliation.discrepancies() if i["kind"] == "stripe_event_failed"}
        assert f"evt_drill_{tag}" not in listed and f"evt_live_{tag}" in listed
        assert await db.raw.audit_log.find_one({"action": "stripe_event_ignored", "entity_id": f"evt_drill_{tag}"})

    _run(scenario())


def _run(coro):
    """On the loop the Motor client is bound to (see test_s2_payments.run)."""
    from core.db import client
    loop = client.get_io_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
