"""Partner scorecards and carrier selection.

Every metric is computed from our own records (demo-seeded rows excluded) and says how many records it rests on. A
metric below its minimum sample is shown but left out of the score: no benchmark is presented as a measurement.

    on_time           delivered legs that arrived no later than 24 h after ETA            shipments.legs
    cost              the carrier's contract/partner card vs the lane median (100 = median) rate_cards
    claims            claims per 100 shipments on the carrier's legs                       claims -> shipments
    response          median hours from RFQ to the first reply                              partner_outreach
    invoice_accuracy  share of carrier invoices within the audit tolerance                  carrier_invoices (s13)

`rank_carriers()` is the deterministic carrier selection other agents call (s2): cost, then scorecard, then allocation.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta

from core.db import db, register_index
from core.lifecycle import background_loop
from core.record_provenance import operational_query
from core.util import NO_ID, now, now_iso, parse_iso

log = logging.getLogger("scorecards")

register_index("carrier_scorecards", "carrier_id", unique=True)

WEIGHTS = {"on_time": 0.30, "cost": 0.30, "claims": 0.15, "response": 0.10, "invoice_accuracy": 0.15}
MIN_SAMPLE = {"on_time": 5, "cost": 1, "claims": 5, "response": 3, "invoice_accuracy": 3}
LOOKBACK_DAYS = 365
STALE_HOURS = 24
PROVENANCE_VERSION = 2
AGENT_SERVICES = ("AGENT_ORIGIN", "AGENT_DESTINATION")
DEFAULT_SERVICE = {"sea": "FCL", "road": "ROAD_FTL", "air": "AIR", "rail": "RAIL_FCL"}
SERVICES_BY_MODE = {"sea": ["FCL", "LCL", "RORO"], "road": ["ROAD_FTL", "ROAD_LTL"], "air": ["AIR"], "rail": ["RAIL_FCL", "RAIL_WAGON"]}
LABELS = {
    "on_time": ("Täsmällisyys", "On time"), "cost": ("Hintataso", "Cost level"), "claims": ("Reklamaatiot", "Claims"),
    "response": ("Vastausaika", "Response time"), "invoice_accuracy": ("Laskujen oikeellisuus", "Invoice accuracy"),
}


def _metric(key: str, value, sample: int, source: str, score, detail: dict | None = None, note_fi: str = "", note_en: str = "") -> dict:
    enough = value is not None and sample >= MIN_SAMPLE[key]
    return {"key": key, "label_fi": LABELS[key][0], "label_en": LABELS[key][1], "value": value, "sample": sample,
            "min_sample": MIN_SAMPLE[key], "enough": enough, "score": round(score, 1) if enough and score is not None else None,
            "source": source, "detail": detail or {}, "note_fi": note_fi, "note_en": note_en}


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def ref_cost(card: dict) -> int | None:
    """One comparable number per card: all-in price, else the 40' container price, else the unit price, else tier 1."""
    if card.get("all_in_cost_cents"):
        return int(card["all_in_cost_cents"])
    cc = card.get("container_costs") or {}
    for k in ("40HC", "40GP", "45HC", "20GP"):
        if cc.get(k):
            return int(cc[k])
    if card.get("unit_cost_cents"):
        return int(card["unit_cost_cents"])
    tiers = card.get("tiers") or []
    return int(tiers[0]["unit_cost_cents"]) if tiers and tiers[0].get("unit_cost_cents") else None


def card_countries(card: dict) -> tuple[str, str]:
    def c(ep):
        return ((ep or {}).get("country") or ((ep or {}).get("unlocode") or "")[:2]).upper()
    return c(card.get("origin")), c(card.get("destination"))


def card_live(card: dict, day: date) -> bool:
    vf, vt = card.get("valid_from"), card.get("valid_to")
    return (card.get("status") in (None, "accepted") and not card.get("archived")
            and (not vf or vf[:10] <= day.isoformat()) and (not vt or day.isoformat() <= vt[:10]))


