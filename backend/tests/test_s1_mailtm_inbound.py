"""mail.tm inbound poller: a genuine reply in the mailbox (the API shapes are captured from api.mail.tm in
fixtures/mailtm_captured.json) reaches the CRM lead through the same pipeline as the webhook, matched by the SMTP2GO
Message-ID it quotes, and is marked seen; a message whose source cannot be read stays unseen for the next poll.
Real MongoDB (own database, dropped at the end); the mail.tm HTTP API is an httpx.MockTransport over captured bodies."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

os.environ["DB_NAME"] = f"huo_test_s1_mailtm_{uuid.uuid4().hex[:8]}"
os.environ.update({"MAILTM_ADDRESS": "inbound@example-mailtm.test", "MAILTM_PASSWORD": "pw-test", "CRM_EMAIL_PROVIDER": "log"})
os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from core.db import client, db  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from services.email_channel import mailtm  # noqa: E402

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "mailtm_captured.json").read_text())
EMAIL_ID = "1xKpQp-FnQW0hPtSHm-sGhz"  # SMTP2GO email_id stored on the outbound activity


def _reply_raw() -> str:
    return ("Received: from a2i949.smtp2go.com (a2i949.smtp2go.com [103.47.207.181])\r\n"
            "From: Huolitsija <02f0ce8dc68e626a0bdf747768fdc1f8@inbound.postmarkapp.com>\r\n"
            "To: inbound@example-mailtm.test\r\nSubject: Re: Rahtihinnat heti verkossa\r\n"
            f"Message-ID: <reply-{uuid.uuid4().hex}@smtpcorp.com>\r\n"
            f"In-Reply-To: <E{EMAIL_ID}@message-id.smtpcorp.com>\r\nReferences: <E{EMAIL_ID}@message-id.smtpcorp.com>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nKyllä, olemme kiinnostuneita. Helsinki → Tukholma, 2 rekkaa viikossa.\r\n")


def _transport(sources: dict, patched: list):
    member = CAPTURED["messages"]["hydra:member"][0]
    listing = {**CAPTURED["messages"], "hydra:member": [{**member, "id": mid, "seen": False} for mid in sources]}

    def handler(req: httpx.Request):
        p = req.url.path
        if p == "/token":
            assert json.loads(req.content) == {"address": "inbound@example-mailtm.test", "password": "pw-test"}
            return httpx.Response(200, json={k: "x" for k in CAPTURED["token_keys"]})
        if p == "/messages":
            return httpx.Response(200, json=listing)
        if p.startswith("/sources/"):
            mid = p.rsplit("/", 1)[1]
            if sources[mid] is None:
                return httpx.Response(500, json={"detail": "upstream"})
            return httpx.Response(200, json={**CAPTURED["source"], "id": mid, "data": sources[mid]})
        if req.method == "PATCH" and p.startswith("/messages/"):
            patched.append((p.rsplit("/", 1)[1], json.loads(req.content), req.headers["content-type"]))
            return httpx.Response(200, json={"seen": True})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_captured_source_shape_is_raw_rfc822():
    import email
    m = email.message_from_string(CAPTURED["source"]["data"])
    assert m["Delivered-To"] and m["From"] and m["Subject"] and m["Received"]
    assert "hydra:member" in CAPTURED["messages"] and "seen" in CAPTURED["messages"]["hydra:member"][0]


def test_genuine_reply_reaches_the_lead_and_is_marked_seen():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        lead = {"id": new_id(), "ref": "L-MTM001", "name": "TEST Liidi Oy", "country": "FI", "domain": "heliosone.fi",
                "status": "contacted", "first_touch_at": now_iso(), "follow_ups_sent": 0, "touches": 1, "score": 70,
                "created_at": now_iso(), "updated_at": now_iso()}
        await db.crm_leads.insert_one(dict(lead))
        await db.crm_activities.insert_one({"id": new_id(), "kind": "email_out", "lead_id": lead["id"], "direction": "out",
                                            "message_id": EMAIL_ID, "to": "marc+s1lead@heliosone.fi", "at": now_iso()})
        patched = []
        async with httpx.AsyncClient(transport=_transport({"m-ok": _reply_raw(), "m-broken": None}, patched),
                                     base_url="https://api.mail.tm") as c:
            res = await mailtm.poll_once(client=c)
        out = {"res": res, "patched": patched, "lead": await db.crm_leads.find_one({"id": lead["id"]}, {"_id": 0}),
               "acts": await db.crm_activities.find({"lead_id": lead["id"], "direction": "in"}, {"_id": 0}).to_list(5)}
        await client.drop_database(os.environ["DB_NAME"])
        return out
    r = asyncio.run(go())
    assert r["res"]["fetched"] == 1 and r["res"]["results"][0]["channel"] == "crm", r["res"]
    assert [p[0] for p in r["patched"]] == ["m-ok"] and r["patched"][0][1] == {"seen": True}
    assert r["patched"][0][2] == "application/merge-patch+json"
    assert r["res"]["errors"] and r["res"]["errors"][0]["id"] == "m-broken"  # left unseen for the next poll
    assert len(r["acts"]) == 1 and r["lead"]["status"] != "contacted" and r["lead"].get("opportunity_id")


def test_off_without_env(monkeypatch):
    monkeypatch.delenv("MAILTM_ADDRESS")
    assert asyncio.run(mailtm.poll_once()) == {"status": "off"}
