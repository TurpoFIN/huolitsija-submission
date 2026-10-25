"""Customer contract tariffs (sopimushinnasto) next to spot prices.

    priced = await apply_contract(priced, company)     # services.pricing.quote_pricing post-step
    tariff = await create_from_tender(tender, lanes, actor)   # services.tenders award

Collection `customer_tariffs` {id, number, company_id, name, status draft|active|expired|superseded, valid_from, valid_to,
currency EUR, source manual|tender, tender_id, exclusive, supersedes_id, superseded_by, lanes[{id, origin{country, locode?,
postcode_prefix?}, destination{...}, mode, service?, equipment_type?, basis, price_cents, min_cents?, surcharges_included,
transit_days?, notes}]}.

Precedence (also in notes/s7-pricing.md):
1. Only tariffs with status active whose valid_from..valid_to (inclusive) contains the request's ready date apply;
   drafts, expired, superseded and not-yet-valid tariffs never do. The quote company must be the tariff company.
2. A lane matches on mode, origin and destination (country; UN/LOCODE = the request place or the main leg's port;
   postcode prefix = the request address), and, when the lane sets them, service and equipment type (every container).
3. Several matching lanes for one mode: the most specific lane wins (LOCODE / postcode 2 points per side, country 1,
   service 1, equipment 1), then the tariff with the newest valid_from, then the newest tariff.
4. The winner becomes an option with price_basis "contract", recommended over every spot option when it is bookable (a
   contract price without a confirmed buy cost is bookable False and leaves a bookable spot option recommended). Spot options stay
   visible (price_basis "spot") unless the tariff is exclusive: then spot options of that mode are dropped.
5. The contract price replaces the transport lines of the cheapest spot option of the same mode (and service /
   equipment when the lane sets them): with surcharges_included every pickup, freight, surcharge and delivery line; without,
   only the base freight, pickup and delivery lines, and the spot surcharge lines (BAF, ETS, THC, fuel ...) stay on top.
   Forwarder fees, customs, insurance, DG and duty / VAT lines always come from the spot option.
6. Cost = the replaced spot lines' cost, so margin is real. No spot option of that mode: cost_basis "unknown",
   cost_cents None (core.autonomy.quote_within_policy then never approves it and margin_rules.margin_floor_ok blocks it).
   The contract price is never raised to a margin floor: an option whose own margin is negative or under its floor gets
   below_floor True and a contract.below_floor warning, the floor check reports it, and it is not recommended while a
   sellable spot option exists.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from datetime import date, timedelta

from fastapi import HTTPException

from core.audit import audit
from core.db import db, register_index
from core.lifecycle import background_loop
from core.refs import next_ref
from core.util import new_id, now, now_iso

log = logging.getLogger("contract_tariffs")

STATUSES = ("draft", "active", "expired", "superseded")
BASES = ("PER_CONTAINER", "PER_SHIPMENT", "PER_KG", "PER_LDM", "PER_PALLET", "PER_WM")
MODES = ("sea", "road", "air", "rail")
TRANSPORT_STAGES = ("origin", "main", "destination")
BASE_FREIGHT_CODES = {"OCEAN_FREIGHT", "AIR_FREIGHT", "ROAD_FREIGHT", "RORO_FREIGHT", "RAIL_FREIGHT", "FEEDER_FREIGHT",
                      "MANUAL", "PICKUP", "DELIVERY"}
CARGO_LINE_CODES = {"DG"}  # cargo-specific, never part of a lane price
BASIS_TEXT = {"PER_CONTAINER": ("per container", "per kontti"), "PER_SHIPMENT": ("per shipment", "per lähetys"),
              "PER_KG": ("per kg", "per kg"), "PER_LDM": ("per loading metre", "per lavametri"),
              "PER_PALLET": ("per pallet", "per lava"), "PER_WM": ("per W/M", "per W/M")}
DEFAULT_TRANSIT = {"road": 3, "sea": 14, "air": 3, "rail": 18}
SERVICE_MODE = {"FCL": "sea", "LCL": "sea", "RORO": "sea", "AIR": "air", "COURIER": "air", "ROAD_LTL": "road",
                "ROAD_FTL": "road", "HAULAGE": "road", "RAIL_FCL": "rail", "RAIL_WAGON": "rail"}
TENDER_LANE_KEYS = ("lanes", "tariff_lanes", "rows")

register_index("customer_tariffs", "id", unique=True)
register_index("customer_tariffs", [("company_id", 1), ("status", 1)])
register_index("customer_tariffs", "tender_id")


class TariffError(ValueError):
    pass


def _day(v, name: str, *, required=False) -> str | None:
    if v in (None, ""):
        if required:
            raise TariffError(f"Enter {name} (YYYY-MM-DD).")
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise TariffError(f"{name} must be a date (YYYY-MM-DD).") from None


def _int(v, name: str, *, allow_none=False, minimum=0) -> int | None:
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not minimum <= v <= 100_000_000:
        raise TariffError(f"{name} must be whole cents from {minimum} to 100 000 000.")
    return v


def _place(p, side: str) -> dict:
    if not isinstance(p, dict):
        raise TariffError(f"Lane {side} must be an object with country (and optionally locode, postcode_prefix).")
    out = {"country": str(p.get("country") or "").upper() or None}
    loc = str(p.get("locode") or p.get("unlocode") or "").upper().replace(" ", "") or None
    if loc:
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", loc):
            raise TariffError(f"Lane {side} UN/LOCODE must be five characters, for example FIHEL.")
        out["country"] = out["country"] or loc[:2]
        if out["country"] != loc[:2]:
            raise TariffError(f"Lane {side} UN/LOCODE {loc} is not in {out['country']}.")
        out["locode"] = loc
    if not out["country"] or not re.fullmatch(r"[A-Z]{2}", out["country"]):
        raise TariffError(f"Lane {side} needs a two-letter country code.")
    pc = str(p.get("postcode_prefix") or "").strip().upper().replace(" ", "")
    if pc:
        if not re.fullmatch(r"[A-Z0-9]{1,10}", pc):
            raise TariffError(f"Lane {side} postcode prefix must be letters or digits.")
        out["postcode_prefix"] = pc
    if p.get("name"):
        out["name"] = str(p["name"])[:120]
    return out


def validate_lane(lane: dict) -> dict:
    if not isinstance(lane, dict):
        raise TariffError("Each lane must be an object.")
    service = str(lane.get("service") or "").upper() or None
    mode = str(lane.get("mode") or SERVICE_MODE.get(service or "", "")).lower()
    if mode not in MODES:
        raise TariffError("Lane mode must be sea, road, air or rail.")
    if service and not re.fullmatch(r"[A-Z_]{2,20}", service):
        raise TariffError("Lane service is a code such as FCL, LCL, AIR, ROAD_LTL or ROAD_FTL.")
    eq = str(lane.get("equipment_type") or "").upper().replace("'", "").replace(" ", "") or None
    if eq:
        from services import pricing

        if eq not in pricing.CONTAINERS:
            raise TariffError(f"Unknown equipment type {eq}. Use {', '.join(pricing.CONTAINERS)}.")
    basis = str(lane.get("basis") or "").upper()
    if basis not in BASES:
        raise TariffError(f"Lane basis must be one of {', '.join(BASES)}.")
    if basis == "PER_CONTAINER" and mode not in ("sea", "rail", "road"):
        raise TariffError("A per-container price needs a sea, rail or road lane.")
    td = lane.get("transit_days")
    if td is not None and (isinstance(td, bool) or not isinstance(td, int) or not 0 <= td <= 120):
        raise TariffError("Transit days must be a whole number from 0 to 120.")
    return {"id": lane.get("id") or new_id(), "origin": _place(lane.get("origin"), "origin"),
            "destination": _place(lane.get("destination"), "destination"), "mode": mode, "service": service,
            "equipment_type": eq, "basis": basis, "price_cents": _int(lane.get("price_cents"), "Lane price", minimum=1),
            "min_cents": _int(lane.get("min_cents"), "Lane minimum", allow_none=True),
            "surcharges_included": bool(lane.get("surcharges_included", False)), "transit_days": td,
            "notes": (str(lane.get("notes") or "").strip()[:500]) or None,
            **({"tender_lane_id": lane["tender_lane_id"]} if lane.get("tender_lane_id") else {})}


async def validate_tariff(body: dict, existing: dict | None = None) -> dict:
    doc = dict(existing or {})
    editable = ("name", "valid_from", "valid_to", "exclusive", "notes")
    if not existing or existing.get("status") == "draft":
        editable += ("company_id", "lanes", "currency", "supersedes_id")
    elif set(body) - set(editable):
        raise TariffError("An active contract only takes changes to name, validity end, exclusivity and notes; "
                          "make a new draft for new prices.")
    doc.update({k: v for k, v in body.items() if k in editable})
    if not doc.get("company_id") or not await db.companies.find_one({"id": doc["company_id"]}, {"id": 1}):
        raise TariffError("Choose the customer company.")
    doc["name"] = (str(doc.get("name") or "").strip()[:160]) or "Contract tariff"
    if (doc.get("currency") or "EUR") != "EUR":
        raise TariffError("Contract tariffs are in EUR.")
    doc["currency"] = "EUR"
    doc["valid_from"] = _day(doc.get("valid_from"), "valid from", required=True)
    doc["valid_to"] = _day(doc.get("valid_to"), "valid to")
    if doc["valid_to"] and doc["valid_to"] < doc["valid_from"]:
        raise TariffError("Valid to must be on or after valid from.")
    doc["exclusive"] = bool(doc.get("exclusive", False))
    doc["notes"] = (str(doc.get("notes") or "").strip()[:2000]) or None
    lanes = doc.get("lanes") or []
    if not isinstance(lanes, list) or len(lanes) > 500:
        raise TariffError("Lanes must be a list of at most 500 lanes.")
    if not existing or existing.get("status") == "draft":
        doc["lanes"] = [validate_lane(ln) for ln in lanes]
    return doc


def lane_label(lane: dict) -> str:
    def side(p):
        return p.get("locode") or ((p.get("postcode_prefix") + " ") if p.get("postcode_prefix") else "") + p["country"]
    return f"{lane['mode']} {side(lane['origin'])}>{side(lane['destination'])}" + (f" {lane['service']}" if lane.get("service") else "") + (
        f" {lane['equipment_type']}" if lane.get("equipment_type") else "")


# ------------------------------------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------------------------------------
def _postcode(place: dict | None) -> str | None:
    p = place or {}
    pc = p.get("postal_code") or p.get("postcode") or (p.get("address") or {}).get("postal_code") or (p.get("address") or {}).get("postcode")
    return str(pc).upper().replace(" ", "") if pc else None


def _side_score(want: dict, place: dict, leg_end: dict | None) -> int | None:
    if (place or {}).get("country") != want["country"]:
        return None
    score = 1
    if want.get("locode"):
        if want["locode"] not in {(place or {}).get("unlocode"), (leg_end or {}).get("unlocode")}:
            return None
        score += 2
    if want.get("postcode_prefix"):
        pc = _postcode(place)
        if not pc or not pc.startswith(want["postcode_prefix"]):
            return None
        score += 2
    return score


def _containers(req: dict, spot: dict | None) -> list[dict]:
    return (req.get("cargo") or {}).get("containers") or (spot or {}).get("suggested_containers") or []


def lane_score(lane: dict, req: dict, spot: dict | None, mode: str) -> int | None:
    """Specificity of a lane for this request and mode (None = no match). spot = the spot option it would price."""
    if lane["mode"] != mode:
        return None
    if lane.get("service") and (spot.get("service") != lane["service"] if spot is not None
                                else lane["service"] not in _services_for_cargo(req)):
        return None
    leg = next((lg for lg in (spot or {}).get("legs") or [] if lg.get("kind") == "main"), {})
    o = _side_score(lane["origin"], req.get("origin"), leg.get("from"))
    d = _side_score(lane["destination"], req.get("destination"), leg.get("to"))
    if o is None or d is None:
        return None
    score = o + d + (1 if lane.get("service") else 0)
    if lane.get("equipment_type"):
        cs = _containers(req, spot)
        if not cs or any(c["type"] != lane["equipment_type"] for c in cs):
            return None
        score += 1
    if _basis_qty(lane["basis"], req, spot) is None:
        return None
    return score


def _services_for_cargo(req: dict) -> set[str]:
    c = req.get("cargo") or {}
    if c.get("containers"):
        return {"FCL", "RAIL_FCL", "HAULAGE"}
    if c.get("load_type") == "FTL":
        return {"ROAD_FTL", "RORO", "RAIL_WAGON"}
    return {"LCL", "AIR", "COURIER", "ROAD_LTL", "ROAD_FTL", "RORO", "RAIL_WAGON"}


def _basis_qty(basis: str, req: dict, spot: dict | None) -> float | None:
    from services import pricing

    cargo = req.get("cargo") or {}
    m = pricing.cargo_metrics(cargo)
    ch = (spot or {}).get("chargeable") or {}
    if basis == "PER_SHIPMENT":
        return 1.0
    if basis == "PER_CONTAINER":
        n = sum(c["count"] for c in _containers(req, spot))
        return float(n) if n else None
    if basis == "PER_KG":
        kg = ch.get("chargeable_kg") or m.get("weight_kg")
        return float(kg) if kg else None
    if basis == "PER_LDM":
        return float(m["ldm"]) if m.get("ldm") else None
    if basis == "PER_PALLET":
        return float(cargo["packages"]) if cargo.get("packages") and str(cargo.get("package_type") or "").startswith("pallet") else None
    if basis == "PER_WM":
        if not (m.get("weight_kg") or m.get("volume_m3")):
            return None
        return float(ch.get("wm") or pricing.chargeable("LCL", m)["wm"])
    return None


async def candidate_tariffs(company_id: str, day: date) -> list[dict]:
    rows = await db.customer_tariffs.find({"company_id": company_id, "status": "active"}, {"_id": 0}).to_list(200)
    out = []
    for t in rows:
        if t.get("valid_from") and date.fromisoformat(t["valid_from"][:10]) > day:
            continue
        if t.get("valid_to") and date.fromisoformat(t["valid_to"][:10]) < day:
            continue
        out.append(t)
    return out


# ------------------------------------------------------------------------------------------------
# Building the contract option
# ------------------------------------------------------------------------------------------------
def _totals(opt: dict, lines: list[dict], cost_known: bool):
    total = sum(ln["amount_cents"] for ln in lines)
    pt = sum(ln["amount_cents"] for ln in lines if ln.get("is_pass_through"))
    vat = sum(int(round(ln["amount_cents"] * ln.get("vat_rate_pct", 0) / 100)) for ln in lines)
    own = total - pt
    opt.update(lines=lines, subtotal_cents=own, pass_through_cents=pt, total_cents=total, vat_cents=vat, total_incl_vat_cents=total + vat)
    if cost_known:
        cost = sum(ln["cost_cents"] for ln in lines)
        margin = sum(ln["amount_cents"] - ln["cost_cents"] for ln in lines if not ln.get("is_pass_through"))
        own_cost = cost - pt
        opt.update(cost_cents=cost, margin_cents=margin, margin_pct=round(100 * margin / own, 1) if own else 0.0,
                   markup_pct=round(100 * margin / own_cost, 1) if own_cost > 0 else None)
    else:
        opt.update(cost_cents=None, margin_cents=None, margin_pct=None, markup_pct=None)


def _contract_line(tariff: dict, lane: dict, qty: float, cost_cents: int | None, like: dict | None, req: dict) -> dict:
    from services import pricing

    en_b, fi_b = BASIS_TEXT[lane["basis"]]
    amount = int(round(lane["price_cents"] * qty))
    unit, q, note, note_fi = lane["price_cents"], round(qty, 3), f"{lane_label(lane)}, {en_b}", f"{lane_label(lane)}, {fi_b}"
    basis = lane["basis"]
    if lane.get("min_cents") and amount < lane["min_cents"]:
        unit, q, amount, basis = lane["min_cents"], 1, lane["min_cents"], "PER_SHIPMENT"
        note, note_fi = note + ", contract minimum", note_fi + ", sopimuksen minimi"
    incl_en = "surcharges included" if lane["surcharges_included"] else "surcharges extra"
    incl_fi = "lisät sisältyvät" if lane["surcharges_included"] else "lisät erikseen"
    ln = {"code": "CONTRACT_FREIGHT", "description": f"Contract freight {tariff['number']} ({incl_en})",
          "description_fi": f"Sopimusrahti {tariff['number']} ({incl_fi})", "stage": "main", "basis": basis,
          "qty": q, "unit_price_cents": unit, "amount_cents": amount,
          "unit_cost_cents": int(round((cost_cents or 0) / q)) if q else 0, "cost_cents": int(cost_cents or 0),
          "is_pass_through": False, "estimated": False, "note": note, "note_fi": note_fi,
          "leg_seq": (like or {}).get("leg_seq"), "contract": True, "contract_tariff_id": tariff["id"], "contract_lane_id": lane["id"]}
    ln["vat_code"] = (like or {}).get("vat_code") or pricing._vat_code_for(ln, req, {})
    ln["vat_rate_pct"] = (like or {}).get("vat_rate_pct", pricing.VAT_RATES.get(ln["vat_code"], 0.0))
    ln["payer"] = (like or {}).get("payer") or ("CONSIGNEE" if req.get("customer_role") == "buyer" else "SHIPPER")
    return ln


def _below_floor(opt: dict) -> bool:
    """Known cost and an own margin under zero or under the option's margin floor."""
    margin = opt.get("margin_cents")
    if opt.get("cost_basis") == "unknown" or type(margin) is not int:
        return False
    return margin < max(int(opt.get("margin_floor_cents") or 0), 0)


