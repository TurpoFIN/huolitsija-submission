"""s7 CRM and prospecting routes (shared/interfaces.md, section s7). Internal [customers.write] except the public
opt-out page (`public_router`, no auth, RFC 8058 one-click)."""
import html

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.db import db
from core.deps import require_perm
from core.util import now_iso
from services import crm, prospecting

router = APIRouter(prefix="/api/crm", tags=["crm"])
public_router = APIRouter(prefix="/api/crm", tags=["crm-public"])
extra_routers = [public_router]
perm = require_perm("customers.write")


@router.get("/leads")
async def leads(status: str | None = None, q: str | None = None, limit: int = 200, user: dict = Depends(perm)):
    return {"items": await crm.list_leads(status, q, limit)}


@router.post("/leads")
async def create_lead(body: dict, user: dict = Depends(perm)):
    test_email = (body.get("contact_email") or "").strip().lower()
    if test_email and not prospecting.company_mailbox(test_email):
        raise HTTPException(409, "contact_email is only for a TEST lead on the company's own mailbox domain")
    lead = await crm.create_lead(body, user, source="manual")
    if test_email:  # TEST lead: the real draft/approve/send/reply path, addressed to a company-owned alias
        c = await crm.upsert_contact(lead, test_email, "general", "Company-controlled test mailbox")
        sets = {"test_mailbox": True, "primary_email": c["email"], "primary_contact_id": c["id"], "contact_ids": [c["id"]]}
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets})
        lead = {**lead, **sets}
    if body.get("score", True):
        from core.settings import get_settings
        lead = await prospecting.score_lead(lead, await get_settings())
    return lead


@router.get("/leads/{lead_id}")
async def lead(lead_id: str, user: dict = Depends(perm)):
    return await crm.lead_detail(lead_id)


@router.patch("/leads/{lead_id}")
async def patch_lead(lead_id: str, body: dict, user: dict = Depends(perm)):
    return await crm.update_lead(lead_id, body, user)


@router.post("/leads/{lead_id}/enrich")
async def enrich(lead_id: str, user: dict = Depends(perm)):
    from core.settings import get_settings
    return await prospecting.enrich_lead(await crm.get_lead(lead_id), await get_settings())


@router.post("/leads/{lead_id}/draft")
async def draft(lead_id: str, user: dict = Depends(perm)):
    """Park the first e-mail for this one lead as an owner-queue card (what the cycle does for every contactable lead)."""
    from core.settings import get_settings
    lead = await crm.get_lead(lead_id)
    if lead["status"] != "contactable" or lead.get("first_touch_at"):
        raise HTTPException(409, f"Only a contactable lead that was never contacted gets a draft (status {lead['status']})")
    d = await prospecting.draft_for(lead, await get_settings(), reason=f"Draft requested by {user.get('name') or 'ops'}")
    if not d:
        raise HTTPException(409, "No active contact address")
    return {"draft": d, "approval_id": (await crm.get_lead(lead_id)).get("draft", {}).get("approval_id")}


@router.post("/leads/{lead_id}/contact")
async def contact(lead_id: str, body: dict | None = None, user: dict = Depends(perm)):
    """A person sends the waiting draft now (first touch, or a follow-up with {"kind": "follow_up"})."""
    lead = await crm.get_lead(lead_id)
    kind = (body or {}).get("kind") or ("follow_up" if lead.get("first_touch_at") else "first")
    if kind not in ("first", "follow_up"):
        raise HTTPException(400, "kind is first or follow_up")
    res = await prospecting.send_prospect_email(lead, kind, actor=user, manual=True)
    if res["status"] not in ("blocked", "held_limit"):
        from core.approvals import cancel_approvals

        await cancel_approvals(lead_id, "prospect_email")  # sent by hand: the owner card is done
    if res["status"] == "blocked":
        raise HTTPException(409, f"Not sent: {res['reason']}")
    if res["status"] == "held_limit":
        raise HTTPException(429, f"Not sent: {res['reason']}")
    return res


@router.post("/leads/{lead_id}/convert")
async def convert(lead_id: str, user: dict = Depends(perm)):
    return await crm.convert_lead(lead_id, user)


@router.get("/opportunities")
async def opportunities(stage: str | None = None, company_id: str | None = None, user: dict = Depends(perm)):
    return {"items": await crm.list_opportunities(stage, company_id)}


@router.post("/opportunities")
async def create_opportunity(body: dict, user: dict = Depends(perm)):
    return await crm.create_opportunity(body, user)


@router.get("/opportunities/{opp_id}")
async def opportunity(opp_id: str, user: dict = Depends(perm)):
    opp = await crm.get_opportunity(opp_id)
    opp["activities"] = await crm.activities(opportunity_id=opp_id)
    return opp


