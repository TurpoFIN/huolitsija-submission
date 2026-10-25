"""Terminal49 JSON:API adapter. Protocol: Terminal49/API docs/openapi.json.

Activation requires a provider account/key. Unit spec examples are not proof of live access.
"""
import os
import hashlib
import httpx
from pymongo.errors import DuplicateKeyError
from urllib.parse import urlparse, urljoin
from core.db import db
from core.util import iso, parse_iso, now_iso
from services.tracking import CarrierAdapter, AdapterStatus

BASE = "https://api.terminal49.com/v2"


class Terminal49(CarrierAdapter):
    key = "terminal49"
    name = "Terminal49"
    standard = "Terminal49 JSON:API v2"

    def configured(self):
        return bool(os.getenv("TERMINAL49_API_KEY", "").strip())

    def status(self):
        return AdapterStatus(self.key, self.name, self.standard, self.configured(), ["TERMINAL49_API_KEY"], BASE)

    def matches(self, leg, carrier_doc):
        return leg.get("mode") == "sea" and self.configured()

    async def _pages(self, c, path, params=None):
        url, seen, out = BASE + path, set(), []
        for _ in range(20):
            if url in seen or urlparse(url).netloc != "api.terminal49.com":
                raise ValueError("Invalid Terminal49 pagination link")
            seen.add(url)
            r = await c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("data") or [])
            nxt = (data.get("links") or {}).get("next")
            if not nxt:
                return out
            url, params = urljoin(url, nxt), None
        raise ValueError("Terminal49 pagination limit reached")

    @staticmethod
    def labelled_test_with_real_reference(shipment):
        """A TEST shipment on a test tenant that tracks a real, publicly sourced container/B/L.

        Only this shape may reach the provider from a test tenant: the record is labelled TEST
        in its customer reference, says it tracks a real reference and cites the public source.
        Demo records never reach the provider.
        """
        refs = shipment.get("references") or {}
        source = str(refs.get("tracking_source_url") or "")
        return (refs.get("real_tracking_reference") is True and source.startswith("https://")
                and "TEST" in str(refs.get("customer_ref") or "").upper())

    async def fetch(self, leg, shipment):
        if any(shipment.get(k) for k in ("demo", "is_demo")):
            return []
        if any(shipment.get(k) for k in ("test", "test_tenant", "is_test")) and not self.labelled_test_with_real_reference(shipment):
            return []
        refs = shipment.get("references") or {}
        containers = leg.get("container_numbers") or refs.get("container_numbers") or []
        number = leg.get("transport_doc_no") or refs.get("bl_no") or (containers[0] if containers else None)
        if not number:
            return []
        kind = "bill_of_lading" if leg.get("transport_doc_no") or refs.get("bl_no") else "container"
        async with httpx.AsyncClient(timeout=20, headers={"Authorization": "Token " + os.getenv("TERMINAL49_API_KEY", "").strip(),
                                                        "Accept": "application/json"}) as c:
            try:
                requests = await self._pages(c, "/tracking_requests", {"filter[request_number]": number})
            except httpx.HTTPStatusError as exc:
                # The provider's free plan permits POST but denies even the GET preflight.
                if exc.response.status_code == 401 and "except for creating tracking requests" in exc.response.text:
                    await self._create_once(c, kind, number, shipment)
                    # Provider's real state: request accepted, every read needs a paid plan.
                    await db.raw.terminal49_requests.update_one(
                        {"_id": hashlib.sha256(f"{kind}:{number}".encode()).hexdigest()},
                        {"$set": {"provider_read": "denied_paid_plan_required", "read_checked_at": now_iso()}})
                    return []
                raise
            matching = [x for x in requests if (x.get("attributes") or {}).get("request_number") == number
                        and (x.get("attributes") or {}).get("request_type") == kind]
            if not matching:
                await self._create_once(c, kind, number, shipment)
                # Provider request may remain pending; never imply it has acquired tracking yet.
                return []
            if not any((x.get("attributes") or {}).get("status") == "created" for x in matching):
                return []
            records = await self._pages(c, "/containers", {"filter[number]": number} if kind == "container" else {"filter[search_by_shipment_number]": number})
            out = []
            for record in records:
                if containers and (record.get("attributes") or {}).get("number") not in containers:
                    continue
                events = await self._pages(c, f"/containers/{record['id']}/transport_events")
                out.extend(x for ev in events if (x := self.normalise(ev, leg)))
            return out

    async def _create_once(self, c, kind, number, shipment):
        # Atomic persistent reservation also survives worker restarts. Ambiguous network
        # failures stay unresolved for reconciliation rather than repeating an external POST.
        identity = hashlib.sha256(f"{kind}:{number}".encode()).hexdigest()
        try:
            await db.raw.terminal49_requests.insert_one({"_id": identity, "request_type": kind,
                "request_number": number, "shipment_id": shipment.get("id"),
                "state": "submitting", "at": now_iso()})
        except DuplicateKeyError:
            return
        try:
            response = await c.post(BASE + "/tracking_requests", json={"data": {"type": "tracking_request", "attributes": {
                "request_type": kind, "request_number": number, "auto_detect_vocc_scac": True}}})
            response.raise_for_status()
            record = response.json().get("data") or {}
            await db.raw.terminal49_requests.update_one({"_id": identity}, {"$set": {
                "state": "submitted", "provider_id": record.get("id"), "http_status": response.status_code}})
        except Exception as exc:
            await db.raw.terminal49_requests.update_one({"_id": identity}, {"$set": {
                "state": "reconciliation_required", "error_type": type(exc).__name__}})
            raise

    @staticmethod
    def normalise(event, leg):
        a = event.get("attributes") or {}
        at = parse_iso(a.get("timestamp"))
        name, loc = a.get("event") or "", a.get("location_locode")
        if not at:
            return None  # creation time does not prove physical occurrence
        mapping = {"vessel_departed": "departed", "vessel_arrived": "arrived", "vessel_discharged": "arrived",
                   "transshipment_arrived": "transshipment", "full_in": "received"}
        code = mapping.get(name.rsplit(".", 1)[-1])
        expected = leg.get("from", {}).get("unlocode") if code in ("departed", "received") else leg.get("to", {}).get("unlocode")
        if not code or not loc or loc != expected:
            return None
        return {"raw_id": event["id"], "code": code, "at": iso(at), "classifier": "EST" if ".estimated." in name else "ACT",
                "label": name, "location": {"unlocode": loc, "name": loc}, "raw": event}
