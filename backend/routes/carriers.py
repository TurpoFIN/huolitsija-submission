"""Carriers, rate cards and carrier ranking for shipment legs (V2).

GET/POST/PATCH /api/carriers, GET /api/carriers/{id}, GET /api/carriers/rank?shipment_id=&leg_seq=
GET/POST/PATCH/DELETE /api/rates (filters: mode, carrier_id, q (lane), state valid|expiring|expired|upcoming)
"""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

import services.seed_rates  # noqa: F401  (registers the order-20 seed and indexes)
from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.util import NO_ID, new_id, now, now_iso
from services import carriers as net
from services import party_screening
from services import pricing

router = APIRouter(prefix="/api", tags=["carriers"])

MODES = ("sea", "road", "air", "rail")
SERVICES = ("FCL", "LCL", "AIR", "ROAD_LTL", "ROAD_FTL", "RORO", "HAULAGE", "RAIL_FCL", "RAIL_WAGON")
BASES = ("PER_CONTAINER", "PER_WM", "PER_KG", "PER_LDM", "PER_SHIPMENT", "PER_KM")
SURCHARGE_BASES = ("PER_CONTAINER", "PER_WM", "PER_KG", "PER_LDM", "PER_SHIPMENT", "PER_BL", "PER_TRUCK", "PERCENT")
INTEGRATION_TYPES = ("dcsa", "maersk", "dhl", "partner_portal", "manual", "digitraffic")
CARRIER_FIELDS = ("name", "code", "modes", "country", "contact_email", "website", "on_time_pct", "co2_factor_g_tkm",
                  "dg_classes_accepted", "dg_un_blocked", "dg_note", "services_note", "active",
                  "claims_email", "recourse_contract")  # s2 carrier recourse: claim letters go out only with both


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def rate_state(card: dict, today: date, expiring_days: int = 21) -> tuple[str, int | None]:
    vf = date.fromisoformat(card["valid_from"][:10]) if card.get("valid_from") else None
    vt = date.fromisoformat(card["valid_to"][:10]) if card.get("valid_to") else None
    if vt and vt < today:
        return "expired", (vt - today).days
    if vf and vf > today:
        return "upcoming", (vt - today).days if vt else None
    if vt and (vt - today).days <= expiring_days:
        return "expiring", (vt - today).days
    return "valid", (vt - today).days if vt else None


def _ep_label(e: dict) -> str:
    if e.get("unlocode"):
        loc = net.network_location(e["unlocode"])
        return f"{(loc or {}).get('name') or e['unlocode']} ({e['unlocode']})"
    if e.get("countries"):
        return ", ".join(e["countries"])
    return e.get("country") or "—"


def enrich_rate(card: dict, carriers: dict, today: date, expiring_days: int = 21) -> dict:
    st, days = rate_state(card, today, expiring_days)
    c = carriers.get(card.get("carrier_id")) or {}
    out = dict(card)
    out.update(carrier_name=c.get("name"), carrier_code=c.get("code"), state=st, days_left=days,
               lane=f"{_ep_label(card['origin'])} → {_ep_label(card['destination'])}")
    for side in ("origin", "destination"):
        e = dict(card[side])
        if e.get("unlocode"):
            loc = net.network_location(e["unlocode"])
            if loc:
                e.setdefault("name", loc["name"])
                e.setdefault("country", loc["country"])
        out[side] = e
    return out


def _int_cents(v, field: str, *, allow_none=True, decimals: int = 0) -> int | float | None:
    if v is None or v == "":
        if allow_none:
            return None
        raise HTTPException(400, f"{field} is required.")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a number of cents.") from None
    if f < 0:
        raise HTTPException(400, f"{field} cannot be negative.")
    return round(f, decimals) if decimals else int(round(f))


