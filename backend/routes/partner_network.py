"""Sourcing Autopilot (Hankinta-autopilotti) API. Internal path keeps /api/partner-network."""
import base64
import hashlib
import hmac
import html
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core import config
from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.settings import get_settings
from core.util import NO_ID, now_iso
from services import partner_discovery as discovery
from services import partner_network as svc

router = APIRouter(prefix="/api/partner-network", tags=["partner-network"])


@router.get("")
async def overview(user: dict = Depends(require_internal)):
    items = await db.partner_outreach.find({}, NO_ID).sort("created_at", -1).to_list(200)
    for row in items:
        row["replies"] = await db.partner_replies.find({"outreach_id": row["id"]}, NO_ID).sort("at", -1).to_list(10)
    return {"items": items, "email_mode": config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER,
            "status": await svc.status(),
            "settings": {k: v for k, v in (await get_settings()).items() if k.startswith("partner_")}}


@router.get("/status")
async def status(user: dict = Depends(require_internal)):
    return await svc.status()


@router.get("/candidates")
async def candidates(lane: str | None = None, status: str | None = None, limit: int = 200,
                     user: dict = Depends(require_internal)):
    q = {}
    if lane:
        q["lanes"] = lane
    if status:
        q["status"] = status
    rows = await db.partner_candidates.find(q, NO_ID).sort([("score", -1), ("name", 1)]).to_list(min(max(limit, 1), 1000))
    runs = await db.partner_discovery_runs.find({}, NO_ID).to_list(100)
    return {"items": rows, "total": await db.partner_candidates.count_documents(q), "runs": runs}


@router.get("/messages")
async def messages(outreach_id: str | None = None, limit: int = 300, user: dict = Depends(require_internal)):
    q = {"outreach_id": outreach_id} if outreach_id else {}
    rows = await db.partner_messages.find(q, NO_ID).sort([("at", -1), ("seq", -1)]).to_list(min(max(limit, 1), 2000))
    return {"items": rows, "total": await db.partner_messages.count_documents(q)}


@router.get("/optouts")
async def optouts(user: dict = Depends(require_internal)):
    return {"items": await db.partner_optouts.find({}, NO_ID).sort("at", -1).to_list(1000)}


