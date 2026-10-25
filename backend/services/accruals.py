"""Cost accruals (kuluvaraukset): the expected purchase cost of every shipment, per supplier, from the booked rate.

One `cost_accruals` row per shipment + supplier (contract: shared/interfaces.md, s13). Source of the numbers:

1. The shipment's booked quote option (shipment.quote_id/option_id, else quote.accepted_option_id): every line with a
   cost (cost_cents, or unit_cost_cents x qty) that is our own purchase. Pass-through lines (duty, import VAT,
   insurance premium re-charged at cost) are disbursements, not our cost, and lines an explicit third party pays are
   skipped. Rows carry `basis: "booked_rate"`.
2. Shipments booked without a quote (ops-created, the demo seed): the same lane and cargo are re-priced with V2's
   rate cards (services.pricing.price_request); the option of the shipment's main-leg carrier is used, else the
   cheapest. Rows carry `basis: "repriced_rate_card"` and the rate card ids.
3. Nothing to price from: one row with status `no_booked_rate` and no amounts; finance is notified. Numbers are never
   invented.

Lines are grouped by supplier: line.leg_seq -> option leg / shipment leg -> carrier; lines without a leg go to the
main-leg carrier (else the option's carrier). Amounts are converted to EUR at the ECB rate of the booking date.
Re-running is idempotent: open rows are updated in place, matched/disputed rows are never touched.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, ROUND_HALF_UP

from core.audit import audit
from core.db import db, register_index
from core.events import emit, on
from core.lifecycle import on_startup
from core.notify import bi, notify_role
from core.util import NO_ID, new_id, now_iso
from services import finance_core as fc
from services.fx import FxUnavailable, to_eur

log = logging.getLogger("finance.accruals")

register_index("cost_accruals", "id", unique=True)
register_index("cost_accruals", [("shipment_id", 1), ("supplier_key", 1)], unique=True)
register_index("cost_accruals", [("status", 1), ("period", 1)])
register_index("cost_accruals", "supplier.carrier_id")

ACCRUAL_STATUSES = ["open", "matched", "partially_matched", "disputed", "released", "written_off", "no_booked_rate"]
SUPPLIER_KINDS = ("carrier", "agent", "customs", "other")


def _norm_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def supplier_key(supplier: dict) -> str:
    if supplier.get("carrier_id"):
        return f"carrier:{supplier['carrier_id']}"
    if supplier.get("name"):
        return f"name:{_norm_name(supplier['name'])}"
    return "unknown"


def _line_cost(ln: dict) -> tuple[int, float, int]:
    """(amount_cents, qty, unit_cost_cents) of our purchase cost on a quote line."""
    qty = ln.get("qty")
    try:
        qty = float(qty) if qty not in (None, "") else 1.0
    except (TypeError, ValueError):
        qty = 1.0
    unit = int(ln.get("unit_cost_cents") or 0)
    if ln.get("cost_cents") not in (None, ""):
        amount = int(ln["cost_cents"])
    else:
        amount = int((Decimal(unit) * Decimal(str(qty))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return amount, qty, unit


def _third_party(ln: dict) -> bool:
    return (ln.get("payer") or "").strip().lower() in ("third_party", "third party")


def _main_leg(legs: list[dict]) -> dict | None:
    if not legs:
        return None
    for lg in legs:
        if lg.get("kind") == "main":
            return lg
    for lg in legs:
        if lg.get("role") == "main":
            return lg
    return max(legs, key=lambda lg: lg.get("distance_km") or 0)


def _leg_carrier(leg: dict | None) -> tuple[str | None, str | None]:
    if not leg:
        return None, None
    return leg.get("carrier_id"), leg.get("carrier_name") or leg.get("carrier")


async def _supplier(carrier_id: str | None, name: str | None, cache: dict) -> dict:
    kind = "carrier"
    if carrier_id:
        if carrier_id not in cache:
            cache[carrier_id] = await db.carriers.find_one({"id": carrier_id}, {"_id": 0, "name": 1, "kind": 1,
                                                                                "type": 1, "country": 1}) or {}
        row = cache[carrier_id]
        name = name or row.get("name")
        k = row.get("kind") or row.get("type")
        if k in SUPPLIER_KINDS:
            kind = k
    if not carrier_id and not name:
        kind = "other"
    return {"carrier_id": carrier_id, "name": name, "kind": kind}


async def _booked_option(shipment: dict) -> tuple[dict | None, dict | None]:
    if not shipment.get("quote_id"):
        return None, None
    quote = await db.quotes.raw.find_one({"id": shipment["quote_id"]}, NO_ID)
    if not quote:
        return None, None
    option_id = shipment.get("option_id") or quote.get("accepted_option_id")
    options = quote.get("options") or []
    option = next((o for o in options if o.get("id") == option_id), None)
    if option is None and len(options) == 1:
        option = options[0]
    return quote, option


def _price_request_for(shipment: dict) -> dict:
    """V2 quote request rebuilt from the shipment as booked (lane, mode, cargo, incoterm)."""
    def loc(p: dict | None) -> dict | None:
        p = p or {}
        if p.get("lat") is not None and p.get("lon") is not None and p.get("country"):
            return {k: p.get(k) for k in ("unlocode", "name", "lat", "lon", "country", "kinds") if p.get(k) is not None}
        return {"unlocode": p.get("unlocode")} if p.get("unlocode") else ({"query": p.get("name")} if p.get("name") else None)

    cargo = dict(shipment.get("cargo") or {})
    if not cargo.get("weight_kg") and shipment.get("weight_kg"):
        cargo["weight_kg"] = shipment["weight_kg"]
    mode = shipment.get("mode")
    return {"mode": mode if mode in ("sea", "road", "air", "rail") else None, "origin": loc(shipment.get("origin")),
            "destination": loc(shipment.get("destination")), "cargo": cargo, "incoterm": shipment.get("incoterm"),
            "incoterm_place": shipment.get("incoterm_place"), "customs_clearance": False}


async def _repriced_option(shipment: dict) -> tuple[dict | None, str | None]:
    from services.pricing import price_request

    company = await db.companies.raw.find_one({"id": shipment.get("company_id")}, NO_ID) if shipment.get("company_id") else None
    try:
        options = await price_request(_price_request_for(shipment), company=company)
    except Exception as exc:  # noqa: BLE001 - QuoteInputError and friends: there is no rate to accrue from
        return None, str(exc)[:300]
    options = [o for o in options or [] if o.get("lines")]
    if not options:
        return None, "No rate card covers this lane and cargo."
    main = _main_leg(shipment.get("legs") or [])
    cid, cname = _leg_carrier(main)
    pick = None
    if cid:
        pick = next((o for o in options if o.get("carrier_id") == cid), None)
    if pick is None and cname:
        pick = next((o for o in options if _norm_name(o.get("carrier_name")) == _norm_name(cname)), None)
    if pick is None:
        pick = min(options, key=lambda o: o.get("cost_cents") if o.get("cost_cents") is not None else 1 << 62)
    return pick, None


def _booking_date(shipment: dict) -> str:
    for m in shipment.get("milestones") or []:
        if m.get("code") == "booked" and m.get("at"):
            return str(m["at"])[:10]
    return str(shipment.get("created_at") or now_iso())[:10]


async def _group_lines(shipment: dict, option: dict) -> dict[str, dict]:
    opt_legs = {lg.get("seq"): lg for lg in option.get("legs") or [] if lg.get("seq") is not None}
    shp_legs = {lg.get("seq"): lg for lg in shipment.get("legs") or [] if lg.get("seq") is not None}
    main = _main_leg(option.get("legs") or []) or _main_leg(shipment.get("legs") or [])
    main_cid, main_name = _leg_carrier(main)
    if not main_cid and not main_name:
        main_cid, main_name = option.get("carrier_id"), option.get("carrier_name")
    cache: dict = {}
    groups: dict[str, dict] = {}
    for ln in option.get("lines") or []:
        if ln.get("is_pass_through") or _third_party(ln):
            continue
        amount, qty, unit = _line_cost(ln)
        if amount <= 0:
            continue
        seq = ln.get("leg_seq")
        leg = (opt_legs.get(seq) or shp_legs.get(seq)) if seq is not None else None
        cid, cname = _leg_carrier(leg) if leg else (None, None)
        if not cid and not cname:
            cid, cname = main_cid, main_name
        sup = await _supplier(cid, cname, cache)
        key = supplier_key(sup)
        g = groups.setdefault(key, {"supplier": sup, "lines": [], "rate_card_ids": set()})
        if leg and leg.get("rate_card_id"):
            g["rate_card_ids"].add(leg["rate_card_id"])
        g["lines"].append({"code": ln.get("code"), "description": ln.get("description_fi") or ln.get("description"),
                           "leg_seq": seq, "stage": ln.get("stage"), "qty": qty, "unit_cost_cents": unit,
                           "amount_cents": amount, "currency": (ln.get("currency") or "EUR").upper(),
                           "is_pass_through": False})
    if option.get("rate_card_id"):
        for g in groups.values():
            if g["supplier"].get("carrier_id") == option.get("carrier_id"):
                g["rate_card_ids"].add(option["rate_card_id"])
    return groups


async def _row_amounts(lines: list[dict], on: str) -> tuple[str, int, int, dict]:
    currencies = {ln["currency"] for ln in lines}
    eur_total, fx = 0, {}
    for cur in sorted(currencies):
        cents = sum(ln["amount_cents"] for ln in lines if ln["currency"] == cur)
        eur, f = await to_eur(cents, cur, on)
        eur_total += eur
        if cur != "EUR" or not fx:
            fx = {"currency": f["currency"], "rate": f["rate"], "rate_date": f["rate_date"], "source": f["source"]}
    if len(currencies) == 1:
        cur = next(iter(currencies))
        return cur, sum(ln["amount_cents"] for ln in lines), eur_total, fx
    return "EUR", eur_total, eur_total, {**fx, "mixed_currencies": sorted(currencies)}


def _base_row(shipment: dict, quote: dict | None, option: dict | None) -> dict:
    created = shipment.get("created_at") or now_iso()
    row = {"shipment_id": shipment["id"], "shipment_ref": shipment.get("reference"),
           "company_id": shipment.get("company_id"), "quote_id": (quote or {}).get("id"),
           "option_id": (option or {}).get("id") if quote else None, "lane": fc.lane_of(shipment),
           "period": fc.period_of(_booking_date(shipment) if shipment.get("milestones") else created)}
    if shipment.get("demo"):
        row["demo"] = True
    if shipment.get("test"):
        row["test"] = True
    return row


async def accrue_shipment(shipment_id: str) -> list[dict]:
    """Accrue (or refresh) the expected cost rows of one shipment. Idempotent; returns the shipment's rows."""
    shipment = await db.shipments.raw.find_one({"id": shipment_id}, NO_ID)
    if not shipment:
        raise ValueError(f"Shipment {shipment_id} not found")
    if shipment.get("status") == "cancelled":
        await release_shipment(shipment_id, reason="cancelled")
        return await db.cost_accruals.find({"shipment_id": shipment_id}, NO_ID).to_list(50)
    quote, option = await _booked_option(shipment)
    basis, problem = "booked_rate", None
    if not option:
        option, problem = await _repriced_option(shipment)
        basis = "repriced_rate_card"
        quote = None
    missing = [ln.get("code") or "unnamed" for ln in (option or {}).get("lines", [])
               if not ln.get("is_pass_through") and not _third_party(ln)
               and ln.get("cost_cents") is None and ln.get("unit_cost_cents") is None]
    await db.shipments.raw.update_one({"id": shipment_id}, {"$set": {
        "finance_cost_incomplete": bool(missing or not option), "finance_missing_cost_lines": missing}})
    groups = await _group_lines(shipment, option) if option else {}
    on = _booking_date(shipment)
    now = now_iso()
    touched: list[dict] = []
    existing = {r["supplier_key"]: r for r in await db.cost_accruals.find({"shipment_id": shipment_id}, NO_ID).to_list(50)}
    if not groups:
        reason = problem or ("The booked quote option has no cost lines." if basis == "booked_rate"
                             else "The re-priced option has no cost lines.")
        main = _main_leg(shipment.get("legs") or [])
        cid, cname = _leg_carrier(main)
        sup = await _supplier(cid, cname, {})
        key = supplier_key(sup)
        prev = existing.get(key)
        if prev and prev["status"] not in ("open", "no_booked_rate"):
            return list(existing.values())
        row = {**_base_row(shipment, quote, option), "supplier": sup, "supplier_key": key, "lines": [],
               "currency": "EUR", "expected_cents": None, "expected_eur_cents": None, "fx": {},
               "status": "no_booked_rate", "basis": None, "reason": reason, "actual_eur_cents": None,
               "variance_eur_cents": None, "carrier_invoice_ids": (prev or {}).get("carrier_invoice_ids", []),
               "updated_at": now}
        if prev:
            await db.cost_accruals.update_one({"id": prev["id"]}, {"$set": row})
            row = {**prev, **row}
        else:
            row.update(id=new_id(), created_at=now)
            await db.cost_accruals.insert_one(dict(row))
            await fc.agent_decide(
                "accrue_shipment", subject=("shipment", shipment_id), subject_label=shipment.get("reference") or shipment_id,
                inputs={"basis": basis, "reason": reason}, decision={"status": "no_booked_rate"}, approval_kind=None,
                company_id=shipment.get("company_id"),
                title="No booked rate to accrue", title_fi="Ei kuluvarausta: hinta puuttuu")
            await notify_role("finance", bi("Kuluvaraus puuttuu", "Cost accrual missing"),
                              bi(f"{shipment.get('reference')}: ei varattua hintaa eikä hinnastoa ({reason})",
                                 f"{shipment.get('reference')}: no booked rate and no rate card ({reason})"),
                              link="/ops/finance/accruals")
        await emit("finance.accrued", {"accrual": row})
        return [row]
    fx_missing = []
    for key, g in groups.items():
        prev = existing.get(key)
        if prev and prev["status"] not in ("open", "no_booked_rate"):
            touched.append(prev)
            continue
        reason = None
        try:
            currency, expected, expected_eur, fx = await _row_amounts(g["lines"], on)
        except FxUnavailable as exc:
            log.warning("accrual %s %s: %s", shipment.get("reference"), key, exc)
            fx_missing.append(key)
            currency, expected, expected_eur, fx = "EUR", None, None, {}
            reason = "Valuuttakurssi puuttuu: " + str(exc)
        row = {**_base_row(shipment, quote, option), "supplier": g["supplier"], "supplier_key": key,
               "lines": g["lines"], "currency": currency, "expected_cents": expected, "expected_eur_cents": expected_eur,
               "fx": fx, "status": "no_booked_rate" if reason else "open", "basis": basis, "rate_card_ids": sorted(g["rate_card_ids"]),
               "reason": reason, "updated_at": now}
        if prev:
            await db.cost_accruals.update_one({"id": prev["id"]}, {"$set": row})
            row = {**prev, **row}
        else:
            row.update(id=new_id(), created_at=now, actual_eur_cents=None, variance_eur_cents=None,
                       carrier_invoice_ids=[])
            await db.cost_accruals.insert_one(dict(row))
        row.pop("_id", None)
        touched.append(row)
        await emit("finance.accrued", {"accrual": row})
    await db.shipments.raw.update_one({"id": shipment_id}, {"$set": {
        "finance_cost_incomplete": bool(missing or fx_missing), "finance_missing_fx_suppliers": fx_missing}})
    # Suppliers no longer on the booked rate: release their open rows (never matched ones).
    for key, prev in existing.items():
        if key not in groups and prev["status"] in ("open", "no_booked_rate"):
            await db.cost_accruals.update_one({"id": prev["id"]}, {"$set": {"status": "released", "updated_at": now,
                                                                             "reason": "supplier no longer on the booked rate"}})
    await audit(fc.AGENT_ACTOR, "finance_accrued", "shipment", shipment_id,
                {"basis": basis, "rows": [{"supplier": r["supplier"].get("name"), "expected_eur_cents": r.get("expected_eur_cents")}
                                          for r in touched]}, company_id=shipment.get("company_id"))
    return touched