async def _valid_endpoint(e: dict | None, side: str) -> dict:
    if not isinstance(e, dict):
        raise HTTPException(400, f"Give the {side} as a UN/LOCODE or a country.")
    out: dict = {}
    if e.get("unlocode"):
        code = str(e["unlocode"]).upper().strip()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", code):
            raise HTTPException(400, f"{side.capitalize()} UN/LOCODE must look like FIHEL.")
        if code[:2] in net.SANCTIONED_COUNTRIES:
            raise HTTPException(400, "Rates to or from Russia or Belarus cannot be loaded (sanctions, RULE K-2).")
        if not net.network_location(code) and not await db.locations.find_one({"unlocode": code}):
            raise HTTPException(400, f"{code} is not a known location. Add it to the locations list first.")
        out["unlocode"] = code
        out["radius_km"] = float(e.get("radius_km") or 40)
    elif e.get("countries"):
        out["countries"] = sorted({str(c).upper()[:2] for c in e["countries"] if c})
    elif e.get("country"):
        out["country"] = str(e["country"]).upper()[:2]
    else:
        raise HTTPException(400, f"Give the {side} as a UN/LOCODE or a country.")
    for cc in [out.get("country")] + list(out.get("countries") or []) + [out.get("unlocode", "")[:2]]:
        if cc in net.SANCTIONED_COUNTRIES:
            raise HTTPException(400, "Rates to or from Russia or Belarus cannot be loaded (sanctions, RULE K-2).")
    if e.get("name"):
        out["name"] = str(e["name"])[:80]
    return out