def _flag_floor(opt: dict):
    """The contract price is binding and never raised; a price under the floor is flagged so the floor check, the
    recommendation and ops see it (margin_rules.margin_floor_ok reports it and Autopilot never sends it)."""
    if not _below_floor(opt):
        opt["below_floor"] = False
        return
    floor = max(int(opt.get("margin_floor_cents") or 0), 0)
    m, f = opt["margin_cents"] / 100, floor / 100
    opt["below_floor"] = True
    opt.setdefault("warnings", []).append({
        "code": "contract.below_floor", "level": "warning", "params": {"margin_cents": opt["margin_cents"], "floor_cents": floor},
        "message": f"The contract price is below the margin floor for this shipment (margin {m:.2f} EUR, floor {f:.2f} EUR): "
                   "it is not recommended and Autopilot does not send it.",
        "message_fi": f"Sopimushinta alittaa tämän lähetyksen katelattian (kate {m:.2f} EUR, lattia {f:.2f} EUR): ".replace(".", ",")
                      + "sitä ei suositella eikä Autopilot lähetä sitä."})


def _sellable(opt: dict) -> bool:
    return opt.get("bookable") is not False and not _below_floor(opt)


def _option_id(tariff: dict, lane: dict, base: str, total: int) -> str:
    return "opt_c" + hashlib.sha1(f"{tariff['id']}|{lane['id']}|{base}|{total}".encode()).hexdigest()[:9]


