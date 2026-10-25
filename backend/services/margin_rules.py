"""Margin rules: which markup and which minimum own margin (floor) a quote option gets.

    r = await resolve_margin(req, option, company, settings)
    # {"margin_pct", "floor_cents", "rule_id", "rule_name", "source": "rule"|"company"|"default", ...}
    ok, reasons = await margin_floor_ok(quote)

Collection `margin_rules` {id, name, active, priority, scope{mode, service, origin_country, destination_country, lane,
company_id, size{metric, min, max}}, margin_pct, floor_cents, floor_basis, floor_min_cents, cap_pct, valid_from, valid_to,
note, default_key}.

Resolution (one option at a time, in services.pricing._finish_option):
1. Candidates: active rules inside valid_from..valid_to (inclusive, UTC date) whose every set scope dimension matches the
   option. Lane "FI>SE" matches on countries, "FIHEL>DEHAM" on UN/LOCODEs (request place or the main leg's port).
   Size bands are min <= value < max on chargeable kg, TEU, loading metres or m³.
2. The most specific rule wins: most matched dimensions first, then company > lane > origin/destination country >
   service > mode > size (a service is a narrower mode), then the higher priority, then the newest rule.
3. No rule: the company's margin_pct, else settings default_margin_pct, floor 0.
Sell = cost x (1 + margin_pct/100); the floor then raises the non-pass-through lines proportionally until own margin >=
floor (floor = floor_cents x units of floor_basis, at least floor_min_cents; cap_pct, when set, caps the raise at
cost x cap_pct/100 so a small shipment never gets an absurd margin).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

from core.db import db, register_index

MODES = ("sea", "road", "air", "rail")
SIZE_METRICS = ("chargeable_kg", "teu", "ldm", "cbm")
FLOOR_BASES = ("PER_SHIPMENT", "PER_CONTAINER", "PER_TEU", "PER_KG", "PER_LDM")
SCOPE_KEYS = ("mode", "service", "origin_country", "destination_country", "lane", "company_id", "size")
LANE_RX = re.compile(r"^([A-Z]{2}|[A-Z]{2}[A-Z0-9]{3})>([A-Z]{2}|[A-Z]{2}[A-Z0-9]{3})$")

register_index("margin_rules", "id", unique=True)
register_index("margin_rules", [("active", 1), ("priority", -1)])


def _today() -> date:
    return datetime.now(timezone.utc).date()


def in_validity(rule: dict, day: date) -> bool:
    vf, vt = rule.get("valid_from"), rule.get("valid_to")
    return not ((vf and date.fromisoformat(vf[:10]) > day) or (vt and date.fromisoformat(vt[:10]) < day))


def _main_leg(option: dict) -> dict:
    return next((lg for lg in option.get("legs") or [] if lg.get("kind") == "main"), {}) or {}


def _side_matches(code: str, place: dict, leg_end: dict | None) -> bool:
    if len(code) == 2:
        return (place or {}).get("country") == code
    return code in {(place or {}).get("unlocode"), (leg_end or {}).get("unlocode")}


def lane_matches(lane: str, req: dict, option: dict) -> bool:
    m = LANE_RX.match(lane or "")
    if not m:
        return False
    leg = _main_leg(option)
    return _side_matches(m.group(1), req.get("origin"), leg.get("from")) and _side_matches(m.group(2), req.get("destination"), leg.get("to"))


def option_metrics(req: dict, option: dict) -> dict:
    """Chargeable kg, TEU, loading metres, m³ and container count of this option's cargo."""
    from services import pricing

    cargo = req.get("cargo") or {}
    m = pricing.cargo_metrics(cargo)
    containers = cargo.get("containers") or option.get("suggested_containers") or []
    teu = sum(pricing.CONTAINERS[c["type"]]["teu"] * c["count"] for c in containers if c.get("type") in pricing.CONTAINERS)
    service = option.get("service") or ""
    w = m.get("weight_kg")
    if service == "AIR" or service == "COURIER":
        kg = pricing.chargeable("AIR", m)["chargeable_kg"] if w else None
    elif service in ("ROAD_LTL", "ROAD_FTL", "RORO"):
        kg = pricing.chargeable("HAULAGE_LTL", m)["chargeable_kg"] if w else None
    elif service == "LCL":
        kg = pricing.chargeable("LCL", m)["wm"] * 1000 if (w or m.get("volume_m3")) else None
    else:
        kg = w
    return {"chargeable_kg": kg, "teu": teu or None, "ldm": m.get("ldm"), "cbm": m.get("volume_m3"),
            "containers": sum(c["count"] for c in containers) or None}


