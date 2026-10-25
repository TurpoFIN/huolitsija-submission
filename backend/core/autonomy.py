"""Backend-owned autonomy policy. Switches are off unless explicitly enabled by an operator."""
import math
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from core.audit import audit
from core.db import db
from core.sandbox import is_test_company
from core.settings import get_settings, update_settings
from core.util import new_id, now_iso

SWITCHES = {"autopilot_enabled", "partner_network_enabled"}
# Changed only through their own typed-confirmation endpoints (routes/autopilot.py), never the generic settings PATCH.
PROTECTED = {"autopilot_halted", "customs_filing_delegated", "customs_delegation_scope"}


async def switch(name: str, enabled: bool, confirmation: str, actor: dict):
    if name not in SWITCHES or type(enabled) is not bool:
        raise HTTPException(400, "Unknown autonomy switch")
    if enabled and confirmation != "enable":
        raise HTTPException(400, "Type enable to enable autonomous actions")
    before = bool((await get_settings())[name])
    changes = {name: enabled}
    if name == "autopilot_enabled" and enabled:
        changes["autopilot_halted"] = False  # switching Autopilot on with the typed word also lifts the kill switch
    result = await update_settings(changes)
    await db.settings.update_one({"key": "app"}, {"$set": {name + "_changed": {
        "at": now_iso(), "by": {"id": actor["id"], "name": actor.get("name")}, "enabled": enabled}}})
    await audit(actor, "autonomy_switch_changed", "settings", "app", {"setting": name, "before": before, "after": enabled})
    return result


async def action(kind: str, entity: str, entity_id: str, *, company_id=None, data=None, reversible=False):
    doc = {"id": new_id(), "kind": kind, "entity": entity, "entity_id": entity_id, "company_id": company_id,
           "at": now_iso(), "data": data or {}, "reversible": reversible,
           "reversal": None, "actor": {"id": "autopilot", "name": "Autopilot"}}
    await db.autonomy_actions.insert_one(dict(doc))
    await audit(doc["actor"], "autopilot_" + kind, entity, entity_id, doc, company_id=company_id)
    try:  # every autonomous action also appears in the agent decision log
        from core.agent_runtime import record_action
        await record_action(doc)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("autonomy").exception("decision log for action %s failed", kind)
    return doc


# s6 r94: Autopilot for one test tenant only, for at most an hour, while the global switch stays off. The grant lives on
# the company (companies.test_autopilot = {enabled, expires_at, ...}) and is set only by PATCH
# /api/autopilot/test-tenants/{id}. Every guard in core.agent_runtime.decide still applies; the kill switch ends it.
TEST_GRANT_MAX_MINUTES = 60


GRANT_CHANNELS = {"email_channel"}  # gates that are not agents in core.agent_runtime.AGENT_LABELS


def grant_agent_names() -> set[str]:
    from core.agent_runtime import AGENT_LABELS
    return set(AGENT_LABELS) | GRANT_CHANNELS


def test_grant_active(company: dict | None, at: datetime | None = None, agent: str | None = None) -> bool:
    """Fails closed: a real company, a missing/false flag, a missing, naive or past expiry, or (when the grant names
    agents) an agent outside that list is no grant."""
    if not is_test_company(company):
        return False
    grant = (company or {}).get("test_autopilot") or {}
    if grant.get("enabled") is not True or not isinstance(grant.get("expires_at"), str):
        return False
    agents = grant.get("agents")
    if agents is not None and (not isinstance(agents, list) or agent not in agents):
        return False
    try:
        expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        return False
    at = at or datetime.now(timezone.utc)
    return at < expires <= at + timedelta(minutes=TEST_GRANT_MAX_MINUTES + 1)


async def autopilot_scope(company_id: str | None, settings: dict | None = None, agent: str | None = None) -> str:
    """'global' (Autopilot on), 'test_tenant' (an active grant for this test tenant and, if the grant names agents,
    this agent) or 'off'. The kill switch is 'off'."""
    s = settings if settings is not None else await get_settings()
    if s.get("autopilot_halted"):
        return "off"
    if s.get("autopilot_enabled"):
        return "global"
    if not company_id:
        return "off"
    company = await db.raw.companies.find_one({"id": company_id}, {"_id": 0, "test_tenant": 1, "test": 1, "test_autopilot": 1})
    return "test_tenant" if test_grant_active(company, agent=agent) else "off"