def _from_spot(tariff: dict, lane: dict, spot: dict, req: dict) -> dict | None:
    lines = spot.get("lines") or []
    transport = [ln for ln in lines if ln.get("stage") in TRANSPORT_STAGES and ln.get("code") not in CARGO_LINE_CODES
                 and not ln.get("is_pass_through")]
    replaced = transport if lane["surcharges_included"] else [ln for ln in transport if ln.get("code") in BASE_FREIGHT_CODES]
    if not replaced:
        return None  # the customer pays no carriage under this Incoterm: a lane price has nothing to replace
    qty = _basis_qty(lane["basis"], req, spot)
    cost = sum(int(ln.get("cost_cents") or 0) for ln in replaced)
    like = next((ln for ln in replaced if ln.get("stage") == "main"), replaced[0])
    cline = _contract_line(tariff, lane, qty, cost, like, req)
    rid = {id(ln) for ln in replaced}
    new_lines, placed = [], False
    for ln in lines:
        if id(ln) in rid:
            if not placed:
                new_lines.append(cline)
                placed = True
            continue
        new_lines.append(copy.deepcopy(ln))
    opt = copy.deepcopy({k: v for k, v in spot.items() if k != "lines"})
    estimate = spot.get("firmness") not in (None, "firm") or any(ln.get("estimated") for ln in replaced)
    _totals(opt, new_lines, True)
    opt.update(cost_basis="estimate" if estimate else "rate_card", spot_option_id=spot["id"],
               replaced_line_codes=sorted({ln["code"] for ln in replaced}))
    return opt


