"""Rows 0/6/7 in test mode. (a) A reply to a prospect e-mail that arrives through the Postmark inbound hook updates the
CRM lead and never becomes a customer intake. (b) A TEST partner on the company's own mailbox gets one real RFQ while
the Sourcing switch is off and single-RFQ mode is on; its reply comes back through the same hook, the offer becomes a
rate that can be accepted, and that rate stays archived without a carrier. Real MongoDB (own database, dropped at the
end); e-mail providers log.
"""
import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path

os.environ["DB_NAME"] = f"huo_test_s1_r2_{uuid.uuid4().hex[:8]}"
os.environ.update({"CRM_EMAIL_PROVIDER": "log", "EMAIL_PROVIDER": "log", "EMAIL_CHANNEL_PROVIDER": "log",
                   "PARTNER_EMAIL_PROVIDER": "log", "PARTNER_EMAIL_FROM": "Huolitsija <marc+huolitsija@heliosone.fi>",
                   "PARTNER_REPLY_TO": "abc123@inbound.postmarkapp.com", "PARTNER_SINGLE_RFQ_ONLY": "true",
                   "PARTNER_OUTREACH_ALLOWLIST": "info@galaxytrans.fi", "PARTNER_SANDBOX_DOMAIN": "none",
                   "CRM_SANDBOX_DOMAIN": "none", "EMAIL_CHANNEL_POSTMARK_TOKEN": "pm-test-token"})
os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from core import deps  # noqa: E402
from core.db import client, db  # noqa: E402
from core.events import emit  # noqa: E402
from core.settings import get_settings, update_settings  # noqa: E402
from core.util import new_id, now_iso  # noqa: E402
from routes import crm as crm_routes  # noqa: E402
from routes import email_channel as email_routes  # noqa: E402
from routes import partner_network as partner_routes  # noqa: E402
from services import partner_network, prospecting  # noqa: E402

OWNER = {"id": "u-owner", "name": "Omistaja", "email": "owner@huolitsija.test", "org_type": "internal", "type": "user",
         "role": "owner",
         "permissions": ["customers.write", "rates.write", "settings.write"]}
AUTH = {"Authorization": "Basic " + base64.b64encode(b"postmark:pm-test-token").decode()}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(email_routes.router)
    app.include_router(crm_routes.router)
    app.include_router(partner_routes.router)
    app.dependency_overrides[deps.get_optional_user] = lambda: OWNER
    return app


def _postmark(frm: str, to: str, subject: str, text: str, in_reply_to: str | None = None) -> dict:
    headers = [{"Name": "In-Reply-To", "Value": in_reply_to}, {"Name": "References", "Value": in_reply_to}] if in_reply_to else []
    return {"MessageID": str(uuid.uuid4()), "From": frm, "To": to, "Subject": subject, "TextBody": text,
            "Date": "Sun, 25 Oct 2026 04:10:00 +0000", "Headers": headers}


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_prospect_reply_through_the_inbound_hook_updates_the_lead():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        alias = "marc+s1lead@heliosone.fi"
        app = _app()
        async with await _client(app) as c:
            refused = await c.post("/api/crm/leads", json={"name": "TEST Ulkoinen Oy", "contact_email": "info@hoviportaat.fi"})
            lead = (await c.post("/api/crm/leads", json={"name": "TEST Liidi Oy", "domain": "heliosone.fi",
                                                         "contact_email": alias})).json()
            enriched = (await c.post(f"/api/crm/leads/{lead['id']}/enrich")).json()
            other = (await c.post("/api/crm/leads", json={"name": "TEST Toinen Oy", "domain": "heliosone.fi",
                                                          "contact_email": "marc+s1other@heliosone.fi"})).json()
            await c.post(f"/api/crm/leads/{other['id']}/enrich")
            drafted = (await c.post(f"/api/crm/leads/{lead['id']}/draft")).json()
            twice = await c.post(f"/api/crm/leads/{lead['id']}/draft")
        card = await db.approvals.find_one({"kind": "prospect_email", "ref_id": lead["id"], "status": "pending"}, {"_id": 0})
        assert refused.status_code == 409 and enriched["status"] == "contactable" and card and drafted["approval_id"] == card["id"]
        assert twice.status_code == 409  # awaiting_person now: one card per lead
        assert not await db.approvals.find_one({"ref_id": other["id"]})  # only the requested lead is drafted
        await db.approvals.update_one({"id": card["id"]}, {"$set": {"status": "approved", "decided_by": OWNER}})
        await emit("approval.decided", {"approval": {**card, "status": "approved"}, "actor": OWNER})
        await asyncio.sleep(0.3)
        out = await db.crm_activities.find_one({"lead_id": lead["id"], "kind": "email_out"}, {"_id": 0})
        assert out and out.get("to") == alias
        async with await _client(_app()) as c:
            # Postmark replaces our Message-ID, so the reply references an id we never stored: matched by sender.
            body = _postmark(f"Testi Henkilö <{alias}>", "abc123@inbound.postmarkapp.com", "Re: " + (out.get("subject") or ""),
                             "Kyllä, olemme kiinnostuneita. Helsinki → Tukholma, 2 rekkaa viikossa.",
                             in_reply_to="<0000-postmark-id@mtasv.net>")
            r = await c.post("/api/email-channel/inbound", content=json.dumps(body), headers={**AUTH, "Content-Type": "application/json"})
            fresh = _postmark("someone@customer.example", "abc123@inbound.postmarkapp.com", "Tarjouspyyntö", "Hei, tarjous kiitos")
            r2 = await prospecting.try_ingest_reply(email_routes.inbound.postmark_to_raw(fresh))
        res = {"r": r.json(), "r2": r2, "lead": await db.crm_leads.find_one({"id": lead["id"]}, {"_id": 0}),
               "acts": await db.crm_activities.find({"lead_id": lead["id"], "direction": "in"}, {"_id": 0}).to_list(5)}
        await client.drop_database(os.environ["DB_NAME"])
        return res
    r = asyncio.run(go())
    assert r["r"]["channel"] == "crm" and r["r"]["status"] == "ingested", r["r"]
    assert r["lead"]["status"] != "contacted" and r["lead"].get("opportunity_id")
    assert len(r["acts"]) == 1 and r["acts"][0]["kind"] == "email_in"
    assert r["r2"] is None  # a fresh request is not a reply: it goes on to customer intake