async def active_test_grants() -> list[dict]:
    rows = await db.raw.companies.find({"test_autopilot.enabled": True},
                                       {"_id": 0, "id": 1, "name": 1, "test_tenant": 1, "test": 1, "test_autopilot": 1}).to_list(50)
    return [{"company_id": c["id"], "name": c.get("name"), **c["test_autopilot"]} for c in rows if _grant_live(c)]


def _grant_live(company: dict) -> bool:
    """Active for at least one of the agents it names (or for every agent when it names none)."""
    agents = (company.get("test_autopilot") or {}).get("agents")
    return any(test_grant_active(company, agent=a) for a in (agents if isinstance(agents, list) else [None]))


async def set_test_grant(company_id: str, enabled: bool, minutes: int, note: str, actor: dict,
                         agents: list[str] | None = None) -> dict:
    s = await get_settings()
    unknown = sorted(set(agents or []) - grant_agent_names())
    if unknown:
        raise HTTPException(422, "Unknown agent: " + ", ".join(unknown))
    if enabled and (s.get("autopilot_enabled") or s.get("autopilot_halted")):
        raise HTTPException(409, "Autopilot is on globally or the kill switch is on; a test-tenant grant is not used")
    company = await db.raw.companies.find_one({"id": company_id}, {"_id": 0, "id": 1, "test_tenant": 1, "test": 1, "test_autopilot": 1})
    if not is_test_company(company):
        raise HTTPException(404, "Test tenant not found")
    at = datetime.now(timezone.utc)
    grant = {"enabled": enabled, "expires_at": iso_z(at + timedelta(minutes=minutes)) if enabled else None,
             "minutes": minutes if enabled else None, "agents": sorted(set(agents)) if agents else None,
             "note": note, "changed_at": iso_z(at),
             "by": {"id": actor["id"], "name": actor.get("name")}}
    await db.raw.companies.update_one({"id": company_id}, {"$set": {"test_autopilot": grant}})
    await audit(actor, "test_tenant_autopilot_changed", "company", company_id,
                {"before": company.get("test_autopilot"), "after": grant, "global_enabled": bool(s.get("autopilot_enabled"))},
                company_id=company_id)
    return {"company_id": company_id, "test_autopilot": grant,
            "effective_scope": await autopilot_scope(company_id, agent=(grant["agents"] or [None])[0]),
            "global_enabled": bool(s.get("autopilot_enabled"))}


async def revoke_test_grants(actor: dict, why: str) -> int:
    """The kill switch ends every test-tenant grant (they are not restored by resume)."""
    ids = [c["id"] for c in await db.raw.companies.find({"test_autopilot.enabled": True}, {"_id": 0, "id": 1}).to_list(None)]
    for cid in ids:
        await db.raw.companies.update_one({"id": cid}, {"$set": {"test_autopilot.enabled": False, "test_autopilot.revoked": {
            "at": now_iso(), "by": {"id": actor["id"], "name": actor.get("name")}, "why": why}}})
        await audit(actor, "test_tenant_autopilot_changed", "company", cid, {"after": {"enabled": False}, "why": why},
                    company_id=cid)
    return len(ids)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quote_within_policy(quote: dict, settings: dict) -> bool:
    if not settings.get("autopilot_enabled") or not quote.get("options"):
        return False
    if ((quote.get("request") or {}).get("cargo") or {}).get("dangerous_goods"):
        return False
    for option in quote["options"]:
        total, cost = option.get("total_cents"), option.get("cost_cents")
        if type(total) is not int or type(cost) is not int or total <= 0 or cost < 0:
            return False
        if total > settings["autopilot_quote_max_cents"] or (total - cost) / total * 100 < settings["autopilot_min_margin_pct"]:
            return False
    return True


