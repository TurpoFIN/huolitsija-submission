"""Cargo insurance: quote and bind with a real insurer API (s7, see shared/interfaces.md).

    iq  = await quote_insurance(shipment_or_quote, "ICC_A", insured_value_cents, "EUR")
    pol = await bind_insurance(iq["id"], actor)

Provider: adapters/insurer.py (Redkik, the only insurer adapter). Without its API token set every quote/bind answers
`insurer_not_configured` (503, state live_ready, blocked on the insurer contract); nothing stands in for an insurer.
Insured value defaults to RULE X-1: 110 % of CIF (goods value + freight to destination).
Exclusions we always apply before asking an insurer: sanctioned countries on any leg (RULE K-2 RU/BY + comprehensive
embargoes KP/IR/SY), a customer or shipment on sanctions/compliance hold, and dangerous goods per the provider's rule
(quoted but never auto-bound with Redkik: legal point `dangerous_goods`).
Autonomy: `shipment.created` with cover ICC_* -> job `insurance.bind_on_booking` -> s2 `decide(agent="insurance",
action="bind_policy")` when present, else autopilot switch + kill switch + core.autonomy.action; not auto -> approval card.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from adapters import insurer as ins
from core.approvals import APPROVAL_KINDS, request_approval
from core.audit import actor_ref, agent_actor, audit
from core.db import db, register_index
from core.events import emit, on
from core.lifecycle import background_loop
from core.settings import get_settings
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso, span

try:  # s2 durable jobs and agent decisions; absent until feat/autopilot-ops merges
    from core.jobs import JobFailed, enqueue, job
except ImportError:  # pragma: no cover - depends on merge order
    job = enqueue = JobFailed = None
try:
    from core.agent_runtime import decide, policy_reason
except ImportError:  # pragma: no cover
    decide = policy_reason = None

log = logging.getLogger("insurance")

register_index("insurance_quotes", "id", unique=True)
register_index("insurance_quotes", [("subject.shipment_id", 1), ("created_at", -1)])
register_index("insurance_quotes", [("subject.quote_id", 1), ("created_at", -1)])
register_index("insurance_policies", "id", unique=True)
register_index("insurance_policies", [("shipment_id", 1), ("bound_at", -1)])
register_index("insurance_policies", "insurance_quote_id")
register_index("shipments", "insurance_auto.state", sparse=True)

APPROVAL_KINDS.setdefault("insurance_bind", {"label": "Cargo insurance bind", "perm": "quotes.write"})

COVERS = ins.COVERS
COVER_LABEL = {"ICC_A": "Institute Cargo Clauses (A)", "ICC_B": "Institute Cargo Clauses (B)", "ICC_C": "Institute Cargo Clauses (C)"}
# RULE K-2 (services.carriers.SANCTIONED_COUNTRIES = RU, BY) + comprehensive embargoes every cargo policy's
# sanction limitation clause excludes.
EMBARGOED = {"KP", "IR", "SY"}
SANCTION_HOLD = {"potential_match", "match", "hit", "blocked", "confirmed"}
X1_FACTOR = 1.10


def _excluded_countries() -> set[str]:
    from services.carriers import SANCTIONED_COUNTRIES
    return set(SANCTIONED_COUNTRIES) | EMBARGOED


def _reason(code: str, fi: str, en: str, **extra) -> dict:
    return {"code": code, "fi": fi, "en": en, **extra}


async def _provider(settings: dict | None = None):
    s = settings or await get_settings()
    return ins.get_provider(s.get("insurance_provider"))


async def providers() -> dict:
    s = await get_settings()
    active = ins.provider_name(s.get("insurance_provider"))
    rk = ins.Redkik()
    info = {**rk.info(), "active": active == "redkik"}
    return {"active": active or None, "state": info["state"] if active == "redkik" else "missing",
            "blocked_on": info["blocked_on"], "blocked_on_fi": info["blocked_on_fi"],
            "providers": [info],
            "documented_only": [{"provider": "loadsure", "note": ins.DOCUMENTED["loadsure"]}]}


# ------------------------------------------------------------------------------------------------- subject

def _kind(subject: dict) -> str:
    if subject.get("legs") is not None and subject.get("reference"):
        return "shipment"
    if subject.get("request") is not None:
        return "quote"
    raise HTTPException(400, "Insurance needs a quote or a shipment.")


def _option(quote: dict, option_id: str | None = None) -> dict | None:
    opts = quote.get("options") or []
    want = option_id or quote.get("accepted_option_id")
    return next((o for o in opts if o.get("id") == want), None) or (opts[0] if opts else None)


def _freight_cents(option: dict | None) -> int | None:
    if not option:
        return None
    freight = [ln["amount_cents"] for ln in option.get("lines") or [] if ln.get("stage") in ("origin", "main")]
    return sum(freight) if freight else None


def cif_insured_value(cargo_value_cents: int, freight_cents: int | None) -> int:
    """RULE X-1: 110 % of CIF (goods + freight)."""
    return int(round((int(cargo_value_cents) + int(freight_cents or 0)) * X1_FACTOR))


async def default_insured_value(subject: dict, option_id: str | None = None) -> int | None:
    kind = _kind(subject)
    if kind == "quote":
        quote, cargo = subject, (subject.get("request") or {}).get("cargo") or {}
    else:
        cargo = subject.get("cargo") or {}
        quote = await db.quotes.find_one({"id": subject.get("quote_id")}, NO_ID) if subject.get("quote_id") else None
        option_id = option_id or subject.get("option_id")
    opt = _option(quote, option_id) if quote else None
    line = next((ln for ln in (opt or {}).get("lines") or [] if ln.get("code") == "INSURANCE" and ln.get("insured_value_cents")), None)
    if line:
        return int(line["insured_value_cents"])
    if not cargo.get("value_cents"):
        return None
    freight = _freight_cents(opt)
    if freight is None and kind == "shipment":
        freight = (subject.get("price") or {}).get("total_cents")
    return cif_insured_value(cargo["value_cents"], freight)


def _loc(x: dict | None) -> dict:
    x = x or {}
    return {"unlocode": x.get("unlocode") or (x.get("code") if len(str(x.get("code") or "")) == 5 else None),
            "country": (x.get("country") or "").upper() or None, "city": x.get("city"), "name": x.get("name"),
            "label": x.get("label")}


async def _context(subject: dict, option_id: str | None = None) -> dict:
    """Everything the insurer and our exclusions need, from a quote (option) or a shipment."""
    kind = _kind(subject)
    if kind == "shipment":
        s = subject
        legs = s.get("legs") or []
        cargo = s.get("cargo") or {}
        ctx = {"kind": "shipment", "subject": {"shipment_id": s["id"], "quote_id": s.get("quote_id")},
               "company_id": s.get("company_id"), "mode": s.get("mode"), "origin": _loc(s.get("origin")),
               "destination": _loc(s.get("destination")), "etd": s.get("etd"), "eta": s.get("eta"),
               "reference": s.get("reference"), "cargo": cargo, "compliance_hold": bool(s.get("compliance_hold")),
               "legs": [{"mode": lg.get("mode"), "from": _loc(lg.get("from")), "to": _loc(lg.get("to")),
                         "carrier": lg.get("carrier"), "vessel": lg.get("vessel"), "voyage": lg.get("voyage")} for lg in legs]}
    else:
        q = subject
        req = q.get("request") or {}
        opt = _option(q, option_id) or {}
        legs = opt.get("legs") or []
        ctx = {"kind": "quote", "subject": {"quote_id": q["id"], "shipment_id": q.get("shipment_id"), "option_id": opt.get("id")},
               "company_id": q.get("company_id"), "mode": opt.get("mode") or req.get("mode"), "origin": _loc(req.get("origin")),
               "destination": _loc(req.get("destination")), "etd": opt.get("etd") or req.get("ready_date"), "eta": opt.get("eta"),
               "transit_days": opt.get("transit_days"), "reference": q.get("number"), "cargo": req.get("cargo") or {},
               "compliance_hold": False,
               "legs": [{"mode": lg.get("mode"), "from": _loc(lg.get("from")), "to": _loc(lg.get("to")),
                         "carrier": lg.get("carrier") or opt.get("carrier_name"), "vessel": None, "voyage": None} for lg in legs]}
    company = await db.companies.find_one({"id": ctx["company_id"]}, NO_ID) if ctx["company_id"] else None
    ctx["company"] = company or {}
    sea = [lg for lg in ctx["legs"] if lg["mode"] == "sea"]
    main = next((lg for lg in ctx["legs"] if lg["mode"] == ctx["mode"]), ctx["legs"][0] if ctx["legs"] else {})
    ctx["loading_port"] = sea[0]["from"]["unlocode"] if sea else None
    ctx["discharge_port"] = sea[-1]["to"]["unlocode"] if sea else None
    ctx["carrier"] = (main or {}).get("carrier")
    ctx["vessel_name"] = (main or {}).get("vessel")
    ctx["voyage"] = (main or {}).get("voyage")
    return ctx


def exclusions_for(ctx: dict, provider) -> tuple[list[dict], list[dict]]:
    """-> (refusals, refer_to_person). Refusals stop the quote; referrals allow a quote but no automatic bind."""
    refuse, refer = [], []
    bad = _excluded_countries()
    from services.pricing import _sanctioned_input
    places = [ctx["origin"], ctx["destination"]] + [p for lg in ctx["legs"] for p in (lg["from"], lg["to"])]
    hit = sorted({p["country"] for p in places if p.get("country") in bad})
    if hit or any(_sanctioned_input(p.get("label") or p.get("name") or "") for p in (ctx["origin"], ctx["destination"])):
        refuse.append(_reason("sanctioned_country", f"Pakotemaa reitillä ({', '.join(hit) or 'RU/BY'}): vakuutusta ei myönnetä.",
                              f"Sanctioned country on the route ({', '.join(hit) or 'RU/BY'}): no cover is offered.", countries=hit))
    if (ctx["company"].get("sanctions_status") or "") in SANCTION_HOLD or ctx["company"].get("credit_status") == "blocked":
        refuse.append(_reason("party_sanctions", "Asiakas on pakoteseulonnan tarkistuksessa.", "The customer is under sanctions review."))
    if ctx.get("compliance_hold"):
        refuse.append(_reason("compliance_hold", "Lähetys on vaatimustenmukaisuuspidossa.", "The shipment is on a compliance hold."))
    if ctx["cargo"].get("dangerous_goods"):
        if provider.dangerous_goods == "refuse":
            refuse.append(_reason("dangerous_goods", f"Vaarallisia aineita ei vakuuteta palvelussa {provider.label}.",
                                  f"Dangerous goods are not insured by {provider.label}."))
        else:
            refer.append(_reason("dangerous_goods", "Vaaralliset aineet: ihminen hyväksyy vakuutuksen.",
                                 "Dangerous goods: a person approves the cover.", legal_point="dangerous_goods"))
    return refuse, refer


# ------------------------------------------------------------------------------------------------- quote

def _public(doc: dict | None) -> dict | None:
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id"}


async def quote_insurance(subject: dict, cover: str, insured_value_cents: int | None = None, currency: str = "EUR", *,
                          actor: dict | None = None, option_id: str | None = None) -> dict:
    cover = (cover or "").upper()
    if cover not in COVERS:
        raise HTTPException(400, "Cover must be ICC_A, ICC_B or ICC_C.")
    currency = (currency or "EUR").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(400, "Currency must be a three-letter ISO code.")
    settings = await get_settings()
    try:
        provider = await _provider(settings)
    except ins.InsurerError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from None
    ctx = await _context(subject, option_id)
    cif = await default_insured_value(subject, option_id)
    if insured_value_cents is None:
        insured_value_cents = cif
    if not insured_value_cents or type(insured_value_cents) is not int or insured_value_cents <= 0:
        raise HTTPException(422, {"code": "value_missing", "message": "Insurance needs the cargo value (insured value = 110 % of CIF).",
                                  "message_fi": "Vakuutus tarvitsee tavaran arvon (vakuutusarvo = 110 % CIF-arvosta)."})
    refuse, refer = exclusions_for(ctx, provider)
    base = {"id": new_id(), "provider": provider.name, "provider_label": provider.label,
            "provider_mode": provider.mode, "cover": cover, "cover_label": COVER_LABEL[cover], "currency": currency,
            "insured_value_cents": insured_value_cents, "cif_insured_value_cents": cif, "subject": ctx["subject"],
            "company_id": ctx["company_id"], "created_at": now_iso(), "created_by": actor_ref(actor)}
    if refuse:
        doc = {**base, "status": "refused", "reasons": refuse, "provider_quote_id": None, "premium_cents": None,
               "exclusions": refuse, "request": None, "response": None}
        await db.insurance_quotes.insert_one(dict(doc))
        await audit(actor, "insurance_quote_refused", "insurance_quote", doc["id"], {"reasons": [r["code"] for r in refuse],
                                                                                   **ctx["subject"]}, company_id=ctx["company_id"])
        raise HTTPException(422, {"code": refuse[0]["code"], "message": refuse[0]["en"], "message_fi": refuse[0]["fi"],
                                  "reasons": refuse, "insurance_quote_id": doc["id"]})
    start, end = ins.cover_dates(ctx.get("etd"), ctx.get("eta"), ctx.get("transit_days"))
    cargo = ctx["cargo"]
    desc = cargo.get("description") or "General cargo"
    if cargo.get("dangerous_goods"):
        dg = cargo.get("dg") or {}
        desc += f" — DANGEROUS GOODS UN{dg.get('un_number') or '?'} class {dg.get('class') or '?'}"
    company = ctx["company"]
    addr = company.get("address") or {}
    req = {"subject": ctx["subject"], "cover": cover, "insured_value_cents": insured_value_cents, "currency": currency,
           "mode": ctx["mode"], "origin": ctx["origin"], "destination": ctx["destination"], "start": start, "end": end,
           "cargo": cargo, "description": desc[:500], "reference": ctx.get("reference"), "loading_port": ctx["loading_port"],
           "discharge_port": ctx["discharge_port"], "carrier": ctx["carrier"], "vessel_name": ctx["vessel_name"], "voyage": ctx["voyage"],
           "customer": {"id": company.get("id"), "name": company.get("name"), "business_id": company.get("business_id"),
                        "country": addr.get("country") or company.get("country"),
                        "address": ", ".join(str(x) for x in (addr.get("street"), addr.get("postal_code"), addr.get("city"), addr.get("country")) if x)}}
    try:
        offer = await provider.quote(req)
    except ins.InsurerError as e:
        blocked = e.code == "insurer_not_configured"
        doc = {**base, "status": "insurer_not_configured" if blocked else "failed",
               "error": {"code": e.code, "message": str(e), "detail": e.detail}, "provider_quote_id": None,
               "premium_cents": None, "exclusions": [], "request": None, "response": None}
        await db.insurance_quotes.insert_one(dict(doc))
        if blocked:
            await audit(actor, "insurance_quote_blocked", "insurance_quote", doc["id"], {"code": e.code, **e.detail, **ctx["subject"]},
                        company_id=ctx["company_id"])
        raise HTTPException(e.status, {"code": e.code, "message": str(e), **(e.detail if blocked else {}),
                                       **({"message_fi": "Vakuutusyhtiösopimusta ei vielä ole: vakuutusta ei voi tarjota eikä sitoa."} if blocked else {}),
                                       "insurance_quote_id": doc["id"]}) from None
    valid_cap = datetime.now(timezone.utc) + timedelta(days=int(settings.get("quote_validity_days") or 14))
    valid_until = offer.get("valid_until") or min(max(parse_iso(start), datetime.now(timezone.utc) + timedelta(hours=24)), valid_cap).strftime("%Y-%m-%dT%H:%M:%SZ")
    clauses = offer.get("clauses") or []
    cover_named = any(f"({cover[-1]})" in (c.get("name") or "") for c in clauses)
    exclusions = [_reason("sanctions_clause", "Pakotemaat (RU, BY, KP, IR, SY) eivät kuulu vakuutukseen.",
                          "Sanctioned countries (RU, BY, KP, IR, SY) are excluded.")]
    exclusions += [{"code": "provider_clause", "en": c.get("name"), "fi": c.get("name")} for c in clauses if c.get("name")]
    exclusions += [{"code": "provider_sanctions_source", "en": s, "fi": s} for s in offer.get("sanction_sources") or []]
    status = "quoted" if offer.get("accepted") else "referred"
    doc = {**base, "status": status, "provider_quote_id": offer["provider_quote_id"], "premium_cents": offer["premium_cents"],
           "breakdown": {k: offer.get(k) for k in ("insurer_premium_cents", "fees_cents", "taxes_cents")},
           "currency_symbol": offer.get("currency_symbol"), "deductible": offer.get("deductibles") or [],
           "clauses": clauses, "cover_named_in_clauses": cover_named,
           "amendments": offer.get("amendments") or [], "exclusions": exclusions, "requires_person": refer,
           "cover_start": start, "cover_end": end, "valid_until": valid_until, "commodity": offer.get("commodity"),
           "request": offer.get("request"), "response": offer.get("response")}
    await db.insurance_quotes.insert_one(dict(doc))
    if ctx["kind"] == "quote":
        await db.quotes.update_one({"id": ctx["subject"]["quote_id"]}, {"$set": {"insurance_quote_id": doc["id"]}})
    await audit(actor, "insurance_quoted", "insurance_quote", doc["id"],
                {"provider": provider.name, "premium_cents": doc["premium_cents"], "cover": cover,
                 "status": status, **ctx["subject"]}, company_id=ctx["company_id"])
    return _public(doc)


def reprice_option_line(option: dict, insurance_quote: dict) -> dict:
    """Post-step for pricing: replace the indicative INSURANCE line (services/pricing.py, 0.25 %) with the insurer's
    premium and move the option totals by the difference. Returns the option (changed in place)."""
    iq = insurance_quote or {}
    if iq.get("status") not in ("quoted", "bound") or not isinstance(iq.get("premium_cents"), int):
        return option
    ln = next((x for x in option.get("lines") or [] if x.get("code") == "INSURANCE"), None)
    if not ln:
        return option
    old, new = int(ln["amount_cents"]), int(iq["premium_cents"])
    delta = new - old
    vat_delta = int(round(new * (ln.get("vat_rate_pct") or 0) / 100)) - int(round(old * (ln.get("vat_rate_pct") or 0) / 100))
    ln.update(unit_cost_cents=new, unit_price_cents=new, amount_cents=new, cost_cents=new, estimated=False,
              insurance_quote_id=iq.get("id"), provider=iq.get("provider"),
              insured_value_cents=iq.get("insured_value_cents"))
    ln["note"] = f"{iq.get('provider_label')}: {iq.get('cover_label')}, insured value {iq.get('insured_value_cents', 0) / 100:,.0f} {iq.get('currency')}"
    ln["note_fi"] = f"{iq.get('provider_label')}: {iq.get('cover_label')}, vakuutusarvo {iq.get('insured_value_cents', 0) / 100:,.0f} {iq.get('currency')}".replace(",", " ")
    for k in ("total_cents", "pass_through_cents", "cost_cents"):
        if isinstance(option.get(k), int):
            option[k] += delta
    if isinstance(option.get("vat_cents"), int):
        option["vat_cents"] += vat_delta
    if isinstance(option.get("total_incl_vat_cents"), int):
        option["total_incl_vat_cents"] += delta + vat_delta
    return option


# ------------------------------------------------------------------------------------------------- bind

async def _store_certificate(provider, policy: dict, actor) -> dict | None:
    try:
        got = await provider.certificate(policy["provider_policy_id"])
    except Exception as exc:  # noqa: BLE001 - the policy stands without its PDF; retried on GET
        log.warning("certificate fetch failed for %s: %s", policy["id"], exc)
        return None
    if not got:
        return None
    data, filename = got
    from services.documents import create_document
    return await create_document(company_id=policy.get("company_id"), shipment_id=policy.get("shipment_id"), doc_type="other",
                                 filename=filename, data=data, actor=actor, source="insurer", extract=False,
                                 extra={"insurance_policy_id": policy["id"], "kind": "insurance_certificate",
                                        "title": "Insurance certificate"})


async def bind_insurance(insurance_quote_id: str, actor: dict | None = None) -> dict:
    iq = await db.insurance_quotes.find_one({"id": insurance_quote_id}, NO_ID)
    if not iq:
        raise HTTPException(404, "Insurance quote not found")
    if iq["status"] in ("bound", "binding"):
        pol = await db.insurance_policies.find_one({"insurance_quote_id": iq["id"]}, NO_ID)
        if pol:
            return _public(pol)
        raise HTTPException(409, "This insurance quote is being bound right now.")
    if iq["status"] != "quoted":
        raise HTTPException(409, {"code": f"quote_{iq['status']}", "message": f"This insurance quote is {iq['status']} and cannot be bound.",
                                  "message_fi": f"Vakuutustarjouksen tila on {iq['status']}, joten sitä ei voi sitoa.",
                                  "amendments": iq.get("amendments") or [], "reasons": iq.get("reasons") or []})
    if parse_iso(iq.get("valid_until")) and parse_iso(iq["valid_until"]) < datetime.now(timezone.utc):
        await db.insurance_quotes.update_one({"id": iq["id"]}, {"$set": {"status": "expired"}})
        raise HTTPException(409, {"code": "quote_expired", "message": "The insurance quote has expired; ask for a new one.",
                                    "message_fi": "Vakuutustarjous on vanhentunut; pyydä uusi."})
    settings = await get_settings()
    provider = await _provider(settings)
    if provider.name != iq["provider"]:
        raise HTTPException(409, {"code": "provider_changed", "message": f"The quote is from {iq['provider']} but the active insurer is {provider.name}; re-quote.",
                                    "message_fi": f"Tarjous on vakuuttajalta {iq['provider']}, mutta käytössä on {provider.name}; pyydä uusi tarjous."})
    claimed = await db.insurance_quotes.find_one_and_update({"id": iq["id"], "status": "quoted"}, {"$set": {"status": "binding"}})
    if not claimed:
        return await bind_insurance(insurance_quote_id, actor)
    try:
        res = await provider.bind(iq["provider_quote_id"])
    except ins.InsurerError as e:
        await db.insurance_quotes.update_one({"id": iq["id"]}, {"$set": {"status": "quoted", "last_bind_error": {"code": e.code, "message": str(e), "at": now_iso()}}})
        raise HTTPException(e.status, {"code": e.code, "message": str(e), "detail": e.detail}) from None
    shipment_id = iq["subject"].get("shipment_id")
    if not shipment_id and iq["subject"].get("quote_id"):
        q = await db.quotes.find_one({"id": iq["subject"]["quote_id"]}, {"_id": 0, "shipment_id": 1})
        shipment_id = (q or {}).get("shipment_id")
    shp = await db.shipments.find_one({"id": shipment_id}, {"_id": 0, "reference": 1}) if shipment_id else None
    pol = {"id": new_id(), "provider": iq["provider"], "provider_label": iq["provider_label"],
           "provider_mode": iq.get("provider_mode"), "insurance_quote_id": iq["id"], "provider_quote_id": iq["provider_quote_id"],
           "provider_policy_id": res["provider_policy_id"], "certificate_number": res.get("certificate_number"),
           "status": res["status"], "provider_status": res.get("provider_status"), "payment_url": res.get("payment_url"),
           "cover": iq["cover"], "cover_label": iq["cover_label"], "insured_value_cents": iq["insured_value_cents"],
           "premium_cents": iq["premium_cents"], "currency": iq["currency"], "deductible": iq.get("deductible") or [],
           "cover_start": iq.get("cover_start"), "cover_end": iq.get("cover_end"),
           "quote_id": iq["subject"].get("quote_id"), "shipment_id": shipment_id, "shipment_reference": (shp or {}).get("reference"),
           "company_id": iq.get("company_id"), "bound_at": now_iso() if res["status"] == "bound" else None,
           "created_at": now_iso(), "bound_by": actor_ref(actor), "cancelled_at": None, "certificate_document_id": None,
           "response": res.get("response")}
    pol["certificate_url"] = f"/api/insurance/policies/{pol['id']}/certificate"
    await db.insurance_policies.insert_one(dict(pol))
    await db.insurance_quotes.update_one({"id": iq["id"]}, {"$set": {"status": "bound", "policy_id": pol["id"]}})
    if shipment_id:
        await db.shipments.update_one({"id": shipment_id}, {"$set": {"insurance_policy_id": pol["id"], "insurance.policy_id": pol["id"],
                                                                     "insurance.provider": pol["provider"],
                                                                     "insurance.certificate_number": pol["certificate_number"]}})
    if pol["quote_id"]:
        await db.quotes.update_one({"id": pol["quote_id"]}, {"$set": {"insurance_quote_id": iq["id"]}})
    doc = await _store_certificate(provider, pol, actor)
    if doc:
        pol["certificate_document_id"] = doc["id"]
        await db.insurance_policies.update_one({"id": pol["id"]}, {"$set": {"certificate_document_id": doc["id"]}})
    await audit(actor, "insurance_bound", "insurance_policy", pol["id"],
                {"provider": pol["provider"], "certificate_number": pol["certificate_number"],
                 "premium_cents": pol["premium_cents"], "shipment_id": shipment_id, "status": pol["status"]}, company_id=pol["company_id"])
    await emit("insurance.bound", {"policy": _public(pol), "actor": actor})
    return _public(pol)


async def cancel_policy(policy_id: str, actor: dict | None = None, reason: str = "") -> dict:
    pol = await db.insurance_policies.find_one({"id": policy_id}, NO_ID)
    if not pol:
        raise HTTPException(404, "Policy not found")
    if pol["status"] == "cancelled":
        return _public(pol)
    provider = await _provider()
    if provider.name != pol["provider"]:
        raise HTTPException(409, f"The policy is with {pol['provider']}; the active insurer is {provider.name}.")
    try:
        res = await provider.cancel(pol["provider_policy_id"])
    except ins.InsurerError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e), "detail": e.detail}) from None
    changes = {"status": "cancelled", "cancelled_at": now_iso(), "cancel_reason": reason, "cancel_response": res.get("response"),
               "cancelled_by": actor_ref(actor)}
    await db.insurance_policies.update_one({"id": policy_id}, {"$set": changes})
    if pol.get("shipment_id"):
        await db.shipments.update_one({"id": pol["shipment_id"], "insurance_policy_id": policy_id},
                                      {"$set": {"insurance.policy_status": "cancelled"}})
    await audit(actor, "insurance_cancelled", "insurance_policy", policy_id, {"reason": reason}, company_id=pol.get("company_id"))
    return _public({**pol, **changes})


async def certificate(policy_id: str) -> tuple[bytes, str, str]:
    """PDF of the certificate (from documents; fetched again from the insurer when the first fetch failed)."""
    from core.storage import get_file
    pol = await db.insurance_policies.find_one({"id": policy_id}, NO_ID)
    if not pol:
        raise HTTPException(404, "Policy not found")
    if not pol.get("certificate_document_id"):
        provider = await _provider()
        doc = await _store_certificate(provider, pol, None) if provider.name == pol["provider"] else None
        if not doc:
            raise HTTPException(404, "The insurer has not issued a certificate document for this policy.")
        await db.insurance_policies.update_one({"id": policy_id}, {"$set": {"certificate_document_id": doc["id"]}})
        pol["certificate_document_id"] = doc["id"]
    d = await db.documents.find_one({"id": pol["certificate_document_id"]}, NO_ID)
    data, filename, ctype = await get_file(d["file_id"])
    return data, filename, ctype


# ------------------------------------------------------------------------------------------------- autonomy

async def _halted() -> bool:
    doc = await db.settings.find_one({"key": "app"}, {"_id": 0, "autopilot_halted": 1}) or {}
    return bool(doc.get("autopilot_halted"))


async def _set_auto(shipment_id: str, state: str, **extra):
    await db.shipments.update_one({"id": shipment_id}, {"$set": {"insurance_auto": {"state": state, "at": now_iso(), **extra}}})


async def _card(s: dict, iq: dict | None, reasons: list[dict]) -> dict:
    prem = (iq or {}).get("premium_cents")
    title = f"Bind cargo insurance {s.get('insurance', {}).get('cover', '')} for {s['reference']}"
    title_fi = f"Kuljetusvakuutus {s.get('insurance', {}).get('cover', '')} lähetykselle {s['reference']}"
    return await request_approval(
        "insurance_bind", "shipment", s["id"], title,
        summary=(f"{iq['provider_label']}: premium {prem / 100:,.2f} {iq['currency']}, insured value "
                 f"{iq['insured_value_cents'] / 100:,.0f} {iq['currency']}" if iq and prem is not None else "No insurer quote yet"),
        reason="; ".join(r["en"] for r in reasons), company_id=s.get("company_id"), requested_by=agent_actor("insurance"),
        data={"shipment_id": s["id"], "insurance_quote_id": (iq or {}).get("id"), "title_fi": title_fi,
              "reason_fi": "; ".join(r["fi"] for r in reasons), "reasons": reasons},
        amount_cents=prem, link=f"/ops/shipments/{s['id']}")


async def bind_on_booking(shipment_id: str) -> dict:
    """Agent step: the accepted quote asked for cover -> quote (or reuse the quote's insurer quote) and bind, or ask a person."""
    s = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    cover = ((s or {}).get("insurance") or {}).get("cover")
    if not s or cover not in COVERS:
        return {"outcome": "skipped", "reason": "no cover requested"}
    if s.get("insurance_policy_id"):
        return {"outcome": "already_bound", "policy_id": s["insurance_policy_id"]}
    agent = agent_actor("insurance")
    iq = None
    if s.get("quote_id"):
        q = await db.quotes.find_one({"id": s["quote_id"]}, {"_id": 0, "insurance_quote_id": 1})
        cand = await db.insurance_quotes.find_one({"id": (q or {}).get("insurance_quote_id")}, NO_ID) if (q or {}).get("insurance_quote_id") else None
        if cand and cand["status"] == "quoted" and cand["cover"] == cover and (parse_iso(cand.get("valid_until")) or datetime.max.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc):
            iq = cand
    reasons: list[dict] = []
    if not iq:
        try:
            iq = await quote_insurance(s, cover, None, (s.get("price") or {}).get("currency") or "EUR", actor=agent)
        except HTTPException as e:
            det = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
            if det.get("code") in ("sanctioned_country", "party_sanctions", "compliance_hold", "dangerous_goods"):
                await _set_auto(shipment_id, "refused", reasons=det.get("reasons") or [])
                await audit(agent, "insurance_bind_refused", "shipment", shipment_id, det, company_id=s.get("company_id"))
                card = await _card(s, None, det.get("reasons") or [_reason(det["code"], det.get("message_fi") or det["message"], det["message"])])
                return {"outcome": "refused", "approval_id": card["id"], "reasons": det.get("reasons")}
            if det.get("code") == "insurer_not_configured":
                # Explicit state, not a failure: no insurer contract yet. The shipment travels uninsured and the owner
                # queue says so; approving the card after the contract exists quotes and binds for real.
                reasons.append(_reason("insurer_not_configured", "Vakuutusta ei sidottu: " + ins.BLOCKED_ON_FI,
                                       "Cover not bound: " + ins.BLOCKED_ON))
                card = await _card(s, None, reasons)
                await _set_auto(shipment_id, "insurer_not_configured", approval_id=card["id"], reasons=reasons,
                                state_detail="live_ready", blocked_on=ins.BLOCKED_ON, blocked_on_fi=ins.BLOCKED_ON_FI)
                await audit(agent, "insurance_bind_blocked", "shipment", shipment_id, {"code": "insurer_not_configured",
                            "blocked_on": ins.BLOCKED_ON}, company_id=s.get("company_id"))
                return {"outcome": "insurer_not_configured", "approval_id": card["id"], "reasons": reasons}
            reasons.append(_reason(det.get("code") or "insurer_unavailable", f"Vakuutustarjousta ei saatu: {det.get('message')}",
                                   f"No insurer quote: {det.get('message')}"))
            card = await _card(s, None, reasons)
            await _set_auto(shipment_id, "owner_queue", approval_id=card["id"], reasons=reasons)
            return {"outcome": "owner_queue", "approval_id": card["id"], "reasons": reasons}
    reasons += iq.get("requires_person") or []
    if iq["status"] != "quoted":
        reasons.append(_reason("insurer_referred", "Vakuuttaja vaatii muutoksia: " + ", ".join(iq.get("amendments") or []),
                               "The insurer asks for amendments: " + ", ".join(iq.get("amendments") or [])))
    settings = await get_settings()
    halted = await _halted()
    legal = next((r.get("legal_point") for r in reasons if r.get("legal_point")), None)
    if decide is not None:
        d = await decide("insurance", "bind_policy", subject=("shipment", s["id"]), subject_label=s["reference"],
                         inputs={"insurance_quote_id": iq["id"], "cover": cover, "provider": iq["provider"]},
                         decision={"bind": True, "premium_cents": iq["premium_cents"]}, confidence=None,
                         spend_cents=iq["premium_cents"], company_id=s.get("company_id"), legal_point=legal,
                         extra_reasons=[policy_reason(r["code"], r["fi"], r["en"]) for r in reasons if not r.get("legal_point")],
                         switch="autopilot_enabled", approval_kind="insurance_bind",
                         title=f"Bind cargo insurance for {s['reference']}",
                         title_fi=f"Kuljetusvakuutus lähetykselle {s['reference']}",
                         summary=f"{iq['provider_label']} {cover}: {iq['premium_cents'] / 100:,.2f} {iq['currency']}",
                         summary_fi=f"{iq.get('provider_label_fi') or iq['provider_label']} {cover}: {iq['premium_cents'] / 100:,.2f} {iq['currency']}",
                         link=f"/ops/shipments/{s['id']}", amount_cents=iq["premium_cents"],
                         approval_data={"shipment_id": s["id"], "insurance_quote_id": iq["id"]})
        auto, card_id = d.auto, (d.approval or {}).get("id")
    else:
        if not settings.get("autopilot_enabled"):
            reasons.append(_reason("autopilot_off", "Autopilotti on pois päältä.", "Autopilot is off."))
        if halted:
            reasons.append(_reason("autopilot_halted", "Autopilotti on pysäytetty (hätäpysäytys).", "Autopilot is halted (kill switch)."))
        auto, card_id = not reasons, None
        if not auto:
            card_id = (await _card(s, iq, reasons))["id"]
    if not auto:
        await _set_auto(shipment_id, "owner_queue", approval_id=card_id, insurance_quote_id=iq["id"], reasons=reasons)
        return {"outcome": "owner_queue", "approval_id": card_id, "insurance_quote_id": iq["id"], "reasons": reasons}
    try:
        pol = await bind_insurance(iq["id"], agent)
    except HTTPException as e:
        det = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        reasons = [_reason(det.get("code") or "bind_failed", f"Sitominen epäonnistui: {det.get('message')}", f"Bind failed: {det.get('message')}")]
        card = await _card(s, iq, reasons)
        await _set_auto(shipment_id, "owner_queue", approval_id=card["id"], insurance_quote_id=iq["id"], reasons=reasons)
        return {"outcome": "owner_queue", "approval_id": card["id"], "reasons": reasons}
    if decide is None:
        from core.autonomy import action
        await action("insurance_bound", "shipment", s["id"], company_id=s.get("company_id"),
                     data={"policy_id": pol["id"], "insurance_quote_id": iq["id"], "provider": pol["provider"],
                           "premium_cents": pol["premium_cents"]})
    await _set_auto(shipment_id, "bound", policy_id=pol["id"], insurance_quote_id=iq["id"])
    return {"outcome": "auto", "policy_id": pol["id"], "insurance_quote_id": iq["id"]}


async def _claim_and_run(shipment_id: str) -> dict | None:
    lease = iso(now() + span(300))
    got = await db.shipments.find_one_and_update(
        {"id": shipment_id, "insurance_policy_id": {"$in": [None]},
         "$or": [{"insurance_auto.state": "queued"}, {"insurance_auto.state": "running", "insurance_auto.lease_until": {"$lt": now_iso()}}]},
        {"$set": {"insurance_auto.state": "running", "insurance_auto.lease_until": lease}})
    if not got:
        return None
    try:
        return await bind_on_booking(shipment_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the shipment; the sweep retries after the lease
        log.exception("insurance.bind_on_booking %s", shipment_id)
        await db.shipments.update_one({"id": shipment_id}, {"$set": {"insurance_auto.error": str(exc)[:300]}})
        raise


if job is not None:
    @job("insurance.bind_on_booking", max_attempts=5, backoff=(60, 300, 1800, 3600, 7200), timeout=180, agent=True,
         label_fi="Kuljetusvakuutuksen sitominen", label_en="Bind cargo insurance")
    async def _bind_job(j: dict) -> dict | None:
        return await _claim_and_run(j["payload"]["shipment_id"])


@on("shipment.created")
async def _on_shipment_created(p: dict):
    s = p.get("shipment") or {}
    if ((s.get("insurance") or {}).get("cover") or "NONE") not in COVERS:
        return
    await _set_auto(s["id"], "queued")
    if enqueue is not None:
        await enqueue("insurance.bind_on_booking", {"shipment_id": s["id"]}, key=f"insurance.bind:{s['id']}",
                      company_id=s.get("company_id"), subject=("shipment", s["id"]), source="event")
    else:
        asyncio.create_task(_safe_run(s["id"]))


async def _safe_run(shipment_id: str):
    try:
        await _claim_and_run(shipment_id)
    except Exception:  # noqa: BLE001
        pass


@background_loop(30)
async def _sweep():
    """Durability without s2's jobs: picks up queued shipments (e.g. after a restart) and expired leases."""
    q = {"insurance_policy_id": {"$in": [None]},
         "$or": [{"insurance_auto.state": "queued", "insurance_auto.at": {"$lt": (datetime.now(timezone.utc) - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")}},
                 {"insurance_auto.state": "running", "insurance_auto.lease_until": {"$lt": now_iso()}}]}
    async for s in db.shipments.find(q, {"_id": 0, "id": 1}).limit(20):
        await _safe_run(s["id"])


@on("approval.decided")
async def _on_approval(p: dict):
    a = p.get("approval") or {}
    if a.get("kind") != "insurance_bind" or a.get("status") != "approved":
        return
    data = a.get("data") or {}
    actor = p.get("actor")
    iq_id = data.get("insurance_quote_id")
    sid = data.get("shipment_id") or a.get("ref_id")
    if not iq_id:
        s = await db.shipments.find_one({"id": sid}, NO_ID)
        if not s:
            return
        iq = await quote_insurance(s, (s.get("insurance") or {}).get("cover") or "ICC_A", None,
                                   (s.get("price") or {}).get("currency") or "EUR", actor=actor)
        iq_id = iq["id"]
    pol = await bind_insurance(iq_id, actor)
    await _set_auto(sid, "bound", policy_id=pol["id"], insurance_quote_id=iq_id, approval_id=a.get("id"))