def _without_spot(tariff: dict, lane: dict, req: dict, settings: dict, fees_from: dict | None) -> dict:
    from services import carriers as net
    from services import pricing

    o, d = req["origin"], req["destination"]
    ready = date.fromisoformat(req["ready_date"])
    transit = lane.get("transit_days") if lane.get("transit_days") is not None else DEFAULT_TRANSIT[lane["mode"]]
    etd = ready + timedelta(days=1)
    eta = etd + timedelta(days=transit)
    km = net.leg_distance_km(lane["mode"], o, d)
    weight_t = pricing.cargo_metrics(req["cargo"])["co2_weight_t"]
    factor = pricing._default_factor({"sea": "FCL", "road": "ROAD_FTL", "air": "AIR", "rail": "RAIL_FCL"}[lane["mode"]], km)
    co2 = round(km * weight_t * factor / 1000.0, 1)
    regime = pricing._liability(req, {"mode": lane["mode"]})
    qty = _basis_qty(lane["basis"], req, None)
    lines = [_contract_line(tariff, lane, qty, None, None, req)]
    lines[0]["leg_seq"] = 1
    if fees_from:
        lines += [copy.deepcopy(ln) for ln in fees_from.get("lines") or [] if ln.get("stage") not in TRANSPORT_STAGES
                  and ln.get("code") not in CARGO_LINE_CODES and ln.get("code") != "ICS2"]
    else:
        if req["direction"] != "domestic":
            lines.append(pricing._fee_line("DOC_FEE", "fees", settings["documentation_fee_eur"]))
        if req.get("customs_clearance"):
            o_eu, d_eu = o["country"] in net.EU_COUNTRIES, d["country"] in net.EU_COUNTRIES
            if d_eu and not o_eu:
                lines.append(pricing._fee_line("CUSTOMS", "import_customs", settings["customs_clearance_fee_eur"], basis="PER_DECLARATION"))
            if o_eu and not d_eu:
                lines.append(pricing._fee_line("EXPORT_CUSTOMS", "export_customs", settings["customs_clearance_fee_eur"], basis="PER_DECLARATION"))
        for ln in lines[1:]:
            ln.pop("fixed_price", None)
            ln.update(amount_cents=int(round(ln["unit_price_cents"] * ln["qty"])), cost_cents=0, note_fi=ln.get("note"),
                      payer=lines[0]["payer"])
            ln["vat_code"] = pricing._vat_code_for(ln, req, {})
            ln["vat_rate_pct"] = pricing.VAT_RATES.get(ln["vat_code"], 0.0) if ln["vat_code"] != "FI_25_5" else float(settings.get("vat_rate_standard_pct", 25.5))
    if lines[0]["vat_code"] == "FI_25_5":
        lines[0]["vat_rate_pct"] = float(settings.get("vat_rate_standard_pct", 25.5))
    opt = {
        "mode": lane["mode"], "service": lane.get("service") or "CONTRACT",
        "service_name": f"Contract {lane['mode']}" + (f" {lane['service']}" if lane.get("service") else ""),
        "carrier_id": None, "carrier_name": None, "carrier_code": None, "rate_card_id": None,
        "legs": [{"seq": 1, "kind": "main", "role": "main", "mode": lane["mode"], "from": pricing._leg_loc(o), "to": pricing._leg_loc(d),
                  "carrier_id": None, "carrier_name": None, "transit_days": transit, "distance_km": round(km),
                  "service": lane.get("service") or "CONTRACT", "etd": etd.isoformat(), "eta": eta.isoformat(), "co2e_kg": co2}],
        "via": [], "rate_source": "contract", "on_time_pct": None, "not_included": [],
        "etd": etd.isoformat(), "eta": eta.isoformat(), "transit_days": transit, "door_to_door_days": (eta - ready).days,
        "schedule_source": "contract" if lane.get("transit_days") is not None else "estimate",
        "co2e_kg": co2, "co2": {"method": "GLEC Framework default intensities (well-to-wheel), indicative", "distance_km": round(km),
                                "weight_t": round(weight_t, 3), "weight_assumed": False},
        "liability_regime": regime, "liability": {"regime": regime, **pricing.LIABILITY[regime]}, "forwarder_role": "principal",
        "chargeable": {}, "valid_until": None, "dg_accepted": not (req.get("cargo") or {}).get("dangerous_goods"),
        "warnings": [{"code": "contract.cost_unknown", "level": "warning",
                      "message": "No spot rate for this mode: the buy cost is unknown until ops books a carrier.",
                      "message_fi": "Tälle kuljetusmuodolle ei ole spot-hintaa: ostohinta selviää, kun ops varaa kuljettajan.", "params": {}}],
        "margin_rule_id": None, "margin_rule_name": None, "margin_source": None, "margin_floor_cents": None,
    }
    _totals(opt, lines, False)
    opt.update(cost_basis="unknown", spot_option_id=None, replaced_line_codes=[], firmness="partial", bookable=False)
    return opt