def validate_settings(body: dict):
    if SWITCHES & body.keys() or PROTECTED & body.keys():
        raise HTTPException(400, "Use the typed confirmation to change autonomy switches")
    for name in ("guard_spend_per_day_cents", "guard_claim_limit_cents", "guard_default_credit_limit_cents",
                 "insurance_claim_limit_cents", "dunning_fixed_compensation_cents", "dunning_reminder_fee_cents"):
        if name in body and (type(body[name]) is not int or not 0 <= body[name] <= 100_000_000):
            raise HTTPException(400, "Limit must be a whole number of cents from 0 to 1 000 000 EUR")
    if "guard_confidence_floor" in body and (not isinstance(body["guard_confidence_floor"], (int, float))
                                             or isinstance(body["guard_confidence_floor"], bool)
                                             or not 0.5 <= body["guard_confidence_floor"] <= 1):
        raise HTTPException(400, "Confidence floor must be between 0.5 and 1")
    for name in ("owner_queue_sla_hours", "owner_escalation_hours"):  # the owner-queue timers read 0 as "not set"
        if name in body and (type(body[name]) is not int or not 1 <= body[name] <= 365):
            raise HTTPException(400, "Response time and reminder interval must be whole hours from 1 to 365")
    for name in ("customs_confirm_timeout_hours", "dunning_reminder1_days", "dunning_reminder2_days", "dunning_final_days", "dunning_collection_days",
                 "rate_expiry_warning_days"):
        if name in body and (type(body[name]) is not int or not 0 <= body[name] <= 365):
            raise HTTPException(400, "Hours and days must be whole numbers from 0 to 365")
    if all(k in body for k in ("dunning_reminder1_days", "dunning_reminder2_days", "dunning_final_days",
                               "dunning_collection_days")) and not (
            body["dunning_reminder1_days"] < body["dunning_reminder2_days"] < body["dunning_final_days"]
            < body["dunning_collection_days"]):
        raise HTTPException(400, "Dunning steps must come in order: reminder, second reminder, final notice, collection")
    if "owner_alert_email" in body and body["owner_alert_email"] and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", str(body["owner_alert_email"])):
        raise HTTPException(400, "Enter a valid e-mail address")
    for name in ("partner_target_margin_pct", "autopilot_min_margin_pct"):
        if name in body and (not isinstance(body[name], (int, float)) or isinstance(body[name], bool)
                             or not math.isfinite(body[name]) or not 0 <= body[name] < 100):
            raise HTTPException(400, "Margin must be at least 0 and below 100 percent")
    for name in ("partner_max_price_cents", "partner_sell_price_cents", "autopilot_quote_max_cents"):
        if name in body and (type(body[name]) is not int or not 0 < body[name] <= 100_000_000):
            raise HTTPException(400, "Price limit must be a positive whole number of cents")
    for name, low, high in (("partner_daily_send_limit", 0, 500), ("partner_per_partner_daily_limit", 0, 100), ("partner_max_rounds", 0, 5), ("partner_rfqs_per_lane", 1, 20)):
        if name in body and (type(body[name]) is not int or not low <= body[name] <= high):
            raise HTTPException(400, f"{name} must be a whole number from {low} to {high}")
    if "partner_counter_anchor_pct" in body and (not isinstance(body["partner_counter_anchor_pct"], (int, float))
            or isinstance(body["partner_counter_anchor_pct"], bool) or not 0 <= body["partner_counter_anchor_pct"] <= 30):
        raise HTTPException(400, "The first counteroffer can start 0–30 percent below the ceiling")
    if "partner_approved_lanes" in body:
        lanes = body["partner_approved_lanes"]
        if not isinstance(lanes, list) or len(lanes) > 100 or any(not isinstance(x, str) or
                not re.fullmatch(r"(sea|road|air|rail):[A-Z]{2}>[A-Z]{2}", x) for x in lanes):
            raise HTTPException(400, "Lanes use mode:ORIGIN>DESTINATION, for example road:FI>SE")
    if "seller_iban" in body and body["seller_iban"]:
        iban = re.sub(r"\s", "", body["seller_iban"]).upper()
        if not re.fullmatch(r"FI[0-9]{16}", iban) or int(iban[4:] + "1518" + iban[2:4]) % 97 != 1:
            raise HTTPException(400, "Enter a valid Finnish IBAN")
        body["seller_iban"] = iban
    if body.get("seller_bic") and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", body["seller_bic"]):
        raise HTTPException(400, "Enter a valid BIC")
    if body.get("seller_business_id"):
        from core.refs import ytunnus_valid
        if not ytunnus_valid(body["seller_business_id"]):
            raise HTTPException(400, "Enter a valid Finnish business ID")
    if body.get("seller_vat_id") and not re.fullmatch(r"FI[0-9]{8}", body["seller_vat_id"]):
        raise HTTPException(400, "Enter a Finnish VAT ID, FI followed by eight digits")