@router.patch("/opportunities/{opp_id}")
async def patch_opportunity(opp_id: str, body: dict, user: dict = Depends(perm)):
    return await crm.update_opportunity(opp_id, body, user)


@router.get("/pipeline")
async def pipeline(user: dict = Depends(perm)):
    return await crm.pipeline_summary()


@router.get("/activities")
async def activities(lead_id: str | None = None, opportunity_id: str | None = None, limit: int = 200, user: dict = Depends(perm)):
    return {"items": await crm.activities(lead_id, opportunity_id, limit)}


@router.post("/prospect/run")
async def prospect_run(body: dict | None = None, user: dict = Depends(perm)):
    body = body or {}
    return await prospecting.run_cycle(user, max_new=body.get("max_new"), max_enrich=body.get("max_enrich"))


@router.post("/follow-ups/run")
async def follow_ups_run(user: dict = Depends(perm)):
    return await prospecting.follow_ups(user)


@router.post("/replies/poll")
async def replies_poll(user: dict = Depends(perm)):
    return await prospecting.poll_replies()


@router.get("/status")
async def status(user: dict = Depends(perm)):
    return await prospecting.status()


@router.get("/optouts")
async def optouts(user: dict = Depends(perm)):
    from core.db import db
    from core.util import NO_ID
    return {"items": await db.crm_optouts.find({}, NO_ID).sort("at", -1).to_list(2000)}


@router.post("/optouts")
async def add_optout(body: dict, user: dict = Depends(perm)):
    email = (body.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter an e-mail address")
    return await crm.add_optout(email, source="operator", scope="domain" if body.get("scope") == "domain" else "address",
                                actor=user)


# ---------------------------------------------------------------- public opt-out (no auth)

def _page(title: str, body: str, form: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="fi"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:48px auto;padding:0 20px;color:#111}}
h1{{font-size:20px;font-weight:500}}button{{background:#111;color:#fff;border:0;border-radius:999px;padding:9px 16px;font-size:14px}}
p{{line-height:1.5}}</style></head><body><p style="color:#666;font-size:13px">Huolitsija</p><h1>{html.escape(title)}</h1>{body}{form}
</body></html>""")


@public_router.get("/opt-out", response_class=HTMLResponse)
async def opt_out_page(t: str = ""):
    address = crm.optout_email_from_token(t)
    if not address:
        return _page("Linkki ei kelpaa · Invalid link", "<p>Vastatkaa viestiin sanalla \"poista\", niin poistamme osoitteen.</p>"
                     "<p>Reply to the message with the word \"unsubscribe\" and we remove the address.</p>")
    a = html.escape(address)
    if await crm.is_opted_out(address):
        return _page("Osoite on jo poistettu · Already removed", f"<p>{a} ei saa meiltä enää viestejä.</p>"
                     f"<p>{a} receives no further e-mail from us.</p>")
    return _page("Lopeta viestit · Stop e-mails",
                 f"<p>Vahvistakaa, ettei osoitteeseen <b>{a}</b> lähetetä enää myyntiviestejä.</p>"
                 f"<p>Confirm that <b>{a}</b> gets no more sales e-mail from us.</p>",
                 f"""<form method="post"><input type="hidden" name="t" value="{html.escape(t)}">
<p><label><input type="checkbox" name="domain" value="1"> Koko yritys (kaikki @{html.escape(address.split('@')[1])}) · Whole company</label></p>
<button type="submit">Lopeta · Stop</button></form>""")


@public_router.post("/opt-out", response_class=HTMLResponse)
async def opt_out_confirm(request: Request, t: str = ""):
    """Form confirmation and RFC 8058 one-click (body List-Unsubscribe=One-Click, token in the URL)."""
    try:
        form = dict(await request.form())
    except Exception:  # noqa: BLE001 - one-click clients may send no form body
        form = {}
    address = crm.optout_email_from_token(form.get("t") or t or "")
    if not address:
        raise HTTPException(400, "Invalid opt-out link")
    scope = "domain" if form.get("domain") == "1" else "address"
    source = "one-click" if form.get("List-Unsubscribe") == "One-Click" else "opt-out link"
    await crm.add_optout(address, source=source, scope=scope, actor={"type": "system", "id": "recipient", "name": address})
    shown = html.escape(address if scope == "address" else "@" + address.split("@")[1])
    return _page("Poistettu · Removed", f"<p>Selvä. {shown} ei saa meiltä enää viestejä. Tallennettu {now_iso()[:10]}.</p>"
                 "<p>Done. No further e-mail will be sent.</p>")
