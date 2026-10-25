"""s7 CRM + prospecting against the real local API, real PRH/Eurostat/company websites and the GreenMail sandbox.

Backend env needed (see notes/s7-commercial.md): CRM_SANDBOX_DOMAIN=prospects.sandbox.test, CRM_EMAIL_PROVIDER=smtp, SMTP_HOST=127.0.0.1,
SMTP_PORT=3025, SMTP_SECURITY=none, CRM_EMAIL_FROM=..., CRM_IMAP_HOST=127.0.0.1, CRM_IMAP_PORT=3143, CRM_IMAP_SECURITY=none,
CRM_IMAP_USER=myynti@huolitsija.test, CRM_IMAP_PASSWORD=x. Test env: V7_API, V7_DB, DEMO_OPS_PASSWORD, GREENMAIL_SMTP/IMAP (defaults).
Live tests hit avoindata.prh.fi, ec.europa.eu (Eurostat Comext) and the company's own website.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import time
import uuid
from email.message import EmailMessage
from functools import lru_cache
from urllib.parse import urlparse

import httpx
import pymongo
import pytest

API = os.environ.get("V7_API", "http://localhost:8072/api")
DB = os.environ.get("V7_DB", "huo_s7_m1")
db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[DB]
IMAP_HOST, IMAP_PORT, SMTP_PORT = "127.0.0.1", 3143, 3025
CRM_INBOX = "myynti@huolitsija.test"
# A real company whose website lists a role address (sales@saato.fi on https://saato.fi/, checked 2026-10-24).
REAL_SITE = "https://saato.fi/"


@lru_cache
def ops() -> httpx.Client:
    r = httpx.post(f"{API}/auth/login", json={"email": "ops.demo@huolitsija.fi", "password": os.environ["DEMO_OPS_PASSWORD"]},
                   timeout=30)
    assert r.status_code == 200, r.text
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {r.json()['token']}"}, timeout=600)


def set_autopilot(on: bool):
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": False}})
    r = ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})
    assert r.status_code == 200, r.text


def mailbox(addr: str) -> list[email.message.Message]:
    c = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    c.login(addr, "x")
    c.select("INBOX")
    out = []
    for i in c.search(None, "ALL")[1][0].split():
        out.append(email.message_from_bytes(c.fetch(i, "(RFC822)")[1][0][1]))
    c.logout()
    return out


def wait(fn, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def _site_down(url: str) -> bool:
    try:
        return httpx.get(url, timeout=15, follow_redirects=True).status_code >= 500
    except httpx.HTTPError:
        return True


def manual_lead() -> dict:
    """A lead on a real website, enriched for real (homepage + contact page)."""
    db.crm_leads.delete_many({"website": REAL_SITE, "test": True})
    db.crm_optouts.delete_many({"email": "sales@saato.fi"})
    db.crm_optouts.delete_many({"domain": "saato.fi"})
    r = ops().post("/crm/leads", json={"name": f"Oy Säätö Ab testi {uuid.uuid4().hex[:4]}", "website": REAL_SITE,
                                       "country": "FI", "tol_code": "2829", "score": True})
    assert r.status_code == 200, r.text
    lead = r.json()
    db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"test": True}})
    r = ops().post(f"/crm/leads/{lead['id']}/enrich")
    assert r.status_code == 200, r.text
    detail = ops().get(f"/crm/leads/{lead['id']}").json()
    if not detail["contacts"] and _site_down(REAL_SITE):
        pytest.skip(f"upstream unreachable: {REAL_SITE} did not answer")
    assert any(c["email"] == "sales@saato.fi" for c in detail["contacts"]), detail["contacts"]
    for c in detail["contacts"]:
        assert c.get("lawful_basis"), c
        local = c["email"].split("@")[0]
        assert not re.fullmatch(r"[a-z]+\.[a-z]+", local), f"personal-looking address collected: {c['email']}"
    return detail


@pytest.fixture(scope="module", autouse=True)
def _sandbox_configured():
    st = ops().get("/crm/status").json()
    if not (st["email"]["sandbox_domain"] == "prospects.sandbox.test" and st["email"]["provider"] == "smtp"
            and st["imap"]["configured"]):
        pytest.skip("backend not started with the CRM sandbox env (see module docstring); refusing to mail real prospects")


@pytest.fixture(autouse=True)
def _reset_limits():
    db.settings.update_one({"key": "app"}, {"$set": {"crm_daily_send_limit": 200, "autopilot_halted": False}}, upsert=True)
    yield
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": False}})


# ---------------------------------------------------------------------------------------------------------------
def test_live_prospect_cycle_real_prh_and_eurostat_sends_only_to_sandbox():
    set_autopilot(True)
    try:
        r = ops().post("/crm/prospect/run", json={"max_new": 6, "max_enrich": 6})
        assert r.status_code == 200, r.text
        cyc = r.json()
    finally:
        set_autopilot(False)
    down = [n for n in cyc["notes"] if "did not answer" in n or "answered HTTP 5" in n]
    if cyc["found"] == 0 and down:
        pytest.skip(f"upstream unreachable: PRH open data ({down[0]})")
    assert cyc["autopilot"] is True and cyc["found"] >= 1, cyc
    assert any("avoindata.prh.fi/opendata-ytj-api/v3/companies?mainBusinessLine=" in n for n in cyc["notes"]), cyc["notes"]
    leads = ops().get("/crm/leads").json()["items"]
    prh = [x for x in leads if (x.get("source") or {}).get("source") == "prh" or x.get("origin") == "prh"]
    assert prh, "no PRH-sourced leads"
    for x in prh[:5]:
        assert re.fullmatch(r"\d{7}-\d", x["business_id"] or ""), x
        # every found company is live in YTJ: the agent skips ended names / liquidation / bankruptcy
        assert x["status"] != "disqualified" or x.get("disqualified_reason")
    traded = [x for x in leads if x.get("trade")]
    assert traded, "no lead carries Eurostat trade statistics"
    t = traded[0]["trade"]
    assert "eurostat" in str(t).lower() or "ec.europa.eu" in str(t), t
    for s in cyc["sends"]:
        if s["status"] == "sent":
            assert s["to"].endswith("@prospects.sandbox.test") and s["to_original"] != s["to"], s


def test_manual_send_lands_in_sandbox_with_rfc8058_headers():
    lead = manual_lead()
    r = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "first"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "sent" and res["to"] == "sales.saato-fi@prospects.sandbox.test", res
    msgs = wait(lambda: [m for m in mailbox(res["to"]) if m["Message-ID"] == res["message_id"]])
    assert msgs, "mail did not reach the GreenMail sandbox"
    m = msgs[0]
    assert re.fullmatch(r"<https?://[^>]+/api/crm/opt-out\?t=[^>]+>", m["List-Unsubscribe"].strip()), m["List-Unsubscribe"]
    assert m["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    body = m.get_payload(0).get_payload(decode=True).decode() if m.is_multipart() else m.get_payload(decode=True).decode()
    assert "sales@saato.fi" in body and "/api/crm/opt-out?t=" in body  # says where the address came from + opt-out link
    act = db.crm_activities.find_one({"lead_id": lead["id"], "direction": "out"})
    assert act and act["message_id"] == res["message_id"]
    # one first touch per company
    r = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "first"})
    assert r.status_code == 409, r.text


def _reply(to_msgid: str, sender: str, text: str) -> str:
    mid = f"<reply-{uuid.uuid4().hex}@saato.fi>"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, CRM_INBOX, "Re: Rahtihinnat"
    msg["Message-ID"], msg["In-Reply-To"], msg["References"] = mid, to_msgid, to_msgid
    msg.set_content(text)
    with smtplib.SMTP(IMAP_HOST, SMTP_PORT, timeout=10) as s:
        s.send_message(msg)
    return mid


def test_interested_reply_qualifies_and_autopilot_converts_to_customer():
    lead = manual_lead()
    sent = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "first"}).json()
    set_autopilot(True)
    try:
        _reply(sent["message_id"], "sales.saato-fi@prospects.sandbox.test",
               "Hei, kiinnostaa. Meillä on Hampuri → Tampere 2 rekkaa viikossa, lähettäkää hinta.")
        out = ops().post("/crm/replies/poll").json()
        assert out.get("configured") is not False, out
        after = wait(lambda: (lambda d: d if d["status"] in ("qualified", "converted") else None)(
            ops().get(f"/crm/leads/{lead['id']}").json()), 30)
    finally:
        set_autopilot(False)
    assert after and after["status"] in ("qualified", "converted"), after
    opp = after["opportunity"]
    assert opp and opp["stage"] in ("qualified", "quoting"), opp
    assert any(n.get("kind") == "lane_request" for n in opp.get("notes") or []) or opp.get("lanes"), opp
    assert after.get("company_id"), "autopilot did not convert the interested lead into a customer company"
    company = db.companies.find_one({"id": after["company_id"]})
    assert company and company.get("kyc_status") in ("not_started", "pending", None), company
    assert db.audit_log.find_one({"entity_id": lead["id"]}) or db.autonomy_actions.find_one({"entity_id": lead["id"]})


def test_opt_out_link_blocks_every_later_send():
    lead = manual_lead()
    sent = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "first"}).json()
    m = wait(lambda: [x for x in mailbox(sent["to"]) if x["Message-ID"] == sent["message_id"]])[0]
    url = m["List-Unsubscribe"].strip()[1:-1]
    path = urlparse(url)
    r = httpx.post(f"{API}/crm/opt-out?{path.query}", data={"List-Unsubscribe": "One-Click"}, timeout=20)
    assert r.status_code == 200 and "sales@saato.fi" in r.text, r.text
    assert db.crm_optouts.find_one({"email": "sales@saato.fi"})
    r = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "follow_up"})
    assert r.status_code == 409 and "opt" in r.text.lower(), r.text
    bad = httpx.get(f"{API}/crm/opt-out?t=c2FsZXNAc2FhdG8uZmk.deadbeef", timeout=20)
    assert bad.status_code in (200, 400) and "sales@saato.fi" not in bad.text  # forged token does nothing


def test_daily_send_limit_holds_sends():
    lead = manual_lead()
    db.settings.update_one({"key": "app"}, {"$set": {"crm_daily_send_limit": 0}})
    r = ops().post(f"/crm/leads/{lead['id']}/contact", json={"kind": "first"})
    assert r.status_code == 429, r.text
    assert not db.crm_activities.find_one({"lead_id": lead["id"], "direction": "out"})


def test_kill_switch_stops_the_agent():
    manual_lead()
    set_autopilot(True)
    db.settings.update_one({"key": "app"}, {"$set": {"autopilot_halted": True}})
    try:
        cyc = ops().post("/crm/prospect/run", json={"max_new": 2, "max_enrich": 2}).json()
        fu = ops().post("/crm/follow-ups/run").json()
    finally:
        set_autopilot(False)
    assert cyc["halted"] is True and cyc["sent"] == 0, cyc
    assert not [s for s in fu.get("sends", []) if s.get("status") == "sent"], fu


def test_pipeline_summary_and_stage_change_events():
    lead = manual_lead()
    opp = ops().post("/crm/opportunities", json={"lead_id": lead["id"], "title": "FI>DE groupage",
                                                 "estimated_annual_value_cents": 4_800_000}).json()
    r = ops().patch(f"/crm/opportunities/{opp['id']}", json={"stage": "negotiation"})
    assert r.status_code == 200 and r.json()["stage"] == "negotiation", r.text
    assert ops().patch(f"/crm/opportunities/{opp['id']}", json={"stage": "bogus"}).status_code == 400
    p = ops().get("/crm/pipeline").json()
    neg = next(s for s in p["stages"] if s["stage"] == "negotiation")
    assert neg["count"] >= 1 and neg["estimated_annual_value_cents"] >= 4_800_000, p
    acts = ops().get("/crm/activities", params={"opportunity_id": opp["id"]}).json()["items"]
    assert any(a["kind"] == "stage_changed" for a in acts), acts