async def apply_contract(priced: dict, company: dict | None) -> dict:
    """Interface (shared/interfaces.md s7): add the customer's contract options to priced quote options (in place)."""
    options = priced.get("options") or []
    for o in options:
        o.setdefault("price_basis", "spot")
    if not company or not company.get("id"):
        return priced
    req = priced["request"]
    try:
        day = date.fromisoformat(req["ready_date"][:10])
    except (KeyError, TypeError, ValueError):
        day = now().date()
    tariffs = await candidate_tariffs(company["id"], day)
    if not tariffs:
        return priced
    from core.settings import get_settings

    settings = await get_settings()
    by_mode: dict[str, list[dict]] = {}
    for o in options:
        by_mode.setdefault(o["mode"], []).append(o)
    modes = {ln["mode"] for t in tariffs for ln in t.get("lanes") or []}
    if req.get("mode"):
        modes &= {req["mode"]}
    contract_opts = []
    for mode in sorted(modes):
        best = None  # (key, tariff, lane, spot)
        for t in tariffs:
            for lane in t.get("lanes") or []:
                # A customer selling tariff cannot turn a benchmark buy estimate into a confirmed carrier rate.
                spots = [s for s in by_mode.get(mode, []) if s.get("firmness") in (None, "firm")
                         and lane_score(lane, req, s, mode) is not None]
                spot = min(spots, key=lambda s: (s.get("cost_cents") is None, s.get("cost_cents") or 0, s["total_cents"])) if spots else None
                score = lane_score(lane, req, spot, mode)
                if score is None:
                    continue
                key = (score, t.get("valid_from") or "", t.get("created_at") or "")
                if best is None or key > best[0]:
                    best = (key, t, lane, spot)
        if not best:
            continue
        _, t, lane, spot = best
        opt = _from_spot(t, lane, spot, req) if spot else _without_spot(t, lane, req, settings, min(options, key=lambda s: s["total_cents"]) if options else None)
        if opt is None:
            continue
        vt = [v for v in ((spot or {}).get("valid_until"), t.get("valid_to")) if v]
        opt["valid_until"] = min(vt) if vt else (now().date() + timedelta(days=int(settings["quote_validity_days"]))).isoformat()
        opt.update(id=_option_id(t, lane, (spot or {}).get("id") or "none", opt["total_cents"]), price_basis="contract",
                   contract_tariff_id=t["id"], contract_tariff_number=t["number"], contract_name=t.get("name"),
                   contract_lane_id=lane["id"], contract_exclusive=bool(t.get("exclusive")),
                   spot_total_cents=(spot or {}).get("total_cents"), tags=["contract"], rank_reason="contract", recommended=False)
        if "firmness" in (spot or {}):
            opt.update(firmness="firm", bookable=True)
        _flag_floor(opt)
        contract_opts.append(opt)
    if not contract_opts:
        return priced
    exclusive_modes = {o["mode"] for o in contract_opts if o["contract_exclusive"]}
    spot_opts = [o for o in options if o["mode"] not in exclusive_modes]
    # a sellable contract option wins; a contract price without a confirmed buy cost (bookable False) or below its margin
    # floor never takes the recommendation from a sellable spot option
    contract_opts.sort(key=lambda o: (not _sellable(o), o["total_cents"]))
    if _sellable(contract_opts[0]) or not any(_sellable(o) for o in spot_opts):
        for o in spot_opts:
            o["recommended"] = False
        contract_opts[0]["recommended"] = True
    priced["options"] = contract_opts + spot_opts
    vals = [o["valid_until"] for o in priced["options"] if o.get("valid_until")]
    if vals:
        priced["valid_until"] = min(vals)
    nums = ", ".join(sorted({o["contract_tariff_number"] for o in contract_opts}))
    priced.setdefault("warnings", []).append({
        "code": "contract.applied", "level": "info", "params": {"tariffs": nums},
        "message": f"Your contract price ({nums}) applies; spot prices are shown for comparison." if spot_opts else
                   f"Your contract price ({nums}) applies.",
        "message_fi": f"Sopimushintanne ({nums}) on voimassa; spot-hinnat näkyvät vertailuksi." if spot_opts else
                      f"Sopimushintanne ({nums}) on voimassa."})
    priced["contract"] = {"tariff_ids": sorted({o["contract_tariff_id"] for o in contract_opts}), "numbers": nums,
                          "exclusive_modes": sorted(exclusive_modes)}
    return priced