async def validate_rate(body: dict, existing: dict | None = None) -> dict:
    data = {**(existing or {}), **{k: v for k, v in body.items() if k not in ("id", "_id", "state", "days_left", "lane",
                                                                              "carrier_name", "carrier_code")}}
    carrier = await db.carriers.find_one({"id": data.get("carrier_id")}, NO_ID)
    if not carrier:
        raise HTTPException(400, "Choose the carrier for this rate card.")
    if data.get("mode") not in MODES:
        raise HTTPException(400, "Mode must be sea, road, air or rail.")
    if data.get("service") not in SERVICES:
        raise HTTPException(400, f"Service must be one of {', '.join(SERVICES)}.")
    if data.get("basis") not in BASES:
        raise HTTPException(400, f"Basis must be one of {', '.join(BASES)}.")
    data["origin"] = await _valid_endpoint(data.get("origin"), "origin")
    data["destination"] = await _valid_endpoint(data.get("destination"), "destination")
    if data["basis"] == "PER_CONTAINER":
        cc = {k: _int_cents(v, f"Container cost {k}") for k, v in (data.get("container_costs") or {}).items()
              if k in pricing.CONTAINERS and v not in (None, "")}
        if not cc:
            raise HTTPException(400, "Give at least one container cost (20GP, 40GP or 40HC).")
        data["container_costs"] = cc
    else:
        tiers = []
        for t in data.get("tiers") or []:
            unit = _int_cents(t.get("unit_cost_cents"), "Tier price", allow_none=False, decimals=2)
            mq = t.get("max_qty")
            tiers.append({"max_qty": float(mq) if mq not in (None, "") else None, "unit_cost_cents": unit})
        data["tiers"] = sorted(tiers, key=lambda t: t["max_qty"] if t["max_qty"] is not None else 1e18) or None
        data["unit_cost_cents"] = _int_cents(data.get("unit_cost_cents"), "Unit cost")
        if not data["tiers"] and data["unit_cost_cents"] is None:
            raise HTTPException(400, "Give a unit cost or weight-break tiers.")
    data["min_cost_cents"] = _int_cents(data.get("min_cost_cents"), "Minimum charge")
    sur = []
    for s in data.get("surcharges") or []:
        code = str(s.get("code") or "").upper().strip()
        if not code:
            raise HTTPException(400, "Every surcharge needs a code (e.g. BAF, ETS, DTHC).")
        sb = s.get("basis") or "PER_SHIPMENT"
        if sb not in SURCHARGE_BASES:
            raise HTTPException(400, f"Surcharge {code}: basis must be one of {', '.join(SURCHARGE_BASES)}.")
        row = {"code": code, "label": (s.get("label") or code)[:80], "basis": sb}
        if sb == "PERCENT":
            try:
                row["pct"] = float(s.get("pct"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Surcharge {code}: give the percentage.") from None
        elif s.get("by_size"):
            row["by_size"] = {k: _int_cents(v, f"Surcharge {code} {k}'") for k, v in s["by_size"].items() if v not in (None, "")}
        else:
            row["unit_cost_cents"] = _int_cents(s.get("unit_cost_cents"), f"Surcharge {code}", allow_none=False)
        if s.get("min_cents") not in (None, ""):
            row["min_cents"] = _int_cents(s.get("min_cents"), f"Surcharge {code} minimum")
        sur.append(row)
    data["surcharges"] = sur
    # References are explicit opt-in; no public tariff is turned into a carrier buy-rate card.
    refs = data.get("public_surcharge_refs") or []
    if not isinstance(refs, list) or len(refs) > 10:
        raise HTTPException(400, "Public surcharge references must be a list of at most ten items.")
    seen = set()
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("tariff_id"), str):
            raise HTTPException(400, "Every public surcharge reference needs a tariff_id.")
        from services.public_surcharges import TARIFF_CODES
        if ref["tariff_id"] not in TARIFF_CODES:
            raise HTTPException(400, "Unknown public surcharge tariff_id.")
        if ref.get("date_basis", "service_date") not in ("service_date", "carrier_price_calculation_date", "booking_date"):
            raise HTTPException(400, "Unknown public surcharge date basis.")
        if ref["tariff_id"] in seen:
            raise HTTPException(400, "A public surcharge reference cannot be repeated.")
        seen.add(ref["tariff_id"])
        if ref.get("on_date"):
            try:
                ref["on_date"] = date.fromisoformat(ref["on_date"]).isoformat()
            except (ValueError, TypeError):
                raise HTTPException(400, "Public surcharge date must be YYYY-MM-DD.") from None
        if ref.get("caf_subject_amount") is not None:
            from services.public_surcharges import decimal
            try:
                if decimal(ref["caf_subject_amount"]) <= 0:
                    raise ValueError("nonpositive CAF base")
            except (ValueError, ArithmeticError):
                raise HTTPException(400, "CAF-subject amount must be positive and finite.") from None
    data["public_surcharge_refs"] = refs
    try:
        vf = date.fromisoformat(str(data.get("valid_from"))[:10])
        vt = date.fromisoformat(str(data.get("valid_to"))[:10])
    except ValueError:
        raise HTTPException(400, "Give valid from and valid to dates (YYYY-MM-DD).") from None
    if vt < vf:
        raise HTTPException(400, "Valid to must be on or after valid from.")
    data["valid_from"], data["valid_to"] = vf.isoformat(), vt.isoformat()
    try:
        data["transit_days"] = int(data.get("transit_days") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Transit days must be a whole number.") from None
    wd = data.get("departure_weekdays") or []
    if any(not isinstance(d, int) or d < 0 or d > 6 for d in wd):
        raise HTTPException(400, "Departure weekdays are numbers 0 (Monday) to 6 (Sunday).")
    data["source"] = data.get("source") if data.get("source") in ("indicative", "contract", "spot") else "contract"
    data["currency"] = "EUR"
    data["via"] = [str(v).upper() for v in data.get("via") or []]
    data["service_name"] = (data.get("service_name") or pricing.SERVICE_NAME.get(data["service"])).strip()[:120]
    return data


# ------------------------------------------------------------------------------------------------
# Carriers
# ------------------------------------------------------------------------------------------------
@router.get("/carriers")
async def list_carriers(mode: str | None = None, q: str | None = None, active: str | None = None,
                        user: dict = Depends(require_internal)):
    query: dict = {}
    if mode:
        query["modes"] = mode
    if q:
        rx = re.escape(q.strip())
        query["$or"] = [{"name": {"$regex": rx, "$options": "i"}}, {"code": {"$regex": rx, "$options": "i"}}]
    if active in ("true", "false"):
        query["active"] = {"$ne": False} if active == "true" else False
    items = await db.carriers.find(query, NO_ID).sort("name", 1).to_list(500)
    today = now().date()
    stats: dict[str, dict] = {}
    async for card in db.rate_cards.find({"archived": {"$ne": True}}, {"_id": 0, "carrier_id": 1, "valid_from": 1, "valid_to": 1}):
        st, _ = rate_state(card, today)
        s = stats.setdefault(card["carrier_id"], {"rate_cards": 0, "valid": 0, "expiring": 0, "expired": 0, "upcoming": 0})
        s["rate_cards"] += 1
        s[st] += 1
    for c in items:
        c["integration"] = {**(c.get("integration") or {}), **net.integration_status(c)}
        c["stats"] = stats.get(c["id"], {"rate_cards": 0, "valid": 0, "expiring": 0, "expired": 0, "upcoming": 0})
        c["performance"] = {"on_time_pct": c.get("on_time_pct"), "source": "benchmark"}
    counts = {m: 0 for m in MODES}
    async for c in db.carriers.find({}, {"_id": 0, "modes": 1}):
        for m in c.get("modes") or []:
            counts[m] = counts.get(m, 0) + 1
    return {"items": items, "total": len(items), "counts": counts}


@router.get("/carriers/rank")
async def rank_for_leg(shipment_id: str, leg_seq: int, user: dict = Depends(require_internal)):
    shipment = await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not shipment:
        raise HTTPException(404, "Shipment not found")
    leg = next((lg for lg in shipment.get("legs") or [] if int(lg.get("seq", -1)) == int(leg_seq)), None)
    if not leg:
        raise HTTPException(404, f"Shipment has no leg {leg_seq}")
    return await rank_carriers_for_leg(shipment, leg)


FREIGHT_STAGES = ("origin", "main", "destination")


async def rank_carriers_for_leg(shipment: dict, leg: dict) -> dict:
    """Rank carriers for one shipment leg with the same route engine the quotes use (direct cards, groupage zones,
    pre/on-carriage, feeder/rail connections, DG acceptance, validity). Cost = freight lines of the option (no
    forwarder fees, customs or disbursements), price = cost with the default markup."""
    from services.gazetteer import GeocoderBusy

    try:
        a = await pricing.resolve_location(leg.get("from") or {})
        b = await pricing.resolve_location(leg.get("to") or {})
    except GeocoderBusy:
        a = b = None
    if not a or not b:
        raise HTTPException(400, "This leg has no resolvable from/to location; set its UN/LOCODEs first.")
    raw_cargo = dict(shipment.get("cargo") or {})
    try:
        pricing.normalize_cargo(raw_cargo)
        cargo = raw_cargo
    except pricing.QuoteInputError:
        cargo = {"load_type": "FCL" if raw_cargo.get("containers") else "loose", "weight_kg": raw_cargo.get("weight_kg") or 1000,
                 "volume_m3": raw_cargo.get("volume_m3"), "containers": raw_cargo.get("containers") or [],
                 "dangerous_goods": raw_cargo.get("dangerous_goods"), "dg": raw_cargo.get("dg")}
    mode = leg.get("mode") or shipment.get("mode")
    leg_info = {"seq": leg.get("seq"), "mode": mode, "from": a, "to": b, "carrier_id": leg.get("carrier_id"), "carrier": leg.get("carrier")}
    try:
        summary = pricing.cargo_summary(pricing.normalize_cargo(dict(cargo)))
    except pricing.QuoteInputError:
        summary = None
    same_place = (a.get("unlocode") and a.get("unlocode") == b.get("unlocode")) or (
        a.get("lat") is not None and b.get("lat") is not None and net.haversine_km(a, b) < 25)
    if same_place:
        # e.g. V1 splits a ferry groupage leg into city -> harbour, crossing, harbour -> city: the local hops are part of
        # the main carrier's service and have no market to compare
        where = a.get("city") or a.get("name")
        return {"leg": leg_info, "cargo_summary": summary, "dangerous_goods": bool(cargo.get("dangerous_goods")), "items": [],
                "not_ranked": [], "warnings": [{
                    "code": "leg.local", "level": "info", "params": {"place": where},
                    "message": f"This leg is a local move within {where} ({a.get('name')} to {b.get('name')}). It is part of the "
                               "connecting carrier's service, so there are no carriers to compare; rank the main leg instead."}]}
    etd = str(leg.get("etd") or "")[:10]
    ready = etd if etd and etd > now().date().isoformat() else None
    loose = (cargo.get("load_type") or "loose") != "FCL" and not cargo.get("containers")
    # a sea crossing of loose cargo is sold as road groupage over the ferry (or RORO): compare both
    price_mode = None if (mode == "sea" and loose) else mode
    try:
        priced = await pricing.quote_pricing({"mode": price_mode, "origin": a, "destination": b, "cargo": cargo, "ready_date": ready},
                                             company=None, strict=False)
    except pricing.QuoteInputError as exc:
        raise HTTPException(400, str(exc)) from None
    if price_mode != mode:
        priced["options"] = [o for o in priced["options"] if o["mode"] in ("sea", "road")]
    settings = await pricing.get_settings()
    markup = float(settings["default_margin_pct"])
    carriers = {c["id"]: c async for c in db.carriers.find({}, NO_ID)}
    best: dict[str, dict] = {}
    for o in priced["options"]:
        freight = [ln for ln in o["lines"] if ln["stage"] in FREIGHT_STAGES and not ln.get("is_pass_through")]
        cost = sum(ln["cost_cents"] for ln in freight)
        if cost <= 0:
            continue
        carrier = carriers.get(o["carrier_id"], {})
        perf = await net.carrier_performance(carrier) if carrier else {"on_time_pct": None, "source": "unknown"}
        row = {
            "carrier_id": o["carrier_id"], "carrier_name": o["carrier_name"], "carrier_code": carrier.get("code"), "mode": o["mode"],
            "rate_card_id": o.get("rate_card_id"), "service": o["service"], "service_name": o.get("service_name"),
            "cost_cents": cost, "price_cents": int(round(cost * (1 + markup / 100))), "transit_days": o["door_to_door_days"],
            "etd": o.get("etd"), "eta": o.get("eta"), "on_time_pct": perf["on_time_pct"], "on_time_source": perf["source"],
            "co2e_kg": o["co2e_kg"], "distance_km": (o.get("co2") or {}).get("distance_km"),
            "legs": [{"kind": lg["kind"], "carrier_name": lg.get("carrier_name"), "from": (lg.get("from") or {}).get("name"),
                      "to": (lg.get("to") or {}).get("name")} for lg in o["legs"]],
            "dg_accepted": True, "dg_reason": None,
            "integration_status": net.integration_status(carrier)["status"] if carrier else "not_configured",
            "rate_source": o.get("rate_source"), "valid_to": o.get("valid_until"),
            "current": o["carrier_id"] == leg.get("carrier_id"),
            "scorecard": o.get("scorecard"), "allocation": o.get("allocation"),
        }
        if row["carrier_id"] not in best or cost < best[row["carrier_id"]]["cost_cents"]:
            best[row["carrier_id"]] = row
    rows = list(best.values())
    not_ranked = [{"carrier_name": n.get("carrier_name"), "reason": n.get("reason"), "code": n.get("code")}
                  for n in priced["not_quoted"]]
    if rows:
        mp = min(r["cost_cents"] for r in rows) or 1
        md = min(r["transit_days"] for r in rows) or 1
        mc = min(r["co2e_kg"] for r in rows) or 0.1

        def score(r):
            alloc = r.get("allocation") or {}  # s3 allocations: behind-MQC credit, cap reached goes last
            return ((r["cost_cents"] - (alloc.get("ranking_credit_cents") or 0)) / mp + 0.35 * r["transit_days"] / md
                    + 0.12 * (r["co2e_kg"] / mc) ** 0.5 + 0.25 * (1 - (r["on_time_pct"] or 70) / 100) + (1.0 if alloc.get("full") else 0.0))

        for r in rows:
            r["tags"] = []
        min(rows, key=lambda r: r["cost_cents"])["tags"].append("cheapest")
        min(rows, key=lambda r: (r["transit_days"], r["cost_cents"]))["tags"].append("fastest")
        min(rows, key=lambda r: r["co2e_kg"])["tags"].append("lowest_co2")
        top = min(rows, key=score)
        top["tags"].append("best_value")
        for r in rows:
            r["recommended"] = r is top
            r["rank_reason"] = "best_value" if r is top else (r["tags"][0] if r["tags"] else None)
        rows.sort(key=score)
    return {"leg": {"seq": leg.get("seq"), "mode": mode, "from": a, "to": b, "carrier_id": leg.get("carrier_id"),
                    "carrier": leg.get("carrier")},
            "cargo_summary": pricing.cargo_summary(pricing.normalize_cargo(cargo)), "dangerous_goods": bool(cargo.get("dangerous_goods")),
            "items": rows, "not_ranked": not_ranked,
            "warnings": [w for w in priced["warnings"]
                         if w.get("code") != "rates.indicative" and not str(w.get("code") or "").startswith("incoterm.")]}


@router.get("/carriers/{carrier_id}")
async def get_carrier(carrier_id: str, user: dict = Depends(require_internal)):
    c = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Carrier not found")
    today = now().date()
    rates = await db.rate_cards.find({"carrier_id": carrier_id, "archived": {"$ne": True}}, NO_ID).sort("valid_to", 1).to_list(500)
    rates = [enrich_rate(r, {c["id"]: c}, today) for r in rates]
    c["integration"] = {**(c.get("integration") or {}), **net.integration_status(c)}
    perf = await net.carrier_performance(c)
    recent = []
    async for s in db.shipments.find({"legs.carrier_id": carrier_id}, {"_id": 0, "id": 1, "reference": 1, "legs": 1, "status": 1}
                                     ).sort("created_at", -1).limit(20):
        for lg in s.get("legs") or []:
            if lg.get("carrier_id") == carrier_id:
                recent.append({"shipment_id": s["id"], "reference": s.get("reference"), "status": s.get("status"),
                               "seq": lg.get("seq"), "from": (lg.get("from") or {}).get("name"), "to": (lg.get("to") or {}).get("name"),
                               "etd": lg.get("etd"), "eta": lg.get("eta"), "atd": lg.get("atd"), "ata": lg.get("ata")})
    c["performance"] = perf
    return {"carrier": c, "rates": rates, "performance": perf, "recent_legs": recent}


def _carrier_doc(body: dict, existing: dict | None = None) -> dict:
    data = {k: body[k] for k in CARRIER_FIELDS if k in body}
    if existing is None or "name" in data:
        if not (data.get("name") or "").strip():
            raise HTTPException(400, "Give the carrier's name.")
        data["name"] = data["name"].strip()[:120]
    if existing is None or "code" in data:
        code = re.sub(r"[^A-Z0-9_]", "", str(data.get("code") or "").upper())
        if not code:
            raise HTTPException(400, "Give a short carrier code (letters and digits, e.g. MAERSK).")
        data["code"] = code
    if "modes" in data or existing is None:
        modes = [m for m in data.get("modes") or [] if m in MODES]
        if not modes:
            raise HTTPException(400, "Choose at least one mode (sea, road, air, rail).")
        data["modes"] = modes
    if "on_time_pct" in data and data["on_time_pct"] not in (None, ""):
        v = float(data["on_time_pct"])
        if not 0 <= v <= 100:
            raise HTTPException(400, "On-time % must be between 0 and 100.")
        data["on_time_pct"] = v
    if "co2_factor_g_tkm" in data and data["co2_factor_g_tkm"] not in (None, ""):
        data["co2_factor_g_tkm"] = float(data["co2_factor_g_tkm"])
    if "dg_un_blocked" in data:
        data["dg_un_blocked"] = [u if str(u).upper().startswith("UN") else f"UN{u}" for u in (data["dg_un_blocked"] or [])]
    if "claims_email" in data:
        data["claims_email"] = (str(data["claims_email"] or "").strip()[:200]) or None
    if "recourse_contract" in data:
        data["recourse_contract"] = bool(data["recourse_contract"])
    itype = body.get("integration_type") or (body.get("integration") or {}).get("type")
    if itype:
        if itype not in INTEGRATION_TYPES:
            raise HTTPException(400, f"Integration type must be one of {', '.join(INTEGRATION_TYPES)}.")
        data["integration"] = {**((existing or {}).get("integration") or {}), "type": itype}
        if (body.get("integration") or {}).get("note"):
            data["integration"]["note"] = body["integration"]["note"][:300]
    return data


@router.post("/carriers")
async def create_carrier(body: dict, user: dict = Depends(require_perm("rates.write"))):
    data = _carrier_doc(body)
    if body.get("test_company_id"):  # s2: a test haulier bound to one test tenant, never priced or booked
        from core.test_counterparty import bind
        data.update(await bind(str(body["test_company_id"])), active=False)
    if await db.raw.carriers.find_one({"code": data["code"]}):
        raise HTTPException(409, f"A carrier with code {data['code']} already exists.")
    doc = {"id": new_id(), "integration": {"type": "manual"}, "active": True, "dg_classes_accepted": [], "dg_un_blocked": [],
           "country": None, "created_at": now_iso(), "updated_at": now_iso(), **data}
    await db.carriers.insert_one(dict(doc))
    await audit(user, "carrier_created", "carrier", doc["id"], {"name": doc["name"], "code": doc["code"]})
    await party_screening.screen_carrier_quietly(doc["id"], user)  # s12 sanctions / denied-party screening
    doc["integration"] = {**doc["integration"], **net.integration_status(doc)}
    return {"carrier": doc}


@router.patch("/carriers/{carrier_id}")
async def update_carrier(carrier_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    c = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    if not c:
        raise HTTPException(404, "Carrier not found")
    data = _carrier_doc(body, c)
    if c.get("test_tenant") and data.get("active"):
        raise HTTPException(409, "A test carrier stays inactive; it is never priced or booked.")
    if data.get("code") and data["code"] != c.get("code") and await db.raw.carriers.find_one({"code": data["code"]}):
        raise HTTPException(409, f"A carrier with code {data['code']} already exists.")
    data["updated_at"] = now_iso()
    await db.carriers.update_one({"id": carrier_id}, {"$set": data})
    await audit(user, "carrier_updated", "carrier", carrier_id,
                {"changes": {k: {"from": c.get(k), "to": v} for k, v in data.items() if c.get(k) != v and k != "updated_at"}})
    if data.get("name", c.get("name")) != c.get("name") or data.get("country", c.get("country")) != c.get("country"):
        await party_screening.screen_carrier_quietly(carrier_id, user)  # s12: re-screen a renamed / moved carrier
    fresh = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    fresh["integration"] = {**(fresh.get("integration") or {}), **net.integration_status(fresh)}
    return {"carrier": fresh}


# ------------------------------------------------------------------------------------------------
# Rate cards
# ------------------------------------------------------------------------------------------------
@router.get("/rates")
async def list_rates(mode: str | None = None, carrier_id: str | None = None, q: str | None = None,
                     state: str | None = None, expiring_days: int = Query(21, ge=1, le=180),
                     service: str | None = None, carrier: str | None = None, lane: str | None = None,
                     expiring: bool | None = None, user: dict = Depends(require_internal)):
    # aliases: carrier = id or code, lane = text match on "FROM -> TO", expiring=true = state=expiring
    if carrier and not carrier_id:
        c = await db.carriers.find_one({"$or": [{"id": carrier}, {"code": carrier.upper()}]}, {"_id": 0, "id": 1})
        carrier_id = (c or {}).get("id") or "__none__"
    q = q or lane
    if expiring and not state:
        state = "expiring"
    query: dict = {"archived": {"$ne": True}}
    if mode:
        query["mode"] = mode
    if carrier_id:
        query["carrier_id"] = carrier_id
    if service:
        query["service"] = service
    carriers = {c["id"]: c async for c in db.carriers.find({}, NO_ID)}
    today = now().date()
    rows = [enrich_rate(r, carriers, today, expiring_days) async for r in db.rate_cards.find(query, NO_ID)]
    if q:
        ql = q.strip().lower()
        rows = [r for r in rows if ql in (r["lane"] + " " + (r.get("service_name") or "") + " " + (r.get("carrier_name") or "") + " "
                                          + " ".join(r.get("via") or [])).lower()]
    counts = {"valid": 0, "expiring": 0, "expired": 0, "upcoming": 0}
    for r in rows:
        counts[r["state"]] += 1
    if state:
        rows = [r for r in rows if r["state"] == state]
    rows.sort(key=lambda r: ({"expiring": 0, "expired": 1, "valid": 2, "upcoming": 3}[r["state"]], r["mode"], r["lane"]))
    return {"items": rows, "total": len(rows), "counts": counts, "expiring_days": expiring_days}


@router.get("/rates/{rate_id}")
async def get_rate(rate_id: str, user: dict = Depends(require_internal)):
    r = await db.rate_cards.find_one({"id": rate_id}, NO_ID)
    if not r:
        raise HTTPException(404, "Rate card not found")
    carriers = {c["id"]: c async for c in db.carriers.find({"id": r["carrier_id"]}, NO_ID)}
    return {"rate": enrich_rate(r, carriers, now().date())}


@router.post("/rates")
async def create_rate(body: dict, user: dict = Depends(require_perm("rates.write"))):
    data = await validate_rate(body)
    doc = {"id": new_id(), **data, "archived": False, "demo": False, "created_at": now_iso(), "updated_at": now_iso(),
           "updated_by": {"id": user["id"], "name": user.get("name")}}
    await db.rate_cards.insert_one(dict(doc))
    await audit(user, "rate_created", "rate_card", doc["id"], {"lane": f"{doc['origin']} -> {doc['destination']}",
                                                               "carrier_id": doc["carrier_id"]})
    carriers = {c["id"]: c async for c in db.carriers.find({"id": doc["carrier_id"]}, NO_ID)}
    return {"rate": enrich_rate(doc, carriers, now().date())}


@router.patch("/rates/{rate_id}")
async def update_rate(rate_id: str, body: dict, user: dict = Depends(require_perm("rates.write"))):
    r = await db.rate_cards.find_one({"id": rate_id}, NO_ID)
    if not r or r.get("archived"):
        raise HTTPException(404, "Rate card not found")
    data = await validate_rate(body, r)
    data["updated_at"] = now_iso()
    data["updated_by"] = {"id": user["id"], "name": user.get("name")}
    changed = {k: {"from": r.get(k), "to": v} for k, v in data.items() if r.get(k) != v and k not in ("updated_at", "updated_by")}
    await db.rate_cards.update_one({"id": rate_id}, {"$set": data})
    await audit(user, "rate_updated", "rate_card", rate_id, {"changes": changed})
    fresh = await db.rate_cards.find_one({"id": rate_id}, NO_ID)
    carriers = {c["id"]: c async for c in db.carriers.find({"id": fresh["carrier_id"]}, NO_ID)}
    return {"rate": enrich_rate(fresh, carriers, now().date())}


@router.delete("/rates/{rate_id}")
async def archive_rate(rate_id: str, user: dict = Depends(require_perm("rates.write"))):
    r = await db.rate_cards.find_one({"id": rate_id}, NO_ID)
    if not r or r.get("archived"):
        raise HTTPException(404, "Rate card not found")
    await db.rate_cards.update_one({"id": rate_id}, {"$set": {"archived": True, "archived_at": now_iso(),
                                                              "updated_by": {"id": user["id"], "name": user.get("name")}}})
    await audit(user, "rate_archived", "rate_card", rate_id, {"snapshot": r})
    return {"ok": True}