# ------------------------------------------------------------------------------------------------- metrics
async def _shipments(carrier_id: str, since: str) -> list[dict]:
    return await db.raw.shipments.find({**await operational_query(), "legs.carrier_id": carrier_id,
                                    "created_at": {"$gte": since}},
                                   {"_id": 0, "id": 1, "reference": 1, "legs": 1, "status": 1}).to_list(5000)


def _on_time(carrier_id: str, shipments: list[dict]) -> dict:
    total = on_time = 0
    late = []
    for s in shipments:
        for leg in s.get("legs") or []:
            if leg.get("carrier_id") != carrier_id or not leg.get("ata") or not leg.get("eta"):
                continue
            ata, eta = parse_iso(leg["ata"]), parse_iso(leg["eta"])
            if not ata or not eta:
                continue
            total += 1
            if (ata - eta).total_seconds() <= 86400:
                on_time += 1
            else:
                late.append(s.get("reference"))
    pct = round(100 * on_time / total, 1) if total else None
    return _metric("on_time", pct, total, "shipments.legs (ATA vs ETA)", pct, {"on_time": on_time, "late_refs": late[:10]},
                   f"{on_time}/{total} toimitettua osuutta perillä viimeistään vuorokausi arvioidun ajan jälkeen.",
                   f"{on_time}/{total} delivered legs arrived no later than one day after ETA.")


async def _cost(carrier_id: str, day: date) -> dict:
    cards = [c async for c in db.rate_cards.find({**await operational_query(), "archived": {"$ne": True}, "status": {"$nin": ["pending", "rejected", "revoked"]},
                                                  "service": {"$nin": list(AGENT_SERVICES)}}, NO_ID)]
    cards = [c for c in cards if card_live(c, day) and ref_cost(c) and c.get("source") != "indicative"]
    lanes: dict[tuple, list[dict]] = {}
    for c in cards:
        lanes.setdefault((c["mode"], c["service"], *card_countries(c)), []).append(c)
    indices, compared = [], []
    for key, group in lanes.items():
        own = [c for c in group if c["carrier_id"] == carrier_id and c.get("source") != "indicative"]
        others = [c for c in group if c["carrier_id"] != carrier_id]
        if not own or not others:
            continue
        median = statistics.median(ref_cost(c) for c in group)
        mine = min(ref_cost(c) for c in own)
        idx = round(100 * mine / median, 1)
        indices.append(idx)
        compared.append({"lane": f"{key[0]}:{key[2]}>{key[3]}", "service": key[1], "own_cents": mine, "median_cents": int(median),
                         "cards_on_lane": len(group), "indicative_on_lane": sum(1 for c in group if c.get("source") == "indicative"),
                         "index": idx})
    value = round(statistics.median(indices), 1) if indices else None
    return _metric("cost", value, len(indices), "rate_cards (own contract/partner cards vs lane median)",
                   None if value is None else _clamp(150 - value), {"lanes": compared[:20]},
                   "Oma sopimus- tai kumppanihinta verrattuna saman reitin ja palvelun mediaaniin (100 = mediaani, pienempi = edullisempi). "
                   "Vertailussa ovat voimassa olevat sopimus- ja kumppanihinnat; testi- ja markkina-arviot on rajattu pois.",
                   "Own contract or partner price against the median of the same lane and service (100 = median, lower = cheaper). "
                   "Only valid operational cards are compared; test and indicative market cards are excluded.")


async def _claims(shipments: list[dict]) -> dict:
    ids = [s["id"] for s in shipments]
    n = await db.raw.claims.count_documents({**await operational_query(), "shipment_id": {"$in": ids}}) if ids else 0
    rate = round(100 * n / len(ids), 1) if ids else None
    return _metric("claims", rate, len(ids), "claims on the carrier's shipments", None if rate is None else _clamp(100 - 10 * rate),
                   {"claims": n, "shipments": len(ids)}, f"{n} reklamaatiota {len(ids)} lähetyksessä (per 100 lähetystä).",
                   f"{n} claims on {len(ids)} shipments (per 100 shipments).")