@router.post("/optouts")
async def add_optout(body: dict, user: dict = Depends(require_perm("settings.write"))):
    email = (body.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter an e-mail address")
    scope = "domain" if body.get("scope") == "domain" else "address"
    return await svc.add_optout(email, source="operator", scope=scope, note=body.get("note"), actor=user)


@router.get("/{outreach_id}/thread")
async def thread(outreach_id: str, user: dict = Depends(require_internal)):
    row = await db.partner_outreach.find_one({"id": outreach_id}, NO_ID)
    if not row:
        raise HTTPException(404, "Partner RFQ not found")
    msgs = await db.partner_messages.find({"outreach_id": outreach_id}, NO_ID).sort([("at", 1), ("seq", 1)]).to_list(200)
    rate = await db.rate_cards.find_one({"id": row.get("rate_card_id")}, NO_ID) if row.get("rate_card_id") else None
    return {"outreach": row, "messages": msgs, "rate": rate}


@router.post("/run")
async def run(body: dict | None = None, user: dict = Depends(require_perm("settings.write"))):
    return await svc.run_cycle(user, discover=(body or {}).get("discover", True) is not False)


@router.post("/discover")
async def discover(body: dict, user: dict = Depends(require_perm("settings.write"))):
    lane = body.get("lane")
    s = await get_settings()
    if lane not in s["partner_approved_lanes"]:
        raise HTTPException(409, "Lane is outside the approved partner policy")
    return await discovery.discover_lane(lane, force=bool(body.get("force")), contact_lookup=svc.published_contact)


@router.post("/poll")
async def poll(user: dict = Depends(require_perm("settings.write"))):
    return await svc.poll_inbox()


@router.post("/stop")
async def stop(user: dict = Depends(require_perm("settings.write"))):
    return await svc.stop(user)


@router.post("/lane-requests")
async def lane_request(body: dict, user: dict = Depends(require_perm("rates.write"))):
    return await svc.request_lane_rates(body.get("origin") or "", body.get("destination") or "", body.get("mode") or "",
                                        body.get("cargo"), max_cost_cents=body.get("max_cost_cents"),
                                        quote_id=body.get("quote_id"), requested_by=user.get("id"), service=body.get("service"))


@router.get("/lane-requests/{request_id}")
async def lane_request_status(request_id: str, user: dict = Depends(require_internal)):
    req = await svc.get_lane_request(request_id)
    if not req:
        raise HTTPException(404, "Lane rate request not found")
    return req


@router.post("/{outreach_id}/offer")
async def offer(outreach_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    o = await db.partner_outreach.find_one({"id": outreach_id}, NO_ID)
    if not o:
        raise HTTPException(404, "Partner RFQ not found")
    if body.get("all_in") is not True:
        raise HTTPException(409, "Confirm that the offer includes all mandatory charges")
    return await svc.process_offer(o, body, user)


@router.post("/rates/{rate_id}/{decision}")
async def decide_rate(rate_id: str, decision: str, user: dict = Depends(require_perm("rates.write"))):
    if decision not in ("accepted", "rejected", "revoked"):
        raise HTTPException(400, "Choose accepted, rejected or revoked")
    r = await db.rate_cards.find_one({"id": rate_id, "source": "partner_network"}, NO_ID)
    if not r:
        raise HTTPException(404, "Partner rate not found")
    if decision == "accepted":
        s = await get_settings()
        o = await db.partner_outreach.find_one({"id": r["outreach_id"]}, NO_ID)
        if (r.get("all_in_cost_cents") or r["unit_cost_cents"]) > svc.row_limit(s, o):
            raise HTTPException(409, "Offer exceeds the current partner policy")
    await db.rate_cards.update_one({"id": rate_id}, {"$set": {"status": decision}})
    await db.partner_outreach.update_one({"id": r["outreach_id"]}, {"$set": {"status": decision}})
    await audit(user, "partner_rate_" + decision, "rate_card", rate_id, {"before": r["status"], "after": decision})
    return {"id": rate_id, "status": decision}


@router.post("/verification-rfq")
async def verification_rfq(user: dict = Depends(require_perm("rates.write"))):
    """One RFQ per configured company mailbox, using the normal send gate and parser. No contact override."""
    email = config.env("PARTNER_VERIFICATION_EMAIL").lower()
    if not email or email not in {x.strip().lower() for x in config.env("PARTNER_OUTREACH_ALLOWLIST").split(",")}:
        raise HTTPException(409, "Configure a company verification mailbox in the outreach allowlist")
    if not await svc.is_on():
        raise HTTPException(409, "Sourcing Autopilot must be enabled for the mailbox check")
    cid = "mail-verification-" + hashlib.sha256(email.encode()).hexdigest()[:16]
    prior = await db.partner_outreach.find_one({"candidate_id": cid}, NO_ID)
    if prior:
        return prior  # never send the single verification RFQ twice
    domain = email.rsplit("@", 1)[-1]
    candidate = {"id": cid, "name": "Company mailbox delivery verification", "domain": domain,
                 "country": "FI", "contact_email": email, "status": "verification", "verification_only": True,
                 "contact_source_url": "Company-controlled mailbox authorized by owner", "sources": []}
    await db.partner_candidates.update_one({"id": cid}, {"$setOnInsert": candidate}, upsert=True)
    settings = await get_settings()
    result = await svc.open_rfq(candidate, "sea:CN>FI", settings,
                                {"id": None, "max_cost_cents": settings["partner_max_price_cents"]})
    await audit(user, "partner_mailbox_verification", "partner_outreach", (result or {}).get("id"), {"recipient": email})
    return result or await db.partner_outreach.find_one({"candidate_id": cid}, NO_ID)


@router.post("/test-partner")
async def test_partner(body: dict, user: dict = Depends(require_perm("rates.write"))):
    """One RFQ to a TEST partner on a company-owned mailbox alias (e.g. marc+partner@<own domain>): the real send,
    inbound reply, offer and acceptance path with no outside company contacted. Its rate card is archived and has no
    carrier, so production pricing never uses it. Idempotent per address and lane."""
    email = (body.get("email") or "").strip().lower()
    if not svc.company_mailbox(email):
        raise HTTPException(409, "A TEST partner must use an address on the company's own sending domain")
    lane = body.get("lane") or "road:FI>SE"
    mode, _, countries = lane.partition(":")
    if mode not in svc.SERVICE or len(countries.split(">")) != 2:
        raise HTTPException(400, "lane is mode:CC>CC, e.g. road:FI>SE")
    settings = await get_settings()
    cid = "test-partner-" + hashlib.sha256(f"{email}|{lane}".encode()).hexdigest()[:16]
    candidate = {"id": cid, "name": f"TEST partner ({email})", "domain": email.rpartition("@")[2],
                 "country": countries.split(">")[0], "contact_email": email, "status": "test", "test_mailbox": True,
                 "lanes": [lane], "contact_source_url": "Company-controlled test mailbox", "sources": [],
                 "created_at": now_iso()}
    await db.partner_candidates.update_one({"id": cid}, {"$setOnInsert": candidate}, upsert=True)
    result = await svc.open_rfq(candidate, lane, settings,
                                {"id": None, "max_cost_cents": settings["partner_max_price_cents"]})
    await audit(user, "partner_test_rfq", "partner_outreach", (result or {}).get("id"), {"recipient": email, "lane": lane})
    return result or await db.partner_outreach.find_one({"candidate_id": cid}, NO_ID)


async def _single_rfq_gates(body: dict):
    """Every precondition for the one real RFQ; raises 409 with the first unmet gate."""
    if not config.env_bool("PARTNER_SINGLE_RFQ_ONLY") or svc.outreach_live():
        raise HTTPException(409, "Enable single-RFQ mode and keep broad outreach disabled")
    if config.sandbox_domain("PARTNER_SANDBOX_DOMAIN") or \
            (config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER) not in ("postmark", "smtp2go"):
        raise HTTPException(409, "Single real RFQ requires a real provider (Postmark or SMTP2GO) without address remapping")
    candidate = await db.partner_candidates.find_one({"id": body.get("candidate_id")}, NO_ID)
    if not candidate or candidate.get("verification_only") or not candidate.get("contact_source_url"):
        raise HTTPException(409, "Select a sourced carrier or agent with published contact evidence")
    email = (candidate.get("contact_email") or "").strip().lower()
    allowed = {x.strip().lower() for x in config.env("PARTNER_OUTREACH_ALLOWLIST").split(",") if x.strip()}
    if allowed != {email} or not email:
        raise HTTPException(409, "Allowlist must contain only the selected recipient")
    ok, _ = discovery.email_is_b2b(email, candidate)
    if not ok or await svc.is_opted_out(email, candidate.get("domain")):
        raise HTTPException(409, "Selected contact is not eligible for outreach")
    req = await db.lane_rate_requests.find_one({"id": body.get("request_id")}, NO_ID)
    if not req or req.get("status") not in ("off", "manual") or not req.get("request_scope"):
        raise HTTPException(409, "Prepare an idle scoped rate request in single-RFQ mode")
    settings = await get_settings()
    if req["lane"] not in settings["partner_approved_lanes"] or req["lane"] not in candidate.get("lanes", []):
        raise HTTPException(409, "Request must match the candidate lane and approved policy")
    if not await svc.is_on() or not svc.partner_reply_to():
        raise HTTPException(409, "Enable sourcing with the inbound reply mailbox configured")
    return candidate, req, settings


@router.post("/single-rfq")
async def single_rfq(body: dict, user: dict = Depends(require_perm("rates.write"))):
    """One real sourced contact, one prepared request, no campaign or automatic follow-up.

    Nothing is sent here: the RFQ waits in the owner queue (kind partner_rfq) and leaves only on approval.
    """
    prior = await db.partner_outreach.find_one({"campaign_key": "single-real-rfq-v1"}, NO_ID)
    if prior:
        return prior  # includes uncertain/failed attempts; never retry the provider automatically
    candidate, req, _ = await _single_rfq_gates(body)
    from pymongo.errors import DuplicateKeyError
    from core.approvals import request_approval
    try:  # one owner card across concurrent calls
        await db.raw.partner_single_rfq.insert_one({"_id": "single-real-rfq-v1", "at": now_iso()})
    except DuplicateKeyError:
        lock = await db.raw.partner_single_rfq.find_one({"_id": "single-real-rfq-v1"}) or {}
        card = await db.approvals.find_one({"id": lock.get("approval_id")}, NO_ID) if lock.get("approval_id") else None
        return {"status": "awaiting_approval" if not card or card.get("status") == "pending" else card.get("status"),
                "approval": card}
    email = candidate["contact_email"].strip().lower()
    cargo = req.get("cargo") or {}
    summary = (f"One non-binding rate enquiry to {candidate['name']} <{email}> (public source {candidate['contact_source_url']}), "
               f"lane {req['lane']}, {req.get('origin')} → {req.get('destination')}, cargo {cargo}. "
               "Sent once through Postmark with sender identity (company in formation), opt-out link, no follow-ups.")
    approval = await request_approval(
        "partner_rfq", "lane_rate_request", req["id"], f"Send one RFQ to {candidate['name']}", summary=summary,
        reason="single real RFQ needs owner approval", requested_by=user,
        data={"candidate_id": candidate["id"], "request_id": req["id"], "recipient": email,
              "contact_source_url": candidate["contact_source_url"], "lane": req["lane"]})
    await db.raw.partner_single_rfq.update_one({"_id": "single-real-rfq-v1"}, {"$set": {"approval_id": approval["id"]}})
    await audit(user, "partner_single_rfq_requested", "approval", approval["id"],
                {"candidate_id": candidate["id"], "request_id": req["id"]})
    return {"status": "awaiting_approval", "approval": approval}


async def send_approved_single_rfq(approval: dict, actor: dict | None) -> dict | None:
    """Owner approved the partner_rfq card: re-check every gate, then send exactly once."""
    data = approval.get("data") or {}
    prior = await db.partner_outreach.find_one({"campaign_key": "single-real-rfq-v1"}, NO_ID)
    if prior:
        return prior
    try:
        candidate, req, settings = await _single_rfq_gates(data)
    except HTTPException as exc:
        await db.approvals.update_one({"id": approval["id"]}, {"$set": {"send_blocked": exc.detail}})
        return None
    if (candidate.get("contact_email") or "").strip().lower() != data.get("recipient"):
        await db.approvals.update_one({"id": approval["id"]}, {"$set": {"send_blocked": "recipient changed after approval"}})
        return None
    await db.lane_rate_requests.update_one({"id": req["id"]}, {"$set": {"status": "manual"}})
    result = await svc.open_rfq(candidate, req["lane"], settings, req, single_rfq_only=True)
    await audit(actor or {"id": "owner", "name": "Owner"}, "partner_single_rfq", "partner_outreach", (result or {}).get("id"),
                {"candidate_id": candidate["id"], "request_id": req["id"], "approval_id": approval["id"]})
    row = result or await db.partner_outreach.find_one({"campaign_key": "single-real-rfq-v1"}, NO_ID)
    await db.approvals.update_one({"id": approval["id"]}, {"$set": {"outreach_id": (row or {}).get("id")}})
    return row


from core.events import on  # noqa: E402


@on("approval.decided")
async def _on_partner_rfq_decided(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") == "partner_rfq" and a.get("status") == "approved":
        await send_approved_single_rfq(a, p.get("actor"))


@router.post("/inbound/postmark")
async def postmark_inbound(request: Request):
    """Native Postmark inbound JSON. Configure HTTP Basic auth in the webhook URL; no invented HMAC gateway."""
    secret = config.env("PARTNER_INBOUND_SECRET")
    auth = request.headers.get("authorization", "")
    try:
        credentials = base64.b64decode(auth[6:], validate=True).decode() if auth.lower().startswith("basic ") else ""
    except (ValueError, UnicodeError):
        credentials = ""
    if not secret or not hmac.compare_digest(credentials, "postmark:" + secret):
        raise HTTPException(401, "Invalid inbound credentials")
    payload = await request.body()
    if len(payload) > 5_000_000:
        raise HTTPException(413, "Reply is too large")
    try:
        body = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    from services.partner_mailbox import parse_postmark
    try:
        msg = parse_postmark(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    result = await svc.queue_inbound(msg)
    # Unmatched/sender-mismatch messages are retained for inspection. Acknowledge them, otherwise Postmark retries
    # permanently undeliverable business messages. Real processing failures return 500 and can be retried.
    return {k: v for k, v in result.items() if k != "parsed"}


@router.post("/inbound")
async def inbound(request: Request):
    """Mail gateway webhook. The gateway signs the raw UTF-8 JSON with PARTNER_INBOUND_SECRET (HMAC-SHA256, hex in
    X-Huolitsija-Signature). JSON: message_id, from, to, subject, text, in_reply_to. The RFQ is found by its reference
    or In-Reply-To; `from` must be the RFQ recipient. Same processing as the IMAP reader."""
    payload = await request.body()
    secret = config.env("PARTNER_INBOUND_SECRET")
    signature = request.headers.get("x-huolitsija-signature", "")
    if not secret or not hmac.compare_digest(hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest(), signature):
        raise HTTPException(401, "Invalid inbound email signature")
    if len(payload) > 50000:
        raise HTTPException(413, "Reply is too large")
    try:
        body = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body.get("message_id"), str) or not body["message_id"].strip():
        raise HTTPException(400, "Inbound message ID is required")
    if body.get("outreach_id") and not body.get("subject"):
        row = await db.partner_outreach.find_one({"id": body["outreach_id"]}, NO_ID)
        body["subject"] = f"[{row['ref']}]" if row and row.get("ref") else ""
    result = await svc.ingest_message({**body, "source": "webhook"})
    if result.get("status") in ("unmatched", "sender_mismatch"):
        raise HTTPException(400, "Reply sender does not match the published RFQ contact")
    return {k: v for k, v in result.items() if k != "parsed"}


def _page(title: str, body: str, form: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="fi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(title)}</title><style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;
color:#111;max-width:560px;margin:48px auto;padding:0 20px;line-height:1.5}}h1{{font-size:20px;font-weight:600}}p{{font-size:15px}}
.muted{{color:#555;font-size:13px}}button{{background:#111;color:#fff;border:0;border-radius:999px;padding:10px 18px;font-size:15px;cursor:pointer}}</style></head>
<body><p class="muted">{html.escape(config.COMPANY_NAME)}</p><h1>{html.escape(title)}</h1>{body}{form}</body></html>""")


@router.get("/opt-out", response_class=HTMLResponse)
async def opt_out_page(t: str = ""):
    email = svc.optout_email_from_token(t)
    if not email:
        return _page("Linkki ei kelpaa · Invalid link", "<p>Pyydä poistoa vastaamalla viestiin sanalla \"poista\".</p>"
                     "<p>Reply to the message with the word \"unsubscribe\" and we remove you.</p>")
    if await svc.is_opted_out(email):
        return _page("Osoite on jo poistettu · Already removed", f"<p>{html.escape(email)} ei saa meiltä enää tarjouspyyntöjä.</p>"
                     f"<p>{html.escape(email)} receives no further requests from us.</p>")
    return _page("Lopeta tarjouspyynnöt · Stop quote requests",
                 f"<p>Vahvista, ettei osoitteeseen <b>{html.escape(email)}</b> lähetetä enää tarjouspyyntöjä.</p>"
                 f"<p>Confirm that <b>{html.escape(email)}</b> gets no more requests for quotation from us.</p>",
                 f"""<form method="post"><input type="hidden" name="t" value="{html.escape(t)}">
<p><label><input type="checkbox" name="domain" value="1"> Koko yritys (kaikki @{html.escape(email.split('@')[1])}) · Whole company</label></p>
<button type="submit">Lopeta · Stop</button></form>""")


@router.post("/opt-out", response_class=HTMLResponse)
async def opt_out_confirm(request: Request, t: str = ""):
    """Form confirmation and RFC 8058 one-click (body List-Unsubscribe=One-Click, token in the query)."""
    form = {}
    try:
        form = dict(await request.form())
    except Exception:  # noqa: BLE001 - one-click clients may send no form body
        form = {}
    token = form.get("t") or t
    email = svc.optout_email_from_token(token or "")
    if not email:
        raise HTTPException(400, "Invalid opt-out link")
    scope = "domain" if form.get("domain") == "1" else "address"
    source = "one-click" if form.get("List-Unsubscribe") == "One-Click" else "opt-out link"
    await svc.add_optout(email, source=source, scope=scope, actor={"id": "recipient", "name": email})
    return _page("Poistettu · Removed", f"<p>Selvä. {html.escape(email if scope == 'address' else '@' + email.split('@')[1])} ei saa meiltä "
                 f"enää tarjouspyyntöjä. Tieto tallennettiin {now_iso()[:10]}.</p><p>Done. No further requests for quotation will be sent.</p>")