async def release_shipment(shipment_id: str, *, reason: str) -> int:
    res = await db.cost_accruals.update_many({"shipment_id": shipment_id, "status": {"$in": ["open", "no_booked_rate"]}},
                                             {"$set": {"status": "released", "reason": reason, "updated_at": now_iso()}})
    return res.modified_count


async def rebuild(limit: int = 500) -> dict:
    """Accrue every shipment that has no accrual rows yet (oldest first)."""
    have = set(await db.cost_accruals.distinct("shipment_id"))
    out = {"processed": 0, "rows": 0, "no_booked_rate": 0, "booked_rate": 0, "repriced_rate_card": 0, "errors": []}
    cursor = db.shipments.raw.find({"status": {"$ne": "cancelled"}}, {"_id": 0, "id": 1}).sort("created_at", 1)
    async for s in cursor:
        if s["id"] in have:
            continue
        if out["processed"] >= limit:
            break
        out["processed"] += 1
        try:
            rows = await accrue_shipment(s["id"])
        except Exception as exc:  # noqa: BLE001 - one bad shipment must not stop the backfill
            log.exception("accrue %s failed", s["id"])
            out["errors"].append({"shipment_id": s["id"], "error": str(exc)[:300]})
            continue
        out["rows"] += len(rows)
        for r in rows:
            if r.get("status") == "no_booked_rate":
                out["no_booked_rate"] += 1
            elif r.get("basis") in ("booked_rate", "repriced_rate_card"):
                out[r["basis"]] += 1
    return out