async def _response(carrier_id: str, since: str) -> dict:
    cand_ids = [c["id"] async for c in db.partner_candidates.find({"carrier_id": carrier_id}, {"id": 1})]
    hours, asked = [], 0
    async for o in db.raw.partner_outreach.find({**await operational_query(), "$or": [{"candidate_id": {"$in": cand_ids}}, {"carrier_id": carrier_id}],
                                             "created_at": {"$gte": since}}, {"log": 1}):
        log_ = o.get("log") or []
        sent = next((e["at"] for e in log_ if e.get("kind") == "rfq"), None)
        reply = next((e["at"] for e in log_ if e.get("kind") == "reply"), None)
        if sent:
            asked += 1
        if sent and reply:
            hours.append((parse_iso(reply) - parse_iso(sent)).total_seconds() / 3600)
    med = round(statistics.median(hours), 1) if hours else None
    return _metric("response", med, len(hours), "partner_outreach (RFQ -> first reply)",
                   None if med is None else _clamp(100 - med * 100 / 72), {"asked": asked, "replied": len(hours)},
                   f"Vastasi {len(hours)}/{asked} tarjouspyyntöön, mediaani {med if med is not None else '–'} h.",
                   f"Replied to {len(hours)}/{asked} RFQs, median {med if med is not None else '–'} h.")


async def _invoices(carrier_id: str, since: str) -> dict:
    if "carrier_invoices" not in await db.list_collection_names():
        return _metric("invoice_accuracy", None, 0, "carrier_invoices (s13) not present", None, {},
                       "Ostolaskujen tarkastus (s13) ei ole vielä käytössä.", "Carrier invoice audit (s13) is not in use yet.")
    ok = total = 0
    variances = []
    async for inv in db.raw.carrier_invoices.find({**await operational_query(), "supplier.carrier_id": carrier_id, "created_at": {"$gte": since},
                                               "status": {"$nin": ["duplicate", "rejected", "received", "extracting"]},
                                               "demo": {"$ne": True}}, {"audit": 1}):
        a = inv.get("audit") or {}
        if a.get("within_tolerance") is None:
            continue
        total += 1
        ok += 1 if a["within_tolerance"] else 0
        if a.get("variance_pct") is not None:
            variances.append(float(a["variance_pct"]))
    pct = round(100 * ok / total, 1) if total else None
    return _metric("invoice_accuracy", pct, total, "carrier_invoices.audit (s13)", pct,
                   {"within_tolerance": ok, "median_variance_pct": round(statistics.median(variances), 2) if variances else None},
                   f"{ok}/{total} laskua toleranssin sisällä.", f"{ok}/{total} invoices within tolerance.")


def grade(score: float | None) -> str | None:
    if score is None:
        return None
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"