def _size_matches(size: dict, metrics: dict) -> bool:
    v = metrics.get(size.get("metric"))
    if v is None:
        return False
    if size.get("min") is not None and v < float(size["min"]):
        return False
    return not (size.get("max") is not None and v >= float(size["max"]))


def match_dims(rule: dict, req: dict, option: dict, company: dict | None, metrics: dict) -> set[str] | None:
    """Scope dimensions the rule sets, or None when one of them does not match."""
    s = rule.get("scope") or {}
    dims = set()
    for key in SCOPE_KEYS:
        want = s.get(key)
        if want in (None, "", {}):
            continue
        if key == "mode":
            ok = option.get("mode") == want
        elif key == "service":
            ok = option.get("service") == want
        elif key == "origin_country":
            ok = (req.get("origin") or {}).get("country") == want
        elif key == "destination_country":
            ok = (req.get("destination") or {}).get("country") == want
        elif key == "lane":
            ok = lane_matches(want, req, option)
        elif key == "company_id":
            ok = bool(company) and company.get("id") == want
        else:
            ok = _size_matches(want, metrics)
        if not ok:
            return None
        dims.add(key)
    return dims


def specificity(rule: dict, dims: set[str]) -> tuple:
    return (len(dims), "company_id" in dims, "lane" in dims,
            ("origin_country" in dims) + ("destination_country" in dims), "service" in dims, "mode" in dims,
            "size" in dims, int(rule.get("priority") or 0), rule.get("created_at") or "")


def floor_for(rule: dict, metrics: dict) -> int:
    per = int(rule.get("floor_cents") or 0)
    basis = rule.get("floor_basis") or "PER_SHIPMENT"
    units = {"PER_SHIPMENT": 1, "PER_CONTAINER": metrics.get("containers"), "PER_TEU": metrics.get("teu"),
             "PER_KG": metrics.get("chargeable_kg"), "PER_LDM": metrics.get("ldm")}.get(basis)
    amount = int(math.ceil(per * float(units))) if units else 0
    return max(amount, int(rule.get("floor_min_cents") or 0))


async def active_rules(day: date | None = None) -> list[dict]:
    day = day or _today()
    rules = await db.margin_rules.find({"active": True}, {"_id": 0}).to_list(2000)
    return [r for r in rules if in_validity(r, day)]


def fallback(company: dict | None, settings: dict) -> dict:
    if (company or {}).get("margin_pct"):
        return {"margin_pct": float(company["margin_pct"]), "floor_cents": 0, "rule_id": None, "rule_name": None,
                "source": "company", "cap_pct": None, "matched": []}
    return {"margin_pct": float(settings["default_margin_pct"]), "floor_cents": 0, "rule_id": None, "rule_name": None,
            "source": "default", "cap_pct": None, "matched": []}


def pick_rule(rules: list[dict], req: dict, option: dict, company: dict | None, settings: dict) -> dict:
    metrics = option_metrics(req, option)
    best, best_key, best_dims = None, None, None
    for r in rules:
        dims = match_dims(r, req, option, company, metrics)
        if dims is None:
            continue
        key = specificity(r, dims)
        if best_key is None or key > best_key:
            best, best_key, best_dims = r, key, dims
    if not best:
        return fallback(company, settings)
    return {"margin_pct": float(best["margin_pct"]), "floor_cents": floor_for(best, metrics), "rule_id": best["id"],
            "rule_name": best.get("name"), "source": "rule", "cap_pct": best.get("cap_pct"), "matched": sorted(best_dims),
            "floor_basis": best.get("floor_basis") or "PER_SHIPMENT"}