# ------------------------------------------------------------------------------------------- jobs and events
@fc.finance_job("finance.accrue_shipment", label_fi="Kuluvaraus toimitukselle", label_en="Accrue shipment cost")
async def accrue_shipment_job(job: dict):
    rows = await accrue_shipment((job.get("payload") or {})["shipment_id"])
    return {"rows": len(rows), "statuses": sorted({r.get("status") for r in rows})}


@on("shipment.created")
async def _on_shipment_created(payload: dict):
    s = payload.get("shipment") or {}
    if s.get("id"):
        await fc.enqueue("finance.accrue_shipment", {"shipment_id": s["id"]}, key=f"accrue:{s['id']}",
                         company_id=s.get("company_id"), subject={"type": "shipment", "id": s["id"]})


@on("shipment.status_changed")
async def _on_status_changed(payload: dict):
    if payload.get("new_status") != "cancelled":
        return
    s = payload.get("shipment") or {}
    if s.get("id"):
        n = await release_shipment(s["id"], reason="shipment cancelled")
        if n:
            await audit(fc.AGENT_ACTOR, "finance_accruals_released", "shipment", s["id"], {"rows": n},
                        company_id=s.get("company_id"))


@on_startup(order=60)
async def _backfill_once():
    if await db.cost_accruals.estimated_document_count() > 0:
        return
    if await db.shipments.raw.estimated_document_count() == 0:
        return
    res = await rebuild(limit=2000)
    log.info("cost accrual backfill: %s", {k: v for k, v in res.items() if k != "errors"})
