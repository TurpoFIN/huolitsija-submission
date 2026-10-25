"""Transparent kinematic ETA, not a trained/calibrated probabilistic model.

No position/speed -> unavailable, never a fake prediction from the timetable.
Ranges are speed-sensitivity bounds (±20%); confidence is qualitative input quality.
"""
import asyncio
from datetime import timedelta
from statistics import median

from core.db import db, register_index
from core.util import NO_ID, iso, now, now_iso, parse_iso
from services import geo, positions

register_index("eta_predictions", [("shipment_id", 1), ("leg_seq", 1)], unique=True)
register_index("port_congestion", "unlocode", unique=True)


def congestion_from_calls(calls, at=None):
    """Observed arrival-vs-reported-ETA delays; not invented anchorage waiting time."""
    at = at or now()
    delays, ids = [], []
    for call in calls:
        for area in call.get("portAreaDetails") or []:
            eta, ata = parse_iso(area.get("eta")), parse_iso(area.get("ata"))
            if not eta or not ata or not at - timedelta(days=7) <= ata <= at:
                continue
            delay = (ata - eta).total_seconds() / 3600
            if -168 <= delay <= 168 and call.get("portCallId") not in ids:
                delays.append(delay)
                ids.append(call.get("portCallId"))
    return {"state": "observed" if len(delays) >= 3 else "insufficient_data", "samples": len(delays),
            "arrival_delay_h_median": round(median(delays), 2) if delays else None, "port_call_ids": ids,
            "source": "digitraffic_port_calls", "metric": "arrival_vs_reported_eta", "at": iso(at)}


async def port_congestion(unlocode):
    if not unlocode or not unlocode.startswith("FI"):
        return {"state": "unavailable", "reason": "no_port_call_feed", "unlocode": unlocode}
    cached = await db.raw.port_congestion.find_one({"unlocode": unlocode}, NO_ID)
    if cached and (now() - parse_iso(cached["at"])).total_seconds() < 900:
        return cached
    row = {**congestion_from_calls(await positions.port_calls(unlocode)), "unlocode": unlocode}
    await db.raw.port_congestion.update_one({"unlocode": unlocode}, {"$set": row}, upsert=True)
    return row


async def predict_leg(leg, fix=None):
    at = now()
    missing = lambda reason: {"state": "unavailable", "reason": reason, "at": iso(at), "leg_seq": leg.get("seq")}
    if leg.get("ata"):
        return missing("already_arrived")
    fix = fix if fix is not None else await positions.live_fix(leg)
    if not fix or not parse_iso(fix.get("at")):
        return missing("no_fresh_position")
    age = (at - parse_iso(fix["at"])).total_seconds()
    if not 0 <= age <= (180 if leg.get("mode") == "air" else 900):
        return missing("stale_position")
    speed = fix.get("speed_kn")
    speed_kmh = speed * 1.852 if speed is not None else fix.get("speed_kmh")
    if speed_kmh is None or speed_kmh < 3 or (leg.get("mode") == "air" and speed_kmh < 100):
        return missing("stationary_or_missing_speed")
    destination = leg.get("to") or {}
    if not all(destination.get(k) is not None for k in ("lat", "lon")):
        return missing("missing_destination")
    start, end = [fix["lon"], fix["lat"]], [destination["lon"], destination["lat"]]
    if leg.get("mode") == "sea":
        import searoute
        try:
            route = await asyncio.to_thread(searoute.searoute, start, end, units="km")
            distance = route["properties"]["length"]
            route_method = "searoute"
        except Exception:
            return missing("sea_route_unavailable")
    else:
        distance = geo.haversine_km(start, end)
        route_method = "great_circle"  # Explicit lower-bound routing model, no airspace prediction.
    congestion = await port_congestion(destination.get("unlocode")) if leg.get("mode") == "sea" else {"state": "not_applicable"}
    delay = max(0, congestion["arrival_delay_h_median"]) if congestion.get("state") == "observed" else 0
    sailing_h = distance / speed_kmh
    eta = parse_iso(fix["at"]) + timedelta(hours=sailing_h + delay)
    if eta <= at:
        return missing("observation_at_destination_requires_arrival_evidence")
    return {"state": "predicted", "leg_seq": leg.get("seq"), "eta": iso(eta),
            "earliest": iso(max(at, parse_iso(fix["at"]) + timedelta(hours=sailing_h / 1.2))),
            "latest": iso(parse_iso(fix["at"]) + timedelta(hours=sailing_h / .8 + delay)),
            "confidence": "limited", "calibrated": False, "method": "constant_speed_v1", "at": iso(at),
            "inputs": {"position": fix, "distance_km": round(distance, 2), "routing": route_method,
                       "speed_kmh": round(speed_kmh, 2), "port_delay": congestion,
                       "delay_applied_h": delay if congestion.get("state") == "observed" else None,
                       "speed_sensitivity_pct": 20, "observation_age_s": round(age),
                       "limitations": ["constant_speed", "not_statistically_calibrated", "no_weather_or_airspace_forecast"]}}


async def predict_shipment(s):
    leg = next((lg for lg in s.get("legs", []) if not lg.get("ata") and lg.get("atd")), None)
    result = await predict_leg(leg) if leg else {"state": "unavailable", "reason": "no_active_leg", "at": now_iso()}
    # This is an active-leg arrival, never mislabeled door delivery on a multimodal shipment.
    result["scope"] = "active_leg_arrival"
    if leg:
        await db.raw.eta_predictions.update_one({"shipment_id": s["id"], "leg_seq": leg["seq"]},
                                               {"$set": {**result, "company_id": s.get("company_id")}}, upsert=True)
    await db.shipments.update_one({"id": s["id"]}, {"$set": {"eta_prediction": result}})
    return result