async def resolve_margin(req: dict, option: dict, company: dict | None, settings: dict) -> dict:
    """Interface (shared/interfaces.md s7): the markup and floor for one option."""
    return pick_rule(await active_rules(), req, option, company, settings)


def _own(lines: list[dict]) -> list[dict]:
    return [ln for ln in lines if not ln.get("is_pass_through")]


def own_margin(lines: list[dict]) -> int:
    return sum(ln["amount_cents"] - ln["cost_cents"] for ln in _own(lines))


def apply_floor(lines: list[dict], floor_cents: int, cap_pct: float | None = None) -> int:
    """Raise the charged non-pass-through lines proportionally so own margin >= the floor. Returns the effective floor
    (the floor, or less when cap_pct limits it); lines raised carry floor_raised_cents."""
    own = _own(lines)
    target = int(floor_cents or 0)
    if cap_pct is not None:
        target = min(target, int(sum(ln["cost_cents"] for ln in own) * float(cap_pct) / 100))
    margin = own_margin(lines)
    if target <= 0 or margin >= target:
        return max(target, 0)
    pool = [ln for ln in own if ln.get("unit_cost_cents", 0) > 0 and ln.get("qty")] or [ln for ln in own if ln.get("qty")]
    if not pool:
        return target
    base = sum(ln["amount_cents"] for ln in pool)
    gap = target - margin
    before = {id(ln): ln["amount_cents"] for ln in pool}
    for ln in pool:  # proportional share, rounded down; the finest-step line takes the last cents
        share = gap * ln["amount_cents"] / base if base > 0 else gap / len(pool)
        ln["unit_price_cents"] = int(ln["unit_price_cents"] + math.floor(share / ln["qty"]))
        ln["amount_cents"] = int(round(ln["unit_price_cents"] * ln["qty"]))
    finest = min(pool, key=lambda ln: ln["qty"])
    for _ in range(100_000):
        short = target - own_margin(lines)
        if short <= 0:
            break
        finest["unit_price_cents"] += max(1, math.floor(short / finest["qty"]))
        finest["amount_cents"] = int(round(finest["unit_price_cents"] * finest["qty"]))
    for ln in pool:
        if ln["amount_cents"] != before[id(ln)]:
            ln["floor_raised_cents"] = ln["amount_cents"] - before[id(ln)]
    return target


# ------------------------------------------------------------------------------------------------
# Autopilot guard
# ------------------------------------------------------------------------------------------------
def _reason(code: str, fi: str, en: str) -> dict:
    return {"code": code, "fi": fi, "en": en}


def _eur(c: int | None) -> str:
    return f"{(c or 0) / 100:,.2f}".replace(",", " ")


async def margin_floor_ok(quote: dict) -> tuple[bool, list[dict]]:
    """Every option meets its floor and has a known cost. Options priced before rules existed (or added by hand) are
    checked against the rule that would apply to them now."""
    from core.settings import get_settings

    reasons: list[dict] = []
    req = quote.get("request") or {}
    company = await db.companies.find_one({"id": quote["company_id"]}, {"_id": 0}) if quote.get("company_id") else None
    settings = rules = None
    for o in quote.get("options") or []:
        label = f"{o.get('carrier_name') or o.get('mode')} {o.get('service_name') or o.get('service') or ''}".strip()
        if o.get("cost_basis") == "unknown" or type(o.get("cost_cents")) is not int:
            reasons.append(_reason("cost_unknown", f"Vaihtoehdon {label} ostohinta ei ole tiedossa, joten katetta ei voi tarkistaa.",
                                   f"Option {label} has no known cost, so its margin cannot be checked."))
            continue
        floor = o.get("margin_floor_cents")
        if floor is None:
            if rules is None:
                settings, rules = await get_settings(), await active_rules()
            floor = pick_rule(rules, req, o, company, settings)["floor_cents"] if req.get("cargo") is not None else 0
        margin = int(o.get("margin_cents") or 0)
        if margin < 0:
            reasons.append(_reason("margin_negative", f"Vaihtoehto {label} myydään alle ostohinnan (kate {_eur(margin)} EUR).",
                                   f"Option {label} sells below cost (margin {_eur(margin)} EUR)."))
        elif floor and margin < floor:
            reasons.append(_reason("margin_below_floor",
                                   f"Vaihtoehdon {label} oma kate {_eur(margin)} EUR alittaa katelattian {_eur(floor)} EUR.",
                                   f"Option {label} own margin {_eur(margin)} EUR is below the floor {_eur(floor)} EUR."))
    return (not reasons, reasons)