def test_test_partner_rfq_reply_offer_and_accepted_rate_with_switch_off():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        await update_settings({"partner_network_enabled": False, "partner_approved_lanes": ["road:FI>SE"]})
        alias = "marc+s1partner@heliosone.fi"
        async with await _client(_app()) as c:
            outside = await c.post("/api/partner-network/test-partner", json={"email": "info@galaxytrans.fi"})
            r = await c.post("/api/partner-network/test-partner", json={"email": alias, "lane": "road:FI>SE"})
            row = r.json()
            again = await c.post("/api/partner-network/test-partner", json={"email": alias, "lane": "road:FI>SE"})
            reply = _postmark(f"TEST Partner <{alias}>", "abc123@inbound.postmarkapp.com", f"Re: {row['ref']}",
                              "Hinta 1450 EUR per kuorma, kaikki lisät mukana. Kuljetusaika 3 päivää. Voimassa 30.11.2026 asti.")
            ri = await c.post("/api/email-channel/inbound", content=json.dumps(reply), headers={**AUTH, "Content-Type": "application/json"})
            job = await db.jobs.find_one({"kind": "partner.inbound"}, {"_id": 0}) or await db.jobs.find_one({}, {"_id": 0})
            handled = await partner_network.process_inbound_job(job)
            o = await c.post(f"/api/partner-network/{row['id']}/offer",
                             json={"cost_cents": 145000, "transit_days": 3, "valid_to": "2026-11-30", "all_in": True})
            rate = o.json()
            rate_id = rate.get("rate_card_id") or rate.get("id")
            acc = await c.post(f"/api/partner-network/rates/{rate_id}/accepted")
        res = {"outside": outside.status_code, "row": row, "again": again.json(), "inbound": ri.json(), "handled": handled,
               "acc": acc.json(), "card": await db.rate_cards.find_one({"id": rate_id}, {"_id": 0}),
               "cand": await db.partner_candidates.find_one({"id": row["candidate_id"]}, {"_id": 0}),
               "carriers": await db.carriers.count_documents({}),
               "msgs": await db.partner_messages.find({"outreach_id": row["id"]}, {"_id": 0}).to_list(5)}
        await client.drop_database(os.environ["DB_NAME"])
        return res
    r = asyncio.run(go())
    assert r["outside"] == 409
    assert r["row"]["send"]["status"] in ("sent", "logged"), r["row"]["send"]
    assert r["row"]["test_mailbox"] and r["again"]["id"] == r["row"]["id"]
    assert r["inbound"]["channel"] == "partner_network"
    assert r["handled"]["status"] == "reply_received" and r["handled"]["parsed"].get("cost_cents") == 145000, r["handled"]
    assert r["acc"]["status"] == "accepted"
    assert r["card"]["archived"] is True and r["card"]["carrier_id"] is None and r["card"]["test_mailbox"] is True
    assert r["carriers"] == 0 and r["cand"]["status"] == "contacted"
    assert {m["direction"] for m in r["msgs"]} == {"out", "in"}


def test_company_mail_domain_never_includes_a_shared_provider_domain(monkeypatch):
    from core import config
    monkeypatch.delenv("COMPANY_MAIL_DOMAINS", raising=False)
    shared = "Huolitsija <02f0ce8d@inbound.postmarkapp.com>"
    assert not config.company_mail_domain("someone-else@inbound.postmarkapp.com", shared)
    assert config.company_mail_domain("marc+x@heliosone.fi", "Huolitsija <marc+huolitsija@heliosone.fi>")
    monkeypatch.setenv("COMPANY_MAIL_DOMAINS", "heliosone.fi")
    assert config.company_mail_domain("marc+x@heliosone.fi", shared)
    assert not config.company_mail_domain("info@hoviportaat.fi", shared)


def test_single_rfq_gate_accepts_smtp2go_as_a_real_provider(monkeypatch):
    from fastapi import HTTPException
    from routes.partner_network import _single_rfq_gates

    async def gate():
        client._io_loop = asyncio.get_running_loop()
        try:
            await _single_rfq_gates({"candidate_id": "missing"})
        except HTTPException as exc:
            return exc.detail
    monkeypatch.setenv("PARTNER_OUTREACH_LIVE", "false")
    monkeypatch.setenv("PARTNER_EMAIL_PROVIDER", "smtp2go")
    assert "sourced carrier" in asyncio.run(gate())  # past the provider gate
    monkeypatch.setenv("PARTNER_EMAIL_PROVIDER", "log")
    assert "real provider" in asyncio.run(gate())