# ------------------------------------------------------------------------------------------------
# Lifecycle: create, activate, expire, tenders
# ------------------------------------------------------------------------------------------------
async def create_tariff(body: dict, actor: dict | None, *, source: str = "manual", tender_id: str | None = None) -> dict:
    doc = await validate_tariff(body)
    doc.update(id=new_id(), number=await next_ref("CT"), status="draft", source=source, tender_id=tender_id,
               supersedes_id=doc.get("supersedes_id"), superseded_by=None, created_at=now_iso(), updated_at=now_iso(),
               created_by={"id": (actor or {}).get("id"), "name": (actor or {}).get("name")}, activated_at=None, expired_at=None)
    await db.customer_tariffs.insert_one(dict(doc))
    doc.pop("_id", None)
    await audit(actor, "tariff_created", "customer_tariff", doc["id"], {"number": doc["number"], "lanes": len(doc["lanes"]),
                                                                       "source": source}, company_id=doc["company_id"])
    return doc


async def activate(tariff_id: str, actor: dict | None) -> dict:
    t = await db.customer_tariffs.find_one({"id": tariff_id}, {"_id": 0})
    if not t:
        raise HTTPException(404, "Contract tariff not found")
    if t["status"] != "draft":
        raise HTTPException(400, f"Only a draft can be activated (this one is {t['status']}).")
    if not t.get("lanes"):
        raise HTTPException(400, "Add at least one lane before activating.")
    if t.get("valid_to") and t["valid_to"] < now().date().isoformat():
        raise HTTPException(400, "The validity has already ended.")
    upd = {"status": "active", "activated_at": now_iso(), "updated_at": now_iso()}
    await db.customer_tariffs.update_one({"id": tariff_id, "status": "draft"}, {"$set": upd})
    if t.get("supersedes_id"):
        await db.customer_tariffs.update_one({"id": t["supersedes_id"], "company_id": t["company_id"], "status": "active"},
                                             {"$set": {"status": "superseded", "superseded_by": tariff_id, "updated_at": now_iso()}})
    await audit(actor, "tariff_activated", "customer_tariff", tariff_id, {"number": t["number"], "supersedes_id": t.get("supersedes_id")},
                company_id=t["company_id"])
    return {**t, **upd}


async def expire(tariff_id: str, actor: dict | None, reason: str = "manual") -> dict:
    t = await db.customer_tariffs.find_one({"id": tariff_id}, {"_id": 0})
    if not t:
        raise HTTPException(404, "Contract tariff not found")
    if t["status"] not in ("active", "draft"):
        raise HTTPException(400, f"This tariff is already {t['status']}.")
    upd = {"status": "expired", "expired_at": now_iso(), "expired_reason": reason, "updated_at": now_iso()}
    await db.customer_tariffs.update_one({"id": tariff_id}, {"$set": upd})
    await audit(actor, "tariff_expired", "customer_tariff", tariff_id, {"number": t["number"], "reason": reason}, company_id=t["company_id"])
    return {**t, **upd}


async def expire_sweep() -> dict:
    """Active tariffs whose valid_to has passed become expired (pricing never applies them either way)."""
    today = now().date().isoformat()
    n = 0
    async for t in db.customer_tariffs.find({"status": "active", "valid_to": {"$ne": None, "$lt": today}}, {"_id": 0, "id": 1}):
        res = await db.customer_tariffs.update_one({"id": t["id"], "status": "active"}, {"$set": {
            "status": "expired", "expired_at": now_iso(), "expired_reason": "validity_ended", "updated_at": now_iso()}})
        if res.modified_count:
            n += 1
            await audit({"type": "system", "id": "tariff_expiry", "name": "Contract tariff expiry"}, "tariff_expired",
                        "customer_tariff", t["id"], {"reason": "validity_ended"})
    return {"expired": n, "date": today}


