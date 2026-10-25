"""Overseas agent network: agents as carrier organisations (`org_type: "agent"`), agent agreements, agent rate cards and
origin/destination agent selection per country.

Only an agent with a signed, in-date agreement and a valid rate card is ever selected. Signing is recorded by a person
(who signed, when, which document); there is no e-signature integration here.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from fastapi import HTTPException

from core.audit import audit
from core.db import db, register_index
from core.test_scope import owner_fields, scope_query
from core.util import NO_ID, new_id, now, now_iso

log = logging.getLogger("agents")

register_index("agent_agreements", "id", unique=True)
register_index("agent_agreements", [("agent_id", 1), ("status", 1)])
register_index("carriers", "org_type")

ROLES = ("origin", "destination")
SERVICES = ("pickup", "export_customs", "import_customs", "delivery", "warehousing", "consolidation")
MODES = ("road", "sea", "air", "rail")
BASES = ("PER_SHIPMENT", "PER_KG", "PER_CBM", "PER_CONTAINER")
CARD_SERVICE = {"origin": "AGENT_ORIGIN", "destination": "AGENT_DESTINATION"}
AGREEMENT_FLOW = {"draft": {"sent", "signed", "terminated"}, "sent": {"signed", "terminated", "draft"},
                  "signed": {"terminated"}, "terminated": set(), "expired": set()}
_cache: dict[tuple, tuple[float, dict | None]] = {}
CACHE_SECONDS = 15


def _countries(values) -> list[str]:
    out = sorted({str(v).strip().upper() for v in values or [] if str(v).strip()})
    if not out or any(len(c) != 2 or not c.isalpha() for c in out):
        raise HTTPException(400, "countries must be ISO 3166 two-letter codes, e.g. [\"DE\", \"PL\"]")
    return out


def _agent_block(body: dict, base: dict | None = None) -> dict:
    base = base or {}
    roles = body.get("roles", base.get("roles") or list(ROLES))
    services = body.get("services", base.get("services") or [])
    modes = body.get("modes", base.get("modes") or [])
    if not roles or any(r not in ROLES for r in roles):
        raise HTTPException(400, "roles must be origin and/or destination")
    if any(s not in SERVICES for s in services):
        raise HTTPException(400, "services must be from " + ", ".join(SERVICES))
    if any(m not in MODES for m in modes):
        raise HTTPException(400, "modes must be road, sea, air or rail")
    return {"countries": _countries(body.get("countries", base.get("countries"))), "roles": sorted(set(roles)),
            "services": sorted(set(services)), "modes": sorted(set(modes))}


# ------------------------------------------------------------------------------------------------- agents
async def create_agent(body: dict, actor: dict) -> dict:
    name = (body.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(400, "Enter the agent's company name")
    country = (body.get("country") or "").strip().upper()
    if len(country) != 2:
        raise HTTPException(400, "country must be the ISO code of the agent's home country")
    block = _agent_block({**body, "countries": body.get("countries") or [country]})
    owner = await owner_fields(body)
    row = {"id": new_id(), "name": name, "code": None, "org_type": "agent", "modes": block["modes"] or list(MODES),
           "country": country, "contact_email": (body.get("contact_email") or "").strip().lower() or None,
           "website": body.get("website"), "business_id": body.get("business_id"), "vat_id": body.get("vat_id"),
           "agent": block, "integration": {"type": "email", "status": "configured"}, "on_time_pct": None,
           "co2_factor_g_tkm": None, "dg_classes_accepted": [], "dg_un_blocked": [], "dg_note": None,
           "services_note": body.get("services_note"), "source": body.get("source") or "operator",
           "sources": body.get("sources") or [], "active": True, **owner, "created_at": now_iso(), "updated_at": now_iso()}
    await db.carriers.insert_one(dict(row))
    await audit(actor, "agent.created", "carrier", row["id"], {"name": name, "countries": block["countries"],
                                                              "company_id": owner.get("company_id")})
    _cache.clear()
    return row


async def update_agent(agent_id: str, body: dict, actor: dict) -> dict:
    agent = await db.carriers.find_one({"id": agent_id, "org_type": "agent"}, NO_ID)
    if not agent:
        raise HTTPException(404, "Agent not found")
    sets = {"agent": _agent_block(body, agent.get("agent"))}
    for k in ("name", "contact_email", "website", "business_id", "vat_id", "services_note", "active"):
        if k in body:
            sets[k] = body[k]
    if sets["agent"]["modes"]:
        sets["modes"] = sets["agent"]["modes"]
    await db.carriers.update_one({"id": agent_id}, {"$set": {**sets, "updated_at": now_iso()}})
    await audit(actor, "agent.updated", "carrier", agent_id, {"changes": list(sets)})
    _cache.clear()
    return await get_agent(agent_id)


async def agent_from_candidate(candidate_id: str, body: dict, actor: dict) -> dict:
    """Promote a Sourcing Autopilot candidate (public-register company with a source URL) to an agent."""
    cand = await db.partner_candidates.find_one({"id": candidate_id}, NO_ID)
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("carrier_id") and await db.carriers.find_one({"id": cand["carrier_id"]}):
        await db.carriers.update_one({"id": cand["carrier_id"]}, {"$set": {"org_type": "agent", "agent": _agent_block(
            {**body, "countries": body.get("countries") or [cand.get("country") or "FI"]}), "updated_at": now_iso()}})
        _cache.clear()
        return await get_agent(cand["carrier_id"])
    row = await create_agent({**body, "name": cand["name"], "country": cand.get("country") or "FI",
                              "countries": body.get("countries") or [cand.get("country") or "FI"],
                              "contact_email": cand.get("contact_email"), "website": cand.get("website"),
                              "business_id": cand.get("business_id"), "source": "partner_network",
                              "sources": cand.get("sources") or []}, actor)
    await db.partner_candidates.update_one({"id": candidate_id}, {"$set": {"carrier_id": row["id"], "status": "partner",
                                                                          "updated_at": now_iso()}})
    return await get_agent(row["id"])


async def get_agent(agent_id: str) -> dict | None:
    agent = await db.carriers.find_one({"id": agent_id, "org_type": "agent"}, NO_ID)
    if not agent:
        return None
    agreements = await db.agent_agreements.find({"agent_id": agent_id}, NO_ID).sort("created_at", -1).to_list(50)
    cards = await db.rate_cards.find({"carrier_id": agent_id, "service": {"$in": list(CARD_SERVICE.values())}, "archived": {"$ne": True}},
                                     NO_ID).sort("valid_to", -1).to_list(100)
    today = now().date()
    return {**agent, "agreements": [_with_state(a, today) for a in agreements], "rate_cards": cards,
            "active_agreement": next((a["id"] for a in agreements if _agreement_live(a, today)), None)}


async def list_agents(country: str | None = None) -> list[dict]:
    q = {"org_type": "agent"}
    if country:
        q["agent.countries"] = country.upper()
    out = []
    async for a in db.carriers.find(q, NO_ID).sort("name", 1):
        out.append(await get_agent(a["id"]))
    return out


# ------------------------------------------------------------------------------------------------- agreements
def _agreement_live(a: dict, day: date) -> bool:
    return a.get("status") == "signed" and a["valid_from"] <= day.isoformat() <= a["valid_to"]


def _with_state(a: dict, day: date) -> dict:
    state = "expired" if a.get("status") == "signed" and a["valid_to"] < day.isoformat() else a.get("status")
    return {**a, "state": state, "live": _agreement_live(a, day)}


async def create_agreement(agent_id: str, body: dict, actor: dict) -> dict:
    agent = await db.carriers.find_one({"id": agent_id, "org_type": "agent"}, NO_ID)
    if not agent:
        raise HTTPException(404, "Agent not found")
    owner = await owner_fields(body, agent)
    try:
        vf, vt = date.fromisoformat(str(body.get("valid_from"))), date.fromisoformat(str(body.get("valid_to")))
    except ValueError:
        raise HTTPException(400, "valid_from and valid_to must be dates YYYY-MM-DD")
    if vt < vf:
        raise HTTPException(400, "valid_to is before valid_from")
    terms = body.get("terms") or {}
    pd, ps = terms.get("payment_days", 30), terms.get("profit_share_pct", 0)
    if type(pd) is not int or not 0 <= pd <= 120:
        raise HTTPException(400, "payment_days must be 0-120")
    if not isinstance(ps, (int, float)) or not 0 <= ps <= 50:
        raise HTTPException(400, "profit_share_pct must be 0-50")
    row = {"id": new_id(), "agent_id": agent_id, "status": "draft", "valid_from": vf.isoformat(), "valid_to": vt.isoformat(),
           "terms": {"payment_days": pd, "profit_share_pct": float(ps), "liability": (terms.get("liability") or "FIATA / NSAB 2015").strip(),
                     "exclusivity_countries": [c.upper() for c in terms.get("exclusivity_countries") or []]},
           "document_id": body.get("document_id"), "signed_by": None, "signed_at": None, "history": [
               {"status": "draft", "at": now_iso(), "by": actor.get("name")}], **owner, "created_at": now_iso(), "updated_at": now_iso()}
    await db.agent_agreements.insert_one(dict(row))
    await audit(actor, "agent_agreement.created", "agent_agreement", row["id"], {"agent_id": agent_id, "valid_to": row["valid_to"],
                                                                                "company_id": owner.get("company_id")})
    return row


async def set_agreement_status(agreement_id: str, status: str, body: dict, actor: dict) -> dict:
    a = await db.agent_agreements.find_one({"id": agreement_id}, NO_ID)
    if not a:
        raise HTTPException(404, "Agreement not found")
    if status not in AGREEMENT_FLOW.get(a["status"], set()):
        raise HTTPException(409, f"An agreement in status {a['status']} cannot become {status}")
    sets = {"status": status, "updated_at": now_iso()}
    if status == "signed":
        signed_by = (body.get("signed_by") or "").strip()
        if len(signed_by) < 3:
            raise HTTPException(400, "Record who signed for the agent (name and title)")
        try:
            signed_at = date.fromisoformat(str(body.get("signed_at") or now().date().isoformat())).isoformat()
        except ValueError:
            raise HTTPException(400, "signed_at must be a date")
        sets.update(signed_by=signed_by, signed_at=signed_at, document_id=body.get("document_id") or a.get("document_id"),
                    countersigned_by={"id": actor.get("id"), "name": actor.get("name")})
    await db.agent_agreements.update_one({"id": agreement_id}, {"$set": sets, "$push": {"history": {
        "status": status, "at": now_iso(), "by": actor.get("name")}}})
    await audit(actor, f"agent_agreement.{status}", "agent_agreement", agreement_id, {"before": a["status"], "after": status})
    _cache.clear()
    return _with_state(await db.agent_agreements.find_one({"id": agreement_id}, NO_ID), now().date())


# ------------------------------------------------------------------------------------------------- rate cards
async def create_rate_card(agent_id: str, body: dict, actor: dict) -> dict:
    agent = await db.carriers.find_one({"id": agent_id, "org_type": "agent"}, NO_ID)
    if not agent:
        raise HTTPException(404, "Agent not found")
    role, mode = body.get("role"), body.get("mode")
    country = (body.get("country") or "").upper()
    if role not in ROLES or mode not in MODES:
        raise HTTPException(400, "role must be origin|destination and mode road|sea|air|rail")
    if country not in (agent.get("agent") or {}).get("countries", []):
        raise HTTPException(400, "The agent does not cover this country")
    owner = await owner_fields(body, agent)
    charges = []
    for ch in body.get("charges") or []:
        code = "".join(x for x in str(ch.get("code") or "").upper() if x.isalnum() or x == "_")[:24]
        amt, mn = ch.get("amount_cents"), ch.get("min_cents")
        if not code or ch.get("basis") not in BASES or type(amt) is not int or amt < 0 or (mn is not None and (type(mn) is not int or mn < 0)):
            raise HTTPException(400, "Each charge needs code, basis (PER_SHIPMENT|PER_KG|PER_CBM|PER_CONTAINER) and amount_cents")
        label = (ch.get("label") or code).strip()
        charges.append({"code": code, "label": label, "label_fi": (ch.get("label_fi") or label).strip(), "basis": ch["basis"],
                        "amount_cents": amt, "min_cents": mn})
    if not charges:
        raise HTTPException(400, "Add at least one charge")
    try:
        vf, vt = date.fromisoformat(str(body.get("valid_from"))), date.fromisoformat(str(body.get("valid_to")))
    except ValueError:
        raise HTTPException(400, "valid_from and valid_to must be dates")
    ep = {"country": country}
    label = {"origin": "lähtöpään agentti / origin agent", "destination": "määränpään agentti / destination agent"}[role]
    card = {"id": new_id(), "carrier_id": agent_id, "mode": mode, "service": CARD_SERVICE[role], "basis": "PER_SHIPMENT",
            "service_name": f"{agent['name']} · {label} {country}", "origin": ep if role == "origin" else {"country": "*"},
            "destination": ep if role == "destination" else {"country": "*"}, "via": [], "agent_role": role, "agent_country": country,
            "charges": charges, "unit_cost_cents": sum(c["amount_cents"] for c in charges if c["basis"] == "PER_SHIPMENT"),
            "min_cost_cents": None, "container_costs": None, "tiers": None, "distance_bands": None, "currency": "EUR",
            "transit_days": None, "valid_from": vf.isoformat(), "valid_to": vt.isoformat(), "status": "accepted",
            "source": body.get("source") or "agent_agreement", "surcharges": [], "archived": False,
            "notes": body.get("notes"), **owner, "created_at": now_iso(), "updated_at": now_iso(),
            "updated_by": {"id": actor.get("id"), "name": actor.get("name")}}
    await db.rate_cards.insert_one(dict(card))
    await audit(actor, "agent_rate.created", "rate_card", card["id"], {"agent_id": agent_id, "role": role, "country": country})
    _cache.clear()
    card.pop("_id", None)
    return card


def charge_lines(card: dict, m: dict) -> list[dict]:
    lines = []
    containers = sum(int(c.get("count") or 0) for c in m.get("containers") or [])
    for ch in card.get("charges") or []:
        qty = {"PER_SHIPMENT": 1, "PER_KG": m.get("weight_kg") or 0, "PER_CBM": m.get("volume_m3") or 0,
               "PER_CONTAINER": containers}[ch["basis"]]
        if not qty:
            continue
        unit = ch["amount_cents"]
        if ch.get("min_cents") and unit * qty < ch["min_cents"]:
            qty, unit, basis = 1, ch["min_cents"], "MINIMUM"
        else:
            basis = ch["basis"]
        stage = card["agent_role"]
        lines.append({"code": f"AGENT_{ch['code']}", "description": ch["label"], "description_fi": ch.get("label_fi") or ch["label"],
                      "stage": stage, "basis": basis, "qty": round(float(qty), 3), "unit_cost_cents": int(round(unit)),
                      "is_pass_through": False, "estimated": False, "leg_seq": None, "source": "agent_rate",
                      "note": f"{card.get('service_name')}", "rate_card_id": card["id"]})
    return lines


# ------------------------------------------------------------------------------------------------- selection
async def select_agent(country: str, role: str, mode: str, *, on_date: date | None = None,
                       company_id: str | None = None) -> dict | None:
    """Best agent for production, or for `company_id` when it is a test tenant with its own TEST agents."""
    from services import scorecards

    day = on_date or now().date()
    scope = scope_query(company_id)
    key = (country.upper(), role, mode, day.isoformat(), company_id)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    best = None
    async for agent in db.carriers.find({"org_type": "agent", "active": {"$ne": False}, "agent.countries": country.upper(),
                                         "agent.roles": role, **scope}, NO_ID):
        modes = (agent.get("agent") or {}).get("modes") or []
        if modes and mode not in modes:
            continue
        agreements = await db.agent_agreements.find({"agent_id": agent["id"], "status": "signed", **scope}, NO_ID).to_list(20)
        live = next((a for a in agreements if _agreement_live(a, day)), None)
        if not live:
            continue
        cards = [c async for c in db.rate_cards.find({"carrier_id": agent["id"], "service": CARD_SERVICE[role], "mode": mode,
                                                       "agent_country": country.upper(), "archived": {"$ne": True},
                                                       "status": "accepted", **scope}, NO_ID)]
        cards = [c for c in cards if c["valid_from"] <= day.isoformat() <= c["valid_to"]]
        if not cards:
            continue
        card = min(cards, key=lambda c: c.get("unit_cost_cents") or 0)
        sc = await scorecards.get(agent["id"])
        score = sc["score"] if sc and sc["score"] is not None else None
        reasons = [{"code": "agreement", "fi": f"Voimassa oleva agenttisopimus {live['valid_to']} asti",
                    "en": f"Signed agreement valid to {live['valid_to']}"},
                   {"code": "rate", "fi": f"Agenttihinnasto {card['valid_to']} asti", "en": f"Agent rate card valid to {card['valid_to']}"}]
        if score is not None:
            reasons.append({"code": "scorecard", "fi": f"Tuloskortti {score:.0f}/100", "en": f"Scorecard {score:.0f}/100"})
        cand = {"agent": {k: agent.get(k) for k in ("id", "name", "country", "contact_email", "agent")}, "agreement": live,
                "rate_card": card, "score": score, "reasons": reasons}
        rank_key = (-(score if score is not None else 75.0), card.get("unit_cost_cents") or 0, agent["name"])
        if best is None or rank_key < best[0]:
            best = (rank_key, cand)
    result = best[1] if best else None
    _cache[key] = (time.monotonic(), result)
    return result


async def agent_charges(country: str, role: str, mode: str, m: dict, *, on_date: date | None = None,
                        company_id: str | None = None) -> dict | None:
    sel = await select_agent(country, role, mode, on_date=on_date, company_id=company_id)
    if not sel:
        return None
    lines = charge_lines(sel["rate_card"], m)
    return {"agent_id": sel["agent"]["id"], "agent_name": sel["agent"]["name"], "role": role, "country": country.upper(),
            "rate_card_id": sel["rate_card"]["id"], "agreement_id": sel["agreement"]["id"], "lines": lines} if lines else None