# ------------------------------------------------------------------------------------------------
# Validation and the starting policy
# ------------------------------------------------------------------------------------------------
class RuleError(ValueError):
    pass


def _pct(v, name: str, *, upper: float = 100.0, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v < upper:
        raise RuleError(f"{name} must be a number from 0 to below {upper:g}.")
    return float(v)


def _cents(v, name: str, *, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100_000_000:
        raise RuleError(f"{name} must be whole cents from 0 to 100 000 000.")
    return v


def _day(v, name: str):
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise RuleError(f"{name} must be a date (YYYY-MM-DD).") from None


async def validate_rule(body: dict, existing: dict | None = None) -> dict:
    """Full rule doc from a POST body, or the merged doc for a PATCH (existing given)."""
    doc = dict(existing or {})
    doc.update({k: v for k, v in body.items() if k in ("name", "active", "priority", "scope", "margin_pct", "floor_cents",
                                                        "floor_basis", "floor_min_cents", "cap_pct", "valid_from", "valid_to", "note")})
    name = str(doc.get("name") or "").strip()
    if not name or len(name) > 120:
        raise RuleError("Give the rule a name (at most 120 characters).")
    doc["name"] = name
    doc["active"] = bool(doc.get("active", True))
    pr = doc.get("priority", 0)
    if isinstance(pr, bool) or not isinstance(pr, int) or not -1000 <= pr <= 1000:
        raise RuleError("Priority must be a whole number from -1000 to 1000.")
    doc["priority"] = pr
    doc["margin_pct"] = _pct(doc.get("margin_pct"), "Margin %")
    doc["floor_cents"] = _cents(doc.get("floor_cents") or 0, "Floor")
    doc["floor_basis"] = doc.get("floor_basis") or "PER_SHIPMENT"
    if doc["floor_basis"] not in FLOOR_BASES:
        raise RuleError(f"Floor basis must be one of {', '.join(FLOOR_BASES)}.")
    doc["floor_min_cents"] = _cents(doc.get("floor_min_cents"), "Floor minimum", allow_none=True)
    doc["cap_pct"] = _pct(doc.get("cap_pct"), "Cap %", upper=1000.0, allow_none=True)
    if doc["cap_pct"] is not None and doc["cap_pct"] < doc["margin_pct"]:
        raise RuleError("Cap % must be at least the margin %.")
    doc["valid_from"], doc["valid_to"] = _day(doc.get("valid_from"), "Valid from"), _day(doc.get("valid_to"), "Valid to")
    if doc["valid_from"] and doc["valid_to"] and doc["valid_to"] < doc["valid_from"]:
        raise RuleError("Valid to must be on or after valid from.")
    doc["note"] = (str(doc.get("note") or "").strip()[:500]) or None
    scope_in = doc.get("scope") or {}
    if not isinstance(scope_in, dict):
        raise RuleError("Scope must be an object.")
    unknown = set(scope_in) - set(SCOPE_KEYS)
    if unknown:
        raise RuleError(f"Unknown scope field(s): {', '.join(sorted(unknown))}.")
    scope = {}
    if scope_in.get("mode"):
        mode = str(scope_in["mode"]).lower()
        if mode not in MODES:
            raise RuleError("Mode must be sea, road, air or rail.")
        scope["mode"] = mode
    if scope_in.get("service"):
        svc = str(scope_in["service"]).upper()
        if not re.fullmatch(r"[A-Z_]{2,20}", svc):
            raise RuleError("Service is a code such as FCL, LCL, AIR, ROAD_LTL, ROAD_FTL, RAIL_FCL or COURIER.")
        scope["service"] = svc
    for k in ("origin_country", "destination_country"):
        if scope_in.get(k):
            cc = str(scope_in[k]).upper()
            if not re.fullmatch(r"[A-Z]{2}", cc):
                raise RuleError("Countries are two-letter ISO codes, for example FI.")
            scope[k] = cc
    if scope_in.get("lane"):
        lane = str(scope_in["lane"]).upper().replace(" ", "")
        if not LANE_RX.match(lane):
            raise RuleError("Lane is ORIGIN>DESTINATION with countries or UN/LOCODEs, for example FI>SE or FIHEL>DEHAM.")
        scope["lane"] = lane
    if scope_in.get("company_id"):
        if not await db.companies.find_one({"id": scope_in["company_id"]}, {"id": 1}):
            raise RuleError("Unknown company.")
        scope["company_id"] = scope_in["company_id"]
    if scope_in.get("size"):
        size = scope_in["size"]
        if not isinstance(size, dict) or size.get("metric") not in SIZE_METRICS:
            raise RuleError(f"Size metric must be one of {', '.join(SIZE_METRICS)}.")
        lo, hi = size.get("min"), size.get("max")
        for v in (lo, hi):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise RuleError("Size min and max are numbers of at least 0.")
        if lo is None and hi is None:
            raise RuleError("A size band needs a min or a max.")
        if lo is not None and hi is not None and hi <= lo:
            raise RuleError("Size max must be above min.")
        scope["size"] = {"metric": size["metric"], "min": lo, "max": hi}
    doc["scope"] = scope
    return doc


# Starting policy, created only by POST /api/margin-rules/defaults. Figures are a forwarder's opening position
# (gross-margin ranges forwarders commonly target per product), not market data; tune them per lane and customer.
DEFAULT_RULES = [
    {"default_key": "courier", "name": "Pikalähetykset / Courier", "scope": {"service": "COURIER"}, "margin_pct": 12.0,
     "floor_cents": 1000, "floor_basis": "PER_SHIPMENT", "cap_pct": 60.0,
     "note": "Express parcels: thin markup on integrator rates, at least 10 EUR own margin, at most 60 % on cost."},
    {"default_key": "road_ltl", "name": "Kappaletavara / Road groupage", "scope": {"mode": "road", "service": "ROAD_LTL"},
     "margin_pct": 20.0, "floor_cents": 2500, "floor_basis": "PER_SHIPMENT", "cap_pct": 80.0,
     "note": "Groupage: 20 % on cost, at least 25 EUR per consignment."},
    {"default_key": "road_ftl", "name": "Täysi kuorma / Full truck", "scope": {"mode": "road", "service": "ROAD_FTL"},
     "margin_pct": 10.0, "floor_cents": 8000, "floor_basis": "PER_SHIPMENT",
     "note": "FTL: 10 % on cost, at least 80 EUR per truck booking."},
    {"default_key": "sea_fcl", "name": "Merikontti FCL / Sea FCL", "scope": {"mode": "sea", "service": "FCL"},
     "margin_pct": 10.0, "floor_cents": 7500, "floor_basis": "PER_CONTAINER",
     "note": "FCL: 10 % on cost, at least 75 EUR own margin per container."},
    {"default_key": "sea_lcl", "name": "Merirahti LCL / Sea LCL", "scope": {"mode": "sea", "service": "LCL"},
     "margin_pct": 22.0, "floor_cents": 4000, "floor_basis": "PER_SHIPMENT", "cap_pct": 100.0,
     "note": "LCL: 22 % on cost, at least 40 EUR per shipment."},
    {"default_key": "air", "name": "Lentorahti / Air freight", "scope": {"mode": "air"}, "margin_pct": 15.0,
     "floor_cents": 10, "floor_basis": "PER_KG", "floor_min_cents": 4000,
     "note": "Air: 15 % on cost, at least 0.10 EUR per chargeable kg and 40 EUR per shipment."},
    {"default_key": "rail_fcl", "name": "Junakontti / Rail container", "scope": {"mode": "rail"}, "margin_pct": 10.0,
     "floor_cents": 6000, "floor_basis": "PER_CONTAINER",
     "note": "Rail: 10 % on cost, at least 60 EUR per container."},
]