try:  # s2 durable jobs when merged, else the in-process loop
    from core.jobs import job as _job, schedule as _schedule  # type: ignore
except ImportError:  # pragma: no cover - depends on merge order
    _job = _schedule = None

if _job and _schedule:
    @_job("tariffs.expire", max_attempts=3, timeout=120, agent=False, label_fi="Sopimushinnastojen vanhentaminen",
          label_en="Contract tariff expiry")
    async def _expire_job(j: dict) -> dict:
        return await expire_sweep()

    _schedule("tariffs.expire_daily", job="tariffs.expire", daily_at="00:10", label_fi="Sopimushinnastojen vanhentaminen",
              label_en="Contract tariff expiry")()
else:
    @background_loop(seconds=3600)
    async def _expire_loop():
        try:
            await expire_sweep()
        except Exception:  # noqa: BLE001
            log.exception("tariff expiry sweep failed")


def _cents_from(row: dict, *keys: str) -> int | None:
    for k in keys:
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(str(v).replace(",", ".").replace(" ", "")) if not isinstance(v, (int, float)) else float(v)
        except ValueError:
            continue
        return int(round(f * 100)) if (k.endswith("_eur") or k in ("unit_price", "price", "rate")) else int(round(f))
    return None


_BASIS_WORDS = [("PER_CONTAINER", ("container", "cntr", "teu", "kontti", "per_container", "box")),
                ("PER_PALLET", ("pallet", "lava", "plt")), ("PER_LDM", ("ldm", "loading metre", "loading meter", "lvm")),
                ("PER_WM", ("w/m", "wm", "cbm", "rt", "revenue ton", "m3")), ("PER_KG", ("kg", "kilo")),
                ("PER_SHIPMENT", ("shipment", "trip", "truck", "ftl", "lähetys", "consignment", "per_shipment"))]
_SERVICE_BASIS = {"FCL": "PER_CONTAINER", "RAIL_FCL": "PER_CONTAINER", "HAULAGE": "PER_CONTAINER", "LCL": "PER_WM", "AIR": "PER_KG",
                  "COURIER": "PER_SHIPMENT", "ROAD_FTL": "PER_SHIPMENT", "RORO": "PER_SHIPMENT", "ROAD_LTL": "PER_KG"}


def _basis_from(row: dict, service: str | None) -> str | None:
    raw = str(row.get("basis") or row.get("price_basis") or row.get("unit") or "").strip()
    if raw.upper() in BASES:
        return raw.upper()
    low = raw.lower()
    for basis, words in _BASIS_WORDS:
        if any(w in low for w in words):
            return basis
    return _SERVICE_BASIS.get(service or "")


