"""Carrier tracking adapters. Inactive until the carrier's API credentials are configured (env), then polled by the
durable job scheduler every 10 minutes for legs that carry a transport document / container / tracking number.

Adapters
- DCSA Track & Trace v2.2 (Maersk, Hapag-Lloyd, CMA CGM, ONE, Evergreen, ZIM, HMM, Yang Ming ... any carrier that
  publishes a DCSA T&T 2.2 endpoint). Per carrier (SCAC code, e.g. MAEU, HLCU, CMDU, ONEY):
      DCSA_<SCAC>_API_KEY        required, switches the adapter on
      DCSA_<SCAC>_BASE_URL       optional, defaults below (path up to and excluding /events)
      DCSA_<SCAC>_API_SECRET     optional second credential (Hapag-Lloyd client secret)
      DCSA_<SCAC>_KEY_HEADER     optional header name override
- DHL Shipment Tracking – Unified API (https://api-eu.dhl.com/track/shipments): DHL_API_KEY.

Normalised event: {code, label, at, classifier ACT|EST|PLN, location{name, unlocode, lat, lon}, eta, raw_id, raw}
Milestone mapping (DCSA): GTIN at POL -> received, DEPA at a leg's origin -> departed, ARRI at an intermediate port ->
transshipment, ARRI/DISC at the final port -> arrived, GTOT is informational and cannot prove delivery dispatch.
Only ACTual events become milestones (source "carrier_api"); ESTimated ARRI at a leg's destination updates that leg's ETA.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

from core.db import db, register_index
from core.util import NO_ID, iso, new_id, now_iso, parse_iso
from services import geo

log = logging.getLogger("tracking")
register_index("tracking_events", [("shipment_id", 1), ("raw_id", 1)])

DCSA_PROFILES = {
    "MAEU": {"name": "Maersk", "aliases": ["maersk", "sealand"], "base": "https://api.maersk.com/track-and-trace-private",
             "key_header": "Consumer-Key"},
    "HLCU": {"name": "Hapag-Lloyd", "aliases": ["hapag"], "base": "https://api.hlag.com/hlag/external/v2",
             "key_header": "X-IBM-Client-Id", "secret_header": "X-IBM-Client-Secret"},
    "CMDU": {"name": "CMA CGM", "aliases": ["cma cgm", "cma-cgm", "cmacgm", "anl", "apl"],
             "base": "https://apis.cma-cgm.net/operation/trackandtrace/v1", "key_header": "KeyId"},
    "MSCU": {"name": "MSC", "aliases": ["msc "], "base": "", "key_header": "apikey"},
    "ONEY": {"name": "Ocean Network Express", "aliases": ["one ", "ocean network express"], "base": "", "key_header": "apikey"},
    "EGLV": {"name": "Evergreen", "aliases": ["evergreen"], "base": "", "key_header": "apikey"},
    "ZIMU": {"name": "ZIM", "aliases": ["zim"], "base": "", "key_header": "apikey"},
    "HDMU": {"name": "HMM", "aliases": ["hmm", "hyundai"], "base": "", "key_header": "apikey"},
    "YMLU": {"name": "Yang Ming", "aliases": ["yang ming"], "base": "", "key_header": "apikey"},
}
DHL_URL = "https://api-eu.dhl.com/track/shipments"


@dataclass
class AdapterStatus:
    key: str
    name: str
    standard: str
    configured: bool
    env: list[str] = field(default_factory=list)
    base_url: str | None = None


class CarrierAdapter:
    key = "base"
    name = "Carrier"
    standard = ""

    def configured(self) -> bool:
        return False

    def status(self) -> AdapterStatus:
        return AdapterStatus(self.key, self.name, self.standard, self.configured())

    def matches(self, leg: dict, carrier_doc: dict | None) -> bool:
        return False

    async def fetch(self, leg: dict, shipment: dict) -> list[dict]:
        raise NotImplementedError


class DCSATrackAndTrace(CarrierAdapter):
    standard = "DCSA Track & Trace 2.2"

    def __init__(self, scac: str, profile: dict):
        self.scac = scac
        self.key = f"dcsa:{scac}"
        self.name = profile["name"]
        self.profile = profile

    def _env(self, suffix: str) -> str:
        return os.environ.get(f"DCSA_{self.scac}_{suffix}", "").strip()

    @property
    def base_url(self) -> str:
        return (self._env("BASE_URL") or self.profile.get("base") or "").rstrip("/")

    def configured(self) -> bool:
        return bool(self._env("API_KEY") and self.base_url)

    def status(self) -> AdapterStatus:
        return AdapterStatus(self.key, self.name, self.standard, self.configured(),
                             [f"DCSA_{self.scac}_API_KEY", f"DCSA_{self.scac}_BASE_URL"], self.base_url or None)

    def matches(self, leg: dict, carrier_doc: dict | None) -> bool:
        if leg.get("mode") != "sea":
            return False
        code = ((carrier_doc or {}).get("code") or "").upper()
        if code == self.scac:
            return True
        name = f" {(leg.get('carrier') or (carrier_doc or {}).get('name') or '').lower()} "
        return any(a in name for a in self.profile["aliases"])

    def _headers(self) -> dict:
        h = {"Accept": "application/json", self._env("KEY_HEADER") or self.profile["key_header"]: self._env("API_KEY")}
        if self.profile.get("secret_header") and self._env("API_SECRET"):
            h[self.profile["secret_header"]] = self._env("API_SECRET")
        return h

    def query_params(self, leg: dict, shipment: dict) -> dict | None:
        refs = shipment.get("references") or {}
        doc = leg.get("transport_doc_no") or refs.get("bl_no")
        containers = leg.get("container_numbers") or refs.get("container_numbers") or []
        booking = refs.get("booking_no")
        if doc:
            return {"transportDocumentReference": doc}
        if containers:
            return {"equipmentReference": containers[0]}
        if booking:
            return {"carrierBookingReference": booking}
        return None

    async def fetch(self, leg: dict, shipment: dict) -> list[dict]:
        params = self.query_params(leg, shipment)
        if not params:
            return []
        params.update({"eventType": "TRANSPORT,EQUIPMENT", "limit": 100})
        out, cursors = [], set()
        async with httpx.AsyncClient(timeout=20) as c:
            for _ in range(20):
                r = await c.get(f"{self.base_url}/events", params=params, headers=self._headers())
                if r.status_code in (401, 403):
                    raise PermissionError(f"{self.name} rejected the API credentials ({r.status_code}).")
                r.raise_for_status()
                data = r.json()
                events = data if isinstance(data, list) else data.get("events") or []
                out.extend(e for e in (self.normalise(ev, leg, shipment) for ev in events) if e)
                cursor = r.headers.get("Next-Page-Cursor")
                if not cursor:
                    return out
                if cursor in cursors:
                    raise ValueError("DCSA repeated pagination cursor")
                cursors.add(cursor)
                params["cursor"] = cursor
        raise ValueError("DCSA pagination limit reached; refusing an incomplete feed")

    @staticmethod
    def normalise(ev: dict, leg: dict, shipment: dict) -> dict | None:
        tc = ev.get("transportCall") or {}
        loc = tc.get("location") or tc.get("facility") or ev.get("eventLocation") or {}
        unloc = (tc.get("UNLocationCode") or loc.get("UNLocationCode") or "").upper() or None
        at = ev.get("eventDateTime")
        classifier = ev.get("eventClassifierCode")
        if classifier not in ("ACT", "EST", "PLN") or not at or not parse_iso(at):
            return None
        etype = ev.get("eventType")
        code = None
        if etype == "TRANSPORT":
            t = ev.get("transportEventTypeCode")
            if t == "DEPA" and unloc == leg.get("from", {}).get("unlocode"):
                code = "departed"
            elif t == "ARRI" and unloc == leg.get("to", {}).get("unlocode"):
                code = "arrived" if unloc and unloc == (shipment.get("legs") or [{}])[-1].get("to", {}).get("unlocode") else "transshipment"
                final_sea = [lg for lg in shipment.get("legs") or [] if lg["mode"] == "sea"]
                if final_sea and unloc == final_sea[-1]["to"].get("unlocode"):
                    code = "arrived"
        elif etype == "EQUIPMENT":
            t = ev.get("equipmentEventTypeCode")
            first_sea = next((lg for lg in shipment.get("legs") or [] if lg["mode"] == "sea"), None)
            last_sea = next((lg for lg in reversed(shipment.get("legs") or []) if lg["mode"] == "sea"), None)
            if t == "GTIN" and first_sea and unloc == first_sea["from"].get("unlocode"):
                code = "received"
            elif t == "DISC" and last_sea and unloc == last_sea["to"].get("unlocode"):
                code = "arrived"
            # Gate-out is not proof of last-mile delivery dispatch. Retain it as a carrier observation.
        if not code or not at:
            return None
        lref = geo.lookup_code(unloc) if unloc else None
        vessel = (tc.get("vessel") or {})
        return {"code": code, "at": iso(parse_iso(at)), "classifier": classifier,
                "label": f"{ev.get('transportEventTypeCode') or ev.get('equipmentEventTypeCode')} {loc.get('locationName') or unloc or ''}".strip(),
                "location": {"name": (lref or {}).get("name") or loc.get("locationName") or unloc, "unlocode": unloc,
                             "lat": (lref or {}).get("lat"), "lon": (lref or {}).get("lon")},
                "vessel": {"name": vessel.get("vesselName") or vessel.get("name"), "imo": vessel.get("vesselIMONumber")} if vessel else None,
                "raw_id": ev.get("eventID") or f"{etype}:{code}:{unloc}:{at}:{classifier}", "raw": ev}


class DHLUnifiedTracking(CarrierAdapter):
    key = "dhl"
    name = "DHL"
    standard = "DHL Shipment Tracking – Unified API"

    def configured(self) -> bool:
        return bool(os.environ.get("DHL_API_KEY", "").strip())

    def status(self) -> AdapterStatus:
        return AdapterStatus(self.key, self.name, self.standard, self.configured(), ["DHL_API_KEY"], DHL_URL)

    def matches(self, leg: dict, carrier_doc: dict | None) -> bool:
        integ = ((carrier_doc or {}).get("integration") or {}).get("type")
        return integ == "dhl" or "dhl" in (leg.get("carrier") or (carrier_doc or {}).get("name") or "").lower()

    async def fetch(self, leg: dict, shipment: dict) -> list[dict]:
        number = leg.get("transport_doc_no") or (shipment.get("references") or {}).get("awb_no")
        if not number:
            return []
        return await self.track(number)

    async def track(self, number: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(DHL_URL, params={"trackingNumber": number},
                            headers={"DHL-API-Key": os.environ.get("DHL_API_KEY", "").strip(), "Accept": "application/json"})
        if r.status_code in (401, 403):
            raise PermissionError(f"DHL rejected the API key ({r.status_code}).")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return self.normalise_response(r.json())

    @staticmethod
    def normalise_response(body):
        out = []
        for sh in body.get("shipments") or []:
            eta = sh.get("estimatedTimeOfDelivery")
            evs = sh.get("events") or []
            seen_transit = False
            for ev in sorted(evs, key=lambda e: e.get("timestamp") or ""):
                sc = ev.get("statusCode")
                code = {"transit": "departed", "delivered": "delivered", "failure": "exception"}.get(sc)
                if code == "departed":
                    if re.search(r"\bout for delivery\b", " ".join(str(ev.get(k) or "") for k in ("status", "description")), re.I):
                        code = "out_for_delivery"
                    elif seen_transit:
                        code = None
                    seen_transit = True
                addr = ((ev.get("location") or {}).get("address") or {})
                out.append({"code": code, "at": iso(parse_iso(ev.get("timestamp"))) if ev.get("timestamp") else None, "classifier": "ACT",
                            "label": ev.get("description") or ev.get("status") or sc,
                            "location": {"name": addr.get("addressLocality"), "unlocode": None, "lat": None, "lon": None,
                                         "country": addr.get("countryCode")},
                            "eta": iso(parse_iso(eta)) if eta else None, "raw_id": f"dhl:{sh.get('id')}:{ev.get('timestamp')}:{sc}",
                            "raw": ev})
            if eta and parse_iso(eta):
                out.append({"code": "arrived", "at": iso(parse_iso(eta)), "classifier": "EST", "label": "DHL estimated delivery",
                            "location": {"name": None, "unlocode": None, "lat": None, "lon": None},
                            "raw_id": f"dhl:{sh.get('id')}:eta:{eta}", "raw": {"estimatedTimeOfDelivery": eta}})
        return out


ADAPTERS: list[CarrierAdapter] = [DCSATrackAndTrace(scac, p) for scac, p in DCSA_PROFILES.items()] + [DHLUnifiedTracking()]


def adapter_statuses() -> list[dict]:
    from services.visibility.carrier_feeds import Terminal49
    from services.visibility.courier_feeds import SeventeenTrack
    return [a.status().__dict__ for a in [*ADAPTERS, Terminal49(), SeventeenTrack()]]


async def adapter_for(leg: dict) -> CarrierAdapter | None:
    carrier_doc = await db.carriers.find_one({"id": leg["carrier_id"]}, NO_ID) if leg.get("carrier_id") else None
    for a in ADAPTERS:
        if a.configured() and a.matches(leg, carrier_doc):
            return a
    integ = ((carrier_doc or {}).get("integration") or {}).get("type")
    if integ == "dcsa" and carrier_doc.get("code") and leg.get("mode") == "sea":
        # any other DCSA carrier: DCSA_<CODE>_API_KEY + DCSA_<CODE>_BASE_URL switch it on
        code = re.sub(r"[^A-Z0-9]", "", carrier_doc["code"].upper())
        return DCSATrackAndTrace(code, {"name": carrier_doc.get("name") or code, "aliases": [], "base": "", "key_header": "apikey"})
    # Carrier-agnostic provider is a fallback after a configured direct carrier feed.
    from services.visibility.carrier_feeds import Terminal49
    from services.visibility.courier_feeds import SeventeenTrack
    for fallback in (Terminal49(), SeventeenTrack()):
        if fallback.matches(leg, carrier_doc):
            return fallback
    return None


async def poll_shipment(s: dict) -> int:
    """Fetch carrier events for every configured leg; store new ones and turn ACTual events into milestones."""
    from services import shipments as svc

    n = 0
    for leg in s.get("legs") or []:
        if leg.get("ata"):
            continue
        a = await adapter_for(leg)
        if not a or not a.configured():
            continue
        try:
            events = await a.fetch(leg, s)
        except Exception as exc:  # noqa: BLE001
            log.warning("tracking %s %s failed: %s", a.key, s.get("reference"), exc)
            await db.shipments.update_one({"id": s["id"]}, {"$set": {"tracking.last_error": str(exc)[:300], "tracking.last_polled_at": now_iso()}})
            continue
        from services.agents.tracking_agent import store_feed_event
        for ev in events:
            # Persist all classifications, but enqueue only actual movement or a carrier ETA.
            await db.tracking_events.update_one({"shipment_id": s["id"], "raw_id": ev["raw_id"]},
                {"$setOnInsert": {"id": new_id(), "shipment_id": s["id"], "company_id": s["company_id"],
                                  **ev, "source": "carrier_api", "provider": a.key}}, upsert=True)
            code = ev.get("code")
            if ev.get("classifier") == "EST" and code in ("arrived", "transshipment"):
                code = "eta"
            elif ev.get("classifier") != "ACT" or not code:
                continue
            _, created = await store_feed_event(a.key, {"event_id": f"{s['id']}:{leg['seq']}:{ev['raw_id']}",
                "shipment_id": s["id"], "leg_seq": leg["seq"], "code": code, "at": ev["at"],
                "eta": ev["at"] if code == "eta" else ev.get("eta"), "location": ev.get("location"),
                "vessel": ev.get("vessel"), "note": ev.get("label")}, verified=True)
            n += int(created)
        await db.shipments.update_one({"id": s["id"]}, {"$set": {"tracking.last_polled_at": now_iso(), "tracking.adapter": a.key,
                                                                 "tracking.last_error": None}})
    return n


async def poll_all() -> int:
    if not any(a.configured() for a in ADAPTERS) and not os.getenv("TERMINAL49_API_KEY") and not os.getenv("SEVENTEENTRACK_API_KEY"):
        return 0
    total = 0
    async for s in db.shipments.find({"status": {"$in": ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery"]}}, NO_ID):
        total += await poll_shipment(s)
    return total
