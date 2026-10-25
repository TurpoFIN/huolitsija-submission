"""17TRACK adapter harvested from rescue/carrier 11725a4.
Configured only by SEVENTEENTRACK_API_KEY. No authenticated live proof yet.
Generic hub scans are informational, not route-leg milestones.
"""
import os
import re
import math
import httpx
from core.db import db
from core.util import NO_ID, iso, parse_iso
from services import tracking
from services.tracking import CarrierAdapter, AdapterStatus

def _env(name):
    return os.environ.get(name, "").strip()

def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None

COURIER_ALIASES = ["ups", "fedex", "dpd", "postnord", "gls", "posti", "usps", "royal mail", "bring", "budbee", "tnt", "dhl",
                   "hermes", "evri", "colissimo", "chronopost", "omniva", "schenker parcel", "matkahuolto", "airmee", "instabox"]
# 17TRACK sub-status (docs "Sub-status of the Shipping Status") -> milestone
ST_MILESTONES = {
    "InTransit_PickedUp": "picked_up",
    "OutForDelivery_Other": "out_for_delivery", "Delivered_Other": "delivered",
    "Exception_Returning": "exception", "Exception_Returned": "exception", "Exception_NoBody": "exception",
    "Exception_Damage": "exception", "Exception_Rejected": "exception", "Exception_Lost": "exception",
    "Exception_Destroyed": "exception", "Exception_Cancel": "exception", "Exception_Other": "exception",
}
# "InTransit_CustomsRequiringInformation: related information is required for clearance"; "*_Security: security, customs
# clearance, or fee issues" -> customs-side hold signals.
ST_CUSTOMS_HOLD = {"InTransit_CustomsRequiringInformation", "Exception_Security", "DeliveryFailure_Security"}


def normalise_17track(item: dict) -> list[dict]:
    """One gettrackinfo `accepted[]` item -> normalised events (all providers' events, oldest first) + EST delivery."""
    number = item.get("number")
    ti = item.get("track_info") or {}
    out = []
    seen = set()
    for prov in (ti.get("tracking") or {}).get("providers") or []:
        pname = ((prov.get("provider") or {}).get("name"))
        for ev in prov.get("events") or []:
            ts = ev.get("time_utc") or ev.get("time_iso")
            if not ts:
                continue
            sub = ev.get("sub_status") or ""
            at = iso(parse_iso(ts))
            rid = f"17t:{number}:{at}:{sub}:{(ev.get('description') or '')[:40]}"
            if rid in seen:
                continue
            seen.add(rid)
            addr = ev.get("address") or {}
            coords = addr.get("coordinates") or {}
            signals = []
            if sub in ST_CUSTOMS_HOLD:
                signals.append({"kind": "customs_hold", "reason": ev.get("description") or sub, "sub_status": sub, "at": at,
                                "country": addr.get("country")})
            out.append({"code": ST_MILESTONES.get(sub), "at": at, "classifier": "ACT",
                        "label": f"{pname + ': ' if pname else ''}{ev.get('description') or sub}"[:200],
                        "location": {"name": addr.get("city") or ev.get("location"), "unlocode": None,
                                     "lat": _number(coords.get("latitude")), "lon": _number(coords.get("longitude")),
                                     "country": addr.get("country")},
                        "signals": signals, "raw_id": rid, "raw": ev})
    edd = ((ti.get("time_metrics") or {}).get("estimated_delivery_date") or {})
    if edd.get("to") or edd.get("from"):
        eta = iso(parse_iso(edd.get("to") or edd.get("from")))
        out.append({"code": "arrived", "at": eta, "classifier": "EST", "label": "17TRACK estimated delivery",
                    "location": {"name": None, "unlocode": None, "lat": None, "lon": None}, "signals": [],
                    "raw_id": f"17t:{number}:eta:{eta}", "raw": edd})
    return sorted(out, key=lambda e: e["at"] or "")


class SeventeenTrack(CarrierAdapter):
    key = "17track"
    name = "17TRACK"
    standard = "17TRACK Tracking API v2.2"

    @property
    def base_url(self) -> str:
        return f"https://api.17track.net/track/{_env('SEVENTEENTRACK_API_VERSION') or 'v2.2'}"

    def configured(self) -> bool:
        return bool(_env("SEVENTEENTRACK_API_KEY"))

    def status(self) -> AdapterStatus:
        return AdapterStatus(self.key, self.name, self.standard, self.configured(), ["SEVENTEENTRACK_API_KEY"], self.base_url)

    def matches(self, leg: dict, carrier_doc: dict | None) -> bool:
        if leg.get("mode") == "sea" and not leg.get("tracking_no"):
            return False
        integ = ((carrier_doc or {}).get("integration") or {}).get("type")
        name = f" {(leg.get('carrier') or (carrier_doc or {}).get('name') or '').lower()} "
        return self.configured() and (integ == "17track" or any(re.search(rf"\b{re.escape(a)}\b", name) for a in COURIER_ALIASES))

    async def _post(self, c: httpx.AsyncClient, op: str, body: list[dict]) -> dict:
        r = await c.post(f"{self.base_url}/{op}", json=body,
                         headers={"17token": _env("SEVENTEENTRACK_API_KEY"), "Content-Type": "application/json"})
        if r.status_code in (401, 403):
            raise PermissionError(f"17TRACK rejected the API key ({r.status_code}).")
        r.raise_for_status()
        data = r.json()
        errs = (data.get("data") or {}).get("errors")
        if data.get("code") not in (0, None) or errs:
            raise ValueError(f"17TRACK {op}: {errs or data.get('code')}")
        return data["data"]

    async def track(self, number: str, carrier: int | None = None) -> list[dict]:
        item = {"number": number, **({"carrier": carrier} if carrier else {})}
        async with httpx.AsyncClient(timeout=30) as c:
            reg = await self._post(c, "register", [item])
            for rej in reg.get("rejected") or []:
                code = (rej.get("error") or {}).get("code")
                if code != -18019901:  # -18019901: the number is already registered -> just read it
                    raise ValueError(f"17TRACK register {number}: {rej.get('error')}")
            info = await self._post(c, "gettrackinfo", [item])
        if info.get("rejected"):
            raise ValueError("17TRACK tracking lookup rejected")
        out = []
        for acc in info.get("accepted") or []:
            if acc.get("number") == number:
                out += normalise_17track(acc)
        return out

    async def fetch(self, leg: dict, shipment: dict) -> list[dict]:
        if any(shipment.get(k) for k in ("demo", "test", "test_tenant", "is_demo", "is_test")):
            return []
        refs = shipment.get("references") or {}
        number = leg.get("tracking_no") or leg.get("transport_doc_no") or refs.get("tracking_no") or refs.get("awb_no")
        if not number:
            return []
        carrier_doc = await db.carriers.find_one({"id": leg["carrier_id"]}, NO_ID) if leg.get("carrier_id") else None
        code = ((carrier_doc or {}).get("integration") or {}).get("seventeentrack_carrier")
        return await self.track(number, int(code) if code else None)