async def _tender_place(v) -> dict | None:
    if isinstance(v, dict) and v.get("_query") and not v.get("locode"):
        from services import pricing

        loc = await pricing.resolve_location(v["_query"])
        out = {k: x for k, x in v.items() if k != "_query"}
        if loc and loc.get("unlocode") and loc["country"] == out.get("country"):
            out["locode"] = loc["unlocode"]
        return out
    if isinstance(v, dict) and v.get("_query"):
        return {k: x for k, x in v.items() if k != "_query"}
    if isinstance(v, dict):
        return {"country": v.get("country") or (v.get("unlocode") or v.get("locode") or "")[:2] or None,
                "locode": v.get("locode") or v.get("unlocode"),
                "postcode_prefix": v.get("postcode_prefix"),
                "name": v.get("name") or v.get("city")} if (v.get("country") or v.get("unlocode") or v.get("locode")) else (
            await _tender_place(v.get("query") or v.get("name") or v.get("city")) if (v.get("query") or v.get("name") or v.get("city")) else None)
    s = str(v or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return {"country": s.upper()}
    if re.fullmatch(r"[A-Za-z]{2}\s?[A-Za-z0-9]{3}", s):
        return {"country": s[:2].upper(), "locode": s.replace(" ", "").upper()}
    if not s:
        return None
    from services import pricing

    loc = await pricing.resolve_location(s)
    return {"country": loc["country"], "locode": loc.get("unlocode"), "name": loc.get("name")} if loc else None


_TENDER_UNIT = {"per_container": "PER_CONTAINER", "per_kg": "PER_KG", "per_wm": "PER_WM", "per_shipment": "PER_SHIPMENT",
                "per_trailer": "PER_SHIPMENT", "per_ldm": "PER_LDM", "per_pallet": "PER_PALLET"}


def _flatten_tender_lane(row: dict) -> dict:
    """services.tenders lane {id, ref, parsed{origin{text, city, unlocode, zip, country}, ...}, request, best{mode, service,
    unit, unit_qty, lines, valid_until, ...}} -> flat lane keys. The tender's best price is all-in (fees, customs and
    pass-through lines too); the contract price is its transport part, the rest is added from spot at quote time."""
    best = row.get("best") or {}
    parsed = row.get("parsed") or {}
    out = dict(row)

    def place(side):
        req = (row.get("request") or {}).get(side)
        p = parsed.get(side) or {}
        if isinstance(req, dict) and (req.get("unlocode") or req.get("locode")):
            # the place the tender lane was actually priced for (what a later quote resolves to as well)
            return {**req, "name": req.get("name") or p.get("city") or p.get("text")}
        if p.get("country") or p.get("unlocode"):
            return {"country": p.get("country") or (p.get("unlocode") or "")[:2], "locode": p.get("unlocode"),
                    "name": p.get("city") or p.get("text"), "_query": p.get("city") or p.get("text")}
        return (row.get("request") or {}).get(side) or p.get("city") or p.get("text")
    out["origin"], out["destination"] = row.get("origin") or place("origin"), row.get("destination") or place("destination")
    out.setdefault("mode", best.get("mode") or parsed.get("mode"))
    out["service"] = row.get("service") or best.get("service") or None
    cs = parsed.get("containers") or []
    if not row.get("equipment_type") and len({c.get("type") for c in cs}) == 1:
        out["equipment_type"] = cs[0]["type"]
    lines = best.get("lines") or []
    transport = [ln for ln in lines if ln.get("stage") in TRANSPORT_STAGES and not ln.get("is_pass_through")
                 and ln.get("code") not in CARGO_LINE_CODES]
    qty = float(best.get("unit_qty") or 1) or 1.0
    unit = _TENDER_UNIT.get(best.get("unit") or "")
    if transport and unit:
        total = sum(int(ln.get("amount_cents") or 0) for ln in transport)
        if best.get("unit") == "per_trailer":
            qty = 1.0  # contract bases have no per-trailer unit: the lane price is the whole trailer booking
        out.update(price_cents=int(round(total / qty)), basis=unit, surcharges_included=True)
    elif best.get("price_per_unit_cents") and unit and "price_cents" not in row:
        out.update(price_cents=int(best["price_per_unit_cents"]), basis=unit)
    out.setdefault("valid_until", best.get("valid_until"))
    out.setdefault("notes", parsed.get("notes") or row.get("ref"))
    return out


async def create_from_tender(tender: dict, lanes: list[dict] | None, actor: dict | None, *, activate_now: bool = True) -> dict:
    """Interface (shared/interfaces.md s7): contract tariff for an awarded tender. Tolerant of tender lane shapes:
    origin/destination as dict {country, locode|unlocode, postcode_prefix|postcode} or text ("FI", "FIHEL", "Turku");
    mode or service; equipment_type|equipment|container_type; price_cents|unit_price_cents|sell_cents or EUR in
    price_eur|unit_price_eur|unit_price|price|rate; basis|price_basis|unit; min_cents|minimum_cents; surcharges_included
    (default True: tender prices come from our all-in spot pricing); valid_until on the lane or the tender.
    Lanes that cannot be read are listed in tariff.import_errors. Idempotent per tender."""
    if not tender or not tender.get("company_id"):
        raise ValueError("The tender has no customer company; link it to a company before the award.")
    existing = await db.customer_tariffs.find_one({"tender_id": tender.get("id"), "status": {"$in": ["draft", "active"]}}, {"_id": 0}) if tender.get("id") else None
    if existing:
        return existing
    rows = lanes if lanes is not None else next((tender[k] for k in TENDER_LANE_KEYS if isinstance(tender.get(k), list)), [])
    good, errors, valid_untils = [], [], []
    for i, row in enumerate(rows or []):
        try:
            if row.get("best") or row.get("parsed"):
                row = _flatten_tender_lane(row)
            if row.get("awarded") is False or row.get("status") in ("lost", "declined", "no_bid"):
                continue
            service = str(row.get("service") or row.get("service_type") or "").upper() or None
            o = await _tender_place(row.get("origin"))
            d = await _tender_place(row.get("destination"))
            if not o or not d:
                raise TariffError("origin or destination not recognised")
            price = _cents_from(row, "price_cents", "unit_price_cents", "sell_cents", "unit_sell_cents", "price_eur",
                                "unit_price_eur", "unit_price", "price", "rate")
            lane = validate_lane({
                "origin": o, "destination": d, "mode": row.get("mode"), "service": service,
                "equipment_type": row.get("equipment_type") or row.get("equipment") or row.get("container_type"),
                "basis": _basis_from(row, service), "price_cents": price,
                "min_cents": _cents_from(row, "min_cents", "minimum_cents", "min_eur"),
                "surcharges_included": row.get("surcharges_included", tender.get("surcharges_included", True)),
                "transit_days": row.get("transit_days") if isinstance(row.get("transit_days"), int) else None,
                "notes": row.get("notes") or row.get("note"),
                "tender_lane_id": row.get("id") or row.get("lane_id") or row.get("ref")})
            good.append(lane)
            if row.get("valid_until"):
                valid_untils.append(str(row["valid_until"])[:10])
        except (TariffError, ValueError, TypeError, KeyError) as exc:
            errors.append({"row": i + 1, "lane": row.get("id") or row.get("ref"), "error": str(exc)})
    if not good:
        raise ValueError("No tender lane could be turned into a contract lane: " + "; ".join(e["error"] for e in errors[:5]))
    vt = tender.get("valid_until") or tender.get("valid_to") or (max(valid_untils) if valid_untils else None)
    vf = tender.get("valid_from") or tender.get("contract_start") or now().date().isoformat()
    t = await create_tariff({"company_id": tender["company_id"], "name": f"Tender {tender.get('number') or tender.get('name') or ''}".strip(),
                             "valid_from": vf, "valid_to": vt, "lanes": good, "exclusive": bool(tender.get("exclusive", False)),
                             "notes": tender.get("notes")}, actor, source="tender", tender_id=tender.get("id"))
    if errors:
        await db.customer_tariffs.update_one({"id": t["id"]}, {"$set": {"import_errors": errors}})
        t["import_errors"] = errors
    if activate_now:
        t = await activate(t["id"], actor)
    return t