async def compute(carrier_id: str) -> dict | None:
    carrier = await db.carriers.find_one({"id": carrier_id}, NO_ID)
    if not carrier:
        return None
    day = now().date()
    since = (now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    shipments = await _shipments(carrier_id, since)
    metrics = [_on_time(carrier_id, shipments), await _cost(carrier_id, day), await _claims(shipments),
               await _response(carrier_id, since), await _invoices(carrier_id, since)]
    used = [m for m in metrics if m["enough"]]
    wsum = sum(WEIGHTS[m["key"]] for m in used)
    score = round(sum(WEIGHTS[m["key"]] * m["score"] for m in used) / wsum, 1) if used else None
    row = {"carrier_id": carrier_id, "carrier_name": carrier["name"], "org_type": carrier.get("org_type") or "carrier",
           "score": score, "grade": grade(score), "metrics": metrics, "metrics_used": [m["key"] for m in used],
           "provenance_version": PROVENANCE_VERSION, "coverage_pct": round(100 * wsum, 0), "lookback_days": LOOKBACK_DAYS, "computed_at": now_iso()}
    await db.carrier_scorecards.update_one({"carrier_id": carrier_id}, {"$set": row}, upsert=True)
    return row


async def get(carrier_id: str, refresh: bool = False) -> dict | None:
    row = await db.carrier_scorecards.find_one({"carrier_id": carrier_id}, NO_ID)
    if refresh or not row or row.get("provenance_version") != PROVENANCE_VERSION or (now() - parse_iso(row["computed_at"])).total_seconds() > STALE_HOURS * 3600:
        row = await compute(carrier_id)
    return row


async def list_all(refresh: bool = False) -> list[dict]:
    out = []
    async for c in db.carriers.find({"active": {"$ne": False}}, {"id": 1}):
        row = await get(c["id"], refresh=refresh)
        if row:
            out.append(row)
    return sorted(out, key=lambda r: (r["score"] is None, -(r["score"] or 0), r["carrier_name"]))


@background_loop(seconds=6 * 3600)
async def scorecard_loop():
    await list_all(refresh=True)


# ------------------------------------------------------------------------------------------------- selection
async def rank_carriers(mode: str, origin_country: str, destination_country: str, *, service: str | None = None,
                        on_date: date | None = None, qty: float = 1.0) -> list[dict]:
    """Carriers with a valid card on the country lane, best first. Effective cost = card price minus the allocation
    credit, weighted by the scorecard (75 = neutral when not enough data); carriers at their allocation cap go last."""
    from services import allocations

    day = on_date or now().date()
    o_cc, d_cc = origin_country.upper()[:2], destination_country.upper()[:2]
    best: dict[str, dict] = {}
    async for c in db.rate_cards.find({"mode": mode, "archived": {"$ne": True}, "status": {"$nin": ["pending", "rejected", "revoked"]},
                                       "service": {"$nin": list(AGENT_SERVICES)}, **({"service": service} if service else {})}, NO_ID):
        if not card_live(c, day) or card_countries(c) != (o_cc, d_cc) or not ref_cost(c):
            continue
        if c["carrier_id"] not in best or ref_cost(c) < ref_cost(best[c["carrier_id"]]):
            best[c["carrier_id"]] = c
    out = []
    for cid, card in best.items():
        carrier = await db.carriers.find_one({"id": cid, "active": {"$ne": False}}, NO_ID)
        if not carrier:
            continue
        sc = await get(cid)
        alloc = await allocations.for_option(cid, mode, {"country": o_cc, "unlocode": (card.get("origin") or {}).get("unlocode")},
                                             {"country": d_cc, "unlocode": (card.get("destination") or {}).get("unlocode")}, day, qty=qty)
        cost = ref_cost(card)
        effective = cost - (alloc or {}).get("ranking_credit_cents", 0)
        s = sc["score"] if sc and sc["score"] is not None else 75.0
        value = effective * (1 + 0.004 * (75 - s))
        eur_fi = f"{cost / 100:,.0f}".replace(",", "\u00a0")
        reasons = [{"code": "price", "fi": f"Hinta {eur_fi} € ({card['service']}, {card.get('basis')})",
                    "en": f"Price {cost / 100:,.0f} EUR ({card['service']}, {card.get('basis')})"}]
        if sc and sc["score"] is not None:
            reasons.append({"code": "scorecard", "fi": f"Tuloskortti {sc['score']:.0f}/100 ({sc['grade']})",
                            "en": f"Scorecard {sc['score']:.0f}/100 ({sc['grade']})"})
        else:
            reasons.append({"code": "scorecard_unknown", "fi": "Tuloskortti: liian vähän omaa dataa (neutraali 75)",
                            "en": "Scorecard: not enough own data (neutral 75)"})
        if alloc and alloc["ranking_credit_cents"]:
            reasons.append({"code": "allocation_behind", "fi": f"Allokaatio jäljessä: {alloc['used']:g}/{alloc['committed']:g} {alloc['unit']}",
                            "en": f"Allocation behind pace: {alloc['used']:g}/{alloc['committed']:g} {alloc['unit']}"})
        if alloc and alloc["full"]:
            reasons.append({"code": "allocation_full", "fi": "Allokaation enimmäismäärä täynnä", "en": "Allocation cap reached"})
        out.append({"carrier_id": cid, "name": carrier["name"], "org_type": carrier.get("org_type") or "carrier",
                    "score": sc["score"] if sc else None, "grade": sc["grade"] if sc else None, "rate_card_id": card["id"],
                    "service": card["service"], "basis": card.get("basis"), "unit_cost_cents": cost, "rate_source": card.get("source"),
                    "valid_to": card.get("valid_to"), "allocation": alloc, "reasons": reasons,
                    "_key": (bool(alloc and alloc["full"]), value, cid)})
    out.sort(key=lambda r: (r["service"], r["_key"]))  # never rank one service's price against another's
    for i, r in enumerate(out, 1):
        r.pop("_key", None)
        r["rank"] = i
    return out


async def annotate_options(options: list[dict]) -> None:
    """pricing: measured on-time replaces the carrier's static field when there is enough own data."""
    cache: dict[str, dict | None] = {}
    for o in options:
        cid = o.get("carrier_id")
        if not cid:
            continue
        if cid not in cache:
            row = await get(cid)
            cache[cid] = row
        sc = cache[cid]
        if not sc:
            continue
        o["scorecard"] = {"score": sc["score"], "grade": sc["grade"]}
        ot = next((m for m in sc["metrics"] if m["key"] == "on_time"), None)
        if ot and ot["enough"]:
            o["on_time_pct"] = ot["value"]
            o["on_time_source"] = "own_shipments"


async def selection_inputs(carrier_ids, mode: str, origin: dict, destination: dict, on_date: date | None = None,
                           unit_qty: dict | None = None, company_id: str | None = None) -> dict[str, dict]:
    """For s2's carrier booking ranking (services.carrier_booking.rank_carriers): per carrier the scorecard, the
    allocation the booking would draw from, a small score adjustment and bilingual reasons.
    adjust: +-0.06 from the scorecard (75 = neutral, only with enough own data), +0.05 when an allocation with a
    shortfall penalty is behind pace, -0.25 when the allocation cap is reached."""
    from services import allocations

    day = on_date or now().date()
    out = {}
    for cid in carrier_ids:
        sc = await get(cid)
        alloc = await allocations.for_option(cid, mode, origin or {}, destination or {}, day, unit_qty=unit_qty,
                                             company_id=company_id)
        adjust, reasons = 0.0, []
        if sc and sc.get("score") is not None:
            adjust += 0.06 * max(-1.0, min(1.0, (sc["score"] - 75) / 25))
            reasons.append({"code": "scorecard", "fi": f"Tuloskortti {sc['score']:.0f}/100 ({sc['grade']})",
                            "en": f"Scorecard {sc['score']:.0f}/100 ({sc['grade']})"})
        if alloc and alloc["ranking_credit_cents"]:
            adjust += 0.05
            reasons.append({"code": "allocation_behind", "fi": f"Allokaatio jäljessä: {alloc['used']:g}/{alloc['committed']:g} {alloc['unit']}",
                            "en": f"Allocation behind pace: {alloc['used']:g}/{alloc['committed']:g} {alloc['unit']}"})
        if alloc and alloc["full"]:
            adjust -= 0.25
            reasons.append({"code": "allocation_full", "fi": "Allokaation enimmäismäärä täynnä", "en": "Allocation cap reached"})
        out[cid] = {"scorecard": {"score": sc.get("score"), "grade": sc.get("grade")} if sc else None, "allocation": alloc,
                    "adjust": round(adjust, 4), "reasons": reasons}
    return out
